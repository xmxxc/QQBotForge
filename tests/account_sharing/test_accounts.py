async def test_owner_management_and_latest_private_preview(host):
    denied = await host.api('POST', 'accounts', {'name': 'x'}, authenticated=False)
    assert denied.status == 401
    account = await host.create()
    await host.event(f'账户管理 预览 {account["id"]}', user='ordinary')
    assert not any('fake-secret' in str(msg) for msg in host.sender.messages)
    await host.event(f'账户管理 预览 {account["id"]}')
    assert host.sender.messages[-1][0:2] == ('private', 'owner-a')
    assert 'fake-secret' in host.sender.messages[-1][2]
    response = await host.api('PATCH', 'accounts/' + account['id'], {'password': 'new-fake-secret'})
    assert response.status == 200
    await host.event(f'账户管理 预览 {account["id"]}')
    assert 'new-fake-secret' in host.sender.messages[-1][2]
    await host.api('PATCH', 'accounts/' + account['id'], {'enabled': False})
    host.sender.messages.clear()
    await host.event(f'账户管理 预览 {account["id"]}')
    assert not any('secret' in str(msg) for msg in host.sender.messages)
    await host.api('DELETE', 'accounts/' + account['id'])
    response = await host.api('GET', 'accounts')
    assert (await response.json())['data'] == []


async def test_bot_isolation_restart_and_command_management(host, tmp_path):
    from plugins.account_sharing.service import AccountSharing

    a = await host.create()
    b = await host.create(bot='bot-b', password='other-fake-secret')
    await host.event(f'账户管理 修改 {a["id"]} {{"name":"改名","description":"说明","url":"https://example.com"}}')
    restored = AccountSharing(tmp_path / 'plugin' / 'data' / 'account-sharing.db')
    try:
        accounts = await restored.accounts('bot-a')
        assert accounts[0]['name'] == '改名'
        assert accounts[0]['id'] == a['id']
        assert (await restored.accounts('bot-b'))[0]['id'] == b['id']
    finally:
        restored.close()
    host.sender.messages.clear()
    await host.event(f'账户管理 预览 {b["id"]}')
    await host.event(f'账户管理 预览 {a["id"]}', group='group-a')
    assert not any('secret' in str(msg) for msg in host.sender.messages)
    await host.event('账户管理 新增 {"name":"命令账户","login":"fake","password":"fake"}')
    response = await host.api('GET', 'accounts')
    assert len((await response.json())['data']) == 2


async def test_management_boundaries_and_plugin_lifecycle(host, tmp_path, monkeypatch):
    from core.plugin.web_pages import get_page_html, match_route

    response = await host.api('POST', 'accounts', {'name': '<script>', 'login': 'fake', 'password': 'fake', 'enabled': 'false'})
    assert response.status == 422
    response = await host.api('GET', 'accounts', bot='unconfigured')
    assert response.status == 404
    response = await host.http.post(
        '/api/ext/account-sharing/accounts?bot=bot-a', json={}, headers={'Cookie': 'elaina_session=test-session', 'Origin': 'https://external.example'}
    )
    assert response.status in {401, 403}
    await host.event('账户管理 新增 {"name":"x","login":"x","password":"x"}', bot='bot-b', user='owner-a')
    assert (await (await host.api('GET', 'accounts', bot='bot-b')).json())['data'] == []
    assert '日志' in get_page_html('account-sharing')
    assert match_route('GET', '/api/ext/account-sharing/accounts')['auth'] is True
    await host.entry.unload()
    assert get_page_html('account-sharing') is None
    assert match_route('GET', '/api/ext/account-sharing/accounts') is None


async def test_plugin_reload_and_unavailable_storage(host):
    import importlib

    account = await host.create()
    old_id = account['id']
    await host.entry.unload()
    importlib.reload(host.entry)
    await host.entry.load()
    await host.event(f'账户管理 预览 {old_id}')
    assert 'fake-secret' in host.sender.messages[-1][2]
    await host.api('DELETE', 'accounts/' + old_id)
    host.sender.messages.clear()
    await host.event(f'账户管理 预览 {old_id}')
    assert not any('fake-secret' in str(msg) for msg in host.sender.messages)
    host.service.store.close()
    response = await host.api('GET', 'accounts')
    assert response.status == 503
    assert '暂不可用' in (await response.json())['error']
    await host.event('账户管理 列表')
    assert '暂不可用' in host.sender.messages[-1][2]
