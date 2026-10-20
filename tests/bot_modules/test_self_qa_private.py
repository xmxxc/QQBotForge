"""Restricted answers: whole-group view or private delivery to a verified identity, and private group contexts."""

from socket_platform import until
from test_access_control import access_host as access_host  # noqa: F401
from test_bindings import bind as bind_identity
from test_self_qa_access import qa_host as qa_host
from test_self_qa_access import rule

ADMINS = {'kind': 'role', 'groups': ['group-a', 'group-b'], 'roles': ['admin'], 'private': True}


async def restricted(host, policy=ADMINS, **fields):
    created = await rule(host, **{'name': '内部', 'target': 'SECRET', 'keywords': ['内部'], 'access': 'restricted', **fields})
    status, _ = await host.qa({'operation': 'rule_policy', 'id': created['id'], 'policy': policy})
    assert status == 200
    return created


async def bind(host, group, member, private='private-a'):
    await bind_identity(host, group, member, private)
    host.sender.messages.clear()


def pushes(host):
    return [m for m in host.sender.messages if m[0].startswith('push')]


async def test_group_question_goes_private_to_verified_identity_never_to_group(qa_host):
    host = qa_host
    await restricted(host)
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='checked', member_role='admin')
    # Missing binding: guidance only, no content anywhere.
    await host.event('内部', user='member-a', group='group-a')
    (status,) = host.said()
    assert '身份绑定 申请 group-a member-a' in status and 'SECRET' not in status and pushes(host) == []
    await bind(host, 'group-a', 'member-a')
    host.sender.messages.clear()
    await host.event('内部', user='member-a', group='group-a')
    assert pushes(host) == [('push', 'private-a', 'SECRET')]
    assert host.said() == ['该答案仅限有权成员查看，已私聊发送给你。']
    # Platform rejects the push: real code reported, never a group fallback, grants untouched.
    host.sender.push_result = (False, {'code': 40034, 'message': 'proactive limit'})
    await host.event('内部', user='member-a', group='group-a')
    (status,) = host.said()
    assert '40034' in status and 'SECRET' not in status
    host.sender.push_result = (False, {'code': -1, 'message': '框架网络层请求超时'})
    await host.event('内部', user='member-a', group='group-a')
    assert host.said() == ['该答案仅限有权成员查看，私聊发送结果未知，请先查看私聊，勿重复索取。']
    assert pushes(host) == []
    # An ineligible asker is refused: nothing about the restricted rule is sent.
    await host.state.confirm_member('bot-a', 'group-a', 'member-b', evidence='checked', member_role='member')
    await host.event('内部', user='member-b', group='group-a')
    assert host.said() == [] and pushes(host) == []


async def test_private_chat_reuses_bindings_and_group_choice(qa_host):
    host = qa_host
    await restricted(host)
    await rule(host, name='公开', target='PUBLIC', keywords=['公开'])
    # Public private answers need no binding.
    await host.event('公开', user='stranger')
    assert host.said('stranger') == ['PUBLIC']
    await host.event('内部', user='private-a')
    assert host.said('private-a') == []
    await bind(host, 'group-a', 'member-a')
    await host.event('内部', user='private-a')
    assert host.said('private-a') == ['SECRET']
    # A second eligible group needs an explicit choice.
    await bind(host, 'group-b', 'member-b')
    await host.event('内部', user='private-a')
    (prompt,) = host.said('private-a')
    assert prompt.startswith('该问题需要先选择群') and 'group-a' in prompt and 'group-b' in prompt
    await host.event('问答群 group-b', user='private-a')
    await host.event('内部', user='private-a')
    assert host.said('private-a')[-1] == 'SECRET'
    # Role change in the chosen group takes effect at once.
    await host.state.confirm_member('bot-a', 'group-b', 'member-b', evidence='demoted', member_role='member')
    await host.event('内部', user='private-a')
    assert host.said('private-a')[-1] != 'SECRET'
    # A denial of the private identity blocks every group context.
    await host.event('问答群', user='private-a')
    host.sender.messages.clear()
    await host.access.manage_access('bot-a', 'self_qa', 'deny', function=f'rule-{(await host.qa())[1]["data"]["rules"][0]["id"]}-policy', member='private-a')
    await host.event('内部', user='private-a')
    assert 'SECRET' not in host.said('private-a')


async def test_leave_unload_and_other_bot_stop_restricted_answers(qa_host):
    host = qa_host
    await restricted(host)
    await rule(host, name='公开', target='PUBLIC', keywords=['公开'])
    await bind(host, 'group-a', 'member-a')
    await host.event('内部', user='private-a')
    assert host.said('private-a') == ['SECRET']
    # Leaving the group revokes the context immediately.
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a')
    await until(lambda: host.state.states['bot-a']['groups']['group-a']['members']['member-a'].get('status') == 'left')
    await until(lambda: host.state.recovery.get_status('bot-a').ready)
    await host.event('内部', user='private-a')
    assert host.said('private-a') == []
    # Other Bot has its own (empty) rules.
    await host.event('内部', user='private-a', bot='bot-b')
    assert host.said('private-a') == []
    # Authorization unloaded: restricted refused, public still answered.
    await host.modules.disable('access_control', _persist=False)
    await host.event('内部', user='private-a')
    await host.event('公开', user='private-a')
    assert host.said('private-a') == ['PUBLIC']


async def test_rich_answer_and_rule_buttons_in_private_delivery_are_rechecked(qa_host):
    host = qa_host
    secret = await restricted(host)
    await restricted(host, name='入口', target='ENTRY', keywords=['入口'], buttons=[{'label': '更多', 'action': 'rule', 'value': secret['id']}])
    await bind(host, 'group-a', 'member-a')
    host.sender.messages.clear()
    await host.event('入口', user='member-a', group='group-a')
    # Buttons are sent with the private push; the group only gets the status.
    assert pushes(host)[0][:2] == ('push', 'private-a') and pushes(host)[0][2].startswith('ENTRY')
    assert host.said() == ['该答案仅限有权成员查看，已私聊发送给你。']
    host.sender.messages.clear()
    # The private session belongs to the private identity; after demotion the button is stale.
    await host.event('问答选择 1', user='private-a')
    assert host.said('private-a') == ['SECRET']
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='demoted', member_role='member')
    await host.event('问答选择 1', user='private-a')
    assert host.said('private-a') == ['该答案已失效，请重新提问。']


async def test_disconnect_pauses_restricted_until_trusted_resume(qa_host):
    host = qa_host
    await restricted(host)
    await rule(host, name='公开', target='PUBLIC', keywords=['公开'])
    await bind(host, 'group-a', 'member-a')
    host.sockets['bot-a'].frames.put_nowait(OSError('fake disconnect'))
    await until(lambda: not host.state.recovery.get_status('bot-a').ready)
    await host.event('内部', user='private-a')
    await host.event('公开', user='private-a')
    assert host.said('private-a') == ['PUBLIC']
    second = host.gateways['bot-a'].add()
    second.feed(10, d={'heartbeat_interval': 45000})
    await until(lambda: any(f['op'] == 6 for f in second.sent))
    second.feed(0, t='RESUMED', s=2, d='')
    await until(lambda: host.state._ready('bot-a'))
    await host.event('内部', user='private-a')
    assert host.said('private-a') == ['SECRET']
