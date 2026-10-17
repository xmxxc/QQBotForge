from core.base.config import cfg


async def access(host, operation, *, group='group-a', member='member-a', **kwargs):
    return await host.api('POST', 'access', {'operation': operation, 'group': group, 'member': member, **kwargs})


async def grant(host, account, **kwargs):
    await access(host, 'whitelist', enabled=True)
    return await access(host, 'grant', resources=[account['id']], identity_type='group_member_openid', confirmed=True, **kwargs)


async def test_group_directory_requires_group_function_and_resource_access(host):
    account = await host.create(name='可用账户')
    secret = await host.create(name='隐藏账户')
    await host.event('账户列表', user='member-a', group='group-a')
    assert host.sender.messages[-1][2] == '你暂无此功能的使用权限，请联系主人'
    await access(host, 'whitelist', enabled=True)
    response = await grant(host, account)
    assert response.status == 200, await response.text()
    await host.event('账户列表', user='member-a', group='group-a')
    assert '可用账户' in host.sender.messages[-1][2]
    assert '隐藏账户' not in host.sender.messages[-1][2]
    assert 'fake-secret' not in str(host.sender.messages)
    for group, bot in [('group-b', 'bot-a'), ('group-a', 'bot-b')]:
        await host.event('账户列表', bot=bot, user='member-a', group=group)
        assert '可用账户' not in host.sender.messages[-1][2]
    await access(host, 'deny', scope='member')
    host.sender.messages.clear()
    await host.event('账户列表', user='member-a', group='group-a')
    assert not any('可用账户' in str(m) for m in host.sender.messages)
    explanation = await access(host, 'explain', resource=account['id'])
    assert (await explanation.json())['data']['reason'] == 'denied'
    await access(host, 'allow', scope='member')
    await access(host, 'revoke')
    host.sender.messages.clear()
    await host.event('账户列表', user='member-a', group='group-a')
    assert not any('可用账户' in str(m) for m in host.sender.messages)
    assert secret['id'] != account['id']


async def test_explicit_native_identity_and_owner_only_management(host):
    account = await host.create()
    response = await access(host, 'grant', resources=[account['id']], identity_type='qq', confirmed=True)
    assert response.status == 422
    response = await access(host, 'grant', resources=[account['id']], identity_type='group_member_openid')
    assert response.status == 422
    await host.event('账户管理 白名单 {"group":"group-a","enabled":true}', group='group-a')
    # Parsed mentions carry reliable native group identity; typed input is required elsewhere.
    event = await host.event(
        '账户管理 授权 {"resources":["' + account['id'] + '"],"confirmed":true}', group='group-a', mentions=[{'id': 'member-a', 'bot': False}]
    )
    assert event.raw_user_id == 'owner-a'
    await host.event('账户列表', user='member-a', group='group-a')
    assert '测试账户' in host.sender.messages[-1][2]
    await host.event('账户管理 撤权 {"group":"group-a","member":"member-a"}', user='ordinary')
    await host.event('账户列表', user='member-a', group='group-a')
    assert '测试账户' in host.sender.messages[-1][2]
    cfg.set_bot_setting('bot-a', 'blacklist.user_list', {'member-a': 'blocked by framework'})
    host.sender.messages.clear()
    await host.event('账户列表', user='member-a', group='group-a')
    assert not any('测试账户' in str(m) for m in host.sender.messages)


async def test_reusable_access_and_no_auto_grant_or_unsolicited_messages(host):
    account = await host.create()
    await grant(host, account)
    host.sender.messages.clear()
    await host.event('普通聊天', user='new-member', group='group-a')
    await host.event('账户列表', user='new-member', group='group-a')
    await host.event('账户列表', user='new-member', group='group-a')
    assert len(host.sender.messages) == 1
    assert all(m[0] == 'group' for m in host.sender.messages)
    acl = host.service.access_provider()
    decision = await acl.decide('bot-a', 'other_feature', 'group-a', 'member-a', 'generic-resource')
    assert not decision['allowed']
    await acl.execute('bot-a', 'whitelist', function='other_feature', group='group-a', enabled=True)
    await acl.execute(
        'bot-a',
        'grant',
        function='other_feature',
        group='group-a',
        member='member-a',
        resources=['generic-resource'],
        identity_type='group_member_openid',
        confirmed=True,
    )
    assert (await acl.decide('bot-a', 'other_feature', 'group-a', 'member-a', 'generic-resource'))['allowed']
    assert not (await acl.decide('bot-a', 'other_feature', 'group-a', 'member-a', account['id']))['allowed']


async def test_function_and_resource_permissions_and_new_member_event(host):
    account = await host.create()
    other = await host.create(name='另一个账户')
    await access(host, 'whitelist', enabled=True)
    await access(host, 'grant', resources=[account['id']], identity_type='group_member_openid', confirmed=True, invoke=False)
    response = await access(host, 'explain', resource=account['id'])
    assert (await response.json())['data']['reason'] == 'no_function_permission'
    await access(host, 'revoke')
    await grant(host, other)
    response = await access(host, 'explain', resource=account['id'])
    assert (await response.json())['data']['reason'] == 'no_resource_permission'
    host.sender.messages.clear()
    await host.lifecycle('GROUP_MEMBER_ADD')
    assert host.sender.messages == []
    response = await host.api('GET', 'access')
    snapshot = (await response.json())['data']
    assert any(m['member'] == 'new-member' and m['status'] == 'unverified' for m in snapshot['members'].values())
    assert not any(g['member'] == 'new-member' for g in snapshot['grants'])
    host.service.access_provider = lambda: None
    await host.event('账户列表', user='member-a', group='group-a')
    assert host.sender.messages[-1][2] == '你暂无此功能的使用权限，请联系主人'
    response = await host.api('GET', 'access')
    assert response.status == 503


async def test_mentions_cannot_be_relabelled_as_another_group_identity(host):
    account = await host.create()
    await host.event('账户管理 授权 {"group":"group-b","resources":["' + account['id'] + '"],"confirmed":true}', group='group-a', mentions=[{'id': 'member-a'}])
    snapshot = (await (await host.api('GET', 'access')).json())['data']
    assert not snapshot['grants']
    await host.event('账户管理 授权 []')
    assert '无效' in host.sender.messages[-1][2] or '对象' in host.sender.messages[-1][2]
    response = await access(host, 'whitelist', enabled=True, unexpected_setting=True)
    assert response.status == 422


async def test_explanation_reports_framework_denial_and_private_mentions_are_not_native_targets(host):
    account = await host.create()
    await grant(host, account)
    cfg.set_bot_setting('bot-a', 'blacklist.user_list', {'member-a': 'framework'})
    response = await access(host, 'explain', resource=account['id'])
    decision = (await response.json())['data']
    assert decision['allowed'] is False
    assert decision['reason'] == 'framework_denied'
    await host.event('账户管理 授权 {"group":"group-b","resources":["' + account['id'] + '"],"confirmed":true}', mentions=[{'id': 'other-native'}])
    snapshot = (await (await host.api('GET', 'access')).json())['data']
    assert not any(g['member'] == 'other-native' for g in snapshot['grants'])
