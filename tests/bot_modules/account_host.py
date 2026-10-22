"""Group state, authorization, Q&A and the account plugin loaded by the real plugin manager."""

import asyncio
import contextlib
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).absolute().parents[2]


@pytest.fixture
async def account_host(access_host):
    from core.plugin.manager import PluginManager

    host = access_host
    shutil.copytree(ROOT / 'modules/self_qa', Path(host.modules._dir) / 'self_qa', ignore=shutil.ignore_patterns('data', '__pycache__'))
    host.modules.discover_module('self_qa')
    assert await host.modules.enable('self_qa', _skip_deps=True, _persist=False)
    plugins = Path(host.modules._dir).parent / 'plugins'
    shutil.copytree(ROOT / 'plugins/account_sharing', plugins / 'account_sharing', ignore=shutil.ignore_patterns('data', '__pycache__'))
    manager = PluginManager(str(plugins))
    await manager.load_all()
    host.application._plugin_manager = manager
    host.plugins = manager

    async def account_api(method='GET', path='accounts', body=None, *, bot='bot-a', authenticated=True):
        response = await host.http.request(
            method,
            '/api/ext/account-sharing/' + path,
            params={'bot': bot},
            json=body,
            headers={'Cookie': 'elaina_session=group-test'} if authenticated else {},
        )
        return response.status, await response.json()

    async def create(name='共享账户', password='fake-secret', bot='bot-a', **fields):
        status, data = await account_api('POST', 'accounts', {'name': name, 'login': 'fake-login', 'password': password, **fields}, bot=bot)
        assert status == 201, data
        return data['data']

    def texts(target=None):
        result = [m[2] for m in host.sender.messages if target is None or m[1] == target]
        host.sender.messages.clear()
        return result

    from core.base import tasks

    original_event = host.event

    async def event(*args, **kwargs):
        # Plugin handlers run as spawned tasks; wait for those this event started.
        before = set(tasks._background_tasks)
        result = await original_event(*args, **kwargs)
        for _ in range(20):
            started = set(tasks._background_tasks) - before
            if not started:
                break
            await asyncio.gather(*started, return_exceptions=True)
            before |= started
        return result

    host.event = event

    async def click(data, *, bot='bot-a', user='member-a', group=''):
        from core.message.event import Event

        interaction = Event.from_websocket(
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
        interaction.start_ack_countdown()
        before = set(tasks._background_tasks)
        host.application._prepare_event(interaction)
        await host.application._on_event(interaction)
        for _ in range(20):
            started = set(tasks._background_tasks) - before
            if not started:
                break
            await asyncio.gather(*started, return_exceptions=True)
            before |= started
        return await interaction.wait_ack_code()

    host.click = click
    host.account_api, host.create, host.texts = account_api, create, texts
    try:
        yield host
    finally:
        for plugin in list(manager._plugins):
            with contextlib.suppress(Exception):
                await manager.unload(plugin)
