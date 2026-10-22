"""Production wiring: real BotRegistry, BotInstance, MessageSender and WSClient, and the application webhook route.

Simulated platform boundary (no network, no real QQ): TokenManager hands out a fake HTTP client answering the
gateway, /users/@me and group bot_state endpoints, and websockets.connect yields scripted socket frames. From
BotInstance construction on (which registers each Bot's HTTP callback source) everything is framework code.
"""

import asyncio
import importlib
import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from socket_platform import Gateway, until

from core.base.config import cfg

BOTS = ('bot-a', 'bot-b')
BOT_STATE = {'member_role': 'admin', 'recv_msg_setting': 'all', 'allow_proactive_msg': False}


def response(status, data):
    return SimpleNamespace(status_code=status, content=json.dumps(data).encode(), json=lambda: data)


class PlatformHttp:
    """Fake QQ OpenAPI for one Bot; `gate` holds bot_state queries open to widen a rebuild."""

    is_closed = False

    def __init__(self, appid):
        self.appid = appid
        self.gate = None
        self.entered = asyncio.Event()

    async def get(self, url, **kwargs):
        if url.endswith('/gateway/bot'):
            return response(200, {'url': f'wss://api.bot.qq.com/websocket/?bot={self.appid}'})
        return response(200, {'id': 'id-' + self.appid, 'username': self.appid})

    async def request(self, method, endpoint, **kwargs):
        if method == 'GET' and endpoint.endswith('/bot_state'):
            if self.gate is not None:
                self.entered.set()
                await self.gate.wait()
            return response(200, BOT_STATE)
        return response(404, {'code': 11253, 'message': 'fake platform: endpoint not served'})


@pytest.fixture
async def wired(tmp_path, monkeypatch):
    from core.application import Application
    from core.bot.registry import BotRegistry
    from core.module.manager import ModuleManager
    from core.network.access import TokenManager

    config = tmp_path / 'config'
    config.mkdir()
    secrets = {bot: f'fake-secret-{bot}' for bot in BOTS}
    owners = {bot: 'owner' for bot in BOTS}

    def write_config():
        (config / 'bot.yaml').write_text(
            'bots:\n'
            + ''.join(
                f'  - appid: {bot}\n    secret: {secrets[bot]}\n    owner_ids: [{owners[bot]}]\n    websocket: {{enabled: true, reconnect_interval: 0}}\n'
                for bot in BOTS
            )
        )

    write_config()
    monkeypatch.setattr(cfg, '_ready', False)
    cfg.init(str(config))
    app = Application()
    monkeypatch.setattr(importlib.import_module('core.application'), '_app', app)

    platforms = {bot: PlatformHttp(bot) for bot in BOTS}
    gateways = {bot: Gateway() for bot in BOTS}

    async def noop(self):
        return None

    async def token(self):
        return 'fake-token'

    async def client(self):
        return platforms[self.appid]

    for name, fake in (('ensure_token', noop), ('start_auto_refresh', noop), ('refresh_token', noop), ('get_token', token), ('get_client', client)):
        monkeypatch.setattr(TokenManager, name, fake)

    @asynccontextmanager
    async def connect(url, **kwargs):
        async with gateways[parse_qs(urlsplit(url).query)['bot'][0]].connect(url) as socket:
            yield socket

    monkeypatch.setattr('core.network.websocket.websockets.connect', connect)

    registry = BotRegistry(
        str(tmp_path / 'logs'), on_event=app._on_event, on_event_received=app._prepare_event, connection_status=app.hook_manager.connection_status
    )
    app._bot_registry = registry
    shutil.copytree(
        Path(__file__).absolute().parents[2] / 'modules/group_state', tmp_path / 'modules/group_state', ignore=shutil.ignore_patterns('data', '__pycache__')
    )
    manager = ModuleManager(str(tmp_path / 'modules'), app.hook_manager)
    app._module_manager = manager
    manager.discover()
    http_app = web.Application()
    http_app.router.add_post('/', app._handle_webhook)  # the route core/server/http_server.py mounts
    webhook = TestClient(TestServer(http_app))
    await webhook.start_server()
    sockets = {}

    class Host:
        application, modules = app, manager

        @property
        def state(self):
            return manager.get('group_state')

        async def load_module(self):
            assert await manager.enable('group_state', _skip_deps=True, _persist=False)

        async def start_bots(self):
            for bot in BOTS:
                sockets[bot] = gateways[bot].add()
            await registry.start_all()
            assert sorted(registry.bots) == list(BOTS)
            for bot in BOTS:
                await self.ready_ws(bot)

        async def ready_ws(self, bot, *extra, session=None):
            # READY on the Bot's socket and on any extra sockets handed to other (superseded) clients.
            for socket in (sockets[bot], *extra):
                socket.feed(10, d={'heartbeat_interval': 45000})
                await until(lambda socket=socket: any(f['op'] == 2 for f in socket.sent))
                socket.feed(0, t='READY', s=1, d={'session_id': session or 'session-' + bot})
            await until(lambda: self.status(bot).phase == 'ready')

        def reconfigure(self, bot, *, secret=None, owner=None):
            """Edit bot.yaml and reload the config; the caller runs BotRegistry._sync (one or overlapping)."""
            if secret is not None:
                secrets[bot] = secret
                sockets[bot] = gateways[bot].add()
            if owner is not None:
                owners[bot] = owner
            write_config()
            cfg.reload_all()

        async def change_secret(self, bot, secret):
            # Production credential hot reload: bot.yaml change -> config reload -> BotRegistry._sync.
            self.reconfigure(bot, secret=secret)
            await registry._sync()

        def status(self, bot, transport='websocket'):
            return app.hook_manager.connection_status.get(bot, transport=transport)

        async def resume(self, bot, seq):
            sockets[bot].frames.put_nowait(OSError('fake disconnect'))
            socket = sockets[bot] = gateways[bot].add()
            socket.feed(10, d={'heartbeat_interval': 45000})
            await until(lambda: any(f['op'] == 6 for f in socket.sent))
            socket.feed(0, t='RESUMED', s=seq, d='')
            await until(lambda: self.status(bot).phase == 'replay_complete')
            await self.settle(bot)

        async def settle(self, bot):
            state = self.state
            await until(lambda: not state.notifications.busy(bot) and not state._connection_pending(bot))

        async def post_webhook(self, bot, seq, *, lifecycle=True, kind='GROUP_MEMBER_ADD', member='member-w', counted=True):
            # A private message changes no group content: only the receipt itself can fail the Bot.
            # counted=False: the instance serving the route no longer owns the public source (split sources).
            before = self.status(bot, 'webhook').context.receive_index
            if lifecycle:
                body = {
                    'op': 0,
                    't': kind,
                    's': seq,
                    'id': f'wh-{bot}-{seq}',
                    'd': {'group_openid': 'group-a', 'member_openid': member, 'timestamp': 5000 + seq},
                }
            else:
                body = {
                    'op': 0,
                    't': 'C2C_MESSAGE_CREATE',
                    's': seq,
                    'id': f'wh-{bot}-{seq}',
                    'd': {'id': f'msg-{seq}', 'content': 'hi', 'author': {'user_openid': 'user-w'}},
                }
            reply = await webhook.post('/', json=body, headers={'X-Bot-Appid': bot})
            assert reply.status == 200
            assert self.status(bot, 'webhook').context.receive_index == before + counted

    host = Host()
    host.platforms, host.registry, host.gateways, host.sockets = platforms, registry, gateways, sockets
    try:
        yield host
    finally:
        await registry.shutdown()
        await manager.shutdown()
        await webhook.close()
        for task in app._track_workers:
            task.cancel()
        await asyncio.gather(*app._track_workers, return_exceptions=True)


async def confirm(state, bot):
    await state.confirm_member(bot, 'group-a', 'member-a', evidence='wiring test owner check')


async def usable(state, bot):
    fact = await state.fact(bot, 'group-a', 'member-a')
    return bool(fact['usable'] and state.fact_current(fact)), fact


async def trusted(host, *bots):
    for bot in bots:
        await host.settle(bot)
        await confirm(host.state, bot)
        assert await host.state.rebuild(bot)
        ok, fact = await usable(host.state, bot)
        assert ok, (bot, fact['reason'])
    return [(await usable(host.state, bot))[1] for bot in bots]


@pytest.mark.parametrize('order', ['module_first', 'bots_first'])
async def test_ws_bot_with_registered_webhook_source_is_usable_after_rebuild(wired, order):
    if order == 'module_first':  # core/application.py: modules start (step 3) before the Bot registry (step 6)
        await wired.load_module()
        await wired.start_bots()
    else:
        await wired.start_bots()
        await wired.load_module()
    for bot in BOTS:
        webhook = wired.status(bot, 'webhook')
        assert (webhook.phase, webhook.reason, webhook.context.receive_index) == ('unknown', 'http_callback', 0)
        assert webhook.context.bot_generation == wired.registry.get(bot).bot_generation == wired.status(bot).context.bot_generation
        assert not (await usable(wired.state, bot))[0]  # no baseline yet
    await trusted(wired, *BOTS)


async def test_module_reload_needs_rebuild_then_usable(wired):
    await wired.load_module()
    await wired.start_bots()
    old = wired.state
    (proof,) = await trusted(wired, 'bot-a')
    assert await wired.modules.reload('group_state')
    new = wired.state
    assert new is not old and not new.fact_current(proof)
    fact = await new.fact('bot-a', 'group-a', 'member-a')
    assert not fact['usable'] and fact['reason'] == 'recovery_required'
    await trusted(wired, 'bot-a')


async def test_ws_disconnect_and_trusted_resume_recovers_without_rebuild(wired):
    await wired.load_module()
    await wired.start_bots()
    proof_a, proof_b = await trusted(wired, *BOTS)
    await wired.resume('bot-a', 2)
    assert not wired.state.fact_current(proof_a)
    ok, fact = await usable(wired.state, 'bot-a')
    assert ok, fact
    assert wired.state.fact_current(proof_b)


async def test_same_bot_webhook_delivery_fails_closed_other_bot_unaffected(wired):
    await wired.load_module()
    await wired.start_bots()
    proof_a, proof_b = await trusted(wired, *BOTS)
    await wired.post_webhook('bot-a', 7, lifecycle=False)
    # Synchronous: no status revision is published for an HTTP receipt.
    assert not wired.state.fact_current(proof_a)
    fact = await wired.state.fact('bot-a', 'group-a', 'member-a')
    assert not fact['usable'] and fact['reason'] == 'recovery_required', fact
    await wired.settle('bot-a')
    await confirm(wired.state, 'bot-a')
    assert await wired.state.rebuild('bot-a') is False
    await wired.post_webhook('bot-a', 8)  # a lifecycle callback over HTTP is no more trusted
    await wired.resume('bot-a', 2)
    assert (await wired.state.fact('bot-a', 'group-a', 'member-a'))['reason'] == 'recovery_required'
    assert wired.state.fact_current(proof_b)
    assert (await usable(wired.state, 'bot-b'))[0]
    # A reload re-reads the same Bot instance's receipt count: still fail closed.
    assert await wired.modules.reload('group_state')
    await confirm(wired.state, 'bot-a')
    assert await wired.state.rebuild('bot-a') is False
    await trusted(wired, 'bot-b')


@pytest.mark.parametrize('change', ['same_bot_registration', 'same_bot_delivery', 'other_bot_delivery'])
async def test_webhook_change_inside_rebuild_window(wired, tmp_path, change):
    from core.bot.instance import BotInstance

    await wired.load_module()
    await wired.start_bots()
    await trusted(wired, *BOTS)
    state, platform = wired.state, wired.platforms['bot-a']
    state.recovery.invalidate('bot-a', reason='wiring_test_force_rebuild')
    await confirm(state, 'bot-a')
    platform.gate = asyncio.Event()
    rebuild = asyncio.create_task(state.rebuild('bot-a'))
    await until(platform.entered.is_set)
    if change == 'same_bot_registration':
        # BotInstance.__init__ for bot-a outside the registry (the first step of an overlapping _sync): its HTTP
        # source supersedes the serving instance's, whose callbacks then never reach the public receive_index.
        BotInstance(
            {'appid': 'bot-a', 'secret': 'fake-secret'}, str(tmp_path / 'other-logs'), connection_status=wired.application.hook_manager.connection_status
        )
        await until(lambda: not state._connection_pending('bot-a'))
    else:
        await wired.post_webhook('bot-a' if change == 'same_bot_delivery' else 'bot-b', 9, lifecycle=False)
    platform.gate.set()
    result = await rebuild
    ok, fact = await usable(state, 'bot-a')
    if change in ('same_bot_delivery', 'same_bot_registration'):
        assert result is False and fact['reason'] == 'recovery_required', (result, fact)
    else:
        assert result is True and ok, (result, fact)


def owners(host, bot):
    """Generations of the registry's BotInstance (which serves the webhook route) and of the two current sources."""
    return host.registry.get(bot).bot_generation, host.status(bot, 'webhook').context.bot_generation, host.status(bot).context.bot_generation


async def overlapping_credential_sync(host, bot, secret):
    """One bot.yaml change seen by two BotRegistry._sync runs (e.g. the file watcher and a web toggle) while the
    first new BotInstance starts slowly: the instance that finishes start() last owns the registry slot and the WS
    source, the one constructed last owns the HTTP callback source."""
    platform, hold, calls = host.platforms[bot], asyncio.Event(), []
    real_get = platform.get

    async def get(url, **kwargs):
        if url.endswith('/users/@me'):
            calls.append(url)
            if len(calls) == 1:
                await hold.wait()
        return await real_get(url, **kwargs)

    platform.get = get
    host.reconfigure(bot, secret=secret)
    extra = host.gateways[bot].add()  # the second new WSClient also connects
    first = asyncio.create_task(host.registry._sync())
    await until(lambda: len(calls) == 1)  # the first new instance is constructed and held inside start()
    await host.registry._sync()  # the second instance starts unheld and takes the registry slot first
    assert len(calls) == 2 and not first.done()
    hold.set()
    await first
    platform.get = real_get
    return extra


async def test_overlapping_sync_split_sources_fail_closed_until_a_formal_replacement(wired):
    await wired.load_module()
    await wired.start_bots()
    proof_a, proof_b = await trusted(wired, *BOTS)
    extra = await overlapping_credential_sync(wired, 'bot-a', 'fake-secret-bot-a-2')
    serving, webhook_owner, ws_owner = owners(wired, 'bot-a')
    assert serving == ws_owner != webhook_owner  # the serving instance's HTTP source was superseded
    assert not wired.state.fact_current(proof_a)
    await wired.ready_ws('bot-a', extra, session='session-a-2')
    await wired.settle('bot-a')
    await confirm(wired.state, 'bot-a')
    assert await wired.state.rebuild('bot-a') is False  # rebuild cannot clear a live source split
    fact = await wired.state.fact('bot-a', 'group-a', 'member-a')
    assert not fact['usable'] and fact['reason'] == 'recovery_required', fact
    # An actual member-left callback is served (200) but never reaches the public receive_index, and recovery
    # drops it as an unproven source: the member must still not be usable.
    await wired.post_webhook('bot-a', 9, kind='GROUP_MEMBER_REMOVE', member='member-a', counted=False)
    assert wired.status('bot-a', 'webhook').context.receive_index == 0
    assert not (await usable(wired.state, 'bot-a'))[0] and not wired.state.fact_current(proof_a)
    await wired.settle('bot-a')
    await confirm(wired.state, 'bot-a')
    assert await wired.state.rebuild('bot-a') is False
    assert await wired.modules.reload('group_state')  # a reload re-reads the same split
    await confirm(wired.state, 'bot-a')
    assert await wired.state.rebuild('bot-a') is False
    assert wired.state.fact_current(proof_b) is False  # the reload retired every proof; bot-b rebuilds as usual
    await trusted(wired, 'bot-b')
    # A formal, non-overlapping replacement gives one instance both sources again; after READY and a baseline
    # the Bot is usable, and that instance's own HTTP receipts are counted and fail closed again.
    await wired.change_secret('bot-a', 'fake-secret-bot-a-3')
    serving, webhook_owner, ws_owner = owners(wired, 'bot-a')
    assert serving == webhook_owner == ws_owner
    await wired.ready_ws('bot-a', session='session-a-3')
    (proof_a,) = await trusted(wired, 'bot-a')
    await wired.post_webhook('bot-a', 10, lifecycle=False)
    assert not wired.state.fact_current(proof_a)
    assert (await usable(wired.state, 'bot-b'))[0]


async def test_split_sources_while_trusted_fail_closed_synchronously(wired, tmp_path):
    await wired.load_module()
    await wired.start_bots()
    proof_a, proof_b = await trusted(wired, *BOTS)
    from core.bot.instance import BotInstance

    # The registry instance's HTTP source is superseded by an instance constructed for the same appid that never
    # reached the registry: the first step of every overlapping _sync. No WS status changes.
    revision = wired.status('bot-a').revision
    BotInstance({'appid': 'bot-a', 'secret': 'fake-secret'}, str(tmp_path / 'stray-logs'), connection_status=wired.application.hook_manager.connection_status)
    assert wired.status('bot-a').revision == revision
    assert not wired.state.fact_current(proof_a)  # synchronously, before any notification
    fact = await wired.state.fact('bot-a', 'group-a', 'member-a')
    assert not fact['usable'] and fact['reason'] == 'recovery_required', fact
    await wired.settle('bot-a')  # the stray registration status is delivered; the split remains
    assert not wired.state.fact_current(proof_a) and not (await usable(wired.state, 'bot-a'))[0]
    await wired.post_webhook('bot-a', 9, kind='GROUP_MEMBER_REMOVE', member='member-a', counted=False)
    assert not wired.state.fact_current(proof_a) and not (await usable(wired.state, 'bot-a'))[0]
    await wired.settle('bot-a')
    await confirm(wired.state, 'bot-a')
    assert await wired.state.rebuild('bot-a') is False
    assert wired.state.fact_current(proof_b) and (await usable(wired.state, 'bot-b'))[0]


def test_unknown_source_identities_never_match():
    from dataclasses import replace

    from core.module.connection_status import ConnectionCapabilities, ConnectionContext, ConnectionStatus
    from modules.group_state.state import GroupState

    def status(transport, runtime, generation):
        context = ConnectionContext('bot-a', transport, runtime, generation)
        return ConnectionStatus(context, 1, 'unknown', 'unknown', None, 'created', ConnectionCapabilities(False, False, False, 'http_callback'))

    webhook, websocket = status('webhook', 'runtime', 'generation'), status('websocket', 'runtime', 'generation')
    assert GroupState._same_instance(webhook, websocket)
    assert not GroupState._same_instance(webhook, None)
    assert not GroupState._same_instance(status('webhook', 'runtime', ''), status('websocket', 'runtime', ''))
    assert not GroupState._same_instance(webhook, replace(websocket, context=replace(websocket.context, runtime_id='other-runtime')))
    assert not GroupState._same_instance(webhook, replace(websocket, context=replace(websocket.context, bot_generation='other')))


async def test_non_overlapping_hot_updates_stay_usable(wired):
    await wired.load_module()
    await wired.start_bots()
    proof_a, proof_b = await trusted(wired, *BOTS)
    # A non-credential edit keeps the BotInstance and both sources: proofs stay current.
    wired.reconfigure('bot-a', owner='owner-2')
    await wired.registry._sync()
    await wired.registry._sync()  # a second, non-overlapping sync of the same file is a no-op
    assert wired.state.fact_current(proof_a) and wired.state.fact_current(proof_b)
    # Formal credential replacement: a new instance owns both sources; it needs READY and a baseline.
    old = owners(wired, 'bot-a')[0]
    await wired.change_secret('bot-a', 'fake-secret-bot-a-2')
    await wired.registry._sync()
    serving, webhook_owner, ws_owner = owners(wired, 'bot-a')
    assert serving == webhook_owner == ws_owner != old
    assert not wired.state.fact_current(proof_a)
    await wired.ready_ws('bot-a', session='session-a-2')
    await trusted(wired, 'bot-a')
    assert wired.state.fact_current(proof_b)


async def test_failed_webhook_status_delivery_during_replacement_needs_rebuild_then_recovers(wired):
    await wired.load_module()
    await wired.start_bots()
    proof_a, proof_b = await trusted(wired, *BOTS)

    async def consumer(change):
        if change['bot'] == 'bot-a' and change['reason'] == 'connection:unknown':
            raise RuntimeError('consumer failed')

    subscription = wired.state.subscribe(consumer, timeout=30)
    # The replacement's HTTP source statuses (created, http_callback) are delivered and fail like any status.
    await wired.change_secret('bot-a', 'fake-secret-bot-a-2')
    subscription_ = wired.state.connection_subscription
    await until(lambda: any(r.status.appid == 'bot-a' and r.status.transport == 'webhook' and r.outcome != 'success' for r in subscription_.results))
    await wired.ready_ws('bot-a', session='session-a-2')
    fact = await wired.state.fact('bot-a', 'group-a', 'member-a')
    assert not fact['usable'] and fact['reason'] == 'recovery_required', fact
    subscription.cancel()
    assert wired.state._connection_pending('bot-a')  # a failed delivery is not settled; rebuild settles it
    await until(lambda: not wired.state.notifications.busy('bot-a'))
    await confirm(wired.state, 'bot-a')
    assert await wired.state.rebuild('bot-a')
    ok, fact = await usable(wired.state, 'bot-a')
    assert ok and not wired.state._connection_pending('bot-a'), fact
    assert wired.state.fact_current(proof_b)
