"""Update transactions under real processes: OS file locks, fencing and takeover after SIGKILL.

Real Git repositories, real child processes and real signals; GitHub, CI and
restarts are stubbed.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from web.tools._updater import framework, supervisor
from web.tools._updater.framework import FrameworkUpdater
from web.tools._updater.supervisor import supervise_update

REPO = Path(__file__).parents[1]
TRANSACTION_ENV = 'ELAINABOT_UPDATE_TRANSACTION'


def git(cwd, *args):
    return subprocess.check_output(['git', '-C', str(cwd), *args], text=True).strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Commits A, B, C; checked out at B as if A→B (transaction 'first') was just switched."""
    root = tmp_path / 'deploy'
    root.mkdir()
    git(root, 'init', '-q')
    git(root, 'config', 'user.name', 'Test')
    git(root, 'config', 'user.email', 'test@example.com')
    (root / '.gitignore').write_text('data/\n')
    shas = []
    for i in range(3):
        (root / 'code.txt').write_text(str(i))
        git(root, 'add', '.')
        git(root, 'commit', '-qm', str(i))
        shas.append(git(root, 'rev-parse', 'HEAD'))
    git(root, 'checkout', '-q', '--detach', shas[1])
    data = root / 'data'
    data.mkdir()
    state = dict(transaction='first', status='awaiting_restart', old_sha=shas[0], target_sha=shas[1], backup_path='data/backup/first')
    (data / 'update_pending.json').write_text(json.dumps(state))
    monkeypatch.setenv('PYTHONPATH', str(REPO))
    monkeypatch.delenv(TRANSACTION_ENV, raising=False)
    return SimpleNamespace(root=root, data=data, shas=shas, pending=data / 'update_pending.json')


def wait_for(path, process=None, seconds=20):
    deadline = time.monotonic() + seconds
    while not path.exists():
        if process is not None and process.poll() is not None:
            raise AssertionError(f'worker exited {process.returncode}: {process.stderr.read() if process.stderr else ""}')
        assert time.monotonic() < deadline, f'{path.name} never appeared'
        time.sleep(0.02)
    return path.read_text()


def lock_held_elsewhere(root):
    """Ask a separate process, so the answer cannot depend on this process's descriptors."""
    code = 'import sys; from web.tools._updater.supervisor import deploy_lock_held; print(deploy_lock_held(sys.argv[1]))'
    return subprocess.check_output([sys.executable, '-c', code, str(root)], text=True).strip() == 'True'


def ready_app():
    ready = SimpleNamespace(transport='websocket', phase='ready')
    return SimpleNamespace(
        hook_manager=SimpleNamespace(connection_status=SimpleNamespace(statuses=[ready])),
        _stop_event=asyncio.Event(),
        _restart_requested=False,
        plugin_manager=None,
    )


def stub_update(monkeypatch, updater, root, target):
    """Everything external to the transaction: GitHub fetch/CI and the restart itself."""
    app = ready_app()
    monkeypatch.setattr(framework, 'get_app', lambda: app)
    updater._fetch = lambda: target

    async def ci(sha):
        return {'status': 'success', 'tree': git(root, 'rev-parse', f'{sha}^{{tree}}')}

    updater._ci_status = ci
    updater._trigger_restart = lambda *args: None
    return app


CONFIRM_THEN_UPDATE = """
import asyncio, json, pathlib, subprocess, sys, time
from types import SimpleNamespace
import web.tools._updater.framework as framework
from web.tools._updater.supervisor import deploy_lock_held
root = pathlib.Path.cwd(); data = root / 'data'
if json.loads((data / 'update_pending.json').read_text())['status'] == 'rolled_back':
    (data / 'old-started').touch(); sys.exit(0)
up = framework.FrameworkUpdater(str(root))
ready = SimpleNamespace(transport='websocket', phase='ready')
app = SimpleNamespace(_stop_event=asyncio.Event(), plugin_manager=None, hook_manager=SimpleNamespace(connection_status=SimpleNamespace(statuses=[ready])))
framework.get_app = lambda: app
def slow_fetch():
    # Give a lagging monitor time to act on the new claim.
    (data / 'claim-during-fetch').write_text(str(deploy_lock_held(root)))
    time.sleep(0.6)
    (data / 'claim-after-wait').write_text(str(deploy_lock_held(root)))
    return sys.argv[1]
up._fetch = slow_fetch
async def ci(sha):
    return {'status': 'success', 'tree': subprocess.check_output(['git', 'rev-parse', sha + '^{tree}'], text=True).strip()}
up._ci_status = ci
up._trigger_restart = lambda *a: None
async def run():
    assert await up.verify_startup(app)
    result = await up.update_to_version(sys.argv[1])
    (data / 'second-result.json').write_text(json.dumps(result))
asyncio.run(run())
time.sleep(1.5)
"""


def test_old_monitor_cannot_undo_or_unlock_the_next_transaction(repo):
    """A→B confirmed, then B→C starts: the A→B monitor must not roll back to A or drop C's lock."""
    script = repo.data / 'child.py'
    script.write_text(CONFIRM_THEN_UPDATE)
    assert supervise_update(repo.root, [sys.executable, str(script), repo.shas[2]], 'first', timeout=1) == 0
    result = json.loads(wait_for(repo.data / 'second-result.json'))
    time.sleep(1.5)  # outlive the old monitor's deadline
    assert result['success'] is True, result
    assert git(repo.root, 'rev-parse', 'HEAD') == repo.shas[2], 'old monitor rolled B→C back'
    assert (repo.data / 'claim-during-fetch').read_text() == 'True'
    assert (repo.data / 'claim-after-wait').read_text() == 'True', 'B→C lock was lost during its fetch'
    state = json.loads(repo.pending.read_text())
    assert state['transaction'] != 'first'
    assert state['status'] == 'awaiting_restart'
    assert not (repo.data / 'old-started').exists()


INTERLEAVE = """
import json, pathlib, time
data = pathlib.Path.cwd() / 'data'
# Another transaction's state appears while the first is verifying.
(data / 'update_pending.json').write_text(json.dumps({'transaction': 'second', 'status': 'awaiting_ws', 'old_sha': 'x', 'target_sha': 'y'}))
time.sleep(1.0)
(data / 'alive-after-deadline').touch()
"""


def test_interleaved_transaction_is_left_untouched(repo):
    script = repo.data / 'child.py'
    script.write_text(INTERLEAVE)
    assert supervise_update(repo.root, [sys.executable, str(script)], 'first', timeout=0.4) == 0
    wait_for(repo.data / 'alive-after-deadline')  # the other transaction's process was not stopped
    assert json.loads(repo.pending.read_text()) == {'transaction': 'second', 'status': 'awaiting_ws', 'old_sha': 'x', 'target_sha': 'y'}
    assert git(repo.root, 'rev-parse', 'HEAD') == repo.shas[1]
    assert not lock_held_elsewhere(repo.root)


def test_transition_only_applies_to_own_transaction(repo):
    before = repo.pending.read_text()
    assert supervisor.transition(repo.root, 'other', ('awaiting_restart',), status='rolled_back') is None
    assert supervisor.transition(repo.root, 'first', ('awaiting_ws',), status='rolled_back') is None
    assert repo.pending.read_text() == before


HOLDER = """
import sys, time
from pathlib import Path
from web.tools._updater import supervisor as s
root = Path(sys.argv[1])
fd = s.acquire_deploy_lock(root)
assert fd is not None
s.transition(root, 'first', ('awaiting_restart',), status='confirmed')
(root / 'data/paused').touch()
while not (root / 'data/resume').exists():
    time.sleep(0.01)
"""


def test_release_vs_reclaim_never_drops_the_next_lock(repo):
    """The confirmed transaction's holder is still alive: no reclaim; once it is gone, the new lock stays."""
    holder = subprocess.Popen([sys.executable, '-c', HOLDER, str(repo.root)], stderr=subprocess.PIPE, text=True)
    try:
        wait_for(repo.data / 'paused', holder)
        with pytest.raises(FileExistsError):
            FrameworkUpdater(str(repo.root), claim_timeout=0.3)._claim()
        (repo.data / 'resume').touch()
        holder.wait(timeout=10)
        fd = FrameworkUpdater(str(repo.root), claim_timeout=2)._claim()
        try:
            time.sleep(0.2)
            assert lock_held_elsewhere(repo.root), 'the released holder took the new lock with it'
        finally:
            os.close(fd)
    finally:
        if holder.poll() is None:
            holder.kill()
    assert not lock_held_elsewhere(repo.root)


MUTEX_HOLDER = """
import json, sys, time
from pathlib import Path
from web.tools._updater import supervisor as s
root = Path(sys.argv[1])
with s.state_mutex(root):
    state = s.read_state(root)
    (root / 'data/paused').touch()
    while not (root / 'data/resume').exists():
        time.sleep(0.01)
    state.update(status='confirmed', writer='holder')
    (root / 'data/update_pending.json').write_text(json.dumps(state))
"""


def test_state_mutex_is_not_stolen_from_a_holder_paused_over_ten_seconds(repo):
    holder = subprocess.Popen([sys.executable, '-c', MUTEX_HOLDER, str(repo.root)], stderr=subprocess.PIPE, text=True)
    outcome = {}
    try:
        wait_for(repo.data / 'paused', holder)
        time.sleep(10.5)
        contender = threading.Thread(
            target=lambda: outcome.update(result=supervisor.transition(repo.root, 'first', ('awaiting_restart',), status='rolling_back'))
        )
        contender.start()
        time.sleep(1.0)
        assert contender.is_alive(), 'the paused holder lost the state mutex'
        assert json.loads(repo.pending.read_text())['status'] == 'awaiting_restart'
        (repo.data / 'resume').touch()
        contender.join(timeout=10)
        holder.wait(timeout=10)
    finally:
        if holder.poll() is None:
            holder.kill()
    assert outcome['result'] is None  # the holder's write came first; the stale expectation no longer holds
    final = json.loads(repo.pending.read_text())
    assert final['status'] == 'confirmed' and final['writer'] == 'holder'


KILL_POINT = """
import sys, time
from pathlib import Path
from web.tools._updater import supervisor as s
root, point = Path(sys.argv[1]), sys.argv[2]
def pause():
    (root / 'data/paused').touch()
    while True:
        time.sleep(1)
def trace(frame, event, arg):
    if event == 'call':
        name = frame.f_code.co_name
        if point == 'before_checkout' and name == 'finish_rollback':
            pause()
        if point == 'after_checkout' and name == 'transition' and frame.f_locals.get('updates', {}).get('status') == 'rolled_back':
            pause()
sys.settrace(trace)
s.supervise_update(root, [sys.executable, '-c', 'import time; time.sleep(60)'], 'first', timeout=0.3)
"""


@pytest.mark.parametrize('round_', [1, 2, 3])
@pytest.mark.parametrize('entry', ['startup', 'next_update'])
@pytest.mark.parametrize('point', ['before_checkout', 'after_checkout'])
def test_rollback_killed_by_sigkill_is_taken_over(repo, monkeypatch, point, entry, round_):
    worker = subprocess.Popen([sys.executable, '-c', KILL_POINT, str(repo.root), point], stderr=subprocess.PIPE, text=True)
    wait_for(repo.data / 'paused', worker)
    os.kill(worker.pid, signal.SIGKILL)
    assert worker.wait(timeout=10) == -signal.SIGKILL
    state = json.loads(repo.pending.read_text())
    assert state['status'] == 'rolling_back' and state['phase'] == 'checking_out'
    assert state['old_sha'] == repo.shas[0] and state['target_sha'] == repo.shas[1] and state['supervisor_pid'] == worker.pid
    assert git(repo.root, 'rev-parse', 'HEAD') == (repo.shas[1] if point == 'before_checkout' else repo.shas[0])
    assert not lock_held_elsewhere(repo.root), 'the killed monitor still holds the lock'

    updater = FrameworkUpdater(str(repo.root))
    app = stub_update(monkeypatch, updater, repo.root, repo.shas[2])
    if entry == 'startup':
        assert asyncio.run(updater.verify_startup(app)) is False
        assert json.loads(repo.pending.read_text())['status'] == 'rolled_back'
        assert git(repo.root, 'rev-parse', 'HEAD') == repo.shas[0]
        # Code on disk changed under the restarted process only when checkout had not happened yet.
        assert app._restart_requested is (point == 'before_checkout')
        app._stop_event.clear()  # the next update comes from the restarted process
    result = asyncio.run(updater.update_to_version(repo.shas[2]))
    assert result['success'] is True, result
    assert git(repo.root, 'rev-parse', 'HEAD') == repo.shas[2]
    state = json.loads(repo.pending.read_text())
    assert state['transaction'] != 'first' and state['status'] == 'awaiting_restart' and state['old_sha'] == repo.shas[0]


def _awaiting_ws(repo):
    state = dict(json.loads(repo.pending.read_text()), status='awaiting_ws')
    repo.pending.write_text(json.dumps(state))
    return ready_app()


def test_process_confirms_only_its_own_transaction(repo, monkeypatch):
    app = _awaiting_ws(repo)
    fd = supervisor.acquire_deploy_lock(repo.root)  # a live monitor
    try:
        monkeypatch.setenv(TRANSACTION_ENV, 'someone-else')
        assert asyncio.run(FrameworkUpdater(str(repo.root)).verify_startup(app)) is False
        assert json.loads(repo.pending.read_text())['status'] == 'awaiting_ws'
        monkeypatch.setenv(TRANSACTION_ENV, 'first')
        assert asyncio.run(FrameworkUpdater(str(repo.root)).verify_startup(app)) is True
        assert json.loads(repo.pending.read_text())['status'] == 'confirmed'
    finally:
        os.close(fd)


def test_manual_start_adopts_only_when_no_live_monitor(repo):
    app = _awaiting_ws(repo)
    holder = subprocess.Popen(
        [sys.executable, '-c', HOLDER.replace("s.transition(root, 'first', ('awaiting_restart',), status='confirmed')", ''), str(repo.root)]
    )
    try:
        wait_for(repo.data / 'paused', holder)
        assert asyncio.run(FrameworkUpdater(str(repo.root)).verify_startup(app)) is False
        assert json.loads(repo.pending.read_text())['status'] == 'awaiting_ws'
    finally:
        holder.kill()
        holder.wait()
    assert asyncio.run(FrameworkUpdater(str(repo.root)).verify_startup(app)) is True
    assert json.loads(repo.pending.read_text())['status'] == 'confirmed'
    assert not lock_held_elsewhere(repo.root)


def test_adopted_verification_without_ws_rolls_back_and_restarts(repo):
    app = _awaiting_ws(repo)
    app.hook_manager.connection_status.statuses = []
    assert asyncio.run(FrameworkUpdater(str(repo.root), ws_timeout=0.3).verify_startup(app)) is False
    assert json.loads(repo.pending.read_text())['status'] == 'rolled_back'
    assert git(repo.root, 'rev-parse', 'HEAD') == repo.shas[0]
    assert app._restart_requested is True


def test_monitor_and_process_agree_on_transaction_variable():
    assert supervisor.TRANSACTION_ENV == TRANSACTION_ENV
