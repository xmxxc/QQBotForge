"""Bot-partitioned group facts. No feature permissions, bindings or business data."""

import asyncio
import copy
import json
import sqlite3
import time
from uuid import uuid4

from aiohttp import web

from core.base.config import cfg
from core.base.logger import EXTENSION, get_logger
from core.plugin._blacklist import get_blacklist_map

from .storage import ExtensionStore

PREFIX = '/api/ext/group-state/'
MESSAGE_TYPES = {'GROUP_AT_MESSAGE_CREATE', 'GROUP_MESSAGE_CREATE', 'C2C_MESSAGE_CREATE'}
log = get_logger(EXTENSION, '群状态')


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
        self.active = True
        self.generation = uuid4().hex
        self.states = {}
        self.load_locks = {}
        self.write_locks = {}
        self.refresh_locks = {}

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
                state['version'] += 1
                self.states[bot] = state
        return self.states[bot]

    async def _save(self, bot, state):
        async with self.write_locks.setdefault(bot, asyncio.Lock()):
            await self.store.put(bot, 'group_state', 'state', state)

    def _group(self, state, group):
        return state['groups'].setdefault(identifier(group), {
            'group': group, 'version': 0, 'source': 'owner_refresh', 'observed_at': None,
            'checked_at': None, 'bot_state': None, 'reliable': False, 'status': 'pending', 'reason': 'not_verified',
        })

    @staticmethod
    def capabilities():
        return {'group_list': 'known_only', 'bot_state_query': 'framework_supported_permission_required',
                'member_roster': 'not_verified', 'role_events': 'not_supported'}

    async def snapshot(self, bot):
        state = await self._state(bot)
        return copy.deepcopy({'bot': bot, 'generation': self.generation, 'version': state['version'],
                              'scope': 'known', 'complete': False, 'groups': list(state['groups'].values()),
                              'capabilities': self.capabilities()})

    async def observe(self, event):
        native = event.get('d/group_openid')
        if not isinstance(native, str) or not native.strip() or len(native) > 256:
            return
        state = await self._state(event.appid)
        group = self._group(state, native)
        group.update(version=group['version'] + 1, source='native_event', observed_at=self.clock(),
                     event_type=event.event_type, reliable=False, status='historical', reason='observed_event_requires_refresh')
        state['version'] += 1
        version = group['version']
        try:
            await self._save(event.appid, state)
        except (RuntimeError, sqlite3.Error, OSError):
            if group['version'] == version:
                group.update(version=version + 1, reason='persistence_failed')
                state['version'] += 1
            raise

    async def refresh(self, bot, native):
        identifier(native)
        async with self.refresh_locks.setdefault((bot, native), asyncio.Lock()):
            state = await self._state(bot)
            group = self._group(state, native)
            group.update(version=group['version'] + 1, reliable=False, status='pending', reason='refreshing')
            state['version'] += 1
            version = group['version']
            failure_reason = 'persistence_failed'
            try:
                await self._save(bot, state)
                failure_reason = 'query_failed'
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
                if isinstance(error, dict):
                    reason = 'permission_denied' if str(error.get('code')) == '11253' else 'query_failed'
                valid = (not reason and error is None and isinstance(data, dict)
                         and isinstance(data.get('member_role'), str)
                         and data.get('member_role') in {'owner', 'admin', 'member'}
                         and isinstance(data.get('recv_msg_setting'), str) and bool(data['recv_msg_setting'])
                         and isinstance(data.get('allow_proactive_msg'), bool))
                async with self.write_locks.setdefault(bot, asyncio.Lock()):
                    if not self.active:
                        raise RuntimeError('群状态模块已卸载')
                    if group['version'] != version:
                        return copy.deepcopy({**group, 'reliable': False, 'reason': 'state_changed'})
                    # Commit a detached candidate; readers keep seeing the pending fact.
                    candidate = copy.deepcopy(state)
                    candidate['version'] += 1
                    updated = candidate['groups'][native]
                    updated['version'] += 1
                    updated.update(checked_at=self.clock(), reliable=bool(valid), status='verified' if valid else 'historical',
                                   reason='' if valid else reason or 'response_unknown')
                    if valid and isinstance(data, dict):
                        updated.update(source='platform_bot_state', bot_state={
                            'member_role': data['member_role'], 'recv_msg_setting': data['recv_msg_setting'],
                            'allow_proactive_msg': data['allow_proactive_msg'], 'in_group': True,
                        })
                    failure_reason = 'persistence_failed'
                    await self.store.put(bot, 'group_state', 'state', candidate)
                    if not self.active:
                        raise RuntimeError('群状态模块已卸载')
                    if group['version'] != version:
                        return copy.deepcopy({**group, 'reliable': False, 'reason': 'state_changed'})
                    group.update(updated)
                    state['version'] += 1
                    return copy.deepcopy(group)
            except BaseException as error:
                if group['version'] == version:
                    reason = 'refresh_cancelled' if isinstance(error, asyncio.CancelledError) else failure_reason
                    group.update(version=version + 1, reliable=False, status='historical', reason=reason)
                    state['version'] += 1
                raise

    async def http(self, request):
        from web.auth import authorize_request

        denied = authorize_request(request)
        if denied is not None:
            return denied
        bot = request.query.get('bot', '')
        if not cfg.get_bot_config(bot):
            return web.json_response({'success': False, 'error': '请选择有效 Bot'}, status=404)
        try:
            path = request.path[len(PREFIX):].strip('/')
            if request.method == 'GET' and not path:
                result = await self.snapshot(bot)
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
        if (event.event_type not in MESSAGE_TYPES or event.is_channel
                or not (text == '群状态' or text.startswith('群状态 ')) or event.user_id not in owner_ids):
            if observation_error is not None:
                raise observation_error
            return event
        if any(identity in get_blacklist_map(event.appid, 'user') for identity in (event.user_id, event.raw_user_id)) or event.group_id in get_blacklist_map(event.appid, 'group'):
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
            elif len(words) == 1:
                result = await self.snapshot(event.appid)
            else:
                raise ValueError('群状态 / 群状态 刷新 <群原生ID>')
            content = ('已知群状态已处理，详情请在主人私聊或已登录后台查看' if event.is_group
                       else '已知群（不代表全部群）\n' + json.dumps(result, ensure_ascii=False))
        except ValueError as error:
            content = str(error)
        except (RuntimeError, sqlite3.Error, OSError):
            content = '群状态存储暂不可用，请检查后台'
        try:
            await sender.reply(event, content)
        except Exception as error:
            # The command is consumed either way: the platform may have delivered it, so neither resend nor fall through.
            log.warning(f'群状态命令回复失败，结果未知，不重发: {type(error).__name__}: {error}')
        return None
