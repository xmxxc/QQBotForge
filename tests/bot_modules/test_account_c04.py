"""Owner to-dos and notifications, one-way module dependencies and the whole owner journey (C04)."""

import ast
import json
from pathlib import Path

from account_host import account_host as account_host  # noqa: F401
from socket_platform import until
from test_access_control import access_host as access_host  # noqa: F401
from test_account_c01 import bind, owner
from test_account_c02 import grant
from test_account_c03 import ask, enable
from test_group_roster import member, page, roster_platform

ROOT = Path(__file__).absolute().parents[2]


def imports(package):
    names = set()
    for path in (ROOT / package).rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names.add(node.module)
    return names


def test_dependencies_point_one_way():
    group_state, access, qa, account = (imports(p) for p in ('modules/group_state', 'modules/access_control', 'modules/self_qa', 'plugins/account_sharing'))
    upper = ('modules.access_control', 'modules.self_qa', 'plugins.')
    assert not any(name.startswith(upper) for name in group_state)
    assert not any(name.startswith(('modules.self_qa', 'plugins.', 'modules.group_state')) for name in access)
    # Q&A reaches authorization only through the module manager at run time.
    assert not any(name.startswith(('modules.access_control', 'modules.group_state', 'plugins.')) for name in qa)
    assert not any(name.startswith('modules.') for name in account)


async def test_todos_filter_point_to_modules_and_notifications_are_idempotent(account_host):
    host = account_host
    a, b = await host.create(name='甲'), await host.create(name='乙')
    # Whitelist over HTTP: no owner private message yet, so no verified notification target.
    status, _ = await host.account_api('POST', 'access', {'operation': 'whitelist', 'group': 'group-a', 'enabled': True})
    assert status == 200
    for member_id in ('member-a', 'member-b'):
        await host.state.confirm_member('bot-a', 'group-a', member_id, evidence='checked', member_role='member')
    await grant(host, 'member-a', a['id'])
    await grant(host, 'member-b', b['id'])
    tasks = (await host.account_api('GET', 'tasks'))[1]['data']
    bindings = [t for t in tasks if t['type'] == 'binding']
    assert {t['member'] for t in bindings} == {'member-a', 'member-b'} and all(t['entry'] == 'access-control' for t in bindings)
    response = await host.http.get(
        '/api/ext/account-sharing/tasks', params={'bot': 'bot-a', 'member': 'member-b', 'resource': b['id']}, headers={'Cookie': 'elaina_session=group-test'}
    )
    filtered = (await response.json())['data']
    assert filtered and all(t.get('member', 'member-b') == 'member-b' for t in filtered if t['type'] != 'notification')
    # Owner notifications had no verified private target: kept as to-dos, never sent to the group.
    notifications = [t for t in tasks if t['type'] == 'notification']
    assert notifications and all(t['notification'] == 'unavailable' for t in notifications)
    assert all(m[0] != 'push' for m in host.sender.messages)
    # The owner's own private message establishes the verified target; retries send once.
    await owner(host, '列表')
    operation = notifications[0]['operation_id']
    for _ in range(2):
        status, result = await host.account_api('POST', 'tasks', {'operation': 'retry_notification', 'id': operation})
        assert status == 200 and result['data']['notification'] == 'sent'
    owner_pushes = [m for m in host.sender.messages if m[0] == 'push' and m[1] == 'owner-a']
    assert len(owner_pushes) == 1 and 'fake-secret' not in owner_pushes[0][2]
    # A member who is no longer reliably known shows a group-state verification to-do.
    host.sockets['bot-a'].frames.put_nowait(OSError('fake disconnect'))
    await until(lambda: not host.state.recovery.get_status('bot-a').ready)
    verification = [t for t in (await host.account_api('GET', 'tasks'))[1]['data'] if t['type'] == 'verification']
    assert {t['member'] for t in verification} == {'member-a', 'member-b'} and all(t['entry'] == 'group-state' for t in verification)


async def test_owner_journey_over_real_framework_entries(account_host):
    host = account_host
    account = await host.create()
    await host.account_api('POST', 'rules', {'name': '会员', 'target': account['id'], 'keywords': ['会员']})
    await owner(host, '白名单 ' + json.dumps({'group': 'group-a', 'enabled': True}))
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='checked', member_role='member')
    # Owner grants in the group: only a short status there; the binding guidance goes to the group.
    await host.event('账户管理 授权 ' + json.dumps({'member': 'member-a', 'resources': [account['id']]}), user='owner-a', group='group-a')
    lines = host.texts()
    assert '授权操作已保存，详情请在主人私聊或后台查看' in lines and any('身份绑定 申请' in line for line in lines)
    assert all('fake-secret' not in line for line in lines)
    await bind(host, 'group-a', 'member-a')
    # Keyword query: status in the group, credentials privately.
    group, private = await ask(host, account)
    assert all('fake-secret' not in g for g in group) and any('fake-secret' in p for p in private)
    # Full roster with both Bots granted: group delivery.
    roster_platform(host)
    # The owner spoke in the group, so the real roster includes the owner too.
    host.sender.json_results[('/v2/groups/group-a/members', 'second')] = page(
        member('this-bot', 'admin', bot=True), member('other-bot', bot=True), member('owner-a', 'owner')
    )
    assert (await host.state.sync_roster('bot-a', 'group-a'))['complete']
    for member_id in ('this-bot', 'other-bot', 'owner-a'):
        await grant(host, member_id, account['id'])
    assert (await enable(host, account))['actual'] == 'group'
    group, private = await ask(host, account)
    assert any('fake-secret' in g for g in group) and private == []
    # A newcomer ends group delivery.
    host.lifecycle('GROUP_MEMBER_ADD', member='newcomer', sequence=2, timestamp=1000)
    await until(lambda: 'newcomer' in host.state.states['bot-a']['groups']['group-a']['members'])
    await until(lambda: host.state.recovery.get_status('bot-a').ready)
    group, private = await ask(host, account)
    assert all('fake-secret' not in g for g in group)
    # Leaving revokes; rejoining needs a new grant from the owner.
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-a', sequence=3, timestamp=1001)
    await until(lambda: host.state.states['bot-a']['groups']['group-a']['members']['member-a'].get('status') == 'left')
    await until(lambda: host.state.recovery.get_status('bot-a').ready)
    snapshot = await host.access.access_snapshot('bot-a', 'account_sharing')
    assert all(g['member'] != 'member-a' for g in snapshot['grants']) and any(r['member'] == 'member-a' for r in snapshot['revocations'])
    host.lifecycle('GROUP_MEMBER_ADD', member='member-a', sequence=4, timestamp=1002)
    await until(lambda: host.state.states['bot-a']['groups']['group-a'].get('last_native', {}).get('sequence') == 4)
    await until(lambda: host.state.recovery.get_status('bot-a').ready)
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='rejoined', member_role='member')
    await host.event(f'账户查询 {account["id"]}', user='private-a')
    assert all('fake-secret' not in t for t in host.texts())
    await grant(host, 'member-a', account['id'])
    await host.event(f'账户查询 {account["id"]}', user='private-a')
    assert 'fake-secret' in host.texts('private-a')[-1]
    # Owner results stay in the operations log with separate statuses and no credentials.
    operations = (await host.account_api('GET', 'operations'))[1]['data']
    kinds = {o['kind'] for o in operations}
    assert {'grant', 'delivery', 'group_mode'} <= kinds and all('fake-secret' not in json.dumps(o) for o in operations)
    grant_result = next(o for o in operations if o['kind'] == 'grant')
    assert {'authorization', 'binding', 'notification', 'delivery'} <= grant_result.keys()
