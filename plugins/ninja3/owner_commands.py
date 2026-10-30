"""Owner @-mention short commands in a group and reusable confirmation buttons.

``@Bot <命令> @成员``: the target is the single native member ID in the platform
``mentions`` payload and the group is the current group; typed names or IDs are
never identities. Display names (#141) only decorate the replies.
"""

import sqlite3
import time
from uuid import uuid4

from core.base.config import cfg
from core.base.logger import PLUGIN, get_logger
from core.display import display_group, display_member

from . import service

log = get_logger(PLUGIN, '忍者必须死3')
COMMANDS = ('授权', '撤销', '拉黑', '解除拉黑', '为什么', '确认在群')
CONFIRM_TTL = 300
WHY = {
    'no_explicit_permission': '未授权，可发送“授权 @成员”',
    'expired': '授权已到期',
    'membership_unverified': '成员在群事实待核验，可发送“确认在群 @成员”',
    'member_left': '成员已离群',
    'group_facts_unavailable': '群状态未启用',
    'group_facts_recovery_required': '群状态需恢复基线',
    'group_facts_processing': '群状态处理中，请稍后再问',
    'scope_disabled': '本群未在授权范围',
    'resource_disabled': '账号未启用',
    'state_changed': '检查期间状态变化，请再问一次',
    'denied': '在拒绝名单中（拉黑），可发送“解除拉黑 @成员”',
    'framework_denied': '被框架黑名单拦截',
    'owner_required': '仅主人可以查询',
    'group_context_required': '须在群内查询',
}
ALLOWED = {'public': '本群公开', 'group_member': '本群成员均可查询', 'member_policy': '成员名单允许', 'role_policy': '群角色允许'}


class Confirmations:
    """【确认】【取消】 buttons for one owner request; #144 owner menus reuse them.

    A request is valid for five minutes, once, and only for the owner who started
    it in the same Bot and group; anything else answers “已失效”. Requests live in
    this process only: a restart or plugin reload invalidates every pending
    button. On 确认 the target state is read again; if it differs from the state
    the owner saw, nothing runs and the owner is asked to start over.
    """

    def __init__(self, plugin):
        self.plugin = plugin
        self.pending = {}

    async def offer(self, event, prompt, *, label, state, run):
        """``state``: async, returns a comparable snapshot; ``run``: async, receives the click event."""
        plugin = self.plugin
        now = plugin.clock()
        self.pending = {key: value for key, value in self.pending.items() if value['expires_at'] > now}
        token = uuid4().hex
        self.pending[token] = {
            'bot': event.appid,
            'group': event.group_id,
            'owner': event.raw_user_id,
            'expires_at': now + CONFIRM_TTL,
            'label': label,
            'snapshot': await state(),
            'state': state,
            'run': run,
        }
        # The platform shows the buttons to the owner only; clicks are still rechecked below.
        buttons = [
            [
                {'text': '确认', 'type': 1, 'data': f'n3:ok:{token}', 'list': [event.raw_user_id]},
                {'text': '取消', 'type': 1, 'data': f'n3:no:{token}', 'list': [event.raw_user_id]},
            ]
        ]
        markdown = cfg.get_bot_setting(event.appid, 'message.use_markdown', True)
        if markdown and await plugin.reply(event, prompt + '\n五分钟内有效，仅发起的主人可以确认', buttons=buttons) != 'failed':
            return  # sent or unknown: the buttons may be live, never duplicate.
        self.pending.pop(token, None)
        await plugin.reply(event, f'按钮暂不可用，本次{label}未执行；可在后台授权管理页面操作')

    async def click(self, event, accepted, token):
        plugin = self.plugin
        request = self.pending.get(token)
        if request and request['expires_at'] <= plugin.clock():
            self.pending.pop(token, None)
            request = None
        if not (
            request
            and request['bot'] == event.appid
            and request['group'] == event.group_id
            and request['owner'] == event.raw_user_id
            and plugin.is_owner(event)
        ):
            # Someone else's click never consumes the owner's request.
            await plugin.reply(event, '已失效')
            return
        del self.pending[token]
        label = request['label']
        if not accepted:
            await plugin.reply(event, f'已取消{label}')
            return
        if plugin.emergency.active(event.appid):
            await plugin.owner_private(event.appid, '紧急模式已开启，成员查询与授权操作暂停')
            return
        try:
            changed = await request['state']() != request['snapshot']
            if not changed:
                await request['run'](event)
        except (ValueError, RuntimeError, sqlite3.Error, OSError) as error:
            log.error(f'[{event.appid}] {label}确认失败: {type(error).__name__}')
            await plugin.reply(event, f'{label}未完成：授权或群状态暂不可用，请检查后台')
            return
        if changed:
            await plugin.reply(event, f'目标状态已变化，请重新发送“{label} @成员”')


def mention_target(event):
    """The single native member mentioned besides the Bot, or None."""
    targets = {
        m['id']
        for m in event.mentions
        if isinstance(m, dict) and isinstance(m.get('id'), str) and m['id'] and not m.get('is_you') and not m.get('bot') and m.get('scope') != 'all'
    }
    return next(iter(targets)) if len(targets) == 1 else None


def denials(snapshot, group, member):
    return [
        d
        for d in snapshot['denies']
        if d['group'] == group
        and d['member'] == member
        and d.get('function_scope', {'kind': 'function', 'id': d['function']}) == {'kind': 'function', 'id': service.FUNCTION}
    ]


class OwnerCommands:
    def __init__(self, plugin):
        self.plugin = plugin

    async def run(self, event, name):
        plugin, bot, group = self.plugin, event.appid, event.group_id
        if plugin.emergency.active(bot):
            if event.is_group and plugin.is_owner(event):
                await plugin.owner_private(bot, '紧急模式已开启，成员查询与授权操作暂停')
            return
        grant = name == '授权'
        if not event.is_group or event.is_channel or not group:
            await plugin.reply(event, '请在要开放的群内发送：授权 @成员' if grant else f'请在群内发送：{name} @成员')
            return
        if not plugin.is_owner(event):
            await plugin.reply(event, '仅主人可以授权' if grant else f'仅主人可以使用“{name} @成员”')
            return
        member = mention_target(event)
        if (event.content or '').strip() != name or member is None:
            # Display names and typed IDs are never identities; only the native mention payload is.
            await plugin.reply(event, '请发送“授权”并 @ 一位要授权的成员' if grant else f'请发送“{name}”并 @ 一位成员')
            return
        try:
            acl = plugin.access()
            # The owner's grant; declaration never touches an existing policy scope.
            await plugin.declare(acl, bot)
            facts = acl.group_state()
            if facts is None:
                await plugin.reply(event, f'{name}待处理：群状态模块未启用')
                return
            proof = await facts.fact(bot, group, member)
            if proof['reason'] in service.PAUSED:
                again = '重新授权' if grant else '重新发送'
                await plugin.reply(event, f'{name}待处理：群状态恢复未完成，请先发送“群状态 恢复”，完成后{again}')
                return
            target = {
                'event': event,
                'acl': acl,
                'facts': facts,
                'proof': proof,
                'member': member,
                'who': await display_member(bot, member),
                'where': await display_group(bot, group),
            }
            await getattr(self, METHODS[name])(target)
        except service.BusinessError as error:
            await plugin.reply(event, f'{name}待处理：{error}')
        except ValueError as error:
            await plugin.reply(event, f'{name}未完成：{error}')
        except (RuntimeError, sqlite3.Error, OSError) as error:
            log.error(f'[{bot}] {name}失败: {type(error).__name__}')
            await plugin.reply(event, f'{name}未完成：授权或群状态存储暂不可用，请检查后台')

    async def confirm_present(self, t, command):
        """Use the owner's command in this group as in-group evidence; a member's message never is."""
        event, proof = t['event'], t['proof']
        if proof['usable'] and (proof['member'] or {}).get('status') == 'present':
            return proof
        evidence = f'主人在本群发送“{command} @成员”（消息 {event.message_id or "未知"}）'
        confirmed = await t['facts'].confirm_member(event.appid, event.group_id, t['member'], evidence=evidence[:1000])
        if not confirmed.get('reliable'):
            await self.plugin.reply(event, f'{command}待处理：成员在群事实未能确认，请稍后重试')
            return None
        return await t['facts'].fact(event.appid, event.group_id, t['member'])

    async def grant(self, t):
        plugin, event, acl, member = self.plugin, t['event'], t['acl'], t['member']
        bot, group = event.appid, event.group_id
        proof = await self.confirm_present(t, '授权')
        if proof is None:
            return
        cycle = (proof['member'] or {}).get('cycle')
        snapshot = await acl.access_snapshot(bot, service.CONSUMER)
        existing = next(
            (
                g
                for g in snapshot['grants']
                if g['group'] == group
                and g['member'] == member
                and g['cycle'] == cycle
                and g.get('mode') == 'continuous'
                and g.get('resource_scope') == service.GRANT['resource_scope']
                and g['expires_at'] is None
                # An automatic join source lapses with its join basis; the owner's grant stands alone.
                and g.get('basis') != 'native_join'
            ),
            None,
        )
        source = existing or await acl.manage_access(bot, service.CONSUMER, 'grant', function=service.FUNCTION, group=group, member=member, **service.GRANT)
        decision = await acl.evaluate(bot, service.CONSUMER, service.FUNCTION, group, member, action='invoke')
        opened = await plugin.store.get(bot, 'groups', group)
        log.info(f'[{bot}] 授权 group={group} member={member} source={source["id"]} existing={existing is not None}')
        who, where = t['who'], t['where']
        lines = [f'{who}已有 {where} 的忍3查询授权' if existing else f'已授权 {who}在 {where} 查询忍3开放账号，长期有效']
        if decision['reason'] in {'denied', 'framework_denied'}:
            lines.append('该成员在拒绝名单中，拒绝优先，当前仍不能查询')
        if not (opened and opened['enabled']):
            lines.append('本群尚未开放，请在后台开放本群及账号后生效')
        await plugin.reply(event, '\n'.join(lines))

    async def revoke(self, t):
        event, acl, member = t['event'], t['acl'], t['member']
        bot, group = event.appid, event.group_id
        snapshot = await acl.access_snapshot(bot, service.CONSUMER)
        if not any(g['group'] == group and g['member'] == member for g in snapshot['grants']):
            await self.plugin.reply(event, f'{t["who"]}在 {t["where"]} 没有忍3查询授权，无需撤销')
            return
        await acl.manage_access(bot, service.CONSUMER, 'revoke', function=service.FUNCTION, group=group, member=member)
        log.info(f'[{bot}] 撤销授权 group={group} member={member}')
        await self.plugin.reply(event, f'已撤销 {t["who"]}在 {t["where"]} 的忍3查询授权')

    async def deny(self, t):
        event, acl, facts, member = t['event'], t['acl'], t['facts'], t['member']
        bot, group = event.appid, event.group_id
        if denials(await acl.access_snapshot(bot, service.CONSUMER), group, member):
            await self.plugin.reply(event, f'{t["who"]}已在 {t["where"]} 的忍3拒绝名单中')
            return

        async def state():
            snapshot = await acl.access_snapshot(bot, service.CONSUMER)
            proof = await facts.fact(bot, group, member)
            row = proof['member'] or {}
            return (
                sorted(g['id'] for g in snapshot['grants'] if g['group'] == group and g['member'] == member),
                sorted(d['id'] for d in denials(snapshot, group, member)),
                proof['usable'],
                proof['reason'],
                row.get('status'),
                row.get('cycle'),
            )

        async def run(click):
            await acl.manage_access(bot, service.CONSUMER, 'deny', function=service.FUNCTION, group=group, member=member)
            log.info(f'[{bot}] 拉黑 group={group} member={member}')
            await self.plugin.reply(click, f'已拉黑 {t["who"]}：在 {t["where"]} 不能查询忍3账号')

        await self.plugin.confirmations.offer(event, f'确认拉黑 {t["who"]}？', label='拉黑', state=state, run=run)

    async def allow(self, t):
        event, acl, member = t['event'], t['acl'], t['member']
        bot, group = event.appid, event.group_id
        if not denials(await acl.access_snapshot(bot, service.CONSUMER), group, member):
            await self.plugin.reply(event, f'{t["who"]}不在 {t["where"]} 的忍3拒绝名单中')
            return
        await acl.manage_access(bot, service.CONSUMER, 'allow', function=service.FUNCTION, group=group, member=member)
        log.info(f'[{bot}] 解除拉黑 group={group} member={member}')
        await self.plugin.reply(event, f'已解除 {t["who"]}在 {t["where"]} 的忍3拉黑')

    async def explain(self, t):
        event, acl, member = t['event'], t['acl'], t['member']
        bot, group = event.appid, event.group_id
        decision = await acl.evaluate(bot, service.CONSUMER, service.FUNCTION, group, member, action='invoke')
        opened = await self.plugin.store.get(bot, 'groups', group)
        is_open = bool(opened and opened['enabled'])
        head = f'{t["who"]}在 {t["where"]}'
        if decision['allowed'] and is_open:
            if decision['reason'] == 'explicit':
                basis = '入群自动授权' if all(s.get('basis') == 'native_join' for s in decision['sources'] if s['status'] == 'active') else '主人授权'
                until = decision['valid_until']
                basis += '，长期有效' if until is None else '，有效至 ' + time.strftime('%Y-%m-%d %H:%M', time.localtime(until))
            else:
                basis = ALLOWED.get(decision['reason'], decision['reason'])
            await self.plugin.reply(event, f'{head} 可以查询忍3账号：{basis}')
            return
        reasons = [] if is_open else ['本群未开放忍3账号查询']
        if not decision['allowed'] and not (decision['reason'] == 'scope_disabled' and not is_open):
            reasons.append(WHY.get(decision['reason'], decision['reason']))
        await self.plugin.reply(event, f'{head} 不能查询忍3账号：' + '；'.join(reasons))

    async def confirm(self, t):
        if t['proof']['usable'] and (t['proof']['member'] or {}).get('status') == 'present':
            await self.plugin.reply(t['event'], f'{t["who"]}在 {t["where"]} 的在群事实已确认，无需重复')
            return
        if await self.confirm_present(t, '确认在群') is not None:
            log.info(f'[{t["event"].appid}] 确认在群 group={t["event"].group_id} member={t["member"]}')
            await self.plugin.reply(t['event'], f'已确认 {t["who"]}在 {t["where"]}')


METHODS = {'授权': 'grant', '撤销': 'revoke', '拉黑': 'deny', '解除拉黑': 'allow', '为什么': 'explain', '确认在群': 'confirm'}
