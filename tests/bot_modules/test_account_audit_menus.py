"""Personal directories and callbacks must survive adversarial scheduling safely."""

import pytest
from account_host import account_host as account_host  # noqa: F401
from test_access_control import access_host as access_host  # noqa: F401
from test_account_audit_management import ready
from test_account_c01 import bind


async def grant(h, a, member='member-a', group='group-a'):
    await ready(h, member, group)
    status, result = await h.account_api('POST', 'access', {'operation': 'grant', 'group': group, 'member': member, 'resources': [a['id']]})
    assert status == 200, result
    h.sender.messages.clear()


def button(h, name):
    return next(b['data'] for row in h.sender.keyboards[-1] for b in row if b['text'] == name)


async def test_group_directory_is_private_and_no_public_fallback(account_host):
    h = account_host
    a = await h.create(name='private-only-name')
    await grant(h, a)
    await bind(h, 'group-a', 'member-a')
    await h.event('账户列表', user='member-a', group='group-a')
    assert all(a['name'] not in m[2] and a['id'] not in m[2] for m in h.sender.messages if m[1] == 'group-a')
    assert any(a['name'] in m[2] for m in h.sender.messages if m[1] == 'private-a')
    h.sender.messages.clear()
    h.sender.push_result = (False, {'code': 40034})
    await h.event('账户列表', user='member-a', group='group-a')
    assert all(a['name'] not in m[2] and a['id'] not in m[2] for m in h.sender.messages)


@pytest.mark.parametrize('change', ['revoke', 'delete', 'rename', 'rule', 'binding'])
async def test_menu_last_wait_rechecks_qualification_and_content(account_host, monkeypatch, change):
    h = account_host
    a = await h.create(name='stale-name')
    await grant(h, a)
    await bind(h, 'group-a', 'member-a')
    service = h.plugins._plugins['account_sharing'].module._instance
    original = service.menus.issue

    async def issue(*args, **kwargs):
        rows = await original(*args, **kwargs)
        if change == 'revoke':
            await h.access.manage_access('bot-a', 'account_sharing', 'revoke', function='account_sharing', group='group-a', member='member-a')
        elif change == 'delete':
            await h.account_api('DELETE', 'accounts/' + a['id'])
        elif change == 'rename':
            await h.account_api('PATCH', 'accounts/' + a['id'], {'name': 'new-name'})
        elif change == 'binding':
            await h.access.bindings.manage('bot-a', 'binding_revoke', {'group': 'group-a', 'member': 'member-a'}, 'owner')
        else:
            await h.modules.get('self_qa').manage('bot-a', 'account_sharing', 'create', {'name': 'new-rule', 'target': a['id'], 'keywords': ['new']})
        return rows

    monkeypatch.setattr(service.menus, 'issue', issue)
    await h.event('账户列表', user='private-a')
    assert all('stale-name' not in t and a['id'] not in t for t in h.texts())


async def test_old_private_button_does_not_change_group(account_host):
    h = account_host
    a = await h.create(name='two-groups')
    for g, m in [('group-a', 'member-a'), ('group-b', 'member-b')]:
        await grant(h, a, m, g)
        await bind(h, g, m)
    await h.event('账户群 group-a', user='private-a')
    old = button(h, a['name'])
    await h.event('账户群 group-b', user='private-a')
    h.sender.messages.clear()
    await h.click(old, user='private-a')
    texts = h.texts()
    assert all('fake-secret' not in t for t in texts)
    assert any('重新' in t for t in texts)


async def test_group_pages_forward_back_boundary_and_candidate_changes(account_host):
    h = account_host
    a = await h.create()
    for i in range(13):
        g, m = f'group-{i:02}', f'member-{i:02}'
        await grant(h, a, m, g)
        await bind(h, g, m)
    await h.event('账户列表', user='private-a')
    assert '（1/3）' in h.texts('private-a')[-1]
    await h.click(button(h, '下一页'), user='private-a')
    text = h.texts('private-a')[-1]
    assert '（2/3）' in text and 'group-06' in text and 'group-00' not in text
    back = button(h, '上一页')
    await h.click(button(h, '下一页'), user='private-a')
    assert '（3/3）' in h.texts('private-a')[-1]
    assert not any(b['text'] == '下一页' for row in h.sender.keyboards[-1] for b in row)
    await h.click(back, user='private-a')
    assert '（1/3）' in h.texts('private-a')[-1]
    next_page = button(h, '下一页')
    for i in range(6, 13):
        await h.account_api('POST', 'access', {'operation': 'revoke', 'group': f'group-{i:02}', 'member': f'member-{i:02}'})
    await h.click(next_page, user='private-a')
    text = h.texts('private-a')[-1]
    assert '（1/1）' in text and 'group-06' not in text
    await h.click(next_page, user='private-b')
    assert all('group-00' not in t for t in h.texts())
    await h.click(next_page, user='private-a', bot='bot-b')
    assert all('group-00' not in t for t in h.texts())


async def test_other_group_keyword_does_not_consume_chat(account_host):
    h = account_host
    a = await h.create()
    await h.account_api('POST', 'rules', {'name': 'elsewhere', 'target': a['id'], 'keywords': ['ordinary'], 'groups': ['group-b']})
    await h.event('ordinary', user='stranger', group='group-a')
    assert h.texts() == []
    await h.event('ordinary', user='stranger', group='group-b')
    assert h.texts() == ['你暂无此功能的使用权限，请联系主人']
    await h.event('ordinary', user='stranger', group='group-b')
    assert h.texts() == []


async def test_directory_unknown_push_reports_unknown_without_retry(account_host):
    h = account_host
    a = await h.create(name='hidden-title')
    await grant(h, a)
    await bind(h, 'group-a', 'member-a')
    h.sender.push_result = (False, {'code': -1})
    await h.event('账户列表', user='member-a', group='group-a')
    texts = h.texts('group-a')
    assert any('结果未知' in t and '身份绑定' in t for t in texts)
    assert all(a['name'] not in t and a['id'] not in t for t in texts)


async def test_old_page_keeps_group_binding_during_dispatch(account_host, monkeypatch):
    h = account_host
    for i in range(7):
        a = await h.create(name=f'group-a-title-{i}')
        await grant(h, a)
    b = await h.create(name='group-b-private-title')
    await grant(h, b, 'member-b', 'group-b')
    await bind(h, 'group-a', 'member-a')
    await bind(h, 'group-b', 'member-b')
    await h.event('账户群 group-a', user='private-a')
    old_page = button(h, '下一页')
    h.sender.messages.clear()
    service = h.plugins._plugins['account_sharing'].module._instance
    original = service.directory

    async def directory(event, **kwargs):
        await service.resolve_context(event, select='group-b')
        return await original(event, **kwargs)

    monkeypatch.setattr(service, 'directory', directory)
    await h.click(old_page, user='private-a')
    texts = h.texts('private-a')
    assert all('group-b-private-title' not in t for t in texts)
    assert any('已失效' in t for t in texts)
