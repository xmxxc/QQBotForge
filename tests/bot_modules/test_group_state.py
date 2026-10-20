"""Independent module installation through real Application and authenticated routes."""

import asyncio
import importlib
import json
import shutil
import sqlite3
import threading
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


@pytest.mark.parametrize('entry', ['http', 'qq'])
async def test_failed_commit_keeps_previous_bot_state_untrusted_until_retry(group_host, entry):
    host = group_host
    host.sender.response = ({'member_role': 'member', 'recv_msg_setting': 'all', 'allow_proactive_msg': False}, None)
    await host.api('POST', 'refresh', body={'group': 'group-a'})
    database = host.modules.get_context('group_state').get_data_path('group-state.db')
    with sqlite3.connect(database) as connection:
        connection.execute("""CREATE TRIGGER reject_verified BEFORE INSERT ON extension_records
            WHEN EXISTS (SELECT 1 FROM json_each(NEW.payload, '$.groups') WHERE json_extract(value, '$.reliable') = 1)
            BEGIN SELECT RAISE(ABORT, 'fake disk failure'); END""")
    host.sender.response = ({'member_role': 'admin', 'recv_msg_setting': 'all', 'allow_proactive_msg': True}, None)
    if entry == 'http':
        response = await host.api('POST', 'refresh', body={'group': 'group-a'})
        assert response.status == 503
    else:
        await host.event('群状态 刷新 group-a')
        assert '存储暂不可用' in host.sender.messages[-1][2]
    failed = (await (await host.api()).json())['data']['groups'][0]
    assert not failed['reliable']
    assert failed['status'] == 'historical' and failed['reason'] == 'persistence_failed'
    assert failed['bot_state']['member_role'] == 'member'
    assert (await (await host.api(bot='bot-b')).json())['data']['groups'] == []
    with sqlite3.connect(database) as connection:
        connection.execute('DROP TRIGGER reject_verified')
    result = (await (await host.api('POST', 'refresh', body={'group': 'group-a'})).json())['data']
    assert result['reliable'] and result['bot_state']['member_role'] == 'admin'
    assert result['version'] > failed['version']


@pytest.mark.parametrize('observed_group', ['', 'group-a', 'group-b'])
async def test_pending_commit_does_not_publish_or_overwrite_new_observations(group_host, observed_group):
    host = group_host
    entered = asyncio.Event()
    database = host.modules.get_context('group_state').get_data_path('group-state.db')
    blocker = sqlite3.connect(database)

    async def lock_final_write():
        blocker.execute('BEGIN IMMEDIATE')
        entered.set()

    host.sender.after_query = lock_final_write
    pending = asyncio.create_task(host.api('POST', 'refresh', body={'group': 'group-a'}))
    observation = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        during = (await (await host.api()).json())['data']
        assert not during['groups'][0]['reliable'] and during['groups'][0]['status'] == 'pending'
        if observed_group:
            observation = asyncio.create_task(host.event('普通消息', user='member-a', group=observed_group))
            await asyncio.sleep(0)
            observed = (await (await host.api()).json())['data']
            assert any(group['group'] == observed_group and group['source'] == 'native_event' for group in observed['groups'])
    finally:
        blocker.rollback()
        blocker.close()
        response = await pending
        if observation:
            await observation
    result = (await response.json())['data']
    final = (await (await host.api()).json())['data']
    if observed_group == 'group-a':
        assert not result['reliable'] and result['reason'] == 'state_changed'
        assert final['groups'][0]['source'] == 'native_event' and not final['groups'][0]['reliable']
    else:
        assert result['reliable'] and final['version'] > during['version']
        assert final['groups'][0]['version'] > during['groups'][0]['version']
    if observed_group == 'group-b':
        assert [group['group'] for group in final['groups']] == ['group-a', 'group-b']
    assert await host.modules.reload('group_state')
    restored = (await (await host.api()).json())['data']
    assert [group['group'] for group in restored['groups']] == [group['group'] for group in final['groups']]
    assert all(not group['reliable'] for group in restored['groups'])


async def test_reload_during_final_commit_rejects_old_http_result(group_host, monkeypatch):
    host = group_host
    entered, release = asyncio.Event(), asyncio.Event()
    old = host.state
    original = old.store.put

    async def delayed_return(bot, collection, key, record):
        await original(bot, collection, key, record)
        if record['groups']['group-a']['reliable']:
            entered.set()
            await release.wait()

    monkeypatch.setattr(old.store, 'put', delayed_return)
    pending = asyncio.create_task(host.api('POST', 'refresh', body={'group': 'group-a'}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert await host.modules.reload('group_state')
        current = (await (await host.api()).json())['data']
        assert not current['groups'][0]['reliable'] and current['groups'][0]['reason'] == 'process_restart'
    finally:
        release.set()
        response = await pending
    assert response.status == 503
    with pytest.raises(RuntimeError):
        await old.snapshot('bot-a')


async def test_disable_allows_event_loop_to_run_while_sqlite_finishes(group_host, monkeypatch):
    host = group_host
    entered, release = threading.Event(), threading.Event()
    released_before_timeout = []
    connect = sqlite3.connect

    def pause_write():
        entered.set()
        released_before_timeout.append(release.wait(2))
        return 1

    def test_connection(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.create_function('audit_pause', 0, pause_write)
        return connection

    monkeypatch.setattr(sqlite3, 'connect', test_connection)
    assert await host.modules.reload('group_state')
    database = host.modules.get_context('group_state').get_data_path('group-state.db')
    with sqlite3.connect(database) as connection:
        connection.execute("""CREATE TRIGGER delay_verified BEFORE INSERT ON extension_records
            WHEN EXISTS (SELECT 1 FROM json_each(NEW.payload, '$.groups') WHERE json_extract(value, '$.reliable') = 1)
            BEGIN SELECT audit_pause(); END""")
    pending = asyncio.create_task(host.api('POST', 'refresh', body={'group': 'group-a'}))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        asyncio.get_running_loop().call_later(0.01, release.set)
        assert await host.modules.disable('group_state', _persist=False)
    finally:
        release.set()
        response = await pending
    assert released_before_timeout == [True]
    assert response.status == 503
    assert (await host.api()).status == 404


@pytest.mark.parametrize('cancellations', [1, 2])
async def test_cancelled_refresh_finishes_its_write_before_new_facts_are_saved(group_host, monkeypatch, cancellations):
    host = group_host
    entered, release = threading.Event(), threading.Event()
    original = host.state.store._run

    def pause_before_database_lock(sql, params, read):
        # Fix thread scheduling at the SQLite boundary while retaining real writes.
        if sql.startswith('INSERT') and json.loads(params[3])['groups']['group-a']['reliable']:
            entered.set()
            if not release.wait(2):
                raise RuntimeError('fake storage scheduling timeout')
        return original(sql, params, read)

    monkeypatch.setattr(host.state.store, '_run', pause_before_database_lock)
    pending = asyncio.create_task(host.state.refresh('bot-a', 'group-a'))
    observation = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        for _ in range(cancellations):
            pending.cancel()
            await asyncio.sleep(0)
        observation = asyncio.create_task(host.event('普通消息', user='member-a', group='group-b'))
        during = (await (await host.api()).json())['data']
        assert not during['groups'][0]['reliable']
        assert not pending.done()  # Cancellation must drain the underlying write.
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        if observation:
            await observation
    assert await host.modules.reload('group_state')
    restored = (await (await host.api()).json())['data']
    assert [group['group'] for group in restored['groups']] == ['group-a', 'group-b']
    assert all(not group['reliable'] for group in restored['groups'])
