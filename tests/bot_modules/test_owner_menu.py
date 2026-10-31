"""Owner menu through real Event parsing, module hooks and temporary stores."""
import shutil
from pathlib import Path

import pytest
from account_host import account_host as account_host  # noqa: F401
from test_access_control import access_host as access_host  # noqa: F401
from test_ninja3 import ninja as ninja  # noqa: F401
from test_ninja3_password_tasks import changing as changing  # noqa: F401
from test_ninja3_password_tasks import forbid_game_network as forbid_game_network  # noqa: F401

ROOT = Path(__file__).absolute().parents[2]


@pytest.fixture
async def menu(ninja):
    h = ninja
    if (ROOT / 'modules/owner_menu').exists():
        shutil.copytree(ROOT / 'modules/owner_menu', Path(h.modules._dir) / 'owner_menu')
        h.modules.discover_module('owner_menu')
        assert await h.modules.enable('owner_menu', _skip_deps=True, _persist=False)
    h.menu = lambda: h.modules.get('owner_menu')
    return h


def button(h, text):
    return next(b['data'] for row in h.sender.keyboards[-1] for b in row if b['text'] == text)


@pytest.mark.parametrize('entry', ['主人菜单', '主人命令'])
async def test_entry_and_actor_channel(menu, entry):
    h = menu
    assert await h.say(entry, user='MEMBERA') == ['仅主人可用']
    assert '群状态' in str(await h.say(entry, user='owner-a'))
    data = button(h, '群状态')
    await h.click(data, user='MEMBERA', group='group-a')
    assert h.texts() == ['仅主人可用']
    await h.event(entry, user='owner-a')
    assert '请选择群' in str(h.texts())


async def press(h, label, *, group='group-a', user='owner-a'):
    await h.click(button(h, label), user=user, group=group)
    return h.texts()


async def private_group(h):
    await h.event('主人菜单', user='owner-a')
    h.texts()
    await press(h, '未知群（…up-a）', group='')


async def members(h, count):
    await h.setup()
    await h.state.refresh('bot-a', 'group-a')
    for i in range(count):
        await h.state.confirm_member('bot-a', 'group-a', f'member-{i}', evidence='fake verification')
    assert await h.state.rebuild('bot-a')


@pytest.mark.parametrize('count', [0, 1, 4, 5, 8, 9])
async def test_pagination_four_rows_and_five_by_five(menu, count):
    h = menu
    await members(h, count)
    await private_group(h)
    await press(h, '授权', group='')
    texts = await press(h, '成员列表', group='')
    # Owners observed by messages may be recorded too; pagination always derives actual records.
    total = len(await h.menu().known('bot-a', 'group-a'))
    assert f'（{total} 人' in str(texts)
    seen = 0
    while True:
        rows = h.sender.keyboards[-1]
        assert len(rows) <= 5 and all(len(r) <= 5 for r in rows)
        assert len(rows)-1 <= 4
        seen += len(rows)-1
        if not any(b['text'] == '下一页' for r in rows for b in r):
            break
        await press(h, '下一页', group='')
    assert seen == total


@pytest.mark.parametrize('fault', ['other_owner', 'other_group', 'other_bot', 'expired', 'removed', 'forged', 'reload'])
async def test_token_binding_and_no_writes(menu, monkeypatch, fault):
    from core.base.config import cfg
    h = menu
    await h.say('主人菜单', user='owner-a')
    data = button(h, '群状态')
    if fault == 'other_owner':
        monkeypatch.setitem(cfg.get_bot_config('bot-a'), 'owner_ids', ['owner-a', 'owner-c'])
    if fault == 'removed':
        monkeypatch.setitem(cfg.get_bot_config('bot-a'), 'owner_ids', [])
    if fault == 'expired':
        h.menu().clock = lambda: 10**12
    if fault == 'reload':
        assert await h.modules.reload('owner_menu')
    if fault == 'forged':
        data = 'om:' + '0'*32 + ':0'
    await h.click(data, user='owner-c' if fault == 'other_owner' else ('owner-b' if fault == 'other_bot' else 'owner-a'),
                  group='group-b' if fault == 'other_group' else 'group-a', bot='bot-b' if fault == 'other_bot' else 'bot-a')
    assert '已失效' in str(h.texts()) if fault != 'removed' else True
    assert not h.menu().confirmations.pending


async def test_replay_consumed_before_dispatch(menu):
    h = menu
    await h.say('主人菜单', user='owner-a')
    data = button(h, '群状态')
    await h.click(data, user='owner-a', group='group-a')
    h.texts()
    await h.click(data, user='owner-a', group='group-a')
    assert '已失效' in str(h.texts())


@pytest.mark.parametrize('result,expected', [((True, {'id': 'fake'}), '已私聊发送'), ((False, {'code': 400}), '私聊不可用'), ((True, {}), '未确认送达'), (None, '私聊不可用')])
async def test_group_lists_private_delivery_and_safe_fallback(menu, monkeypatch, result, expected):
    h = menu
    await members(h, 5)
    if result is not None:
        await h.event('主人菜单', user='owner-a')
        h.texts()
    captured = []
    async def send(address, content, **kwargs):
        assert kwargs['send_check']()
        captured.append((address, content, kwargs['buttons']))
        return (*result, {})
    monkeypatch.setattr(h.sender, 'send_to_user', send)
    await h.say('主人菜单', user='owner-a')
    await press(h, '授权')
    replies = await press(h, '成员列表')
    assert expected in str(replies)
    assert 'member-' not in str(replies) and '未知成员' not in str(replies)
    if result is not None:
        assert captured and captured[0][0] == 'owner-a'
        assert all(len(row) <= 5 for row in captured[0][2])
        if result[0]:
            h.sender.keyboards.append(captured[0][2])
            if any(b['text'] == '下一页' for row in captured[0][2] for b in row):
                assert '成员列表' in str(await press(h, '下一页', group=''))


async def test_private_business_group_authorization_and_confirmation(menu):
    h = menu
    await members(h, 1)
    await private_group(h)
    await press(h, '授权', group='')
    await press(h, '成员列表', group='')
    assert '已授权' in str(await press(h, '授权', group=''))
    snapshot = await h.access.access_snapshot('bot-a', 'ninja3')
    assert snapshot['grants'][0]['group'] == 'group-a'
    await private_group(h)
    await press(h, '授权', group='')
    await press(h, '成员列表', group='')
    await press(h, '拉黑', group='')
    data = button(h, '确认')
    assert data.startswith('omc:')
    await h.click(data, user='MEMBERA')
    assert h.texts() == ['仅主人可用']
    await h.click(data, user='owner-a')
    assert '已拉黑' in str(h.texts())
    assert len((await h.access.access_snapshot('bot-a', 'ninja3'))['denies']) == 1
    await h.click(data, user='owner-a')
    assert '已失效' in str(h.texts())


async def test_pagination_state_change_resets_first_page(menu):
    h = menu
    await members(h, 5)
    await private_group(h)
    await press(h, '授权', group='')
    await press(h, '成员列表', group='')
    data = button(h, '下一页')
    await h.state.confirm_member('bot-a', 'group-a', 'member-new', evidence='new')
    await h.click(data, user='owner-a')
    assert '第一页' in str(h.texts())


async def test_ninja_owner_button_opens_menu(menu):
    h = menu
    await h.setup()
    await h.say('菜单', user='owner-a')
    data = button(h, '主人菜单')
    await h.click(data, user='MEMBERA', group='group-a')
    assert h.texts() == ['仅主人可用']
    await h.click(data, user='owner-a', group='group-a')
    assert '主人菜单' in str(h.texts())


@pytest.mark.parametrize('count', [0, 2, 101])
async def test_recovery_known_members_only_and_no_grants(menu, count):
    h = menu
    await members(h, count)
    await private_group(h)
    before = await h.menu().known('bot-a', 'group-a')
    await press(h, '群状态', group='')
    reply = await press(h, '全部确认在群并恢复', group='')
    assert f'已确认 {len(before)} 人，恢复已完成' in str(reply)
    after = await h.menu().known('bot-a', 'group-a')
    assert {r['member'] for r in after} == {r['member'] for r in before}
    for record in after:
        assert record['source'] == 'owner_confirmed'
        assert record['member_role'] is None
        assert '主人在主人菜单批量确认' in record['evidence']
    snap = await h.state.snapshot('bot-a')
    assert not snap['complete'] and not snap['groups'][0]['roster']['complete']
    assert not (await h.access.access_snapshot('bot-a', 'ninja3'))['grants']


@pytest.mark.parametrize('failure', ['false', 'exception', 'partial_write', 'range_change'])
async def test_recovery_partial_failure_honest_result(menu, monkeypatch, failure):
    h = menu
    await members(h, 2)
    await private_group(h)
    await press(h, '群状态', group='')
    from unittest.mock import AsyncMock
    original = h.state.confirm_member
    calls = []
    async def confirm(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2 and failure == 'partial_write':
            raise OSError('fake disk failure')
        result = await original(*args, **kwargs)
        if len(calls) == 1 and failure == 'range_change':
            await original('bot-a', 'group-a', 'new-member', evidence='concurrent owner')
        return result
    monkeypatch.setattr(h.state, 'confirm_member', confirm)
    rebuild = AsyncMock(side_effect=RuntimeError('fake') if failure == 'exception' else None, return_value=False)
    monkeypatch.setattr(h.state, 'rebuild', rebuild)
    reply = await press(h, '全部确认在群并恢复', group='')
    assert '恢复未完成' in str(reply) and '恢复已完成' not in str(reply)
    if failure in {'partial_write', 'range_change'}:
        assert not rebuild.called


@pytest.mark.parametrize('outcome', ['sent', 'failed', 'unknown', 'no_address'])
async def test_startup_push_once_and_fallback(menu, monkeypatch, caplog, outcome):
    h = menu
    await members(h, 2)
    if outcome != 'no_address':
        await h.event('忍3', user='owner-a')
        h.texts()
    sent = []
    async def send(address, content, **kwargs):
        sent.append((content, kwargs['buttons']))
        assert kwargs['send_check']()
        return {'sent': (True, {'id': 'fake'}), 'failed': (False, {'code': 400, 'message': 'sensitive body'}), 'unknown': (True, {})}[outcome]
    monkeypatch.setattr(h.sender, 'send_to_user', send)
    await h.modules.reload('group_state')
    await h.menu().push_pending('bot-a')
    await h.menu().push_pending('bot-a')
    assert len(sent) == (0 if outcome == 'no_address' else 1)
    assert 'sensitive body' not in caplog.text
    assert not h.menu().sessions if outcome in {'failed', 'no_address'} else True
    assert '待恢复' in str(await h.say('主人菜单', user='owner-a'))
    if outcome == 'sent':
        h.sender.keyboards.append(sent[0][1])
        assert '恢复已完成' in str(await press(h, '全部确认在群并恢复', group=''))


async def test_account_list_masked_and_updater_existing_entry(menu, monkeypatch):
    from unittest.mock import AsyncMock, Mock
    h = menu
    await members(h, 1)
    updater = Mock()
    updater.check_for_updates = AsyncMock(return_value={'has_update': False})
    updater.update_to_latest = AsyncMock(return_value={'success': False, 'message': 'CI 未通过'})
    updater.get_progress = Mock(return_value={'message': '未更新', 'progress': 0})
    monkeypatch.setattr(h.menu(), 'updater', lambda: updater)
    await private_group(h)
    await press(h, '忍3', group='')
    text = await press(h, '账号列表', group='')
    assert 'fake-login' not in str(text) and 'fake-ninja-secret' not in str(text)
    assert '账号A' in str(text)
    await private_group(h)
    await press(h, '更新', group='')
    assert '最新版本' in str(await press(h, '检查更新', group=''))
    updater.check_for_updates.assert_awaited_once_with()
    await private_group(h)
    await press(h, '更新', group='')
    await press(h, '更新到最新', group='')
    await __import__('asyncio').gather(*list(h.menu().tasks))
    updater.update_to_latest.assert_awaited_once_with()


@pytest.mark.parametrize('seconds,accepted', [(299, True), (300, False)])
async def test_confirmation_ttl_exact_boundary(menu, seconds, accepted):
    h = menu
    await members(h, 1)
    await private_group(h)
    now = [1000.0]
    h.menu().clock = lambda: now[0]
    await press(h, '授权', group='')
    await press(h, '成员列表', group='')
    await press(h, '拉黑', group='')
    data = button(h, '确认')
    now[0] += seconds
    await h.click(data, user='owner-a')
    assert ('已拉黑' if accepted else '已失效') in str(h.texts())


@pytest.mark.parametrize('change', ['acl', 'member', 'owner', 'cancel', 'reject_buttons'])
async def test_confirmation_state_changes_and_cancel(menu, monkeypatch, change):
    from core.base.config import cfg
    h = menu
    await members(h, 1)
    await private_group(h)
    await press(h, '授权', group='')
    await press(h, '成员列表', group='')
    if change == 'reject_buttons':
        h.sender.reject_buttons = True
    reply = await press(h, '拉黑', group='')
    if change == 'reject_buttons':
        assert not h.menu().confirmations.pending
        assert '未执行' in str(reply)
        return
    data = button(h, '取消' if change == 'cancel' else '确认')
    if change == 'acl':
        await h.access.manage_access('bot-a', 'ninja3', 'deny', function='query', member='member-0')
    if change == 'member':
        await h.state.confirm_member('bot-a', 'group-a', 'member-0', evidence='changed')
    if change == 'owner':
        monkeypatch.setitem(cfg.get_bot_config('bot-a'), 'owner_ids', [])
    await h.click(data, user='owner-a')
    text = str(h.texts())
    assert '状态已变化' in text if change in {'acl', 'member'} else ('仅主人可用' in text if change == 'owner' else '已取消' in text)
    assert not [d for d in (await h.access.access_snapshot('bot-a', 'ninja3'))['denies'] if d['group'] == 'group-a']


async def test_private_delivery_send_check_blocks_changed_owner(menu, monkeypatch):
    from core.base.config import cfg
    h = menu
    await members(h, 1)
    await h.event('主人菜单', user='owner-a')
    h.texts()
    async def send(address, content, **kwargs):
        monkeypatch.setitem(cfg.get_bot_config('bot-a'), 'owner_ids', [])
        assert not kwargs['send_check']()
        return False, {'code': 'send_check_failed'}, {}
    monkeypatch.setattr(h.sender, 'send_to_user', send)
    await h.say('主人菜单', user='owner-a')
    await press(h, '授权')
    reply = await press(h, '成员列表')
    assert '未知成员' not in str(reply) and '共 ' in str(reply)


async def test_all_groups_recovery_and_bot_isolation(menu):
    h = menu
    await members(h, 1)
    await h.state.confirm_member('bot-a', 'group-b', 'member-b', evidence='fake')
    await h.state.confirm_member('bot-b', 'group-c', 'member-c', evidence='other bot')
    await h.event('忍3', user='owner-a')
    h.texts()
    sent = []
    async def send(address, content, **kwargs):
        sent.append(kwargs['buttons'])
        return True, {'id': 'fake'}, {}
    h.sender.send_to_user = send
    await h.modules.reload('group_state')
    await h.menu().push_pending('bot-a')
    assert sent
    h.sender.keyboards.append(sent[0])
    assert '恢复已完成' in str(await press(h, '全部确认在群并恢复', group=''))
    assert (await h.state.fact('bot-a', 'group-b', 'member-b'))['member']['source'] == 'owner_confirmed'
    assert '主人在主人菜单批量确认' not in str((await h.state.fact('bot-b', 'group-c', 'member-c'))['member'].get('evidence'))


async def test_missing_dependencies_are_explicit(menu, monkeypatch):
    h = menu
    await h.say('主人菜单', user='owner-a')
    data = button(h, '授权')
    original = h.menu().module
    monkeypatch.setattr(h.menu(), 'module', lambda name: None if name == 'access_control' else original(name))
    await h.click(data, user='owner-a', group='group-a')
    h.texts()
    assert '授权模块未启用' in str(await press(h, '成员列表'))


@pytest.mark.parametrize('all_accounts', [False, True])
async def test_password_batch_buttons_real_engine_fake_platform(menu, changing, all_accounts):
    import asyncio
    h = menu
    await private_group(h)
    await press(h, '忍3', group='')
    await press(h, '批量改密', group='')
    await press(h, '全部' if all_accounts else '选择此账号', group='')
    batches = await h.ninja().password_batches.list('bot-a')
    assert batches and batches[-1]['state'] == 'selected' and h.center.changes == 0
    data = button(h, '确认' if all_accounts else '确认执行')
    await h.click(data, user='owner-a')
    h.texts()
    await asyncio.gather(*list(h.ninja().password_batches.runners.values()))
    await h.finished()
    assert h.center.changes == 1
    await h.click(data, user='owner-a')
    assert '已失效' in str(h.texts()) and h.center.changes == 1
    await private_group(h)
    await press(h, '忍3', group='')
    await press(h, '批量进度与取消', group='')
    assert '批量改密批次' in str(await press(h, '查看进度', group=''))
    assert '取消只停止未开始项' in str(await press(h, '取消未开始项', group=''))


async def test_batch_all_accounts_changed_after_selection(menu, changing):
    h = menu
    await private_group(h)
    await press(h, '忍3', group='')
    await press(h, '批量改密', group='')
    await press(h, '全部', group='')
    data = button(h, '确认')
    await h.setup(name='new-account')
    await h.click(data, user='owner-a')
    assert '状态已变化' in str(h.texts()) and h.center.changes == 0


@pytest.mark.parametrize('path', [
    ['群状态', '刷新'], ['群状态', '恢复基线'], ['群状态', '全部确认在群并恢复'],
    ['授权', '成员列表'], ['授权', '授权列表'], ['授权', '拉黑列表'], ['授权', '未授权查询通知'],
    ['授权', '成员列表', '授权'], ['授权', '成员列表', '撤销'], ['授权', '成员列表', '拉黑'],
    ['忍3', '账号列表'], ['忍3', '批量改密'], ['忍3', '批量进度与取消'],
    ['忍3', '开放本群'], ['忍3', '关闭本群'], ['忍3', '批量改密', '全部'],
    ['更新', '检查更新'], ['更新', '更新到最新'], ['更新', '更新进度'],
])
async def test_every_action_rechecks_actor(menu, monkeypatch, path):
    from unittest.mock import Mock

    from core.base.config import cfg
    h = menu
    await members(h, 1)
    monkeypatch.setattr(h.menu(), 'updater', lambda: Mock())
    await private_group(h)
    for name in path[:-1]:
        await press(h, name, group='')
    data = button(h, path[-1])
    acl_before = await h.access.access_snapshot('bot-a', 'ninja3')
    groups_before = await h.ninja().manage_group('bot-a', 'list')
    await h.click(data, user='MEMBERA')
    assert h.texts() == ['仅主人可用']
    monkeypatch.setitem(cfg.get_bot_config('bot-a'), 'owner_ids', [])
    await h.click(data, user='owner-a')
    assert h.texts() == ['仅主人可用']
    assert await h.access.access_snapshot('bot-a', 'ninja3') == acl_before
    assert await h.ninja().manage_group('bot-a', 'list') == groups_before
    assert not await h.ninja().password_batches.list('bot-a')
    assert not await h.ninja().store.all('bot-a', 'notices')


async def test_private_confirmation_atomic_acl_boundary(menu, monkeypatch):
    h = menu
    await members(h, 1)
    await private_group(h)
    await press(h, '授权', group='')
    await press(h, '成员列表', group='')
    await press(h, '拉黑', group='')
    original = h.access.manage_access
    async def race(*args, **kwargs):
        if args[2] == 'deny':
            await original('bot-a', 'ninja3', 'deny', function='query', member='member-0')
        return await original(*args, **kwargs)
    monkeypatch.setattr(h.access, 'manage_access', race)
    assert '状态已变化' in str(await press(h, '确认', group=''))
    assert not [d for d in (await h.access.access_snapshot('bot-a', 'ninja3'))['denies'] if d['group'] == 'group-a']


async def test_deny_confirmation_rechecks_existing_emergency_gate(menu, monkeypatch):
    h = menu
    await members(h, 1)
    await private_group(h)
    await press(h, '授权', group='')
    await press(h, '成员列表', group='')
    await press(h, '拉黑', group='')
    monkeypatch.setattr(h.ninja().emergency, 'active', lambda bot: True)
    reply = await press(h, '确认', group='')
    assert '状态已变化' in str(reply)
    assert not (await h.access.access_snapshot('bot-a', 'ninja3'))['denies']


async def test_empty_new_consumer_lists_and_button_return(menu):
    h = menu
    await h.say('主人菜单', user='owner-a')
    await private_group(h)
    await press(h, '授权', group='')
    reply = await press(h, '成员列表', group='')
    assert '成员列表' in str(reply)
    await press(h, '返回', group='')
    await press(h, '返回', group='')
    await press(h, '忍3', group='')
    await press(h, '开放本群', group='')
    assert '主人菜单' in str(await press(h, '返回', group=''))


@pytest.mark.parametrize('change', ['reload', 'disable', 'binding'])
async def test_pending_deny_dependency_revalidation(menu, monkeypatch, change):
    h = menu
    await members(h, 1)
    await private_group(h)
    await press(h, '授权', group='')
    await press(h, '成员列表', group='')
    await press(h, '拉黑', group='')
    data = button(h, '确认')
    if change == 'reload':
        await h.plugins.reload('ninja3')
    elif change == 'disable':
        await h.plugins.unload('ninja3')
    else:
        monkeypatch.setattr(h.ninja(), 'plugin_available', lambda bot: False)
    await h.click(data, user='owner-a')
    assert '已拉黑' not in str(h.texts())
    assert not (await h.access.access_snapshot('bot-a', 'ninja3'))['denies']


async def test_confirmation_cancel_has_clickable_return(menu):
    h = menu
    await members(h, 1)
    await private_group(h)
    await press(h, '授权', group='')
    await press(h, '成员列表', group='')
    await press(h, '拉黑', group='')
    assert '已取消' in str(await press(h, '取消', group=''))
    assert '主人菜单' in str(await press(h, '返回', group=''))


async def test_deny_state_becomes_unavailable_during_offer(menu, monkeypatch):
    h = menu
    await members(h, 1)
    await private_group(h)
    await press(h, '授权', group='')
    await press(h, '成员列表', group='')
    original = h.access.evaluate
    async def evaluate(*args, **kwargs):
        decision = await original(*args, **kwargs)
        monkeypatch.setattr(h.ninja().emergency, 'active', lambda bot: True)
        return decision
    monkeypatch.setattr(h.access, 'evaluate', evaluate)
    assert '状态已变化' in str(await press(h, '拉黑', group=''))
    assert not h.menu().confirmations.pending
    assert not (await h.access.access_snapshot('bot-a', 'ninja3'))['denies']
