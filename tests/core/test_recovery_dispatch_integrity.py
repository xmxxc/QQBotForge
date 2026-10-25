"""协议事件类型在兼容解析后仍须保持，所有恢复判定走真实 WS 入口。"""

import json

import pytest

from core.message.event import Event
from tests.core import test_resume_processing as recovery_helpers
from tests.core.test_connection_status import until
from tests.core.test_recovery_regressions import other_bot
from tests.core.test_resume_processing import login, resume

wired = recovery_helpers.wired


JOIN_MARKERS = [
    pytest.param({'_qqbotRawEvent': 'GROUP_JOIN_REQUEST'}, id='raw-event'),
    pytest.param({'post_type': 'request', 'request_type': 'group.add'}, id='request-fields'),
]


@pytest.mark.parametrize('marker', JOIN_MARKERS)
@pytest.mark.parametrize('original_type', ['GROUP_MEMBER_ADD', 'GUILD_UPDATE'])
async def test_flat_compat_payload_cannot_rename_protocol_dispatch(wired, marker, original_type):
    ctx, platform, client, ordinary = wired
    deliveries = []

    async def consume(delivery):
        deliveries.append(delivery)
        delivery.acknowledge()

    sub = ctx.subscribe_recovery_events(consume, event_types={original_type})
    first = await login(ctx, platform)
    assert sub.establish(sub.get_status('sample-bot'))
    second = await resume(ctx, platform, first)
    second.frames.put_nowait(json.dumps({
        'op': 0, 't': original_type, 's': 2, 'd': None,
        **marker, 'group_openid': 'g', 'member_openid': 'm',
    }))
    # 等读取完整个协议范围，安全预期不能依赖失败通知是否先被观察。
    second.feed(0, t='RESUMED', s=3, d='')
    await until(lambda: ctx.get_connection_status('sample-bot').context.sequence == 3)
    status = sub.get_status('sample-bot')
    assert not status.ready, (status, ordinary, deliveries)
    assert status.failed
    assert ctx.get_connection_status('sample-bot').reason == 'unproven_resumed'
    assert status.reason == 'unproven_resumed'
    assert deliveries == []
    assert ordinary == []


@pytest.mark.parametrize('data', [
    pytest.param({}, id='missing'),
    pytest.param({'d': []}, id='list'),
    pytest.param({'d': 'flat'}, id='string'),
    pytest.param({'d': 1}, id='number'),
    pytest.param({'d': False}, id='boolean'),
])
async def test_non_mapping_data_cannot_hide_dispatch_type_change(wired, data):
    ctx, platform, client, ordinary = wired
    deliveries = []

    async def consume(delivery):
        deliveries.append(delivery)
        delivery.acknowledge()

    sub = ctx.subscribe_recovery_events(consume, event_types={'GUILD_UPDATE'})
    first = await login(ctx, platform)
    assert sub.establish(sub.get_status('sample-bot'))
    second = await resume(ctx, platform, first)
    second.frames.put_nowait(json.dumps({
        'op': 0, 't': 'GUILD_UPDATE', 's': 2,
        '_qqbotRawEvent': 'GROUP_JOIN_REQUEST', 'group_openid': 'g', **data,
    }))
    await until(lambda: ctx.get_connection_status('sample-bot').phase == 'unknown')
    assert ctx.get_connection_status('sample-bot').reason == 'dispatch_type_mismatch'
    assert sub.get_status('sample-bot').failed
    second.feed(0, t='RESUMED', s=3, d='')
    await until(lambda: ctx.get_connection_status('sample-bot').context.sequence == 3)
    assert ctx.get_connection_status('sample-bot').reason == 'unproven_resumed'
    assert not sub.get_status('sample-bot').ready
    assert deliveries == []
    assert ordinary == []


@pytest.mark.parametrize('original_type', ['GROUP_MEMBER_ADD', 'GUILD_UPDATE'])
async def test_online_type_mismatch_invalidates_source_and_keeps_other_bot_healthy(wired, original_type):
    ctx, platform, client, ordinary = wired
    deliveries = []

    async def consume(delivery):
        deliveries.append((delivery.event.appid, delivery.event.event_type))
        delivery.acknowledge()

    sub = ctx.subscribe_recovery_events(consume, event_types={original_type})
    dependent = ctx.subscribe_recovery_events(consume, event_types={'GUILD_DELETE'}, depends_on=(sub,))
    reclassified = ctx.subscribe_recovery_events(consume, event_types={'GROUP_JOIN_REQUEST'})
    first = await login(ctx, platform)
    for subscription in (sub, dependent, reclassified):
        assert subscription.establish(subscription.get_status('sample-bot'))
    old_checkpoint = sub.get_status('sample-bot')
    async with other_bot(ctx, platform) as other:
        for subscription in (sub, dependent, reclassified):
            assert subscription.establish(subscription.get_status('other-bot'))
        other_before = tuple(subscription.get_status('other-bot') for subscription in (sub, dependent, reclassified))
        first.frames.put_nowait(json.dumps({
            'op': 0, 't': original_type, 's': 2, 'd': None,
            '_qqbotRawEvent': 'GROUP_JOIN_REQUEST', 'group_openid': 'g',
        }))
        await until(lambda: ctx.get_connection_status('sample-bot').phase == 'unknown')
        assert ctx.get_connection_status('sample-bot').reason == 'dispatch_type_mismatch'
        for subscription in (sub, dependent, reclassified):
            status = subscription.get_status('sample-bot')
            assert status.failed and not status.ready and status.pending == 0
        assert not sub.establish(old_checkpoint)
        assert tuple(subscription.get_status('other-bot') for subscription in (sub, dependent, reclassified)) == other_before
        assert deliveries == []
        assert ordinary == []
        # 后续合法事件仍投递，成功处理不会隐去同来源已知缺口。
        first.feed(0, t=original_type, s=3, d={'id': 'guild', 'group_openid': 'g'})
        other.feed(0, t=original_type, s=2, d={'id': 'other-guild', 'group_openid': 'other'})
        await until(lambda: len(deliveries) == 2 and len(ordinary) == 1 and sub.get_status('other-bot').ready)
        assert sorted(deliveries) == [('other-bot', original_type), ('sample-bot', original_type)]
        assert sub.get_status('sample-bot').failed
        assert not sub.get_status('sample-bot').ready
        # 下一轮合法补发边界也不能自动重建消费者基线。
        second = await resume(ctx, platform, first)
        second.feed(0, t='RESUMED', s=4, d='')
        await until(lambda: ctx.get_connection_status('sample-bot').phase == 'replay_complete')
        assert sub.get_status('sample-bot').reason == 'dispatch_type_mismatch'
        assert not sub.get_status('sample-bot').ready
        assert not dependent.get_status('sample-bot').ready
        assert sub.get_status('other-bot').ready
        assert dependent.get_status('other-bot').ready


@pytest.mark.parametrize('marker', JOIN_MARKERS)
@pytest.mark.parametrize('original_type', ['GROUP_MEMBER_ADD', 'GUILD_UPDATE'])
async def test_mapping_data_with_compat_markers_preserves_protocol_event(wired, marker, original_type):
    ctx, platform, client, ordinary = wired
    deliveries = []

    async def consume(delivery):
        deliveries.append(delivery)
        delivery.acknowledge()

    sub = ctx.subscribe_recovery_events(consume, event_types={original_type})
    first = await login(ctx, platform)
    assert sub.establish(sub.get_status('sample-bot'))
    second = await resume(ctx, platform, first)
    second.frames.put_nowait(json.dumps({
        'op': 0, 't': original_type, 's': 2,
        'd': {'id': 'guild', 'group_openid': 'g', 'member_openid': 'm'}, **marker,
    }))
    second.feed(0, t='RESUMED', s=3, d='')
    await until(lambda: sub.get_status('sample-bot').ready and len(ordinary) == 1)
    assert len(deliveries) == 1
    assert deliveries[0].event.event_type == original_type
    assert ordinary[0].event_type == original_type
    assert ctx.get_connection_status('sample-bot').reason == 'resumed'


@pytest.mark.parametrize('marker', JOIN_MARKERS)
@pytest.mark.parametrize('transport', ['webhook', 'adapter'])
def test_flat_join_request_compatibility_remains_available(marker, transport):
    payload = {
        **marker, 'event_id': 'request-event', 'group_openid': 'group-001',
        'member_openid': 'user-001', 'join_request_id': 'request-001',
        'verify_info': {'method': 'admin_review_qa', 'review_qa_list': [{'question': 'answer?', 'answer': '42'}]},
    }
    event = (
        Event.from_webhook({'X-Bot-Appid': 'sample-bot'}, payload)
        if transport == 'webhook' else Event.from_websocket('sample-bot', payload)
    )
    assert event.appid == 'sample-bot'
    assert event.event_type == 'GROUP_JOIN_REQUEST'
    assert event.event_id == 'request-event'
    assert event.group_id == 'group-001'
    assert event.raw_user_id == 'user-001'
    assert event.join_request_id == 'request-001'
    assert event.verify_method == 'admin_review_qa'
    assert event.review_qa_list == [{'question': 'answer?', 'answer': '42'}]
