"""Shared ExtensionStore contract across the independently shipped storage copies (#61).

Each extension keeps its own ``storage.py`` so it installs alone. These tests run every
copy against the same real temporary SQLite behaviour, so a fix applied to one copy only
(WAL, version check, close ordering, ...) fails here instead of silently diverging.
Intentional differences are listed in ``EXTRAS`` / ``DRAINS_CANCELLED_WRITES`` and in
``docs/bot-modules-installation.md``; only those copies are held to them.
"""

import ast
import asyncio
import importlib.util
import inspect
import itertools
import json
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STORES = {
    'group_state': 'modules/group_state/storage.py',
    'access_control': 'modules/access_control/storage.py',
    'self_qa': 'modules/self_qa/storage.py',
    'account_sharing': 'plugins/account_sharing/storage.py',
}
_NINJA3_STORE = 'plugins/ninja3/storage.py'
if (Path(__file__).resolve().parents[2] / _NINJA3_STORE).exists():
    STORES['ninja3'] = _NINJA3_STORE
COMMON_API = {'ready', 'all', 'get', 'put', 'delete', 'close'}
EXTRAS = {'access_control': {'keys'}, 'ninja3': {'atomic', 'project'}}
GET_MISSING_KEYWORD = {'access_control'}
# Reviewed in-flight write drain on cancellation; not imposed on the other copies.
DRAINS_CANCELLED_WRITES = ['group_state', 'access_control']
_names = itertools.count()


def load(name):
    """Load one copy straight from its file, without its extension package."""
    spec = importlib.util.spec_from_file_location(f'_storage_contract_{name}_{next(_names)}', ROOT / STORES[name])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ExtensionStore


class Gate:
    """Pauses a real SQLite INSERT inside the store's worker thread."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self):
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError('fake storage scheduling timeout')


@pytest.fixture
def gate(monkeypatch):
    gate = Gate()
    original = sqlite3.connect

    def connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.create_function('contract_pause', 0, gate)
        return connection

    monkeypatch.setattr(sqlite3, 'connect', connect)
    yield gate
    gate.release.set()


def pause_inserts(path, collection='paused'):
    with sqlite3.connect(path) as connection:
        connection.execute(
            'CREATE TRIGGER contract_pause BEFORE INSERT ON extension_records '
            f"WHEN NEW.collection='{collection}' BEGIN SELECT contract_pause(); END"
        )


def raw_rows(path):
    with sqlite3.connect(path) as connection:
        return connection.execute('SELECT bot, collection, id, payload FROM extension_records ORDER BY rowid').fetchall()


parametrize_store = pytest.mark.parametrize('name', list(STORES))


def test_every_extension_store_copy_is_under_contract():
    found = {
        str(path.relative_to(ROOT))
        for pattern in ['modules/*/storage.py', 'plugins/*/storage.py']
        for path in ROOT.glob(pattern)
        if 'class ExtensionStore' in path.read_text(encoding='utf-8')
    }
    assert found == set(STORES.values())
    guide = (ROOT / 'docs/bot-modules-installation.md').read_text(encoding='utf-8')
    assert all(relative in guide for relative in STORES.values())


@parametrize_store
def test_copy_depends_only_on_standard_library(name):
    tree = ast.parse((ROOT / STORES[name]).read_text(encoding='utf-8'))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, 'relative imports would tie the copy to another file'
            imported.add(node.module.split('.')[0])
        elif isinstance(node, ast.Import):
            imported.update(alias.name.split('.')[0] for alias in node.names)
    assert imported <= sys.stdlib_module_names, imported - sys.stdlib_module_names


@parametrize_store
def test_public_surface_matches_documented_differences(name):
    store_class = load(name)
    public = {attribute for attribute in vars(store_class) if not attribute.startswith('_')}
    assert public == COMMON_API | EXTRAS.get(name, set())
    assert ('missing' in inspect.signature(store_class.get).parameters) == (name in GET_MISSING_KEYWORD)


@parametrize_store
async def test_v1_schema_and_wal_on_first_use(tmp_path, name):
    path = tmp_path / 'nested' / 'store.db'
    store = load(name)(path)
    try:
        assert not path.exists()  # Opened lazily; the parent directory is created on use.
        await store.ready()
        with sqlite3.connect(path) as connection:
            assert connection.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
            assert connection.execute('PRAGMA user_version').fetchone()[0] == 1
            columns = connection.execute('PRAGMA table_info(extension_records)').fetchall()
        assert [(column[1], column[3], column[5]) for column in columns] == [
            ('bot', 0, 1),
            ('collection', 0, 2),
            ('id', 0, 3),
            ('payload', 1, 0),
        ]
    finally:
        store.close()


@parametrize_store
async def test_newer_database_version_is_rejected_untouched(tmp_path, name):
    path = tmp_path / 'future.db'
    with sqlite3.connect(path) as connection:
        connection.execute('PRAGMA user_version=3')
    store = load(name)(path)
    try:
        for _ in range(2):  # A refused open leaves no half-open connection behind.
            with pytest.raises(RuntimeError, match='版本'):
                await store.ready()
    finally:
        store.close()
    with sqlite3.connect(path) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 3
        assert connection.execute('PRAGMA journal_mode').fetchone()[0] != 'wal'
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='extension_records'").fetchall() == []


@parametrize_store
async def test_records_are_isolated_by_bot_collection_and_id(tmp_path, name):
    store = load(name)(tmp_path / 'store.db')
    try:
        cells = [(bot, collection, key) for bot in ['bot-a', 'bot-b'] for collection in ['rules', 'settings'] for key in ['k1', 'k2']]
        for bot, collection, key in cells:
            await store.put(bot, collection, key, {'cell': [bot, collection, key]})
        for bot, collection, key in cells:
            assert await store.get(bot, collection, key) == {'cell': [bot, collection, key]}
        assert await store.all('bot-a', 'rules') == [{'cell': ['bot-a', 'rules', 'k1']}, {'cell': ['bot-a', 'rules', 'k2']}]
        assert await store.get('bot-c', 'rules', 'k1') is None
        assert await store.all('bot-a', 'missing') == []

        await store.delete('bot-a', 'rules', 'k1')
        assert await store.get('bot-a', 'rules', 'k1') is None
        remaining = [cell for cell in cells if cell != ('bot-a', 'rules', 'k1')]
        for bot, collection, key in remaining:
            assert await store.get(bot, collection, key) == {'cell': [bot, collection, key]}
        await store.delete('bot-a', 'rules', 'k1')  # Deleting an absent record is a no-op.
    finally:
        store.close()


@parametrize_store
async def test_writes_are_committed_upserts_that_survive_reopen(tmp_path, name):
    path = tmp_path / 'store.db'
    store_class = load(name)
    store = store_class(path)
    try:
        await store.put('bot-a', 'accounts', 'first', {'name': '第一', 'n': 1})
        await store.put('bot-a', 'accounts', 'second', {'name': '第二', 'n': 2})
        await store.put('bot-a', 'accounts', 'first', {'name': '第一', 'n': 3})
        # Visible to another connection while the store is still open: each write commits.
        rows = raw_rows(path)
        assert [row[:3] for row in rows] == [('bot-a', 'accounts', 'first'), ('bot-a', 'accounts', 'second')]
        assert json.loads(rows[0][3]) == {'name': '第一', 'n': 3} and '第一' in rows[0][3]
        assert await store.all('bot-a', 'accounts') == [{'name': '第一', 'n': 3}, {'name': '第二', 'n': 2}]
        await store.delete('bot-a', 'accounts', 'second')
        assert [row[2] for row in raw_rows(path)] == ['first']
    finally:
        store.close()
    reopened = store_class(path)
    try:
        assert await reopened.all('bot-a', 'accounts') == [{'name': '第一', 'n': 3}]
    finally:
        reopened.close()


@parametrize_store
async def test_concurrent_writes_are_serialized_without_loss(tmp_path, name):
    store = load(name)(tmp_path / 'store.db')
    try:
        await asyncio.gather(*(store.put('bot-a', 'items', f'k{index}', {'index': index}) for index in range(20)))
        assert sorted(item['index'] for item in await store.all('bot-a', 'items')) == list(range(20))
    finally:
        store.close()


@parametrize_store
async def test_close_waits_for_in_flight_write_then_refuses_use(tmp_path, gate, name):
    path = tmp_path / 'store.db'
    store_class = load(name)
    store = store_class(path)
    store.close()  # Closing before first use is harmless.
    store = store_class(path)
    await store.ready()
    pause_inserts(path)
    write = asyncio.create_task(store.put('bot-a', 'paused', 'k', {'v': 1}))
    closing = None
    try:
        assert await asyncio.to_thread(gate.entered.wait, 5)
        closing = asyncio.create_task(asyncio.to_thread(store.close))
        await asyncio.sleep(0.1)
        assert not closing.done()  # Shutdown is serialized behind the running write.
    finally:
        gate.release.set()
        await write
        if closing:
            await closing
    with pytest.raises(RuntimeError, match='已关闭'):
        await store.get('bot-a', 'paused', 'k')
    with pytest.raises(RuntimeError, match='已关闭'):
        await store.put('bot-a', 'other', 'k', {})
    store.close()  # Idempotent.
    reopened = store_class(path)
    try:
        assert await reopened.get('bot-a', 'paused', 'k') == {'v': 1}
    finally:
        reopened.close()


@pytest.mark.parametrize('name', DRAINS_CANCELLED_WRITES)
@pytest.mark.parametrize('cancellations', [1, 2])
async def test_cancelled_write_drains_before_cancellation_propagates(tmp_path, gate, name, cancellations):
    path = tmp_path / 'store.db'
    store = load(name)(path)
    await store.ready()
    pause_inserts(path)
    write = asyncio.create_task(store.put('bot-a', 'paused', 'k', {'v': 1}))
    try:
        assert await asyncio.to_thread(gate.entered.wait, 5)
        for _ in range(cancellations):
            write.cancel()
            await asyncio.sleep(0.05)
        assert not write.done()  # The SQLite worker cannot be stopped, so the caller waits for it.
    finally:
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await write
    try:
        assert [row[:3] for row in raw_rows(path)] == [('bot-a', 'paused', 'k')]  # Already committed, not later.
        assert await store.get('bot-a', 'paused', 'k') == {'v': 1}
    finally:
        store.close()


@pytest.mark.parametrize('name', ['access_control'])
async def test_keys_and_missing_sentinel_extension(tmp_path, name):
    store = load(name)(tmp_path / 'store.db')
    marker = object()
    try:
        await store.put('bot-a', 'c', 'b', {})
        await store.put('bot-a', 'c', 'a', {})
        await store.put('bot-b', 'c', 'z', {})
        assert await store.keys('bot-a', 'c') == ['b', 'a']
        assert await store.get('bot-a', 'c', 'gone', missing=marker) is marker
        assert await store.get('bot-a', 'c', 'gone') is None
    finally:
        store.close()
