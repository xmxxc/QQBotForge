"""Version-one fixtures exercise unchanged paths through existing management seams."""

import json
import sqlite3

import pytest

ACCOUNT = {'id': 'legacy-account', 'name': '旧账户', 'login': 'fake-login', 'password': 'fake-secret', 'enabled': True, 'description': '', 'url': '', 'groups': ['旧分组']}
RULE = {'id': 'legacy-rule', 'name': '旧关键词', 'kind': 'resource', 'target': 'legacy-account', 'keywords': [{'text': '旧关键词', 'enabled': True}], 'mode': 'exact', 'priority': None, 'enabled': True}
SOURCE = {'id': 'legacy-source', 'group': 'group-a', 'member': 'member-a', 'cycle': 'legacy-cycle', 'function': 'account_sharing', 'invoke': True, 'expires_at': 4102444800, 'resources': ['legacy-account']}
DENY = {'id': 'legacy-deny', 'group': 'group-a', 'member': 'member-a', 'function': 'account_sharing', 'function_scope': {'kind': 'function', 'id': 'account_sharing'}}
BINDING = {'group': 'group-a', 'member': 'member-a', 'private_id': 'private-a', 'source': 'owner_confirmed', 'confirmed_by': 'owner-a', 'confirmed_at': 1000, 'evidence': '旧主人核对'}
REQUEST = {'id': 'legacy-request', 'group': 'group-b', 'member': 'member-b', 'private_id': 'private-b', 'status': 'pending', 'source': 'observed_c2c_request', 'created_at': 1000}
OPERATION = {'id': 'legacy-operation', 'kind': 'grant', 'authorization': 'saved', 'binding': 'pending', 'notification': 'failed'}
REVOKED = {**SOURCE, 'id': 'revoked-source', 'member': 'departed', 'cycle': 'old-cycle', 'revoked_at': 1001, 'reason': 'member_left'}


def legacy_database(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE extension_records (bot TEXT, collection TEXT, id TEXT, payload TEXT NOT NULL, PRIMARY KEY(bot, collection, id))')
        connection.execute('PRAGMA user_version=1')
        for collection, key, value in records:
            connection.execute('INSERT INTO extension_records VALUES (?,?,?,?)', ('bot-a', collection, key, json.dumps(value, ensure_ascii=False)))


@pytest.fixture
async def legacy_host(tmp_path, host_seed, host):
    return host


@pytest.fixture
def host_seed(tmp_path):
    def key(*parts):
        return json.dumps(parts, ensure_ascii=False)
    state = {
        'schema_version': 5, 'version': 12,
        'whitelist': {key('group-a', 'account_sharing'): True},
        'members': {
            key('group-a', 'member-a'): {'group': 'group-a', 'member': 'member-a', 'cycle': 'legacy-cycle', 'status': 'present', 'verified_at': 1000},
            key('group-a', 'departed'): {'group': 'group-a', 'member': 'departed', 'cycle': 'old-cycle', 'status': 'left', 'left_at': 1001},
        },
        'grants': [SOURCE], 'denies': [DENY], 'revocations': [REVOKED],
        'resources': {'account_sharing': {'legacy-account': {'id': 'legacy-account', 'groups': ['旧分组'], 'enabled': True}}},
    }
    legacy_database(tmp_path / 'access/data/access-control.db', [('access', 'state', state)])
    legacy_database(tmp_path / 'qa/data/self-qa.db', [('qa_rules:account_sharing', RULE['id'], RULE)])
    legacy_database(tmp_path / 'plugin/data/account-sharing.db', [
        ('accounts', ACCOUNT['id'], ACCOUNT), ('bindings', key('group-a', 'member-a'), BINDING),
        ('binding_requests', REQUEST['id'], REQUEST), ('operations', OPERATION['id'], OPERATION),
    ])


async def test_legacy_records_remain_manageable_without_reviving_qualifications(legacy_host):
    host = legacy_host
    assert (await (await host.api('GET', 'accounts')).json())['data'] == [ACCOUNT]
    assert (await (await host.api('GET', 'rules')).json())['data'] == [RULE]
    bindings = (await (await host.api('GET', 'bindings')).json())['data']
    assert bindings == {'requests': [REQUEST], 'bindings': [BINDING]}
    assert (await (await host.api('GET', 'operations')).json())['data'] == [OPERATION]
    tasks = (await (await host.api('GET', 'tasks')).json())['data']
    assert any(t.get('id') == REQUEST['id'] and t['type'] == 'binding_request' for t in tasks)
    assert any(t.get('id') == OPERATION['id'] and t['type'] == 'notification' for t in tasks)
    acl = host.service.access()
    for _ in range(2):
        state = (await (await host.api('GET', 'access')).json())['data']
        assert state['grants'] == [SOURCE]
        assert state['denies'] == [DENY]
        assert state['revocations'] == [REVOKED]
        assert state['members'][json.dumps(['group-a', 'member-a'])]['cycle'] == 'legacy-cycle'
        assert state['members'][json.dumps(['group-a', 'member-a'])]['status'] == 'pending'
        assert not (await acl.decide('bot-a', 'account_sharing', 'group-a', 'member-a', ACCOUNT['id']))['allowed']
        assert not (await acl.decide('bot-a', 'account_sharing', 'group-a', 'departed', ACCOUNT['id']))['allowed']
        await host.restart_access()
        acl = host.service.access()
    # Real owner management, not just raw-store reads, still updates the stable ID.
    response = await host.api('PATCH', 'accounts/' + ACCOUNT['id'], {'password': 'new-fake-secret'})
    assert response.status == 200
    host.sender.messages.clear()
    await host.event('账户管理 预览 legacy-account')
    assert 'new-fake-secret' in host.sender.messages[-1][2]
    host.service.access().lifecycle_available = False
    host.sender.messages.clear()
    await host.event('账户查询 legacy-account', user='private-a')
    assert not any('secret' in str(message) for message in host.sender.messages)
    assert (await (await host.api('GET', 'accounts', bot='bot-b')).json())['data'] == []


@pytest.mark.parametrize('package', ['modules.access_control.storage', 'modules.self_qa.storage', 'plugins.account_sharing.storage'])
async def test_failed_write_retains_legacy_record_and_retry_is_safe(tmp_path, package):
    from importlib import import_module

    path = tmp_path / 'legacy.db'
    legacy_database(path, [('accounts', ACCOUNT['id'], ACCOUNT)])
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TRIGGER fail_writes BEFORE INSERT ON extension_records BEGIN SELECT RAISE(ABORT, 'fake disk failure'); END")
    store = import_module(package).ExtensionStore(path)
    await store.ready()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            await store.put('bot-a', 'accounts', ACCOUNT['id'], {**ACCOUNT, 'password': 'replacement'})
        assert await store.get('bot-a', 'accounts', ACCOUNT['id']) == ACCOUNT
    finally:
        store.close()
    with sqlite3.connect(path) as connection:
        connection.execute('DROP TRIGGER fail_writes')
    for _ in range(2):
        store = import_module(package).ExtensionStore(path)
        try:
            await store.ready()
            await store.put('bot-a', 'accounts', ACCOUNT['id'], ACCOUNT)
            assert await store.all('bot-a', 'accounts') == [ACCOUNT]
        finally:
            store.close()
