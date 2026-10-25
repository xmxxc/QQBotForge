"""从可控 QQ socket 到本地状态更新的恢复判定。"""

import asyncio
from types import SimpleNamespace

import pytest

from core.application import Application
from core.module.manager import ModuleContext
from core.network.websocket import WSClient
from tests.core.test_connection_status import Platform, until


@pytest.fixture
async def wired(monkeypatch, tmp_path, sample_config_dir, request):
    from core.base.config import cfg

    cfg._ready = False
    cfg.init(sample_config_dir)
    app = Application()
    hooks = app.hook_manager
    app._bot_registry = SimpleNamespace(bots={'sample-bot': SimpleNamespace()})
    app._bot_registry.get = app._bot_registry.bots.get
    ctx = ModuleContext('state-consumer', str(tmp_path), hooks)
    platform = Platform(monkeypatch)
    ordinary = []

    async def on_event(event):
        ordinary.append(event)

    client = WSClient(
        'sample-bot',
        platform,
        on_event,
        ack_interaction=on_event,
        reconnect_interval=0,
        custom_url='wss://custom.invalid/ws' if getattr(request, 'param', None) == 'custom' else '',
        connection_status=hooks.connection_status,
        on_event_received=app._prepare_event if getattr(request, 'param', True) else None,
    )
    task = asyncio.create_task(client.connect())
    try:
        yield ctx, platform, client, ordinary
    finally:
        ctx._close_subscriptions()
        await client.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        cfg._ready = False


async def login(ctx, platform):
    socket = platform.add()
    socket.feed(10, d={'heartbeat_interval': 45000})
    await until(lambda: any(frame['op'] == 2 for frame in socket.sent))
    socket.feed(0, t='READY', s=1, d={'session_id': 'sample-session'})
    await until(lambda: ctx.get_connection_status('sample-bot').phase == 'ready')
    return socket


async def resume(ctx, platform, first):
    first.frames.put_nowait(OSError('sample disconnect'))
    second = platform.add()
    second.feed(10, d={'heartbeat_interval': 45000})
    await until(lambda: any(frame['op'] == 6 for frame in second.sent))
    return second


async def test_resumed_waits_for_explicit_local_completion(wired):
    ctx, platform, client, ordinary = wired
    entered, release = asyncio.Event(), asyncio.Event()
    state = []

    async def update(delivery):
        entered.set()
        await release.wait()
        state.append(delivery.event.member_openid)
        delivery.acknowledge()

    sub = ctx.subscribe_recovery_events(update, event_types={'GROUP_MEMBER_REMOVE'})
    first = await login(ctx, platform)
    assert not sub.get_status('sample-bot').ready
    assert sub.establish(sub.get_status('sample-bot'))
    second = await resume(ctx, platform, first)
    second.feed(
        0,
        t='GROUP_MEMBER_REMOVE',
        s=2,
        d={'group_openid': 'group', 'member_openid': 'member'},
    )
    await until(entered.is_set)
    second.feed(0, t='RESUMED', s=3, d='')
    await until(lambda: ctx.get_connection_status('sample-bot').phase == 'replay_complete')
    assert not sub.get_status('sample-bot').ready
    assert sub.get_status('sample-bot').pending == 1
    release.set()
    await until(lambda: sub.get_status('sample-bot').ready)
    assert state == ['member']
    assert sub.get_status('sample-bot').replay_end_sequence == 3


async def test_callback_return_without_acknowledgement_does_not_restore(wired):
    ctx, platform, client, ordinary = wired
    background_release = asyncio.Event()
    background = []

    async def update(delivery):
        background.append(asyncio.create_task(background_release.wait()))

    sub = ctx.subscribe_recovery_events(update, event_types={'GROUP_MEMBER_REMOVE'})
    first = await login(ctx, platform)
    assert sub.establish(sub.get_status('sample-bot'))
    second = await resume(ctx, platform, first)
    second.feed(0, t='GROUP_MEMBER_REMOVE', s=2, d={'group_openid': 'g'})
    second.feed(0, t='RESUMED', s=3, d='')
    await until(lambda: sub.get_status('sample-bot').failed)
    assert sub.get_status('sample-bot').reason == 'unconfirmed'
    assert not sub.get_status('sample-bot').ready
    background_release.set()
    await asyncio.gather(*background)


async def test_out_of_order_completion_cannot_skip_pending_event(wired):
    ctx, platform, client, ordinary = wired
    release = asyncio.Event()
    finished = []

    async def update(delivery):
        if delivery.event.event_id == 'slow':
            await release.wait()
        finished.append(delivery.event.event_id)
        delivery.acknowledge()

    sub = ctx.subscribe_recovery_events(update, event_types={'GUILD_UPDATE'})
    first = await login(ctx, platform)
    assert sub.establish(sub.get_status('sample-bot'))
    second = await resume(ctx, platform, first)
    # 使用非生命周期事件走独立消费者路径，完成顺序由消费者控制。
    second.frames.put_nowait('{"op":0,"t":"GUILD_UPDATE","s":2,"id":"slow","d":{"id":"guild"}}')
    second.frames.put_nowait('{"op":0,"t":"GUILD_UPDATE","s":3,"id":"fast","d":{"id":"guild"}}')
    second.feed(0, t='RESUMED', s=4, d='')
    await until(lambda: finished == ['fast'] and sub.get_status('sample-bot').pending == 1)
    assert sub.get_status('sample-bot').pending == 1
    assert not sub.get_status('sample-bot').ready
    assert len(ordinary) == 2
    release.set()
    await until(lambda: sub.get_status('sample-bot').ready)
    assert finished == ['fast', 'slow']


@pytest.mark.parametrize(
    'kind,reason',
    [('error', 'error'), ('timeout', 'timeout'), ('cancelled', 'cancelled')],
)
async def test_failures_are_sticky_and_isolated(wired, kind, reason):
    ctx, platform, client, ordinary = wired
    delivered = []

    async def broken(delivery):
        delivered.append(delivery)
        delivery.acknowledge()
        if kind == 'error':
            raise RuntimeError('sample error')
        if kind == 'cancelled':
            raise asyncio.CancelledError
        await asyncio.Event().wait()

    async def healthy(delivery):
        delivery.acknowledge()

    bad = ctx.subscribe_recovery_events(broken, event_types={'GUILD_UPDATE'}, timeout=0.03)
    good = ctx.subscribe_recovery_events(healthy, event_types={'GUILD_UPDATE'})
    first = await login(ctx, platform)
    for sub in (bad, good):
        assert sub.establish(sub.get_status('sample-bot'))
    second = await resume(ctx, platform, first)
    second.feed(0, t='GUILD_UPDATE', s=2, d={'id': 'guild'})
    second.feed(0, t='RESUMED', s=3, d='')
    await until(lambda: bad.get_status('sample-bot').failed and good.get_status('sample-bot').ready)
    assert bad.get_status('sample-bot').reason == reason
    assert not bad.get_status('sample-bot').ready
    assert not delivered[0].acknowledge()
    assert len(ordinary) == 1
    second.feed(0, t='GUILD_UPDATE', s=4, d={'id': 'guild'})
    await until(lambda: len(ordinary) == 2)
    assert not bad.get_status('sample-bot').ready


async def test_old_session_callback_cannot_certify_or_mutate_established_new_baseline(
    wired,
):
    ctx, platform, client, ordinary = wired
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    deliveries = []

    async def update(delivery):
        deliveries.append(delivery)
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
        delivery.acknowledge()

    sub = ctx.subscribe_recovery_events(update, event_types={'GUILD_UPDATE'})
    first = await login(ctx, platform)
    assert sub.establish(sub.get_status('sample-bot'))
    first.feed(0, t='GUILD_UPDATE', s=2, d={'id': 'guild'})
    await until(entered.is_set)
    await client.close()
    next_task = asyncio.create_task(client.connect())
    try:
        await login(ctx, platform)
        assert not sub.get_status('sample-bot').ready
        assert not sub.establish(sub.get_status('sample-bot'))
        assert not deliveries[0].acknowledge()
        release.set()
        await until(lambda: sub.establish(sub.get_status('sample-bot')))
        assert sub.get_status('sample-bot').ready
    finally:
        release.set()
        await client.close()
        next_task.cancel()
        await asyncio.gather(next_task, return_exceptions=True)


async def test_duplicate_and_conflicting_duplicate(wired):
    ctx, platform, client, ordinary = wired
    members = []

    async def update(delivery):
        members.append(delivery.event.member_openid)
        delivery.acknowledge()

    sub = ctx.subscribe_recovery_events(update, event_types={'GROUP_MEMBER_ADD'})
    first = await login(ctx, platform)
    assert sub.establish(sub.get_status('sample-bot'))
    for _ in range(2):
        first.feed(
            0,
            t='GROUP_MEMBER_ADD',
            s=2,
            d={'group_openid': 'g', 'member_openid': 'one'},
        )
    await until(lambda: len(ordinary) == 2)
    await until(lambda: sub.get_status('sample-bot').pending == 0)
    assert members == ['one']
    first.feed(0, t='GROUP_MEMBER_ADD', s=2, d={'group_openid': 'g', 'member_openid': 'two'})
    await until(lambda: len(ordinary) == 3)
    assert sub.get_status('sample-bot').reason == 'conflicting_duplicate'
    assert not sub.get_status('sample-bot').ready


async def test_shared_dependency_invalidates_actual_dependents_only(wired):
    ctx, platform, client, ordinary = wired

    async def update(delivery):
        delivery.acknowledge()

    shared = ctx.subscribe_recovery_events(update, event_types={'GUILD_UPDATE'})
    dependent = ctx.subscribe_recovery_events(update, event_types={'GUILD_UPDATE'}, depends_on=(shared,))
    independent = ctx.subscribe_recovery_events(update, event_types={'GUILD_UPDATE'})
    await login(ctx, platform)
    for sub in (shared, dependent, independent):
        assert sub.establish(sub.get_status('sample-bot'))
    checkpoint = dependent.get_status('sample-bot')
    shared.invalidate('sample-bot')
    assert dependent.get_status('sample-bot').reason == 'dependency_invalidated'
    assert independent.get_status('sample-bot').ready
    assert shared.establish(shared.get_status('sample-bot'))
    assert not dependent.establish(checkpoint)
    assert not dependent.get_status('sample-bot').ready
    assert dependent.establish(dependent.get_status('sample-bot'))
    assert dependent.get_status('sample-bot').ready
    shared.cancel()
    assert not dependent.get_status('sample-bot').ready
    assert independent.get_status('sample-bot').ready


async def test_fenced_baseline_rejects_event_received_during_rebuild(wired):
    ctx, platform, client, ordinary = wired

    async def update(delivery):
        delivery.acknowledge()

    sub = ctx.subscribe_recovery_events(update, event_types={'GUILD_UPDATE'})
    first = await login(ctx, platform)
    checkpoint = sub.get_status('sample-bot')
    first.feed(0, t='GUILD_UPDATE', s=2, d={'id': 'guild'})
    await until(lambda: len(ordinary) == 1)
    await until(lambda: sub.get_status('sample-bot').pending == 0)
    assert not sub.establish(checkpoint)
    assert sub.establish(sub.get_status('sample-bot'))


async def test_late_subscriber_reload_and_new_application_require_baselines(wired, tmp_path):
    ctx, platform, client, ordinary = wired

    async def update(delivery):
        delivery.acknowledge()

    first = await login(ctx, platform)
    second = await resume(ctx, platform, first)
    second.feed(0, t='RESUMED', s=2, d='')
    await until(lambda: ctx.get_connection_status('sample-bot').phase == 'replay_complete')
    sub = ctx.subscribe_recovery_events(update, event_types={'GUILD_UPDATE'})
    assert not sub.get_status('sample-bot').ready
    assert sub.establish(sub.get_status('sample-bot'))
    checkpoint = sub.get_status('sample-bot')
    ctx._close_subscriptions()
    assert sub.get_status('sample-bot').reason == 'unloaded'
    assert not sub.establish(checkpoint)
    replacement_ctx = ModuleContext('state-consumer', str(tmp_path / 'replacement'), ctx._hooks)
    replacement = replacement_ctx.subscribe_recovery_events(update, event_types={'GUILD_UPDATE'})
    assert not replacement.get_status('sample-bot').ready
    assert not replacement.establish(checkpoint)
    fresh_app = Application()
    fresh_ctx = ModuleContext('state-consumer', str(tmp_path / 'fresh'), fresh_app.hook_manager)
    fresh = fresh_ctx.subscribe_recovery_events(update, event_types={'GUILD_UPDATE'})
    assert not fresh.get_status('sample-bot').ready
    assert not fresh.establish(checkpoint)
    replacement_ctx._close_subscriptions()
    fresh_ctx._close_subscriptions()


async def test_max_pending_fails_closed_without_blocking_ordinary_dispatch(wired):
    ctx, platform, client, ordinary = wired
    release = asyncio.Event()
    called = []

    async def update(delivery):
        called.append(delivery)
        await release.wait()
        delivery.acknowledge()

    sub = ctx.subscribe_recovery_events(update, event_types={'GUILD_UPDATE'}, max_pending=2)
    first = await login(ctx, platform)
    assert sub.establish(sub.get_status('sample-bot'))
    for sequence in range(2, 12):
        first.feed(0, t='GUILD_UPDATE', s=sequence, d={'id': 'guild'})
    await until(lambda: len(ordinary) == 10)
    assert len(called) == 2
    assert sub.get_status('sample-bot').pending == 2
    assert sub.get_status('sample-bot').reason == 'overflow'
    release.set()
    await until(lambda: sub.get_status('sample-bot').pending == 0)
    assert not sub.get_status('sample-bot').ready


async def test_unknown_protocol_and_unverifiable_gateway_never_restore(wired):
    ctx, platform, client, ordinary = wired

    async def update(delivery):
        delivery.acknowledge()

    sub = ctx.subscribe_recovery_events(update, event_types={'GUILD_UPDATE'})
    first = await login(ctx, platform)
    assert sub.establish(sub.get_status('sample-bot'))
    second = await resume(ctx, platform, first)
    second.feed(99)
    second.feed(0, t='RESUMED', s=2, d='')
    await until(lambda: ctx.get_connection_status('sample-bot').phase == 'replay_complete')
    assert not sub.get_status('sample-bot').ready
    assert sub.get_status('sample-bot').reason == 'unknown_opcode'
    assert not sub.get_status('sample-bot', transport='webhook').ready
    assert sub.get_status('sample-bot', transport='webhook').phase == 'unsupported'


async def test_two_bots_are_isolated_with_one_subscription(wired):
    ctx, platform, client, ordinary = wired
    release = asyncio.Event()

    async def update(delivery):
        if delivery.event.appid == 'sample-bot':
            await release.wait()
            raise ValueError('sample failure')
        delivery.acknowledge()

    sub = ctx.subscribe_recovery_events(update, event_types={'GUILD_UPDATE'})
    first = await login(ctx, platform)
    assert sub.establish(sub.get_status('sample-bot'))
    other_events = []

    async def other_dispatch(event):
        other_events.append(event)

    # 第二个 Bot 仍通过同一个实际接收入口和状态管理器。
    app = Application()
    app._hook_manager = ctx._hooks
    app._bot_registry = SimpleNamespace(bots={'sample-bot': SimpleNamespace(), 'other-bot': SimpleNamespace()})
    app._bot_registry.get = app._bot_registry.bots.get
    other = WSClient(
        'other-bot',
        platform,
        other_dispatch,
        ack_interaction=other_dispatch,
        reconnect_interval=0,
        connection_status=ctx._hooks.connection_status,
        on_event_received=app._prepare_event,
    )
    other_task = asyncio.create_task(other.connect())
    try:
        other_socket = platform.add()
        other_socket.feed(10, d={'heartbeat_interval': 45000})
        await until(lambda: any(frame['op'] == 2 for frame in other_socket.sent))
        other_socket.feed(0, t='READY', s=1, d={'session_id': 'other-session'})
        await until(lambda: ctx.get_connection_status('other-bot').phase == 'ready')
        assert sub.establish(sub.get_status('other-bot'))
        first.feed(0, t='GUILD_UPDATE', s=2, d={'id': 'guild'})
        other_socket.feed(0, t='GUILD_UPDATE', s=2, d={'id': 'other-guild'})
        await until(lambda: len(other_events) == 1 and sub.get_status('other-bot').ready)
        release.set()
        await until(lambda: sub.get_status('sample-bot').failed)
        assert sub.get_status('other-bot').ready
    finally:
        release.set()
        await other.close()
        other_task.cancel()
        await asyncio.gather(other_task, return_exceptions=True)


async def test_actual_module_unload_reload_and_setup_failure(wired, tmp_path):
    import shutil
    from pathlib import Path

    from core.module.manager import ModuleManager

    ctx, platform, client, ordinary = wired
    module_root = tmp_path / 'modules'
    shutil.copytree(
        Path(__file__).parents[1] / 'fixtures/modules/recovery_consumer',
        module_root / 'consumer',
    )
    manager = ModuleManager(str(module_root), ctx._hooks)
    manager.discover()
    assert await manager.enable('consumer', _skip_deps=True, _persist=False)
    consumer = manager.get('consumer')
    first = await login(ctx, platform)
    sub = consumer['subscription']
    assert sub.establish(sub.get_status('sample-bot'))
    first.feed(0, t='GROUP_MEMBER_ADD', s=2, d={'group_openid': 'g', 'member_openid': 'member'})
    await until(lambda: sub.get_status('sample-bot').pending == 1)
    assert await manager.disable('consumer', _persist=False)
    assert sub.get_status('sample-bot').reason == 'unloaded'
    assert consumer['received'] == []
    assert await manager.reload('consumer')
    replacement = manager.get('consumer')
    assert replacement['subscription'].subscription_id != sub.subscription_id
    assert not replacement['subscription'].get_status('sample-bot').ready
    # 注销不等于旧回调退出；先等其响应取消，再开始新代际处理。
    await until(lambda: replacement['subscription'].get_status('sample-bot').reason != 'old_callback_running')
    replacement['release'].set()
    first.feed(0, t='GROUP_MEMBER_ADD', s=3, d={'group_openid': 'g', 'member_openid': 'other'})
    await until(lambda: replacement['received'] == ['GROUP_MEMBER_ADD'])
    broken = module_root / 'broken'
    observed = tmp_path / 'broken-recovery-events.log'
    broken.mkdir()
    (broken / 'main.py').write_text(f"""from pathlib import Path

observed = Path({str(observed)!r})

async def consume(delivery):
    observed.write_text('called\\n', encoding='utf-8')
def setup(ctx):
    ctx.subscribe_recovery_events(consume, event_types={'GUILD_UPDATE'})
    raise ValueError('sample setup failure')
""")
    manager.discover()
    assert not await manager.enable('broken', _skip_deps=True, _persist=False)
    assert manager.get_module('broken') is None
    first.feed(0, t='GUILD_UPDATE', s=4, d={'id': 'guild'})
    await until(lambda: len(ordinary) == 3)
    assert not observed.exists()
    await manager.shutdown()


@pytest.mark.parametrize('event_type', ['GROUP_MEMBER_REMOVE', 'GUILD_UPDATE'])
async def test_timeout_resistant_callback_stays_local_and_cannot_reestablish(wired, event_type):
    ctx, platform, client, ordinary = wired
    release, cancelled = asyncio.Event(), asyncio.Event()
    deliveries = []

    async def stubborn(delivery):
        deliveries.append(delivery)
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
        delivery.acknowledge()

    async def healthy(delivery):
        delivery.acknowledge()

    bad = ctx.subscribe_recovery_events(stubborn, event_types={event_type}, timeout=0.02)
    good = ctx.subscribe_recovery_events(healthy, event_types={event_type})
    first = await login(ctx, platform)
    for sub in (bad, good):
        assert sub.establish(sub.get_status('sample-bot'))
    second = await resume(ctx, platform, first)
    second.feed(0, t=event_type, s=2, d={'id': 'guild', 'group_openid': 'g'})
    second.feed(0, t='RESUMED', s=3, d='')
    try:
        await until(lambda: cancelled.is_set() and good.get_status('sample-bot').ready)
        assert bad.get_status('sample-bot').reason == 'timeout'
        assert not deliveries[0].acknowledge()
        assert not bad.establish(bad.get_status('sample-bot'))
        second.feed(0, t=event_type, s=4, d={'id': 'guild', 'group_openid': 'other'})
        await until(lambda: len(ordinary) == 2)
        assert good.get_status('sample-bot').ready
    finally:
        release.set()


async def test_post_boundary_events_do_not_move_fixed_recovery_target(wired):
    ctx, platform, client, ordinary = wired
    release = asyncio.Event()
    entered = asyncio.Event()

    async def update(delivery):
        if delivery.context.sequence > 3:
            entered.set()
            await release.wait()
        delivery.acknowledge()

    sub = ctx.subscribe_recovery_events(update, event_types={'GUILD_UPDATE'})
    first = await login(ctx, platform)
    assert sub.establish(sub.get_status('sample-bot'))
    second = await resume(ctx, platform, first)
    second.feed(0, t='GUILD_UPDATE', s=2, d={'id': 'guild'})
    second.feed(0, t='RESUMED', s=3, d='')
    second.feed(0, t='GUILD_UPDATE', s=4, d={'id': 'guild'})
    await until(lambda: entered.is_set() and sub.get_status('sample-bot').pending == 1)
    assert sub.get_status('sample-bot').ready
    assert sub.get_status('sample-bot').pending == 1
    assert sub.get_status('sample-bot').boundary_pending == 0
    assert sub.get_status('sample-bot').replay_end_sequence == 3
    release.set()


async def test_callback_receives_snapshot_isolated_from_ordinary_plugin_mutation(wired):
    ctx, platform, client, ordinary = wired
    release = asyncio.Event()
    values = []

    async def update(delivery):
        await release.wait()
        values.append(delivery.event.raw['d']['name'])
        with pytest.raises(TypeError):
            delivery.event.raw['d']['name'] = 'modified'
        with pytest.raises(AttributeError):
            delivery.context = None
        with pytest.raises(AttributeError):
            delivery.event = ordinary[0]
        delivery.acknowledge()

    sub = ctx.subscribe_recovery_events(update, event_types={'GUILD_UPDATE'})
    first = await login(ctx, platform)
    first.feed(0, t='GUILD_UPDATE', s=2, d={'id': 'guild', 'name': 'original'})
    await until(lambda: len(ordinary) == 1)
    ordinary[0].raw['d']['name'] = 'ordinary-modification'
    release.set()
    await until(lambda: sub.get_status('sample-bot').pending == 0)
    assert values == ['original']


async def test_appid_scope_cannot_establish_another_bots_baseline(wired):
    ctx, platform, client, ordinary = wired

    async def update(delivery):
        delivery.acknowledge()

    sub = ctx.subscribe_recovery_events(update, event_types={'GUILD_UPDATE'}, appid='different-bot')
    await login(ctx, platform)
    assert not sub.establish(sub.get_status('sample-bot'))


@pytest.mark.parametrize(
    'kwargs,error',
    [
        ({'event_types': 'GUILD_UPDATE'}, TypeError),
        ({'event_types': []}, ValueError),
        ({'event_types': ['RESUMED']}, ValueError),
        ({'timeout': True}, ValueError),
        ({'timeout': float('inf')}, ValueError),
        ({'max_pending': 0}, ValueError),
        ({'max_pending': True}, ValueError),
    ],
)
async def test_invalid_subscription_contract_is_rejected(wired, kwargs, error):
    ctx, platform, client, ordinary = wired

    async def update(delivery):
        delivery.acknowledge()

    options = {'event_types': {'GUILD_UPDATE'}, **kwargs}
    with pytest.raises(error):
        ctx.subscribe_recovery_events(update, **options)


@pytest.mark.parametrize('wired', [False], indirect=True)
@pytest.mark.parametrize('event_type', ['GROUP_MEMBER_ADD', 'GUILD_UPDATE'])
async def test_adapter_without_receipt_registration_cannot_restore(wired, event_type):
    ctx, platform, client, ordinary = wired

    async def update(delivery):
        delivery.acknowledge()

    sub = ctx.subscribe_recovery_events(update, event_types={event_type})
    first = await login(ctx, platform)
    assert sub.establish(sub.get_status('sample-bot'))
    second = await resume(ctx, platform, first)
    second.feed(0, t=event_type, s=2, d={'group_openid': 'g', 'id': 'guild'})
    second.feed(0, t='RESUMED', s=3, d='')
    await until(lambda: ctx.get_connection_status('sample-bot').phase == 'replay_complete')
    assert not sub.get_status('sample-bot').ready
    assert sub.get_status('sample-bot').reason == 'unobserved_dispatch'


async def test_unparseable_dispatch_leaves_known_gap(wired):
    ctx, platform, client, ordinary = wired

    async def update(delivery):
        delivery.acknowledge()

    sub = ctx.subscribe_recovery_events(update, event_types={'GROUP_AT_MESSAGE_CREATE'})
    first = await login(ctx, platform)
    assert sub.establish(sub.get_status('sample-bot'))
    second = await resume(ctx, platform, first)
    # 非法事件名使真实事件解析失败，不能把日志后跳过当作成功。
    second.feed(0, t=['malformed-event-name'], s=2, d={'content': 'sample'})
    second.feed(0, t='RESUMED', s=3, d='')
    await until(lambda: ctx.get_connection_status('sample-bot').reason == 'unproven_resumed')
    assert not sub.get_status('sample-bot').ready


@pytest.mark.parametrize('event_type', ['GROUP_MEMBER_ADD', 'GUILD_UPDATE'])
@pytest.mark.parametrize('sequence', [None, True])
async def test_missing_or_invalid_sequence_requires_rebuild(wired, event_type, sequence):
    ctx, platform, client, ordinary = wired
    applied = []

    async def update(delivery):
        applied.append(delivery)
        delivery.acknowledge()

    sub = ctx.subscribe_recovery_events(update, event_types={event_type})
    first = await login(ctx, platform)
    assert sub.establish(sub.get_status('sample-bot'))
    first.feed(0, t=event_type, s=sequence, d={'group_openid': 'g', 'id': 'guild'})
    await until(lambda: len(ordinary) == 1)
    assert sub.get_status('sample-bot').reason == 'missing_sequence'
    assert not sub.get_status('sample-bot').ready
    assert applied == []


@pytest.mark.parametrize('event_type', ['GROUP_MEMBER_ADD', 'GUILD_UPDATE'])
async def test_old_sequence_beyond_deduplication_window_fails_closed(wired, event_type):
    ctx, platform, client, ordinary = wired
    applied = []

    async def update(delivery):
        applied.append(delivery.context.sequence)
        delivery.acknowledge()

    sub = ctx.subscribe_recovery_events(update, event_types={event_type}, max_pending=512)
    first = await login(ctx, platform)
    assert sub.establish(sub.get_status('sample-bot'))
    for sequence in range(2, 260):
        first.feed(0, t=event_type, s=sequence, d={'group_openid': 'g', 'id': 'guild'})
    await until(lambda: len(applied) == 258 and sub.get_status('sample-bot').pending == 0)
    assert sub.get_status('sample-bot').ready
    first.feed(0, t=event_type, s=2, d={'group_openid': 'g', 'id': 'guild'})
    await until(lambda: len(ordinary) == 259)
    assert len(applied) == 258
    assert sub.get_status('sample-bot').reason == 'late_event'
    assert not sub.get_status('sample-bot').ready


async def test_invalid_session_revokes_established_state(wired):
    ctx, platform, client, ordinary = wired

    async def update(delivery):
        delivery.acknowledge()

    sub = ctx.subscribe_recovery_events(update, event_types={'GUILD_UPDATE'})
    first = await login(ctx, platform)
    assert sub.establish(sub.get_status('sample-bot'))
    second = await resume(ctx, platform, first)
    second.feed(9, d=False)
    await until(lambda: ctx.get_connection_status('sample-bot').phase == 'resume_failed')
    assert not sub.get_status('sample-bot').ready
    assert sub.get_status('sample-bot').reason == 'invalid_session'
    assert not sub.establish(sub.get_status('sample-bot'))


@pytest.mark.parametrize('event_type', ['GROUP_MEMBER_ADD', 'GUILD_UPDATE'])
async def test_pending_event_before_disconnect_is_part_of_recovery_boundary(wired, event_type):
    ctx, platform, client, ordinary = wired
    release, entered = asyncio.Event(), asyncio.Event()

    async def update(delivery):
        entered.set()
        await release.wait()
        delivery.acknowledge()

    sub = ctx.subscribe_recovery_events(update, event_types={event_type})
    first = await login(ctx, platform)
    assert sub.establish(sub.get_status('sample-bot'))
    first.feed(0, t=event_type, s=2, d={'group_openid': 'g', 'id': 'guild'})
    await until(entered.is_set)
    second = await resume(ctx, platform, first)
    second.feed(0, t='RESUMED', s=3, d='')
    await until(lambda: ctx.get_connection_status('sample-bot').phase == 'replay_complete')
    assert sub.get_status('sample-bot').boundary_pending == 1
    assert not sub.get_status('sample-bot').ready
    release.set()
    await until(lambda: sub.get_status('sample-bot').ready)


@pytest.mark.parametrize('wired', ['custom'], indirect=True)
async def test_custom_gateway_cannot_establish_continuity_even_after_resumed(wired):
    ctx, platform, client, ordinary = wired

    async def update(delivery):
        delivery.acknowledge()

    sub = ctx.subscribe_recovery_events(update, event_types={'GUILD_UPDATE'})
    first = await login(ctx, platform)
    assert sub.get_status('sample-bot').phase == 'unsupported'
    assert not sub.establish(sub.get_status('sample-bot'))
    second = await resume(ctx, platform, first)
    second.feed(0, t='RESUMED', s=2, d='')
    await until(lambda: ctx.get_connection_status('sample-bot').reason == 'unproven_resumed')
    assert sub.get_status('sample-bot').phase == 'unsupported'
    assert not sub.establish(sub.get_status('sample-bot'))
