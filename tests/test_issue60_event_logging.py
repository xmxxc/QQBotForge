"""before_event 接管事件的接收元信息与模块启用日志 (#60)

真实 Application._on_event、真实 HookManager 与真实 ModuleManager；模块为临时目录中
人工构造的 main.py，仅替换存储 (log_service) 与依赖安装边界，不访问网络或正式实例。
"""

import asyncio
import json
import logging
import os
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import core.module.manager as manager_mod
from core.application import Application
from core.base.config import cfg
from core.bot.registry import BotRegistry
from core.message.event import Event
from core.module.manager import ModuleManager

APPID = '123456'
SECRET_TEXT = '群状态 绑定 password=hunter2 token=tk-SECRET-42'

_CONSUMER = """
__module_meta__ = {'name': '群状态', 'version': '2.3.4'}


async def setup(ctx):
    @ctx.hook('before_event', priority=0)
    async def take(event):
        if event.event_type == 'GROUP_ADD_ROBOT' or '群状态' in (event.content or ''):
            return None
        return event
"""

_PASSTHROUGH = """
__module_meta__ = {'name': 'observer', 'version': '0.1'}


async def setup(ctx):
    @ctx.hook('before_event', priority=-10)
    async def look(event):
        return event
"""


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append((record.levelname, record.getMessage()))


def _write_module(modules_dir, name, source):
    path = os.path.join(modules_dir, name)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, 'main.py'), 'w', encoding='utf-8') as f:
        f.write(textwrap.dedent(source))


@pytest.fixture
def manager_log():
    handler = _Records()
    level = manager_mod.log.level
    manager_mod.log.setLevel(logging.INFO)
    manager_mod.log.addHandler(handler)
    yield handler
    manager_mod.log.removeHandler(handler)
    manager_mod.log.setLevel(level)


@pytest.fixture
def no_deps(monkeypatch):
    calls = []

    async def fake_install(name, target_dir, **kwargs):
        calls.append(name)

    monkeypatch.setattr(manager_mod, '_install_deps', fake_install)
    return calls


@pytest.fixture
async def event_app(tmp_path, sample_config_dir, no_deps):
    cfg._ready = False
    cfg.init(sample_config_dir)
    app = Application()
    store = SimpleNamespace(
        add=AsyncMock(), add_sync=Mock(return_value=True), db_queue=Mock(),
        db_fetch_one=AsyncMock(return_value=None), db_execute=AsyncMock(),
    )
    app._bot_registry = BotRegistry(str(tmp_path / 'logs'), on_event=app._on_event, on_event_received=app._prepare_event)
    app._bot_registry._bots[APPID] = SimpleNamespace(
        appid=APPID, name='bot', robot_qq='', secret='fake-secret',
        log_service=store, token_manager=SimpleNamespace(),
        sender=SimpleNamespace(refresh_group_info=AsyncMock(), ack_interaction=AsyncMock()),
    )
    web = []
    app._web_log_cb = lambda kind, entry: web.append((kind, entry))
    modules_dir = str(tmp_path / 'modules')
    _write_module(modules_dir, 'consumer', _CONSUMER)
    _write_module(modules_dir, 'observer', _PASSTHROUGH)
    manager = ModuleManager(modules_dir, app.hook_manager)
    manager.discover()
    app._module_manager = manager
    yield SimpleNamespace(app=app, manager=manager, store=store, web=web)
    await manager.shutdown()
    for worker in app._track_workers:
        worker.cancel()
    await asyncio.gather(*app._track_workers, return_exceptions=True)
    await asyncio.sleep(0)
    cfg._ready = False


def _group_at(msg_id, content, event_id=None):
    return Event.from_websocket(APPID, {
        'op': 0, 't': 'GROUP_AT_MESSAGE_CREATE', 'id': event_id or f'evt-{msg_id}',
        'd': {
            'id': msg_id, 'content': content, 'group_openid': 'group-1',
            'author': {'member_openid': 'owner-1'}, 'timestamp': '2026-10-24T00:00:00+08:00',
        },
    })


def _rows(store, kind):
    return [c.args[1] for c in store.add_sync.call_args_list if c.args[0] == kind]


def _assert_no_secret(obj):
    text = json.dumps(obj, ensure_ascii=False, default=str)
    for needle in ('hunter2', 'tk-SECRET-42', 'password', SECRET_TEXT):
        assert needle not in text, text


async def test_consumed_message_records_metadata_only(event_app):
    assert await event_app.manager.enable('consumer', _persist=False)
    assert await event_app.manager.enable('observer', _persist=False)

    await event_app.app._on_event(_group_at('m-1', SECRET_TEXT))

    rows = _rows(event_app.store, 'message')
    assert len(rows) == 1
    row = rows[0]
    assert row['direction'] == 'receive'
    assert (row['message_id'], row['user_id'], row['group_id']) == ('m-1', 'owner-1', 'group-1')
    assert row['content'] == '' and row['raw_message'] == ''
    # plugin_name 仅属于回复日志，接管模块写在 context 中
    assert row.get('plugin_name', '') == ''
    assert row['context'] == {'consumed': {'event_type': 'GROUP_AT_MESSAGE_CREATE', 'consumed_by': '群状态'}}
    assert _rows(event_app.store, 'lifecycle') == []
    _assert_no_secret(row)

    web_events = [e for k, e in event_app.web if k == 'event']
    assert len(web_events) == 1
    assert web_events[0]['event_type'] == 'GROUP_AT_MESSAGE_CREATE'
    assert '群状态' in web_events[0]['content']
    assert not [e for k, e in event_app.web if k == 'message']
    _assert_no_secret(event_app.web)


async def test_passthrough_message_keeps_single_normal_log(event_app):
    assert await event_app.manager.enable('consumer', _persist=False)

    await event_app.app._on_event(_group_at('m-2', '账户管理 查看'))

    rows = _rows(event_app.store, 'message')
    assert len(rows) == 1
    assert rows[0]['content'] == '账户管理 查看'
    assert 'context' not in rows[0]
    assert not [e for k, e in event_app.web if k == 'event']


async def test_consumed_non_message_event_goes_to_lifecycle_without_raw(event_app):
    assert await event_app.manager.enable('consumer', _persist=False)
    event = Event.from_websocket(APPID, {
        'op': 0, 't': 'GROUP_ADD_ROBOT', 'id': 'evt-add',
        'd': {'group_openid': 'group-9', 'op_member_openid': 'owner-1', 'timestamp': 1, 'note': 'token=tk-SECRET-42'},
    })

    await event_app.app._on_event(event)

    assert _rows(event_app.store, 'message') == []
    rows = _rows(event_app.store, 'lifecycle')
    assert len(rows) == 1
    assert rows[0]['type'] == 'GROUP_ADD_ROBOT'
    assert rows[0]['group_id'] == 'group-9'
    assert json.loads(rows[0]['extra']) == {'consumed': {'event_type': 'GROUP_ADD_ROBOT', 'consumed_by': '群状态'}}
    _assert_no_secret(rows)
    _assert_no_secret(event_app.web)


async def test_duplicate_consumed_event_logged_once(event_app, monkeypatch):
    original = cfg.get_bot_setting

    def setting(appid, key, default=None):
        return True if key == 'dedup.enabled' else original(appid, key, default)

    monkeypatch.setattr(cfg, 'get_bot_setting', setting)
    assert await event_app.manager.enable('consumer', _persist=False)

    await event_app.app._on_event(_group_at('m-3', SECRET_TEXT, event_id='evt-dup'))
    await event_app.app._on_event(_group_at('m-3', SECRET_TEXT, event_id='evt-dup'))

    assert len(_rows(event_app.store, 'message')) == 1


async def test_consumer_disabled_returns_to_normal_logging(event_app):
    assert await event_app.manager.enable('consumer', _persist=False)
    await event_app.app._on_event(_group_at('m-4', SECRET_TEXT))
    assert await event_app.manager.disable('consumer', _persist=False)

    await event_app.app._on_event(_group_at('m-5', '群状态 查看'))

    rows = _rows(event_app.store, 'message')
    assert [r['message_id'] for r in rows] == ['m-4', 'm-5']
    assert rows[0]['content'] == '' and 'consumed' in rows[0]['context']
    assert rows[1]['content'] == '群状态 查看' and 'context' not in rows[1]


async def test_hook_pipeline_reports_consumer_owner():
    from core.module.hook import HookManager

    hooks = HookManager()
    hooks.register('x', lambda d: d, owner='a', priority=1)
    hooks.register('x', lambda d: None, owner='b', priority=2)
    hooks.register('x', lambda d: d, owner='c', priority=3)
    assert await hooks.pipeline_with_owner('x', 1) == (None, 'b')
    assert await hooks.pipeline('x', 1) is None
    hooks.unregister_owner('b')
    assert await hooks.pipeline_with_owner('x', 1) == (1, None)
    assert await hooks.pipeline('x', 1) == 1


# ==================== 模块启用日志 ====================


def _lines(handler, word):
    return [(lvl, msg) for lvl, msg in handler.lines if word in msg]


async def test_enable_success_logs_name_and_version_once(tmp_path, manager_log, no_deps):
    _write_module(str(tmp_path), 'consumer', _CONSUMER)
    mm = ModuleManager(str(tmp_path))
    mm.discover()

    assert await mm.enable('consumer', _skip_deps=True)
    assert await mm.enable('consumer', _skip_deps=True)  # 已运行，不重复记录

    assert _lines(manager_log, '已启用') == [('INFO', '模块 [群状态(consumer)] v2.3.4 已启用')]
    await mm.shutdown()


async def test_enable_failure_logs_name_and_version(tmp_path, manager_log, no_deps, monkeypatch):
    _write_module(str(tmp_path), 'broken', """
        __module_meta__ = {'version': '9.9'}

        async def setup(ctx):
            raise RuntimeError('init boom')
    """)
    reported = []
    monkeypatch.setattr(manager_mod, 'report_error', lambda *a, **k: reported.append((a, k)))
    mm = ModuleManager(str(tmp_path))
    mm.discover()

    assert await mm.enable('broken') is False

    assert _lines(manager_log, '启用失败') == [('WARNING', '模块 [broken] v9.9 启用失败: init boom')]
    assert not _lines(manager_log, '已启用')
    assert mm._modules['broken'].error == 'init boom'
    assert not mm.is_enabled('broken')
    # 初始化失败仍按原方式上报错误，并附带版本
    assert len(reported) == 1
    assert reported[0][0][1] == 'broken'
    assert reported[0][1]['context']['version'] == '9.9'
    assert not mm.is_module_enabled_persist('broken')


async def test_import_failure_logs_and_does_not_persist(tmp_path, manager_log, no_deps, monkeypatch):
    _write_module(str(tmp_path), 'syntax', 'def setup(:\n')
    monkeypatch.setattr(manager_mod, 'report_error', lambda *a, **k: None)
    mm = ModuleManager(str(tmp_path))
    mm.discover()

    assert await mm.enable('syntax') is False
    assert len(_lines(manager_log, '启用失败')) == 1
    assert _lines(manager_log, '启用失败')[0][1].startswith('模块 [syntax] v1.0.0 启用失败')


async def test_disable_and_reload_logging_unchanged(tmp_path, manager_log, no_deps):
    _write_module(str(tmp_path), 'observer', _PASSTHROUGH)
    mm = ModuleManager(str(tmp_path))
    mm.discover()

    assert await mm.enable('observer', _skip_deps=True)
    assert await mm.reload('observer')
    assert mm.is_enabled('observer')
    assert await mm.disable('observer')

    assert _lines(manager_log, '已启用') == [
        ('INFO', '模块 [observer] v0.1 已启用'),
        ('INFO', '模块 [observer] v0.1 已启用'),
    ]
    assert _lines(manager_log, '已禁用') == [('INFO', '模块 [observer] 已禁用')]
    assert not mm.is_module_enabled_persist('observer')
