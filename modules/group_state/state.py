"""Bot-partitioned group facts. No feature permissions, bindings or business data."""

import asyncio
import copy
import hashlib
import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from aiohttp import web

from core.base.config import cfg
from core.plugin._blacklist import get_blacklist_map

from .lifecycle import LIFECYCLE, event_time
from .notifications import Notifications
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
        if hasattr(self, 'notifications'):
            self.notifications.close()
            self.recovery.cancel()
            self.connection_subscription.cancel()
        self.store.close()

    async def aclose(self):
        self.active = False
        if hasattr(self, 'notifications'):
            self.notifications.close()
            self.recovery.cancel()
            self.connection_subscription.cancel()
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
                        member.update(reliable=False, status='left' if member.get('status') == 'left' else 'historical', reason='process_restart')
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
        if reason == 'persistence_failed':
            group.update(reliable=False, status='historical', reason=reason)
            for member in group['members'].values():
                member.update(reliable=False, reason=reason)
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
            'recovery': 'qq_official_websocket_v1',
            'changes': 'completion_v1',
        }

    def _public_group(self, bot, group):
        result = copy.deepcopy(group)
        if result['roster'].get('complete') and result['roster'].get('version') != result['version']:
            result['roster'].update(complete=False, reason='state_changed')
        if not self._ready(bot):
            result.update(reliable=False, continuity_reason='recovery_pending')
            result['roster'].update(complete=False, reason='recovery_pending')
            for member in result['members'].values():
                member.update(reliable=False, continuity_reason='recovery_pending')
        if self.pending_writes.get(bot, 0):
            result.update(reliable=False, reason='persistence_pending')
            result['roster'].update(complete=False, reason='persistence_pending')
            for member in result['members'].values():
                member.update(reliable=False, reason='persistence_pending')
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
                'groups': [self._public_group(bot, group) for group in state['groups'].values()],
                'capabilities': self.capabilities(),
                'recovery': {'ready': self._ready(bot), 'phase': self.recovery.get_status(bot).phase, 'reason': self.recovery.get_status(bot).reason},
            }
        )

    async def observe(self, event):
        native = event.get('d/group_openid')
        if not isinstance(native, str) or not native.strip() or len(native) > 256:
            return
        state = await self._state(event.appid)
        group = self._group(state, native)
        observed_member = event.get('d/member_openid') or event.get('d/author/member_openid')
        if valid_native(observed_member) and observed_member in group['members']:
            return
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
        await self._notify(event.appid, group, 'native_observation')

    async def refresh(self, bot, native):
        identifier(native)
        async with self.refresh_locks.setdefault((bot, native), asyncio.Lock()):
            state = await self._state(bot)
            group = self._group(state, native)
            self._invalidate(state, group, 'bot_state_refreshing')
            group.update(reliable=False, status='pending', reason='refreshing')
            version = group['version']
            try:
                await self._save(bot, state)
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
                    return {**self._public_group(bot, group), 'reliable': False, 'reason': 'state_changed'}
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
                    return {**self._public_group(bot, group), 'reliable': False, 'reason': 'state_changed'}
                committed_version = group['version']
                await self._notify(bot, group, 'bot_state_refreshed')
                result = self._public_group(bot, group)
                if group['version'] != committed_version:
                    result.update(reliable=False, reason='state_changed')
                return result
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
            try:
                await self._save(bot, state)
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
                        self._merge_member(updated, {
                            **record,
                            'reliable': reliable,
                            'status': 'present' if reliable else 'pending',
                            'reason': '' if reliable else 'role_missing',
                            'source': 'platform_roster',
                            'checked_at': checked,
                        })
                    if any(not updated['members'][r['member']]['reliable'] for r in report['members']):
                        report.update(complete=False, reason='stale_membership')
                    self._invalidate(candidate, updated, report['reason'])
                    report.update(
                        id=uuid4().hex, generation=self.generation, version=updated['version'], group=native_group, source='platform_roster', checked_at=checked
                    )
                    updated['roster'] = copy.deepcopy(report)

                if not await self._commit(bot, state, native_group, apply):
                    report.update(complete=False, reason='state_changed')
                else:
                    await self._notify(bot, group, 'roster_refreshed')
                    if group['version'] != report['version']:
                        report.update(complete=False, reason='state_changed')
                if not self._ready(bot):
                    report.update(complete=False, continuity_reason='recovery_pending')
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
        previous = copy.deepcopy(record)
        record.update(reliable=False, status='pending', reason='verifying')
        try:
            await self._save(bot, state)
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
            return await self._commit_member(bot, state, native_group, member, fact, previous=previous)
        except BaseException as error:
            if reason := self._abandon(state, group, version, error, 'verification'):
                record.update(status='historical', reason=reason)
            raise

    async def _commit_member(self, bot, state, native_group, member, fact, *, previous=None, manual=False):
        published: dict = {}

        def apply(candidate, updated):
            self._merge_member(updated, {'member': member, **fact}, manual=manual, previous=previous)
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
        await self._notify(bot, group, 'member_confirmed' if manual else 'member_verified')
        return self._member_result(bot, group, published, published['version'])

    async def confirm_member(self, bot, native_group, member, *, evidence, member_role=None):
        identifier(native_group)
        identifier(member)
        if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 1000:
            raise ValueError('请记录主人核验依据，最多 1000 字符')
        if member_role is not None and (not isinstance(member_role, str) or member_role not in ROLES):
            raise ValueError('角色须为 member、admin、owner 或 null')
        state = await self._state(bot)
        group = self._group(state, native_group)
        self._invalidate(state, group, 'individual_verification')
        version = group['version']
        record = group['members'].setdefault(member, {'member': member, 'member_role': None, 'bot': None})
        previous = copy.deepcopy(record)
        record.update(reliable=False, status='pending', reason='confirming')
        fact = {
            'member_role': member_role,
            'reliable': True,
            'status': 'present',
            'reason': '',
            'source': 'owner_confirmed',
            'checked_at': self.clock(),
            'evidence': evidence,
            'state': 'present',
            'confirmation_generation': self.generation,
        }
        try:
            return await self._commit_member(bot, state, native_group, member, fact, previous=previous, manual=True)
        except BaseException as error:
            if reason := self._abandon(state, group, version, error, 'confirmation'):
                record.update(status='historical', reason=reason)
            raise

    def _member_result(self, bot, group, record, version):
        result = {**copy.deepcopy(record), 'group': group['group'], 'version': version, 'generation': self.generation}
        if group['version'] != version or self.pending_writes.get(bot, 0):
            result.update(reliable=False, state='unknown', reason='state_changed')
        if not self._ready(bot):
            result.update(reliable=False, continuity_reason='recovery_pending')
        return result

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
        group = self._public_group(bot, group)
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

    def connect(self, ctx):
        if ctx.recovery_api_version != 1 or ctx.group_lifecycle_api_version != 1:
            raise RuntimeError('群状态需要恢复与群生命周期 v1 契约')
        self.notifications = Notifications(str(Path(ctx.module_dir).parent.resolve() / Path(ctx.module_dir).name))
        self.recovery = ctx.subscribe_recovery_events(self._lifecycle, event_types=LIFECYCLE)
        self.connection_subscription = ctx.subscribe_connection_status(self._connection_change, replay_current=False)
        self.rebuild_locks = {}

    def subscribe(self, callback, *, timeout=5.0):
        if not self.active:
            raise RuntimeError('群状态已卸载')
        return self.notifications.subscribe(callback, timeout=timeout)

    def _ready(self, bot):
        status = self.recovery.get_status(bot)
        webhook = self.recovery.get_status(bot, transport='webhook')
        return (
            self.active and status.ready and not status.pending and webhook.context is None and not self.notifications.busy(bot)
        )

    async def fact(self, bot, group, member=None):
        state = await self._state(bot)
        record = state['groups'].get(group)
        public = self._public_group(bot, record) if record else None
        row = public['members'].get(member) if public and member else None
        status = self.recovery.get_status(bot)
        return {
            'bot': bot,
            'group': group,
            'member_id': member,
            'generation': self.generation,
            'version': record['version'] if record else None,
            'recovery_revision': status.revision,
            'writer_revision': self.notifications.checkpoint(bot),
            'usable': bool(
                public
                and self._ready(bot)
                and not self.pending_writes.get(bot)
                and (row['reliable'] if member and row else public['reliable'] if not member else False)
            ),
            'member': row,
            'group_state': public,
        }

    def fact_current(self, proof):
        if not isinstance(proof, dict) or proof.get('generation') != self.generation or not proof.get('usable'):
            return False
        bot = proof.get('bot')
        if not self._ready(bot) or self.pending_writes.get(bot):
            return False
        state = self.states.get(bot)
        group = state['groups'].get(proof.get('group')) if state else None
        return bool(
            group
            and group['version'] == proof.get('version')
            and self.recovery.get_status(bot).revision == proof.get('recovery_revision')
            and self.notifications.checkpoint(bot) == proof.get('writer_revision')
        )

    async def _notify(self, bot, group, reason, *, event=None):
        change = {'bot': bot, 'group': group['group'], 'generation': self.generation, 'version': group['version'], 'reason': reason}
        if event is not None:
            change['native'] = asdict(event)
        try:
            await self.notifications.publish(change)
        except BaseException:
            self.recovery.invalidate(bot, reason='consumer_failed')
            raise

    async def _connection_change(self, status):
        # Current recovery status gates proofs synchronously, before this async
        # notification. Do not mutate historical presence on a transport failure.
        state = self.states.get(status.appid)
        if state and self.active:
            for group in tuple(state['groups'].values()):
                await self._notify(status.appid, group, 'connection:' + status.phase)

    async def _lifecycle(self, delivery):
        event = delivery.event
        bot = event.appid
        state = await self._state(bot)
        if not valid_native(event.group_openid):
            self.recovery.invalidate(bot, reason='missing_native_group')
            for group in state['groups'].values():
                self._suspend(state, group, 'missing_native_group')
            await self._save(bot, state)
            for group in state['groups'].values():
                await self._notify(bot, group, 'missing_native_group', event=event)
            return
        group = self._group(state, event.group_openid)
        signature = [event.event_type, event.member_openid, event.op_member_openid, event.timestamp]
        events = group.setdefault('events', {})
        if event.event_id and event.event_id in events:
            if events[event.event_id] == signature:
                delivery.acknowledge()
                return
            reason = 'conflicting_duplicate'
        else:
            reason = ''
        timestamp = event_time(event.timestamp)
        is_member = event.event_type in {'GROUP_MEMBER_ADD', 'GROUP_MEMBER_REMOVE'}
        target = (
            group['members'].setdefault(event.member_openid, {'member': event.member_openid, 'member_role': None, 'bot': None, 'cycle': uuid4().hex})
            if is_member and valid_native(event.member_openid)
            else group
        )
        previous = target.get('last_transition', {})
        if not valid_native(event.event_id) or timestamp is None or (is_member and not valid_native(event.member_openid)):
            reason = reason or 'incomplete_event'
        elif previous.get('time') is not None and timestamp <= previous['time']:
            reason = reason or ('same_time_conflict' if timestamp == previous['time'] else 'late_event')
        self._suspend(state, group, reason or 'lifecycle_changed')
        metadata = asdict(event)
        group.update(source='native_lifecycle', observed_at=self.clock(), event_type=event.event_type, last_native=metadata)
        if reason:
            # Ambiguous events never revive an earlier presence or cycle.
            target.update(reliable=False, reason=reason)
            self.recovery.invalidate(bot, reason=reason)
        else:
            added = event.event_type in {'GROUP_MEMBER_ADD', 'GROUP_ADD_ROBOT'}
            if is_member:
                if added:
                    target.update(
                        cycle=uuid4().hex,
                        cycle_anchor=None,
                        checked_at=None,
                        joined_at=None,
                        status='pending',
                        state='unknown',
                        reliable=False,
                        reason='join_requires_verification',
                    )
                else:
                    target.update(status='left', state='left', reliable=True, reason='', left_at=timestamp)
                target.update(source='native_lifecycle', last_transition={'time': timestamp, 'event_id': event.event_id, 'type': event.event_type})
            else:
                group.update(status='pending' if added else 'left', reliable=False, reason='bot_added_requires_baseline' if added else 'bot_removed')
                group['bot_state'] = {**(group.get('bot_state') or {}), 'in_group': added}
                group['last_transition'] = {'time': timestamp, 'event_id': event.event_id, 'type': event.event_type}
                self.recovery.invalidate(bot, reason=group['reason'])
        if event.event_id:
            events[event.event_id] = signature
            while len(events) > 256:
                del events[next(iter(events))]
        await self._save(bot, state)
        await self._notify(bot, group, reason or event.event_type, event=event)
        if not reason:
            delivery.acknowledge()

    def _suspend(self, state, group, reason):
        self._invalidate(state, group, reason)
        group.update(reliable=False, reason=reason)
        for member in group['members'].values():
            member.update(reliable=False, reason=reason)

    def _merge_member(self, group, data, *, manual=False, previous=None):
        previous = previous if previous is not None else group['members'].get(data['member'], {})
        cycle = previous.get('cycle') or uuid4().hex
        joined = data.get('joined_at')
        anchor = previous.get('cycle_anchor') or previous.get('checked_at')
        changed = previous.get('status') == 'left' or (
            joined is not None
            and (
                previous.get('joined_at') is not None
                and joined != previous['joined_at']
                or previous.get('joined_at') is None
                and anchor is not None
                and joined > anchor
            )
        )
        stale = not manual and previous.get('left_at') and (joined is None or joined <= previous['left_at'])
        already_joined = previous.get('last_transition', {}).get('type') == 'GROUP_MEMBER_ADD' and previous.get('status') == 'pending'
        if changed and not stale and not already_joined:
            cycle = uuid4().hex
        result = {**copy.deepcopy(previous), **data, 'cycle': cycle}
        result['cycle_anchor'] = self.clock() if anchor is None or cycle != previous.get('cycle') else anchor
        if stale:
            result.update(reliable=False, status='left' if previous.get('status') == 'left' else 'pending', state='unknown', reason='stale_membership')
        group['members'][data['member']] = result
        return result

    async def rebuild(self, bot):
        async with self.rebuild_locks.setdefault(bot, asyncio.Lock()):
            if not self.active or self.notifications.busy(bot):
                return False
            self.recovery.invalidate(bot, reason='rebuilding')
            checkpoint = self.recovery.get_status(bot)
            state = await self._state(bot)
            groups = list(state['groups'])
            good = True
            for native_group in groups:
                await self.refresh(bot, native_group)
                good = good and state['groups'][native_group]['reliable']
                for member, record in tuple(state['groups'][native_group]['members'].items()):
                    current_manual = (
                        record.get('source') == 'owner_confirmed' and record.get('confirmation_generation') == self.generation and record.get('reliable')
                    )
                    if record.get('status') != 'left' and not current_manual:
                        await self.verify_member(bot, native_group, member)
                        good = good and state['groups'][native_group]['members'][member]['reliable']
            if not good or list(state['groups']) != groups or self.pending_writes.get(bot) or self.notifications.busy(bot):
                return False
            # The checkpoint also rejects events/connection transitions during queries.
            return self.recovery.establish(checkpoint)

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
            elif request.method == 'POST' and path == 'rebuild':
                result = {'ready': await self.rebuild(bot)}
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
        if event.group_id and event.event_type not in LIFECYCLE:
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
            if words == ['群状态', '恢复']:
                result = {'ready': await self.rebuild(event.appid)}
            elif len(words) == 3 and words[1] == '刷新':
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
