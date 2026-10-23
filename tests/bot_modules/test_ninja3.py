"""忍者必须死3 #72: owner grant by @mention and public group queries, through the real app."""

import asyncio
import logging
import shutil
import sys
from pathlib import Path

import pytest
from account_host import account_host as account_host  # noqa: F401
from socket_platform import until
from test_access_control import access_host as access_host  # noqa: F401

ROOT = Path(__file__).absolute().parents[2]
SECRET = 'fake-ninja-secret'


@pytest.fixture
async def ninja(account_host):
    """Old account plugin and the new plugin loaded side by side by the real plugin manager."""
    host = account_host
    shutil.copytree(ROOT / 'plugins/ninja3', Path(host.plugins._dir) / 'ninja3', ignore=shutil.ignore_patterns('data', '__pycache__'))
    await host.plugins.reload('ninja3')
    assert {'account_sharing', 'ninja3'} <= set(host.plugins.plugins)
    host.ninja = lambda: sys.modules['plugins.ninja3']._instance

    async def api(method='GET', path='accounts', body=None, *, bot='bot-a', authenticated=True):
        response = await host.http.request(
            method, '/api/ext/ninja3/' + path, params={'bot': bot}, json=body, headers={'Cookie': 'elaina_session=group-test'} if authenticated else {}
        )
        return response.status, await response.json()

    async def say(text, *, user, group='group-a', bot='bot-a', mentions=(), at_bot=True):
        from core.base import tasks
        from core.message.event import Event

        payload = [{'id': 'BOT' + bot[-1].upper(), 'is_you': True, 'bot': True}] if at_bot else []
        payload += [{'id': m, 'member_openid': m, 'is_you': False, 'bot': False, 'username': '显示名'} for m in mentions]
        content = ('<@BOT> ' if at_bot else '') + text + ''.join(f' <@{m}>' for m in mentions)
        event = Event.from_websocket(
            bot,
            {
                't': 'GROUP_AT_MESSAGE_CREATE',
                'd': {'id': 'fake-message', 'content': content, 'group_openid': group, 'author': {'id': user, 'member_openid': user}, 'mentions': payload},
            },
        )
        before = set(tasks._background_tasks)
        host.application._prepare_event(event)
        await host.application._on_event(event)
        for _ in range(20):
            started = set(tasks._background_tasks) - before
            if not started:
                break
            await asyncio.gather(*started, return_exceptions=True)
            before |= started
        # Group replies only; private pushes stay recorded for the test to inspect.
        replies = [m for m in host.sender.messages if m[:2] == (bot, group)]
        host.sender.messages[:] = [m for m in host.sender.messages if m not in replies]
        return [m[2] for m in replies]

    async def setup(bot='bot-a', group='group-a', name='账号A', password=SECRET, **fields):
        assert (await api('POST', 'groups', {'group': group, 'enabled': True}, bot=bot))[0] == 200
        status, data = await api(
            'POST', 'accounts', {'name': name, 'login': 'fake-login', 'password': password, 'enabled': True, 'open_groups': [group], **fields}, bot=bot
        )
        assert status == 201, data
        return data['data']

    host.ninja_api, host.say, host.setup = api, say, setup
    return host


async def grant(host, member='MEMBERA', **kwargs):
    return await host.say('授权', user='owner-a' if kwargs.get('bot', 'bot-a') == 'bot-a' else 'owner-b', mentions=[member], **kwargs)


def grants(snapshot, member='MEMBERA'):
    return [g for g in snapshot['grants'] if g['member'] == member]


async def test_backend_crud_defaults_closed_and_isolated_per_bot(ninja):
    host = ninja
    assert (await host.ninja_api(authenticated=False))[0] == 401
    status, group = await host.ninja_api('POST', 'groups', {'group': 'group-a'})
    assert status == 200 and group['data'] == {'group': 'group-a', 'name': '', 'enabled': False}
    status, account = await host.ninja_api('POST', 'accounts', {'name': '账号A', 'login': 'fake-login', 'password': SECRET})
    assert status == 201 and account['data']['enabled'] is False and account['data']['open_groups'] == []
    assert SECRET not in str(account) and SECRET not in str(await host.ninja_api())
    assert (await host.ninja_api('POST', 'accounts', {'name': '账号A', 'login': 'x', 'password': 'x'}))[0] == 409
    assert (await host.ninja_api('PATCH', 'accounts/' + account['data']['id'], {'open_groups': ['unknown-group']}))[0] == 404
    assert (await host.ninja_api('PATCH', 'accounts/' + account['data']['id'], {'groups': ['group-a']}))[0] == 422
    assert (await host.ninja_api(bot='bot-b'))[1]['data'] == [] and (await host.ninja_api('GET', 'groups', bot='bot-b'))[1]['data'] == []
    snapshot = await host.access.access_snapshot('bot-a', 'ninja3')
    # Resource category groups stay empty; QQ group openness lives in the policy scope only.
    assert snapshot['resources'][account['data']['id']] == {'id': account['data']['id'], 'groups': [], 'enabled': False}
    assert snapshot['functions']['query'] == {'kind': 'explicit', 'groups': [], 'private': False}
    await host.ninja_api('POST', 'groups', {'group': 'group-a', 'enabled': True})
    assert (await host.access.access_snapshot('bot-a', 'ninja3'))['functions']['query']['groups'] == ['group-a']


async def test_owner_mention_grant_covers_future_accounts_and_member_queries_publicly(ninja, caplog):
    caplog.set_level(logging.INFO, logger='ElainaBot')
    host = ninja
    await host.setup(description='', url='')
    # The member was never verified, rostered or bound: the owner's command is the in-group evidence.
    assert await grant(host) == ['已授权该成员查询本群开放账号，长期有效']
    fact = await host.state.fact('bot-a', 'group-a', 'MEMBERA')
    assert fact['usable'] and fact['member']['source'] == 'owner_confirmed' and '授权 @成员' in fact['member']['evidence']
    snapshot = await host.access.access_snapshot('bot-a', 'ninja3')
    # Open QQ groups never become resource category groups (no group-scoped grant can widen through them).
    assert [r['groups'] for r in snapshot['resources'].values()] == [[]]
    (source,) = grants(snapshot)
    assert (source['group'], source['mode'], source['resource_scope'], source['expires_at']) == ('group-a', 'continuous', {'kind': 'all'}, None)
    assert await host.say('账号列表', user='MEMBERA') == ['本群开放账号：\n1. 账号A\n发送“账号 名称”获取']
    # Public group reply without trailing empty description/url lines; nothing private.
    assert await host.say('账号 账号A', user='MEMBERA') == [f'名称：账号A\n账号：fake-login\n密码：{SECRET}']
    assert not [m for m in host.sender.messages if m[0] in {'push', 'group-push'}]
    assert not host.state.states['bot-a']['groups']['group-a']['roster']['complete']
    # A later account opened in this group is covered by the same grant; repeat grants add nothing.
    await host.setup(name='账号B', password='fake-b', description='说明B')
    assert await host.say('账号 账号B', user='MEMBERA', at_bot=False) == ['名称：账号B\n账号：fake-login\n密码：fake-b\n说明：说明B']
    assert await grant(host) == ['该成员已有本群授权']
    assert len(grants(await host.access.access_snapshot('bot-a', 'ninja3'))) == 1
    assert '账号查询 group=group-a member=MEMBERA result=sent' in caplog.text and SECRET not in caplog.text and 'fake-b' not in caplog.text


async def test_grant_requires_owner_native_single_mention_and_never_crosses_bot_or_group(ninja):
    host = ninja
    await host.create(name='旧插件账户')  # registers the old consumer
    await host.setup()
    await host.setup(bot='bot-b')
    await host.ninja_api('POST', 'groups', {'group': 'group-b', 'enabled': True})
    account = (await host.ninja_api())[1]['data'][0]
    await host.ninja_api('PATCH', 'accounts/' + account['id'], {'open_groups': ['group-a', 'group-b']})
    assert await host.say('授权', user='MEMBERB', mentions=['MEMBERA']) == ['仅主人可以授权']
    # A typed display name, several targets, or only the Bot mention are refused.
    assert await host.say('授权 @小号', user='owner-a') == ['请发送“授权”并 @ 一位要授权的成员']
    assert await host.say('授权', user='owner-a', mentions=['MEMBERA', 'MEMBERB']) == ['请发送“授权”并 @ 一位要授权的成员']
    assert await host.say('授权', user='owner-a') == ['请发送“授权”并 @ 一位要授权的成员']
    assert not (await host.access.access_snapshot('bot-a', 'ninja3'))['grants']
    assert await grant(host)
    assert await host.say('账号 账号A', user='MEMBERA', group='group-b') == ['你暂无查询权限，已记录给主人']
    assert await host.say('账号 账号A', user='MEMBERA', bot='bot-b') == ['你暂无查询权限，已记录给主人']
    assert not (await host.access.access_snapshot('bot-b', 'ninja3'))['grants']
    # The old account consumer gains nothing from the new plugin's grant.
    assert not (await host.access.access_snapshot('bot-a', 'account_sharing'))['grants']
    assert await host.say('账号 账号A', user='MEMBERA') == [f'名称：账号A\n账号：fake-login\n密码：{SECRET}']
    # Group admission set from the authorization page never replaces the personal grant.
    await host.state.confirm_member('bot-a', 'group-a', 'MEMBERB', evidence='fake verified', member_role='member')
    await host.access.manage_access('bot-a', 'ninja3', 'policy', function='query', policy={'kind': 'group', 'groups': ['group-a', 'group-b']})
    assert (await host.access.evaluate('bot-a', 'ninja3', 'query', 'group-a', 'MEMBERB'))['allowed']
    assert await host.say('账号 账号A', user='MEMBERB') == ['你暂无查询权限，已记录给主人']
    assert await host.say('账号列表', user='MEMBERB') == ['你暂无查询权限，已记录给主人']


async def test_unready_recovery_returns_pending_without_confirming_or_granting(ninja):
    host = ninja
    await host.setup()
    await host.say('忍3', user='owner-a')  # the group becomes known to group state
    host.lifecycle('GROUP_ADD_ROBOT', sequence=2, timestamp=1000)
    await until(lambda: host.state.states['bot-a']['groups']['group-a'].get('last_native', {}).get('sequence') == 2)
    await until(lambda: not host.state.notifications.busy('bot-a'))
    assert await grant(host) == ['授权待处理：群状态恢复未完成，请先发送“群状态 恢复”，完成后重新授权']
    assert 'MEMBERA' not in host.state.states['bot-a']['groups']['group-a']['members']
    assert not (await host.access.access_snapshot('bot-a', 'ninja3'))['grants']


async def test_unauthorized_attempts_notify_owner_once_per_ten_minutes_and_keep_backend_todo(ninja):
    host = ninja
    await host.setup()
    await host.event('忍3', user='owner-a')  # owner's private chat becomes the notice address
    assert '已记录本私聊为主人通知地址' in host.texts()[-1]
    now = [1000.0]
    host.ninja().clock = lambda: now[0]
    store, read = host.ninja().store, []
    real_all = store.all
    store.all = lambda bot, collection: read.append(collection) or real_all(bot, collection)
    assert await host.say('账号列表', user='MEMBERA') == ['你暂无查询权限，已通知主人']
    assert 'accounts' not in read  # an ungranted caller never loads account rows
    store.all = real_all
    pushes = [m for m in host.sender.messages if m[0] == 'push']
    assert len(pushes) == 1 and pushes[0][1] == 'owner-a' and 'MEMBERA' in pushes[0][2] and '授权 @该成员' in pushes[0][2]
    host.sender.messages.clear()
    now[0] += 599
    assert await host.say('账号 账号A', user='MEMBERA') == ['你暂无查询权限，已记录给主人']
    assert not [m for m in host.sender.messages if m[0] == 'push']
    (notice,) = (await host.ninja_api('GET', 'notices'))[1]['data']
    assert (notice['count'], notice['action'], notice['notification']) == (2, 'query', 'sent')
    await host.say('账号列表', user='MEMBERB')
    now[0] += 2
    await host.say('账号列表', user='MEMBERA')
    assert [m[2].split('的成员 ')[1][:7] for m in host.sender.messages if m[0] == 'push'] == ['MEMBERB', 'MEMBERA']
    assert all(SECRET not in str(m) for m in host.sender.messages)
    # Unreachable owner (bot-b never chatted privately): the notice stays as an authenticated todo.
    host.sender.messages.clear()
    await host.setup(bot='bot-b')
    assert await host.say('账号列表', user='MEMBERA', bot='bot-b') == ['你暂无查询权限，已记录给主人']
    (pending,) = (await host.ninja_api('GET', 'notices', bot='bot-b'))[1]['data']
    assert pending['notification'] == 'unavailable' and not [m for m in host.sender.messages if m[0] == 'push']
    await host.event('忍3', user='owner-b', bot='bot-b')
    assert '未送达的未授权通知 1 条' in host.texts()[-1]
    host.sender.push_result = (False, {'code': 'fake-push-rejected'})
    status, failed = await host.ninja_api('POST', 'notices', {'operation': 'retry', 'id': pending['id']}, bot='bot-b')
    assert status == 200 and failed['data']['notification'] == 'failed'
    host.sender.push_result = (True, {'id': 'fake-push'})
    status, retried = await host.ninja_api('POST', 'notices', {'operation': 'retry', 'id': pending['id']}, bot='bot-b')
    assert status == 200 and retried['data']['notification'] == 'sent'
    assert [m[1] for m in host.sender.messages if m[0] == 'push'] == ['owner-b']


async def test_denial_wins_and_disable_close_rescope_delete_take_effect(ninja):
    host = ninja
    account = await host.setup()
    await grant(host)
    acl = host.access
    await acl.manage_access('bot-a', 'ninja3', 'deny', function='query', group='group-a', member='MEMBERA')
    host.sender.messages.clear()
    assert await host.say('账号 账号A', user='MEMBERA') == ['你暂无查询权限']
    assert not [m for m in host.sender.messages if m[0] == 'push'] and not (await host.ninja_api('GET', 'notices'))[1]['data']
    # A repeated owner grant cannot lift the denial.
    assert await grant(host) == ['该成员已有本群授权\n该成员在拒绝名单中，拒绝优先，当前仍不能查询']
    await acl.manage_access('bot-a', 'ninja3', 'allow', function='query', group='group-a', member='MEMBERA')
    assert SECRET in (await host.say('账号 账号A', user='MEMBERA'))[0]
    await host.ninja_api('PATCH', 'accounts/' + account['id'], {'enabled': False})
    assert await host.say('账号 账号A', user='MEMBERA') == ['本群没有开放名为“账号A”的账号，发送“账号列表”查看']
    assert await host.say('账号列表', user='MEMBERA') == ['本群暂无开放账号']
    await host.ninja_api('PATCH', 'accounts/' + account['id'], {'enabled': True, 'open_groups': []})
    assert SECRET not in str(await host.say('账号 账号A', user='MEMBERA'))
    await host.ninja_api('PATCH', 'accounts/' + account['id'], {'open_groups': ['group-a']})
    await host.ninja_api('POST', 'groups', {'group': 'group-a', 'enabled': False})
    assert await host.say('账号 账号A', user='MEMBERA') == ['本群未开放账号查询']
    await host.ninja_api('POST', 'groups', {'group': 'group-a', 'enabled': True})
    assert SECRET in (await host.say('账号 账号A', user='MEMBERA'))[0]
    await host.ninja_api('DELETE', 'accounts/' + account['id'])
    assert SECRET not in str(await host.say('账号 账号A', user='MEMBERA'))
    assert account['id'] not in (await acl.access_snapshot('bot-a', 'ninja3'))['resources']
    # Leaving the group retires the source; a rejoin needs a new owner grant.
    await host.setup(name='账号C')
    host.lifecycle('GROUP_MEMBER_REMOVE', member='MEMBERA', sequence=2, timestamp=1000)
    await until(lambda: not grants(host.access.scope_states[('bot-a', 'ninja3')]))
    await until(lambda: host.state.recovery.get_status('bot-a').ready and not host.state.notifications.busy('bot-a'))
    assert await host.say('账号 账号C', user='MEMBERA') == ['你暂无查询权限，已记录给主人']


@pytest.mark.parametrize('change', ['password', 'disable', 'close_group', 'revoke', 'deny', 'leave', 'reload_access', 'reload_plugin'])
async def test_changes_while_query_prepares_never_send_old_content(ninja, change):
    host = ninja
    account = await host.setup()
    await grant(host)
    acl = host.access
    real = type(acl).evaluate

    async def during(self, bot, consumer, function, group, member, resource='', **kwargs):
        decision = await real(self, bot, consumer, function, group, member, resource, **kwargs)
        if consumer == 'ninja3' and resource:  # the last preparatory await before the final gate
            if change == 'password':
                await host.ninja_api('PATCH', 'accounts/' + account['id'], {'password': 'fake-new'})
            elif change == 'disable':
                await host.ninja_api('PATCH', 'accounts/' + account['id'], {'enabled': False})
            elif change == 'close_group':
                await host.ninja_api('POST', 'groups', {'group': 'group-a', 'enabled': False})
            elif change == 'revoke':
                await self.manage_access(bot, 'ninja3', 'revoke', function='query', group=group, member=member)
            elif change == 'deny':
                await self.manage_access(bot, 'ninja3', 'deny', function='query', group=group, member=member)
            elif change == 'leave':
                host.lifecycle('GROUP_MEMBER_REMOVE', member=member, sequence=2, timestamp=1000)
                await until(lambda: not self.decision_current(bot, decision))
            elif change == 'reload_access':
                await host.modules.disable('access_control', _persist=False)
                await host.modules.enable('access_control', _skip_deps=True, _persist=False)
            else:
                await host.plugins.reload('ninja3')
        return decision

    type(acl).evaluate = during
    try:
        replies = await host.say('账号 账号A', user='MEMBERA')
    finally:
        type(acl).evaluate = real
    assert replies == ['账号或授权刚刚变化，请重新发送查询'] and 'fake-new' not in str(host.sender.messages)


async def test_old_account_plugin_keeps_its_own_data_and_commands(ninja):
    host = ninja
    old = await host.create(name='旧账户')
    await host.setup(name='旧账户', password='fake-new-plugin')
    await grant(host)
    # The new commands never reach the old plugin, and the old commands never reach the new one.
    assert await host.say('账号列表', user='MEMBERA') == ['本群开放账号：\n1. 旧账户\n发送“账号 名称”获取']
    await host.event('账户管理 列表', user='owner-a')
    assert host.texts() == [f'{old["id"]} 旧账户 启用']
    status, accounts = await host.account_api()
    assert status == 200 and [a['password'] for a in accounts['data']] == ['fake-secret']
    assert (await host.access.access_snapshot('bot-a', 'account_sharing'))['functions']['account_sharing']['groups'] == []
    await host.say('账户列表', user='MEMBERA')
    assert all('fake-secret' not in str(m) for m in host.sender.messages)
