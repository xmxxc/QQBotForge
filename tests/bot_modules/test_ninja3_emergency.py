"""#78 persistent emergency mode through the real Ninja3 message and HTTP paths."""

import asyncio
import os
import sqlite3

import pytest
from account_host import account_host as account_host  # noqa: F401
from test_access_control import access_host as access_host  # noqa: F401
from test_ninja3 import ninja as ninja  # noqa: F401
from test_ninja3_password_tasks import CHANGE
from test_ninja3_password_tasks import changing as changing  # noqa: F401


async def wait_emergency(host):
    for _ in range(200):
        runners = list(host.ninja().emergency.runners.values())
        if not runners:
            return
        await asyncio.gather(*runners, return_exceptions=True)
    raise AssertionError('emergency runner did not finish')


async def private_messages(host):
    return [message[2] for message in host.sender.messages if message[1] == 'owner-a']


async def test_enter_persists_before_all_account_tasks_and_exit_restores_only_resolved_accounts(changing):
    host = changing
    second = await host.setup(name='账号B', login='16600000000', password='other-old')
    await host.event('忍3紧急模式', user='owner-a')
    status, response = await host.ninja_api('GET', 'emergency')
    assert status == 200 and response['data']['state'] == 'active'
    assert await host.say('账号列表', user='MEMBERA') == []
    await wait_emergency(host)
    result = (await host.ninja_api('GET', 'emergency'))[1]['data']
    assert result['phase'] == 'complete' and len(result['accounts']) == 2
    assert host.center.changes == 2
    assert (await host.tasks.store.get('bot-a', 'accounts', second['id']))['password'] != 'other-old'
    assert not [message for message in host.sender.messages if message[:2] == ('bot-a', 'group-a')]
    assert any('紧急模式' in message for message in await private_messages(host))
    await host.event('忍3退出紧急模式', user='owner-a')
    assert (await host.ninja_api('GET', 'emergency'))[1]['data']['state'] == 'exited'
    assert len(await host.say('账号 账号A', user='MEMBERA')) == 3


async def test_emergency_save_failure_is_fail_closed_until_a_later_owner_retry(changing, monkeypatch):
    host = changing
    emergency = host.ninja().emergency
    real = emergency.store.atomic

    async def fail(_action):
        raise sqlite3.OperationalError('fake emergency write failure')

    monkeypatch.setattr(emergency.store, 'atomic', fail)
    await host.event('忍3紧急模式', user='owner-a')
    assert await host.say('账号列表', user='MEMBERA') == []
    monkeypatch.setattr(emergency.store, 'atomic', real)
    await host.event('忍3紧急模式', user='owner-a')
    await wait_emergency(host)
    assert (await host.ninja_api('GET', 'emergency'))[1]['data']['state'] == 'active'


async def test_running_emergency_blocks_exit_and_restart_does_not_repeat_change(changing):
    host = changing
    host.center.block_path = CHANGE
    await host.event('忍3紧急模式', user='owner-a')
    await host.center.entered.wait()
    await host.event('忍3退出紧急模式', user='owner-a')
    assert any('仍在进行' in message for message in await private_messages(host))
    host.center.release.set()
    await wait_emergency(host)
    changes = host.center.changes
    await host.plugins.reload('ninja3')
    host.tasks = host.ninja().password_tasks
    assert (await host.ninja_api('GET', 'emergency'))[1]['data']['state'] == 'active'
    assert await host.say('账号列表', user='MEMBERA') == []
    await host.event('忍3紧急模式', user='owner-a')
    await wait_emergency(host)
    assert host.center.changes == changes
    await host.event('忍3退出紧急模式', user='owner-a')
    assert (await host.ninja_api('GET', 'emergency'))[1]['data']['state'] == 'exited'


async def test_non_owner_emergency_command_is_silent_and_other_bot_is_unaffected(changing):
    host = changing
    before = len(host.sender.messages)
    await host.event('忍3紧急模式', user='owner-b')
    assert len(host.sender.messages) == before
    assert (await host.ninja_api('GET', 'emergency', bot='bot-b'))[1]['data']['state'] == 'inactive'


async def test_emergency_gate_fails_closed_on_locked_or_unreadable_sqlite(changing, monkeypatch):
    host = changing
    emergency = host.ninja().emergency
    assert emergency.current('bot-a')

    def locked():
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(emergency, '_read_only', locked)
    assert not emergency.current('bot-a') and emergency.active('bot-a')


async def test_emergency_exit_is_observed_by_a_second_mode_instance(changing):
    host = changing
    emergency = host.ninja().emergency
    await emergency.enter('bot-a')
    peer = type(emergency)(host.tasks.store, host.tasks, notify=emergency.notify)
    await peer.initialize()
    assert not peer.current('bot-a')
    await wait_emergency(host)
    await emergency.exit('bot-a')
    assert peer.current('bot-a')
    await peer.shutdown()


async def test_active_emergency_routes_owner_group_management_responses_privately(changing):
    host = changing
    await host.ninja().emergency.enter('bot-a')
    commands = (
        '忍3改密 账号A',
        '忍3批量改密 账号A',
        '忍3确认 ' + '0' * 32,
        '忍3批量确认 ' + '0' * 32,
        '忍3',
    )
    for command in commands:
        assert await host.say(command, user='owner-a') == []
    await wait_emergency(host)
    await host.ninja().emergency.exit('bot-a')


async def test_emergency_query_gate_covers_unopened_group_and_c2c_paths(changing):
    host = changing
    await host.ninja_api('DELETE', 'groups/group-a')
    await host.ninja().emergency.enter('bot-a')
    assert await host.say('账号列表', user='MEMBERA') == []
    assert await host.say('账号列表', user='owner-a') == []

    before = len(host.sender.messages)
    await host.event('账号列表', user='owner-b')
    assert len(host.sender.messages) == before
    before = len(host.sender.messages)
    await host.event('账号列表', user='owner-a')
    private = host.sender.messages[before:]
    assert private and all(message[1] == 'owner-a' for message in private)
    await wait_emergency(host)
    await host.ninja().emergency.exit('bot-a')


async def test_second_emergency_connection_cannot_exit_during_remote_child_task(changing):
    host = changing
    host.center.block_path = CHANGE
    emergency = host.ninja().emergency
    await emergency.enter('bot-a')
    await host.center.entered.wait()

    from plugins.ninja3.storage import ExtensionStore

    peer_store = ExtensionStore(host.tasks.store.path)
    peer = type(emergency)(peer_store, host.tasks, notify=emergency.notify)
    await peer.initialize()
    await peer.enter('bot-a')
    await asyncio.gather(*list(peer.runners.values()), return_exceptions=True)
    with pytest.raises(ValueError, match='仍在进行'):
        await peer.exit('bot-a')
    host.center.release.set()
    await wait_emergency(host)
    await peer.exit('bot-a')
    assert peer.current('bot-a')
    await peer.shutdown()
    peer_store.close()


@pytest.mark.parametrize('persist_task_id', [False, True])
async def test_restart_clears_dead_runner_and_explicit_reentry_recovers_handoff(changing, monkeypatch, persist_task_id):
    host = changing
    emergency = host.ninja().emergency
    monkeypatch.setattr(emergency, '_start', lambda _bot: None)
    await emergency.enter('bot-a')
    state = await emergency.status('bot-a')
    account = state['accounts'][0]['account']
    task = None
    if persist_task_id:
        task = await host.tasks.select('bot-a', account, 'emergency:' + state['generation'])

    def handoff(conn):
        current = emergency._read(conn, 'bot-a')
        current['runner'] = 'dead-runner'
        current['runner_pid'] = os.getpid()
        current['phase'] = 'running'
        current['accounts'][0].update(status='running', task=task['id'] if task else None)
        emergency._save(conn, 'bot-a', current)

    await emergency.store.atomic(handoff)
    await host.plugins.reload('ninja3')
    host.tasks = host.ninja().password_tasks
    restarted = host.ninja().emergency
    normalized = await restarted.status('bot-a')
    assert normalized['phase'] == 'interrupted'
    assert all(item['status'] == ('running' if persist_task_id else 'pending') for item in normalized['accounts'])
    await restarted.enter('bot-a')
    await wait_emergency(host)
    assert (await restarted.status('bot-a'))['phase'] == 'complete'
    await restarted.exit('bot-a')


async def test_restart_preserves_completed_emergency_phase(changing, monkeypatch):
    host = changing
    emergency = host.ninja().emergency
    monkeypatch.setattr(emergency, '_start', lambda _bot: None)
    await emergency.enter('bot-a')
    await emergency.store.atomic(
        lambda conn: emergency._save(
            conn,
            'bot-a',
            {**emergency._read(conn, 'bot-a'), 'phase': 'complete', 'runner': 'dead-runner', 'runner_pid': os.getpid()},
        )
    )
    await host.plugins.reload('ninja3')
    restarted = host.ninja().emergency
    assert (await restarted.status('bot-a'))['phase'] == 'complete'
    await restarted.exit('bot-a')


async def test_exit_cas_rechecks_a_peer_runner_inside_the_transition(changing, monkeypatch):
    host = changing
    emergency = host.ninja().emergency
    monkeypatch.setattr(emergency, '_start', lambda _bot: None)
    await emergency.enter('bot-a')

    def peer_claim(conn):
        state = emergency._read(conn, 'bot-a')
        state.update(phase='running', runner='peer-runner', runner_pid=os.getpid())
        emergency._save(conn, 'bot-a', state)

    await emergency.store.atomic(peer_claim)
    with pytest.raises(ValueError, match='仍在进行'):
        await emergency.exit('bot-a')

    def clear_peer(conn):
        state = emergency._read(conn, 'bot-a')
        state.update(phase='interrupted', runner=None, runner_pid=None)
        emergency._save(conn, 'bot-a', state)

    await emergency.store.atomic(clear_peer)
    await emergency.exit('bot-a')


async def test_completed_emergency_stays_complete_after_real_runner_and_reload(changing):
    host = changing
    await host.ninja().emergency.enter('bot-a')
    await wait_emergency(host)
    assert (await host.ninja().emergency.status('bot-a'))['phase'] == 'complete'
    changes = host.center.changes
    await host.plugins.reload('ninja3')
    state = await host.ninja().emergency.status('bot-a')
    assert (state['state'], state['phase']) == ('active', 'complete')
    assert host.center.changes == changes
    assert not host.ninja().emergency.current('bot-a')
