"""忍者必须死3 final send gate: real MessageSender, before_send pipeline, TokenManager and _request; fake external HTTP client only."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from account_host import account_host as account_host  # noqa: F401
from socket_platform import until
from test_access_control import access_host as access_host  # noqa: F401
from test_ninja3 import SECRET, grant, grants
from test_ninja3 import ninja as ninja  # noqa: F401

CHANGED = '账号或授权刚刚变化，请重新发送查询'
CHANGES = ['control', 'password', 'disable', 'close_group', 'narrow_acl', 'revoke', 'deny', 'leave', 'reload_access', 'reload_plugin']


@pytest.fixture
async def wire(ninja, monkeypatch):
    """Real Sender for bot-a; ``marker`` parks the first matching send inside an earlier before_send hook.

    ``result`` is ``(ok, data)`` or a callable of the payload returning it; ``code -1`` raises in the client.
    """
    from core.message.sender import MessageSender
    from core.network.access import TokenManager

    host = ninja
    sent = []
    wire = SimpleNamespace(sent=sent, entered=asyncio.Event(), release=asyncio.Event(), marker=None, result=(True, {'id': 'fake-message'}))

    class Client:
        is_closed = False

        async def post(self, url, **kwargs):
            return SimpleNamespace(status_code=200, json=lambda: {'access_token': 'fake-token', 'expires_in': 7200})

        async def request(self, method, url, **kwargs):
            payload = kwargs.get('json')
            sent.append(payload)
            ok, data = wire.result(payload) if callable(wire.result) else wire.result
            if ok:
                return SimpleNamespace(status_code=200, content=json.dumps(data).encode())
            if str(data.get('code')) == '-1':
                raise RuntimeError(data.get('message', 'fake'))
            return SimpleNamespace(status_code=400, content=json.dumps(data).encode())

        async def aclose(self):
            self.is_closed = True

    tokens = TokenManager('bot-a', 'fake-local-secret')
    tokens._client = Client()
    tokens._token, tokens._expires_at = 'fake-token', time.time() + 7200
    wire.tokens = tokens

    async def preparation(packet):
        # Stands for any asynchronous preparation (another hook, an upload) after the plugin's own checks.
        if wire.marker and wire.marker in str(packet['payload']):
            wire.marker = None
            wire.entered.set()
            await wire.release.wait()
        return packet

    host.application.get_bot('bot-a').sender = MessageSender(tokens)
    host.application.hook_manager.register('before_send', preparation, owner='test-preparation')
    wire.texts = lambda: [str(p) for p in sent]
    yield wire
    wire.release.set()
    host.application.hook_manager.unregister_owner('test-preparation')
    await tokens.close()


async def change_during(host, wire, change, account, send):
    task = asyncio.create_task(send())
    try:
        await asyncio.wait_for(wire.entered.wait(), 5)
        acl = host.access
        if change == 'password':
            await host.ninja_api('PATCH', 'accounts/' + account['id'], {'password': 'fake-new'})
        elif change == 'disable':
            await host.ninja_api('PATCH', 'accounts/' + account['id'], {'enabled': False})
        elif change == 'close_group':
            await host.ninja_api('POST', 'groups', {'group': 'group-a', 'enabled': False})
        elif change == 'narrow_acl':
            configured = (await acl.access_snapshot('bot-a', 'ninja3'))['functions']['query']
            await acl.manage_access('bot-a', 'ninja3', 'policy', function='query', policy={**configured, 'groups': []})
        elif change in {'revoke', 'deny'}:
            await acl.manage_access('bot-a', 'ninja3', change, function='query', group='group-a', member='MEMBERA')
        elif change == 'leave':
            host.lifecycle('GROUP_MEMBER_REMOVE', member='MEMBERA', sequence=2, timestamp=1000)
            await until(lambda: not grants(host.access.scope_states[('bot-a', 'ninja3')]))
        elif change == 'reload_access':
            await host.modules.disable('access_control', _persist=False)
            await host.modules.enable('access_control', _skip_deps=True, _persist=False)
        elif change == 'reload_plugin':
            await host.plugins.reload('ninja3')
    finally:
        wire.release.set()
    await task


@pytest.mark.parametrize('change', CHANGES)
async def test_text_query_rechecks_after_sender_preparation(ninja, wire, change):
    host = ninja
    account = await host.setup()
    await grant(host)
    wire.sent.clear()
    wire.marker = '名称：账号A'
    await change_during(host, wire, change, account, lambda: host.say('账号 账号A', user='MEMBERA'))
    if change == 'control':
        assert len(wire.sent) == 3 and SECRET in wire.texts()[-1]
    else:
        # Nothing stale reached the transport; one notice without credentials instead.
        assert len(wire.sent) == 1 and CHANGED in wire.texts()[0], wire.texts()
        assert SECRET not in str(wire.sent) and 'fake-login' not in str(wire.sent) and 'fake-new' not in str(wire.sent)


@pytest.mark.parametrize(
    ('marker', 'sent_before_block'),
    [('名称：账号A', 0), ('fake-login', 1), (SECRET, 2)],
)
async def test_each_credential_message_has_its_own_final_gate(ninja, wire, marker, sent_before_block):
    host = ninja
    await host.setup()
    await grant(host)
    wire.sent.clear()
    wire.marker = marker
    task = asyncio.create_task(host.say('账号 账号A', user='MEMBERA'))
    await asyncio.wait_for(wire.entered.wait(), 5)
    await host.ninja_api('PATCH', 'accounts/' + (await host.ninja_api())[1]['data'][0]['id'], {'enabled': False})
    wire.release.set()
    await task
    # The page's group name lookup is a GET without payload (#142); only messages count here.
    contents = [payload.get('content', payload.get('markdown', {}).get('content')) for payload in wire.sent if payload is not None]
    assert contents == ['名称：账号A', 'fake-login', SECRET][:sent_before_block] + [CHANGED]


@pytest.mark.parametrize('stop_at', [1, 2, 3])
@pytest.mark.parametrize('code', [400, 500, -1])
async def test_credential_send_failure_stops_without_sending_later_parts(ninja, wire, stop_at, code):
    host = ninja
    await host.setup()
    await grant(host)
    wire.sent.clear()

    def result(payload):
        if len(wire.sent) == stop_at:
            return False, {'code': code, 'message': 'fake credential failure'}
        return True, {'id': 'fake-message'}

    wire.result = result
    await host.say('账号 账号A', user='MEMBERA')
    assert [payload['content'] for payload in wire.sent] == ['名称：账号A', 'fake-login', SECRET][:stop_at]


@pytest.mark.parametrize('change', CHANGES)
async def test_directory_rechecks_after_sender_preparation(ninja, wire, change):
    host = ninja
    account = await host.setup()
    await grant(host)
    wire.sent.clear()
    wire.marker = '1. 账号A'
    await change_during(host, wire, change, account, lambda: host.say('账号列表', user='MEMBERA'))
    assert len(wire.sent) == 1
    if change == 'control':
        assert '1. 账号A' in wire.texts()[0]
    else:
        assert CHANGED in wire.texts()[0] and '账号A' not in wire.texts()[0], wire.texts()


async def test_latest_before_send_callback_cannot_outrun_check(ninja, wire):
    host = ninja
    await host.setup()
    await grant(host)
    wire.sent.clear()
    entered, release, hold = asyncio.Event(), asyncio.Event(), []

    async def later(packet):
        if hold and SECRET in str(packet.get('payload')):
            entered.set()
            await release.wait()
        return {'payload': packet['payload']}  # also drops event and content

    # The check is a Sender argument, not a hook: a callback ordered last does not displace it.
    host.application.hook_manager.register('before_send', later, owner='test-later', priority=float('inf'))
    try:
        await host.say('账号 账号A', user='MEMBERA')
        assert SECRET in wire.texts()[-1]
        hold.append(1)
        task = asyncio.create_task(host.say('账号 账号A', user='MEMBERA'))
        await asyncio.wait_for(entered.wait(), 5)
        await host.access.manage_access('bot-a', 'ninja3', 'revoke', function='query', group='group-a', member='MEMBERA')
        release.set()
        await task
    finally:
        release.set()
        host.application.hook_manager.unregister_owner('test-later')
    assert len(wire.sent) == 6 and CHANGED in wire.texts()[-1] and SECRET not in wire.texts()[-1]


async def test_failing_check_blocks_and_unknown_result_is_not_resent(ninja, wire, monkeypatch):
    host = ninja
    await host.setup()
    await grant(host)
    service = type(host.ninja())
    wire.sent.clear()
    real = service.unchanged
    calls = []

    def flaky(self, bot, revision):
        calls.append(1)
        if len(calls) == 2:  # the before_send call, after the plugin's own check passed
            raise RuntimeError('check failed')
        return real(self, bot, revision)

    monkeypatch.setattr(service, 'unchanged', flaky)
    await host.say('账号 账号A', user='MEMBERA')
    assert len(calls) == 2 and len(wire.sent) == 1 and CHANGED in wire.texts()[0] and SECRET not in str(wire.sent)
    monkeypatch.setattr(service, 'unchanged', real)
    # Transport outcome unknown (framework network code -1): attempted once, never repeated.
    wire.sent.clear()
    wire.result = (False, {'code': -1, 'message': 'fake network'})
    await host.say('账号 账号A', user='MEMBERA')
    assert len(wire.sent) == 1 and '名称：账号A' in wire.texts()[0] and SECRET not in wire.texts()[0]


async def test_directory_and_query_read_credentials_only_for_selected_account(ninja, wire, monkeypatch):
    host = ninja
    selected = await host.setup()
    other = await host.setup(name='账号B', password='fake-other')
    await grant(host)
    store = host.ninja().store
    real = store._run
    rows = []

    def observe(sql, params, read):
        result = real(sql, params, read)
        if read and 'accounts' in params:
            rows.extend(result)
        return result

    monkeypatch.setattr(store, '_run', observe)
    wire.sent.clear()
    await host.say('账号列表', user='MEMBERA')
    assert rows and all(set(r) == {'id', 'name', 'enabled', 'open_groups'} for r in rows)
    assert '1. 账号A' in wire.texts()[0] and '2. 账号B' in wire.texts()[0] and SECRET not in str(wire.sent) and 'fake-login' not in str(wire.sent)
    rows.clear()
    await host.say('账号 账号A', user='MEMBERA')
    full = [r for r in rows if 'password' in r]
    assert [r['id'] for r in full] == [selected['id']] and other['id'] in {r['id'] for r in rows}
    assert SECRET in wire.texts()[-1] and 'fake-other' not in str(wire.sent)
    # A caller without a grant never causes a credential read.
    rows.clear()
    await host.say('账号 账号A', user='MEMBERB')
    assert not [r for r in rows if 'password' in r]


async def test_copyable_credentials_have_no_markdown_suffix_and_use_three_replies(ninja, wire):
    from core.base.config import cfg

    host = ninja
    await host.setup()
    await grant(host)
    cfg.set_bot_setting('bot-a', 'message.markdown_suffix', 'fake-suffix')
    wire.sent.clear()
    await host.say('账号 账号A', user='MEMBERA')
    assert [payload.get('content') for payload in wire.sent] == ['名称：账号A', 'fake-login', SECRET]
    assert all(payload['msg_type'] == 0 for payload in wire.sent)
    assert len({payload['msg_id'] for payload in wire.sent}) == 1
