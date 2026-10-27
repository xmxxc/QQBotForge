#!/usr/bin/env python
"""拓展模块管理器 — 自动发现、依赖安装、启停管理"""

import asyncio
import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from weakref import WeakSet

from core.base.context import BaseContext
from core.base.logger import EXTENSION, get_logger, report_error
from core.base.pip_helper import install_requirements as _install_deps
from core.base.python_source import read_dict_assignment
from core.module.hook import get_hook_manager

log = get_logger(EXTENSION, '管理器')


async def _await_if_coro(result):
    """await 协程结果, 否则直接返回"""
    return await result if asyncio.iscoroutine(result) else result


# __module_meta__ 字段默认值
_DEFAULT_MANIFEST = {
    'name': '',
    'description': '',
    'version': '1.0.0',
    'author': '',
    'github': '',
    'releases': '',
}


class ModuleContext(BaseContext):
    """模块上下文 — 继承 BaseContext, 额外提供 Hook 注册"""

    __slots__ = ('_hooks', '_group_subscriptions', '_subscriptions_closed', '_connection_subscriptions', '_recovery_subscriptions', '_recovery_scope')

    def __init__(self, name, module_dir, hook_manager):
        super().__init__(name, module_dir, EXTENSION)
        self._hooks = hook_manager
        # 绑定安装槽位而非展示名；入口 symlink 切换版本后仍属于同一模块。
        module_path = Path(module_dir)
        # 先按真实文件系统解析父路径，不能先折叠 symlink/..。
        self._recovery_scope = str(module_path.resolve() if module_path.name == '..' else module_path.parent.resolve() / module_path.name)
        self._group_subscriptions = WeakSet()
        self._connection_subscriptions = WeakSet()
        self._recovery_subscriptions = WeakSet()
        self._subscriptions_closed = False

    @property
    def module_dir(self):
        return self._root_dir

    @property
    def group_lifecycle_api_version(self):
        return self._hooks.group_lifecycle.api_version

    def subscribe_group_lifecycle(self, callback, *, appid=None, timeout=5.0):
        """独立于插件订阅群变化，模块卸载时自动注销。"""
        if self._subscriptions_closed:
            raise RuntimeError('module context is unloaded')
        subscription = self._hooks.group_lifecycle.subscribe(callback, owner=self.name, appid=appid, timeout=timeout)
        self._group_subscriptions.add(subscription)
        return subscription

    @property
    def connection_status_api_version(self):
        return self._hooks.connection_status.api_version

    @property
    def connection_statuses(self):
        return self._hooks.connection_status.statuses

    def get_connection_status(self, appid, *, transport='websocket'):
        return self._hooks.connection_status.get(appid, transport=transport)

    def get_connection_capabilities(self, appid, *, transport='websocket'):
        return self._hooks.connection_status.capabilities(appid, transport=transport)

    def subscribe_connection_status(self, callback, *, appid=None, transport=None, timeout=5.0, replay_current=True):
        """订阅连接事实，回调结果不构成本地恢复完成凭证。"""
        if self._subscriptions_closed:
            raise RuntimeError('module context is unloaded')
        subscription = self._hooks.connection_status.subscribe(
            callback,
            owner=self.name,
            appid=appid,
            transport=transport,
            timeout=timeout,
            replay_current=replay_current,
        )
        self._connection_subscriptions.add(subscription)
        return subscription

    @property
    def recovery_api_version(self):
        return self._hooks.recovery.api_version

    def subscribe_recovery_events(self, callback, *, event_types, appid=None, timeout=5.0, max_pending=128, depends_on=()):
        """显式参与状态恢复；回调需在更新完成后调用 delivery.acknowledge()。"""
        if self._subscriptions_closed:
            raise RuntimeError('module context is unloaded')
        subscription = self._hooks.recovery.subscribe(callback, owner=self.name, event_types=event_types, appid=appid,
                                                       timeout=timeout, max_pending=max_pending, depends_on=depends_on, _scope=self._recovery_scope)
        self._recovery_subscriptions.add(subscription)
        return subscription

    def _close_subscriptions(self):
        self._subscriptions_closed = True
        for subscription in self._recovery_subscriptions:
            subscription._close('unloaded')
        self._recovery_subscriptions.clear()
        for subscription in self._group_subscriptions:
            subscription._close('unloaded')
        self._group_subscriptions.clear()
        for subscription in self._connection_subscriptions:
            subscription._close('unloaded')
        self._connection_subscriptions.clear()

    # ---------- Hook ----------

    def hook(self, hook_name, *, priority=100):
        """装饰器注册 hook: @ctx.hook('before_send')"""

        def decorator(func):
            self.register_hook(hook_name, func, priority=priority)
            return func

        return decorator

    def register_hook(self, hook_name, callback, *, priority=100):
        """直接注册 hook 回调 (非装饰器方式)；归属本上下文实例，模块卸载或启用失败时只注销本实例的回调"""
        if self._subscriptions_closed:
            raise RuntimeError('module context is unloaded')
        self._hooks.register(hook_name, callback, owner=self.name, priority=priority, _token=self)

    async def emit(self, hook_name, *args, **kwargs):
        """触发一个 hook (广播模式)"""
        await self._hooks.emit(hook_name, *args, **kwargs)

    async def pipeline(self, hook_name, data):
        """触发一个 hook (管道模式, 可修改/拦截数据)"""
        return await self._hooks.pipeline(hook_name, data)


class ModuleInfo:
    """已发现模块的信息"""

    __slots__ = (
        'name',
        'display_name',
        'description',
        'module_dir',
        'module',
        'version',
        'author',
        'github',
        'releases',
        'instance',
        'ctx',
        'error',
    )

    def __init__(self, name, module_dir):
        self.name = name
        self.module_dir = module_dir
        self.display_name = name
        self.description = ''
        self.version = '1.0.0'
        self.author = ''
        self.github = ''
        self.releases = ''
        self.module = None
        self.instance = None
        self.ctx = None
        self.error = None


class ModuleManager:
    """拓展模块管理器"""

    def __init__(self, modules_dir=None, hook_manager=None):
        if modules_dir:
            self._dir = os.path.abspath(modules_dir)
        else:
            self._dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'modules')
        self._hook_manager = hook_manager or get_hook_manager()
        self._modules = {}  # {name: ModuleInfo}
        self._lock = asyncio.Lock()
        self._enabled_file = os.path.join(self._dir, 'modules_enabled.json')
        self._enabled_map = self._load_enabled_map()  # {name: bool}

    # ==================== 发现 ====================

    def discover(self):
        """扫描 modules/ 下所有模块目录"""
        if not os.path.isdir(self._dir):
            os.makedirs(self._dir, exist_ok=True)
            return
        for name in sorted(os.listdir(self._dir)):
            self.discover_module(name)
        log.info(f'发现 {len(self._modules)} 个模块: {", ".join(f"{n}@{m.version}" for n, m in self._modules.items())}')

    def discover_module(self, name):
        """发现或刷新单个未运行模块；运行中模块原样返回，目录失效时丢弃未运行的旧记录。

        运行期请通过 enable/refresh_and_enable 调用，它们在锁内刷新，避免替换正在启用的记录。
        """
        if not self._is_module_name(name):
            return None
        current = self._modules.get(name)
        if current and current.instance is not None:
            return current

        mod_dir = os.path.join(self._dir, name)
        # 按平台路径语义只接受单级组件（Windows 的 C:x、C: 带盘符），再要求名称是目录中的真实条目，
        # 挡住大小写、末尾点号等指向同一目录的别名；symlink 条目照常可用。
        if os.path.splitdrive(name)[0] or os.path.basename(name) != name or os.path.dirname(mod_dir) != self._dir:
            return None
        if not self._is_listed(name) or not os.path.isdir(mod_dir) or not self._find_entry(mod_dir):
            self._modules.pop(name, None)
            return None

        info = ModuleInfo(name, mod_dir)
        meta = self._read_manifest(mod_dir)
        info.display_name = meta.get('name') or name
        for key in ('description', 'version', 'author', 'github', 'releases'):
            val = meta.get(key)
            if val is not None:
                setattr(info, key, str(val))
        self._modules[name] = info
        return info

    # ==================== 持久化开关 ====================

    def _load_enabled_map(self):
        """读取 modules_enabled.json, 不存在则返回空 dict"""
        if not os.path.isfile(self._enabled_file):
            return {}
        try:
            with open(self._enabled_file, encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_enabled_map(self):
        """保存 modules_enabled.json"""
        os.makedirs(os.path.dirname(self._enabled_file), exist_ok=True)
        try:
            with open(self._enabled_file, 'w', encoding='utf-8') as f:
                json.dump(self._enabled_map, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning(f'保存模块开关状态失败: {e}')

    def is_module_enabled_persist(self, name):
        """查询模块是否在持久化配置中标记为启用 (默认 False)"""
        return self._enabled_map.get(name, False)

    def set_module_enabled_persist(self, name, enabled):
        """设置模块持久化开关状态"""
        self._enabled_map[name] = bool(enabled)
        self._save_enabled_map()

    # ==================== 自动启动 ====================

    async def start_enabled(self):
        """启动持久化配置中标记为启用的模块"""
        to_start = [n for n in self._modules if self.is_module_enabled_persist(n)]
        if not to_start:
            log.info('无已启用模块, 跳过启动')
            return
        tasks = [_install_deps(n, self._modules[n].module_dir, skip_if_met=True, no_cache=True) for n in to_start]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for name in to_start:
            try:
                await self.enable(name, _skip_deps=True)
            except Exception as e:
                report_error(EXTENSION, name, e)

    # ==================== 启用/禁用 ====================

    async def enable(self, name, _skip_deps=False, _persist=True):
        """启用模块；未运行模块先按当前磁盘内容刷新，已运行模块不重载。"""
        async with self._lock:
            return await self._enable_locked(name, _skip_deps, _persist)

    async def _enable_locked(self, name, _skip_deps, _persist):
        info = self.discover_module(name)
        if not info:
            log.warning(f'模块不存在: {name!r}')
            return False
        if info.instance is not None:
            if _persist and not self.is_module_enabled_persist(name):
                return self._persist_enabled(info)
            return True
        ctx = None
        try:
            if not _skip_deps:
                await _install_deps(name, info.module_dir, skip_if_met=True, no_cache=True)
            ctx = ModuleContext(info.display_name or name, info.module_dir, self._hook_manager)
            info.ctx = ctx
            module = self._import_module(name, info.module_dir)
            info.module = module
            setup_fn = getattr(module, 'setup', None)
            try:
                result = await _await_if_coro(setup_fn(ctx)) if setup_fn else None
            except BaseException:
                ctx._close_subscriptions()
                raise
            info.instance = result if result is not None else True
            info.error = None
            log.info(f'{self._label(info)} 已启用')
        except BaseException as e:
            # 失败或被取消的 setup 只注销本次上下文注册的 hook、移出半初始化模块，避免重试叠加回调；
            # 同显示名的其他运行中模块不受影响。
            if ctx is not None:
                ctx._close_subscriptions()
                self._hook_manager._unregister_token(ctx)
            info.ctx = info.module = None
            sys.modules.pop(f'modules.{name}', None)
            if not isinstance(e, Exception):
                log.warning(f'{self._label(info)} 启用中断: {type(e).__name__}')
                raise
            info.error = str(e)
            log.warning(f'{self._label(info)} 启用失败: {e}')
            report_error(EXTENSION, name, e, context={'module': name, 'version': info.version})
            return False
        return self._persist_enabled(info) if _persist else True

    def _persist_enabled(self, info):
        """setup 已成功后写启用标记；保存失败时模块保持完整运行（可禁用），内存标记回滚，重试启用会再次保存。"""
        name = info.name
        previous = self._enabled_map.get(name)
        try:
            self.set_module_enabled_persist(name, True)
        except Exception as e:
            if previous is None:
                self._enabled_map.pop(name, None)
            else:
                self._enabled_map[name] = previous
            info.error = f'模块已运行，但启用标记保存失败: {e}'
            report_error(EXTENSION, name, e)
            return False
        info.error = None
        return True

    async def disable(self, name, _persist=True):
        """禁用模块"""
        async with self._lock:
            return await self._disable_locked(name, _persist)

    async def _disable_locked(self, name, _persist):
        info = self._modules.get(name)
        is_running = info and info.instance is not None
        if not is_running:
            if _persist:
                self.set_module_enabled_persist(name, False)
            return False
        info.ctx._close_subscriptions()
        try:
            teardown_fn = getattr(info.module, 'teardown', None)
            if teardown_fn:
                await _await_if_coro(teardown_fn())
        except Exception as e:
            report_error(EXTENSION, name, e)
        self._hook_manager._unregister_token(info.ctx)
        info.instance = info.ctx = None
        sys.modules.pop(f'modules.{name}', None)
        if _persist:
            self.set_module_enabled_persist(name, False)
            log.info(f'模块 [{info.display_name or name}] 已禁用')
        return True

    @staticmethod
    def _label(info):
        display = info.display_name or info.name
        name = display if display == info.name else f'{display}({info.name})'
        return f'模块 [{name}] v{info.version}'

    async def reload(self, name):
        """重载模块 (teardown → 重新 import + setup), 不改变持久化状态"""
        info = self._modules.get(name)
        if not info:
            return False
        was_enabled = info.instance is not None
        if was_enabled:
            await self.disable(name, _persist=False)
        return await self.enable(name, _persist=False)

    async def refresh_and_enable(self, name):
        """刷新已安装模块并启用，统一市场安装和更新的生命周期。"""
        async with self._lock:
            if self.is_enabled(name):
                await self._disable_locked(name, _persist=False)
            if self.discover_module(name) is None:
                return False

            # 启动失败时仍保留启用意图，环境修复后可在下次启动重试。
            self.set_module_enabled_persist(name, True)
            return await self._enable_locked(name, False, _persist=False)

    # ==================== 查询 ====================

    def get(self, name):
        """获取已启用模块实例 (setup 返回值)"""
        info = self._modules.get(name)
        return info.instance if info and info.instance is not None else None

    def get_context(self, name):
        """获取模块上下文"""
        info = self._modules.get(name)
        return info.ctx if info else None

    def get_module(self, name):
        """获取模块 Python 对象"""
        info = self._modules.get(name)
        return info.module if info else None

    def is_enabled(self, name):
        info = self._modules.get(name)
        return info.instance is not None if info else False

    def list_modules(self):
        """获取所有模块状态"""
        return [
            {
                'name': i.name,
                'display_name': i.display_name,
                'description': i.description,
                'version': i.version,
                'author': i.author,
                'github': i.github,
                'releases': i.releases,
                'enabled': i.instance is not None,
                'persist_enabled': self.is_module_enabled_persist(i.name),
                'error': i.error,
            }
            for i in self._modules.values()
        ]

    # ==================== 内部 ====================

    @staticmethod
    def _is_module_name(name):
        """仅接受 modules/ 下的单级目录名，拒绝 ./.. 与路径分隔符。"""
        return isinstance(name, str) and bool(name) and not name.startswith('_') and name not in ('.', '..') and not any(c in name for c in ('/', '\\', '\0'))

    def _is_listed(self, name):
        try:
            return name in os.listdir(self._dir)
        except OSError:
            return False

    @staticmethod
    def _find_entry(mod_dir):
        """main.py 存在则返回路径"""
        path = os.path.join(mod_dir, 'main.py')
        return path if os.path.isfile(path) else None

    @staticmethod
    def _import_module(name, mod_dir):
        """动态导入模块"""
        entry = os.path.join(mod_dir, 'main.py')
        if not os.path.isfile(entry):
            raise FileNotFoundError(f'模块入口不存在: {mod_dir} (需要 main.py)')

        mod_name = f'modules.{name}'
        spec = importlib.util.spec_from_file_location(mod_name, entry, submodule_search_locations=[mod_dir])
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _read_manifest(mod_dir):
        """从 main.py 的 __module_meta__ 读取模块元数据"""
        entry = os.path.join(mod_dir, 'main.py')
        return read_dict_assignment(entry, '__module_meta__') or dict(_DEFAULT_MANIFEST)

    async def shutdown(self):
        """关闭所有已启用模块 (不改变持久化状态, 重启后按用户设置恢复)"""
        running = [n for n, i in self._modules.items() if i.instance is not None]
        display_names = [self._modules[n].display_name or n for n in running]
        for name in running:
            await self.disable(name, _persist=False)
        if display_names:
            log.info(f'已停止 {len(display_names)} 个模块: {", ".join(display_names)}')
