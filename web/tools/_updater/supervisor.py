"""Restart/rollback monitor, copied to the backup before changing tracked code.

This file intentionally does not import the application. A broken target must
not disable rollback. It is invoked only by FrameworkUpdater after a manual
update, never as a periodic updater.
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


def _read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def _write(path, state):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


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

    Returns the final child exit status. All children are terminated on monitor
    interruption, which also makes disposable-process integration tests safe.
    """
    root = Path(base_dir).resolve()
    pending = root / 'data/update_pending.json'
    lock = root / 'data/update.lock'
    state = _read(pending)
    if state['transaction'] != transaction or state['status'] != 'awaiting_restart':
        raise RuntimeError('更新状态与监护事务不匹配')
    if any(not re.fullmatch('[0-9a-f]{40}', state[k]) for k in ('old_sha', 'target_sha')):
        raise RuntimeError('更新提交状态无效')
    git_env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    git_env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0')
    current = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True, env=git_env).strip()
    if current != state['target_sha']:
        raise RuntimeError('工作区已被外部切换，停止启动监护')
    env = os.environ.copy()
    env['ELAINABOT_RESTART_RECOVERY'] = '1'
    child = None
    try:
        state.update(status='awaiting_ws', message='新版本启动中，等待 WS 连接确认')
        _write(pending, state)
        deadline = time.monotonic() + timeout
        try:
            child = subprocess.Popen(command, cwd=root, env=env, start_new_session=True)
            while child.poll() is None and time.monotonic() < deadline:
                try:
                    observed = _read(pending)
                except (OSError, ValueError):
                    observed = {}
                if isinstance(observed, dict) and observed.get('transaction') == transaction and observed.get('status') == 'confirmed':
                    lock.unlink(missing_ok=True)
                    return child.wait()
                time.sleep(0.1)
        except OSError:
            pass  # Failing even to launch the target must still roll back.
        if child:
            _stop(child)
        # Never reset/clean: edits made after switching can cause rollback to
        # refuse, in which case the backup and precise failure remain available.
        result = subprocess.run(
            ['git', '-C', str(root), 'checkout', '--no-overwrite-ignore', '--detach', state['old_sha']], capture_output=True, text=True, timeout=60, env=git_env
        )
        if result.returncode:
            state.update(status='rollback_failed', message='WS 验证失败且 Git 回滚被拒绝，请手动检查；备份已保留')
            _write(pending, state)
            return 1
        state.update(status='rolled_back', message='新版本启动失败或 WS 超时，已回滚原提交并重启；数据库备份已保留')
        _write(pending, state)
        lock.unlink(missing_ok=True)
        child = subprocess.Popen(command, cwd=root, env=env, start_new_session=True)
        return child.wait()
    finally:
        if child and child.poll() is None:
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
