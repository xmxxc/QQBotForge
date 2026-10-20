"""Strict whole-group delivery per group and account, diagnostics and restore choices (C03)."""

import asyncio
import json

from account_host import account_host as account_host  # noqa: F401
from socket_platform import until
from test_access_control import access_host as access_host  # noqa: F401
from test_account_c01 import bind
from test_account_c02 import grant, member_ready
from test_group_roster import roster_platform

DENIED = '你暂无此功能的使用权限，请联系主人'

EVERYONE = ('member-a', 'this-bot', 'other-bot')


async def full_group(host, *accounts, members=EVERYONE):
    """Complete roster (both Bots included) and explicit grants for the given members."""
    await member_ready(host)
    # Binding re-confirms member-a in group state, so sync the full roster afterwards.
    await bind(host, 'group-a', 'member-a')
    roster_platform(host)
    assert (await host.state.sync_roster('bot-a', 'group-a'))['complete']
    # One source per account, so revoking one account never touches another.
    for member in members:
        for account in accounts:
            await grant(host, member, account['id'])


async def enable(host, account, *, enabled=True, auto_restore=None, group='group-a'):
    body = {'group': group, 'resource': account['id'], 'enabled': enabled}
    if auto_restore is not None:
        body['auto_restore'] = auto_restore
    status, data = await host.account_api('POST', 'group-delivery', body)
    assert status == 200, data
    host.sender.messages.clear()
    return data['data']


async def ask(host, account):
    await host.event(f'账户查询 {account["id"]}', user='member-a', group='group-a')
    group = [m[2] for m in host.sender.messages if m[1] == 'group-a']
    private = [m[2] for m in host.sender.messages if m[0] == 'push' and m[1] == 'private-a']
    host.sender.messages.clear()
    return group, private


async def test_group_send_needs_explicit_enable_and_every_member_including_both_bots(account_host):
    host = account_host
    account = await host.create()
    await full_group(host, account, members=('member-a', 'this-bot'))
    # Not enabled: private delivery even if everyone qualified.
    group, private = await ask(host, account)
    assert group[0].startswith('将尝试私发') and any('fake-secret' in p for p in private)
    mode = await enable(host, account)
    assert mode['desired'] == 'group' and mode['actual'] == 'private'
    report = mode['report']
    assert report['complete'] and report['reason'] == 'member_ineligible'
    assert [f['member'] for f in report['failures']] == ['other-bot'] and report['failures_complete']
    assert '_proof' not in report
    group, private = await ask(host, account)
    assert all('fake-secret' not in g for g in group) and any('fake-secret' in p for p in private)
    await grant(host, 'other-bot', account['id'])
    mode = await enable(host, account)
    assert mode['actual'] == 'group'
    group, private = await ask(host, account)
    assert any('fake-secret' in g for g in group) and private == []


async def test_each_change_invalidates_and_accounts_are_judged_separately(account_host):
    host = account_host
    a, b = await host.create(name='甲'), await host.create(name='乙', password='other-secret')
    await full_group(host, a, b)
    assert (await enable(host, a))['actual'] == 'group'
    assert (await enable(host, b))['actual'] == 'group'
    # Revoking member-a's source for 乙 only affects 乙.
    snapshot = await host.access.access_snapshot('bot-a', 'account_sharing')
    source = next(g for g in snapshot['grants'] if g['member'] == 'other-bot' and b['id'] in g['resources'])
    await host.account_api('POST', 'access', {'operation': 'revoke', 'group': 'group-a', 'member': 'other-bot', 'source': source['id']})
    group, _ = await ask(host, a)
    assert any('fake-secret' in g for g in group)
    group, private = await ask(host, b)
    assert all('other-secret' not in g for g in group) and any('other-secret' in p for p in private)
    # A newcomer invalidates 甲 as well; it stays private until the owner acts.
    host.lifecycle('GROUP_MEMBER_ADD', member='newcomer', sequence=2, timestamp=1000)
    await until(lambda: 'newcomer' in host.state.states['bot-a']['groups']['group-a']['members'])
    await until(lambda: host.state.recovery.get_status('bot-a').ready)
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='still here', member_role='member')
    group, private = await ask(host, a)
    assert all('fake-secret' not in g for g in group) and any('fake-secret' in p for p in private)
    modes = {m['resource']: m for m in (await host.account_api('GET', 'group-delivery'))[1]['data']}
    assert modes[a['id']]['desired'] == 'group' and modes[a['id']]['actual'] == 'private'


async def test_expiry_and_disconnect_invalidate_and_restore_needs_a_new_check(account_host):
    host = account_host
    account = await host.create()
    await full_group(host, account)
    assert (await enable(host, account))['actual'] == 'group'
    host.sockets['bot-a'].frames.put_nowait(OSError('fake disconnect'))
    await until(lambda: not host.state.recovery.get_status('bot-a').ready)
    group, private = await ask(host, account)
    assert all('fake-secret' not in g for g in group)
    second = host.gateways['bot-a'].add()
    second.feed(10, d={'heartbeat_interval': 45000})
    await until(lambda: any(f['op'] == 6 for f in second.sent))
    second.feed(0, t='RESUMED', s=2, d='')
    await until(lambda: host.state._ready('bot-a'))
    # Facts settle after the trusted resume; wait until member-a is eligible again.
    for _ in range(300):
        decision = await host.access.evaluate('bot-a', 'account_sharing', 'account_sharing', 'group-a', 'member-a', account['id'])
        if decision['allowed']:
            break
        await asyncio.sleep(0.01)
    assert decision['allowed']
    # Conditions are back, but auto-restore is off: still private, owner is told.
    # The member's message arrives through the resumed WebSocket, as in production. Group
    # state may still refuse the very first event right after RESUMED (in-flight guard), so
    # retry with fresh sequences; the expectation is private delivery, never public.
    service = host.plugins._plugins['account_sharing'].module._instance
    for sequence in (3, 4, 5):
        service.denial_times.clear()
        host.sender.messages.clear()
        second.feed(
            0,
            t='GROUP_AT_MESSAGE_CREATE',
            s=sequence,
            event_id=f'after-resume-{sequence}',
            d={
                'id': f'msg-{sequence}',
                'content': f'账户查询 {account["id"]}',
                'group_openid': 'group-a',
                'author': {'id': 'member-a', 'member_openid': 'member-a'},
            },
        )
        await until(lambda: any(m[0] == 'push' and m[1] == 'private-a' for m in host.sender.messages) or any(m[2] == DENIED for m in host.sender.messages))
        if any(m[0] == 'push' and m[1] == 'private-a' for m in host.sender.messages):
            break
    group = [m[2] for m in host.sender.messages if m[1] == 'group-a']
    private = [m[2] for m in host.sender.messages if m[0] == 'push' and m[1] == 'private-a']
    host.sender.messages.clear()
    assert all('fake-secret' not in g for g in group) and any('fake-secret' in p for p in private)
    operations = (await host.account_api('GET', 'operations'))[1]['data']
    assert any(o['kind'] == 'group_recovered' and o['mode'] == 'private' for o in operations)
    # Mode changes never send credentials.
    assert all('fake-secret' not in str(o) for o in operations)
    assert (await enable(host, account))['actual'] == 'group'
    # Expiry of one member's grant ends group delivery.
    snapshot = await host.access.access_snapshot('bot-a', 'account_sharing')
    source = next(g for g in snapshot['grants'] if g['member'] == 'this-bot')
    await host.account_api('POST', 'access', {'operation': 'expiry', 'group': 'group-a', 'member': 'this-bot', 'source': source['id'], 'expires_at': 1})
    group, private = await ask(host, account)
    assert all('fake-secret' not in g for g in group) and any('fake-secret' in p for p in private)


async def test_auto_restore_choice_and_closing_clears_it(account_host):
    host = account_host
    account = await host.create()
    await full_group(host, account)
    mode = await enable(host, account, auto_restore=True)
    assert mode['actual'] == 'group' and mode['auto_restore']
    snapshot = await host.access.access_snapshot('bot-a', 'account_sharing')
    source = next(g for g in snapshot['grants'] if g['member'] == 'other-bot')
    await host.account_api('POST', 'access', {'operation': 'revoke', 'group': 'group-a', 'member': 'other-bot', 'source': source['id']})
    group, _ = await ask(host, account)
    assert all('fake-secret' not in g for g in group)
    await grant(host, 'other-bot', account['id'])
    # Auto-restore: the next query re-checks and goes back to group delivery.
    group, private = await ask(host, account)
    assert any('fake-secret' in g for g in group) and private == []
    mode = await enable(host, account, enabled=False)
    assert mode['desired'] == 'private' and mode['auto_restore'] is False
    group, private = await ask(host, account)
    assert all('fake-secret' not in g for g in group) and any('fake-secret' in p for p in private)


async def test_owner_report_private_short_group_status_and_unconfirmed_send(account_host):
    host = account_host
    account = await host.create()
    await full_group(host, account, members=('member-a', 'this-bot'))
    body = json.dumps({'group': 'group-a', 'resource': account['id'], 'enabled': True})
    await host.event('账户管理 群发 ' + body, user='owner-a')
    report = json.loads(host.texts('owner-a')[-1])['report']
    rows = {row['member']: row for row in report['members']}
    assert rows['other-bot']['bot'] and rows['other-bot']['actions']['view']['reason'] == 'no_explicit_permission'
    assert rows['this-bot']['self'] and report['complete']
    # Incomplete roster: unknown, not a claimed full failure list.
    host.sender.json_results[('/v2/groups/group-a/members', 'second')] = False, {'code': '11253'}
    await host.state.sync_roster('bot-a', 'group-a')
    report = (await enable(host, account))['report']
    assert not report['complete'] and not report['failures_complete'] and report['reason'] == 'roster_incomplete'
    # A group send whose result is unknown is never re-sent elsewhere.
    roster_platform(host)
    assert (await host.state.sync_roster('bot-a', 'group-a'))['complete']
    await grant(host, 'other-bot', account['id'])
    assert (await enable(host, account))['actual'] == 'group'
    original = host.sender.reply

    async def unknown(event, content=None, **kwargs):
        if content and 'fake-secret' in content:
            return {'code': -1, 'message': '框架网络层请求超时'}
        return await original(event, content, **kwargs)

    host.sender.reply = unknown
    group, private = await ask(host, account)
    assert group == ['群发未确认成功，请稍后重新查询；未自动重发。'] and private == []
    host.sender.reply = original
    # In the group the owner only gets a short status; details stay private.
    await host.event('账户管理 群发 ' + body, user='owner-a', group='group-a')
    assert host.texts('group-a') == ['群发检查已处理，逐人原因请在主人私聊或后台查看']


async def test_change_between_check_and_send_falls_back_to_private(account_host):
    host = account_host
    account = await host.create()
    await full_group(host, account)
    assert (await enable(host, account))['actual'] == 'group'
    service = host.plugins._plugins['account_sharing'].module._instance
    original = service.query_content
    snapshot = await host.access.access_snapshot('bot-a', 'account_sharing')
    source = next(g for g in snapshot['grants'] if g['member'] == 'other-bot')
    revoked = []

    async def revoke_then_read(bot, resource, rule_id):
        # Another Bot's grant is revoked after the whole-group check passed.
        if not revoked:
            revoked.append(
                await host.access.manage_access(
                    'bot-a', 'account_sharing', 'revoke', function='account_sharing', group='group-a', member='other-bot', source=source['id']
                )
            )
        return await original(bot, resource, rule_id)

    service.query_content = revoke_then_read
    group, private = await ask(host, account)
    assert revoked and all('fake-secret' not in g for g in group) and any('fake-secret' in p for p in private)
