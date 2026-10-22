"""Directory, keywords, buttons and private queries share one eligibility and identity path (C02)."""

import json

import pytest
from account_host import account_host as account_host  # noqa: F401
from socket_platform import until
from test_access_control import access_host as access_host  # noqa: F401
from test_account_c01 import bind, owner

DENIED = '你暂无此功能的使用权限，请联系主人'


async def grant(host, member, *resources, group='group-a', **fields):
    status, data = await host.account_api('POST', 'access', {'operation': 'grant', 'group': group, 'member': member, 'resources': list(resources), **fields})
    assert status == 200, data
    host.sender.messages.clear()
    return data['data']


async def member_ready(host, member='member-a', group='group-a', role='member'):
    await owner(host, '白名单 ' + json.dumps({'group': group, 'enabled': True}))
    await host.state.confirm_member('bot-a', group, member, evidence='checked', member_role=role)


def pushed(host, private='private-a'):
    return [m[2] for m in host.sender.messages if m[0] == 'push' and m[1] == private]


async def test_admission_and_roles_never_replace_explicit_account_grants(account_host):
    host = account_host
    account = await host.create()
    await host.account_api('POST', 'rules', {'name': '账户', 'target': account['id'], 'keywords': ['账户']})
    await member_ready(host, role='admin')
    await bind(host, 'group-a', 'member-a')
    # Owner tries a role policy for the account consumer: an admin still needs a grant.
    await host.access.manage_access(
        'bot-a', 'account_sharing', 'policy', function='account_sharing', policy={'kind': 'role', 'groups': ['group-a'], 'roles': ['admin'], 'private': True}
    )
    await host.event('账户', user='member-a', group='group-a')
    await host.event('账户列表', user='private-a')
    assert host.texts() == [DENIED, DENIED] and pushed(host) == []
    await host.access.manage_access(
        'bot-a', 'account_sharing', 'policy', function='account_sharing', policy={'kind': 'explicit', 'groups': ['group-a'], 'private': True}
    )
    await grant(host, 'member-a', account['id'])
    await host.event('账户', user='member-a', group='group-a')
    assert any('fake-secret' in text for text in pushed(host))
    # A non-whitelisted group never qualifies, whatever is granted.
    await owner(host, '白名单 {"group":"group-a","enabled":false}')
    host.sender.messages.clear()
    await host.event('账户', user='member-a', group='group-a')
    # Denial was already shown moments ago (rate limited); nothing is sent.
    assert pushed(host) == [] and all('fake-secret' not in t for t in host.texts())


async def test_directory_buttons_paging_ack_and_text_fallback(account_host):
    host = account_host
    accounts = [await host.create(name=f'账户{i}', password=f'secret-{i}') for i in range(8)]
    await host.account_api('PATCH', 'accounts/' + accounts[7]['id'], {'enabled': False})
    await member_ready(host)
    await bind(host, 'group-a', 'member-a')
    await grant(host, 'member-a', *[a['id'] for a in accounts])
    host.sender.keyboards.clear()
    await host.event('账户列表', user='private-a')
    (menu,) = host.texts('private-a')
    assert '账户0' in menu and '账户5' in menu and '账户6' not in menu and '账户7' not in menu
    rows = host.sender.keyboards[-1]
    labels = [button['text'] for row in rows for button in row]
    assert '下一页' in labels and all(button.get('list') in (None, ['private-a']) for row in rows for button in row)
    query = next(button['data'] for row in rows for button in row if button['text'] == '账户1')
    # Click: ACK first, then the latest credentials privately.
    await host.account_api('PATCH', 'accounts/' + accounts[1]['id'], {'password': 'rotated-secret'})
    assert await host.click(query, user='private-a') == 0
    assert 'rotated-secret' in host.texts('private-a')[-1]
    # Another user cannot use this personal menu.
    assert await host.click(query, user='private-b') == 0
    assert all('secret' not in text for text in host.texts())
    next_page = next(button['data'] for row in rows for button in row if button['text'] == '下一页')
    assert await host.click(next_page, user='private-a') == 0
    assert '账户6' in host.texts('private-a')[-1]
    # Buttons refused by the platform: the same menu as text commands.
    host.sender.reject_buttons = True
    await host.event('账户列表', user='private-a')
    text = host.texts('private-a')[-1]
    assert '按钮暂不可用' in text and f'账户查询 {accounts[0]["id"]}' in text
    await host.event(f'账户查询 {accounts[0]["id"]}', user='private-a')
    assert 'secret-0' in host.texts('private-a')[-1]


async def test_private_group_selection_and_group_status_then_private(account_host):
    host = account_host
    a, b = await host.create(name='甲'), await host.create(name='乙', password='other-secret')
    await member_ready(host)
    await member_ready(host, 'member-b', 'group-b')
    await bind(host, 'group-a', 'member-a')
    await bind(host, 'group-b', 'member-b')
    await grant(host, 'member-a', a['id'])
    await grant(host, 'member-b', b['id'], group='group-b')
    await host.event('账户列表', user='private-a')
    assert '请选择授权群' in host.texts('private-a')[-1]
    await host.event('账户群 group-b', user='private-a')
    listing = host.texts('private-a')[-1]
    assert '乙' in listing and '甲' not in listing
    await host.event(f'账户查询 {b["id"]}', user='private-a')
    assert 'other-secret' in host.texts('private-a')[-1]
    # Group query: status in the group, credentials only privately.
    await host.event(f'账户查询 {a["id"]}', user='member-a', group='group-a')
    group_lines = host.texts('group-a')
    assert group_lines[0].startswith('将尝试私发') and group_lines[-1] == '账户信息已私发。'
    assert any('fake-secret' in m[2] for m in host.sender.messages) is False
    # Platform refuses the push: real code reported, never public.
    host.sender.push_result = (False, {'code': 40034, 'message': 'proactive limit'})
    await host.event(f'账户查询 {a["id"]}', user='member-a', group='group-a')
    lines = host.texts('group-a')
    assert '40034' in lines[-1] and all('fake-secret' not in line for line in lines)


async def test_latest_password_expiry_revoke_leave_and_rejoin(account_host):
    host = account_host
    account = await host.create()
    await host.account_api('POST', 'rules', {'name': '会员', 'target': account['id'], 'keywords': ['会员']})
    await member_ready(host)
    await bind(host, 'group-a', 'member-a')
    source = await grant(host, 'member-a', account['id'])
    await host.event('会员', user='private-a')
    assert 'fake-secret' in host.texts('private-a')[-1]
    await host.account_api('PATCH', 'accounts/' + account['id'], {'password': 'latest-secret'})
    await host.event('会员', user='private-a')
    assert 'latest-secret' in host.texts('private-a')[-1]
    await host.account_api('POST', 'access', {'operation': 'expiry', 'group': 'group-a', 'member': 'member-a', 'source': source['id'], 'expires_at': 1})
    await host.event('会员', user='private-a')
    assert host.texts('private-a') == [DENIED]
    second = await grant(host, 'member-a', account['id'])
    await host.account_api('POST', 'access', {'operation': 'revoke', 'group': 'group-a', 'member': 'member-a', 'source': second['id']})
    await host.event('会员', user='private-a')
    assert all('latest-secret' not in t for t in host.texts())
    await grant(host, 'member-a', account['id'])
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a')
    await until(lambda: host.state.states['bot-a']['groups']['group-a']['members']['member-a'].get('status') == 'left')
    await until(lambda: host.state.recovery.get_status('bot-a').ready)
    await host.event('会员', user='private-a')
    assert all('latest-secret' not in t for t in host.texts())
    host.lifecycle('GROUP_MEMBER_ADD', member='member-a', sequence=3, timestamp=1001)
    await until(lambda: host.state.states['bot-a']['groups']['group-a'].get('last_native', {}).get('sequence') == 3)
    await until(lambda: host.state.recovery.get_status('bot-a').ready)
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='rejoined', member_role='member')
    await host.event('会员', user='private-a')
    assert all('latest-secret' not in t for t in host.texts())
    await grant(host, 'member-a', account['id'])
    await host.event('会员', user='private-a')
    assert 'latest-secret' in host.texts('private-a')[-1]


async def test_keyword_ties_show_only_permitted_names_and_stale_rules_do_not_send(account_host):
    host = account_host
    seen, hidden = await host.create(name='可见账户'), await host.create(name='他人账户', password='hidden-secret')
    third = await host.create(name='第三账户', password='third-secret')
    for account in (seen, hidden, third):
        status, _ = await host.account_api(
            'POST', 'rules', {'name': account['name'], 'target': account['id'], 'keywords': ['会员'], 'mode': 'contains', 'priority': 1}
        )
        assert status == 201
    await member_ready(host)
    await bind(host, 'group-a', 'member-a')
    await grant(host, 'member-a', seen['id'], third['id'])
    host.sender.keyboards.clear()
    await host.event('我要会员', user='private-a')
    menu = host.texts('private-a')[-1]
    assert '可见账户' in menu and '第三账户' in menu and '他人账户' not in menu
    button = next(b['data'] for row in host.sender.keyboards[-1] for b in row if b['text'] == '第三账户')
    rules = (await host.account_api('GET', 'rules'))[1]['data']
    third_rule = next(r for r in rules if r['target'] == third['id'])
    await host.account_api('PATCH', 'rules/' + third_rule['id'], {'enabled': False})
    assert await host.click(button, user='private-a') == 0
    assert all('third-secret' not in t for t in host.texts())


async def test_unauthorized_is_rate_limited_chat_is_silent_and_bots_are_isolated(account_host):
    host = account_host
    account = await host.create()
    await host.account_api('POST', 'rules', {'name': '账户', 'target': account['id'], 'keywords': ['账户']})
    await member_ready(host)
    await host.event('今天天气不错', user='member-a', group='group-a')
    assert host.texts() == []
    await host.event('账户', user='member-a', group='group-a')
    await host.event('账户', user='member-a', group='group-a')
    assert host.texts() == [DENIED]
    # No per-attempt owner notification and no account name in the denial.
    assert all(m[0] != 'push' for m in host.sender.messages)
    await host.event('账户', user='member-a', group='group-a', bot='bot-b')
    assert all('fake-secret' not in t for t in host.texts())
    assert (await host.account_api(bot='bot-b'))[1]['data'] == []


async def group_delivery_ready(host, account):
    from test_group_roster import roster_platform

    await member_ready(host)
    await bind(host, 'group-a', 'member-a')
    roster_platform(host)
    assert (await host.state.sync_roster('bot-a', 'group-a'))['complete']
    for member in ('member-a', 'this-bot', 'other-bot'):
        await grant(host, member, account['id'])
    status, result = await host.account_api('POST', 'group-delivery', {'group': 'group-a', 'resource': account['id'], 'enabled': True})
    assert status == 200 and result['data']['actual'] == 'group', result
    host.sender.messages.clear()


@pytest.mark.parametrize('change', ['revoke_other', 'new_member', 'revoke_asker', 'account_edit', 'other_account_edit'])
async def test_group_send_rechecks_after_the_last_await(account_host, monkeypatch, change):
    """Real keyword entry; the change lands while the final rule read of the group attempt is awaited."""
    import inspect

    host = account_host
    account, other = await host.create(), await host.create(name='其他账户', password='other-secret')
    status, response = await host.account_api('POST', 'rules', {'name': '群发', 'target': account['id'], 'keywords': ['群发触发']})
    assert status == 201, response
    await group_delivery_ready(host, account)
    service = host.plugins._plugins['account_sharing'].module._instance
    qa = host.modules.get('self_qa')
    original, changed = qa.manage, []

    async def during_rule_read(*args, **kwargs):
        if not changed and kwargs.get('key') and 'try_send' in [frame.function for frame in inspect.stack()]:
            changed.append(True)
            if change == 'revoke_other':
                await host.access.manage_access('bot-a', 'account_sharing', 'revoke', function='account_sharing', group='group-a', member='other-bot')
            elif change == 'revoke_asker':
                # Group admission now allows the function, but never an account send.
                await host.access.manage_access(
                    'bot-a', 'account_sharing', 'policy', function='account_sharing', policy={'kind': 'group', 'groups': ['group-a'], 'private': True}
                )
                await host.access.manage_access('bot-a', 'account_sharing', 'revoke', function='account_sharing', group='group-a', member='member-a')
            elif change == 'new_member':
                host.lifecycle('GROUP_MEMBER_ADD', member='new-member', sequence=2, timestamp=1000)
                await until(lambda: 'new-member' in host.state.states['bot-a']['groups']['group-a']['members'])
                await until(lambda: host.state._ready('bot-a'))
            else:
                edited = account if change == 'account_edit' else other
                status, _ = await host.account_api('PATCH', 'accounts/' + edited['id'], {'password': 'edited-secret'})
                assert status == 200
            if change not in ('account_edit', 'other_account_edit'):
                mode = await service.group_delivery.mode('bot-a', 'group-a', account['id'])
                assert not service.group_delivery.can_send('bot-a', mode)
        return await original(*args, **kwargs)

    monkeypatch.setattr(qa, 'manage', during_rule_read)
    await host.event('群发触发', user='member-a', group='group-a')
    assert changed, 'the change must land during the group attempt'
    group = [m[2] for m in host.sender.messages if m[1] == 'group-a']
    if change == 'account_edit':
        # Neither the old nor the new password: the asker is told to query again.
        assert host.texts() == ['账户信息刚更新，请重新查询。']
    elif change == 'other_account_edit':
        # Another account's edit changes nothing about this send.
        assert any('fake-secret' in text for text in group) and pushed(host) == []
    elif change == 'revoke_asker':
        assert all('fake-secret' not in text for text in host.texts()) and pushed(host) == []
    else:
        # The asker is still eligible: only the whole-group condition failed, so it goes private.
        assert all('fake-secret' not in text for text in group), group
        assert any('fake-secret' in text for text in pushed(host)), host.sender.messages


@pytest.mark.parametrize('edited', ['same', 'other'])
async def test_private_send_rechecks_account_content_after_the_last_await(account_host, monkeypatch, edited):
    host = account_host
    account, other = await host.create(), await host.create(name='其他账户', password='other-secret')
    status, response = await host.account_api('POST', 'rules', {'name': '私发', 'target': account['id'], 'keywords': ['私发触发']})
    assert status == 201, response
    await member_ready(host)
    await bind(host, 'group-a', 'member-a')
    await grant(host, 'member-a', account['id'])
    qa = host.modules.get('self_qa')
    original, changed = qa.manage, []

    async def during_rule_read(*args, **kwargs):
        if not changed and kwargs.get('key'):
            changed.append(True)
            target = account if edited == 'same' else other
            status, _ = await host.account_api('PATCH', 'accounts/' + target['id'], {'password': 'edited-secret'})
            assert status == 200
        return await original(*args, **kwargs)

    monkeypatch.setattr(qa, 'manage', during_rule_read)
    await host.event('私发触发', user='member-a', group='group-a')
    assert changed
    if edited == 'same':
        assert pushed(host) == [] and host.texts()[-1] == '账户信息刚更新，请重新查询。'
    else:
        assert any('fake-secret' in text for text in pushed(host))
