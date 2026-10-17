"""Account ownership and shared business entry points for QQ and the extension page."""

import asyncio
import json
import sqlite3
import time
from uuid import uuid4

from aiohttp import web

from core.base.config import cfg
from core.base.logger import PLUGIN, get_logger
from core.plugin._blacklist import get_blacklist_map
from core.storage.extension import ExtensionStore

log = get_logger(PLUGIN, '账户共享')
PREFIX = '/api/ext/account-sharing/'
UNAVAILABLE = '账户共享存储暂不可用，请联系主人检查后台日志'
DENIED = '你暂无此功能的使用权限，请联系主人'
FUNCTION = 'account_sharing'
ACCESS_COMMANDS = {'白名单': 'whitelist', '授权': 'grant', '撤权': 'revoke', '拉黑': 'deny', '解除拉黑': 'allow', '解释': 'explain'}
ACCOUNT_FIELDS = {'name', 'login', 'password', 'enabled', 'description', 'url'}


class BusinessError(ValueError):
    def __init__(self, message, status=422):
        self.status = status
        super().__init__(message)


def validate_account(data, *, create=False):
    if not isinstance(data, dict) or set(data) - ACCOUNT_FIELDS:
        raise BusinessError('账户字段无效')
    if create and not {'name', 'login', 'password'} <= data.keys():
        raise BusinessError('请填写名称、账号和密码')
    for key, value in data.items():
        if key == 'enabled':
            if not isinstance(value, bool):
                raise BusinessError('启用状态须为布尔值')
        elif not isinstance(value, str) or len(value) > 4000 or (key in {'name', 'login', 'password'} and not value.strip()):
            raise BusinessError('账户字段须为非空文字（最多 4000 字符）')
    return data


class AccountSharing:
    def __init__(self, path, *, access_provider=None):
        self.store = ExtensionStore(path)
        self.lock = asyncio.Lock()
        self.active = True
        self.access_provider = access_provider or self._access_module
        self.denial_times = {}

    @staticmethod
    def _access_module():
        from core.application import get_app

        app = get_app()
        return app.module_manager.get('access_control') if app else None

    def access(self):
        module = self.access_provider()
        if module is None or not module.active:
            raise BusinessError('黑白名单授权模块未启用，查询暂停，请联系主人', 503)
        return module

    @staticmethod
    def framework_allowed(event):
        users = get_blacklist_map(event.appid, 'user')
        groups = get_blacklist_map(event.appid, 'group')
        owner = event.user_id in (cfg.get_bot_config(event.appid) or {}).get('owner_ids', [])
        return not (
            event.user_id in users
            or event.raw_user_id in users
            or event.group_id in groups
            or (cfg.get_bot_setting(event.appid, 'maintenance.enabled', False) and not owner)
        )

    async def deny(self, event):
        key = (event.appid, event.group_id, event.raw_user_id)
        now = time.monotonic()
        # Bound retained rate-limit entries without exposing denial details to members.
        if len(self.denial_times) > 10000:
            self.denial_times = {k: t for k, t in self.denial_times.items() if now - t < 10}
        if now - self.denial_times.get(key, -100) >= 10:
            self.denial_times[key] = now
            await event.reply(DENIED)
        log.info(f'[{event.appid}] 查询拒绝 group={event.group_id} member={event.raw_user_id}')

    async def access_management(self, bot, data):
        if not isinstance(data, dict):
            raise BusinessError('授权请求须为对象')
        values = dict(data)
        operation = values.pop('operation', '')
        values.setdefault('function', FUNCTION)
        try:
            if operation == 'explain':
                target = values.get('member', '')
                values['framework_allowed'] = not (
                    target in get_blacklist_map(bot, 'user')
                    or values.get('group', '') in get_blacklist_map(bot, 'group')
                    or (cfg.get_bot_setting(bot, 'maintenance.enabled', False) and target not in (cfg.get_bot_config(bot) or {}).get('owner_ids', []))
                )
            if operation == 'grant':
                for resource in values.get('resources', []):
                    if not await self.store.get(bot, 'accounts', resource):
                        raise BusinessError('账户不存在', 404)
            return await self.access().execute(bot, operation, **values)
        except BusinessError:
            raise
        except (TypeError, ValueError) as error:
            raise BusinessError(str(error) if isinstance(error, ValueError) else '授权参数不完整') from error

    async def directory(self, event):
        if not event.is_group or not self.framework_allowed(event):
            await self.deny(event)
            return
        module = self.access()
        allowed = []
        for account in await self.accounts(event.appid):
            decision = await module.decide(event.appid, FUNCTION, event.group_id, event.raw_user_id, account['id'])
            if account['enabled'] and decision['allowed']:
                allowed.append(account)
        if not allowed:
            await self.deny(event)
            return
        # Re-evaluate after async reads, so a known mutation cannot leak an old list.
        visible = []
        for account in allowed:
            if (await module.decide(event.appid, FUNCTION, event.group_id, event.raw_user_id, account['id']))['allowed']:
                visible.append(f'{account["name"]} — 账户查询 {account["id"]}')
        await event.reply('\n'.join(visible) if visible else DENIED)

    async def initialize(self):
        await self.store.ready()

    def close(self):
        self.active = False
        self.store.close()

    async def accounts(self, bot):
        return await self.store.all(bot, 'accounts')

    async def manage(self, bot, operation, data=None, key=''):
        if not self.active:
            raise BusinessError('账户共享未加载', 503)
        async with self.lock:
            if operation == 'list':
                return await self.accounts(bot)
            old = await self.store.get(bot, 'accounts', key) if key else None
            if operation in {'update', 'delete', 'preview'} and not old:
                raise BusinessError('账户不存在', 404)
            if operation == 'preview':
                if not old['enabled']:
                    raise BusinessError('账户已停用', 409)
                return old
            if operation == 'delete':
                await self.store.delete(bot, 'accounts', key)
                log.info(f'[{bot}] 删除账户 {key}')
                return {'id': key}
            if operation not in {'create', 'update'}:
                raise BusinessError('未知账户操作')
            validate_account(data, create=operation == 'create')
            account = old or {'id': uuid4().hex, 'enabled': True, 'description': '', 'url': ''}
            account.update(data)
            await self.store.put(bot, 'accounts', account['id'], account)
            log.info(f'[{bot}] 保存账户 {account["id"]}')
            return account

    async def http(self, request):
        # Explicit check also protects direct consumers of this registered handler.
        from web.auth import authorize_request

        denied = authorize_request(request)
        if denied is not None:
            return denied
        bot = request.query.get('bot', '')
        if not cfg.get_bot_config(bot):
            return web.json_response({'success': False, 'error': '请选择有效 Bot'}, status=404)
        parts = request.path[len(PREFIX) :].strip('/').split('/')
        try:
            if parts == ['access']:
                if request.method == 'GET':
                    result = await self.access().snapshot(bot)
                elif request.method == 'POST':
                    result = await self.access_management(bot, await request.json())
                else:
                    raise BusinessError('方法不适用', 405)
                return web.json_response({'success': True, 'data': result})
            if parts[0] != 'accounts' or len(parts) > 2:
                raise BusinessError('入口不存在', 404)
            key = parts[1] if len(parts) == 2 else ''
            operation = {'GET': 'list', 'POST': 'create', 'PATCH': 'update', 'DELETE': 'delete'}.get(request.method)
            if not operation or (operation in {'update', 'delete'} and not key) or (operation in {'list', 'create'} and key):
                raise BusinessError('方法不适用', 405)
            data = await request.json() if request.method in {'POST', 'PATCH'} else None
            result = await self.manage(bot, operation, data, key)
            return web.json_response({'success': True, 'data': result}, status=201 if operation == 'create' else 200)
        except (BusinessError, json.JSONDecodeError) as error:
            return web.json_response({'success': False, 'error': str(error)}, status=getattr(error, 'status', 422))

        except (sqlite3.Error, OSError, RuntimeError):
            log.error(f'[{bot}] 账户共享持久化不可用')
            return web.json_response({'success': False, 'error': UNAVAILABLE}, status=503)

    @staticmethod
    def credentials(account):
        return f'{account["name"]}\n账号：{account["login"]}\n密码：{account["password"]}\n{account["description"]}\n{account["url"]}'

    async def on_event(self, event):
        if (event.content or '').strip() == '账户列表':
            try:
                await self.directory(event)
            except (BusinessError, RuntimeError, sqlite3.Error, OSError):
                await self.deny(event)
            return
        if not (event.content or '').startswith('账户管理'):
            return
        bot = event.appid
        if not self.framework_allowed(event) or event.user_id not in (cfg.get_bot_config(bot) or {}).get('owner_ids', []):
            await self.deny(event)
            return
        try:
            words = event.content.split(maxsplit=2)
            action = words[1] if len(words) > 1 else '帮助'
            args = words[2] if len(words) > 2 else ''
            if action in ACCESS_COMMANDS:
                data = json.loads(args) if args else {}
                if not isinstance(data, dict):
                    raise BusinessError('授权请求须为对象')
                data['operation'] = ACCESS_COMMANDS[action]
                if event.is_group:
                    data.setdefault('group', event.group_id)
                if action == '授权' and not data.get('member'):
                    if not event.is_group or data.get('group') != event.group_id:
                        raise BusinessError('@成员授权须使用当前群上下文；其他群请明确填写身份类型与成员标识')
                    targets = [m for m in event.mentions if isinstance(m, dict) and m.get('id') and not m.get('is_you')]
                    if len(targets) == 1:
                        data['member'] = targets[0]['id']
                        data['identity_type'] = 'group_member_openid'
                result = await self.access_management(bot, data)
                await event.reply(json.dumps(result, ensure_ascii=False) if event.is_direct else '授权操作已保存，详情请在主人私聊或后台查看')
                return
            if action == '预览':
                if not event.is_direct or event.is_channel:
                    raise BusinessError('请在主人私聊中预览账户')
                account = await self.manage(bot, 'preview', key=args)
                await event.reply(self.credentials(account))
                return
            if action == '新增':
                await self.manage(bot, 'create', json.loads(args))
            elif action == '修改':
                key, payload = args.split(maxsplit=1)
                await self.manage(bot, 'update', json.loads(payload), key)
            elif action == '删除':
                await self.manage(bot, 'delete', key=args)
            elif action == '列表':
                accounts = await self.accounts(bot)
                await event.reply(
                    '\n'.join(f'{a["id"]} {a["name"]} {"启用" if a["enabled"] else "停用"}' for a in accounts)
                    if event.is_direct
                    else '请在主人私聊或后台查看账户详情'
                )
                return
            else:
                await event.reply('账户管理 新增 <JSON> / 修改 <ID> <JSON> / 删除 <ID> / 列表 / 预览 <ID>（仅私聊）')
                return
            await event.reply('账户操作已保存；可在主人私聊或后台继续查看')
        except (ValueError, BusinessError) as error:
            await event.reply(str(error) if isinstance(error, BusinessError) else '命令参数无效，请检查 JSON 和账户 ID')
        except (sqlite3.Error, OSError, RuntimeError):
            log.error(f'[{bot}] 账户共享持久化不可用')
            await event.reply(UNAVAILABLE)
