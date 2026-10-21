"""Persistence failure boundaries through public group-state operations and real SQLite."""

import asyncio
import sqlite3

import pytest
from test_group_roster import roster_platform


async def wait_until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), 3)


async def complete_roster(host):
    roster_platform(host)
    assert (await host.state.snapshot('bot-b'))['groups'] == []
    assert (await (await host.api('POST', 'roster', body={'group': 'group-a'})).json())['data']['complete']
    return await host.state.members('bot-a', 'group-a', limit=1)


async def assert_retired(host, earlier):
    group = next(g for g in (await host.state.snapshot('bot-a'))['groups'] if g['group'] == 'group-a')
    assert not group['roster']['complete']
    assert not group['members']['member-a']['reliable']
    assert group['members']['member-a']['member_role'] == 'member'
    assert group['version'] > earlier['version']
    with pytest.raises(ValueError):
        await host.state.members('bot-a', 'group-a', cursor=earlier['next_cursor'], version=earlier['version'], generation=earlier['generation'])
    return group


async def test_failed_confirmation_retires_old_proof_until_new_roster(group_host):
    host = group_host
    earlier = await complete_roster(host)
    path = host.modules.get_context('group_state').get_data_path('group-state.db')
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER fail_confirmation BEFORE INSERT ON extension_records BEGIN SELECT RAISE(ABORT, 'fake disk failure'); END")
    try:
        response = await host.api('POST', 'confirm-member', body={'group': 'group-a', 'member': 'member-a', 'evidence': '主人核对角色变化', 'member_role': 'owner'})
        assert response.status == 503
        group = await assert_retired(host, earlier)
        assert group['members']['member-a']['reason'] == 'persistence_failed'
    finally:
        with sqlite3.connect(path) as db:
            db.execute('DROP TRIGGER fail_confirmation')
    retry = await host.api('POST', 'confirm-member', body={'group': 'group-a', 'member': 'member-a', 'evidence': '主人重新核对', 'member_role': 'owner'})
    result = (await retry.json())['data']
    assert result['reliable'] and result['member_role'] == 'owner'
    assert not (await host.state.snapshot('bot-a'))['groups'][0]['roster']['complete']
    assert (await host.state.snapshot('bot-b'))['groups'] == []
    assert (await host.state.sync_roster('bot-a', 'group-a'))['complete']


@pytest.mark.parametrize('cancelled', [False, True])
async def test_confirmation_wait_and_cancel_never_publish_old_proof(group_host, cancelled):
    host = group_host
    earlier = await complete_roster(host)
    blocker = sqlite3.connect(host.modules.get_context('group_state').get_data_path('group-state.db'))
    blocker.execute('BEGIN IMMEDIATE')
    operation = asyncio.create_task(host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='主人核对角色', member_role='owner'))
    try:
        await wait_until(lambda: host.state.pending_writes.get('bot-a'))
        await assert_retired(host, earlier)
        if cancelled:
            operation.cancel()
            await asyncio.sleep(0.01)
            assert not operation.done()  # SQLite's real worker still owns the write order.
    finally:
        blocker.commit()
        blocker.close()
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await operation
        else:
            assert (await operation)['reliable']
    if cancelled:
        group = await assert_retired(host, earlier)
        assert group['members']['member-a']['status'] == 'historical'
        assert group['members']['member-a']['reason'] == 'confirmation_cancelled'
        assert await host.modules.reload('group_state')
        restored = (await host.state.snapshot('bot-a'))['groups'][0]
        assert not restored['roster']['complete'] and not restored['members']['member-a']['reliable']


@pytest.mark.parametrize('operation', ['refresh', 'roster', 'member'])
async def test_cancelling_initial_queued_write_ends_current_operation(group_host, operation):
    host = group_host
    earlier = await complete_roster(host)
    blocker = sqlite3.connect(host.modules.get_context('group_state').get_data_path('group-state.db'))
    blocker.execute('BEGIN IMMEDIATE')
    first = asyncio.create_task(host.state.refresh('bot-a', 'group-blocker'))
    second = None
    try:
        await wait_until(lambda: host.state.pending_writes.get('bot-a'))
        method = {'refresh': host.state.refresh, 'roster': host.state.sync_roster, 'member': host.state.verify_member}[operation]
        args = ('bot-a', 'group-a', 'member-a') if operation == 'member' else ('bot-a', 'group-a')
        second = asyncio.create_task(method(*args))
        await wait_until(lambda: host.state.pending_writes.get('bot-a', 0) >= 2)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        group = next(g for g in (await host.state.snapshot('bot-a'))['groups'] if g['group'] == 'group-a')
        assert not group['roster']['complete'] and group['version'] > earlier['version']
        if operation == 'refresh':
            assert group['status'] == 'historical' and group['reason'] == 'refresh_cancelled'
        elif operation == 'roster':
            assert group['roster']['reason'] == 'roster_cancelled'
            assert group['members']['member-a']['reason'] == 'roster_cancelled'
        else:
            record = group['members']['member-a']
            assert record['status'] == 'historical' and record['reason'] == 'verification_cancelled'
        assert (await host.state.snapshot('bot-b'))['groups'] == []
    finally:
        blocker.commit()
        blocker.close()
        await first
        if second is not None and not second.done():
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)


async def test_cancelled_confirmation_preserves_newer_same_group_observation(group_host):
    host = group_host
    earlier = await complete_roster(host)
    blocker = sqlite3.connect(host.modules.get_context('group_state').get_data_path('group-state.db'))
    blocker.execute('BEGIN IMMEDIATE')
    operation = asyncio.create_task(host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='主人核对角色', member_role='owner'))
    observation = None
    try:
        await wait_until(lambda: host.state.pending_writes.get('bot-a'))
        observation = asyncio.create_task(host.event('新的群消息', user='member-a', group='group-a'))
        await wait_until(lambda: host.state.pending_writes.get('bot-a', 0) >= 2)
        operation.cancel()
    finally:
        blocker.commit()
        blocker.close()
        with pytest.raises(asyncio.CancelledError):
            await operation
        if observation is not None:
            await observation
    group = await assert_retired(host, earlier)
    assert group['source'] == 'native_event' and group['reason'] == 'observed_event_requires_refresh'
    assert group['members']['member-a']['reason'] == 'requires_verification'
