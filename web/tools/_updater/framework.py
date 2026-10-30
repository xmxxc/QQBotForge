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
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import aiohttp

from core.application import get_app
from web.tools._updater.shared import GITHUB_API_URL, GITHUB_BRANCH, GITHUB_REPO, GITHUB_URL, log
from web.tools._updater.supervisor import (
    ACTIVE,
    TRANSACTION_ENV,
    acquire_deploy_lock,
    clean_git_env,
    deploy_git,
    deploy_lock_held,
    finish_rollback,
    read_state,
    state_mutex,
    take_over,
    transition,
)

_SHA_RE = re.compile(r'[0-9a-f]{8,40}')
RECOVERY_REMINDER = '重启后请确认群成员并使用「群状态 恢复」重建群状态。'
_WS_OK_PHASES = {'ready', 'recovering', 'replay_complete'}
_FETCH_REF = f'refs/remotes/qqbotforge-updates/{GITHUB_BRANCH}'


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

    def __init__(self, base_dir: str, *, ws_timeout: float = 90.0, claim_timeout: float = 5.0):
        self.base_dir = Path(base_dir).resolve()
        self.version_file = self.base_dir / 'data' / 'version.json'
        self.pending_file = self.base_dir / 'data' / 'update_pending.json'
        self.lock_file = self.base_dir / 'data' / 'update.lock'
        self.ws_timeout = ws_timeout
        self.claim_timeout = claim_timeout
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
        if state.get('status') in ACTIVE:
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
        # Local commands only; the deployment repository never performs transport.
        return deploy_git(self.base_dir, *args)

    def _git(self, *args):
        result = self._run_git(*args)
        if result.returncode:
            raise RuntimeError('Git 操作失败: ' + (result.stderr.strip() or args[0]))
        return result.stdout.strip()

    def _current_sha(self):
        result = self._run_git('rev-parse', '--verify', 'HEAD')
        return result.stdout.strip() if not result.returncode else ''

    def environment(self):
        if os.name != 'posix':
            return 'unsupported'  # the update locks rely on POSIX flock
        root = self._run_git('rev-parse', '--show-toplevel')
        return 'git' if not root.returncode and Path(root.stdout.strip()).resolve() == self.base_dir else 'non_git'

    def _fetch(self):
        """Fetch personal in a throwaway repository, then import only its objects.

        The deployment repository's configuration (remotes, URL rewrites,
        proxies, extra headers, credential helpers, includes) never takes part
        in transport: the fetch runs in a new bare repository with no system,
        global or inherited Git settings and no proxy variables. Objects come
        back through a bundle, which the deployment repository only unpacks.
        """
        objects = self._git('rev-parse', '--path-format=absolute', '--git-path', 'objects')
        head = self._current_sha()
        with tempfile.TemporaryDirectory(prefix='qqbotforge-fetch-') as scratch:
            bare = Path(scratch, 'source.git')
            env = clean_git_env(HOME=scratch)

            def run(*args):
                try:
                    result = subprocess.run(['git', *args], capture_output=True, text=True, timeout=300, env=env)
                except FileNotFoundError:
                    raise RuntimeError('未安装 git') from None
                if result.returncode:
                    raise RuntimeError('Git 获取失败: ' + (result.stderr.strip() or args[0]))
                return result.stdout.strip()

            run('init', '-q', '--bare', str(bare))
            # Reuse local objects so only new history is downloaded.
            (bare / 'objects/info/alternates').write_text(objects + '\n', encoding='utf-8')
            git = ('-C', str(bare), '-c', 'http.followRedirects=false', '-c', 'credential.helper=', '-c', 'core.alternateRefsCommand=exit 0')
            if head:
                run(*git, 'update-ref', 'refs/heads/base', head)
            run(*git, 'fetch', '--no-tags', '--no-recurse-submodules', GITHUB_URL + '.git', f'+refs/heads/{GITHUB_BRANCH}:refs/heads/{GITHUB_BRANCH}')
            tip = run(*git, 'rev-parse', '--verify', f'refs/heads/{GITHUB_BRANCH}^{{commit}}')
            tree = run(*git, 'rev-parse', '--verify', f'{tip}^{{tree}}')
            if self._run_git('cat-file', '-e', f'{tip}^{{commit}}').returncode:
                bundle = Path(scratch, 'personal.bundle')
                run(*git, 'bundle', 'create', str(bundle), f'refs/heads/{GITHUB_BRANCH}', *(['^refs/heads/base'] if head else []))
                self._git('bundle', 'unbundle', str(bundle))
        if self._git('rev-parse', '--verify', f'{tip}^{{tree}}') != tree:
            raise RuntimeError('导入的提交与获取结果不一致')
        self._git('update-ref', _FETCH_REF, tip)
        return tip

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
            tree = None
            if status == 'success':
                commit = await self._fetch_api(f'/commits/{sha}')
                if commit.get('sha') != sha:
                    raise ValueError('GitHub 提交响应不匹配')
                tree = commit['commit']['tree']['sha']
            return {'status': status, 'checks': expected, 'run_id': run['id'] if run else None, 'tree': tree}
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
        if result['environment'] == 'unsupported':
            return dict(result, error='框架更新器仅支持 Linux/macOS 的 Git 部署')
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

    def lock_active(self):
        """A live process is driving an update or verifying one."""
        return deploy_lock_held(self.base_dir)

    def _claim(self):
        """Take the deployment lock, finishing any transaction a killed process left behind."""
        data_root = self.lock_file.parent.resolve()
        if not data_root.is_relative_to(self.base_dir) or data_root == self.base_dir:
            raise OSError('更新数据必须位于部署目录内部')
        fd = acquire_deploy_lock(self.base_dir, timeout=self.claim_timeout)
        if fd is None:
            raise FileExistsError(str(self.lock_file))
        try:
            take_over(self.base_dir)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _local_changes(self):
        return {p for p in self._git('diff', '--name-only', '-z', 'HEAD').split('\0') if p}

    async def update_to_version(self, version, skip_backup=False, auto_restart=True):
        del skip_backup, auto_restart
        environment = self.environment()
        if environment == 'unsupported':
            return {'success': False, 'message': '框架更新器仅支持 Linux/macOS 的 Git 部署'}
        if environment != 'git':
            return {'success': False, 'message': '当前部署不是 Git 工作区，请拉取新镜像或重新 Git 部署'}
        if not isinstance(version, str) or not _SHA_RE.fullmatch(version):
            return {'success': False, 'message': '目标版本必须是 8 至 40 位 Git SHA'}
        transaction = uuid4().hex
        try:
            lock_fd = self._claim()
        except FileExistsError:
            return {'success': False, 'message': '已有更新或启动验证正在进行，请先查看更新结果'}
        except (OSError, TimeoutError):
            return {'success': False, 'message': '无法获取更新锁，请检查运行目录写入权限'}
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
            target_tree = self._git('rev-parse', '--verify', f'{target}^{{tree}}')
            if ci.get('tree') != target_tree:
                raise RuntimeError('本地导入的目标提交与 GitHub 上 CI 通过的提交内容不一致')
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
                phase='checking_out',
            )
            with state_mutex(self.base_dir):
                write_state(self.pending_file, state)
            manager = app.plugin_manager
            was_running = bool(manager and manager._watcher_running)
            if manager:
                manager.stop_watcher()
            self._report('updating', '正在切换 Git 提交', 70)
            local = self._local_changes()
            switched = True
            self._git('checkout', '--no-overwrite-ignore', '--detach', target)
            # Hooks and fsmonitor are disabled; still prove the tracked files are the target's.
            drift = {p for p in self._git('diff', '--name-only', '-z', target).split('\0') if p} - local
            if self._current_sha() != target or drift:
                raise RuntimeError('切换后工作区与目标提交不一致: ' + ', '.join(sorted(drift)[:10]))
            state = transition(self.base_dir, transaction, ('switching',), status='awaiting_restart', message='等待重启和 WS 连接确认', phase='restarting')
            if not state:
                raise RuntimeError('更新事务已被其他操作取代')
            self._trigger_restart(app, guard, state, lock_fd)
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
            if switched:
                transition(self.base_dir, transaction, ('switching', 'awaiting_restart'), status='rolling_back', message=message, phase='checking_out')
                if not finish_rollback(self.base_dir, transaction, message + '；已回到原提交'):
                    failure = '更新失败且无法回退；请手动检查 Git 和数据库备份'
                    self._report('failed', failure)
                    return dict(success=False, message=failure, backup_path=backup_path)
            elif state:
                transition(self.base_dir, transaction, ('switching',), status='failed', message=message)
            if manager and was_running:
                manager.start_watcher()
            self._report('failed', message)
            return dict(success=False, message=message, backup_path=backup_path)
        finally:
            # The monitor holds its own inherited reference once started.
            os.close(lock_fd)

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

    def _trigger_restart(self, app, guard, state, lock_fd):
        import psutil

        # The guard waits until this process exits, then owns the new process.
        # Do not use relaunch(): it would kill the guard as a descendant.
        # It inherits the locked deployment-lock file, so the lock never lapses.
        process = subprocess.Popen(
            [
                sys.executable,
                str(guard),
                str(self.base_dir),
                str(os.getpid()),
                str(psutil.Process().create_time()),
                state['transaction'],
                str(self.ws_timeout),
                str(lock_fd),
                sys.executable,
                *sys.argv,
            ],
            cwd=self.base_dir,
            start_new_session=True,
            pass_fds=(lock_fd,),
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

    async def _await_ws(self, app, transaction, deadline=None):
        """Confirm the transaction once a real WebSocket is ready; False if it stops being ours."""
        while (state := self._state()).get('transaction') == transaction and state.get('status') == 'awaiting_ws':
            statuses = app.hook_manager.connection_status.statuses
            if any(s.transport == 'websocket' and s.phase in _WS_OK_PHASES for s in statuses):
                message = '新版本 WS 已连接，更新已确认；' + RECOVERY_REMINDER
                return bool(transition(self.base_dir, transaction, ('awaiting_ws',), status='confirmed', message=message, phase='done'))
            if deadline is not None and time.monotonic() > deadline:
                return False
            if deadline is None and not deploy_lock_held(self.base_dir):
                return None  # the monitor died: the caller takes over
            await asyncio.sleep(0.2)
        return False

    async def verify_startup(self, app):
        """Confirm this process's own update, or take over one whose driver was killed.

        A process started by the monitor confirms the transaction named in its
        environment while the monitor holds the deployment lock. Any other
        start (manual restart, or the monitor died) takes the lock: switched
        but unverified targets verify themselves within ws_timeout, everything
        else is rolled back to the original commit.
        """
        state = self._state()
        if state.get('status') not in ACTIVE:
            return False
        transaction = state.get('transaction')
        if os.environ.get(TRANSACTION_ENV) == transaction and state.get('status') == 'awaiting_ws':
            confirmed = await self._await_ws(app, transaction)
            if confirmed is not None:
                return confirmed
        fd = await asyncio.to_thread(acquire_deploy_lock, self.base_dir, 1.0)
        if fd is None:
            return False
        try:
            head = self._current_sha()
            outcome = {}

            def adopt(current):
                if head != current.get('target_sha'):
                    return False
                outcome['adopted'] = bool(
                    transition(
                        self.base_dir,
                        current['transaction'],
                        ('awaiting_restart', 'awaiting_ws'),
                        status='awaiting_ws',
                        phase='verifying',
                        supervisor_pid=os.getpid(),
                    )
                )
                return outcome['adopted']

            await asyncio.to_thread(take_over, self.base_dir, adopt)
            if outcome.get('adopted'):
                if await self._await_ws(app, transaction, time.monotonic() + self.ws_timeout):
                    return True
                transition(self.base_dir, transaction, ('awaiting_ws',), status='rolling_back', message='WS 验证超时，正在回滚', phase='checking_out')
                await asyncio.to_thread(finish_rollback, self.base_dir, transaction, '接管验证超时，已回滚原提交并重启；数据库备份已保留')
            if self._current_sha() != head:
                # The code on disk changed under this process: restart into it.
                app._restart_requested = True
                if app._stop_event:
                    app._stop_event.set()
            return False
        finally:
            os.close(fd)
