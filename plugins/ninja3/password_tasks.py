"""Durable single-account password intents; no remote retries or result-login probing."""

import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import time
from collections.abc import Awaitable, Callable
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

from cryptography.fernet import Fernet

from core.base.logger import PLUGIN, get_logger

from .password_protocol import ChangeInput, ChangeResult, RemoteStatus, change_password
from .storage import ExtensionStore

Record = dict[str, Any]
log = get_logger(PLUGIN, '忍者必须死3')  # type: ignore[no-untyped-call]  # project logger is untyped
# Seconds between attempts to persist a decided remote result (seven writes over about 20 seconds).
RESULT_WRITE_RETRIES = (0.2, 0.5, 1, 2, 5, 10)
# After that, a decided result stays in this process; background flushes back off, then
# only an explicit recover retries (no busy loop, no unbounded blocking of entry points).
HELD_RESULT_RETRIES = (30, 60, 120, 300, 600)
# Remote results proving the remote password was not changed by this intent.
UNCHANGED_REMOTE = {RemoteStatus.NOT_SUBMITTED, RemoteStatus.REJECTED}
PUBLIC_FIELDS = ('id', 'account', 'state', 'remote', 'local', 'error', 'notification', 'created_at', 'cancel_requested')


class TaskError(ValueError):
    """Only fixed, safe messages cross the application boundary."""


class SupersededError(TaskError):
    """This executor no longer owns the intent: released, cancelled, retried or completed."""


class IntentCipher:
    """External owner-only Fernet key; never automatically generated or stored in DB."""

    def __init__(self, key_file: str | None = None):
        self._fernet: Fernet | None = None
        try:
            path = key_file or os.environ.get('NINJA3_INTENT_KEY_FILE')
            if not path:
                return
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    return
                value = os.read(fd, 129).strip()
                if len(value) != 44:
                    return
                self._fernet = Fernet(value)
            finally:
                os.close(fd)
        except (OSError, ValueError):
            self._fernet = None

    def __repr__(self) -> str:
        return 'IntentCipher()'

    def seal(self, value: Record) -> str:
        if self._fernet is None:
            raise TaskError('待用密码密钥不可用，改密未开始')
        return self._fernet.encrypt(json.dumps(value, ensure_ascii=False).encode()).decode()

    def open(self, value: str) -> Record:
        try:
            if self._fernet is None:
                raise ValueError
            result = json.loads(self._fernet.decrypt(value.encode()))
            if isinstance(result, dict):
                return result
        except Exception:
            pass
        raise TaskError('待用密码无法恢复，账号保持暂停') from None


def random_password(old: str) -> str:
    while True:
        digits = f'{secrets.randbelow(10000):04d}'
        candidate = secrets.choice((f'{digits}xmx', f'xmx{digits}', f'{digits}xc', f'xc{digits}'))
        if candidate != old:
            return candidate


class PasswordTasks:
    def __init__(
        self,
        store: ExtensionStore,
        *,
        cipher: IntentCipher | None = None,
        protocol: Callable[..., Awaitable[ChangeResult]] = change_password,
        notify: Callable[[str, Record], Awaitable[str]],
        clock: Callable[[], float] = time.time,
    ):
        self.store = store
        self.cipher = cipher or IntentCipher()
        self.protocol = protocol
        self.notify = notify
        self.clock = clock
        self.workers: dict[tuple[str, str], asyncio.Task[None]] = {}
        # Decided outcomes whose durable write failed; keyed by task, bound to the executor token.
        self.held: dict[tuple[str, str], tuple[str, Record]] = {}
        self.flushers: dict[tuple[str, str], asyncio.Task[None]] = {}
        # Held outcomes being written and settled; like workers, they block retry/release/recover.
        self.flushing: set[tuple[str, str]] = set()
        self.closed = False

    @staticmethod
    def public(task: Record) -> Record:
        return {key: task[key] for key in PUBLIC_FIELDS}

    @staticmethod
    def _read(conn: sqlite3.Connection, bot: str, key: str) -> Record:
        row = conn.execute('SELECT payload FROM ninja3_password_tasks WHERE bot=? AND id=?', (bot, key)).fetchone()
        if not row:
            raise TaskError('改密任务不存在')
        return dict(json.loads(row[0]))

    @staticmethod
    def _save(conn: sqlite3.Connection, bot: str, task: Record) -> None:
        conn.execute(
            'INSERT INTO ninja3_password_tasks(bot,id,account,active,payload) VALUES(?,?,?,?,?) '
            'ON CONFLICT(bot,id) DO UPDATE SET active=excluded.active,payload=excluded.payload',
            (bot, task['id'], task['account'], int(task['active']), json.dumps(task, ensure_ascii=False)),
        )

    async def initialize(self) -> None:
        def setup(conn: sqlite3.Connection) -> list[tuple[str, str]]:
            conn.execute(
                'CREATE TABLE IF NOT EXISTS ninja3_password_tasks '
                '(bot TEXT NOT NULL,id TEXT NOT NULL,account TEXT NOT NULL,active INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(bot,id))'
            )
            conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS ninja3_one_password_intent ON ninja3_password_tasks(bot,account) WHERE active=1')
            conn.execute('PRAGMA user_version=2')
            recovery = []
            for bot, raw in conn.execute('SELECT bot,payload FROM ninja3_password_tasks WHERE active=1').fetchall():
                task = json.loads(raw)
                if task['remote'] == RemoteStatus.REPORTED_SUCCESS:
                    recovery.append((bot, task['id']))
                elif task['state'] == 'submitting':
                    task.update(state='paused', remote=RemoteStatus.UNKNOWN, error='interrupted')
                elif task['state'] in {'prepared', 'preparing'}:
                    task.update(state='paused', error='interrupted')
                self._save(conn, bot, task)
            return recovery

        recovery = await self.store.atomic(setup)
        for bot, key in recovery:
            # Restart only completes a known remote success locally; never calls protocol.
            await self.recover(bot, key)

    def _read_only(self) -> sqlite3.Connection:
        """Zero-busy-timeout read-only connection: a locked/unavailable database denies immediately."""
        uri = Path(self.store.path).resolve().as_uri() + '?mode=ro'
        return sqlite3.connect(uri, uri=True, timeout=0)

    @staticmethod
    def _account_row(conn: sqlite3.Connection, bot: str, account: str) -> str | None:
        row = conn.execute('SELECT payload FROM extension_records WHERE bot=? AND collection=? AND id=?', (bot, 'accounts', account)).fetchone()
        return str(row[0]) if row else None

    def paused_accounts(self, bot: str) -> set[str] | None:
        """Synchronous final-send gate. None means unreadable: fail closed.

        Separate read-only, zero-busy-timeout connection sees committed cross-process
        claims; a locked/unavailable database denies immediately rather than waiting.
        """
        if self.closed:
            return None
        try:
            with closing(self._read_only()) as conn:
                return {row[0] for row in conn.execute('SELECT account FROM ninja3_password_tasks WHERE bot=? AND active=1', (bot,))}
        except (sqlite3.Error, OSError):
            return None

    def is_paused(self, bot: str, account: str) -> bool:
        paused = self.paused_accounts(bot)
        return paused is None or account in paused

    def query_current(self, bot: str, accounts: list[Record]) -> bool:
        """Final gate checks pause and exact persisted account snapshot without await."""
        if self.closed:
            return False
        try:
            with closing(self._read_only()) as conn:
                conn.execute('BEGIN')
                paused = {row[0] for row in conn.execute('SELECT account FROM ninja3_password_tasks WHERE bot=? AND active=1', (bot,))}
                for account in accounts:
                    if account['id'] in paused:
                        return False
                    row = self._account_row(conn, bot, account['id'])
                    if not row or json.loads(row) != account:
                        return False
                return True
        except (sqlite3.Error, OSError, ValueError):
            return False

    def _view(self, bot: str, task: Record) -> Record:
        view = self.public(task)
        if (bot, task['id']) in self.held:
            view['error'] = 'result_not_persisted'
        return view

    async def list(self, bot: str) -> list[Record]:
        def read(conn: sqlite3.Connection) -> list[Record]:
            return [json.loads(row[0]) for row in conn.execute('SELECT payload FROM ninja3_password_tasks WHERE bot=? ORDER BY rowid', (bot,))]

        return [self._view(bot, task) for task in await self.store.atomic(read)]

    async def get(self, bot: str, key: str) -> Record:
        return self._view(bot, await self.store.atomic(lambda conn: self._read(conn, bot, key)))

    async def select(self, bot: str, account: str, actor: str) -> Record:
        if not account or self.closed:
            raise TaskError('请明确选择一个账号')

        def select(conn: sqlite3.Connection) -> Record:
            account_row = self._account_row(conn, bot, account)
            if not account_row:
                raise TaskError('账号不存在')
            digest = hashlib.sha256(account_row.encode()).hexdigest()
            row = conn.execute('SELECT payload FROM ninja3_password_tasks WHERE bot=? AND account=? AND active=1', (bot, account)).fetchone()
            if row:
                return self.public(json.loads(row[0]))
            # Repeated selection by the same actor of an unchanged account shows the same task.
            row = conn.execute(
                'SELECT payload FROM ninja3_password_tasks WHERE bot=? AND account=? AND active=0 '
                "AND json_extract(payload,'$.state')='selected' AND json_extract(payload,'$.actor')=? "
                "AND json_extract(payload,'$.account_digest')=? ORDER BY rowid DESC LIMIT 1",
                (bot, account, actor, digest),
            ).fetchone()
            if row:
                return self.public(json.loads(row[0]))
            task = dict(
                id=uuid4().hex,
                account=account,
                actor=actor,
                account_digest=digest,
                active=False,
                state='selected',
                remote=RemoteStatus.NOT_SUBMITTED,
                local='not_committed',
                error='',
                notification='not_attempted',
                created_at=self.clock(),
                cancel_requested=False,
            )
            self._save(conn, bot, task)
            return self.public(task)

        return dict(await self.store.atomic(select))

    async def confirm(self, bot: str, key: str, actor: str) -> Record:
        if self.closed:
            raise TaskError('改密执行器已停止')

        def claim(conn: sqlite3.Connection) -> tuple[Record, bool]:
            task = self._read(conn, bot, key)
            if task['actor'] != actor:
                raise TaskError('请由选择账号的主人确认')
            if task['state'] != 'selected':
                return task, False
            row = conn.execute('SELECT payload FROM ninja3_password_tasks WHERE bot=? AND account=? AND active=1', (bot, task['account'])).fetchone()
            if row:
                return json.loads(row[0]), False
            account_row = self._account_row(conn, bot, task['account'])
            if not account_row:
                raise TaskError('账号不存在')
            if hashlib.sha256(account_row.encode()).hexdigest() != task['account_digest']:
                raise TaskError('账号在选择后变化，请重新选择并确认')
            account = json.loads(account_row)
            intent = dict(
                bot=bot,
                id=key,
                account=task['account'],
                login=account['login'],
                area='86',
                old_password=account['password'],
                new_password=random_password(account['password']),
            )
            # Atomic claim + encrypted intent + durable pause. No network before commit.
            task.update(active=True, state='prepared', intent=self.cipher.seal(intent))
            self._save(conn, bot, task)
            return task, True

        # The claim thread may finish after caller cancellation; it never sends HTTP.
        task, claimed = await self.store.atomic(claim)
        if claimed:
            self._start(bot, task['id'])
        return self.public(task)

    def _start(self, bot: str, key: str) -> None:
        """Schedule a worker; it must still win the durable prepared→preparing claim."""
        worker = asyncio.create_task(self._run(bot, key))
        self.workers[(bot, key)] = worker

        def finished(done: asyncio.Task[None]) -> None:
            if self.workers.get((bot, key)) is done:
                self.workers.pop((bot, key))
            if not done.cancelled():
                done.exception()  # Retrieve without exposing exception/intent in default task logs.

        worker.add_done_callback(finished)

    def _intent(self, bot: str, task: Record) -> Record:
        intent = self.cipher.open(task['intent'])
        if any(intent.get(k) != v for k, v in {'bot': bot, 'id': task['id'], 'account': task['account']}.items()):
            raise TaskError('待用密码关联不符，账号保持暂停')
        return intent

    async def _mutate(self, bot: str, key: str, runner: str | None = None, **changes: Any) -> Record:
        """With runner: compare-and-set on the durable executor token and active intent."""

        def update(conn: sqlite3.Connection) -> Record:
            task = self._read(conn, bot, key)
            if runner is not None and (task.get('runner') != runner or not task['active']):
                raise SupersededError('改密意图已被放弃、重试或完成')
            task.update(changes)
            self._save(conn, bot, task)
            return task

        return dict(await self.store.atomic(update))

    def _owned(self, task: Record, runner: str | None, state: str) -> None:
        if task.get('runner') != runner or not task['active'] or task['state'] != state:
            raise SupersededError('改密意图状态变化，账号保持暂停')
        if task['cancel_requested'] or self.closed:
            raise SupersededError('改密已取消，账号保持暂停')

    async def _claim_run(self, bot: str, key: str, runner: str) -> Record:
        """Start linearization point: only a still-active, uncancelled, unclaimed prepared intent runs."""

        def claim(conn: sqlite3.Connection) -> Record:
            task = self._read(conn, bot, key)
            self._owned(task, None, 'prepared')
            task.update(state='preparing', runner=runner)
            self._save(conn, bot, task)
            return task

        return dict(await self.store.atomic(claim))

    async def _before_submit(self, bot: str, key: str, runner: str) -> None:
        """Submit linearization point: release/retry/cancel/restart before this commit wins."""

        def mark(conn: sqlite3.Connection) -> None:
            task = self._read(conn, bot, key)
            self._owned(task, runner, 'preparing')
            task.update(state='submitting', remote=RemoteStatus.UNKNOWN)
            self._save(conn, bot, task)

        await self.store.atomic(mark)

    async def _run(self, bot: str, key: str) -> None:
        runner = uuid4().hex
        result: ChangeResult | None = None
        failure = 'cancelled'
        try:
            task = await self._claim_run(bot, key, runner)
            intent = self._intent(bot, task)
            result = await self.protocol(
                ChangeInput(intent['login'], intent['area'], intent['old_password'], intent['new_password']),
                before_submit=lambda: self._before_submit(bot, key, runner),
            )
        except asyncio.CancelledError:
            pass
        except SupersededError:
            failure = 'superseded'
        except Exception:
            failure = 'preparation'
            # Preserve a durable UNKNOWN marker if an injected protocol fails unexpectedly.
        # Separate shielded finalizer: cancellation of caller/runner never discards a
        # decisive result or interrupts local commit. Hard kill relies on durable state.
        finalizer = asyncio.create_task(self._finish(bot, key, runner, result, failure))
        try:
            await asyncio.shield(finalizer)
        except asyncio.CancelledError:
            await finalizer

    async def _persist_outcome(self, bot: str, key: str, runner: str, changes: Record) -> None:
        """A transient write failure must not turn a decisive remote result into unknown."""
        for delay in RESULT_WRITE_RETRIES:
            try:
                await self._mutate(bot, key, runner=runner, **changes)
                return
            except (sqlite3.Error, OSError):
                await asyncio.sleep(delay)
        await self._mutate(bot, key, runner=runner, **changes)

    async def _finish(self, bot: str, key: str, runner: str, result: ChangeResult | None, failure: str) -> None:
        if result is not None:
            changes = dict(
                state='paused',
                remote=result.remote_status,
                error=result.error_code.value,
                local='pending' if result.remote_status == RemoteStatus.REPORTED_SUCCESS else 'not_committed',
            )
        else:
            # Unknown if the durable pre-submit marker was already crossed.
            changes = dict(state='paused', error=failure)
        try:
            await self._persist_outcome(bot, key, runner, changes)
        except SupersededError:
            # Result-write linearization point: a stale executor never touches a
            # released/retried/completed intent, its pause or the account.
            return
        except (sqlite3.Error, OSError):
            # Keep the decided outcome in this process instead of dropping it.
            self._hold(bot, key, runner, changes)
            return
        except Exception:
            # No raw exceptions or intent; active=1 and the durable marker keep it paused.
            return
        await self._settle(bot, key)

    async def _settle(self, bot: str, key: str) -> None:
        try:
            task = await self.store.atomic(lambda conn: self._read(conn, bot, key))
            if task['remote'] == RemoteStatus.REPORTED_SUCCESS:
                try:
                    await self._commit_local(bot, key)
                except Exception:
                    await self._mutate(bot, key, error='local_commit_failed')
            await self._receipt(bot, key)
        except Exception:
            # No raw exceptions or intent; active=1 keeps the account paused for recovery.
            return

    def _hold(self, bot: str, key: str, runner: str, changes: Record) -> None:
        log.error('改密任务结果暂未落盘，已在进程内保留并稍后重试: %s', key)
        self.held[(bot, key)] = (runner, changes)
        if (bot, key) not in self.flushers and not self.closed:
            flusher = asyncio.create_task(self._flush_later(bot, key))
            self.flushers[(bot, key)] = flusher
            flusher.add_done_callback(lambda _: self.flushers.pop((bot, key), None))

    async def _flush_later(self, bot: str, key: str) -> None:
        for delay in HELD_RESULT_RETRIES:
            await asyncio.sleep(delay)
            try:
                if not await self._flush(bot, key):
                    return
            except (sqlite3.Error, OSError):
                continue
        # Still held: an explicit recover keeps trying while this process lives.

    async def _flush(self, bot: str, key: str) -> bool:
        """Write a held outcome under the same executor token; True if one was written."""
        # Taken before awaiting: a concurrent recover and background flush write and notify once.
        held = self.held.pop((bot, key), None)
        if held is None:
            return False
        runner, changes = held
        self.flushing.add((bot, key))
        try:
            try:
                await self._mutate(bot, key, runner=runner, **changes)
            except SupersededError:
                log.error('改密任务保留结果已被新的任务状态取代，未写入: %s', key)
                return False
            except BaseException:
                # Not written (storage error or cancellation): hold it again unless re-held meanwhile.
                self.held.setdefault((bot, key), held)
                raise
            await self._settle(bot, key)
            return True
        finally:
            self.flushing.discard((bot, key))

    async def _commit_local(self, bot: str, key: str) -> None:
        def commit(conn: sqlite3.Connection) -> None:
            task = self._read(conn, bot, key)
            if task['remote'] != RemoteStatus.REPORTED_SUCCESS or task['local'] == 'committed' or not task['active']:
                return
            intent = self._intent(bot, task)
            row = self._account_row(conn, bot, task['account'])
            if not row:
                raise TaskError('账号记录缺失，等待本地恢复')
            account = json.loads(row)
            if account['login'] != intent['login'] or account['password'] not in (intent['old_password'], intent['new_password']):
                raise TaskError('账号凭据变化，等待本地恢复')
            # Keep enablement/scope and future emergency/pause flags untouched.
            account['password'] = intent['new_password']
            conn.execute(
                'UPDATE extension_records SET payload=? WHERE bot=? AND collection=? AND id=?',
                (json.dumps(account, ensure_ascii=False), bot, 'accounts', task['account']),
            )
            task.update(state='completed', local='committed', active=False, error='')
            self._save(conn, bot, task)

        await self.store.atomic(commit)

    async def recover(self, bot: str, key: str) -> Record:
        if (bot, key) in self.flushing:
            raise TaskError('已确定结果正在写入，请稍后再恢复')
        try:
            if await self._flush(bot, key):
                return await self.get(bot, key)
        except (sqlite3.Error, OSError):
            raise TaskError('存储暂不可写，已确定结果仍保留在本进程，请稍后再恢复') from None
        task = await self.store.atomic(lambda conn: self._read(conn, bot, key))
        if task['remote'] != RemoteStatus.REPORTED_SUCCESS:
            raise TaskError('仅允许恢复已确认成功的本地提交；未知结果保持暂停')
        try:
            await self._commit_local(bot, key)
        except Exception:
            # The known remote success is already durable, even with a lost key/disk.
            return await self.get(bot, key)
        await self._receipt(bot, key)
        return await self.get(bot, key)

    async def retry(self, bot: str, key: str, actor: str) -> Record:
        """Resubmit the same sealed intent; only when the remote password provably did not change."""
        if self.closed:
            raise TaskError('改密执行器已停止')

        def claim(conn: sqlite3.Connection) -> Record:
            task = self._settled(conn, bot, key)
            if task['actor'] != actor:
                raise TaskError('请由选择账号的主人重试')
            # A new attempt: the previous not_submitted/rejected result described an unchanged remote.
            # Clearing the executor token supersedes any late worker of the previous attempt.
            task.update(state='prepared', runner=None, remote=RemoteStatus.NOT_SUBMITTED, error='', cancel_requested=False, notification='not_attempted')
            self._save(conn, bot, task)
            return task

        task = await self.store.atomic(claim)
        self._start(bot, key)
        return self.public(task)

    async def release(self, bot: str, key: str) -> Record:
        """Owner gives up an intent that never changed the remote password; old credentials stay valid."""

        def drop(conn: sqlite3.Connection) -> Record:
            task = self._settled(conn, bot, key)
            task.update(state='released', active=False)
            self._save(conn, bot, task)
            return task

        return self.public(await self.store.atomic(drop))

    def _settled(self, conn: sqlite3.Connection, bot: str, key: str) -> Record:
        task = self._read(conn, bot, key)
        busy = (bot, key) in self.workers or (bot, key) in self.held or (bot, key) in self.flushing
        if busy or task['state'] != 'paused' or not task['active']:
            raise TaskError('任务尚未结束或已解决')
        if task['remote'] not in UNCHANGED_REMOTE:
            raise TaskError('仅尚未提交或远端明确拒绝的任务可以重试或放弃；未知结果保持暂停')
        return task

    async def cancel(self, bot: str, key: str) -> Record:
        def mark(conn: sqlite3.Connection) -> tuple[Record, bool]:
            task = self._read(conn, bot, key)
            if task['cancel_requested'] or task['state'] == 'completed':
                return task, False
            task['cancel_requested'] = True
            if task['state'] == 'selected':
                task['state'] = 'cancelled'
            elif task['state'] == 'prepared':
                task.update(state='paused', error='cancelled')
            self._save(conn, bot, task)
            return task, True

        task, first = await self.store.atomic(mark)
        worker = self.workers.get((bot, key))
        if first and worker is not None:
            worker.cancel()
        return self.public(task)

    async def _receipt(self, bot: str, key: str) -> None:
        task = await self.get(bot, key)
        if task['notification'] == 'sent':
            return
        try:
            status = await asyncio.wait_for(self.notify(bot, task), timeout=10)
            if status not in {'sent', 'failed', 'unknown', 'unavailable'}:
                status = 'unknown'
        except Exception:
            status = 'unknown'
        await self._mutate(bot, key, notification=status)

    async def shutdown(self) -> None:
        self.closed = True
        for flusher in list(self.flushers.values()):
            flusher.cancel()
        workers = list(self.workers.values())
        for worker in workers:
            if worker.cancelling() == 0:
                worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        if self.held:
            # Process exit loses only results the store never accepted; restart treats them as unknown.
            log.error('改密执行器停止时仍有 %d 个结果未落盘，重启后按未知暂停', len(self.held))
