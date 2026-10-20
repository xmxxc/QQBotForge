"""Deployment contract on unchanged framework sources and the three extension folders."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_three_extension_folders_load_on_unmodified_framework(tmp_path):
    root = Path(__file__).resolve().parents[2]
    for package in ['core', 'web']:
        shutil.copytree(root / package, tmp_path / package, ignore=shutil.ignore_patterns('__pycache__'))
    for relative in ['modules/access_control', 'modules/self_qa', 'plugins/account_sharing']:
        shutil.copytree(root / relative, tmp_path / relative, ignore=shutil.ignore_patterns('data', '__pycache__'))
    script = """
import asyncio
from pathlib import Path
from core.base.config import cfg
from core.module.manager import ModuleManager
from core.plugin.manager import PluginManager

async def verify():
    Path('config').mkdir()
    Path('config/bot.yaml').write_text('bots:\\n  - appid: fake-bot\\n    owner_ids: [fake-owner]\\n')
    cfg.init('config')
    modules = ModuleManager('modules')
    modules.discover()
    assert await modules.enable('access_control', _skip_deps=True, _persist=False)
    assert await modules.enable('self_qa', _skip_deps=True, _persist=False)
    plugins = PluginManager('plugins')
    await plugins.load_all()
    entry = plugins.plugins['account_sharing'].module
    service = entry._instance
    account = await service.manage('fake-bot', 'create', {'name':'bundle', 'login':'fake', 'password':'fake-secret'})
    assert (await service.manage('fake-bot', 'preview', key=account['id']))['password'] == 'fake-secret'
    qa = modules.get('self_qa')
    await qa.manage('fake-bot', 'standalone', 'create', {'name':'reply','kind':'text','target':'hello','keywords':['hi']})
    assert (await qa.match('fake-bot', 'standalone', 'hi'))[0]['target'] == 'hello'
    acl = modules.get('access_control')
    await acl.register_resources('fake-bot', 'account_sharing', [{'id':account['id']}])
    await acl.execute('fake-bot', 'whitelist', function='account_sharing', group='fake-group', enabled=True)
    await acl.execute('fake-bot', 'grant', function='account_sharing', group='fake-group', member='fake-member', resources=[account['id']], identity_type='group_member_openid', confirmed=True)
    decision = await acl.decide('fake-bot', 'account_sharing', 'fake-group', 'fake-member', account['id'])
    assert not decision['allowed'] and decision['reason'] == 'event_integration_unavailable'
    assert (await acl.snapshot('fake-bot'))['capabilities']['lifecycle_events'] is False
    await plugins.unload('account_sharing')
    await modules.shutdown()

asyncio.run(verify())
"""
    result = subprocess.run(
        [sys.executable, '-c', script], cwd=tmp_path, env={**os.environ, 'PYTHONPATH': str(tmp_path)}, capture_output=True, text=True, timeout=45
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def test_member_queries_stop_without_observer_but_owner_preview_remains_available(host):
    from test_access import grant
    from test_binding import bind

    account = await host.create()
    await grant(host, account)
    await bind(host)
    host.service.access_provider().lifecycle_available = False
    host.sender.messages.clear()
    await host.event(f'账户查询 {account["id"]}', user='private-a')
    assert not any('fake-secret' in str(message) for message in host.sender.messages)
    await host.event(f'账户管理 预览 {account["id"]}')
    assert 'fake-secret' in host.sender.messages[-1][2]


async def test_unavailable_integration_does_not_invite_unusable_binding(host):
    from test_access import grant

    account = await host.create()
    host.service.access_provider().lifecycle_available = False
    host.sender.messages.clear()
    response = await grant(host, account)
    assert response.status == 200
    result = (await response.json())['data']['operation_result']
    assert result['authorization'] == 'saved'
    assert result['event_integration'] == 'unavailable' and result['guidance'] == 'unavailable'
    assert not any('账户绑定' in str(message) for message in host.sender.messages)


def test_qa_folder_alone_persists_and_replies_without_access_control(tmp_path):
    root = Path(__file__).resolve().parents[2]
    for relative in ['core', 'web', 'modules/self_qa']:
        shutil.copytree(root / relative, tmp_path / relative, ignore=shutil.ignore_patterns('data', '__pycache__'))
    script = """
import asyncio
from core.module.manager import ModuleManager

async def verify():
    manager = ModuleManager('modules')
    manager.discover()
    assert [item['name'] for item in manager.list_modules()] == ['self_qa']
    assert await manager.enable('self_qa', _skip_deps=True, _persist=False)
    qa = manager.get('self_qa')
    rule = await qa.manage('fake-bot', 'public', 'create', {'name': '帮助', 'kind': 'text', 'target': '公开说明', 'keywords': ['帮助']})
    await manager.disable('self_qa', _persist=False)
    assert await manager.enable('self_qa', _skip_deps=True, _persist=False)
    qa = manager.get('self_qa')
    assert (await qa.manage('fake-bot', 'public', 'get', key=rule['id']))['id'] == rule['id']
    sent = []
    async def send(text):
        sent.append(text)
        return True
    await qa.answer('fake-bot', 'public', '帮助', send, allowed=True)
    await qa.answer('other-bot', 'public', '帮助', send, allowed=True)
    assert sent == ['公开说明']
    assert not __import__('pathlib').Path('modules/access_control').exists()
    await manager.shutdown()

asyncio.run(verify())
"""
    result = subprocess.run([sys.executable, '-c', script], cwd=tmp_path,
                            env={**os.environ, 'PYTHONPATH': str(tmp_path)}, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('extension', ['modules/access_control', 'plugins/account_sharing'])
def test_extension_storage_loads_without_other_extension_folders(tmp_path, extension):
    root = Path(__file__).resolve().parents[2]
    for relative in ['core', 'web', extension]:
        shutil.copytree(root / relative, tmp_path / relative, ignore=shutil.ignore_patterns('data', '__pycache__'))
    script = """
import asyncio
from pathlib import Path
from core.base.config import cfg
from core.module.manager import ModuleManager
from core.plugin.manager import PluginManager

async def verify():
    Path('config').mkdir()
    Path('config/bot.yaml').write_text('bots:\\n  - appid: fake-bot\\n    owner_ids: [fake-owner]\\n')
    cfg.init('config')
    if Path('modules/access_control').exists():
        manager = ModuleManager('modules')
        manager.discover()
        assert await manager.enable('access_control', _skip_deps=True, _persist=False)
        acl = manager.get('access_control')
        await acl.execute('fake-bot', 'whitelist', function='demo', group='fake-group', enabled=True)
        assert (await acl.snapshot('fake-bot'))['whitelist']
        await manager.shutdown()
    else:
        manager = PluginManager('plugins')
        await manager.load_all()
        service = manager.plugins['account_sharing'].module._instance
        account = await service.manage('fake-bot', 'create', {'name': 'demo', 'login': 'fake', 'password': 'fake-secret'})
        assert (await service.manage('fake-bot', 'preview', key=account['id']))['password'] == 'fake-secret'
        await manager.unload('account_sharing')

asyncio.run(verify())
"""
    result = subprocess.run([sys.executable, '-c', script], cwd=tmp_path,
                            env={**os.environ, 'PYTHONPATH': str(tmp_path)}, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
