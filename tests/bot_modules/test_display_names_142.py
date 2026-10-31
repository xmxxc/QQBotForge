"""#142: owner-facing QQ replies and pages show names; identities and permissions keep native IDs."""

import json

import pytest
from account_host import account_host as account_host  # noqa: F401
from test_access_control import access_host as access_host  # noqa: F401
from test_access_control import request
from test_group_roster import roster_platform
from test_ninja3 import grant
from test_ninja3 import ninja as ninja  # noqa: F401

HOSTILE = '<script>alert(1)</script>"\'&'


def stored_names(host, *, users=(), groups=(), bot='bot-a'):
    """The users / groups_users rows the framework would have recorded in this Bot's data.db."""
    users, groups = dict(users), dict(groups)

    async def fetch(sql, params):
        if 'FROM users' in sql:
            return {'name': users[params[0]]} if params[0] in users else None
        return {'group_name': groups[params[0]]} if params[0] in groups else None

    host.application._bot_registry._bots[bot].log_service.db_fetch_one = fetch
    return users, groups


def pushes(host):
    return [m[2] for m in host.sender.messages if m[0] == 'push']


# ---------- ninja3 ----------


async def notified(host, user='MEMBERA'):
    await host.setup()
    await host.event('忍3', user='owner-a')
    host.texts()
    await host.say('账号列表', user=user)
    (content,) = pushes(host)
    return content


async def test_unauthorized_notice_names_group_and_member(ninja):
    stored_names(ninja, users={'MEMBERA': '张三'}, groups={'group-a': '测试群'})
    content = await notified(ninja)
    assert content.startswith('忍者必须死3：群 测试群 的成员 张三（…BERA） 尝试查看账号列表')
    assert 'group-a' not in content and 'MEMBERA' not in content


async def test_unauthorized_notice_degrades_for_unknown_group_and_member(ninja):
    content = await notified(ninja)
    assert content.startswith('忍者必须死3：群 未知群（…up-a） 的成员 未知成员（…BERA） 尝试')


async def test_hostile_names_are_neutralised_in_the_notice(ninja):
    stored_names(ninja, users={'MEMBERA': '**主人** <@owner-a> @全体'}, groups={'group-a': '[CQ:at,qq=all]'})
    content = await notified(ninja)
    assert '＊＊主人＊＊ ＜＠owner－a＞ ＠全体（…BERA）' in content and '［CQ：at，qq＝all］' in content
    assert '<@' not in content and '[CQ:' not in content


async def test_authorize_reply_names_the_mentioned_member(ninja):
    host = ninja
    await host.setup()
    # The mention carries the member's latest name; the framework's write may still be queued.
    assert await grant(host) == ['已授权 显示名（…BERA） 查询本群开放账号，长期有效']
    assert await grant(host) == ['显示名（…BERA） 已有本群授权']


async def test_ninja3_owner_page_api_returns_names_beside_unchanged_data(ninja):
    host = ninja
    stored_names(host, users={'MEMBERA': HOSTILE}, groups={'group-a': '测试群'})
    await host.setup()
    await host.ninja_api('POST', 'groups', {'group': 'group-b', 'enabled': False})
    await host.event('忍3', user='owner-a')
    await host.say('账号列表', user='MEMBERA')
    for path in ('groups', 'accounts', 'notices', 'status', 'operations'):
        status, body = await host.ninja_api('GET', path)
        assert status == 200 and set(body) == {'success', 'data', 'names'}, path
        assert body['names']['groups']['group-a'] == '测试群', path
    groups = (await host.ninja_api('GET', 'groups'))[1]
    assert [g['group'] for g in groups['data']] == ['group-a', 'group-b']
    assert groups['names']['groups']['group-b'] == '未知群（…up-b）'
    notices = (await host.ninja_api('GET', 'notices'))[1]
    assert notices['data'][0]['member'] == 'MEMBERA'
    assert notices['names']['members']['MEMBERA'] == '＜script＞alert（1）＜／script＞＂＇＆（…BERA）'


# ---------- group state ----------


async def owner_reply(host, text):
    host.sender.messages.clear()
    await host.event(text)
    (message,) = host.sender.messages
    return message[2]


async def test_group_state_owner_replies_use_names(group_host):
    host = group_host
    stored_names(host, users={'member-a': '小明', 'this-bot': '机器人'}, groups={'group-a': '测试群'})
    roster_platform(host)
    await host.event('普通聊天', user='member-a', group='group-a')
    overview = await owner_reply(host, '群状态')
    assert '\n测试群：' in overview and 'group-a' not in overview
    refreshed = await owner_reply(host, '群状态 刷新 group-a')
    assert refreshed.startswith('群刷新结果\n群 测试群：') and 'group-a' not in refreshed
    assert await owner_reply(host, '群状态 恢复') == '恢复基线重建：已完成，事实恢复可用'
    roster = await owner_reply(host, '群状态 名册 group-a')
    assert roster.startswith('群名册核验\n群 测试群：名册完整')
    assert '机器人（…-bot）：管理员，本 Bot' in roster and '未知成员（…-bot）：成员，Bot' in roster and '小明（…er-a）：成员' in roster
    verified = await owner_reply(host, '群状态 核验 group-a member-a')
    assert verified.startswith('成员核验结果\n群 测试群 成员 小明（…er-a）：可靠（已核验）')
    confirmed = await owner_reply(host, '群状态 确认 {"group":"group-a","member":"member-z","evidence":"主人已核对"}')
    assert confirmed.startswith('成员人工确认\n群 测试群 成员 未知成员（…er-z）：可靠（已核验）')
    for text in (overview, refreshed, roster, verified, confirmed):
        assert 'member-a' not in text and 'member-z' not in text and 'this-bot' not in text


async def test_group_state_page_api_returns_names_beside_unchanged_data(group_host):
    host = group_host
    stored_names(host, users={'member-a': '小明'}, groups={'group-a': '测试群'})
    roster_platform(host)
    await host.event('普通聊天', user='member-a', group='group-a')
    body = await (await host.api()).json()
    assert set(body) == {'success', 'data', 'names'}
    assert body['data']['groups'][0]['group'] == 'group-a' and body['names']['groups'] == {'group-a': '测试群'}
    await host.event('群状态 恢复')
    body = await (await host.api('POST', 'roster', body={'group': 'group-a'})).json()
    assert {m['member'] for m in body['data']['members']} == {'this-bot', 'other-bot', 'member-a'}
    assert body['names']['members'] == {'member-a': '小明（…er-a）', 'other-bot': '未知成员（…-bot）', 'this-bot': '未知成员（…-bot）'}


# ---------- access control ----------


async def configure(host):
    acl = host.access
    await acl.declare('bot-a', 'reports', owner='plugin:reports', functions={'read': {'kind': 'explicit', 'groups': ['group-a']}})
    await acl.declare_resources('bot-a', 'reports', [{'id': 'report-r', 'groups': ['daily']}])
    await host.state.confirm_member('bot-a', 'group-a', 'member-a', evidence='verified')
    await acl.manage_access('bot-a', 'reports', 'grant', function='read', group='group-a', member='member-a', resources=['report-r'])
    return acl


async def test_access_management_private_reply_adds_chinese_summary_and_keeps_json(access_host):
    host = access_host
    stored_names(host, users={'member-a': '小明'}, groups={'group-a': '测试群'})
    acl = await configure(host)
    host.sender.messages.clear()
    await host.event('授权管理 ' + json.dumps({'operation': 'snapshot', 'consumer': 'reports'}))
    summary, raw = [m[2] for m in host.sender.messages]
    # The JSON reply stays the troubleshooting entry, byte for byte.
    assert raw == json.dumps(await acl.access_snapshot('bot-a', 'reports'), ensure_ascii=False)
    assert summary.startswith('授权管理：查看策略与来源已处理（消费方 reports）')
    assert '群：测试群' in summary and '成员：小明（…er-a）' in summary and '原始 JSON 见下一条' in summary
    assert 'group-a' not in summary and 'member-a' not in summary and 'daily' not in summary
    host.sender.messages.clear()
    await host.event('授权管理 ' + json.dumps({'operation': 'snapshot', 'consumer': 'reports'}), group='group-a')
    assert [m[2] for m in host.sender.messages] == ['已处理，请在主人私聊或后台查看详情']


async def test_access_page_api_returns_names_but_resource_groups_are_not_qq_groups(access_host):
    host = access_host
    stored_names(host, users={'member-a': '小明'})
    await configure(host)
    body = await (await request(host)).json()
    assert set(body) == {'success', 'data', 'names'}
    assert body['data']['resources']['report-r']['groups'] == ['daily']
    assert body['names'] == {'groups': {'group-a': '未知群（…up-a）'}, 'members': {'member-a': '小明（…er-a）'}}


# ---------- permissions never read names ----------

IDENTITIES = [('group-a', 'member-a'), ('group-a', 'member-b'), ('group-a', 'owner-a'), ('group-b', 'member-a'), ('group-a', '小明')]


async def decisions(acl):
    result = []
    for group, member in IDENTITIES:
        decision = await acl.evaluate('bot-a', 'reports', 'read', group, member, 'report-r')
        result.append({key: decision.get(key) for key in ('allowed', 'reason', 'sources', 'valid_until')})
    return result


@pytest.mark.parametrize(
    'users',
    [
        {'member-a': '小明', 'member-b': 'member-a', 'owner-a': '主人'},
        {'member-b': '小明', 'member-a': 'owner-a'},
        {'member-a': HOSTILE, 'member-b': HOSTILE},
    ],
)
async def test_permission_decisions_are_identical_with_or_without_names(access_host, monkeypatch, users):
    host = access_host
    acl = await configure(host)
    before = await decisions(acl)
    stored_names(host, users=users, groups={'group-a': 'group-b', 'group-b': 'group-a'})
    assert await decisions(acl) == before

    import core.display

    async def forbidden(*args):
        raise AssertionError('权限判断不能读取显示名')

    monkeypatch.setattr(core.display, '_read_name', forbidden)
    assert await decisions(acl) == before
    assert [d['allowed'] for d in before] == [True, False, False, False, False]


async def test_ninja3_eligibility_is_identical_with_or_without_names(ninja, monkeypatch):
    host = ninja
    await host.setup()
    await grant(host)
    asked = [('MEMBERA', '账号 账号A'), ('MEMBERB', '账号 账号A'), ('MEMBERA', '账号列表')]

    async def run():
        result = []
        for user, text in asked:
            result.append(await host.say(text, user=user))
        host.sender.messages.clear()
        return result

    before = await run()
    # MEMBERB is "named" like the granted member; a name never becomes an identity.
    stored_names(host, users={'MEMBERB': '显示名', 'MEMBERA': 'MEMBERB'}, groups={'group-a': 'group-b'})
    assert await run() == before
    assert 'fake-login' in str(before[0]) and 'fake-login' not in str(before[1])


@pytest.mark.parametrize('area', ['ninja3', 'access', 'state'])
async def test_display_names_resolve_every_identity_beyond_one_batch(ninja, access_host, group_host, area):
    import sys

    groups = [f'group-{i:04d}' for i in range(205)]
    members = [f'member-{i:04d}' for i in range(205)]
    if area == 'ninja3':
        host, module = ninja, sys.modules['plugins.ninja3.service']
        value = {'groups': groups, 'rows': [{'member': member} for member in members]}
    elif area == 'access':
        host = access_host
        module = sys.modules[type(host.access).__module__.rsplit('.', 1)[0] + '.management']
        value = {'groups': groups, 'members': members}
    else:
        host, module = group_host, sys.modules[type(group_host.state).__module__]
        value = {'groups': [{'group': group} for group in groups], 'members': [{'member': member} for member in members]}
    stored_names(host, users={member: '成员' for member in members}, groups={group: '群名' for group in groups})
    names = await module.display_names('bot-a', value)
    assert names['groups'] == dict.fromkeys(groups, '群名')
    assert names['members'] == {member: '成员（…' + member[-4:] + '）' for member in members}
