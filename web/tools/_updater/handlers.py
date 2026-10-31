"""框架更新 — 版本检查/更新日志/在线更新/上传更新"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from aiohttp import web

from web.tools._market.mirror import _test_one_mirror
from web.tools._market.mirrors import GITHUB_FILE_MIRRORS, _load_mirror_cache, _save_mirror_cache, clear_mirror_cache
from web.tools._updater.framework import FrameworkUpdater
from web.tools._updater.supervisor import UNSUPPORTED

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
    environment = updater.environment()
    is_git = environment == 'git'
    warnings = [] if is_git else [UNSUPPORTED if environment == 'unsupported' else '请拉取新镜像或重新 Git 部署']
    return web.json_response({'success': True, 'data': {'git': is_git, 'writable': is_git, 'warnings': warnings}})


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
    if not updater.supported():
        return web.json_response({'success': False, 'message': UNSUPPORTED}, status=400)
    try:
        if (_update_task and not _update_task.done()) or updater.lock_active():
            return web.json_response({'success': False, 'message': '已有更新或启动验证正在进行'}, status=409)
    except OSError:
        return web.json_response({'success': False, 'message': '无法获取更新锁，请检查运行目录权限和文件系统 flock 支持'}, status=400)

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
# 仅供插件市场（Market.js）使用；框架更新的 Git fetch 完全不读取这些设置。


async def handle_get_mirrors(request: web.Request):
    """获取镜像列表 (含缓存的测速结果)"""
    try:
        updater = _get_updater()
        cached = _load_mirror_cache()
        return web.json_response(
            {
                'success': True,
                'data': {
                    'mirrors': list(GITHUB_FILE_MIRRORS),
                    'fast_mirrors': cached,
                    'custom_mirror': updater.custom_mirror,
                },
            }
        )
    except Exception as e:
        return web.json_response({'success': False, 'message': str(e)}, status=500)


async def handle_test_mirrors(request: web.Request):
    """SSE 流式测速所有镜像, 每完成一个立即推送"""
    clear_mirror_cache()
    resp = web.StreamResponse()
    resp.content_type = 'text/event-stream'
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'
    await resp.prepare(request)

    all_results = []
    tasks = {asyncio.ensure_future(_test_one_mirror(m, 3)): m for m in GITHUB_FILE_MIRRORS}
    tasks[asyncio.ensure_future(_test_one_mirror('', 3))] = ''

    pending = set(tasks.keys())
    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            result = task.result()
            all_results.append(result)
            try:
                await resp.write(f'data: {json.dumps(result, ensure_ascii=False)}\n\n'.encode())
            except ConnectionResetError:
                return resp

    # 保存测速结果到磁盘
    _save_mirror_cache(sorted([r for r in all_results if r['success']], key=lambda r: r['latency']))

    # 发送结束标记
    try:
        await resp.write(b'data: {"done": true}\n\n')
        await resp.write_eof()
    except Exception as e:
        log.debug(f'SSE 结束标记发送失败: {e}')
    return resp


async def handle_set_custom_mirror(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    mirror = body.get('mirror', '')
    _get_updater().set_custom_mirror(mirror)
    return web.json_response({'success': True, 'message': f'已设置自定义镜像: {mirror or "(自动选择)"}'})


# ==================== 上传更新 ====================


async def handle_upload_update(request: web.Request):
    """Archive replacement is intentionally unsupported."""
    return web.json_response({'success': False, 'message': '上传更新已禁用，请使用 personal 的 Git 提交'})
