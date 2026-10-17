"""Delivery rules use a synthetic observer; production integration remains unavailable."""

import pytest
from test_access import access, grant
from test_binding import bind


def roster(host, members):
    host.sender.query_results[('/v2/groups/group-a/members', '')] = (True, {'members': members, 'next_cursor': ''})
    host.sender.query_results[('/v2/groups/group-a/bot_state', '')] = (True, {'member_openid': 'bot-native'})


async def configure(host, account, **kwargs):
    return await host.api('POST', 'group-delivery', {'group': 'group-a', 'resource': account['id'], **kwargs})


async def test_owner_cannot_enable_group_delivery_until_bot_also_authorized(host):
    account = await host.create()
    await grant(host, account)
    roster(host, [{'member_openid': 'member-a', 'username': '小明'}, {'member_openid': 'bot-native', 'bot': True}])
    response = await configure(host, account, enabled=True)
    assert response.status == 200, await response.text()
    mode = (await response.json())['data']
    assert mode['desired'] == 'group' and mode['actual'] == 'private'
    failed = next(m for m in mode['report']['members'] if m['member'] == 'bot-native')
    assert not failed['eligible']
    await access(host, 'grant', member='bot-native', resources=[account['id']], identity_type='group_member_openid', confirmed=True)
    response = await configure(host, account, enabled=True)
    assert (await response.json())['data']['actual'] == 'group'
    host.sender.messages.clear()
    await host.event(f'账户查询 {account["id"]}', group='group-a', user='member-a')
    assert any(m[0:2] == ('group', 'group-a') and 'fake-secret' in m[2] for m in host.sender.messages)
    assert not any(m[0] == 'private' and 'fake-secret' in m[2] for m in host.sender.messages)
    # A private invocation still sends privately even when group delivery is enabled.
    await bind(host)
    host.sender.messages.clear()
    await host.event(f'账户查询 {account["id"]}', user='private-a')
    assert any(m[0:2] == ('private', 'private-a') and 'fake-secret' in m[2] for m in host.sender.messages)


async def test_mode_configuration_cannot_bypass_missing_event_source(host):
    account = await host.create()
    await grant(host, account)
    host.service.access().lifecycle_available = False
    roster(host, [{'member_openid': 'member-a'}, {'member_openid': 'bot-native', 'bot': True}])
    response = await configure(host, account, enabled=True, auto_restore=True)
    mode = (await response.json())['data']
    assert mode['actual'] == 'private'
    assert mode['report']['reason'] == 'event_integration_unavailable'
    assert (await configure(host, account, enabled=True, force=True)).status == 422
    assert (await host.api('GET', 'group-delivery', authenticated=False)).status == 401


@pytest.mark.parametrize('failure', ['person', 'other_bot', 'incomplete', 'missing_own_bot'])
async def test_group_falls_back_to_private_without_publishing_password(host, failure):
    account = await host.create()
    await grant(host, account)
    await bind(host)
    await access(host, 'grant', member='bot-native', resources=[account['id']], identity_type='group_member_openid', confirmed=True)
    members = [{'member_openid': 'member-a'}, {'member_openid': 'bot-native', 'bot': True}]
    roster(host, members)
    assert (await (await configure(host, account, enabled=True)).json())['data']['actual'] == 'group'
    if failure in {'person', 'other_bot'}:
        members.append({'member_openid': 'new-ungranted', 'bot': failure == 'other_bot'})
    elif failure == 'missing_own_bot':
        members.pop()
    else:
        host.sender.query_results[('/v2/groups/group-a/members', '')] = (False, {'code': '11253'})
    host.sender.messages.clear()
    await host.event(f'账户查询 {account["id"]}', group='group-a', user='member-a')
    assert '尝试私发' in host.sender.messages[0][2]
    assert any(m[0:2] == ('private', 'private-a') and 'fake-secret' in m[2] for m in host.sender.messages)
    assert not any(m[0] == 'group' and 'fake-secret' in m[2] for m in host.sender.messages)


@pytest.mark.parametrize('auto_restore', [False, True])
async def test_recovery_choice_and_explicit_disable_never_push_credentials(host, auto_restore):
    account = await host.create()
    await host.event('账户管理 列表')
    await grant(host, account)
    roster(host, [{'member_openid': 'member-a'}, {'member_openid': 'bot-native', 'bot': True}])
    await configure(host, account, enabled=True, auto_restore=auto_restore)
    await access(host, 'grant', member='bot-native', resources=[account['id']], identity_type='group_member_openid', confirmed=True)
    host.sender.messages.clear()
    result = (await (await configure(host, account)).json())['data']
    assert result['actual'] == ('group' if auto_restore else 'private')
    assert len(host.sender.messages) == 1
    assert 'fake-secret' not in str(host.sender.messages)
    await configure(host, account)
    assert len(host.sender.messages) == 1
    await configure(host, account, enabled=False)
    result = (await (await configure(host, account)).json())['data']
    assert result['desired'] == result['actual'] == 'private'


async def test_revoke_during_check_invalidates_group_send_and_accounts_stay_independent(host):
    account = await host.create()
    other = await host.create('另一个账户')
    await grant(host, account)
    await bind(host)
    await access(host, 'grant', member='bot-native', resources=[account['id']], identity_type='group_member_openid', confirmed=True)
    roster(host, [{'member_openid': 'member-a'}, {'member_openid': 'bot-native', 'bot': True}])
    await configure(host, account, enabled=True)
    assert (await (await configure(host, other, enabled=True)).json())['data']['actual'] == 'private'

    async def revoke(path, cursor):
        if path.endswith('/bot_state'):
            await access(host, 'revoke', member='bot-native')

    host.sender.after_get = revoke
    host.sender.messages.clear()
    await host.event(f'账户查询 {account["id"]}', group='group-a', user='member-a')
    assert not any(m[0] == 'group' and 'fake-secret' in m[2] for m in host.sender.messages)
    assert any(m[0] == 'private' and 'fake-secret' in m[2] for m in host.sender.messages)


@pytest.mark.parametrize('entry', ['keyword', 'button'])
async def test_keyword_and_button_share_current_group_routing(host, entry):
    from test_buttons import callback
    from test_qa import rule

    account = await host.create()
    await grant(host, account)
    await access(host, 'grant', member='bot-native', resources=[account['id']], identity_type='group_member_openid', confirmed=True)
    roster(host, [{'member_openid': 'member-a'}, {'member_openid': 'bot-native', 'bot': True}])
    await configure(host, account, enabled=True)
    await rule(host, account['id'], ['共享口令'])
    await host.event('账户列表', group='group-a', user='member-a')
    data = callback(host.sender.messages[-1], account['name'])
    host.sender.messages.clear()
    if entry == 'keyword':
        await host.event('共享口令', group='group-a', user='member-a')
    else:
        await host.click(data, group='group-a', user='member-a')
    assert any(m[0:2] == ('group', 'group-a') and 'fake-secret' in m[2] for m in host.sender.messages)


async def test_failed_private_fallback_and_foreign_bot_never_leak_credentials(host):
    account = await host.create()
    await grant(host, account)
    await bind(host)
    roster(host, [{'member_openid': 'member-a'}, {'member_openid': 'bot-native', 'bot': True}])
    await configure(host, account, enabled=True)
    host.sender.messages.clear()
    host.sender.fail_private = True
    await host.event(f'账户查询 {account["id"]}', group='group-a', user='member-a')
    await host.event(f'账户查询 {account["id"]}', bot='bot-b', group='group-a', user='member-a')
    assert 'fake-secret' not in str(host.sender.messages)
    assert any('私发失败' in m[2] for m in host.sender.messages)


async def test_framework_blacklist_change_during_full_check_blocks_group_send(host):
    from core.base.config import cfg

    account = await host.create()
    await grant(host, account)
    await bind(host)
    await access(host, 'grant', member='bot-native', resources=[account['id']], identity_type='group_member_openid', confirmed=True)
    roster(host, [{'member_openid': 'member-a'}, {'member_openid': 'bot-native', 'bot': True}])
    await configure(host, account, enabled=True)

    async def block(path, cursor):
        if path.endswith('/bot_state'):
            cfg.set_bot_setting('bot-a', 'blacklist.user_list', {'bot-native': 'blocked'})

    host.sender.after_get = block
    host.sender.messages.clear()
    await host.event(f'账户查询 {account["id"]}', group='group-a', user='member-a')
    assert not any(m[0] == 'group' and 'fake-secret' in m[2] for m in host.sender.messages)


async def test_known_invalidation_requires_manual_reenable_even_if_repaired_before_recheck(host):
    account = await host.create()
    await grant(host, account)
    await access(host, 'grant', member='bot-native', resources=[account['id']], identity_type='group_member_openid', confirmed=True)
    roster(host, [{'member_openid': 'member-a'}, {'member_openid': 'bot-native', 'bot': True}])
    assert (await (await configure(host, account, enabled=True)).json())['data']['actual'] == 'group'
    await host.lifecycle('GROUP_MEMBER_ADD', member='new-person', timestamp=2000000000, event_id='new-person-join')
    assert (await (await host.api('GET', 'group-delivery')).json())['data'][0]['actual'] == 'private'
    await access(host, 'grant', member='new-person', resources=[account['id']], identity_type='group_member_openid', confirmed=True)
    roster(host, [{'member_openid': 'member-a'}, {'member_openid': 'bot-native', 'bot': True}, {'member_openid': 'new-person'}])
    result = (await (await configure(host, account)).json())['data']
    assert result['report']['eligible']
    assert result['actual'] == 'private'


async def test_observed_new_member_missing_from_platform_round_keeps_group_private(host):
    account = await host.create()
    await grant(host, account)
    await bind(host)
    await access(host, 'grant', member='bot-native', resources=[account['id']], identity_type='group_member_openid', confirmed=True)
    roster(host, [{'member_openid': 'member-a'}, {'member_openid': 'bot-native', 'bot': True}])
    await configure(host, account, enabled=True, auto_restore=True)
    await host.lifecycle('GROUP_MEMBER_ADD', member='known-new-person', timestamp=2000000000, event_id='known-new-person')
    host.sender.messages.clear()
    await host.event(f'账户查询 {account["id"]}', group='group-a', user='member-a')
    assert not any(m[0] == 'group' and 'fake-secret' in m[2] for m in host.sender.messages)


async def test_function_group_change_is_global_even_when_request_has_another_group(host):
    account = await host.create()
    await access(host, 'whitelist', enabled=True)
    await access(host, 'function_group', group='another-group', id='features', functions=['account_sharing'])
    for member in ('member-a', 'bot-native'):
        await access(
            host,
            'grant',
            member=member,
            mode='continuous',
            function_scope={'kind': 'group', 'id': 'features'},
            resources=[account['id']],
            identity_type='group_member_openid',
            confirmed=True,
        )
    roster(host, [{'member_openid': 'member-a'}, {'member_openid': 'bot-native', 'bot': True}])
    assert (await (await configure(host, account, enabled=True)).json())['data']['actual'] == 'group'
    await access(host, 'function_group', group='another-group', id='features', functions=[])
    await host.api('GET', 'group-delivery')
    await access(host, 'function_group', group='another-group', id='features', functions=['account_sharing'])
    result = (await (await configure(host, account)).json())['data']
    assert result['report']['eligible']
    assert result['actual'] == 'private'
