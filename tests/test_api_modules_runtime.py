"""运行中新增/更新的模块目录：已鉴权后台列表 → 显式启用 (#58)

全部使用真实 ModuleManager、真实 HookManager 与真实后台路由；模块为临时目录中
人工构造的 main.py，依赖安装边界替换为记录器，不访问网络或正式实例。
"""

import asyncio
import json
import os
import shutil
import sys
import textwrap
from types import SimpleNamespace

import pytest

import core.module.manager as manager_mod
from core.module.hook import HookManager
from core.module.manager import ModuleManager
from tests.helpers import assert_success_response

_TEMPLATE = """
import asyncio
import os

__module_meta__ = {{'name': {display!r}, 'version': {version!r}, 'description': 'probe'}}

VERSION = {version!r}
_LOG = {log!r}


def _log(event):
    with open(_LOG, 'a', encoding='utf-8') as f:
        f.write(event + '\\n')


async def setup(ctx):
    _log('setup:' + {name!r} + ':' + VERSION)

    async def on_probe(*args):
        _log('hook:' + {name!r} + ':' + VERSION)

    ctx.register_hook('probe58', on_probe)
    await asyncio.sleep({delay!r})
    if {fail!r}:
        raise RuntimeError('setup boom ' + VERSION)
    return {{'name': {name!r}, 'version': VERSION}}


async def teardown():
    _log('teardown:' + {name!r} + ':' + VERSION)
"""


class Env:
    def __init__(self, base_dir, monkeypatch):
        self.base_dir = base_dir
        self.modules_dir = os.path.join(base_dir, 'modules')
        self.log_path = os.path.join(base_dir, 'events.log')
        self.hooks = HookManager()
        self.deps = []
        os.makedirs(self.modules_dir, exist_ok=True)
        self.mm = None

        async def fake_install(name, target_dir, **kwargs):
            self.deps.append((name, target_dir))
            self.log(f'deps:{name}')
            hook = self.on_deps.get(name)
            if hook:
                hook()

        self.on_deps = {}
        monkeypatch.setattr(manager_mod, '_install_deps', fake_install)

    def log(self, event):
        with open(self.log_path, 'a', encoding='utf-8') as f:
            f.write(event + '\n')

    def events(self):
        if not os.path.isfile(self.log_path):
            return []
        with open(self.log_path, encoding='utf-8') as f:
            return f.read().split()

    def write_module(self, name, *, version='1.0.0', display=None, fail=False, delay=0.0, source=None):
        mod_dir = os.path.join(self.modules_dir, name)
        os.makedirs(mod_dir, exist_ok=True)
        if source is None:
            source = _TEMPLATE.format(name=name, display=display or f'{name}-display', version=version, log=self.log_path, fail=fail, delay=delay)
        with open(os.path.join(mod_dir, 'main.py'), 'w', encoding='utf-8') as f:
            f.write(textwrap.dedent(source))
        with open(os.path.join(mod_dir, 'requirements.txt'), 'w', encoding='utf-8') as f:
            f.write('# none\n')
        return mod_dir

    def persisted(self):
        path = os.path.join(self.modules_dir, 'modules_enabled.json')
        if not os.path.isfile(path):
            return {}
        with open(path, encoding='utf-8') as f:
            return json.load(f)

    def owners(self):
        return [entry['owner'] for entry in self.hooks.list_hooks().get('probe58', [])]

    async def start(self):
        """模拟框架启动：发现 + 按启用标记启动。"""
        self.mm = ModuleManager(self.modules_dir, self.hooks)
        self.mm.discover()
        await self.mm.start_enabled()
        return self.mm


@pytest.fixture
async def env(setup_app, api_config_dir, monkeypatch):
    import web.tools._plugin_mgr.shared as shared

    e = Env(api_config_dir, monkeypatch)
    holder = SimpleNamespace(module_manager=None)
    previous = shared._state['bot_manager']
    shared._state['bot_manager'] = holder
    e.holder = holder
    yield e
    if e.mm is not None:
        await e.mm.shutdown()
    shared._state['bot_manager'] = previous
    for key in [k for k in sys.modules if k.startswith('modules.probe58')]:
        sys.modules.pop(key, None)


async def start(env):
    mm = await env.start()
    env.holder.module_manager = mm
    return mm


async def toggle(client, cookies, name, action='enable', **kwargs):
    resp = await client.post('/api/modules/toggle', json={'name': name, 'action': action}, cookies=cookies, **kwargs)
    return resp.status, await resp.json()


async def scan(client, cookies):
    resp = await client.get('/api/modules/scan', cookies=cookies)
    data = await resp.json()
    assert_success_response(data)
    return {m['name']: m for m in data['modules']}


# ==================== 新目录 → 列表 → 启用 → 功能入口 ====================


async def test_runtime_added_module_listed_then_enabled(api_client, auth_cookies, env):
    mm = await start(env)
    env.write_module('probe58_new')

    listed = await scan(api_client, auth_cookies)
    assert listed['probe58_new']['enabled'] is False

    status, data = await toggle(api_client, auth_cookies, 'probe58_new')
    assert status == 200
    assert data['success'] is True, data

    # 依赖安装先于导入/setup，且针对当前磁盘目录
    assert env.events() == ['deps:probe58_new', 'setup:probe58_new:1.0.0']
    assert env.deps == [('probe58_new', os.path.join(env.modules_dir, 'probe58_new'))]
    assert mm.get('probe58_new') == {'name': 'probe58_new', 'version': '1.0.0'}
    assert env.persisted() == {'probe58_new': True}

    await env.hooks.emit('probe58')
    assert env.events()[-1] == 'hook:probe58_new:1.0.0'

    listed = await scan(api_client, auth_cookies)
    assert listed['probe58_new']['enabled'] is True
    assert listed['probe58_new']['persist_enabled'] is True


async def test_enabling_one_new_module_does_not_enable_others(api_client, auth_cookies, env):
    mm = await start(env)
    env.write_module('probe58_a')
    env.write_module('probe58_b')

    status, data = await toggle(api_client, auth_cookies, 'probe58_a')
    assert data['success'] is True, data
    assert mm.is_enabled('probe58_a')
    assert not mm.is_enabled('probe58_b')
    assert env.persisted() == {'probe58_a': True}
    assert 'setup:probe58_b:1.0.0' not in env.events()


async def test_startup_persisted_module_still_starts(api_client, auth_cookies, env):
    env.write_module('probe58_boot')
    with open(os.path.join(env.modules_dir, 'modules_enabled.json'), 'w', encoding='utf-8') as f:
        json.dump({'probe58_boot': True}, f)
    env.write_module('probe58_idle')

    mm = await start(env)
    assert mm.is_enabled('probe58_boot')
    assert not mm.is_enabled('probe58_idle')
    assert env.events() == ['deps:probe58_boot', 'setup:probe58_boot:1.0.0']


# ==================== 未运行模块元数据/入口更新 ====================


async def test_not_running_module_updated_on_disk_loads_current_content(api_client, auth_cookies, env):
    env.write_module('probe58_upd', version='1.0.0', display='Old Name')
    mm = await start(env)
    assert not mm.is_enabled('probe58_upd')

    env.write_module('probe58_upd', version='2.0.0', display='New Name')
    status, data = await toggle(api_client, auth_cookies, 'probe58_upd')
    assert data['success'] is True, data

    assert mm.get('probe58_upd')['version'] == '2.0.0'
    info = {m['name']: m for m in mm.list_modules()}['probe58_upd']
    assert (info['display_name'], info['version']) == ('New Name', '2.0.0')
    # Hook owner 跟随当前元数据，禁用时能完整注销
    assert env.owners() == ['New Name']

    status, data = await toggle(api_client, auth_cookies, 'probe58_upd', 'disable')
    assert data['success'] is True, data
    assert env.owners() == []
    assert env.persisted() == {'probe58_upd': False}


# ==================== 已运行模块不隐式重载 ====================


async def test_running_module_not_reloaded_by_scan_or_repeated_enable(api_client, auth_cookies, env):
    env.write_module('probe58_run', version='1.0.0')
    with open(os.path.join(env.modules_dir, 'modules_enabled.json'), 'w', encoding='utf-8') as f:
        json.dump({'probe58_run': True}, f)
    mm = await start(env)
    instance = mm.get('probe58_run')
    ctx = mm.get_context('probe58_run')

    env.write_module('probe58_run', version='2.0.0', display='Changed')
    await scan(api_client, auth_cookies)
    for _ in range(2):
        status, data = await toggle(api_client, auth_cookies, 'probe58_run')
        assert data['success'] is True, data
    assert mm.discover_module('probe58_run') is not None

    assert mm.get('probe58_run') is instance
    assert mm.get_context('probe58_run') is ctx
    assert [e for e in env.events() if e.startswith(('setup', 'teardown'))] == ['setup:probe58_run:1.0.0']
    assert env.owners() == ['probe58_run-display']
    await env.hooks.emit('probe58')
    assert env.events()[-1] == 'hook:probe58_run:1.0.0'


async def test_enable_running_module_records_missing_enable_flag(api_client, auth_cookies, env):
    env.write_module('probe58_flag')
    mm = await start(env)
    assert await mm.enable('probe58_flag', _persist=False)
    assert env.persisted() == {}

    status, data = await toggle(api_client, auth_cookies, 'probe58_flag')
    assert data['success'] is True, data
    assert env.persisted() == {'probe58_flag': True}
    assert [e for e in env.events() if e.startswith('setup')] == ['setup:probe58_flag:1.0.0']


# ==================== 目录删除 / 坏入口 / 初始化失败 ====================


async def test_deleted_directory_reports_missing_and_drops_stale_record(api_client, auth_cookies, env):
    mod_dir = env.write_module('probe58_gone')
    mm = await start(env)
    assert 'probe58_gone' in {m['name'] for m in mm.list_modules()}

    shutil.rmtree(mod_dir)
    assert 'probe58_gone' not in await scan(api_client, auth_cookies)
    status, data = await toggle(api_client, auth_cookies, 'probe58_gone')
    assert data['success'] is False
    assert '不存在' in data['message']
    assert 'probe58_gone' not in {m['name'] for m in mm.list_modules()}
    assert env.persisted() == {}
    assert env.deps == []


async def test_broken_entry_fails_explainably_then_recovers(api_client, auth_cookies, env):
    mm = await start(env)
    env.write_module('probe58_bad', source='def setup(ctx):\n    return (\n')

    status, data = await toggle(api_client, auth_cookies, 'probe58_bad')
    assert data['success'] is False
    assert 'SyntaxError' in data['message'] or 'never closed' in data['message'] or 'syntax' in data['message'].lower()
    assert env.persisted() == {}
    assert not mm.is_enabled('probe58_bad')
    assert 'modules.probe58_bad' not in sys.modules
    listed = await scan(api_client, auth_cookies)
    assert listed['probe58_bad']['enabled'] is False
    assert listed['probe58_bad']['error']

    env.write_module('probe58_bad')
    status, data = await toggle(api_client, auth_cookies, 'probe58_bad')
    assert data['success'] is True, data
    assert env.persisted() == {'probe58_bad': True}
    assert (await scan(api_client, auth_cookies))['probe58_bad']['error'] is None


async def test_setup_failure_leaves_no_hooks_or_flag_and_retry_is_clean(api_client, auth_cookies, env):
    mm = await start(env)
    env.write_module('probe58_fail', fail=True)

    status, data = await toggle(api_client, auth_cookies, 'probe58_fail')
    assert data['success'] is False
    assert 'setup boom 1.0.0' in data['message']
    assert env.owners() == []
    assert env.persisted() == {}
    assert not mm.is_enabled('probe58_fail')

    env.write_module('probe58_fail', version='1.0.1')
    status, data = await toggle(api_client, auth_cookies, 'probe58_fail')
    assert data['success'] is True, data
    assert env.owners() == ['probe58_fail-display']
    await env.hooks.emit('probe58')
    assert [e for e in env.events() if e.startswith('hook')] == ['hook:probe58_fail:1.0.1']


async def test_directory_removed_while_enabling(api_client, auth_cookies, env):
    mm = await start(env)
    mod_dir = env.write_module('probe58_race')
    env.on_deps['probe58_race'] = lambda: shutil.rmtree(mod_dir)

    status, data = await toggle(api_client, auth_cookies, 'probe58_race')
    assert data['success'] is False
    assert '入口不存在' in data['message']
    assert not mm.is_enabled('probe58_race')
    assert env.persisted() == {}
    assert env.owners() == []


# ==================== 名称/路径校验 ====================


async def test_invalid_names_are_never_discovered(api_client, auth_cookies, env):
    mm = await start(env)
    # modules/ 的父目录与兄弟目录都放置可执行入口，任何越界发现都会留下 setup 记录
    marker = os.path.join(env.base_dir, 'escaped')
    escape = f'def setup(ctx):\n    open({marker!r}, "w").close()\n'
    with open(os.path.join(env.base_dir, 'main.py'), 'w', encoding='utf-8') as f:
        f.write(escape)
    os.makedirs(os.path.join(env.base_dir, 'plugins', 'evil'), exist_ok=True)
    with open(os.path.join(env.base_dir, 'plugins', 'evil', 'main.py'), 'w', encoding='utf-8') as f:
        f.write(escape)
    env.write_module('_private')
    env.write_module('probe58_ok')

    for name in ('..', '.', '../plugins/evil', 'probe58_ok/..', os.path.join(env.base_dir, 'plugins', 'evil'), '_private', 'probe58_ok\x00'):
        status, data = await toggle(api_client, auth_cookies, name)
        assert data['success'] is False, (name, data)
        assert status in (200, 400), (name, status)
    for name in (123, ['probe58_ok'], None):
        status, data = await toggle(api_client, auth_cookies, name)
        assert status == 400, (name, status, data)

    assert not os.path.exists(marker)
    assert env.deps == []
    assert {m['name'] for m in mm.list_modules()} == set()
    assert env.persisted() == {}


# ==================== 并发启用 ====================


async def test_concurrent_enable_runs_setup_once(api_client, auth_cookies, env):
    mm = await start(env)
    env.write_module('probe58_conc', delay=0.1)

    results = await asyncio.gather(*(toggle(api_client, auth_cookies, 'probe58_conc') for _ in range(3)))
    assert all(data['success'] is True for _, data in results), results
    assert [e for e in env.events() if e.startswith('setup')] == ['setup:probe58_conc:1.0.0']
    assert env.owners() == ['probe58_conc-display']
    assert env.persisted() == {'probe58_conc': True}
    assert mm.is_enabled('probe58_conc')


async def test_concurrent_enable_and_market_refresh_serialized(api_client, auth_cookies, env):
    mm = await start(env)
    env.write_module('probe58_mix', delay=0.05)

    results = await asyncio.gather(toggle(api_client, auth_cookies, 'probe58_mix'), mm.refresh_and_enable('probe58_mix'))
    assert results[0][1]['success'] is True
    assert results[1] is True
    lifecycle = [e for e in env.events() if e.startswith(('setup', 'teardown'))]
    # 无论谁先拿到锁，都不会出现两个实例并存或孤儿实例
    assert lifecycle in (
        ['setup:probe58_mix:1.0.0'],
        ['setup:probe58_mix:1.0.0', 'teardown:probe58_mix:1.0.0', 'setup:probe58_mix:1.0.0'],
    )
    assert env.owners() == ['probe58_mix-display']
    await mm.shutdown()
    assert env.events()[-1] == 'teardown:probe58_mix:1.0.0'
    assert env.owners() == []


# ==================== 鉴权与同源 ====================


async def test_toggle_requires_auth_and_same_origin(api_client, auth_cookies, env):
    mm = await start(env)
    env.write_module('probe58_auth')

    resp = await api_client.post('/api/modules/toggle', json={'name': 'probe58_auth', 'action': 'enable'})
    assert resp.status == 401
    resp = await api_client.post(
        '/api/modules/toggle',
        json={'name': 'probe58_auth', 'action': 'enable'},
        cookies=auth_cookies,
        headers={'Origin': 'http://evil.example'},
    )
    assert resp.status == 403
    resp = await api_client.get('/api/modules/scan')
    assert resp.status == 401
    assert not mm.is_enabled('probe58_auth')
    assert env.deps == []
    assert 'probe58_auth' not in {m['name'] for m in mm.list_modules()}


# ==================== 原市场 / 禁用 / 重载路径 ====================


async def test_market_auto_enable_refreshes_running_module(api_client, auth_cookies, env, monkeypatch):
    import web.tools._market.install as market_install

    env.write_module('probe58_mkt', version='1.0.0')
    mm = await start(env)
    monkeypatch.setattr(market_install, 'get_app', lambda: env.holder)
    assert await mm.enable('probe58_mkt')

    env.write_module('probe58_mkt', version='2.0.0')
    await market_install._auto_enable_module('probe58_mkt')
    assert mm.get('probe58_mkt')['version'] == '2.0.0'
    assert [e for e in env.events() if e.startswith(('setup', 'teardown'))] == [
        'setup:probe58_mkt:1.0.0',
        'teardown:probe58_mkt:1.0.0',
        'setup:probe58_mkt:2.0.0',
    ]
    assert env.persisted() == {'probe58_mkt': True}

    # 市场路径失败时保留启用意图（既有约定），但目录不存在不写标记
    env.write_module('probe58_mkt_fail', fail=True)
    assert await mm.refresh_and_enable('probe58_mkt_fail') is False
    assert env.persisted()['probe58_mkt_fail'] is True
    assert env.owners() == ['probe58_mkt-display']
    assert await mm.refresh_and_enable('probe58_absent') is False
    assert 'probe58_absent' not in env.persisted()


async def test_disable_and_reload_unchanged(api_client, auth_cookies, env):
    env.write_module('probe58_dr', version='1.0.0')
    mm = await start(env)
    status, data = await toggle(api_client, auth_cookies, 'probe58_dr')
    assert data['success'] is True

    env.write_module('probe58_dr', version='1.1.0')
    assert await mm.reload('probe58_dr')
    assert mm.get('probe58_dr')['version'] == '1.1.0'
    assert env.persisted() == {'probe58_dr': True}
    assert env.owners() == ['probe58_dr-display']

    status, data = await toggle(api_client, auth_cookies, 'probe58_dr', 'disable')
    assert data['success'] is True
    assert not mm.is_enabled('probe58_dr')
    assert env.owners() == []
    assert env.persisted() == {'probe58_dr': False}
    assert 'modules.probe58_dr' not in sys.modules

    # 未运行模块禁用仍成功并写关闭标记
    status, data = await toggle(api_client, auth_cookies, 'probe58_unknown', 'disable')
    assert data['success'] is True
    assert env.persisted()['probe58_unknown'] is False


async def test_cancelled_setup_leaves_no_hooks_and_retry_is_clean(api_client, auth_cookies, env):
    mm = await start(env)
    env.write_module('probe58_cancel', delay=30)

    task = asyncio.create_task(mm.enable('probe58_cancel'))
    for _ in range(100):
        if env.owners():
            break
        await asyncio.sleep(0.01)
    assert env.owners() == ['probe58_cancel-display']
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert env.owners() == []
    assert not mm.is_enabled('probe58_cancel')
    assert 'modules.probe58_cancel' not in sys.modules
    assert env.persisted() == {}

    env.write_module('probe58_cancel')
    status, data = await toggle(api_client, auth_cookies, 'probe58_cancel')
    assert data['success'] is True, data
    assert env.owners() == ['probe58_cancel-display']
