from test_access import access, grant
from test_binding import bind


async def rule(host, target, keywords, **kwargs):
    response = await host.api('POST', 'rules', {'name': '规则', 'target': target, 'keywords': keywords, **kwargs})
    assert response.status == 201, await response.text()
    return (await response.json())['data']


async def test_keywords_use_current_permissions_dedupe_and_latest_account(host):
    account = await host.create(name='公开候选')
    hidden = await host.create(name='不可见候选')
    await grant(host, account)
    await bind(host)
    first = await rule(host, account['id'], ['别名', '另一别名'])
    await rule(host, account['id'], ['别名'])
    await rule(host, hidden['id'], ['别名'])
    host.sender.messages.clear()
    await host.event('别名', user='private-a')
    assert len([m for m in host.sender.messages if 'fake-secret' in m[2]]) == 1
    assert not any('不可见候选' in str(m) for m in host.sender.messages)
    await host.api('PATCH', 'accounts/' + account['id'], {'password': 'fresh-fake-secret'})
    await host.event('另一别名', user='private-a')
    assert 'fresh-fake-secret' in host.sender.messages[-1][2]
    await host.api('PATCH', 'rules/' + first['id'], {'enabled': False})
    host.sender.messages.clear()
    await host.event('另一别名', user='private-a')
    assert not host.sender.messages
    await access(host, 'revoke')
    await host.event('别名', user='private-a')
    assert not any('fake-secret' in str(m) for m in host.sender.messages)


async def test_rule_order_ambiguity_keywords_and_bot_isolation(host):
    a = await host.create(name='账户甲')
    b = await host.create(name='账户乙')
    await grant(host, a)
    await grant(host, b)
    await bind(host)
    await rule(host, a['id'], ['密码'], mode='contains')
    await rule(host, b['id'], ['测试密码'], mode='contains')
    host.sender.messages.clear()
    await host.event('测试密码', user='private-a')
    assert '账户乙' in host.sender.messages[-1][2]
    exact = await rule(host, a['id'], ['测试密码'])
    await host.event('测试密码', user='private-a')
    assert '账户甲' in host.sender.messages[-1][2]
    await host.api('PATCH', 'rules/' + exact['id'], {'enabled': False})
    await rule(host, a['id'], ['测'], mode='contains', priority=10)
    await host.event('测试密码', user='private-a')
    assert '账户甲' in host.sender.messages[-1][2]
    await rule(host, b['id'], ['密码'], mode='contains', priority=10)
    host.sender.messages.clear()
    await host.event('测试密码', user='private-a')
    assert '账户甲' in host.sender.messages[-1][2] and '账户乙' in host.sender.messages[-1][2]
    assert not any('fake-secret' in str(m) for m in host.sender.messages)
    host.sender.messages.clear()
    await host.event('测试密码', bot='bot-b', user='private-a')
    await host.event('未命中普通聊天', user='private-a')
    assert not host.sender.messages


async def test_static_replies_are_reusable_and_consumer_controls_admission(host):
    module = host.service.qa_provider()
    await module.manage('bot-a', 'other_consumer', 'create', {'name': 'FAQ', 'kind': 'text', 'target': '静态答案', 'keywords': ['问题']})
    assert await module.match('bot-a', 'account_sharing', '问题') == []
    assert await module.match('bot-b', 'other_consumer', '问题') == []
    host.sender.messages.clear()
    await module.answer('bot-a', 'other_consumer', '问题', lambda text: host.sender.send_to_group('fake-group', text), allowed=False)
    assert host.sender.messages == []
    await module.answer('bot-a', 'other_consumer', '问题', lambda text: host.sender.send_to_group('fake-group', text), allowed=True)
    assert host.sender.messages[-1][2] == '静态答案'


async def test_rule_commands_keyword_toggle_and_static_private_delivery(host):
    account = await host.create()
    await grant(host, account)
    await bind(host)
    await host.event('账户管理 规则新增 {"name":"静态提示","kind":"text","target":"假静态回复","keywords":["帮助",{"text":"关闭词","enabled":false}]}')
    response = await host.api('GET', 'rules')
    saved = (await response.json())['data'][0]
    assert saved['mode'] == 'exact'
    host.sender.messages.clear()
    await host.event('关闭词', user='private-a')
    assert host.sender.messages == []
    await host.event('帮助', user='member-a', group='group-a')
    assert not any(m[0] == 'group' and '假静态回复' in m[2] for m in host.sender.messages)
    assert any(m[0:2] == ('private', 'private-a') and m[2] == '假静态回复' for m in host.sender.messages)
    await host.event('账户管理 规则修改 ' + saved['id'] + ' {"target":"更新静态回复"}')
    await host.event('帮助', user='private-a')
    assert host.sender.messages[-1][2] == '更新静态回复'
    await host.event('账户管理 规则删除 ' + saved['id'])
    host.sender.messages.clear()
    await host.event('帮助', user='private-a')
    assert host.sender.messages == []


async def test_module_match_contract_returns_stable_refs_and_no_external_actions(host):
    module = host.service.qa_provider()
    short = await module.manage('bot-a', 'other', 'create', {'name': '短', 'kind': 'text', 'target': '短答案', 'keywords': ['词'], 'mode': 'contains'})
    long = await module.manage('bot-a', 'other', 'create', {'name': '长', 'kind': 'text', 'target': '长答案', 'keywords': ['关键词'], 'mode': 'contains'})
    matches = await module.match('bot-a', 'other', '关键词')
    assert [r['rule_id'] for r in module.best(matches)] == [long['id']]
    exact = await module.manage('bot-a', 'other', 'create', {'name': '精确', 'kind': 'text', 'target': '精确答案', 'keywords': ['关键词']})
    assert module.best(await module.match('bot-a', 'other', '关键词'))[0]['rule_id'] == exact['id']
    assert short['id'] != long['id']
    try:
        await module.manage('bot-a', 'other', 'create', {'name': '外部动作', 'kind': 'execute', 'target': 'fake', 'keywords': ['x']})
    except ValueError:
        pass
    else:
        raise AssertionError('External actions must not be accepted')


async def test_disabling_rule_during_status_send_cancels_pending_keyword_reply(host):
    account = await host.create()
    await grant(host, account)
    await bind(host)
    saved = await rule(host, account['id'], ['切换中'])

    async def disable_after_hint(event, text):
        if '尝试私发' in text:
            await host.api('PATCH', 'rules/' + saved['id'], {'enabled': False})

    host.sender.after_reply = disable_after_hint
    host.sender.messages.clear()
    await host.event('切换中', user='member-a', group='group-a')
    assert not any('fake-secret' in str(m) for m in host.sender.messages)
