"""Multi-part answers re-check before every part; send results follow the real Sender contract."""

from test_access_control import access_host as access_host  # noqa: F401
from test_group_roster import member, page, roster_platform
from test_self_qa import qa_host as qa_host
from test_self_qa_access import qa_host as qa_access_host  # noqa: F401
from test_self_qa_access import rule as access_rule
from test_self_qa_rich import rich
from test_self_qa_selection import setup


async def guide(host):
    (linked,) = await setup(host, {'name': '详情', 'target': '详细说明', 'keywords': ['详情']})
    created = await rich(host, linked['id'])
    host.sender.sent.clear()
    return created, linked


def once_after(host, kind, change):
    async def after_send(sent):
        if sent == kind:
            host.sender.after_send = None
            assert (await host.api(change))[0] == 200

    host.sender.after_send = after_send


async def test_rule_changed_after_the_text_part_stops_the_image(qa_host):
    host = qa_host
    created, _ = await guide(host)
    for change in ({'enabled': False}, {'image': 'https://example.com/new.png'}, {'groups': ['group-b']}):
        once_after(host, 'buttons', {'operation': 'update', 'id': created['id'], **change})
        await host.event('指南', group='group-a')
        out = [m[2] for m in host.replies()]
        assert out == ['**欢迎**', '答案已变化，其余内容未发送，请重新提问。'], (change, out)
        await host.api({'operation': 'update', 'id': created['id'], 'enabled': True, 'image': 'https://example.com/guide.png', 'groups': []})


async def test_rule_changed_while_the_rich_part_was_rejected_resends_current_text(qa_host):
    host = qa_host
    created, _ = await guide(host)
    host.sender.reject.add('buttons')
    edit = {'operation': 'update', 'id': created['id'], 'target': '**新欢迎**', 'image': None}
    original = host.sender.reply
    pending = [edit]

    # The rejected rich attempt delivered nothing; the edit lands before the text fallback.
    async def reply(event, content=None, **kwargs):
        result = await original(event, content, **kwargs)
        if kwargs.get('buttons') and 'code' in result and pending:
            assert (await host.api(pending.pop()))[0] == 200
        return result

    host.sender.reply = reply
    await host.event('指南', group='group-a')
    host.sender.reply = original
    out = [m[2] for m in host.replies()]
    assert all('**欢迎**' not in m for m in out) and out[0].startswith('**新欢迎**\n官网：'), out


async def test_server_error_is_unknown_not_a_rejection(qa_host):
    host = qa_host
    await guide(host)
    host.sender.server_error.add('buttons')
    await host.event('指南', group='group-a')
    # 5xx: the platform may have delivered it; no text re-send, no image.
    assert host.replies() == []
    assert [s['kind'] for s in host.sender.sent] == []
    assert 'buttons' not in (await host.api())[1]['data']['capabilities']['formats']['last_rejections']


async def test_menu_transport_error_is_not_offered_again(qa_host):
    host = qa_host
    await setup(host, *[{'name': f'答案{i}', 'target': f'T{i}', 'mode': 'contains', 'keywords': ['问'], 'priority': 1} for i in range(2)])
    host.sender.raises.add('buttons')
    await host.event('问', group='group-a')
    assert host.replies() == []
    host.sender.raises.clear()
    host.sender.server_error.add('buttons')
    await host.event('问', group='group-a')
    assert host.replies() == []
    # A definite rejection still falls back to the text choice.
    host.sender.server_error.clear()
    host.sender.reject_buttons = True
    await host.event('问', group='group-a')
    assert '按钮暂不可用' in host.replies()[-1][2]


async def test_rule_button_target_changed_during_its_wait_is_not_sent_old(qa_host):
    host = qa_host
    _, linked = await guide(host)
    await host.event('指南', group='group-a')
    callback = host.sender.sent[0]['buttons'][2][0]['data']
    host.replies()
    qa = host.modules.get('self_qa')
    original = qa.settings

    def during_settings(change):
        state = {'pending': True}

        async def settings(*args):
            result = await original(*args)
            if state['pending']:
                state['pending'] = False
                await host.api(change)
            return result

        qa.settings = settings

    during_settings({'operation': 'update', 'id': linked['id'], 'target': '新的说明'})
    assert await host.click(callback, group='group-a') == 0
    assert host.replies() == [('bot-a', 'group-a', '新的说明')]
    during_settings({'operation': 'update', 'id': linked['id'], 'enabled': False})
    assert await host.click(callback, group='group-a') == 0
    qa.settings = original
    assert host.replies() == [('bot-a', 'group-a', '该答案已失效，请重新提问。')]


async def test_recipient_denied_between_parts_stops_restricted_image(qa_access_host):  # noqa: F811
    host = qa_access_host
    restricted = await access_rule(
        host, name='内部', target='SECRET-TEXT', keywords=['机密'], access='restricted', groups=['group-a'], image='https://example.com/secret.png'
    )
    function = f'rule-{restricted["id"]}-policy'
    await host.qa({'operation': 'rule_policy', 'id': restricted['id'], 'policy': {'kind': 'group', 'groups': ['group-a']}})
    roster_platform(host)
    host.sender.json_results[('/v2/groups/group-a/members', 'second')] = page(
        member('this-bot', 'admin', bot=True), member('other-bot', bot=True), member('member-b')
    )
    assert (await host.state.sync_roster('bot-a', 'group-a'))['complete']
    images = []

    async def reply_image(event, image, content='', **kwargs):
        images.append(image)
        return {'id': 'fake-image'}

    host.sender.reply_image = reply_image
    await host.event('机密', user='member-a', group='group-a')
    assert host.said() == ['SECRET-TEXT'] and images == ['https://example.com/secret.png']
    images.clear()
    original = host.sender.reply

    async def reply(event, content=None, **kwargs):
        result = await original(event, content, **kwargs)
        if content == 'SECRET-TEXT':
            await host.access.manage_access('bot-a', 'self_qa', 'deny', function=function, group='group-a', member='member-b')
        return result

    host.sender.reply = reply
    await host.event('机密', user='member-a', group='group-a')
    host.sender.reply = original
    assert host.said() == ['SECRET-TEXT', '答案已变化，其余内容未发送，请重新提问。'] and images == []
