"""实际应用和插件分发与恢复消费者的独立接收、确认边界。"""

import asyncio
import json
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.application import Application
from core.bot.registry import BotRegistry
from core.module.manager import ModuleContext
from core.network.websocket import WSClient
from core.plugin.manager import PluginManager
from tests.core.test_connection_status import Platform, until
from tests.core.test_resume_processing import login, resume


@pytest.fixture
async def application_recovery(monkeypatch, tmp_path, sample_config_dir):
    from core.base.config import cfg

    cfg._ready = False
    cfg.init(sample_config_dir)
    app = Application()
    app._bot_registry = BotRegistry(str(tmp_path / 'logs'), on_event=app._on_event, on_event_received=app._prepare_event)
    app._bot_registry._bots['sample-bot'] = SimpleNamespace(
        appid='sample-bot', name='sample-bot', sender=SimpleNamespace(),
        log_service=SimpleNamespace(add=AsyncMock(), add_sync=Mock(), db_queue=Mock()),
    )
    root = tmp_path / 'plugins' / 'record'
    root.mkdir(parents=True)
    (root / 'main.py').write_text('''from core.plugin.decorators import handler
received = []
entered = None
release = None
@handler(r'.*', event_types=['INDEPENDENT_UPDATE'])
async def record(event, match):
    if event.event_id == 'slow':
        entered.set()
        await release.wait()
    received.append(event.event_id)
''')
    plugins = PluginManager(str(root.parent), bot_appid='sample-bot')
    await plugins.load_all()
    app._plugin_manager = plugins
    module = plugins.plugins['record'].module
    module.entered, module.release = asyncio.Event(), asyncio.Event()
    ctx = ModuleContext('recovery-integration', str(tmp_path / 'consumer'), app.hook_manager)
    platform = Platform(monkeypatch)
    client = WSClient(
        'sample-bot', platform, app._on_event,
        ack_interaction=AsyncMock(), on_event_received=app._prepare_event,
        connection_status=app.hook_manager.connection_status, reconnect_interval=0,
    )
    task = asyncio.create_task(client.connect())
    try:
        yield ctx, platform, client, app, module
    finally:
        module.release.set()
        ctx._close_subscriptions()
        await client.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await plugins.unload('record')
        cfg._ready = False


@pytest.mark.parametrize('event_type', ['GUILD_UPDATE', 'GROUP_MEMBER_REMOVE'])
async def test_group_preregistration_does_not_skip_recovery_registration(application_recovery, event_type):
    ctx, platform, client, app, plugin = application_recovery
    deliveries = []

    async def update(delivery):
        deliveries.append(delivery)
        delivery.acknowledge()

    def prepare(event, **kwargs):
        # 适配器已经登记群顺序，应用仍需独立登记恢复处理。
        app.hook_manager.group_lifecycle.prepare(event, **kwargs)
        app._prepare_event(event, **kwargs)

    client._on_event_received = prepare
    sub = ctx.subscribe_recovery_events(update, event_types={event_type})
    first = await login(ctx, platform)
    assert sub.establish(sub.get_status('sample-bot'))
    second = await resume(ctx, platform, first)
    second.feed(0, t=event_type, s=2, d={'id': 'guild', 'group_openid': 'g', 'member_openid': 'member'})
    second.feed(0, t='RESUMED', s=3, d='')
    await until(lambda: sub.get_status('sample-bot').ready)
    assert len(deliveries) == 1
    assert deliveries[0].event.event_type == event_type
    assert sub.get_status('sample-bot').pending == 0
    assert sub.get_status('sample-bot').replay_end_sequence == 3


@pytest.mark.parametrize('event_type', ['GUILD_UPDATE', 'GROUP_MEMBER_REMOVE'])
async def test_real_plugins_run_concurrently_while_recovery_is_waiting(application_recovery, event_type):
    ctx, platform, client, app, plugin = application_recovery
    entered, release = asyncio.Event(), asyncio.Event()

    async def update(delivery):
        entered.set()
        await release.wait()
        delivery.acknowledge()

    sub = ctx.subscribe_recovery_events(update, event_types={event_type})
    first = await login(ctx, platform)
    assert sub.establish(sub.get_status('sample-bot'))
    second = await resume(ctx, platform, first)
    second.feed(0, t=event_type, s=2, d={'id': 'guild', 'group_openid': 'g', 'member_openid': 'member'})
    second.feed(0, t='RESUMED', s=3, d='')
    try:
        await until(lambda: entered.is_set() and ctx.get_connection_status('sample-bot').phase == 'replay_complete')
        assert not sub.get_status('sample-bot').ready
        assert sub.get_status('sample-bot').boundary_pending == 1
        for sequence, event_id in [(4, 'slow'), (5, 'fast')]:
            second.frames.put_nowait(json.dumps({'op': 0, 't': 'INDEPENDENT_UPDATE', 's': sequence, 'd': {}, 'id': event_id}))
        await until(lambda: plugin.entered.is_set() and plugin.received == ['fast'])
        assert not sub.get_status('sample-bot').ready
        assert not release.is_set()
        plugin.release.set()
        await until(lambda: plugin.received == ['fast', 'slow'])
        assert not sub.get_status('sample-bot').ready
        release.set()
        await until(lambda: sub.get_status('sample-bot').ready)
        assert sub.get_status('sample-bot').pending == 0
    finally:
        release.set()


@pytest.mark.parametrize('eager', [False, True])
async def test_self_cancelled_writer_blocks_replacement_until_real_exit(application_recovery, eager):
    if eager and not hasattr(asyncio, 'eager_task_factory'):
        pytest.skip('eager task factory requires Python 3.12+')
    ctx, platform, client, app, plugin = application_recovery
    entered, release, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()
    state = []

    async def old_update(delivery):
        old.cancel()
        entered.set()
        while not release.is_set():
            with suppress(asyncio.CancelledError):
                await release.wait()
        state.append('late old write')
        assert not delivery.acknowledge()
        exited.set()

    async def replacement_update(delivery):
        delivery.acknowledge()

    old = ctx.subscribe_recovery_events(old_update, event_types={'GUILD_UPDATE'})
    socket = await login(ctx, platform)
    assert old.establish(old.get_status('sample-bot'))
    loop = asyncio.get_running_loop()
    previous = loop.get_task_factory()
    try:
        if eager:
            loop.set_task_factory(asyncio.eager_task_factory)
        socket.feed(0, t='GUILD_UPDATE', s=2, d={'id': 'guild'})
        await until(entered.is_set)
        replacement = ctx.subscribe_recovery_events(replacement_update, event_types={'GUILD_UPDATE'})
        checkpoint = replacement.get_status('sample-bot')
        assert not replacement.establish(checkpoint)
        assert replacement.get_status('sample-bot').reason == 'old_callback_running'
        assert state == []
        release.set()
        await until(exited.is_set)
        await until(lambda: replacement.get_status('sample-bot').reason != 'old_callback_running')
        assert not replacement.establish(checkpoint)
        assert state == ['late old write']
        assert replacement.establish(replacement.get_status('sample-bot'))
        assert replacement.get_status('sample-bot').ready
    finally:
        release.set()
        await until(exited.is_set)
        loop.set_task_factory(previous)
