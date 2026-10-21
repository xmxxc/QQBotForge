"""Temporary independent group-state host with actual framework entry points."""

import asyncio
import importlib
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.base.config import cfg
from core.message.event import Event


@pytest.fixture
async def group_host(tmp_path, monkeypatch):
    from core.application import Application
    from core.bot.registry import BotRegistry
    from core.module.manager import ModuleManager
    from web import auth
    from web.api import handle_ext_route

    config = tmp_path / 'config'
    config.mkdir()
    (config / 'bot.yaml').write_text('bots:\n  - appid: bot-a\n    owner_ids: [owner-a]\n  - appid: bot-b\n    owner_ids: [owner-b]\n')
    monkeypatch.setattr(cfg, '_ready', False)
    cfg.init(str(config))
    app = Application()
    monkeypatch.setattr(importlib.import_module('core.application'), '_app', app)
    app._bot_registry = BotRegistry(str(tmp_path / 'logs'), on_event=app._on_event, on_event_received=app._prepare_event)

    class Platform:
        def __init__(self):
            self.messages = []
            self.response = ({'member_role': 'admin', 'recv_msg_setting': 'all', 'allow_proactive_msg': False}, None)
            self.after_query = None
            self.json_results = {}
            self.after_json = None

        async def get_group_bot_state(self, group, **kwargs):
            if self.after_query:
                await self.after_query()
            return self.response

        async def get_json(self, path, **kwargs):
            cursor = (kwargs.get('params') or {}).get('cursor', '')
            if self.after_json:
                await self.after_json(path, cursor)
            return self.json_results.get((path, cursor), (False, {'code': '11253'}))

        # Private pushes: Sender.send_to_user returns (ok, data, payload).
        push_result = (True, {'id': 'fake-push'})

        async def send_to_user(self, user_id, content=None, **kwargs):
            ok, data = self.push_result
            if ok:
                self.messages.append(('push', user_id, content))
            return ok, data, {}

        async def send_to_group(self, group_id, content=None, **kwargs):
            self.messages.append(('group-push', group_id, content))
            return True, {'id': 'fake-group-push'}, {}

        async def send_image(self, target_type, target_id, image, content='', **kwargs):
            self.messages.append(('push-image', target_id, image))
            return True, {'id': 'fake-image'}

        async def reply(self, event, content=None, **kwargs):
            self.messages.append((event.appid, event.group_id if event.is_group else event.raw_user_id, content))
            # Same shape as Sender.reply: the platform response dict.
            return {'id': 'fake-reply'}

    platform = Platform()
    for bot in ('bot-a', 'bot-b'):
        app._bot_registry._bots[bot] = SimpleNamespace(
            appid=bot,
            name=bot,
            sender=platform,
            log_service=SimpleNamespace(
                add=AsyncMock(), add_sync=Mock(), db_queue=Mock(), group_member_touch=AsyncMock(), group_member_remove=AsyncMock(), group_member_add=AsyncMock()
            ),
        )
    source = Path(__file__).absolute().parents[2]
    shutil.copytree(source / 'modules/group_state', tmp_path / 'modules/group_state', ignore=shutil.ignore_patterns('data', '__pycache__'))
    manager = ModuleManager(str(tmp_path / 'modules'), app.hook_manager)
    app._module_manager = manager
    manager.discover()
    assert [entry['name'] for entry in manager.list_modules()] == ['group_state']
    assert await manager.enable('group_state', _skip_deps=True, _persist=False)
    monkeypatch.setattr(auth, 'valid_sessions', {'group-test': {'expires': datetime.now() + timedelta(days=1), 'created': datetime.now()}})
    http_app = web.Application()
    http_app.router.add_route('*', '/api/ext/{tail:.*}', handle_ext_route)
    client = TestClient(TestServer(http_app))
    await client.start_server()

    class Host:
        application = app
        sender = platform
        modules = manager
        http = client

        @property
        def state(self):
            return manager.get('group_state')

        async def api(self, method='GET', path='', *, body=None, bot='bot-a', authenticated=True, origin=''):
            return await client.request(
                method,
                '/api/ext/group-state/' + path,
                params={'bot': bot},
                json=body,
                headers={**({'Cookie': f'{auth.SESSION_COOKIE}=group-test'} if authenticated else {}), **({'Origin': origin} if origin else {})},
            )

        async def event(self, text, *, bot='bot-a', user='owner-a', group=''):
            event = Event.from_websocket(
                bot,
                {
                    't': 'GROUP_AT_MESSAGE_CREATE' if group else 'C2C_MESSAGE_CREATE',
                    'd': {'id': 'fake-message', 'content': text, 'group_openid': group, 'author': {'id': user, 'member_openid': user}},
                },
            )
            app._prepare_event(event)
            await app._on_event(event)
            return event

    from socket_platform import Gateway, until

    from core.network.websocket import WSClient

    gateways, sockets, clients, tasks = {}, {}, [], []
    # Drive READY and real initial baseline independently for each fake Bot.
    for bot in ('bot-a', 'bot-b'):
        gateway = Gateway()
        gateways[bot] = gateway
        monkeypatch.setattr('core.network.websocket.websockets.connect', gateway.connect)
        client_ws = WSClient(
            bot,
            gateway,
            app._on_event,
            ack_interaction=app._on_event,
            reconnect_interval=0,
            connection_status=app.hook_manager.connection_status,
            on_event_received=app._prepare_event,
        )
        task = asyncio.create_task(client_ws.connect())
        clients.append(client_ws)
        tasks.append(task)
        socket = gateway.add()
        sockets[bot] = socket
        socket.feed(10, d={'heartbeat_interval': 45000})
        await until(lambda socket=socket: any(f['op'] == 2 for f in socket.sent))
        socket.feed(0, t='READY', s=1, d={'session_id': 'session-' + bot})
        await until(lambda bot=bot: app.hook_manager.connection_status.get(bot).phase == 'ready')
        assert await manager.get('group_state').rebuild(bot)
    # The global connector dispatches by each client's token (set below).
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def choose_gateway(url, **kwargs):
        async with gateways[connecting_bot[0]].connect(url, **kwargs) as value:
            yield value

    # Tests reconnect bot-a only; bot-b remains connected and isolated.
    connecting_bot = ['bot-a']
    monkeypatch.setattr('core.network.websocket.websockets.connect', choose_gateway)
    host = Host()
    host.gateways, host.sockets, host.connecting_bot = gateways, sockets, connecting_bot

    def lifecycle(kind, *, member=None, group='group-a', sequence=2, timestamp=1000, event_id='auto', bot='bot-a'):
        sockets[bot].feed(
            0,
            t=kind,
            s=sequence,
            event_id=f'event-{sequence}' if event_id == 'auto' else event_id,
            d={'group_openid': group, 'member_openid': member, 'timestamp': timestamp},
        )

    host.lifecycle = lifecycle
    try:
        yield host
    finally:
        for client_ws in clients:
            await client_ws.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await manager.shutdown()
        await client.close()
        for task in app._track_workers:
            task.cancel()
        await asyncio.gather(*app._track_workers, return_exceptions=True)
