"""忍者必须死3 plugin: extension routes, owner page and QQ entry points."""

from pathlib import Path

from aiohttp import web

from core.plugin import context
from core.plugin.decorators import handler, interceptor, on_load, on_unload
from core.plugin.web_pages import register_page, register_route, unregister_page, unregister_route

from .service import PREFIX, UNAVAILABLE, Ninja3

__plugin_meta__ = {'name': '忍者必须死3', 'description': '主人维护账号，授权成员在开放群内查询', 'version': '1.0.0'}
_ctx = context.ctx
_instance = None


@register_route('*', PREFIX + '*', auth=True)
async def web_entry(request):
    if _instance is None:
        return web.json_response({'success': False, 'error': UNAVAILABLE}, status=503)
    response = await _instance.http(request)
    response.headers['Cache-Control'] = 'no-store'
    return response


register_page('ninja3', '忍者必须死3', source_name='ninja3', html_file=str(Path(__file__).with_name('page.html')))


@on_load
async def load():
    global _instance
    candidate = Ninja3(_ctx.get_data_path('ninja3.db'))
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
    unregister_page('ninja3')
    unregister_route('*', PREFIX + '*')
    if _instance is not None:
        await _instance.emergency.shutdown()
        await _instance.password_batches.shutdown()
        await _instance.password_tasks.shutdown()
        _instance.close()
        _instance = None


@handler(
    r'^(?:忍3(?:批量(?:改密|确认|任务|取消)|改密|确认|重试|任务|取消|恢复|核验|放弃|紧急模式|退出紧急模式|紧急状态)(?:\s.*)?|授权(?:\s.*)?|账号列表|账号\s+\S.*|忍3)$',
    name='忍者必须死3',
    priority=20,
    block=True,
    event_types=['GROUP_AT_MESSAGE_CREATE', 'GROUP_MESSAGE_CREATE', 'C2C_MESSAGE_CREATE'],
)
async def entry(event, _match):
    if _instance is None:
        await event.reply('忍者必须死3 未加载，请联系主人')
        return
    content = (event.content or '').strip()
    if content.startswith(('忍3紧急模式', '忍3退出紧急模式', '忍3紧急状态')):
        await _instance.emergency_command(event)
    elif content.startswith(('忍3批量', '忍3改密', '忍3确认', '忍3重试', '忍3任务', '忍3取消', '忍3恢复', '忍3核验', '忍3放弃')):
        await _instance.password_command(event)
    elif content.startswith('授权'):
        await _instance.authorize(event)
    elif content == '账号列表':
        await _instance.query(event)
    elif content == '忍3':
        await _instance.owner_status(event)
    else:
        await _instance.query(event, content.split(maxsplit=1)[1].strip())


@interceptor(priority=200)
async def menu_route(event):
    return await _instance.route(event) if _instance is not None else False


@handler(r'^n3:', name='忍3菜单回调', event_types=['INTERACTION_CREATE'], priority=30, block=True)
async def menu_callback(event, _match):
    if _instance is not None:
        await _instance.on_interaction(event)
