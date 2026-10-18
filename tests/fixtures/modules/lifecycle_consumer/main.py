"""独立样例消费者，用于验证模块通知接线。"""


def setup(ctx):
    events = []

    async def observe(event):
        events.append(event)

    return {'events': events, 'subscription': ctx.subscribe_group_lifecycle(observe)}
