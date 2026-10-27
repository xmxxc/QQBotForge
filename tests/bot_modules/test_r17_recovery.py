"""R17 recovery through real modules, temporary SQLite and fake WS/platform I/O."""

import asyncio
import sqlite3

import pytest
from socket_platform import until
from test_access_control import access_host as access_host  # noqa: F401
from test_access_control import configure, lifecycle_done

from core.plugin._blacklist import get_blacklist_map, set_blacklist_map


async def restart_modules(host):
    # Cold module instances read the same temporary databases; no process restart.
    assert await host.modules.reload('group_state')
    assert await host.modules.reload('access_control')
    host.access = host.modules.get('access_control')
    return host.state, host.access


async def granted(host):
    acl = await configure(host)
    acl.clock = lambda: 100
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='owner checked')
    source = await acl.manage_access('bot-a', 'reports', 'grant', function='read', group='group-a', member='member-a', resources=['report-r'], expires_at=200)
    return source


async def evaluate(host):
    return await host.access.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r')


@pytest.mark.parametrize(
    'control, reason',
    [
        ('revoked', 'no_explicit_permission'),
        ('leave_revoked', 'no_explicit_permission'),
        ('expired', 'expired'),
        ('denied', 'denied'),
        ('user_blacklist', 'framework_denied'),
        ('group_blacklist', 'framework_denied'),
    ],
)
async def test_recovery_never_lifts_revocation_expiry_denial_or_blacklist(access_host, control, reason):
    host = access_host
    old = await granted(host)
    if control == 'revoked':
        await host.access.manage_access('bot-a', 'reports', 'revoke', function='read', group='group-a', member='member-a', source=old['id'])
    elif control == 'leave_revoked':
        host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a')
        await lifecycle_done(host, 2)
    elif control == 'denied':
        await host.access.manage_access('bot-a', 'reports', 'deny', function='read', group='group-a', member='member-a')
    state, acl = await restart_modules(host)
    acl.clock = lambda: 200 if control == 'expired' else 100
    kind = 'group' if control == 'group_blacklist' else 'user'
    before = get_blacklist_map('bot-a', kind)
    if control.endswith('blacklist'):
        set_blacklist_map('bot-a', kind, {**before, 'group-a' if kind == 'group' else 'member-a': 'test only'})
    try:
        await state.confirm_member('bot-a', 'group-a', 'member-a', evidence='currently present')
        assert await state.rebuild('bot-a')
        after = await evaluate(host)
        assert not after['allowed'] and after['reason'] == reason
        snapshot = await acl.access_snapshot('bot-a', 'reports')
        if control in {'revoked', 'leave_revoked'}:
            assert not snapshot['grants'] and [r['id'] for r in snapshot['revocations']] == [old['id']]
            assert snapshot['revocations'][0]['reason'] == ('owner_revoked' if control == 'revoked' else 'group_lifecycle')
        else:
            assert snapshot['grants'] == [old] and not snapshot['revocations']
    finally:
        set_blacklist_map('bot-a', kind, before)


@pytest.mark.parametrize('gap', ['restart_modules', 'resume_replay'])
@pytest.mark.parametrize('events', [('GROUP_MEMBER_REMOVE',), ('GROUP_MEMBER_ADD',), ('GROUP_MEMBER_REMOVE', 'GROUP_MEMBER_ADD')])
async def test_observed_offline_transitions_retire_old_sources(access_host, gap, events):
    host = access_host
    old = await granted(host)
    if gap == 'restart_modules':
        await restart_modules(host)
    else:
        host.sockets['bot-a'].frames.put_nowait(OSError('fake disconnect'))
        socket = host.sockets['bot-a'] = host.gateways['bot-a'].add()
        socket.feed(10, d={'heartbeat_interval': 45000})
        await until(lambda: any(frame['op'] == 6 for frame in socket.sent))
    assert not (await evaluate(host))['allowed']
    for sequence, kind in enumerate(events, 2):
        host.lifecycle(kind, member='member-a', sequence=sequence, timestamp=1000 + sequence)
    if gap == 'resume_replay':
        socket.feed(0, t='RESUMED', s=sequence + 1, d='')
    await lifecycle_done(host, sequence)
    if gap == 'resume_replay':
        # The replay is trusted once the same-session boundary and every connection status settled.
        await until(lambda: host.state._ready('bot-a'))
    host.access.clock = lambda: 100
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='present after observed transition')
    assert await host.state.rebuild('bot-a')
    after = await evaluate(host)
    assert not after['allowed'] and after['cycle'] != old['cycle']
    snapshot = await host.access.access_snapshot('bot-a', 'reports')
    assert not snapshot['grants'] and [r['id'] for r in snapshot['revocations']] == [old['id']]
    # Persistence across another module generation cannot revive the old source.
    state, acl = await restart_modules(host)
    acl.clock = lambda: 100
    await state.confirm_member('bot-a', 'group-a', 'member-a', evidence='present once more')
    assert await state.rebuild('bot-a')
    assert not (await evaluate(host))['allowed']
    assert (await acl.access_snapshot('bot-a', 'reports'))['revocations'] == snapshot['revocations']


async def test_single_member_confirmation_preserves_scope_and_never_proves_roster(access_host):
    host = access_host
    old = await granted(host)
    await configure(host, 'other')
    await configure(host, bot='bot-b')
    await host.access.declare('bot-a', 'reports', owner='plugin:reports', functions={'read': {'kind': 'explicit', 'groups': ['group-a', 'group-b']}})
    for bot, group, member in [('bot-a', 'group-a', 'member-b'), ('bot-a', 'group-b', 'member-a'), ('bot-b', 'group-a', 'member-a')]:
        await host.state.confirm_member(bot, group, member, evidence='initial owner check')
    state, acl = await restart_modules(host)
    acl.clock = lambda: 100
    await state.confirm_member('bot-a', 'group-a', 'member-a', evidence='only this member checked')
    assert not (await state.fact('bot-a', 'group-a', 'member-b'))['usable']
    assert not await state.rebuild('bot-a')  # unverified known members still block the baseline
    assert not (await evaluate(host))['allowed']
    # Reconfirm all known facts after the failed rebuild changed the recovery revision.
    for bot, group, member in [('bot-a', 'group-a', 'member-a'), ('bot-a', 'group-a', 'member-b'), ('bot-a', 'group-b', 'member-a')]:
        await state.confirm_member(bot, group, member, evidence='individual owner check')
    assert await state.rebuild('bot-a')
    assert not (await acl.evaluate('bot-b', 'reports', 'read', 'group-a', 'member-a', 'report-r'))['allowed']
    await state.confirm_member('bot-b', 'group-a', 'member-a', evidence='bot-b owner check')
    assert await state.rebuild('bot-b')
    assert (await evaluate(host))['sources'][0]['id'] == old['id']
    for bot, consumer, group, member in [
        ('bot-b', 'reports', 'group-a', 'member-a'),
        ('bot-a', 'other', 'group-a', 'member-a'),
        ('bot-a', 'reports', 'group-b', 'member-a'),
        ('bot-a', 'reports', 'group-a', 'member-b'),
    ]:
        assert not (await acl.evaluate(bot, consumer, 'read', group, member, 'report-r'))['allowed']
    report = await acl.audience_report('bot-a', 'reports', 'read', 'group-a', 'report-r')
    assert not report['complete'] and not report['eligible'] and not report['failures_complete']


@pytest.mark.parametrize('failed_query', [False, True])
async def test_cancelled_query_cannot_erase_unconsumed_departure_on_restart(access_host, failed_query):
    host = access_host
    old = await granted(host)
    # A consumer that was offline must still retire the leave when it next reads history.
    await host.modules.disable('access_control', _persist=False)
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a')
    await lifecycle_done(host, 2)
    entered = asyncio.Event()

    async def slow_platform(path, cursor):
        entered.set()
        await asyncio.Event().wait()

    host.sender.after_json = slow_platform
    pending = asyncio.create_task(host.state.verify_member('bot-a', 'group-a', 'member-a'))
    await until(entered.is_set)  # the query's pending marker is already on disk
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    host.sender.after_json = None
    assert await host.modules.reload('group_state')
    assert await host.modules.enable('access_control', _skip_deps=True, _persist=False)
    host.access = host.modules.get('access_control')
    host.access.clock = lambda: 100
    if failed_query:
        assert not (await host.state.verify_member('bot-a', 'group-a', 'member-a'))['reliable']
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='present after departure')
    assert await host.state.rebuild('bot-a')
    after = await evaluate(host)
    assert not after['allowed'] and after['cycle'] != old['cycle']
    snapshot = await host.access.access_snapshot('bot-a', 'reports')
    assert not snapshot['grants'] and [r['id'] for r in snapshot['revocations']] == [old['id']]


@pytest.mark.parametrize('race', ['write_pending', 'cancelled', 'leave'])
async def test_recovery_confirmation_waits_for_storage_and_cannot_overwrite_leave(access_host, race):
    host = access_host
    old = await granted(host)
    state, acl = await restart_modules(host)
    acl.clock = lambda: 100
    # Load history before holding a real SQLite writer lock.
    await state.fact('bot-a', 'group-a', 'member-a')
    blocker = sqlite3.connect(state.store.path)
    blocker.execute('BEGIN IMMEDIATE')
    pending = asyncio.create_task(state.confirm_member('bot-a', 'group-a', 'member-a', evidence='current owner check'))
    try:
        await until(lambda: bool(state.pending_writes.get('bot-a')))
        assert not (await evaluate(host))['allowed']
        if race == 'cancelled':
            pending.cancel()
            await asyncio.sleep(0.02)
            assert not pending.done()  # SQLite must drain before releasing the write boundary
        elif race == 'leave':
            host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a')
            await until(lambda: state.states['bot-a']['groups']['group-a']['members']['member-a']['status'] == 'left')
    finally:
        blocker.rollback()
        blocker.close()
    if race == 'cancelled':
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not (await evaluate(host))['allowed']
    else:
        result = await pending
        if race == 'leave':
            assert not result['reliable']
            await lifecycle_done(host, 2)
            assert not (await evaluate(host))['allowed']
            assert (await acl.access_snapshot('bot-a', 'reports'))['revocations'][0]['id'] == old['id']
            return
    await state.confirm_member('bot-a', 'group-a', 'member-a', evidence='fresh confirmation after storage settled')
    assert await state.rebuild('bot-a')
    after = await evaluate(host)
    assert after['allowed'] and after['sources'][0] == {**old, 'status': 'active'}


@pytest.mark.parametrize('ambiguity', ['late_event', 'same_time_conflict', 'conflicting_duplicate', 'incomplete_event', 'missing_member'])
async def test_rejected_member_transition_is_not_proof_of_continuity(access_host, ambiguity):
    host = access_host
    await host.state.confirm_member('bot-a', 'group-a', 'member-b', evidence='owner checked')
    # A trusted join before the grant gives later events an ordering anchor.
    host.lifecycle('GROUP_MEMBER_ADD', member='member-a', sequence=2, timestamp=1000)
    await lifecycle_done(host, 2)
    old = await granted(host)
    other = await host.access.manage_access('bot-a', 'reports', 'grant', function='read', group='group-a', member='member-b', resources=['report-r'])
    event = {
        'late_event': dict(timestamp=999),
        'same_time_conflict': dict(timestamp=1000),
        'conflicting_duplicate': dict(timestamp=1003, event_id='event-2'),
        'incomplete_event': dict(timestamp=None),
        'missing_member': dict(timestamp=1003),
    }[ambiguity]
    host.lifecycle('GROUP_MEMBER_REMOVE', member=None if ambiguity == 'missing_member' else 'member-a', sequence=3, **event)
    await lifecycle_done(host, 3)
    assert not host.state.recovery.get_status('bot-a').ready
    # The rejected event revokes nothing by itself.
    assert [g['id'] for g in (await host.access.access_snapshot('bot-a', 'reports'))['grants']] == [old['id'], other['id']]
    state, acl = await restart_modules(host)
    acl.clock = lambda: 100
    for member in ('member-a', 'member-b'):
        await state.confirm_member('bot-a', 'group-a', member, evidence='currently present')
    assert await state.rebuild('bot-a')
    after = await evaluate(host)
    assert not after['allowed'] and after['cycle'] != old['cycle']
    revoked = [r['id'] for r in (await acl.access_snapshot('bot-a', 'reports'))['revocations']]
    assert old['id'] in revoked
    # A transition attributed to member-a says nothing about member-b; an unattributed one may concern anyone.
    b = await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-b', 'report-r')
    assert b['allowed'] == (ambiguity != 'missing_member')
    assert (other['id'] in revoked) == (ambiguity == 'missing_member')


async def test_restart_marker_persisted_by_earlier_release_does_not_retire_sources(access_host):
    host = access_host
    old = await granted(host)
    # Releases before R17 persisted this marker on every member loaded after a restart.
    state = await host.state._state('bot-a')
    state['groups']['group-a']['members']['member-a']['cycle_uncertain'] = True
    await host.state._save('bot-a', state)
    state, acl = await restart_modules(host)
    acl.clock = lambda: 100
    await state.confirm_member('bot-a', 'group-a', 'member-a', evidence='currently present')
    assert await state.rebuild('bot-a')
    after = await evaluate(host)
    assert after['allowed'] and after['cycle'] == old['cycle'] and after['sources'][0]['id'] == old['id']
    assert 'cycle_uncertain' not in (await state.fact('bot-a', 'group-a', 'member-a'))['member']
