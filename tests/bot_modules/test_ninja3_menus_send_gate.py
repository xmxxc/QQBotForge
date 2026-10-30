"""#74 menus through the real MessageSender, before_send pipeline, TokenManager and _request; fake external HTTP client only."""

import asyncio
import re

import pytest
from account_host import account_host as account_host  # noqa: F401
from test_access_control import access_host as access_host  # noqa: F401
from test_ninja3 import SECRET, grant
from test_ninja3 import ninja as ninja  # noqa: F401
from test_ninja3_send_gate import CHANGED, CHANGES, change_during
from test_ninja3_send_gate import wire as wire  # noqa: F401

MENU_CHANGES = [*CHANGES, 'expiry']


def keyboard(payload):
    return [b['action']['data'] for row in payload['keyboard']['content']['rows'] for b in row['buttons']]


async def run(host, wire, change, account, clock, send):
    if change != 'expiry':
        await change_during(host, wire, change, account, send)
        return
    task = asyncio.create_task(send())
    try:
        await asyncio.wait_for(wire.entered.wait(), 5)
        clock[0] += 300  # the shared menu reference expires while Sender prepares
    finally:
        wire.release.set()
    await task


async def directory(host, wire, accounts=1):
    account = await host.setup()
    for i in range(1, accounts):
        await host.setup(name=f'账号{i}')
    await grant(host)
    clock = [1000]
    host.ninja().clock = lambda: clock[0]
    wire.sent.clear()
    await host.say('账号列表', user='MEMBERA')
    (payload,) = wire.sent
    wire.sent.clear()
    return account, clock, payload


def stale_free(wire):
    assert len(wire.sent) == 1 and CHANGED in wire.texts()[0], wire.texts()
    assert SECRET not in str(wire.sent) and 'fake-login' not in str(wire.sent) and 'fake-new' not in str(wire.sent)


@pytest.mark.parametrize('entry', ['click', 'text'])
@pytest.mark.parametrize('change', MENU_CHANGES)
async def test_selected_credentials_recheck_after_sender_preparation(ninja, wire, change, entry):
    host = ninja
    account, clock, payload = await directory(host, wire)
    data = keyboard(payload)[0]
    token = re.search(r'忍3选择 ([0-9a-f]{32})', str(payload))[1]
    wire.marker = '名称：账号A'

    async def send():
        if entry == 'click':
            return await host.click(data, user='MEMBERA', group='group-a')
        return await host.say(f'忍3选择 {token} 1', user='MEMBERA')

    await run(host, wire, change, account, clock, send)
    if change == 'control':
        assert len(wire.sent) == 3 and SECRET in wire.texts()[-1]
    else:
        stale_free(wire)


@pytest.mark.parametrize('change', ['control', 'expiry', 'disable', 'revoke'])
async def test_page_navigation_rechecks_after_sender_preparation(ninja, wire, change):
    host = ninja
    account, clock, payload = await directory(host, wire, accounts=8)
    wire.marker = '账号7'
    await run(host, wire, change, account, clock, lambda: host.click(keyboard(payload)[-1], user='MEMBERA', group='group-a'))
    assert len(wire.sent) == 1
    if change == 'control':
        assert '2/2' in wire.texts()[0] and '账号7' in wire.texts()[0]
    else:
        assert CHANGED in wire.texts()[0] and '账号' not in wire.texts()[0].replace(CHANGED, '')


@pytest.mark.parametrize('change', MENU_CHANGES)
async def test_rejected_buttons_fallback_rechecks_after_sender_preparation(ninja, wire, change):
    host = ninja
    account = await host.setup()
    await grant(host)
    clock = [1000]
    host.ninja().clock = lambda: clock[0]

    # The platform definitively rejects the keyboard; the text fallback is a new send.
    wire.result = lambda payload: (False, {'code': 400, 'message': 'fake keyboard rejected'}) if 'keyboard' in payload else (True, {'id': 'fake-text'})
    wire.sent.clear()
    wire.marker = '按钮暂不可用'
    await run(host, wire, change, account, clock, lambda: host.say('账号列表', user='MEMBERA'))
    rejected, *rest = wire.sent
    assert 'keyboard' in rejected and '1. 账号A' in str(rejected)
    texts = [str(p) for p in rest if 'fake keyboard rejected' not in str(p)]  # api_error template, if configured
    assert len(texts) == 1, texts
    if change == 'control':
        assert '按钮暂不可用' in texts[0] and '1. 账号A' in texts[0] and "'msg_type': 0" in texts[0]
    else:
        assert CHANGED in texts[0] and '账号A' not in texts[0]


@pytest.mark.parametrize('command', ['菜单', '账号列表'])
async def test_keyboard_menu_omits_legacy_text_selection_hint_and_fallback_keeps_it(ninja, wire, command):
    host = ninja
    await host.setup()
    await grant(host)
    wire.sent.clear()
    await host.say(command, user='MEMBERA')
    assert len(wire.sent) == 1
    assert '五分钟内发送“忍3选择' not in wire.texts()[0]

    wire.sent.clear()
    wire.result = lambda payload: (False, {'code': 400, 'message': 'fake keyboard rejected'}) if 'keyboard' in payload else (True, {'id': 'fake-text'})
    await host.say(command, user='MEMBERA')
    assert len(wire.sent) == 2
    assert '五分钟内发送“忍3选择' in wire.texts()[-1]


async def test_management_prompt_rechecks_menu_after_sender_preparation(ninja, wire):
    host = ninja
    await host.setup()
    clock = [1000]
    host.ninja().clock = lambda: clock[0]
    wire.sent.clear()
    await host.say('菜单', user='owner-a')
    manage = keyboard(wire.sent[0])[1]
    wire.sent.clear()
    wire.marker = '尚未交付'
    await run(host, wire, 'expiry', None, clock, lambda: host.click(manage, user='owner-a', group='group-a'))
    assert len(wire.sent) == 1 and CHANGED in wire.texts()[0]


async def test_management_prompt_rechecks_scope_after_sender_preparation(ninja, wire):
    """Closing the plugin group while preparing a placeholder must block it."""
    host = ninja
    await host.setup()
    clock = [1000]
    host.ninja().clock = lambda: clock[0]
    wire.sent.clear()
    await host.say('菜单', user='owner-a')
    manage = keyboard(wire.sent[0])[1]
    wire.sent.clear()
    wire.marker = '尚未交付'
    await run(host, wire, 'close_group', None, clock, lambda: host.click(manage, user='owner-a', group='group-a'))
    assert len(wire.sent) == 1 and CHANGED in wire.texts()[0]
    assert '尚未交付' not in wire.texts()[0]
