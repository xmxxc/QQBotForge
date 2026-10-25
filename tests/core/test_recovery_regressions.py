"""恢复信任缺口的真实协议与模块生命周期回归。"""

import asyncio
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from core.application import Application
from core.module.manager import ModuleManager
from core.network.websocket import WSClient
from tests.core import test_resume_processing as recovery_helpers
from tests.core.test_connection_status import until
from tests.core.test_resume_processing import login, resume

wired = recovery_helpers.wired


@pytest.mark.parametrize('event_type', ['GUILD_UPDATE', 'GROUP_MEMBER_ADD'])
@pytest.mark.parametrize('retirement', ['reload', 'timeout_reload', 'cancel_reload', 'disable_enable'])
async def test_reload_waits_for_old_writer_and_requires_fresh_rebuild(wired, tmp_path, monkeypatch, event_type, retirement):
    ctx, platform, client, ordinary = wired
    state = SimpleNamespace(
        value='initial', calls=0, entered=asyncio.Event(), cancelled=asyncio.Event(),
        release=asyncio.Event(), old_done=asyncio.Event(), event_type=event_type,
        timeout=0.02 if retirement == 'timeout_reload' else 5.0,
    )
    monkeypatch.setitem(sys.modules, 'recovery_shared_state', state)
    module_root = tmp_path / 'modules'
    consumer = module_root / 'consumer'
    consumer.mkdir(parents=True)
    (consumer / 'main.py').write_text('''import asyncio
import recovery_shared_state as state
async def consume(delivery):
    state.calls += 1
    state.entered.set()
    while not state.release.is_set():
        try:
            await state.release.wait()
        except asyncio.CancelledError:
            state.cancelled.set()
    state.value = 'late write from old generation'
    assert delivery.acknowledge() is False
    state.old_done.set()
async def companion(delivery):
    delivery.acknowledge()
def setup(ctx):
    return (ctx.subscribe_recovery_events(consume, event_types={state.event_type}, max_pending=1, timeout=state.timeout),
            ctx.subscribe_recovery_events(companion, event_types={'GUILD_DELETE'}))
''')
    manager = ModuleManager(str(module_root), ctx._hooks)
    manager.discover()
    assert await manager.enable('consumer', _skip_deps=True, _persist=False)
    first = await login(ctx, platform)
    old_sub, peer = manager.get('consumer')
    healthy_calls = []

    async def healthy(delivery):
        healthy_calls.append(delivery)
        delivery.acknowledge()

    unrelated = ctx.subscribe_recovery_events(healthy, event_types={event_type})
    for sub in (old_sub, peer, unrelated):
        assert sub.establish(sub.get_status('sample-bot'))
    try:
        first.feed(0, t=event_type, s=2, d={'id': 'guild', 'group_openid': 'g'})
        await until(state.entered.is_set)
        if retirement == 'timeout_reload':
            await until(state.cancelled.is_set)
            assert old_sub.get_status('sample-bot').reason == 'timeout'
        if retirement == 'cancel_reload':
            old_sub.cancel()
        if retirement == 'disable_enable':
            assert await manager.disable('consumer', _persist=False)
            assert await manager.enable('consumer', _skip_deps=True, _persist=False)
        else:
            assert await manager.reload('consumer')
        await until(state.cancelled.is_set)
        new_sub, peer = manager.get('consumer')
        state.value = 'premature rebuild'
        checkpoint = new_sub.get_status('sample-bot')
        assert not new_sub.establish(checkpoint)
        assert not peer.establish(peer.get_status('sample-bot'))
        assert not new_sub.get_status('sample-bot').ready
        # 重复重载及新事件不能绕过旧回调容量，产生更多旧代际写入者。
        for sequence in (3, 4):
            assert await manager.reload('consumer')
            first.feed(0, t=event_type, s=sequence, d={'id': 'guild', 'group_openid': 'other'})
            await until(lambda sequence=sequence: len(ordinary) >= sequence - 1)
        new_sub, peer = manager.get('consumer')
        checkpoint = new_sub.get_status('sample-bot')
        assert not new_sub.establish(checkpoint)
        assert state.calls == 1
        await until(lambda: len(healthy_calls) == 3 and unrelated.get_status('sample-bot').ready)
        assert unrelated.get_status('sample-bot').ready
        state.release.set()
        await until(state.old_done.is_set)
        # give task completion callbacks a turn, without accepting the old credential
        await asyncio.sleep(0)
        assert not new_sub.establish(checkpoint)
        assert state.value == 'late write from old generation'
        fresh = new_sub.get_status('sample-bot')
        state.value = 'authoritative rebuild after writer exit'
        assert new_sub.establish(fresh)
        assert new_sub.get_status('sample-bot').ready
        assert peer.establish(peer.get_status('sample-bot'))
    finally:
        state.release.set()
        await manager.shutdown()


@asynccontextmanager
async def other_bot(ctx, platform):
    app = Application()
    app._hook_manager = ctx._hooks
    app._bot_registry = SimpleNamespace(bots={'other-bot': SimpleNamespace()})
    app._bot_registry.get = app._bot_registry.bots.get

    async def dispatch(event):
        pass

    client = WSClient(
        'other-bot', platform, dispatch, ack_interaction=dispatch,
        reconnect_interval=0, connection_status=ctx._hooks.connection_status,
        on_event_received=app._prepare_event,
    )
    task = asyncio.create_task(client.connect())
    try:
        socket = platform.add()
        socket.feed(10, d={'heartbeat_interval': 45000})
        await until(lambda: any(frame['op'] == 2 for frame in socket.sent))
        socket.feed(0, t='READY', s=1, d={'session_id': 'other-session'})
        await until(lambda: ctx.get_connection_status('other-bot').phase == 'ready')
        yield socket
    finally:
        await client.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('event_type', ['GUILD_UPDATE', 'GROUP_MEMBER_ADD'])
async def test_retired_writer_does_not_block_other_bot_of_same_owner(wired, event_type):
    ctx, platform, client, ordinary = wired
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    received = []

    async def update(delivery):
        if delivery.event.appid == 'sample-bot':
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
        received.append(delivery.event.appid)
        delivery.acknowledge()

    old_sub = ctx.subscribe_recovery_events(update, event_types={event_type})
    first = await login(ctx, platform)
    assert old_sub.establish(old_sub.get_status('sample-bot'))
    try:
        async with other_bot(ctx, platform) as other:
            first.feed(0, t=event_type, s=2, d={'id': 'guild', 'group_openid': 'g'})
            await until(entered.is_set)
            old_sub.cancel()
            await until(cancelled.is_set)
            replacement = ctx.subscribe_recovery_events(update, event_types={event_type})
            assert not replacement.establish(replacement.get_status('sample-bot'))
            assert replacement.establish(replacement.get_status('other-bot'))
            other.feed(0, t=event_type, s=2, d={'id': 'other-guild', 'group_openid': 'g'})
            await until(lambda: received == ['other-bot'])
            await until(lambda: replacement.get_status('other-bot').ready)
    finally:
        release.set()


@pytest.mark.parametrize('bad_type', [None, '', False, {}, [], True, 1, 0, 1.5, ['event'], {'name': 'event'}])
async def test_invalid_dispatch_type_cannot_restore_trust(wired, bad_type):
    ctx, platform, client, ordinary = wired
    calls = []

    async def update(delivery):
        calls.append(delivery)
        delivery.acknowledge()

    # 无法确定非法事件的类型时，该来源的所有实际依赖者都必须失效。
    group = ctx.subscribe_recovery_events(update, event_types={'GROUP_MEMBER_ADD'}, appid='sample-bot')
    general = ctx.subscribe_recovery_events(update, event_types={'GUILD_UPDATE'})
    first = await login(ctx, platform)
    for sub in (group, general):
        assert sub.establish(sub.get_status('sample-bot'))
    async with other_bot(ctx, platform) as other:
        assert general.establish(general.get_status('other-bot'))
        second = await resume(ctx, platform, first)
        second.feed(0, t=bad_type, s=2, d={'group_openid': 'g'})
        await until(lambda: ctx.get_connection_status('sample-bot').phase == 'unknown')
        assert ctx.get_connection_status('sample-bot').reason == 'invalid_dispatch_type'
        for sub in (group, general):
            assert sub.get_status('sample-bot').failed
            assert not sub.get_status('sample-bot').ready
        second.feed(0, t='RESUMED', s=3, d='')
        await until(lambda: ctx.get_connection_status('sample-bot').reason == 'unproven_resumed')
        for sub in (group, general):
            assert not sub.get_status('sample-bot').ready
            assert not sub.establish(sub.get_status('sample-bot'))
        assert calls == []
        other.feed(0, t='GUILD_UPDATE', s=2, d={'id': 'other-guild'})
        await until(lambda: len(calls) == 1 and general.get_status('other-bot').ready)
