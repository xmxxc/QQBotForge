"""Independent module installation and owner management behavior."""

import pytest


async def test_independent_known_groups_owner_entry_refresh_and_restart(group_host):
    host = group_host
    assert (await host.api(authenticated=False)).status == 401
    await host.event('普通聊天', user='member-a', group='group-a')
    state = (await (await host.api()).json())['data']
    assert state['scope'] == 'known' and state['complete'] is False
    assert [g['group'] for g in state['groups']] == ['group-a']
    assert state['groups'][0]['reliable'] is False
    assert (await (await host.api(bot='bot-b')).json())['data']['groups'] == []
    assert (await host.api('POST', 'refresh', body={'group': 'group-a'}, origin='https://external.example')).status == 403
    host.sender.messages.clear()
    await host.event('群状态', user='ordinary')
    assert host.sender.messages == []
    await host.event('群状态 刷新 group-a')
    refreshed = await host.state.snapshot('bot-a')
    group = refreshed['groups'][0]
    assert group['reliable'] and group['bot_state']['member_role'] == 'admin'
    assert group['bot_state']['in_group'] is True
    assert 'group-a' in host.sender.messages[-1][2]
    group['bot_state']['member_role'] = 'forged'
    assert (await host.state.snapshot('bot-a'))['groups'][0]['bot_state']['member_role'] == 'admin'
    assert await host.modules.reload('group_state')
    restored = (await host.state.snapshot('bot-a'))['groups'][0]
    assert restored['group'] == 'group-a' and not restored['reliable']
    assert restored['bot_state']['member_role'] == 'admin'
    assert restored['reason'] == 'process_restart'
    host.sender.messages.clear()
    await host.event('群状态')
    assert len(host.sender.messages) == 1
    from core.plugin.web_pages import get_page_html, match_route

    assert '已知群' in get_page_html('group-state')
    await host.modules.disable('group_state', _persist=False)
    assert get_page_html('group-state') is None
    assert match_route('GET', '/api/ext/group-state/') is None


@pytest.mark.parametrize('response,reason', [(None, 'response_unknown'), ((None, {'code': '11253'}), 'permission_denied'), (({}, None), 'response_unknown')])
async def test_failed_refresh_retains_history_and_never_claims_departure(group_host, response, reason):
    host = group_host
    assert (await host.api('POST', 'refresh', body={'group': 'group-a'})).status == 200
    host.sender.response = response
    result = (await (await host.api('POST', 'refresh', body={'group': 'group-a'})).json())['data']
    assert not result['reliable'] and result['reason'] == reason
    assert result['bot_state']['in_group'] is True  # explicitly historical; error is not absence
    assert result['status'] == 'historical'


async def test_observation_during_refresh_invalidates_older_result(group_host):
    host = group_host

    async def change():
        await host.event('普通聊天', user='member-a', group='group-a')

    host.sender.after_query = change
    result = (await (await host.api('POST', 'refresh', body={'group': 'group-a'})).json())['data']
    assert result['reason'] == 'state_changed' and not result['reliable']


async def test_refresh_completion_changes_snapshot_version(group_host):
    host = group_host
    pending = []

    async def capture():
        pending.append(await host.state.snapshot('bot-a'))

    host.sender.after_query = capture
    await host.api('POST', 'refresh', body={'group': 'group-a'})
    final = await host.state.snapshot('bot-a')
    assert pending[0]['groups'][0]['status'] == 'pending'
    assert final['version'] > pending[0]['version']
    assert final['groups'][0]['version'] > pending[0]['groups'][0]['version']
