"""Restart/rollback monitor, copied to the backup before changing tracked code.

This file intentionally does not import the application. A broken target must
not disable rollback. It is invoked only by FrameworkUpdater after a manual
update, never as a periodic updater.

Every update has a transaction ID stored in the pending state, as the content
of the deployment lock and in the monitor arguments. State changes go through
``transition`` under a short mutex and only apply while the state still
belongs to the expected transaction and status. A monitor whose transaction
was confirmed or superseded leaves without touching processes, Git, state or
locks. Each lock has a single releaser: the new process on confirmation, the
monitor after rollback, or the updater when it fails before restarting.
"""

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

TRANSACTION_ENV = 'ELAINABOT_UPDATE_TRANSACTION'
TERMINAL = ('confirmed', 'rolled_back', 'failed')
_MUTEX_STALE = 10.0


def _read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def _write(path, state):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def read_state(root):
    try:
        state = _read(Path(root) / 'data/update_pending.json')
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def lock_owner(root):
    try:
        return (Path(root) / 'data/update.lock').read_text(encoding='utf-8').strip()
    except OSError:
        return None


@contextlib.contextmanager
def state_mutex(root, timeout=10.0):
    """Cross-platform exclusive section for read-check-write of update state.

    A holder killed inside the section leaves the file behind; it is broken
    once older than a few seconds because every section is short.
    """
    mutex = Path(root) / 'data/update_state.mutex'
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(mutex, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            with contextlib.suppress(OSError):
                if time.time() - mutex.stat().st_mtime > _MUTEX_STALE:
                    mutex.unlink()
                    continue
            if time.monotonic() > deadline:
                raise TimeoutError('更新状态互斥等待超时') from None
            time.sleep(0.02)
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        mutex.unlink(missing_ok=True)


def transition(root, transaction, expected, **updates):
    """Apply updates only if the state still belongs to transaction/expected."""
    pending = Path(root) / 'data/update_pending.json'
    with state_mutex(root):
        state = read_state(root)
        if state.get('transaction') != transaction or state.get('status') not in expected:
            return None
        state.update(updates)
        _write(pending, state)
        return state


def release_lock(root, transaction):
    """Remove the lock only while it is held by transaction.

    Safe without the mutex for the lock's single releaser: nobody else can
    claim an existing lock, so its content cannot change before the unlink.
    """
    if lock_owner(root) == transaction:
        (Path(root) / 'data/update.lock').unlink(missing_ok=True)
        return True
    return False


def _stop(process):
    with contextlib.suppress(psutil.Error):
        root = psutil.Process(process.pid)
        children = root.children(recursive=True)
        for child in reversed(children):
            with contextlib.suppress(psutil.Error):
                child.terminate()
        process.terminate()
        _, alive = psutil.wait_procs(children + [root], timeout=3)
        for child in alive:
            with contextlib.suppress(psutil.Error):
                child.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=3)


def supervise_update(base_dir, command, transaction, timeout=90.0):
    """Start the target, require WS acknowledgement, roll back and restart once.

    Returns the final child exit status, or 0 when the transaction was
    confirmed or superseded (the running process is then left alone).
    """
    root = Path(base_dir).resolve()
    state = read_state(root)
    if state.get('transaction') != transaction or state.get('status') != 'awaiting_restart':
        return 0
    if any(not re.fullmatch('[0-9a-f]{40}', str(state.get(k))) for k in ('old_sha', 'target_sha')):
        raise RuntimeError('更新提交状态无效')
    git_env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    git_env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0')
    current = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True, env=git_env).strip()
    if current != state['target_sha']:
        raise RuntimeError('工作区已被外部切换，停止启动监护')
    me = psutil.Process()
    started = transition(
        root,
        transaction,
        ('awaiting_restart',),
        status='awaiting_ws',
        message='新版本启动中，等待 WS 连接确认',
        supervisor_pid=me.pid,
        supervisor_created=me.create_time(),
    )
    if not started:
        return 0
    env = os.environ.copy()
    env['ELAINABOT_RESTART_RECOVERY'] = '1'
    env[TRANSACTION_ENV] = transaction
    child = None
    owns_child = True
    try:
        deadline = time.monotonic() + timeout
        try:
            child = subprocess.Popen(command, cwd=root, env=env, start_new_session=True)
            while child.poll() is None and time.monotonic() < deadline:
                observed = read_state(root)
                if observed.get('transaction') != transaction or observed.get('status') != 'awaiting_ws':
                    owns_child = False  # confirmed or superseded: nothing left to do
                    return 0
                time.sleep(0.1)
        except OSError:
            pass  # Failing even to launch the target must still roll back.
        # Fence before any action; a late confirmation or newer transaction wins.
        if not transition(root, transaction, ('awaiting_ws',), status='rolling_back', message='WS 验证失败，正在回滚'):
            owns_child = False
            return 0
        if child:
            _stop(child)
        # Never reset/clean: edits made after switching can cause rollback to
        # refuse, in which case the backup and precise failure remain available.
        result = subprocess.run(
            ['git', '-C', str(root), 'checkout', '--no-overwrite-ignore', '--detach', state['old_sha']], capture_output=True, text=True, timeout=60, env=git_env
        )
        if result.returncode:
            transition(root, transaction, ('rolling_back',), status='rollback_failed', message='WS 验证失败且 Git 回滚被拒绝，请手动检查；备份已保留')
            return 1
        transition(root, transaction, ('rolling_back',), status='rolled_back', message='新版本启动失败或 WS 超时，已回滚原提交并重启；数据库备份已保留')
        release_lock(root, transaction)
        env.pop(TRANSACTION_ENV)
        child = subprocess.Popen(command, cwd=root, env=env, start_new_session=True)
        return child.wait()
    finally:
        if owns_child and child and child.poll() is None:
            _stop(child)


def main():
    base, parent_pid, parent_created, transaction, timeout, *command = sys.argv[1:]
    Path(__file__).with_suffix('.ready').write_text(transaction, encoding='utf-8')
    # Bind to a process identity, not a PID that may have been recycled.
    with contextlib.suppress(psutil.NoSuchProcess):
        parent = psutil.Process(int(parent_pid))
        if abs(parent.create_time() - float(parent_created)) < 0.01:
            while parent.is_running() and parent.status() != psutil.STATUS_ZOMBIE:
                time.sleep(0.1)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: sys.exit(1))
    raise SystemExit(supervise_update(base, command, transaction, float(timeout)))


if __name__ == '__main__':
    main()
