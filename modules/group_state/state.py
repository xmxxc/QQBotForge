"""Bot-partitioned group facts. No feature permissions, bindings or business data."""

import asyncio
import copy
import hashlib
import json
import sqlite3
import time
from uuid import uuid4

from aiohttp import web

from core.base.config import cfg
from core.plugin._blacklist import get_blacklist_map

from .reader import ROLES, MembershipReader
from .reader import native as valid_native
from .storage import ExtensionStore

PREFIX = '/api/ext/group-state/'
MESSAGE_TYPES = {'GROUP_AT_MESSAGE_CREATE', 'GROUP_MESSAGE_CREATE', 'C2C_MESSAGE_CREATE'}


def identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError('请提供明确的 Bot 或群原生标识')
    return value


class GroupState:
    """Snapshots are detached historical/current facts, never an admission decision."""

    def __init__(self, path, *, sender_provider=None, clock=time.time):
        self.store = ExtensionStore(path)
        self.sender_provider = sender_provider or self._sender
        self.clock = clock
        self.reader = MembershipReader(self.sender_provider)
        self.active = True
        self.generation = uuid4().hex
        self.states = {}
        self.load_locks = {}
        self.write_locks = {}
        self.refresh_locks = {}
        self.roster_locks = {}
        self.durable = {}
        self.pending_writes = {}

    @staticmethod
    def _sender(bot):
        from core.application import get_app

        app = get_app()
        instance = app.get_bot(bot) if app else None
        return instance.sender if instance else None

    async def initialize(self):
        await self.store.ready()

    def close(self):
        self.active = False
        self.store.close()

    async def aclose(self):
        self.active = False
        await asyncio.to_thread(self.store.close)

    async def _state(self, bot):
        identifier(bot)
        if not self.active:
            raise RuntimeError('群状态模块未启用')
        async with self.load_locks.setdefault(bot, asyncio.Lock()):
            if not self.active:
                raise RuntimeError('群状态模块未启用')
            if bot not in self.states:
                state = await self.store.get(bot, 'group_state', 'state') or {'schema_version': 1, 'version': 0, 'groups': {}}
                if not self.active:
                    raise RuntimeError('群状态模块已卸载')
                if state.get('schema_version') != 1:
                    raise RuntimeError('群状态数据版本不兼容')
                for group in state['groups'].values():
                    group.update(reliable=False, status='historical', reason='process_restart')
                    group.setdefault('members', {})
                    group.setdefault('roster', {'complete': False, 'reason': 'not_verified'})
                    group['roster'].update(complete=False, reason='process_restart')
                    for member in group['members'].values():
                        member.update(reliable=False, status='historical', reason='process_restart')
                state['version'] += 1
                self.durable[bot] = {}
                self.states[bot] = state
        return self.states[bot]

    async def _commit(self, bot, state, native, apply):
        """Persist a detached candidate; publish it only if its group stayed current.

        Readers keep seeing the in-progress fact until the write succeeds, and a failed
        write leaves the previous fact as history.
        """
        group = state['groups'][native]
        version = group['version']
        self.pending_writes[bot] = self.pending_writes.get(bot, 0) + 1
        try:
            async with self.write_locks.setdefault(bot, asyncio.Lock()):
                if not self.active:
                    raise RuntimeError('群状态模块已卸载')
                if group['version'] != version:
                    return False
                candidate = copy.deepcopy(state)
                apply(candidate, candidate['groups'][native])
                await self.store.put(bot, 'group_state', 'state', candidate)
                self.durable[bot] = {key: value['version'] for key, value in candidate['groups'].items()}
                if not self.active:
                    raise RuntimeError('群状态模块已卸载')
                if group['version'] != version:
                    return False
                group.clear()
                group.update(candidate['groups'][native])
                state['version'] += 1
                return True
        finally:
            self.pending_writes[bot] -= 1

    def _abandon(self, state, group, version, error, label):
        # An interrupted operation must not leave its in-progress marker as the latest fact.
        if group['version'] != version:
            return None
        if isinstance(error, asyncio.CancelledError):
            reason = label + '_cancelled'
        elif isinstance(error, (sqlite3.Error, OSError)):
            reason = 'persistence_failed'
        else:
            reason = label + '_failed'
        self._invalidate(state, group, reason)
        return reason

    async def _save(self, bot, state):
        self.pending_writes[bot] = self.pending_writes.get(bot, 0) + 1
        try:
            async with self.write_locks.setdefault(bot, asyncio.Lock()):
                written = {native: group['version'] for native, group in state['groups'].items()}
                try:
                    await self.store.put(bot, 'group_state', 'state', state)
                except BaseException:
                    # Retire only the facts this write carried; newer ones belong to a queued write.
                    durable = self.durable.get(bot, {})
                    for native, version in written.items():
                        group = state['groups'][native]
                        if group['version'] == version and durable.get(native) != version:
                            self._invalidate(state, group, 'persistence_failed')
                            group.update(reliable=False, status='historical', reason='persistence_failed')
                            for member in group['members'].values():
                                member.update(reliable=False, reason='persistence_failed')
                    raise
                self.durable[bot] = written
        finally:
            self.pending_writes[bot] -= 1

    def _group(self, state, group):
        return state['groups'].setdefault(
            identifier(group),
            {
                'group': group,
                'version': 0,
                'source': 'owner_refresh',
                'observed_at': None,
                'members': {},
                'roster': {'complete': False, 'reason': 'not_verified'},
                'checked_at': None,
                'bot_state': None,
                'reliable': False,
                'status': 'pending',
                'reason': 'not_verified',
            },
        )

    @staticmethod
    def capabilities():
        return {
            'group_list': 'known_only',
            'bot_state_query': 'framework_supported_permission_required',
            'member_roster': 'paginated_permission_required',
            'role_events': 'not_supported',
        }

    @staticmethod
    def _public_group(group):
        result = copy.deepcopy(group)
        if result['roster'].get('complete') and result['roster'].get('version') != result['version']:
            result['roster'].update(complete=False, reason='state_changed')
        return result

    async def snapshot(self, bot):
        state = await self._state(bot)
        return copy.deepcopy(
            {
                'bot': bot,
                'generation': self.generation,
                'version': state['version'],
                'scope': 'known',
                'complete': False,
                'groups': [self._public_group(group) for group in state['groups'].values()],
                'capabilities': self.capabilities(),
            }
        )

    async def observe(self, event):
        native = event.get('d/group_openid')
        if not isinstance(native, str) or not native.strip() or len(native) > 256:
            return
        state = await self._state(event.appid)
        group = self._group(state, native)
        group.update(
            version=group['version'] + 1,
            source='native_event',
            observed_at=self.clock(),
            event_type=event.event_type,
            reliable=False,
            status='historical',
            reason='observed_event_requires_refresh',
        )
        group['roster'].update(complete=False, reason='observed_event')
        member = event.get('d/member_openid') or event.get('d/author/member_openid')
        if valid_native(member):
            record = group['members'].setdefault(member, {'member': member, 'member_role': None, 'bot': None})
            record.update(reliable=False, status='observed', reason='requires_verification', source='native_event', observed_at=self.clock())
        state['version'] += 1
        await self._save(event.appid, state)

    async def refresh(self, bot, native):
        identifier(native)
        async with self.refresh_locks.setdefault((bot, native), asyncio.Lock()):
            state = await self._state(bot)
            group = self._group(state, native)
            self._invalidate(state, group, 'bot_state_refreshing')
            group.update(reliable=False, status='pending', reason='refreshing')
            version = group['version']
            await self._save(bot, state)
            try:
                sender = self.sender_provider(bot)
                data, error, reason = None, None, ''
                if sender is None or not hasattr(sender, 'get_group_bot_state'):
                    reason = 'unsupported'
                else:
                    try:
                        result = await sender.get_group_bot_state(native, return_error=True)
                        if isinstance(result, tuple) and len(result) == 2:
                            data, error = result
                        else:
                            reason = 'response_unknown'
                    except Exception:
                        reason = 'query_failed'
                if not self.active:
                    raise RuntimeError('群状态模块已卸载')
                if group['version'] != version:
                    return {**self._public_group(group), 'reliable': False, 'reason': 'state_changed'}
                if isinstance(error, dict):
                    reason = 'permission_denied' if str(error.get('code')) == '11253' else 'query_failed'
                valid = (
                    not reason
                    and error is None
                    and isinstance(data, dict)
                    and isinstance(data.get('member_role'), str)
                    and data.get('member_role') in {'owner', 'admin', 'member'}
                    and isinstance(data.get('recv_msg_setting'), str)
                    and bool(data['recv_msg_setting'])
                    and isinstance(data.get('allow_proactive_msg'), bool)
                )

                def apply(candidate, updated):
                    self._invalidate(candidate, updated, 'bot_state_refreshed')
                    updated.update(
                        checked_at=self.clock(),
                        reliable=bool(valid),
                        status='verified' if valid else 'historical',
                        reason='' if valid else reason or 'response_unknown',
                    )
                    if valid and isinstance(data, dict):
                        updated.update(
                            source='platform_bot_state',
                            bot_state={
                                'member_role': data['member_role'],
                                'recv_msg_setting': data['recv_msg_setting'],
                                'allow_proactive_msg': data['allow_proactive_msg'],
                                'in_group': True,
                            },
                        )

                if not await self._commit(bot, state, native, apply):
                    return {**self._public_group(group), 'reliable': False, 'reason': 'state_changed'}
                return self._public_group(group)
            except BaseException as error:
                if reason := self._abandon(state, group, version, error, 'refresh'):
                    group.update(status='historical', reason=reason)
                raise

    @staticmethod
    def _invalidate(state, group, reason):
        state['version'] += 1
        group['version'] += 1
        group['roster'].update(complete=False, reason=reason)

    async def sync_roster(self, bot, native_group):
        identifier(native_group)
        async with self.roster_locks.setdefault((bot, native_group), asyncio.Lock()):
            state = await self._state(bot)
            group = self._group(state, native_group)
            known = {key for key, member in group['members'].items() if member.get('status') != 'left'}
            self._invalidate(state, group, 'roster_refreshing')
            for record in group['members'].values():
                record.update(reliable=False, status='historical', reason='roster_refreshing')
            version = group['version']
            await self._save(bot, state)
            try:
                report = await self.reader.roster(bot, native_group)
                if not self.active:
                    raise RuntimeError('群状态模块已卸载')
                if group['version'] != version:
                    report.update(complete=False, reason='state_changed')
                    return copy.deepcopy(report)
                if report['complete'] and known - {r['member'] for r in report['members']}:
                    report.update(complete=False, reason='known_member_missing', missing_known_members=sorted(known - {r['member'] for r in report['members']}))
                checked = self.clock()

                def apply(candidate, updated):
                    for record in report['members']:
                        reliable = record['member_role'] is not None
                        updated['members'][record['member']] = {
                            **record,
                            'reliable': reliable,
                            'status': 'present' if reliable else 'pending',
                            'reason': '' if reliable else 'role_missing',
                            'source': 'platform_roster',
                            'checked_at': checked,
                        }
                    self._invalidate(candidate, updated, report['reason'])
                    report.update(
                        id=uuid4().hex, generation=self.generation, version=updated['version'], group=native_group, source='platform_roster', checked_at=checked
                    )
                    updated['roster'] = copy.deepcopy(report)

                if not await self._commit(bot, state, native_group, apply):
                    report.update(complete=False, reason='state_changed')
                return copy.deepcopy(report)
            except BaseException as error:
                if reason := self._abandon(state, group, version, error, 'roster'):
                    for record in group['members'].values():
                        record.update(reason=reason)
                raise

    async def verify_member(self, bot, native_group, member):
        identifier(native_group)
        identifier(member)
        state = await self._state(bot)
        group = self._group(state, native_group)
        self._invalidate(state, group, 'individual_verification')
        version = group['version']
        record = group['members'].setdefault(member, {'member': member, 'member_role': None, 'bot': None})
        record.update(reliable=False, status='pending', reason='verifying')
        await self._save(bot, state)
        try:
            result = await self.reader.member(bot, native_group, member)
            if not self.active:
                raise RuntimeError('群状态模块已卸载')
            if group['version'] != version:
                return {**copy.deepcopy(record), 'state': 'unknown', 'reliable': False, 'reason': 'state_changed'}
            fact = dict(
                result,
                source='platform_member',
                checked_at=self.clock(),
                reliable=result['state'] == 'present',
                status='present' if result['state'] == 'present' else 'pending',
            )
            return await self._commit_member(bot, state, native_group, member, fact)
        except BaseException as error:
            if reason := self._abandon(state, group, version, error, 'verification'):
                record.update(status='historical', reason=reason)
            raise

    async def _commit_member(self, bot, state, native_group, member, fact):
        published: dict = {}

        def apply(candidate, updated):
            updated['members'].setdefault(member, {'member': member, 'member_role': None, 'bot': None}).update(fact)
            self._invalidate(candidate, updated, 'individual_verification')
            published.update(updated['members'][member], group=native_group, version=updated['version'], generation=self.generation)

        group = state['groups'][native_group]
        if not await self._commit(bot, state, native_group, apply):
            current = group['members'].get(member, {'member': member})
            return {
                **copy.deepcopy(current),
                'group': native_group,
                'version': group['version'],
                'generation': self.generation,
                'reliable': False,
                'state': 'unknown',
                'reason': 'state_changed',
            }
        return copy.deepcopy(published)

    async def confirm_member(self, bot, native_group, member, *, evidence, member_role=None):
        identifier(native_group)
        identifier(member)
        if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 1000:
            raise ValueError('请记录主人核验依据，最多 1000 字符')
        if member_role is not None and (not isinstance(member_role, str) or member_role not in ROLES):
            raise ValueError('角色须为 member、admin、owner 或 null')
        state = await self._state(bot)
        self._group(state, native_group)
        fact = {
            'member_role': member_role,
            'reliable': True,
            'status': 'present',
            'reason': '',
            'source': 'owner_confirmed',
            'checked_at': self.clock(),
            'evidence': evidence,
            'state': 'present',
        }
        return await self._commit_member(bot, state, native_group, member, fact)

    def _page_key(self, bot, native_group, version):
        # Cursors continue only the Bot, group and snapshot version that issued them.
        return hashlib.sha256(json.dumps([self.generation, bot, native_group, version]).encode()).hexdigest()[:16]

    async def members(self, bot, native_group, *, cursor='', limit=30, version=None, generation=None):
        identifier(native_group)
        start, _, page_key = cursor.partition('.') if isinstance(cursor, str) else ('', '', '')
        if type(limit) is not int or not 1 <= limit <= 100 or not isinstance(cursor, str) or (cursor and not (start.isdecimal() and page_key)):
            raise ValueError('成员分页 limit 须为 1 至 100，cursor 须为上一页返回的游标')
        state = await self._state(bot)
        if self.pending_writes.get(bot, 0):
            raise ValueError('群事实持久化中，请稍后从第一页读取')
        group = state['groups'].get(native_group, {'version': 0, 'members': {}, 'roster': {'complete': False, 'reason': 'unknown_group'}})
        if cursor and (
            generation != self.generation or str(version) != str(group['version']) or page_key != self._page_key(bot, native_group, group['version'])
        ):
            raise ValueError('成员快照已变化，请从第一页重新读取')
        group = self._public_group(group)
        rows, offset = list(group['members'].values()), int(start or 0)
        return copy.deepcopy(
            {
                'group': native_group,
                'generation': self.generation,
                'version': group['version'],
                'roster': {key: value for key, value in group['roster'].items() if key != 'members'},
                'members': rows[offset : offset + limit],
                'next_cursor': f'{offset + limit}.{self._page_key(bot, native_group, group["version"])}' if offset + limit < len(rows) else '',
            }
        )

    async def http(self, request):
        from web.auth import authorize_request

        denied = authorize_request(request)
        if denied is not None:
            return denied
        bot = request.query.get('bot', '')
        if not cfg.get_bot_config(bot):
            return web.json_response({'success': False, 'error': '请选择有效 Bot'}, status=404)
        try:
            path = request.path[len(PREFIX) :].strip('/')
            if request.method == 'GET' and not path:
                result = await self.snapshot(bot)
            elif request.method == 'GET' and path == 'members':
                result = await self.members(
                    bot,
                    request.query.get('group', ''),
                    cursor=request.query.get('cursor', ''),
                    limit=int(request.query.get('limit', '30')),
                    version=request.query.get('version'),
                    generation=request.query.get('generation'),
                )
            elif request.method == 'POST' and path in {'roster', 'member', 'confirm-member'}:
                data = await request.json()
                fields = {'roster': {'group'}, 'member': {'group', 'member'}, 'confirm-member': {'group', 'member', 'evidence', 'member_role'}}[path]
                if not isinstance(data, dict) or set(data) - fields or not {'group'} <= data.keys():
                    raise ValueError('成员核验字段无效')
                if path == 'roster':
                    result = await self.sync_roster(bot, data['group'])
                elif path == 'member':
                    result = await self.verify_member(bot, data['group'], data.get('member'))
                else:
                    result = await self.confirm_member(
                        bot, data['group'], data.get('member'), evidence=data.get('evidence'), member_role=data.get('member_role')
                    )
            elif request.method == 'POST' and path == 'refresh':
                data = await request.json()
                if not isinstance(data, dict) or set(data) != {'group'}:
                    raise ValueError('刷新只接受 group 原生标识')
                result = await self.refresh(bot, identifier(data['group']))
            else:
                return web.json_response({'success': False, 'error': '方法或入口不存在'}, status=404)
            return web.json_response({'success': True, 'data': result}, headers={'Cache-Control': 'no-store'})
        except ValueError as error:
            return web.json_response({'success': False, 'error': str(error)}, status=422)
        except (RuntimeError, sqlite3.Error, OSError):
            return web.json_response({'success': False, 'error': '群状态存储暂不可用'}, status=503)

    async def on_event(self, event):
        # Facts are observed independently of plugin admission; management is owner-only.
        observation_error = None
        if event.group_id:
            try:
                await self.observe(event)
            except (RuntimeError, sqlite3.Error, OSError) as error:
                observation_error = error
        text = (event.content or '').strip()
        owner_ids = (cfg.get_bot_config(event.appid) or {}).get('owner_ids', [])
        if event.event_type not in MESSAGE_TYPES or event.is_channel or not (text == '群状态' or text.startswith('群状态 ')) or event.user_id not in owner_ids:
            if observation_error is not None:
                raise observation_error
            return event
        if any(identity in get_blacklist_map(event.appid, 'user') for identity in (event.user_id, event.raw_user_id)) or event.group_id in get_blacklist_map(
            event.appid, 'group'
        ):
            return None
        sender = self.sender_provider(event.appid)
        if sender is None:
            return None
        try:
            if observation_error is not None:
                raise observation_error
            words = text.split()
            if len(words) == 3 and words[1] == '刷新':
                result = await self.refresh(event.appid, words[2])
            elif len(words) == 3 and words[1] == '名册':
                result = await self.sync_roster(event.appid, words[2])
            elif len(words) == 4 and words[1] == '核验':
                result = await self.verify_member(event.appid, words[2], words[3])
            elif text.startswith('群状态 确认 '):
                data = json.loads(text.split(maxsplit=2)[2])
                if not isinstance(data, dict) or set(data) - {'group', 'member', 'evidence', 'member_role'}:
                    raise ValueError('人工确认须为成员事实对象')
                result = await self.confirm_member(
                    event.appid, data.get('group'), data.get('member'), evidence=data.get('evidence'), member_role=data.get('member_role')
                )
            elif len(words) == 1:
                result = await self.snapshot(event.appid)
            else:
                raise ValueError('群状态 / 群状态 刷新或名册 <群ID> / 群状态 核验 <群ID> <成员ID> / 群状态 确认 <JSON>')
            content = (
                '已知群状态已处理，详情请在主人私聊或已登录后台查看' if event.is_group else '已知群（不代表全部群）\n' + json.dumps(result, ensure_ascii=False)
            )
        except ValueError as error:
            content = str(error)
        except (RuntimeError, sqlite3.Error, OSError):
            content = '群状态存储暂不可用，请检查后台'
        await sender.reply(event, content)
        return None
