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
    # Consumer results precede the framework's final delivery bookkeeping.
    await until(lambda: not state.recovery.get_status('bot-a').pending)
    assert state.recovery.get_status('bot-a').failed
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


@pytest.mark.parametrize('operation', ['refresh', 'roster', 'member', 'confirm'])
async def test_candidate_commits_wait_for_consumer_completion(group_host, operation):
    from test_group_roster import roster_platform

    host = group_host
    state = host.state
    roster_platform(host)
    await state.confirm_member('bot-a', 'group-a', 'member-a', evidence='主人已核验')
    proof = await state.fact('bot-a', 'group-a', 'member-a')
    assert state.fact_current(proof)
    entered, release = asyncio.Event(), asyncio.Event()

    async def consumer(change):
        assert not state.fact_current(proof)
        entered.set()
        await release.wait()

    sub = state.subscribe(consumer)
    actions = {
        'refresh': lambda: state.refresh('bot-a', 'group-a'),
        'roster': lambda: state.sync_roster('bot-a', 'group-a'),
        'member': lambda: state.verify_member('bot-a', 'group-a', 'member-a'),
        'confirm': lambda: state.confirm_member('bot-a', 'group-a', 'member-a', evidence='主人重新核验'),
    }
    pending = asyncio.create_task(actions[operation]())
    try:
        await until(entered.is_set)
        assert not pending.done()
        assert not (await state.fact('bot-a', 'group-a', 'member-a'))['usable']
    finally:
        release.set()
    result = await pending
    assert sub.results[-1].status == 'success'
    assert result['complete'] if operation == 'roster' else result['reliable']
    assert (await state.fact('bot-a', 'group-a', 'member-a'))['usable']
    assert not state.fact_current(proof)


@pytest.mark.parametrize('kind', ['GROUP_MEMBER_ADD', 'GROUP_MEMBER_REMOVE'])
async def test_trusted_member_transition_retires_only_that_member(group_host, kind):
    from test_group_roster import roster_platform

    host, state = group_host, group_host.state
    roster_platform(host)
    await state.refresh('bot-a', 'group-a')
    assert (await state.sync_roster('bot-a', 'group-a'))['complete']
    group_proof = await state.fact('bot-a', 'group-a')
    other = await state.fact('bot-a', 'group-a', 'other-bot')
    assert group_proof['usable'] and other['usable']
    target = 'member-new' if kind == 'GROUP_MEMBER_ADD' else 'member-a'
    host.lifecycle(kind, member=target, sequence=2, timestamp=1000)
    await until(lambda: state.states['bot-a']['groups']['group-a'].get('last_native', {}).get('sequence') == 2 and state._ready('bot-a'))
    # The version change retires every earlier snapshot, roster proof and member proof.
    assert not state.fact_current(group_proof) and not state.fact_current(other)
    group = (await state.snapshot('bot-a'))['groups'][0]
    assert not group['roster']['complete'] and group['roster']['reason'] == 'lifecycle_changed'
    # Unaffected members and the Bot's own group facts stay usable once read again.
    again = await state.fact('bot-a', 'group-a', 'other-bot')
    assert again['usable'] and state.fact_current(again) and again['member']['cycle'] == other['member']['cycle']
    assert (await state.fact('bot-a', 'group-a'))['usable']
    changed = await state.fact('bot-a', 'group-a', target)
    if kind == 'GROUP_MEMBER_ADD':
        assert not changed['usable'] and changed['member']['status'] == 'pending'
        return
    assert changed['member']['status'] == 'left'
    cycle = changed['member']['cycle']
    host.lifecycle('GROUP_MEMBER_ADD', member=target, sequence=3, timestamp=1001)
    await until(lambda: state.states['bot-a']['groups']['group-a']['members'][target]['status'] == 'pending' and state._ready('bot-a'))
    rejoined = await state.fact('bot-a', 'group-a', target)
    assert not rejoined['usable'] and rejoined['member']['cycle'] != cycle
    assert (await state.fact('bot-a', 'group-a', 'other-bot'))['usable']


async def test_trusted_member_event_rejects_inflight_check_of_other_member(group_host):
    host, state = group_host, group_host.state
    from test_group_roster import member

    host.sender.json_results = {('/v2/groups/group-a/members/member-b', ''): (True, member('member-b'))}
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(path, cursor):
        entered.set()
        await release.wait()

    host.sender.after_json = hold
    pending = asyncio.create_task(state.verify_member('bot-a', 'group-a', 'member-b'))
    await until(entered.is_set)
    host.lifecycle('GROUP_MEMBER_ADD', member='member-c', sequence=2, timestamp=1000)
    await until(lambda: 'member-c' in state.states['bot-a']['groups']['group-a']['members'])
    release.set()
    assert (await pending)['reason'] == 'state_changed'
    host.sender.after_json = None
    await until(lambda: state._ready('bot-a'))
    assert (await state.verify_member('bot-a', 'group-a', 'member-b'))['reliable']


@pytest.mark.parametrize('fault', ['late', 'no_id'])
async def test_untrusted_member_event_still_pauses_whole_bot(group_host, fault):
    host, state = group_host, group_host.state
    for member in ('member-a', 'member-b'):
        await state.confirm_member('bot-a', 'group-a', member, evidence='verified')
    host.lifecycle('GROUP_MEMBER_ADD', member='member-c', sequence=2, timestamp=1000)
    await until(lambda: state._ready('bot-a') and 'member-c' in state.states['bot-a']['groups']['group-a']['members'])
    options = {'timestamp': 999} if fault == 'late' else {'event_id': None, 'timestamp': 1002}
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-c', sequence=3, **options)
    await until(lambda: state.recovery.get_status('bot-a').failed)
    for member in ('member-a', 'member-b'):
        fact = await state.fact('bot-a', 'group-a', member)
        assert not fact['usable'] and fact['reason'] == 'recovery_required'
    assert state.states['bot-a']['groups']['group-a']['members']['member-c']['status'] == 'pending'


@pytest.mark.parametrize('slow', [False, True])
@pytest.mark.parametrize('groups', [1, 3])
async def test_trusted_resume_waits_for_consumers_before_facts_return(group_host, groups, slow):
    host, state = group_host, group_host.state
    names = ['group-' + letter for letter in 'abc'[:groups]]
    for name in names:
        await state.confirm_member('bot-a', name, 'member-a', evidence='verified')
    await until(lambda: state._ready('bot-a'))
    before = await state.fact('bot-a', names[-1], 'member-a')
    release, seen = asyncio.Event(), []

    async def consumer(change):
        seen.append(change['reason'])
        if slow:
            await release.wait()

    state.subscribe(consumer, timeout=30)
    host.sockets['bot-a'].frames.put_nowait(OSError('fake disconnect'))
    second = host.gateways['bot-a'].add()
    second.feed(10, d={'heartbeat_interval': 45000})
    await until(lambda: any(f['op'] == 6 for f in second.sent))
    assert not state.fact_current(before)
    second.feed(0, t='RESUMED', s=2, d='')
    await until(lambda: state.recovery.get_status('bot-a').ready)
    if slow:
        # The framework replay boundary is done, but consumers still hold the barrier.
        for name in names:
            paused = await state.fact('bot-a', name, 'member-a')
            assert not paused['usable'] and paused['reason'] == 'recovery_processing'
        release.set()
    await until(lambda: state._ready('bot-a'))
    for name in names:
        fact = await state.fact('bot-a', name, 'member-a')
        assert fact['usable'] and fact['reason'] == '' and state.fact_current(fact)
    assert not state.fact_current(before)
    assert len([reason for reason in seen if reason.startswith('connection:')]) >= 2 * groups


def delay_queued_connection_callback(state, monkeypatch, phase='replay_complete'):
    """Hold the framework-queued connection callback before it enters group state;
    statuses and recovery stay real, only the dispatch is late."""
    original = state.connection_subscription._callback
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(status):
        if status.appid == 'bot-a' and status.phase == phase:
            entered.set()
            await release.wait()
        return await original(status)

    monkeypatch.setattr(state.connection_subscription, '_callback', delayed)
    return entered, release


async def resume(host):
    host.sockets['bot-a'].frames.put_nowait(OSError('fake disconnect'))
    second = host.gateways['bot-a'].add()
    second.feed(10, d={'heartbeat_interval': 45000})
    await until(lambda: any(f['op'] == 6 for f in second.sent))
    second.feed(0, t='RESUMED', s=2, d='')


@pytest.mark.parametrize('groups', [1, 3])
async def test_queued_connection_notification_holds_facts_until_consumers_finish(group_host, monkeypatch, groups):
    host, state = group_host, group_host.state
    names = ['group-' + letter for letter in 'abc'[:groups]]
    for name in names:
        await state.confirm_member('bot-a', name, 'member-a', evidence='verified')
    await until(lambda: state._ready('bot-a'))
    before = await state.fact('bot-a', names[-1], 'member-a')
    consumer_entered, consumer_release = asyncio.Event(), asyncio.Event()

    async def consumer(change):
        if change['reason'] == 'connection:replay_complete':
            consumer_entered.set()
            await consumer_release.wait()

    state.subscribe(consumer, timeout=30)
    entered, release = delay_queued_connection_callback(state, monkeypatch)
    await resume(host)
    await until(entered.is_set)
    assert state.recovery.get_status('bot-a').ready  # the framework side is done
    assert not (await state.snapshot('bot-a'))['recovery']['ready']
    for name in names:
        queued = await state.fact('bot-a', name, 'member-a')
        assert not queued['usable'] and queued['reason'] == 'recovery_processing'
        assert not state.fact_current(queued)
    assert not state.fact_current(before)
    release.set()
    await until(consumer_entered.is_set)
    during = await state.fact('bot-a', names[0], 'member-a')
    assert not during['usable'] and during['reason'] == 'recovery_processing'
    consumer_release.set()
    await until(lambda: state._ready('bot-a'))
    for name in names:
        fact = await state.fact('bot-a', name, 'member-a')
        assert fact['usable'] and state.fact_current(fact)
    assert not state.fact_current(before)


async def test_queued_connection_notification_failure_needs_rebuild_not_forever(group_host, monkeypatch):
    host, state = group_host, group_host.state
    await state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    await until(lambda: state._ready('bot-a'))

    async def failing(change):
        if change['reason'] == 'connection:replay_complete':
            raise RuntimeError('consumer failed')

    sub = state.subscribe(failing)
    entered, release = delay_queued_connection_callback(state, monkeypatch)
    await resume(host)
    await until(entered.is_set)
    assert (await state.fact('bot-a', 'group-a', 'member-a'))['reason'] == 'recovery_processing'
    release.set()
    await until(lambda: any(r.status.phase == 'replay_complete' and r.outcome == 'error' for r in state.connection_subscription.results))
    failed = await state.fact('bot-a', 'group-a', 'member-a')
    assert not failed['usable'] and failed['reason'] == 'recovery_required'
    sub.cancel()
    # A new baseline (current owner confirmation + rebuild) restores use; no permanent pause.
    await state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified again')
    assert await state.rebuild('bot-a')
    assert (await state.fact('bot-a', 'group-a', 'member-a'))['usable']


async def test_reload_with_queued_connection_notification_cannot_mark_new_instance(group_host, monkeypatch):
    host, old = group_host, group_host.state
    await old.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    await until(lambda: old._ready('bot-a'))
    entered, release = delay_queued_connection_callback(old, monkeypatch)
    await resume(host)
    await until(entered.is_set)
    assert await host.modules.reload('group_state')
    new = host.state
    assert new is not old and not old.active
    await new.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    assert await new.rebuild('bot-a')
    fact = await new.fact('bot-a', 'group-a', 'member-a')
    assert fact['usable']
    release.set()
    await until(lambda: any(r.status.phase == 'replay_complete' for r in old.connection_subscription.results))
    # The old instance's late callback neither marks nor pauses the new instance.
    assert new.fact_current(fact) and not old._ready('bot-a')


async def test_timed_out_connection_callback_and_skipped_statuses_need_rebuild_not_forever(group_host, monkeypatch):
    host, state = group_host, group_host.state
    await state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    await until(lambda: state._ready('bot-a'))
    release = asyncio.Event()

    async def slow(change):
        if change['reason'].startswith('connection:'):
            await release.wait()

    sub = state.subscribe(slow, timeout=30)
    # The framework stops waiting for our callback; statuses queued behind it are skipped as 'busy'.
    monkeypatch.setattr(state.connection_subscription, 'timeout', 0.05)
    await resume(host)
    await until(lambda: {'timeout', 'busy'} <= {r.outcome for r in state.connection_subscription.results})
    await until(lambda: not state.notifications.busy('bot-a'))
    paused = await state.fact('bot-a', 'group-a', 'member-a')
    assert not paused['usable'] and paused['reason'] == 'recovery_required'
    release.set()
    sub.cancel()
    await state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified again')
    assert await state.rebuild('bot-a')
    assert (await state.fact('bot-a', 'group-a', 'member-a'))['usable']
