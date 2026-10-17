"""Owner tasks and result retries use real HTTP authentication and persisted operations."""

import json

from test_access import access, grant


async def test_owner_tasks_filter_by_bot_group_member_and_retries_are_idempotent(host):
    account = await host.create()
    await host.event('账户管理 列表')
    host.sender.fail_private = True
    await grant(host, account)
    response = await host.api('GET', 'tasks')
    assert response.status == 200, await response.text()
    tasks = (await response.json())['data']
    notification = next(t for t in tasks if t['type'] == 'notification')
    assert any(t['type'] == 'binding' and t['member'] == 'member-a' for t in tasks)
    assert (await host.api('GET', 'tasks', authenticated=False)).status == 401
    assert not (await (await host.api('GET', 'tasks', bot='bot-b')).json())['data']
    host.sender.fail_private = False
    host.sender.messages.clear()
    for _ in range(2):
        response = await host.api('POST', 'tasks', {'operation': 'retry_notification', 'id': notification['operation_id']})
        assert response.status == 200
    assert len(host.sender.messages) == 1
    assert 'fake-secret' not in str(host.sender.messages)
    assert (await host.api('POST', 'tasks', {'operation': 'retry_notification', 'id': notification['operation_id']}, bot='bot-b')).status == 404
    await host.event('账户管理 待办 {"group":"group-a","member":"member-a"}')
    shown = json.loads(host.sender.messages[-1][2])
    assert shown and all(t.get('group') == 'group-a' and t.get('member') == 'member-a' for t in shown)


async def test_non_owner_cannot_manage_tasks_and_verification_remains_separate(host):
    account = await host.create()
    await grant(host, account)
    await host.restart_access()
    response = await host.api('GET', 'tasks')
    assert any(t['type'] == 'verification' for t in (await response.json())['data'])
    host.sender.messages.clear()
    await host.event('账户管理 待办', user='private-a')
    assert 'member-a' not in str(host.sender.messages)
    response = await access(
        host, 'verify', identity_type='group_member_openid', confirmed=True, state='present', evidence='主人核实同一周期', actor='forged-owner'
    )
    assert response.status == 200
    record = (await (await host.api('GET', 'access')).json())['data']['verifications'][-1]
    assert record['actor'] == 'panel_admin'


async def test_continuous_scope_tasks_include_future_accounts_and_bulk_verification_status(host):
    account = await host.create()
    await access(host, 'whitelist', enabled=True)
    await access(host, 'grant', resource_scope={'kind': 'all'}, mode='continuous', identity_type='group_member_openid', confirmed=True)
    added = await host.create('后来新增')
    await host.event('账户管理 待办 ' + json.dumps({'resource': added['id']}))
    tasks = json.loads(host.sender.messages[-1][2])
    assert any(t['type'] == 'binding' and added['id'] in t['resources'] for t in tasks)
    await host.restart_access()
    result = await access(host, 'bulk_verify', state='present', evidence='已核对同一周期', confirmed=True, members=[{'member': 'member-a', 'confirmed': True}])
    operation = (await result.json())['data']['operation_result']
    assert operation['verification'] == 'saved'
    assert operation.get('authorization', 'not_changed') == 'not_changed'
    assert account['id'] != added['id']
