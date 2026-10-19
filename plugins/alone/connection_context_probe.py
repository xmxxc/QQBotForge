"""发送“连接状态验收 标签”时记录实际 Event 上下文，仅回复简短确认。"""

from core.application import get_app
from core.plugin.decorators import handler


@handler(r'^连接状态验收(?:\s+(\S+))?$', name='连接状态验收', priority=1000)
async def connection_context_probe(event, match):
    app = get_app()
    probe = app.module_manager.get('connection_status_probe') if app and app.module_manager else None
    if probe is None:
        await event.reply('请先启用连接状态真机观察模块。', msg_type=0)
        return
    probe.record_event(event, match.group(1))
    context = event.connection_context
    transport = context.transport if context else 'missing'
    await event.reply(f'已记录连接状态验收：{transport}', msg_type=0)
