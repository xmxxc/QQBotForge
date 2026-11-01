"""Historical owner buttons through real event hooks and durable module data."""
import pytest
from test_owner_menu import access_host as access_host
from test_owner_menu import account_host as account_host  # noqa: F401
from test_owner_menu import button, members, press, private_group  # noqa: F401
from test_owner_menu import changing as changing  # noqa: F401
from test_owner_menu import forbid_game_network as forbid_game_network
from test_owner_menu import menu as menu
from test_owner_menu import ninja as ninja


async def test_history_navigation_survives_reuse_refresh_and_reload(menu):
    h = menu
    await h.say('主人菜单', user='owner-a')
    data = button(h, '群状态')
    await h.click(data, user='owner-a', group='group-a')
    assert '已记录成员' in str(h.texts())
    await h.say('主人菜单', user='owner-a')
    assert await h.modules.reload('owner_menu')
    h.menu().clock = lambda: 10**12
    for _ in range(2):
        await h.click(data, user='owner-a', group='group-a')
        assert '已记录成员' in str(h.texts())


async def test_old_revoke_uses_current_target_and_requires_confirmation(menu):
    h = menu
    await members(h, 1)
    await private_group(h)
    await press(h, '授权', group='')
    await press(h, '成员列表', group='')
    grant, revoke = button(h, '授权'), button(h, '撤销')
    await h.click(grant, user='owner-a')
    h.texts()
    await h.click(revoke, user='owner-a')
    assert '确认撤销' in str(h.texts())
    assert len((await h.access.access_snapshot('bot-a', 'ninja3'))['grants']) == 1
    confirmation = button(h, '确认')
    assert await h.modules.reload('owner_menu')
    await h.click(confirmation, user='owner-a')
    assert '确认撤销' in str(h.texts())
    assert len((await h.access.access_snapshot('bot-a', 'ninja3'))['grants']) == 1
    await press(h, '确认', group='')
    assert not (await h.access.access_snapshot('bot-a', 'ninja3'))['grants']


@pytest.mark.parametrize('fault', ['unknown', 'disabled', 'disabled_list'])
async def test_unavailable_history_has_new_menu_button(menu, fault):
    h = menu
    await h.say('主人菜单', user='owner-a')
    data = button(h, '忍3')
    if fault == 'disabled_list':
        await press(h, '授权')
        data = button(h, '成员列表')
    if fault in {'disabled', 'disabled_list'}:
        assert await h.plugins.unload('ninja3')
    else:
        data = 'om:' + '0' * 32 + ':0'
    await h.click(data, user='owner-a', group='group-a')
    text = str(h.texts())
    assert '不可用' in text or '未启用' in text
    assert '主人菜单' in str(await press(h, '打开新菜单'))


async def test_historical_all_batch_confirmation_keeps_batch_identity(menu, changing):
    import asyncio
    h = menu
    await private_group(h)
    await press(h, '忍3', group='')
    await press(h, '批量改密', group='')
    await press(h, '全部', group='')
    old = button(h, '确认')
    assert await h.modules.reload('owner_menu')
    await h.click(old, user='owner-a')
    assert '确认' in str(h.texts()) and h.center.changes == 0
    await press(h, '确认', group='')
    await asyncio.gather(*list(h.ninja().password_batches.runners.values()))
    await h.finished()
    assert h.center.changes == 1
    for _ in range(2):
        await h.click(old, user='owner-a')
        h.texts()
    assert h.center.changes == 1
    assert len(await h.ninja().password_batches.list('bot-a')) == 1


async def test_group_close_rechecks_current_target_and_confirms(menu):
    h = menu
    await members(h, 0)
    await private_group(h)
    await press(h, '忍3', group='')
    data = button(h, '关闭本群')
    await h.click(data, user='owner-a')
    assert '确认关闭' in str(h.texts())
    assert (await h.ninja().store.get('bot-a', 'groups', 'group-a'))['enabled']
    await press(h, '确认', group='')
    assert not (await h.ninja().store.get('bot-a', 'groups', 'group-a'))['enabled']


@pytest.mark.parametrize('fault', ['non_owner', 'other_owner', 'other_bot', 'other_group'])
async def test_historical_grant_rechecks_actor_bot_and_channel(menu, monkeypatch, fault):
    from core.base.config import cfg
    h = menu
    await members(h, 1)
    await private_group(h)
    await press(h, '授权', group='')
    await press(h, '成员列表', group='')
    data = button(h, '授权')
    assert await h.modules.reload('owner_menu')
    if fault == 'other_owner':
        monkeypatch.setitem(cfg.get_bot_config('bot-a'), 'owner_ids', ['owner-a', 'owner-c'])
    await h.click(data, user={'non_owner': 'member-0', 'other_owner': 'owner-c', 'other_bot': 'owner-b'}.get(fault, 'owner-a'),
                  bot='bot-b' if fault == 'other_bot' else 'bot-a', group='group-b' if fault == 'other_group' else '')
    h.texts()
    assert not (await h.access.access_snapshot('bot-a', 'ninja3'))['grants']
    # A rejected click never consumes the initiating owner's durable action.
    await h.click(data, user='owner-a')
    assert '已授权' in str(h.texts())


async def test_history_grant_reloads_current_acl_and_serializes_repeats(menu):
    import asyncio
    h = menu
    await members(h, 1)
    await private_group(h)
    await press(h, '授权', group='')
    await press(h, '成员列表', group='')
    data = button(h, '授权')
    await asyncio.gather(h.click(data, user='owner-a'), h.click(data, user='owner-a'))
    h.texts()
    original = (await h.access.access_snapshot('bot-a', 'ninja3'))['grants']
    assert len(original) == 1
    await h.access.manage_access('bot-a', 'ninja3', 'revoke', function='query', group='group-a', member='member-0')
    await h.click(data, user='owner-a')
    h.texts()
    current = (await h.access.access_snapshot('bot-a', 'ninja3'))['grants']
    assert len(current) == 1 and current[0]['id'] != original[0]['id']


async def test_historical_account_removed_has_explicit_fallback(menu):
    h = menu
    await members(h, 0)
    account = (await h.ninja_api())[1]['data'][0]
    await private_group(h)
    await press(h, '忍3', group='')
    await press(h, '账号列表', group='')
    data = button(h, '改密')
    assert (await h.ninja_api('DELETE', 'accounts/' + account['id']))[0] == 200
    await h.click(data, user='owner-a')
    assert '参数或状态已变化' in str(h.texts())
    assert '请选择群' in str(await press(h, '打开新菜单', group=''))
