"""Owner verification works independently of the deferred production event source."""

import pytest
from test_access import access, grant
from test_binding import bind


async def test_owner_verifies_persisted_member_without_enabling_production_queries(host):
    account = await host.create()
    await grant(host, account)
    await bind(host)
    await host.restart_access()
    host.service.access_provider().lifecycle_available = False
    response = await access(host, 'explain', resource=account['id'])
    assert (await response.json())['data']['membership'] == 'pending'
    response = await access(host, 'verify', identity_type='group_member_openid', confirmed=True, state='present', evidence='主人核对同一入群周期，期间未退出')
    assert response.status == 200, await response.text()
    decision = (await (await access(host, 'explain', resource=account['id'])).json())['data']
    assert decision['membership'] == 'present'
    assert decision['reason'] == 'event_integration_unavailable'
    state = (await (await host.api('GET', 'access')).json())['data']
    assert state['verifications'][-1]['actor'] == 'panel_admin'
    assert not state['rosters']
    host.sender.messages.clear()
    await host.event(f'账户查询 {account["id"]}', user='private-a')
    assert not any('fake-secret' in str(m) for m in host.sender.messages)


async def test_automatic_verification_and_failed_query_do_not_infer_absence(host):
    account = await host.create()
    await grant(host, account)
    await host.restart_access()
    response = await access(host, 'verify_auto')
    assert response.status == 200
    assert (await response.json())['data']['state'] == 'unknown'
    state = (await (await host.api('GET', 'access')).json())['data']
    assert len(state['grants']) == 1
    host.sender.query_results[('/v2/groups/group-a/members/member-a', '')] = (True, {'member_openid': 'member-a', 'joined_at': '2020-01-01T00:00:00Z'})
    response = await access(host, 'verify_auto')
    assert (await response.json())['data']['state'] == 'present'
    assert (await (await access(host, 'explain', resource=account['id'])).json())['data']['allowed']
    await host.restart_access()
    host.sender.query_results[('/v2/groups/group-a/members/member-a', '')] = (True, {'member_openid': 'member-a', 'joined_at': '2099-01-01T00:00:00Z'})
    response = await access(host, 'verify_auto')
    assert (await response.json())['data']['state'] == 'new_cycle'
    assert not (await (await host.api('GET', 'access')).json())['data']['grants']


async def test_bulk_verification_partial_failure_retains_denies_and_no_roster(host):
    account = await host.create()
    await grant(host, account)
    await access(host, 'deny', scope='member')
    await host.restart_access()
    response = await access(
        host,
        'bulk_verify',
        state='present',
        confirmed=True,
        evidence='核对同一周期',
        members=[{'member': 'member-a', 'confirmed': True}, {'member': 'unknown'}],
    )
    assert response.status == 200
    result = (await response.json())['data']
    assert result['summary'] == {'success': 1, 'failed': 1}
    assert (await (await access(host, 'explain', resource=account['id'])).json())['data']['reason'] == 'denied'
    await access(host, 'verify', identity_type='group_member_openid', confirmed=True, state='absent', evidence='已核实离群')
    response = await access(host, 'verify', identity_type='group_member_openid', confirmed=True, state='present', evidence='回来')
    assert response.status == 422
    assert not (await (await host.api('GET', 'access')).json())['data']['rosters']


@pytest.mark.parametrize('conflict', ['none', 'duplicate', 'interrupted', 'changed', 'missing_cursor'])
async def test_roster_uses_all_pages_and_rejects_conflicting_or_incomplete_rounds(host, conflict):
    account = await host.create()
    await grant(host, account)
    first = {'member_openid': 'member-a', 'bot': False}
    other = {'member_openid': 'bot-native', 'bot': True}
    host.sender.query_results[('/v2/groups/group-a/members', '')] = (True, {'members': [first], 'next_cursor': 'page2'})
    host.sender.query_results[('/v2/groups/group-a/members', 'page2')] = (
        (False, {'code': 'network'})
        if conflict == 'interrupted'
        else (True, {'members': [first if conflict == 'duplicate' else other], **({} if conflict == 'missing_cursor' else {'next_cursor': ''})})
    )
    if conflict == 'changed':

        async def change(path, cursor):
            if cursor:
                await host.lifecycle('GROUP_MEMBER_ADD', timestamp=2000000000, event_id='new-round')

        host.sender.after_get = change
    response = await access(host, 'roster_sync')
    assert response.status == 200, await response.text()
    result = (await response.json())['data']
    assert result['complete'] is (conflict == 'none')
    if result['complete']:
        assert {m['member'] for m in result['members']} == {'member-a', 'bot-native'}
    state = (await (await host.api('GET', 'access')).json())['data']
    assert len(state['grants']) == 1
    assert len(state['members']) <= 2  # A roster is not a grant or a complete historical merge.


async def test_bot_removed_freezes_group_and_rejoin_cannot_restore_old_sources(host):
    account = await host.create()
    await grant(host, account)
    await bind(host)
    await access(host, 'whitelist', group='group-b', enabled=True)
    await access(host, 'grant', group='group-b', member='member-b', resources=[account['id']], identity_type='group_member_openid', confirmed=True)
    await host.lifecycle('GROUP_DEL_ROBOT', member='operator-not-subject', timestamp=2000000000, event_id='removed-bot')
    decision = (await (await access(host, 'explain', resource=account['id'])).json())['data']
    assert not decision['allowed'] and decision['reason'] == 'bot_not_in_group'
    assert (await (await access(host, 'explain', group='group-b', member='member-b', resource=account['id'])).json())['data']['allowed']
    state = (await (await host.api('GET', 'access')).json())['data']
    assert not any(m['member'] == 'operator-not-subject' for m in state['members'].values())
    await host.lifecycle('GROUP_ADD_ROBOT', timestamp=2000000001, event_id='rejoin-bot')
    await access(host, 'verify', identity_type='group_member_openid', confirmed=True, state='present', evidence='仍为原成员')
    assert not (await (await access(host, 'explain', resource=account['id'])).json())['data']['allowed']
    await grant(host, account)
    await host.lifecycle('GROUP_ADD_ROBOT', timestamp=2000000001, event_id='rejoin-bot')
    assert (await (await access(host, 'explain', resource=account['id'])).json())['data']['allowed']


async def test_extension_suspension_preserves_grants_but_not_eligibility(host):
    account = await host.create()
    await grant(host, account)
    await host.service.access().suspend('bot-a', reason='continuity_unknown')
    decision = (await (await access(host, 'explain', resource=account['id'])).json())['data']
    assert not decision['allowed'] and decision['membership'] == 'pending'
    assert len(decision['sources']) == 1
