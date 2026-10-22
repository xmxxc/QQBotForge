"""Q&A installed alone: owner configuration, real message entry and public replies."""

import asyncio
import importlib
import json
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

BOTS = """bots:
  - appid: bot-a
    owner_ids: [owner-a]
    message:
      send_default_response: true
    non_at_message:
      enabled: false
      group_whitelist: [group-open]
      ignore_at_other_bot: true
    blacklist:
      user_list: {blocked-user: spam}
      group_list: {blocked-group: spam}
  - appid: bot-b
    owner_ids: [owner-b]
"""


@pytest.fixture
async def qa_host(tmp_path, monkeypatch):
    from core.application import Application
    from core.bot.registry import BotRegistry
    from core.module.manager import ModuleManager
    from web import auth
    from web.api import handle_ext_route

    config = tmp_path / 'config'
    config.mkdir()
    (config / 'bot.yaml').write_text(BOTS)
    monkeypatch.setattr(cfg, '_ready', False)
    cfg.init(str(config))
    importlib.import_module('core.plugin._blacklist')._BLACKLIST_CACHE.clear()
    app = Application()
    monkeypatch.setattr(importlib.import_module('core.application'), '_app', app)
    app._bot_registry = BotRegistry(str(tmp_path / 'logs'), on_event=app._on_event, on_event_received=app._prepare_event)

    class Platform:
        _bot_name = 'QA'

        def __init__(self):
            self.messages = []
            self.buttons = []
            self.reject_buttons = False

        async def reply(self, event, content=None, **kwargs):
            if kwargs.get('buttons') is not None:
                if self.reject_buttons:
                    return False, {'code': 'fake-button-rejected'}
                self.buttons.append(kwargs['buttons'])
            # Framework template replies (default/blacklist/maintenance) are recorded by name.
            content = content if content is not None else 'template:' + str(kwargs.get('template_name'))
            self.messages.append((event.appid, event.group_id if event.is_group else event.raw_user_id, content))
            return True, {'id': 'fake-reply'}

    platform = Platform()
    for bot in ('bot-a', 'bot-b'):
        app._bot_registry._bots[bot] = SimpleNamespace(
            appid=bot,
            name=bot,
            sender=platform,
            log_service=SimpleNamespace(
                add=AsyncMock(),
                add_sync=Mock(),
                db_queue=Mock(),
                wakeup_update=AsyncMock(),
                group_member_touch=AsyncMock(),
                group_member_remove=AsyncMock(),
                group_member_add=AsyncMock(),
            ),
        )
    # Real dispatcher with no plugins installed: receive policy and before_handler run there.
    from core.plugin.manager import PluginManager

    (tmp_path / 'plugins').mkdir()
    app._plugin_manager = PluginManager(str(tmp_path / 'plugins'))
    await app._plugin_manager.load_all()
    source = Path(__file__).absolute().parents[2]
    shutil.copytree(source / 'modules/self_qa', tmp_path / 'modules/self_qa', ignore=shutil.ignore_patterns('data', '__pycache__'))
    manager = ModuleManager(str(tmp_path / 'modules'), app.hook_manager)
    app._module_manager = manager
    manager.discover()
    # Neither group state, authorization nor account folders exist.
    assert [entry['name'] for entry in manager.list_modules()] == ['self_qa']
    assert await manager.enable('self_qa', _skip_deps=True, _persist=False)
    monkeypatch.setattr(auth, 'valid_sessions', {'qa-test': {'expires': datetime.now() + timedelta(days=1), 'created': datetime.now()}})
    http_app = web.Application()
    http_app.router.add_route('*', '/api/ext/{tail:.*}', handle_ext_route)
    client = TestClient(TestServer(http_app))
    await client.start_server()

    class Host:
        application = app
        sender = platform
        modules = manager

        async def api(self, body=None, *, bot='bot-a', authenticated=True):
            response = await client.request(
                'POST' if body else 'GET',
                '/api/ext/self-qa/',
                params={'bot': bot},
                json=body,
                headers={'Cookie': f'{auth.SESSION_COOKIE}=qa-test'} if authenticated else {},
            )
            return response.status, (await response.json())

        async def event(self, text, *, bot='bot-a', user='member-a', group='', at=True, mentions=None):
            kind = ('GROUP_AT_MESSAGE_CREATE' if at else 'GROUP_MESSAGE_CREATE') if group else 'C2C_MESSAGE_CREATE'
            data = {'id': 'fake-message', 'content': text, 'group_openid': group, 'author': {'id': user, 'member_openid': user}}
            if mentions is not None:
                data['mentions'] = mentions
            event = Event.from_websocket(bot, {'t': kind, 'd': data})
            app._prepare_event(event)
            await app._on_event(event)
            for _ in range(5):  # let spawned framework replies run
                await asyncio.sleep(0)
            return event

        async def click(self, data, *, bot='bot-a', user='member-a', group=''):
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
                        'user_openid': '' if group else user,
                        'data': {'type': 11, 'resolved': {'button_data': data}},
                    },
                },
            )
            event.start_ack_countdown()
            app._prepare_event(event)
            await app._on_event(event)
            return await event.wait_ack_code()

        def replies(self, *, templates=False):
            result = [m for m in self.sender.messages if templates or not m[2].startswith('template:')]
            self.sender.messages.clear()
            return result

    host = Host()
    try:
        yield host
    finally:
        await manager.shutdown()
        await client.close()
        for task in app._track_workers:
            task.cancel()


async def configure(host, *, bot='bot-a', groups=('group-a',), private=True):
    status, data = await host.api({'operation': 'settings', 'groups': list(groups), 'private': private}, bot=bot)
    assert status == 200, data
    status, data = await host.api({'operation': 'create', 'name': '帮助', 'target': '公开说明', 'keywords': ['帮助', '说明']}, bot=bot)
    assert status == 200, data
    return data['data']


async def test_alone_owner_configures_and_members_get_public_text_replies(qa_host):
    host = qa_host
    assert not (Path(host.modules._dir) / 'access_control').exists()
    # Default: nothing enabled, so nobody gets an answer anywhere.
    status, data = await host.api({'operation': 'create', 'name': '帮助', 'target': '公开说明', 'keywords': ['帮助']})
    assert status == 200 and data['data']['groups'] == []
    await host.event('帮助', group='group-a')
    await host.event('帮助')
    assert host.replies() == []
    await host.api({'operation': 'delete', 'id': data['data']['id']})
    rule = await configure(host)
    log = host.application.get_bot('bot-a').log_service.add_sync
    log.reset_mock()
    await host.event('帮助', group='group-a')
    await host.event('说明')
    assert host.replies() == [('bot-a', 'group-a', '公开说明'), ('bot-a', 'member-a', '公开说明')]
    # Answered messages are still recorded by the framework message log.
    assert [call.args[0] for call in log.call_args_list].count('message') == 2
    # Unknown groups, other Bots and misses get nothing from Q&A.
    await host.event('帮助', group='group-unknown')
    await host.event('帮助', bot='bot-b', group='group-a')
    await host.event('随便聊聊', group='group-a')
    assert host.replies() == []
    # A miss reaches the framework's own default reply; a hit consumes it (no duplicate).
    await host.event('随便聊聊', group='group-a')
    await host.event('帮助', group='group-a')
    assert host.replies(templates=True) == [('bot-a', 'group-a', 'template:default'), ('bot-a', 'group-a', '公开说明')]
    # Contains mode, keyword disable and rule disable.
    await host.api({'operation': 'update', 'id': rule['id'], 'mode': 'contains', 'keywords': ['帮助', {'text': '说明', 'enabled': False}]})
    await host.event('请问帮助在哪', group='group-a')
    await host.event('说明')
    assert host.replies() == [('bot-a', 'group-a', '公开说明')]
    await host.api({'operation': 'update', 'id': rule['id'], 'enabled': False})
    await host.event('帮助', group='group-a')
    assert host.replies() == []


async def test_group_rules_global_rules_and_private_scope(qa_host):
    host = qa_host
    await configure(host, groups=('group-a', 'group-b'))
    status, data = await host.api({'operation': 'create', 'name': '群规', 'target': 'A群规则', 'keywords': ['群规'], 'groups': ['group-a']})
    assert status == 200
    await host.event('群规', group='group-a')
    await host.event('群规', group='group-b')
    await host.event('群规')
    assert host.replies() == [('bot-a', 'group-a', 'A群规则')]
    await host.api({'operation': 'settings', 'groups': ['group-a'], 'private': False})
    await host.event('帮助')
    await host.event('帮助', group='group-b')
    assert host.replies() == []
    listed = (await host.api())[1]['data']
    assert listed['settings'] == {'groups': ['group-a'], 'private': False} and len(listed['rules']) == 2
    assert (await host.api({'operation': 'settings', 'groups': 'group-a'}))[0] == 422
    assert (await host.api({'operation': 'create', 'name': 'x', 'target': 'y', 'keywords': []}))[0] == 422


async def test_framework_receive_policy_still_applies(qa_host):
    host = qa_host
    await configure(host, groups=('group-a', 'group-open', 'blocked-group'))
    # Non-@ group messages only where the framework allows them.
    await host.event('帮助', group='group-a', at=False)
    await host.event('帮助', group='group-open', at=False)
    assert host.replies() == [('bot-a', 'group-open', '公开说明')]
    await host.event('帮助', group='group-open', at=False, mentions=[{'id': 'x', 'bot': True}])
    await host.event('帮助', user='blocked-user', group='group-a')
    await host.event('帮助', group='blocked-group')
    assert host.replies(templates=True) == [('bot-a', 'group-a', 'template:blacklist'), ('bot-a', 'blocked-group', 'template:group_blacklist')]
    cfg.set_bot_setting('bot-a', 'maintenance.enabled', True)
    await host.event('帮助', group='group-a')
    assert host.replies(templates=True) == [('bot-a', 'group-a', 'template:maintenance')]
    await host.event('帮助', user='owner-a', group='group-a')
    assert host.replies() == [('bot-a', 'group-a', '公开说明')]


async def test_owner_command_http_auth_reload_and_no_duplicate_replies(qa_host):
    host = qa_host
    assert (await host.api(authenticated=False))[0] == 401
    command = '问答管理 ' + json.dumps({'operation': 'settings', 'groups': ['group-a'], 'private': True})
    await host.event(command, user='member-a')
    assert (await host.api())[1]['data']['settings']['groups'] == []
    await host.event(command, user='owner-a')
    assert json.loads(host.replies()[-1][2]) == {'groups': ['group-a'], 'private': True}
    await host.event(
        '问答管理 ' + json.dumps({'operation': 'create', 'name': '帮助', 'target': '公开说明', 'keywords': ['帮助']}), user='owner-a', group='group-a'
    )
    assert host.replies() == [('bot-a', 'group-a', '已处理，请在主人私聊或后台查看详情')]
    for _ in range(2):
        await host.modules.disable('self_qa', _persist=False)
        assert await host.modules.enable('self_qa', _skip_deps=True, _persist=False)
    await host.event('帮助', group='group-a')
    assert host.replies() == [('bot-a', 'group-a', '公开说明')]
    await host.modules.disable('self_qa', _persist=False)
    await host.event('帮助', group='group-a')
    assert host.replies() == []
    assert (await host.api())[0] == 404


async def test_consumer_namespace_is_separate_from_public_entry(qa_host):
    host = qa_host
    await configure(host)
    qa = host.modules.get('self_qa')
    await qa.manage('bot-a', 'account_sharing', 'create', {'name': '账户', 'kind': 'text', 'target': '私有', 'keywords': ['账户']})
    await host.event('账户', group='group-a')
    assert host.replies() == []
    assert [m['target'] for m in await qa.match('bot-a', 'account_sharing', '账户')] == ['私有']
    assert [m['target'] for m in await qa.match('bot-a', 'public', '帮助', group='group-a')] == ['公开说明']
