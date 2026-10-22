"""Public question/answer message entry and owner management for the ``public`` namespace.

Messages arrive through the framework ``before_handler`` hook, which runs after
message logging and the dispatcher's receive checks (blacklist, maintenance,
non-@ permission, interceptors). Not enabled or not matched messages pass on
to plugin handlers untouched; a matched answer consumes the message.
"""

import json
import sqlite3

from aiohttp import web

from core.base.config import cfg
from core.plugin._dispatch import _strip_leading_bot_name_at

PREFIX = '/api/ext/self-qa/'
NAMESPACE = 'public'
COMMAND = '问答管理 '
MESSAGE_TYPES = {'GROUP_AT_MESSAGE_CREATE', 'GROUP_MESSAGE_CREATE', 'C2C_MESSAGE_CREATE'}


def owner(event):
    return bool(event.user_id) and event.user_id in ((cfg.get_bot_config(event.appid) or {}).get('owner_ids') or [])


class PublicEntry:
    def __init__(self, qa):
        self.qa = qa

    @staticmethod
    def sender(bot):
        from core.application import get_app

        app = get_app()
        instance = app.get_bot(bot) if app else None
        return instance.sender if instance else None

    async def execute(self, bot, data):
        if not isinstance(data, dict):
            raise ValueError('操作须为对象')
        values = dict(data)
        operation = values.pop('operation', '')
        if operation == 'list':
            if values:
                raise ValueError('查看不接受其他字段')
            return {'settings': await self.qa.settings(bot, NAMESPACE), 'rules': await self.qa.manage(bot, NAMESPACE, 'list')}
        if operation == 'settings':
            return await self.qa.save_settings(bot, NAMESPACE, values)
        if operation == 'create':
            return await self.qa.manage(bot, NAMESPACE, 'create', {'kind': 'text', **values})
        if operation in {'update', 'delete'}:
            key = values.pop('id', '')
            if not isinstance(key, str) or not key:
                raise ValueError('请提供规则 id')
            return await self.qa.manage(bot, NAMESPACE, operation, values if operation == 'update' else None, key)
        raise ValueError('操作须为 list/settings/create/update/delete')

    async def on_event(self, event):
        text = (event.content or '').strip()
        if event.event_type not in MESSAGE_TYPES or event.is_channel or not text:
            return event
        if text.startswith(COMMAND) and owner(event):
            return await self.command(event, text)
        bot, group = event.appid, event.group_id if event.is_group else ''
        if event.event_type == 'GROUP_MESSAGE_CREATE' and cfg.get_bot_setting(bot, 'non_at_message.strip_bot_name_at', False):
            text = _strip_leading_bot_name_at(text, getattr(self.sender(bot), '_bot_name', '') or '').strip()
        try:
            settings = await self.qa.settings(bot, NAMESPACE)
            if group not in settings['groups'] if group else not settings['private']:
                return event
            sender = self.sender(bot)
            if sender is None:
                return event
            sent = await self.qa.answer(bot, NAMESPACE, text, lambda target: sender.reply(event, target), allowed=True, group=group)
        except (sqlite3.Error, OSError, RuntimeError):
            # Storage trouble never blocks plugin handlers.
            return event
        return None if sent else event

    async def command(self, event, text):
        sender = self.sender(event.appid)
        if sender is None:
            return None
        try:
            result = await self.execute(event.appid, json.loads(text[len(COMMAND) :]))
            content = '已处理，请在主人私聊或后台查看详情' if event.is_group else json.dumps(result, ensure_ascii=False)
        except (ValueError, TypeError, KeyError) as error:
            content = str(error)
        except (sqlite3.Error, OSError, RuntimeError):
            content = '问答存储暂不可用'
        await sender.reply(event, content)
        return None

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
                result = await self.execute(bot, {'operation': 'list'})
            elif request.method == 'POST':
                result = await self.execute(bot, await request.json())
            else:
                return web.json_response({'success': False, 'error': '方法不存在'}, status=405)
            return web.json_response({'success': True, 'data': result}, headers={'Cache-Control': 'no-store'})
        except (ValueError, TypeError, KeyError) as error:
            return web.json_response({'success': False, 'error': str(error)}, status=422)
        except (sqlite3.Error, OSError, RuntimeError):
            return web.json_response({'success': False, 'error': '问答存储暂不可用'}, status=503)
