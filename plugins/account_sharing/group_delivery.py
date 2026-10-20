"""Per-account delivery intent and full-roster diagnostics, without subscribing to events."""

import asyncio
import copy
from urllib.parse import quote

from .validation import identifier, identity_key


class GroupDelivery:
    def __init__(self, service):
        self.service = service
        self.locks = {}
        self.modes = {}

    async def mode(self, bot, group, resource):
        key = identity_key(group, resource)
        cache_key = (bot, key)
        if cache_key not in self.modes:
            saved = await self.service.store.get(bot, 'group_delivery', key)
            self.modes.setdefault(
                cache_key,
                saved or {'id': key, 'group': group, 'resource': resource, 'desired': 'private', 'actual': 'private', 'auto_restore': False, 'report': {}},
            )
            # A persisted decision is never a fresh authorization to send.
            self.modes[cache_key]['actual'] = 'private'
        return self.modes[cache_key]

    async def report(self, bot, group, resource):
        service = self.service
        acl = service.access()
        framework_guard = service.framework_guard(bot)
        await service.sync_access_resources(bot)
        roster = await acl.sync_roster(bot, group)
        state = await acl.snapshot(bot)
        version = state['version']
        rows, guards = [], []
        for member in roster['members']:
            allowed = await service.framework_context_allowed(bot, group, member['member'])
            invoke = await acl.decide(bot, 'account_sharing', group, member['member'], action='invoke', framework_allowed=allowed)
            view = await acl.decide(bot, 'account_sharing', group, member['member'], resource, framework_allowed=allowed)
            guards.extend([invoke, view])
            rows.append(
                {
                    **member,
                    'eligible': invoke['allowed'] and view['allowed'],
                    'invoke_reason': invoke['reason'],
                    'view_reason': view['reason'],
                    'action': '检查授权、期限、成员核验与拒绝规则',
                }
            )
        own, own_error = await acl.reader.request(bot, f'/v2/groups/{quote(group, safe="")}/bot_state') if acl.reader else (None, 'unavailable')
        own_member = own.get('member_openid') if own else None
        own_confirmed = isinstance(own_member, str) and any(row['member'] == own_member for row in rows)
        current_state = await acl.snapshot(bot)
        stable = current_state['version'] == version == roster['version']
        stable &= current_state['rosters'].get(group, {}).get('id') == roster['id']
        stable &= framework_guard == service.framework_guard(bot)
        reason = (
            'event_integration_unavailable'
            if not acl.lifecycle_available
            else 'roster_incomplete'
            if not roster['complete']
            else 'own_bot_unverified'
            if own_error or not own_confirmed
            else 'state_changed'
            if not stable
            else 'member_ineligible'
            if not all(row['eligible'] for row in rows)
            else 'eligible'
        )
        return {
            'eligible': reason == 'eligible',
            'reason': reason,
            'roster_complete': roster['complete'],
            'roster_reason': roster['reason'],
            'own_bot_confirmed': own_confirmed,
            'members': rows,
            'version': version,
            'roster_id': roster['id'],
            'guards': guards,
            'framework_guard': framework_guard,
            'qualification_version': [state['qualification_version'], state['qualification_groups'].get(group, 0)],
            'checked_at': service.clock(),
            'group': group,
            'resource': resource,
        }

    async def configure(self, bot, data):
        if not isinstance(data, dict) or set(data) - {'group', 'resource', 'enabled', 'auto_restore'}:
            raise ValueError('群发配置只接受 group、resource、enabled、auto_restore；不提供强制群发')
        group, resource = identifier(data.get('group')), identifier(data.get('resource'))
        for field in ('enabled', 'auto_restore'):
            if field in data and not isinstance(data[field], bool):
                raise ValueError('模式开关须为布尔值')
        account = await self.service.store.get(bot, 'accounts', resource)
        if not account or not account['enabled']:
            raise ValueError('请选择已启用的账户')
        async with self.locks.setdefault((bot, group, resource), asyncio.Lock()):
            mode = await self.mode(bot, group, resource)
            self.invalidate_stale_mode(bot, mode)
            before = copy.deepcopy(mode)
            if 'auto_restore' in data:
                mode['auto_restore'] = data['auto_restore']
            if 'enabled' in data:
                mode['desired'] = 'group' if data['enabled'] else 'private'
            mode['actual'] = 'private'
            if mode['desired'] == 'group':
                report = await self.report(bot, group, resource)
                if report['eligible'] and (data.get('enabled') is True or mode['auto_restore'] or before['actual'] == 'group'):
                    mode['actual'] = 'group'
                mode['report'] = report
            else:
                mode['report'] = {}
            try:
                await self.service.store.put(bot, 'group_delivery', mode['id'], mode)
            except Exception:
                mode['actual'] = 'private'
                raise
            recovered = mode['report'].get('eligible') and before['report'].get('eligible') is False
            if recovered:
                result = await self.service.operation(
                    bot, 'group_recovered', group=group, resource=resource, mode=mode['actual'], delivery='not_requested', notification='pending'
                )
                await self.service.notify_result(bot, result)
            elif any(mode[k] != before[k] for k in ('desired', 'actual', 'auto_restore')) or mode['report'].get('reason') != before['report'].get('reason'):
                result = await self.service.operation(
                    bot,
                    'group_mode',
                    group=group,
                    resource=resource,
                    mode=mode['actual'],
                    mode_requested=mode['desired'],
                    reason=mode['report'].get('reason', ''),
                    delivery='not_requested',
                    notification='pending',
                )
                await self.service.notify_result(bot, result)
            return self.public(mode, bot)

    def public(self, mode, bot):
        self.invalidate_stale_mode(bot, mode)
        result = copy.deepcopy(mode)
        result['report'].pop('guards', None)
        result['report'].pop('framework_guard', None)
        if not self.can_send(bot, mode):
            result['actual'] = 'private'
        return result

    def invalidate_stale_mode(self, bot, mode):
        report = mode.get('report', {})
        acl = self.service.access()
        continuous = (
            acl.qualification_revision(bot, mode['group']) == report.get('qualification_version')
            and report.get('framework_guard') == self.service.framework_guard(bot)
            and all(g.get('valid_until') is None or acl.clock() < g['valid_until'] for g in report.get('guards', []))
        )
        if mode['actual'] == 'group' and not continuous:
            mode['actual'] = 'private'
            mode['report'].update(eligible=False, reason='state_changed')

    def can_send(self, bot, mode):
        acl = self.service.access()
        report = mode.get('report', {})
        return bool(
            self.service.active
            and mode['desired'] == 'group'
            and mode['actual'] == 'group'
            and report.get('eligible')
            and acl.roster_current(bot, mode['group'], report.get('version'), report.get('roster_id'))
            and report.get('framework_guard') == self.service.framework_guard(bot)
            and report.get('guards')
            and all(acl.decision_current(bot, g) for g in report['guards'])
        )

    async def list(self, bot):
        saved = await self.service.store.all(bot, 'group_delivery')
        return [self.public(await self.mode(bot, row['group'], row['resource']), bot) for row in saved]

    async def try_send(self, event, context, resource, qa_guard):
        bot, group = event.appid, context['group']
        mode = await self.mode(bot, group, resource)
        if mode['desired'] != 'group':
            return False
        await self.configure(bot, {'group': group, 'resource': resource})
        decision = await self.service.query_decision(event, context, resource)
        content = await self.service.query_content(bot, resource, None)
        if (
            not content
            or not self.can_send(bot, mode)
            or not self.service.access().decision_current(bot, decision)
            or not self.service.current_rules(bot, qa_guard)
        ):
            return False
        try:
            response = await event.reply(content)
            ok = bool(response[0])
        except Exception:
            ok = False
        await self.service.operation(
            bot, 'delivery', group=group, member=context['member'], resource=resource, delivery='sent' if ok else 'failed', target='group'
        )
        # A failed/ambiguous public send is not retried with another destination.
        if not ok:
            await event.reply('群发未确认成功，请稍后重新查询；未自动重发。')
        return True
