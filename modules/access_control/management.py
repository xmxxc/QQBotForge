"""Owner HTTP and QQ management share the scoped authorization operations."""

import asyncio
import json
import sqlite3

from aiohttp import web

from core.base.config import cfg
from core.plugin._blacklist import get_blacklist_map

PREFIX = '/api/ext/access-control/'


def classify_receipt(reply):
    """Classify a Sender.reply result as (notification, error).

    reply returns the platform response dict on success ({} for an empty body),
    the error dict on failure (``code`` -1 is the framework's network failure),
    and None when nothing was sent or the framework suppressed the error. It
    drops the success flag, so an error-shaped body (a code, an err_code, or a
    message without a message id) counts as rejected, never as sent.
    """
    if not isinstance(reply, dict):
        return 'send_failed', 'no_response'
    code = next((reply[key] for key in ('code', 'err_code') if reply.get(key) not in (None, 0, '', '0')), None)
    if str(code) == '-1':
        return 'send_failed', 'network_error'
    if code is not None:
        return 'platform_rejected', str(code)[:128]
    if 'message' in reply and 'id' not in reply:
        return 'platform_rejected', 'unknown'
    return 'sent', None


class Management:
    def __init__(self, service):
        self.service = service

    async def execute(self, bot, data, *, actor='web_admin'):
        if not isinstance(data, dict):
            raise ValueError('操作须为对象')
        values = dict(data)
        consumer, operation = values.pop('consumer', ''), values.pop('operation', '')
        if operation == 'binding_import':
            if consumer or set(values) != {'bundle'}:
                raise ValueError('身份迁移须提供bundle且不选择消费方')
            return await self.service.bindings.import_history(bot, values['bundle'])
        if operation.startswith('binding_'):
            if consumer:
                raise ValueError('身份关联独立于消费方，不填写consumer')
            return await self.service.bindings.manage(bot, operation, values, actor)
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
        if operation == 'audience':
            if set(values) - {'function', 'group', 'resource', 'actions'} or not {'function', 'group'} <= values.keys():
                raise ValueError('全员报告须明确功能和群')
            return self.service.audience.owner_view(await self.service.audience_report(bot, consumer, **values))
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
        if event.event_type == 'C2C_MESSAGE_CREATE':
            await self.service.bindings.observe(event)
        text = (event.content or '').strip()
        if event.event_type == 'C2C_MESSAGE_CREATE' and text.startswith('身份绑定 '):
            if any(value in get_blacklist_map(event.appid, 'user') for value in (event.user_id, event.raw_user_id)):
                return None
            from core.application import get_app

            app = get_app()
            instance = app.get_bot(event.appid) if app else None
            if not instance:
                return None
            result = None
            try:
                words = text.split()
                if len(words) == 4 and words[1] == '申请':
                    result = await self.service.bindings.request(event.appid, event.get('d/author/id'), words[2], words[3])
                    content = '绑定待主人核实，申请编号：' + result['id'] + '；绑定不授予功能资格'
                elif 4 <= len(words) <= 6 and words[1] == '群资格':
                    contexts = await self.service.private_contexts(
                        event.appid,
                        words[2],
                        words[3],
                        event.get('d/author/id'),
                        group=words[4] if len(words) > 4 else '',
                        resource=words[5] if len(words) > 5 else '',
                    )
                    selected = contexts['selected']
                    content = json.dumps(
                        {
                            'reason': contexts['reason'],
                            'groups': [row['group'] for row in contexts['candidates']],
                            'selected': selected['group'] if selected else None,
                        },
                        ensure_ascii=False,
                    )
                else:
                    raise ValueError('身份绑定 申请 <群原生ID> <成员原生ID> / 身份绑定 群资格 <消费方> <功能> [群] [资源]')
            except (ValueError, RuntimeError, sqlite3.Error, OSError):
                content = '绑定申请暂不可用，请核对原生身份或联系主人'
            try:
                status, error = classify_receipt(await instance.sender.reply(event, content))
            except asyncio.CancelledError:
                # The platform may or may not have received it; never record it as sent.
                if result:
                    await self.service.bindings.delivery(event.appid, result['id'], 'send_unknown', 'cancelled')
                raise
            except Exception as failure:
                status, error = 'send_failed', type(failure).__name__[:128]
            if result:
                await self.service.bindings.delivery(event.appid, result['id'], status, error)
            return None
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
            result = await self.execute(event.appid, json.loads(text.split(maxsplit=1)[1]), actor=event.user_id)
            content = '已处理，请在主人私聊或后台查看详情' if event.is_group else json.dumps(result, ensure_ascii=False)
        except (ValueError, TypeError, KeyError) as error:
            content = str(error)
        except (sqlite3.Error, OSError, RuntimeError):
            content = '该授权范围暂不可用，请检查存储与群事实'
        await source.reply(event, content)
        return None
