from test_access import access


async def scope_grant(host, member='member-a', **kwargs):
    response = await access(host, 'grant', member=member, identity_type='group_member_openid', confirmed=True, **kwargs)
    assert response.status == 200, await response.text()
    return (await response.json())['data']


async def test_snapshot_and_continuous_account_groups_follow_explicit_future_choice(host):
    account = await host.create(name='当前账户', groups=['工作'])
    await access(host, 'whitelist', enabled=True)
    await scope_grant(host, mode='snapshot', resource_scope={'kind': 'group', 'id': '工作'})
    await scope_grant(host, 'member-b', mode='continuous', resource_scope={'kind': 'group', 'id': '工作'})
    future = await host.create(name='未来账户', groups=['工作'])
    await host.event('账户列表', user='member-a', group='group-a')
    assert '当前账户' in host.sender.messages[-1][2] and '未来账户' not in host.sender.messages[-1][2]
    await host.event('账户列表', user='member-b', group='group-a')
    assert '未来账户' in host.sender.messages[-1][2]
    await host.api('PATCH', 'accounts/' + account['id'], {'groups': ['其他']})
    await host.event('账户列表', user='member-b', group='group-a')
    assert '当前账户' not in host.sender.messages[-1][2]
    await host.event('账户列表', user='member-a', group='group-a')
    assert '当前账户' in host.sender.messages[-1][2]
    await host.api('PATCH', 'accounts/' + future['id'], {'enabled': False})
    host.sender.messages.clear()
    await host.event('账户列表', user='member-b', group='group-a')
    assert not any('未来账户' in str(m) for m in host.sender.messages)


async def test_function_groups_all_scopes_and_independent_consumer(host):
    account = await host.create()
    acl = host.service.access_provider()
    await access(host, 'whitelist', enabled=True)
    await acl.execute('bot-a', 'whitelist', function='reports', group='group-a', enabled=True)
    response = await access(host, 'function_group', id='共享功能', functions=['account_sharing', 'reports'])
    assert response.status == 200, await response.text()
    source = await scope_grant(host, function_scope={'kind': 'group', 'id': '共享功能'}, resources=[account['id']], mode='snapshot')
    assert (await acl.decide('bot-a', 'reports', 'group-a', 'member-a', account['id'], resource_namespace='account_sharing'))['allowed']
    await acl.execute('bot-a', 'whitelist', function='independent', group='group-a', enabled=True)
    assert not (await acl.decide('bot-a', 'independent', 'group-a', 'member-a', account['id'], resource_namespace='account_sharing'))['allowed']
    await scope_grant(host, 'future-member', function_scope={'kind': 'all'}, resource_scope={'kind': 'all'}, mode='continuous')
    await scope_grant(host, 'snapshot-member', function_scope={'kind': 'all'}, resource_scope={'kind': 'all'}, mode='snapshot')
    await acl.execute('bot-a', 'whitelist', function='new_feature', group='group-a', enabled=True)
    assert (await acl.decide('bot-a', 'new_feature', 'group-a', 'future-member', account['id'], resource_namespace='account_sharing'))['allowed']
    assert not (await acl.decide('bot-a', 'new_feature', 'group-a', 'snapshot-member', account['id'], resource_namespace='account_sharing'))['allowed']
    direct = await scope_grant(host, resources=[account['id']])
    await access(host, 'revoke', source=direct['id'])
    explanation = (await (await access(host, 'explain', resource=account['id'])).json())['data']
    assert explanation['allowed']
    assert source['id'] in [g['id'] for g in explanation['sources']]
    await access(host, 'deny', scope='member')
    await scope_grant(host, resources=[account['id']])
    assert not (await acl.decide('bot-a', 'account_sharing', 'group-a', 'member-a', account['id']))['allowed']


async def test_bulk_results_account_batches_and_deletion_do_not_revive_same_name(host):
    account = await host.create(groups=['批量'])
    await access(host, 'whitelist', enabled=True)
    response = await access(
        host,
        'bulk_grant',
        members=[
            {'member': 'member-a', 'identity_type': 'group_member_openid', 'confirmed': True},
            {'member': '123456', 'identity_type': 'qq', 'confirmed': True},
        ],
        mode='continuous',
        resource_scope={'kind': 'all'},
    )
    assert response.status == 200, await response.text()
    result = (await response.json())['data']
    assert result['summary'] == {'success': 1, 'failed': 1}
    await host.event('账户列表', user='member-a', group='group-a')
    assert '测试账户' in host.sender.messages[-1][2]
    response = await host.api('POST', 'account-batches', {'operation': 'update', 'ids': [account['id'], 'missing'], 'data': {'groups': ['移组']}})
    assert response.status == 200, await response.text()
    assert (await response.json())['data']['summary'] == {'success': 1, 'failed': 1}
    await access(host, 'revoke')
    await scope_grant(host, resources=[account['id']])
    await host.api('DELETE', 'accounts/' + account['id'])
    replacement = await host.create()
    host.sender.messages.clear()
    await host.event('账户列表', user='member-a', group='group-a')
    assert not any(replacement['id'] in str(m) for m in host.sender.messages)
    snapshot = (await (await host.api('GET', 'access')).json())['data']
    assert not any(account['id'] in grant['resources'] for grant in snapshot['grants'])


async def test_continuous_function_groups_and_generic_resource_namespace(host):
    account = await host.create()
    acl = host.service.access_provider()
    await access(host, 'whitelist', enabled=True)
    await acl.execute('bot-a', 'whitelist', function='reports', group='group-a', enabled=True)
    await access(host, 'function_group', id='动态组', functions=['account_sharing', 'reports'])
    await scope_grant(host, resources=[account['id']], function_scope={'kind': 'group', 'id': '动态组'}, mode='snapshot')
    await scope_grant(host, 'member-b', resources=[account['id']], function_scope={'kind': 'group', 'id': '动态组'}, mode='continuous')
    await access(host, 'function_group', id='动态组', functions=['reports'])
    assert (await acl.decide('bot-a', 'account_sharing', 'group-a', 'member-a', account['id']))['allowed']
    assert not (await acl.decide('bot-a', 'account_sharing', 'group-a', 'member-b', account['id']))['allowed']
    await acl.register_resources('bot-a', 'reports', [{'id': 'report-r', 'groups': ['日报'], 'enabled': True}])
    await acl.execute(
        'bot-a',
        'grant',
        function='reports',
        group='group-a',
        member='generic-member',
        identity_type='group_member_openid',
        confirmed=True,
        function_scope={'kind': 'group', 'id': '动态组'},
        resource_scope={'kind': 'group', 'id': '日报'},
        mode='continuous',
    )
    assert (await acl.decide('bot-a', 'reports', 'group-a', 'generic-member', 'report-r'))['allowed']
    assert not (await acl.decide('bot-b', 'reports', 'group-a', 'generic-member', 'report-r'))['allowed']
    assert not (await acl.decide('bot-a', 'reports', 'group-b', 'generic-member', 'report-r'))['allowed']


async def test_owner_commands_bulk_revoke_and_scope_failures_are_truthful(host):
    account = await host.create()
    await access(host, 'whitelist', enabled=True)
    await host.event(
        '账户管理 批量授权 {"group":"group-a","members":[{"member":"member-a","identity_type":"group_member_openid","confirmed":true}],"resource_scope":{"kind":"all"},"mode":"snapshot"}'
    )
    await host.event('账户列表', user='member-a', group='group-a')
    assert '测试账户' in host.sender.messages[-1][2]
    response = await access(
        host, 'bulk_revoke', members=[{'member': 'member-a', 'identity_type': 'group_member_openid'}, {'member': 'member-a', 'identity_type': 'qq'}]
    )
    assert (await response.json())['data']['summary'] == {'success': 1, 'failed': 1}
    host.sender.messages.clear()
    await host.event('账户列表', user='member-a', group='group-a')
    assert not any(account['id'] in str(m) for m in host.sender.messages)
    response = await access(host, 'grant', identity_type='group_member_openid', confirmed=True, mode='ambiguous', resources=[account['id']])
    assert response.status == 422
    response = await access(
        host, 'grant', identity_type='group_member_openid', confirmed=True, function_scope={'kind': 'group', 'id': '不存在'}, resources=[account['id']]
    )
    assert response.status == 422


async def test_scoped_inputs_reject_unknown_accounts_and_ambiguous_future_ranges(host):
    account = await host.create()
    await access(host, 'whitelist', enabled=True)
    for extra, expected in [
        ({'resource_scope': {'kind': 'resources', 'ids': ['missing']}}, 404),
        ({'resource_scope': {'kind': 'all', 'ids': [account['id']]}}, 422),
        ({'resources': [account['id']], 'function_scope': {'kind': 'all', 'id': 'mixed'}}, 422),
        ({'resources': [account['id']], 'resource_scope': {'kind': 'all'}}, 422),
    ]:
        response = await access(host, 'grant', identity_type='group_member_openid', confirmed=True, **extra)
        assert response.status == expected, await response.text()
    assert not (await (await host.api('GET', 'access')).json())['data']['grants']


async def test_concurrent_account_edits_and_directories_keep_latest_scope(host):
    import asyncio

    account = await host.create(groups=['原分组'])
    await access(host, 'whitelist', enabled=True)
    await scope_grant(host, resource_scope={'kind': 'group', 'id': '原分组'}, mode='continuous')
    for index in range(6):
        responses = await asyncio.gather(
            host.api('GET', 'access'),
            host.api('PATCH', 'accounts/' + account['id'], {'groups': ['原分组'] if index % 2 else ['新分组']}),
            host.api('GET', 'access'),
        )
        assert all(response.status == 200 for response in responses)
    response = await host.api('PATCH', 'accounts/' + account['id'], {'groups': ['新分组']})
    assert response.status == 200
    host.sender.messages.clear()
    await host.event('账户列表', user='member-a', group='group-a')
    assert not any(account['id'] in str(message) for message in host.sender.messages)
    state = (await (await host.api('GET', 'access')).json())['data']
    assert state['resources']['account_sharing'][account['id']]['groups'] == ['新分组']
