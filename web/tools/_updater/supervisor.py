"""Restart/rollback monitor, copied to the backup before changing tracked code.

This file intentionally does not import the application. A broken target must
not disable rollback. It is invoked only by FrameworkUpdater after a manual
update, never as a periodic updater. FrameworkUpdater imports the lock, state
and rollback primitives below so that both sides follow one protocol.

Locks are operating-system file locks (``flock``) that disappear with their
holder; files are never deleted to release them and no age or timeout ever
decides that a holder is gone:

- ``data/update.lock`` (deployment lock) is held by whoever is driving an
  update: the updating process, then the monitor, which inherits the same
  open file. Being able to take it proves no live process is acting.
- ``data/update_state.lock`` (state mutex) covers every read-check-write of
  ``data/update_pending.json``.

Every update has a transaction ID kept in the pending state and passed to the
monitor and new process. A state that is still active while nobody holds the
deployment lock was abandoned by a killed process and is taken over.
"""

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil

try:
    import fcntl
except ImportError:  # Windows: the updater refuses to run, see FrameworkUpdater.environment
    fcntl = None  # type: ignore[assignment]

TRANSACTION_ENV = 'ELAINABOT_UPDATE_TRANSACTION'
ACTIVE = ('switching', 'awaiting_restart', 'awaiting_ws', 'rolling_back')
TERMINAL = ('confirmed', 'rolled_back', 'failed', 'rollback_failed')
_PROXY_ENV = {'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'}
_no_hooks_dir = None


def clean_git_env(**extra):
    """No inherited Git variables, system/global configuration or proxies."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_') and k.lower() not in _PROXY_ENV}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0', **extra)
    return env


def deploy_git(root, *args, timeout=60):
    """Run Git in the deployment repository without hooks or fsmonitor."""
    global _no_hooks_dir
    if _no_hooks_dir is None or not os.path.isdir(_no_hooks_dir):
        _no_hooks_dir = tempfile.mkdtemp(prefix='qqbotforge-nohooks-')
    command = ['git', '-c', f'core.hooksPath={_no_hooks_dir}', '-c', 'core.fsmonitor=false', '-C', str(root), *args]
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=clean_git_env())
    except FileNotFoundError:
        return subprocess.CompletedProcess(command, 127, '', '未安装 git')


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


def _open_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)


def _flock(fd, timeout):
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)


def acquire_deploy_lock(root, timeout=0.0):
    """Return a locked file descriptor, or None while a live process holds it."""
    fd = _open_lock(Path(root) / 'data/update.lock')
    if _flock(fd, timeout):
        return fd
    os.close(fd)
    return None


def deploy_lock_held(root):
    fd = acquire_deploy_lock(root)
    if fd is None:
        return True
    os.close(fd)
    return False


@contextlib.contextmanager
def state_mutex(root, timeout=30.0):
    """Exclusive section for update state; never broken while its holder lives."""
    fd = _open_lock(Path(root) / 'data/update_state.lock')
    try:
        if not _flock(fd, timeout):
            raise TimeoutError('更新状态正被其他进程使用')
        yield
    finally:
        os.close(fd)


def transition(root, transaction, expected, **updates):
    """Apply updates only if the state still belongs to transaction/expected."""
    with state_mutex(root):
        state = read_state(root)
        if state.get('transaction') != transaction or state.get('status') not in expected:
            return None
        state.update(updates)
        _write(Path(root) / 'data/update_pending.json', state)
        return state


def finish_rollback(root, transaction, message):
    """Idempotently return to old_sha and record the result; the caller holds the deployment lock.

    Safe to repeat after being killed at any point: HEAD is inspected before
    acting and the terminal state is written only after HEAD is verified.
    """
    state = read_state(root)
    if state.get('transaction') != transaction or state.get('status') != 'rolling_back':
        return False
    old = state['old_sha']
    head = deploy_git(root, 'rev-parse', 'HEAD').stdout.strip()
    if head != old:
        # Never reset/clean: edits made after switching can cause rollback to
        # refuse, in which case the backup and precise failure remain available.
        deploy_git(root, 'checkout', '--no-overwrite-ignore', '--detach', old)
        head = deploy_git(root, 'rev-parse', 'HEAD').stdout.strip()
    if head != old:
        transition(root, transaction, ('rolling_back',), status='rollback_failed', message='Git 回滚被拒绝，请手动检查；数据库备份已保留')
        return False
    transition(root, transaction, ('rolling_back',), status='rolled_back', message=message)
    return True


def take_over(root, adopt=None):
    """Finish a transaction abandoned by a killed process; the caller holds the deployment lock.

    ``adopt(state)`` may claim a switched-but-unverified transaction instead
    (the restarted target verifying itself) and returns True when it did.
    Returns the state afterwards, or None when nothing was abandoned.
    """
    with state_mutex(root):
        state = read_state(root)
        if state.get('status') not in ACTIVE:
            return None
        transaction = state['transaction']
    if state['status'] in ('awaiting_restart', 'awaiting_ws') and adopt and adopt(state):
        return read_state(root)
    if state['status'] != 'rolling_back':
        transition(root, transaction, ACTIVE, status='rolling_back', message='更新进程已退出，正在接管并回滚', phase='takeover')
    finish_rollback(root, transaction, '更新进程意外退出，已接管并回滚到原提交；数据库备份已保留')
    return read_state(root)


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


def supervise_update(base_dir, command, transaction, timeout=90.0, lock_fd=None):
    """Start the target, require WS acknowledgement, roll back and restart once.

    ``lock_fd`` is the deployment lock inherited from the updater; without it
    the lock is taken here. Returns the final child exit status, or 0 when the
    transaction was confirmed or is not ours (the running process is then left
    alone).
    """
    root = Path(base_dir).resolve()
    if lock_fd is None:
        lock_fd = acquire_deploy_lock(root, timeout=5)
        if lock_fd is None:
            return 0
    elif not _flock(lock_fd, 0):
        raise RuntimeError('监护器未持有部署锁')
    child = None
    owns_child = True
    try:
        state = read_state(root)
        if state.get('transaction') != transaction or state.get('status') != 'awaiting_restart':
            return 0
        if any(not re.fullmatch('[0-9a-f]{40}', str(state.get(k))) for k in ('old_sha', 'target_sha')):
            raise RuntimeError('更新提交状态无效')
        if deploy_git(root, 'rev-parse', 'HEAD').stdout.strip() != state['target_sha']:
            raise RuntimeError('工作区已被外部切换，停止启动监护')
        me = psutil.Process()
        monitor = dict(supervisor_pid=me.pid, supervisor_created=me.create_time())
        if not transition(
            root, transaction, ('awaiting_restart',), status='awaiting_ws', message='新版本启动中，等待 WS 连接确认', phase='verifying', **monitor
        ):
            return 0
        env = os.environ.copy()
        env['ELAINABOT_RESTART_RECOVERY'] = '1'
        env[TRANSACTION_ENV] = transaction
        deadline = time.monotonic() + timeout
        try:
            child = subprocess.Popen(command, cwd=root, env=env, start_new_session=True)
            while child.poll() is None and time.monotonic() < deadline:
                observed = read_state(root)
                if observed.get('transaction') != transaction or observed.get('status') != 'awaiting_ws':
                    owns_child = False  # confirmed (or not ours): nothing left to do
                    return 0
                time.sleep(0.1)
        except OSError:
            pass  # Failing even to launch the target must still roll back.
        # Commit the decision before acting; a late confirmation wins.
        if not transition(root, transaction, ('awaiting_ws',), status='rolling_back', message='WS 验证失败，正在回滚', phase='stopping_target'):
            owns_child = False
            return 0
        if child:
            _stop(child)
        transition(root, transaction, ('rolling_back',), phase='checking_out')
        if not finish_rollback(root, transaction, '新版本启动失败或 WS 超时，已回滚原提交并重启；数据库备份已保留'):
            return 1
        os.close(lock_fd)
        lock_fd = None
        env.pop(TRANSACTION_ENV)
        child = subprocess.Popen(command, cwd=root, env=env, start_new_session=True)
        return child.wait()
    finally:
        if owns_child and child and child.poll() is None:
            _stop(child)
        if lock_fd is not None:
            os.close(lock_fd)


def main():
    base, parent_pid, parent_created, transaction, timeout, lock_fd, *command = sys.argv[1:]
    Path(__file__).with_suffix('.ready').write_text(transaction, encoding='utf-8')
    # Bind to a process identity, not a PID that may have been recycled.
    with contextlib.suppress(psutil.NoSuchProcess):
        parent = psutil.Process(int(parent_pid))
        if abs(parent.create_time() - float(parent_created)) < 0.01:
            while parent.is_running() and parent.status() != psutil.STATUS_ZOMBIE:
                time.sleep(0.1)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: sys.exit(1))
    raise SystemExit(supervise_update(base, command, transaction, float(timeout), int(lock_fd)))


if __name__ == '__main__':
    main()
