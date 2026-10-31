"""Public updater contracts; Git and SQLite use disposable local fixtures only."""

import asyncio
import os
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from web.tools._updater import supervisor
from web.tools._updater.framework import FrameworkUpdater

GITHUB_URL = 'https://github.com/xmxxc/QQBotForge'


def git(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def github_commit(publisher, path):
    sha = path.split('/')[2]
    return {'sha': sha, 'commit': {'tree': {'sha': git(publisher, 'rev-parse', f'{sha}^{{tree}}')}}}


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    remote = tmp_path / 'remote.git'
    publisher = tmp_path / 'publisher'
    work = tmp_path / 'app'
    git(tmp_path, 'init', '--bare', str(remote))
    git(tmp_path, 'clone', str(remote), str(publisher))
    git(publisher, 'config', 'user.email', 'test@example.com')
    git(publisher, 'config', 'user.name', 'Test')
    (publisher / '.gitignore').write_text('/data\n', encoding='utf-8')
    (publisher / 'core.txt').write_text('one', encoding='utf-8')
    git(publisher, 'add', '.')
    git(publisher, 'commit', '-m', 'one')
    git(publisher, 'branch', '-M', 'personal')
    git(publisher, 'push', '-u', 'origin', 'personal')
    old = git(publisher, 'rev-parse', 'HEAD')
    git(tmp_path, 'clone', '--branch', 'personal', str(remote), str(work))
    (publisher / 'core.txt').write_text('two', encoding='utf-8')
    git(publisher, 'add', '.')
    git(publisher, 'commit', '-m', 'two')
    git(publisher, 'push', 'origin', 'personal')
    target = git(publisher, 'rev-parse', 'HEAD')
    real_run = subprocess.run
    requests = []

    def local_network(command, *args, **kwargs):
        if 'fetch' in command:
            requests.append(command[:])
            command = [str(remote) if x == GITHUB_URL + '.git' else x for x in command]
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', local_network)
    updater = FrameworkUpdater(str(work))
    checks = [
        dict(
            name='check',
            status='completed',
            conclusion='success',
            head_sha=target,
            app={'slug': 'github-actions'},
            details_url=f'{GITHUB_URL}/actions/runs/123/job/456',
        )
    ]

    async def api(path=''):
        if '/check-runs' in path:
            return {'total_count': len(checks), 'check_runs': checks}
        if '/actions/workflows/ci.yml/runs' in path:
            return {
                'total_count': 1,
                'workflow_runs': [
                    dict(
                        head_sha=target, head_branch='personal', event='push', path='.github/workflows/ci.yml', status='completed', conclusion='success', id=123
                    )
                ],
            }
        if path.startswith('/commits?'):
            return [{'sha': target, 'commit': {'message': 'two'}, 'html_url': f'{GITHUB_URL}/commit/{target}'}]
        if path.startswith('/commits/'):
            return github_commit(publisher, path)
        return {'sha': target}

    monkeypatch.setattr(updater, '_fetch_api', api)
    return SimpleNamespace(updater=updater, work=work, publisher=publisher, remote=remote, old=old, target=target, checks=checks, requests=requests)


def test_check_reports_fixed_personal_source_and_exact_ci(deployment):
    d = deployment
    result = asyncio.run(d.updater.check_for_updates())
    assert result['source'] == {'repository': 'xmxxc/QQBotForge', 'branch': 'personal'}
    assert result['current_version'] == d.old
    assert result['latest_version'] == d.target
    assert result['gap'] == 1
    assert result['can_update'] is True
    # A local origin is deliberately configured: deployment origin must not select the update source.
    assert GITHUB_URL + '.git' in d.requests[0]


def test_unrelated_success_check_cannot_authorize_update(deployment):
    deployment.checks[0]['name'] = 'unrelated-job'
    result = asyncio.run(deployment.updater.check_for_updates())
    assert result['can_update'] is False


def test_non_git_update_is_refused(tmp_path):
    result = asyncio.run(FrameworkUpdater(str(tmp_path)).update_to_latest())
    assert result['success'] is False
    assert 'git' in result['message'].lower()


@pytest.mark.parametrize('kind', ['same', 'diverged', 'unrelated', 'conflict', 'untracked', 'ignored', 'ci-failure'])
def test_unsafe_update_is_refused_without_changing_head(deployment, kind):
    d = deployment
    version = d.target
    if kind == 'same':
        version = d.old
    elif kind == 'diverged':
        git(d.work, 'config', 'user.email', 'test@example.com')
        git(d.work, 'config', 'user.name', 'Test')
        (d.work / 'local.txt').write_text('local')
        git(d.work, 'add', '.')
        git(d.work, 'commit', '-m', 'diverged')
    elif kind == 'unrelated':
        git(d.work, 'fetch', str(d.remote), 'personal')
        git(d.work, 'config', 'user.email', 'test@example.com')
        git(d.work, 'config', 'user.name', 'Test')
        git(d.work, 'checkout', '--detach', d.target)
        (d.work / 'other.txt').write_text('outside personal')
        git(d.work, 'add', '.')
        git(d.work, 'commit', '-m', 'not on personal')
        version = git(d.work, 'rev-parse', 'HEAD')
        git(d.work, 'checkout', '--detach', d.old)
    elif kind == 'conflict':
        (d.work / 'core.txt').write_text('local changes')
    elif kind in ('untracked', 'ignored'):
        (d.publisher / 'new.txt').write_text('upstream')
        git(d.publisher, 'add', '.')
        git(d.publisher, 'commit', '-m', 'add new file')
        git(d.publisher, 'push', 'origin', 'personal')
        version = git(d.publisher, 'rev-parse', 'HEAD')
        d.checks[0]['head_sha'] = version

        # Fixture CI stays pinned to the original tip, so bypass only the external API.
        async def ci_api(path=''):
            if '/check-runs' in path:
                return {'total_count': 1, 'check_runs': d.checks}
            if path.startswith('/commits/'):
                return github_commit(d.publisher, path)
            return {
                'workflow_runs': [
                    dict(
                        head_sha=version,
                        head_branch='personal',
                        event='push',
                        path='.github/workflows/ci.yml',
                        id=123,
                        status='completed',
                        conclusion='success',
                    )
                ]
            }

        d.updater._fetch_api = ci_api
        (d.work / 'new.txt').write_text('keep me')
        if kind == 'ignored':
            (d.work / '.git/info/exclude').write_text('new.txt\n')
    else:
        d.checks[0]['conclusion'] = 'failure'
    before = git(d.work, 'rev-parse', 'HEAD')
    result = asyncio.run(d.updater.update_to_version(version))
    assert result['success'] is False
    assert git(d.work, 'rev-parse', 'HEAD') == before
    assert not list((d.work / 'data').glob('backup/*'))
    if kind in ('untracked', 'ignored'):
        assert (d.work / 'new.txt').read_text() == 'keep me'


def test_safe_update_preserves_local_files_and_backs_up_committed_wal(deployment, monkeypatch):
    d = deployment
    data = d.work / 'data'
    data.mkdir()
    db = sqlite3.connect(data / 'extension.db')
    db.execute('pragma journal_mode=WAL')
    db.execute('create table value (item text)')
    db.execute("insert into value values ('committed WAL')")
    db.commit()
    (d.work / 'owner.txt').write_text('local untracked')
    (d.work / '.gitignore').write_text('/data\n/owner-cache\n')
    git(d.work, 'add', '.gitignore')
    events = []

    class Watcher:
        _watcher_running = True

        def stop_watcher(self):
            events.append(('paused', git(d.work, 'rev-parse', 'HEAD')))

        def start_watcher(self):
            events.append(('resumed', git(d.work, 'rev-parse', 'HEAD')))

    app = SimpleNamespace(plugin_manager=Watcher(), _stop_event=asyncio.Event(), _restart_requested=False)
    monkeypatch.setattr('web.tools._updater.framework.get_app', lambda: app)
    launched = []
    real_popen = subprocess.Popen

    def launch(command, *args, **kwargs):
        if command[0] == 'git':
            return real_popen(command, *args, **kwargs)
        launched.append((command, kwargs))
        Path(command[1]).with_suffix('.ready').write_text(command[5])
        return SimpleNamespace(pid=99999, poll=lambda: None)

    monkeypatch.setattr(subprocess, 'Popen', launch)
    result = asyncio.run(d.updater.update_to_version(d.target, skip_backup=True, auto_restart=False))
    assert result['success'] is True
    assert git(d.work, 'rev-parse', 'HEAD') == d.target
    assert git(d.work, 'rev-parse', '--abbrev-ref', 'HEAD') == 'HEAD'
    assert (d.work / 'owner.txt').read_text() == 'local untracked'
    assert (d.work / '.gitignore').read_text() == '/data\n/owner-cache\n'
    assert git(d.work, 'diff', '--cached', '--name-only') == '.gitignore'
    assert events == [('paused', d.old)]
    assert launched and app._stop_event.is_set()
    backup = d.work / result['backup_path'] / 'data/extension.db'
    with sqlite3.connect(backup) as restored:
        assert restored.execute('select item from value').fetchone() == ('committed WAL',)
    db.close()


@pytest.mark.parametrize('mode', ['crash', 'timeout', 'confirm'])
def test_supervisor_handles_startup_crash_timeout_and_success(deployment, tmp_path, mode):
    import json
    import sys

    from web.tools._updater.supervisor import supervise_update

    d = deployment
    git(d.work, 'fetch', str(d.remote), 'personal')
    git(d.work, 'checkout', '--detach', d.target)
    data = d.work / 'data'
    data.mkdir()
    pending = data / 'update_pending.json'
    pending.write_text(json.dumps(dict(transaction='test', status='awaiting_restart', old_sha=d.old, target_sha=d.target, backup_path='data/backup/kept')))
    script = tmp_path / 'candidate.py'
    script.write_text(
        """
import json, pathlib, subprocess, time
p = pathlib.Path('data/update_pending.json')
s = json.loads(p.read_text())
head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
if head == s['old_sha']:
    pathlib.Path('data/old-started').touch()
elif MODE == 'crash':
    raise SystemExit(2)
elif MODE == 'confirm':
    s['status'] = 'confirmed'
    p.write_text(json.dumps(s))
    time.sleep(.4)
else:
    time.sleep(20)
""".replace('MODE', repr(mode))
    )
    assert supervise_update(d.work, [sys.executable, str(script)], 'test', timeout=0.2) == 0
    state = json.loads(pending.read_text())
    assert state['status'] == ('confirmed' if mode == 'confirm' else 'rolled_back')
    assert git(d.work, 'rev-parse', 'HEAD') == (d.target if mode == 'confirm' else d.old)
    assert (data / 'old-started').exists() == (mode != 'confirm')
    assert state['backup_path'] == 'data/backup/kept'
    assert not supervisor.deploy_lock_held(d.work)


@pytest.mark.asyncio
async def test_real_connection_status_confirms_update(deployment):
    import json

    from core.module.hook import HookManager

    d = deployment
    git(d.work, 'fetch', str(d.remote), 'personal')
    git(d.work, 'checkout', '--detach', d.target)
    (d.work / 'data').mkdir()
    d.updater.pending_file.write_text(json.dumps(dict(transaction='t1', status='awaiting_ws', target_sha=d.target)))
    hook = HookManager()
    publisher = hook.connection_status._register('test-bot', 'websocket', bot_generation='new')
    publisher.publish('ready', reason='ready', login_kind='fresh')
    assert await d.updater.verify_startup(SimpleNamespace(hook_manager=hook)) is True
    assert d.updater.get_version_info()['last_update']['status'] == 'confirmed'


def test_dead_supervisor_refuses_restart_and_restores_old_checkout(deployment, monkeypatch):
    d = deployment
    app = SimpleNamespace(plugin_manager=None, _stop_event=asyncio.Event(), _restart_requested=False)
    monkeypatch.setattr('web.tools._updater.framework.get_app', lambda: app)
    real_popen = subprocess.Popen

    def launch(command, *args, **kwargs):
        if command[0] == 'git':
            return real_popen(command, *args, **kwargs)
        return SimpleNamespace(pid=99999, poll=lambda: 1, terminate=lambda: None, wait=lambda **_: 1)

    monkeypatch.setattr(subprocess, 'Popen', launch)
    result = asyncio.run(d.updater.update_to_version(d.target))
    assert result['success'] is False
    assert git(d.work, 'rev-parse', 'HEAD') == d.old
    assert not app._stop_event.is_set()


def test_concurrent_updaters_cannot_switch_same_deployment_twice(deployment):
    d = deployment
    fd = supervisor.acquire_deploy_lock(d.work)  # another live updater
    d.updater.claim_timeout = 0.2
    try:
        result = asyncio.run(d.updater.update_to_version(d.target))
    finally:
        os.close(fd)
    assert result['success'] is False
    assert git(d.work, 'rev-parse', 'HEAD') == d.old
    assert not d.requests


@pytest.mark.parametrize('bad_ci', ['missing', 'wrong-sha', 'wrong-event', 'malformed', 'pending'])
def test_check_fails_closed_for_unproven_ci(deployment, monkeypatch, bad_ci):
    d = deployment
    original = d.updater._fetch_api

    async def api(path=''):
        response = await original(path)
        if '/actions/workflows/' in path:
            if bad_ci == 'missing':
                response['workflow_runs'] = []
            elif bad_ci == 'wrong-sha':
                response['workflow_runs'][0]['head_sha'] = d.old
            elif bad_ci == 'wrong-event':
                response['workflow_runs'][0]['event'] = 'pull_request'
            elif bad_ci == 'malformed':
                return {'workflow_runs': [42]}
            else:
                response['workflow_runs'][0]['status'] = 'in_progress'
        return response

    monkeypatch.setattr(d.updater, '_fetch_api', api)
    result = asyncio.run(d.updater.check_for_updates())
    assert result.get('can_update') is False


def test_nested_non_git_directory_does_not_update_parent_repository(deployment):
    nested = deployment.work / 'nested'
    nested.mkdir()
    result = asyncio.run(FrameworkUpdater(str(nested)).update_to_latest())
    assert result['success'] is False
    assert not deployment.requests


def test_corrupt_database_refuses_checkout_and_preserves_local_bytes(deployment, monkeypatch):
    d = deployment
    (d.work / 'data').mkdir()
    invalid = d.work / 'data/broken.db'
    invalid.write_bytes(b'not a SQLite file')
    monkeypatch.setattr('web.tools._updater.framework.get_app', lambda: SimpleNamespace(_stop_event=asyncio.Event(), plugin_manager=None))
    result = asyncio.run(d.updater.update_to_version(d.target))
    assert result['success'] is False
    assert git(d.work, 'rev-parse', 'HEAD') == d.old
    assert invalid.read_bytes() == b'not a SQLite file'
    assert not d.updater.lock_active()


def test_git_url_rewrite_in_deployment_is_not_used(deployment):
    d = deployment
    git(d.work, 'config', 'url.https://mirror.example/.insteadOf', 'https://github.com/')
    result = asyncio.run(d.updater.check_for_updates())
    assert result['error'] is None
    assert result['latest_version'] == d.target


@pytest.mark.asyncio
async def test_prior_terminal_result_does_not_hide_new_update_progress(deployment, monkeypatch):
    import json

    d = deployment
    (d.work / 'data').mkdir()
    d.updater.pending_file.write_text(json.dumps({'status': 'confirmed', 'message': 'previous update'}))
    entered = asyncio.Event()
    proceed = asyncio.Event()
    api = d.updater._fetch_api

    async def waiting_api(path=''):
        if '/actions/workflows/' in path:
            entered.set()
            await proceed.wait()
        return await api(path)

    monkeypatch.setattr(d.updater, '_fetch_api', waiting_api)
    task = asyncio.create_task(d.updater.update_to_version(d.target))
    await entered.wait()
    try:
        assert d.updater.get_progress()['is_updating'] is True
    finally:
        proceed.set()
        await task


@pytest.mark.parametrize('key,value', [('http.https://github.com/.proxy', 'http://mirror.example'), ('http.https://github.com/.followRedirects', 'true')])
def test_per_url_git_network_settings_cannot_override_direct_fetch(deployment, key, value):
    d = deployment
    git(d.work, 'config', key, value)
    result = asyncio.run(d.updater.check_for_updates())
    assert result['error'] is None
    assert result['latest_version'] == d.target


def test_supervisor_rollback_ignores_ambient_git_directory(deployment, tmp_path, monkeypatch):
    import json
    import sys

    from web.tools._updater.supervisor import supervise_update

    d = deployment
    git(d.work, 'fetch', str(d.remote), 'personal')
    git(d.work, 'checkout', '--detach', d.target)
    outsider = tmp_path / 'unrelated'
    outsider.mkdir()
    git(outsider, 'init')
    data = d.work / 'data'
    data.mkdir()
    pending = data / 'update_pending.json'
    pending.write_text(json.dumps(dict(transaction='test', status='awaiting_restart', old_sha=d.old, target_sha=d.target, backup_path='data/backup/kept')))
    with monkeypatch.context() as context:
        context.setenv('GIT_DIR', str(outsider / '.git'))
        assert supervise_update(d.work, [sys.executable, '-c', 'pass'], 'test', timeout=0.2) == 0
    assert json.loads(pending.read_text())['status'] == 'rolled_back'
    assert git(d.work, 'rev-parse', 'HEAD') == d.old


def test_backup_refuses_destination_symlink_outside_deployment(deployment, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (deployment.work / 'data').mkdir()
    (deployment.work / 'data/backup').symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match='部署目录'):
        deployment.updater.backup_databases()
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_confirmation_releases_lock_without_live_supervisor(deployment):
    import json

    from core.module.hook import HookManager

    d = deployment
    git(d.work, 'fetch', str(d.remote), 'personal')
    git(d.work, 'checkout', '--detach', d.target)
    (d.work / 'data').mkdir()
    d.updater.pending_file.write_text(json.dumps(dict(transaction='t1', status='awaiting_ws', target_sha=d.target)))
    hook = HookManager()
    hook.connection_status._register('test-bot', 'websocket', bot_generation='new').publish('ready', reason='ready', login_kind='fresh')
    assert await d.updater.verify_startup(SimpleNamespace(hook_manager=hook)) is True
    assert not d.updater.lock_active()


def test_startup_recovery_does_not_need_web_panel_context(tmp_path, monkeypatch):
    from web.tools._updater import handlers

    monkeypatch.setattr(handlers, '_updater', None)
    updater = handlers.get_updater(str(tmp_path))
    assert updater.base_dir == tmp_path.resolve()
    assert handlers.get_updater() is updater


@pytest.fixture
def foreign(tmp_path):
    """A bare repository with an unrelated personal branch an attacker would redirect to."""
    remote = tmp_path / 'foreign.git'
    seed = tmp_path / 'foreign-seed'
    git(tmp_path, 'init', '--bare', str(remote))
    git(tmp_path, 'init', '-b', 'personal', str(seed))
    git(seed, 'config', 'user.email', 'x@example.com')
    git(seed, 'config', 'user.name', 'X')
    (seed / 'file').write_text('foreign')
    git(seed, 'add', '.')
    git(seed, 'commit', '-m', 'foreign')
    git(seed, 'push', str(remote), 'personal')
    return remote


@pytest.mark.parametrize('form', ['include.path', 'includeIf.gitdir', 'includeIf.onbranch'])
def test_included_git_config_cannot_redirect_update_source(deployment, foreign, tmp_path, form):
    """Deployment configuration, includes and all, never takes part in the fetch."""
    d = deployment
    included = tmp_path / 'included.config'
    included.write_text(f'[url "{foreign}"]\n    insteadOf = {GITHUB_URL}.git\n')
    key = {
        'include.path': 'include.path',
        'includeIf.gitdir': f'includeIf.gitdir:{d.work}/.git.path',
        'includeIf.onbranch': 'includeIf.onbranch:personal.path',
    }[form]
    git(d.work, 'config', '--local', key, str(included))
    result = asyncio.run(d.updater.check_for_updates())
    assert result['error'] is None
    assert result['latest_version'] == d.target


@pytest.mark.parametrize('scope', ['global', 'xdg', 'system'])
def test_global_and_system_git_config_are_ignored(deployment, foreign, tmp_path, monkeypatch, scope):
    d = deployment
    rewrite = f'[url "{foreign}"]\n    insteadOf = {d.remote}\n'
    home = tmp_path / 'home'
    (home / '.config/git').mkdir(parents=True)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)
    if scope == 'global':
        (home / '.gitconfig').write_text(rewrite)
    elif scope == 'xdg':
        (home / '.config/git/config').write_text(rewrite)
    else:
        system = tmp_path / 'system.config'
        system.write_text(rewrite)
        monkeypatch.setenv('GIT_CONFIG_SYSTEM', str(system))
    result = asyncio.run(d.updater.check_for_updates())
    assert result['error'] is None
    assert result['latest_version'] == d.target


def _url_named_source(d, monkeypatch):
    """Serve the fixed source from a file:// URL; Git, like for https://, honours a remote named after it."""
    url = f'file://{d.remote}'
    monkeypatch.setattr('web.tools._updater.framework.GITHUB_URL', url[: -len('.git')])
    return url


def test_same_named_remote_cannot_redirect_the_fixed_source(deployment, foreign, monkeypatch):
    d = deployment
    url = _url_named_source(d, monkeypatch)
    git(d.work, 'config', f'remote.{url}.url', str(foreign))
    result = asyncio.run(d.updater.check_for_updates())
    assert result['error'] is None
    assert result['latest_version'] == d.target


@pytest.mark.parametrize('variant', ['remote-url', 'remote-proxy'])
def test_deployment_transport_settings_send_nothing_anywhere(deployment, monkeypatch, variant):
    """Same-named remote URL/proxy plus an extra auth header must not produce a single request."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.path, self.headers.get('Authorization')))
            self.send_response(404)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    d = deployment
    url = _url_named_source(d, monkeypatch)
    try:
        if variant == 'remote-url':
            git(d.work, 'config', f'remote.{url}.url', f'http://127.0.0.1:{server.server_port}/foreign.git')
        else:
            git(d.work, 'config', f'remote.{url}.url', 'http://review-only.invalid/foreign.git')
            git(d.work, 'config', f'remote.{url}.proxy', f'http://127.0.0.1:{server.server_port}')
        git(d.work, 'config', 'http.extraHeader', 'Authorization: Basic TEST_ONLY_DUMMY')
        result = asyncio.run(d.updater.check_for_updates())
    finally:
        server.shutdown()
    assert received == []
    assert result['error'] is None
    assert result['latest_version'] == d.target


def test_proxy_and_git_variables_never_reach_git(monkeypatch):
    for name in ('HTTP_PROXY', 'https_proxy', 'ALL_PROXY', 'GIT_DIR', 'GIT_CONFIG_PARAMETERS'):
        monkeypatch.setenv(name, 'http://127.0.0.1:9')
    env = supervisor.clean_git_env()
    assert not {'HTTP_PROXY', 'https_proxy', 'ALL_PROXY', 'GIT_DIR', 'GIT_CONFIG_PARAMETERS'} & set(env)
    assert env['GIT_CONFIG_NOSYSTEM'] == '1' and env['GIT_CONFIG_GLOBAL'] == os.devnull


def _switch(d, monkeypatch):
    app = SimpleNamespace(plugin_manager=None, _stop_event=asyncio.Event(), _restart_requested=False)
    monkeypatch.setattr('web.tools._updater.framework.get_app', lambda: app)
    real_popen = subprocess.Popen

    def launch(command, *args, **kwargs):
        if command[0] == 'git':
            return real_popen(command, *args, **kwargs)
        Path(command[1]).with_suffix('.ready').write_text(command[5])
        return SimpleNamespace(pid=99999, poll=lambda: None)

    monkeypatch.setattr(subprocess, 'Popen', launch)
    return asyncio.run(d.updater.update_to_version(d.target))


@pytest.mark.parametrize('surface', ['default-hook', 'hooks-path', 'fsmonitor'])
def test_checkout_runs_no_hooks_or_fsmonitor(deployment, monkeypatch, tmp_path, surface):
    d = deployment
    marker = tmp_path / 'executed'
    script = f'#!/bin/sh\necho ran >> {marker}\necho tampered > {d.work}/core.txt\n'
    if surface == 'fsmonitor':
        monitor = tmp_path / 'fsmonitor.sh'
        monitor.write_text(script)
        monitor.chmod(0o755)
        git(d.work, 'config', 'core.fsmonitor', str(monitor))
    else:
        hooks = d.work / '.git/hooks' if surface == 'default-hook' else tmp_path / 'hooks'
        hooks.mkdir(exist_ok=True)
        for name in ('post-checkout', 'reference-transaction'):
            (hooks / name).write_text(script)
            (hooks / name).chmod(0o755)
        if surface == 'hooks-path':
            git(d.work, 'config', 'core.hooksPath', str(hooks))
    result = _switch(d, monkeypatch)
    assert result['success'] is True, result
    assert not marker.exists()
    assert (d.work / 'core.txt').read_text() == 'two'
    assert git(d.work, 'rev-parse', 'HEAD') == d.target


def test_github_tree_must_match_the_imported_commit(deployment, monkeypatch):
    d = deployment
    original = d.updater._fetch_api

    async def api(path=''):
        response = await original(path)
        if path == f'/commits/{d.target}':
            response['commit']['tree']['sha'] = '0' * 40
        return response

    monkeypatch.setattr(d.updater, '_fetch_api', api)
    result = _switch(d, monkeypatch)
    assert result['success'] is False
    assert git(d.work, 'rev-parse', 'HEAD') == d.old
