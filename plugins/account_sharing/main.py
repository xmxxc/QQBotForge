"""Account sharing plugin: extension routes and QQ entry point."""

from pathlib import Path

from aiohttp import web

from core.plugin import context
from core.plugin.decorators import handler, on_load, on_unload
from core.plugin.web_pages import register_page, register_route, unregister_page, unregister_route

from .service import PREFIX, UNAVAILABLE, AccountSharing

__plugin_meta__ = {'name': '账户共享', 'description': '主人管理账户与授权查询', 'version': '1.0.0'}
_ctx = context.ctx
_instance = None


@register_route('*', PREFIX + '*', auth=True)
async def web_entry(request):
    if _instance is None:
        return web.json_response({'success': False, 'error': UNAVAILABLE}, status=503)
    return await _instance.http(request)


register_page('account-sharing', '账户共享', source_name='account_sharing', html_file=str(Path(__file__).with_name('page.html')))


@on_load
async def load():
    global _instance
    candidate = AccountSharing(_ctx.get_data_path('account-sharing.db'))
    try:
        await candidate.initialize()
    except Exception:
        candidate.close()
        _ctx.log.error(UNAVAILABLE)
        raise
    _instance = candidate


@on_unload
async def unload():
    global _instance
    unregister_page('account-sharing')
    unregister_route('*', PREFIX + '*')
    if _instance is not None:
        _instance.close()
        _instance = None


@handler(r'^(?:账户管理(?:\s|$)|账户列表$)', name='账户管理', priority=20)
async def account_management(event, _match):
    if _instance is None:
        await event.reply('账户共享未加载，请联系主人')
        return
    await _instance.on_event(event)
