"""Owner-facing scoped grants reuse range selection within one Bot/consumer."""

import pytest
from test_access_control import access_host as access_host
from test_access_control import configure, request


async def test_snapshot_and_continuous_scopes_follow_explicit_owner_intent(access_host):
    host = access_host
    acl = await configure(host)
    await acl.declare('bot-a', 'reports', owner='plugin:reports', functions={'export': {'kind': 'explicit', 'groups': ['group-a']}})
    await acl.declare_resources('bot-a', 'reports', [{'id': 'report-r', 'groups': ['daily']}])
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    base = {'consumer': 'reports', 'function': 'read', 'group': 'group-a', 'member': 'member-a'}
    group = await request(host, {**base, 'operation': 'function_group', 'id': 'bundle', 'functions': ['read']})
    assert group.status == 200
    saved = await request(
        host, {**base, 'operation': 'grant', 'function_scope': {'kind': 'all'}, 'resource_scope': {'kind': 'group', 'id': 'daily'}, 'mode': 'snapshot'}
    )
    assert saved.status == 200
    snapshot = (await saved.json())['data']
    future = await request(
        host,
        {
            **base,
            'operation': 'grant',
            'function_scope': {'kind': 'group', 'id': 'bundle'},
            'resource_scope': {'kind': 'group', 'id': 'daily'},
            'mode': 'continuous',
        },
    )
    assert future.status == 200
    continuous = (await future.json())['data']
    assert continuous['id'] != snapshot['id']
    await acl.declare_resources('bot-a', 'reports', [{'id': 'report-r', 'groups': ['daily']}, {'id': 'report-new', 'groups': ['daily']}])
    assert (await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-new'))['allowed']
    assert not (await acl.evaluate('bot-a', 'reports', 'export', 'group-a', 'member-a', 'report-new'))['allowed']
    await request(host, {**base, 'operation': 'function_group', 'id': 'bundle', 'functions': ['read', 'export']})
    after = await acl.evaluate('bot-a', 'reports', 'export', 'group-a', 'member-a', 'report-new')
    assert after['allowed']
    assert continuous['id'] in [source['id'] for source in after['sources']]
    await request(host, {**base, 'operation': 'revoke', 'source': continuous['id']})
    assert not (await acl.evaluate('bot-a', 'reports', 'export', 'group-a', 'member-a', 'report-new'))['allowed']
    old = await acl.evaluate('bot-a', 'reports', 'export', 'group-a', 'member-a', 'report-r')
    assert old['allowed'] and old['sources'][0]['id'] == snapshot['id']


async def test_resource_removal_recomputes_continuous_without_reviving_snapshot(access_host):
    host = access_host
    acl = await configure(host)
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    await acl.declare_resources('bot-a', 'reports', [{'id': 'report-r', 'groups': ['daily']}, {'id': 'keep', 'groups': ['daily']}])
    base = {
        'consumer': 'reports',
        'function': 'read',
        'group': 'group-a',
        'member': 'member-a',
        'operation': 'grant',
        'resource_scope': {'kind': 'group', 'id': 'daily'},
    }
    snap = (await (await request(host, {**base, 'mode': 'snapshot'})).json())['data']
    live = (await (await request(host, {**base, 'mode': 'continuous'})).json())['data']
    await acl.declare_resources('bot-a', 'reports', [{'id': 'keep', 'groups': ['daily']}])
    await acl.declare_resources('bot-a', 'reports', [{'id': 'report-r', 'groups': ['daily']}, {'id': 'keep', 'groups': ['daily']}])
    after = await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r')
    assert after['allowed']
    assert live['id'] in [g['id'] for g in after['sources']]
    await acl.manage_access('bot-a', 'reports', 'revoke', function='read', group='group-a', member='member-a', source=live['id'])
    assert not (await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r'))['allowed']
    kept = await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'keep')
    assert kept['allowed'] and kept['sources'][0]['id'] == snap['id']


async def test_bulk_partial_retry_deny_precedence_and_expiry_boundary(access_host):
    host = access_host
    acl = await configure(host)
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    acl.clock = lambda: 100
    base = {'consumer': 'reports', 'function': 'read', 'group': 'group-a'}
    batch = await request(
        host, {**base, 'operation': 'bulk_grant', 'members': [{'member': 'member-a'}, {'member': 'member-b'}], 'resources': ['report-r'], 'expires_at': 200}
    )
    assert batch.status == 200
    report = (await batch.json())['data']
    assert report['summary'] == {'success': 1, 'failed': 1}
    first_id = report['items'][0]['result']['id']
    await host.state.confirm_member('bot-a', 'group-a', 'member-b', evidence='verified')
    retry = (
        await (
            await request(host, {**base, 'operation': 'bulk_grant', 'members': [{'member': 'member-b'}], 'resources': ['report-r'], 'expires_at': 200})
        ).json()
    )['data']
    assert retry['summary'] == {'success': 1, 'failed': 0}
    sources = (await acl.access_snapshot('bot-a', 'reports'))['grants']
    assert len(sources) == 2 and first_id in [g['id'] for g in sources]
    await request(host, {**base, 'member': 'member-a', 'operation': 'deny', 'function_scope': {'kind': 'all'}})
    await request(host, {**base, 'member': 'member-a', 'operation': 'grant', 'resources': ['report-r']})
    assert (await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r'))['reason'] == 'denied'
    before = await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-b', 'report-r')
    assert before['allowed'] and before['valid_until'] == 200
    acl.clock = lambda: 200
    assert not acl.decision_current('bot-a', before)
    expired = await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-b', 'report-r')
    assert not expired['allowed'] and expired['reason'] == 'expired'
    assert expired['sources'][0]['status'] == 'expired'


async def test_private_function_policy_write_keeps_unrelated_decision_current(access_host):
    import asyncio
    import sqlite3

    host = access_host
    acl = await configure(host, kind='public')
    await acl.declare('bot-a', 'reports', owner='plugin:reports', functions={'export': {'kind': 'public', 'groups': ['group-a']}})
    read = await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r')
    export = await acl.evaluate('bot-a', 'reports', 'export', 'group-a', 'member-a', 'report-r')
    blocker = sqlite3.connect(acl.store.path)
    blocker.execute('BEGIN IMMEDIATE')
    task = asyncio.create_task(acl.manage_access('bot-a', 'reports', 'policy', function='read', policy={'kind': 'explicit', 'groups': ['group-a']}))
    try:
        from socket_platform import until

        await until(lambda: not acl.decision_current('bot-a', read))
        assert acl.decision_current('bot-a', export)
    finally:
        blocker.rollback()
        blocker.close()
        await task
    assert acl.decision_current('bot-a', export)
    assert not acl.decision_current('bot-a', read)


async def test_scoped_sources_survive_reload_and_source_revoke_after_scope_empty(access_host):
    host, acl = access_host, access_host.access
    await configure(host)
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    base = {'consumer': 'reports', 'function': 'read', 'group': 'group-a', 'member': 'member-a'}
    await request(host, {**base, 'operation': 'function_group', 'id': 'future', 'functions': []})
    response = await request(
        host, {**base, 'operation': 'grant', 'function_scope': {'kind': 'group', 'id': 'future'}, 'resource_scope': {'kind': 'all'}, 'mode': 'continuous'}
    )
    grant = (await response.json())['data']
    assert response.status == 200 and grant['functions'] == []
    await request(host, {**base, 'operation': 'function_group', 'id': 'future', 'functions': ['read']})
    assert (await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r'))['allowed']
    assert await host.modules.reload('access_control')
    acl = host.modules.get('access_control')
    before = await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r')
    assert before['allowed'] and before['sources'][0]['id'] == grant['id']
    await request(host, {**base, 'operation': 'function_group', 'id': 'future', 'functions': []})
    assert not acl.decision_current('bot-a', before)
    assert (await request(host, {**base, 'operation': 'revoke', 'source': grant['id']})).status == 200
    assert not (await acl.access_snapshot('bot-a', 'reports'))['grants']


async def test_shortest_required_capability_expiry_and_scoped_deny_source(access_host):
    host = access_host
    acl = await configure(host)
    await acl.declare('bot-a', 'reports', owner='plugin:reports', functions={'export': {'kind': 'explicit', 'groups': ['group-a']}})
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    acl.clock = lambda: 100
    base = {'consumer': 'reports', 'function': 'read', 'group': 'group-a', 'member': 'member-a'}
    await request(host, {**base, 'operation': 'grant', 'function_scope': {'kind': 'all'}, 'resources': [], 'expires_at': 300})
    await request(host, {**base, 'operation': 'grant', 'function_scope': {'kind': 'all'}, 'resources': ['report-r'], 'invoke': False, 'expires_at': 150})
    decision = await acl.evaluate('bot-a', 'reports', 'export', 'group-a', 'member-a', 'report-r')
    assert decision['allowed'] and decision['valid_until'] == 150
    denied = await acl.evaluate('bot-a', 'reports', 'export', 'group-a', 'member-a', 'report-r', framework_allowed=False)
    assert not denied['allowed'] and denied['reason'] == 'framework_denied'
    await request(host, {**base, 'operation': 'deny', 'function_scope': {'kind': 'all'}})
    assert not acl.decision_current('bot-a', decision)
    denies = (await acl.access_snapshot('bot-a', 'reports'))['denies']
    assert (await acl.evaluate('bot-a', 'reports', 'export', 'group-a', 'member-a', 'report-r'))['reason'] == 'denied'
    await request(host, {**base, 'operation': 'allow', 'source': denies[0]['id']})
    after = await acl.evaluate('bot-a', 'reports', 'export', 'group-a', 'member-a', 'report-r')
    assert after['allowed']
    acl.clock = lambda: 150
    assert not acl.decision_current('bot-a', after)
    assert not (await acl.evaluate('bot-a', 'reports', 'export', 'group-a', 'member-a', 'report-r'))['allowed']


async def test_resource_group_changes_and_bot_all_scope_isolation(access_host):
    host = access_host
    acl = await configure(host)
    await configure(host, bot='bot-b')
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    await host.state.confirm_member('bot-b', 'group-a', 'member-a', evidence='verified')
    await acl.declare_resources('bot-a', 'reports', [{'id': 'report-r', 'groups': ['daily']}])
    await acl.manage_access(
        'bot-a',
        'reports',
        'grant',
        function='read',
        group='group-a',
        member='member-a',
        function_scope={'kind': 'all'},
        resource_scope={'kind': 'group', 'id': 'daily'},
        mode='continuous',
    )
    assert (await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r'))['allowed']
    assert not (await acl.evaluate('bot-b', 'reports', 'read', 'group-a', 'member-a', 'report-r'))['allowed']
    await acl.declare_resources('bot-a', 'reports', [{'id': 'report-r', 'groups': ['weekly']}])
    assert not (await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r'))['allowed']


async def test_owner_command_updates_same_source_expiry_and_hides_member_details(access_host):
    import json

    host = access_host
    acl = await configure(host)
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    source = await acl.manage_access('bot-a', 'reports', 'grant', function='read', group='group-a', member='member-a', resources=['report-r'])
    operation = {
        'consumer': 'reports',
        'operation': 'expiry',
        'function': 'read',
        'group': 'group-a',
        'member': 'member-a',
        'source': source['id'],
        'expires_at': 100,
    }
    host.sender.messages.clear()
    await host.event('授权管理 ' + json.dumps(operation), user='ordinary')
    assert not host.sender.messages
    assert (await acl.access_snapshot('bot-a', 'reports'))['grants'][0]['expires_at'] is None
    await host.event('授权管理 ' + json.dumps(operation))
    state = (await (await request(host)).json())['data']
    assert state['grants'][0]['id'] == source['id'] and state['grants'][0]['expires_at'] == 100


async def test_continuous_invoke_only_source_never_requires_view_resources(access_host):
    host = access_host
    acl = await configure(host)
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    source = await acl.manage_access('bot-a', 'reports', 'grant', function='read', group='group-a', member='member-a', resources=[], mode='continuous')
    invoke = await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', action='invoke')
    assert invoke['allowed'] and invoke['sources'][0]['id'] == source['id']
    assert not (await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r'))['allowed']


@pytest.mark.parametrize('scope', [{'kind': 'all'}, {'kind': 'group', 'id': 'large'}])
async def test_all_resources_with_501_items_remains_valid_after_reload(access_host, scope):
    host = access_host
    acl = await configure(host)
    await acl.declare_resources('bot-a', 'reports', [{'id': f'resource-{index}', 'groups': ['large']} for index in range(501)])
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    saved = await acl.manage_access('bot-a', 'reports', 'grant', function='read', group='group-a', member='member-a', resource_scope=scope)
    assert len(saved['resources']) == 501
    assert await host.modules.reload('access_control')
    restored = host.modules.get('access_control')
    assert (await restored.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'resource-500'))['allowed']


async def test_function_inventory_bound_rejects_before_mutating_existing_policy(access_host):
    host = access_host
    acl = await configure(host)
    functions = {f'function-{index}': {'kind': 'public', 'groups': ['group-a']} for index in range(99)}
    await acl.declare('bot-a', 'reports', owner='plugin:reports', functions=functions)
    before = await acl.access_snapshot('bot-a', 'reports')
    with pytest.raises(ValueError):
        await acl.declare('bot-a', 'reports', owner='plugin:reports', functions={'overflow': {'kind': 'public'}})
    assert (await acl.access_snapshot('bot-a', 'reports'))['version'] == before['version']
    assert 'overflow' not in (await acl.access_snapshot('bot-a', 'reports'))['functions']
    assert await host.modules.reload('access_control')
    assert len((await host.modules.get('access_control').access_snapshot('bot-a', 'reports'))['functions']) == 100
