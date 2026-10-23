"""Extension folders install, load and persist independently (moved from the legacy account suite)."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ACCOUNT = {
    'id': 'legacy-account',
    'name': '旧账户',
    'login': 'fake-login',
    'password': 'fake-secret',
    'enabled': True,
    'description': '',
    'url': '',
    'groups': ['旧分组'],
}


def legacy_database(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE extension_records (bot TEXT, collection TEXT, id TEXT, payload TEXT NOT NULL, PRIMARY KEY(bot, collection, id))')
        connection.execute('PRAGMA user_version=1')
        for collection, key, value in records:
            connection.execute('INSERT INTO extension_records VALUES (?,?,?,?)', ('bot-a', collection, key, json.dumps(value, ensure_ascii=False)))


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
    result = subprocess.run(
        [sys.executable, '-c', script], cwd=tmp_path, env={**os.environ, 'PYTHONPATH': str(tmp_path)}, capture_output=True, text=True, timeout=45
    )
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
    result = subprocess.run(
        [sys.executable, '-c', script], cwd=tmp_path, env={**os.environ, 'PYTHONPATH': str(tmp_path)}, capture_output=True, text=True, timeout=45
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('package', ['modules.access_control.storage', 'modules.self_qa.storage', 'plugins.account_sharing.storage'])
async def test_failed_write_retains_legacy_record_and_retry_is_safe(tmp_path, package):
    from importlib import import_module

    path = tmp_path / 'legacy.db'
    legacy_database(path, [('accounts', ACCOUNT['id'], ACCOUNT)])
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TRIGGER fail_writes BEFORE INSERT ON extension_records BEGIN SELECT RAISE(ABORT, 'fake disk failure'); END")
    store = import_module(package).ExtensionStore(path)
    await store.ready()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            await store.put('bot-a', 'accounts', ACCOUNT['id'], {**ACCOUNT, 'password': 'replacement'})
        assert await store.get('bot-a', 'accounts', ACCOUNT['id']) == ACCOUNT
    finally:
        store.close()
    with sqlite3.connect(path) as connection:
        connection.execute('DROP TRIGGER fail_writes')
    for _ in range(2):
        store = import_module(package).ExtensionStore(path)
        try:
            await store.ready()
            await store.put('bot-a', 'accounts', ACCOUNT['id'], ACCOUNT)
            assert await store.all('bot-a', 'accounts') == [ACCOUNT]
        finally:
            store.close()
