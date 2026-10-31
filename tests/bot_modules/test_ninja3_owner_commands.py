"""#143 owner @-mention short commands and the reusable confirmation buttons, through the real app."""

import asyncio
import json
from types import FunctionType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from account_host import account_host as account_host  # noqa: F401
from socket_platform import until
from test_access_control import access_host as access_host  # noqa: F401
from test_ninja3 import SECRET, grant
from test_ninja3 import ninja as ninja  # noqa: F401

from core.base.config import cfg
from core.message.event import Event
from plugins.ninja3.owner_commands import mention_target

MEMBER = '未知成员（…BERA）'
GROUP = '未知群（…up-a）'
COMMANDS = ['授权', '撤销', '拉黑', '解除拉黑', '为什么', '确认在群']


def buttons(host):
    return [b for row in host.sender.keyboards[-1] for b in row]


async def command(host, text, member='MEMBERA', *, user='owner-a', **kwargs):
    return await host.say(text, user=user, mentions=[member] if member else [], **kwargs)


async def acl_state(host, member='MEMBERA'):
    snapshot = await host.access.access_snapshot('bot-a', 'ninja3')
    return (
        [g for g in snapshot['grants'] if g['member'] == member and g['group'] == 'group-a'],
        [d for d in snapshot['denies'] if d['member'] == member and d['group'] == 'group-a'],
    )


async def offer_deny(host, member='MEMBERA'):
    assert await command(host, '拉黑', member) == [f'确认拉黑 {MEMBER}？\n五分钟内有效，仅发起的主人可以确认']
    ok, cancel = buttons(host)
    assert (ok['text'], cancel['text']) == ('确认', '取消')
    assert ok['data'].startswith('n3:ok:') and cancel['data'].startswith('n3:no:')
    return ok['data'], cancel['data']


async def click(host, data, user='owner-a', group='group-a'):
    await host.click(data, user=user, group=group)
    replies = [m for m in host.sender.messages if m[:2] == ('bot-a', group)]
    host.sender.messages[:] = [m for m in host.sender.messages if m not in replies]
    return [m[2] for m in replies]


# ---------- normal paths ----------


async def test_grant_reply_names_member_and_group(ninja):
    host = ninja
    await host.setup()
    assert await grant(host) == [f'已授权 {MEMBER}在 {GROUP} 查询忍3开放账号，长期有效']
    assert await grant(host) == [f'{MEMBER}已有 {GROUP} 的忍3查询授权']
    assert SECRET in str(await host.say('账号 账号A', user='MEMBERA'))


async def test_known_names_are_displayed(ninja):
    host = ninja
    await host.setup()
    names = {'MEMBERA': {'name': 'chair'}, 'group-a': {'group_name': '白子'}}
    log = host.application.get_bot('bot-a').log_service
    log.db_fetch_one = AsyncMock(side_effect=lambda sql, params: names.get(params[0]))
    await grant(host)
    assert await command(host, '撤销') == ['已撤销 chair（…BERA）在 白子 的忍3查询授权']


async def test_revoke_removes_this_group_grant_only(ninja):
    host = ninja
    await host.setup()
    await grant(host)
    await grant(host, member='MEMBERB')
    assert await command(host, '撤销') == [f'已撤销 {MEMBER}在 {GROUP} 的忍3查询授权']
    assert (await acl_state(host)) == ([], [])
    assert len((await acl_state(host, 'MEMBERB'))[0]) == 1
    assert await host.say('账号 账号A', user='MEMBERA') == ['你暂无查询权限，已记录给主人']
    assert await command(host, '撤销') == [f'{MEMBER}在 {GROUP} 没有忍3查询授权，无需撤销']


async def test_deny_confirmed_by_owner_button(ninja):
    host = ninja
    await host.setup()
    await grant(host)
    ok, _ = await offer_deny(host)
    # Nothing happens before the click.
    assert (await acl_state(host))[1] == []
    assert await click(host, ok) == [f'已拉黑 {MEMBER}：在 {GROUP} 不能查询忍3账号']
    assert len((await acl_state(host))[1]) == 1
    assert await host.say('账号 账号A', user='MEMBERA') == ['你暂无查询权限']
    # Already denied: no new confirmation.
    keyboards = len(host.sender.keyboards)
    assert await command(host, '拉黑') == [f'{MEMBER}已在 {GROUP} 的忍3拒绝名单中']
    assert len(host.sender.keyboards) == keyboards


async def test_deny_cancel_button(ninja):
    host = ninja
    await host.setup()
    ok, cancel = await offer_deny(host)
    assert await click(host, cancel) == ['已取消拉黑']
    assert await click(host, ok) == ['已失效']
    assert (await acl_state(host))[1] == []


async def test_allow_removes_this_group_denial(ninja):
    host = ninja
    await host.setup()
    await grant(host)
    await host.access.manage_access('bot-a', 'ninja3', 'deny', function='query', group='group-a', member='MEMBERA')
    assert await command(host, '解除拉黑') == [f'已解除 {MEMBER}在 {GROUP} 的忍3拉黑']
    assert (await acl_state(host))[1] == []
    assert SECRET in str(await host.say('账号 账号A', user='MEMBERA'))
    assert await command(host, '解除拉黑') == [f'{MEMBER}不在 {GROUP} 的忍3拒绝名单中']


async def test_why_explains_in_chinese(ninja):
    host = ninja
    await host.setup()
    (reply,) = await command(host, '为什么')
    assert reply.startswith(f'{MEMBER}在 {GROUP} 不能查询忍3账号：')
    assert '成员在群事实待核验' in reply
    await grant(host)
    assert await command(host, '为什么') == [f'{MEMBER}在 {GROUP} 可以查询忍3账号：主人授权，长期有效']
    await host.access.manage_access('bot-a', 'ninja3', 'deny', function='query', group='group-a', member='MEMBERA')
    (reply,) = await command(host, '为什么')
    assert '在拒绝名单中' in reply and '解除拉黑' in reply
    await host.ninja_api('POST', 'groups', {'group': 'group-a', 'enabled': False})
    (reply,) = await command(host, '为什么')
    assert '本群未开放忍3账号查询' in reply
    # Explaining never writes anything.
    assert len((await acl_state(host))[0]) == 1


async def test_confirm_in_group_records_owner_evidence(ninja):
    host = ninja
    await host.setup()
    assert await command(host, '确认在群') == [f'已确认 {MEMBER}在 {GROUP}']
    fact = await host.state.fact('bot-a', 'group-a', 'MEMBERA')
    assert fact['usable'] and fact['member']['source'] == 'owner_confirmed'
    assert '确认在群 @成员' in fact['member']['evidence'] and 'fake-message' in fact['member']['evidence']
    assert await command(host, '确认在群') == [f'{MEMBER}在 {GROUP} 的在群事实已确认，无需重复']
    # Confirmation alone grants nothing.
    assert (await acl_state(host))[0] == []


# ---------- refusal paths, every command ----------


def refusals(name):
    if name == '授权':
        return '仅主人可以授权', '请发送“授权”并 @ 一位要授权的成员'
    return f'仅主人可以使用“{name} @成员”', f'请发送“{name}”并 @ 一位成员'


@pytest.mark.parametrize('name', COMMANDS)
async def test_refusals_for_non_owner_and_bad_targets(ninja, name):
    host = ninja
    await host.setup()
    not_owner, bad_target = refusals(name)
    assert await command(host, name, user='MEMBERB') == [not_owner]
    assert await host.say(name, user='owner-a', mentions=['MEMBERA', 'MEMBERB']) == [bad_target]
    assert await host.say(name, user='owner-a') == [bad_target]  # only the Bot was mentioned
    assert host.sender.keyboards == []
    assert (await acl_state(host)) == ([], [])
    assert 'MEMBERA' not in host.state.states['bot-a']['groups'].get('group-a', {}).get('members', {})


@pytest.mark.parametrize('name', COMMANDS)
async def test_private_chat_is_refused(ninja, name):
    host = ninja
    await host.event(name, user='owner-a')
    expected = '请在要开放的群内发送：授权 @成员' if name == '授权' else f'请在群内发送：{name} @成员'
    assert host.texts() == [expected]


@pytest.mark.parametrize('name', COMMANDS)
async def test_unready_group_state_is_pending(ninja, name):
    host = ninja
    await host.setup()
    await host.say('忍3', user='owner-a')
    host.lifecycle('GROUP_ADD_ROBOT', sequence=2, timestamp=1000)
    await until(lambda: host.state.states['bot-a']['groups']['group-a'].get('last_native', {}).get('sequence') == 2)
    await until(lambda: not host.state.notifications.busy('bot-a'))
    expected = (
        '授权待处理：群状态恢复未完成，请先发送“群状态 恢复”，完成后重新授权'
        if name == '授权'
        else f'{name}待处理：群状态恢复未完成，请先发送“群状态 恢复”，完成后重新发送'
    )
    assert await command(host, name) == [expected]
    assert host.sender.keyboards == []
    assert (await acl_state(host)) == ([], [])
    assert 'MEMBERA' not in host.state.states['bot-a']['groups']['group-a']['members']


@pytest.mark.parametrize('name', COMMANDS)
async def test_group_state_module_missing_is_pending(ninja, name, monkeypatch):
    host = ninja
    await host.setup()
    monkeypatch.setattr(type(host.access), 'group_state', staticmethod(lambda: None))
    assert await command(host, name) == [f'{name}待处理：群状态模块未启用']
    assert (await acl_state(host)) == ([], [])


@pytest.mark.parametrize('name', COMMANDS)
async def test_emergency_pauses_owner_commands(ninja, name, monkeypatch):
    host = ninja
    await host.setup()
    await host.event('忍3', user='owner-a')  # owner private address for the notice
    host.texts()
    # Real emergency entry would start password runners; the gate is what matters here.
    monkeypatch.setattr(host.ninja().emergency, 'active', lambda bot: True)
    assert await command(host, name) == []
    assert '紧急模式已开启' in host.texts()[-1]
    assert (await acl_state(host)) == ([], [])


# ---------- confirmation buttons ----------


async def test_confirmation_expires_after_five_minutes(ninja):
    host = ninja
    await host.setup()
    now = [1000.0]
    host.ninja().clock = lambda: now[0]
    ok, _ = await offer_deny(host)
    now[0] += 301
    assert await click(host, ok) == ['已失效']
    assert (await acl_state(host))[1] == []


async def test_confirmation_only_for_initiating_owner(ninja, monkeypatch):
    host = ninja
    await host.setup()
    monkeypatch.setitem(cfg.get_bot_config('bot-a'), 'owner_ids', ['owner-a', 'owner-c'])
    ok, _ = await offer_deny(host)
    assert await click(host, ok, user='MEMBERB') == ['已失效']
    assert await click(host, ok, user='owner-c') == ['已失效']
    assert await click(host, ok, user='owner-a', group='group-b') == ['已失效']
    assert (await acl_state(host))[1] == []
    # Clicks by others never consume it; the initiating owner still can.
    assert await click(host, ok) == [f'已拉黑 {MEMBER}：在 {GROUP} 不能查询忍3账号']


async def test_confirmation_is_single_use(ninja):
    host = ninja
    await host.setup()
    ok, cancel = await offer_deny(host)
    assert await click(host, ok) == [f'已拉黑 {MEMBER}：在 {GROUP} 不能查询忍3账号']
    assert await click(host, ok) == ['已失效']
    assert await click(host, cancel) == ['已失效']
    assert len((await acl_state(host))[1]) == 1


@pytest.mark.parametrize('change', ['grant', 'deny', 'left'])
async def test_confirmation_rechecks_target_state(ninja, change):
    host = ninja
    await host.setup()
    ok, _ = await offer_deny(host)
    if change == 'grant':
        await grant(host)
    elif change == 'deny':
        await host.access.manage_access('bot-a', 'ninja3', 'deny', function='query', group='group-a', member='MEMBERA')
    else:
        await host.state.confirm_member('bot-a', 'group-a', 'MEMBERA', evidence='fake verified')
    before = await acl_state(host)
    assert await click(host, ok) == ['目标状态已变化，请重新发送“拉黑 @成员”']
    assert await acl_state(host) == before
    assert await click(host, ok) == ['已失效']


async def test_confirmation_rechecks_expiry_and_global_deny(ninja):
    host = ninja
    await host.setup()
    now = [1000.0]
    host.access.clock = lambda: now[0]
    host.ninja().clock = lambda: now[0]
    await grant(host)
    await host.access.manage_access('bot-a', 'ninja3', 'expiry', function='query', group='group-a', member='MEMBERA', expires_at=1050)
    ok, _ = await offer_deny(host)
    now[0] = 1060
    assert await click(host, ok) == ['目标状态已变化，请重新发送“拉黑 @成员”']

    ok, _ = await offer_deny(host)
    await host.access.manage_access('bot-a', 'ninja3', 'deny', function='query', member='MEMBERA')
    assert await click(host, ok) == ['目标状态已变化，请重新发送“拉黑 @成员”']


async def test_confirmation_conditional_write_rechecks_after_acl_lock(ninja, monkeypatch):
    host = ninja
    await host.setup()
    await grant(host)
    ok, _ = await offer_deny(host)
    original = host.access.manage_access
    raced = False

    async def race(*args, **kwargs):
        nonlocal raced
        if args[2] == 'deny' and not raced:
            raced = True
            await original('bot-a', 'ninja3', 'deny', function='query', member='MEMBERA')
        return await original(*args, **kwargs)

    monkeypatch.setattr(host.access, 'manage_access', race)
    assert await click(host, ok) == ['目标状态已变化，请重新发送“拉黑 @成员”']
    assert (await acl_state(host))[1] == []


@pytest.mark.parametrize('change', ['confirm', 'pending', 'expiry', 'unchanged'])
async def test_confirmation_checks_facts_after_waiting_for_write(ninja, monkeypatch, change):
    host = ninja
    await host.setup()
    await grant(host)
    now = [1000.0]
    host.access.clock = lambda: now[0]
    host.ninja().clock = lambda: now[0]
    if change == 'expiry':
        await host.access.manage_access('bot-a', 'ninja3', 'expiry', function='query', group='group-a', member='MEMBERA', expires_at=1050)
    ok, _ = await offer_deny(host)
    entered, release = asyncio.Event(), asyncio.Event()
    original = host.access.manage_access

    async def wait_before_write(*args, **kwargs):
        if args[2] == 'deny':
            entered.set()
            await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(host.access, 'manage_access', wait_before_write)
    action = asyncio.create_task(click(host, ok))
    await asyncio.wait_for(entered.wait(), timeout=3)
    if change == 'confirm':
        await host.state.confirm_member('bot-a', 'group-a', 'MEMBERA', evidence='new verification')
    elif change == 'pending':
        host.state.pending_writes['bot-a'] = 1
    elif change == 'expiry':
        now[0] = 1060
    release.set()
    try:
        reply = await action
        if change == 'unchanged':
            assert reply == [f'已拉黑 {MEMBER}：在 {GROUP} 不能查询忍3账号']
            assert len((await acl_state(host))[1]) == 1
        else:
            assert reply == ['目标状态已变化，请重新发送“拉黑 @成员”']
            assert (await acl_state(host))[1] == []
    finally:
        if change == 'pending':
            host.state.pending_writes.pop('bot-a', None)


@pytest.mark.parametrize('group,member,scope', [('', 'MEMBERA', {'kind': 'function', 'id': 'query'}), ('', '', {'kind': 'all'}), ('group-a', '', {'kind': 'all'})])
@pytest.mark.parametrize('framework_denied', [False, True])
async def test_applicable_denial_does_not_offer_confirmation(ninja, group, member, scope, framework_denied):
    host = ninja
    await host.setup()
    await host.access.manage_access('bot-a', 'ninja3', 'deny', function='query', group=group, member=member, function_scope=scope)
    if framework_denied:
        cfg.set_bot_setting('bot-a', 'blacklist.user_list', {'MEMBERA': 'test'})
    count = len(host.sender.keyboards)
    assert await command(host, '拉黑') == [f'{MEMBER}已在 {GROUP} 的忍3拒绝名单中']
    assert len(host.sender.keyboards) == count
    assert host.ninja().confirmations.pending == {}


async def test_pending_confirmation_does_not_capture_original_event(ninja):
    await ninja.setup()
    await offer_deny(ninja)
    seen = set()

    def contains_event(value):
        if id(value) in seen:
            return False
        seen.add(id(value))
        if isinstance(value, Event):
            return True
        if isinstance(value, FunctionType):
            return any(contains_event(cell.cell_contents) for cell in value.__closure__ or ())
        if isinstance(value, dict):
            return any(contains_event(item) for item in value.values())
        if isinstance(value, (tuple, list)):
            return any(contains_event(item) for item in value)
        return False

    assert not contains_event(ninja.ninja().confirmations.pending)


async def test_confirmation_lost_on_reload(ninja):
    host = ninja
    await host.setup()
    ok, _ = await offer_deny(host)
    await host.plugins.reload('ninja3')
    assert await click(host, ok) == ['已失效']
    assert (await acl_state(host))[1] == []


async def test_rejected_buttons_cancel_the_request(ninja):
    host = ninja
    await host.setup()
    host.sender.reject_buttons = True
    try:
        assert await command(host, '拉黑') == ['按钮暂不可用，本次拉黑未执行；可在后台授权管理页面操作']
    finally:
        host.sender.reject_buttons = False
    assert host.ninja().confirmations.pending == {}
    assert (await acl_state(host))[1] == []


async def test_missing_mention_flags_are_not_a_member_target():
    assert mention_target(SimpleNamespace(mentions=[{'id': 'BOTA'}])) is None
    assert mention_target(SimpleNamespace(mentions=[{'id': 'MEMBERA', 'is_you': False, 'bot': False}])) == 'MEMBERA'


async def test_why_requires_the_explicit_query_qualification(ninja):
    host = ninja
    await host.setup()
    await host.state.confirm_member('bot-a', 'group-a', 'MEMBERA', evidence='fake verification', member_role='member')
    await host.access.manage_access('bot-a', 'ninja3', 'policy', function='query', policy={'kind': 'group', 'groups': ['group-a']})
    reply = await command(host, '为什么')
    assert '不能查询' in reply[0] and '未授权' in reply[0]


async def test_ordinary_non_at_chat_does_not_consume_owner_command(ninja):
    host = ninja
    await host.setup()
    cfg.set_bot_setting('bot-a', 'non_at_message.enabled', True)
    assert await host.say('撤销 今天的计划', user='MEMBERA', at_bot=False, event_type='GROUP_MESSAGE_CREATE') == []


async def test_menu_buttons_still_work_alongside_confirmations(ninja):
    host = ninja
    await host.setup()
    await grant(host)
    await offer_deny(host)
    replies = await host.say('', user='MEMBERA')
    assert '忍者必须死3 菜单' in replies[0]
    assert await click(host, buttons(host)[0]['data'], user='MEMBERA') != ['已失效']


# ---------- JSON commands unchanged ----------


async def test_json_commands_keep_working(ninja):
    host = ninja
    await host.setup()
    await host.event('忍3', user='owner-a')
    host.texts()
    await host.event(
        '授权管理 ' + json.dumps({'consumer': 'ninja3', 'operation': 'deny', 'function': 'query', 'group': 'group-a', 'member': 'MEMBERA'}), user='owner-a'
    )
    assert json.loads(host.texts()[-1])['version']
    assert len((await acl_state(host))[1]) == 1
    await host.event('群状态 确认 {"group":"group-a","member":"MEMBERB","evidence":"主人已核对"}', user='owner-a')
    assert host.texts()[-1].startswith('成员人工确认\n群 group-a 成员 MEMBERB：可靠（已核验）')
