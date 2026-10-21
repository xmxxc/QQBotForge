"""显式消费者确认与可信协议边界组合；不持久化或推断业务状态。"""

import asyncio
import inspect
import json
import math
from collections import OrderedDict
from dataclasses import dataclass, field
from hashlib import sha256
from types import MappingProxyType
from uuid import uuid4

from core.module.connection_status import ConnectionContext
from core.module.group_lifecycle import GROUP_LIFECYCLE_TYPES, GroupLifecycleEvent

RECOVERY_API_VERSION = 1


def _session(context):
    return (context.runtime_id, context.bot_generation, context.session_generation, context.session_id)


@dataclass(frozen=True, slots=True)
class RecoveryStatus:
    subscription_id: str
    appid: str
    transport: str
    revision: int
    context: ConnectionContext | None
    phase: str
    reason: str
    ready: bool
    pending: int
    boundary_pending: int
    replay_end_sequence: int | None
    replay_end_receive_index: int | None
    failed: bool
    dependencies: tuple['RecoveryStatus', ...] = ()


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class RecoveryEvent:
    """非群生命周期事件的接收快照，不与普通插件共享可变 Event。"""

    appid: str
    event_type: str
    event_id: str | None
    raw: object
    connection_context: ConnectionContext | None


@dataclass(slots=True)
class _Confirmation:
    acknowledged: bool = False
    active: bool = True


@dataclass(frozen=True, slots=True)
class RecoveryDelivery:
    """必须在承诺的更新完成后确认；返回回调和派生后台任务都不代替确认。"""

    event: GroupLifecycleEvent | RecoveryEvent
    context: ConnectionContext
    _confirmation: _Confirmation = field(default_factory=_Confirmation, repr=False, compare=False)

    def acknowledge(self) -> bool:
        if not self._confirmation.active:
            return False
        self._confirmation.acknowledged = True
        return True

    def _expire(self):
        self._confirmation.active = False


@dataclass(slots=True)
class _Progress:
    context: ConnectionContext | None = None
    revision: int = 0
    baseline: bool = False
    failure: str | None = None
    pending: dict = field(default_factory=dict)
    seen: OrderedDict = field(default_factory=OrderedDict)
    high_sequence: int | None = None


class RecoverySubscription:
    """每个消费者拥有独立的基线、失败和有界在途处理。"""

    def __init__(self, manager, callback, owner, event_types, appid, timeout, max_pending, depends_on, *, _scope=None):
        self.subscription_id = uuid4().hex
        self.owner = owner
        self._scope = object() if _scope is None else _scope
        self.event_types = event_types
        self.appid = appid
        self.timeout = timeout
        self.max_pending = max_pending
        self.depends_on = depends_on
        self._manager = manager
        self._callback = callback
        self._progress: dict[tuple[str, str], _Progress] = {}
        self._tickets: dict[str, tuple[_Progress, RecoveryDelivery]] = {}
        self._running: dict[asyncio.Task, tuple[str, str]] = {}
        self._workers: set[asyncio.Task] = set()
        self._group = None
        self._closed = None

    @property
    def active(self):
        return self._closed is None

    def _observe(self, status):
        if self.appid is not None and self.appid != status.appid:
            return
        key = (status.appid, status.transport)
        progress = self._progress.setdefault(key, _Progress())
        if progress.context is not None and _session(progress.context) != _session(status.context):
            self._retire(key, progress)
            self._manager._invalidate_dependents(self, *key)
            progress = self._progress[key] = _Progress(revision=progress.revision)
        progress.context = status.context
        progress.revision += 1
        if status.phase in {'unknown', 'identifying', 'resume_failed', 'stopped'}:
            progress.baseline = False
            progress.failure = status.reason
            self._manager._invalidate_dependents(self, *key)
            if status.phase == 'stopped':
                self._retire(key, progress)

    def _retire(self, key, progress):
        for delivery_id, delivery in tuple(progress.pending.items()):
            delivery._expire()
            self._tickets.pop(delivery_id, None)
        progress.pending.clear()
        for task, scope in tuple(self._running.items()):
            if scope == key:
                self._manager._abandon(self, task, scope)
                task.cancel()

    def get_status(self, appid, *, transport='websocket') -> RecoveryStatus:
        appid = str(appid)
        key = (appid, transport)
        progress = self._progress.get(key, _Progress())
        status = self._manager.connections.get(appid, transport=transport)
        phase, reason, ready = 'needs_baseline', 'baseline_required', False
        end_sequence = status.replay_end_sequence if status else None
        end_index = status.replay_end_receive_index if status else None
        boundary_pending = len(progress.pending)
        if status and status.phase == 'replay_complete' and end_index is not None:
            boundary_pending = sum(
                delivery.context is None
                or delivery.context.connection_generation != status.context.connection_generation
                or delivery.context.receive_index <= end_index
                for delivery in progress.pending.values()
            )
        if self._closed:
            phase, reason = 'closed', self._closed
        elif transport != 'websocket' or (status and not status.capabilities.replay_boundary):
            phase, reason = 'unsupported', 'unverified_transport'
        elif self._manager._blocked(self._scope, key):
            phase, reason = 'needs_baseline', 'old_callback_running'
        elif progress.failure:
            phase, reason = 'needs_baseline', progress.failure
        elif progress.baseline and status:
            if status.phase == 'replay_complete' and status.continuity == 'resumed':
                phase, reason, ready = 'ready', 'replay_processed', boundary_pending == 0
                if not ready:
                    phase, reason = 'processing', 'pending_processing'
            elif status.phase == 'ready':
                ready = not progress.pending
                phase, reason = ('ready', 'baseline_established') if ready else ('processing', 'pending_processing')
            else:
                phase, reason = 'recovering', status.reason
        dependencies = tuple(dep.get_status(appid, transport=transport) for dep in self.depends_on)
        if ready and any(not dep.ready for dep in dependencies):
            phase, reason, ready = 'dependency_failed', 'dependency_not_ready', False
        return RecoveryStatus(
            self.subscription_id,
            appid,
            transport,
            progress.revision,
            progress.context,
            phase,
            reason,
            ready,
            len(progress.pending),
            boundary_pending,
            end_sequence,
            end_index,
            progress.failure is not None,
            dependencies,
        )

    def establish(self, checkpoint: RecoveryStatus):
        """调用者已重建状态后确认基线；拒绝重建期间发生变化的旧凭据。"""
        if not isinstance(checkpoint, RecoveryStatus) or checkpoint.subscription_id != self.subscription_id:
            return False
        current = self.get_status(checkpoint.appid, transport=checkpoint.transport)
        status = self._manager.connections.get(checkpoint.appid, transport=checkpoint.transport)
        if (
            not self.active
            or current != checkpoint
            or current.pending
            or current.context is None
            or (self.appid is not None and self.appid != checkpoint.appid)
            or checkpoint.transport != 'websocket'
            or status is None
            or not status.capabilities.replay_boundary
            or status.phase not in {'ready', 'replay_complete'}
            or not status.context.session_generation
            or self._manager._blocked(self._scope, (checkpoint.appid, checkpoint.transport))
            or (checkpoint.appid, checkpoint.transport) in self._running.values()
            or any(not dep.ready for dep in current.dependencies)
        ):
            return False
        progress = self._progress[(checkpoint.appid, checkpoint.transport)]
        progress.baseline, progress.failure = True, None
        progress.revision += 1
        return True

    def invalidate(self, appid, *, transport='websocket', reason='consumer_invalidated'):
        """共享事实失效时显式关闭信任；声明的依赖者同步看到未就绪。"""
        progress = self._progress.setdefault((str(appid), transport), _Progress())
        progress.baseline, progress.failure = False, reason
        progress.revision += 1
        self._manager._invalidate_dependents(self, str(appid), transport)

    def cancel(self):
        self._close('unsubscribed')

    def _close(self, reason):
        if self._closed:
            return
        self._closed = reason
        self._manager._subscriptions.pop(self.subscription_id, None)
        if self._group:
            self._group._close(reason)
        for key, progress in self._progress.items():
            self._manager._invalidate_dependents(self, *key)
            progress.revision += 1
            for delivery in progress.pending.values():
                delivery._expire()
            progress.pending.clear()
        self._tickets.clear()
        for task, scope in self._running.items():
            self._manager._abandon(self, task, scope)
        for task in (*self._running, *self._workers):
            task.cancel()

    def _prepare(self, event, delivery_id):
        context = event.connection_context
        appid = str(event.appid)
        transport = context.transport if context else 'internal'
        if not self.active or event.event_type not in self.event_types or (self.appid is not None and self.appid != appid):
            return None
        key = (appid, transport)
        progress = self._progress.setdefault(key, _Progress())
        progress.revision += 1
        if self._manager._blocked(self._scope, key):
            self.invalidate(appid, transport=transport, reason='old_callback_running')
            return None
        if (
            context is None
            or progress.context is None
            or _session(context) != _session(progress.context)
            or context.connection_generation != progress.context.connection_generation
        ):
            self.invalidate(appid, transport=transport, reason='unproven_event_source')
            return None
        sequence = context.sequence
        if type(sequence) is not int:
            self.invalidate(appid, transport=transport, reason='missing_sequence')
            return None
        # 序号只在同一实际会话内使用；不补造平台序号或承诺无限去重。
        raw = getattr(event, 'raw', None)
        identity = sha256(
            json.dumps(
                raw
                if raw is not None
                else (
                    event.event_type,
                    event.event_id,
                    event.group_openid,
                    event.member_openid,
                    event.op_member_openid,
                    event.timestamp,
                ),
                sort_keys=True,
                ensure_ascii=True,
            ).encode()
        ).digest()
        if sequence in progress.seen:
            if progress.seen[sequence] != identity:
                self.invalidate(appid, transport=transport, reason='conflicting_duplicate')
            return None
        if progress.high_sequence is not None and sequence <= progress.high_sequence:
            self.invalidate(appid, transport=transport, reason='late_event')
            return None
        progress.high_sequence = sequence
        progress.seen[sequence] = identity
        if len(progress.seen) > 256:
            progress.seen.popitem(last=False)
        if len(progress.pending) >= self.max_pending or sum(scope == key for scope in self._running.values()) >= self.max_pending:
            self.invalidate(appid, transport=transport, reason='overflow')
            return None
        if not isinstance(event, GroupLifecycleEvent):
            event = RecoveryEvent(event.appid, event.event_type, event.event_id, _freeze(event.raw), context)
        delivery = RecoveryDelivery(event, context)
        progress.pending[delivery_id] = delivery
        self._tickets[delivery_id] = (progress, delivery)
        return delivery

    def _finish(self, delivery_id, outcome):
        item = self._tickets.pop(delivery_id, None)
        if item is None:
            return
        progress, delivery = item
        delivery._expire()
        if progress is not self._progress.get((delivery.context.appid, delivery.context.transport)):
            return
        progress.pending.pop(delivery_id, None)
        progress.revision += 1
        if outcome != 'success' or not delivery._confirmation.acknowledged:
            self.invalidate(delivery.context.appid, transport=delivery.context.transport, reason=outcome if outcome != 'success' else 'unconfirmed')

    async def _on_group(self, event):
        item = self._tickets.get(event.delivery_id)
        if item is not None and item[1]._confirmation.active:
            if self._manager._blocked(self._scope, (event.appid, event.transport)):
                self._finish(event.delivery_id, 'old_callback_running')
                return
            task = asyncio.current_task()
            assert task is not None
            self._running[task] = (event.appid, event.transport)
            task.add_done_callback(lambda finished: self._release(finished, item[0]))
            await self._callback(item[1])

    def _group_result(self, result):
        self._finish(result.event.delivery_id, result.status)

    def _generic(self, event):
        delivery_id = uuid4().hex
        delivery = self._prepare(event, delivery_id)
        if delivery is None:
            return
        task = asyncio.create_task(self._invoke(delivery_id, delivery))
        self._workers.add(task)
        task.add_done_callback(self._workers.discard)
        task.add_done_callback(_consume)

    async def _invoke(self, delivery_id, delivery):
        if not self.active or not delivery._confirmation.active:
            self._finish(delivery_id, 'cancelled')
            return
        if self._manager._blocked(self._scope, (delivery.context.appid, delivery.context.transport)):
            self._finish(delivery_id, 'old_callback_running')
            return
        task = asyncio.create_task(self._run_callback(delivery))
        self._running[task] = (delivery.context.appid, delivery.context.transport)
        progress = self._progress[(delivery.context.appid, delivery.context.transport)]
        task.add_done_callback(lambda finished: self._release(finished, progress))
        outcome = 'cancelled'
        try:
            done, _ = await asyncio.wait((task,), timeout=self.timeout)
            if not done:
                outcome = 'timeout'
            elif not task.cancelled():
                outcome = 'error' if task.exception() else 'success'
        finally:
            self._finish(delivery_id, outcome)
            if not task.done():
                task.cancel()

    async def _run_callback(self, delivery):
        # eager 工厂也先让创建方登记真实回调及外层任务，再执行用户代码。
        await asyncio.sleep(0)
        await self._callback(delivery)

    def _release(self, task, progress: _Progress):
        key = self._running.pop(task, None)
        if self.active and key is not None and self._progress.get(key) is progress:
            # 超时结果不代表写入者已退出；期间的重建凭据也必须单调过期。
            progress.revision += 1
        _consume(task)


def _consume(task):
    if not task.cancelled():
        task.exception()


class RecoveryManager:
    """复用成员投递结果，直接观察同步连接事实，不依赖异步通知排空。"""

    api_version = RECOVERY_API_VERSION

    def __init__(self, connections, groups):
        self.connections = connections
        self.groups = groups
        self._subscriptions = {}
        # 注销订阅不代表它的业务写入者已经退出；守卫必须跨重载存活。
        self._abandoned = {}
        connections._observers.append(self._observe)
        connections._dispatch_observers.append(self._audit_dispatch)

    def _observe(self, status):
        for subscription in tuple(self._subscriptions.values()):
            subscription._observe(status)

    def _blocked(self, scope, key):
        return (scope, *key) in self._abandoned.values()

    def _abandon(self, source, task, key):
        if task.done() or task in self._abandoned:
            return
        scope = (source._scope, *key)
        self._abandoned[task] = scope
        for subscription in tuple(self._subscriptions.values()):
            if subscription._scope == source._scope and (subscription.appid is None or subscription.appid == key[0]):
                subscription.invalidate(key[0], transport=key[1], reason='old_callback_running')

        def release(finished):
            self._abandoned.pop(finished, None)
            # 屏障期间开始的重建可能已被旧写入覆盖，退出也必须使其凭据过期。
            for subscription in tuple(self._subscriptions.values()):
                if subscription._scope == scope[0] and (subscription.appid is None or subscription.appid == key[0]):
                    progress = subscription._progress.setdefault(key, _Progress())
                    progress.revision += 1

        task.add_done_callback(release)

    def _audit_dispatch(self, event):
        prepared = event._group_lifecycle_prepared if event.event_type in GROUP_LIFECYCLE_TYPES else event._recovery_prepared
        if not prepared:
            for subscription in tuple(self._subscriptions.values()):
                if event.event_type in subscription.event_types and (subscription.appid is None or subscription.appid == event.appid):
                    subscription.invalidate(event.appid, reason='unobserved_dispatch')

    def _invalidate_dependents(self, source, appid, transport):
        for subscription in tuple(self._subscriptions.values()):
            if source in subscription.depends_on and (subscription.appid is None or subscription.appid == appid):
                subscription.invalidate(appid, transport=transport, reason='dependency_invalidated')

    def subscribe(self, callback, *, owner='unknown', event_types, appid=None, timeout=5.0, max_pending=128, depends_on=(), _scope=None):
        if not inspect.iscoroutinefunction(callback):
            raise TypeError('recovery callbacks must be async functions')
        if isinstance(event_types, str):
            raise TypeError('event_types must be a collection of event names')
        event_types = frozenset(event_types)
        if not event_types or any(not isinstance(name, str) or not name or name in {'READY', 'RESUMED'} for name in event_types):
            raise ValueError('event_types must contain dispatch event names')
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('timeout must be finite and positive')
        if type(max_pending) is not int or max_pending <= 0:
            raise ValueError('max_pending must be a positive integer')
        depends_on = tuple(depends_on)
        if any(not isinstance(dep, RecoverySubscription) or dep._manager is not self or not dep.active for dep in depends_on):
            raise ValueError('dependencies must be active subscriptions from this application')
        subscription = RecoverySubscription(self, callback, owner, event_types, str(appid) if appid is not None else None,
                                            timeout, max_pending, depends_on, _scope=_scope)
        self._subscriptions[subscription.subscription_id] = subscription
        for status in self.connections.statuses:
            subscription._observe(status)
        if event_types & GROUP_LIFECYCLE_TYPES:
            group = self.groups.subscribe(subscription._on_group, owner=owner, appid=appid, timeout=timeout)
            group._on_prepare = lambda event: subscription._prepare(event, event.delivery_id)
            group._on_result = subscription._group_result
            subscription._group = group
        return subscription

    def prepare(self, event):
        if event._recovery_prepared:
            return
        event._recovery_prepared = True
        if event.event_type not in GROUP_LIFECYCLE_TYPES:
            for subscription in tuple(self._subscriptions.values()):
                subscription._generic(event)
