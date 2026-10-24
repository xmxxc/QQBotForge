"""#74 real message, interaction and sender boundary with fake identities only."""

import pytest
from account_host import account_host as account_host  # noqa: F401
from test_access_control import access_host as access_host  # noqa: F401
from test_ninja3 import SECRET, grant
from test_ninja3 import ninja as ninja  # noqa: F401


def buttons(host):
    return [b for row in host.sender.keyboards[-1] for b in row]


async def test_menu_entries_and_shared_actual_clicker(ninja, caplog):
    import logging

    caplog.set_level(logging.INFO, logger='ElainaBot')
    h = ninja
    await h.setup()
    await grant(h)
    for command in ('', '菜单'):
        replies = await h.say(command, user='MEMBERA')
        assert len(replies) == 1 and '忍者必须死3 菜单' in replies[0]
        assert [b['text'] for b in buttons(h)] == ['账号列表']
    await h.click(buttons(h)[0]['data'], user='MEMBERA', group='group-a')
    assert '账号A' in h.texts()[0]
    payload = buttons(h)[0]['data']
    await h.click(payload, user='MEMBERB', group='group-a')
    assert SECRET not in str(h.texts())
    assert (await h.ninja_api('GET', 'notices'))[1]['data'][0]['action'] == 'query'
    await h.click(payload, user='MEMBERA', group='group-a')
    assert SECRET in str(h.texts())
    assert any('账号查询 group=group-a member=MEMBERA result=sent' in r.getMessage() for r in caplog.records)


async def test_directory_six_items_pages_and_text(ninja):
    h = ninja
    for i in range(8):
        await h.setup(name=f'账号{i}')
    await grant(h)
    result = (await h.say('账号列表', user='MEMBERA'))[0]
    assert '1/2' in result and '账号5' in result and '账号6' not in result
    assert len(buttons(h)) == 7
    await h.click(buttons(h)[-1]['data'], user='MEMBERA', group='group-a')
    assert '2/2' in h.texts()[0]
    assert [b['text'] for b in buttons(h)] == ['账号6', '账号7', '上一页']
    await h.click(buttons(h)[-1]['data'], user='MEMBERA', group='group-a')
    assert '1/2' in h.texts()[0]
    selection = result.split('发送“')[1].split('”')[0].replace('<编号>', '1')
    assert SECRET in str(await h.say(selection, user='MEMBERA'))


async def test_acl_disabled_names_filtered(ninja):
    h = ninja
    a = await h.setup()
    await grant(h)
    await h.access.declare_resources('bot-a', 'ninja3', [{'id': a['id'], 'enabled': False}])
    assert await h.say('账号列表', user='MEMBERA') == ['本群暂无开放账号']


async def test_owner_menu_is_shared_but_management_rechecks_owner(ninja):
    h = ninja
    await h.setup()
    assert '忍者必须死3 菜单' in (await h.say('菜单', user='MEMBERU'))[0]
    assert not (await h.ninja_api('GET', 'notices'))[1]['data']
    await h.say('菜单', user='owner-a')
    management = buttons(h)[1:]
    assert [b['text'] for b in management] == ['修改密码', '紧急模式']
    for b in management:
        await h.click(b['data'], user='MEMBERA', group='group-a')
        assert h.texts() == ['仅主人可以管理']
        await h.click(b['data'], user='owner-a', group='group-a')
        assert h.texts() == [b['text'] + '尚未交付，当前不可用']


@pytest.mark.parametrize('invalid', ['expiry', 'reload', 'bot', 'group'])
async def test_session_domain_expiry_and_old_instance(ninja, invalid):
    h = ninja
    await h.setup()
    await grant(h)
    await h.setup(bot='bot-b')
    await grant(h, bot='bot-b')
    await h.setup(group='group-b', name='另一群')
    await grant(h, group='group-b')
    now = [1000]
    h.ninja().clock = lambda: now[0]
    result = (await h.say('账号列表', user='MEMBERA'))[0]
    payload = buttons(h)[0]['data']
    selection = result.split('发送“')[1].split('”')[0].replace('<编号>', '1')
    if invalid == 'expiry':
        now[0] += 300
    elif invalid == 'reload':
        await h.plugins.reload('ninja3')
    kwargs = {'user': 'MEMBERA', 'group': 'group-b' if invalid == 'group' else 'group-a', 'bot': 'bot-b' if invalid == 'bot' else 'bot-a'}
    await h.click(payload, **kwargs)
    assert h.texts() == ['菜单已失效，请重新发送：账号列表']
    assert await h.say(selection, **kwargs) == ['菜单已失效，请重新发送：账号列表']


async def test_shared_text_uses_actual_member_and_reads_current_credentials(ninja):
    h = ninja
    a = await h.setup()
    await grant(h)
    result = (await h.say('账号列表', user='MEMBERA'))[0]
    command = result.split('发送“')[1].split('”')[0].replace('<编号>', '1')
    assert SECRET not in str(await h.say(command, user='MEMBERB'))
    await grant(h, 'MEMBERB')
    await h.access.manage_access('bot-a', 'ninja3', 'revoke', function='query', group='group-a', member='MEMBERA')
    await h.ninja_api('PATCH', 'accounts/' + a['id'], {'password': 'fake-current', 'name': '新名称'})
    reply = await h.say(command, user='MEMBERB')
    assert reply == ['名称：新名称\n账号：fake-login\n密码：fake-current']
    assert SECRET not in str(reply)


@pytest.mark.parametrize('change', ['disabled', 'deleted', 'rescope', 'acl_disabled', 'close', 'deny'])
async def test_selected_resource_is_revalidated(ninja, change):
    h = ninja
    a = await h.setup()
    await grant(h)
    await h.say('账号列表', user='MEMBERA')
    payload = buttons(h)[0]['data']
    if change == 'disabled':
        await h.ninja_api('PATCH', 'accounts/' + a['id'], {'enabled': False})
    elif change == 'deleted':
        await h.ninja_api('DELETE', 'accounts/' + a['id'])
    elif change == 'rescope':
        await h.ninja_api('PATCH', 'accounts/' + a['id'], {'open_groups': []})
    elif change == 'acl_disabled':
        await h.access.declare_resources('bot-a', 'ninja3', [{'id': a['id'], 'enabled': False}])
    elif change == 'close':
        await h.ninja_api('POST', 'groups', {'group': 'group-a', 'enabled': False})
    else:
        await h.access.manage_access('bot-a', 'ninja3', 'deny', function='query', group='group-a', member='MEMBERA')
    await h.click(payload, user='MEMBERA', group='group-a')
    assert SECRET not in str(h.texts())


@pytest.mark.parametrize(
    'change', ['password', 'name', 'scope', 'revoke', 'deny', 'leave', 'blacklist', 'resource', 'expire_menu', 'expire_grant', 'reload', 'binding']
)
async def test_final_gate_during_selection(ninja, change, monkeypatch):
    from socket_platform import until

    from core.plugin._blacklist import get_blacklist_map, set_blacklist_map

    h = ninja
    a = await h.setup()
    await grant(h)
    now = [1000]
    h.ninja().clock = lambda: now[0]
    await h.say('账号列表', user='MEMBERA')
    payload = buttons(h)[0]['data']
    acl = h.access
    acl_now = [acl.clock()]
    if change == 'expire_grant':
        acl.clock = lambda: acl_now[0]
        source = (await acl.access_snapshot('bot-a', 'ninja3'))['grants'][0]
        await acl.manage_access(
            'bot-a', 'ninja3', 'expiry', function='query', group='group-a', member='MEMBERA', source=source['id'], expires_at=acl_now[0] + 100
        )
    real = type(acl).evaluate

    async def during(self, bot, consumer, function, group, member, resource='', **kwargs):
        decision = await real(self, bot, consumer, function, group, member, resource, **kwargs)
        if consumer != 'ninja3' or not resource:
            return decision
        if change in {'password', 'name'}:
            await h.ninja_api('PATCH', 'accounts/' + a['id'], {change: 'fake-new'})
        elif change == 'scope':
            await h.ninja_api('PATCH', 'accounts/' + a['id'], {'open_groups': []})
        elif change in {'revoke', 'deny'}:
            await self.manage_access(bot, consumer, change, function=function, group=group, member=member)
        elif change == 'leave':
            h.lifecycle('GROUP_MEMBER_REMOVE', member=member, sequence=2, timestamp=1000)
            await until(lambda: not self.decision_current(bot, decision))
        elif change == 'blacklist':
            set_blacklist_map(bot, 'user', {**get_blacklist_map(bot, 'user'), member: 'fake'})
        elif change == 'resource':
            await self.declare_resources(bot, consumer, [{'id': resource, 'enabled': False}])
        elif change == 'expire_menu':
            now[0] += 300
        elif change == 'expire_grant':
            acl_now[0] += 101
        elif change == 'reload':
            await h.plugins.reload('ninja3')
        elif change == 'binding':
            h.plugins.set_plugin_bots({'ninja3': ['bot-b']})
        return decision

    monkeypatch.setattr(type(acl), 'evaluate', during)
    users = get_blacklist_map('bot-a', 'user')
    try:
        await h.click(payload, user='MEMBERA', group='group-a')
    finally:
        set_blacklist_map('bot-a', 'user', users)
    assert h.texts() == ['账号或授权刚刚变化，请重新发送查询']


@pytest.mark.parametrize('result,attempts', [({'code': 'denied'}, 2), ({'code': -1}, 1), ({'code': 502}, 1), (None, 1), ({'id': 'sent'}, 1), (True, 1)])
async def test_menu_send_results_only_definite_rejection_falls_back(ninja, result, attempts):
    h = ninja
    await h.setup()
    await grant(h)
    original = h.sender.reply
    calls = []

    async def reply(event, content=None, **kwargs):
        calls.append((content, kwargs))
        if kwargs.get('buttons'):
            return result
        return await original(event, content, **kwargs)

    h.sender.reply = reply
    await h.say('账号列表', user='MEMBERA')
    assert len(calls) == attempts
    assert all(SECRET not in str(c) and 'fake-login' not in str(c) for c in calls)
    if attempts == 2:
        assert calls[1][0].startswith(calls[0][0]) and '按钮暂不可用' in calls[1][0]
        selection = calls[1][0].split('发送“')[1].split('”')[0].replace('<编号>', '1')
        assert SECRET in str(await h.say(selection, user='MEMBERA'))


@pytest.mark.parametrize('change', ['account', 'acl', 'expiry'])
async def test_rejected_send_wait_rechecks_before_fallback(ninja, change):
    h = ninja
    a = await h.setup()
    await grant(h)
    now = [1000]
    h.ninja().clock = lambda: now[0]
    original = h.sender.reply
    attempts = []

    async def reply(event, content=None, **kwargs):
        attempts.append(content)
        if kwargs.get('buttons'):
            if change == 'account':
                await h.ninja_api('PATCH', 'accounts/' + a['id'], {'enabled': False})
            elif change == 'acl':
                await h.access.declare_resources('bot-a', 'ninja3', [{'id': a['id'], 'enabled': False}])
            else:
                now[0] += 300
            return {'code': 'fake-rejected'}
        return await original(event, content, **kwargs)

    h.sender.reply = reply
    assert await h.say('账号列表', user='MEMBERA') == ['账号或授权刚刚变化，请重新发送查询']
    assert len(attempts) == 2 and attempts[1] == '账号或授权刚刚变化，请重新发送查询'


async def test_scoped_route_and_self_qa_menu_do_not_double_reply(ninja):
    from core.base.config import cfg

    h = ninja
    await h.setup()
    await grant(h)

    async def qa(body):
        response = await h.http.post('/api/ext/self-qa/', params={'bot': 'bot-a'}, json=body, headers={'Cookie': 'elaina_session=group-test'})
        assert response.status == 200, await response.text()

    await qa({'operation': 'settings', 'groups': ['group-a', 'group-b'], 'private': True})
    await qa({'operation': 'create', 'name': '问答菜单', 'target': '问答自己的菜单', 'keywords': ['菜单'], 'access': 'public'})
    await qa({'operation': 'create', 'name': '普通聊天', 'target': '问答聊天', 'keywords': ['聊天'], 'access': 'public'})
    assert await h.say('菜单', user='MEMBERA', group='group-b') == ['问答自己的菜单']
    assert await h.say('聊天', user='MEMBERA') == ['问答聊天']
    assert '忍者必须死3 菜单' in (await h.say('菜单', user='MEMBERA'))[0]
    # Direct text really uses GROUP_MESSAGE_CREATE, not an artificial AT event.
    cfg.set_bot_setting('bot-a', 'non_at_message.enabled', True)
    out = await h.say('菜单', user='MEMBERA', at_bot=False, event_type='GROUP_MESSAGE_CREATE')
    assert len(out) == 1 and '忍者必须死3 菜单' in out[0]
    assert '账号A' in str(await h.say('账号列表', user='MEMBERA', at_bot=False, event_type='GROUP_MESSAGE_CREATE'))
    cfg.set_bot_setting('bot-a', 'non_at_message.enabled', False)
    assert await h.say('菜单', user='MEMBERA', at_bot=False, event_type='GROUP_MESSAGE_CREATE') == []
    await h.event('菜单', user='MEMBERA')  # C2C stays with self_qa.
    assert h.texts() == ['问答自己的菜单']
    # Interceptor bindings are enforced even though the framework doesn't do that itself.
    h.plugins.set_plugin_bots({'ninja3': ['bot-b']})
    assert await h.say('菜单', user='MEMBERA') == ['问答自己的菜单']


@pytest.mark.parametrize('response,expected', [({'code': 400}, 2), ({'code': 502}, 1), ({'code': -1}, 1), ({'id': 'fake-sent'}, 1)])
async def test_real_message_sender_payload_and_return_shape(ninja, monkeypatch, response, expected):
    from types import SimpleNamespace

    from core.message.sender import MessageSender

    h = ninja
    await h.setup()
    await grant(h)
    sender = MessageSender(SimpleNamespace(appid='bot-a'))
    sent = []

    async def platform_send(self, endpoint, payload, event, content):
        sent.append((endpoint, payload))
        return (False, response) if len(sent) == 1 else (True, {'id': 'fake-text'})

    monkeypatch.setattr(MessageSender, '_send_with_error_handling', platform_send)
    h.application.get_bot('bot-a').sender = sender
    await h.say('账号列表', user='MEMBERA')
    assert len(sent) == expected
    assert sent[0][0] == '/v2/groups/group-a/messages'
    payload = sent[0][1]
    assert payload['msg_type'] == 2
    action = payload['keyboard']['content']['rows'][0]['buttons'][0]['action']
    assert action['permission'] == {'type': 2} and action['data'].startswith('n3:')
    assert SECRET not in str(payload) and 'fake-login' not in str(payload)
    if expected == 2:
        assert sent[1][1]['msg_type'] == 0 and 'keyboard' not in sent[1][1]
    else:
        # Also exercise selection and a credential reply through real Sender.
        sent.clear()
        await h.click(action['data'], user='MEMBERA', group='group-a')
        assert len(sent) == 1 and SECRET in str(sent[0][1])


async def test_text_only_pagination_and_resource_limited_names(ninja):
    from core.base.config import cfg

    h = ninja
    a = await h.setup(name='允许')
    await h.setup(name='不允许')
    await grant(h)
    await h.access.manage_access('bot-a', 'ninja3', 'revoke', function='query', group='group-a', member='MEMBERA')
    await h.access.manage_access('bot-a', 'ninja3', 'grant', function='query', group='group-a', member='MEMBERA', resources=[a['id']])
    cfg.set_bot_setting('bot-a', 'message.use_markdown', False)
    h.sender.keyboards.clear()
    reply = (await h.say('账号列表', user='MEMBERA'))[0]
    assert '1. 允许' in reply and '不允许' not in reply and not h.sender.keyboards
    command = reply.split('发送“')[1].split('”')[0].replace('<编号>', '1')
    assert SECRET in str(await h.say(command, user='MEMBERA'))


async def test_list_final_gate_after_titles_loaded(ninja, monkeypatch):
    h = ninja
    a = await h.setup()
    await grant(h)
    store = h.ninja().store
    # The directory reads its titles through the metadata projection.
    original = store.project

    async def changed(bot, collection, fields):
        result = await original(bot, collection, fields)
        if collection == 'accounts':
            monkeypatch.setattr(store, 'project', original)
            await h.ninja_api('PATCH', 'accounts/' + a['id'], {'name': 'fake-new'})
        return result

    monkeypatch.setattr(store, 'project', changed)
    assert await h.say('账号列表', user='MEMBERA') == ['账号或授权刚刚变化，请重新发送查询']


async def test_transport_exception_does_not_repeat(ninja):
    h = ninja
    await h.setup()
    await grant(h)
    calls = []

    async def broken(event, content=None, **kwargs):
        calls.append(content)
        raise ConnectionResetError('fake transport')

    h.sender.reply = broken
    assert await h.say('账号列表', user='MEMBERA') == []
    assert len(calls) == 1
    calls.clear()
    assert await h.say('账号 账号A', user='MEMBERA') == []
    assert len(calls) == 1 and SECRET in calls[0]


@pytest.mark.parametrize('invalid', ['c2c', 'wrong_app', 'wrong_chat', 'channel', 'blocked'])
async def test_callback_native_scene_validation(ninja, invalid):
    import asyncio

    from core.base import tasks
    from core.message.event import Event
    from core.plugin._blacklist import get_blacklist_map, set_blacklist_map

    h = ninja
    await h.setup()
    await grant(h)
    await h.say('账号列表', user='MEMBERA')
    payload = buttons(h)[0]['data']
    raw = {
        'id': 'fake-click',
        'type': 11,
        'application_id': 'bot-a',
        'scene': 'group',
        'chat_type': 1,
        'group_openid': 'group-a',
        'group_member_openid': 'MEMBERA',
        'data': {'resolved': {'button_data': payload}},
    }
    if invalid == 'c2c':
        raw.update(scene='c2c', chat_type=2, group_openid='', user_openid='MEMBERA')
    elif invalid == 'wrong_app':
        raw['application_id'] = 'bot-b'
    elif invalid == 'wrong_chat':
        raw['chat_type'] = 2
    elif invalid == 'channel':
        raw['channel_id'] = 'fake-channel'
    users = get_blacklist_map('bot-a', 'user')
    if invalid == 'blocked':
        set_blacklist_map('bot-a', 'user', {**users, 'MEMBERA': 'fake'})
    try:
        event = Event.from_websocket('bot-a', {'t': 'INTERACTION_CREATE', 'd': raw})
        event.start_ack_countdown()
        before = set(tasks._background_tasks)
        h.application._prepare_event(event)
        await h.application._on_event(event)
        for _ in range(20):
            started = set(tasks._background_tasks) - before
            if not started:
                break
            await asyncio.gather(*started, return_exceptions=True)
            before |= started
        assert await event.wait_ack_code() == 0
        assert h.texts() == []
    finally:
        set_blacklist_map('bot-a', 'user', users)
