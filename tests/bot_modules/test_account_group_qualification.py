"""Account group choices require an actual explicitly granted, enabled resource."""

import pytest
from account_host import account_host as account_host  # noqa: F401
from test_access_control import access_host as access_host  # noqa: F401
from test_account_audit_menus import button, grant
from test_account_c01 import bind


async def usable(h, account, group, member):
    await grant(h, account, member, group)
    await bind(h, group, member)
    await h.event('账户查询 ' + account['id'], user=member, group=group)
    assert any('fake-secret' in text for text in h.texts('private-a')), 'the granted baseline must query successfully'


async def test_only_group_with_viewable_account_is_auto_selected(account_host):
    h = account_host
    a = await h.create(name='valid-account')
    await usable(h, a, 'group-empty', 'member-empty')
    await usable(h, a, 'group-valid', 'member-valid')
    await h.account_api('POST', 'access', {'operation': 'revoke', 'group': 'group-empty', 'member': 'member-empty'})
    await h.account_api('POST', 'access', {'operation': 'grant', 'group': 'group-empty', 'member': 'member-empty', 'resources': []})
    h.sender.messages.clear()
    await h.event('账户列表', user='private-a')
    texts = h.texts('private-a')
    assert any('valid-account' in text for text in texts)
    assert all('请选择授权群' not in text and 'group-empty' not in text for text in texts)


@pytest.mark.parametrize('kind', ['group', 'role', 'members', 'public'])
async def test_implicit_policies_never_create_account_group_choices(account_host, kind):
    h = account_host
    a = await h.create(name='hidden-account')
    for group, member in [('group-a', 'member-a'), ('group-b', 'member-b')]:
        await usable(h, a, group, member)
        assert (await h.account_api('POST', 'access', {'operation': 'revoke', 'group': group, 'member': member}))[0] == 200
    policy = {'kind': kind, 'groups': ['group-a', 'group-b'], 'private': True}
    if kind == 'role':
        policy['roles'] = ['admin']
    if kind == 'members':
        policy['members'] = ['member-a', 'member-b']
    await h.access.manage_access('bot-a', 'account_sharing', 'policy', function='account_sharing', policy=policy)
    h.sender.messages.clear()
    await h.event('账户列表', user='private-a')
    texts = h.texts('private-a')
    assert texts == ['你暂无此功能的使用权限，请联系主人']


@pytest.mark.parametrize(
    'loss', ['binding_only', 'invoke_only', 'view_only', 'expired', 'denied', 'not_admitted', 'disabled', 'deleted', 'other_bot', 'other_consumer']
)
async def test_ineligible_groups_are_removed_before_single_group_selection(account_host, loss):
    h = account_host
    bad = await h.create(name='ineligible-title')
    good = await h.create(name='eligible-title')
    await usable(h, bad, 'group-bad', 'member-bad')
    await usable(h, good, 'group-good', 'member-good')
    if loss in {'binding_only', 'invoke_only', 'view_only', 'other_bot', 'other_consumer'}:
        assert (await h.account_api('POST', 'access', {'operation': 'revoke', 'group': 'group-bad', 'member': 'member-bad'}))[0] == 200
    if loss in {'invoke_only', 'view_only'}:
        assert (
            await h.account_api(
                'POST',
                'access',
                {
                    'operation': 'grant',
                    'group': 'group-bad',
                    'member': 'member-bad',
                    'resources': [] if loss == 'invoke_only' else [bad['id']],
                    'invoke': loss == 'invoke_only',
                },
            )
        )[0] == 200
    elif loss == 'expired':
        assert (await h.account_api('POST', 'access', {'operation': 'expiry', 'group': 'group-bad', 'member': 'member-bad', 'expires_at': 1}))[0] == 200
    elif loss == 'denied':
        assert (await h.account_api('POST', 'access', {'operation': 'deny', 'scope': 'group', 'group': 'group-bad'}))[0] == 200
    elif loss == 'not_admitted':
        assert (await h.account_api('POST', 'access', {'operation': 'whitelist', 'group': 'group-bad', 'enabled': False}))[0] == 200
    elif loss == 'disabled':
        assert (await h.account_api('PATCH', 'accounts/' + bad['id'], {'enabled': False}))[0] == 200
    elif loss == 'deleted':
        assert (await h.account_api('DELETE', 'accounts/' + bad['id']))[0] == 200
    elif loss in {'other_bot', 'other_consumer'}:
        bot, consumer = ('bot-b', 'account_sharing') if loss == 'other_bot' else ('bot-a', 'other')
        await h.state.confirm_member(bot, 'group-bad', 'member-bad', evidence='checked', member_role='admin')
        await h.access.declare(
            bot, consumer, owner='plugin:' + consumer, functions={'account_sharing': {'kind': 'explicit', 'groups': ['group-bad'], 'private': True}}
        )
        await h.access.declare_resources(bot, consumer, [{'id': bad['id']}])
        await h.access.manage_access(bot, consumer, 'grant', function='account_sharing', group='group-bad', member='member-bad', resources=[bad['id']])
    h.sender.messages.clear()
    await h.event('账户列表', user='private-a')
    texts = h.texts('private-a')
    assert any('eligible-title' in text for text in texts), texts
    assert all('group-bad' not in text and 'ineligible-title' not in text and '请选择授权群' not in text for text in texts)


async def test_mixed_groups_are_filtered_before_paging_and_old_choice_expires(account_host):
    h = account_host
    a = await h.create(name='shared-title')
    for i in range(15):
        group, member = f'group-{i:02}', f'member-{i:02}'
        await usable(h, a, group, member)
        if i % 2:
            await h.account_api('POST', 'access', {'operation': 'revoke', 'group': group, 'member': member})
            await h.account_api('POST', 'access', {'operation': 'grant', 'group': group, 'member': member, 'resources': []})
    await h.event('账户列表', user='private-a')
    text = h.texts('private-a')[-1]
    assert '（1/2）' in text and 'group-10' in text and 'group-12' not in text
    assert all(f'group-{i:02}' not in text for i in range(1, 15, 2))
    first_choice = button(h, 'group-00')
    await h.click(button(h, '下一页'), user='private-a')
    text = h.texts('private-a')[-1]
    assert '（2/2）' in text and 'group-12' in text and 'group-14' in text and 'group-10' not in text
    await h.click(button(h, '上一页'), user='private-a')
    assert '（1/2）' in h.texts('private-a')[-1]
    await h.click(first_choice, user='private-a')
    assert any(a['name'] in text for text in h.texts('private-a'))
    old_account = button(h, a['name'])
    await h.account_api('POST', 'access', {'operation': 'revoke', 'group': 'group-00', 'member': 'member-00'})
    # Invocation alone cannot keep the saved selection or old buttons alive.
    await h.account_api('POST', 'access', {'operation': 'grant', 'group': 'group-00', 'member': 'member-00', 'resources': []})
    h.sender.messages.clear()
    await h.event('账户列表', user='private-a')
    texts = h.texts('private-a')
    assert any('请选择授权群' in text for text in texts) and all('group-00' not in text for text in texts)
    await h.click(old_account, user='private-a')
    await h.click(first_choice, user='private-a')
    assert all('fake-secret' not in text and a['name'] not in text and 'group-00' not in text for text in h.texts())


async def test_direct_query_selects_groups_for_the_requested_resource(account_host):
    h = account_host
    a, b = await h.create(name='account-a'), await h.create(name='account-b')
    await usable(h, a, 'group-a', 'member-a')
    await usable(h, b, 'group-b', 'member-b')
    await h.event('账户查询 ' + b['id'], user='private-a')
    texts = h.texts('private-a')
    assert any('account-b' in text and 'fake-secret' in text for text in texts), texts
    assert all('请选择授权群' not in text and 'account-a' not in text for text in texts)


@pytest.mark.parametrize('change', ['revoke_view', 'expire_view', 'deny', 'delete'])
@pytest.mark.parametrize('stage', ['menu_write', 'final_identity'])
async def test_group_choices_recheck_resource_qualification_after_waits(account_host, monkeypatch, change, stage):
    h = account_host
    a, b = await h.create(name='private-a-title'), await h.create(name='private-b-title')
    await usable(h, a, 'group-a', 'member-a')
    await usable(h, b, 'group-b', 'member-b')
    # Separate invocation and viewing sources: losing only view must still hide the group.
    await h.account_api('POST', 'access', {'operation': 'revoke', 'group': 'group-a', 'member': 'member-a'})
    await h.account_api('POST', 'access', {'operation': 'grant', 'group': 'group-a', 'member': 'member-a', 'resources': []})
    clock = [100]
    h.access.clock = lambda: clock[0]
    status, result = await h.account_api(
        'POST',
        'access',
        {
            'operation': 'grant',
            'group': 'group-a',
            'member': 'member-a',
            'resources': [a['id']],
            'invoke': False,
            'expires_at': 200,
        },
    )
    assert status == 200, result
    source = result['data']['id']
    await h.event('账户查询 ' + a['id'], user='member-a', group='group-a')
    assert any('fake-secret' in text for text in h.texts('private-a'))
    service = h.plugins._plugins['account_sharing'].module._instance
    issue, lookup = service.menus.issue, h.access.private_contexts
    issued, changed = [], []

    async def invalidate():
        changed.append(True)
        if change == 'revoke_view':
            await h.account_api('POST', 'access', {'operation': 'revoke', 'group': 'group-a', 'member': 'member-a', 'source': source})
        elif change == 'expire_view':
            clock[0] = 201
        elif change == 'deny':
            await h.account_api('POST', 'access', {'operation': 'deny', 'group': 'group-a', 'scope': 'group'})
        else:
            await h.account_api('DELETE', 'accounts/' + a['id'])

    async def issuing(*args, **kwargs):
        rows = await issue(*args, **kwargs)
        issued.append(True)
        if stage == 'menu_write':
            await invalidate()
        return rows

    async def looking(*args, **kwargs):
        found = await lookup(*args, **kwargs)
        if stage == 'final_identity' and issued and not changed:
            await invalidate()
        return found

    monkeypatch.setattr(service.menus, 'issue', issuing)
    monkeypatch.setattr(h.access, 'private_contexts', looking)
    h.sender.messages.clear()
    await h.event('账户列表', user='private-a')
    assert issued and changed, 'the invalidation must happen during candidate preparation'
    assert all('group-a' not in text and a['name'] not in text for text in h.texts())
    monkeypatch.setattr(service.menus, 'issue', issue)
    monkeypatch.setattr(h.access, 'private_contexts', lookup)
    await h.event('账户列表', user='private-a')
    texts = h.texts('private-a')
    assert any(b['name'] in text for text in texts) and all('请选择授权群' not in text for text in texts)


async def test_saved_group_losing_last_view_does_not_rebind_old_account_button(account_host):
    h = account_host
    a = await h.create(name='shared-account')
    await usable(h, a, 'group-a', 'member-a')
    await usable(h, a, 'group-b', 'member-b')
    await h.event('账户群 group-a', user='private-a')
    assert any(a['name'] in text for text in h.texts('private-a'))
    old = button(h, a['name'])
    await h.account_api('POST', 'access', {'operation': 'revoke', 'group': 'group-a', 'member': 'member-a'})
    await h.account_api('POST', 'access', {'operation': 'grant', 'group': 'group-a', 'member': 'member-a', 'resources': []})
    h.sender.messages.clear()
    await h.event('账户列表', user='private-a')
    texts = h.texts('private-a')
    assert any(a['name'] in text for text in texts) and all('请选择授权群' not in text for text in texts)
    await h.click(old, user='private-a')
    assert all('fake-secret' not in text for text in h.texts())
    await h.event('账户查询 ' + a['id'], user='private-a')
    assert any('fake-secret' in text for text in h.texts('private-a'))


async def test_another_current_account_keeps_group_eligible_when_first_witness_expires(account_host, monkeypatch):
    h = account_host
    a, b = await h.create(name='expiring-account'), await h.create(name='remaining-account')
    await usable(h, a, 'group-a', 'member-a')
    await usable(h, b, 'group-a', 'member-a')
    await usable(h, b, 'group-b', 'member-b')
    clock = [100]
    h.access.clock = lambda: clock[0]
    snapshot = await h.access.access_snapshot('bot-a', 'account_sharing')
    source = next(s['id'] for s in snapshot['grants'] if s['group'] == 'group-a' and a['id'] in s['resources'])
    await h.account_api('POST', 'access', {'operation': 'expiry', 'group': 'group-a', 'member': 'member-a', 'source': source, 'expires_at': 200})
    service = h.plugins._plugins['account_sharing'].module._instance
    issue, lookup = service.menus.issue, h.access.private_contexts
    issued, changed = [], []

    async def issuing(*args, **kwargs):
        rows = await issue(*args, **kwargs)
        issued.append(True)
        return rows

    async def looking(*args, **kwargs):
        found = await lookup(*args, **kwargs)
        if issued and not changed and kwargs.get('resource') == a['id']:
            clock[0] = 201
            changed.append(True)
        return found

    monkeypatch.setattr(service.menus, 'issue', issuing)
    monkeypatch.setattr(h.access, 'private_contexts', looking)
    h.sender.messages.clear()
    await h.event('账户列表', user='private-a')
    assert changed
    texts = h.texts('private-a')
    assert any('group-a' in text and 'group-b' in text for text in texts), texts
