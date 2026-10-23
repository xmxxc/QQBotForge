"""Owner migration of legacy plugin identities after the module already observed C2C authors (#69)."""

import asyncio
import json
import sqlite3

from account_host import account_host as account_host  # noqa: F401
from test_access_control import access_host as access_host  # noqa: F401
from test_bindings import bind

from core.base import tasks
from core.message.event import Event


def service(host):
    return host.plugins._plugins['account_sharing'].module._instance


async def c2c(host, text, user, union=None):
    """A real native C2C message through the application entry (observes the author)."""
    author = {'id': user, 'user_openid': user, **({'union_openid': union} if union else {})}
    event = Event.from_websocket('bot-a', {'t': 'C2C_MESSAGE_CREATE', 'd': {'id': 'fake-message', 'content': text, 'author': author}})
    before = set(tasks._background_tasks)
    host.application._prepare_event(event)
    await host.application._on_event(event)
    for _ in range(20):
        started = set(tasks._background_tasks) - before
        if not started:
            break
        await asyncio.gather(*started, return_exceptions=True)
        before |= started


def legacy_private(private_id, union):
    # Shape written by the old plugin (identities.observe_private).
    return {'private_id': private_id, 'identity_observed': True, 'source': 'observed_c2c', 'current_union_id': union, 'union_ids': [union]}


async def seed_legacy(host, *, binding_private='private-a'):
    store = service(host).store
    await store.put('bot-a', 'private_identities', 'owner-a', legacy_private('owner-a', 'union-owner-old'))
    await store.put('bot-a', 'private_identities', binding_private, legacy_private(binding_private, 'union-member-old'))
    # Old bindings had no id; their key is the identity key.
    await store.put(
        'bot-a',
        'bindings',
        json.dumps(['group-a', 'member-a']),
        {
            'group': 'group-a',
            'member': 'member-a',
            'private_id': binding_private,
            'source': 'owner_confirmed',
            'confirmed_by': 'owner-a',
            'confirmed_at': 100,
            'evidence': 'legacy owner check',
        },
    )
    await store.put(
        'bot-a',
        'binding_requests',
        'legacy-request',
        {
            'id': 'legacy-request',
            'group': 'group-a',
            'member': 'member-a',
            'private_id': binding_private,
            'status': 'confirmed',
            'source': 'observed_c2c_request',
            'created_at': 90,
        },
    )


async def migrate_by_owner_private_chat(host):
    host.sender.messages.clear()
    await c2c(host, '账户管理 迁移', 'owner-a')
    replies = [m for m in host.sender.messages if m[0] == 'bot-a' and m[1] == 'owner-a']
    pushes = [m[2] for m in host.sender.messages if m[0] == 'push' and m[1] == 'owner-a']
    return json.loads(replies[-1][2]), pushes


async def last_operation(host):
    results = await service(host).store.all('bot-a', 'operations')
    return max((r for r in results if r['kind'] == 'migration'), key=lambda r: r['created_at'])


async def admin(host, data):
    response = await host.http.post('/api/ext/access-control/', params={'bot': 'bot-a'}, json=data, headers={'Cookie': 'elaina_session=group-test'})
    return response.status


async def test_observed_c2c_identity_merges_through_owner_private_migration(account_host):
    host = account_host
    await seed_legacy(host)
    # The module observes the member before the owner migrates from private chat
    # (which itself observes the owner first).
    await c2c(host, '你好', 'private-a', union='union-member-new')
    acl = host.access
    observed = {r['private_id']: r for r in (await acl.binding_snapshot('bot-a'))['private_identities']}
    assert observed['private-a']['identity_type'] == 'c2c_openid'

    report, pushes = await migrate_by_owner_private_chat(host)
    assert 'error' not in report['bindings'], report
    assert report['bindings']['status'] == 'imported', report
    assert report['bindings']['imported'] == {'private_identities': 0, 'bindings': 1, 'requests': 1}
    assert report['bindings']['existing'] == {'private_identities': 2, 'bindings': 0, 'requests': 0}
    assert report['authorization']['status'] == 'unchanged' and report['rules']['status'] == 'checked'
    assert report['status'] == 'imported'
    snapshot = await acl.binding_snapshot('bot-a')
    private = {r['private_id']: r for r in snapshot['private_identities']}
    # The newer observation is kept; legacy union aliases are not merged into it.
    assert private['private-a'] == observed['private-a'] and private['private-a']['union_id'] == 'union-member-new'
    assert 'union_ids' not in private['owner-a'] and private['owner-a']['identity_type'] == 'c2c_openid'
    assert [(b['group'], b['member'], b['private_id']) for b in snapshot['bindings']] == [('group-a', 'member-a', 'private-a')]
    assert (await acl.binding_snapshot('bot-b'))['private_identities'] == []
    operation = await last_operation(host)
    assert operation['authorization'] == 'unchanged' and operation['binding'] == 'imported' and operation['rules'] == 'checked'
    assert operation['migration'] == 'imported' and operation['report'] == report and operation['notification'] == 'sent'
    assert '授权=unchanged，绑定=imported' in pushes[-1]

    # Re-running is compatible and writes nothing.
    version = snapshot['version']
    again, pushes = await migrate_by_owner_private_chat(host)
    assert again['bindings']['status'] == 'unchanged' and again['status'] == 'unchanged', again
    assert again['bindings']['existing'] == {'private_identities': 2, 'bindings': 1, 'requests': 1}
    assert (await acl.binding_snapshot('bot-a'))['version'] == version
    assert '绑定=unchanged' in pushes[-1]


async def test_owner_confirmed_binding_in_module_is_kept_over_legacy_record(account_host):
    host = account_host
    await seed_legacy(host)
    current = await bind(host, 'group-a', 'member-a', 'private-a')
    report, _ = await migrate_by_owner_private_chat(host)
    assert report['bindings']['status'] == 'imported' and report['bindings']['existing']['bindings'] == 1, report
    assert (await host.access.binding_snapshot('bot-a'))['bindings'] == [current]


async def test_contradicting_binding_is_rejected_and_reported_as_failed(account_host):
    host = account_host
    # Legacy says member-a is private-a; the module already confirmed member-a as private-b.
    await seed_legacy(host)
    current = await bind(host, 'group-a', 'member-a', 'private-b')
    await c2c(host, '你好', 'private-a')
    await c2c(host, '你好', 'owner-a')
    before = await host.access.binding_snapshot('bot-a')
    report, pushes = await migrate_by_owner_private_chat(host)
    assert '绑定=imported' not in pushes[-1]
    assert report['bindings'] == {'status': 'failed', 'error': '旧绑定与现存关联冲突'}, report
    assert report['authorization']['status'] == 'unchanged' and report['status'] == 'partial'
    after = await host.access.binding_snapshot('bot-a')
    assert after == before and after['bindings'] == [current]
    operation = await last_operation(host)
    assert operation['binding'] == 'failed' and operation['migration'] == 'partial'
    assert '绑定=failed' in pushes[-1]


async def test_same_private_identity_for_another_member_in_group_is_rejected(account_host):
    host = account_host
    await seed_legacy(host, binding_private='private-b')
    await bind(host, 'group-a', 'member-b', 'private-b')
    report, _ = await migrate_by_owner_private_chat(host)
    assert report['bindings'] == {'status': 'failed', 'error': '同群私聊身份不能关联多个成员'}, report


async def test_unobserved_legacy_identity_and_revoked_link_are_not_imported(account_host):
    host = account_host
    await seed_legacy(host)
    store = service(host).store
    await store.put('bot-a', 'private_identities', 'forged', {'private_id': 'forged', 'source': 'observed_c2c'})
    report, _ = await migrate_by_owner_private_chat(host)
    assert report['bindings'] == {'status': 'failed', 'error': '迁移私聊身份须有原观察来源'}
    assert (await host.access.binding_snapshot('bot-a'))['bindings'] == []
    await store.delete('bot-a', 'private_identities', 'forged')
    assert (await migrate_by_owner_private_chat(host))[0]['bindings']['status'] == 'imported'
    assert await admin(host, {'operation': 'binding_revoke', 'group': 'group-a', 'member': 'member-a'}) == 200
    report, _ = await migrate_by_owner_private_chat(host)
    assert report['bindings']['status'] == 'failed' and '已撤销' in report['bindings']['error']
    assert (await host.access.binding_snapshot('bot-a'))['bindings'] == []


async def test_identity_write_failure_is_failed_while_authorization_reports_its_own_result(account_host):
    host = account_host
    account = await host.create(name='旧账户')
    acl = host.access
    legacy_grant = {
        'id': 'legacy-source',
        'group': 'group-a',
        'member': 'member-a',
        'cycle': 'old',
        'function': 'account_sharing',
        'resources': [account['id']],
        'resource_namespace': 'account_sharing',
    }
    await acl.store.put('bot-a', 'access', 'state', {'version': 1, 'whitelist': {}, 'members': {}, 'grants': [legacy_grant], 'denies': [], 'revocations': []})
    acl.states.pop('bot-a', None)
    await seed_legacy(host)
    await c2c(host, '你好', 'private-a')
    with sqlite3.connect(acl.store.path) as db:
        db.execute(
            "CREATE TRIGGER fail_identity BEFORE UPDATE ON extension_records WHEN NEW.collection='identity_state' "
            "AND NEW.bot='bot-a' BEGIN SELECT RAISE(ABORT, 'test failure'); END"
        )
    status, data = await host.account_api('POST', 'migration', {})
    assert status == 200 and data['success'] is True, data
    report = data['data']
    assert report['authorization']['status'] == 'imported' and report['authorization']['grants'] == 1
    assert report['bindings'] == {'status': 'failed', 'error': '身份资料写入失败'}
    assert report['status'] == 'partial'
    operation = await last_operation(host)
    assert operation['authorization'] == 'imported' and operation['binding'] == 'failed' and operation['migration'] == 'partial'


async def test_all_failed_migration_is_not_reported_as_success(account_host):
    host = account_host
    await seed_legacy(host, binding_private='never-observed')
    await service(host).store.delete('bot-a', 'private_identities', 'never-observed')
    acl = host.access
    bad = {'id': 'bad', 'member': 'member-a', 'function': 'account_sharing'}  # no group
    await acl.store.put('bot-a', 'access', 'state', {'version': 1, 'whitelist': {}, 'members': {}, 'grants': [bad], 'denies': [], 'revocations': []})
    acl.states.pop('bot-a', None)
    status, data = await host.account_api('POST', 'migration', {})
    assert status == 422 and data['success'] is False, data
    report = data['data']
    assert report['authorization'] == {'status': 'failed', 'error': '旧资料结构无效'}
    assert report['bindings'] == {'status': 'failed', 'error': '旧绑定缺少已观察私聊身份'}
    assert report['status'] == 'failed'
    assert (await last_operation(host))['migration'] == 'failed'


async def test_cancelled_migration_waiting_for_identity_lock_records_no_result(account_host):
    host = account_host
    await seed_legacy(host)
    await c2c(host, '你好', 'private-a')
    acl = host.access
    lock = acl.bindings.locks['bot-a']
    await lock.acquire()
    task = asyncio.create_task(service(host).migrate('bot-a'))
    async with asyncio.timeout(5):
        while not lock._waiters:
            await asyncio.sleep(0)
    task.cancel()
    lock.release()
    try:
        await task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError('cancelled migration completed')
    assert [r for r in await service(host).store.all('bot-a', 'operations') if r['kind'] == 'migration'] == []
    assert (await acl.binding_snapshot('bot-a'))['bindings'] == []
    report, _ = await migrate_by_owner_private_chat(host)
    assert report['bindings']['status'] == 'imported' and report['status'] == 'imported'
