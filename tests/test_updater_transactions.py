"""Update transactions are fenced: a monitor or process only acts on its own transaction.

Real Git repositories and real child processes; GitHub, CI and restart are stubbed.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from web.tools._updater import supervisor
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
    (data / 'update.lock').write_text('first')
    monkeypatch.setenv('PYTHONPATH', str(REPO))
    monkeypatch.delenv(TRANSACTION_ENV, raising=False)
    return SimpleNamespace(root=root, data=data, shas=shas, pending=data / 'update_pending.json', lock=data / 'update.lock')


def wait_for(path, seconds=15):
    deadline = time.monotonic() + seconds
    while not path.exists():
        assert time.monotonic() < deadline, f'{path.name} never appeared'
        time.sleep(0.05)
    return path.read_text()


CONFIRM_THEN_UPDATE = """
import asyncio, json, pathlib, sys, time
from types import SimpleNamespace
import web.tools._updater.framework as framework
root = pathlib.Path.cwd(); data = root / 'data'
if json.loads((data / 'update_pending.json').read_text())['status'] == 'rolled_back':
    (data / 'old-started').touch(); sys.exit(0)
up = framework.FrameworkUpdater(str(root))
ready = SimpleNamespace(transport='websocket', phase='ready')
app = SimpleNamespace(_stop_event=asyncio.Event(), plugin_manager=None, hook_manager=SimpleNamespace(connection_status=SimpleNamespace(statuses=[ready])))
framework.get_app = lambda: app
def slow_fetch():
    # Give a lagging monitor time to act on the new claim.
    (data / 'claim-during-fetch').write_text(up.lock_file.read_text() if up.lock_file.exists() else '')
    time.sleep(0.6)
    (data / 'claim-after-wait').write_text(up.lock_file.read_text() if up.lock_file.exists() else '')
    return sys.argv[1]
up._fetch = slow_fetch
async def ci(sha): return {'status': 'success'}
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
    assert repo.lock.exists(), 'old monitor removed the B→C lock'
    assert (repo.data / 'claim-after-wait').read_text(), 'old monitor removed the B→C lock during its fetch'
    state = json.loads(repo.pending.read_text())
    second = state['transaction']
    assert second != 'first'
    assert state['status'] == 'awaiting_restart'
    assert (repo.data / 'claim-during-fetch').read_text() == second
    assert (repo.data / 'claim-after-wait').read_text() == second
    assert repo.lock.read_text() == second
    assert not (repo.data / 'old-started').exists()


INTERLEAVE = """
import json, pathlib, time
data = pathlib.Path.cwd() / 'data'
# Another transaction takes over the state and lock while the first is verifying.
(data / 'update.lock').write_text('second')
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
    assert repo.lock.read_text() == 'second'
    assert git(repo.root, 'rev-parse', 'HEAD') == repo.shas[1]


def test_transition_and_release_only_apply_to_own_transaction(repo):
    before = repo.pending.read_text()
    assert supervisor.transition(repo.root, 'other', ('awaiting_restart',), status='rolled_back') is None
    assert supervisor.transition(repo.root, 'first', ('awaiting_ws',), status='rolled_back') is None
    assert repo.pending.read_text() == before
    assert supervisor.release_lock(repo.root, 'other') is False
    assert repo.lock.read_text() == 'first'


def test_monitor_killed_between_fence_and_action_leaves_nothing_half_done(repo, monkeypatch):
    """The decision is committed atomically before acting; dying afterwards changes neither Git nor the lock."""
    script = repo.data / 'child.py'
    script.write_text('import time; time.sleep(2)')

    def killed(process):
        raise KeyboardInterrupt

    monkeypatch.setattr(supervisor, '_stop', killed)
    with pytest.raises(KeyboardInterrupt):
        supervise_update(repo.root, [sys.executable, str(script)], 'first', timeout=0.3)
    state = json.loads(repo.pending.read_text())
    assert state['transaction'] == 'first' and state['status'] == 'rolling_back'
    assert git(repo.root, 'rev-parse', 'HEAD') == repo.shas[1]
    assert repo.lock.read_text() == 'first'
    # A late confirmation of the abandoned transaction is fenced out as well.
    monkeypatch.setenv(TRANSACTION_ENV, 'first')
    ready = SimpleNamespace(transport='websocket', phase='ready')
    app = SimpleNamespace(hook_manager=SimpleNamespace(connection_status=SimpleNamespace(statuses=[ready])))
    assert asyncio.run(FrameworkUpdater(str(repo.root)).verify_startup(app)) is False
    assert repo.lock.read_text() == 'first'


def test_mutex_left_by_killed_holder_is_broken(repo):
    mutex = repo.data / 'update_state.mutex'
    mutex.write_text('12345')
    old = time.time() - 60
    os.utime(mutex, (old, old))
    assert supervisor.transition(repo.root, 'first', ('awaiting_restart',), status='awaiting_ws')['status'] == 'awaiting_ws'
    assert not mutex.exists()


def _awaiting_ws(repo, **extra):
    git(repo.root, 'checkout', '-q', '--detach', repo.shas[1])
    state = dict(json.loads(repo.pending.read_text()), status='awaiting_ws', **extra)
    repo.pending.write_text(json.dumps(state))
    ready = SimpleNamespace(transport='websocket', phase='ready')
    return SimpleNamespace(hook_manager=SimpleNamespace(connection_status=SimpleNamespace(statuses=[ready])))


def test_process_confirms_only_its_own_transaction(repo, monkeypatch):
    app = _awaiting_ws(repo)
    monkeypatch.setenv(TRANSACTION_ENV, 'someone-else')
    assert asyncio.run(FrameworkUpdater(str(repo.root)).verify_startup(app)) is False
    assert json.loads(repo.pending.read_text())['status'] == 'awaiting_ws'
    assert repo.lock.read_text() == 'first'
    monkeypatch.setenv(TRANSACTION_ENV, 'first')
    assert asyncio.run(FrameworkUpdater(str(repo.root)).verify_startup(app)) is True
    assert json.loads(repo.pending.read_text())['status'] == 'confirmed'
    assert not repo.lock.exists()


def test_manual_start_adopts_transaction_only_when_its_monitor_is_gone(repo):
    import psutil

    me = psutil.Process()
    app = _awaiting_ws(repo, supervisor_pid=me.pid, supervisor_created=me.create_time())
    assert asyncio.run(FrameworkUpdater(str(repo.root)).verify_startup(app)) is False
    assert repo.lock.read_text() == 'first'
    state = json.loads(repo.pending.read_text())
    dead = subprocess.Popen([sys.executable, '-c', 'pass'])
    dead.wait()
    state.update(supervisor_pid=dead.pid, supervisor_created=0)
    repo.pending.write_text(json.dumps(state))
    assert asyncio.run(FrameworkUpdater(str(repo.root)).verify_startup(app)) is True
    assert not repo.lock.exists()


def test_lock_of_finished_transaction_does_not_block_next_update(repo):
    updater = FrameworkUpdater(str(repo.root))
    assert updater.lock_active() is True
    state = dict(json.loads(repo.pending.read_text()), status='confirmed')
    repo.pending.write_text(json.dumps(state))  # releaser died after confirming
    assert updater.lock_active() is False
    updater._claim('next')
    assert repo.lock.read_text() == 'next'


def test_monitor_and_process_agree_on_transaction_variable():
    assert supervisor.TRANSACTION_ENV == TRANSACTION_ENV
