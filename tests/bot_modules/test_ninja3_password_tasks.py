"""#75 real plugin/message/authenticated HTTP paths with temporary DB and fake account-center HTTP."""

import asyncio
import base64
import functools
import json
import re
import socket
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest
from account_host import account_host as account_host  # noqa: F401
from cryptography.fernet import Fernet
from test_access_control import access_host as access_host  # noqa: F401
from test_ninja3 import grant
from test_ninja3 import ninja as ninja  # noqa: F401

from plugins.ninja3.password_protocol import change_password
from plugins.ninja3.password_tasks import IntentCipher, PasswordTasks, random_password
from plugins.ninja3.storage import ExtensionStore

LOGIN = '16600000000'
OLD = 'fake-old-password'
CHANGE = '/api/web/user_center/change_pwd'


@pytest.fixture(autouse=True)
def forbid_game_network(monkeypatch):
    """Permit only the loopback host used by the real app tests; deny all other DNS/TCP."""
    for name in ('connect', 'connect_ex'):
        real = getattr(socket.socket, name)

        def connect(self, address, _real=real):
            if not isinstance(address, tuple) or address[0] not in {'127.0.0.1', '::1'}:
                raise AssertionError('external network forbidden')
            return _real(self, address)

        monkeypatch.setattr(socket.socket, name, connect)
    real_dns = socket.getaddrinfo

    def dns(host, *args, **kwargs):
        if host not in {'127.0.0.1', '::1', 'localhost'}:
            raise AssertionError('external DNS forbidden')
        return real_dns(host, *args, **kwargs)

    monkeypatch.setattr(socket, 'getaddrinfo', dns)


class FakeCenter:
    def __init__(self):
        self.requests = []
        self.change_result = httpx.Response(200, json={'code': 100})
        self.block_path = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.identity = {'phone': LOGIN, 'area': '86'}

    async def handle(self, request):
        self.requests.append(request)
        path = request.url.path
        if path == self.block_path:
            self.entered.set()
            await self.release.wait()
        if path == '/env.js':
            key = base64.b64encode(bytes(range(32))).decode()
            config = dict(appId='fake-app', baseUrl='/api/web', backend='user.pandadastudio.com', signSecret=key, encryptoSecret=key)
            return httpx.Response(200, text='\n'.join(f'{k}: "{v}",' for k, v in config.items()))
        if path.endswith('/login/pwd'):
            return httpx.Response(200, json={'code': 100}, headers={'set-cookie': 's=fake-cookie; Secure; Path=/'})
        if path.endswith('/userinfo'):
            return httpx.Response(200, json={'code': 100, 'data': self.identity})
        assert path == CHANGE and request.headers['cookie'] == 's=fake-cookie'
        if isinstance(self.change_result, BaseException):
            raise self.change_result
        return self.change_result

    @property
    def changes(self):
        return sum(request.url.path == CHANGE for request in self.requests)


@pytest.fixture
async def changing(ninja, tmp_path):
    host = ninja
    key = tmp_path / 'test-only-intent.key'
    key.write_bytes(Fernet.generate_key())
    key.chmod(0o600)
    host.key_file = key
    host.center = FakeCenter()
    host.tasks = host.ninja().password_tasks
    host.tasks.cipher = type(host.tasks.cipher)(str(key))
    host.tasks.protocol = functools.partial(change_password, transport=httpx.MockTransport(host.center.handle))
    host.account = await host.setup(login=LOGIN, password=OLD)
    await grant(host)
    await host.event('忍3', user='owner-a')
    host.sender.messages.clear()

    async def select(account=None, **kwargs):
        status, response = await host.ninja_api('POST', 'password-tasks', {'account': account or host.account['id']}, **kwargs)
        assert status == 200, response
        return response['data']

    async def action(task, action='confirm', **kwargs):
        status, response = await host.ninja_api('POST', f'password-tasks/{task["id"]}/{action}', {}, **kwargs)
        return status, response

    async def finished():
        for _ in range(200):
            workers = list(host.tasks.workers.values())
            if not workers:
                return
            await asyncio.gather(*workers)
        raise AssertionError('workers did not finish')

    host.select, host.action, host.finished = select, action, finished
    yield host
    host.center.release.set()
    await host.ninja().password_tasks.shutdown()


async def task_info(host, task):
    return (await host.ninja_api('GET', 'password-tasks/' + task['id']))[1]['data']


async def raw_task(host, task):
    return await host.tasks.store.atomic(lambda conn: host.tasks._read(conn, 'bot-a', task['id']))


async def test_backend_selection_confirmation_and_protected_intent_then_one_submit(changing, caplog):
    host = changing
    task = await host.select()
    assert task['state'] == 'selected' and not host.center.requests
    with sqlite3.connect(host.tasks.store.path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 2
    assert Path(host.tasks.store.path).stat().st_mode & 0o077 == 0
    assert not host.ninja().account_paused('bot-a', host.account['id'])
    assert (await host.action(task, authenticated=False))[0] == 401
    assert (await host.action(task, bot='bot-b'))[0] == 422
    host.center.block_path = CHANGE
    assert (await host.action(task))[0] == 200
    await host.center.entered.wait()
    assert host.ninja().account_paused('bot-a', host.account['id'])
    raw = await raw_task(host, task)
    intent = host.tasks._intent('bot-a', raw)
    new = intent['new_password']
    assert re.fullmatch(r'(?:[0-9]{4}(?:xmx|xc)|(?:xmx|xc)[0-9]{4})', new) and new != OLD
    assert raw['remote'] == 'unknown' and raw['state'] == 'submitting'
    with sqlite3.connect(host.tasks.store.path) as conn:
        payload = conn.execute('SELECT payload FROM ninja3_password_tasks').fetchone()[0]
    assert new not in payload and OLD not in payload and LOGIN not in payload
    locked = await host.ninja_api('PATCH', 'accounts/' + host.account['id'], {'password': 'other'})
    assert locked == (409, {'success': False, 'error': '账号改密任务未解决，禁止修改登录凭据或删除'})
    # Non-credential edits (scope/description) stay possible and keep the pause.
    assert (await host.ninja_api('PATCH', 'accounts/' + host.account['id'], {'description': 'during task'}))[0] == 200
    assert (await host.ninja_api('DELETE', 'accounts/' + host.account['id']))[0] == 409
    assert await host.say('账号列表', user='MEMBERA') == ['本群暂无开放账号']
    host.center.release.set()
    await host.finished()
    result = await task_info(host, task)
    assert (result['remote'], result['local'], result['state'], result['notification']) == ('reported_success', 'committed', 'completed', 'sent')
    assert not host.ninja().account_paused('bot-a', host.account['id'])
    assert new in (await host.say('账号 账号A', user='MEMBERA'))[0]
    assert host.center.changes == 1
    assert (await host.action(task))[1]['data']['id'] == task['id'] and host.center.changes == 1
    assert new not in str(result) + caplog.text
    pushes = [m for m in host.sender.messages if m[0] == 'push']
    assert pushes and all(new not in str(m) and OLD not in str(m) for m in pushes)


async def test_message_owner_confirmation_group_and_private_real_entries(changing):
    host = changing
    assert '尚未选择账号' in (await host.say('忍3改密', user='owner-a'))[0]
    assert await host.say('忍3改密 账号A', user='MEMBERA') == ['仅主人可以操作改密任务']
    assert await host.say('忍3改密 账号A', user='owner-b') == ['仅主人可以操作改密任务']
    reply = (await host.say('忍3改密 账号A', user='owner-a'))[0]
    key = re.search(r'忍3确认 ([0-9a-f]{32})', reply)[1]
    assert not host.center.requests
    assert '已记录' in (await host.say('忍3确认 ' + key, user='owner-a'))[0]
    await host.finished()
    assert host.center.changes == 1
    assert '远端' not in str(await host.say('忍3任务 ' + key, user='owner-a'))
    await host.event('忍3任务 ' + key, user='owner-a')
    assert any('远端报告成功' in str(m) for m in host.sender.messages)
    # Private selection/confirmation also uses the actual plugin event dispatcher.
    await host.setup(name='账号B', login=LOGIN, password=OLD)
    await host.event('忍3改密 账号B', user='owner-a')
    tasks = (await host.ninja_api('GET', 'password-tasks'))[1]['data']
    selected = next(t for t in tasks if t['state'] == 'selected')
    await host.event('忍3确认 ' + selected['id'], user='owner-a')
    await host.finished()
    assert host.center.changes == 2


async def test_cross_actor_confirmation_and_no_password_parameter(changing):
    host = changing
    reply = (await host.say('忍3改密 账号A', user='owner-a'))[0]
    key = re.search(r'忍3确认 ([0-9a-f]{32})', reply)[1]
    assert (await host.action({'id': key}))[0] == 422  # backend cannot borrow QQ selection
    for body in ({}, {'account': ''}, {'account': host.account['id'], 'password': 'fake-sensitive'}):
        status, _ = await host.ninja_api('POST', 'password-tasks', body)
        assert status == 422
    assert not host.center.requests


async def test_durable_concurrent_claim_across_two_connections_returns_same_task(changing):
    host = changing
    other_store = ExtensionStore(host.tasks.store.path)
    other = PasswordTasks(other_store, cipher=IntentCipher(str(host.key_file)), notify=host.ninja().password_receipt, protocol=host.tasks.protocol)
    await other.initialize()
    host.center.block_path = CHANGE
    a = await host.select()
    assert (await host.select())['id'] == a['id']  # repeated selection shows the same task
    b = await other.select('bot-a', host.account['id'], 'backend-2')
    assert b['id'] != a['id']
    first, second = await asyncio.gather(host.tasks.confirm('bot-a', a['id'], 'backend'), other.confirm('bot-a', b['id'], 'backend-2'))
    assert first['id'] == second['id']
    await host.center.entered.wait()
    selected_again = await host.select()
    assert selected_again['id'] == first['id']
    assert other.is_paused('bot-a', host.account['id'])
    host.center.release.set()
    await host.finished()
    if other.workers:
        await asyncio.gather(*list(other.workers.values()))
    assert host.center.changes == 1
    await other.shutdown()
    other_store.close()


@pytest.mark.parametrize(
    ('response', 'remote'),
    [
        (httpx.Response(200, json={'code': 1300, 'msg': 'fake-response-secret'}), 'rejected'),
        (httpx.ReadTimeout('fake-response-secret'), 'unknown'),
        (httpx.Response(200, text='fake-response-secret'), 'unknown'),
    ],
)
async def test_rejected_unknown_pause_only_target_and_restart_never_resubmits(changing, response, remote):
    host = changing
    await host.setup(name='账号B', password='fake-other-account')
    host.center.change_result = response
    task = await host.select()
    await host.action(task)
    await host.finished()
    assert (await task_info(host, task))['remote'] == remote
    assert host.ninja().account_paused('bot-a', host.account['id'])
    assert '账号A' not in str(await host.say('账号列表', user='MEMBERA'))
    assert 'fake-other-account' in (await host.say('账号 账号B', user='MEMBERA'))[0]
    assert (await host.action(task, 'recover'))[0] == 422
    raw = await raw_task(host, task)
    await host.plugins.reload('ninja3')
    assert host.ninja().account_paused('bot-a', host.account['id'])
    restarted = host.ninja().password_tasks
    assert (await restarted.store.atomic(lambda conn: restarted._read(conn, 'bot-a', task['id'])))['intent'] == raw['intent']
    assert host.center.changes == 1 and not restarted.workers


@pytest.mark.parametrize('path', ['/env.js', CHANGE])
async def test_cancel_again_cancel_and_restart_preserve_intent_without_resubmit(changing, path):
    host = changing
    host.center.block_path = path
    task = await host.select()
    await host.action(task)
    await host.center.entered.wait()
    raw = await raw_task(host, task)
    for _ in range(2):
        assert (await host.action(task, 'cancel'))[0] == 200
    await host.finished()
    result = await task_info(host, task)
    assert result['remote'] == ('unknown' if path == CHANGE else 'not_submitted')
    assert host.ninja().account_paused('bot-a', host.account['id'])
    host.center.release.set()
    await host.plugins.reload('ninja3')
    restarted = host.ninja().password_tasks
    assert (await restarted.store.atomic(lambda conn: restarted._read(conn, 'bot-a', task['id'])))['intent'] == raw['intent']
    assert host.center.changes == int(path == CHANGE)


@pytest.mark.parametrize('key_on_restart', [False, True])
async def test_success_local_failure_then_restart_completes_only_local_and_keeps_other_flags(changing, monkeypatch, key_on_restart):
    host = changing
    # Future emergency flags are unrelated to task completion and must survive.
    account = await host.tasks.store.get('bot-a', 'accounts', host.account['id'])
    account['future_emergency'] = True
    await host.tasks.store.put('bot-a', 'accounts', account['id'], account)

    async def fail(*args):
        raise OSError('fake secret in disk error')

    monkeypatch.setattr(host.tasks, '_commit_local', fail)
    task = await host.select()
    await host.action(task)
    await host.finished()
    result = await task_info(host, task)
    assert result['remote'] == 'reported_success' and result['local'] == 'pending'
    assert result['error'] == 'local_commit_failed' and result['notification'] == 'sent'
    raw = await raw_task(host, task)
    new = host.tasks._intent('bot-a', raw)['new_password']
    assert (await host.tasks.store.get('bot-a', 'accounts', account['id']))['password'] == OLD
    if key_on_restart:
        monkeypatch.setenv('NINJA3_INTENT_KEY_FILE', str(host.key_file))
    await host.plugins.reload('ninja3')
    restarted = host.ninja().password_tasks
    # Missing key during reload is safe; provide the test key and explicitly resume local.
    if not key_on_restart:
        assert restarted.is_paused('bot-a', account['id'])
        restarted.cipher = type(restarted.cipher)(str(host.key_file))
        assert (await restarted.recover('bot-a', task['id']))['local'] == 'committed'
    else:
        assert (await restarted.get('bot-a', task['id']))['local'] == 'committed'
    final_account = await restarted.store.get('bot-a', 'accounts', account['id'])
    assert final_account['password'] == new and final_account['future_emergency'] is True
    assert host.center.changes == 1 and not restarted.is_paused('bot-a', account['id'])


async def test_final_send_gate_catches_pause_started_after_last_query_await(changing):
    host = changing
    task = await host.select()
    host.center.block_path = CHANGE
    acl = host.access
    real = type(acl).evaluate
    once = False

    async def evaluate(self, *args, **kwargs):
        nonlocal once
        decision = await real(self, *args, **kwargs)
        if len(args) >= 6 and args[1] == 'ninja3' and args[5] and not once:
            once = True
            await host.action(task)
            await host.center.entered.wait()
        return decision

    type(acl).evaluate = evaluate
    try:
        assert await host.say('账号 账号A', user='MEMBERA') == ['账号或授权刚刚变化，请重新发送查询']
    finally:
        type(acl).evaluate = real
    assert OLD not in str(host.sender.messages)
    host.center.release.set()
    await host.finished()


async def test_missing_key_or_transaction_failure_never_starts_or_loses_existing_pause(changing, monkeypatch):
    host = changing
    task = await host.select()
    monkeypatch.setattr(host.tasks, 'cipher', type(host.tasks.cipher)('/does-not-exist'))
    assert (await host.action(task))[0] == 422
    assert not host.center.requests and not host.ninja().account_paused('bot-a', host.account['id'])
    host.tasks.cipher = type(host.tasks.cipher)(str(host.key_file))
    real_save = host.tasks._save

    def fail_claim(conn, bot, record):
        real_save(conn, bot, record)
        if record['active']:
            raise sqlite3.OperationalError('fake sensitive details')

    monkeypatch.setattr(host.tasks, '_save', fail_claim)
    assert (await host.action(task))[0] == 503
    assert not host.center.requests and (await task_info(host, task))['state'] == 'selected'


async def test_identity_failure_and_notification_failure_do_not_lift_pause(changing):
    host = changing
    host.center.identity = {'phone': '16600000001', 'area': '86'}
    host.sender.push_result = (False, {'code': 'fake-push-failure'})
    task = await host.select()
    await host.action(task)
    await host.finished()
    result = await task_info(host, task)
    assert result['remote'] == 'not_submitted' and result['error'] == 'identity'
    assert result['notification'] == 'failed' and host.center.changes == 0
    assert host.ninja().account_paused('bot-a', host.account['id'])


def test_random_pattern_leading_zero_and_old_value_avoidance(monkeypatch):
    monkeypatch.setattr('secrets.randbelow', lambda n: 7)
    choices = iter(['0007xmx', 'xmx0007'])
    monkeypatch.setattr('secrets.choice', lambda choices_: next(choices))
    assert random_password('0007xmx') == 'xmx0007'


def test_network_guard_controls():
    with socket.socket() as connection, pytest.raises(AssertionError, match='external network'):
        connection.connect(('203.0.113.1', 443))
    with pytest.raises(AssertionError, match='external DNS'):
        socket.getaddrinfo('user.pandadastudio.com', 443)


async def test_selection_snapshot_changes_require_new_confirmation(changing):
    host = changing
    task = await host.select()
    assert (await host.ninja_api('PATCH', 'accounts/' + host.account['id'], {'login': '16600000001'}))[0] == 200
    assert (await host.action(task))[0] == 422
    assert not host.center.requests


async def test_actual_local_transaction_failure_rolls_back_password_and_pause_together(changing, monkeypatch):
    host = changing
    real = host.tasks._save

    def fail_complete(conn, bot, task):
        real(conn, bot, task)
        if task['state'] == 'completed':
            raise sqlite3.OperationalError('fake-secret-write-failure')

    monkeypatch.setattr(host.tasks, '_save', fail_complete)
    task = await host.select()
    await host.action(task)
    await host.finished()
    assert (await task_info(host, task))['remote'] == 'reported_success'
    assert (await task_info(host, task))['local'] == 'pending'
    assert (await host.tasks.store.get('bot-a', 'accounts', host.account['id']))['password'] == OLD
    assert host.tasks.is_paused('bot-a', host.account['id'])
    monkeypatch.setattr(host.tasks, '_save', real)
    assert (await host.action(task, 'recover'))[1]['data']['local'] == 'committed'
    assert host.center.changes == 1


async def test_completed_change_during_last_await_cannot_send_old_credentials(changing):
    host = changing
    task = await host.select()
    acl = host.access
    real = type(acl).evaluate
    once = False

    async def evaluate(self, *args, **kwargs):
        nonlocal once
        result = await real(self, *args, **kwargs)
        if len(args) >= 6 and args[1] == 'ninja3' and args[5] and not once:
            once = True
            await host.action(task)
            await host.finished()
        return result

    type(acl).evaluate = evaluate
    try:
        assert await host.say('账号 账号A', user='MEMBERA') == ['账号或授权刚刚变化，请重新发送查询']
    finally:
        type(acl).evaluate = real
    assert not host.tasks.is_paused('bot-a', host.account['id']) and host.center.changes == 1


async def test_force_exit_after_durable_submit_marker_is_unknown_on_real_reopen(changing):
    """A child exits without cleanup inside fake HTTP dispatch; restart must never POST again."""
    host = changing
    task = await host.select()
    code = r"""
import asyncio,base64,functools,os,socket,sys
import httpx
from plugins.ninja3.password_tasks import PasswordTasks,IntentCipher
from plugins.ninja3.password_protocol import change_password
from plugins.ninja3.storage import ExtensionStore

def blocked(*args,**kwargs): raise AssertionError('no network in child')
for name in ('connect','connect_ex'): setattr(socket.socket,name,blocked)
socket.getaddrinfo=blocked
async def notify(*args): return 'unavailable'
async def handle(request):
    path=request.url.path
    if path=='/env.js':
        key=base64.b64encode(bytes(range(32))).decode()
        config=dict(appId='fake-app',baseUrl='/api/web',backend='user.pandadastudio.com',signSecret=key,encryptoSecret=key)
        return httpx.Response(200,text='\n'.join(f'{k}: "{v}",' for k,v in config.items()))
    if path.endswith('/login/pwd'): return httpx.Response(200,json={'code':100})
    if path.endswith('/userinfo'): return httpx.Response(200,json={'code':100,'data':{'phone':'16600000000','area':'86'}})
    if path.endswith('/change_pwd'): os._exit(75)
    raise AssertionError('unexpected path')
async def run():
    store=ExtensionStore(sys.argv[1])
    tasks=PasswordTasks(store,cipher=IntentCipher(sys.argv[2]),notify=notify,protocol=functools.partial(change_password,transport=httpx.MockTransport(handle)))
    await tasks.initialize()
    await tasks.confirm('bot-a',sys.argv[3],'backend')
    await asyncio.gather(*list(tasks.workers.values()))
asyncio.run(run())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-c', code, host.tasks.store.path, str(host.key_file), task['id'], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
    assert process.returncode == 75, (stdout, stderr)
    raw = await raw_task(host, task)
    assert raw['state'] == 'submitting' and raw['remote'] == 'unknown'
    await host.plugins.reload('ninja3')
    restarted = host.ninja().password_tasks
    result = await restarted.get('bot-a', task['id'])
    assert result['state'] == 'paused' and result['remote'] == 'unknown'
    assert restarted.is_paused('bot-a', host.account['id'])
    assert not restarted.workers and not host.center.requests
    assert (await restarted.store.atomic(lambda conn: restarted._read(conn, 'bot-a', task['id'])))['intent'] == raw['intent']


@pytest.mark.parametrize('problem', ['missing', 'permissions', 'symlink', 'invalid', 'wrong-key'])
def test_key_failures_are_safe_and_do_not_expose_secrets(tmp_path, problem):
    path = tmp_path / 'fake-key'
    key = Fernet.generate_key()
    path.write_bytes(key)
    path.chmod(0o600)
    good = IntentCipher(str(path))
    encrypted = good.seal({'test': 'fake-sensitive'})
    if problem == 'missing':
        path.unlink()
    elif problem == 'permissions':
        path.chmod(0o644)
    elif problem == 'symlink':
        link = tmp_path / 'fake-link'
        link.symlink_to(path)
        path = link
    elif problem == 'invalid':
        path.write_bytes(b'invalid fake key')
    else:
        path.write_bytes(Fernet.generate_key())
    broken = IntentCipher(str(path))
    with pytest.raises(ValueError) as caught:
        broken.open(encrypted)
    assert 'fake-sensitive' not in repr(caught.value) + str(caught.value) + repr(broken)
    assert key.decode() not in repr(caught.value) + repr(broken)


async def test_success_notice_failure_is_truthful_without_undoing_completed_commit(changing):
    host = changing
    host.sender.push_result = (False, {'code': 'fake-push-failure'})
    task = await host.select()
    await host.action(task)
    await host.finished()
    result = await task_info(host, task)
    assert (result['remote'], result['local'], result['notification']) == ('reported_success', 'committed', 'failed')
    assert not host.tasks.is_paused('bot-a', host.account['id'])
    host.sender.push_result = (True, {'id': 'fake-delivered'})
    assert (await host.action(task, 'recover'))[1]['data']['notification'] == 'sent'
    assert host.center.changes == 1


async def test_cancel_during_submit_marker_write_never_reaches_http(changing):
    host = changing
    real = host.tasks._before_submit
    entered = asyncio.Event()

    async def before(*args):
        await real(*args)
        entered.set()
        await asyncio.Event().wait()

    host.tasks._before_submit = before
    task = await host.select()
    await host.action(task)
    await entered.wait()
    await host.action(task, 'cancel')
    await host.finished()
    assert (await task_info(host, task))['remote'] == 'not_submitted'
    assert host.tasks.is_paused('bot-a', host.account['id']) and host.center.changes == 0


async def test_task_and_cipher_errors_never_leak_to_http_logs_or_private_receipts(changing, monkeypatch, caplog, capsys):
    host = changing
    host.center.block_path = CHANGE
    task = await host.select()
    await host.action(task)
    await host.center.entered.wait()
    raw = await raw_task(host, task)
    new = host.tasks._intent('bot-a', raw)['new_password']
    host.center.change_result = httpx.ReadError(' '.join([OLD, new, 'fake-cookie', 'fake-token']))
    host.center.release.set()
    await host.finished()
    results = str(await host.ninja_api('GET', 'password-tasks')) + str(await task_info(host, task))
    captured = capsys.readouterr()
    visible = results + caplog.text + captured.out + captured.err + str(host.sender.messages)
    for value in (OLD, new, 'fake-cookie', 'fake-token', raw['intent']):
        assert value not in visible


async def test_read_failure_final_gate_fails_closed(changing, monkeypatch):
    host = changing
    account = await host.tasks.store.get('bot-a', 'accounts', host.account['id'])
    assert host.tasks.query_current('bot-a', [account])
    monkeypatch.setattr(host.tasks.store, 'path', '/does-not-exist/ninja3.db')
    assert host.tasks.is_paused('bot-a', host.account['id'])
    assert not host.tasks.query_current('bot-a', [account])


async def test_rejected_retry_reuses_same_intent_and_release_restores_unchanged_account(changing):
    host = changing
    host.center.change_result = httpx.Response(200, json={'code': 1300})
    task = await host.select()
    await host.action(task)
    await host.finished()
    raw = await raw_task(host, task)
    new = host.tasks._intent('bot-a', raw)['new_password']
    assert raw['remote'] == 'rejected' and host.ninja().account_paused('bot-a', host.account['id'])
    # Retry is bound to the selecting actor and resubmits the same sealed intent.
    host.center.change_result = httpx.Response(200, json={'code': 100})
    assert await host.say('忍3重试 ' + task['id'], user='owner-a') == ['请由选择账号的主人重试']
    assert (await host.action(task, 'retry', bot='bot-b'))[0] != 200 and host.center.changes == 1
    status, retried = await host.action(task, 'retry')
    assert status == 200 and (retried['data']['state'], retried['data']['remote']) == ('prepared', 'not_submitted')
    await host.finished()
    after = await raw_task(host, task)
    assert after['intent'] == raw['intent'] and (after['remote'], after['local'], after['state']) == ('reported_success', 'committed', 'completed')
    assert host.center.changes == 2 and (await host.tasks.store.get('bot-a', 'accounts', host.account['id']))['password'] == new
    assert (await host.action(task, 'retry'))[0] == 422 and host.center.changes == 2
    # Release: a rejected intent never changed the remote password, so the old one stays valid.
    other = await host.setup(name='账号B', login=LOGIN, password=OLD)
    host.center.change_result = httpx.Response(200, json={'code': 1300})
    second = await host.select(other['id'])
    await host.action(second)
    await host.finished()
    assert '账号B' not in str(await host.say('账号列表', user='MEMBERA'))
    status, released = await host.action(second, 'release')
    assert status == 200 and released['data']['state'] == 'released'
    assert not host.ninja().account_paused('bot-a', other['id'])
    assert OLD in (await host.say('账号 账号B', user='MEMBERA'))[0]
    assert (await host.action(second, 'retry'))[0] == 422 and host.center.changes == 3


async def test_unknown_result_cannot_be_retried_or_released(changing):
    host = changing
    host.center.change_result = httpx.ReadTimeout('fake')
    task = await host.select()
    await host.action(task)
    await host.finished()
    for action in ('retry', 'release'):
        status, response = await host.action(task, action)
        assert status == 422 and '未知' in response['error']
    assert '未知' in (await host.say('忍3放弃 ' + task['id'], user='owner-a'))[0]
    assert host.ninja().account_paused('bot-a', host.account['id']) and host.center.changes == 1


async def test_group_reply_lists_no_account_names_but_private_does(changing):
    host = changing
    group = (await host.say('忍3改密', user='owner-a'))[0]
    assert '账号A' not in group and '私聊' in group
    host.sender.messages.clear()
    await host.event('忍3改密', user='owner-a')
    assert any('账号A' in str(m) for m in host.sender.messages)


async def test_group_delete_during_task_and_stale_credential_write_never_reverts_password(changing):
    host = changing
    host.center.block_path = CHANGE
    task = await host.select()
    await host.action(task)
    await host.center.entered.wait()
    assert (await host.ninja_api('DELETE', 'groups/group-a'))[0] == 200
    assert (await host.tasks.store.get('bot-a', 'accounts', host.account['id']))['open_groups'] == []
    host.center.release.set()
    await host.finished()
    store = host.tasks.store
    committed = await store.get('bot-a', 'accounts', host.account['id'])
    assert (await task_info(host, task))['local'] == 'committed' and committed['password'] != OLD
    # A read-modify-write that read the pre-commit account cannot write the old password back.
    stale = {**committed, 'password': OLD}
    with pytest.raises(RuntimeError, match='刚刚变化'):
        await store.put('bot-a', 'accounts', host.account['id'], {**stale, 'description': 'x'}, base=stale)
    assert (await store.get('bot-a', 'accounts', host.account['id']))['password'] == committed['password']


async def test_transient_result_write_failure_keeps_decisive_success(changing, monkeypatch):
    host = changing
    # The plugin runs from its loaded copy; patch that module, not the test-time import.
    monkeypatch.setattr(sys.modules[type(host.tasks).__module__], 'RESULT_WRITE_RETRIES', (0, 0, 0))
    real = host.tasks._mutate
    failures = 2

    async def flaky(bot, key, **changes):
        nonlocal failures
        if changes.get('remote') == 'reported_success' and failures:
            failures -= 1
            raise sqlite3.OperationalError('database is locked')
        return await real(bot, key, **changes)

    monkeypatch.setattr(host.tasks, '_mutate', flaky)
    task = await host.select()
    await host.action(task)
    await host.finished()
    result = await task_info(host, task)
    assert failures == 0 and (result['remote'], result['local'], result['state']) == ('reported_success', 'committed', 'completed')
    assert host.center.changes == 1


OTHER_PROCESS = r"""
import asyncio,json,socket,sys
from plugins.ninja3.password_tasks import IntentCipher,PasswordTasks
from plugins.ninja3.storage import ExtensionStore
def blocked(*args,**kwargs): raise AssertionError('no network in child')
for name in ('connect','connect_ex'): setattr(socket.socket,name,blocked)
socket.getaddrinfo=blocked
async def notify(*args): return 'unavailable'
async def forbidden(*args,**kwargs): raise AssertionError('child must not run the protocol')
async def run():
    store=ExtensionStore(sys.argv[1])
    tasks=PasswordTasks(store,cipher=IntentCipher(sys.argv[2]),notify=notify,protocol=forbidden)
    tasks._start=lambda *args: None  # leave confirmed/retried intents for the parent to start
    await tasks.initialize()
    out=[]
    for step in json.loads(sys.argv[3]):
        name,*args=step
        out.append(await getattr(tasks,name)('bot-a',*args))
    print(json.dumps(out))
    await tasks.shutdown()
    store.close()
asyncio.run(run())
"""


async def other_process(host, *steps):
    """A second real process on the same DB: initialize, then legitimate task operations."""
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        '-c',
        OTHER_PROCESS,
        host.tasks.store.path,
        str(host.key_file),
        json.dumps(steps),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(child.communicate(), 20)
    assert child.returncode == 0, err.decode()
    return json.loads(out)


async def test_late_start_after_other_process_release_never_submits(changing, monkeypatch):
    host = changing
    task = await host.select()
    delayed = []
    start = host.tasks._start
    monkeypatch.setattr(host.tasks, '_start', lambda bot, key: delayed.append((bot, key)))
    assert (await host.action(task))[0] == 200 and delayed
    (released,) = await other_process(host, ['release', task['id']])
    assert released['state'] == 'released' and not host.tasks.is_paused('bot-a', host.account['id'])
    for bot, key in delayed:
        start(bot, key)
    await host.finished()
    raw = await raw_task(host, task)
    assert (raw['state'], raw['active'], raw.get('runner')) == ('released', False, None)
    assert not host.center.requests  # never even logged in
    assert (await host.tasks.store.get('bot-a', 'accounts', host.account['id']))['password'] == OLD
    assert OLD in str(await host.say('账号 账号A', user='MEMBERA'))  # released: the old password is valid


async def test_late_start_cannot_take_account_back_from_new_task(changing, monkeypatch):
    host = changing
    old = await host.select()
    delayed = []
    start = host.tasks._start
    monkeypatch.setattr(host.tasks, '_start', lambda bot, key: delayed.append((bot, key)))
    await host.action(old)
    _, new, confirmed = await _release_then_new(host, old)
    assert new['id'] != old['id'] and confirmed['state'] == 'prepared'
    for bot, key in delayed:
        start(bot, key)
    await host.finished()
    assert (await raw_task(host, old))['state'] == 'released' and not host.center.requests
    assert (await raw_task(host, new))['state'] == 'prepared'
    assert host.tasks.is_paused('bot-a', host.account['id'])  # the new intent's pause is untouched
    start('bot-a', new['id'])
    await host.finished()
    raw = await raw_task(host, new)
    assert (raw['state'], host.center.changes) == ('completed', 1)
    account = await host.tasks.store.get('bot-a', 'accounts', host.account['id'])
    assert account['password'] == host.tasks._intent('bot-a', raw)['new_password']


async def _release_then_new(host, old):
    released, new = await other_process(host, ['release', old['id']], ['select', host.account['id'], 'backend'])
    (confirmed,) = await other_process(host, ['confirm', new['id'], 'backend'])
    return released, new, confirmed


@pytest.mark.parametrize('operation', ['release', 'retry'])
async def test_superseded_worker_mid_flight_cannot_submit_or_overwrite(changing, operation):
    host = changing
    host.center.block_path = '/api/web/user_center/userinfo'
    task = await host.select()
    await host.action(task)
    await host.center.entered.wait()  # this worker owns the claim and is preparing
    args = [task['id'], 'backend'] if operation == 'retry' else [task['id']]
    (result,) = await other_process(host, [operation, *args])  # initialize turned it paused first
    host.center.release.set()
    await host.finished()
    raw = await raw_task(host, task)
    assert host.center.changes == 0
    if operation == 'release':
        assert (result['state'], raw['state'], raw['active']) == ('released', 'released', False)
        assert not host.tasks.is_paused('bot-a', host.account['id'])
        assert (await host.tasks.store.get('bot-a', 'accounts', host.account['id']))['password'] == OLD
        return
    # The stale result did not overwrite the retried attempt; a fresh worker submits exactly once.
    assert (raw['state'], raw['active'], raw.get('runner')) == ('prepared', True, None)
    host.center.block_path = None
    host.tasks._start('bot-a', task['id'])
    await host.finished()
    assert ((await raw_task(host, task))['state'], host.center.changes) == ('completed', 1)


async def test_duplicate_start_of_one_claim_submits_once(changing):
    host = changing
    host.center.block_path = CHANGE
    task = await host.select()
    await host.action(task)
    first = host.tasks.workers[('bot-a', task['id'])]
    host.tasks._start('bot-a', task['id'])
    second = host.tasks.workers[('bot-a', task['id'])]
    await host.center.entered.wait()
    host.center.release.set()
    await asyncio.gather(first, second)
    assert ((await task_info(host, task))['state'], host.center.changes) == ('completed', 1)


async def test_decided_success_held_through_real_write_lock_then_local_only(changing):
    """Product retry timings (about 20 s) against a real second SQLite writer."""
    host = changing
    await host.tasks.store.atomic(lambda conn: conn.execute('PRAGMA busy_timeout=0').fetchall())
    blocker = sqlite3.connect(host.tasks.store.path)
    original = host.center.handle

    async def handle(request):
        result = await original(request)
        if request.url.path == CHANGE:
            blocker.execute('BEGIN IMMEDIATE')
        return result

    host.tasks.protocol = functools.partial(change_password, transport=httpx.MockTransport(handle))
    task = await host.select()
    await host.action(task)
    try:
        await host.finished()
        assert ('bot-a', task['id']) in host.tasks.held
        # Every store transaction is IMMEDIATE, so even reads are refused while the writer holds the lock.
        assert (await host.ninja_api('GET', 'password-tasks/' + task['id']))[0] == 503
        status, response = await host.action(task, 'recover')
        assert status == 422 and '保留在本进程' in response['error']
        for action in ('retry', 'release'):
            assert (await host.action(task, action))[0] != 200
        assert host.tasks.is_paused('bot-a', host.account['id'])
    finally:
        blocker.rollback()
        blocker.close()
    status, response = await host.action(task, 'recover')
    assert status == 200 and (response['data']['remote'], response['data']['local']) == ('reported_success', 'committed')
    assert not host.tasks.held and not host.tasks.is_paused('bot-a', host.account['id'])
    raw = await raw_task(host, task)
    account = await host.tasks.store.get('bot-a', 'accounts', host.account['id'])
    assert account['password'] == host.tasks._intent('bot-a', raw)['new_password'] and host.center.changes == 1


@pytest.mark.parametrize('ending', ['background', 'rejected', 'exit'])
async def test_held_result_background_flush_or_process_exit(changing, monkeypatch, ending):
    host = changing
    module = sys.modules[type(host.tasks).__module__]
    monkeypatch.setattr(module, 'RESULT_WRITE_RETRIES', (0,))
    monkeypatch.setattr(module, 'HELD_RESULT_RETRIES', (0.05, 0.05, 0.05))
    real = host.tasks._mutate
    locked = True

    async def flaky(bot, key, **changes):
        if locked and changes.get('runner'):
            raise sqlite3.OperationalError('database is locked')
        return await real(bot, key, **changes)

    monkeypatch.setattr(host.tasks, '_mutate', flaky)
    if ending == 'rejected':
        host.center.change_result = httpx.Response(200, json={'code': 1300})
    task = await host.select()
    await host.action(task)
    await host.finished()
    assert ('bot-a', task['id']) in host.tasks.held
    info = await task_info(host, task)
    assert (info['state'], info['remote'], info['error']) == ('submitting', 'unknown', 'result_not_persisted')
    if ending == 'background':
        locked = False
        await asyncio.wait_for(asyncio.gather(*host.tasks.flushers.values()), 5)
        assert ((await task_info(host, task))['local'], host.center.changes) == ('committed', 1)
        assert not host.tasks.held and not host.tasks.is_paused('bot-a', host.account['id'])
        return
    if ending == 'rejected':
        # Explicit recover and the background flush race; the held rejection is written and notified once.
        locked = False
        sent = len(host.sender.messages)
        status, response = await host.action(task, 'recover')
        await asyncio.gather(*host.tasks.flushers.values())
        assert status == 200 and response['data']['remote'] == (await task_info(host, task))['remote'] == 'rejected'
        assert len(host.sender.messages) - sent == 1 and host.tasks.is_paused('bot-a', host.account['id'])
        assert (await host.action(task, 'release'))[0] == 200 and host.center.changes == 1
        assert (await host.tasks.store.get('bot-a', 'accounts', host.account['id']))['password'] == OLD
        return
    # Process exit loses only the result the store never accepted: restart is conservative unknown.
    await host.plugins.reload('ninja3')
    restarted = host.ninja().password_tasks
    result = await restarted.get('bot-a', task['id'])
    assert (result['state'], result['remote']) == ('paused', 'unknown')
    assert restarted.is_paused('bot-a', host.account['id']) and host.center.changes == 1
    assert (await restarted.store.get('bot-a', 'accounts', host.account['id']))['password'] == OLD


async def test_settling_held_result_blocks_retry_release_and_recover(changing, monkeypatch):
    host = changing
    module = sys.modules[type(host.tasks).__module__]
    monkeypatch.setattr(module, 'RESULT_WRITE_RETRIES', (0,))
    monkeypatch.setattr(module, 'HELD_RESULT_RETRIES', (3600,))
    real = host.tasks._mutate
    locked = True

    async def flaky(bot, key, **changes):
        if locked and changes.get('runner'):
            raise sqlite3.OperationalError('database is locked')
        return await real(bot, key, **changes)

    monkeypatch.setattr(host.tasks, '_mutate', flaky)
    host.center.change_result = httpx.Response(200, json={'code': 1300})
    task = await host.select()
    await host.action(task)
    await host.finished()
    settle, entered, proceed = host.tasks._settle, asyncio.Event(), asyncio.Event()

    async def slow_settle(*args):
        entered.set()
        await proceed.wait()
        await settle(*args)

    monkeypatch.setattr(host.tasks, '_settle', slow_settle)
    locked = False
    flushing = asyncio.create_task(host.action(task, 'recover'))
    await entered.wait()
    # The rejection is written but its receipt is pending: no new attempt or second settle may start.
    assert (await raw_task(host, task))['remote'] == 'rejected'
    for action in ('retry', 'release', 'recover'):
        assert (await host.action(task, action))[0] == 422
    proceed.set()
    assert (await flushing)[0] == 200
    assert (await host.action(task, 'retry'))[0] == 200
    await host.finished()
    assert host.center.changes == 2
