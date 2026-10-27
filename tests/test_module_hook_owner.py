"""同显示名模块的 Hook 归属隔离 (#90)

真实 ModuleManager / HookManager / 已鉴权后台路由 + 临时模块；断言走真实 before_send 管道 trace。
"""

import asyncio
import os
import shutil

import pytest

from core.module.hook import HookManager
from tests.test_api_modules_runtime import env as env  # noqa: F401
from tests.test_api_modules_runtime import start, toggle


def _source(tag, *, display='same-visible-owner', priority=100, mode='ok', saved=None, modules_dir=None):
    tail = {
        'ok': f'return {{"tag": {tag!r}, "ctx": ctx}}',
        'raise': 'raise RuntimeError("setup boom")',
        'cancel': 'await asyncio.sleep(30)',
        # setup 成功后阻断 modules 目录，使启用标记保存失败 (#58 F1 语义)
        'persist': f'os.rename({modules_dir!r}, {saved!r}); open({modules_dir!r}, "w").close(); return {{"tag": {tag!r}, "ctx": ctx}}',
    }[mode]
    return f"""
import asyncio
import os

__module_meta__ = {{'name': {display!r}}}


async def setup(ctx):
    async def before_send(data):
        data['trace'].append({tag!r})
        return data

    ctx.register_hook('before_send', before_send, priority={priority})
    {tail}
"""


async def trace(env):
    return (await env.hooks.pipeline('before_send', {'trace': []}))['trace']


def owners(env):
    return [e['owner'] for e in env.hooks.list_hooks().get('before_send', [])]


async def test_failed_same_display_module_leaves_only_running_module(api_client, auth_cookies, env):
    """原遗留反例：[A,failed-B] → [A,failed-B,retry-B] → [] 现在应为 [A] → [A,B] → [A]。"""
    env.write_module('probe90_a', source=_source('A', priority=10))
    env.write_module('probe90_b', source=_source('failed-B', priority=20, mode='raise'))
    mm = await start(env)
    assert (await toggle(api_client, auth_cookies, 'probe90_a'))[1]['success']
    assert not (await toggle(api_client, auth_cookies, 'probe90_b'))[1]['success']
    assert await trace(env) == ['A']

    env.write_module('probe90_b', source=_source('retry-B', priority=20))
    assert (await toggle(api_client, auth_cookies, 'probe90_b'))[1]['success']
    assert await trace(env) == ['A', 'retry-B']
    # 展示名保留：两个注册都显示同一个人类可读 owner
    assert owners(env) == ['same-visible-owner', 'same-visible-owner']

    assert (await toggle(api_client, auth_cookies, 'probe90_b', 'disable'))[1]['success']
    assert mm.is_enabled('probe90_a')
    assert await trace(env) == ['A']

    assert (await toggle(api_client, auth_cookies, 'probe90_a', 'disable'))[1]['success']
    assert await trace(env) == []


async def test_cancelled_same_display_setup_only_drops_its_own_hooks(api_client, auth_cookies, env):
    env.write_module('probe90_a', source=_source('A', priority=10))
    env.write_module('probe90_b', source=_source('pending-B', priority=20, mode='cancel'))
    mm = await start(env)
    assert await mm.enable('probe90_a')

    task = asyncio.create_task(mm.enable('probe90_b'))
    async with asyncio.timeout(5):
        while len(owners(env)) < 2:
            await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await trace(env) == ['A']

    env.write_module('probe90_b', source=_source('retry-B', priority=20))
    assert await mm.enable('probe90_b')
    assert await trace(env) == ['A', 'retry-B']


async def test_different_display_names_control(api_client, auth_cookies, env):
    env.write_module('probe90_a', source=_source('A', display='owner-a', priority=10))
    env.write_module('probe90_c', source=_source('C', display='owner-c', priority=20))
    mm = await start(env)
    assert await mm.enable('probe90_a') and await mm.enable('probe90_c')
    assert await trace(env) == ['A', 'C']
    assert await mm.disable('probe90_c')
    assert await trace(env) == ['A']
    assert owners(env) == ['owner-a']


async def test_reload_and_stale_context_cannot_touch_new_or_sibling_instance(api_client, auth_cookies, env):
    env.write_module('probe90_a', source=_source('A1', priority=10))
    env.write_module('probe90_b', source=_source('B', priority=20))
    mm = await start(env)
    assert await mm.enable('probe90_a') and await mm.enable('probe90_b')
    old_ctx = mm.get('probe90_a')['ctx']

    env.write_module('probe90_a', source=_source('A2', priority=10))
    assert await mm.reload('probe90_a')
    assert await trace(env) == ['A2', 'B']

    # 旧上下文迟到注册被拒绝；迟到的实例级清理只作用于旧实例
    async def late(data):
        data['trace'].append('late-old-A')
        return data

    with pytest.raises(RuntimeError):
        old_ctx.register_hook('before_send', late)
    env.hooks._unregister_token(old_ctx)
    assert await trace(env) == ['A2', 'B']
    assert mm.get('probe90_a')['ctx'] is not old_ctx


async def test_market_refresh_of_same_display_module_keeps_sibling(api_client, auth_cookies, env):
    env.write_module('probe90_a', source=_source('A', priority=10))
    env.write_module('probe90_b', source=_source('B1', priority=20))
    mm = await start(env)
    assert await mm.enable('probe90_a') and await mm.enable('probe90_b')

    env.write_module('probe90_b', source=_source('B2', priority=20))
    assert await mm.refresh_and_enable('probe90_b')
    assert await trace(env) == ['A', 'B2']

    env.write_module('probe90_b', source=_source('B3', priority=20, mode='raise'))
    assert not await mm.refresh_and_enable('probe90_b')
    assert await trace(env) == ['A']
    assert env.persisted()['probe90_b'] is True  # 市场路径失败保留启用意图 (#58 既有约定)


async def test_persist_failure_keeps_running_instance_alongside_same_display(api_client, auth_cookies, env):
    saved = env.modules_dir + '-saved'
    env.write_module('probe90_a', source=_source('A', priority=10))
    mm = await start(env)
    assert await mm.enable('probe90_a', _persist=False)
    env.write_module('probe90_b', source=_source('B', priority=20, mode='persist', saved=saved, modules_dir=env.modules_dir))
    try:
        status, data = await toggle(api_client, auth_cookies, 'probe90_b')
        assert data['success'] is False and '启用标记保存失败' in data['message']
        assert mm.is_enabled('probe90_b')
        assert await trace(env) == ['A', 'B']
    finally:
        if os.path.isfile(env.modules_dir):
            os.unlink(env.modules_dir)
            os.rename(saved, env.modules_dir)
    assert (await toggle(api_client, auth_cookies, 'probe90_b', 'disable'))[1]['success']
    assert await trace(env) == ['A']


async def test_shutdown_removes_every_instance_hook(api_client, auth_cookies, env):
    env.write_module('probe90_a', source=_source('A', priority=10))
    env.write_module('probe90_b', source=_source('B', priority=20))
    mm = await start(env)
    assert await mm.enable('probe90_a') and await mm.enable('probe90_b')
    shutil.rmtree(os.path.join(env.modules_dir, 'probe90_b'))  # 运行中目录被删也能正常停
    await mm.shutdown()
    assert await trace(env) == []


async def test_hook_manager_owner_api_compatible():
    hooks = HookManager()

    async def a(data):
        return data

    async def b(data):
        return data

    token = object()
    hooks.register('before_send', a, owner='direct', priority=5)
    hooks.register('before_send', b, owner='direct', priority=6, _token=token)
    entry = hooks._hooks['before_send'][0]
    assert (entry[0], entry[1], entry[2]) == (5, 'direct', a)  # 既有位置字段不变
    assert hooks.list_hooks() == {'before_send': [{'owner': 'direct', 'priority': 5}, {'owner': 'direct', 'priority': 6}]}

    hooks._unregister_token(token)
    assert [e[2] for e in hooks._hooks['before_send']] == [a]
    hooks.register('before_send', b, owner='direct', priority=6, _token=token)
    hooks.unregister_owner('direct')  # 按展示名注销的公开语义保持
    assert not hooks.has('before_send')
