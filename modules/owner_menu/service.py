"""Server-held owner sessions keep message channel separate from business group."""
import asyncio
import re
import sqlite3
import time
from uuid import uuid4

from core.application import get_app
from core.base.config import cfg
from core.display import display_group, display_member
from core.plugin._blacklist import get_blacklist_map
from modules.group_state.storage import ExtensionStore


class OwnerMenu:
    def __init__(self, ctx):
        self.ctx = ctx
        self.active = True
        self.clock = time.time
        self.store = ExtensionStore(ctx.get_data_path('owner-menu.db'))
        self.addresses = {}
        self.action_lock = asyncio.Lock()
        self.pushed = set()
        self.subscription = None
        self.tasks = set()
        # Reuse #143's one-shot, initiating-owner confirmation implementation.
        from plugins.ninja3.owner_commands import Confirmations
        self.confirmations = Confirmations(self, prefix='omc', menu=True)

    @staticmethod
    def is_owner(event):
        from plugins.ninja3.service import Ninja3
        return Ninja3.is_owner(event)

    @staticmethod
    def module(name):
        app = get_app()
        return app.module_manager.get(name) if app and app.module_manager else None

    @staticmethod
    def ninja(bot):
        import sys
        entry = sys.modules.get('plugins.ninja3')
        plugin = getattr(entry, '_instance', None)
        return plugin if plugin and plugin.active and plugin.plugin_available(bot) else None

    @staticmethod
    def sender(bot):
        app = get_app()
        instance = app.get_bot(bot) if app else None
        return instance.sender if instance else None

    def updater(self):
        from web.tools._updater.handlers import get_updater
        try:
            return get_updater()
        except RuntimeError:
            return None

    async def reply(self, event, content, **kwargs):
        from plugins.ninja3.service import Ninja3
        sender = self.sender(event.appid)
        if not sender:
            return 'unavailable'
        try:
            return Ninja3.outcome(await sender.reply(event, content, **kwargs))
        except Exception:  # transport outcome unknown; never retry sensitive cards
            return 'unknown'

    def valid(self, event, session):
        return (self.active and self.is_owner(event) and session.get('bot') == event.appid
                and session.get('owner') == event.raw_user_id and session.get('channel') == event.group_id
                and session.get('schema') == 1)

    async def card(self, bot, owner, channel, group, title, rows, *, stamp=None):
        now = self.clock()
        token = uuid4().hex
        actions = []
        buttons = []
        for row in rows:
            line = []
            for label, action in row:
                actions.append(action)
                line.append({'text': label[:40], 'type': 1, 'data': f'om:{token}:{len(actions)-1}', 'list': [owner]})
            buttons.append(line)
        session = dict(schema=1, bot=bot, owner=owner, channel=channel, group=group,
                       created=now, actions=actions, stamp=stamp)
        await self.store.put(bot, 'cards', token, session)
        return title, buttons, token, session

    async def show(self, event, group, title, rows, *, stamp=None):
        content, buttons, token, session = await self.card(event.appid, event.raw_user_id, event.group_id, group, title, rows, stamp=stamp)
        result = await self.reply(event, content, buttons=buttons, send_check=lambda: self.valid(event, session))
        if result in {'failed', 'unavailable'}:
            await self.store.delete(event.appid, 'cards', token)
            await self.reply(event, '按钮暂不可用，请重新打开主人菜单或使用后台')

    async def on_event(self, event):
        if not self.active:
            return event
        content = (event.content or '').strip()
        command = content in {'主人菜单', '主人命令'} and event.event_type in {'GROUP_AT_MESSAGE_CREATE', 'GROUP_MESSAGE_CREATE', 'C2C_MESSAGE_CREATE'}
        callback = event.event_type == 'INTERACTION_CREATE' and content.startswith(('om:', 'omc:'))
        if not command and not callback:
            return event
        if callback:
            event.set_callback_code(0)
        if not self.is_owner(event):
            await self.reply(event, '仅主人可用')
            return None
        if event.is_channel:
            await self.reply(event, '请在群或私聊打开主人菜单')
            return None
        try:
            if event.event_type == 'C2C_MESSAGE_CREATE':
                self.addresses[(event.appid, event.raw_user_id)] = event.raw_user_id
                ninja = self.ninja(event.appid)
                if ninja:
                    await ninja.store.put(event.appid, 'owners', 'observed_c2c', {'private_id': event.raw_user_id, 'authenticated_id': event.raw_user_id, 'source': 'observed_owner_c2c'})
            if command:
                await self.open(event)
            else:
                raw = event.interaction_data or {}
                if raw.get('application_id', event.appid) != event.appid or raw.get('type') not in {11, 12} or raw.get('scene') != ('group' if event.group_id else 'c2c') or raw.get('chat_type') != (1 if event.group_id else 2):
                    await self.reply(event, '已失效，请重新打开主人菜单')
                else:
                    async with self.action_lock:
                        if not self.active:
                            return None
                        if not self.is_owner(event):
                            await self.reply(event, '仅主人可用')
                            return None
                        if content.startswith('omc:'):
                            await self.confirmation_click(event)
                        else:
                            await self.click(event)
        except (ValueError, RuntimeError, sqlite3.Error, OSError) as error:
            self.ctx.log.error(f'主人菜单未完成: {type(error).__name__}')
            await self.unavailable(event, '操作未完成：参数或状态已变化，或服务暂不可用')
        return None

    async def open(self, event):
        facts = self.module('group_state')
        if not facts:
            await self.unavailable(event, '群状态模块未启用')
            return
        snapshot = await facts.snapshot(event.appid)
        if not event.group_id:
            await self.groups(event, snapshot, 0)
        else:
            await self.home(event, event.group_id, snapshot)

    async def groups(self, event, snapshot, page):
        groups = snapshot['groups']
        page = min(max(page, 0), max(0, (len(groups)-1)//4))
        rows = [[(await display_group(event.appid, g['group']), ('home', g['group']))] for g in groups[page*4:page*4+4]]
        nav = []
        if page:
            nav.append(('上一页', ('groups', page-1)))
        if (page+1)*4 < len(groups):
            nav.append(('下一页', ('groups', page+1)))
        nav.append(('返回', ('groups', 0)))
        await self.show(event, '', f'请选择群（已知 {len(groups)} 群）', rows+[nav])

    async def home(self, event, group, snapshot=None):
        facts = self.module('group_state')
        snapshot = snapshot or await facts.snapshot(event.appid)
        pending = not snapshot['recovery']['ready'] or snapshot['recovery']['pause']
        rows = [[('群状态', ('state',))], [('授权', ('access',))], [('忍3', ('ninja',))]]
        if self.updater() is not None:
            rows.append([('更新', ('updates',))])
        rows.append([('返回', ('groups', 0))])
        await self.show(event, group, f'主人菜单 · {await display_group(event.appid, group)}\n群状态' + ('：待恢复' if pending else '：已就绪'), rows)

    async def click(self, event):
        if event.content == 'om:open':
            await self.open(event)
            return
        match = re.fullmatch(r'om:([0-9a-f]{32}):([0-9]{1,2})', event.content or '')
        session = await self.store.get(event.appid, 'cards', match[1]) if match else None
        if not session or not self.valid(event, session) or int(match[2]) >= len(session['actions']):
            await self.unavailable(event, '卡片记录不可用或与当前主人、Bot、群不匹配')
            return
        action = session['actions'][int(match[2])]
        group = session['group']
        facts = self.module('group_state')
        if not facts:
            await self.unavailable(event, '群状态模块未启用')
            return
        snapshot = await facts.snapshot(event.appid)
        if not self.valid(event, session):
            await self.unavailable(event, '当前主人或菜单状态已变化')
            return
        if action[0] == 'groups':
            await self.groups(event, snapshot, action[1])
            return
        if action[0] == 'home':
            group = action[1] if len(action) > 1 else group
        if group and group not in {g['group'] for g in snapshot['groups']}:
            await self.unavailable(event, '卡片对应的群已不在当前群记录中')
            return
        if action[0] == 'list' and session['stamp'] and tuple(session['stamp'][1:]) != await self.stamp(event.appid, group):
            await self.reply(event, '名单状态已变化，已返回第一页')
            await self.listing(event, group, session['stamp'][0], 0)
            return
        pending = set(self.confirmations.pending)
        await self.dispatch(event, group, action, snapshot)
        await self.remember_confirmations(event, group, action, pending)
        if action[0] in {'member', 'enable', 'recover', 'rebuild', 'recover_all', 'check_update', 'update', 'update_progress', 'batch_confirm'} and not (set(self.confirmations.pending) - pending):
            await self.return_card(event, group)

    async def return_card(self, event, group):
        if self.active and self.is_owner(event):
            await self.show(event, group, '继续管理', [[('返回', ('home',) if group else ('groups', 0)), ('打开新菜单', ('open',))]])

    async def unavailable(self, event, reason):
        await self.reply(event, reason, buttons=[[{'text': '打开新菜单', 'type': 1, 'data': 'om:open', 'list': [event.raw_user_id]}]])

    async def remember_confirmations(self, event, group, action, before):
        for token in set(self.confirmations.pending) - before:
            request = self.confirmations.pending[token]
            reopen = action
            if action[0] == 'select':
                reopen = ('batch_review', request['snapshot'][2]['id'], event.raw_user_id)
            await self.store.put(event.appid, 'confirmations', token, dict(
                schema=1, bot=event.appid, owner=event.raw_user_id, channel=event.group_id,
                group=group, action=reopen,
            ))

    async def confirmation_click(self, event):
        match = re.fullmatch(r'omc:(ok|no):([0-9a-f]{32})', event.content or '')
        if not match:
            await self.unavailable(event, '确认按钮参数无效')
            return
        choice, token = match.groups()
        request = self.confirmations.pending.get(token)
        if request and request['expires_at'] > self.clock():
            await self.confirmations.click(event, choice == 'ok', token)
            if token not in self.confirmations.pending:
                await self.return_card(event, request.get('business_group') or '')
            return
        self.confirmations.pending.pop(token, None)
        saved = await self.store.get(event.appid, 'confirmations', token)
        if not saved or not self.valid(event, saved):
            await self.unavailable(event, '确认记录不可用或与当前主人、Bot、群不匹配')
            return
        if choice == 'no':
            await self.reply(event, '已取消；历史按钮不会执行操作')
            await self.return_card(event, saved['group'])
            return
        # Durable records contain action IDs only. Fresh confirmation closures
        # always read today's target; no callable or old permission snapshot is persisted.
        facts = self.module('group_state')
        if not facts:
            await self.unavailable(event, '群状态模块未启用')
            return
        snapshot = await facts.snapshot(event.appid)
        if saved['group'] and saved['group'] not in {g['group'] for g in snapshot['groups']}:
            await self.unavailable(event, '确认对应的群已不在当前群记录中')
            return
        before = set(self.confirmations.pending)
        await self.dispatch(event, saved['group'], saved['action'], snapshot)
        await self.remember_confirmations(event, saved['group'], saved['action'], before)

    async def offer_revoke(self, event, group, member, ninja):
        from plugins.ninja3.owner_commands import TargetStateChangedError
        acl, facts, bot = self.module('access_control'), self.module('group_state'), event.appid

        if not acl or not facts:
            await self.unavailable(event, '授权或群状态模块未启用')
            return

        async def state():
            if (self.ninja(bot) is not ninja or self.module('access_control') is not acl
                    or self.module('group_state') is not facts or ninja.emergency.active(bot)):
                raise TargetStateChangedError
            return (await acl.access_snapshot(bot, 'ninja3'), await self.stamp(bot, group))

        async def run(click, offered):
            if await state() != offered or not self.active or not self.is_owner(click):
                raise TargetStateChangedError
            try:
                await acl.manage_access(bot, 'ninja3', 'revoke', function='query', group=group, member=member,
                                        _expected_version=offered[0]['version'])
            except ValueError as error:
                if str(error) == '授权状态已变化':
                    raise TargetStateChangedError from error
                raise
            await self.reply(click, f'已撤销 {await display_member(bot, member)}在 {await display_group(bot, group)} 的忍3查询授权')

        await self.confirmations.offer(
            event, f'确认撤销 {await display_member(bot, member)}在 {await display_group(bot, group)} 的当前忍3查询授权？',
            label='撤销', state=state, run=run, business_group=group,
        )

    async def dispatch(self, event, group, action, snapshot):
        kind, bot = action[0], event.appid
        back = [('返回', ('home',))]
        if kind == 'open':
            await self.open(event)
        elif kind == 'home':
            await self.home(event, group, snapshot)
        elif kind == 'state':
            from modules.group_state.text import REASONS, group_line, recovery_line
            recovery = snapshot['recovery']
            row = next(g for g in snapshot['groups'] if g['group'] == group)
            reason = recovery['pause'] or row.get('reason', '')
            title = f'{await display_group(bot, group)}\n' + ('待恢复：' + REASONS.get(reason, reason) if not recovery['ready'] or recovery['pause'] else '已就绪')
            names = {'groups': {group: await display_group(bot, group)}}
            title += '\n' + recovery_line(recovery, recovery['pause']) + '\n' + group_line(row, names)
            title += '\n仅包含已记录成员；恢复基线影响此 Bot 的所有已知群'
            await self.show(event, group, title, [[('本群状态', ('state',)), ('刷新', ('refresh',))], [('恢复基线', ('rebuild',))], [('全部确认在群并恢复', ('recover',))], [back[0]]])
        elif kind == 'refresh':
            await self.module('group_state').refresh(bot, group)
            await self.dispatch(event, group, ('state',), await self.module('group_state').snapshot(bot))
        elif kind in {'rebuild', 'recover', 'recover_all'}:
            await self.recover(event, [group] if kind == 'recover' else None, confirm=kind != 'rebuild')
        elif kind == 'access':
            await self.show(event, group, '授权管理', [[(label, ('list', key, 0))] for label, key in [('成员列表', 'members'), ('授权列表', 'grants'), ('拉黑列表', 'denies'), ('未授权查询通知', 'notices')]] + [back])
        elif kind == 'list':
            await self.listing(event, group, action[1], action[2])
        elif kind == 'member':
            ninja = self.ninja(bot)
            if not ninja:
                await self.unavailable(event, '忍3功能未启用')
            elif action[1] == '撤销':
                await self.offer_revoke(event, group, action[2], ninja)
            else:
                await ninja.owner_commands.execute(event, action[1], group, action[2], confirmations=self.confirmations)
        elif kind == 'ninja':
            if not self.ninja(bot):
                await self.unavailable(event, '忍3功能未启用')
                return
            await self.show(event, group, '忍3管理', [[('账号列表', ('accounts', 0))], [('批量改密', ('select_accounts', 0))], [('批量进度与取消', ('batches', 0))], [('开放本群', ('enable', True)), ('关闭本群', ('enable', False))], back])
        elif kind == 'enable':
            ninja = self.require_ninja(bot)
            if not action[1]:
                await self.offer_close(event, group, ninja)
            else:
                await ninja.manage_group(bot, 'save', {'group': group, 'enabled': True})
                await self.reply(event, '已开放本群')
        elif kind in {'accounts', 'select_accounts', 'batches'}:
            await self.ninja_list(event, group, kind, action[1])
        elif kind == 'select':
            ninja = self.require_ninja(bot)
            batch = await ninja.password_batches.select(bot, event.raw_user_id, None if action[1] is None else [action[1]])
            key, actor = batch['id'], event.raw_user_id
            if action[1] is None:
                await self.review_batch(event, group, ninja, key, actor)
            else:
                await self.show(event, group, '已选择一个账号，确认后执行改密', [[('确认执行', ('batch_confirm', key, actor))], back])
        elif kind == 'batch_review':
            await self.review_batch(event, group, self.require_ninja(bot), action[1], action[2])
        elif kind == 'batch_confirm':
            await self.confirm_batch(event, self.require_ninja(bot), action[1], action[2])
        elif kind in {'batch_get', 'batch_cancel'}:
            ninja = self.require_ninja(bot)
            batch = await (ninja.password_batches.cancel(bot, action[1]) if kind == 'batch_cancel' else ninja.password_batches.get(bot, action[1]))
            await self.show(event, group, ninja.batch_summary(batch, detailed=not bool(event.group_id)) + '\n取消只停止未开始项', [[('刷新进度', ('batch_get', action[1])), ('取消未开始项', ('batch_cancel', action[1]))], back])
        elif kind == 'updates':
            await self.show(event, group, '更新（沿用更新器 CI、锁、备份与重启门禁）', [[('检查更新', ('check_update',)), ('更新到最新', ('update',)), ('更新进度', ('update_progress',))], back])
        elif kind in {'check_update', 'update', 'update_progress'}:
            updater = self.updater()
            if not updater:
                await self.unavailable(event, '更新功能当前不可用')
                return
            if kind == 'update_progress':
                result = updater.get_progress()
                await self.reply(event, f'更新进度 {result.get("progress", 0)}%：{result.get("message", "未知")}')
            elif kind == 'check_update':
                result = await updater.check_for_updates()
                await self.reply(event, result.get('error') or ('有可用更新' if result.get('has_update') else '当前已是最新版本'))
            else:
                await self.reply(event, '更新已提交给现有更新器，请在后台查看进度')
                self.start(self.update(updater, event))
        else:
            await self.unavailable(event, '卡片功能已下线或参数版本不再支持')

    def require_ninja(self, bot):
        ninja = self.ninja(bot)
        if ninja is None:
            raise RuntimeError('忍3未启用')
        return ninja

    async def offer_close(self, event, group, ninja):
        from plugins.ninja3.owner_commands import TargetStateChangedError
        bot = event.appid

        async def state():
            if self.ninja(bot) is not ninja:
                raise TargetStateChangedError
            current = await ninja.store.get(bot, 'groups', group)
            if current is None:
                raise TargetStateChangedError
            return current

        async def run(click, offered):
            if await state() != offered or not self.active or not self.is_owner(click):
                raise TargetStateChangedError
            await ninja.manage_group(bot, 'save', {'group': group, 'enabled': False})
            await self.reply(click, '已关闭本群')

        current = await ninja.store.get(bot, 'groups', group)
        if not current:
            await self.unavailable(event, '此群已不在当前忍3开放群记录中')
            return
        await self.confirmations.offer(
            event, f'确认关闭 {await display_group(bot, group)} 的忍3查询入口？',
            label='关闭本群', state=state, run=run, business_group=group,
        )

    async def confirm_batch(self, event, ninja, key, actor):
        if actor != event.raw_user_id or not self.is_owner(event) or self.ninja(event.appid) is not ninja:
            await self.unavailable(event, '批次记录与当前主人或忍3服务不匹配')
            return
        batch = await ninja.password_batches.confirm(event.appid, key, actor)
        await self.reply(event, ninja.batch_summary(batch, detailed=not bool(event.group_id)))

    async def review_batch(self, event, group, ninja, key, actor):
        from plugins.ninja3.owner_commands import TargetStateChangedError
        bot = event.appid
        batch = await ninja.password_batches.get(bot, key)
        if actor != event.raw_user_id:
            await self.unavailable(event, '请由选择账号的当前主人操作此批次')
            return
        if batch['state'] != 'selected':
            await self.reply(event, ninja.batch_summary(batch, detailed=not bool(event.group_id)))
            return

        async def state():
            if self.ninja(bot) is not ninja:
                raise TargetStateChangedError
            return (True, [{k: v for k, v in a.items() if k != 'login'} for a in await ninja.manage_account(bot, 'list')],
                    await ninja.password_batches.get(bot, key))

        async def run(click, offered):
            await self.confirm_batch(click, ninja, key, actor)

        await self.confirmations.offer(
            event, f'确认对当前批次中的 {len(batch["items"])} 个账号批量改密？',
            label='批量改密全部', state=state, run=run, business_group=group,
        )

    async def update(self, updater, event):
        try:
            if self.active and self.is_owner(event):
                result = await updater.update_to_latest()
                if self.active and self.is_owner(event):
                    await self.reply(event, result.get('message') or '更新器已返回，请查看进度')
        except Exception as error:
            self.ctx.log.error(f'主人菜单更新失败: {type(error).__name__}')

    def start(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def known(self, bot, group):
        facts = self.module('group_state')
        page = await facts.members(bot, group, limit=100)
        rows = list(page['members'])
        while page['next_cursor']:
            page = await facts.members(bot, group, cursor=page['next_cursor'], limit=100, version=page['version'], generation=page['generation'])
            rows += page['members']
        return rows

    async def stamp(self, bot, group):
        facts = self.module('group_state')
        page = await facts.members(bot, group, limit=1)
        acl = self.module('access_control')
        snapshot = await acl.access_snapshot(bot, 'ninja3') if acl else {}
        return page['generation'], page['version'], snapshot.get('generation'), snapshot.get('version')

    async def listing(self, event, group, kind, page):
        bot = event.appid
        acl = self.module('access_control')
        if not acl:
            await self.unavailable(event, '授权模块未启用')
            return
        ninja = self.ninja(bot)
        if not ninja:
            await self.unavailable(event, '忍3未启用')
            return
        await ninja.declare(acl, bot)
        before = await self.stamp(bot, group)
        snapshot = await acl.access_snapshot(bot, 'ninja3')
        known = {r['member']: r for r in await self.known(bot, group)}
        if kind == 'members':
            ids = list(known)
        elif kind == 'grants':
            ids = list(dict.fromkeys(r['member'] for r in snapshot['grants'] if r['group'] == group))
        elif kind == 'denies':
            # Include global sources and apply evaluate's denial semantics below.
            ids = list(dict.fromkeys([*known, *(r['member'] for r in snapshot['denies'] if r['member'] and r['group'] in {'', group})]))
            ids = [m for m in ids if (await acl.evaluate(bot, 'ninja3', 'query', group, m, action='invoke')).get('denies')]
        else:
            ninja = self.require_ninja(bot)
            notices = [r for r in await ninja.store.all(bot, 'notices') if r.get('group') == group]
            ids = list(dict.fromkeys(r['member'] for r in notices))
        page = min(max(0, page), max(0, (len(ids)-1)//4))
        title = {'members': '成员列表', 'grants': '授权列表', 'denies': '拉黑列表', 'notices': '未授权查询通知'}[kind]
        lines, rows = [f'{title}（{len(ids)} 人，第 {page+1} 页）'], []
        for i, member in enumerate(ids[page*4:page*4+4], 1):
            decision = await acl.evaluate(bot, 'ninja3', 'query', group, member, action='invoke')
            denied = bool(decision.get('denies')) or decision['reason'] == 'framework_denied'
            status = '已拉黑' if denied else ('已授权' if decision['allowed'] and decision['reason'] == 'explicit' else '未授权')
            if not decision['allowed'] and decision['reason'] in {'group_facts_recovery_required', 'group_facts_processing', 'membership_unverified'}:
                status += '（待处理）'
            lines.append(f'{i}. {await display_member(bot, member)} · {status}')
            if kind == 'notices':
                records = [r for r in notices if r['member'] == member]
                count = sum(r.get('count', 1) for r in records)
                outcomes = {'sent': '已通知', 'failed': '通知失败', 'unknown': '送达未知', 'unavailable': '无私聊地址', 'not_attempted': '未通知'}
                delivery = '、'.join(dict.fromkeys(outcomes.get(r.get('notification'), '待处理') for r in records))
                lines.append(f'查询 {count} 次；{delivery}')
            operations = [('解除', '解除拉黑')] if kind == 'denies' else ([('撤销', '撤销'), ('拉黑', '拉黑')] if kind == 'grants' else [('授权', '授权'), ('撤销', '撤销'), ('拉黑', '拉黑')])
            rows.append([(label, ('member', op, member)) for label, op in operations])
        if kind == 'denies':
            lines.append('全局拒绝需在后台解除；此处解除只影响本群来源')
        nav = []
        if page:
            nav.append(('上一页', ('list', kind, page-1)))
        if (page+1)*4 < len(ids):
            nav.append(('下一页', ('list', kind, page+1)))
        nav.append(('返回', ('access',)))
        rows.append(nav)
        if before != await self.stamp(bot, group):
            await self.unavailable(event, '名单状态已变化，请重新打开')
            return
        title = '\n'.join(lines)
        if event.group_id:
            await self.deliver_list(event, group, kind, title, rows, before, len(ids))
        else:
            # stamp carries list kind only for a safe reset to the first page.
            await self.show(event, group, title, rows, stamp=(kind, *before))

    async def address(self, bot, actor):
        if (bot, actor) in self.addresses:
            return self.addresses[(bot, actor)]
        ninja = self.ninja(bot)
        owner = await ninja.store.get(bot, 'owners', 'observed_c2c') if ninja else None
        if owner and owner.get('authenticated_id') == actor:
            return owner['private_id']
        return None

    async def deliver_list(self, event, group, kind, title, rows, stamp, count):
        from plugins.ninja3.service import Ninja3
        address = await self.address(event.appid, event.raw_user_id)
        sender = self.sender(event.appid)
        result = 'unavailable'
        if address and sender:
            content, buttons, token, session = await self.card(event.appid, event.raw_user_id, '', group, title, rows, stamp=(kind, *stamp))
            facts = self.module('group_state')
            acl = self.module('access_control')
            def check():
                state = facts.states.get(event.appid, {}).get('groups', {}).get(group, {})
                access = acl.scope_states.get((event.appid, 'ninja3'), {})
                return (self.active and self.is_owner(event) and facts is self.module('group_state')
                        and acl is self.module('access_control') and facts.active and acl.active
                        and stamp == (facts.generation, state.get('version', 0), acl.generation, access.get('version'))
                        and not facts.pending_writes.get(event.appid) and (event.appid, 'ninja3') not in acl.scope_pending)
            try:
                if check():
                    result = Ninja3.outcome(await sender.send_to_user(address, content, buttons=buttons, send_check=check))
            except Exception:
                result = 'unknown'
            if result in {'failed', 'unavailable'}:
                await self.store.delete(event.appid, 'cards', token)
        await self.reply(event, '已私聊发送' if result == 'sent' else f'共 {count} 人；' + ('私聊未确认送达，请私聊打开主人菜单' if result == 'unknown' else '私聊不可用，请私聊打开主人菜单'))

    async def ninja_list(self, event, group, kind, page):
        from core.display import format_group
        ninja, bot = self.require_ninja(event.appid), event.appid
        items = await (ninja.password_batches.list(bot) if kind == 'batches' else ninja.manage_account(bot, 'list'))
        page = min(max(page, 0), max(0, (len(items)-1)//4))
        lines = [f'忍3 · {len(items)} 项，第 {page+1} 页']
        rows = []
        if kind == 'select_accounts' and page == 0:
            # Four account rows plus navigation; the all action sits in navigation.
            lines.append('全部将固定当前账号范围，随后按钮确认')
        for item in items[page*4:page*4+4]:
            if kind == 'batches':
                lines.append(ninja.batch_summary(item, detailed=not bool(event.group_id)))
                rows.append([('查看进度', ('batch_get', item['id'])), ('取消未开始项', ('batch_cancel', item['id']))])
            else:
                # Use the management projection, then mask login before displaying it.
                label = format_group(item['id'], item['name'])
                masked = '…' + item.get('login', '')[-2:] if item.get('login') else '已脱敏'
                lines.append(f'{label} · {masked}')
                rows.append([('选择此账号' if kind == 'select_accounts' else '改密', ('select', item['id']))])
        nav = []
        if page:
            nav.append(('上一页', (kind, page-1)))
        if (page+1)*4 < len(items):
            nav.append(('下一页', (kind, page+1)))
        if kind == 'select_accounts':
            nav.append(('全部', ('select', None)))
        nav.append(('返回', ('ninja',)))
        await self.show(event, group, '\n'.join(lines), rows+[nav])

    async def recover(self, event, groups, *, confirm):
        from modules.group_state.state import member_departed
        from modules.group_state.text import REASONS
        facts, bot = self.module('group_state'), event.appid
        snapshot = await facts.snapshot(bot)
        groups = groups if groups is not None else [g['group'] for g in snapshot['groups']]
        count, failed, total, skipped, reason = 0, 0, 0, 0, ''
        try:
            if confirm:
                # Read all pages before any write; fixed IDs never grow during execution.
                versions = {group: await facts.members(bot, group, limit=1) for group in groups}
                records = {group: await self.known(bot, group) for group in groups}
                targets = {group: [r['member'] for r in rows if not member_departed(r)] for group, rows in records.items()}
                skipped = sum(member_departed(r) for rows in records.values() for r in rows)
                total = sum(len(ids) for ids in targets.values())
                for group in groups:
                    if records[group] != await self.known(bot, group):
                        raise ValueError('已记录成员范围变化')
                for group, members in targets.items():
                    for member in members:
                        if not self.is_owner(event) or not self.active or facts is not self.module('group_state'):
                            raise ValueError('主人或模块状态变化')
                        current = await facts.members(bot, group, limit=1)
                        if any(current[k] != versions[group][k] for k in ('version', 'generation')):
                            raise ValueError('已记录成员范围变化')
                        result = await facts.confirm_member(
                            bot, group, member, evidence=f'主人在主人菜单批量确认（消息 {event.message_id or "未知"}）',
                            expected_version=versions[group]['version'],
                        )
                        if result.get('source') != 'owner_confirmed' or result.get('status') != 'present' or result.get('reason'):
                            failed += 1
                            raise ValueError('成员确认未完成')
                        count += 1
                        current = await facts.members(bot, group, limit=1)
                        if any(current[k] != result[k] for k in ('version', 'generation')):
                            raise ValueError('已记录成员范围变化')
                        versions[group] = current
                for group in groups:
                    current = await facts.members(bot, group, limit=1)
                    if any(current[k] != versions[group][k] for k in ('version', 'generation')):
                        raise ValueError('已记录成员范围变化')
            if not self.is_owner(event) or facts is not self.module('group_state'):
                raise ValueError('主人或模块状态变化')
            success = await facts.rebuild(bot)
            recovery = (await facts.snapshot(bot))['recovery']
            ready = success and recovery['ready'] and not recovery['pause']
            reason = REASONS.get(recovery['pause'] or recovery['reason'], recovery['pause'] or recovery['reason'])
        except (ValueError, RuntimeError, sqlite3.Error, OSError) as error:
            ready = False
            failed = max(failed, total-count)
            reason = str(error) if isinstance(error, ValueError) else type(error).__name__
        await self.reply(event, f'已确认 {count} 人，' + (f'跳过已退群 {skipped} 人，' if skipped else '') + ('恢复已完成' if ready else f'恢复未完成；失败 {failed} 项：{reason or "状态仍待核验"}') + '（恢复范围为此 Bot 的所有已知群）')

    async def available(self, facts):
        # The availability hook runs before ModuleManager publishes setup's return.
        for status in self.ctx.connection_statuses:
            if status.phase in {'ready', 'resumed'}:
                await self.push_pending(status.appid, facts=facts)

    async def connection(self, status):
        if status.phase not in {'ready', 'resumed'}:
            return
        await self.push_pending(status.appid)

    async def push_pending(self, bot, *, facts=None):
        from plugins.ninja3.service import Ninja3
        facts, sender = facts or self.module('group_state'), self.sender(bot)
        if not self.active or bot in self.pushed or not facts or not sender:
            return
        snapshot = await facts.snapshot(bot)
        if not snapshot['groups'] or (snapshot['recovery']['ready'] and not snapshot['recovery']['pause']):
            return
        # Mark before transport; rejected/unknown startup cards are never retried.
        self.pushed.add(bot)
        names = [await display_group(bot, g['group']) for g in snapshot['groups']]
        for actor in (cfg.get_bot_config(bot) or {}).get('owner_ids', []):
            if actor in get_blacklist_map(bot, 'user'):
                continue
            address = await self.address(bot, actor)
            if not address:
                self.ctx.log.info('主人菜单恢复推送不可用: no_c2c_address')
                continue
            content, buttons, token, _ = await self.card(bot, actor, '', '', '待恢复群：\n'+'\n'.join(names)+'\n只确认未退群的已记录成员；退群成员请单人确认；恢复影响此 Bot 所有已知群', [[('全部确认在群并恢复', ('recover_all',))]])
            def check(actor=actor):
                return self.active and actor in (cfg.get_bot_config(bot) or {}).get('owner_ids', []) and actor not in get_blacklist_map(bot, 'user')
            try:
                result = await sender.send_to_user(address, content, buttons=buttons, send_check=check)
                outcome = Ninja3.outcome(result)
                if outcome == 'failed':
                    data = result[1] if isinstance(result, tuple) and len(result)>1 else result
                    code = data.get('code') if isinstance(data, dict) else None
                    # Only the platform code, never its response body or group list.
                    self.ctx.log.info(f'主人菜单恢复推送拒绝 code={code if isinstance(code, (int, str)) else "unknown"}')
                    await self.store.delete(bot, 'cards', token)
            except Exception:
                self.ctx.log.info('主人菜单恢复推送: unknown')

    async def close(self):
        self.active = False
        self.confirmations.pending.clear()
        if self.subscription:
            self.subscription.cancel()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        async with self.action_lock:
            self.store.close()
