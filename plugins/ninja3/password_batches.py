"""Owner batch password changes: a stable account snapshot handed one by one to PasswordTasks.

A batch never runs the protocol itself. Each account becomes an ordinary single-account
task (same intent, execution ownership, pause and receipt rules); the batch only keeps the
confirmed scope, the order and the link from each item to its task.
"""

import asyncio
import builtins
import json
import sqlite3
import time
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from core.base.logger import PLUGIN, get_logger

from .password_protocol import RemoteStatus
from .password_tasks import (
    AccountChangedError,
    GuardRefusedError,
    PasswordTasks,
    SamePasswordError,
    TaskError,
    account_digest,
    password_format_error,
)
from .storage import ExtensionStore

log = get_logger(PLUGIN, '忍者必须死3')  # type: ignore[no-untyped-call]  # project logger is untyped
Record = dict[str, Any]
PUBLIC_FIELDS = ('id', 'state', 'mode', 'all_accounts', 'notification', 'created_at', 'cancel_requested')
# Child task states still in flight, and those in which a batch cancel has something to stop.
RUNNING = {'prepared', 'preparing', 'submitting'}
CANCELLABLE = {'selected'} | RUNNING
OUTCOMES = {
    'success': '成功',
    'local_pending': '远端成功、本地待完成',
    'rejected': '远端明确拒绝',
    'unknown': '结果未知（账号暂停）',
    'not_submitted': '尚未提交（账号暂停）',
    'running': '执行中',
    'released': '已放弃（原密码有效）',
    'existing': '已有其他改密任务',
    'changed': '账号在选择后变化，未开始',
    'input_rejected': '指定密码与旧密码相同，未开始',
    'not_started': '未开始',
    'failed': '未能开始',
}


class PasswordBatches:
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
        self.runners: dict[tuple[str, str], asyncio.Task[None]] = {}
        self.closed = False

    @staticmethod
    def _read(conn: sqlite3.Connection, bot: str, key: str) -> Record:
        row = conn.execute('SELECT payload FROM ninja3_password_batches WHERE bot=? AND id=?', (bot, key)).fetchone()
        if not row:
            raise TaskError('批量改密批次不存在')
        return dict(json.loads(row[0]))

    @staticmethod
    def _save(conn: sqlite3.Connection, bot: str, batch: Record) -> None:
        conn.execute(
            'INSERT INTO ninja3_password_batches(bot,id,payload) VALUES(?,?,?) ON CONFLICT(bot,id) DO UPDATE SET payload=excluded.payload',
            (bot, batch['id'], json.dumps(batch, ensure_ascii=False)),
        )

    @staticmethod
    def _accounts(conn: sqlite3.Connection, bot: str) -> dict[str, str]:
        rows = conn.execute('SELECT id,payload FROM extension_records WHERE bot=? AND collection=? ORDER BY rowid', (bot, 'accounts'))
        return {str(key): str(payload) for key, payload in rows}

    async def initialize(self) -> None:
        def setup(conn: sqlite3.Connection) -> None:
            conn.execute('CREATE TABLE IF NOT EXISTS ninja3_password_batches (bot TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(bot,id))')
            for bot, raw in conn.execute("SELECT bot,payload FROM ninja3_password_batches WHERE json_extract(payload,'$.state')='running'").fetchall():
                # A restart never continues a batch: items not yet handed to a task stay unstarted.
                # Tasks already confirmed follow their own durable rules.
                batch = self._unstarted(json.loads(raw))
                batch.update(state='interrupted', runner=None, sealed_password=None)
                self._save(conn, bot, batch)

        await self.store.atomic(setup)

    @staticmethod
    def _unstarted(batch: Record) -> Record:
        for item in batch['items']:
            if item['status'] == 'pending':
                item['status'] = 'not_started'
        return batch

    # ---------- views ----------

    @staticmethod
    def outcome(item: Record, task: Record | None) -> str:
        if task is None or task['state'] in {'selected', 'cancelled'}:
            return 'not_started' if item['status'] in {'pending', 'linked', 'dispatched'} else str(item['status'])
        if item['status'] == 'existing':
            return 'existing'
        remote, local = task['remote'], task['local']
        if task['state'] == 'completed':
            return 'success'
        if task['state'] == 'released':
            return 'released'
        if task['state'] in RUNNING:
            return 'running'
        if remote == RemoteStatus.REPORTED_SUCCESS:
            return 'local_pending' if local != 'committed' else 'success'
        if remote == RemoteStatus.REJECTED:
            return 'rejected'
        if remote == RemoteStatus.UNKNOWN:
            return 'unknown'
        return 'not_submitted'

    async def view(self, bot: str, batch: Record) -> Record:
        result = {key: batch[key] for key in PUBLIC_FIELDS}
        items, counts = [], dict[str, int]()
        for item in batch['items']:
            task = None
            if item.get('task'):
                try:
                    task = await self.tasks.get(bot, item['task'])
                except TaskError:
                    task = None
            outcome = self.outcome(item, task)
            counts[outcome] = counts.get(outcome, 0) + 1
            items.append({'account': item['account'], 'name': item['name'], 'task': item.get('task'), 'outcome': outcome})
        result.update(items=items, counts=counts)
        return result

    async def list(self, bot: str) -> list[Record]:
        def read(conn: sqlite3.Connection) -> list[Record]:
            return [json.loads(row[0]) for row in conn.execute('SELECT payload FROM ninja3_password_batches WHERE bot=? ORDER BY rowid', (bot,))]

        return [await self.view(bot, batch) for batch in await self.store.atomic(read)]

    async def get(self, bot: str, key: str) -> Record:
        return await self.view(bot, await self.store.atomic(lambda conn: self._read(conn, bot, key)))

    # ---------- selection and confirmation ----------

    async def select(self, bot: str, actor: str, accounts: builtins.list[str] | None, *, new_password: str | None = None) -> Record:
        """``accounts=None`` means every account of this Bot now; the snapshot never grows later."""
        if self.closed:
            raise TaskError('改密执行器已停止')
        if new_password is not None and (problem := password_format_error(new_password)):
            raise TaskError(problem)
        key = uuid4().hex
        # Sealed with the task key before the transaction; the value is never stored in clear.
        sealed = None if new_password is None else self.tasks.cipher.seal({'bot': bot, 'batch': key, 'new_password': new_password})

        def choose(conn: sqlite3.Connection) -> Record:
            current = self._accounts(conn, bot)
            if accounts is None:
                targets = list(current)
            else:
                targets = list(dict.fromkeys(accounts))
                if any(account not in current for account in targets):
                    raise TaskError('有账号不存在，请重新选择')
            if not targets:
                raise TaskError('请明确选择至少一个账号')
            items = []
            for account in targets:
                record = json.loads(current[account])
                rejected = new_password is not None and record['password'] == new_password
                items.append(
                    {
                        'account': account,
                        'name': record['name'],
                        'digest': account_digest(current[account]),
                        'status': 'input_rejected' if rejected else 'pending',
                    }
                )
            scope = [[item['account'], item['digest']] for item in items]
            if sealed is None:
                # Repeating the same random selection of unchanged accounts shows the same batch.
                for (raw,) in conn.execute(
                    "SELECT payload FROM ninja3_password_batches WHERE bot=? AND json_extract(payload,'$.state')='selected' "
                    "AND json_extract(payload,'$.actor')=? AND json_extract(payload,'$.mode')='random' ORDER BY rowid DESC",
                    (bot, actor),
                ):
                    existing = json.loads(raw)
                    if existing['all_accounts'] == (accounts is None) and [[i['account'], i['digest']] for i in existing['items']] == scope:
                        return dict(existing)
            batch = dict(
                id=key,
                actor=actor,
                mode='random' if sealed is None else 'specified',
                all_accounts=accounts is None,
                sealed_password=sealed,
                items=items,
                state='selected',
                runner=None,
                notification='not_attempted',
                created_at=self.clock(),
                cancel_requested=False,
            )
            self._save(conn, bot, batch)
            return batch

        return await self.view(bot, await self.store.atomic(choose))

    async def confirm(self, bot: str, key: str, actor: str) -> Record:
        if self.closed:
            raise TaskError('改密执行器已停止')
        runner = uuid4().hex

        def claim(conn: sqlite3.Connection) -> tuple[Record, bool]:
            batch = self._read(conn, bot, key)
            if batch['actor'] != actor:
                raise TaskError('请由选择账号的主人确认')
            if batch['state'] != 'selected':
                return batch, False  # repeated confirmation: the same batch and its tasks
            current = self._accounts(conn, bot)
            if batch['all_accounts'] and set(current) != {item['account'] for item in batch['items']}:
                raise TaskError('账号范围在选择后变化，请重新选择并确认')
            for item in batch['items']:
                if item['account'] not in current or account_digest(current[item['account']]) != item['digest']:
                    raise TaskError('账号在选择后变化，请重新选择并确认')
            batch.update(state='running', runner=runner)
            self._save(conn, bot, batch)
            return batch, True

        batch, claimed = await self.store.atomic(claim)
        if claimed:
            task = asyncio.create_task(self._run(bot, key, runner))
            self.runners[(bot, key)] = task
            task.add_done_callback(lambda done: self.runners.pop((bot, key), None) if self.runners.get((bot, key)) is done else None)
        return await self.view(bot, batch)

    async def _update(self, bot: str, key: str, runner: str, index: int | None = None, **changes: Any) -> Record:
        """Compare-and-set on the batch runner: a cancelled, interrupted or stale runner stops."""

        def update(conn: sqlite3.Connection) -> Record:
            batch = self._running(conn, bot, key, runner)
            if index is None:
                batch.update(changes)
            else:
                batch['items'][index].update(changes)
            self._save(conn, bot, batch)
            return batch

        return dict(await self.store.atomic(update))

    def _running(self, conn: sqlite3.Connection, bot: str, key: str, runner: str) -> Record:
        """The batch as this runner still owns it; otherwise it was cancelled, interrupted or taken over."""
        batch = self._read(conn, bot, key)
        if batch.get('runner') != runner or batch['state'] != 'running' or batch['cancel_requested']:
            raise GuardRefusedError('批次已取消或中断')
        return batch

    async def _run(self, bot: str, key: str, runner: str) -> None:
        try:
            batch = await self.store.atomic(lambda conn: self._read(conn, bot, key))
            new_password = None
            if batch['sealed_password']:
                try:
                    sealed = self.tasks.cipher.open(batch['sealed_password'])
                    if sealed.get('bot') != bot or sealed.get('batch') != key:
                        raise TaskError('指定密码关联不符')
                except TaskError:
                    # Key missing or wrong: nothing is started with a guessed value.
                    await self._stop(bot, key, runner, 'interrupted')
                    raise
                new_password = str(sealed['new_password'])
            for index, item in enumerate(batch['items']):
                if item['status'] != 'pending':
                    continue
                await self._update(bot, key, runner)  # cancel/interrupt checkpoint before each account
                await self._dispatch(bot, key, runner, index, item, batch['actor'], new_password)
            await self._stop(bot, key, runner, 'finished')
        except TaskError:
            pass
        except Exception:
            # Storage failed for the batch itself: no raw exception or password. Stop it if the
            # store allows (otherwise restart does), and still try to tell the owner.
            log.error(f'[{bot}] 批量改密批次中断: {key}')
            try:
                await self._stop(bot, key, runner, 'interrupted')
            except Exception:
                log.error(f'[{bot}] 批量改密批次暂无法记录中断，重启后转为中断: {key}')
        try:
            await self._summary(bot, key, runner)
        except Exception:
            log.error(f'[{bot}] 批量改密汇总通知未能记录: {key}')

    async def _stop(self, bot: str, key: str, runner: str, state: str) -> None:
        def stop(conn: sqlite3.Connection) -> None:
            batch = self._running(conn, bot, key, runner)
            self._unstarted(batch).update(state=state, sealed_password=None)
            self._save(conn, bot, batch)

        await self.store.atomic(stop)

    async def _dispatch(self, bot: str, key: str, runner: str, index: int, item: Record, actor: str, new_password: str | None) -> None:
        try:
            child = await self.tasks.select(bot, item['account'], actor)
        except TaskError:
            await self._update(bot, key, runner, index, status='changed')
            return
        except (sqlite3.Error, OSError):
            # This account's storage failed before anything started: record it and go on.
            log.error(f'[{bot}] 批量改密单项未能开始: {key}#{index}')
            await self._update(bot, key, runner, index, status='failed')
            return
        if child['state'] != 'selected':
            # Same account already has an intent (single task, another batch, future emergency).
            await self._update(bot, key, runner, index, status='existing', task=child['id'])
            return
        await self._update(bot, key, runner, index, status='linked', task=child['id'])

        def still_running(conn: sqlite3.Connection) -> None:
            # Same transaction as the task claim: a cancelled, interrupted or stale batch starts nothing.
            self._running(conn, bot, key, runner)

        try:
            started = await self.tasks.confirm(bot, child['id'], actor, new_password=new_password, expect_digest=item['digest'], guard=still_running)
        except GuardRefusedError:
            raise
        except TaskError as error:
            status = 'changed' if isinstance(error, AccountChangedError) else 'input_rejected' if isinstance(error, SamePasswordError) else 'failed'
            await self._update(bot, key, runner, index, status=status)
            return
        except (sqlite3.Error, OSError):
            # The claim transaction rolled back: nothing started for this account.
            log.error(f'[{bot}] 批量改密单项未能开始: {key}#{index}')
            await self._update(bot, key, runner, index, status='failed')
            return
        if started['id'] != child['id']:
            await self._update(bot, key, runner, index, status='existing', task=started['id'])
            return
        worker = self.tasks.workers.get((bot, child['id']))
        try:
            await self._update(bot, key, runner, index, status='dispatched')
        finally:
            # Started: always wait for it before anything else (even if recording failed), and
            # without propagating our own cancellation into the task's worker.
            if worker is not None:
                await asyncio.wait({worker})

    async def _summary(self, bot: str, key: str, runner: str) -> None:
        batch = await self.store.atomic(lambda conn: self._read(conn, bot, key))
        if batch.get('runner') != runner:
            return
        try:
            status = await asyncio.wait_for(self.notify(bot, await self.view(bot, batch)), timeout=10)
            if status not in {'sent', 'failed', 'unknown', 'unavailable'}:
                status = 'unknown'
        except asyncio.CancelledError:
            # The message may already have reached the platform: unknown, never "not sent".
            await asyncio.shield(self._record_notice(bot, key, runner, 'unknown'))
            raise
        except Exception:
            status = 'unknown'
        await self._record_notice(bot, key, runner, status)

    async def _record_notice(self, bot: str, key: str, runner: str, status: str) -> None:
        def record(conn: sqlite3.Connection) -> None:
            current = self._read(conn, bot, key)
            if current.get('runner') == runner:
                current['notification'] = status
                self._save(conn, bot, current)

        await self.store.atomic(record)

    # ---------- cancellation and shutdown ----------

    async def cancel(self, bot: str, key: str) -> Record:
        def mark(conn: sqlite3.Connection) -> Record:
            batch = self._read(conn, bot, key)
            if batch['state'] in {'selected', 'running'}:
                self._unstarted(batch).update(state='cancelled', cancel_requested=True, sealed_password=None)
                self._save(conn, bot, batch)
            return batch

        batch = await self.store.atomic(mark)
        for item in batch['items']:
            if item.get('task') and item['status'] in {'linked', 'dispatched'}:
                # The account currently being changed follows the single-task cancel rules.
                task = await self.tasks.get(bot, item['task'])
                if task['state'] in CANCELLABLE:
                    await self.tasks.cancel(bot, item['task'])
        return await self.view(bot, batch)

    async def shutdown(self) -> None:
        self.closed = True
        runners = list(self.runners.values())
        for runner in runners:
            runner.cancel()
        if runners:
            await asyncio.gather(*runners, return_exceptions=True)
