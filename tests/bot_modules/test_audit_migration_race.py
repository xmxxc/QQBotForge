"""Deterministically serialize migration against management using real temporary SQLite."""

import asyncio
import sqlite3

import pytest
from account_host import account_host as account_host  # noqa: F401
from test_access_control import access_host as access_host  # noqa: F401


async def prepared(h):
    a = await h.create()
    await h.account_api('POST', 'access', {'operation': 'whitelist', 'group': 'group-a', 'enabled': True})
    await h.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='checked', member_role='member')
    status, result = await h.account_api(
        'POST',
        'access',
        {
            'operation': 'grant',
            'group': 'group-a',
            'member': 'member-a',
            'resources': [a['id']],
            'expires_at': 2000000000,
        },
    )
    assert status == 200, result
    return a, result['data']['id']


async def queued(lock, count):
    async with asyncio.timeout(5):
        while len(lock._waiters or ()) < count:
            await asyncio.sleep(0)


@pytest.mark.parametrize('migration_first', [False, True])
async def test_import_does_not_restore_concurrently_revoked_source(account_host, migration_first):
    h = account_host
    a, source = await prepared(h)
    acl = h.access
    lock = acl.scope_locks['bot-a', 'account_sharing']
    await lock.acquire()

    async def revoke():
        return await acl.manage_access('bot-a', 'account_sharing', 'revoke', function='account_sharing', group='group-a', member='member-a')

    async def migrate():
        return await acl.import_legacy('bot-a', 'account_sharing', 'account_sharing')

    first = asyncio.create_task(migrate() if migration_first else revoke())
    await queued(lock, 1)
    second = asyncio.create_task(revoke() if migration_first else migrate())
    await queued(lock, 2)
    lock.release()
    await asyncio.gather(first, second)
    await asyncio.gather(migrate(), migrate())
    state = await acl.access_snapshot('bot-a', 'account_sharing')
    assert state['grants'] == []
    assert [r['id'] for r in state['revocations']] == [source]
    assert state['revocations'][0]['expires_at'] == 2000000000
    assert not (await acl.evaluate('bot-a', 'account_sharing', 'account_sharing', 'group-a', 'member-a', a['id']))['allowed']
    # A fresh instance reads the committed row, not the process cache.
    fresh = type(acl)(acl.store.path)
    await fresh.initialize()
    try:
        await fresh.import_legacy('bot-a', 'account_sharing', 'account_sharing')
        assert (await fresh.access_snapshot('bot-a', 'account_sharing'))['grants'] == []
    finally:
        fresh.close()


async def test_cancelled_waiting_import_and_failed_write_are_not_success(account_host):
    h = account_host
    _, source = await prepared(h)
    acl = h.access
    lock = acl.scope_locks['bot-a', 'account_sharing']
    await lock.acquire()
    task = asyncio.create_task(acl.import_legacy('bot-a', 'account_sharing', 'account_sharing'))
    await queued(lock, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    lock.release()
    assert (await acl.access_snapshot('bot-a', 'account_sharing'))['grants'][0]['id'] == source
    with sqlite3.connect(acl.store.path) as db:
        db.execute(
            "CREATE TRIGGER fail_import BEFORE UPDATE ON extension_records WHEN NEW.collection='access_consumers' AND NEW.bot='bot-a' BEGIN SELECT RAISE(ABORT, 'test failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        await acl.import_legacy('bot-a', 'account_sharing', 'account_sharing')
    with pytest.raises(RuntimeError):
        await acl.import_legacy('bot-a', 'account_sharing', 'account_sharing')
    with sqlite3.connect(acl.store.path) as db:
        db.execute('DROP TRIGGER fail_import')
    fresh = type(acl)(acl.store.path)
    await fresh.initialize()
    try:
        report = await fresh.import_legacy('bot-a', 'account_sharing', 'account_sharing')
        assert report['grants'] == 0
        assert (await fresh.access_snapshot('bot-a', 'account_sharing'))['grants'][0]['id'] == source
    finally:
        fresh.close()


async def test_cancel_after_sql_commit_is_unknown_and_reload_can_retry(account_host, monkeypatch):
    h = account_host
    _, source = await prepared(h)
    acl = h.access
    await acl.manage_access('bot-a', 'account_sharing', 'revoke', function='account_sharing', group='group-a', member='member-a')
    committed = asyncio.Event()
    original = acl.store.put

    async def put(*args, **kwargs):
        await original(*args, **kwargs)
        committed.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(acl.store, 'put', put)
    task = asyncio.create_task(acl.import_legacy('bot-a', 'account_sharing', 'account_sharing'))
    await asyncio.wait_for(committed.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(RuntimeError):
        await acl.access_snapshot('bot-a', 'account_sharing')
    fresh = type(acl)(acl.store.path)
    await fresh.initialize()
    try:
        await fresh.import_legacy('bot-a', 'account_sharing', 'account_sharing')
        state = await fresh.access_snapshot('bot-a', 'account_sharing')
        assert not state['grants'] and [r['id'] for r in state['revocations']] == [source]
    finally:
        fresh.close()
