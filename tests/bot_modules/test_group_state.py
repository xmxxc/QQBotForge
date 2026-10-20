"""Independent module installation through real Application and authenticated routes."""

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

        async def get_group_bot_state(self, group, **kwargs):
            if self.after_query:
                await self.after_query()
            return self.response

        async def reply(self, event, content=None, **kwargs):
            self.messages.append((event.appid, event.group_id if event.is_group else event.raw_user_id, content))
            return True, {'id': 'fake-reply'}

    platform = Platform()
    for bot in ('bot-a', 'bot-b'):
        app._bot_registry._bots[bot] = SimpleNamespace(
            appid=bot, name=bot, sender=platform,
            log_service=SimpleNamespace(add=AsyncMock(), add_sync=Mock(), db_queue=Mock(), group_member_touch=AsyncMock()),
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
        sender = platform
        modules = manager
        http = client

        @property
        def state(self):
            return manager.get('group_state')

        async def api(self, method='GET', path='', *, body=None, bot='bot-a', authenticated=True, origin=''):
            return await client.request(method, '/api/ext/group-state/' + path, params={'bot': bot}, json=body,
                                        headers={**({'Cookie': f'{auth.SESSION_COOKIE}=group-test'} if authenticated else {}), **({'Origin': origin} if origin else {})})

        async def event(self, text, *, bot='bot-a', user='owner-a', group=''):
            event = Event.from_websocket(bot, {'t': 'GROUP_AT_MESSAGE_CREATE' if group else 'C2C_MESSAGE_CREATE',
                                              'd': {'id': 'fake-message', 'content': text, 'group_openid': group,
                                                    'author': {'id': user, 'member_openid': user}}})
            app._prepare_event(event)
            await app._on_event(event)
            return event

    try:
        yield Host()
    finally:
        await manager.shutdown()
        await client.close()
        for task in app._track_workers:
            task.cancel()
        await asyncio.gather(*app._track_workers, return_exceptions=True)


async def test_independent_known_groups_owner_entry_refresh_and_restart(group_host):
    host = group_host
    assert (await host.api(authenticated=False)).status == 401
    await host.event('普通聊天', user='member-a', group='group-a')
    state = (await (await host.api()).json())['data']
    assert state['scope'] == 'known' and state['complete'] is False
    assert [g['group'] for g in state['groups']] == ['group-a']
    assert state['groups'][0]['reliable'] is False
    assert (await (await host.api(bot='bot-b')).json())['data']['groups'] == []
    assert (await host.api('POST', 'refresh', body={'group': 'group-a'}, origin='https://external.example')).status == 403
    host.sender.messages.clear()
    await host.event('群状态', user='ordinary')
    assert host.sender.messages == []
    await host.event('群状态 刷新 group-a')
    refreshed = await host.state.snapshot('bot-a')
    group = refreshed['groups'][0]
    assert group['reliable'] and group['bot_state']['member_role'] == 'admin'
    assert group['bot_state']['in_group'] is True
    assert 'group-a' in host.sender.messages[-1][2]
    group['bot_state']['member_role'] = 'forged'
    assert (await host.state.snapshot('bot-a'))['groups'][0]['bot_state']['member_role'] == 'admin'
    assert await host.modules.reload('group_state')
    restored = (await host.state.snapshot('bot-a'))['groups'][0]
    assert restored['group'] == 'group-a' and not restored['reliable']
    assert restored['bot_state']['member_role'] == 'admin'
    assert restored['reason'] == 'process_restart'
    host.sender.messages.clear()
    await host.event('群状态')
    assert len(host.sender.messages) == 1
    from core.plugin.web_pages import get_page_html, match_route
    assert '已知群' in get_page_html('group-state')
    await host.modules.disable('group_state', _persist=False)
    assert get_page_html('group-state') is None
    assert match_route('GET', '/api/ext/group-state/') is None


@pytest.mark.parametrize('response,reason', [(None, 'response_unknown'), ((None, {'code': '11253'}), 'permission_denied'), (({}, None), 'response_unknown')])
async def test_failed_refresh_retains_history_and_never_claims_departure(group_host, response, reason):
    host = group_host
    assert (await host.api('POST', 'refresh', body={'group': 'group-a'})).status == 200
    host.sender.response = response
    result = (await (await host.api('POST', 'refresh', body={'group': 'group-a'})).json())['data']
    assert not result['reliable'] and result['reason'] == reason
    assert result['bot_state']['in_group'] is True  # explicitly historical; error is not absence
    assert result['status'] == 'historical'


async def test_observation_during_refresh_invalidates_older_result(group_host):
    host = group_host
    async def change():
        await host.event('普通聊天', user='member-a', group='group-a')
    host.sender.after_query = change
    result = (await (await host.api('POST', 'refresh', body={'group': 'group-a'})).json())['data']
    assert result['reason'] == 'state_changed' and not result['reliable']


async def test_refresh_completion_changes_snapshot_version(group_host):
    host = group_host
    pending = []
    async def capture():
        pending.append(await host.state.snapshot('bot-a'))
    host.sender.after_query = capture
    await host.api('POST', 'refresh', body={'group': 'group-a'})
    final = await host.state.snapshot('bot-a')
    assert pending[0]['groups'][0]['status'] == 'pending'
    assert final['version'] > pending[0]['version']
    assert final['groups'][0]['version'] > pending[0]['groups'][0]['version']
