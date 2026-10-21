"""Member verification and full-roster proof through independent owner management."""

import pytest


def page(*members, cursor=''):
    return True, {'members': list(members), 'next_cursor': cursor}


def member(native, role='member', *, bot=False):
    return {'member_openid': native, 'username': native, 'member_role': role, 'bot': bot, 'joined_at': '2020-01-01T00:00:00Z'}


def roster_platform(host):
    host.sender.json_results = {
        ('/v2/groups/group-a/bot_state', ''): (True, {'member_openid': 'this-bot', 'member_role': 'admin'}),
        ('/v2/groups/group-a/members', ''): page(member('member-a'), cursor='second'),
        ('/v2/groups/group-a/members', 'second'): page(member('this-bot', 'admin', bot=True), member('other-bot', bot=True)),
        ('/v2/groups/group-a/members/member-a', ''): (True, member('member-a', 'owner')),
    }


async def test_owner_syncs_complete_pages_roles_and_verifies_one_member(group_host):
    host = group_host
    roster_platform(host)
    response = await host.api('POST', 'roster', body={'group': 'group-a'})
    assert response.status == 200
    report = (await response.json())['data']
    assert report['complete'] is True
    assert {m['member'] for m in report['members']} == {'member-a', 'this-bot', 'other-bot'}
    assert report['self_member'] == 'this-bot'
    state = await host.state.snapshot('bot-a')
    assert state['groups'][0]['roster']['complete']
    listed = await host.http.get('/api/ext/group-state/members?bot=bot-a&group=group-a&limit=2', headers={'Cookie': 'elaina_session=group-test'})
    data = (await listed.json())['data']
    assert len(data['members']) == 2 and data['next_cursor']
    second = await host.http.get(
        '/api/ext/group-state/members',
        params={
            'bot': 'bot-a',
            'group': 'group-a',
            'limit': '2',
            'cursor': data['next_cursor'],
            'version': str(data['version']),
            'generation': data['generation'],
        },
        headers={'Cookie': 'elaina_session=group-test'},
    )
    assert len((await second.json())['data']['members']) == 1
    response = await host.api('POST', 'member', body={'group': 'group-a', 'member': 'member-a'})
    verified = (await response.json())['data']
    assert verified['member_role'] == 'owner' and verified['source'] == 'platform_member'
    assert not (await host.state.snapshot('bot-a'))['groups'][0]['roster']['complete']
    assert (await host.state.snapshot('bot-b'))['groups'] == []
    await host.event('群状态 名册 group-a')
    assert 'other-bot' in host.sender.messages[-1][2]


@pytest.mark.parametrize(
    'failure', ['missing_cursor', 'duplicate', 'repeated_cursor', 'interrupted', 'missing_role', 'missing_self', 'missing_known', 'changed']
)
async def test_incomplete_or_changed_pages_cannot_prove_complete(group_host, failure):
    host = group_host
    roster_platform(host)
    if failure == 'missing_cursor':
        host.sender.json_results[('/v2/groups/group-a/members', 'second')] = (True, {'members': [member('this-bot', 'admin', bot=True)]})
    elif failure == 'duplicate':
        host.sender.json_results[('/v2/groups/group-a/members', 'second')] = page(member('member-a'))
    elif failure == 'repeated_cursor':
        host.sender.json_results[('/v2/groups/group-a/members', 'second')] = page(member('this-bot', 'admin', bot=True), cursor='second')
    elif failure == 'interrupted':
        host.sender.json_results[('/v2/groups/group-a/members', 'second')] = False, {'code': '11253'}
    elif failure == 'missing_role':
        host.sender.json_results[('/v2/groups/group-a/members', '')] = page({'member_openid': 'member-a'}, cursor='second')
    elif failure == 'missing_self':
        host.sender.json_results[('/v2/groups/group-a/members', 'second')] = page(member('other-bot', bot=True))
    elif failure == 'missing_known':
        await host.event('普通聊天', user='known-member', group='group-a')
    elif failure == 'changed':

        async def change(path, cursor):
            if cursor:
                await host.event('普通聊天', user='new-member', group='group-a')

        host.sender.after_json = change
    response = await host.api('POST', 'roster', body={'group': 'group-a'})
    assert response.status == 200
    report = (await response.json())['data']
    assert report['complete'] is False and report['reason']
    assert not (await host.state.snapshot('bot-a'))['groups'][0]['roster']['complete']
    if failure == 'missing_known':
        assert report['reason'] == 'known_member_missing' and report['missing_known_members'] == ['known-member']


async def test_manual_one_member_is_sourced_and_errors_never_mean_left(group_host):
    host = group_host
    response = await host.api('POST', 'confirm-member', body={'group': 'group-a', 'member': 'member-a', 'evidence': '主人核实当前在群', 'member_role': 'admin'})
    assert response.status == 200
    result = (await response.json())['data']
    assert result['source'] == 'owner_confirmed' and result['member_role'] == 'admin'
    assert not (await host.state.snapshot('bot-a'))['groups'][0]['roster']['complete']
    response = await host.api('POST', 'member', body={'group': 'group-a', 'member': 'member-a'})
    result = (await response.json())['data']
    assert result['state'] == 'unknown' and result['reason'] == 'permission_denied'
    assert result['member_role'] == 'admin' and not result['reliable']
    assert result['status'] != 'left'
    assert await host.modules.reload('group_state')
    group = (await host.state.snapshot('bot-a'))['groups'][0]
    assert not group['roster']['complete'] and not group['members']['member-a']['reliable']


async def test_other_bot_and_group_have_independent_snapshots_and_paging(group_host):
    host = group_host
    roster_platform(host)
    await host.api('POST', 'roster', body={'group': 'group-a'})
    host.sender.json_results.update(
        {
            ('/v2/groups/group-b/members', ''): page(member('other-native', 'owner'), member('own-b', 'admin', bot=True)),
            ('/v2/groups/group-b/bot_state', ''): (True, {'member_openid': 'own-b', 'member_role': 'admin'}),
        }
    )
    response = await host.api('POST', 'roster', body={'group': 'group-b'}, bot='bot-b')
    assert (await response.json())['data']['complete']
    assert {m['member'] for m in (await host.state.snapshot('bot-b'))['groups'][0]['roster']['members']} == {'own-b', 'other-native'}
    first = await host.state.members('bot-a', 'group-a', limit=1)
    await host.api('POST', 'member', body={'group': 'group-a', 'member': 'member-a'})
    with pytest.raises(ValueError, match='快照已变化'):
        await host.state.members('bot-a', 'group-a', cursor=first['next_cursor'], version=first['version'], generation=first['generation'])
    assert (await host.state.snapshot('bot-b'))['groups'][0]['roster']['complete']


@pytest.mark.parametrize('kind', ['empty', 'self_role_conflict', 'invalid_member', 'missing_own_identity'])
async def test_missing_or_conflicting_bot_and_member_fields_fail_closed(group_host, kind):
    host = group_host
    roster_platform(host)
    if kind == 'empty':
        host.sender.json_results[('/v2/groups/group-a/members', '')] = page()
    elif kind == 'self_role_conflict':
        host.sender.json_results[('/v2/groups/group-a/bot_state', '')] = (True, {'member_openid': 'this-bot', 'member_role': 'member'})
    elif kind == 'invalid_member':
        host.sender.json_results[('/v2/groups/group-a/members', '')] = page({'member_openid': [], 'member_role': 'admin'})
    else:
        host.sender.json_results[('/v2/groups/group-a/bot_state', '')] = (True, {'member_role': 'admin'})
    response = await host.api('POST', 'roster', body={'group': 'group-a'})
    assert response.status == 200
    assert not (await response.json())['data']['complete']


async def test_failed_durable_update_keeps_roster_unproven_until_retry(group_host):
    import sqlite3

    host = group_host
    roster_platform(host)
    path = host.modules.get_context('group_state').get_data_path('group-state.db')
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TRIGGER fail_complete BEFORE INSERT ON extension_records
            WHEN json_extract(NEW.payload, '$.groups."group-a".roster.complete') = 1
            BEGIN SELECT RAISE(ABORT, 'fake persistence failure'); END""")
    response = await host.api('POST', 'roster', body={'group': 'group-a'})
    assert response.status == 503
    failed = (await (await host.api()).json())['data']['groups'][0]
    assert not failed['roster']['complete'] and failed['roster']['reason'] == 'persistence_failed'
    assert not any(member['reliable'] for member in failed['members'].values())
    with sqlite3.connect(path) as connection:
        connection.execute('DROP TRIGGER fail_complete')
    retried = (await (await host.api('POST', 'roster', body={'group': 'group-a'})).json())['data']
    assert retried['complete'] and retried['version'] > failed['version']


async def test_pending_durable_update_does_not_publish_complete_roster(group_host):
    import asyncio
    import sqlite3

    host = group_host
    roster_platform(host)
    path = host.modules.get_context('group_state').get_data_path('group-state.db')
    blocker = sqlite3.connect(path)
    entered = asyncio.Event()

    async def lock_final_write(path, cursor):
        if path.endswith('/bot_state'):
            blocker.execute('BEGIN IMMEDIATE')
            entered.set()

    host.sender.after_json = lock_final_write
    operation = asyncio.create_task(host.api('POST', 'roster', body={'group': 'group-a'}))
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        snapshot = (await (await host.api()).json())['data']
        assert not snapshot['groups'][0]['roster']['complete']
        with pytest.raises(ValueError, match='持久化中'):
            await host.state.members('bot-a', 'group-a', limit=1)
    finally:
        blocker.commit()
        blocker.close()
        response = await operation
    assert (await response.json())['data']['complete']
    assert (await host.state.snapshot('bot-a'))['groups'][0]['roster']['complete']


async def test_bot_role_refresh_invalidates_previous_complete_roster(group_host):
    host = group_host
    roster_platform(host)
    await host.api('POST', 'roster', body={'group': 'group-a'})
    host.sender.response = ({'member_role': 'member', 'recv_msg_setting': 'only_mention', 'allow_proactive_msg': False}, None)
    response = await host.api('POST', 'refresh', body={'group': 'group-a'})
    assert not (await response.json())['data']['roster']['complete']
    snapshot = await host.state.snapshot('bot-a')
    assert not snapshot['groups'][0]['roster']['complete']


async def test_stale_bot_refresh_cannot_publish_another_queries_uncommitted_proof(group_host):
    import asyncio
    import sqlite3

    host = group_host
    roster_platform(host)
    entered, release, writing = asyncio.Event(), asyncio.Event(), asyncio.Event()
    blocker = sqlite3.connect(host.modules.get_context('group_state').get_data_path('group-state.db'))

    async def hold_refresh():
        entered.set()
        await release.wait()

    async def hold_roster(path, cursor):
        if path.endswith('/bot_state'):
            blocker.execute('BEGIN IMMEDIATE')
            writing.set()

    host.sender.after_query = hold_refresh
    host.sender.after_json = hold_roster
    refresh = asyncio.create_task(host.api('POST', 'refresh', body={'group': 'group-a'}))
    roster = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        roster = asyncio.create_task(host.api('POST', 'roster', body={'group': 'group-a'}))
        await asyncio.wait_for(writing.wait(), timeout=3)
        release.set()
        response = await refresh
        result = (await response.json())['data']
        assert result['reason'] == 'state_changed' and not result['roster']['complete']
    finally:
        release.set()
        blocker.commit()
        blocker.close()
        await refresh
        if roster:
            await roster


@pytest.mark.parametrize('path', ['roster', 'member', 'confirm-member'])
async def test_unload_during_final_write_cannot_publish_complete_or_reliable_facts(group_host, path):
    import asyncio
    import sqlite3
    import threading
    import time

    host = group_host
    roster_platform(host)
    await host.state.snapshot('bot-a')
    blocker = sqlite3.connect(host.modules.get_context('group_state').get_data_path('group-state.db'), check_same_thread=False)
    entered = asyncio.Event()

    async def lock_final_write(path, cursor):
        if path.endswith(('/bot_state', '/members/member-a')):
            blocker.execute('BEGIN IMMEDIATE')
            entered.set()

    host.sender.after_json = lock_final_write
    body = {'group': 'group-a'} if path == 'roster' else {'group': 'group-a', 'member': 'member-a'}
    if path == 'confirm-member':
        body.update(evidence='fake owner checked', member_role='member')
        blocker.execute('BEGIN IMMEDIATE')
        entered.set()
    operation = asyncio.create_task(host.api('POST', path, body=body))
    await asyncio.wait_for(entered.wait(), 3)
    await asyncio.sleep(0.05)
    # Release from another thread: the close below waits for the same SQLite worker.
    release = threading.Thread(target=lambda: (time.sleep(0.2), blocker.commit(), blocker.close()))
    release.start()
    await host.modules.disable('group_state', _persist=False)
    response = await operation
    release.join()
    assert response.status == 503


@pytest.mark.parametrize('method', ['snapshot', 'members'])
async def test_read_queued_across_unload_returns_no_old_proof(group_host, method):
    import asyncio

    host = group_host
    roster_platform(host)
    old = host.state
    assert (await old.sync_roster('bot-a', 'group-a'))['complete']
    lock = old.load_locks['bot-a']
    await lock.acquire()
    operation = asyncio.create_task(old.snapshot('bot-a') if method == 'snapshot' else old.members('bot-a', 'group-a'))
    await asyncio.sleep(0)
    await host.modules.disable('group_state', _persist=False)
    lock.release()
    with pytest.raises(RuntimeError):
        await operation


@pytest.mark.parametrize('target', [('bot-a', 'group-b'), ('bot-b', 'group-a')])
async def test_member_cursor_continues_only_its_own_bot_and_group(group_host, target):
    host = group_host
    for bot, group in {('bot-a', 'group-a'), target}:
        for n in range(2):
            await host.state.confirm_member(bot, group, f'{bot}-{group}-{n}', evidence='fake owner checked')
    first = await host.state.members('bot-a', 'group-a', limit=1)
    other = await host.state.members(*target, limit=1)
    assert other['version'] == first['version'] and other['generation'] == first['generation']
    with pytest.raises(ValueError, match='快照已变化'):
        await host.state.members(*target, cursor=first['next_cursor'], version=first['version'], generation=first['generation'], limit=1)
    with pytest.raises(ValueError, match='cursor'):
        await host.state.members('bot-a', 'group-a', cursor='1', version=first['version'], generation=first['generation'])
    second = await host.state.members('bot-a', 'group-a', cursor=first['next_cursor'], version=first['version'], generation=first['generation'], limit=1)
    assert [m['member'] for m in second['members']] == ['bot-a-group-a-1']


async def test_cancelled_roster_query_ends_without_in_progress_markers(group_host):
    import asyncio

    host = group_host
    roster_platform(host)
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='fake owner checked')
    entered = asyncio.Event()

    async def hold(path, cursor):
        entered.set()
        await asyncio.Event().wait()

    host.sender.after_json = hold
    operation = asyncio.create_task(host.state.sync_roster('bot-a', 'group-a'))
    await asyncio.wait_for(entered.wait(), 3)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    group = (await host.state.snapshot('bot-a'))['groups'][0]
    assert not group['roster']['complete'] and group['roster']['reason'] == 'roster_cancelled'
    assert group['members']['member-a']['reason'] == 'roster_cancelled' and not group['members']['member-a']['reliable']
