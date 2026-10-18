"""Independent sample consumer; no account-sharing code or data."""


def setup(ctx):
    events = []

    async def observe(event):
        events.append(event)

    return {'events': events, 'subscription': ctx.subscribe_group_lifecycle(observe)}
