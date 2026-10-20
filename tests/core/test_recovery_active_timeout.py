"""真实协议回调超时后，重建凭据必须等待实际写入者退出。"""

import asyncio

import pytest

from tests.core import test_resume_processing as recovery_helpers
from tests.core.test_connection_status import until
from tests.core.test_resume_processing import login

wired = recovery_helpers.wired


@pytest.mark.parametrize('event_type', ['GUILD_UPDATE', 'GROUP_MEMBER_ADD'])
@pytest.mark.parametrize('exit_kind', ['return', 'error', 'cancelled'])
async def test_active_timeout_writer_exit_expires_rebuild_checkpoint(wired, event_type, exit_kind):
    ctx, platform, client, ordinary = wired
    cancelled, release, exited = (asyncio.Event() for _ in range(3))
    state = {'value': 'initial'}
    state_lock = asyncio.Lock()
    writes = []

    async def stubborn(delivery):
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
        try:
            async with state_lock:
                state['value'] = 'old callback write'
                writes.append(state['value'])
            assert delivery.acknowledge() is False
            if exit_kind == 'error':
                raise RuntimeError('sample late callback failure')
            if exit_kind == 'cancelled':
                raise asyncio.CancelledError
        finally:
            exited.set()

    sub = ctx.subscribe_recovery_events(stubborn, event_types={event_type}, timeout=0.02)
    first = await login(ctx, platform)
    assert sub.establish(sub.get_status('sample-bot'))
    try:
        first.feed(0, t=event_type, s=2, d={'id': 'guild', 'group_openid': 'g'})
        await until(cancelled.is_set)
        checkpoint = sub.get_status('sample-bot')
        assert checkpoint.reason == 'timeout' and checkpoint.pending == 0
        async with state_lock:
            state['value'] = 'authoritative rebuild'
            writes.append(state['value'])
        assert not sub.establish(checkpoint)
        release.set()
        async with asyncio.timeout(3):
            await exited.wait()
        # Event 唤醒可以早于 task done 回调；完成尚未记账也不能接受旧凭据。
        assert not sub.establish(checkpoint)
        await until(lambda: sub.get_status('sample-bot').revision > checkpoint.revision)
        status = sub.get_status('sample-bot')
        assert writes == ['authoritative rebuild', 'old callback write']
        assert status.failed and status.reason == 'timeout' and not status.ready
        assert not sub.establish(checkpoint)
        fresh = sub.get_status('sample-bot')
        async with state_lock:
            state['value'] = 'authoritative rebuild after exit'
        assert sub.establish(fresh)
        assert sub.get_status('sample-bot').ready
        assert state['value'] == 'authoritative rebuild after exit'
    finally:
        release.set()


@pytest.mark.parametrize('event_type', ['GUILD_UPDATE', 'GROUP_MEMBER_ADD'])
async def test_active_timeout_exit_preserves_independent_subscription_and_bot(wired, event_type):
    from tests.core.test_recovery_regressions import other_bot

    ctx, platform, client, ordinary = wired
    cancelled, release, exited = (asyncio.Event() for _ in range(3))
    received = []

    async def update(delivery):
        if delivery.event.appid == 'sample-bot':
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
            assert delivery.acknowledge() is False
            exited.set()
        else:
            received.append(delivery.event.appid)
            delivery.acknowledge()

    async def healthy(delivery):
        delivery.acknowledge()

    sub = ctx.subscribe_recovery_events(update, event_types={event_type}, timeout=0.02)
    peer = ctx.subscribe_recovery_events(healthy, event_types={event_type})
    first = await login(ctx, platform)
    for subscription in (sub, peer):
        assert subscription.establish(subscription.get_status('sample-bot'))
    try:
        async with other_bot(ctx, platform) as other:
            assert sub.establish(sub.get_status('other-bot'))
            first.feed(0, t=event_type, s=2, d={'id': 'guild', 'group_openid': 'g'})
            await until(cancelled.is_set)
            await until(lambda: peer.get_status('sample-bot').ready)
            checkpoint = sub.get_status('sample-bot')
            # 同模块的独立订阅可继续重建，另一个 Bot 仍处理实际事件。
            assert peer.establish(peer.get_status('sample-bot'))
            other.feed(0, t=event_type, s=2, d={'id': 'other-guild', 'group_openid': 'g'})
            await until(lambda: received == ['other-bot'] and sub.get_status('other-bot').ready)
            stable_peer = peer.get_status('sample-bot')
            stable_bot = sub.get_status('other-bot')
            release.set()
            await until(exited.is_set)
            await until(lambda: sub.get_status('sample-bot').revision > checkpoint.revision)
            assert peer.get_status('sample-bot') == stable_peer
            assert sub.get_status('other-bot') == stable_bot
            assert sub.establish(stable_bot)
            assert not sub.establish(checkpoint)
            assert sub.get_status('sample-bot').reason == 'timeout'
    finally:
        release.set()


@pytest.mark.parametrize('event_type', ['GUILD_UPDATE', 'GROUP_MEMBER_ADD'])
async def test_each_active_timeout_writer_exit_expires_checkpoint(wired, event_type):
    ctx, platform, client, ordinary = wired
    entered = [asyncio.Event(), asyncio.Event()]
    cancelled = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    exited = [asyncio.Event(), asyncio.Event()]

    async def update(delivery):
        index = delivery.context.sequence - 2
        entered[index].set()
        while not release[index].is_set():
            try:
                await release[index].wait()
            except asyncio.CancelledError:
                cancelled[index].set()
        assert delivery.acknowledge() is False
        exited[index].set()

    sub = ctx.subscribe_recovery_events(update, event_types={event_type}, timeout=0.1)
    first = await login(ctx, platform)
    assert sub.establish(sub.get_status('sample-bot'))
    try:
        for sequence in (2, 3):
            first.feed(0, t=event_type, s=sequence, d={'id': 'guild', 'group_openid': str(sequence)})
        await until(lambda: all(event.is_set() for event in entered))
        await until(lambda: all(event.is_set() for event in cancelled))
        assert sub.get_status('sample-bot').pending == 0
        original = sub.get_status('sample-bot')
        release[0].set()
        await until(exited[0].is_set)
        await until(lambda: sub.get_status('sample-bot').revision > original.revision)
        intermediate = sub.get_status('sample-bot')
        assert not sub.establish(original)
        assert not sub.establish(intermediate)
        release[1].set()
        await until(exited[1].is_set)
        await until(lambda: sub.get_status('sample-bot').revision > intermediate.revision)
        assert not sub.establish(original)
        assert not sub.establish(intermediate)
        assert sub.get_status('sample-bot').reason == 'timeout'
        assert sub.establish(sub.get_status('sample-bot'))
    finally:
        for event in release:
            event.set()


@pytest.mark.parametrize('event_type', ['GUILD_UPDATE', 'GROUP_MEMBER_ADD'])
@pytest.mark.parametrize('retirement', ['cancel', 'new_session'])
async def test_active_timeout_retirement_keeps_new_baseline_guarded(wired, event_type, retirement):
    ctx, platform, client, ordinary = wired
    cancelled, release, exited = (asyncio.Event() for _ in range(3))

    async def stubborn(delivery):
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
        assert delivery.acknowledge() is False
        exited.set()

    sub = ctx.subscribe_recovery_events(stubborn, event_types={event_type}, timeout=0.02)
    first = await login(ctx, platform)
    assert sub.establish(sub.get_status('sample-bot'))
    try:
        first.feed(0, t=event_type, s=2, d={'id': 'guild', 'group_openid': 'g'})
        await until(cancelled.is_set)
        original = sub.get_status('sample-bot')
        if retirement == 'cancel':
            sub.cancel()
            replacement = ctx.subscribe_recovery_events(stubborn, event_types={event_type})
        else:
            first.feed(9, d=False)
            second = platform.add()
            second.feed(10, d={'heartbeat_interval': 45000})
            # INVALID_SESSION 的实际协议路径包含三秒退避。
            async with asyncio.timeout(5):
                while not any(frame['op'] == 2 for frame in second.sent):
                    await asyncio.sleep(0.001)
            second.feed(0, t='READY', s=1, d={'session_id': 'replacement-session'})
            await until(lambda: ctx.get_connection_status('sample-bot').context.session_id == 'replacement-session')
            replacement = sub
        checkpoint = replacement.get_status('sample-bot')
        assert checkpoint.reason == 'old_callback_running'
        assert not replacement.establish(checkpoint)
        release.set()
        async with asyncio.timeout(3):
            await exited.wait()
        assert not replacement.establish(checkpoint)
        await until(lambda: replacement.get_status('sample-bot').revision > checkpoint.revision)
        assert not replacement.get_status('sample-bot').ready
        assert not replacement.establish(checkpoint)
        assert not sub.establish(original)
        assert replacement.establish(replacement.get_status('sample-bot'))
        assert replacement.get_status('sample-bot').ready
    finally:
        release.set()
