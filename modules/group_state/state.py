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
from core.base.logger import EXTENSION, get_logger
from core.plugin._blacklist import get_blacklist_map

from .lifecycle import LIFECYCLE, event_time
from .notifications import Notifications
from .reader import ROLES, MembershipReader
from .reader import native as valid_native
from .storage import ExtensionStore
from .text import owner_reply, short_reply

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
        # Native join bases are current only within one rebuild epoch per Bot.
        self.join_epochs = {}
        # Set by connect(); a partial connect leaves later ones None.
        self.notifications = self.recovery = self.connection_subscription = None

    @staticmethod
    def _sender(bot):
        from core.application import get_app

        app = get_app()
        instance = app.get_bot(bot) if app else None
        return instance.sender if instance else None

    async def initialize(self):
        await self.store.ready()

    def _release(self):
        # Idempotent; releases only what connect() acquired before any failure.
        self.active = False
        if self.notifications is not None:
            self.notifications.close()
        for subscription in (self.recovery, self.connection_subscription):
            if subscription is not None:
                subscription.cancel()

    def close(self):
        self._release()
        self.store.close()

    async def aclose(self):
        self._release()
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
                        member.update(
                            reliable=False, cycle_uncertain=True, status='left' if member.get('status') == 'left' else 'historical', reason='process_restart'
                        )
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
                durable = {key: value['version'] for key, value in candidate['groups'].items()}
                if group['version'] != version:
                    # The candidate's version number can equal a newer live observation's; that fact is not on disk.
                    durable[native] = None
                self.durable[bot] = durable
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

    @staticmethod
    def _failure(error, label):
        if isinstance(error, asyncio.CancelledError):
            return label + '_cancelled'
        return 'persistence_failed' if isinstance(error, (sqlite3.Error, OSError)) else label + '_failed'

    def _abandon(self, state, group, version, error, label):
        # An interrupted operation must not leave its in-progress marker as the latest fact.
        if group['version'] != version:
            return None
        reason = self._failure(error, label)
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
                'recovery': self._recovery_view(bot),
            }
        )

    def _recovery_view(self, bot):
        # ready/phase/reason keep their meaning; pause (the module's own verdict, see _pause_reason)
        # and rebuilding are additive display hints so QQ and page name the same next step.
        status = self.recovery.get_status(bot)
        lock = self.rebuild_locks.get(bot)
        return {
            'ready': self._ready(bot),
            'phase': status.phase,
            'reason': status.reason,
            'pause': self._pause_reason(bot),
            'rebuilding': bool(lock and lock.locked()),
        }

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
    def _settle_roster(group, reason):
        # The roster lock serialises passes per group, so remaining markers are this pass's; newer facts already replaced theirs.
        for record in group['members'].values():
            if record.get('reason') == 'roster_refreshing':
                record.update(reliable=False, status='left' if record.get('status') == 'left' else 'historical', reason=reason)

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
                record.update(reliable=False, status='left' if record.get('status') == 'left' else 'historical', reason='roster_refreshing')
            version = group['version']
            try:
                await self._save(bot, state)
                report = await self.reader.roster(bot, native_group)
                if not self.active:
                    raise RuntimeError('群状态模块已卸载')
                if group['version'] != version:
                    report.update(complete=False, reason='state_changed')
                    self._settle_roster(group, 'state_changed')
                    return copy.deepcopy(report)
                if report['complete'] and known - {r['member'] for r in report['members']}:
                    report.update(complete=False, reason='known_member_missing', missing_known_members=sorted(known - {r['member'] for r in report['members']}))
                checked = self.clock()

                # Duplicates, cursor loops (an unending cursor chain included) and identity contradictions make the whole pass unusable as member proof.
                conflict = report['reason'] in ('pagination_conflict', 'pagination_limit', 'bot_identity_conflict')

                def apply(candidate, updated):
                    for record in [] if conflict else report['members']:
                        reliable = record['member_role'] is not None
                        self._merge_member(updated, {
                            **record,
                            'reliable': reliable,
                            'status': 'present' if reliable else 'pending',
                            'reason': '' if reliable else 'role_missing',
                            'source': 'platform_roster',
                            'checked_at': checked,
                        })
                    if not conflict and any(not updated['members'][r['member']]['reliable'] for r in report['members']):
                        report.update(complete=False, reason='stale_membership')
                    self._settle_roster(updated, report['reason'] or 'roster_refreshed')
                    self._invalidate(candidate, updated, report['reason'])
                    report.update(
                        id=uuid4().hex, generation=self.generation, version=updated['version'], group=native_group, source='platform_roster', checked_at=checked
                    )
                    updated['roster'] = copy.deepcopy(report)

                if not await self._commit(bot, state, native_group, apply):
                    report.update(complete=False, reason='state_changed')
                    self._settle_roster(group, 'state_changed')
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
                self._settle_roster(group, self._failure(error, 'roster'))
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

    async def _commit_member(self, bot, state, native_group, member, fact, *, previous=None, manual=False, continuity_unknown=False):
        published: dict = {}

        def apply(candidate, updated):
            self._merge_member(updated, {'member': member, **fact}, manual=manual, previous=previous, continuity_unknown=continuity_unknown)
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
            'confirmation_revision': self.recovery.get_status(bot).revision,
        }
        try:
            return await self._commit_member(
                bot, state, native_group, member, fact, previous=previous, manual=True,
                continuity_unknown=self.recovery.get_status(bot).phase in {'needs_baseline', 'unsupported', 'closed'},
            )
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
        # A connection status is current (and queued for this subscription) as soon
        # as it is published; facts stay paused until this instance delivered it.
        # Statuses before subscribing are never delivered (replay_current=False).
        # One entry per Bot/transport, so it does not grow with reconnects.
        self.current_connection_statuses = lambda: ctx.connection_statuses
        self.webhook_status = lambda bot: ctx.get_connection_status(bot, transport='webhook')
        self.websocket_status = lambda bot: ctx.get_connection_status(bot, transport='websocket')
        self.connection_baseline = {(status.appid, status.transport): status.revision for status in ctx.connection_statuses}
        self.connection_settled = dict(self.connection_baseline)
        # Highest status our callback started for, whatever its outcome.
        self.connection_attempted = dict(self.connection_baseline)
        self.connection_subscription = ctx.subscribe_connection_status(self._connection_change, replay_current=False)
        self.rebuild_locks = {}

    def subscribe(self, callback, *, timeout=5.0):
        if not self.active:
            raise RuntimeError('群状态已卸载')
        return self.notifications.subscribe(callback, timeout=timeout)

    def _ready(self, bot):
        status = self.recovery.get_status(bot)
        return (
            self.active
            and status.ready
            and not status.pending
            and not self._webhook_delivered(bot)
            and not self.notifications.busy(bot)
            and not self._connection_pending(bot)
        )

    def _connection_pending(self, bot):
        # Only a completed delivery settles a status. An error, timeout or a status the
        # framework skips as 'busy' behind a timed-out callback invalidated recovery in
        # _notify, so it reports recovery_required until rebuild() settles it.
        return any(
            status.revision > self.connection_settled.get((bot, status.transport), 0) for status in self.current_connection_statuses() if status.appid == bot
        )

    def _webhook_delivered(self, bot):
        # Every BotInstance registers, starts and stops an HTTP callback source; those statuses
        # carry no event and settle like any connection status. receive_index counts callbacks
        # this Bot instance actually read (updated without a new revision). HTTP has no
        # continuity proof, so once it delivered anything this Bot instance stays untrusted.
        # Source agreement alone is insufficient: both may belong to an orphan while a
        # WS-disabled instance serves HTTP. Resolve the same registry used by WebhookHandler.
        from core.application import get_app

        app = get_app()
        serving = app.get_bot(bot) if app else None
        status = self.webhook_status(bot)
        if status is None:
            # Standalone WS adapters have no HTTP receiver/source. A real HTTP-capable
            # instance without its source is unknown, never evidence of zero receipts.
            return callable(getattr(serving, 'observe_webhook', None))
        if status.context.receive_index > 0 or not self._same_instance(status, self.websocket_status(bot)):
            return True
        generation = getattr(serving, 'bot_generation', None)
        runtime = getattr(getattr(serving, 'connection_status', None), 'runtime_id', None)
        return not (generation and runtime and (runtime, generation) == (status.context.runtime_id, status.context.bot_generation))

    @staticmethod
    def _same_instance(webhook, websocket):
        # A BotInstance registers its HTTP source at construction and its WS source when its
        # WSClient is created (start or hot update), both with its bot_generation; the registry
        # serves callbacks with the instance whose start() finished last. Sources owned by
        # different instances (one superseded the other's, e.g. overlapping config syncs) mean
        # the serving instance may count callbacks into a source that no longer publishes, so
        # receive_index cannot show they did not happen. Missing identities never match.
        if websocket is None:
            return False
        http, ws = webhook.context, websocket.context
        return bool(http.runtime_id and http.bot_generation) and (http.runtime_id, http.bot_generation) == (ws.runtime_id, ws.bot_generation)

    def _pause_reason(self, bot):
        """Why this Bot's facts are paused: '' when ready; 'recovery_processing' while a
        trusted boundary or consumer notification is still finishing and
        'persistence_pending' while a write is (both: retry later); otherwise
        'recovery_required' (a rebuild must establish a new baseline)."""
        if self._ready(bot):
            return 'persistence_pending' if self.pending_writes.get(bot) else ''
        status = self.recovery.get_status(bot)
        if self.active and not self._webhook_delivered(bot) and status.phase in {'ready', 'processing', 'recovering'}:
            return 'recovery_processing'
        return 'recovery_required'

    def _join(self, bot, group, record):
        """The member's native join basis while current: this generation and rebuild epoch, same cycle, not left."""
        basis = record.get('native_join') if record else None
        if not (
            basis
            and basis.get('generation') == self.generation
            and basis.get('epoch') == self.join_epochs.get(bot, 0)
            and basis.get('cycle') == record.get('cycle')
            and record.get('status') != 'left'
        ):
            return None
        return {**copy.deepcopy(basis), 'bot': bot, 'group': group, 'member': record.get('member')}

    async def native_join(self, bot, group, member):
        """Current native join basis regardless of pause, for writing a source inside a consumer notification."""
        state = await self._state(bot)
        record = state['groups'].get(group, {}).get('members', {}).get(member)
        return self._join(bot, group, record)

    async def fact(self, bot, group, member=None):
        state = await self._state(bot)
        record = state['groups'].get(group)
        return self._proof(bot, group, record, self._public_group(bot, record) if record else None, member)

    async def facts(self, bot, group):
        """One group proof plus per-member proofs from the same public copy.

        Each member proof is checked with ``fact_current`` exactly like ``fact``
        but omits ``group_state``, avoiding a group copy per roster member.
        """
        state = await self._state(bot)
        record = state['groups'].get(group)
        public = self._public_group(bot, record) if record else None
        members = {member: {**self._proof(bot, group, record, public, member), 'group_state': None} for member in (public['members'] if public else {})}
        return self._proof(bot, group, record, public), members

    def _proof(self, bot, group, record, public, member=None):
        row = public['members'].get(member) if public and member else None
        status = self.recovery.get_status(bot)
        paused = self._pause_reason(bot)
        usable = bool(public and not paused and (row['reliable'] if member and row else public['reliable'] if not member else False))
        join = self._join(bot, group, row) if row and not paused else None
        return {
            'bot': bot,
            'group': group,
            'member_id': member,
            'generation': self.generation,
            'version': record['version'] if record else None,
            'recovery_revision': status.revision,
            'writer_revision': self.notifications.checkpoint(bot),
            'usable': usable,
            'reason': '' if usable else paused or 'unverified',
            'join': join,
            'member': row,
            'group_state': public,
        }

    def fact_current(self, proof):
        return isinstance(proof, dict) and bool(proof.get('usable')) and self._fresh(proof)

    def join_current(self, proof):
        """Like ``fact_current`` for a proof whose member is backed only by its native join basis."""
        return isinstance(proof, dict) and bool(proof.get('join')) and self._fresh(proof)

    def _fresh(self, proof):
        if proof.get('generation') != self.generation:
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
        key = status.appid, status.transport
        self.connection_attempted[key] = max(self.connection_attempted.get(key, 0), status.revision)
        state = self.states.get(status.appid)
        if state and self.active:
            for group in tuple(state['groups'].values()):
                await self._notify(status.appid, group, 'connection:' + status.phase)
        self._settle_connection(status)

    def _settle_connection(self, status):
        key = status.appid, status.transport
        self.connection_settled[key] = max(self.connection_settled.get(key, 0), status.revision)

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
        if reason or not is_member:
            self._suspend(state, group, reason or 'lifecycle_changed')
        else:
            # A trusted member transition changes only that member's facts. The version
            # bump still retires older snapshots, roster proofs and in-flight checks.
            self._invalidate(state, group, 'lifecycle_changed')
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
                    cycle = uuid4().hex
                    # A join basis needs WS continuity: an established baseline and no failure when the callback runs.
                    status = self.recovery.get_status(bot)
                    trusted = event.transport == 'websocket' and not status.failed and status.phase in {'ready', 'processing', 'recovering'}
                    target.update(
                        cycle=cycle,
                        cycle_anchor=None,
                        native_join={
                            'event_id': event.event_id,
                            'timestamp': timestamp,
                            'sequence': event.sequence,
                            'session_id': event.session_id,
                            'cycle': cycle,
                            'generation': self.generation,
                            'epoch': self.join_epochs.get(bot, 0),
                        }
                        if trusted
                        else None,
                        checked_at=None,
                        joined_at=None,
                        status='pending',
                        state='unknown',
                        reliable=False,
                        reason='join_requires_verification',
                    )
                else:
                    target.update(status='left', state='left', reliable=True, reason='', left_at=timestamp, native_join=None)
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
            member.update(reliable=False, reason=reason, native_join=None)

    def _merge_member(self, group, data, *, manual=False, previous=None, continuity_unknown=False):
        previous = previous if previous is not None else group['members'].get(data['member'], {})
        cycle = previous.get('cycle') or uuid4().hex
        joined = data.get('joined_at')
        anchor = previous.get('cycle_anchor') or previous.get('checked_at')
        changed = (
            previous.get('status') == 'left'
            or manual
            and (previous.get('cycle_uncertain') or continuity_unknown)
            or (
                joined is not None
                and (
                    previous.get('joined_at') is not None
                    and joined != previous['joined_at']
                    or previous.get('joined_at') is None
                    and anchor is not None
                    and joined > anchor
                )
            )
        )
        stale = not manual and previous.get('left_at') and (joined is None or joined <= previous['left_at'])
        already_joined = not manual and previous.get('last_transition', {}).get('type') == 'GROUP_MEMBER_ADD' and previous.get('status') == 'pending'
        if changed and not stale and not already_joined:
            cycle = uuid4().hex
        result = {**copy.deepcopy(previous), **data, 'cycle': cycle}
        result['cycle_anchor'] = self.clock() if anchor is None or cycle != previous.get('cycle') else anchor
        if stale:
            result.update(reliable=False, status='left' if previous.get('status') == 'left' else 'pending', state='unknown', reason='stale_membership')
        if result.get('reliable'):
            result['cycle_uncertain'] = False
        group['members'][data['member']] = result
        return result

    async def rebuild(self, bot):
        async with self.rebuild_locks.setdefault(bot, asyncio.Lock()):
            if not self.active or self.notifications.busy(bot):
                return False
            previous_revision = self.recovery.get_status(bot).revision
            self.recovery.invalidate(bot, reason='rebuilding')
            # Continuity before this rebuild is unproven: earlier native join bases never return.
            self.join_epochs[bot] = self.join_epochs.get(bot, 0) + 1
            checkpoint = self.recovery.get_status(bot)
            unsettled = [
                status
                for status in self.current_connection_statuses()
                if status.appid == bot and status.revision > self.connection_settled.get((bot, status.transport), 0)
            ]
            state = await self._state(bot)
            groups = list(state['groups'])
            good = True
            for native_group in groups:
                await self.refresh(bot, native_group)
                good = good and state['groups'][native_group]['reliable']
                for member, record in tuple(state['groups'][native_group]['members'].items()):
                    current_manual = (
                        record.get('source') == 'owner_confirmed'
                        and record.get('confirmation_generation') == self.generation
                        and record.get('confirmation_revision') == previous_revision
                        and record.get('reliable')
                    )
                    if record.get('status') != 'left' and not current_manual:
                        await self.verify_member(bot, native_group, member)
                        good = good and state['groups'][native_group]['members'][member]['reliable']
            failed = []
            for status in unsettled:
                if status.revision > self.connection_attempted.get((bot, status.transport), 0):
                    # Still queued or skipped as 'busy': consumers never received it, so deliver it
                    # now; it settles once delivered (recovery stays invalidated until establish).
                    await self._connection_change(status)
                else:
                    # Our callback failed on it (a running one keeps notifications busy and fails the
                    # check below); the refresh above re-notified every group after it.
                    failed.append(status)
            if not good or list(state['groups']) != groups or self.pending_writes.get(bot) or self.notifications.busy(bot):
                return False
            # The checkpoint also rejects events/connection transitions during queries; an HTTP
            # receipt bumps no revision, so check it in the same step as establishing.
            if self._webhook_delivered(bot) or not self.recovery.establish(checkpoint):
                return False
            for status in failed:
                self._settle_connection(status)
            return True

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
                operation, result = 'rebuild', {'ready': await self.rebuild(event.appid)}
            elif len(words) == 3 and words[1] == '刷新':
                operation, result = 'refresh', await self.refresh(event.appid, words[2])
            elif len(words) == 3 and words[1] == '名册':
                operation, result = 'roster', await self.sync_roster(event.appid, words[2])
            elif len(words) == 4 and words[1] == '核验':
                operation, result = 'member', await self.verify_member(event.appid, words[2], words[3])
            elif text.startswith('群状态 确认 '):
                data = json.loads(text.split(maxsplit=2)[2])
                if not isinstance(data, dict) or set(data) - {'group', 'member', 'evidence', 'member_role'}:
                    raise ValueError('人工确认须为成员事实对象')
                operation = 'confirm'
                result = await self.confirm_member(
                    event.appid, data.get('group'), data.get('member'), evidence=data.get('evidence'), member_role=data.get('member_role')
                )
            elif len(words) == 1:
                operation, result = 'overview', await self.snapshot(event.appid)
            else:
                raise ValueError('群状态 / 群状态 刷新或名册 <群ID> / 群状态 核验 <群ID> <成员ID> / 群状态 确认 <JSON>')
            content = short_reply(operation) if event.is_group else owner_reply(operation, result, pause=self._pause_reason(event.appid))
        except ValueError as error:
            content = str(error)
        except (RuntimeError, sqlite3.Error, OSError):
            content = '群状态存储暂不可用，请检查后台'
        try:
            await sender.reply(event, content)
        except Exception as error:
            # The command is consumed either way: the platform may have delivered it, so neither resend nor fall through.
            log.error(f'群状态命令回复失败，结果未知，不重发: {type(error).__name__}: {error}')
        return None
