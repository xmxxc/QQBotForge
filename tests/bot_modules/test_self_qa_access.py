"""Per-rule authorization and per-group delegation through the real modules and message entry."""

import json
import shutil
from pathlib import Path

import pytest
from test_access_control import access_host as access_host
from test_group_roster import member, page, roster_platform


@pytest.fixture
async def qa_host(access_host):
    from core.plugin.manager import PluginManager

    host = access_host
    shutil.copytree(
        Path(__file__).absolute().parents[2] / 'modules/self_qa', Path(host.modules._dir) / 'self_qa', ignore=shutil.ignore_patterns('data', '__pycache__')
    )
    host.modules.discover_module('self_qa')
    assert await host.modules.enable('self_qa', _skip_deps=True, _persist=False)
    plugins = Path(host.modules._dir).parent / 'plugins'
    plugins.mkdir()
    host.application._plugin_manager = PluginManager(str(plugins))
    await host.application._plugin_manager.load_all()

    async def qa(body=None, *, bot='bot-a'):
        response = await host.http.request(
            'POST' if body else 'GET', '/api/ext/self-qa/', params={'bot': bot}, json=body, headers={'Cookie': 'elaina_session=group-test'}
        )
        return response.status, await response.json()

    def said(group='group-a'):
        result = [m[2] for m in host.sender.messages if m[1] == group]
        host.sender.messages.clear()
        return result

    host.qa, host.said = qa, said
    status, _ = await qa({'operation': 'settings', 'groups': ['group-a', 'group-b'], 'private': True})
    assert status == 200
    return host


async def rule(host, **fields):
    status, data = await host.qa({'operation': 'create', **fields})
    assert status == 200, data
    return data['data']


async def test_restricted_rules_are_filtered_before_ranking_and_need_whole_group_view(qa_host):
    host = qa_host
    await rule(host, name='公开', target='P', keywords=['规则'])
    restricted = await rule(host, name='内部', target='R', keywords=['规则'], access='restricted')
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='checked', member_role='admin')
    await host.state.confirm_member('bot-a', 'group-a', 'member-b', evidence='checked', member_role='member')
    # Default restricted policy enables no group: nobody sees the restricted title.
    await host.event('规则', user='member-a', group='group-a')
    assert host.said() == ['P']
    status, data = await host.qa({'operation': 'rule_policy', 'id': restricted['id'], 'policy': {'kind': 'role', 'groups': ['group-a'], 'roles': ['admin']}})
    assert status == 200 and data['data']['function'] == f'rule-{restricted["id"]}-policy'
    await host.event('规则', user='member-b', group='group-a')
    assert host.said() == ['P']
    await host.event('规则', user='member-a', group='group-a')
    menu = host.said()[0]
    # The admin may choose it, but the group may not all view it: its title is masked.
    assert '公开' in menu and '受限答案' in menu and '内部' not in menu
    await host.event('问答选择 2', user='member-a', group='group-a')
    (status,) = host.said()
    assert status.startswith('该答案仅限有权成员查看，不能在本群发送。请先私聊 Bot 发送：身份绑定 申请 group-a member-a') and 'R' not in status
    # Every roster member (both Bots included) may view: group send is allowed.
    await host.qa({'operation': 'rule_policy', 'id': restricted['id'], 'policy': {'kind': 'group', 'groups': ['group-a']}})
    roster_platform(host)
    host.sender.json_results[('/v2/groups/group-a/members', 'second')] = page(
        member('this-bot', 'admin', bot=True), member('other-bot', bot=True), member('member-b')
    )
    assert (await host.state.sync_roster('bot-a', 'group-a'))['complete']
    await host.event('规则', user='member-a', group='group-a')
    await host.event('问答选择 2', user='member-a', group='group-a')
    assert host.said()[-1] == 'R'
    # Role revoked: the admin no longer qualifies under a role policy, the title disappears.
    await host.qa({'operation': 'rule_policy', 'id': restricted['id'], 'policy': {'kind': 'role', 'groups': ['group-a'], 'roles': ['admin']}})
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='demoted', member_role='member')
    await host.event('规则', user='member-a', group='group-a')
    assert host.said() == ['P']


async def test_explicit_deny_beats_public_and_public_does_not_widen_scope(qa_host):
    host = qa_host
    await rule(host, name='公开', target='P', keywords=['帮助'])
    await host.event('帮助', user='member-a', group='group-a')
    await host.event('帮助', user='member-a', group='group-c')
    assert host.said() == ['P'] and host.said('group-c') == []
    await host.access.manage_access('bot-a', 'self_qa', 'deny', function='public', group='group-a', member='member-a')
    await host.event('帮助', user='member-a', group='group-a')
    await host.event('帮助', user='member-b', group='group-a')
    assert host.said() == ['P']


async def test_authorization_unavailable_keeps_public_and_refuses_restricted(qa_host):
    host = qa_host
    await rule(host, name='公开', target='P', keywords=['帮助'])
    await rule(host, name='内部', target='R', keywords=['内部'], access='restricted')
    assert (await host.qa())[1]['data']['capabilities']['authorization'] == 'available'
    await host.modules.disable('access_control', _persist=False)
    capabilities = (await host.qa())[1]['data']['capabilities']
    assert {k: capabilities[k] for k in ('authorization', 'restricted', 'delegation', 'delegated_groups')} == {
        'authorization': 'unavailable',
        'restricted': 'refused',
        'delegation': 'paused',
        'delegated_groups': [],
    }
    await host.event('帮助', user='member-a', group='group-a')
    await host.event('内部', user='owner-a', group='group-a')
    assert host.said() == ['P']


async def test_owner_delegates_group_admin_manages_only_this_groups_rules(qa_host):
    host = qa_host
    global_rule = await rule(host, name='全局', target='G', keywords=['全局'])
    await host.state.confirm_member('bot-a', 'group-a', 'admin-a', evidence='checked', member_role='admin')
    await host.state.confirm_member('bot-a', 'group-a', 'member-b', evidence='checked', member_role='member')
    await host.state.confirm_member('bot-a', 'group-b', 'admin-a', evidence='checked', member_role='admin')

    async def command(body, user='admin-a', group='group-a'):
        await host.event('问答管理 ' + json.dumps(body, ensure_ascii=False), user=user, group=group)
        return host.said(group)

    create = {'operation': 'create', 'name': '本群', 'target': 'L', 'keywords': ['本群']}
    # Default: only the owner manages.
    assert await command(create) == []
    status, data = await host.qa({'operation': 'delegate', 'groups': ['group-a']})
    assert status == 200 and data['data']['delegated_groups'] == ['group-a']
    created = json.loads((await command(create))[0])
    saved = next(r for r in (await host.qa())[1]['data']['rules'] if r['id'] == created['id'])
    assert saved['groups'] == ['group-a'] and saved['access'] == 'public'
    await host.event('本群', user='member-b', group='group-a')
    assert host.said() == ['L']
    # Ordinary members, other groups and other Bots are not delegated.
    assert await command(create, user='member-b') == []
    assert await command(create, group='group-b') == []
    await host.event('问答管理 ' + json.dumps(create), user='admin-a', group='group-a', bot='bot-b')
    assert (await host.qa(bot='bot-b'))[1]['data']['rules'] == []
    host.sender.messages.clear()
    # No global, other-group, scope, access, policy or delegation changes.
    assert await command({'operation': 'update', 'id': global_rule['id'], 'target': 'X'}) == ['本群没有该规则']
    assert '委托不能' in (await command({'operation': 'update', 'id': created['id'], 'groups': []}))[0]
    assert '委托不能' in (await command({'operation': 'update', 'id': created['id'], 'access': 'restricted'}))[0]
    assert '委托只能' in (await command({'operation': 'delegate', 'groups': ['group-b']}))[0]
    assert '委托只能' in (await command({'operation': 'settings', 'groups': [], 'private': False}))[0]
    assert '委托只能' in (await command({'operation': 'rule_policy', 'id': created['id'], 'policy': None}))[0]
    assert json.loads((await command({'operation': 'update', 'id': created['id'], 'target': 'L2'}))[0]) == {'id': created['id']}
    # A request decided before a role change cannot write afterwards.
    import importlib

    qa = host.modules.get('self_qa')
    bridge = importlib.import_module(type(qa).__module__.rsplit('.', 1)[0] + '.access').AccessBridge(qa, 'public')
    decision = await bridge.delegate('bot-a', 'group-a', 'admin-a')
    assert decision is not None
    await host.state.confirm_member('bot-a', 'group-a', 'admin-a', evidence='demoted', member_role='member')
    with pytest.raises(PermissionError):
        await qa.manage('bot-a', 'public', 'update', {'target': 'late'}, created['id'], guard=lambda: bridge.current('bot-a', decision))
    assert await command({'operation': 'update', 'id': created['id'], 'target': 'X'}) == []
    # Revoked delegation and unloaded group facts both stop delegated management.
    await host.state.confirm_member('bot-a', 'group-a', 'admin-a', evidence='promoted', member_role='owner')
    assert len(await command({'operation': 'list'})) == 1
    await host.qa({'operation': 'delegate', 'groups': []})
    assert await command({'operation': 'list'}) == []
    await host.qa({'operation': 'delegate', 'groups': ['group-a']})
    await host.modules.disable('group_state', _persist=False)
    assert await command({'operation': 'list'}) == []
    assert next(r for r in (await host.qa())[1]['data']['rules'] if r['id'] == created['id'])['target'] == 'L2'
