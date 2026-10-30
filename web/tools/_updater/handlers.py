"""框架更新 — 版本检查/更新日志/在线更新/上传更新"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiohttp import web

from web.tools._updater.framework import FrameworkUpdater

log = logging.getLogger('ElainaBot.web.updater')

_base_dir = ''
_updater = None
_update_task = None


def set_context(base_dir: str):
    global _base_dir, _updater
    _base_dir = base_dir
    _updater = FrameworkUpdater(base_dir)


def get_updater(base_dir: str | None = None):
    # Startup recovery and the owner menu must work without the web panel.
    if not _updater and base_dir:
        set_context(base_dir)
    if not _updater:
        raise RuntimeError('更新器未初始化')
    return _updater


_get_updater = get_updater  # older callers


# ==================== 环境检测 ====================


async def handle_detect_environment(request: web.Request):
    updater = _get_updater()
    is_git = updater.environment() == 'git'
    return web.json_response({'success': True, 'data': {'git': is_git, 'writable': is_git, 'warnings': [] if is_git else ['请拉取新镜像或重新 Git 部署']}})


# ==================== 更新日志 ====================


async def handle_get_changelog(request: web.Request):
    try:
        updater = _get_updater()
        commits = await updater.fetch_changelog()
        if commits is None:
            return web.json_response(
                {
                    'success': False,
                    'message': 'GitHub API 请求失败，请检查网络或稍后重试',
                    'data': [],
                }
            )

        result = []
        for c in commits if isinstance(commits, list) else []:
            info = c.get('commit')
            if not info:
                continue
            author = info.get('author', {})
            date_str = author.get('date', '')
            try:
                fmt = datetime.fromisoformat(date_str.replace('Z', '+00:00')).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                fmt = '未知时间'
            result.append(
                {
                    'sha': c.get('sha', '')[:8],
                    'message': info.get('message', '').strip(),
                    'author': author.get('name', '未知'),
                    'date': fmt,
                    'url': c.get('html_url', ''),
                    'full_sha': c.get('sha', ''),
                }
            )
        return web.json_response(
            {
                'success': True,
                'data': result,
                'total': len(result),
            }
        )
    except Exception as e:
        return web.json_response({'success': False, 'message': str(e)}, status=500)


# ==================== 版本信息 ====================


async def handle_get_current_version(request: web.Request):
    try:
        info = _get_updater().get_version_info()
        return web.json_response({'success': True, 'data': info})
    except Exception as e:
        return web.json_response({'success': False, 'message': str(e)}, status=500)


# ==================== 检查更新 ====================


async def handle_check_update(request: web.Request):
    try:
        data = await _get_updater().check_for_updates()
        return web.json_response({'success': True, 'data': data})
    except Exception as e:
        return web.json_response({'success': False, 'message': str(e)}, status=500)


# ==================== 进度 ====================


async def handle_get_update_progress(request: web.Request):
    try:
        return web.json_response({'success': True, 'data': _get_updater().get_progress()})
    except Exception as e:
        return web.json_response({'success': False, 'message': str(e)}, status=500)


# ==================== 开始更新 ====================


async def handle_start_update(request: web.Request):
    global _update_task
    try:
        body = await request.json()
    except (ValueError, TypeError):
        return web.json_response({'success': False, 'message': '请求必须是 JSON 对象'}, status=400)
    if not isinstance(body, dict) or ('version' in body and not isinstance(body['version'], str)):
        return web.json_response({'success': False, 'message': '更新参数无效'}, status=400)
    updater = get_updater()
    if (_update_task and not _update_task.done()) or updater.lock_file.exists():
        return web.json_response({'success': False, 'message': '已有更新或启动验证正在进行'}, status=409)

    async def do_update():
        try:
            if body.get('version'):
                result = await updater.update_to_version(body['version'])
            else:
                result = await updater.update_to_latest()
            if not result['success']:
                updater._report('failed', result['message'])
        except Exception:
            log.exception('手动更新失败')
            updater._report('failed', '更新失败，请查看框架日志')

    updater._report('preparing', '正在准备手动更新', 0)
    _update_task = asyncio.create_task(do_update())
    return web.json_response({'success': True, 'message': '更新已开始；重启后请确认群成员并使用「群状态 恢复」'})


# ==================== 镜像管理 ====================


async def handle_get_mirrors(request: web.Request):
    return web.json_response({'success': True, 'data': {'mirrors': [], 'fast_mirrors': [], 'custom_mirror': '', 'direct': True}})


async def handle_test_mirrors(request: web.Request):
    return web.json_response({'success': False, 'message': '更新器只允许直连 GitHub，不提供镜像测速'})


async def handle_set_custom_mirror(request: web.Request):
    return web.json_response({'success': False, 'message': '更新器只允许直连 GitHub，不支持第三方镜像'})


# ==================== 上传更新 ====================


async def handle_upload_update(request: web.Request):
    """Archive replacement is intentionally unsupported."""
    return web.json_response({'success': False, 'message': '上传更新已禁用，请使用 personal 的 Git 提交'})
