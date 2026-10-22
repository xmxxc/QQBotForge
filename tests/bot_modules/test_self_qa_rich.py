"""Rich answers: Markdown, images and buttons, platform rejection and current-rule clicks."""

from test_access_control import access_host as access_host  # noqa: F401
from test_self_qa import qa_host as qa_host
from test_self_qa_access import qa_host as qa_access_host  # noqa: F401
from test_self_qa_access import rule as access_rule
from test_self_qa_selection import setup

from core.base.config import cfg


async def rich(host, linked_id, **fields):
    status, data = await host.api(
        {
            'operation': 'create',
            'name': '指南',
            'target': '**欢迎**',
            'keywords': ['指南'],
            'format': 'markdown',
            'image': 'https://example.com/guide.png',
            'buttons': [
                {'label': '官网', 'action': 'link', 'value': 'https://example.com'},
                {'label': '再问', 'action': 'command', 'value': '帮助'},
                {'label': '详情', 'action': 'rule', 'value': linked_id},
            ],
            **fields,
        }
    )
    assert status == 200, data
    return data['data']


async def test_markdown_buttons_and_image_reach_the_right_chat(qa_host):
    host = qa_host
    (linked,) = await setup(host, {'name': '详情', 'target': '详细说明', 'keywords': ['详情']})
    await rich(host, linked['id'])
    host.sender.sent.clear()
    await host.event('指南', group='group-a')
    assert host.replies() == [('bot-a', 'group-a', '**欢迎**'), ('bot-a', 'group-a', 'image:https://example.com/guide.png')]
    rows = host.sender.sent[0]['buttons']
    assert host.sender.sent[0]['kind'] == 'buttons'
    assert rows[0] == [{'text': '官网', 'link': 'https://example.com'}] and rows[1] == [{'text': '再问', 'type': 2, 'data': '帮助'}]
    callback = rows[2][0]
    assert callback['type'] == 1 and callback['list'] == ['member-a'] and '详细说明' not in callback['data']
    # Click: the linked rule is read now; edits show, disabling makes the button stale.
    assert await host.click(callback['data'], group='group-a') == 0
    assert host.replies() == [('bot-a', 'group-a', '详细说明')]
    await host.api({'operation': 'update', 'id': linked['id'], 'target': '新的说明'})
    assert await host.click(callback['data'], group='group-a') == 0
    assert host.replies() == [('bot-a', 'group-a', '新的说明')]
    assert await host.click(callback['data'], user='member-b', group='group-a') == 0
    assert host.replies() == [('bot-a', 'group-a', '这不是你的选择菜单。')]
    await host.api({'operation': 'update', 'id': linked['id'], 'enabled': False})
    assert await host.click(callback['data'], group='group-a') == 0
    assert host.replies() == [('bot-a', 'group-a', '该答案已失效，请重新提问。')]
    # Private chat answers go to the private chat.
    await host.event('指南')
    assert [m[1] for m in host.replies()] == ['member-a', 'member-a']


async def test_rejected_formats_degrade_to_text_and_unknown_is_never_resent(qa_host):
    host = qa_host
    (linked,) = await setup(host, {'name': '详情', 'target': '详细说明', 'keywords': ['详情']})
    await rich(host, linked['id'])
    host.sender.reject.add('buttons')
    await host.event('指南')
    text, image = host.replies()
    assert text[2] == '**欢迎**\n官网：https://example.com\n再问：发送「帮助」\n详情：发送「问答选择 3」'
    assert image[2] == 'image:https://example.com/guide.png'
    await host.event('问答选择 3')
    assert host.replies() == [('bot-a', 'member-a', '详细说明')]
    capabilities = (await host.api())[1]['data']['capabilities']['formats']
    assert capabilities['last_rejections']['buttons']['code'] == 'fake-buttons-rejected'
    # Image upload rejected: link instead.
    host.sender.reject = {'image'}
    await host.event('指南')
    assert [m[2] for m in host.replies()] == ['**欢迎**', '图片：https://example.com/guide.png']
    # Unknown result: nothing is re-sent, not even the image.
    host.sender.reject = set()
    host.sender.unknown = {'buttons'}
    await host.event('指南')
    assert host.replies() == []
    # Markdown disabled for this Bot: straight to text, no rich attempt.
    host.sender.unknown = set()
    cfg.set_bot_setting('bot-a', 'message.use_markdown', False)
    host.sender.sent.clear()
    await host.event('指南')
    assert [s['kind'] for s in host.sender.sent] == ['text']
    assert host.replies()[0][2].startswith('**欢迎**\n官网：')
    assert (await host.api())[1]['data']['capabilities']['formats']['markdown'] == 'disabled_by_message.use_markdown'


async def test_invalid_rich_fields_are_rejected(qa_host):
    host = qa_host
    base = {'operation': 'create', 'name': 'x', 'target': 'y', 'keywords': ['x']}
    for fields in (
        {'format': 'html'},
        {'image': 'ftp://example.com/a.png'},
        {'buttons': [{'label': 'a', 'action': 'link', 'value': 'javascript:alert(1)'}]},
        {'buttons': [{'label': 'a', 'action': 'run', 'value': 'x'}]},
        {'buttons': [{'label': str(i), 'action': 'command', 'value': 'x'} for i in range(11)]},
    ):
        assert (await host.api({**base, **fields}))[0] == 422


async def test_rule_buttons_cannot_bypass_restricted_rules(qa_access_host):  # noqa: F811
    host = qa_access_host
    restricted = await access_rule(host, name='内部', target='SECRET', keywords=['内部'], access='restricted')
    await access_rule(host, name='入口', target='公开入口', keywords=['入口'], buttons=[{'label': '内部', 'action': 'rule', 'value': restricted['id']}])
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='checked', member_role='admin')
    await host.event('入口', user='member-a', group='group-a')
    assert host.said() == ['公开入口']
    await host.event('问答选择 1', user='member-a', group='group-a')
    assert host.said() == ['该答案已失效，请重新提问。']
    # Even with permission, a group without whole-group view gets status only.
    await host.qa({'operation': 'rule_policy', 'id': restricted['id'], 'policy': {'kind': 'role', 'groups': ['group-a'], 'roles': ['admin']}})
    await host.event('问答选择 1', user='member-a', group='group-a')
    assert host.said() == ['「内部」仅限有权成员查看，不能在本群公开发送。']
