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
        self.private_error = 'not_friends'
        self.after_reply = None
        self.reject_buttons = False
        self.acks = []

    async def reply(self, event, content=None, **kwargs):
        if kwargs.get('buttons'):
            from core.message.keyboard import build_keyboard

            if self.reject_buttons:
                return False, {'code': 'button_not_allowed'}
            kwargs['keyboard'] = build_keyboard(kwargs['buttons'], event.appid)
        target = event.group_id if event.is_group else event.raw_user_id
        result = await self.send('group' if event.is_group else 'private', target, content, **kwargs)
        if self.after_reply:
            await self.after_reply(event, content)
        return result

    async def send(self, scene, target, content, **kwargs):
        if scene == 'private' and self.fail_private:
            if self.private_error == 'network_error':
                raise ConnectionError('fake network failure')
            return False, {'code': self.private_error}
        self.messages.append((scene, target, content, kwargs))
        return True, {'id': str(len(self.messages))}

    async def send_to_user(self, target, content=None, **kwargs):
        return await self.send('private', target, content, **kwargs)

    async def send_to_group(self, target, content=None, **kwargs):
        return await self.send('group', target, content, **kwargs)


def message(text, *, bot='bot-a', user='owner-a', group='', union='', mentions=None):
    return Event.from_websocket(
        bot,
        {
            't': 'GROUP_AT_MESSAGE_CREATE' if group else 'C2C_MESSAGE_CREATE',
            'd': {
                'id': 'fake-msg',
                'content': text,
                'author': {'id': user, 'member_openid': user, 'union_openid': union},
                'group_openid': group,
                'mentions': mentions or [],
            },
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
    from core.module.hook import HookManager
    from core.module.manager import ModuleContext
    from modules.access_control import main as access_module

    hooks = HookManager()
    acl = await access_module.setup(ModuleContext('access_control', str(tmp_path / 'access'), hooks))
    plugin._instance.access_provider = lambda: acl
    plugin._instance.sender_provider = lambda bot: platform
    from modules.self_qa import main as qa_module

    qa = await qa_module.setup(ModuleContext('self_qa', str(tmp_path / 'qa'), hooks))
    plugin._instance.qa_provider = lambda: qa
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

        async def dispatch(self, event):
            event._sender = platform
            await hooks.emit('qq_event', event)
            await plugin.account_management(event, None)
            return event

        async def event(self, text, **kwargs):
            return await self.dispatch(message(text, **kwargs))

        async def lifecycle(self, kind, *, bot='bot-a', group='group-a', member='new-member'):
            return await self.dispatch(Event.from_websocket(bot, {'t': kind, 'd': {'id': 'fake-event', 'group_openid': group, 'member_openid': member}}))

        async def click(self, data, *, bot='bot-a', user='private-a', group=''):
            event = Event.from_websocket(
                bot,
                {
                    't': 'INTERACTION_CREATE',
                    'd': {
                        'id': 'fake-interaction',
                        'type': 11,
                        'application_id': bot,
                        'chat_type': 1 if group else 2,
                        'scene': 'group' if group else 'c2c',
                        'group_openid': group,
                        'group_member_openid': user if group else '',
                        'user_openid': user if not group else '',
                        'data': {'type': 11, 'resolved': {'button_data': data}},
                    },
                },
            )
            event._sender = platform
            event.start_ack_countdown()
            await hooks.emit('qq_event', event)
            await plugin.account_interaction(event, None)
            event.finish_dispatch()
            platform.acks.append((event.message_id, await event.wait_ack_code()))
            return event

        async def create(self, name='测试账户', bot='bot-a', **kwargs):
            response = await self.api('POST', 'accounts', {'name': name, 'login': 'fake-login', 'password': 'fake-secret', **kwargs}, bot=bot)
            assert response.status == 201, await response.text()
            return (await response.json())['data']

    yield Host()
    await plugin.unload()
    await access_module.teardown()
    await qa_module.teardown()
    await client.close()
