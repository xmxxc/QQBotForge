"""Native member lifecycle invalidates every consumer before durable history work."""

from test_scoped_grants import access, scope_grant


async def test_leave_revokes_all_sources_only_in_that_group_and_rejoin_requires_grant(host):
    from core.base.config import cfg

    acl = host.service.access_provider()
    acl.clock = lambda: 100
    account = await host.create()
    await access(host, 'whitelist', enabled=True)
    await access(host, 'whitelist', group='group-b', enabled=True)
    await acl.execute('bot-a', 'whitelist', function='reports', group='group-a', enabled=True)
    await access(host, 'function_group', id='多个功能', functions=['account_sharing', 'reports'])
    await scope_grant(host, resources=[account['id']])
    await scope_grant(host, function_scope={'kind': 'group', 'id': '多个功能'}, resource_scope={'kind': 'all'}, mode='continuous')
    await scope_grant(host, function_scope={'kind': 'all'}, resource_scope={'kind': 'all'}, mode='continuous')
    await scope_grant(host, group='group-b', resources=[account['id']])
    cfg.set_bot_setting('bot-a', 'identity.use_union_id_for_group', True)
    await host.event('普通聊天', user='member-a', group='group-a', union='union-a')
    cfg.set_bot_setting('bot-a', 'blacklist.user_list', {'union-a': 'framework blocked'})
    cfg.set_bot_setting('bot-a', 'maintenance.enabled', True)
    before = (await (await host.api('GET', 'access')).json())['data']
    old_cycle = next(m['cycle'] for m in before['members'].values() if m['group'] == 'group-a' and m['member'] == 'member-a')
    host.sender.messages.clear()
    await host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a', timestamp=101, plugin=False)
    assert not host.sender.messages
    result = await acl.decide('bot-a', 'reports', 'group-a', 'member-a', account['id'], resource_namespace='account_sharing')
    assert not result['allowed'] and result['reason'] == 'left_group'
    assert (await acl.decide('bot-a', 'account_sharing', 'group-b', 'member-a', account['id']))['allowed']
    await host.lifecycle('GROUP_MEMBER_ADD', member='member-a', timestamp=102, plugin=False)
    state = (await (await host.api('GET', 'access')).json())['data']
    member = next(m for m in state['members'].values() if m['group'] == 'group-a' and m['member'] == 'member-a')
    assert member['cycle'] != old_cycle and member['status'] == 'unverified'
    assert len(state['revocations']) == 3
    assert not any(g['group'] == 'group-a' and g['member'] == 'member-a' for g in state['grants'])
    await scope_grant(host, resources=[account['id']])
    assert (await acl.decide('bot-a', 'account_sharing', 'group-a', 'member-a', account['id']))['allowed']
    assert not (await acl.decide('bot-a', 'reports', 'group-a', 'member-a', account['id'], resource_namespace='account_sharing'))['allowed']


async def test_duplicate_and_late_lifecycle_never_restore_or_guess_membership(host, tmp_path):
    from modules.access_control.policy import AccessControl

    acl = host.service.access_provider()
    acl.clock = lambda: 100
    account = await host.create()
    await access(host, 'whitelist', enabled=True)
    await scope_grant(host, resources=[account['id']])
    await access(host, 'deny', member='other-member', scope='member')
    await host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a', timestamp=101, event_id='left-1', plugin=False)
    await host.lifecycle('GROUP_MEMBER_ADD', member='member-a', timestamp=102, event_id='join-2', plugin=False)
    await scope_grant(host, resources=[account['id']])
    before = (await (await host.api('GET', 'access')).json())['data']
    await host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a', timestamp=101, event_id='left-1', plugin=False)
    after = (await (await host.api('GET', 'access')).json())['data']
    assert after['version'] == before['version']
    assert (await acl.decide('bot-a', 'account_sharing', 'group-a', 'member-a', account['id']))['allowed']
    await host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a', timestamp=101, event_id='delayed-unknown', plugin=False)
    result = await acl.decide('bot-a', 'account_sharing', 'group-a', 'member-a', account['id'])
    assert result['membership'] == 'pending' and not result['allowed']
    restored = AccessControl(tmp_path / 'access' / 'data' / 'access-control.db', clock=lambda: 103)
    await restored.initialize()
    try:
        await restored.observe(await host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a', timestamp=101, event_id='delayed-unknown', plugin=False))
        result = await restored.decide('bot-a', 'account_sharing', 'group-a', 'member-a', account['id'])
        assert not result['allowed'] and result['membership'] == 'pending'
        state = await restored.snapshot('bot-a')
        assert len(state['revocations']) == 1 and len(state['denies']) == 1
    finally:
        restored.close()
    await scope_grant(host, resources=[account['id']])
    await host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a', plugin=False)
    result = await acl.decide('bot-a', 'account_sharing', 'group-a', 'member-a', account['id'])
    assert result['membership'] == 'pending' and not result['allowed']


async def test_leave_during_query_and_old_cycle_buttons_cannot_send(host):
    from test_binding import bind
    from test_buttons import callback

    acl = host.service.access_provider()
    acl.clock = lambda: 100
    account = await host.create()
    await access(host, 'whitelist', enabled=True)
    await scope_grant(host, resources=[account['id']])
    await bind(host)
    await host.event('账户列表', user='private-a')
    old_button = callback(host.sender.messages[-1], '测试账户')

    async def leave_while_replying(event, text):
        if '尝试私发' in text:
            await host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a', timestamp=101, plugin=False)

    host.sender.after_reply = leave_while_replying
    host.sender.messages.clear()
    await host.event(f'账户查询 {account["id"]}', user='member-a', group='group-a')
    assert not any('fake-secret' in str(m) for m in host.sender.messages)
    host.sender.after_reply = None
    await host.lifecycle('GROUP_MEMBER_ADD', member='member-a', timestamp=102, plugin=False)
    await scope_grant(host, resources=[account['id']])
    host.sender.messages.clear()
    await host.click(old_button)
    assert not any('fake-secret' in str(m) for m in host.sender.messages)
    await host.event(f'账户查询 {account["id"]}', user='private-a')
    assert any(m[0:2] == ('private', 'private-a') and 'fake-secret' in m[2] for m in host.sender.messages)


async def test_other_bot_remains_independent_and_joins_never_contact_members(host):
    account = await host.create()
    other = await host.create(bot='bot-b')
    acl = host.service.access_provider()
    acl.clock = lambda: 100
    await access(host, 'whitelist', enabled=True)
    await scope_grant(host, resources=[account['id']])
    response = await host.api('POST', 'access', {'operation': 'whitelist', 'group': 'group-a', 'enabled': True}, bot='bot-b')
    assert response.status == 200
    response = await host.api(
        'POST',
        'access',
        {'operation': 'grant', 'group': 'group-a', 'member': 'member-a', 'identity_type': 'group_member_openid', 'confirmed': True, 'resources': [other['id']]},
        bot='bot-b',
    )
    assert response.status == 200
    host.sender.messages.clear()
    await host.lifecycle('GROUP_MEMBER_ADD', member='new-member', timestamp=101, plugin=False)
    await host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a', timestamp=101, plugin=False)
    assert host.sender.messages == []
    assert (await acl.decide('bot-b', 'account_sharing', 'group-a', 'member-a', other['id']))['allowed']
    assert not (await acl.decide('bot-a', 'account_sharing', 'group-a', 'member-a', account['id']))['allowed']


async def test_owner_member_history_exposes_cycles_without_restoring_revoked_sources(host):
    acl = host.service.access_provider()
    acl.clock = lambda: 100
    account = await host.create()
    await access(host, 'whitelist', enabled=True)
    source = await scope_grant(host, resources=[account['id']])
    await host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a', timestamp=101, plugin=False)
    await host.lifecycle('GROUP_MEMBER_ADD', member='member-a', timestamp=102, plugin=False)
    await host.event('账户管理 成员 {"group":"group-a","member":"member-a"}')
    text = host.sender.messages[-1][2]
    assert source['id'] in text and 'unverified' in text and 'member_left' in text
    response = await access(host, 'members')
    result = (await response.json())['data']
    assert result['members'][0]['cycle'] != source['cycle']
    assert result['revocations'][0]['id'] == source['id']
    await scope_grant(host, resources=[account['id']])
    response = await access(host, 'members')
    assert len((await response.json())['data']['revocations']) == 1


async def test_same_second_and_missing_native_identity_pause_without_guessing(host):
    acl = host.service.access_provider()
    acl.clock = lambda: 100
    account = await host.create()
    await access(host, 'whitelist', enabled=True)
    await scope_grant(host, resources=[account['id']])
    await host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a', timestamp=101, plugin=False)
    await host.lifecycle('GROUP_MEMBER_ADD', member='member-a', timestamp=101, plugin=False)
    result = await acl.decide('bot-a', 'account_sharing', 'group-a', 'member-a', account['id'])
    assert result['membership'] == 'pending' and not result['allowed']
    await scope_grant(host, resources=[account['id']])
    await host.lifecycle('GROUP_MEMBER_REMOVE', member='', timestamp=102, plugin=False)
    result = await acl.decide('bot-a', 'account_sharing', 'group-a', 'member-a', account['id'])
    assert result['membership'] == 'pending' and result['verification_reason'] == 'native_identity_unavailable'
    assert not result['allowed']


async def test_identifierless_repeated_remove_after_regrant_is_not_assumed_duplicate(host):
    acl = host.service.access_provider()
    acl.clock = lambda: 100
    account = await host.create()
    await access(host, 'whitelist', enabled=True)
    await scope_grant(host, resources=[account['id']])
    await host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a', plugin=False)
    await scope_grant(host, resources=[account['id']])
    assert (await acl.decide('bot-a', 'account_sharing', 'group-a', 'member-a', account['id']))['allowed']
    await host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a', plugin=False)
    result = await acl.decide('bot-a', 'account_sharing', 'group-a', 'member-a', account['id'])
    assert not result['allowed'] and result['membership'] == 'pending'

    await scope_grant(host, resources=[account['id']])
    await host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a', timestamp=101, plugin=False)
    await host.lifecycle('GROUP_MEMBER_ADD', member='member-a', timestamp=102, plugin=False)
    await scope_grant(host, resources=[account['id']])
    await host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a', timestamp=101, plugin=False)
    result = await acl.decide('bot-a', 'account_sharing', 'group-a', 'member-a', account['id'])
    assert not result['allowed'] and result['membership'] == 'pending'
