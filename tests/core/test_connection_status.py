"""连接状态通过实际 socket 消息循环接线；所有身份和平台响应均为样例。"""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from core.application import Application
from core.module.manager import ModuleContext
from core.network.websocket import WSClient


class Socket:
    """可控制断开和逐帧读取的网络替身，不调用状态通知接口。"""

    def __init__(self):
        self.frames = asyncio.Queue()
        self.sent = []

    def feed(self, op, *, t=None, s=None, d=None):
        self.frames.put_nowait(json.dumps({"op": op, "t": t, "s": s, "d": d}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self.frames.get()
        if isinstance(frame, Exception):
            raise frame
        if frame is None:
            raise StopAsyncIteration
        return frame

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def close(self):
        self.frames.put_nowait(None)


class Platform:
    """模拟获取官方网关及 socket 建连，不访问平台。"""

    api_base = "https://api.bot.qq.com"

    def __init__(self, monkeypatch):
        self.sockets = asyncio.Queue()
        self.opened = []
        monkeypatch.setattr("core.network.websocket.websockets.connect", self.connect)

    async def get_token(self):
        return "sample-token"

    async def get_client(self):
        return self

    async def get(self, url, **kwargs):
        return SimpleNamespace(json=lambda: {"url": "wss://api.bot.qq.com/websocket/"})

    @asynccontextmanager
    async def connect(self, url, **kwargs):
        socket = await self.sockets.get()
        self.opened.append(socket)
        try:
            yield socket
        finally:
            await socket.close()

    def add(self):
        socket = Socket()
        self.sockets.put_nowait(socket)
        return socket


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.fixture
async def wired(monkeypatch, tmp_path):
    app = Application()
    hooks = app.hook_manager
    app._bot_registry = SimpleNamespace(bots={"sample-bot": SimpleNamespace()})
    app._bot_registry.get = app._bot_registry.bots.get
    ctx = ModuleContext("sample-consumer", str(tmp_path), hooks)
    platform = Platform(monkeypatch)
    events = []

    async def on_event(event):
        await hooks.group_lifecycle.wait(event)
        events.append(event)

    client = WSClient(
        "sample-bot",
        platform,
        on_event,
        ack_interaction=on_event,
        reconnect_interval=0,
        connection_status=hooks.connection_status,
        on_event_received=app._prepare_event,
    )
    task = asyncio.create_task(client.connect())
    try:
        yield ctx, platform, client, events
    finally:
        await client.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_first_login_status_is_queryable_and_notifies(wired):
    ctx, platform, client, events = wired
    changes = []

    async def observed(status):
        changes.append(status)

    subscription = ctx.subscribe_connection_status(observed, appid="sample-bot")
    socket = platform.add()
    socket.feed(10, d={"heartbeat_interval": 45000})
    await until(lambda: any(frame["op"] == 2 for frame in socket.sent))
    socket.feed(0, t="READY", s=1, d={"session_id": "sample-session"})
    await until(lambda: any(s.phase == "ready" for s in changes))
    status = ctx.get_connection_status("sample-bot")
    assert status.phase == "ready"
    assert status.login_kind == "first"
    assert status.continuity == "unknown"
    assert status.context.session_id == "sample-session"
    assert status.context.session_generation
    assert ctx.connection_status_api_version == 1
    assert ctx.get_connection_capabilities("sample-bot").replay_boundary is True
    assert subscription.active
    assert events == []


async def login(ctx, platform):
    socket = platform.add()
    socket.feed(10, d={"heartbeat_interval": 45000})
    await until(lambda: any(frame["op"] == 2 for frame in socket.sent))
    socket.feed(0, t="READY", s=1, d={"session_id": "sample-session"})
    await until(lambda: ctx.get_connection_status("sample-bot").phase == "ready")
    return socket


async def test_resume_reports_server_boundary_while_local_processing_is_pending(
    wired, ws_task_factory
):
    ctx, platform, client, events = wired
    changes = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def observed(status):
        changes.append(status)

    async def slow_member(event):
        entered.set()
        await release.wait()

    ctx.subscribe_connection_status(observed, replay_current=False)
    members = ctx.subscribe_group_lifecycle(slow_member)
    first = await login(ctx, platform)
    original = ctx.get_connection_status("sample-bot").context
    first.frames.put_nowait(OSError("sample socket loss"))
    second = platform.add()
    second.feed(10, d={"heartbeat_interval": 45000})
    await until(lambda: any(frame["op"] == 6 for frame in second.sent))
    resuming = ctx.get_connection_status("sample-bot")
    assert resuming.phase == "resuming"
    assert resuming.continuity == "pending"
    assert resuming.resume_from_sequence == 1
    assert resuming.context.session_generation == original.session_generation
    assert resuming.context.connection_generation != original.connection_generation
    assert resuming.context.resume_attempt_id
    second.feed(
        0,
        t="GROUP_MEMBER_REMOVE",
        s=2,
        d={"group_openid": "sample-group", "member_openid": "sample-member"},
    )
    await until(entered.is_set)
    second.feed(0, t="RESUMED", s=3, d="")
    await until(
        lambda: ctx.get_connection_status("sample-bot").phase == "replay_complete"
    )
    boundary = ctx.get_connection_status("sample-bot")
    assert boundary.continuity == "resumed"
    assert boundary.replay_end_sequence == 3
    assert boundary.login_kind == "resume"
    assert boundary.context.session_generation == original.session_generation
    assert events == []
    assert members.results == ()
    release.set()
    await until(lambda: bool(events))
    event = events[0]
    assert (
        event.connection_context.resume_attempt_id == boundary.context.resume_attempt_id
    )
    assert event.connection_context.receive_index < boundary.context.receive_index
    assert event.connection_context.sequence == 2
    assert (
        event.group_lifecycle_delivery.event.connection_context
        == event.connection_context
    )
    await until(lambda: any(s.phase == "replay_complete" for s in changes))
    assert any(s.phase == "disconnected" for s in changes)
    assert any(s.phase == "reconnecting" for s in changes)
    assert [f["d"]["seq"] for f in second.sent if f["op"] == 6] == [1]


class HttpPlatform:
    """仅替换 HTTP 传输，保留真实 TokenManager、BotInstance 和存储启停。"""

    def __init__(self, *args, **kwargs):
        self.is_closed = False

    async def post(self, url, **kwargs):
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"access_token": "sample-token", "expires_in": 7200},
        )

    async def get(self, url, **kwargs):
        data = (
            {"url": "wss://api.bot.qq.com/websocket/"}
            if url.endswith("/gateway/bot")
            else {"id": "sample-id", "username": "sample-bot"}
        )
        return SimpleNamespace(status_code=200, json=lambda: data)

    async def aclose(self):
        self.is_closed = True


@pytest.fixture
async def registry_env(sample_config_dir, tmp_path, monkeypatch):
    from core.base.config import cfg
    from core.bot.registry import BotRegistry

    cfg._ready = False
    cfg.init(sample_config_dir)
    monkeypatch.setattr("core.network.access.AsyncHttpClient", HttpPlatform)
    app = Application()
    events = []

    async def on_event(event):
        events.append(event)

    registry = BotRegistry(
        str(tmp_path / "log"),
        on_event=on_event,
        on_event_received=app._prepare_event,
        connection_status=app.hook_manager.connection_status,
    )
    app._bot_registry = registry
    ctx = ModuleContext("sample-module", str(tmp_path), app.hook_manager)
    try:
        yield app, registry, ctx, events
    finally:
        await registry.shutdown()


async def test_registry_webhook_has_unknown_continuity_without_ws_events(registry_env):
    from core.server.webhook import WebhookHandler

    app, registry, ctx, events = registry_env
    changes = []

    async def observed(status):
        changes.append(status)

    ctx.subscribe_connection_status(observed)
    await registry.start_all()
    status = ctx.get_connection_status("123456", transport="webhook")
    assert status.phase == "unknown"
    assert ctx.get_connection_status("123456") is None
    assert not status.capabilities.transport_disconnect
    assert not status.capabilities.session_resume
    assert not status.capabilities.replay_boundary

    class Request:
        headers = {"X-Bot-Appid": "123456"}
        query = {}

        async def read(self):
            return json.dumps({"op": 0, "t": "RESUMED", "s": 99, "d": {}}).encode()

    async def receive(event):
        events.append(event)

    response = await WebhookHandler(registry, receive).handle(Request())
    assert response.status == 200
    await until(lambda: bool(events))
    assert events[0].connection_context.transport == "webhook"
    assert events[0].connection_context.session_generation is None
    assert (
        ctx.get_connection_status("123456", transport="webhook").continuity == "unknown"
    )
    await registry.shutdown()
    await until(lambda: any(c.phase == "stopped" for c in changes))
    assert not any(
        c.phase in {"disconnected", "resuming", "replay_complete"} for c in changes
    )


async def test_invalid_session_falls_back_to_identify_with_new_generation(wired):
    ctx, platform, client, events = wired
    changes = []

    async def observed(status):
        changes.append(status)

    ctx.subscribe_connection_status(observed, replay_current=False)
    first = await login(ctx, platform)
    original = ctx.get_connection_status("sample-bot").context
    first.feed(7)
    second = platform.add()
    second.feed(10, d={"heartbeat_interval": 45000})
    await until(lambda: any(f["op"] == 6 for f in second.sent))
    second.feed(9, d=False)
    await until(lambda: any(s.phase == "resume_failed" for s in changes))
    third = platform.add()
    third.feed(10, d={"heartbeat_interval": 45000})
    async with asyncio.timeout(5):
        while not any(f["op"] == 2 for f in third.sent):
            await asyncio.sleep(0.001)
    third.feed(0, t="READY", s=1, d={"session_id": "replacement-session"})
    await until(lambda: ctx.get_connection_status("sample-bot").phase == "ready")
    status = ctx.get_connection_status("sample-bot")
    assert status.login_kind == "new"
    assert status.context.session_generation != original.session_generation
    assert status.context.bot_generation == original.bot_generation
    assert status.context.runtime_id == original.runtime_id
    assert status.context.resume_attempt_id is None
    assert status.continuity == "unknown"
    assert not any(s.phase == "replay_complete" for s in changes)


async def test_ready_and_resumed_duplicates_do_not_create_new_sessions_or_boundaries(
    wired,
):
    ctx, platform, client, events = wired
    changes = []

    async def observed(status):
        changes.append(status)

    ctx.subscribe_connection_status(observed, replay_current=False)
    first = await login(ctx, platform)
    original = ctx.get_connection_status("sample-bot").context
    first.feed(0, t="READY", s=1, d={"session_id": "late-session"})
    first.feed(7)
    second = platform.add()
    second.feed(10, d={"heartbeat_interval": 45000})
    await until(lambda: any(f["op"] == 6 for f in second.sent))
    second.feed(0, t="RESUMED", s=2, d="")
    await until(
        lambda: ctx.get_connection_status("sample-bot").phase == "replay_complete"
    )
    boundary = ctx.get_connection_status("sample-bot")
    second.feed(0, t="RESUMED", s=2, d="")
    second.feed(11)
    await until(
        lambda: (
            ctx.get_connection_status("sample-bot").context.receive_index
            > boundary.context.receive_index
        )
    )
    assert ctx.get_connection_status("sample-bot").revision == boundary.revision
    assert (
        ctx.get_connection_status("sample-bot").context.session_generation
        == original.session_generation
    )
    await until(lambda: any(s.phase == "replay_complete" for s in changes))
    assert len([s for s in changes if s.phase == "ready"]) == 1
    assert len([s for s in changes if s.phase == "replay_complete"]) == 1
    assert [f["d"]["session_id"] for f in second.sent if f["op"] == 6] == [
        "sample-session"
    ]


async def test_late_subscription_replays_current_snapshot_and_cancel_stops_delivery(
    wired,
):
    ctx, platform, client, events = wired
    await login(ctx, platform)
    changes = []

    async def observed(status):
        changes.append(status)

    current = ctx.get_connection_status("sample-bot")
    subscription = ctx.subscribe_connection_status(observed)
    await until(lambda: bool(subscription.results))
    assert len(changes) == 1
    assert changes[0].is_snapshot
    assert changes[0].revision == current.revision
    assert not current.is_snapshot
    subscription.cancel()
    subscription.cancel()
    await client.close()
    assert len(changes) == 1
    assert ctx.get_connection_status("sample-bot").phase == "stopped"
    assert ctx.get_connection_status("sample-bot").continuity == "unknown"


async def test_closed_client_reconnect_uses_new_session_within_same_bot(wired):
    ctx, platform, client, events = wired
    await login(ctx, platform)
    original = ctx.get_connection_status("sample-bot").context
    await client.close()
    await until(lambda: client._connect_task is None)
    task = asyncio.create_task(client.connect())
    try:
        await login(ctx, platform)
        status = ctx.get_connection_status("sample-bot")
        assert status.login_kind == "new"
        assert status.context.bot_generation == original.bot_generation
        assert status.context.session_generation != original.session_generation
    finally:
        await client.close()
        await asyncio.gather(task, return_exceptions=True)


async def test_registry_ws_toggle_keeps_bot_identity_but_replaces_session(
    registry_env, monkeypatch
):
    from core.base.config import cfg

    app, registry, ctx, events = registry_env
    platform = Platform(monkeypatch)
    cfg.set_bot_setting("123456", "websocket.enabled", True)
    await registry.start_all()
    first = platform.add()
    first.feed(10, d={"heartbeat_interval": 45000})
    await until(lambda: any(f["op"] == 2 for f in first.sent))
    first.feed(0, t="READY", s=1, d={"session_id": "one"})
    await until(lambda: ctx.get_connection_status("123456").phase == "ready")
    original = ctx.get_connection_status("123456").context
    cfg.set_bot_setting("123456", "websocket.enabled", False)
    await registry._sync()
    assert ctx.get_connection_status("123456").phase == "stopped"
    cfg.set_bot_setting("123456", "websocket.enabled", True)
    await registry._sync()
    second = platform.add()
    second.feed(10, d={"heartbeat_interval": 45000})
    await until(lambda: any(f["op"] == 2 for f in second.sent))
    second.feed(0, t="READY", s=1, d={"session_id": "two"})
    await until(lambda: ctx.get_connection_status("123456").phase == "ready")
    status = ctx.get_connection_status("123456")
    assert status.login_kind == "new"
    assert status.context.bot_generation == original.bot_generation
    assert status.context.session_generation != original.session_generation
    assert status.context.connection_generation != original.connection_generation
    assert ctx.get_connection_status("123456", transport="webhook").phase == "unknown"


@pytest.mark.parametrize("gateway", ["custom_url", "custom_api", "custom_discovery"])
async def test_custom_gateway_cannot_assert_replay_completion(
    monkeypatch, tmp_path, gateway
):
    platform = Platform(monkeypatch)
    if gateway == "custom_api":
        platform.api_base = "https://gateway.example.invalid"
    if gateway == "custom_discovery":

        async def get(url, **kwargs):
            return SimpleNamespace(
                json=lambda: {"url": "wss://gateway.example.invalid/websocket/"}
            )

        platform.get = get
    app = Application()
    ctx = ModuleContext("sample", str(tmp_path), app.hook_manager)

    async def receive(event):
        pass

    client = WSClient(
        "sample-bot",
        platform,
        receive,
        ack_interaction=receive,
        connection_status=app.hook_manager.connection_status,
        reconnect_interval=0,
        custom_url="wss://api.bot.qq.com/websocket/" if gateway == "custom_url" else "",
    )
    task = asyncio.create_task(client.connect())
    try:
        first = await login(ctx, platform)
        first.feed(7)
        second = platform.add()
        second.feed(10, d={"heartbeat_interval": 45000})
        await until(lambda: any(f["op"] == 6 for f in second.sent))
        assert ctx.get_connection_status("sample-bot").continuity == "unknown"
        second.feed(0, t="RESUMED", s=2, d="")
        await until(
            lambda: ctx.get_connection_status("sample-bot").reason == "unproven_resumed"
        )
        status = ctx.get_connection_status("sample-bot")
        assert status.phase == "unknown"
        assert not status.capabilities.replay_boundary
        assert status.replay_end_sequence is None
    finally:
        await client.close()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("sequence", [None, True, "4", -1, 0])
async def test_invalid_replay_boundary_is_unknown(wired, sequence):
    ctx, platform, client, events = wired
    first = await login(ctx, platform)
    first.feed(7)
    second = platform.add()
    second.feed(10, d={"heartbeat_interval": 45000})
    await until(lambda: any(f["op"] == 6 for f in second.sent))
    second.feed(0, t="RESUMED", s=sequence, d="")
    await until(lambda: ctx.get_connection_status("sample-bot").phase != "resuming")
    status = ctx.get_connection_status("sample-bot")
    assert status.phase == "unknown"
    assert status.continuity == "unknown"
    assert status.replay_end_sequence is None


async def test_subscriber_error_timeout_and_other_bot_are_isolated(wired):
    ctx, platform, client, events = wired
    release = asyncio.Event()
    entered = asyncio.Event()
    observed = []

    async def failing(status):
        raise ValueError("sample exception")

    async def stubborn(status):
        if status.appid == "sample-bot":
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
        observed.append(status.appid)

    async def healthy(status):
        observed.append(status.phase)

    errors = ctx.subscribe_connection_status(failing, replay_current=False)
    slow = ctx.subscribe_connection_status(stubborn, timeout=0.02, replay_current=False)
    good = ctx.subscribe_connection_status(healthy, replay_current=False)
    await login(ctx, platform)
    await until(entered.is_set)
    await until(lambda: any(r.outcome == "timeout" for r in slow.results))
    await until(lambda: any(r.outcome == "error" for r in errors.results))
    assert ctx.get_connection_status("sample-bot").phase == "ready"
    second_client = WSClient(
        "other-bot",
        platform,
        healthy,
        ack_interaction=healthy,
        connection_status=client.connection_status,
        reconnect_interval=0,
    )
    second_task = asyncio.create_task(second_client.connect())
    try:
        socket = platform.add()
        socket.feed(10, d={"heartbeat_interval": 45000})
        socket.feed(0, t="READY", s=1, d={"session_id": "other-session"})
        await until(lambda: "other-bot" in observed)
        await until(lambda: any(r.status.phase == "ready" for r in good.results))
        assert any(r.outcome == "busy" for r in slow.results)
        assert all(r.error_type == "ValueError" for r in errors.results)
        await until(
            lambda: any(
                r.outcome == "success" and r.status.appid == "other-bot"
                for r in slow.results
            )
        )
    finally:
        release.set()
        await second_client.close()
        await asyncio.gather(second_task, return_exceptions=True)


async def test_module_reload_unloads_subscription_and_replays_current_state(
    wired, tmp_path
):
    from core.module.manager import ModuleManager

    ctx, platform, client, events = wired
    await login(ctx, platform)
    module_dir = tmp_path / "modules" / "sample_status"
    module_dir.mkdir(parents=True)
    (module_dir / "main.py").write_text(
        """
from types import SimpleNamespace
async def setup(ctx):
    seen = []
    async def observe(status):
        seen.append(status)
    subscription = ctx.subscribe_connection_status(observe)
    return SimpleNamespace(ctx=ctx, seen=seen, subscription=subscription)
""",
        encoding="utf-8",
    )
    modules = ModuleManager(str(module_dir.parent), ctx._hooks)
    modules.discover()
    try:
        assert await modules.enable("sample_status")
        first = modules.get("sample_status")
        await until(lambda: bool(first.subscription.results))
        assert first.seen[0].phase == "ready"
        assert first.seen[0].is_snapshot
        assert await modules.reload("sample_status")
        assert not first.subscription.active
        with pytest.raises(RuntimeError, match="unloaded"):
            first.ctx.subscribe_connection_status(first.seen.append)
        second = modules.get("sample_status")
        await until(lambda: bool(second.subscription.results))
        assert second.subscription.subscription_id != first.subscription.subscription_id
        await client.close()
        await until(lambda: any(s.phase == "stopped" for s in second.seen))
        assert len(first.seen) == 1
    finally:
        await modules.shutdown()


@pytest.mark.parametrize("failure", ["exception", "cancelled"])
async def test_failed_module_setup_removes_connection_subscription(
    wired, tmp_path, failure
):
    from core.module.manager import ModuleManager

    ctx, platform, client, events = wired
    await login(ctx, platform)
    module_dir = tmp_path / "modules" / "sample_failed"
    module_dir.mkdir(parents=True)
    error = "ValueError()" if failure == "exception" else "asyncio.CancelledError()"
    (module_dir / "main.py").write_text(
        f"""
import asyncio
async def setup(ctx):
    async def observe(status):
        pass
    ctx.subscribe_connection_status(observe)
    raise {error}
""",
        encoding="utf-8",
    )
    modules = ModuleManager(str(module_dir.parent), ctx._hooks)
    modules.discover()
    if failure == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await modules.enable("sample_failed")
    else:
        assert not await modules.enable("sample_failed")
    assert not ctx._hooks.connection_status._subscriptions


async def test_superseded_socket_and_bot_late_ready_cannot_replace_current_status(
    wired,
):
    ctx, platform, old_client, events = wired
    first = await login(ctx, platform)
    original = ctx.get_connection_status("sample-bot")

    async def receive(event):
        pass

    replacement = WSClient(
        "sample-bot",
        platform,
        receive,
        ack_interaction=receive,
        connection_status=old_client.connection_status,
    )
    task = asyncio.create_task(replacement.connect())
    try:
        second = platform.add()
        second.feed(10, d={"heartbeat_interval": 45000})
        second.feed(0, t="READY", s=1, d={"session_id": "new-bot-session"})
        await until(lambda: ctx.get_connection_status("sample-bot").phase == "ready")
        status = ctx.get_connection_status("sample-bot")
        assert status.context.bot_generation != original.context.bot_generation
        assert status.login_kind == "first"
        first.feed(0, t="RESUMED", s=999, d="")
        first.feed(0, t="READY", s=999, d={"session_id": "old-bot-late-session"})
        await until(lambda: first.frames.empty())
        assert ctx.get_connection_status("sample-bot") == status
    finally:
        await replacement.close()
        await asyncio.gather(task, return_exceptions=True)


async def test_heartbeat_uses_received_sequence_while_subscriber_is_pending(wired):
    ctx, platform, client, events = wired
    release = asyncio.Event()

    async def slow(status):
        await release.wait()

    subscription = ctx.subscribe_connection_status(
        slow, timeout=2, replay_current=False
    )
    socket = platform.add()
    socket.feed(10, d={"heartbeat_interval": 5})
    socket.feed(0, t="READY", s=1, d={"session_id": "sample-session"})
    socket.feed(
        0,
        t="C2C_MESSAGE_CREATE",
        s=15,
        d={
            "id": "sample-message",
            "content": "sample",
            "author": {"id": "sample-user"},
        },
    )
    await until(lambda: any(f["op"] == 1 and f["d"] == 15 for f in socket.sent))
    assert subscription.results == ()
    assert ctx.get_connection_status("sample-bot").phase == "ready"
    release.set()
    await until(lambda: bool(subscription.results))


async def test_resume_interrupted_before_resumed_never_claims_continuity(wired):
    ctx, platform, client, events = wired
    changes = []

    async def observed(status):
        changes.append(status)

    ctx.subscribe_connection_status(observed, replay_current=False)
    first = await login(ctx, platform)
    first.feed(7)
    second = platform.add()
    second.feed(10, d={"heartbeat_interval": 45000})
    await until(lambda: any(f["op"] == 6 for f in second.sent))
    attempt = ctx.get_connection_status("sample-bot").context.resume_attempt_id
    second.frames.put_nowait(OSError("interrupted replay"))
    await until(lambda: any(s.phase == "resume_failed" for s in changes))
    failure = next(s for s in changes if s.phase == "resume_failed")
    assert failure.context.resume_attempt_id == attempt
    assert failure.continuity == "unknown"
    assert not any(s.phase == "replay_complete" for s in changes)


async def test_reconnect_exhaustion_and_cancellation_are_observable(
    monkeypatch, tmp_path
):
    platform = Platform(monkeypatch)
    app = Application()
    ctx = ModuleContext("sample", str(tmp_path), app.hook_manager)

    @asynccontextmanager
    async def refuse(url, **kwargs):
        raise OSError("sample handshake failure")
        yield

    monkeypatch.setattr("core.network.websocket.websockets.connect", refuse)

    async def receive(event):
        pass

    client = WSClient(
        "sample-bot",
        platform,
        receive,
        ack_interaction=receive,
        connection_status=app.hook_manager.connection_status,
        max_reconnects=1,
    )
    try:
        await client.connect()
        status = ctx.get_connection_status("sample-bot")
        assert status.phase == "stopped"
        assert status.reason == "reconnect_exhausted"
        assert status.continuity == "unknown"
    finally:
        await client.close()


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
async def test_subscription_rejects_invalid_timeout(wired, timeout):
    ctx, *_ = wired

    async def observe(status):
        pass

    with pytest.raises(ValueError):
        ctx.subscribe_connection_status(observe, timeout=timeout)


async def test_callback_cancellation_and_unsubscribe_have_individual_results(wired):
    ctx, platform, client, events = wired
    entered = asyncio.Event()

    async def cancelled(status):
        raise asyncio.CancelledError

    async def pending(status):
        entered.set()
        await asyncio.Event().wait()

    cancelled_subscription = ctx.subscribe_connection_status(
        cancelled, replay_current=False
    )
    pending_subscription = ctx.subscribe_connection_status(
        pending, replay_current=False
    )
    await login(ctx, platform)
    await until(entered.is_set)
    pending_subscription.cancel()
    await until(lambda: bool(pending_subscription.results))
    await until(lambda: bool(cancelled_subscription.results))
    assert all(r.outcome == "unsubscribed" for r in pending_subscription.results)
    assert all(r.outcome == "cancelled" for r in cancelled_subscription.results)
    assert ctx.get_connection_status("sample-bot").phase == "ready"


async def test_unloaded_module_reports_inflight_unloaded_result(wired, tmp_path):
    from core.module.manager import ModuleManager

    ctx, platform, client, events = wired
    await login(ctx, platform)
    module_dir = tmp_path / "modules" / "sample_inflight"
    module_dir.mkdir(parents=True)
    (module_dir / "main.py").write_text(
        """
import asyncio
from types import SimpleNamespace
async def setup(ctx):
    entered = asyncio.Event()
    async def observe(status):
        entered.set()
        await asyncio.Event().wait()
    return SimpleNamespace(entered=entered, subscription=ctx.subscribe_connection_status(observe))
""",
        encoding="utf-8",
    )
    modules = ModuleManager(str(module_dir.parent), ctx._hooks)
    modules.discover()
    assert await modules.enable("sample_inflight")
    sample = modules.get("sample_inflight")
    await until(sample.entered.is_set)
    await modules.disable("sample_inflight")
    await until(lambda: bool(sample.subscription.results))
    assert sample.subscription.results[0].outcome == "unloaded"
    assert not sample.subscription.active


async def test_runtime_restart_identity_and_subscription_filters(wired, tmp_path):
    ctx, platform, client, events = wired
    await login(ctx, platform)
    original = ctx.get_connection_status("sample-bot").context
    restarted = Application()
    other_ctx = ModuleContext("restart-sample", str(tmp_path), restarted.hook_manager)
    filtered = []

    async def observe(status):
        filtered.append(status)

    other_ctx.subscribe_connection_status(observe, appid="unrelated-bot")
    other_ctx.subscribe_connection_status(observe, transport="webhook")
    other_client = WSClient(
        "sample-bot",
        platform,
        observe,
        ack_interaction=observe,
        connection_status=restarted.hook_manager.connection_status,
    )
    task = asyncio.create_task(other_client.connect())
    try:
        await login(other_ctx, platform)
        status = other_ctx.get_connection_status("sample-bot")
        assert status.login_kind == "first"
        assert status.continuity == "unknown"
        assert status.context.runtime_id != original.runtime_id
        assert status.context.bot_generation != original.bot_generation
        assert status.context.session_generation != original.session_generation
        assert filtered == []
        assert other_ctx.connection_statuses == (status,)
        assert other_ctx.get_connection_capabilities("unknown-bot") is None
    finally:
        await other_client.close()
        await asyncio.gather(task, return_exceptions=True)


async def test_invalid_ready_and_unsolicited_resumed_never_claim_session_continuity(
    wired,
):
    ctx, platform, client, events = wired
    socket = platform.add()
    socket.feed(10, d={"heartbeat_interval": 45000})
    socket.feed(0, t="READY", s=1, d={"session_id": 42})
    await until(
        lambda: ctx.get_connection_status("sample-bot").reason == "invalid_ready"
    )
    assert ctx.get_connection_status("sample-bot").context.session_generation is None
    socket.feed(0, t="RESUMED", s=2, d="")
    await until(
        lambda: ctx.get_connection_status("sample-bot").reason == "unexpected_resumed"
    )
    assert ctx.get_connection_status("sample-bot").continuity == "unknown"


async def test_start_failure_is_stopped_and_does_not_leave_unknown_running_bot(
    registry_env, monkeypatch
):
    app, registry, ctx, events = registry_env

    async def refuse(self, url, **kwargs):
        return SimpleNamespace(
            status_code=400, json=lambda: {"error": "sample rejection"}
        )

    monkeypatch.setattr(HttpPlatform, "post", refuse)
    monkeypatch.setattr("core.network.access._RETRY_DELAYS", (0, 0, 0))
    await registry.start_all()
    assert len(registry) == 0
    status = ctx.get_connection_status("123456", transport="webhook")
    assert status.phase == "stopped"
    assert status.reason == "start_failed"


@pytest.mark.parametrize("frame", ["{broken", "[]"])
async def test_malformed_replay_frame_invalidates_continuity(wired, frame):
    ctx, platform, client, events = wired
    first = await login(ctx, platform)
    first.feed(7)
    second = platform.add()
    second.feed(10, d={"heartbeat_interval": 45000})
    await until(lambda: any(f["op"] == 6 for f in second.sent))
    second.frames.put_nowait(frame)
    second.feed(0, t="RESUMED", s=2, d="")
    await until(lambda: ctx.get_connection_status("sample-bot").phase != "resuming")
    assert ctx.get_connection_status("sample-bot").continuity == "unknown"
    assert ctx.get_connection_status("sample-bot").replay_end_sequence is None


async def test_close_waits_for_pending_connect_and_prevents_late_frames(
    monkeypatch, tmp_path
):
    platform = Platform(monkeypatch)
    app = Application()
    ctx = ModuleContext("sample", str(tmp_path), app.hook_manager)

    async def receive(event):
        pass

    client = WSClient(
        "sample-bot",
        platform,
        receive,
        ack_interaction=receive,
        connection_status=app.hook_manager.connection_status,
    )
    task = asyncio.create_task(client.connect())
    await until(lambda: ctx.get_connection_status("sample-bot").phase == "connecting")
    try:
        await client.close()
        assert task.done()
        status = ctx.get_connection_status("sample-bot")
        socket = platform.add()
        socket.feed(0, t="READY", s=1, d={"session_id": "late-after-stop"})
        await asyncio.sleep(0)
        assert ctx.get_connection_status("sample-bot") == status
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_normal_socket_eof_reports_disconnect_and_reconnects(wired):
    ctx, platform, client, events = wired
    changes = []

    async def observe(status):
        changes.append(status)

    ctx.subscribe_connection_status(observe, replay_current=False)
    first = await login(ctx, platform)
    first.frames.put_nowait(None)
    second = platform.add()
    second.feed(10, d={"heartbeat_interval": 45000})
    await until(lambda: any(f["op"] == 6 for f in second.sent))
    await until(lambda: any(s.phase == "disconnected" for s in changes))
    disconnected = next(s for s in changes if s.phase == "disconnected")
    assert disconnected.reason == "socket_eof"
    assert disconnected.continuity == "unknown"
    assert ctx.get_connection_status("sample-bot").phase == "resuming"


async def test_resumable_invalid_session_retries_same_session_with_new_attempt(wired):
    ctx, platform, client, events = wired
    first = await login(ctx, platform)
    original = ctx.get_connection_status("sample-bot").context
    first.feed(7)
    second = platform.add()
    second.feed(10, d={"heartbeat_interval": 45000})
    await until(lambda: any(f["op"] == 6 for f in second.sent))
    attempt = ctx.get_connection_status("sample-bot").context.resume_attempt_id
    second.feed(9, d=True)
    await until(
        lambda: ctx.get_connection_status("sample-bot").phase == "resume_failed"
    )
    third = platform.add()
    third.feed(10, d={"heartbeat_interval": 45000})
    async with asyncio.timeout(5):
        while not any(f["op"] == 6 for f in third.sent):
            await asyncio.sleep(0.001)
    current = ctx.get_connection_status("sample-bot")
    assert current.context.resume_attempt_id != attempt
    assert current.context.session_generation == original.session_generation
    assert [f["d"]["session_id"] for f in third.sent if f["op"] == 6] == [
        "sample-session"
    ]
    third.feed(0, t="RESUMED", s=2, d="")
    await until(
        lambda: ctx.get_connection_status("sample-bot").phase == "replay_complete"
    )
    assert ctx.get_connection_status("sample-bot").continuity == "resumed"


async def test_registry_disable_before_scheduled_connect_runs_stays_stopped(
    registry_env, monkeypatch
):
    from core.base.config import cfg

    app, registry, ctx, events = registry_env
    platform = Platform(monkeypatch)
    await registry.start_all()
    cfg.set_bot_setting("123456", "websocket.enabled", True)
    await registry._sync()
    cfg.set_bot_setting("123456", "websocket.enabled", False)
    await registry._sync()
    stopped = ctx.get_connection_status("123456")
    assert stopped.phase == "stopped"
    assert registry.get("123456").ws_client is None
    socket = platform.add()
    socket.feed(10, d={"heartbeat_interval": 45000})
    socket.feed(0, t="READY", s=1, d={"session_id": "after-disable"})
    # 先让排队连接得到执行机会，再检查公开停机事实。
    await asyncio.sleep(0.02)
    assert ctx.get_connection_status("123456") == stopped
    assert socket.sent == []
    assert platform.opened == []
    assert events == []


@pytest.mark.parametrize("previously_started", [False, True])
async def test_superseded_client_queued_connect_cannot_reclaim_current_source(
    monkeypatch, tmp_path, previously_started
):
    app = Application()
    ctx = ModuleContext("sample", str(tmp_path), app.hook_manager)
    platform = Platform(monkeypatch)

    async def receive(event):
        pass

    def make_client():
        return WSClient(
            "sample-bot",
            platform,
            receive,
            ack_interaction=receive,
            connection_status=app.hook_manager.connection_status,
        )

    old = make_client()
    if previously_started:
        initial_task = asyncio.create_task(old.connect())
        await login(ctx, platform)
        await old.close()
        await asyncio.gather(initial_task, return_exceptions=True)
    queued = old.connect()
    replacement = make_client()
    current_task = asyncio.create_task(replacement.connect())
    old_task = None
    try:
        await login(ctx, platform)
        current = ctx.get_connection_status("sample-bot")
        old_task = asyncio.create_task(queued)
        await asyncio.sleep(0.02)
        assert ctx.get_connection_status("sample-bot") == current
        assert old_task.done()
        assert old_task.exception() is None
        assert len(platform.opened) == (2 if previously_started else 1)
    finally:
        await old.close()
        await replacement.close()
        if old_task is None:
            queued.close()
        await asyncio.gather(
            *[t for t in (old_task, current_task) if t is not None],
            return_exceptions=True,
        )


async def test_exhausted_client_restart_resumes_with_matching_public_session(
    monkeypatch, tmp_path
):
    app = Application()
    ctx = ModuleContext("sample", str(tmp_path), app.hook_manager)
    platform = Platform(monkeypatch)
    events = []

    async def receive(event):
        events.append(event)

    client = WSClient(
        "sample-bot",
        platform,
        receive,
        ack_interaction=receive,
        connection_status=app.hook_manager.connection_status,
        max_reconnects=1,
        reconnect_interval=0,
    )
    task = asyncio.create_task(client.connect())
    restarted = None
    try:
        first = await login(ctx, platform)
        original = ctx.get_connection_status("sample-bot").context
        first.frames.put_nowait(OSError("sample socket loss"))
        await until(task.done)
        assert task.exception() is None
        assert ctx.get_connection_status("sample-bot").reason == "reconnect_exhausted"
        restarted = asyncio.create_task(client.connect())
        second = platform.add()
        second.feed(10, d={"heartbeat_interval": 45000})
        await until(lambda: any(f["op"] == 6 for f in second.sent))
        resuming = ctx.get_connection_status("sample-bot")
        assert resuming.phase == "resuming"
        assert resuming.continuity == "pending"
        assert resuming.context.session_id == "sample-session"
        assert resuming.context.session_generation == original.session_generation
        assert resuming.context.bot_generation == original.bot_generation
        assert resuming.context.connection_generation != original.connection_generation
        assert [f["d"]["session_id"] for f in second.sent if f["op"] == 6] == [
            "sample-session"
        ]
        second.feed(0, t="RESUMED", s=2, d="")
        await until(lambda: ctx.get_connection_status("sample-bot").phase != "resuming")
        status = ctx.get_connection_status("sample-bot")
        assert status.phase == "replay_complete"
        assert status.continuity == "resumed"
        assert status.context.session_id == "sample-session"
        assert status.context.session_generation == original.session_generation
        second.feed(
            0, t="C2C_MESSAGE_CREATE", s=3,
            d={"id": "after-restart", "content": "sample", "author": {"id": "sample-user"}},
        )
        await until(lambda: bool(events))
        assert events[0].connection_context.session_id == "sample-session"
        assert events[0].connection_context.session_generation == original.session_generation
    finally:
        await client.close()
        await asyncio.gather(
            *[t for t in (task, restarted) if t is not None], return_exceptions=True
        )


@pytest.mark.parametrize("previously_started", [False, True])
@pytest.mark.parametrize("schedule_before_close", [False, True])
async def test_close_invalidates_created_connect_and_allows_intentional_restart(
    monkeypatch, tmp_path, previously_started, schedule_before_close
):
    app = Application()
    ctx = ModuleContext("sample", str(tmp_path), app.hook_manager)
    platform = Platform(monkeypatch)

    async def receive(event):
        pass

    client = WSClient(
        "sample-bot", platform, receive, ack_interaction=receive,
        connection_status=app.hook_manager.connection_status,
    )
    original = None
    if previously_started:
        first_task = asyncio.create_task(client.connect())
        await login(ctx, platform)
        original = ctx.get_connection_status("sample-bot").context
        await client.close()
        await asyncio.gather(first_task, return_exceptions=True)
    queued = client.connect()
    stale_task = asyncio.create_task(queued) if schedule_before_close else None
    await client.close()
    stopped = ctx.get_connection_status("sample-bot")
    assert stopped.phase == "stopped"
    socket = platform.add()
    socket.feed(10, d={"heartbeat_interval": 45000})
    socket.feed(0, t="READY", s=1, d={"session_id": "intentional-new-session"})
    restarted = None
    try:
        if stale_task is None:
            stale_task = asyncio.create_task(queued)
        await asyncio.sleep(0.02)
        assert stale_task.done()
        assert stale_task.exception() is None
        assert ctx.get_connection_status("sample-bot") == stopped
        assert socket.sent == []
        restarted = asyncio.create_task(client.connect())
        await until(lambda: ctx.get_connection_status("sample-bot").phase == "ready")
        status = ctx.get_connection_status("sample-bot")
        assert [f["op"] for f in socket.sent] == [2]
        assert status.context.session_id == "intentional-new-session"
        assert status.login_kind == ("new" if previously_started else "first")
        if original:
            assert status.context.bot_generation == original.bot_generation
            assert status.context.session_generation != original.session_generation
    finally:
        await client.close()
        await asyncio.gather(
            *[t for t in (stale_task, restarted) if t is not None],
            return_exceptions=True,
        )


async def test_duplicate_hello_preserves_resume_round_without_proving_boundary(wired):
    ctx, platform, client, events = wired
    first = await login(ctx, platform)
    first.feed(7)
    second = platform.add()
    second.feed(10, d={"heartbeat_interval": 45000})
    await until(lambda: any(frame["op"] == 6 for frame in second.sent))
    resuming = ctx.get_connection_status("sample-bot")
    second.feed(
        0,
        t="C2C_MESSAGE_CREATE",
        s=2,
        d={"content": "sample", "author": {"id": "sample-author"}},
    )
    await until(lambda: len(events) == 1)
    second.feed(10, d={"heartbeat_interval": 45000})
    await until(
        lambda: ctx.get_connection_status("sample-bot").context.receive_index
        > events[0].connection_context.receive_index
    )
    unexpected = ctx.get_connection_status("sample-bot")
    assert unexpected.phase == "unknown"
    assert unexpected.reason == "unexpected_hello"
    assert unexpected.context.resume_attempt_id == resuming.context.resume_attempt_id
    assert unexpected.continuity == "unknown"
    assert [frame["d"]["seq"] for frame in second.sent if frame["op"] == 6] == [1]
    second.feed(0, t="RESUMED", s=3, d="")
    await until(lambda: ctx.get_connection_status("sample-bot").context.sequence == 3)
    boundary = ctx.get_connection_status("sample-bot")
    assert boundary.phase == "unknown"
    assert boundary.reason == "unproven_resumed"
    assert boundary.continuity == "unknown"
    assert boundary.replay_end_sequence is None
    assert boundary.context.resume_attempt_id == resuming.context.resume_attempt_id
    assert events[0].connection_context.resume_attempt_id == resuming.context.resume_attempt_id


@pytest.mark.parametrize("phase", ["identifying", "ready", "replay_complete"])
async def test_duplicate_hello_does_not_restart_authentication(wired, phase):
    ctx, platform, client, events = wired
    if phase == "identifying":
        socket = platform.add()
        socket.feed(10, d={"heartbeat_interval": 45000})
        await until(lambda: any(frame["op"] == 2 for frame in socket.sent))
    else:
        socket = await login(ctx, platform)
        if phase == "replay_complete":
            socket.feed(7)
            socket = platform.add()
            socket.feed(10, d={"heartbeat_interval": 45000})
            await until(lambda: any(frame["op"] == 6 for frame in socket.sent))
            socket.feed(0, t="RESUMED", s=2, d="")
            await until(
                lambda: ctx.get_connection_status("sample-bot").phase == phase
            )
    before = ctx.get_connection_status("sample-bot")
    sent = [frame for frame in socket.sent if frame["op"] in (2, 6)]
    socket.feed(10, d={"heartbeat_interval": 45000})
    await until(
        lambda: ctx.get_connection_status("sample-bot").context.receive_index
        > before.context.receive_index
    )
    unexpected = ctx.get_connection_status("sample-bot")
    assert unexpected.phase == "unknown"
    assert unexpected.reason == "unexpected_hello"
    assert unexpected.continuity == "unknown"
    assert unexpected.context.session_generation == before.context.session_generation
    assert unexpected.context.resume_attempt_id == before.context.resume_attempt_id
    assert [frame for frame in socket.sent if frame["op"] in (2, 6)] == sent


async def test_callback_argument_error_has_individual_results_without_losing_queue(wired):
    ctx, platform, client, events = wired
    seen = []

    async def incompatible(status, required):
        pass

    async def healthy(status):
        seen.append(status.phase)

    failed = ctx.subscribe_connection_status(incompatible, replay_current=False)
    good = ctx.subscribe_connection_status(healthy, replay_current=False)
    await login(ctx, platform)
    await until(lambda: any(result.status.phase == "ready" for result in good.results))
    assert seen[-1] == "ready"
    assert good.active
    assert failed.active
    assert failed.results
    assert [result.status.revision for result in failed.results] == [
        result.status.revision for result in good.results
    ]
    assert all(result.outcome == "error" for result in failed.results)
    assert all(result.error_type == "TypeError" for result in failed.results)
    assert all(result.outcome == "success" for result in good.results)


async def test_unknown_opcode_reports_without_preventing_later_resume_boundary(wired):
    ctx, platform, client, events = wired
    changes = []

    async def observed(status):
        changes.append(status)

    subscription = ctx.subscribe_connection_status(observed, replay_current=False)
    first = await login(ctx, platform)
    first.feed(7)
    second = platform.add()
    second.feed(10, d={"heartbeat_interval": 45000})
    await until(lambda: any(frame["op"] == 6 for frame in second.sent))
    resuming = ctx.get_connection_status("sample-bot")
    second.feed(42, s=2, d={"sample": "unknown extension"})
    await until(
        lambda: ctx.get_connection_status("sample-bot").context.sequence == 2
    )
    unknown = ctx.get_connection_status("sample-bot")
    assert unknown.phase == "unknown"
    assert unknown.reason == "unknown_opcode"
    assert unknown.continuity == "unknown"
    assert unknown.revision > resuming.revision
    assert unknown.context.resume_attempt_id == resuming.context.resume_attempt_id
    await until(lambda: any(s.revision == unknown.revision for s in changes))
    second.feed(11)
    second.feed(
        0, t="C2C_MESSAGE_CREATE", s=3,
        d={"content": "sample", "author": {"id": "sample-author"}},
    )
    await until(lambda: len(events) == 1)
    assert events[0].connection_context.resume_attempt_id == resuming.context.resume_attempt_id
    assert ctx.get_connection_status("sample-bot").revision == unknown.revision
    second.feed(0, t="RESUMED", s=4, d="")
    await until(lambda: ctx.get_connection_status("sample-bot").phase == "replay_complete")
    boundary = ctx.get_connection_status("sample-bot")
    assert boundary.continuity == "resumed"
    assert boundary.replay_end_sequence == 4
    assert boundary.context.resume_attempt_id == resuming.context.resume_attempt_id
    second.feed(42, s=5)
    await until(lambda: ctx.get_connection_status("sample-bot").context.sequence == 5)
    after = ctx.get_connection_status("sample-bot")
    assert after.phase == "unknown"
    assert after.reason == "unknown_opcode"
    assert after.continuity == "unknown"
    assert after.replay_end_sequence is None
    await until(lambda: any(r.status.revision == after.revision for r in subscription.results))
    second.feed(7)
    third = platform.add()
    third.feed(10, d={"heartbeat_interval": 45000})
    await until(lambda: any(frame["op"] == 6 for frame in third.sent))
    assert next(frame["d"]["seq"] for frame in third.sent if frame["op"] == 6) == 5
    third.feed(0, t="RESUMED", s=6, d="")
    await until(lambda: ctx.get_connection_status("sample-bot").phase == "replay_complete")
    recovered = ctx.get_connection_status("sample-bot")
    assert recovered.continuity == "resumed"
    assert recovered.context.resume_attempt_id != resuming.context.resume_attempt_id


@pytest.mark.parametrize(
    "opcode_fields",
    [
        pytest.param({}, id="missing"),
        pytest.param({"op": None}, id="null"),
        pytest.param({"op": False}, id="false-dispatch"),
        pytest.param({"op": True}, id="true-heartbeat"),
        pytest.param({"op": 0.0}, id="float-dispatch"),
        pytest.param({"op": 7.0}, id="float-reconnect"),
        pytest.param({"op": 9.0}, id="float-invalid-session"),
        pytest.param({"op": 10.0}, id="float-hello"),
        pytest.param({"op": 11.0}, id="float-ack"),
        pytest.param({"op": "0"}, id="string"),
        pytest.param({"op": []}, id="list"),
        pytest.param({"op": {}}, id="object"),
    ],
)
async def test_invalid_opcode_cannot_act_as_a_protocol_message(wired, opcode_fields):
    ctx, platform, client, events = wired
    changes = []

    async def observed(status):
        changes.append(status)

    ctx.subscribe_connection_status(observed, replay_current=False)
    first = await login(ctx, platform)
    first.feed(7)
    second = platform.add()
    second.feed(10, d={"heartbeat_interval": 45000})
    await until(lambda: any(frame["op"] == 6 for frame in second.sent))
    resuming = ctx.get_connection_status("sample-bot")
    second.frames.put_nowait(json.dumps({**opcode_fields, "t": "RESUMED", "s": 2, "d": ""}))
    await until(lambda: ctx.get_connection_status("sample-bot").context.sequence == 2)
    invalid = ctx.get_connection_status("sample-bot")
    assert invalid.phase == "unknown"
    assert invalid.reason == "invalid_opcode"
    assert invalid.continuity == "unknown"
    assert invalid.replay_end_sequence is None
    assert invalid.context.resume_attempt_id == resuming.context.resume_attempt_id
    await until(lambda: any(s.revision == invalid.revision for s in changes))
    assert events == []
    assert len(platform.opened) == 2
    assert [frame["op"] for frame in second.sent] == [6]
    second.feed(0, t="RESUMED", s=3, d="")
    await until(lambda: ctx.get_connection_status("sample-bot").context.sequence == 3)
    boundary = ctx.get_connection_status("sample-bot")
    assert boundary.phase == "replay_complete"
    assert boundary.continuity == "resumed"
    assert boundary.replay_end_sequence == 3
    assert boundary.context.resume_attempt_id == resuming.context.resume_attempt_id


@pytest.mark.parametrize("dependent_cleanup", [False, True])
@pytest.mark.parametrize("before_socket_open", [False, True])
async def test_close_preserves_connect_caller_and_does_not_wait_for_its_cleanup(
    monkeypatch, dependent_cleanup, before_socket_open
):
    platform = Platform(monkeypatch)
    platform.add()

    async def receive(event):
        pass

    client = WSClient("sample-bot", platform, receive, ack_interaction=receive)
    close_returned = asyncio.Event()
    starting = asyncio.Event()
    continued = []

    async def owner():
        try:
            starting.set()
            await client.connect()
            continued.append("after connect")
        finally:
            if dependent_cleanup:
                await close_returned.wait()

    async def stop():
        await client.close()
        close_returned.set()

    owner_task = asyncio.create_task(owner())
    stop_task = None
    restarted = None
    try:
        await starting.wait()
        if not before_socket_open:
            await until(lambda: bool(platform.opened))
        if before_socket_open:
            # 直接关闭，避免新建 stop 任务时连接循环先获得执行机会。
            async with asyncio.timeout(0.3):
                await stop()
        else:
            stop_task = asyncio.create_task(stop())
        _, pending = await asyncio.wait(
            [task for task in (owner_task, stop_task) if task is not None],
            timeout=0.3,
        )
        assert not pending, "close() must not wait for the caller's exit cleanup"
        assert not owner_task.cancelled(), "close() must not cancel the connect() caller"
        assert owner_task.exception() is None
        if stop_task is not None:
            assert stop_task.exception() is None
        assert continued == ["after connect"]
        opened_before_restart = len(platform.opened)
        platform.add()
        restarted = asyncio.create_task(client.connect())
        await until(lambda: len(platform.opened) == opened_before_restart + 1)
        await client.close()
        await restarted
    finally:
        close_returned.set()
        await client.close()
        await asyncio.gather(
            *[task for task in (owner_task, stop_task, restarted) if task is not None],
            return_exceptions=True,
        )


@pytest.mark.parametrize("before_socket_open", [False, True])
async def test_external_connect_cancellation_still_propagates_and_allows_restart(
    monkeypatch, before_socket_open
):
    platform = Platform(monkeypatch)
    platform.add()

    async def receive(event):
        pass

    client = WSClient("sample-bot", platform, receive, ack_interaction=receive)
    starting = asyncio.Event()
    continued = []

    async def owner():
        starting.set()
        await client.connect()
        continued.append("unexpected continuation")

    owner_task = asyncio.create_task(owner())
    restarted = None
    try:
        await starting.wait()
        if not before_socket_open:
            await until(lambda: bool(platform.opened))
        owner_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner_task
        assert continued == []
        assert client.connection_status.get("sample-bot").phase == "stopped"
        restarted = asyncio.create_task(client.connect())
        if not before_socket_open:
            platform.add()
        await until(lambda: len(platform.opened) == (1 if before_socket_open else 2))
        await client.close()
        await restarted
    finally:
        await client.close()
        await asyncio.gather(
            *[task for task in (owner_task, restarted) if task is not None],
            return_exceptions=True,
        )


@pytest.mark.parametrize("eager", [False, True], ids=["scheduled", "eager"])
async def test_immediately_exhausted_connection_can_be_started_again(eager):
    if eager and not hasattr(asyncio, "eager_task_factory"):
        pytest.skip("eager task factories require Python 3.12+")
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()

    async def receive(event):
        pass

    client = WSClient(
        "sample-bot", SimpleNamespace(api_base="https://api.bot.qq.com"),
        receive, ack_interaction=receive,
        custom_url="ws://sample.invalid:not-a-port", max_reconnects=1,
    )
    loop.set_task_factory(asyncio.eager_task_factory if eager else None)
    try:
        await client.connect()
        first = client.connection_status.get("sample-bot")
        assert first.phase == "stopped"
        assert first.reason == "reconnect_exhausted"
        await client.connect()
        second = client.connection_status.get("sample-bot")
        assert second.phase == "stopped"
        assert second.reason == "reconnect_exhausted"
        assert second.context.connection_generation != first.context.connection_generation
    finally:
        await client.close()
        loop.set_task_factory(previous_factory)


@pytest.mark.parametrize("eager", [False, True], ids=["scheduled", "eager"])
async def test_timed_out_subscriber_recovers_after_busy_without_resubscribing(
    monkeypatch, tmp_path, eager
):
    if eager and not hasattr(asyncio, "eager_task_factory"):
        pytest.skip("eager task factories require Python 3.12+")
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    platform = Platform(monkeypatch)
    app = Application()
    ctx = ModuleContext("sample", str(tmp_path), app.hook_manager)
    release, finished = asyncio.Event(), asyncio.Event()
    delivered = []

    async def receive(event):
        pass

    async def observe(status):
        delivered.append(status.phase)
        if len(delivered) == 1:
            try:
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
            finally:
                finished.set()

    client = WSClient(
        "sample-bot", platform, receive, ack_interaction=receive,
        connection_status=app.hook_manager.connection_status,
    )
    loop.set_task_factory(asyncio.eager_task_factory if eager else None)
    task = asyncio.create_task(client.connect())
    subscription = None
    try:
        first = await login(ctx, platform)
        subscription = ctx.subscribe_connection_status(observe, timeout=0.02)
        await until(lambda: any(r.outcome == "timeout" for r in subscription.results))
        first.feed(99, s=2)
        await until(lambda: any(r.outcome == "busy" for r in subscription.results))
        release.set()
        await finished.wait()
        first.feed(7, s=3)
        second = platform.add()
        second.feed(10, d={"heartbeat_interval": 45000})
        await until(lambda: any(f["op"] == 6 for f in second.sent))
        second.feed(0, t="RESUMED", s=4, d="")
        await until(lambda: "replay_complete" in delivered)
        assert subscription.active
        assert subscription.results[-1].outcome == "success"
        assert subscription.results[-1].status.phase == "replay_complete"
    finally:
        release.set()
        if subscription is not None:
            subscription.cancel()
        await client.close()
        await asyncio.gather(task, return_exceptions=True)
        loop.set_task_factory(previous_factory)


@pytest.fixture(params=[False, True], ids=["scheduled", "eager"])
async def ws_task_factory(request):
    eager = request.param
    if eager and not hasattr(asyncio, "eager_task_factory"):
        pytest.skip("eager task factories require Python 3.12+")
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    loop.set_task_factory(asyncio.eager_task_factory if eager else None)
    try:
        yield
    finally:
        loop.set_task_factory(previous_factory)


async def test_group_barrier_burst_preserves_order_and_other_groups_flow(
    wired, ws_task_factory
):
    ctx, platform, client, events = wired
    entered, release = asyncio.Event(), asyncio.Event()

    async def observe(event):
        entered.set()
        await release.wait()

    subscription = ctx.subscribe_group_lifecycle(observe)
    first = await login(ctx, platform)
    first.feed(
        0, t="GROUP_MEMBER_REMOVE", s=2,
        d={"group_openid": "group-a", "member_openid": "sample-member"},
    )
    await until(entered.is_set)
    for sequence, group, content in [
        (3, "group-a", "a-first"),
        (4, "group-a", "a-second"),
        (5, "group-b", "b-independent"),
    ]:
        first.feed(
            0, t="GROUP_AT_MESSAGE_CREATE", s=sequence,
            d={"id": content, "group_openid": group, "content": content,
               "author": {"member_openid": "sample-member"}},
        )
    try:
        await until(lambda: any(e.content == "b-independent" for e in events))
        assert [e.content for e in events] == ["b-independent"]
        release.set()
        await until(lambda: len(events) == 4)
        assert [e.connection_context.sequence for e in events] == [5, 2, 3, 4]
        subscription.cancel()
        # 群观察订阅取消后，同一群后续通知仍应继续交给原有事件链。
        for sequence in (6, 7):
            first.feed(
                0, t="GROUP_MEMBER_REMOVE", s=sequence,
                d={"group_openid": "group-a", "member_openid": "sample-member"},
            )
        await until(lambda: len(events) == 6)
        assert [e.connection_context.sequence for e in events] == [5, 2, 3, 4, 6, 7]
    finally:
        release.set()
        subscription.cancel()


@pytest.mark.parametrize("sequence", ["bad-seq", {"n": 1}, [1], True, False, 2.5])
async def test_invalid_resume_start_sequence_is_not_exposed_in_status(wired, sequence):
    ctx, platform, client, events = wired
    observed = []

    async def observe(status):
        observed.append(status)

    subscription = ctx.subscribe_connection_status(observe, replay_current=False)
    first = await login(ctx, platform)
    first.feed(99, s=sequence)
    first.feed(7)
    second = platform.add()
    second.feed(10, d={"heartbeat_interval": 45000})
    await until(lambda: any(s.phase == "resuming" for s in observed))
    snapshot = ctx.get_connection_status("sample-bot")
    notification = next(s for s in observed if s.phase == "resuming")
    assert snapshot.phase == "resuming"
    assert snapshot.continuity == "unknown"
    assert snapshot.resume_from_sequence is None
    assert notification.resume_from_sequence is None
    # 保留框架原有发送行为；公开状态不泄露原始非法值。
    assert [f["d"]["seq"] for f in second.sent if f["op"] == 6] == [sequence]
    second.feed(0, t="RESUMED", s=2, d="")
    await until(lambda: ctx.get_connection_status("sample-bot").phase == "unknown")
    assert ctx.get_connection_status("sample-bot").reason == "unproven_resumed"
    assert ctx.get_connection_status("sample-bot").replay_end_sequence is None
    assert snapshot.resume_from_sequence is None
    subscription.cancel()


@pytest.mark.parametrize("sequence", [{"n": 1}, [1]])
async def test_public_resume_snapshot_cannot_mutate_future_wire_sequence(wired, sequence):
    ctx, platform, client, events = wired
    first = await login(ctx, platform)
    first.feed(99, s=sequence)
    first.feed(7)
    second = platform.add()
    second.feed(10, d={"heartbeat_interval": 45000})
    await until(lambda: any(f["op"] == 6 for f in second.sent))
    snapshot = ctx.get_connection_status("sample-bot")
    exposed = snapshot.resume_from_sequence
    if isinstance(exposed, dict):
        exposed["n"] = 99
    elif isinstance(exposed, list):
        exposed[0] = 99
    second.feed(7)
    third = platform.add()
    third.feed(10, d={"heartbeat_interval": 45000})
    await until(lambda: any(f["op"] == 6 for f in third.sent))
    assert [f["d"]["seq"] for f in third.sent if f["op"] == 6] == [sequence]
    assert snapshot.resume_from_sequence is None
