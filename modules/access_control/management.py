"""Owner HTTP and QQ management share the scoped authorization operations."""

import json
import sqlite3

from aiohttp import web

from core.base.config import cfg
from core.plugin._blacklist import get_blacklist_map

PREFIX = '/api/ext/access-control/'


class Management:
    def __init__(self, service):
        self.service = service

    async def execute(self, bot, data):
        if not isinstance(data, dict):
            raise ValueError('操作须为对象')
        values = dict(data)
        consumer, operation = values.pop('consumer', ''), values.pop('operation', '')
        if operation == 'register':
            if set(values) != {'owner', 'functions'}:
                raise ValueError('注册须明确 owner/functions')
            return await self.service.declare(bot, consumer, **values)
        if operation == 'resources':
            if set(values) != {'resources'}:
                raise ValueError('资源须明确 resources 清单')
            await self.service.declare_resources(bot, consumer, values['resources'])
            return {'saved': True}
        if operation == 'snapshot':
            if values:
                raise ValueError('查看不接受其他字段')
            return await self.service.access_snapshot(bot, consumer)
        if operation == 'explain':
            if set(values) - {'function', 'group', 'member', 'resource', 'action'} or not {'function', 'group', 'member'} <= values.keys():
                raise ValueError('解释须明确功能、群和成员')
            return await self.service.evaluate(bot, consumer, **values)
        return await self.service.manage_access(bot, consumer, operation, **values)

    async def http(self, request):
        from web.auth import authorize_request

        denied = authorize_request(request)
        if denied is not None:
            return denied
        if request.path.rstrip('/') != PREFIX.rstrip('/'):
            return web.json_response({'success': False, 'error': '入口不存在'}, status=404)
        bot = request.query.get('bot', '')
        if not cfg.get_bot_config(bot):
            return web.json_response({'success': False, 'error': '请选择有效 Bot'}, status=404)
        try:
            if request.method == 'GET':
                result = await self.service.access_snapshot(bot, request.query.get('consumer', ''))
            elif request.method == 'POST':
                result = await self.execute(bot, await request.json())
            else:
                return web.json_response({'success': False, 'error': '方法不存在'}, status=405)
            return web.json_response({'success': True, 'data': result}, headers={'Cache-Control': 'no-store'})
        except (ValueError, TypeError, KeyError) as error:
            return web.json_response({'success': False, 'error': str(error)}, status=422)
        except (sqlite3.Error, OSError, RuntimeError):
            return web.json_response({'success': False, 'error': '该授权范围暂不可用，请检查存储与群事实'}, status=503)

    async def on_event(self, event):
        text = (event.content or '').strip()
        if (
            event.event_type not in {'GROUP_AT_MESSAGE_CREATE', 'GROUP_MESSAGE_CREATE', 'C2C_MESSAGE_CREATE'}
            or event.is_channel
            or not text.startswith('授权管理 ')
        ):
            return event
        if event.user_id not in (cfg.get_bot_config(event.appid) or {}).get('owner_ids', []):
            return event
        if any(value in get_blacklist_map(event.appid, 'user') for value in (event.user_id, event.raw_user_id)) or event.group_id in get_blacklist_map(
            event.appid, 'group'
        ):
            return None
        source = self.service.sender_provider(event.appid) if hasattr(self.service, 'sender_provider') else None
        if source is None:
            from core.application import get_app

            app = get_app()
            bot = app.get_bot(event.appid) if app else None
            source = bot.sender if bot else None
        if source is None:
            return None
        try:
            result = await self.execute(event.appid, json.loads(text.split(maxsplit=1)[1]))
            content = '已处理，请在主人私聊或后台查看详情' if event.is_group else json.dumps(result, ensure_ascii=False)
        except (ValueError, TypeError, KeyError) as error:
            content = str(error)
        except (sqlite3.Error, OSError, RuntimeError):
            content = '该授权范围暂不可用，请检查存储与群事实'
        await source.reply(event, content)
        return None
