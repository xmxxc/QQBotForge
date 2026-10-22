"""Bounded consumer completion and real-writer barriers across module reloads."""

import asyncio
import inspect
import math
from collections import deque
from dataclasses import dataclass
from types import MappingProxyType

# This imported submodule survives ModuleManager's entry-point reload. Installation
# path separates independent copies; unfinished real writers remain quarantined.
_writers: dict[tuple[str, str], set[asyncio.Task]] = {}
_revisions: dict[tuple[str, str], int] = {}


def freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class NotificationResult:
    bot: str
    generation: str
    version: int
    status: str
    error: str | None = None


class Subscription:
    def __init__(self, hub, callback, timeout):
        self.hub, self.callback, self.timeout = hub, callback, timeout
        self.closed = asyncio.Event()
        self.close_reason = None
        self.history: deque[NotificationResult] = deque(maxlen=100)
        self.tasks = set()

    @property
    def active(self):
        return not self.closed.is_set()

    @property
    def results(self):
        return tuple(self.history)

    def cancel(self):
        self.close('cancelled')

    def close(self, reason):
        if self.active:
            self.close_reason = reason
            self.closed.set()
            for task in self.tasks:
                task.cancel()

    async def invoke(self, change):
        status, error = self.close_reason or 'cancelled', None
        if self.active:

            async def call():
                await asyncio.sleep(0)  # register the actual writer before eager execution
                await self.callback(freeze(change))

            task = asyncio.create_task(call())
            self.tasks.add(task)
            key = self.hub.scope, change['bot']
            _writers.setdefault(key, set()).add(task)

            def release(finished):
                self.tasks.discard(finished)
                _writers[key].discard(finished)
                _revisions[key] = _revisions.get(key, 0) + 1
                if not finished.cancelled():
                    finished.exception()

            task.add_done_callback(release)
            closed = asyncio.create_task(self.closed.wait())
            try:
                done, _ = await asyncio.wait((task, closed), timeout=self.timeout, return_when=asyncio.FIRST_COMPLETED)
                if not self.active:
                    status = self.close_reason or 'cancelled'
                elif task not in done:
                    status = 'timeout'
                elif task.cancelled():
                    status = 'cancelled'
                else:
                    exc = task.exception()
                    status, error = ('error', type(exc).__name__) if exc else ('success', None)
            except asyncio.CancelledError:
                status = self.close_reason or 'cancelled'
                raise
            finally:
                closed.cancel()
                if not task.done():
                    task.cancel()
                self.history.append(NotificationResult(change['bot'], change['generation'], change['version'], status, error))
        return status == 'success'


class Notifications:
    def __init__(self, scope):
        self.scope = scope
        self.subscriptions = []
        self.pending = {}
        self.locks = {}

    def subscribe(self, callback, *, timeout=5.0):
        if (
            not inspect.iscoroutinefunction(callback)
            or isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError('消费回调须为 async，timeout 须为有限正数')
        result = Subscription(self, callback, timeout)
        self.subscriptions.append(result)
        return result

    def checkpoint(self, bot):
        return _revisions.get((self.scope, bot), 0)

    def busy(self, bot):
        return bool(self.pending.get(bot) or _writers.get((self.scope, bot)))

    async def publish(self, change):
        bot = change['bot']
        self.pending[bot] = self.pending.get(bot, 0) + 1
        try:
            async with self.locks.setdefault(bot, asyncio.Lock()):
                # Current notifications finish in order. Only a writer that
                # outlived its publication can remain after acquiring this lock.
                if _writers.get((self.scope, bot)):
                    raise RuntimeError('旧消费回调尚未退出')
                for subscription in tuple(self.subscriptions):
                    if subscription.active and not await subscription.invoke(change):
                        raise RuntimeError('消费回调未完成')
        finally:
            self.pending[bot] -= 1

    def close(self):
        for subscription in self.subscriptions:
            subscription.close('unloaded')
