"""Observed native C2C identities, owner-reviewed binding and scoped contexts."""

import json

from test_access_control import access_host as access_host
from test_access_control import configure, request


async def test_private_request_owner_binding_two_consumers_do_not_share_grants(access_host):
    host = access_host
    acl = await configure(host, kind='group')
    await configure(host, 'other')
    for consumer in ('reports', 'other'):
        await acl.manage_access(
            'bot-a',
            consumer,
            'policy',
            function='read',
            policy={'kind': 'group' if consumer == 'reports' else 'explicit', 'groups': ['group-a'], 'private': True},
        )
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    host.sender.messages.clear()
    await host.event('身份绑定 申请 group-a member-a', user='private-a')
    assert host.sender.messages
    pending = (await (await request(host, {'operation': 'binding_list'})).json())['data']['requests']
    assert pending[0]['status'] == 'pending' and pending[0]['private_id'] == 'private-a'
    assert not (await acl.private_contexts('bot-a', 'reports', 'read', 'private-a'))['candidates']
    result = await request(host, {'operation': 'binding_confirm', 'request': pending[0]['id'], 'confirmed': True, 'evidence': 'owner verified both identities'})
    assert result.status == 200
    chosen = await acl.private_contexts('bot-a', 'reports', 'read', 'private-a')
    assert chosen['selected']['group'] == 'group-a'
    assert not (await acl.private_contexts('bot-a', 'other', 'read', 'private-a'))['candidates']
    assert (await acl.access_snapshot('bot-a', 'reports'))['grants'] == []
    assert not (await acl.private_contexts('bot-b', 'reports', 'read', 'private-a'))['candidates']
    # Ordinary private users cannot use owner confirmation even with known request ID.
    await host.event(
        '授权管理 ' + json.dumps({'operation': 'binding_confirm', 'request': pending[0]['id'], 'confirmed': True, 'evidence': 'forged'}), user='private-a'
    )
    assert (await acl.binding_snapshot('bot-a'))['bindings'][0]['confirmed_by'] == 'web_admin'


async def bind(host, group, member, private='private-a'):
    await host.state.confirm_member('bot-a', group, member, evidence='group identity verified', member_role='admin')
    await host.event(f'身份绑定 申请 {group} {member}', user=private)
    pending = (await host.access.binding_snapshot('bot-a'))['requests'][-1]
    response = await request(
        host, {'operation': 'binding_confirm', 'request': pending['id'], 'confirmed': True, 'evidence': 'owner checked both native identities'}
    )
    assert response.status == 200
    return (await response.json())['data']


async def test_multiple_group_selection_role_and_revoke_recheck_context(access_host):
    host = access_host
    acl = await configure(host, kind='public')
    await acl.manage_access(
        'bot-a', 'reports', 'policy', function='read', policy={'kind': 'role', 'groups': ['group-a', 'group-b'], 'roles': ['admin'], 'private': True}
    )
    await bind(host, 'group-a', 'member-a')
    await bind(host, 'group-b', 'member-b')
    choices = await acl.private_contexts('bot-a', 'reports', 'read', 'private-a')
    assert choices['reason'] == 'choose_group' and not choices['selected']
    chosen = (await acl.private_contexts('bot-a', 'reports', 'read', 'private-a', group='group-b'))['selected']
    assert chosen['member'] == 'member-b' and acl.context_current('bot-a', chosen)
    await host.state.confirm_member('bot-a', 'group-b', 'member-b', evidence='role removed', member_role='member')
    assert not acl.context_current('bot-a', chosen)
    only = (await acl.private_contexts('bot-a', 'reports', 'read', 'private-a'))['selected']
    assert only['group'] == 'group-a'
    assert (await request(host, {'operation': 'binding_revoke', 'group': 'group-a', 'member': 'member-a'})).status == 200
    assert not acl.context_current('bot-a', only)
    assert not (await acl.private_contexts('bot-a', 'reports', 'read', 'private-a'))['candidates']


async def test_public_private_no_binding_or_group_source_and_no_equal_string_inference(access_host):
    import pytest

    host = access_host
    acl = await configure(host, kind='public')
    await acl.manage_access('bot-a', 'reports', 'policy', function='read', policy={'kind': 'public', 'private': True})
    await host.modules.disable('group_state', _persist=False)
    selected = (await acl.private_contexts('bot-a', 'reports', 'read', 'private-a'))['selected']
    assert selected['group'] == '' and acl.context_current('bot-a', selected)
    with pytest.raises(ValueError):
        await acl.bindings.request('bot-a', 'same-id', 'group-a', 'same-id')
    await host.event('身份绑定 申请 group-a same-id', user='same-id')
    assert (await acl.binding_snapshot('bot-a'))['bindings'] == []
    assert (await acl.binding_snapshot('bot-a'))['requests'][0]['status'] == 'pending'


async def test_binding_requires_evidence_pending_rejection_and_platform_reply_failure(access_host, monkeypatch):
    host = access_host
    await configure(host)
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    original_reply = host.sender.reply

    async def fail_reply(event, content=None, **kwargs):
        raise ConnectionError('fake platform unavailable')

    monkeypatch.setattr(host.sender, 'reply', fail_reply)
    await host.event('身份绑定 申请 group-a member-a', user='private-a')
    pending = (await host.access.binding_snapshot('bot-a'))['requests'][0]
    assert pending['status'] == 'pending' and pending['notification'] == 'send_failed'
    assert (await request(host, {'operation': 'binding_confirm', 'request': pending['id'], 'confirmed': True, 'evidence': ''})).status == 422
    assert (await request(host, {'operation': 'binding_reject', 'request': pending['id']})).status == 200
    assert (await request(host, {'operation': 'binding_confirm', 'request': pending['id'], 'confirmed': True, 'evidence': 'too late'})).status == 422
    monkeypatch.setattr(host.sender, 'reply', original_reply)
    await host.event('身份绑定 申请 group-a member-a', user='private-a')
    assert len((await host.access.binding_snapshot('bot-a'))['requests']) == 2


async def test_legacy_binding_import_preserves_pending_ids_without_importing_grants(access_host):
    host = access_host
    acl = await configure(host)
    bundle = {
        'private_identities': [{'private_id': 'legacy-private', 'identity_observed': True, 'source': 'observed_c2c', 'union_ids': ['union-evidence']}],
        'bindings': [
            {
                'id': 'stable-binding',
                'group': 'group-a',
                'member': 'legacy-member',
                'private_id': 'legacy-private',
                'source': 'owner_confirmed',
                'confirmed_by': 'owner-a',
                'confirmed_at': 100,
                'evidence': 'original verified identity',
            }
        ],
        'binding_requests': [
            {
                'id': 'stable-pending',
                'group': 'group-a',
                'member': 'other-member',
                'private_id': 'legacy-private',
                'status': 'pending',
                'created_at': 90,
                'source': 'observed_c2c_request',
            }
        ],
    }
    assert (await request(host, {'operation': 'binding_import', 'bundle': bundle})).status == 200
    assert await host.modules.reload('access_control')
    acl = host.modules.get('access_control')
    state = await acl.binding_snapshot('bot-a')
    assert state['bindings'][0]['id'] == 'stable-binding'
    assert state['requests'][0]['id'] == 'stable-pending' and state['requests'][0]['status'] == 'pending'
    assert not (await acl.private_contexts('bot-a', 'reports', 'read', 'legacy-private'))['candidates']
    assert (await acl.access_snapshot('bot-a', 'reports'))['grants'] == []
    assert (await acl.binding_snapshot('bot-b'))['bindings'] == []
    assert (await request(host, {'operation': 'binding_import', 'bundle': bundle})).status == 200


async def test_bound_context_expiry_deny_and_real_leave_never_share_old_grants(access_host):
    from socket_platform import until

    host = access_host
    acl = await configure(host)
    await acl.manage_access('bot-a', 'reports', 'policy', function='read', policy={'kind': 'explicit', 'groups': ['group-a'], 'private': True})
    await bind(host, 'group-a', 'member-a')
    acl.clock = lambda: 100
    await acl.manage_access('bot-a', 'reports', 'grant', function='read', group='group-a', member='member-a', resources=['report-r'], expires_at=200)
    old = (await acl.private_contexts('bot-a', 'reports', 'read', 'private-a'))['selected']
    assert old and acl.context_current('bot-a', old)
    acl.clock = lambda: 200
    assert not acl.context_current('bot-a', old)
    assert not (await acl.private_contexts('bot-a', 'reports', 'read', 'private-a'))['candidates']
    await acl.manage_access('bot-a', 'reports', 'grant', function='read', group='group-a', member='member-a', resources=['report-r'])
    await acl.manage_access('bot-a', 'reports', 'deny', function='read', group='group-a', member='member-a')
    assert not (await acl.private_contexts('bot-a', 'reports', 'read', 'private-a'))['candidates']
    await acl.manage_access('bot-a', 'reports', 'allow', function='read', group='group-a', member='member-a')
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a')
    await until(
        lambda: host.state.recovery.get_status('bot-a').ready and host.state.states['bot-a']['groups']['group-a'].get('last_native', {}).get('sequence') == 2
    )
    assert not (await acl.private_contexts('bot-a', 'reports', 'read', 'private-a'))['candidates']
    host.lifecycle('GROUP_MEMBER_ADD', member='member-a', sequence=3, timestamp=1001)
    await until(
        lambda: host.state.recovery.get_status('bot-a').ready and host.state.states['bot-a']['groups']['group-a'].get('last_native', {}).get('sequence') == 3
    )
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='current after rejoin')
    assert not (await acl.private_contexts('bot-a', 'reports', 'read', 'private-a'))['candidates']
    assert len((await acl.binding_snapshot('bot-a'))['bindings']) == 1


async def test_private_qualification_command_uses_actual_requesting_identity(access_host):
    host = access_host
    acl = await configure(host, kind='group')
    await acl.manage_access('bot-a', 'reports', 'policy', function='read', policy={'kind': 'group', 'groups': ['group-a'], 'private': True})
    await bind(host, 'group-a', 'member-a')
    host.sender.messages.clear()
    await host.event('身份绑定 群资格 reports read', user='private-a')
    own = json.loads(host.sender.messages[-1][2])
    assert own['selected'] == 'group-a'
    await host.event('身份绑定 群资格 reports read', user='different-private')
    other = json.loads(host.sender.messages[-1][2])
    assert not other['groups'] and other['selected'] is None


async def test_private_identity_deny_overrides_bound_group_permission_and_old_context(access_host):
    host = access_host
    acl = await configure(host, kind='group')
    await acl.manage_access('bot-a', 'reports', 'policy', function='read', policy={'kind': 'group', 'groups': ['group-a'], 'private': True})
    await bind(host, 'group-a', 'member-a')
    old = (await acl.private_contexts('bot-a', 'reports', 'read', 'private-a'))['selected']
    assert old and acl.context_current('bot-a', old)
    await acl.manage_access('bot-a', 'reports', 'deny', function='read', group='', member='private-a')
    assert not acl.context_current('bot-a', old)
    assert not (await acl.private_contexts('bot-a', 'reports', 'read', 'private-a'))['candidates']


async def test_revoked_legacy_association_cannot_be_restored_by_reimport(access_host):
    host = access_host
    acl = await configure(host, kind='group')
    await acl.manage_access('bot-a', 'reports', 'policy', function='read', policy={'kind': 'group', 'groups': ['group-a'], 'private': True})
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    bundle = {
        'private_identities': [{'private_id': 'private-a', 'identity_observed': True, 'source': 'observed_c2c'}],
        'bindings': [
            {
                'id': 'legacy-link',
                'group': 'group-a',
                'member': 'member-a',
                'private_id': 'private-a',
                'source': 'owner_confirmed',
                'confirmed_by': 'owner-a',
                'evidence': 'old verified identity',
            }
        ],
        'binding_requests': [],
    }
    assert (await request(host, {'operation': 'binding_import', 'bundle': bundle})).status == 200
    assert (await acl.private_contexts('bot-a', 'reports', 'read', 'private-a'))['selected']
    assert (await request(host, {'operation': 'binding_revoke', 'group': 'group-a', 'member': 'member-a'})).status == 200
    assert await host.modules.reload('access_control')
    acl = host.modules.get('access_control')
    assert (await request(host, {'operation': 'binding_import', 'bundle': bundle})).status == 422
    assert not (await acl.private_contexts('bot-a', 'reports', 'read', 'private-a'))['candidates']


async def test_import_rejects_ambiguous_group_private_identity_before_qualification(access_host):
    host = access_host
    acl = await configure(host, kind='group')
    await acl.manage_access(
        'bot-a', 'reports', 'policy', function='read', policy={'kind': 'members', 'members': ['member-a'], 'groups': ['group-a'], 'private': True}
    )
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    bundle = {
        'private_identities': [{'private_id': 'private-a', 'identity_observed': True, 'source': 'observed_c2c'}],
        'bindings': [
            {
                'id': 'link-a',
                'group': 'group-a',
                'member': 'member-a',
                'private_id': 'private-a',
                'source': 'owner_confirmed',
                'confirmed_by': 'owner-a',
                'evidence': 'first',
            },
            {
                'id': 'link-b',
                'group': 'group-a',
                'member': 'member-b',
                'private_id': 'private-a',
                'source': 'owner_confirmed',
                'confirmed_by': 'owner-a',
                'evidence': 'second',
            },
        ],
        'binding_requests': [],
    }
    assert (await request(host, {'operation': 'binding_import', 'bundle': bundle})).status == 422
    assert not (await acl.private_contexts('bot-a', 'reports', 'read', 'private-a'))['candidates']


async def test_explicit_new_owner_confirmation_after_revoke_keeps_old_import_blocked(access_host):
    host = access_host
    await configure(host, kind='group')
    acl = host.access
    first = await bind(host, 'group-a', 'member-a')
    await request(host, {'operation': 'binding_revoke', 'group': 'group-a', 'member': 'member-a'})
    second = await bind(host, 'group-a', 'member-a')
    assert first['id'] != second['id']
    snapshot = await acl.binding_snapshot('bot-a')
    assert snapshot['revocations'][0]['id'] == first['id']
    assert snapshot['bindings'][0]['id'] == second['id']
    await request(host, {'operation': 'binding_revoke', 'group': 'group-a', 'member': 'member-a'})
    snapshot = await acl.binding_snapshot('bot-a')
    assert {row['id'] for row in snapshot['revocations']} == {first['id'], second['id']}
    assert await host.modules.reload('access_control')
    snapshot = await host.modules.get('access_control').binding_snapshot('bot-a')
    assert {row['id'] for row in snapshot['revocations']} == {first['id'], second['id']}


async def test_platform_rejected_binding_receipt_preserves_error_code(access_host, monkeypatch):
    host = access_host

    async def reject(event, content=None, **kwargs):
        return False, {'code': 11253}

    monkeypatch.setattr(host.sender, 'reply', reject)
    await host.event('身份绑定 申请 group-a member-a', user='private-a')
    pending = (await host.access.binding_snapshot('bot-a'))['requests'][0]
    assert pending['status'] == 'pending'
    assert pending['notification'] == 'platform_rejected' and pending['notification_error'] == '11253'
