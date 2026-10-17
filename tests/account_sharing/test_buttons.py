from test_access import access, grant
from test_binding import bind


def buttons(message):
    return [b for row in message[3].get('keyboard', {}).get('content', {}).get('rows', []) for b in row['buttons']]


def callback(message, label):
    return next(b['action']['data'] for b in buttons(message) if b['render_data']['label'] == label)


async def test_each_account_button_queries_latest_secret_and_ack_is_separate(host):
    account = await host.create()
    await grant(host, account)
    await bind(host)
    await host.event('账户列表', user='private-a')
    menu = host.sender.messages[-1]
    data = callback(menu, '测试账户')
    assert 'fake-secret' not in data
    assert 'fake-secret' not in str(menu)
    await host.api('PATCH', 'accounts/' + account['id'], {'password': 'button-fresh-fake'})
    host.sender.messages.clear()
    event = await host.click(data)
    assert event.callback_code == 0
    assert host.sender.acks[-1] == ('fake-interaction', 0)
    assert host.sender.messages[-1][0:2] == ('private', 'private-a')
    assert 'button-fresh-fake' in host.sender.messages[-1][2]
    await host.api('PATCH', 'accounts/' + account['id'], {'enabled': False})
    host.sender.messages.clear()
    await host.click(data)
    assert not any('button-fresh-fake' in str(m) for m in host.sender.messages)


async def test_personal_callbacks_reject_other_clickers_bot_group_and_framework_denial(host):
    from core.base.config import cfg

    account = await host.create()
    await grant(host, account)
    await bind(host)
    await host.event('账户列表', user='member-a', group='group-a')
    data = callback(host.sender.messages[-1], '测试账户')
    host.sender.messages.clear()
    await host.click(data, user='other-member', group='group-a')
    await host.click(data, user='member-a', group='group-b')
    await host.click(data, bot='bot-b', user='member-a', group='group-a')
    assert not any('fake-secret' in str(m) for m in host.sender.messages)
    cfg.set_bot_setting('bot-a', 'blacklist.user_list', {'member-a': 'framework'})
    await host.click(data, user='member-a', group='group-a')
    assert not any('fake-secret' in str(m) for m in host.sender.messages)
    cfg.set_bot_setting('bot-a', 'blacklist.user_list', {})
    host.sender.messages.clear()
    await host.click(data, user='member-a', group='group-a')
    assert any(m[0:2] == ('private', 'private-a') and 'fake-secret' in m[2] for m in host.sender.messages)
    assert not any(m[0] == 'group' and 'fake-secret' in m[2] for m in host.sender.messages)


async def test_pages_recompute_permissions_and_button_failures_have_text_equivalents(host):
    accounts = [await host.create(name=f'账户{i}') for i in range(8)]
    for account in accounts:
        await grant(host, account)
    await bind(host)
    await host.event('账户列表', user='private-a')
    data = callback(host.sender.messages[-1], '下一页')
    await access(host, 'revoke')
    await grant(host, accounts[-1])
    host.sender.messages.clear()
    await host.click(data)
    assert not any('账户0' in str(m) for m in host.sender.messages)
    host.sender.reject_buttons = True
    await host.event('账户列表', user='private-a')
    text = host.sender.messages[-1][2]
    assert '账户查询 ' + accounts[-1]['id'] in text
    assert '按钮' in text
    assert not host.sender.messages[-1][3].get('buttons')


async def test_damaged_expired_and_foreign_callbacks_can_recover(host):
    account = await host.create()
    await grant(host, account)
    await bind(host)
    await host.event('账户列表', user='private-a')
    data = callback(host.sender.messages[-1], '测试账户')
    host.service.clock = lambda: 99999999999
    host.sender.messages.clear()
    await host.click(data)
    await host.click('as:damaged:broken')
    assert all('fake-secret' not in str(m) for m in host.sender.messages)
    assert any('账户列表' in m[2] for m in host.sender.messages)
    host.sender.messages.clear()
    event = await host.click('another-plugin:action')
    assert event.callback_code is None
    assert host.sender.messages == []


async def test_group_selection_generic_entry_delete_and_private_failure(host):
    account = await host.create()
    await grant(host, account)
    await bind(host)
    await access(host, 'whitelist', group='group-b', enabled=True)
    await access(host, 'grant', group='group-b', member='member-b', resources=[account['id']], identity_type='group_member_openid', confirmed=True)
    await bind(host, group='group-b', member='member-b')
    await host.event('账户列表', user='private-a')
    groups = host.sender.messages[-1]
    await host.click(callback(groups, 'group-b'))
    menu = host.sender.messages[-1]
    data = callback(menu, '测试账户')
    await host.click(callback(menu, '账户列表'))
    assert '测试账户' in host.sender.messages[-1][2]
    host.sender.fail_private = True
    await host.event('账户列表', user='member-b', group='group-b')
    group_data = callback(host.sender.messages[-1], '测试账户')
    host.sender.messages.clear()
    event = await host.click(group_data, user='member-b', group='group-b')
    assert event.callback_code == 0
    assert 'not_friends' in host.sender.messages[-1][2]
    assert not any('fake-secret' in str(m) for m in host.sender.messages)
    host.sender.fail_private = False
    await host.api('DELETE', 'accounts/' + account['id'])
    host.sender.messages.clear()
    await host.click(data)
    assert not any('fake-secret' in str(m) for m in host.sender.messages)


async def test_keyboard_shape_and_text_pagination(host):
    accounts = [await host.create(name=f'账户{i}') for i in range(8)]
    for account in accounts:
        await grant(host, account)
    await bind(host)
    await host.event('账户列表', user='private-a')
    menu = host.sender.messages[-1]
    rows = menu[3]['keyboard']['content']['rows']
    assert len(rows) <= 5 and all(len(row['buttons']) <= 5 for row in rows)
    assert len([b for b in buttons(menu) if b['render_data']['label'].startswith('账户') and b['render_data']['label'] != '账户列表']) == 6
    await host.event('账户列表 2', user='private-a')
    assert '账户6' in host.sender.messages[-1][2] and '账户7' in host.sender.messages[-1][2]
    await host.click(callback(host.sender.messages[-1], '上一页'))
    assert '账户0' in host.sender.messages[-1][2]


async def test_text_fallback_can_page_when_platform_rejects_every_button(host):
    import re

    accounts = [await host.create(name=f'账户{i}') for i in range(8)]
    for account in accounts:
        await grant(host, account)
    await bind(host)
    host.sender.reject_buttons = True
    await host.event('账户列表', user='private-a')
    text = host.sender.messages[-1][2]
    command = re.search(r'账户翻页 as:[^\s]+', text).group(0)
    await host.event(command, user='private-a')
    assert '账户6' in host.sender.messages[-1][2] and '账户7' in host.sender.messages[-1][2]


async def test_private_callback_preserves_distinct_c2c_union_framework_blacklist(host):
    from core.base.config import cfg

    account = await host.create()
    await grant(host, account)
    await host.event('普通聊天', user='member-a', group='group-a', union='group-union')
    await bind(host)
    cfg.set_bot_setting('bot-a', 'identity.use_union_id_for_group', True)
    await host.event('账户列表', user='private-a', union='private-union')
    data = callback(host.sender.messages[-1], '测试账户')
    cfg.set_bot_setting('bot-a', 'blacklist.user_list', {'private-union': 'private framework identity'})
    host.sender.messages.clear()
    await host.click(data)
    assert not any('fake-secret' in str(m) for m in host.sender.messages)


async def test_unmatched_private_chat_remains_silent_when_identity_store_is_unavailable(host):
    host.service.store.close()
    host.sender.messages.clear()
    await host.event('未命中任何规则的普通聊天', user='unseen-private')
    assert host.sender.messages == []
    await host.event('账户列表', user='unseen-private')
    assert host.sender.messages[-1][2] == '你暂无此功能的使用权限，请联系主人'


async def test_group_query_obeys_private_target_deny_changed_during_status_reply(host):
    from core.base.config import cfg

    account = await host.create()
    await grant(host, account)
    await bind(host)

    async def blacklist_private_target(event, text):
        if '尝试私发' in text:
            cfg.set_bot_setting('bot-a', 'blacklist.user_list', {'private-a': 'target denied'})

    host.sender.after_reply = blacklist_private_target
    host.sender.messages.clear()
    await host.event(f'账户查询 {account["id"]}', user='member-a', group='group-a')
    assert not any('fake-secret' in str(m) for m in host.sender.messages)
