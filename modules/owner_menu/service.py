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


class OwnerMenu:
    def __init__(self, ctx):
        self.ctx = ctx
        self.active = True
        self.clock = time.time
        self.sessions = {}
        self.addresses = {}
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
        return (self.active and self.is_owner(event) and session['bot'] == event.appid
                and session['owner'] == event.raw_user_id and session['channel'] == event.group_id
                and session['expires'] > self.clock() and session['ninja'] == id(self.ninja(event.appid)))

    def card(self, bot, owner, channel, group, title, rows, *, stamp=None):
        now = self.clock()
        self.sessions = {k: v for k, v in self.sessions.items() if v['expires'] > now}
        token = uuid4().hex
        actions = []
        buttons = []
        for row in rows:
            line = []
            for label, action in row:
                actions.append(action)
                line.append({'text': label[:40], 'type': 1, 'data': f'om:{token}:{len(actions)-1}', 'list': [owner]})
            buttons.append(line)
        session = self.sessions[token] = dict(bot=bot, owner=owner, channel=channel, group=group,
                                              expires=now + 300, actions=actions, stamp=stamp, ninja=id(self.ninja(bot)))
        return title, buttons, token, session

    async def show(self, event, group, title, rows, *, stamp=None):
        content, buttons, token, session = self.card(event.appid, event.raw_user_id, event.group_id, group, title, rows, stamp=stamp)
        result = await self.reply(event, content, buttons=buttons, send_check=lambda: self.valid(event, session))
        if result in {'failed', 'unavailable'}:
            self.sessions.pop(token, None)
            await self.reply(event, '按钮暂不可用，请重新打开主人菜单或使用后台')

    async def on_event(self, event):
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
                elif content.startswith('omc:'):
                    match = re.fullmatch(r'omc:(ok|no):([0-9a-f]{32})', content)
                    if match:
                        request = self.confirmations.pending.get(match[2])
                        await self.confirmations.click(event, match[1] == 'ok', match[2])
                        if (request and match[2] not in self.confirmations.pending
                                and request['bot'] == event.appid and request['group'] == event.group_id
                                and request['owner'] == event.raw_user_id and self.is_owner(event)):
                            await self.return_card(event, request.get('business_group') or '')
                    else:
                        await self.reply(event, '已失效，请重新打开主人菜单')
                else:
                    await self.click(event)
        except (ValueError, RuntimeError, sqlite3.Error, OSError) as error:
            self.ctx.log.error(f'主人菜单未完成: {type(error).__name__}')
            await self.reply(event, '操作未完成：状态变化或服务暂不可用，请重新打开主人菜单')
        return None

    async def open(self, event):
        facts = self.module('group_state')
        if not facts:
            await self.reply(event, '群状态模块未启用')
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
        match = re.fullmatch(r'om:([0-9a-f]{32}):([0-9]{1,2})', event.content or '')
        session = self.sessions.get(match[1]) if match else None
        if not session or not self.valid(event, session) or int(match[2]) >= len(session['actions']):
            await self.reply(event, '已失效，请重新打开主人菜单')
            return
        action = session['actions'][int(match[2])]
        # Consume before awaiting: concurrent replay cannot execute a write twice.
        del self.sessions[match[1]]
        group = session['group']
        facts = self.module('group_state')
        if not facts:
            await self.reply(event, '群状态模块未启用')
            return
        snapshot = await facts.snapshot(event.appid)
        if action[0] == 'groups':
            await self.groups(event, snapshot, action[1])
            return
        if action[0] == 'home':
            group = action[1] if len(action) > 1 else group
        if group and group not in {g['group'] for g in snapshot['groups']}:
            await self.reply(event, '群状态已变化，请重新选群')
            return
        if session['stamp'] and session['stamp'][1:] != await self.stamp(event.appid, group):
            await self.reply(event, '名单状态已变化，已返回第一页')
            await self.listing(event, group, session['stamp'][0], 0)
            return
        pending = len(self.confirmations.pending)
        await self.dispatch(event, group, action, snapshot)
        if action[0] in {'member', 'enable', 'recover', 'rebuild', 'recover_all', 'check_update', 'update', 'update_progress', 'batch_confirm'} and len(self.confirmations.pending) <= pending:
            await self.return_card(event, group)

    async def return_card(self, event, group):
        if self.active and self.is_owner(event):
            await self.show(event, group, '继续管理', [[('返回', ('home',) if group else ('groups', 0))]])

    async def dispatch(self, event, group, action, snapshot):
        kind, bot = action[0], event.appid
        back = [('返回', ('home',))]
        if kind == 'home':
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
                await self.reply(event, '忍3未启用')
            else:
                await ninja.owner_commands.execute(event, action[1], group, action[2], confirmations=self.confirmations)
        elif kind == 'ninja':
            if not self.ninja(bot):
                await self.reply(event, '忍3未启用')
                return
            await self.show(event, group, '忍3管理', [[('账号列表', ('accounts', 0))], [('批量改密', ('select_accounts', 0))], [('批量进度与取消', ('batches', 0))], [('开放本群', ('enable', True)), ('关闭本群', ('enable', False))], back])
        elif kind == 'enable':
            await self.require_ninja(bot).manage_group(bot, 'save', {'group': group, 'enabled': action[1]})
            await self.reply(event, '已' + ('开放' if action[1] else '关闭') + '本群')
        elif kind in {'accounts', 'select_accounts', 'batches'}:
            await self.ninja_list(event, group, kind, action[1])
        elif kind == 'select':
            ninja = self.require_ninja(bot)
            batch = await ninja.password_batches.select(bot, event.raw_user_id, None if action[1] is None else [action[1]])
            key, actor = batch['id'], event.raw_user_id
            async def state():
                from plugins.ninja3.owner_commands import TargetStateChangedError
                if self.ninja(bot) is not ninja:
                    raise TargetStateChangedError
                return (True, [{k: v for k, v in a.items() if k != 'login'} for a in await ninja.manage_account(bot, 'list')], await ninja.password_batches.get(bot, key))
            async def run(click, offered):
                await self.confirm_batch(click, ninja, key, actor)
            if action[1] is None:
                await self.confirmations.offer(event, f'确认对全部 {len(batch["items"])} 个账号批量改密？', label='批量改密全部', state=state, run=run, business_group=group)
            else:
                await self.show(event, group, '已选择一个账号，确认后执行改密', [[('确认执行', ('batch_confirm', key, actor))], back])
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
                await self.reply(event, '更新器不可用')
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

    def require_ninja(self, bot):
        ninja = self.ninja(bot)
        if ninja is None:
            raise RuntimeError('忍3未启用')
        return ninja

    async def confirm_batch(self, event, ninja, key, actor):
        if actor != event.raw_user_id or not self.is_owner(event) or self.ninja(event.appid) is not ninja:
            await self.reply(event, '已失效')
            return
        batch = await ninja.password_batches.confirm(event.appid, key, actor)
        await self.reply(event, ninja.batch_summary(batch, detailed=not bool(event.group_id)))

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
            await self.reply(event, '授权模块未启用')
            return
        ninja = self.ninja(bot)
        if not ninja:
            await self.reply(event, '忍3未启用')
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
            await self.reply(event, '名单状态已变化，请重新打开')
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
            content, buttons, token, session = self.card(event.appid, event.raw_user_id, '', group, title, rows, stamp=(kind, *stamp))
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
                self.sessions.pop(token, None)
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
        from modules.group_state.text import REASONS
        facts, bot = self.module('group_state'), event.appid
        snapshot = await facts.snapshot(bot)
        groups = groups if groups is not None else [g['group'] for g in snapshot['groups']]
        count, failed, total, reason = 0, 0, 0, ''
        try:
            if confirm:
                # Read all pages before any write; fixed IDs never grow during execution.
                targets = {group: [r['member'] for r in await self.known(bot, group)] for group in groups}
                versions = {group: await facts.members(bot, group, limit=1) for group in groups}
                total = sum(len(ids) for ids in targets.values())
                for group in groups:
                    if targets[group] != [r['member'] for r in await self.known(bot, group)]:
                        raise ValueError('已记录成员范围变化')
                for group, members in targets.items():
                    for member in members:
                        if not self.is_owner(event) or not self.active or facts is not self.module('group_state'):
                            raise ValueError('主人或模块状态变化')
                        current = await facts.members(bot, group, limit=1)
                        if any(current[k] != versions[group][k] for k in ('version', 'generation')):
                            raise ValueError('已记录成员范围变化')
                        result = await facts.confirm_member(bot, group, member, evidence=f'主人在主人菜单批量确认（消息 {event.message_id or "未知"}）')
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
        await self.reply(event, f'已确认 {count} 人，' + ('恢复已完成' if ready else f'恢复未完成；失败 {failed} 项：{reason or "状态仍待核验"}') + '（恢复范围为此 Bot 的所有已知群）')

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
            content, buttons, token, _ = self.card(bot, actor, '', '', '待恢复群：\n'+'\n'.join(names)+'\n只确认已记录成员；恢复影响此 Bot 所有已知群', [[('全部确认在群并恢复', ('recover_all',))]])
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
                    self.sessions.pop(token, None)
            except Exception:
                self.ctx.log.info('主人菜单恢复推送: unknown')

    async def close(self):
        self.active = False
        self.sessions.clear()
        self.confirmations.pending.clear()
        if self.subscription:
            self.subscription.cancel()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
