"""Native socket → framework subscription → group facts → consuming callback."""

import asyncio
from contextlib import suppress

import pytest
from socket_platform import until


async def test_lifecycle_invalidates_before_history_and_consumers_finish(group_host):
    host = group_host
    state = host.state
    await state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    proof = await state.fact('bot-a', 'group-a', 'member-a')
    assert state.fact_current(proof)
    seen, entered, release = [], asyncio.Event(), asyncio.Event()

    async def consumer(change):
        seen.append(change)
        assert not state.fact_current(proof)
        entered.set()
        await release.wait()

    subscription = state.subscribe(consumer)
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a', sequence=2)
    await until(entered.is_set)
    pending = await state.fact('bot-a', 'group-a', 'member-a')
    assert not pending['usable'] and pending['member']['status'] == 'left'
    release.set()
    await until(lambda: bool(subscription.results))
    await until(lambda: state.recovery.get_status('bot-a').ready)
    left = await state.fact('bot-a', 'group-a', 'member-a')
    assert left['member']['cycle'] == proof['member']['cycle']
    assert left['member']['status'] == 'left'
    assert subscription.results[-1].status == 'success'
    assert seen[-1]['native']['event_id'] == 'event-2'
    assert seen[-1]['native']['sequence'] == 2
    host.lifecycle('GROUP_MEMBER_ADD', member='member-a', sequence=3, timestamp=1001)
    await until(lambda: len(subscription.results) == 2)
    joined = await state.fact('bot-a', 'group-a', 'member-a')
    assert joined['member']['cycle'] != proof['member']['cycle']
    assert not joined['member']['reliable']  # roles/membership require reliable query
    version = joined['version']
    host.lifecycle('GROUP_MEMBER_ADD', member='member-a', sequence=3, timestamp=1001)
    await asyncio.sleep(0.03)
    assert (await state.fact('bot-a', 'group-a', 'member-a'))['version'] == version


async def test_first_ready_reload_and_disconnect_need_real_baseline(group_host):
    host = group_host
    state = host.state
    await state.refresh('bot-a', 'group-a')
    await state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    proof = await state.fact('bot-a', 'group-a', 'member-a')
    assert proof['usable']
    host.sockets['bot-a'].frames.put_nowait(OSError('fake connection loss'))
    second = host.gateways['bot-a'].add()
    second.feed(10, d={'heartbeat_interval': 45000})
    await until(lambda: any(f['op'] == 6 for f in second.sent))
    assert not state.fact_current(proof)
    second.feed(0, t='RESUMED', s=2, d='')
    await until(lambda: state.recovery.get_status('bot-a').ready)
    assert (await state.fact('bot-a', 'group-a', 'member-a'))['usable']
    assert await host.modules.reload('group_state')
    state = host.state
    assert not (await state.fact('bot-a', 'group-a', 'member-a'))['usable']
    assert not await state.rebuild('bot-a')  # absent member query permission
    assert not state.recovery.get_status('bot-a').ready


async def test_delayed_sqlite_write_gates_fact_before_consumer_notification(group_host):
    import sqlite3

    host = group_host
    state = host.state
    await state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    proof = await state.fact('bot-a', 'group-a', 'member-a')
    seen = []

    async def consumer(change):
        seen.append(change)

    state.subscribe(consumer)
    # A real SQLite lock stalls history persistence rather than replacing _save.
    blocker = sqlite3.connect(state.store.path)
    blocker.execute('BEGIN IMMEDIATE')
    try:
        host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a')
        await until(lambda: state.pending_writes.get('bot-a', 0) > 0)
        assert not state.fact_current(proof)
        assert not (await state.fact('bot-a', 'group-a', 'member-a'))['usable']
        assert not seen
    finally:
        blocker.rollback()
        blocker.close()
    await until(lambda: bool(seen))
    await until(lambda: state.recovery.get_status('bot-a').ready)


async def test_consumer_timeout_and_unload_keep_real_writer_barrier(group_host):
    host = group_host
    state = host.state
    await state.refresh('bot-a', 'group-a')
    await state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def stubborn(change):
        entered.set()
        while not release.is_set():
            with suppress(asyncio.CancelledError):
                await release.wait()
        finished.set()

    sub = state.subscribe(stubborn, timeout=0.02)
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a')
    await until(entered.is_set)
    await until(lambda: bool(sub.results))
    assert sub.results[-1].status == 'timeout'
    assert not state.recovery.get_status('bot-a').ready
    assert await host.modules.reload('group_state')
    assert not await host.state.rebuild('bot-a')
    assert await host.modules.reload('group_state')
    assert not await host.state.rebuild('bot-a')
    release.set()
    await until(finished.is_set)
    await until(lambda: not host.state.notifications.busy('bot-a'))
    assert await host.state.rebuild('bot-a')
    # Reload preserved the left cycle; neither late callback nor baseline revives it.
    fact = await host.state.fact('bot-a', 'group-a', 'member-a')
    assert fact['member']['status'] == 'left'  # terminal history survives reload and cannot be revived
    assert not fact['usable']


async def test_unload_returns_consumer_status_and_no_trust_resurrection(group_host):
    host = group_host
    state = host.state
    await state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(change):
        entered.set()
        await release.wait()

    sub = state.subscribe(blocked)
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a')
    await until(entered.is_set)
    await host.modules.disable('group_state', _persist=False)
    await until(lambda: bool(sub.results))
    assert sub.results[-1].status == 'unloaded'
    assert not state.active
    release.set()


async def test_native_replay_id_is_not_applied_twice_and_bad_time_cannot_revive(group_host):
    host = group_host
    state = host.state
    await state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a')
    await until(lambda: state.recovery.get_status('bot-a').ready and state.states['bot-a']['groups']['group-a']['members']['member-a']['status'] == 'left')
    fact = await state.fact('bot-a', 'group-a', 'member-a')
    version, cycle = fact['version'], fact['member']['cycle']
    # A native replay may arrive at a different dispatch sequence.
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a', sequence=3, event_id='event-2')
    await asyncio.sleep(0.02)
    assert (await state.fact('bot-a', 'group-a', 'member-a'))['version'] == version
    host.lifecycle('GROUP_MEMBER_ADD', member='member-a', sequence=4, timestamp=1000)
    await until(lambda: state.recovery.get_status('bot-a').failed)
    fact = await state.fact('bot-a', 'group-a', 'member-a')
    assert not fact['usable'] and fact['member']['cycle'] == cycle
    assert fact['member']['status'] == 'left'
    await until(lambda: not state.pending_writes.get('bot-a'))
    assert (await state.fact('bot-a', 'group-a', 'member-a'))['group_state']['reason'] == 'same_time_conflict'


async def test_stale_platform_join_time_does_not_revive_left_cycle(group_host):
    host = group_host
    state = host.state
    await state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    cycle = (await state.fact('bot-a', 'group-a', 'member-a'))['member']['cycle']
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a', timestamp=2000000000)
    await until(lambda: state.recovery.get_status('bot-a').ready and state.states['bot-a']['groups']['group-a']['members']['member-a']['status'] == 'left')
    host.sender.json_results[('/v2/groups/group-a/members/member-a', '')] = (
        True,
        {'member_openid': 'member-a', 'member_role': 'admin', 'joined_at': '2020-01-01T00:00:00Z'},
    )
    result = await state.verify_member('bot-a', 'group-a', 'member-a')
    assert not result['reliable'] and result['reason'] == 'stale_membership'
    assert result['cycle'] == cycle


async def test_bot_removal_suspends_only_affected_bot_and_notifies_native_metadata(group_host):
    host = group_host
    state = host.state
    await state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    await state.confirm_member('bot-b', 'group-a', 'member-b', evidence='verified')
    proof_b = await state.fact('bot-b', 'group-a', 'member-b')
    changes = []

    async def consumer(change):
        changes.append(change)
        try:
            change['native']['group_openid'] = 'forged'
        except TypeError:
            return
        raise AssertionError('mutable notification')

    state.subscribe(consumer)
    host.lifecycle('GROUP_DEL_ROBOT')
    await until(lambda: bool(changes))
    assert not (await state.fact('bot-a', 'group-a', 'member-a'))['usable']
    assert state.fact_current(proof_b)
    assert changes[-1]['native']['group_openid'] == 'group-a'
    assert changes[-1]['native']['connection_context']['session_id'] == 'session-bot-a'


@pytest.mark.parametrize('fault', ['no_id', 'no_time', 'bad_time', 'no_member', 'no_group', 'late', 'conflict'])
async def test_ambiguous_native_events_suspend_facts_without_reviving_cycle(group_host, fault):
    host, state = group_host, group_host.state
    await state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a')
    await until(lambda: state.recovery.get_status('bot-a').ready and state.states['bot-a']['groups']['group-a']['members']['member-a']['status'] == 'left')
    cycle = (await state.fact('bot-a', 'group-a', 'member-a'))['member']['cycle']
    options = {'member': 'member-a', 'sequence': 3, 'timestamp': 1001}
    if fault == 'no_id':
        options['event_id'] = None
    elif fault == 'no_time':
        options['timestamp'] = None
    elif fault == 'bad_time':
        options['timestamp'] = 'invalid'
    elif fault == 'no_member':
        options['member'] = None
    elif fault == 'no_group':
        options['group'] = None
    elif fault == 'late':
        options['timestamp'] = 999
    elif fault == 'conflict':
        options['event_id'] = 'event-2'
    host.lifecycle('GROUP_MEMBER_ADD', **options)
    await until(lambda: state.recovery.get_status('bot-a').failed)
    fact = await state.fact('bot-a', 'group-a', 'member-a')
    assert not fact['usable'] and fact['member']['cycle'] == cycle
    assert fact['member']['status'] == 'left'


@pytest.mark.parametrize('outcome', ['error', 'cancelled'])
async def test_consumer_failure_cannot_acknowledge_recovery(group_host, outcome):
    host, state = group_host, group_host.state
    await state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')

    async def consumer(change):
        if outcome == 'error':
            raise ValueError('fake failure')
        raise asyncio.CancelledError

    sub = state.subscribe(consumer)
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a')
    await until(lambda: bool(sub.results))
    assert sub.results[-1].status == outcome
    assert not state.recovery.get_status('bot-a').ready
    sub.cancel()
    assert not sub.active
    assert await state.rebuild('bot-a')


async def test_successful_rebuild_rejects_checkpoint_changed_during_query(group_host):
    host, state = group_host, group_host.state
    await state.refresh('bot-a', 'group-a')

    # An actual dispatch is received while the authoritative baseline is queried.
    async def changed():
        host.lifecycle('GROUP_ADD_ROBOT', sequence=2)
        await until(lambda: state.recovery.get_status('bot-a').pending > 0)

    host.sender.after_query = changed
    assert not await state.rebuild('bot-a')
    host.sender.after_query = None


async def test_concurrent_group_notifications_wait_for_current_consumer(group_host):
    host, state = group_host, group_host.state
    for group in ('group-a', 'group-b'):
        await state.confirm_member('bot-a', group, 'member-a', evidence='verified')
    entered, release = asyncio.Event(), asyncio.Event()
    changes = []

    async def consume(change):
        changes.append(change)
        entered.set()
        await release.wait()

    sub = state.subscribe(consume)
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a', sequence=2)
    await until(entered.is_set)
    host.lifecycle('GROUP_MEMBER_REMOVE', group='group-b', member='member-a', sequence=3)
    await until(lambda: state.recovery.get_status('bot-a').pending == 2)
    await asyncio.sleep(0.02)
    release.set()
    await until(lambda: len(sub.results) == 2 or state.recovery.get_status('bot-a').failed)
    assert not state.recovery.get_status('bot-a').failed
    await until(lambda: state.recovery.get_status('bot-a').ready)
    assert [change['group'] for change in changes] == ['group-a', 'group-b']


async def test_disconnect_notifications_queue_behind_inflight_consumer(group_host):
    host, state = group_host, group_host.state
    await state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    entered, release = asyncio.Event(), asyncio.Event()

    async def consume(change):
        entered.set()
        await release.wait()

    state.subscribe(consume)
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a', sequence=2)
    await until(entered.is_set)
    host.sockets['bot-a'].frames.put_nowait(OSError('fake disconnect'))
    second = host.gateways['bot-a'].add()
    second.feed(10, d={'heartbeat_interval': 45000})
    await until(lambda: any(f['op'] == 6 for f in second.sent))
    second.feed(0, t='RESUMED', s=3, d='')
    await asyncio.sleep(0.02)
    release.set()
    await until(lambda: state.recovery.get_status('bot-a').ready or state.recovery.get_status('bot-a').failed)
    assert not state.recovery.get_status('bot-a').failed
    await until(lambda: state._ready('bot-a'))


@pytest.mark.parametrize('native_first', [False, True])
async def test_first_platform_query_after_manual_anchor_detects_new_cycle(group_host, native_first):
    host, state = group_host, group_host.state
    state.clock = lambda: 1500000000
    if native_first:
        host.lifecycle('GROUP_MEMBER_ADD', member='member-a', timestamp=1499999999)
        await until(
            lambda: (
                state.recovery.get_status('bot-a').ready and 'member-a' in state.states.get('bot-a', {}).get('groups', {}).get('group-a', {}).get('members', {})
            )
        )
    await state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    cycle = (await state.fact('bot-a', 'group-a', 'member-a'))['member']['cycle']
    assert await host.modules.reload('group_state')
    state = host.state
    host.sender.json_results[('/v2/groups/group-a/members/member-a', '')] = (
        True,
        {'member_openid': 'member-a', 'member_role': 'admin', 'joined_at': '2020-01-01T00:00:00Z'},
    )
    await state.verify_member('bot-a', 'group-a', 'member-a')
    record = (await state.fact('bot-a', 'group-a', 'member-a'))['member']
    assert record['cycle'] != cycle
