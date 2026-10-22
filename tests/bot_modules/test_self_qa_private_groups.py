"""Private chat group contexts: Q&A enabled groups, verified bindings and current membership; each rule keeps its own permission."""

from socket_platform import until
from test_access_control import access_host as access_host  # noqa: F401
from test_self_qa_access import qa_host as qa_host
from test_self_qa_access import rule
from test_self_qa_private import bind, restricted

MEMBERS = {'kind': 'role', 'groups': ['group-a', 'group-b'], 'roles': ['admin'], 'private': True}


async def group_rules(host):
    await rule(host, name='全局', target='GLOBAL', keywords=['全局'])
    await rule(host, name='甲群规则', target='GROUP-A', keywords=['群规'], groups=['group-a'])
    await rule(host, name='乙群规则', target='GROUP-B', keywords=['群规'], groups=['group-b'])


def leaks(out):
    return [text for text in out if any(secret in text for secret in ('甲群规则', '乙群规则', 'GROUP-A', 'GROUP-B'))]


async def test_one_valid_group_is_used_without_any_owner_switch_or_choice(qa_host):
    host = qa_host
    await group_rules(host)
    # No binding: public global answers, group rules neither answered nor named.
    await host.event('全局', user='private-a')
    await host.event('群规', user='private-a')
    assert host.said('private-a') == ['GLOBAL']
    await bind(host, 'group-a', 'member-a')
    await host.event('群规', user='private-a')
    assert host.said('private-a') == ['GROUP-A']
    # The owner's existing Q&A enabled groups decide: group-a no longer enabled, no group-a context.
    assert (await host.qa({'operation': 'settings', 'groups': ['group-b'], 'private': True}))[0] == 200
    await host.event('群规', user='private-a')
    await host.event('全局', user='private-a')
    assert host.said('private-a') == ['GLOBAL']
    # Private chat itself disabled: nothing, not even the group context entry.
    assert (await host.qa({'operation': 'settings', 'groups': ['group-a', 'group-b'], 'private': False}))[0] == 200
    await host.event('群规', user='private-a')
    await host.event('问答群 group-a', user='private-a')
    assert host.said('private-a') == []


async def test_several_valid_groups_offer_a_choice_and_switching(qa_host):
    host = qa_host
    await group_rules(host)
    await bind(host, 'group-a', 'member-a')
    await bind(host, 'group-b', 'member-b')
    await host.event('群规', user='private-a')
    (prompt,) = host.said('private-a')
    assert prompt.startswith('该问题需要先选择群') and 'group-a' in prompt and 'group-b' in prompt and not leaks([prompt])
    # Public global answers are never held back by the group choice.
    await host.event('全局', user='private-a')
    assert host.said('private-a') == ['GLOBAL']
    await host.event('问答群 group-b', user='private-a')
    assert host.said('private-a') == ['已选择群 group-b，请重新提问。']
    await host.event('群规', user='private-a')
    assert host.said('private-a') == ['GROUP-B']
    await host.event('问答群 group-a', user='private-a')
    host.said('private-a')
    await host.event('群规', user='private-a')
    assert host.said('private-a') == ['GROUP-A']
    # A group without a verified current context cannot be chosen; the current choice stays.
    await host.event('问答群 group-zzz', user='private-a')
    (refused,) = host.said('private-a')
    assert refused.startswith('无法选择该群') and not leaks([refused])
    await host.event('群规', user='private-a')
    assert host.said('private-a') == ['GROUP-A']
    # Clearing the choice brings the prompt back.
    await host.event('问答群', user='private-a')
    host.said('private-a')
    await host.event('群规', user='private-a')
    assert host.said('private-a')[0].startswith('该问题需要先选择群')


async def test_choice_lists_only_groups_where_this_asker_has_an_answer(qa_host):
    host = qa_host
    await rule(host, name='甲群规则', target='GROUP-A', keywords=['群规'], groups=['group-a'])
    await restricted(host, policy=MEMBERS, name='乙群受限', target='SECRET-B', keywords=['群规'], groups=['group-b'])
    await bind(host, 'group-a', 'member-a')
    await bind(host, 'group-b', 'member-b')
    await host.state.confirm_member('bot-a', 'group-b', 'member-b', evidence='demoted', member_role='member')
    # Only group-a answers this asker: it is used without a prompt naming group-b's restricted rule.
    await host.event('群规', user='private-a')
    (prompt,) = host.said('private-a')
    assert 'group-a' in prompt and 'group-b' not in prompt and '乙群受限' not in prompt


async def test_group_deny_follows_the_member_into_that_groups_private_context_only(qa_host):
    host = qa_host
    await group_rules(host)
    await bind(host, 'group-a', 'member-a')
    await bind(host, 'group-b', 'member-b')
    await host.qa()
    await host.access.manage_access('bot-a', 'self_qa', 'deny', function='public', group='group-a', member='member-a')
    # In group-a the member is refused, and so is group-a's rule in private chat.
    await host.event('群规', user='member-a', group='group-a')
    assert host.said() == []
    await host.event('问答群 group-a', user='private-a')
    host.said('private-a')
    await host.event('群规', user='private-a')
    assert host.said('private-a') == []
    # Not spread to group-b, to global rules answered as the private identity, or to another Bot.
    await host.event('问答群 group-b', user='private-a')
    host.said('private-a')
    await host.event('群规', user='private-a')
    await host.event('全局', user='private-a')
    assert host.said('private-a') == ['GROUP-B', 'GLOBAL']
    await host.access.declare('bot-b', 'self_qa', owner='module:self_qa', functions={'public': {'kind': 'public'}})
    await host.access.manage_access('bot-b', 'self_qa', 'deny', function='public', group='group-b', member='member-b')
    await host.event('群规', user='private-a')
    assert host.said('private-a') == ['GROUP-B']


async def test_private_identity_deny_is_not_bypassed_through_a_group_identity(qa_host):
    host = qa_host
    await group_rules(host)
    await bind(host, 'group-a', 'member-a')
    await host.event('群规', user='private-a')
    assert host.said('private-a') == ['GROUP-A']
    await host.access.manage_access('bot-a', 'self_qa', 'deny', function='public', member='private-a')
    await host.event('群规', user='private-a')
    await host.event('全局', user='private-a')
    assert host.said('private-a') == []
    # The bound member still answers in the group itself.
    await host.event('群规', user='member-a', group='group-a')
    assert host.said() == ['GROUP-A']


async def test_context_follows_leaving_unbinding_scope_and_reload(qa_host):
    host = qa_host
    await group_rules(host)
    await bind(host, 'group-a', 'member-a')
    await bind(host, 'group-b', 'member-b')
    await host.event('问答群 group-a', user='private-a')
    host.said('private-a')
    await host.event('群规', user='private-a')
    assert host.said('private-a') == ['GROUP-A']
    # Reload forgets the choice; two valid groups again need one.
    await host.modules.disable('self_qa', _persist=False)
    assert await host.modules.enable('self_qa', _skip_deps=True, _persist=False)
    await host.event('群规', user='private-a')
    assert host.said('private-a')[0].startswith('该问题需要先选择群')
    await host.event('问答群 group-a', user='private-a')
    host.said('private-a')
    # Unbound from group-a: the old choice no longer applies; group-b is the only valid group.
    await host.access.bindings.manage('bot-a', 'binding_revoke', {'group': 'group-a', 'member': 'member-a'}, 'owner')
    await host.event('群规', user='private-a')
    assert host.said('private-a') == ['GROUP-B']
    # Left group-b: no valid group, nothing named.
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-b', group='group-b')
    await until(lambda: host.state.states['bot-a']['groups']['group-b']['members']['member-b'].get('status') == 'left')
    await until(lambda: host.state.recovery.get_status('bot-a').ready)
    await host.event('群规', user='private-a')
    assert host.said('private-a') == []


async def test_old_menu_is_rechecked_after_the_context_changes(qa_host):
    host = qa_host
    await rule(host, name='甲一', target='A1', keywords=['群规'], groups=['group-a'])
    await rule(host, name='甲二', target='A2', keywords=['群规'], groups=['group-a'])
    await bind(host, 'group-a', 'member-a')
    await host.event('群规', user='private-a')
    (menu,) = host.said('private-a')
    assert '甲一' in menu and '甲二' in menu
    assert (await host.qa({'operation': 'settings', 'groups': ['group-b'], 'private': True}))[0] == 200
    await host.event('问答选择 1', user='private-a')
    assert host.said('private-a') == ['该答案已失效，请重新提问。']


async def test_no_separate_private_group_switch(qa_host):
    host = qa_host
    status, data = await host.qa({'operation': 'private_groups', 'groups': ['group-a']})
    assert status == 422 and 'private_groups' not in data['error']
    capabilities = (await host.qa())[1]['data']['capabilities']
    assert 'private_groups' not in capabilities
    snapshot = await host.access.access_snapshot('bot-a', 'self_qa')
    assert 'private-group' not in snapshot['functions']


async def test_leftover_private_group_function_from_development_data_is_ignored(qa_host):
    host = qa_host
    await group_rules(host)
    # An earlier development build declared this function and the owner opened only group-b with it.
    old = {'kind': 'group', 'groups': ['group-b'], 'private': True}
    await host.access.declare('bot-a', 'self_qa', owner='module:self_qa', functions={'private-group': old})
    await bind(host, 'group-a', 'member-a')
    await host.event('群规', user='private-a')
    assert host.said('private-a') == ['GROUP-A']
    assert 'private_groups' not in (await host.qa())[1]['data']['capabilities']
