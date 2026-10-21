"""Independent authorization management consuming real group facts and WS events."""

import asyncio
import shutil
from pathlib import Path

import pytest
from socket_platform import until


@pytest.fixture
async def access_host(group_host):
    host = group_host
    destination = Path(host.modules._dir) / 'access_control'
    shutil.copytree(Path(__file__).absolute().parents[2] / 'modules/access_control', destination, ignore=shutil.ignore_patterns('data', '__pycache__'))
    host.modules.discover_module('access_control')
    assert await host.modules.enable('access_control', _skip_deps=True, _persist=False)
    host.access = host.modules.get('access_control')
    return host


async def configure(host, consumer='reports', *, bot='bot-a', kind='explicit'):
    acl = host.access
    await acl.declare(bot, consumer, owner='plugin:' + consumer, functions={'read': {'kind': kind, 'groups': ['group-a']}})
    await acl.declare_resources(bot, consumer, [{'id': 'report-r'}])
    return acl


async def test_consumer_isolation_native_leave_and_role_reentry(access_host):
    host = access_host
    acl = await configure(host)
    await configure(host, 'other')
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='owner verified', member_role='admin')
    await acl.manage_access('bot-a', 'reports', 'grant', function='read', group='group-a', member='member-a', resources=['report-r'])
    decision = await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r')
    assert decision['allowed'] and acl.decision_current('bot-a', decision)
    assert not (await acl.evaluate('bot-a', 'other', 'read', 'group-a', 'member-a', 'report-r'))['allowed']
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a')
    await until(lambda: not acl.decision_current('bot-a', decision))
    await until(lambda: host.state.recovery.get_status('bot-a').ready)
    snapshot = await acl.access_snapshot('bot-a', 'reports')
    assert not snapshot['grants'] and snapshot['revocations'][-1]['id'] == decision['sources'][0]['id']
    host.lifecycle('GROUP_MEMBER_ADD', member='member-a', sequence=3, timestamp=1001)
    await until(
        lambda: host.state.recovery.get_status('bot-a').ready and host.state.states['bot-a']['groups']['group-a'].get('last_native', {}).get('sequence') == 3
    )
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='rejoined', member_role='admin')
    assert not (await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r'))['allowed']
    await acl.manage_access('bot-a', 'reports', 'policy', function='read', policy={'kind': 'role', 'groups': ['group-a'], 'roles': ['admin']})
    assert (await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r'))['allowed']
    assert not (await acl.access_snapshot('bot-a', 'reports'))['grants']


async def test_public_policy_deny_and_owner_identity_collision(access_host):
    host = access_host
    acl = await configure(host, kind='public')
    await host.modules.disable('group_state', _persist=False)
    decision = await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r')
    assert decision['allowed'] and acl.decision_current('bot-a', decision)
    assert not (await acl.evaluate('bot-a', 'reports', 'read', 'unknown-group', 'member-a', 'report-r'))['allowed']
    with pytest.raises(ValueError):
        await acl.declare('bot-a', 'reports', owner='different-plugin', functions={'read': {'kind': 'public'}})
    await acl.manage_access('bot-a', 'reports', 'deny', function='read', group='group-a', member='member-a')
    assert (await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r'))['reason'] == 'denied'
    assert not acl.decision_current('bot-a', decision)


async def request(host, body=None, *, authenticated=True, bot='bot-a', consumer='reports', origin=''):
    return await host.http.request(
        'POST' if body else 'GET',
        '/api/ext/access-control/',
        params={'bot': bot, 'consumer': consumer},
        json=body,
        headers={**({'Cookie': 'elaina_session=group-test'} if authenticated else {}), **({'Origin': origin} if origin else {})},
    )


async def test_owner_http_commands_persistence_and_expiry(access_host):
    import json

    host, acl = access_host, access_host.access
    assert (await request(host, authenticated=False)).status == 401
    data = {'operation': 'register', 'consumer': 'reports', 'owner': 'plugin:reports', 'functions': {'read': {'kind': 'explicit', 'groups': ['group-a']}}}
    assert (await request(host, data, origin='https://external.example')).status == 403
    assert (await request(host, data)).status == 200
    assert (await request(host, {'operation': 'resources', 'consumer': 'reports', 'resources': [{'id': 'report-r'}]})).status == 200
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    acl.clock = lambda: 100
    saved = await request(
        host,
        {
            'operation': 'grant',
            'consumer': 'reports',
            'function': 'read',
            'group': 'group-a',
            'member': 'member-a',
            'resources': ['report-r'],
            'expires_at': 200,
        },
    )
    source = (await saved.json())['data']
    assert saved.status == 200 and source['expires_at'] == 200
    decision = await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r')
    assert decision['allowed'] and decision['valid_until'] == 200
    acl.clock = lambda: 200
    assert not acl.decision_current('bot-a', decision)
    assert (await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r'))['reason'] == 'expired'
    text = '授权管理 ' + json.dumps({'operation': 'snapshot', 'consumer': 'reports'})
    host.sender.messages.clear()
    await host.event(text, user='ordinary')
    assert not host.sender.messages
    await host.event(text)
    assert source['id'] in host.sender.messages[-1][2]
    assert await host.modules.reload('access_control')
    restored = host.modules.get('access_control')
    snapshot = await restored.access_snapshot('bot-a', 'reports')
    assert snapshot['grants'][0]['id'] == source['id']
    assert snapshot['grants'][0]['cycle'] == source['cycle']
    assert not restored.decision_current('bot-a', decision)


async def test_local_sqlite_failure_does_not_poison_other_consumer_or_bot(access_host):
    import sqlite3

    host = access_host
    acl = await configure(host, kind='public')
    await configure(host, 'other', kind='public')
    await configure(host, bot='bot-b', kind='public')
    good = await acl.evaluate('bot-a', 'other', 'read', 'group-a', 'member-a', 'report-r')
    other_bot = await acl.evaluate('bot-b', 'reports', 'read', 'group-a', 'member-a', 'report-r')
    with sqlite3.connect(acl.store.path) as db:
        db.execute(
            "CREATE TRIGGER local_failure BEFORE UPDATE ON extension_records WHEN NEW.bot='bot-a' AND NEW.collection='access_consumers' AND NEW.id='reports' BEGIN SELECT RAISE(ABORT, 'sample row failure'); END"
        )
    response = await request(host, {'operation': 'deny', 'consumer': 'reports', 'function': 'read', 'group': 'group-a', 'member': 'member-a'})
    assert response.status == 503
    assert acl.decision_current('bot-a', good)
    assert acl.decision_current('bot-b', other_bot)
    assert not acl.shared_failure
    with pytest.raises(RuntimeError):
        await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r')


async def test_shared_database_failure_invalidates_all_cached_consumers(access_host):
    import sqlite3

    host = access_host
    acl = await configure(host, kind='public')
    before = await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r')
    with sqlite3.connect(acl.store.path) as db:
        db.execute('DROP TABLE extension_records')
    with pytest.raises(sqlite3.OperationalError):
        await acl.declare('bot-b', 'other', owner='plugin:other', functions={'read': {'kind': 'public'}})
    assert acl.shared_failure
    assert not acl.decision_current('bot-a', before)


async def test_current_role_change_drops_derived_permission_and_keeps_deny(access_host):
    host = access_host
    acl = await configure(host, kind='public')
    await acl.manage_access('bot-a', 'reports', 'policy', function='read', policy={'kind': 'role', 'roles': ['admin'], 'groups': ['group-a']})
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='admin verified', member_role='admin')
    old = await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r')
    assert old['allowed']
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='now regular member', member_role='member')
    assert not acl.decision_current('bot-a', old)
    assert not (await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r'))['allowed']
    await acl.manage_access('bot-a', 'reports', 'deny', function='read', group='group-a', member='member-a')
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='admin again', member_role='admin')
    assert (await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r'))['reason'] == 'denied'


async def test_leave_revokes_all_consumers_despite_one_row_write_failure(access_host):
    import sqlite3

    host = access_host
    acl = await configure(host)
    await configure(host, 'other')
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    for consumer in ('reports', 'other'):
        await acl.manage_access('bot-a', consumer, 'grant', function='read', group='group-a', member='member-a', resources=['report-r'])
    with sqlite3.connect(acl.store.path) as db:
        db.execute(
            "CREATE TRIGGER bad_consumer BEFORE UPDATE ON extension_records WHEN NEW.collection='access_consumers' AND NEW.id='reports' BEGIN SELECT RAISE(ABORT, 'sample row failure'); END"
        )
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a')
    await until(lambda: ('bot-a', 'reports') in acl.scope_failed)
    await until(lambda: host.state.recovery.get_status('bot-a').ready)
    good = await acl.access_snapshot('bot-a', 'other')
    assert not good['grants'] and good['revocations']
    assert not acl.shared_failure
    with sqlite3.connect(acl.store.path) as db:
        db.execute('DROP TRIGGER bad_consumer')
    assert await host.modules.reload('access_control')
    acl = host.modules.get('access_control')
    # Failed row still contains its historical grant. The group fact gates it.
    decision = await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r')
    assert not decision['allowed']


async def test_query_detected_new_cycle_retires_old_explicit_sources(access_host):
    host = access_host
    acl = await configure(host)
    host.state.clock = lambda: 1500000000
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    grant = await acl.manage_access('bot-a', 'reports', 'grant', function='read', group='group-a', member='member-a', resources=['report-r'])
    host.sender.json_results[('/v2/groups/group-a/members/member-a', '')] = (
        True,
        {'member_openid': 'member-a', 'member_role': 'admin', 'joined_at': '2020-01-01T00:00:00Z'},
    )
    await host.state.verify_member('bot-a', 'group-a', 'member-a')
    state = await acl.access_snapshot('bot-a', 'reports')
    assert not state['grants'] and state['revocations'][-1]['id'] == grant['id']
    assert not (await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r'))['allowed']


async def test_reload_authorization_subscribes_before_any_permission_query(access_host):
    host = access_host
    acl = await configure(host)
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    source = await acl.manage_access('bot-a', 'reports', 'grant', function='read', group='group-a', member='member-a', resources=['report-r'])
    assert await host.modules.reload('access_control')
    acl = host.modules.get('access_control')
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a')
    await until(
        lambda: host.state.recovery.get_status('bot-a').ready and host.state.states['bot-a']['groups']['group-a'].get('last_native', {}).get('sequence') == 2
    )
    snapshot = await acl.access_snapshot('bot-a', 'reports')
    assert not snapshot['grants']
    assert snapshot['revocations'][-1]['id'] == source['id']


@pytest.mark.parametrize('corruption', ['json', 'id', 'grants', 'schema', 'policy_fields', 'expiry_field', 'null'])
async def test_bad_consumer_read_is_local_during_shared_lifecycle(access_host, corruption):
    import json
    import sqlite3

    host = access_host
    acl = await configure(host)
    await configure(host, 'other', kind='group')
    await acl.manage_access('bot-a', 'other', 'policy', function='read', policy={'kind': 'group', 'groups': ['group-a', 'group-b']})
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    await host.state.confirm_member('bot-a', 'group-b', 'member-b', evidence='verified')
    bad = await acl.access_snapshot('bot-a', 'reports')
    bad.pop('generation')
    if corruption == 'null':
        bad = None
    elif corruption == 'id':
        del bad['id']
    elif corruption == 'grants':
        bad['grants'] = {'wrong': 'shape'}
    elif corruption == 'schema':
        bad['schema_version'] = 999
    elif corruption == 'policy_fields':
        bad['functions']['read'] = {'kind': 'public'}
    elif corruption == 'expiry_field':
        bad['grants'] = [
            {
                'id': 'old-source',
                'function': 'read',
                'group': 'group-a',
                'member': 'other-member',
                'cycle': 'old-cycle',
                'resources': ['report-r'],
                'invoke': True,
            }
        ]
    with sqlite3.connect(acl.store.path) as db:
        db.execute(
            "UPDATE extension_records SET payload=? WHERE collection='access_consumers' AND id='reports'",
            ('{invalid' if corruption == 'json' else json.dumps(bad),),
        )
    assert await host.modules.reload('access_control')
    acl = host.modules.get('access_control')
    healthy = await acl.evaluate('bot-a', 'other', 'read', 'group-b', 'member-b', 'report-r')
    assert healthy['allowed']
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a')
    await until(
        lambda: (
            host.state.recovery.get_status('bot-a').failed
            or (host.state.recovery.get_status('bot-a').ready and host.state.states['bot-a']['groups']['group-a'].get('last_native', {}).get('sequence') == 2)
        )
    )
    assert not host.state.recovery.get_status('bot-a').failed
    assert not acl.shared_failure
    with pytest.raises(RuntimeError):
        await acl.access_snapshot('bot-a', 'reports')
    assert (await acl.evaluate('bot-a', 'other', 'read', 'group-b', 'member-b', 'report-r'))['allowed']


async def test_group_state_late_enable_and_reload_rebinds_authorization(access_host):
    host = access_host
    acl = await configure(host)
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    grant = await acl.manage_access('bot-a', 'reports', 'grant', function='read', group='group-a', member='member-a', resources=['report-r'])
    await host.modules.disable('group_state', _persist=False)
    assert await host.modules.reload('access_control')
    acl = host.modules.get('access_control')
    assert await host.modules.enable('group_state', _skip_deps=True, _persist=False)
    # Re-enable has no trusted baseline yet, but must already deliver changes.
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a')
    await until(
        lambda: (
            host.state.states.get('bot-a', {}).get('groups', {}).get('group-a', {}).get('last_native', {}).get('sequence') == 2
            and not host.state.recovery.get_status('bot-a').pending
        )
    )
    snapshot = await acl.access_snapshot('bot-a', 'reports')
    assert not snapshot['grants'] and snapshot['revocations'][-1]['id'] == grant['id']
    assert await host.modules.reload('group_state')
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='owner rechecked')
    assert await host.state.rebuild('bot-a')
    grant = await acl.manage_access('bot-a', 'reports', 'grant', function='read', group='group-a', member='member-a', resources=['report-r'])
    assert await host.modules.reload('group_state')
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a', sequence=3, timestamp=1002)
    await until(
        lambda: (
            not host.state.recovery.get_status('bot-a').pending
            and host.state.states.get('bot-a', {}).get('groups', {}).get('group-a', {}).get('last_native', {}).get('sequence') == 3
        )
    )
    snapshot = await acl.access_snapshot('bot-a', 'reports')
    assert not snapshot['grants'] and snapshot['revocations'][-1]['id'] == grant['id']


@pytest.mark.parametrize('gap', ['restart', 'failed_query', 'new_session'])
async def test_manual_current_presence_after_restart_does_not_restore_old_grant(access_host, gap):
    host = access_host
    acl = await configure(host)
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified before restart')
    old = await acl.manage_access('bot-a', 'reports', 'grant', function='read', group='group-a', member='member-a', resources=['report-r'])
    if gap == 'new_session':
        host.sockets['bot-a'].feed(9, d=False)
        second = host.gateways['bot-a'].add()
        second.feed(10, d={'heartbeat_interval': 45000})
        await asyncio.sleep(3.1)
        await until(lambda: any(frame['op'] == 2 for frame in second.sent))
        second.feed(0, t='READY', s=2, d={'session_id': 'new-session-bot-a'})
        await until(lambda: host.state.recovery.get_status('bot-a').context.session_id == 'new-session-bot-a')
        assert not await host.state.rebuild('bot-a')
    else:
        assert await host.modules.reload('group_state')
        if gap == 'failed_query':
            await host.state.verify_member('bot-a', 'group-a', 'member-a')
    # Owner confirms only current presence, not continuity over the unknown gap.
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='currently in group')
    assert await host.state.rebuild('bot-a')
    after = await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r')
    assert not after['allowed']
    snapshot = await acl.access_snapshot('bot-a', 'reports')
    assert not snapshot['grants'] and snapshot['revocations'][-1]['id'] == old['id']


def platform_member(host, *members, group='group-a'):
    from test_group_roster import member

    for native in members:
        host.sender.json_results[(f'/v2/groups/{group}/members/{native}', '')] = (True, member(native))


async def lifecycle_done(host, sequence):
    state = host.state
    await until(lambda: state.states['bot-a']['groups']['group-a'].get('last_native', {}).get('sequence') == sequence)
    await until(lambda: not state.recovery.get_status('bot-a').pending and not state.notifications.busy('bot-a'))


@pytest.mark.parametrize('event', ['GROUP_DEL_ROBOT', 'GROUP_ADD_ROBOT'])
async def test_bot_join_or_removal_pauses_explicit_sources_until_verified(access_host, event):
    host = access_host
    acl, state = await configure(host), host.state
    for member in ('member-a', 'member-b', 'member-c'):
        await state.confirm_member('bot-a', 'group-a', member, evidence='owner verified', member_role='member')
    grants = {
        member: await acl.manage_access('bot-a', 'reports', 'grant', function='read', group='group-a', member=member, resources=['report-r'])
        for member in ('member-a', 'member-b', 'member-c')
    }
    await acl.manage_access('bot-a', 'reports', 'revoke', function='read', group='group-a', member='member-b')
    host.lifecycle(event, sequence=2, timestamp=1000)
    await lifecycle_done(host, 2)
    snapshot = await acl.access_snapshot('bot-a', 'reports')
    assert {g['id'] for g in snapshot['grants']} == {grants['member-a']['id'], grants['member-c']['id']}
    assert [r['reason'] for r in snapshot['revocations']] == ['owner_revoked']
    paused = await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r')
    assert not paused['allowed'] and paused['reason'] == 'group_facts_recovery_required' and not paused['retryable']
    # member-c is no longer in the group according to the platform; member-a and member-b still are.
    platform_member(host, 'member-a', 'member-b')
    assert not await state.rebuild('bot-a')
    platform_member(host, 'member-c')
    host.sender.json_results.pop(('/v2/groups/group-a/members/member-c', ''))
    await state.verify_member('bot-a', 'group-a', 'member-c')
    restored = await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r')
    assert not restored['allowed']
    platform_member(host, 'member-c')
    assert await state.rebuild('bot-a')
    restored = await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r')
    assert restored['allowed'] and [s['id'] for s in restored['sources']] == [grants['member-a']['id']]
    assert not (await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-b', 'report-r'))['allowed']
    assert (await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-c', 'report-r'))['allowed']


@pytest.mark.parametrize('stale', ['late', 'same_time', 'conflict', 'incomplete', 'replay'])
async def test_rejected_native_event_cannot_revoke_new_cycle_grant(access_host, stale):
    host = access_host
    acl, state = await configure(host), host.state
    await configure(host, 'other')
    await state.confirm_member('bot-a', 'group-a', 'member-a', evidence='owner verified', member_role='member')
    first = await acl.manage_access('bot-a', 'reports', 'grant', function='read', group='group-a', member='member-a', resources=['report-r'])
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a', sequence=2, timestamp=1000)
    await lifecycle_done(host, 2)
    assert (await acl.access_snapshot('bot-a', 'reports'))['revocations'][-1]['id'] == first['id']
    host.lifecycle('GROUP_MEMBER_ADD', member='member-a', sequence=3, timestamp=1001)
    await lifecycle_done(host, 3)
    await state.confirm_member('bot-a', 'group-a', 'member-a', evidence='rejoined', member_role='member')
    renewed = {
        consumer: await acl.manage_access('bot-a', consumer, 'grant', function='read', group='group-a', member='member-a', resources=['report-r'])
        for consumer in ('reports', 'other')
    }
    assert (await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r'))['allowed']
    options = {
        'late': {'event_id': 'event-late', 'timestamp': 1000},
        'same_time': {'event_id': 'event-same', 'timestamp': 1001},
        'conflict': {'event_id': 'event-3', 'timestamp': 1002},
        'incomplete': {'event_id': None, 'timestamp': 1002},
        'replay': {'event_id': 'event-2', 'timestamp': 1000},
    }[stale]
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a', sequence=4, **options)
    await until(lambda: not state.recovery.get_status('bot-a').pending)
    await asyncio.sleep(0.05)
    for consumer, grant in renewed.items():
        assert [g['id'] for g in (await acl.access_snapshot('bot-a', consumer))['grants']] == [grant['id']]
    if stale != 'replay':
        paused = await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r')
        assert not paused['allowed'] and paused['reason'] == 'group_facts_recovery_required'
        platform_member(host, 'member-a')
        assert await state.rebuild('bot-a')
    assert (await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r'))['allowed']
    # A real, ordered departure still revokes every explicit source of that member in that group.
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a', sequence=5, event_id='event-real', timestamp=1003)
    await lifecycle_done(host, 5)
    for consumer, grant in renewed.items():
        snapshot = await acl.access_snapshot('bot-a', consumer)
        assert not snapshot['grants'] and snapshot['revocations'][-1]['id'] == grant['id']
    left = await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r')
    assert not left['allowed'] and left['reason'] == 'member_left'


async def test_unaffected_member_keeps_grant_after_trusted_member_events(access_host):
    host = access_host
    acl, state = await configure(host), host.state
    await state.confirm_member('bot-a', 'group-a', 'member-b', evidence='owner verified', member_role='member')
    grant = await acl.manage_access('bot-a', 'reports', 'grant', function='read', group='group-a', member='member-b', resources=['report-r'])
    before = await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-b', 'report-r')
    host.lifecycle('GROUP_MEMBER_ADD', member='member-c', sequence=2, timestamp=1000)
    await lifecycle_done(host, 2)
    assert not acl.decision_current('bot-a', before)
    newcomer = await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-c', 'report-r')
    assert not newcomer['allowed'] and newcomer['reason'] == 'membership_unverified'
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-c', sequence=3, timestamp=1001)
    await lifecycle_done(host, 3)
    after = await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-b', 'report-r')
    assert after['allowed'] and acl.decision_current('bot-a', after) and after['sources'][0]['id'] == grant['id']
    assert (await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-c', 'report-r'))['reason'] == 'member_left'
    assert host.sender.messages == []


@pytest.mark.parametrize('groups', [1, 3])
async def test_first_message_after_trusted_resume_is_retryable_then_allowed(access_host, groups):
    host = access_host
    acl, state = await configure(host, kind='group'), host.state
    names = ['group-' + letter for letter in 'abc'[:groups]]
    await acl.manage_access('bot-a', 'reports', 'policy', function='read', policy={'kind': 'group', 'groups': names})
    for name in names:
        await state.confirm_member('bot-a', name, 'member-a', evidence='owner verified', member_role='member')
    await until(lambda: state._ready('bot-a'))
    release = asyncio.Event()

    async def slow(change):
        await release.wait()

    state.subscribe(slow, timeout=30)
    host.sockets['bot-a'].frames.put_nowait(OSError('fake disconnect'))
    second = host.gateways['bot-a'].add()
    second.feed(10, d={'heartbeat_interval': 45000})
    await until(lambda: any(f['op'] == 6 for f in second.sent))
    second.feed(0, t='RESUMED', s=2, d='')
    await until(lambda: state.recovery.get_status('bot-a').ready)
    for name in names:
        first = await acl.evaluate('bot-a', 'reports', 'read', name, 'member-a', 'report-r')
        assert not first['allowed'] and first['reason'] == 'group_facts_processing' and first['retryable']
    release.set()
    await until(lambda: state._ready('bot-a'))
    for name in names:
        later = await acl.evaluate('bot-a', 'reports', 'read', name, 'member-a', 'report-r')
        assert later['allowed'] and acl.decision_current('bot-a', later)


@pytest.mark.parametrize('failure', ['page', 'route', 'cancel'])
async def test_failed_setup_releases_only_its_own_entries(access_host, monkeypatch, failure):
    from core.application import get_app
    from core.plugin import web_pages

    host = access_host
    hook_manager = get_app().hook_manager
    assert await host.modules.disable('access_control', _persist=False)
    other, newer = object(), object()
    web_pages.register_route('GET', '/api/ext/other-module/', other, auth=True)
    web_pages.register_page('other-module', 'Other', source='module', source_name='other')
    register_page, register_route = web_pages.register_page, web_pages.register_route

    def failing_page(key, *args, **kwargs):
        if key != 'access-control':
            return register_page(key, *args, **kwargs)
        if failure == 'cancel':
            raise asyncio.CancelledError
        # A newer instance replaced the shared route before this failed attempt unwinds.
        register_route('*', '/api/ext/access-control/*', newer, auth=True)
        raise RuntimeError('fake page failure')

    def failing_route(method, path, handler=None, **kwargs):
        if path.startswith('/api/ext/access-control/'):
            raise ValueError('fake route failure')
        return register_route(method, path, handler, **kwargs)

    if failure == 'route':
        monkeypatch.setattr(web_pages, 'register_route', failing_route)
    else:
        monkeypatch.setattr(web_pages, 'register_page', failing_page)
    try:
        if failure == 'cancel':
            with pytest.raises(asyncio.CancelledError):
                await host.modules.enable('access_control', _skip_deps=True, _persist=False)
        else:
            assert not await host.modules.enable('access_control', _skip_deps=True, _persist=False)
        hooks = hook_manager.list_hooks()
        assert [e for name in ('before_event', 'group_state_available') for e in hooks.get(name, []) if e['owner'] == '黑白名单授权'] == []
        route = web_pages.match_route('POST', '/api/ext/access-control/')
        assert (route['handler'] is newer) if failure == 'page' else (route is None)
        assert web_pages.get_page_html('access-control') is None
        assert web_pages.match_route('GET', '/api/ext/other-module/')['handler'] is other
        assert web_pages.get_page_html('other-module') is not None
        assert host.modules.get('access_control') is None
        assert [e for e in hooks.get('before_event', []) if e['owner'] == '群状态']
    finally:
        web_pages.unregister_route('GET', '/api/ext/other-module/')
        web_pages.unregister_page('other-module')
        web_pages.unregister_route('*', '/api/ext/access-control/*')
    monkeypatch.setattr(web_pages, 'register_page', register_page)
    monkeypatch.setattr(web_pages, 'register_route', register_route)
    assert await host.modules.enable('access_control', _skip_deps=True, _persist=False)
    hooks = hook_manager.list_hooks()
    assert len([e for e in hooks.get('before_event', []) if e['owner'] == '黑白名单授权']) == 1
    assert len([e for e in hooks.get('group_state_available', []) if e['owner'] == '黑白名单授权']) == 1
    host.sender.messages.clear()
    await host.event('授权管理 {"consumer":"reports","operation":"snapshot"}', user='owner-a')
    assert len(host.sender.messages) == 1
