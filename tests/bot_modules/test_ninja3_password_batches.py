"""#77 batch selection and owner-specified passwords through real QQ/HTTP entries, temporary DB and fake platform HTTP."""

import asyncio
import functools
import json
import logging
import re
import sqlite3
import sys

import httpx
import pytest
from account_host import account_host as account_host  # noqa: F401
from test_access_control import access_host as access_host  # noqa: F401
from test_ninja3 import ninja as ninja  # noqa: F401
from test_ninja3_password_tasks import CHANGE, LOGIN, OLD, task_info
from test_ninja3_password_tasks import changing as changing  # noqa: F401
from test_ninja3_password_tasks import forbid_game_network as forbid_game_network  # noqa: F401

from plugins.ninja3.password_protocol import change_password

SUCCESS = httpx.Response(200, json={'code': 100})
REJECT = httpx.Response(200, json={'code': 1300})


def script(host, *results):
    """Answer successive change requests in order (the batch is sequential)."""
    queue = list(results)
    original = host.center.handle

    async def handle(request):
        if request.url.path == CHANGE and queue:
            host.center.change_result = queue.pop(0)
        return await original(request)

    host.tasks.protocol = functools.partial(change_password, transport=httpx.MockTransport(handle))


async def settle(host):
    batches = host.ninja().password_batches
    for _ in range(50):
        runners = list(batches.runners.values())
        if not runners:
            break
        await asyncio.gather(*runners, return_exceptions=True)
    await host.finished()


async def accounts(host, *names, password=OLD):
    made = [host.account]
    for name in names:
        made.append(await host.setup(name=name, login=LOGIN, password=password))
    return made


async def batch_api(host, method='GET', path='', body=None, **kwargs):
    return await host.ninja_api(method, 'password-batches' + path, body, **kwargs)


async def private(host, text, user='owner-a'):
    await host.event(text, user=user)
    replies = [m[2] for m in host.sender.messages if m[:2] == ('bot-a', user)]
    host.sender.messages[:] = [m for m in host.sender.messages if m[:2] != ('bot-a', user)]
    return replies


def pushes(host):
    return [m[2] for m in host.sender.messages if m[0] == 'push']


async def stored(host, password):
    """Every persisted row (batches, tasks, accounts) checked for a clear-text value."""

    def read(conn):
        rows = conn.execute('SELECT payload FROM ninja3_password_batches').fetchall()
        rows += conn.execute('SELECT payload FROM ninja3_password_tasks').fetchall()
        return [r[0] for r in rows]

    return [row for row in await host.tasks.store.atomic(read) if password in row]


async def test_backend_random_batch_shows_scope_then_runs_each_account_once_with_mixed_results(changing, monkeypatch):
    host = changing
    a, b, c, d = await accounts(host, '账号B', '账号C', '账号D')
    status, response = await batch_api(host, 'POST', body={'accounts': []})
    assert status == 422  # default is nothing selected
    status, response = await batch_api(host, 'POST', body={'accounts': [a['id'], b['id'], c['id'], d['id']]})
    batch = response['data']
    assert status == 200 and batch['state'] == 'selected' and batch['mode'] == 'random'
    assert [i['name'] for i in batch['items']] == ['账号A', '账号B', '账号C', '账号D']
    assert not host.center.requests and not host.tasks.is_paused('bot-a', a['id'])
    # Same random selection of unchanged accounts shows the same batch.
    assert (await batch_api(host, 'POST', body={'accounts': [a['id'], b['id'], c['id'], d['id']]}))[1]['data']['id'] == batch['id']
    real_commit = host.tasks._commit_local

    async def commit(bot, key):
        if (await host.tasks.get(bot, key))['account'] == d['id']:
            raise OSError('fake local commit failure')
        return await real_commit(bot, key)

    monkeypatch.setattr(host.tasks, '_commit_local', commit)
    script(host, SUCCESS, REJECT, httpx.ReadTimeout('fake timeout'), SUCCESS)
    status, response = await batch_api(host, 'POST', f'/{batch["id"]}/confirm', {})
    assert status == 200 and response['data']['state'] == 'running'
    await settle(host)
    result = (await batch_api(host, 'GET', f'/{batch["id"]}'))[1]['data']
    assert [i['outcome'] for i in result['items']] == ['success', 'rejected', 'unknown', 'local_pending']
    assert result['state'] == 'finished' and result['notification'] == 'sent'
    assert host.center.changes == 4  # one submission per account, no automatic resend
    assert [host.tasks.is_paused('bot-a', x['id']) for x in (a, b, c, d)] == [False, True, True, True]
    changed = await host.tasks.store.get('bot-a', 'accounts', a['id'])
    assert re.fullmatch(r'(\d{4}(xmx|xc)|(xmx|xc)\d{4})', changed['password'])
    # Each account has its own task and independently fixed intent.
    task_ids = [i['task'] for i in result['items']]
    assert len(set(task_ids)) == 4
    summary = [p for p in pushes(host) if '批量改密批次' in p]
    assert len(summary) == 1 and '账号B：远端明确拒绝' in summary[0] and '账号D：远端成功、本地待完成' in summary[0]
    # Repeated confirmation returns the same batch and tasks; nothing new is submitted.
    again = (await batch_api(host, 'POST', f'/{batch["id"]}/confirm', {}))[1]['data']
    assert [i['task'] for i in again['items']] == task_ids and host.center.changes == 4
    # Failed items are not resent: the single-task rules (retry/recover) stay with the owner.
    assert (await task_info(host, {'id': task_ids[1]}))['remote'] == 'rejected'


async def test_group_and_private_qq_entries_keep_names_private(changing):
    host = changing
    await accounts(host, '账号B')
    assert await host.say('忍3批量改密 全部', user='MEMBERA') == ['仅主人可以操作改密任务']
    assert await host.say('忍3批量改密 全部', user='owner-b') == ['仅主人可以操作改密任务']
    assert '默认不选择' in (await host.say('忍3批量改密', user='owner-a'))[0]
    reply = (await host.say('忍3批量改密 账号A 账号B', user='owner-a'))[0]
    assert '已选择 2 个账号' in reply and '账号A' not in reply.replace('忍3批量改密 账号A', '')
    assert '账号B' not in reply
    key = re.search(r'忍3批量确认 ([0-9a-f]{32})', reply)[1]
    assert '有账号名称不存在，请在私聊' in (await host.say('忍3批量改密 账号A 无此账号', user='owner-a'))[0]
    detailed = (await private(host, '忍3批量改密 账号A 账号B'))[0]
    assert '账号A' in detailed and '账号B' in detailed and key in detailed  # same random batch
    accepted = (await host.say('忍3批量确认 ' + key, user='owner-a'))[0]
    assert '逐项结果请在主人私聊或后台查看' in accepted and '账号A' not in accepted
    await settle(host)
    assert host.center.changes == 2
    group_view = (await host.say('忍3批量任务 ' + key, user='owner-a'))[0]
    assert '成功 2' in group_view and '账号A' not in group_view
    assert '账号A：成功' in (await private(host, '忍3批量任务 ' + key))[0]


async def test_specified_password_only_from_owner_private_chat_or_backend(changing, caplog):
    caplog.set_level(logging.DEBUG)
    host = changing
    a, b = await accounts(host, '账号B')
    await host.tasks.store.put('bot-a', 'accounts', b['id'], {**b, 'password': 'Fake-new1'})
    # Group (public) and non-owner inputs are refused without using or echoing the value.
    for text in ('忍3批量改密 账号A 指定 Fake-new1', '忍3批量改密 账号A 指定Fake-new1'):
        replies = await host.say(text, user='owner-a')
        assert replies == ['指定密码只接受主人私聊或已登录后台，本条未使用']
    assert await private(host, '忍3批量改密 账号A 指定 Fake-new1', user='MEMBERA') == ['仅主人可以操作改密任务']
    assert (await batch_api(host))[1]['data'] == []
    assert '6到16' in (await private(host, '忍3批量改密 账号A 指定 abc'))[0]
    assert '空格' in (await private(host, '忍3批量改密 账号A 指定Fake-new1'))[0]
    # Owner C2C: same value for this confirmation only, checked per account against its old password.
    reply = (await private(host, '忍3批量改密 账号A 账号B 指定 Fake-new1'))[0]
    assert '账号B（指定密码与旧密码相同，将跳过）' in reply and 'Fake-new1' not in reply
    key = re.search(r'忍3批量确认 ([0-9a-f]{32})', reply)[1]
    assert not await stored(host, 'Fake-new1')  # sealed, not clear text
    confirmed = (await private(host, '忍3批量确认 ' + key))[0]
    await settle(host)
    result = (await batch_api(host, 'GET', '/' + key))[1]['data']
    assert [i['outcome'] for i in result['items']] == ['success', 'input_rejected'] and host.center.changes == 1
    assert (await host.tasks.store.get('bot-a', 'accounts', a['id']))['password'] == 'Fake-new1'
    raw = await host.tasks.store.atomic(lambda conn: json.loads(conn.execute('SELECT payload FROM ninja3_password_batches').fetchone()[0]))
    assert raw['sealed_password'] is None  # dropped once every item was handed over
    everything = ' '.join([confirmed, *pushes(host), *(r.getMessage() for r in caplog.records)])
    assert 'Fake-new1' not in everything
    # Authenticated, same-origin backend accepts it; cross-site or unauthenticated never does.
    body = {'accounts': [a['id']], 'password': 'Fake-new2'}
    assert (await batch_api(host, 'POST', body=body, authenticated=False))[0] == 401
    response = await host.http.request(
        'POST',
        '/api/ext/ninja3/password-batches',
        params={'bot': 'bot-a'},
        json=body,
        headers={'Cookie': 'elaina_session=group-test', 'Origin': 'https://evil.example'},
    )
    assert response.status == 403
    status, response = await batch_api(host, 'POST', body=body)
    assert status == 200 and response['data']['mode'] == 'specified' and 'Fake-new2' not in json.dumps(response)
    assert (await batch_api(host, 'POST', body={'accounts': [a['id']], 'password': 'Fake-new1'}))[1]['data']['items'][0]['outcome'] == ('input_rejected')
    # The single-account task entry still refuses any password parameter.
    assert (await host.ninja_api('POST', 'password-tasks', {'account': a['id'], 'password': 'Fake-new3'}))[0] == 422


async def test_scope_changes_after_selection_are_refused_at_confirmation(changing):
    host = changing
    (a,) = await accounts(host)
    everything = (await batch_api(host, 'POST', body={'accounts': 'all'}))[1]['data']
    one = (await batch_api(host, 'POST', body={'accounts': [a['id']]}))[1]['data']
    await host.setup(name='账号新', login=LOGIN, password=OLD)  # not part of the confirmed "all" snapshot
    status, response = await batch_api(host, 'POST', f'/{everything["id"]}/confirm', {})
    assert status == 422 and '范围在选择后变化' in response['error']
    assert (await host.ninja_api('PATCH', 'accounts/' + a['id'], {'description': 'changed'}))[0] == 200
    status, response = await batch_api(host, 'POST', f'/{one["id"]}/confirm', {})
    assert status == 422 and '账号在选择后变化' in response['error']
    assert not host.center.requests
    # A fresh "all" selection is a new snapshot including the new account.
    assert len((await batch_api(host, 'POST', body={'accounts': 'all'}))[1]['data']['items']) == 2


async def test_account_changed_while_earlier_item_runs_is_not_started(changing):
    host = changing
    a, b = await accounts(host, '账号B')
    batch = (await batch_api(host, 'POST', body={'accounts': [a['id'], b['id']]}))[1]['data']
    host.center.block_path = CHANGE
    await batch_api(host, 'POST', f'/{batch["id"]}/confirm', {})
    await host.center.entered.wait()
    assert (await host.ninja_api('PATCH', 'accounts/' + b['id'], {'description': 'changed'}))[0] == 200
    host.center.release.set()
    await settle(host)
    result = (await batch_api(host, 'GET', f'/{batch["id"]}'))[1]['data']
    assert [i['outcome'] for i in result['items']] == ['success', 'changed'] and host.center.changes == 1
    assert not host.tasks.is_paused('bot-a', b['id'])


async def test_actor_bot_and_existing_intent_rules(changing):
    host = changing
    a, b = await accounts(host, '账号B')
    reply = (await host.say('忍3批量改密 账号A', user='owner-a'))[0]
    key = re.search(r'忍3批量确认 ([0-9a-f]{32})', reply)[1]
    status, response = await batch_api(host, 'POST', f'/{key}/confirm', {})
    assert status == 422 and '请由选择账号的主人确认' in response['error']  # backend cannot borrow QQ selection
    assert (await batch_api(host, 'GET', '/' + key, bot='bot-b'))[0] == 422  # other Bot cannot see it
    # Same account already in a single-account task: the batch shows it and never submits again.
    single = await host.select(a['id'])
    host.center.block_path = CHANGE
    await host.action(single)
    await host.center.entered.wait()
    batch = (await batch_api(host, 'POST', body={'accounts': [a['id'], b['id']]}))[1]['data']
    await batch_api(host, 'POST', f'/{batch["id"]}/confirm', {})
    host.center.release.set()
    await settle(host)
    result = (await batch_api(host, 'GET', f'/{batch["id"]}'))[1]['data']
    assert result['items'][0]['outcome'] == 'existing' and result['items'][0]['task'] == single['id']
    assert result['items'][1]['outcome'] == 'success' and host.center.changes == 2


async def test_cancel_stops_unstarted_items_and_keeps_submitted_rules(changing):
    host = changing
    a, b, c = await accounts(host, '账号B', '账号C')
    unconfirmed = (await batch_api(host, 'POST', body={'accounts': [a['id']]}))[1]['data']
    assert (await batch_api(host, 'POST', f'/{unconfirmed["id"]}/cancel', {}))[1]['data']['state'] == 'cancelled'
    assert (await batch_api(host, 'POST', f'/{unconfirmed["id"]}/confirm', {}))[1]['data']['state'] == 'cancelled'
    batch = (await batch_api(host, 'POST', body={'accounts': [a['id'], b['id'], c['id']]}))[1]['data']
    host.center.block_path = CHANGE
    await batch_api(host, 'POST', f'/{batch["id"]}/confirm', {})
    await host.center.entered.wait()  # account A's change request is in flight
    cancelled = (await batch_api(host, 'POST', f'/{batch["id"]}/cancel', {}))[1]['data']
    host.center.release.set()
    await settle(host)
    result = (await batch_api(host, 'GET', f'/{batch["id"]}'))[1]['data']
    assert cancelled['state'] == 'cancelled'
    assert [i['outcome'] for i in result['items']] == ['unknown', 'not_started', 'not_started']
    assert host.center.changes == 1
    assert [host.tasks.is_paused('bot-a', x['id']) for x in (a, b, c)] == [True, False, False]


async def test_reload_interrupts_unstarted_items_and_old_reference_never_restarts(changing):
    host = changing
    a, b = await accounts(host, '账号B')
    batch = (await batch_api(host, 'POST', body={'accounts': [a['id'], b['id']]}))[1]['data']
    host.center.block_path = CHANGE
    await batch_api(host, 'POST', f'/{batch["id"]}/confirm', {})
    await host.center.entered.wait()
    await host.plugins.reload('ninja3')
    host.center.release.set()
    host.tasks = host.ninja().password_tasks
    result = (await batch_api(host, 'GET', f'/{batch["id"]}'))[1]['data']
    assert result['state'] == 'interrupted'
    assert [i['outcome'] for i in result['items']] == ['unknown', 'not_started']
    again = (await batch_api(host, 'POST', f'/{batch["id"]}/confirm', {}))[1]['data']
    assert again['state'] == 'interrupted' and not host.ninja().password_batches.runners
    assert host.center.changes == 1 and not host.tasks.is_paused('bot-a', b['id'])


OTHER_PROCESS = r"""
import asyncio,json,socket,sys
from plugins.ninja3.password_batches import PasswordBatches
from plugins.ninja3.password_tasks import IntentCipher,PasswordTasks
from plugins.ninja3.storage import ExtensionStore
def blocked(*args,**kwargs): raise AssertionError('no network in child')
for name in ('connect','connect_ex'): setattr(socket.socket,name,blocked)
socket.getaddrinfo=blocked
async def notify(*args): return 'unavailable'
async def run():
    store=ExtensionStore(sys.argv[1])
    tasks=PasswordTasks(store,cipher=IntentCipher(sys.argv[2]),notify=notify)
    batches=PasswordBatches(store,tasks,notify=notify)
    await batches.initialize()
    print(json.dumps((await batches.get('bot-a',sys.argv[3]))['state']))
    store.close()
asyncio.run(run())
"""


async def test_other_process_restart_stops_a_live_runner(changing):
    host = changing
    a, b = await accounts(host, '账号B')
    batch = (await batch_api(host, 'POST', body={'accounts': [a['id'], b['id']]}))[1]['data']
    host.center.block_path = CHANGE
    await batch_api(host, 'POST', f'/{batch["id"]}/confirm', {})
    await host.center.entered.wait()
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        '-c',
        OTHER_PROCESS,
        host.tasks.store.path,
        str(host.key_file),
        batch['id'],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(child.communicate(), 20)
    assert child.returncode == 0, err.decode()
    assert json.loads(out) == 'interrupted'
    host.center.release.set()
    await settle(host)
    result = (await batch_api(host, 'GET', f'/{batch["id"]}'))[1]['data']
    # Account A's own task finishes normally; the stale runner never starts account B.
    assert [i['outcome'] for i in result['items']] == ['success', 'not_started'] and host.center.changes == 1


@pytest.mark.parametrize('failure', ['failed', 'raises'])
async def test_summary_notice_failure_is_recorded_without_changing_results(changing, monkeypatch, failure):
    host = changing
    (a,) = await accounts(host)

    async def notify(bot, view):
        if failure == 'raises':
            raise RuntimeError('fake private send failure')
        return 'failed'

    monkeypatch.setattr(host.ninja().password_batches, 'notify', notify)
    batch = (await batch_api(host, 'POST', body={'accounts': [a['id']]}))[1]['data']
    await batch_api(host, 'POST', f'/{batch["id"]}/confirm', {})
    await settle(host)
    result = (await batch_api(host, 'GET', f'/{batch["id"]}'))[1]['data']
    assert result['notification'] == ('failed' if failure == 'failed' else 'unknown')
    assert result['items'][0]['outcome'] == 'success' and host.center.changes == 1


async def test_missing_key_starts_nothing_for_a_specified_batch(changing):
    host = changing
    (a,) = await accounts(host)
    batch = (await batch_api(host, 'POST', body={'accounts': [a['id']], 'password': 'Fake-new4'}))[1]['data']
    host.tasks.cipher = type(host.tasks.cipher)(str(host.key_file) + '.missing')
    await batch_api(host, 'POST', f'/{batch["id"]}/confirm', {})
    await settle(host)
    result = (await batch_api(host, 'GET', f'/{batch["id"]}'))[1]['data']
    assert result['state'] == 'interrupted' and result['items'][0]['outcome'] == 'not_started'
    assert not host.center.requests and not host.tasks.is_paused('bot-a', a['id'])
    with sqlite3.connect(host.tasks.store.path) as conn:
        assert json.loads(conn.execute('SELECT payload FROM ninja3_password_batches').fetchone()[0])['sealed_password'] is None


async def test_concurrent_confirm_and_overlapping_batch_submit_once_per_account(changing):
    host = changing
    a, b = await accounts(host, '账号B')
    first = (await batch_api(host, 'POST', body={'accounts': [a['id']]}))[1]['data']
    host.center.block_path = CHANGE
    one, two = await asyncio.gather(*(batch_api(host, 'POST', f'/{first["id"]}/confirm', {}) for _ in range(2)))
    assert one[1]['data']['id'] == two[1]['data']['id'] == first['id']
    assert len(host.ninja().password_batches.runners) == 1
    await host.center.entered.wait()
    # A second batch over the same account shows the first batch's task instead of a second intent.
    second = (await batch_api(host, 'POST', body={'accounts': [a['id'], b['id']]}))[1]['data']
    assert second['id'] != first['id']
    await batch_api(host, 'POST', f'/{second["id"]}/confirm', {})
    host.center.release.set()
    await settle(host)
    first_task = (await batch_api(host, 'GET', f'/{first["id"]}'))[1]['data']['items'][0]['task']
    items = (await batch_api(host, 'GET', f'/{second["id"]}'))[1]['data']['items']
    assert (items[0]['outcome'], items[0]['task']) == ('existing', first_task)
    assert items[1]['outcome'] == 'success' and host.center.changes == 2


async def interrupt_in_other_process(host, key):
    child = await asyncio.create_subprocess_exec(
        sys.executable, '-c', OTHER_PROCESS, host.tasks.store.path, str(host.key_file), key, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await asyncio.wait_for(child.communicate(), 20)
    assert child.returncode == 0, err.decode()
    return json.loads(out)


async def test_stale_runner_cannot_claim_an_account_after_another_process_interrupts(changing, monkeypatch):
    host = changing
    a, b = await accounts(host, '账号B')
    batch = (await batch_api(host, 'POST', body={'accounts': [a['id'], b['id']]}))[1]['data']
    real = host.tasks.confirm
    interrupted = []

    async def confirm(*args, **kwargs):
        # Between linking the account and claiming its task, another process restarts.
        if not interrupted:
            interrupted.append(await interrupt_in_other_process(host, batch['id']))
        return await real(*args, **kwargs)

    monkeypatch.setattr(host.tasks, 'confirm', confirm)
    await batch_api(host, 'POST', f'/{batch["id"]}/confirm', {})
    await settle(host)
    result = (await batch_api(host, 'GET', f'/{batch["id"]}'))[1]['data']
    assert interrupted == ['interrupted'] and result['state'] == 'interrupted'
    assert [i['outcome'] for i in result['items']] == ['not_started', 'not_started']
    assert not host.center.requests and not host.tasks.is_paused('bot-a', a['id'])


async def test_one_account_storage_error_continues_with_the_others(changing, monkeypatch):
    host = changing
    a, b = await accounts(host, '账号B')
    real = host.tasks.confirm

    async def confirm(bot, key, *args, **kwargs):
        if (await host.tasks.get(bot, key))['account'] == a['id']:
            raise sqlite3.OperationalError('fake disk error')
        return await real(bot, key, *args, **kwargs)

    monkeypatch.setattr(host.tasks, 'confirm', confirm)
    batch = (await batch_api(host, 'POST', body={'accounts': [a['id'], b['id']]}))[1]['data']
    await batch_api(host, 'POST', f'/{batch["id"]}/confirm', {})
    await settle(host)
    result = (await batch_api(host, 'GET', f'/{batch["id"]}'))[1]['data']
    assert result['state'] == 'finished' and result['notification'] == 'sent'
    assert [i['outcome'] for i in result['items']] == ['failed', 'success'] and host.center.changes == 1


async def test_live_view_shows_an_account_in_flight_as_running(changing):
    host = changing
    (a,) = await accounts(host)
    batch = (await batch_api(host, 'POST', body={'accounts': [a['id']]}))[1]['data']
    host.center.block_path = CHANGE
    await batch_api(host, 'POST', f'/{batch["id"]}/confirm', {})
    await host.center.entered.wait()  # submitted: the remote marker is already unknown
    assert (await batch_api(host, 'GET', f'/{batch["id"]}'))[1]['data']['items'][0]['outcome'] == 'running'
    host.center.release.set()
    await settle(host)
    assert (await batch_api(host, 'GET', f'/{batch["id"]}'))[1]['data']['items'][0]['outcome'] == 'success'
