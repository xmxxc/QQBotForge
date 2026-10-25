"""群生命周期隔离、交互清理及排队资源回归测试。"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from core.module.group_lifecycle import GroupLifecycleManager
from core.network.websocket import WSClient
from core.plugin.manager import PluginManager
from tests.core.test_group_lifecycle_subscription import (
    context,
    eventually,
    lifecycle,
    webhook,
    ws_client,
)

pytest_plugins = ('tests.core.test_group_lifecycle_subscription',)


async def recording_plugin(app, tmp_path):
    for bot in app._bots.values():
        bot.log_service.group_member_remove = AsyncMock(return_value=True)
        bot.log_service.group_member_touch = AsyncMock(return_value=True)
        bot.log_service.wakeup_update = AsyncMock()
        bot.log_service.subscribe_record = AsyncMock()
    root = tmp_path / 'plugins' / 'audit'
    root.mkdir(parents=True)
    (root / 'main.py').write_text("""from core.plugin.decorators import handler
received = []
@handler(r'.*', event_types=['C2C_MESSAGE_CREATE', 'GROUP_AT_MESSAGE_CREATE', 'INTERACTION_CREATE', 'AT_MESSAGE_CREATE', 'DIRECT_MESSAGE_CREATE', 'FRIEND_ADD', 'FRIEND_DEL', 'SUBSCRIBE_MESSAGE_STATUS'])
async def record(event, match):
    received.append((event.event_type, event.group_id, event.message_id))
    if event.is_interaction:
        event.set_callback_code(7)
""")
    manager = PluginManager(str(root.parent), bot_appid='123456')
    await manager.load_all()
    app._plugin_manager = manager
    return manager, manager.plugins['audit'].module.received


def button(group, identity):
    return {
        'op': 0,
        't': 'INTERACTION_CREATE',
        'd': {
            'id': identity,
            'type': 11,
            'chat_type': 1,
            'group_openid': group,
            'group_member_openid': 'member',
            'data': {'resolved': {'button_data': 'click'}},
        },
    }


async def test_private_message_remains_independent_of_slow_group(lifecycle_app, tmp_path):
    app, modules = lifecycle_app
    ctx = await context(app, modules)
    entered, release = asyncio.Event(), asyncio.Event()

    async def callback(event):
        entered.set()
        await release.wait()

    ctx.subscribe_group_lifecycle(callback, timeout=10)
    app.get_bot('123456').log_service.wakeup_update = AsyncMock()
    plugins, received = await recording_plugin(app, tmp_path)
    client = ws_client(app)
    try:
        await client._on_dispatch(lifecycle())
        await entered.wait()
        await client._on_dispatch(
            {
                'op': 0,
                't': 'C2C_MESSAGE_CREATE',
                'd': {
                    'id': 'private-message',
                    'author': {'id': 'private-user'},
                    'content': 'hello',
                },
            }
        )
        await client._on_dispatch(
            {
                'op': 0,
                't': 'GROUP_AT_MESSAGE_CREATE',
                'd': {
                    'id': 'other-group-message',
                    'group_openid': 'other-group',
                    'content': 'control',
                },
            }
        )
        await eventually(lambda: any(row[2] == 'other-group-message' for row in received))
        await asyncio.sleep(0.05)
        print('dispatch before group release:', received)
        assert any(row[2] == 'private-message' for row in received), 'unrelated private message was blocked by the group lifecycle barrier'
    finally:
        release.set()
        await client._event_queue.join()
        await client.close()
        await plugins.unload('audit')


async def test_other_group_button_keeps_plugin_ack_when_one_group_waits(lifecycle_app, tmp_path):
    app, modules = lifecycle_app
    ctx = await context(app, modules)
    entered, release = asyncio.Event(), asyncio.Event()

    async def callback(event):
        entered.set()
        await release.wait()

    ctx.subscribe_group_lifecycle(callback, timeout=10)
    plugins, received = await recording_plugin(app, tmp_path)
    client = ws_client(app)
    ack = app.get_bot('123456').sender.ack_interaction
    try:
        await client._on_dispatch(lifecycle())
        await entered.wait()
        for n in range(256):
            await client._on_dispatch(button('g', f'waiting-{n}'))
        await asyncio.sleep(0.02)
        await client._on_dispatch(button('other-group', 'independent-button'))
        private = button('', 'independent-private-button')
        private['d'].update(chat_type=2, user_openid='private-user')
        await client._on_dispatch(private)
        async with asyncio.timeout(3):
            while not {'independent-button', 'independent-private-button'}.issubset({call.args[0].message_id for call in ack.await_args_list}):
                await asyncio.sleep(0.005)
        code = next(call.args[1] for call in ack.await_args_list if call.args[0].message_id == 'independent-button')
        handled = any(row[2] == 'independent-button' for row in received)
        print(
            'independent group button:',
            {
                'ack_code': code,
                'handler_ran': handled,
                'semaphore_locked': client._event_sem.locked(),
            },
        )
        assert code == 7 and handled, 'unrelated button got default ACK before its plugin handler could execute'
        private_code = next(call.args[1] for call in ack.await_args_list if call.args[0].message_id == 'independent-private-button')
        assert private_code == 7 and any(row[2] == 'independent-private-button' for row in received)
    finally:
        release.set()
        await eventually(lambda: any(row[2] == 'independent-button' for row in received))
        await client.close()
        await plugins.unload('audit')


async def test_lifecycle_backlog_uses_bounded_scheduler_tasks(lifecycle_app):
    """单群排队数量增长时，调度任务数不随事件数量增长。"""
    app, modules = lifecycle_app
    ctx = await context(app, modules)
    entered, release = asyncio.Event(), asyncio.Event()
    observed = []
    independent = set()

    async def callback(event):
        if event.appid != '123456' or event.group_openid != 'g':
            independent.add((event.appid, event.group_openid))
            return
        entered.set()
        await release.wait()
        observed.append(event.sequence)

    subscription = ctx.subscribe_group_lifecycle(callback, timeout=30)
    for bot in app._bots.values():
        bot.log_service.group_member_remove = AsyncMock(return_value=False)
    client = ws_client(app)
    receipts = []
    original_receive = client._on_event_received

    def capture_receipt(event, **metadata):
        original_receive(event, **metadata)
        if event.group_lifecycle_delivery is not None and event.group_id == 'g':
            receipts.append(event.group_lifecycle_delivery)

    client._on_event_received = capture_receipt
    before = len(asyncio.all_tasks())
    try:
        first = lifecycle()
        first['s'] = 0
        await client._on_dispatch(first)
        await entered.wait()
        measurements = {}
        for index in range(1, 10000):
            payload = lifecycle()
            payload['s'] = index
            await client._on_dispatch(payload)
            if index + 1 in (100, 1000, 10000):
                for _ in range(5):
                    await asyncio.sleep(0)
                measurements[index + 1] = len(asyncio.all_tasks()) - before
        print('单群积压的新增存活任务：', measurements)
        assert max(measurements.values()) < 320
        assert max(measurements.values()) - min(measurements.values()) < 10
        await client._on_dispatch(lifecycle(group='other-group'))
        await webhook(app, lifecycle(), appid='other')
        await eventually(lambda: independent == {('123456', 'other-group'), ('other', 'g')})
        release.set()
        await asyncio.wait_for(client._event_queue.join(), 15)
        assert observed == list(range(10000))
        assert all(result.status == 'success' for result in subscription.results)
        assert len(receipts) == 10000
        assert len({receipt.event.delivery_id for receipt in receipts}) == 10000
        assert all(next(r for r in receipt.results if r.subscription_id == subscription.subscription_id).status == 'success' for receipt in receipts)
        await eventually(lambda: len(asyncio.all_tasks()) - before <= 256)
    finally:
        release.set()
        await client.close()


async def test_closing_ws_discards_waiting_interactions(lifecycle_app, tmp_path):
    """连接关闭后，尚未放行的按钮不会再次执行处理器。"""
    app, modules = lifecycle_app
    ctx = await context(app, modules)
    entered, release = asyncio.Event(), asyncio.Event()

    async def callback(event):
        entered.set()
        await release.wait()

    subscription = ctx.subscribe_group_lifecycle(callback)
    plugins, received = await recording_plugin(app, tmp_path)
    client = ws_client(app)
    try:
        await client._on_dispatch(lifecycle())
        await entered.wait()
        await client._on_dispatch(button('g', 'waiting-at-close'))
        await asyncio.sleep(0.02)
        await client.close()
        await asyncio.wait_for(client._event_queue.join(), 1)
        await client._on_dispatch(button('other-group', 'arrived-after-close'))
        release.set()
        await eventually(lambda: subscription.results)
        await asyncio.sleep(0.02)
        assert not any(row[2] in {'waiting-at-close', 'arrived-after-close'} for row in received)
    finally:
        release.set()
        await client.close()
        await plugins.unload('audit')


@pytest.mark.parametrize('resume_started', [False, True])
async def test_ws_close_clears_deferred_queue_before_or_after_resume_starts(resume_started):
    """恢复任务无论是否开始运行，关闭都会清空暂存并配平队列计数。"""
    manager = GroupLifecycleManager()
    entered, release = asyncio.Event(), asyncio.Event()
    observed, receipts = [], []

    async def callback(event):
        entered.set()
        await release.wait()
        observed.append(event.sequence)

    subscription = manager.subscribe(callback)

    def on_received(event, **metadata):
        manager.prepare(event, **metadata)
        receipts.append(event.group_lifecycle_delivery)

    async def on_event(event):
        await manager.wait(event)

    client = WSClient('123456', None, on_event, ack_interaction=AsyncMock(), on_event_received=on_received)
    try:
        for sequence in range(100):
            payload = lifecycle()
            payload['s'] = sequence
            await client._on_dispatch(payload)
        if resume_started:
            await entered.wait()
            await asyncio.sleep(0)
        # 未让出执行权时，普通 worker 先暂存，关闭先于新恢复任务运行。
        closing = asyncio.create_task(client.close())
        await asyncio.wait_for(closing, 1)
        assert not client._deferred_events
        await asyncio.wait_for(client._event_queue.join(), 1)
        # 重复关闭不能对已清理的暂存事件再次调用 task_done。
        await client.close()
        await asyncio.wait_for(client._event_queue.join(), 1)
    finally:
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*(receipt.wait() for receipt in receipts)), 3)
        await client.close()
        subscription.cancel()
    assert observed == list(range(100))
    assert all(result.status == 'success' for delivery_results in results for result in delivery_results)


@pytest.mark.parametrize('waiting_on', ['group_barrier', 'semaphore'])
async def test_starting_ws_close_cancels_buttons_before_they_are_released(waiting_on):
    """普通回调仍在取消清理时，已开始关闭的连接不会再分发等待中的按钮。"""
    manager = GroupLifecycleManager()
    entered, release = asyncio.Event(), asyncio.Event()
    worker_started, worker_cleaning, worker_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
    received = []
    held = 0

    async def lifecycle_callback(event):
        entered.set()
        await release.wait()

    subscription = manager.subscribe(lifecycle_callback)

    async def on_event(event):
        nonlocal held
        await manager.wait(event)
        if event.message_id == 'ordinary':
            worker_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                # 模拟正常回调响应取消时仍需完成的异步清理。
                worker_cleaning.set()
                await worker_cleanup.wait()
        elif event.message_id.startswith('holding-'):
            held += 1
            await release.wait()
        elif event.message_id == 'released-during-close':
            received.append(event.message_id)
            event.set_callback_code(7)

    ack = AsyncMock()
    client = WSClient('123456', None, on_event, ack_interaction=ack, on_event_received=manager.prepare)
    closing = None
    try:
        await client._on_dispatch({'t': 'C2C_MESSAGE_CREATE', 'd': {'id': 'ordinary', 'author': {'id': 'user'}, 'content': 'hello'}})
        await worker_started.wait()
        if waiting_on == 'group_barrier':
            await client._on_dispatch(lifecycle())
            await entered.wait()
        else:
            for index in range(256):
                await client._on_dispatch(button('g', f'holding-{index}'))
            await eventually(lambda: held == 256)
        await client._on_dispatch(button('g', 'released-during-close'))
        for _ in range(3):
            await asyncio.sleep(0)
        closing = asyncio.create_task(client.close())
        await worker_cleaning.wait()
        release.set()
        if waiting_on == 'group_barrier':
            await eventually(lambda: subscription.results)
        # 让已解除的屏障或已归还的并发槽位完成调度，关闭仍等待普通回调清理。
        for _ in range(20):
            await asyncio.sleep(0)
        assert not closing.done()
        assert not received
        assert not any(call.args[0].message_id == 'released-during-close' for call in ack.await_args_list)
    finally:
        release.set()
        worker_cleanup.set()
        if closing is not None:
            await closing
        await client.close()
        await asyncio.wait_for(client._event_queue.join(), 1)
        subscription.cancel()


@pytest.mark.parametrize('transport', ['websocket', 'webhook'])
@pytest.mark.parametrize(
    'kind',
    [
        'C2C_MESSAGE_CREATE',
        'AT_MESSAGE_CREATE',
        'DIRECT_MESSAGE_CREATE',
        'FRIEND_ADD',
        'FRIEND_DEL',
        'SUBSCRIBE_MESSAGE_STATUS',
    ],
)
async def test_non_group_events_ignore_group_barriers(lifecycle_app, tmp_path, transport, kind):
    """私聊、频道和好友事件不会被群 ID 为空误判成未知群。"""
    app, modules = lifecycle_app
    ctx = await context(app, modules)
    entered, release = asyncio.Event(), asyncio.Event()

    async def callback(event):
        entered.set()
        await release.wait()

    ctx.subscribe_group_lifecycle(callback)
    plugins, received = await recording_plugin(app, tmp_path)
    client = ws_client(app)
    payload = {
        't': kind,
        'd': {
            'id': 'independent',
            'content': 'hello',
            'author': {'id': 'user'},
            'openid': 'user',
            'guild_id': 'guild',
            'channel_id': 'channel',
        },
    }

    async def send(value):
        if transport == 'websocket':
            await client._on_dispatch(value)
        else:
            await webhook(app, value)

    try:
        await send(lifecycle())
        await entered.wait()
        await send(payload)
        await asyncio.wait_for(eventually(lambda: any(row[0] == kind for row in received)), 1)
    finally:
        release.set()
        await client._event_queue.join()
        await client.close()
        await plugins.unload('audit')


@pytest.mark.parametrize('transport', ['websocket', 'webhook'])
async def test_private_button_is_independent_but_unknown_group_button_waits(lifecycle_app, tmp_path, transport):
    """区分无群 ID 的私聊按钮和缺原生群身份的群按钮。"""
    app, modules = lifecycle_app
    ctx = await context(app, modules)
    entered, release = asyncio.Event(), asyncio.Event()

    async def callback(event):
        entered.set()
        await release.wait()

    ctx.subscribe_group_lifecycle(callback)
    plugins, received = await recording_plugin(app, tmp_path)
    client = ws_client(app)
    pending = None
    ack = app.get_bot('123456').sender.ack_interaction
    private = button('', 'private-button')
    private['d'].update(chat_type=2, user_openid='private-user')
    unknown_group = button('', 'unknown-group-button')
    try:
        await client._on_dispatch(lifecycle())
        await entered.wait()
        if transport == 'websocket':
            await client._on_dispatch(unknown_group)
            await client._on_dispatch(private)
            await eventually(lambda: any(call.args[0].message_id == 'private-button' for call in ack.await_args_list))
            assert next(call.args[1] for call in ack.await_args_list if call.args[0].message_id == 'private-button') == 7
        else:
            pending = asyncio.create_task(webhook(app, unknown_group))
            response = await asyncio.wait_for(webhook(app, private), 1)

            assert json.loads(response.text) == {'op': 12, 'code': 7}
        assert any(row[2] == 'private-button' for row in received)
        assert not any(row[2] == 'unknown-group-button' for row in received)
        release.set()
        await eventually(lambda: any(row[2] == 'unknown-group-button' for row in received))
    finally:
        release.set()
        if pending is not None:
            await pending
        await client.close()
        await plugins.unload('audit')


async def test_ack_timer_runs_while_group_barrier_is_pending(lifecycle_app, tmp_path):
    """同群等待仍从接收时计时，超时后不补发第二次 ACK。"""
    app, modules = lifecycle_app
    ctx = await context(app, modules)
    entered, release = asyncio.Event(), asyncio.Event()

    async def callback(event):
        entered.set()
        await release.wait()

    ctx.subscribe_group_lifecycle(callback, timeout=10)
    plugins, received = await recording_plugin(app, tmp_path)
    client = ws_client(app)
    ack = app.get_bot('123456').sender.ack_interaction
    try:
        await client._on_dispatch(lifecycle())
        await entered.wait()
        await client._on_dispatch(button('g', 'still-waiting'))
        async with asyncio.timeout(3):
            while not ack.await_args_list:
                await asyncio.sleep(0.005)
        assert ack.await_args_list[0].args[1] == 0
        assert not received
        release.set()
        await eventually(lambda: received)
        await asyncio.sleep(0.02)
        assert len(ack.await_args_list) == 1
    finally:
        release.set()
        await client.close()
        await plugins.unload('audit')


@pytest.mark.parametrize(
    'action,expected',
    [('cancel', 'unsubscribed'), ('unload', 'unloaded'), ('close', 'unsubscribed'), ('timeout', 'timeout')],
)
async def test_lifecycle_backlog_settles_all_receipts(lifecycle_app, action, expected):
    """排队回执在取消、卸载及回调超时后都有真实终态。"""
    app, modules = lifecycle_app
    ctx = await context(app, modules)
    entered = asyncio.Event()

    async def callback(event):
        entered.set()
        await asyncio.Event().wait()

    subscription = ctx.subscribe_group_lifecycle(callback, timeout=0.01 if action == 'timeout' else 5)
    client = ws_client(app)
    original_receive = client._on_event_received
    receipts = []

    def capture(event, **metadata):
        original_receive(event, **metadata)
        receipts.append(event.group_lifecycle_delivery)

    client._on_event_received = capture
    for bot in app._bots.values():
        bot.log_service.group_member_remove = AsyncMock(return_value=False)
    try:
        for n in range(20):
            payload = lifecycle()
            payload['s'] = n
            await client._on_dispatch(payload)
        await entered.wait()
        if action == 'close':
            await client.close()
            subscription.cancel()
        elif action == 'cancel':
            subscription.cancel()
        elif action == 'unload':
            assert await modules.disable('consumer', _persist=False)
        await asyncio.wait_for(client._event_queue.join(), 2)
        assert len(receipts) == 20
        for receipt in receipts:
            results = await asyncio.wait_for(receipt.wait(), 2)
            assert next(r for r in results if r.subscription_id == subscription.subscription_id).status == expected
    finally:
        subscription.cancel()
        await client.close()
