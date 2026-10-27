"""Persistent Ninja3 emergency mode orchestration.

The mode is deliberately separate from password-task ownership.  Entering it
first commits a fail-closed row, then delegates each account to the normal
single-account task service.  A restart preserves the row and task IDs but
does not silently start another password change.
"""

import asyncio
import contextlib
import json
import os
import sqlite3
import time
from collections.abc import Awaitable, Callable
from contextlib import closing
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from .password_tasks import PasswordTasks, TaskError
from .storage import ExtensionStore

Record = dict[str, Any]
PUBLIC_FIELDS = ('state', 'phase', 'generation', 'accounts', 'created_at', 'updated_at', 'notification', 'error', 'exit_requested')
_LIVE_RUNNERS: set[tuple[int, str]] = set()


class EmergencyError(ValueError):
    """Safe, owner-facing emergency mode errors."""


class EmergencyMode:
    def __init__(
        self,
        store: ExtensionStore,
        tasks: PasswordTasks,
        *,
        notify: Callable[[str, Record], Awaitable[str]],
        clock: Callable[[], float] = time.time,
    ):
        self.store = store
        self.tasks = tasks
        self.notify = notify
        self.clock = clock
        self.runners: dict[str, asyncio.Task[None]] = {}
        self.closed = False
        self._fail_closed: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._active_cache: dict[str, bool] = {}
        self._ready = False
        self.runner_id = uuid4().hex

    @staticmethod
    def _read(conn: sqlite3.Connection, bot: str) -> Record | None:
        row = conn.execute('SELECT payload FROM ninja3_emergency WHERE bot=? AND id=?', (bot, 'state')).fetchone()
        return dict(json.loads(row[0])) if row else None

    @staticmethod
    def _save(conn: sqlite3.Connection, bot: str, state: Record) -> None:
        conn.execute(
            'INSERT INTO ninja3_emergency(bot,id,payload) VALUES(?,?,?) ON CONFLICT(bot,id) DO UPDATE SET payload=excluded.payload',
            (bot, 'state', json.dumps(state, ensure_ascii=False)),
        )

    @staticmethod
    def public(state: Record | None) -> Record:
        if state is None:
            return {
                'state': 'inactive',
                'phase': 'idle',
                'generation': None,
                'accounts': [],
                'created_at': None,
                'updated_at': None,
                'notification': 'not_attempted',
                'error': '',
                'exit_requested': False,
            }
        return {key: state.get(key, [] if key == 'accounts' else '') for key in PUBLIC_FIELDS}

    def _read_only(self) -> sqlite3.Connection:
        uri = Path(self.store.path).resolve().as_uri() + '?mode=ro'
        return sqlite3.connect(uri, uri=True, timeout=0)

    def current(self, bot: str) -> bool:
        """Synchronous final-send gate: normal mode is allowed only when readable."""
        if self.closed or bot in self._fail_closed:
            return False
        try:
            with closing(self._read_only()) as conn:
                row = self._read(conn, bot)
        except (sqlite3.Error, OSError):
            return False
        active = bool(row and row.get('state') in {'active', 'starting'})
        self._active_cache[bot] = active
        return not active

    def active(self, bot: str) -> bool:
        if self.closed or bot in self._fail_closed:
            return True
        try:
            with closing(self._read_only()) as conn:
                row = self._read(conn, bot)
        except (sqlite3.Error, OSError):
            return True
        active = bool(row and row.get('state') in {'active', 'starting'})
        self._active_cache[bot] = active
        return active

    async def initialize(self) -> None:
        def setup(conn: sqlite3.Connection) -> None:
            conn.execute('CREATE TABLE IF NOT EXISTS ninja3_emergency (bot TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(bot,id))')
            for bot, raw in conn.execute('SELECT bot,payload FROM ninja3_emergency').fetchall():
                state = json.loads(raw)
                if state.get('state') in {'active', 'starting'} and not self._runner_live(state):
                    # The prior process may have been killed while a child task was
                    # running.  Keep the fail-closed mode and its durable task IDs,
                    # but require an explicit owner command to resume pending items.
                    state['runner'] = None
                    state['runner_pid'] = None
                    state['phase'] = 'interrupted'
                    state['error'] = 'interrupted'
                    state['updated_at'] = self.clock()
                    for item in state.get('accounts', []):
                        if item.get('status') == 'running' and not item.get('task'):
                            item['status'] = 'pending'
                    self._save(conn, bot, state)

        await self.store.atomic(setup)
        rows = await self.store.atomic(lambda conn: conn.execute('SELECT bot,payload FROM ninja3_emergency').fetchall())
        self._active_cache = {bot: bool(json.loads(raw).get('state') in {'active', 'starting'}) for bot, raw in rows}
        self._ready = True

    def _lock(self, bot: str) -> asyncio.Lock:
        return self._locks.setdefault(bot, asyncio.Lock())

    async def _get(self, bot: str) -> Record | None:
        return cast(Record | None, await self.store.atomic(lambda conn: self._read(conn, bot)))

    def _start(self, bot: str) -> None:
        if self.closed or bot in self.runners:
            return
        runner = asyncio.create_task(self._run(bot))
        self.runners[bot] = runner

        def finished(done: asyncio.Task[None]) -> None:
            if self.runners.get(bot) is done:
                self.runners.pop(bot, None)
            if not done.cancelled():
                done.exception()

        runner.add_done_callback(finished)

    @staticmethod
    def _runner_live(state: Record) -> bool:
        runner = state.get('runner')
        pid = state.get('runner_pid')
        if not runner or not isinstance(pid, int):
            return False
        if (pid, runner) in _LIVE_RUNNERS:
            return True
        if pid == os.getpid():
            return False
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        return True

    async def _claim_runner(self, bot: str, generation: str) -> None:
        def claim(conn: sqlite3.Connection) -> None:
            state = self._read(conn, bot)
            if not state or state.get('generation') != generation or state.get('state') != 'active':
                raise EmergencyError('紧急模式已变化，请查看后台状态')
            owner = state.get('runner')
            if owner not in {None, self.runner_id}:
                raise EmergencyError('紧急模式由另一进程执行，请稍后查看状态')
            state.update(runner=self.runner_id, phase='running', updated_at=self.clock())
            state.update(runner_pid=os.getpid())
            self._save(conn, bot, state)

        await self.store.atomic(claim)
        _LIVE_RUNNERS.add((os.getpid(), self.runner_id))

    async def enter(self, bot: str) -> Record:
        async with self._lock(bot):
            try:
                existing = await self._get(bot)
                if existing and existing.get('state') in {'active', 'starting'}:
                    self._fail_closed.discard(bot)
                    self._active_cache[bot] = True
                    result = self.public(existing)
                    self._start(bot)
                    return result
                rows = await self.store.project(bot, 'accounts', ('id', 'name'))
                now = self.clock()
                generation = uuid4().hex
                state = {
                    'state': 'active',
                    'phase': 'starting',
                    'generation': generation,
                    'accounts': [{'account': row['id'], 'name': row.get('name', ''), 'status': 'pending', 'task': None} for row in rows],
                    'created_at': now,
                    'updated_at': now,
                    'notification': 'not_attempted',
                    'error': '',
                    'exit_requested': False,
                    'runner': None,
                    'runner_pid': None,
                }
                await self.store.atomic(lambda conn: self._save(conn, bot, state))
            except (sqlite3.Error, OSError, RuntimeError, ValueError) as error:
                # A failed entry write leaves member queries denied until a later
                # successful explicit retry proves the state is readable again.
                self._fail_closed.add(bot)
                raise EmergencyError('紧急模式未能保存，成员查询已暂停，请稍后重试') from error
            self._fail_closed.discard(bot)
            self._active_cache[bot] = True
            self._start(bot)
            return self.public(state)

    async def _update(self, bot: str, generation: str, **changes: Any) -> Record:
        def update(conn: sqlite3.Connection) -> Record:
            state = self._read(conn, bot)
            if not state or state.get('generation') != generation or state.get('state') != 'active' or state.get('runner') != self.runner_id:
                raise EmergencyError('紧急模式已变化，请查看后台状态')
            state.update(changes, updated_at=self.clock())
            self._save(conn, bot, state)
            return state

        return dict(await self.store.atomic(update))

    async def _run(self, bot: str) -> None:
        state = await self._get(bot)
        if not state or state.get('state') != 'active':
            return
        generation = state['generation']
        actor = 'emergency:' + generation
        try:
            await self._claim_runner(bot, generation)
            for index, item in enumerate(state['accounts']):
                state = await self._get(bot)
                if not state or state.get('state') != 'active' or state.get('generation') != generation:
                    return
                if item.get('status') not in {'pending', 'running'}:
                    continue
                account = item['account']
                try:
                    # Publish the handoff before selecting or confirming the child
                    # task.  A second process can then reject an exit during this
                    # otherwise task-less window.
                    await self._update_item(bot, generation, index, status='running')
                    # A task ID already recorded for this generation is authoritative.
                    # On restart, never create a fresh task for a completed or paused
                    # item; doing so would submit a second password change.
                    task = None
                    if item.get('task'):
                        try:
                            task = await self.tasks.get(bot, item['task'])
                        except TaskError:
                            task = None
                    if task is None:
                        task = await self.tasks.select(bot, account, actor)
                    await self._update_item(bot, generation, index, task=task['id'], status='running')
                    if task['state'] == 'selected':
                        started = await self.tasks.confirm(bot, task['id'], actor)
                    else:
                        started = task
                    worker = self.tasks.workers.get((bot, started['id']))
                    if worker is not None:
                        await asyncio.shield(worker)
                    final = await self.tasks.get(bot, started['id'])
                    outcome = 'success' if final['state'] == 'completed' else final['remote']
                    await self._update_item(bot, generation, index, status=outcome, task=started['id'])
                except TaskError:
                    await self._update_item(bot, generation, index, status='failed')
                except (sqlite3.Error, OSError, RuntimeError):
                    await self._update_item(bot, generation, index, status='failed')
                state = await self._get(bot)
                if not state or state.get('state') != 'active':
                    return
            state = await self._update(bot, generation, phase='complete')
            await self._notify(bot, state)
        except asyncio.CancelledError:
            raise
        except Exception:
            with contextlib.suppress(Exception):
                await self._update(bot, generation, phase='interrupted', error='runner_failed')

    async def _update_item(self, bot: str, generation: str, index: int, **changes: Any) -> Record:
        def update(conn: sqlite3.Connection) -> Record:
            state = self._read(conn, bot)
            if not state or state.get('generation') != generation or state.get('state') != 'active' or state.get('runner') != self.runner_id:
                raise EmergencyError('紧急模式已变化，请查看后台状态')
            state['accounts'][index].update(changes)
            state['updated_at'] = self.clock()
            self._save(conn, bot, state)
            return state

        return dict(await self.store.atomic(update))

    async def _notify(self, bot: str, state: Record) -> None:
        try:
            status = await asyncio.wait_for(self.notify(bot, self.public(state)), timeout=10)
            if status not in {'sent', 'failed', 'unknown', 'unavailable'}:
                status = 'unknown'
        except asyncio.CancelledError:
            status = 'unknown'
            raise
        except Exception:
            status = 'unknown'
        with contextlib.suppress(sqlite3.Error, OSError, RuntimeError):
            await self.store.atomic(
                lambda conn: self._save(
                    conn,
                    bot,
                    {**(self._read(conn, bot) or state), 'notification': status, 'updated_at': self.clock()},
                )
            )

    async def exit(self, bot: str) -> Record:
        async with self._lock(bot):
            if bot in self.runners and not self.runners[bot].done():
                raise EmergencyError('紧急改密仍在进行，请等待所有账号有结果后再退出')
            try:
                result = await self.store.atomic(lambda conn: self._leave_if_idle(conn, bot))
            except (sqlite3.Error, OSError, RuntimeError) as error:
                self._fail_closed.add(bot)
                raise EmergencyError('紧急模式退出未能保存，成员查询仍暂停') from error
            self._fail_closed.discard(bot)
            self._active_cache[bot] = False
            return self.public(result)

    def _leave_if_idle(self, conn: sqlite3.Connection, bot: str) -> Record:
        """CAS-like exit: read state and child task rows under one SQLite transaction."""
        current = self._read(conn, bot)
        if not current or current.get('state') in {'inactive', 'exited'}:
            return current or self.public(None)
        if current.get('phase') in {'starting', 'running'}:
            raise EmergencyError('紧急改密仍在进行，请等待所有账号有结果后再退出')
        if self._has_running_tasks_in_conn(conn, bot, current):
            raise EmergencyError('紧急改密仍在进行，请等待所有账号有结果后再退出')
        current.update(state='exited', phase='exited', exit_requested=True, updated_at=self.clock())
        self._save(conn, bot, current)
        return current

    @staticmethod
    def _has_running_tasks_in_conn(conn: sqlite3.Connection, bot: str, state: Record) -> bool:
        for item in state.get('accounts', []):
            if item.get('status') != 'running':
                continue
            key = item.get('task')
            if not key:
                return True
            row = conn.execute('SELECT payload FROM ninja3_password_tasks WHERE bot=? AND id=?', (bot, key)).fetchone()
            if not row:
                return True
            task = json.loads(row[0])
            if task.get('state') == 'selected':
                return True
            if task.get('active') and task.get('state') in {'prepared', 'preparing', 'submitting', 'verifying'}:
                return True
        return False

    async def _has_running_tasks(self, bot: str, state: Record) -> bool:
        """Check durable child-task handoffs, including tasks owned by another process."""

        def check(conn: sqlite3.Connection) -> bool:
            return self._has_running_tasks_in_conn(conn, bot, state)

        try:
            return bool(await self.store.atomic(check))
        except (sqlite3.Error, OSError, RuntimeError):
            # An unreadable task table cannot prove that no remote operation is in flight.
            raise EmergencyError('紧急任务状态暂不可读，成员查询仍暂停') from None

    async def status(self, bot: str) -> Record:
        return self.public(await self._get(bot))

    async def shutdown(self) -> None:
        self.closed = True
        runners = list(self.runners.values())
        for runner in runners:
            runner.cancel()
        if runners:
            await asyncio.gather(*runners, return_exceptions=True)
        _LIVE_RUNNERS.discard((os.getpid(), self.runner_id))
