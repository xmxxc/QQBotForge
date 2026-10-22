"""Whole-group qualification reports from real roster facts and consumer send entries."""

import json

import pytest
from socket_platform import until
from test_access_control import access_host as access_host
from test_access_control import configure, request
from test_group_roster import member, page, roster_platform

PROBE = '''
"""Fake consumer: group-send only when every recipient passes, else status plus private."""

_report = {}


async def setup(ctx):
    from core.application import get_app

    async def on_event(event):
        if event.event_type != 'GROUP_AT_MESSAGE_CREATE' or (event.content or '').strip() != '受限答案':
            return event
        app = get_app()
        acl = app.module_manager.get('access_control')
        sender = app.get_bot(event.appid).sender
        report = await acl.audience_report(event.appid, 'reports', 'read', event.group_id, 'report-r', actions=['invoke', 'view'])
        _report['last'] = report
        context = dict(consumer='reports', function='read', group=event.group_id, resource='report-r', actions=['invoke', 'view'])
        if not acl.member_current(event.appid, report, event.get('d/author/member_openid'), **context):
            await sender.reply(event, 'denied')
            return None
        await _report.get('pause', _noop)()
        if report['eligible'] and acl.report_current(event.appid, report, **context):
            await sender.reply(event, 'group:answer')
        else:
            await sender.reply(event, 'status:' + report['reason'])
        return None

    ctx.register_hook('before_event', on_event, priority=50)
    return _report


async def _noop():
    return None
'''


VIEW = {'consumer': 'reports', 'function': 'read', 'group': 'group-a', 'resource': 'report-r', 'actions': ['view']}
BOTH = {**VIEW, 'actions': ['invoke', 'view']}


async def grant_all(acl, *members, bot='bot-a', consumer='reports', expires_at=None):
    for native in members:
        await acl.manage_access(bot, consumer, 'grant', function='read', group='group-a', member=native, resources=['report-r'], expires_at=expires_at)


async def audience(host):
    roster_platform(host)
    acl = await configure(host)
    assert (await host.state.sync_roster('bot-a', 'group-a'))['complete']
    return acl


@pytest.fixture
async def probe_host(access_host, tmp_path):
    host = access_host
    probe = tmp_path / 'modules/audience_probe'
    probe.mkdir()
    (probe / 'main.py').write_text(PROBE)
    host.modules.discover_module('audience_probe')
    assert await host.modules.enable('audience_probe', _skip_deps=True, _persist=False)
    host.probe = host.modules.get('audience_probe')
    return host


async def test_complete_roster_includes_both_bots_and_every_member_must_pass(access_host):
    host = access_host
    acl = await audience(host)
    await grant_all(acl, 'member-a', 'this-bot')
    report = await acl.audience_report('bot-a', 'reports', 'read', 'group-a', 'report-r', actions=['invoke', 'view'])
    assert report['complete'] and report['failures_complete'] and report['reason'] == 'member_ineligible'
    rows = {row['member']: row for row in report['members']}
    assert set(rows) == {'member-a', 'this-bot', 'other-bot'}
    assert rows['this-bot']['self'] and rows['this-bot']['bot'] and rows['other-bot']['bot'] and not rows['other-bot']['self']
    assert [f['member'] for f in report['failures']] == ['other-bot']
    assert report['failures'][0]['reasons'] == {'invoke': 'no_explicit_permission', 'view': 'no_explicit_permission'}
    assert not acl.report_current('bot-a', report, **BOTH)
    assert acl.member_current('bot-a', report, 'member-a', **BOTH) and not acl.member_current('bot-a', report, 'other-bot', **BOTH)
    await grant_all(acl, 'other-bot')
    report = await acl.audience_report('bot-a', 'reports', 'read', 'group-a', 'report-r', actions=['invoke', 'view'])
    assert report['eligible'] and report['reason'] == 'eligible' and acl.report_current('bot-a', report, **BOTH)
    # Other consumers' policy changes do not invalidate this report.
    await configure(host, 'other')
    await acl.manage_access('bot-a', 'other', 'deny', function='read', group='group-a', member='member-a')
    assert acl.report_current('bot-a', report, **BOTH)
    # Own denial invalidates immediately.
    await acl.manage_access('bot-a', 'reports', 'deny', function='read', group='group-a', member='other-bot')
    assert not acl.report_current('bot-a', report, **BOTH) and acl.member_current('bot-a', report, 'member-a', **BOTH) is False


async def test_incomplete_roster_lists_only_confirmed_and_never_claims_all_failures(access_host):
    host = access_host
    roster_platform(host)
    host.sender.json_results[('/v2/groups/group-a/members', 'second')] = False, {'code': '11253'}
    acl = await configure(host, kind='group')
    assert not (await host.state.sync_roster('bot-a', 'group-a'))['complete']
    await host.state.confirm_member('bot-a', 'group-a', 'member-b', evidence='owner checked', member_role='member')
    report = await acl.audience_report('bot-a', 'reports', 'read', 'group-a', 'report-r')
    assert not report['complete'] and not report['failures_complete'] and report['reason'] == 'roster_incomplete'
    assert report['roster_reason'] and not report['eligible'] and not acl.report_current('bot-a', report, **VIEW)
    rows = {row['member']: row for row in report['members']}
    assert rows['member-b']['eligible'] and 'this-bot' not in rows
    assert acl.member_current('bot-a', report, 'member-b', **VIEW)
    unknown = await acl.audience_report('bot-a', 'reports', 'read', 'unknown-group', 'report-r')
    assert unknown['reason'] == 'roster_incomplete' and unknown['members'] == []
    await host.modules.disable('group_state', _persist=False)
    unavailable = await acl.audience_report('bot-a', 'reports', 'read', 'group-a', 'report-r')
    assert unavailable['reason'] == 'group_facts_unavailable' and not acl.report_current('bot-a', report, **VIEW)


async def test_restricted_view_report_with_public_policy_still_needs_complete_roster(access_host):
    host = access_host
    acl = await configure(host, kind='public')
    no_roster = await acl.audience_report('bot-a', 'reports', 'read', 'group-a', 'report-r')
    assert no_roster['reason'] == 'roster_incomplete'
    roster_platform(host)
    assert (await host.state.sync_roster('bot-a', 'group-a'))['complete']
    report = await acl.audience_report('bot-a', 'reports', 'read', 'group-a', 'report-r')
    assert report['eligible'] and report['actions'] == ['view'] and acl.report_current('bot-a', report, **VIEW)
    # A newly observed member makes the old roster proof stale.
    await host.event('普通聊天', user='new-member', group='group-a')
    assert not acl.report_current('bot-a', report, **VIEW)
    assert (await acl.audience_report('bot-a', 'reports', 'read', 'group-a', 'report-r'))['reason'] == 'roster_incomplete'
    with pytest.raises(ValueError):
        await acl.audience_report('bot-a', 'reports', 'read', 'group-a', actions=['delete'])
    with pytest.raises(ValueError):
        await acl.audience_report('bot-a', 'reports', 'missing', 'group-a')


async def test_leave_role_expiry_and_other_scope_changes(access_host, monkeypatch):
    host = access_host
    acl = await audience(host)
    now = [1000.0]
    monkeypatch.setattr(acl, 'clock', lambda: now[0])
    await grant_all(acl, 'member-a', 'this-bot', 'other-bot', expires_at=2000)
    report = await acl.audience_report('bot-a', 'reports', 'read', 'group-a', 'report-r', actions=['invoke', 'view'])
    assert report['eligible'] and report['valid_until'] == 2000
    now[0] = 2000.0
    assert not acl.report_current('bot-a', report, **BOTH)
    expired = await acl.audience_report('bot-a', 'reports', 'read', 'group-a', 'report-r', actions=['invoke', 'view'])
    assert {f['reasons']['invoke'] for f in expired['failures']} == {'expired'} and len(expired['failures']) == 3
    now[0] = 1000.0
    await acl.manage_access('bot-a', 'reports', 'policy', function='read', policy={'kind': 'role', 'groups': ['group-a'], 'roles': ['member', 'admin']})
    report = await acl.audience_report('bot-a', 'reports', 'read', 'group-a', 'report-r', actions=['invoke', 'view'])
    assert report['eligible']
    # Activity in bot-b and another group of bot-a leave this group's proof current.
    host.lifecycle('GROUP_MEMBER_ADD', member='x', group='group-z', bot='bot-b', sequence=2)
    await until(lambda: host.state.recovery.get_status('bot-b').ready and 'group-z' in host.state.states['bot-b']['groups'])
    assert acl.report_current('bot-a', report, **BOTH)
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='role changed', member_role='owner')
    assert not acl.report_current('bot-a', report, **BOTH)
    report = await acl.audience_report('bot-a', 'reports', 'read', 'group-a', 'report-r', actions=['invoke', 'view'])
    assert report['reason'] == 'roster_incomplete'
    assert (await host.state.sync_roster('bot-a', 'group-a'))['complete']
    report = await acl.audience_report('bot-a', 'reports', 'read', 'group-a', 'report-r', actions=['invoke', 'view'])
    assert report['eligible']
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a')
    await until(lambda: not acl.report_current('bot-a', report, **BOTH))


async def test_consumer_send_entry_rechecks_before_group_send(probe_host):
    host = probe_host
    acl = await audience(host)
    await grant_all(acl, 'member-a', 'this-bot')
    host.sender.messages.clear()
    await host.event('受限答案', user='member-a', group='group-a')
    assert host.sender.messages[-1] == ('bot-a', 'group-a', 'status:member_ineligible')
    await host.event('受限答案', user='other-bot', group='group-a')
    assert host.sender.messages[-1][2] == 'denied'
    await grant_all(acl, 'other-bot')
    await host.event('受限答案', user='member-a', group='group-a')
    assert host.sender.messages[-1][2] == 'group:answer'

    # A member leaves after the report was built but before the send.
    async def leave():
        host.lifecycle('GROUP_MEMBER_REMOVE', member='other-bot', sequence=5, timestamp=1005)
        await until(lambda: host.state.states['bot-a']['groups']['group-a']['members']['other-bot'].get('status') == 'left')

    host.probe['pause'] = leave
    await host.event('受限答案', user='member-a', group='group-a')
    assert host.sender.messages[-1][2] == 'status:eligible'
    assert not acl.report_current('bot-a', host.probe['last'], **BOTH)

    # A new member joins: the stale roster no longer proves the full audience.
    await until(lambda: host.state.recovery.get_status('bot-a').ready)
    host.probe.pop('pause')
    roster_platform(host)
    host.sender.json_results[('/v2/groups/group-a/members', 'second')] = page(member('this-bot', 'admin', bot=True))
    assert (await host.state.sync_roster('bot-a', 'group-a'))['complete']
    await host.event('受限答案', user='member-a', group='group-a')
    assert host.sender.messages[-1][2] == 'group:answer'
    host.lifecycle('GROUP_MEMBER_ADD', member='newcomer', sequence=6, timestamp=1006)
    await until(lambda: 'newcomer' in host.state.states['bot-a']['groups']['group-a']['members'])
    await until(lambda: host.state.recovery.get_status('bot-a').ready)
    await host.event('受限答案', user='member-a', group='group-a')
    # The join retires the old roster proof but not the asker's own verified fact:
    # no group answer, only the status (the consumer then answers privately).
    assert host.sender.messages[-1][2] == 'status:roster_incomplete' and host.probe['last']['reason'] == 'roster_incomplete'
    assert not any(row['member'] == 'newcomer' for row in host.probe['last']['members'])


async def test_owner_only_management_report_over_http_and_qq(access_host):
    host = access_host
    acl = await audience(host)
    await grant_all(acl, 'member-a')
    body = {'consumer': 'reports', 'operation': 'audience', 'function': 'read', 'group': 'group-a', 'resource': 'report-r', 'actions': ['invoke', 'view']}
    assert (await request(host, body, authenticated=False)).status == 401
    response = await request(host, body)
    assert response.status == 200
    data = (await response.json())['data']
    assert '_proof' not in data and data['reason'] == 'member_ineligible'
    assert {row['member'] for row in data['members']} == {'member-a', 'this-bot', 'other-bot'}
    assert (await request(host, {**body, 'member': 'member-a'})).status == 422
    host.sender.messages.clear()
    await host.event('授权管理 ' + json.dumps(body), user='member-a')
    assert host.sender.messages == []
    await host.event('授权管理 ' + json.dumps(body), user='owner-a')
    detail = json.loads(host.sender.messages[-1][2])
    assert detail['complete'] and [f['member'] for f in detail['failures']] == ['this-bot', 'other-bot']
    # In a group the owner gets only a short status; the details stay private.
    await host.event('授权管理 ' + json.dumps(body), user='owner-a', group='group-a')
    assert host.sender.messages[-1] == ('bot-a', 'group-a', '已处理，请在主人私聊或后台查看详情')


async def eligible_view_report(host):
    acl = await audience(host)
    await configure(host, 'accounts')
    await acl.declare_resources('bot-a', 'reports', [{'id': 'report-r'}, {'id': 'secret'}])
    await acl.declare_resources('bot-a', 'accounts', [{'id': 'report-r'}, {'id': 'secret'}])
    await acl.declare('bot-a', 'reports', owner='plugin:reports', functions={'write': {'kind': 'group', 'groups': ['group-a']}})
    for consumer in ('reports', 'accounts'):
        await grant_all(acl, 'member-a', 'this-bot', 'other-bot', consumer=consumer)
    report = await acl.audience_report('bot-a', 'reports', 'read', 'group-a', 'report-r', actions=['view'])
    assert report['eligible'] and acl.report_current('bot-a', report, **VIEW)
    return acl, report


@pytest.mark.parametrize(
    'field, value',
    [('consumer', 'accounts'), ('function', 'write'), ('group', 'group-b'), ('resource', 'secret'), ('actions', ['invoke', 'view']), ('actions', ['invoke'])],
)
async def test_report_credential_is_bound_to_the_actual_send_context(access_host, field, value):
    acl, report = await eligible_view_report(access_host)
    expected = {**VIEW, field: value}
    assert not acl.report_current('bot-a', report, **expected)
    assert not acl.member_current('bot-a', report, 'member-a', **expected)
    assert acl.member_current('bot-a', report, 'member-a', **VIEW)
    # Rewriting the display fields to the expected context grants nothing either.
    forged = {**report, field: value}
    assert not acl.report_current('bot-a', forged, **expected)
    assert not acl.member_current('bot-a', forged, 'member-a', **expected)


async def test_report_credential_is_bound_to_bot_and_member(access_host):
    host = access_host
    acl, report = await eligible_view_report(host)
    assert not acl.report_current('bot-b', report, **VIEW)
    assert not acl.member_current('bot-b', report, 'member-a', **VIEW)
    assert not acl.member_current('bot-a', report, 'member-z', **VIEW)
    # Another report's credential keeps its own context.
    other = await acl.audience_report('bot-a', 'accounts', 'read', 'group-a', 'report-r', actions=['invoke', 'view'])
    assert other['eligible']
    accounts = {**VIEW, 'consumer': 'accounts', 'actions': ['invoke', 'view']}
    assert acl.report_current('bot-a', other, **accounts)
    assert not acl.report_current('bot-a', {**report, 'credential': other['credential']}, **VIEW)
    assert not acl.report_current('bot-a', {**other, 'credential': report['credential']}, **accounts)
    # The old context-free entry points are gone.
    with pytest.raises(TypeError):
        acl.report_current('bot-a', report)
    with pytest.raises(TypeError):
        acl.member_current('bot-a', report, 'member-a')


async def test_rewritten_deadline_or_proof_fields_cannot_extend_a_report(access_host, monkeypatch):
    host = access_host
    acl = await audience(host)
    now = [1000.0]
    monkeypatch.setattr(acl, 'clock', lambda: now[0])
    await grant_all(acl, 'member-a', 'this-bot', 'other-bot', expires_at=1050)
    report = await acl.audience_report('bot-a', 'reports', 'read', 'group-a', 'report-r', actions=['view'])
    assert report['valid_until'] == 1050 and acl.report_current('bot-a', report, **VIEW)
    assert '_proof' not in report and report['credential'] is not None
    forged = {**report, 'valid_until': None, 'eligible': True, 'complete': True, 'members': [], 'failures': [], '_proof': {'decisions': []}}
    now[0] = 1049.0
    assert acl.report_current('bot-a', forged, **VIEW)  # only the credential counts, not the rewritten fields
    now[0] = 1050.0
    assert not acl.report_current('bot-a', report, **VIEW) and not acl.report_current('bot-a', forged, **VIEW)
    assert not acl.member_current('bot-a', forged, 'member-a', **VIEW)


@pytest.mark.parametrize('change', ['revoke', 'policy', 'fact', 'reload'])
async def test_report_credential_follows_authorization_and_fact_changes(access_host, change):
    host = access_host
    acl, report = await eligible_view_report(host)
    if change == 'revoke':
        await acl.manage_access('bot-a', 'reports', 'revoke', function='read', group='group-a', member='member-a')
    elif change == 'policy':
        await acl.manage_access('bot-a', 'reports', 'policy', function='read', policy={'kind': 'owner', 'groups': ['group-a']})
    elif change == 'fact':
        await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='role changed', member_role='admin')
    else:
        assert await host.modules.reload('access_control')
        acl = host.modules.get('access_control')
    assert not acl.report_current('bot-a', report, **VIEW)
    if change in {'revoke', 'policy', 'reload'}:
        assert not acl.member_current('bot-a', report, 'member-a', **VIEW)


async def test_owner_view_hides_the_credential(access_host):
    host = access_host
    acl, report = await eligible_view_report(host)
    view = acl.audience.owner_view(report)
    assert 'credential' not in view and '_proof' not in view and view['eligible']
    response = await request(host, {'operation': 'audience', **VIEW})
    data = (await response.json())['data']
    assert 'credential' not in data and '_proof' not in data


async def test_copied_reports_keep_their_credential_and_forgeries_fail(access_host):
    import copy
    import sys

    host = access_host
    acl, report = await eligible_view_report(host)
    assert acl.report_current('bot-a', copy.deepcopy(report), **VIEW) and acl.report_current('bot-a', dict(report), **VIEW)
    credential = type(report['credential'])
    assert credential is sys.modules[type(acl.audience).__module__].Credential
    # A look-alike credential built outside the issuing instance is not accepted.
    forged = credential(report['credential']._record)
    assert not acl.report_current('bot-a', {**report, 'credential': forged}, **VIEW)
    assert not acl.report_current('bot-a', {**report, 'credential': 'credential-string'}, **VIEW)
    assert repr(report['credential']) == '<audience credential>'
