"""#140 review regressions through real event, Sender and durable task boundaries."""

import asyncio
import functools
import importlib
from types import SimpleNamespace

import httpx
import pytest
from account_host import account_host as account_host  # noqa: F401
from test_access_control import access_host as access_host  # noqa: F401
from test_ninja3 import SECRET, grant
from test_ninja3 import ninja as ninja  # noqa: F401
from test_ninja3_emergency import wait_emergency
from test_ninja3_password_tasks import CHANGE
from test_ninja3_password_tasks import changing as changing  # noqa: F401
from test_ninja3_password_tasks import forbid_game_network as forbid_game_network  # noqa: F401
from test_ninja3_send_gate import wire as wire  # noqa: F401

from core.base.config import cfg
from plugins.ninja3.password_protocol import change_password


async def allow_owner(host):
    await host.state.confirm_member('bot-a', 'group-a', 'owner-a', evidence='test native owner query')
    await host.access.manage_access(
        'bot-a', 'ninja3', 'grant', function='query', group='group-a', member='owner-a',
        mode='continuous', resource_scope={'kind': 'all'}, expires_at=None,
    )


@pytest.mark.parametrize('union_mode', [False, True])
@pytest.mark.parametrize(('native_id', 'union_id', 'owner'), [('owner-a', 'union-owner-a', True), ('MEMBERA', 'owner-a', False)])
async def test_pause_detail_uses_native_owner_identity(changing, union_mode, native_id, union_id, owner):
    host = changing
    await allow_owner(host)
    task = await host.select()
    host.center.block_path = CHANGE
    await host.action(task)
    await asyncio.wait_for(host.center.entered.wait(), 5)
    cfg.set_bot_setting('bot-a', 'identity.use_union_id_for_group', union_mode)
    expected = f'该账号正在改密（批量任务 {task["id"]}），暂不可查询' if owner else '该账号暂不可查询，请稍后再试'
    assert await host.say('账号 账号A', user=native_id, union_id=union_id) == [expected]
    host.center.release.set()
    await host.finished()


@pytest.mark.parametrize('union_mode', [False, True])
@pytest.mark.parametrize(('native_id', 'union_id', 'owner'), [('owner-a', 'union-owner-a', True), ('MEMBERA', 'owner-a', False)])
@pytest.mark.parametrize('command', ['授权', '忍3改密', '忍3批量改密', '菜单', '忍3紧急状态', '忍3'])
async def test_owner_entry_points_use_native_identity(changing, union_mode, native_id, union_id, owner, command):
    host = changing
    cfg.set_bot_setting('bot-a', 'identity.use_union_id_for_group', union_mode)
    reply = await host.say(command, user=native_id, union_id=union_id, mentions=['MEMBERB'] if command == '授权' else [])
    if command == '授权':
        assert reply == ['已授权该成员查询本群开放账号，长期有效'] if owner else reply == ['仅主人可以授权']
    elif command.startswith('忍3') and command.endswith('改密'):
        assert ('尚未选择账号' in str(reply)) == owner
        assert ('仅主人可以操作改密任务' in str(reply)) == (not owner)
    elif command == '菜单':
        labels = [button['text'] for row in host.sender.keyboards[-1] for button in row]
        assert ('紧急模式' in labels) == owner
    elif command == '忍3紧急状态':
        pushes = [message for message in host.sender.messages if message[0] == 'push' and '紧急模式：' in message[2]]
        assert bool(pushes) == owner
    else:
        assert ('忍者必须死3：账号' in str(reply)) == owner


@pytest.mark.parametrize('union_mode', [False, True])
@pytest.mark.parametrize('kind', ['user', 'group'])
async def test_native_owner_keeps_existing_blacklist_checks(changing, union_mode, kind):
    from core.plugin._blacklist import get_blacklist_map, set_blacklist_map

    host = changing
    cfg.set_bot_setting('bot-a', 'identity.use_union_id_for_group', union_mode)
    blocked = 'group-a' if kind == 'group' else 'union-owner-a' if union_mode else 'owner-a'
    previous = get_blacklist_map('bot-a', kind)
    try:
        set_blacklist_map('bot-a', kind, {**previous, blocked: 'test blocked'})
        reply = await host.say('忍3改密', user='owner-a', union_id='union-owner-a')
        assert '尚未选择账号' not in str(reply)
    finally:
        set_blacklist_map('bot-a', kind, previous)


@pytest.mark.parametrize('union_mode', [False, True])
async def test_native_owner_private_registration_keeps_notifications_deliverable(changing, union_mode):
    host = changing
    cfg.set_bot_setting('bot-a', 'identity.use_union_id_for_group', union_mode)
    await host.say('忍3', user='owner-a', union_id='union-owner-a', group='', event_type='C2C_MESSAGE_CREATE')
    host.sender.messages.clear()
    await host.say('忍3紧急状态', user='owner-a', union_id='union-owner-a')
    assert any(message[:2] == ('push', 'owner-a') and '紧急模式：' in message[2] for message in host.sender.messages)


def responses(wire, monkeypatch, kind, stop_at=None):
    """Replace only the external HTTP response; retain Sender's result conversion."""
    def result(_payload):
        if stop_at is not None and len(wire.sent) != stop_at:
            return True, {'id': 'fake-delivered'}
        if kind == 'network':
            return False, {'code': -1, 'message': 'fake unknown result'}
        return True, {
            'body': {}, 'empty': {}, 'success': {'id': 'fake-delivered'},
            'msg_id': {'msg_id': 'fake-delivered'}, 'code_only': {'code': 0}, 'empty_id': {'id': ''},
        }[kind]

    wire.result = result
    original = wire.tokens._client.request

    async def request(*args, **kwargs):
        response = await original(*args, **kwargs)
        if kind == 'body' and (stop_at is None or len(wire.sent) == stop_at):
            response.content = b''
        return response

    monkeypatch.setattr(wire.tokens._client, 'request', request)


@pytest.mark.parametrize('kind', ['body', 'empty', 'success', 'msg_id', 'network', 'code_only', 'empty_id'])
@pytest.mark.parametrize('stop_at', [1, 2])
async def test_credential_parts_require_message_success_evidence(ninja, wire, monkeypatch, kind, stop_at):
    host = ninja
    await host.setup()
    await grant(host)
    wire.sent.clear()
    responses(wire, monkeypatch, kind, stop_at)
    await host.say('账号 账号A', user='MEMBERA')
    expected = ['名称：账号A', 'fake-login', SECRET]
    assert [payload['content'] for payload in wire.sent] == (expected if kind in {'success', 'msg_id'} else expected[:stop_at])


@pytest.mark.parametrize('kind', ['body', 'empty'])
@pytest.mark.parametrize('command', ['菜单', '账号列表'])
async def test_unknown_menu_result_never_falls_back_or_repeats(ninja, wire, monkeypatch, kind, command):
    host = ninja
    await host.setup()
    await grant(host)
    wire.sent.clear()
    responses(wire, monkeypatch, kind)
    await host.say(command, user='MEMBERA')
    assert len(wire.sent) == 1 and 'keyboard' in wire.sent[0]


@pytest.mark.parametrize('kind', ['body', 'empty', 'success', 'network'])
@pytest.mark.parametrize('receipt', ['notice', 'password', 'batch', 'emergency'])
async def test_owner_notifications_require_message_success_evidence(changing, wire, monkeypatch, kind, receipt):
    host = changing
    responses(wire, monkeypatch, kind)
    if receipt == 'notice':
        await host.say('账号列表', user='MEMBERB')
        record = (await host.ninja_api('GET', 'notices'))[1]['data'][0]
        text = str(wire.sent[-1])
        assert ('已通知主人' in text) == (kind == 'success')
    elif receipt == 'password':
        task = await host.select()
        await host.action(task)
        await host.finished()
        record = (await host.ninja_api('GET', 'password-tasks/' + task['id']))[1]['data']
    elif receipt == 'batch':
        batch = (await host.ninja_api('POST', 'password-batches', {'accounts': [host.account['id']]}))[1]['data']
        await host.ninja_api('POST', f'password-batches/{batch["id"]}/confirm', {})
        await asyncio.gather(*list(host.ninja().password_batches.runners.values()))
        record = (await host.ninja_api('GET', 'password-batches/' + batch['id']))[1]['data']
    else:
        await host.ninja().emergency.enter('bot-a')
        await wait_emergency(host)
        record = (await host.ninja_api('GET', 'emergency'))[1]['data']
    assert record['notification'] == ('sent' if kind == 'success' else 'unknown')


async def test_interrupted_linked_batch_yields_to_actual_dispatching_batch(changing, monkeypatch):
    host = changing
    first = host.ninja().password_batches
    entered, release = asyncio.Event(), asyncio.Event()
    original_confirm = host.tasks.confirm

    async def before_claim(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original_confirm(*args, **kwargs)

    monkeypatch.setattr(host.tasks, 'confirm', before_claim)
    with monkeypatch.context() as patch:
        patch.setattr(importlib.import_module(first.__class__.__module__), 'uuid4', lambda: SimpleNamespace(hex='0' * 32))
        old = await first.select('bot-a', 'backend', [host.account['id']])
    await first.confirm('bot-a', old['id'], 'backend')
    await asyncio.wait_for(entered.wait(), 5)
    child = (await first.get('bot-a', old['id']))['items'][0]['task']
    assert (await host.tasks.get('bot-a', child))['state'] == 'selected'

    # Reload while A has linked the child but has not claimed or executed it.
    await host.plugins.reload('ninja3')
    host.tasks = host.ninja().password_tasks
    host.tasks.cipher = type(host.tasks.cipher)(str(host.key_file))
    host.tasks.protocol = functools.partial(change_password, transport=httpx.MockTransport(host.center.handle))
    second = host.ninja().password_batches
    assert (await second.get('bot-a', old['id']))['state'] == 'interrupted'
    with monkeypatch.context() as patch:
        patch.setattr(importlib.import_module(second.__class__.__module__), 'uuid4', lambda: SimpleNamespace(hex='f' * 32))
        new = await second.select('bot-a', 'backend', [host.account['id']])
    host.center.block_path = CHANGE
    await second.confirm('bot-a', new['id'], 'backend')
    await asyncio.wait_for(host.center.entered.wait(), 5)
    assert (await second.get('bot-a', new['id']))['items'][0]['task'] == child
    await allow_owner(host)
    assert await host.say('账号 账号A', user='owner-a') == [f'该账号正在改密（批量任务 {new["id"]}），暂不可查询']
    assert await second.batch_for_task('bot-a', child) == new['id']
    host.center.release.set()
    await asyncio.gather(*list(second.runners.values()))


async def test_two_batches_link_same_child_only_first_marks_dispatching_batch(changing, monkeypatch):
    host = changing
    batches = host.ninja().password_batches
    module = importlib.import_module(batches.__class__.__module__)
    with monkeypatch.context() as patch:
        patch.setattr(module, 'uuid4', lambda: SimpleNamespace(hex='f' * 32))
        first = await batches.select('bot-a', 'backend', [host.account['id']])
    second_account = await host.setup(name='账号B')
    with monkeypatch.context() as patch:
        patch.setattr(module, 'uuid4', lambda: SimpleNamespace(hex='0' * 32))
        second = await batches.select('bot-a', 'backend', [host.account['id'], second_account['id']])

    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    original_confirm = host.tasks.confirm
    calls = []

    async def before_claim(*args, **kwargs):
        index = len(calls)
        calls.append(args[1])
        if index < 2:
            entered[index].set()
            await release[index].wait()
        return await original_confirm(*args, **kwargs)

    monkeypatch.setattr(host.tasks, 'confirm', before_claim)
    host.center.block_path = CHANGE
    await batches.confirm('bot-a', first['id'], 'backend')
    await asyncio.wait_for(entered[0].wait(), 5)
    await batches.confirm('bot-a', second['id'], 'backend')
    await asyncio.wait_for(entered[1].wait(), 5)
    assert calls[0] == calls[1]

    release[0].set()
    await asyncio.wait_for(host.center.entered.wait(), 5)
    release[1].set()
    for _ in range(100):
        current = await batches.store.atomic(lambda conn: batches._read(conn, 'bot-a', second['id']))
        if current['items'][0]['status'] != 'linked':
            break
        await asyncio.sleep(0.01)
    assert current['items'][0]['status'] == 'existing'
    assert await batches.batch_for_task('bot-a', calls[0]) == first['id']
    host.center.release.set()
    await asyncio.gather(*list(batches.runners.values()))
