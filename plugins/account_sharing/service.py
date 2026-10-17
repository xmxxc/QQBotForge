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

from .identities import IdentityBindings

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
    def __init__(self, path, *, access_provider=None, sender_provider=None, qa_provider=None):
        self.store = ExtensionStore(path)
        self.lock = asyncio.Lock()
        self.active = True
        self.access_provider = access_provider or self._access_module
        self.denial_times = {}
        self.sender_provider = sender_provider or self._bot_sender
        self.identities = IdentityBindings(self.store, self.access)
        self.qa_provider = qa_provider or self._qa_module

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
    def _qa_module():
        from core.application import get_app

        app = get_app()
        return app.module_manager.get('self_qa') if app else None

    def qa(self):
        module = self.qa_provider()
        if module is None or not module.active:
            raise BusinessError('自助问答模块未启用，关键词查询暂停', 503)
        return module

    async def rule_management(self, bot, operation, data=None, key=''):
        try:
            if operation in {'create', 'update'} and isinstance(data, dict):
                old = await self.qa().manage(bot, FUNCTION, 'get', key=key) if key else None
                merged = {**(old or {}), **data}
                if merged.get('kind', 'resource') == 'resource' and not await self.store.get(bot, 'accounts', merged.get('target', '')):
                    raise BusinessError('规则引用的账户不存在', 404)
            return await self.qa().manage(bot, FUNCTION, operation, data, key)
        except KeyError as error:
            raise BusinessError('规则不存在', 404) from error
        except BusinessError:
            raise
        except ValueError as error:
            raise BusinessError(str(error)) from error

    async def keyword_query(self, event):
        module = self.qa_provider()
        if module is None or not module.active:
            return
        matches = await module.match(event.appid, FUNCTION, event.content or '')
        qa_guard = (module, module.version(event.appid, FUNCTION))
        if not matches:
            return
        context = await self.resolve_context(event)
        if not context:
            await self.deny(event)
            return
        if context.get('selection_required'):
            return
        allowed = []
        for match in matches:
            resource = match['target'] if match['kind'] == 'resource' else ''
            decision = await self.access().decide(
                event.appid,
                FUNCTION,
                context['group'],
                context['member'],
                resource,
                action='view' if match['kind'] == 'resource' else 'invoke',
                framework_allowed=await self.framework_context_allowed(event.appid, context['group'], context['member']),
            )
            if not decision['allowed']:
                continue
            if match['kind'] == 'resource':
                account = await self.store.get(event.appid, 'accounts', resource)
                if not account or not account['enabled']:
                    continue
                match['name'] = account['name']
            allowed.append(match)
        selected = {}
        for match in module.best(allowed):
            selected.setdefault((match['kind'], match['target']), match)
        candidates = list(selected.values())
        if not candidates:
            await self.deny(event)
        elif len(candidates) == 1:
            match = candidates[0]
            await self.query(
                event, match['target'] if match['kind'] == 'resource' else '', rule_id=match['rule_id'] if match['kind'] == 'text' else None, qa_guard=qa_guard
            )
        else:
            await event.reply(
                '请选择有权候选：\n'
                + '\n'.join(f'{m["name"]} — ' + (f'账户查询 {m["target"]}' if m['kind'] == 'resource' else f'账户答复 {m["rule_id"]}') for m in candidates)
            )

    async def query_content(self, bot, resource, rule_id):
        if rule_id:
            rule = await self.qa().manage(bot, FUNCTION, 'get', key=rule_id)
            return rule['target'] if rule and rule['enabled'] and rule['kind'] == 'text' else None
        account = await self.store.get(bot, 'accounts', resource)
        return self.credentials(account) if account and account['enabled'] else None

    @staticmethod
    def _bot_sender(bot):
        from core.application import get_app

        app = get_app()
        instance = app.get_bot(bot) if app else None
        return instance.sender if instance else None

    @staticmethod
    def framework_subject_allowed(bot, group, user, raw_user=''):
        users = get_blacklist_map(bot, 'user')
        groups = get_blacklist_map(bot, 'group')
        owner = user in (cfg.get_bot_config(bot) or {}).get('owner_ids', [])
        return not (user in users or raw_user in users or group in groups or (cfg.get_bot_setting(bot, 'maintenance.enabled', False) and not owner))

    @classmethod
    def framework_allowed(cls, event):
        return cls.framework_subject_allowed(event.appid, event.group_id, event.user_id, event.raw_user_id)

    async def operation(self, bot, kind, **status):
        result = {'id': uuid4().hex, 'kind': kind, 'created_at': time.time(), **status}
        await self.store.put(bot, 'operations', result['id'], result)
        log.info(f'[{bot}] {kind} operation={result["id"]}')
        return result

    async def notify_result(self, bot, result):
        owner = await self.store.get(bot, 'owners', 'observed_c2c')
        sender = self.sender_provider(bot)
        owner_ids = (cfg.get_bot_config(bot) or {}).get('owner_ids', [])
        if (
            not owner
            or sender is None
            or owner.get('authenticated_id') not in owner_ids
            or not self.framework_subject_allowed(bot, '', owner.get('authenticated_id', ''), owner['private_id'])
        ):
            result['notification'] = 'unavailable'
        else:
            try:
                response = await sender.send_to_user(
                    owner['private_id'],
                    f'账户共享操作 {result["id"]}：{result["kind"]}；授权={result.get("authorization", "未变更")}，绑定={result.get("binding", "未变更")}，发送={result.get("delivery", "未请求")}；详情请在后台查看',
                )
                result['notification'] = 'sent' if response[0] else 'failed'
            except Exception:
                result['notification'] = 'failed'
        await self.store.put(bot, 'operations', result['id'], result)
        return result

    async def grant_result(self, bot, source):
        binding = await self.identities.lookup(bot, source['group'], source['member'])
        result = await self.operation(
            bot,
            'grant',
            group=source['group'],
            member=source['member'],
            source=source['id'],
            authorization='saved',
            binding='verified' if binding else 'pending',
            notification='pending',
            delivery='not_requested',
        )
        if not binding:
            sender = self.sender_provider(bot)
            try:
                response = (
                    await sender.send_to_group(
                        source['group'], '授权已保存。需绑定的成员请私聊本 Bot：账户绑定 <群原生ID> <成员原生OpenID>，再由主人核对确认。'
                    )
                    if sender
                    else (False, {})
                )
                result['guidance'] = 'sent' if response[0] else 'unavailable'
            except Exception:
                result['guidance'] = 'failed'
        return await self.notify_result(bot, result)

    async def binding_management(self, bot, data, actor='panel_admin'):
        try:
            binding = await self.identities.confirm(bot, data, actor)
        except (ValueError, TypeError) as error:
            raise BusinessError(str(error)) from error
        result = await self.operation(
            bot, 'binding', group=binding['group'], member=binding['member'], binding='verified', notification='pending', delivery='not_requested'
        )
        await self.notify_result(bot, result)
        return binding

    async def resolve_context(self, event, *, select=''):
        if not self.framework_allowed(event):
            return None
        if event.is_group:
            return {'group': event.group_id, 'member': event.raw_user_id}
        if not event.is_direct or event.is_channel:
            return None
        bindings = [
            b
            for b in await self.identities.contexts(event.appid, event.raw_user_id)
            if await self.framework_context_allowed(event.appid, b['group'], b['member'])
        ]
        if select:
            chosen = next((b for b in bindings if b['group'] == select), None)
            if chosen:
                await self.store.put(event.appid, 'contexts', event.raw_user_id, {'group': select})
            return chosen
        if len(bindings) == 1:
            return bindings[0]
        selected = await self.store.get(event.appid, 'contexts', event.raw_user_id)
        chosen = next((b for b in bindings if selected and selected['group'] == b['group']), None)
        if chosen:
            return chosen
        if bindings:
            await event.reply('请选择授权群：\n' + '\n'.join('账户群 ' + b['group'] for b in bindings))
            return {'selection_required': True}
        return None

    async def query_decision(self, event, context, resource, rule_id=None):
        return await self.access().decide(
            event.appid,
            FUNCTION,
            context['group'],
            context['member'],
            resource,
            action='invoke' if rule_id else 'view',
            framework_allowed=self.framework_allowed(event) and await self.framework_context_allowed(event.appid, context['group'], context['member']),
        )

    def current_rules(self, bot, guard):
        if guard is None:
            return True
        module, version = guard
        return self.qa_provider() is module and module.active and module.version(bot, FUNCTION) == version

    async def query(self, event, resource, *, rule_id=None, qa_guard=None):
        context = await self.resolve_context(event)
        if context is None:
            await self.deny(event)
            return
        if context.get('selection_required'):
            return
        bot, group, member = event.appid, context['group'], context['member']
        decision = await self.query_decision(event, context, resource, rule_id)
        if not decision['allowed'] or not self.current_rules(bot, qa_guard):
            await self.deny(event)
            return
        content = await self.query_content(bot, resource, rule_id)
        if content is None:
            await self.deny(event)
            return
        if event.is_group:
            await event.reply('将尝试私发账户信息，请留意本 Bot 私聊；发送结果以实际状态为准。')
        binding = await self.identities.lookup(bot, group, member)
        if not binding or (not event.is_group and binding['private_id'] != event.raw_user_id):
            await self.operation(bot, 'delivery', group=group, member=member, resource=resource, delivery='pending_binding')
            await event.reply(f'私发未完成：尚未绑定。请私聊本 Bot：账户绑定 {group} {member}，由主人核对确认。')
            return
        current = await self.query_decision(event, context, resource, rule_id)
        if not current['allowed'] or not self.current_rules(bot, qa_guard):
            await self.deny(event)
            return
        content = await self.query_content(bot, resource, rule_id)
        final = await self.query_decision(event, context, resource, rule_id)
        if not final['allowed'] or final['version'] != current['version'] or content is None or not self.active or not self.current_rules(bot, qa_guard):
            await self.deny(event)
            return
        try:
            response = await event._sender.send_to_user(binding['private_id'], content)
            ok, data = response[:2]
            code = str(data.get('code', 'send_failed'))[:80] if isinstance(data, dict) else 'send_failed'
        except Exception:
            ok, code = False, 'network_error'
        await self.operation(bot, 'delivery', group=group, member=member, resource=resource, delivery='sent' if ok else 'failed', error='' if ok else code)
        if not ok:
            await event.reply(f'私发失败（{code}），请检查好友关系、接收许可或重新提交账户绑定申请；账密未公开。')
        elif event.is_group:
            await event.reply('账户信息已私发。')

    async def framework_context_allowed(self, bot, group, member):
        subject = await self.access().subject_identity(bot, group, member)
        aliases = subject.get('union_ids', [])
        users = get_blacklist_map(bot, 'user')
        union_enabled = cfg.get_bot_setting(bot, 'identity.use_union_id_for_group', False)
        if union_enabled and users and not subject.get('identity_observed'):
            return False  # Cannot check a configured display-identity deny without a reliable relation.
        if any(alias in users for alias in aliases):
            return False
        display_id = subject.get('current_union_id', member) if union_enabled else member
        return self.framework_subject_allowed(bot, group, display_id, member)

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
                values['framework_allowed'] = await self.framework_context_allowed(bot, values.get('group', ''), values.get('member', ''))
            if operation == 'grant':
                for resource in values.get('resources', []):
                    if not await self.store.get(bot, 'accounts', resource):
                        raise BusinessError('账户不存在', 404)
            result = await self.access().execute(bot, operation, **values)
            if operation == 'grant':
                result['operation_result'] = await self.grant_result(bot, result)
            return result
        except BusinessError:
            raise
        except (TypeError, ValueError) as error:
            raise BusinessError(str(error) if isinstance(error, ValueError) else '授权参数不完整') from error

    async def directory(self, event):
        context = await self.resolve_context(event)
        if not context:
            await self.deny(event)
            return
        if context.get('selection_required'):
            return
        module = self.access()
        allowed = []
        for account in await self.accounts(event.appid):
            decision = await module.decide(
                event.appid,
                FUNCTION,
                context['group'],
                context['member'],
                account['id'],
                framework_allowed=await self.framework_context_allowed(event.appid, context['group'], context['member']),
            )
            if account['enabled'] and decision['allowed']:
                allowed.append(account)
        if not allowed:
            await self.deny(event)
            return
        # Re-evaluate after async reads, so a known mutation cannot leak an old list.
        visible = []
        for account in allowed:
            if (
                await module.decide(
                    event.appid,
                    FUNCTION,
                    context['group'],
                    context['member'],
                    account['id'],
                    framework_allowed=await self.framework_context_allowed(event.appid, context['group'], context['member']),
                )
            )['allowed']:
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
            if parts[0] == 'rules' and len(parts) <= 2:
                key = parts[1] if len(parts) == 2 else ''
                operation = {'GET': 'list', 'POST': 'create', 'PATCH': 'update', 'DELETE': 'delete'}.get(request.method)
                if not operation or bool(key) != (operation in {'update', 'delete'}):
                    raise BusinessError('方法不适用', 405)
                data = await request.json() if operation in {'create', 'update'} else None
                result = await self.rule_management(bot, operation, data, key)
                return web.json_response({'success': True, 'data': result}, status=201 if operation == 'create' else 200)
            if parts == ['operations'] and request.method == 'GET':
                return web.json_response({'success': True, 'data': await self.store.all(bot, 'operations')})
            if parts == ['bindings']:
                if request.method == 'GET':
                    result = {'requests': await self.store.all(bot, 'binding_requests'), 'bindings': await self.store.all(bot, 'bindings')}
                elif request.method == 'POST':
                    result = await self.binding_management(bot, await request.json())
                else:
                    raise BusinessError('方法不适用', 405)
                return web.json_response({'success': True, 'data': result})
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
        content = (event.content or '').strip()
        if content == '账户列表' or content.startswith(('账户查询 ', '账户群 ', '账户绑定 ', '账户答复 ')):
            try:
                if content == '账户列表':
                    await self.directory(event)
                elif content.startswith('账户查询 '):
                    await self.query(event, content.split(maxsplit=1)[1])
                elif content.startswith('账户答复 '):
                    await self.query(event, '', rule_id=content.split(maxsplit=1)[1])
                elif content.startswith('账户群 '):
                    if await self.resolve_context(event, select=content.split(maxsplit=1)[1]):
                        await self.directory(event)
                    else:
                        await self.deny(event)
                else:
                    if not event.is_direct or event.is_channel or not self.framework_allowed(event):
                        await self.deny(event)
                        return
                    words = content.split()
                    if len(words) != 3:
                        raise BusinessError('请使用：账户绑定 <群原生ID> <成员原生OpenID>')
                    request = await self.identities.request(event.appid, event.raw_user_id, words[1], words[2])
                    await event.reply(f'绑定申请 {request["id"]} 已保存，等待主人核对群成员与本次私聊身份。申请本身不会授予资格。')
            except PermissionError:
                await self.deny(event)
            except BusinessError as error:
                if error.status == 503:
                    await self.deny(event)
                else:
                    await event.reply(str(error))
            except (ValueError, RuntimeError, sqlite3.Error, OSError):
                await self.deny(event)
            return
        if not (event.content or '').startswith('账户管理'):
            try:
                await self.keyword_query(event)
            except (BusinessError, RuntimeError, ValueError, sqlite3.Error, OSError):
                await self.deny(event)
            return
        bot = event.appid
        if not self.framework_allowed(event) or event.user_id not in (cfg.get_bot_config(bot) or {}).get('owner_ids', []):
            await self.deny(event)
            return
        try:
            if event.is_direct and not event.is_channel:
                await self.store.put(
                    bot, 'owners', 'observed_c2c', {'private_id': event.raw_user_id, 'authenticated_id': event.user_id, 'source': 'observed_owner_c2c'}
                )
            words = event.content.split(maxsplit=2)
            action = words[1] if len(words) > 1 else '帮助'
            args = words[2] if len(words) > 2 else ''
            if action in {'规则新增', '规则修改', '规则删除', '规则列表'}:
                if action == '规则新增':
                    result = await self.rule_management(bot, 'create', json.loads(args))
                elif action == '规则修改':
                    key, payload = args.split(maxsplit=1)
                    result = await self.rule_management(bot, 'update', json.loads(payload), key)
                elif action == '规则删除':
                    result = await self.rule_management(bot, 'delete', key=args)
                else:
                    result = await self.rule_management(bot, 'list')
                await event.reply(json.dumps(result, ensure_ascii=False) if event.is_direct else '规则操作已完成，详情请在主人私聊或后台查看')
                return
            if action == '确认绑定':
                data = json.loads(args)
                if isinstance(data, dict):
                    data.setdefault('operation', 'confirm')
                result = await self.binding_management(bot, data, actor=event.raw_user_id)
                await event.reply('绑定已确认，可继续私聊查询' if event.is_group else json.dumps(result, ensure_ascii=False))
                return
            if action == '结果':
                results = await self.store.all(bot, 'operations')
                await event.reply(json.dumps(results, ensure_ascii=False) if event.is_direct else '请在主人私聊或后台查看结果')
                return
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
