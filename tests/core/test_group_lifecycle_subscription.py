"""真实传输入口、应用和模块订阅的接线测试。"""

import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp.test_utils import make_mocked_request

from core.application import Application
from core.base.config import cfg
from core.bot.registry import BotRegistry
from core.module.manager import ModuleManager
from core.plugin.manager import PluginManager


@pytest.fixture
async def lifecycle_app(tmp_path, sample_config_dir):
    cfg._ready = False
    cfg.init(sample_config_dir)
    app = Application()
    # 仅替换平台与存储边界，使用独立临时数据。
    store = SimpleNamespace(
        add=AsyncMock(), add_sync=Mock(), db_queue=Mock(),
        db_fetch_one=AsyncMock(return_value=None), db_execute=AsyncMock(),
    )
    app._bot_registry = BotRegistry(str(tmp_path / 'logs'), on_event=app._on_event, on_event_received=app._prepare_event)
    for appid in ('123456', 'other'):
        app._bot_registry._bots[appid] = SimpleNamespace(
            appid=appid, name=appid, secret='fake-secret',
            log_service=store, token_manager=SimpleNamespace(),
            sender=SimpleNamespace(refresh_group_info=AsyncMock(), ack_interaction=AsyncMock()),
        )
    module_dir = tmp_path / 'modules'
    shutil.copytree(Path(__file__).parents[1] / 'fixtures/modules/lifecycle_consumer', module_dir / 'consumer')
    manager = ModuleManager(str(module_dir), app.hook_manager)
    manager.discover()
    app._module_manager = manager
    yield app, manager
    await manager.shutdown()
    for worker in app._track_workers:
        worker.cancel()
    await asyncio.gather(*app._track_workers, return_exceptions=True)
    # 事件循环关闭前，让存储及后台分发结束。
    await asyncio.sleep(0)
    cfg._ready = False


async def webhook(app, payload, appid='123456'):
    request = make_mocked_request('POST', '/', headers={'X-Bot-Appid': appid})
    request._read_bytes = json.dumps(payload).encode()
    return await app._handle_webhook(request)


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


async def test_real_module_observes_webhook(lifecycle_app):
    app, manager = lifecycle_app
    assert await manager.enable('consumer', _skip_deps=True, _persist=False)
    consumer = manager.get('consumer')
    await webhook(app, {
        'op': 0, 't': 'GROUP_MEMBER_REMOVE', 'id': 'evt-remove', 's': 42,
        'd': {'group_openid': 'native-group', 'member_openid': 'native-member', 'timestamp': 123},
    })
    await eventually(lambda: consumer['events'])
    event = consumer['events'][0]
    assert (event.appid, event.group_openid, event.member_openid) == ('123456', 'native-group', 'native-member')
    assert event.transport == 'webhook'
    assert (event.event_id, event.sequence, event.timestamp) == ('evt-remove', 42, 123)
    await eventually(lambda: consumer['subscription'].results)
    assert consumer['subscription'].results[-1].status == 'success'


def lifecycle(kind='GROUP_MEMBER_REMOVE', group='g', **changes):
    data = {'group_openid': group, 'member_openid': 'member', 'op_member_openid': 'operator', 'timestamp': 123}
    data.update(changes)
    return {'op': 0, 't': kind, 'id': 'platform-id', 's': 9, 'd': data}


async def context(app, manager):
    assert await manager.enable('consumer', _skip_deps=True, _persist=False)
    return manager.get_context('consumer')


def ws_client(app, appid='123456'):
    bot = app.get_bot(appid)
    return app.bot_registry._create_ws_client(appid, bot, {})


@pytest.mark.parametrize('transport', ['websocket', 'webhook'])
@pytest.mark.parametrize('kind', ['GROUP_MEMBER_ADD', 'GROUP_MEMBER_REMOVE', 'GROUP_ADD_ROBOT', 'GROUP_DEL_ROBOT'])
async def test_four_kinds_keep_native_identity(lifecycle_app, kind, transport):
    app, manager = lifecycle_app
    await context(app, manager)
    consumer = manager.get('consumer')
    payload = lifecycle(kind)
    payload['d']['union_openid'] = 'display-id'
    if kind in ('GROUP_ADD_ROBOT', 'GROUP_DEL_ROBOT'):
        payload['d'].pop('member_openid')
    cfg.set_bot_setting('123456', 'identity.use_union_id_for_group', True)
    if transport == 'websocket':
        client = ws_client(app)
        client._session_id = 'fake-session'
        try:
            await client._on_dispatch(payload)
            await client._event_queue.join()
        finally:
            await client.close()
    else:
        await webhook(app, payload)
    await eventually(lambda: consumer['subscription'].results)
    event = consumer['events'][0]
    assert (event.event_type, event.member_openid, event.op_member_openid) == (kind, 'member' if 'MEMBER' in kind else None, 'operator')
    assert event.transport == transport
    assert event.session_id == ('fake-session' if transport == 'websocket' else None)


async def test_missing_native_fields_are_explicit_and_not_inferred(lifecycle_app):
    app, manager = lifecycle_app
    await context(app, manager)
    consumer = manager.get('consumer')
    # 适配器别名仍可传给旧插件，但不能当作 QQ 原生身份。
    await webhook(app, {'op': 0, 't': 'GROUP_MEMBER_ADD', 'd': {'group_id': 'alias', 'user_id': 'alias-user'}})
    await eventually(lambda: consumer['events'])
    event = consumer['events'][0]
    assert (event.group_openid, event.member_openid, event.op_member_openid) == (None, None, None)
    assert (event.event_id, event.timestamp, event.sequence) == (None, None, None)
    assert event.delivery_id


async def test_local_ids_do_not_claim_platform_dedup_or_order(lifecycle_app):
    app, manager = lifecycle_app
    await context(app, manager)
    consumer = manager.get('consumer')
    cfg.set_bot_setting('123456', 'dedup.enabled', True)
    for seq in (10, 10, 8):
        payload = lifecycle()
        payload['s'] = seq
        await webhook(app, payload)
    await eventually(lambda: len(consumer['events']) == 3)
    assert len({event.delivery_id for event in consumer['events']}) == 3
    assert [event.sequence for event in consumer['events']] == [10, 10, 8]


async def load_recording_plugin(app, tmp_path):
    path = tmp_path / 'plugins/record'
    path.mkdir(parents=True)
    (path / 'main.py').write_text('''from core.plugin.decorators import handler
received = []
@handler(r'.*', event_types=['GROUP_MEMBER_ADD', 'GROUP_MEMBER_REMOVE', 'GROUP_ADD_ROBOT', 'GROUP_DEL_ROBOT', 'GROUP_AT_MESSAGE_CREATE'])
async def record(event, match):
    received.append(event)
''')
    pm = PluginManager(str(path.parent), bot_appid='123456')
    await pm.load_all()
    app._plugin_manager = pm
    return pm, pm.plugins['record'].module.received


@pytest.mark.parametrize('transport', ['websocket', 'webhook'])
async def test_ingress_barrier_precedes_later_business_dispatch(lifecycle_app, tmp_path, transport):
    app, manager = lifecycle_app
    ctx = await context(app, manager)
    entered, release = asyncio.Event(), asyncio.Event()

    async def invalidate(event):
        entered.set()
        await release.wait()

    subscription = ctx.subscribe_group_lifecycle(invalidate)
    pm, received = await load_recording_plugin(app, tmp_path)
    client = ws_client(app)
    async def send(payload, appid='123456'):
        if transport == 'websocket' and appid == '123456':
            await client._on_dispatch(payload)
        else:
            await webhook(app, payload, appid)
    try:
        await send(lifecycle())
        await entered.wait()
        await send({'t': 'GROUP_AT_MESSAGE_CREATE', 'd': {'group_openid': 'g', 'content': 'query'}})
        await asyncio.sleep(0.02)
        assert not received
        # 第一个群等待期间，其他 Bot 和群仍能分发。
        await send({'t': 'GROUP_AT_MESSAGE_CREATE', 'd': {'group_openid': 'elsewhere', 'content': 'independent'}})
        await eventually(lambda: len(received) == 1)
        assert received[0].content == 'independent'
        release.set()
        await eventually(lambda: len(received) == 3)
        assert [e.event_type for e in received] == ['GROUP_AT_MESSAGE_CREATE', 'GROUP_MEMBER_REMOVE', 'GROUP_AT_MESSAGE_CREATE']
        receipt = received[1].group_lifecycle_delivery
        assert (await receipt.wait())[-1].subscription_id == subscription.subscription_id
    finally:
        release.set()
        await client.close()
        await pm.unload('record')


async def test_plugin_filters_and_disable_do_not_filter_modules(lifecycle_app, tmp_path):
    app, manager = lifecycle_app
    await context(app, manager)
    consumer = manager.get('consumer')
    pm, received = await load_recording_plugin(app, tmp_path)
    try:
        cfg.set_bot_setting('123456', 'maintenance.enabled', True)
        cfg.set_bot_setting('123456', 'blacklist.group_list', ['g'])
        cfg.set_bot_setting('123456', 'non_at_message.group_whitelist', ['other-group'])
        await webhook(app, lifecycle())
        await eventually(lambda: received)
        # 现有生命周期快速路径也跳过普通消息过滤。
        assert received[0].event_type == 'GROUP_MEMBER_REMOVE'
        pm.disable_plugin('record')
        await webhook(app, lifecycle('GROUP_MEMBER_ADD'))
        await eventually(lambda: len(consumer['events']) == 2)
        assert len(received) == 1
    finally:
        await pm.unload('record')


async def test_subscriber_errors_timeout_and_cancellation_are_independent(lifecycle_app):
    app, manager = lifecycle_app
    ctx = await context(app, manager)
    async def failing(event):
        raise ValueError('sample failure')
    async def slow(event):
        if event.appid == '123456':
            await asyncio.Event().wait()
    async def cancelled(event):
        raise asyncio.CancelledError
    failed = ctx.subscribe_group_lifecycle(failing)
    timed = ctx.subscribe_group_lifecycle(slow, timeout=0.02)
    cancelled_sub = ctx.subscribe_group_lifecycle(cancelled, appid='123456')
    await webhook(app, lifecycle())
    await webhook(app, lifecycle(), appid='other')
    await eventually(lambda: len(timed.results) == 2 and cancelled_sub.results)
    assert {r.event.appid: r.status for r in timed.results} == {'123456': 'timeout', 'other': 'success'}
    assert all(r.status == 'error' and r.error_type == 'ValueError' for r in failed.results)
    assert cancelled_sub.results[0].status == 'cancelled'
    consumer = manager.get('consumer')
    assert len(consumer['events']) == 2
    assert all(r.status == 'success' for r in consumer['subscription'].results)


@pytest.mark.parametrize('unload', [False, True])
async def test_cancel_or_unload_reports_inflight_and_pending(lifecycle_app, unload):
    app, manager = lifecycle_app
    ctx = await context(app, manager)
    started = asyncio.Event()
    async def callback(event):
        started.set()
        await asyncio.Event().wait()
    sub = ctx.subscribe_group_lifecycle(callback)
    await webhook(app, lifecycle())
    await started.wait()
    await webhook(app, lifecycle())
    if unload:
        assert await manager.disable('consumer', _persist=False)
    else:
        sub.cancel()
        sub.cancel()
    await eventually(lambda: len(sub.results) == 2)
    assert [r.status for r in sub.results] == ['unloaded' if unload else 'unsubscribed'] * 2
    if unload:
        with pytest.raises(RuntimeError, match='unloaded'):
            ctx.subscribe_group_lifecycle(callback)
        assert await manager.reload('consumer')
        new_consumer = manager.get('consumer')
        await webhook(app, lifecycle())
        await eventually(lambda: new_consumer['events'])
        assert len(sub.results) == 2


async def test_setup_failure_removes_partial_subscriptions(lifecycle_app, tmp_path):
    app, manager = lifecycle_app
    bad = tmp_path / 'modules/bad'
    bad.mkdir()
    (bad / 'main.py').write_text('''events = []
async def callback(event):
    events.append(event)
def setup(ctx):
    ctx.subscribe_group_lifecycle(callback)
    raise ValueError('setup failed')
''')
    manager.discover()
    assert not await manager.enable('bad', _skip_deps=True, _persist=False)
    module = manager.get_module('bad')
    await context(app, manager)
    await webhook(app, lifecycle())
    await eventually(lambda: manager.get('consumer')['events'])
    assert module.events == []


async def test_timeout_does_not_overlap_cancellation_resistant_callback(lifecycle_app):
    app, manager = lifecycle_app
    ctx = await context(app, manager)
    release, cancelled = asyncio.Event(), asyncio.Event()
    calls = []
    async def stubborn(event):
        calls.append(event.delivery_id)
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
    sub = ctx.subscribe_group_lifecycle(stubborn, timeout=0.02)
    try:
        await webhook(app, lifecycle())
        await cancelled.wait()
        await eventually(lambda: sub.results)
        await webhook(app, lifecycle())
        await eventually(lambda: len(sub.results) == 2)
        assert [r.status for r in sub.results] == ['timeout', 'busy']
        assert len(calls) == 1
        await webhook(app, lifecycle(group='another-group'))
        await eventually(lambda: len(sub.results) == 3)
        assert sub.results[-1].status == 'busy'
        assert len(calls) == 1
        # 无关 Bot 使用独立的调用通道。
        await webhook(app, lifecycle(), appid='other')
        await eventually(lambda: len(calls) == 2)
    finally:
        release.set()
        await asyncio.sleep(0)


async def test_delivery_receipt_exposes_complete_correlated_results(lifecycle_app, tmp_path):
    app, manager = lifecycle_app
    ctx = await context(app, manager)
    started, release = asyncio.Event(), asyncio.Event()
    async def observer(event):
        started.set()
        await release.wait()
    sub = ctx.subscribe_group_lifecycle(observer)
    pm, received = await load_recording_plugin(app, tmp_path)
    try:
        await webhook(app, lifecycle())
        await started.wait()
        # 开始实际插件分发时，事件携带管理器返回的投递回执。
        release.set()
        await eventually(lambda: received)
        receipt = received[0].group_lifecycle_delivery
        assert receipt.results[-1].subscription_id == sub.subscription_id
        assert (await receipt.wait())[-1].status == 'success'
    finally:
        release.set()
        await pm.unload('record')


async def test_missing_group_barrier_is_bot_scoped(lifecycle_app, tmp_path):
    app, manager = lifecycle_app
    ctx = await context(app, manager)
    started, release = asyncio.Event(), asyncio.Event()
    async def invalidation(event):
        started.set()
        await release.wait()
    ctx.subscribe_group_lifecycle(invalidation, appid='123456')
    pm, received = await load_recording_plugin(app, tmp_path)
    try:
        await webhook(app, {'t': 'GROUP_MEMBER_REMOVE', 'd': {'member_openid': 'member'}})
        await started.wait()
        await webhook(app, {'t': 'GROUP_AT_MESSAGE_CREATE', 'd': {'group_openid': 'any-group', 'content': 'wait'}})
        await webhook(app, {'t': 'GROUP_AT_MESSAGE_CREATE', 'd': {'group_openid': 'any-group', 'content': 'other-bot'}}, appid='other')
        await eventually(lambda: received)
        assert [(e.appid, e.content) for e in received] == [('other', 'other-bot')]
        release.set()
        await eventually(lambda: len(received) == 3)
    finally:
        release.set()
        await pm.unload('record')


async def test_same_subscriber_handles_concurrent_groups(lifecycle_app):
    app, manager = lifecycle_app
    ctx = await context(app, manager)
    first, second, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def callback(event):
        (first if event.group_openid == 'g' else second).set()
        await release.wait()
    sub = ctx.subscribe_group_lifecycle(callback)
    try:
        await webhook(app, lifecycle())
        await first.wait()
        await webhook(app, lifecycle(group='g2'))
        await asyncio.wait_for(second.wait(), 1)
        release.set()
        await eventually(lambda: len(sub.results) == 2)
        assert [r.status for r in sub.results] == ['success', 'success']
    finally:
        release.set()


async def test_background_work_is_not_part_of_callback_success(lifecycle_app):
    app, manager = lifecycle_app
    ctx = await context(app, manager)
    release = asyncio.Event()
    tasks = []
    async def callback(event):
        tasks.append(asyncio.create_task(release.wait()))
    sub = ctx.subscribe_group_lifecycle(callback)
    try:
        await webhook(app, lifecycle())
        await eventually(lambda: sub.results)
        assert sub.results[0].status == 'success'
        assert not tasks[0].done()
    finally:
        release.set()
        await asyncio.gather(*tasks)


async def test_result_history_is_bounded_and_receipts_are_waiter_safe(lifecycle_app):
    from core.server.webhook import WebhookHandler

    app, manager = lifecycle_app
    ctx = await context(app, manager)
    release, captured = asyncio.Event(), asyncio.Event()
    deliveries = []
    async def callback(event):
        await release.wait()
    sub = ctx.subscribe_group_lifecycle(callback)
    async def dispatch(event):
        deliveries.append(event.group_lifecycle_delivery)
        captured.set()
        await app._on_event(event)
    handler = WebhookHandler(app.bot_registry, dispatch, on_event_received=app._prepare_event)
    request = make_mocked_request('POST', '/', headers={'X-Bot-Appid': '123456'})
    request._read_bytes = json.dumps(lifecycle()).encode()
    try:
        await handler.handle(request)
        await captured.wait()
        receipt = deliveries[0]
        assert receipt.results is None
        waiter = asyncio.create_task(receipt.wait())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert receipt.results is None
        release.set()
        assert (await receipt.wait())[-1].status == 'success'
        for _ in range(105):
            await webhook(app, lifecycle())
        await eventually(lambda: len(sub.results) == 100)
        # 等待队列中的全部投递完成，再检查结果历史上限。
        await eventually(lambda: len(manager.get('consumer')['events']) == 106)
        assert sub.results[0].event.delivery_id != receipt.event.delivery_id
        assert (await receipt.wait())[-1].event.delivery_id == receipt.event.delivery_id
    finally:
        release.set()


@pytest.mark.parametrize('field,value', [('group_openid', 5), ('member_openid', []), ('op_member_openid', {}), ('timestamp', True)])
async def test_invalid_optional_metadata_is_not_promoted_to_identity(lifecycle_app, field, value):
    app, manager = lifecycle_app
    await context(app, manager)
    consumer = manager.get('consumer')
    payload = lifecycle(**{field: value})
    payload['s'] = True
    await webhook(app, payload)
    await eventually(lambda: consumer['events'])
    event = consumer['events'][0]
    assert getattr(event, field) is None
    assert event.sequence is None


async def test_ws_busy_group_does_not_exhaust_workers(lifecycle_app, tmp_path):
    app, manager = lifecycle_app
    ctx = await context(app, manager)
    entered, release = asyncio.Event(), asyncio.Event()
    async def callback(event):
        entered.set()
        await release.wait()
    ctx.subscribe_group_lifecycle(callback)
    pm, received = await load_recording_plugin(app, tmp_path)
    client = ws_client(app)
    try:
        await client._on_dispatch(lifecycle())
        await entered.wait()
        for _ in range(300):
            await client._on_dispatch({'t': 'GROUP_AT_MESSAGE_CREATE', 'd': {'group_openid': 'g', 'content': 'waiting'}})
        await client._on_dispatch({'t': 'GROUP_AT_MESSAGE_CREATE', 'd': {'group_openid': 'independent', 'content': 'ready'}})
        await eventually(lambda: received)
        assert [e.content for e in received] == ['ready']
        release.set()
        await asyncio.wait_for(client._event_queue.join(), 2)
        await eventually(lambda: len(received) == 302)
    finally:
        release.set()
        await client.close()
        await pm.unload('record')


async def test_cancelled_subscription_can_be_collected_before_module_unload(lifecycle_app):
    import gc
    import weakref

    app, manager = lifecycle_app
    ctx = await context(app, manager)
    async def callback(event):
        pass
    subscription = ctx.subscribe_group_lifecycle(callback)
    reference = weakref.ref(subscription)
    subscription.cancel()
    del subscription
    gc.collect()
    assert reference() is None


async def test_initial_bot_instance_ws_factory_is_wired(lifecycle_app, tmp_path):
    from core.bot.instance import BotInstance

    app, manager = lifecycle_app
    await context(app, manager)
    bot = BotInstance({'appid': '123456', 'secret': 'fake', 'websocket': {'enabled': True}}, str(tmp_path / 'initial-logs'))
    platform = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: {'username': 'fake'})))
    bot.token_manager = SimpleNamespace(
        ensure_token=AsyncMock(), start_auto_refresh=AsyncMock(),
        get_token=AsyncMock(return_value='fake'), get_client=AsyncMock(return_value=platform), api_base='https://example.invalid',
    )
    try:
        await bot.start(app._on_event, on_event_received=app._prepare_event)
        await bot.ws_client._on_dispatch(lifecycle())
        await bot.ws_client._event_queue.join()
        consumer = manager.get('consumer')
        assert consumer['events'][0].transport == 'websocket'
    finally:
        if bot.ws_client is not None:
            await bot.ws_client.close()
        await bot.log_service.shutdown()
        await bot.sender.close()
