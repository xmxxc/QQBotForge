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
from core.plugin._blacklist import get_blacklist_map
from core.plugin._dispatch import _strip_leading_bot_name_at

from .menus import PAGE, Menus

PREFIX = '/api/ext/self-qa/'
NAMESPACE = 'public'
COMMAND = '问答管理 '
MESSAGE_TYPES = {'GROUP_AT_MESSAGE_CREATE', 'GROUP_MESSAGE_CREATE', 'C2C_MESSAGE_CREATE'}
SELECT = '问答选择'
CALLBACK = 'qa:'


def owner(event):
    return bool(event.user_id) and event.user_id in ((cfg.get_bot_config(event.appid) or {}).get('owner_ids') or [])


class PublicEntry:
    def __init__(self, qa):
        self.qa = qa
        self.menus = Menus()

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
        if operation == 'test':
            # Owner feedback: what a message would match, without sending.
            if set(values) - {'text', 'group'} or not isinstance(values.get('text'), str):
                raise ValueError('测试须提供 text，可选 group')
            results = await self.qa.match(bot, NAMESPACE, values['text'], group=values.get('group', ''))
            return {'matches': results, 'best': [r['rule_id'] for r in self.candidates(results)]}
        if operation in {'update', 'delete'}:
            key = values.pop('id', '')
            if not isinstance(key, str) or not key:
                raise ValueError('请提供规则 id')
            return await self.qa.manage(bot, NAMESPACE, operation, values if operation == 'update' else None, key)
        raise ValueError('操作须为 list/settings/create/update/delete/test')

    async def reply(self, event, content, **kwargs):
        # before_event runs before dispatch assigns event._sender; use the Bot's sender.
        sender = self.sender(event.appid)
        return await sender.reply(event, content, **kwargs) if sender else None

    async def enabled(self, event):
        settings = await self.qa.settings(event.appid, NAMESPACE)
        return event.group_id in settings['groups'] if event.is_group else settings['private']

    def candidates(self, results):
        """Best tied text answers, one per distinct answer."""
        chosen = {}
        for result in self.qa.best([r for r in results if r['kind'] == 'text']):
            chosen.setdefault(result['target'], result)
        return list(chosen.values())

    async def current(self, event, menu):
        group = event.group_id if event.is_group else ''
        return self.candidates(await self.qa.match(event.appid, NAMESPACE, menu['text'], group=group)) if await self.enabled(event) else []

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
            if text == SELECT or text.startswith(SELECT + ' '):
                return await self.select_text(event, text[len(SELECT) :].strip())
            if not await self.enabled(event):
                return event
            candidates = self.candidates(await self.qa.match(bot, NAMESPACE, text, group=group))
            if not candidates:
                return event
            if len(candidates) == 1:
                await self.reply(event, candidates[0]['target'])
            else:
                await self.offer(event, self.menus.issue(event, NAMESPACE, text, candidates), candidates, 0)
        except (sqlite3.Error, OSError, RuntimeError):
            # Storage trouble never blocks plugin handlers.
            return event
        return None

    async def offer(self, event, token, candidates, page):
        page, last, shown = self.menus.page(candidates, page)
        start = page * PAGE
        lines = ['找到多个答案，请选择：'] + [f'{start + i + 1}. {c["name"]}' for i, c in enumerate(shown)]
        if last:
            lines.append(f'第 {page + 1}/{last + 1} 页，发送：{SELECT} 页 <页码>')
        lines.append(f'也可发送：{SELECT} <序号>')
        text = '\n'.join(lines)
        caller = event.raw_user_id or event.user_id
        rows = [
            [{'text': c['name'][:20], 'type': 1, 'data': f'{CALLBACK}{token}:{start + i}', 'list': [caller], 'tips': f'请发送：{SELECT} {start + i + 1}'}]
            for i, c in enumerate(shown)
        ]
        navigation = [
            {'text': label, 'type': 1, 'data': f'{CALLBACK}{token}:p{target}', 'list': [caller], 'tips': f'请发送：{SELECT} 页 {target + 1}'}
            for label, target in (('上一页', page - 1), ('下一页', page + 1))
            if 0 <= target <= last
        ]
        if navigation:
            rows.append(navigation)
        if cfg.get_bot_setting(event.appid, 'message.use_markdown', True):
            try:
                response = await self.reply(event, text, buttons=rows)
                if isinstance(response, tuple) and response and response[0] is True:
                    return
            except Exception:  # noqa: BLE001 - any platform rejection falls back to the equal text choice
                pass
        await self.reply(event, text + '\n按钮暂不可用，请使用以上文字选择。')

    async def choose(self, event, menu, token, action):
        """Re-read current rules for this caller/context; stale choices answer nothing."""
        candidates = await self.current(event, menu)
        if action.startswith('p'):
            menu['rules'] = [c['rule_id'] for c in candidates]
            if not candidates:
                await self.reply(event, '这些答案已变化，请重新提问。')
                return
            await self.offer(event, token, candidates, int(action[1:]))
            return
        index = int(action)
        rule_id = menu['rules'][index] if 0 <= index < len(menu['rules']) else None
        chosen = next((c for c in candidates if c['rule_id'] == rule_id), None)
        if chosen is None:
            await self.reply(event, '该答案已失效，请重新提问。')
            return
        await self.reply(event, chosen['target'])

    async def select_text(self, event, argument):
        menu, reason = self.menus.resolve(event)
        if menu is None:
            # No menu of this caller here: leave the text to plugins and defaults.
            return event if reason == 'expired' else None
        words = argument.split()
        if len(words) == 2 and words[0] == '页' and words[1].isdecimal():
            action = f'p{int(words[1]) - 1}'
        elif len(words) == 1 and words[0].isdecimal() and int(words[0]) >= 1:
            action = str(int(words[0]) - 1)
        else:
            await self.reply(event, f'请发送：{SELECT} <序号> 或 {SELECT} 页 <页码>')
            return None
        await self.choose(event, menu, menu['token'], action)
        return None

    async def on_interaction(self, event):
        """Button callbacks (before_event): ACK at once, answer separately, keep the event for framework logging."""
        payload = event.content or ''
        if event.event_type != 'INTERACTION_CREATE' or not payload.startswith(CALLBACK):
            return event
        event.set_callback_code(0)
        appid = event.appid
        if (
            event.is_channel
            or not (event.is_group or event.is_direct)
            or str(event.user_id) in get_blacklist_map(appid, 'user')
            or event.group_id
            and str(event.group_id) in get_blacklist_map(appid, 'group')
            or cfg.get_bot_setting(appid, 'maintenance.enabled', False)
            and not owner(event)
        ):
            return event
        parts = payload[len(CALLBACK) :].split(':')
        if len(parts) != 2 or not (parts[1].isdecimal() or parts[1][:1] == 'p' and parts[1][1:].isdecimal()):
            return event
        try:
            menu, reason = self.menus.resolve(event, parts[0])
            if menu is None:
                await self.reply(event, '这不是你的选择菜单。' if reason == 'context_mismatch' else '菜单已失效，请重新提问。')
            else:
                await self.choose(event, menu, parts[0], parts[1])
        except (sqlite3.Error, OSError, RuntimeError):
            await self.reply(event, '问答暂不可用，请稍后重试。')
        return event

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
