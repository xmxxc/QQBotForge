"""Account plugin as a composed consumer of group state, authorization and Q&A (C01)."""

import json

from account_host import account_host as account_host  # noqa: F401
from test_access_control import access_host as access_host  # noqa: F401
from test_bindings import bind as bind_identity


async def bind(host, group, member, private='private-a'):
    await bind_identity(host, group, member, private)
    host.sender.messages.clear()


async def owner(host, text, **kwargs):
    await host.event('账户管理 ' + text, user='owner-a', **kwargs)
    return host.texts()


async def test_owner_crud_preview_and_non_owners_cannot_manage(account_host):
    host = account_host
    assert (await host.account_api('POST', 'accounts', {'name': 'x'}, authenticated=False))[0] == 401
    account = await host.create()
    assert account['id'] and account['enabled'] is True
    # Ordinary members and Q&A delegates have no account management.
    await host.event(f'账户管理 预览 {account["id"]}', user='member-a')
    assert all('fake-secret' not in t for t in host.texts())
    # A current group admin with ordinary Q&A delegation still cannot manage accounts.
    await host.state.confirm_member('bot-a', 'group-a', 'admin-a', evidence='checked', member_role='admin')
    await host.modules.get('self_qa').manage('bot-a', 'public', 'list')
    await host.event('账户管理 新增 {"name":"偷建","login":"x","password":"x"}', user='admin-a', group='group-a')
    assert host.texts('group-a') == ['你暂无此功能的使用权限，请联系主人'] and len((await host.account_api())[1]['data']) == 1
    preview = await owner(host, f'预览 {account["id"]}')
    assert 'fake-secret' in preview[-1]
    assert all('fake-secret' not in t for t in await owner(host, f'预览 {account["id"]}', group='group-a'))
    status, _ = await host.account_api('PATCH', 'accounts/' + account['id'], {'password': 'new-fake-secret', 'groups': ['daily']})
    assert status == 200
    assert 'new-fake-secret' in (await owner(host, f'预览 {account["id"]}'))[-1]
    status, batch = await host.account_api('POST', 'account-batches', {'operation': 'update', 'ids': [account['id'], 'missing'], 'data': {'enabled': False}})
    assert batch['data']['summary'] == {'success': 1, 'failed': 1}
    assert all('secret' not in t for t in await owner(host, f'预览 {account["id"]}'))
    await owner(host, '新增 {"name":"命令账户","login":"fake","password":"fake"}')
    assert len((await host.account_api())[1]['data']) == 2
    assert (await host.account_api(bot='bot-b'))[1]['data'] == []


async def test_accounts_are_resources_and_deletion_never_passes_grants_to_a_same_name_account(account_host):
    host = account_host
    old = await host.create(name='同名', groups=['daily'])
    snapshot = await host.access.access_snapshot('bot-a', 'account_sharing')
    assert snapshot['owner'] == 'plugin:account_sharing'
    assert snapshot['resources'][old['id']] == {'id': old['id'], 'groups': ['daily'], 'enabled': True}
    await owner(host, '白名单 {"group":"group-a","enabled":true}')
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='checked', member_role='member')
    status, granted = await host.account_api('POST', 'access', {'operation': 'grant', 'group': 'group-a', 'member': 'member-a', 'resources': [old['id']]})
    assert status == 200, granted
    # Granting saves and reports; it never sends credentials.
    assert granted['data']['operation_result']['authorization'] == 'saved' and granted['data']['operation_result']['delivery'] == 'not_requested'
    assert all('fake-secret' not in str(m) for m in host.sender.messages)
    host.sender.messages.clear()
    await host.account_api('DELETE', 'accounts/' + old['id'])
    new = await host.create(name='同名')
    assert new['id'] != old['id']
    snapshot = await host.access.access_snapshot('bot-a', 'account_sharing')
    assert old['id'] not in snapshot['resources'] and all(new['id'] not in g['resources'] for g in snapshot['grants'])
    decision = await host.access.evaluate('bot-a', 'account_sharing', 'account_sharing', 'group-a', 'member-a', new['id'])
    assert not decision['allowed']
    assert (await host.account_api('POST', 'access', {'operation': 'grant', 'group': 'group-a', 'member': 'member-a', 'resources': [old['id']]}))[0] == 404


async def test_keyword_resource_rule_private_delivery_and_disable(account_host):
    host = account_host
    account = await host.create(name='视频会员')
    status, rule = await host.account_api('POST', 'rules', {'name': '视频', 'target': account['id'], 'keywords': ['视频']})
    assert status == 201
    # The Q&A rule stores only the account reference.
    qa_rule = await host.modules.get('self_qa').manage('bot-a', 'account_sharing', 'get', key=rule['data']['id'])
    assert qa_rule['target'] == account['id'] and 'fake-secret' not in json.dumps(qa_rule)
    await owner(host, '白名单 {"group":"group-a","enabled":true}')
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='checked', member_role='member')
    # No grant yet: denied without naming the account.
    await host.event('视频', user='member-a', group='group-a')
    assert host.texts() == ['你暂无此功能的使用权限，请联系主人']
    await host.account_api('POST', 'access', {'operation': 'grant', 'group': 'group-a', 'member': 'member-a', 'resources': [account['id']]})
    host.sender.messages.clear()
    await host.event('视频', user='member-a', group='group-a')
    status_lines = host.texts('group-a')
    assert status_lines[0].startswith('将尝试私发') and '身份绑定 申请 group-a member-a' in status_lines[1]
    await bind(host, 'group-a', 'member-a')
    await host.event('视频', user='member-a', group='group-a')
    assert ('push', 'private-a') in [m[:2] for m in host.sender.messages]
    assert any('fake-secret' in m[2] for m in host.sender.messages if m[0] == 'push')
    assert all('fake-secret' not in m[2] for m in host.sender.messages if m[1] == 'group-a')
    host.sender.messages.clear()
    # Private chat: the authorization-verified group context answers directly.
    await host.event(f'账户查询 {account["id"]}', user='private-a')
    assert 'fake-secret' in host.texts('private-a')[-1]
    # Disabling the account stops queries at once.
    await host.account_api('PATCH', 'accounts/' + account['id'], {'enabled': False})
    await host.event(f'账户查询 {account["id"]}', user='private-a')
    await host.event('视频', user='member-a', group='group-a')
    assert all('fake-secret' not in str(m) for m in host.sender.messages)


async def test_identity_and_member_facts_moved_out_of_the_plugin(account_host):
    host = account_host
    await host.event('账户绑定 group-a member-a', user='private-a')
    assert '身份绑定 申请' in host.texts('private-a')[-1]
    status, data = await host.account_api('POST', 'access', {'operation': 'verify', 'group': 'group-a', 'member': 'member-a'})
    assert status == 422 and '群状态' in data['error']
    status, data = await host.account_api('POST', 'bindings', {'operation': 'confirm'})
    assert status == 410


async def test_legacy_migration_keeps_ids_and_never_widens_or_revives(account_host):
    host = account_host
    account = await host.create(name='旧账户')
    gone = 'deleted-account'
    acl = host.access
    key = json.dumps(['group-a', 'account_sharing'], ensure_ascii=False)

    def legacy_grant(source_id, member, **fields):
        return {
            'id': source_id,
            'group': 'group-a',
            'member': member,
            'cycle': 'old-cycle-' + member,
            'function': 'account_sharing',
            'invoke': True,
            'expires_at': None,
            'resources': [account['id'], gone],
            'functions': ['account_sharing'],
            'function_scope': {'kind': 'function', 'id': 'account_sharing'},
            'resource_scope': {'kind': 'resources', 'ids': [account['id'], gone]},
            'resource_namespace': 'account_sharing',
            'mode': 'snapshot',
            **fields,
        }

    legacy = {
        'version': 3,
        'whitelist': {key: True},
        'members': {},
        'grants': [legacy_grant('keep-source', 'member-a'), legacy_grant('expired-source', 'member-b', expires_at=1)],
        'denies': [{'id': 'deny-source', 'function': 'account_sharing', 'group': 'group-a', 'member': 'member-c'}],
        'revocations': [{**legacy_grant('revoked-source', 'member-a'), 'revoked_at': 5, 'reason': 'group_lifecycle'}],
    }
    await acl.store.put('bot-a', 'access', 'state', legacy)
    acl.states.pop('bot-a', None)
    status, data = await host.account_api('POST', 'migration', {})
    assert status == 200, data
    report = data['data']['authorization']
    assert report['grants'] == 2 and report['revocations'] == 1 and report['denies'] == 1 and report['policy_groups'] == ['group-a']
    assert report['dropped_resources'] >= 2
    snapshot = await acl.access_snapshot('bot-a', 'account_sharing')
    grants = {g['id']: g for g in snapshot['grants']}
    assert set(grants) == {'keep-source', 'expired-source'} and 'revoked-source' not in grants
    assert grants['keep-source']['resources'] == [account['id']] and grants['keep-source']['cycle'].startswith('legacy:')
    # Imported but inactive: even a currently present member is not allowed until adoption.
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='checked', member_role='member')
    await host.state.confirm_member('bot-a', 'group-a', 'member-b', evidence='checked', member_role='member')
    await host.state.confirm_member('bot-a', 'group-a', 'member-c', evidence='checked', member_role='member')
    assert not (await acl.evaluate('bot-a', 'account_sharing', 'account_sharing', 'group-a', 'member-a', account['id']))['allowed']
    tasks = (await host.account_api('GET', 'tasks'))[1]['data']
    assert {t['source'] for t in tasks if t['type'] == 'legacy_adoption'} == {'keep-source', 'expired-source'}
    for member in ('member-a', 'member-b'):
        status, adopted = await host.account_api('POST', 'access', {'operation': 'adopt_legacy', 'group': 'group-a', 'member': member})
        assert status == 200, adopted
    assert (await acl.evaluate('bot-a', 'account_sharing', 'account_sharing', 'group-a', 'member-a', account['id']))['allowed']
    assert (await acl.evaluate('bot-a', 'account_sharing', 'account_sharing', 'group-a', 'member-b', account['id']))['reason'] == 'expired'
    assert (await acl.evaluate('bot-a', 'account_sharing', 'account_sharing', 'group-a', 'member-c', account['id']))['reason'] == 'denied'
    # Re-running is idempotent and cannot restore the revoked source.
    status, again = await host.account_api('POST', 'migration', {})
    assert again['data']['authorization']['grants'] == 0 and again['data']['authorization']['skipped'] == 4
    assert 'revoked-source' not in {g['id'] for g in (await acl.access_snapshot('bot-a', 'account_sharing'))['grants']}
