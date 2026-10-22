"""Restricted answers and delegated reads re-checked after every wait, through the real modules."""

import json

from test_access_control import access_host as access_host  # noqa: F401
from test_group_roster import member, page, roster_platform
from test_self_qa_access import qa_host as qa_host
from test_self_qa_access import rule


async def whole_group_may_view(host):
    restricted = await rule(host, name='内部机密标题', target='SECRET-R', keywords=['机密'], access='restricted', groups=['group-a'])
    await host.qa({'operation': 'rule_policy', 'id': restricted['id'], 'policy': {'kind': 'group', 'groups': ['group-a', 'group-b']}})
    roster_platform(host)
    host.sender.json_results[('/v2/groups/group-a/members', 'second')] = page(
        member('this-bot', 'admin', bot=True), member('other-bot', bot=True), member('member-b')
    )
    assert (await host.state.sync_roster('bot-a', 'group-a'))['complete']
    await host.event('机密', user='member-a', group='group-a')
    assert host.said() == ['SECRET-R']
    return restricted


def after(host, name, action, *, once=True):
    """Run ``action`` right after a real awaited call of the authorization module returns."""
    acl = host.modules.get('access_control')
    original = getattr(acl, name)
    state = {'pending': True}

    async def wrapped(*args, **kwargs):
        result = await original(*args, **kwargs)
        if state['pending']:
            state['pending'] = not once
            await action(*args)
        return result

    setattr(acl, name, wrapped)
    return lambda: setattr(acl, name, original)


async def test_restricted_answer_unchanged_during_audience_wait_is_sent(qa_host):
    host = qa_host
    await whole_group_may_view(host)

    async def unrelated(*_):
        # Another Bot's rules change; this Bot's rules, scope and authorization do not.
        assert (await host.qa({'operation': 'create', 'name': 'x', 'target': 'X', 'keywords': ['x']}, bot='bot-b'))[0] == 200

    restore = after(host, 'audience_report', unrelated)
    await host.event('机密', user='member-a', group='group-a')
    restore()
    assert host.said() == ['SECRET-R']


async def test_restricted_rule_changed_during_audience_wait_never_sends_old_answer(qa_host):
    host = qa_host
    for change, expected in (
        ({'enabled': False}, []),
        ({'groups': ['group-b']}, []),
        ({'operation': 'rule_policy', 'policy': None}, []),
        ({'operation': 'delete'}, []),
        ({'target': 'NEW-R'}, ['NEW-R']),
    ):
        # A fresh eligible rule each round; earlier rounds left theirs disabled, elsewhere or gone.
        restricted = await whole_group_may_view(host)
        body = {'operation': 'update', 'id': restricted['id'], **change}

        async def owner_change(*_, body=body):
            assert (await host.qa(body))[0] == 200

        restore = after(host, 'audience_report', owner_change)
        await host.event('机密', user='member-a', group='group-a')
        restore()
        out = host.said()
        assert 'SECRET-R' not in out and out == expected, (change, out)


async def test_other_recipient_denied_during_audience_wait_is_status_only(qa_host):
    host = qa_host
    restricted = await whole_group_may_view(host)

    async def deny(*_):
        await host.access.manage_access('bot-a', 'self_qa', 'deny', function=f'rule-{restricted["id"]}-policy', group='group-a', member='member-b')

    restore = after(host, 'audience_report', deny)
    await host.event('机密', user='member-a', group='group-a')
    restore()
    out = host.said()
    assert len(out) == 1 and 'SECRET-R' not in out[0] and '仅限有权成员查看' in out[0]


async def test_public_answer_after_asker_denied_during_wait_is_not_sent(qa_host):
    host = qa_host
    await rule(host, name='甲', target='PUBLIC-A', keywords=['公告'], mode='contains', priority=1)
    await rule(host, name='乙', target='PUBLIC-B', keywords=['公告'], mode='contains', priority=0)

    async def deny(bot, consumer, function, *rest):
        if function == 'public':
            await host.access.manage_access('bot-a', 'self_qa', 'deny', function='public', group='group-a', member='member-a')

    # Denied after the best rule was admitted, while another hit is still evaluated.
    restore = after(host, 'evaluate', deny)
    await host.event('公告', user='member-a', group='group-a')
    restore()
    assert host.said() == ['资格已变化，请重新提问。']
    await host.event('公告', user='member-a', group='group-a')
    assert host.said() == []


async def delegated(host):
    await host.state.confirm_member('bot-a', 'group-a', 'admin-a', evidence='checked', member_role='admin')
    restricted = await rule(host, name='内部机密标题', target='SECRET-R', keywords=['机密口令'], access='restricted', groups=['group-a'])
    public = await rule(host, name='本群公开', target='P', keywords=['本群'], groups=['group-a'])
    await host.qa({'operation': 'delegate', 'groups': ['group-a']})
    return restricted, public


async def test_delegated_list_in_group_does_not_publish_restricted_rules(qa_host):
    host = qa_host
    restricted, public = await delegated(host)
    await host.event('问答管理 ' + json.dumps({'operation': 'list'}), user='admin-a', group='group-a')
    (out,) = host.said()
    assert '内部机密标题' not in out and '机密口令' not in out and restricted['id'] not in out
    listed = json.loads(out)
    assert [r['id'] for r in listed['rules']] == [public['id']] and listed['restricted'] == 1


async def test_delegated_list_rechecks_role_delegation_and_module_after_reading(qa_host):
    host = qa_host
    await delegated(host)
    qa = host.modules.get('self_qa')
    original = qa.manage

    def reading(change):
        state = {'pending': True}

        async def manage(bot, namespace, operation, *args, **kwargs):
            result = await original(bot, namespace, operation, *args, **kwargs)
            if operation == 'list' and state['pending']:
                state['pending'] = False
                await change()
            return result

        qa.manage = manage

    async def demote():
        await host.state.confirm_member('bot-a', 'group-a', 'admin-a', evidence='demoted', member_role='member')

    async def revoke():
        await host.qa({'operation': 'delegate', 'groups': []})

    for change in (demote, revoke):
        reading(change)
        await host.event('问答管理 ' + json.dumps({'operation': 'list'}), user='admin-a', group='group-a')
        qa.manage = original
        assert host.said() == ['管理资格已变化，请重新发送'], change
        await host.state.confirm_member('bot-a', 'group-a', 'admin-a', evidence='checked', member_role='admin')
        await host.qa({'operation': 'delegate', 'groups': ['group-a']})

    async def reload():
        await host.modules.disable('self_qa', _persist=False)

    reading(reload)
    await host.event('问答管理 ' + json.dumps({'operation': 'list'}), user='admin-a', group='group-a')
    assert host.said() == ['管理资格已变化，请重新发送']
