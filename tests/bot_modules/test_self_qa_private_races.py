"""Private delivery re-checked after identity waits; delegated details go private; private group contexts."""

import json

from socket_platform import until
from test_access_control import access_host as access_host  # noqa: F401
from test_self_qa_access import qa_host as qa_host
from test_self_qa_access import rule
from test_self_qa_private import ADMINS, bind, pushes, restricted


def after(host, name, action):
    """Run ``action`` once, right after a real awaited call of the authorization module returns."""
    acl = host.modules.get('access_control')
    original = getattr(acl, name)
    state = {'pending': True}

    async def wrapped(*args, **kwargs):
        result = await original(*args, **kwargs)
        if state['pending']:
            state['pending'] = False
            await action()
        return result

    setattr(acl, name, wrapped)
    return lambda: setattr(acl, name, original)


def owner(host, body):
    async def change():
        assert (await host.qa(body))[0] == 200

    return change


async def secret_by_private_delivery(host, **fields):
    created = await restricted(host, target='SECRET-P', keywords=['机密'], **fields)
    await bind(host, 'group-a', 'member-a')
    await host.event('机密', user='member-a', group='group-a')
    assert pushes(host)[0] == ('push', 'private-a', 'SECRET-P')
    host.sender.messages.clear()
    return created


async def test_private_delivery_unchanged_during_identity_wait_is_sent(qa_host):
    host = qa_host
    await secret_by_private_delivery(host)
    restore = after(host, 'member_context', owner(host, {'operation': 'settings', 'groups': ['group-a', 'group-b'], 'private': True}))
    await host.event('机密', user='member-a', group='group-a')
    restore()
    assert pushes(host) == [('push', 'private-a', 'SECRET-P')]
    assert host.said() == ['该答案仅限有权成员查看，已私聊发送给你。']


async def test_rule_changed_during_identity_wait_never_pushes_old_answer(qa_host):
    host = qa_host
    for change, expected in (
        ({'enabled': False}, []),
        ({'groups': ['group-b']}, []),
        ({'operation': 'rule_policy', 'policy': None}, []),
        ({'operation': 'delete'}, []),
        ({'target': 'NEW-P'}, [('push', 'private-a', 'NEW-P')]),
    ):
        # A fresh eligible rule each round; earlier rounds left theirs disabled, elsewhere or gone.
        created = await secret_by_private_delivery(host)
        restore = after(host, 'member_context', owner(host, {'operation': 'update', 'id': created['id'], **change}))
        await host.event('机密', user='member-a', group='group-a')
        restore()
        assert pushes(host) == expected, change
        assert not any('SECRET-P' in str(m) for m in host.sender.messages), change
        host.sender.messages.clear()


async def test_native_private_answer_rechecks_rule_after_context_wait(qa_host):
    host = qa_host
    created = await restricted(host, target='SECRET-N', keywords=['机密'])
    await bind(host, 'group-a', 'member-a')
    await host.event('机密', user='private-a')
    assert host.said('private-a') == ['SECRET-N']
    restore = after(host, 'private_contexts', owner(host, {'operation': 'update', 'id': created['id'], 'enabled': False}))
    await host.event('机密', user='private-a')
    restore()
    assert host.said('private-a') == []


async def test_private_image_part_withheld_after_rule_change(qa_host):
    host = qa_host
    created = await secret_by_private_delivery(host, image='https://example.com/secret.png')
    host.sender.messages.clear()
    original = host.sender.send_to_user

    async def send_to_user(user_id, content=None, **kwargs):
        result = await original(user_id, content, **kwargs)
        assert (await host.qa({'operation': 'update', 'id': created['id'], 'enabled': False}))[0] == 200
        return result

    host.sender.send_to_user = send_to_user
    await host.event('机密', user='member-a', group='group-a')
    host.sender.send_to_user = original
    assert pushes(host) == [('push', 'private-a', 'SECRET-P')]
    assert host.said() == ['该答案仅限有权成员查看，已私聊发送部分内容；资格已变化，其余内容未发送。']


async def delegated(host):
    await rule(host, name='内部机密标题', target='SECRET-R', keywords=['机密口令'], access='restricted', groups=['group-a'])
    await rule(host, name='本群公开', target='P', keywords=['本群'], groups=['group-a'])
    await host.qa({'operation': 'delegate', 'groups': ['group-a']})
    await host.state.confirm_member('bot-a', 'group-a', 'admin-a', evidence='checked', member_role='admin')


async def list_in_group(host):
    """The group reply (parsed) and the private pushes of one delegated list."""
    await host.event('问答管理 ' + json.dumps({'operation': 'list'}), user='admin-a', group='group-a')
    private = pushes(host)
    (out,) = host.said()
    assert '内部机密标题' not in out and '机密口令' not in out
    return json.loads(out), private


async def test_delegated_restricted_details_go_to_verified_private_identity(qa_host):
    host = qa_host
    await delegated(host)
    # No binding: guidance, no details, no delivery claim.
    listed, private = await list_in_group(host)
    assert listed['restricted'] == 1 and '身份绑定 申请 group-a admin-a' in listed['restricted_detail'] and private == []
    assert [r['name'] for r in listed['rules']] == ['本群公开']
    await bind(host, 'group-a', 'admin-a', 'private-admin')
    listed, private = await list_in_group(host)
    assert listed['restricted_detail'] == '已私聊发送'
    ((_, to, content),) = private
    assert to == 'private-admin' and [r['name'] for r in json.loads(content)] == ['内部机密标题']
    # Push rejected or unknown: reported as such, never claimed.
    host.sender.push_result = (False, {'code': 40034, 'message': 'proactive limit'})
    assert '40034' in (await list_in_group(host))[0]['restricted_detail']
    host.sender.push_result = (False, {'code': -1, 'message': '框架网络层请求超时'})
    assert '未知' in (await list_in_group(host))[0]['restricted_detail']
    host.sender.push_result = (True, {'id': 'fake-push'})
    # Delegation revoked while the identity was looked up: nothing private, no details.
    restore = after(host, 'member_context', owner(host, {'operation': 'delegate', 'groups': []}))
    await host.event('问答管理 ' + json.dumps({'operation': 'list'}), user='admin-a', group='group-a')
    restore()
    assert host.said() == ['管理资格已变化，请重新发送'] and pushes(host) == []


async def open_private_group(host, groups=('group-a',)):
    status, data = await host.qa({'operation': 'private_groups', 'groups': list(groups)})
    assert status == 200 and data['data']['private_groups'] == list(groups)


async def test_private_chat_matches_rules_of_a_selected_verified_group(qa_host):
    host = qa_host
    await rule(host, name='全局', target='GLOBAL', keywords=['全局'])
    await rule(host, name='甲群', target='GROUP-A', keywords=['群规'], groups=['group-a'])
    await rule(host, name='乙群', target='GROUP-B', keywords=['群规'], groups=['group-b'])
    # Public global answers need no binding or choice.
    await host.event('全局', user='stranger')
    assert host.said('stranger') == ['GLOBAL']
    # Nothing selected: group rules are not matched or listed.
    await host.event('群规', user='private-a')
    assert host.said('private-a') == []
    # Any typed group ID needs a current verified context in a group the owner opened.
    await host.event('问答群 group-a', user='private-a')
    host.sender.messages.clear()
    await host.event('群规', user='private-a')
    assert host.said('private-a') == []
    await bind(host, 'group-a', 'member-a')
    await host.event('群规', user='private-a')
    assert host.said('private-a') == []
    await open_private_group(host)
    await host.event('群规', user='private-a')
    assert host.said('private-a') == ['GROUP-A']
    # Global rules still take part, ranked as before.
    await host.event('全局', user='private-a')
    assert host.said('private-a') == ['GLOBAL']
    # Another group's rules: not bound there, not opened, nothing.
    await host.event('问答群 group-b', user='private-a')
    host.sender.messages.clear()
    await host.event('群规', user='private-a')
    assert host.said('private-a') == []
    # Group chat scope is unchanged.
    await host.event('群规', user='member-z', group='group-b')
    assert host.said('group-b') == ['GROUP-B']


async def test_private_group_context_lost_during_wait_is_not_answered(qa_host):
    host = qa_host
    await rule(host, name='甲群', target='GROUP-A', keywords=['群规'], groups=['group-a'])
    await bind(host, 'group-a', 'member-a')
    await open_private_group(host)
    await host.event('问答群 group-a', user='private-a')
    host.sender.messages.clear()
    await host.event('群规', user='private-a')
    assert host.said('private-a') == ['GROUP-A']

    async def leave():
        host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a')
        await until(lambda: host.state.states['bot-a']['groups']['group-a']['members']['member-a'].get('status') == 'left')

    restore = after(host, 'private_contexts', leave)
    await host.event('群规', user='private-a')
    restore()
    assert host.said('private-a') == ['资格已变化，请重新提问。']
    await until(lambda: host.state.recovery.get_status('bot-a').ready)
    await host.event('群规', user='private-a')
    assert host.said('private-a') == []


async def test_private_group_rules_keep_restricted_policy(qa_host):
    host = qa_host
    await restricted(host, policy=ADMINS, target='GROUP-SECRET', keywords=['群密'], groups=['group-a'])
    await bind(host, 'group-a', 'member-a')
    await open_private_group(host)
    await host.event('问答群 group-a', user='private-a')
    host.sender.messages.clear()
    await host.event('群密', user='private-a')
    assert host.said('private-a') == ['GROUP-SECRET']
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='demoted', member_role='member')
    await host.event('群密', user='private-a')
    assert host.said('private-a') == []


async def test_menu_titles_follow_rule_changes_during_title_checks(qa_host):
    host = qa_host
    await restricted(host, name='内部', target='SECRET', keywords=['问'], mode='contains', priority=1)
    public = await rule(host, name='公开旧名', target='PUBLIC', keywords=['问'], mode='contains', priority=1)
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='checked', member_role='admin')
    # The restricted title needs a whole-group report; the public rule is renamed meanwhile.
    restore = after(host, 'audience_report', owner(host, {'operation': 'update', 'id': public['id'], 'name': '公开新名'}))
    await host.event('问', user='member-a', group='group-a')
    restore()
    (menu,) = host.said()
    assert '公开新名' in menu and '公开旧名' not in menu and '内部' not in menu and '受限答案' in menu


async def test_menu_not_sent_when_asker_loses_eligibility_during_title_checks(qa_host):
    host = qa_host
    await restricted(host, name='内部', target='SECRET', keywords=['问'], mode='contains', priority=1)
    await rule(host, name='公开', target='PUBLIC', keywords=['问'], mode='contains', priority=1)
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='checked', member_role='admin')

    async def deny():
        await host.access.manage_access('bot-a', 'self_qa', 'deny', function='public', group='group-a', member='member-a')

    restore = after(host, 'audience_report', deny)
    await host.event('问', user='member-a', group='group-a')
    restore()
    assert host.said() == ['资格已变化，请重新提问。']


async def test_private_rule_button_reaches_group_rule_only_with_verified_context(qa_host):
    host = qa_host
    target = await rule(host, name='甲群详情', target='GROUP-DETAIL', keywords=['甲详情'], groups=['group-a'])
    await rule(host, name='入口', target='ENTRY', keywords=['入口'], buttons=[{'label': '详情', 'action': 'rule', 'value': target['id']}])
    await bind(host, 'group-a', 'member-a')
    await host.event('入口', user='private-a')
    assert host.said('private-a')[0].startswith('ENTRY')
    # No chosen group context: the group rule behind the button is not reachable.
    await host.event('问答选择 1', user='private-a')
    assert host.said('private-a') == ['该答案已失效，请重新提问。']
    await open_private_group(host)
    await host.event('问答群 group-a', user='private-a')
    host.sender.messages.clear()
    await host.event('问答选择 1', user='private-a')
    assert host.said('private-a') == ['GROUP-DETAIL']


async def test_delegated_details_follow_rule_changes_and_report_late_revocation(qa_host):
    host = qa_host
    await delegated(host)
    await bind(host, 'group-a', 'admin-a', 'private-admin')
    secret = next(r for r in (await host.qa())[1]['data']['rules'] if r['access'] == 'restricted')
    # A restricted rule renamed while the identity was looked up: no stale details pushed, current ones are.
    restore = after(host, 'member_context', owner(host, {'operation': 'update', 'id': secret['id'], 'name': '改名'}))
    listed, private = await list_in_group(host)
    restore()
    ((_, to, content),) = private
    assert to == 'private-admin' and [r['name'] for r in json.loads(content)] == ['改名']
    assert listed['restricted_detail'] == '已私聊发送' and [r['name'] for r in listed['rules']] == ['本群公开']
    # Revoked right after the push: the group hears that, and that the push went out.
    original = host.sender.send_to_user

    async def send_to_user(user_id, content=None, **kwargs):
        result = await original(user_id, content, **kwargs)
        await host.qa({'operation': 'delegate', 'groups': []})
        return result

    host.sender.send_to_user = send_to_user
    await host.event('问答管理 ' + json.dumps({'operation': 'list'}), user='admin-a', group='group-a')
    host.sender.send_to_user = original
    assert [m[1] for m in pushes(host)] == ['private-admin']
    assert host.said() == ['管理资格已变化，请重新发送（受限详情：已私聊发送）']


async def test_delegated_list_rechecks_rules_and_manager_after_the_private_push(qa_host):
    host = qa_host
    await delegated(host)
    await bind(host, 'group-a', 'admin-a', 'private-admin')
    original = host.sender.send_to_user

    def during_push(change):
        async def send_to_user(user_id, content=None, **kwargs):
            result = await original(user_id, content, **kwargs)
            host.sender.send_to_user = original
            await change()
            return result

        host.sender.send_to_user = send_to_user

    def public_id():
        return next(r['id'] for r in rules if r['name'] == '本群公开')

    async def reload():
        await host.modules.disable('self_qa', _persist=False)
        assert await host.modules.enable('self_qa', _skip_deps=True, _persist=False)

    async def demote():
        await host.state.confirm_member('bot-a', 'group-a', 'admin-a', evidence='demoted', member_role='member')

    rules = (await host.qa())[1]['data']['rules']
    for update in (
        {'access': 'restricted', 'name': '刚改为受限的标题'},
        {'enabled': False},
        {'name': '新标题', 'keywords': ['新问法']},
        {'groups': ['group-b']},
    ):
        during_push(owner(host, {'operation': 'update', 'id': public_id(), **update}))
        await host.event('问答管理 ' + json.dumps({'operation': 'list'}), user='admin-a', group='group-a')
        # The push went out and cannot be undone; the group gets only that status, no old public listing.
        assert [m[1] for m in pushes(host)] == ['private-admin'], update
        assert host.said() == ['规则已变化，请重新发送（受限详情：已私聊发送）'], update
        await host.qa({'operation': 'delete', 'id': public_id()})
        await rule(host, name='本群公开', target='P', keywords=['本群'], groups=['group-a'])
        rules = (await host.qa())[1]['data']['rules']

    during_push(owner(host, {'operation': 'delete', 'id': public_id()}))
    await host.event('问答管理 ' + json.dumps({'operation': 'list'}), user='admin-a', group='group-a')
    assert len(pushes(host)) == 1 and host.said() == ['规则已变化，请重新发送（受限详情：已私聊发送）']
    await rule(host, name='本群公开', target='P', keywords=['本群'], groups=['group-a'])

    for change in (demote, reload):
        during_push(change)
        await host.event('问答管理 ' + json.dumps({'operation': 'list'}), user='admin-a', group='group-a')
        assert len(pushes(host)) == 1 and host.said() == ['管理资格已变化，请重新发送（受限详情：已私聊发送）'], change
        await host.state.confirm_member('bot-a', 'group-a', 'admin-a', evidence='checked', member_role='admin')

    # Another Bot's rules change during the push: the list stands.
    async def other_bot():
        assert (await host.qa({'operation': 'create', 'name': 'x', 'target': 'X', 'keywords': ['x']}, bot='bot-b'))[0] == 200

    during_push(other_bot)
    listed, private = await list_in_group(host)
    assert listed['restricted_detail'] == '已私聊发送' and [r['name'] for r in listed['rules']] == ['本群公开'] and len(private) == 1


async def test_delegated_list_still_changing_returns_only_a_status(qa_host):
    host = qa_host
    await delegated(host)
    await bind(host, 'group-a', 'admin-a', 'private-admin')
    counter = {'n': 0}

    async def churn():
        counter['n'] += 1
        assert (await host.qa({'operation': 'create', 'name': f'变化{counter["n"]}', 'target': 'T', 'keywords': [f'问{counter["n"]}'], 'groups': ['group-a']}))[
            0
        ] == 200

    acl = host.modules.get('access_control')
    original = acl.member_context

    async def member_context(*args, **kwargs):
        result = await original(*args, **kwargs)
        await churn()
        return result

    acl.member_context = member_context
    await host.event('问答管理 ' + json.dumps({'operation': 'list'}), user='admin-a', group='group-a')
    acl.member_context = original
    # Changed before every push: nothing pushed, no titles in the group.
    assert pushes(host) == [] and host.said() == ['规则正在变化，请稍后重新发送'] and counter['n'] == 3
