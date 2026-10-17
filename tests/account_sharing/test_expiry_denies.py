"""Owner-scoped refusal and time-bound qualification through real public entries."""

from test_scoped_grants import access, scope_grant


async def test_overlapping_scoped_denies_remain_until_each_source_is_removed(host):
    account = await host.create()
    acl = host.service.access_provider()
    await access(host, 'whitelist', enabled=True)
    await acl.execute('bot-a', 'whitelist', function='reports', group='group-a', enabled=True)
    await access(host, 'function_group', id='共享功能', functions=['account_sharing', 'reports'])
    await scope_grant(host, function_scope={'kind': 'all'}, resource_scope={'kind': 'all'}, mode='continuous')
    first = await access(host, 'deny', scope='member', function_scope={'kind': 'group', 'id': '共享功能'})
    assert first.status == 200, await first.text()
    first_id = (await first.json())['data']['id']
    second = await access(host, 'deny', scope='group', function_scope={'kind': 'all'})
    assert second.status == 200
    second_id = (await second.json())['data']['id']
    await scope_grant(host, resources=[account['id']])
    explanation = (await (await access(host, 'explain', resource=account['id'])).json())['data']
    assert explanation['reason'] == 'denied'
    assert {d['id'] for d in explanation['denies']} == {first_id, second_id}
    await access(host, 'allow', scope='member', source=first_id)
    assert (await acl.decide('bot-a', 'reports', 'group-a', 'member-a', account['id'], resource_namespace='account_sharing'))['reason'] == 'denied'
    await access(host, 'allow', scope='group', source=second_id)
    assert (await acl.decide('bot-a', 'reports', 'group-a', 'member-a', account['id'], resource_namespace='account_sharing'))['allowed']
    await access(host, 'deny', scope='member')
    assert (await acl.decide('bot-a', 'reports', 'group-a', 'member-a', account['id'], resource_namespace='account_sharing'))['allowed']
    assert not (await acl.decide('bot-a', 'account_sharing', 'group-a', 'member-a', account['id']))['allowed']


async def test_expiry_is_live_for_old_buttons_keywords_and_final_private_send(host):
    from test_binding import bind
    from test_buttons import callback
    from test_qa import rule

    now = [2000.0]
    host.service.access_provider().clock = lambda: now[0]
    account = await host.create()
    await access(host, 'whitelist', enabled=True)
    response = await access(host, 'grant', identity_type='group_member_openid', confirmed=True, resources=[account['id']], expires_at=2010)
    assert response.status == 200, await response.text()
    source = (await response.json())['data']
    await bind(host)
    await rule(host, account['id'], ['到期别名'])
    await host.event('账户列表', user='private-a')
    data = callback(host.sender.messages[-1], '测试账户')
    now[0] = 2010
    host.sender.messages.clear()
    await host.click(data)
    await host.event('到期别名', user='private-a')
    assert not any('fake-secret' in str(m) for m in host.sender.messages)
    result = (await (await access(host, 'explain', resource=account['id'])).json())['data']
    assert result['reason'] == 'expired'
    assert result['sources'][0]['expires_at'] == 2010
    response = await access(host, 'set_expiry', source=source['id'], expires_at=2020)
    assert response.status == 200, await response.text()

    async def expire_during_hint(event, text):
        if '尝试私发' in text:
            now[0] = 2020

    host.sender.after_reply = expire_during_hint
    host.sender.messages.clear()
    await host.event(f'账户查询 {account["id"]}', user='member-a', group='group-a')
    assert not any('fake-secret' in str(m) for m in host.sender.messages)


async def test_owner_explanation_separates_binding_and_bulk_expiry_results(host):
    account = await host.create()
    await access(host, 'whitelist', enabled=True)
    await scope_grant(host, resources=[account['id']])
    view = (await (await access(host, 'explain', resource=account['id'])).json())['data']
    assert view['allowed']
    private_response = await access(host, 'explain', resource=account['id'], action='private')
    assert private_response.status == 200, await private_response.text()
    private = (await private_response.json())['data']
    assert private['reason'] == 'pending_binding'
    assert private['qualification_allowed'] and not private['allowed']
    await host.event('账户管理 批量期限 {"group":"group-a","members":[{"member":"member-a"},{"member":"missing-member"}],"expires_at":0}')
    result = host.sender.messages[-1][2]
    assert '"success": 1' in result and '"failed": 1' in result
    explanation = (await (await access(host, 'explain', resource=account['id'])).json())['data']
    assert explanation['reason'] == 'expired'
    assert explanation['membership'] == 'present'
    assert explanation['sources'][0]['expires_at'] == 0
    response = await access(host, 'bulk_expiry', members=[{'member': 'member-a'}, {'member': 'missing-member'}], expires_at=None)
    assert response.status == 200
    assert (await response.json())['data']['summary'] == {'success': 1, 'failed': 1}
    assert (await (await access(host, 'explain', resource=account['id'])).json())['data']['allowed']


async def test_explanation_distinguishes_missing_invoke_resource_and_pending_member(host):
    account = await host.create()
    other = await host.create(name='独立资源')
    await access(host, 'whitelist', enabled=True)
    await scope_grant(host, resources=[account['id']], invoke=False)
    result = (await (await access(host, 'explain', resource=account['id'])).json())['data']
    assert result['reason'] == 'no_function_permission'
    await scope_grant(host, resources=[account['id']])
    result = (await (await access(host, 'explain', resource=other['id'])).json())['data']
    assert result['reason'] == 'no_resource_permission'
    await host.event('普通聊天', user='unverified-member', group='group-a')
    result = (await (await access(host, 'explain', member='unverified-member', resource=account['id'])).json())['data']
    assert result['reason'] == 'pending_verification'
    for invalid in [True, '2030-01-01', -1, float('inf')]:
        response = await access(host, 'set_expiry', expires_at=invalid)
        assert response.status == 422


async def test_expired_source_never_overrides_a_live_source_and_bulk_grants_expire(host):
    now = [1000]
    host.service.access_provider().clock = lambda: now[0]
    account = await host.create()
    await access(host, 'whitelist', enabled=True)
    await scope_grant(host, resources=[account['id']], expires_at=1001)
    live = await scope_grant(host, resources=[account['id']])
    now[0] = 1001
    result = (await (await access(host, 'explain', resource=account['id'])).json())['data']
    assert result['allowed'] and len(result['sources']) == 2
    await access(host, 'revoke', source=live['id'])
    assert (await (await access(host, 'explain', resource=account['id'])).json())['data']['reason'] == 'expired'
    response = await access(
        host,
        'bulk_grant',
        members=[
            {'member': 'bulk-a', 'identity_type': 'group_member_openid', 'confirmed': True},
            {'member': 'bulk-b', 'identity_type': 'group_member_openid', 'confirmed': True},
        ],
        resource_scope={'kind': 'all'},
        mode='continuous',
        expires_at=1002,
    )
    assert (await response.json())['data']['summary'] == {'success': 2, 'failed': 0}
    now[0] = 1002
    for member in ['bulk-a', 'bulk-b']:
        result = (await (await access(host, 'explain', member=member, resource=account['id'])).json())['data']
        assert result['reason'] == 'expired'


async def test_expiry_while_issuing_menu_does_not_publish_stale_account_names(host):
    now = [1000]
    host.service.access_provider().clock = lambda: now[0]
    account = await host.create(name='到期时不可见')
    await access(host, 'whitelist', enabled=True)
    await scope_grant(host, resources=[account['id']], expires_at=1001)

    def menu_time():
        now[0] = 1001
        return now[0]

    host.service.clock = menu_time
    host.sender.messages.clear()
    await host.event('账户列表', user='member-a', group='group-a')
    assert not any('到期时不可见' in str(message) for message in host.sender.messages)


async def test_private_explanation_applies_recipient_framework_blacklist(host):
    from test_binding import bind

    from core.base.config import cfg

    account = await host.create()
    await access(host, 'whitelist', enabled=True)
    await scope_grant(host, resources=[account['id']])
    await bind(host)
    await host.event('普通私聊', user='private-a', union='private-union')
    cfg.set_bot_setting('bot-a', 'blacklist.user_list', {'private-union': 'private blocked'})
    view = (await (await access(host, 'explain', resource=account['id'])).json())['data']
    assert view['allowed']
    result = (await (await access(host, 'explain', resource=account['id'], action='private')).json())['data']
    assert result['qualification_allowed'] and result['binding'] == 'verified'
    assert not result['allowed'] and result['reason'] == 'framework_denied'
