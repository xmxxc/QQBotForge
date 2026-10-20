"""最小状态消费者；仅供框架接线测试。"""

import asyncio

received = []
release = None
subscription = None


async def consume(delivery):
    await release.wait()
    received.append(delivery.event.event_type)
    delivery.acknowledge()


def setup(ctx):
    global release, subscription
    release = asyncio.Event()
    subscription = ctx.subscribe_recovery_events(
        consume, event_types={"GROUP_MEMBER_ADD", "GUILD_UPDATE"}
    )
    return {"subscription": subscription, "release": release, "received": received}
