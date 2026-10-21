"""Per-account group delivery intent on top of the authorization whole-group report.

The report (every roster member, both Bots included, invoke and view) comes from
the authorization module; this plugin never reads the platform roster or
authorization versions. A persisted mode is never a fresh permission to send:
after a restart the in-memory report is gone and delivery is private.
"""

import asyncio
import copy

from .validation import identifier, identity_key


class GroupDelivery:
    def __init__(self, service):
        self.service = service
        self.locks = {}
        self.modes = {}
        self.reports = {}

    async def mode(self, bot, group, resource):
        key = identity_key(group, resource)
        cache_key = (bot, key)
        if cache_key not in self.modes:
            saved = await self.service.store.get(bot, 'group_delivery', key)
            self.modes.setdefault(
                cache_key,
                saved or {'id': key, 'group': group, 'resource': resource, 'desired': 'private', 'actual': 'private', 'auto_restore': False, 'report': {}},
            )
            self.modes[cache_key]['actual'] = 'private'
        return self.modes[cache_key]

    def send_context(self, group, resource):
        """The exact context of an account group send: invoke and view for everyone."""
        return dict(consumer=self.service.CONSUMER, function=self.service.FUNCTION, group=group, resource=resource, actions=['invoke', 'view'])

    async def report(self, bot, group, resource):
        acl = await self.service.ensure(bot)
        context = self.send_context(group, resource)
        report = await acl.audience_report(bot, context['consumer'], context['function'], group, resource, actions=context['actions'])
        # Accounts need explicit grants for everyone; admission or roles never count.
        implicit = [row['member'] for row in report['members'] if any(a['allowed'] and a['reason'] != 'explicit' for a in row['actions'].values())]
        if implicit:
            report['failures'] += [{'member': m, 'reasons': {'invoke': 'explicit_required', 'view': 'explicit_required'}} for m in implicit]
            report.update(eligible=False, reason='member_ineligible')
        return report

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
                if not data['enabled']:
                    # Turning group delivery off also clears any restore intent.
                    mode['auto_restore'] = False if 'auto_restore' not in data else mode['auto_restore']
            mode['actual'] = 'private'
            key = (bot, mode['id'])
            if mode['desired'] == 'group':
                report = await self.report(bot, group, resource)
                self.reports[key] = report
                if report['eligible'] and (data.get('enabled') is True or mode['auto_restore'] or before['actual'] == 'group'):
                    mode['actual'] = 'group'
                mode['report'] = self.service.access().audience.owner_view(report)
            else:
                self.reports.pop(key, None)
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
        if not self.can_send(bot, mode):
            result['actual'] = 'private'
        return result

    def invalidate_stale_mode(self, bot, mode):
        if mode['actual'] == 'group' and not self.can_send(bot, mode):
            mode['actual'] = 'private'
            mode['report'].update(eligible=False, reason='state_changed')

    def can_send(self, bot, mode):
        report = self.reports.get((bot, mode['id']))
        module = self.service.access_provider()
        return bool(
            self.service.active
            and module is not None
            and mode['desired'] == 'group'
            and mode['actual'] == 'group'
            and report
            and report['eligible']
            and module.report_current(bot, report, **self.send_context(mode['group'], mode['resource']))
        )

    async def list(self, bot):
        saved = await self.service.store.all(bot, 'group_delivery')
        return [self.public(await self.mode(bot, row['group'], row['resource']), bot) for row in saved]

    async def try_send(self, event, context, resource, via_rule=None):
        """Group send only while the whole-group report is current; returns True when handled here."""
        bot, group = event.appid, context['group']
        mode = await self.mode(bot, group, resource)
        if mode['desired'] != 'group':
            return False
        await self.configure(bot, {'group': group, 'resource': resource})
        decision = await self.service.decision(bot, context, resource, 'view')
        content = await self.service.query_content(bot, resource, None)
        if (
            not content
            or not self.can_send(bot, mode)
            or not decision['allowed']
            or not self.service.access().decision_current(bot, decision)
            or not await self.service.rule_current(bot, via_rule)
        ):
            return False
        state = self.service.reply_outcome(await self.service.safe_reply(event, content))
        await self.service.operation(bot, 'delivery', group=group, member=context['member'], resource=resource, delivery=state, target='group')
        # A failed or unknown public send is never retried elsewhere.
        if state != 'sent':
            await self.service.safe_reply(event, '群发未确认成功，请稍后重新查询；未自动重发。')
        return True
