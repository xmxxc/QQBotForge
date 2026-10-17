"""Real HTTP/session + parsed QQ events; only platform I/O and time are replaced."""

from datetime import datetime, timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.base.config import cfg
from core.message.event import Event


class Platform:
    def __init__(self):
        self.messages = []
        self.fail_private = False

    async def reply(self, event, content=None, **kwargs):
        target = event.group_id if event.is_group else event.raw_user_id
        return await self.send('group' if event.is_group else 'private', target, content, **kwargs)

    async def send(self, scene, target, content, **kwargs):
        if scene == 'private' and self.fail_private:
            return False, {'code': 'not_friends'}
        self.messages.append((scene, target, content, kwargs))
        return True, {'id': str(len(self.messages))}

    async def send_to_user(self, target, content=None, **kwargs):
        return await self.send('private', target, content, **kwargs)

    async def send_to_group(self, target, content=None, **kwargs):
        return await self.send('group', target, content, **kwargs)


def message(text, *, bot='bot-a', user='owner-a', group='', union=''):
    return Event.from_websocket(
        bot,
        {
            't': 'GROUP_AT_MESSAGE_CREATE' if group else 'C2C_MESSAGE_CREATE',
            'd': {'id': 'fake-msg', 'content': text, 'author': {'id': user, 'member_openid': user, 'union_openid': union}, 'group_openid': group},
        },
    )


@pytest.fixture
async def host(tmp_path, monkeypatch):
    import importlib

    from core.plugin import context
    from core.plugin.context import PluginContext
    from web import auth
    from web.api import handle_ext_route

    config_dir = tmp_path / 'config'
    config_dir.mkdir()
    (config_dir / 'bot.yaml').write_text('bots:\n  - appid: bot-a\n    owner_ids: [owner-a]\n  - appid: bot-b\n    owner_ids: [owner-b]\n')
    monkeypatch.setattr(cfg, '_ready', False)
    cfg.init(str(config_dir))
    # The repository supports both a bots list and per-file configuration; use its public loader.
    platform = Platform()
    monkeypatch.setattr(context, 'ctx', PluginContext('account_sharing', str(tmp_path / 'plugin')))
    plugin = importlib.import_module('plugins.account_sharing.main')
    plugin = importlib.reload(plugin)
    await plugin.load()
    monkeypatch.setattr(auth, 'valid_sessions', {'test-session': {'expires': datetime.now() + timedelta(days=1), 'created': datetime.now()}})
    app = web.Application()
    app.router.add_route('*', '/api/ext/{tail:.*}', handle_ext_route)
    client = TestClient(TestServer(app))
    await client.start_server()

    class Host:
        @property
        def service(self):
            return plugin._instance

        entry = plugin
        sender = platform
        http = client

        async def api(self, method, path='', body=None, bot='bot-a', authenticated=True):
            return await client.request(
                method,
                '/api/ext/account-sharing/' + path,
                params={'bot': bot},
                json=body,
                headers={'Cookie': f'{auth.SESSION_COOKIE}=test-session'} if authenticated else {},
            )

        async def event(self, text, **kwargs):
            event = message(text, **kwargs)
            event._sender = platform
            await plugin.account_management(event, None)
            return event

        async def create(self, name='测试账户', bot='bot-a', **kwargs):
            response = await self.api('POST', 'accounts', {'name': name, 'login': 'fake-login', 'password': 'fake-secret', **kwargs}, bot=bot)
            assert response.status == 201, await response.text()
            return (await response.json())['data']

    yield Host()
    await plugin.unload()
    await client.close()
