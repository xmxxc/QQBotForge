from conftest import message
from test_access import access, grant


async def bind(host, *, private='private-a', group='group-a', member='member-a'):
    await host.event(f'账户绑定 {group} {member}', user=private)
    response = await host.api('GET', 'bindings')
    assert response.status == 200, await response.text()
    requests = (await response.json())['data']['requests']
    request = next(r for r in requests if r['private_id'] == private and r['group'] == group and r['member'] == member)
    response = await host.api(
        'POST', 'bindings', {'operation': 'confirm', 'request': request['id'], 'confirmed': True, 'evidence': '主人已核对群成员及本次私聊申请为同一人'}
    )
    assert response.status == 200, await response.text()
    return request


async def test_binding_requires_owner_confirmation_and_private_query_uses_distinct_id(host):
    account = await host.create()
    await grant(host, account)
    host.sender.messages.clear()
    await host.event(f'账户查询 {account["id"]}', user='private-a')
    assert not any('fake-secret' in str(m) for m in host.sender.messages)
    await host.event('账户绑定 group-a member-a', user='private-a')
    host.sender.messages.clear()
    await host.event(f'账户查询 {account["id"]}', user='private-a')
    assert not any('fake-secret' in str(m) for m in host.sender.messages)
    await bind(host)
    host.sender.messages.clear()
    await host.event(f'账户查询 {account["id"]}', user='private-a')
    assert host.sender.messages[-1][0:2] == ('private', 'private-a')
    assert 'fake-secret' in host.sender.messages[-1][2]
    host.sender.messages.clear()
    event = message(f'账户查询 {account["id"]}', user='member-a', group='group-a', union='union-a')
    event.user_id = 'union-a'
    await host.dispatch(event)
    assert host.sender.messages[0][0] == 'group'
    assert '尝试私发' in host.sender.messages[0][2]
    assert 'fake-secret' not in host.sender.messages[0][2]
    assert host.sender.messages[1][0:2] == ('private', 'private-a')
    assert not any(m[1] == 'member-a' and 'fake-secret' in m[2] for m in host.sender.messages)


async def test_private_contexts_and_failed_delivery_preserve_authorization(host):
    account = await host.create()
    await grant(host, account)
    await bind(host)
    await access(host, 'whitelist', group='group-b', enabled=True)
    await access(host, 'grant', group='group-b', member='member-b', resources=[account['id']], identity_type='group_member_openid', confirmed=True)
    await bind(host, group='group-b', member='member-b')
    host.sender.messages.clear()
    await host.event('账户列表', user='private-a')
    assert 'group-a' in host.sender.messages[-1][2] and 'group-b' in host.sender.messages[-1][2]
    await host.event('账户群 group-b', user='private-a')
    assert '测试账户' in host.sender.messages[-1][2]
    host.sender.fail_private = True
    host.sender.messages.clear()
    await host.event(f'账户查询 {account["id"]}', user='member-b', group='group-b')
    assert not any('fake-secret' in str(m) for m in host.sender.messages)
    assert 'not_friends' in host.sender.messages[-1][2]
    response = await access(host, 'explain', group='group-b', member='member-b', resource=account['id'])
    assert (await response.json())['data']['allowed']
    response = await host.api('GET', 'operations')
    results = (await response.json())['data']
    assert any(r.get('delivery') == 'failed' and r.get('error') == 'not_friends' for r in results)
    await access(host, 'revoke', group='group-b', member='member-b')
    host.sender.fail_private = False
    host.sender.messages.clear()
    await host.event(f'账户查询 {account["id"]}', user='private-a')
    assert host.sender.messages[-1][0:2] == ('private', 'private-a')


async def test_binding_and_notification_failures_do_not_imply_grants_or_password_delivery(host):
    account = await host.create()
    await host.event('账户管理 列表')  # Reliable observed owner C2C identity.
    host.sender.fail_private = True
    await grant(host, account)
    assert not any('fake-secret' in str(m) for m in host.sender.messages)
    result = (await (await host.api('GET', 'operations')).json())['data'][-1]
    assert result['authorization'] == 'saved'
    assert result['binding'] == 'pending'
    assert result['notification'] == 'failed'
    assert result['delivery'] == 'not_requested'
    await host.event('账户绑定 group-a member-a', user='attacker-private')
    response = await host.api('GET', 'bindings')
    request = (await response.json())['data']['requests'][-1]
    response = await host.api('POST', 'bindings', {'operation': 'confirm', 'request': request['id'], 'confirmed': False, 'evidence': 'x'})
    assert response.status == 422
    response = await host.api('GET', 'bindings', authenticated=False)
    assert response.status == 401
    host.sender.fail_private = False
    host.sender.messages.clear()
    await host.event(f'账户查询 {account["id"]}', user='attacker-private')
    assert not any('fake-secret' in str(m) for m in host.sender.messages)
    await host.event(f'账户查询 {account["id"]}', user='member-a', group='group-a')
    assert not any('fake-secret' in str(m) for m in host.sender.messages)
    assert '绑定' in host.sender.messages[-1][2]


async def test_current_authorization_and_password_are_rechecked_after_group_hint(host):
    account = await host.create()
    await grant(host, account)
    await bind(host)
    await host.api('PATCH', 'accounts/' + account['id'], {'password': 'fresh-fake-secret'})
    host.sender.messages.clear()
    await host.event(f'账户查询 {account["id"]}', user='private-a')
    assert 'fresh-fake-secret' in host.sender.messages[-1][2]
    host.sender.messages.clear()

    async def revoke_after_hint(event, text):
        if '尝试私发' in text:
            await access(host, 'revoke')

    host.sender.after_reply = revoke_after_hint
    await host.event(f'账户查询 {account["id"]}', user='member-a', group='group-a')
    assert not any('secret' in str(m) for m in host.sender.messages)


async def test_private_failures_are_distinct_and_never_fall_back_to_public_credentials(host):
    account = await host.create()
    await grant(host, account)
    await bind(host)
    host.sender.fail_private = True
    for code in ['not_friends', 'rejected', 'permission_denied', 'network_error']:
        host.sender.private_error = code
        host.sender.messages.clear()
        await host.event(f'账户查询 {account["id"]}', user='member-a', group='group-a')
        assert not any('fake-secret' in str(m) for m in host.sender.messages)
        assert code in host.sender.messages[-1][2]
    host.sender.fail_private = False
    host.sender.messages.clear()
    await host.event(f'账户查询 {account["id"]}', user='private-a', bot='bot-b')
    assert not any('fake-secret' in str(m) for m in host.sender.messages)
    await host.event(f'账户查询 {account["id"]}', user='member-a')
    assert not any('fake-secret' in str(m) for m in host.sender.messages)


async def test_private_binding_preserves_group_union_framework_denials(host):
    from core.base.config import cfg

    account = await host.create()
    await grant(host, account)
    await host.event('普通聊天', user='member-a', group='group-a', union='union-a')
    await bind(host)
    cfg.set_bot_setting('bot-a', 'identity.use_union_id_for_group', True)
    cfg.set_bot_setting('bot-a', 'blacklist.user_list', {'union-a': 'blocked group identity'})
    host.sender.messages.clear()
    await host.event('账户列表', user='private-a')
    await host.event(f'账户查询 {account["id"]}', user='private-a')
    assert not any('fake-secret' in str(m) or '测试账户' in str(m) for m in host.sender.messages)
    response = await access(host, 'explain', resource=account['id'])
    assert (await response.json())['data']['allowed'] is False


async def test_removed_owner_no_longer_receives_notifications(host):
    from core.base.config import cfg

    account = await host.create()
    await host.event('账户管理 列表')
    cfg.set_bot_setting('bot-a', 'owner_ids', ['new-owner'])
    host.sender.messages.clear()
    await grant(host, account)
    assert not any(m[0:2] == ('private', 'owner-a') for m in host.sender.messages)
    result = (await (await host.api('GET', 'operations')).json())['data'][-1]
    assert result['notification'] == 'unavailable'
