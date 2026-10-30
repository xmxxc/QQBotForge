"""Manual, CI-gated Git updates for this repository's personal deployments."""

import asyncio
import contextlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import aiohttp

from core.application import get_app
from web.tools._updater.shared import GITHUB_API_URL, GITHUB_BRANCH, GITHUB_REPO, GITHUB_URL, log
from web.tools._updater.supervisor import TERMINAL, TRANSACTION_ENV, lock_owner, read_state, release_lock, state_mutex, transition

_SHA_RE = re.compile(r'[0-9a-f]{8,40}')
RECOVERY_REMINDER = '重启后请确认群成员并使用「群状态 恢复」重建群状态。'
_WS_OK_PHASES = {'ready', 'recovering', 'replay_complete'}
# Effective configuration keys that could change where or how personal is fetched.
_SOURCE_OVERRIDE = re.compile(r'^(url\..*\.(insteadof|pushinsteadof)|http\.(.*\.)?(proxy|followredirects|sslverify|sslcainfo|sslcapath)|core\.gitproxy)$')


def write_state(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


class FrameworkUpdater:
    """Shared by the authenticated dashboard and owner commands.

    check_for_updates is read-only apart from fetch. update_to_latest and
    update_to_version claim a deployment-wide lock, always back up, and stop
    the app for a supervised restart. Legacy skip/restart flags cannot weaken
    those guarantees. Terminal state and backup paths survive process restarts.
    """

    def __init__(self, base_dir: str, *, ws_timeout: float = 90.0):
        self.base_dir = Path(base_dir).resolve()
        self.version_file = self.base_dir / 'data' / 'version.json'
        self.pending_file = self.base_dir / 'data' / 'update_pending.json'
        self.lock_file = self.base_dir / 'data' / 'update.lock'
        self.ws_timeout = ws_timeout
        self.progress = dict(stage='idle', message='', progress=0, is_updating=False, config_diff=None)
        self._recovery_task = None

    def _report(self, stage, message, progress=0, config_diff=None):
        self.progress = dict(stage=stage, message=message, progress=progress, is_updating=stage not in ('idle', 'completed', 'failed'), config_diff=config_diff)
        log.info('[更新] %s: %s', stage, message)

    def _state(self):
        return read_state(self.base_dir)

    def get_progress(self):
        if self.progress['is_updating']:
            return self.progress.copy()
        state = self._state()
        if state.get('status') in ('awaiting_restart', 'awaiting_ws', 'switching', 'rolling_back'):
            return dict(
                self.progress, stage='restarting', message=state.get('message', ''), progress=90, is_updating=True, backup_path=state.get('backup_path')
            )
        if state.get('status') in ('confirmed', 'rolled_back', 'rollback_failed', 'failed'):
            return dict(
                self.progress,
                stage='completed' if state['status'] == 'confirmed' else 'failed',
                message=state.get('message', '') + ' ' + RECOVERY_REMINDER,
                is_updating=False,
                backup_path=state.get('backup_path'),
                last_update=state,
            )
        return self.progress.copy()

    def get_version_info(self):
        return dict(
            version=self._current_sha() or 'unknown',
            update_time=self._state().get('created_at', 'unknown'),
            source={'repository': GITHUB_REPO, 'branch': GITHUB_BRANCH},
            environment=self.environment(),
            last_update=self._state(),
            recovery_reminder=RECOVERY_REMINDER,
        )

    @property
    def custom_mirror(self):
        """Legacy plugin-market preference; never used for framework updates."""
        from web.tools._updater.shared import load_github_mirror

        return load_github_mirror(self.base_dir)

    def set_custom_mirror(self, mirror):
        """Keep older plugin-market callers working; the updater ignores this."""
        path = self.base_dir / 'data/update_settings.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            settings = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            settings = {}
        settings['custom_mirror'] = mirror or ''
        write_state(path, settings)

    def _run_git(self, *args):
        # Deployment-specific Git environment must not redirect the fixed source.
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0')
        try:
            return subprocess.run(
                ['git', '-c', 'http.followRedirects=false', '-c', 'http.proxy=', '-C', str(self.base_dir), *args],
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )
        except FileNotFoundError:
            return subprocess.CompletedProcess(args, 127, '', '未安装 git')

    def _git(self, *args):
        result = self._run_git(*args)
        if result.returncode:
            raise RuntimeError('Git 操作失败: ' + (result.stderr.strip() or args[0]))
        return result.stdout.strip()

    def _current_sha(self):
        result = self._run_git('rev-parse', '--verify', 'HEAD')
        return result.stdout.strip() if not result.returncode else ''

    def environment(self):
        root = self._run_git('rev-parse', '--show-toplevel')
        return 'git' if not root.returncode and Path(root.stdout.strip()).resolve() == self.base_dir else 'non_git'

    def _check_source_config(self):
        """Reject effective settings that could redirect the fixed source.

        Git cannot skip repository configuration for a command run inside the
        repository, so the effective configuration Git itself will use is
        expanded, including include.path and every includeIf form. System and
        global files are already excluded by _run_git's environment.
        """
        listed = self._run_git('config', '--includes', '--list', '--show-scope', '-z')
        if listed.returncode not in (0, 1):
            raise RuntimeError('无法读取 Git 有效配置，已拒绝更新')
        # Fields are "scope\0key\nvalue\0"; the updater's own -c overrides have scope "command".
        fields = listed.stdout.split('\0')
        keys = [entry.split('\n', 1)[0].lower() for scope, entry in zip(fields[::2], fields[1::2], strict=False) if scope != 'command']
        found = sorted({k for k in keys if _SOURCE_OVERRIDE.match(k)})
        if found:
            raise RuntimeError('Git 配置（含 include/includeIf）中的 URL 重写或代理/重定向/证书设置可能改变更新来源，已拒绝更新: ' + ', '.join(found))

    def _fetch(self):
        self._check_source_config()
        self._git(
            'fetch', '--no-tags', '--no-recurse-submodules', GITHUB_URL + '.git', f'+refs/heads/{GITHUB_BRANCH}:refs/remotes/qqbotforge-updates/{GITHUB_BRANCH}'
        )
        return self._git('rev-parse', f'refs/remotes/qqbotforge-updates/{GITHUB_BRANCH}^{{commit}}')

    def _ancestor(self, old, target):
        return self._run_git('merge-base', '--is-ancestor', old, target).returncode == 0

    def _conflicting_paths(self, old, target):
        changed = self._git('diff', '--name-only', '-z', old, target).split('\0')
        local = self._git('diff', '--name-only', '-z', 'HEAD').split('\0')
        local += self._git('ls-files', '--others', '--exclude-standard', '-z').split('\0')
        local += self._git('ls-files', '--others', '--ignored', '--exclude-standard', '-z', '--', *filter(None, changed)).split('\0')
        return sorted({p for p in changed if p and any(q and (p == q or p.startswith(q + '/') or q.startswith(p + '/')) for q in local)})

    async def _fetch_api(self, path=''):
        headers = {'User-Agent': 'QQBotForge-updater', 'Accept': 'application/vnd.github+json'}
        async with (
            aiohttp.ClientSession(trust_env=False) as session,
            session.get(GITHUB_API_URL + path, headers=headers, timeout=aiohttp.ClientTimeout(total=20), allow_redirects=False) as response,
        ):
            if response.status != 200:
                raise RuntimeError(f'GitHub API HTTP {response.status}')
            return await response.json()

    async def _ci_status(self, sha):
        """Require this SHA's personal push CI, never a PR merge or unrelated check."""
        try:
            runs = await self._fetch_api(f'/actions/workflows/ci.yml/runs?head_sha={sha}&branch=personal&event=push&per_page=100')
            candidates = [
                r
                for r in runs['workflow_runs']
                if r.get('head_sha') == sha
                and r.get('head_branch') == GITHUB_BRANCH
                and r.get('event') == 'push'
                and r.get('path') == '.github/workflows/ci.yml'
            ]
            run = max(candidates, key=lambda r: r['id']) if candidates else None
            checks = []
            for page in range(1, 11):
                data = await self._fetch_api(f'/commits/{sha}/check-runs?filter=latest&per_page=100&page={page}')
                checks.extend(data['check_runs'])
                if len(checks) >= data['total_count']:
                    break
            else:
                raise ValueError('检查数量超出上限')
            expected = [
                c
                for c in checks
                if c.get('name') == 'check'
                and c.get('head_sha') == sha
                and (c.get('app') or {}).get('slug') == 'github-actions'
                and run
                and c.get('details_url', '').startswith(f'{GITHUB_URL}/actions/runs/{run["id"]}/')
            ]
            if not run or not expected or run.get('status') != 'completed' or any(c.get('status') != 'completed' for c in expected):
                status = 'pending'
            elif run.get('conclusion') == 'success' and all(c.get('conclusion') == 'success' for c in expected):
                status = 'success'
            else:
                status = 'failure'
            return {'status': status, 'checks': expected, 'run_id': run['id'] if run else None}
        except (AttributeError, KeyError, TypeError, ValueError, RuntimeError, aiohttp.ClientError, TimeoutError):
            return {'status': 'unknown', 'checks': []}

    async def fetch_changelog(self):
        commits = await self._fetch_api(f'/commits?sha={GITHUB_BRANCH}&per_page=20')
        if not isinstance(commits, list) or any(not isinstance(c, dict) for c in commits):
            raise RuntimeError('GitHub 提交响应无效')
        return commits

    async def check_for_updates(self):
        result = dict(
            has_update=False, can_update=False, source={'repository': GITHUB_REPO, 'branch': GITHUB_BRANCH}, environment=self.environment(), error=None
        )
        if result['environment'] != 'git':
            return dict(result, error='当前部署不是 Git 工作区，请拉取新镜像或重新 Git 部署')
        try:
            target = await asyncio.to_thread(self._fetch)
            current = self._current_sha()
            ci = await self._ci_status(target)
            descendant = current != target and self._ancestor(current, target)
            gap = int(self._git('rev-list', '--count', f'{current}..{target}')) if self._ancestor(current, target) else None
            return dict(
                result,
                has_update=descendant,
                can_update=descendant and ci['status'] == 'success',
                latest_version=target,
                current_version=current,
                gap=gap,
                ci=ci,
                ci_status=ci['status'],
                relation='behind' if descendant else 'equal' if current == target else 'diverged',
                changelog=await self.fetch_changelog(),
            )
        except (RuntimeError, OSError, ValueError, aiohttp.ClientError, TimeoutError) as exc:
            return dict(result, error=str(exc))

    def backup_databases(self):
        """SQLite online backup includes committed WAL contents; no DB is restored automatically."""
        parent = (self.base_dir / 'data' / 'backup').resolve()
        if not parent.is_relative_to(self.base_dir) or parent == self.base_dir:
            raise RuntimeError('备份必须位于部署目录内部')
        parent.mkdir(parents=True, exist_ok=True)
        backup_id = uuid4().hex
        backup = parent / ('update-' + backup_id)
        backup.mkdir(mode=0o700)
        marker = backup / '.owner'
        marker.write_text(backup_id, encoding='utf-8')
        sources = []
        try:
            for root, dirs, files in os.walk(self.base_dir, followlinks=False):
                dirs[:] = [
                    d
                    for d in dirs
                    if d not in ('.git', '.venv', 'venv', '.bootstrap', '__pycache__') and not Path(root, d).is_symlink() and Path(root, d) != parent
                ]
                for name in files:
                    source = Path(root, name)
                    if source.suffix not in ('.db', '.sqlite', '.sqlite3'):
                        continue
                    if source.is_symlink():
                        raise RuntimeError('数据库路径是符号链接，无法安全备份: ' + str(source.relative_to(self.base_dir)))
                    relative = source.relative_to(self.base_dir)
                    destination = backup / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    deadline = time.monotonic() + 20

                    def bounded_backup(*_, deadline=deadline):
                        if time.monotonic() > deadline:
                            raise RuntimeError('SQLite 备份等待超时')

                    with (
                        contextlib.closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src,
                        contextlib.closing(sqlite3.connect(destination)) as dst,
                    ):
                        src.backup(dst, pages=256, progress=bounded_backup)
                    sources.append(str(relative))
            write_state(backup / 'manifest.json', {'databases': sources})
            return str(backup.relative_to(self.base_dir))
        except Exception:
            with contextlib.suppress(OSError):
                resolved = backup.resolve()
                if resolved.is_relative_to(self.base_dir) and resolved != self.base_dir and marker.read_text(encoding='utf-8') == backup_id:
                    shutil.rmtree(resolved)
            raise

    def _stale_lock(self):
        """A lock whose own transaction already finished, e.g. its releaser was killed."""
        owner, state = lock_owner(self.base_dir), self._state()
        return bool(owner) and state.get('transaction') == owner and state.get('status') in TERMINAL

    def lock_active(self):
        return self.lock_file.exists() and not self._stale_lock()

    def _claim(self, transaction):
        data_root = self.lock_file.parent.resolve()
        if not data_root.is_relative_to(self.base_dir) or data_root == self.base_dir:
            raise OSError('更新数据必须位于部署目录内部')
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        with state_mutex(self.base_dir):
            if self._stale_lock():
                self.lock_file.unlink()
            with self.lock_file.open('x', encoding='utf-8') as lock:
                lock.write(transaction)

    async def update_to_version(self, version, skip_backup=False, auto_restart=True):
        del skip_backup, auto_restart
        if self.environment() != 'git':
            return {'success': False, 'message': '当前部署不是 Git 工作区，请拉取新镜像或重新 Git 部署'}
        if not isinstance(version, str) or not _SHA_RE.fullmatch(version):
            return {'success': False, 'message': '目标版本必须是 8 至 40 位 Git SHA'}
        transaction = uuid4().hex
        try:
            self._claim(transaction)
        except FileExistsError:
            return {'success': False, 'message': '已有更新或启动验证正在进行，请先查看更新结果'}
        except (OSError, TimeoutError):
            return {'success': False, 'message': '无法创建更新锁，请检查运行目录写入权限'}
        manager = None
        was_running = False
        switched = False
        state = {}
        old = self._current_sha()
        try:
            self._report('preparing', '正在获取 personal 并验证提交和 CI', 5)
            tip = await asyncio.to_thread(self._fetch)
            target = self._git('rev-parse', '--verify', f'{version}^{{commit}}')
            if old == target or not self._ancestor(old, target) or not self._ancestor(target, tip):
                raise RuntimeError('目标必须属于 personal 且是当前 HEAD 的严格后续提交')
            ci = await self._ci_status(target)
            if ci['status'] != 'success':
                raise RuntimeError(f'目标提交 CI 未成功（{ci["status"]}），拒绝更新')
            conflicts = self._conflicting_paths(old, target)
            if conflicts:
                raise RuntimeError('本地改动与目标提交冲突: ' + ', '.join(conflicts))
            app = get_app()
            if app is None or app._stop_event is None or app._stop_event.is_set():
                raise RuntimeError('应用未运行，无法进行受监护的重启')
            self._report('backing_up', '正在备份扩展 SQLite 数据库', 35)
            backup = await asyncio.to_thread(self.backup_databases)
            # The supervisor source is copied BEFORE checkout and remains runnable
            # even if the target cannot import its own application or updater.
            guard = self.base_dir / backup / 'supervisor.py'
            shutil.copyfile(Path(__file__).with_name('supervisor.py'), guard)
            state = dict(
                status='switching',
                old_sha=old,
                target_sha=target,
                backup_path=backup,
                transaction=transaction,
                created_at=datetime.now(UTC).isoformat(),
                message='已备份数据库，正在切换版本',
                recovery_reminder=RECOVERY_REMINDER,
            )
            with state_mutex(self.base_dir):
                write_state(self.pending_file, state)
            manager = app.plugin_manager
            was_running = bool(manager and manager._watcher_running)
            if manager:
                manager.stop_watcher()
            self._report('updating', '正在切换 Git 提交', 70)
            self._git('checkout', '--no-overwrite-ignore', '--detach', target)
            switched = True
            state = transition(self.base_dir, transaction, ('switching',), status='awaiting_restart', message='等待重启和 WS 连接确认')
            if not state:
                raise RuntimeError('更新事务已被其他操作取代')
            self._trigger_restart(app, guard, state)
            self._report('completed', '已切换并安排重启；' + RECOVERY_REMINDER, 100)
            return dict(
                success=True,
                message=self.progress['message'],
                current_version=target,
                target_version=target,
                backup_path=backup,
                restart_required=True,
                recovery_reminder=RECOVERY_REMINDER,
                ci=ci,
            )
        except Exception as exc:
            message = str(exc)
            backup_path = (state or {}).get('backup_path')
            active = ('switching', 'awaiting_restart')
            if switched:
                try:
                    self._git('checkout', '--no-overwrite-ignore', '--detach', old)
                except RuntimeError:
                    failure = '启动监护器失败且无法回退；请手动检查 Git 和数据库备份'
                    transition(self.base_dir, transaction, active, status='rollback_failed', message=failure)
                    self._report('failed', failure)
                    return dict(success=False, message=failure, backup_path=backup_path)
            if manager and was_running:
                manager.start_watcher()
            if state:
                transition(self.base_dir, transaction, active, status='failed', message=message)
            release_lock(self.base_dir, transaction)
            self._report('failed', message)
            return dict(success=False, message=message, backup_path=backup_path)

    async def update_to_latest(self, skip_backup=False, auto_restart=True):
        if self.lock_active():
            return {'success': False, 'message': '已有更新或启动验证正在进行'}
        result = await self.check_for_updates()
        if not result.get('can_update'):
            message = result.get('error') or f'无法更新：版本关系 {result.get("relation")}，CI {result.get("ci_status", "unknown")}'
            self._report('failed', message)
            return {'success': False, 'message': message}
        return await self.update_to_version(result['latest_version'], skip_backup=skip_backup, auto_restart=auto_restart)

    async def force_update(self, skip_backup=False, auto_restart=True):
        return await self.update_to_latest(skip_backup=skip_backup, auto_restart=auto_restart)

    def update_from_upload(self, *args, **kwargs):
        return {'success': False, 'message': '上传覆盖更新已禁用，请使用 personal Git 提交'}

    def _trigger_restart(self, app, guard, state):
        import psutil

        # The guard waits until this process exits, then owns the new process.
        # Do not use relaunch(): it would kill the guard as a descendant.
        process = subprocess.Popen(
            [
                sys.executable,
                str(guard),
                str(self.base_dir),
                str(os.getpid()),
                str(psutil.Process().create_time()),
                state['transaction'],
                str(self.ws_timeout),
                sys.executable,
                *sys.argv,
            ],
            cwd=self.base_dir,
            start_new_session=True,
        )
        ready = guard.with_suffix('.ready')
        deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < deadline:
            if ready.is_file() and ready.read_text(encoding='utf-8') == state['transaction']:
                break
            time.sleep(0.05)
        else:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3)
            raise RuntimeError('启动监护器未就绪，已拒绝重启')
        app._restart_requested = False
        app._stop_event.set()

    def schedule_startup_recovery(self, app):
        if not self._recovery_task or self._recovery_task.done():
            self._recovery_task = asyncio.create_task(self.verify_startup(app))

    def _own_transaction(self, state):
        """The transaction this process was started to verify.

        The monitor passes it in the environment. A process started by hand
        after its monitor died may adopt the waiting transaction instead,
        since nobody else can act on it any more.
        """
        transaction = os.environ.get(TRANSACTION_ENV)
        if transaction:
            return transaction
        import psutil

        try:
            monitor = psutil.Process(int(state['supervisor_pid']))
            if abs(monitor.create_time() - float(state['supervisor_created'])) < 0.01 and monitor.is_running():
                return None
        except (KeyError, TypeError, ValueError, psutil.Error):
            pass
        return state.get('transaction')

    async def verify_startup(self, app):
        """Only real ready/recovering WS facts may acknowledge this process's own transaction."""
        state = self._state()
        transaction = self._own_transaction(state) if state.get('status') == 'awaiting_ws' else None
        if not transaction or state.get('transaction') != transaction or self._current_sha() != state.get('target_sha'):
            return False
        while (current := self._state()).get('transaction') == transaction and current.get('status') == 'awaiting_ws':
            statuses = app.hook_manager.connection_status.statuses
            if any(s.transport == 'websocket' and s.phase in _WS_OK_PHASES for s in statuses):
                confirmed = transition(
                    self.base_dir, transaction, ('awaiting_ws',), status='confirmed', message='新版本 WS 已连接，更新已确认；' + RECOVERY_REMINDER
                )
                if not confirmed:
                    return False
                release_lock(self.base_dir, transaction)
                return True
            await asyncio.sleep(0.2)
        return False
