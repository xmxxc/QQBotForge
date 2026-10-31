"""Owner HTTP and QQ management share the scoped authorization operations."""

import asyncio
import json
import sqlite3

from aiohttp import web

from core.base.config import cfg
from core.base.logger import EXTENSION, get_logger
from core.display import display_group, display_member
from core.plugin._blacklist import get_blacklist_map

PREFIX = '/api/ext/access-control/'
log = get_logger(EXTENSION, '黑白名单授权')
# Fields holding native QQ group / member IDs. Resource category groups and scopes are not QQ groups.
GROUP_KEYS = {'group', 'groups'}
MEMBER_KEYS = {'member', 'members'}
SKIP_KEYS = {'resources', 'resource_scope', 'function_scope', 'credential'}
NAMES_BATCH = 200
SUMMARY_MAX = 10
OPERATIONS = {
    'snapshot': '查看策略与来源',
    'register': '注册消费方',
    'resources': '保存资源清单',
    'policy': '保存功能策略',
    'grant': '授权',
    'revoke': '撤销授权',
    'deny': '明确拒绝',
    'allow': '移除拒绝',
    'expiry': '修改来源期限',
    'function_group': '保存功能组',
    'bulk_grant': '批量授权',
    'bulk_revoke': '批量撤销',
    'bulk_expiry': '批量期限',
    'explain': '解释资格',
    'audience': '全员资格报告',
    'join_rule': '关闭入群自动授权',
    'adopt_legacy': '接管旧来源',
    'binding_list': '查看身份绑定',
    'binding_confirm': '确认身份关联',
    'binding_reject': '拒绝绑定申请',
    'binding_revoke': '撤销身份关联',
    'binding_import': '身份迁移',
}


def native_ids(value, groups, members):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in SKIP_KEYS:
                continue
            target = groups if key in GROUP_KEYS else members if key in MEMBER_KEYS else None
            if target is not None:
                target.update(v for v in (item if isinstance(item, list) else [item]) if isinstance(v, str) and v)
            native_ids(item, groups, members)
    elif isinstance(value, list):
        for item in value:
            native_ids(item, groups, members)


async def display_names(bot, *values):
    """Display text for the IDs an owner reply or page shows; identities and data keep native IDs."""
    groups, members = set(), set()
    for value in values:
        native_ids(value, groups, members)
    groups, members = sorted(groups), sorted(members)
    # Resolve every identity, with a bounded number of concurrent lookups.
    identities = [(display_group, g) for g in groups] + [(display_member, m) for m in members]
    shown = []
    for start in range(0, len(identities), NAMES_BATCH):
        shown.extend(await asyncio.gather(*(resolve(bot, native) for resolve, native in identities[start:start + NAMES_BATCH])))
    split = len(groups)
    return {'groups': dict(zip(groups, shown[:split], strict=True)), 'members': dict(zip(members, shown[split:], strict=True))}


def owner_summary(request, result, names):
    """Chinese owner summary sent before the unchanged JSON reply."""
    operation = request.get('operation', '') if isinstance(request, dict) else ''
    consumer = request.get('consumer', '') if isinstance(request, dict) else ''
    title = OPERATIONS.get(operation, '操作')
    lines = [f'授权管理：{title}已处理' + (f'（消费方 {consumer}）' if consumer else '')]
    if isinstance(result, dict) and isinstance(result.get('allowed'), bool):
        lines.append('结论：允许' if result['allowed'] else f'结论：不允许（{result.get("reason") or "原因未知"}）')
    for label, kind in (('群', 'groups'), ('成员', 'members')):
        shown = list(names[kind].values())
        if shown:
            more = f' 等 {len(shown)} 个' if len(shown) > SUMMARY_MAX else ''
            lines.append(f'{label}：' + '、'.join(shown[:SUMMARY_MAX]) + more)
    lines.append('原始 JSON 见下一条消息')
    return '\n'.join(lines)


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
        if operation == 'join_rule' and values.get('enabled') is not False:
            # Owners may narrow here; enabling automatic grants belongs to the consumer that owns the rule.
            raise ValueError('入群自动授权只能由消费方插件开启，此处只能关闭')
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
            body = {'success': True, 'data': result, 'names': await display_names(bot, result)}
            return web.json_response(body, headers={'Cache-Control': 'no-store'})
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
                # Recording is best effort: neither its failure nor a second cancel
                # may replace the original cancellation.
                if result:
                    try:
                        await self.service.bindings.delivery(event.appid, result['id'], 'send_unknown', 'cancelled')
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        log.exception(f'[{event.appid}] 绑定申请 {result["id"]} 的取消回执未能记录')
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
        summary = None
        try:
            data = json.loads(text.split(maxsplit=1)[1])
            result = await self.execute(event.appid, data, actor=event.user_id)
            content = '已处理，请在主人私聊或后台查看详情' if event.is_group else json.dumps(result, ensure_ascii=False)
            if not event.is_group:
                summary = owner_summary(data, result, await display_names(event.appid, data, result))
        except (ValueError, TypeError, KeyError) as error:
            content = str(error)
        except (sqlite3.Error, OSError, RuntimeError):
            content = '该授权范围暂不可用，请检查存储与群事实'
        if summary is not None:
            await source.reply(event, summary)
        await source.reply(event, content)
        return None
