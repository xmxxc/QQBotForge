"""Account business on top of the group-state, authorization and Q&A modules.

The plugin owns accounts, account rules (Q&A namespace ``account_sharing``),
delivery intent and owner results. Eligibility, private identities and group
facts come from the authorization module (consumer ``account_sharing``), which
reads group state; keywords come from the Q&A matcher. The plugin never reads
platform rosters, binding internals or authorization versions.
"""

import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace
from uuid import uuid4

from aiohttp import web

from core.base.config import cfg
from core.base.logger import PLUGIN, get_logger
from core.plugin._blacklist import get_blacklist_map

from .group_delivery import ACCOUNT_CHANGED, GroupDelivery
from .menus import MenuSessions
from .storage import ExtensionStore

log = get_logger(PLUGIN, '账户共享')
PREFIX = '/api/ext/account-sharing/'
UNAVAILABLE = '账户共享存储暂不可用，请联系主人检查后台日志'
DENIED = '你暂无此功能的使用权限，请联系主人'
FUNCTION = 'account_sharing'
CONSUMER = 'account_sharing'
OWNER = 'plugin:account_sharing'
DEFAULT_POLICY = {'kind': 'explicit', 'groups': [], 'private': True}
BIND_HINT = '身份绑定 申请 {group} {member}'
# Owner commands → scoped authorization operations (function = account_sharing).
ACCESS_COMMANDS = {
    '白名单': 'whitelist',
    '授权': 'grant',
    '撤权': 'revoke',
    '拉黑': 'deny',
    '解除拉黑': 'allow',
    '解释': 'explain',
    '功能组': 'function_group',
    '批量授权': 'bulk_grant',
    '批量撤权': 'bulk_revoke',
    '期限': 'expiry',
    '批量期限': 'bulk_expiry',
    '恢复来源': 'adopt_legacy',
}
MOVED = {
    'members': '成员事实请在“群状态”查看',
    'verify': '成员核验请使用群状态：群状态 核验／群状态 确认',
    'bulk_verify': '成员核验请使用群状态：群状态 核验／群状态 确认',
    'verify_auto': '成员核验请使用群状态：群状态 核验',
    'roster_sync': '完整名册请使用群状态：群状态 名册',
}
ACCOUNT_FIELDS = {'name', 'login', 'password', 'enabled', 'description', 'url', 'groups'}


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
        if key == 'groups':
            if not isinstance(value, list) or len(value) > 50 or any(not isinstance(g, str) or not g.strip() or len(g) > 100 for g in value):
                raise BusinessError('账户分组须为最多 50 个非空名称，名称最多 100 字符')
        elif key == 'enabled':
            if not isinstance(value, bool):
                raise BusinessError('启用状态须为布尔值')
        elif not isinstance(value, str) or len(value) > 4000 or (key in {'name', 'login', 'password'} and not value.strip()):
            raise BusinessError('账户字段须为非空文字（最多 4000 字符）')
    return data


class AccountSharing:
    CONSUMER = CONSUMER
    FUNCTION = FUNCTION

    def __init__(self, path, *, access_provider=None, sender_provider=None, qa_provider=None, clock=time.time):
        self.store = ExtensionStore(path)
        self.lock = asyncio.Lock()
        self.active = True
        self.access_provider = access_provider or self._module('access_control')
        self.qa_provider = qa_provider or self._module('self_qa')
        self.sender_provider = sender_provider or self._bot_sender
        self.denial_times = {}
        self.notification_locks = {}
        self.declared = {}
        self.inventory_version = {}
        self.context_versions = {}
        self.account_versions = {}
        self.account_writes = set()
        self.clock = clock
        self.menus = MenuSessions(self.store, lambda: self.clock())
        self.group_delivery = GroupDelivery(self)

    # ---------- providers ----------

    @staticmethod
    def _module(name):
        def provider():
            from core.application import get_app

            app = get_app()
            return app.module_manager.get(name) if app and app.module_manager else None

        return provider

    def access(self):
        module = self.access_provider()
        if module is None or not module.active:
            raise BusinessError('黑白名单授权模块未启用，查询暂停，请联系主人', 503)
        return module

    def qa(self):
        module = self.qa_provider()
        if module is None or not module.active:
            raise BusinessError('自助问答模块未启用，关键词查询暂停', 503)
        return module

    @staticmethod
    def _bot_sender(bot):
        from core.application import get_app

        app = get_app()
        instance = app.get_bot(bot) if app else None
        return instance.sender if instance else None

    async def ensure(self, bot):
        """Register the consumer and the current account inventory with authorization."""
        acl = self.access()
        key = (id(acl), acl.generation, self.inventory_version.get(bot, 0))
        if self.declared.get(bot) != key:
            await acl.declare(bot, CONSUMER, owner=OWNER, functions={FUNCTION: DEFAULT_POLICY})
            accounts = await self.accounts(bot)
            await acl.declare_resources(bot, CONSUMER, [{'id': a['id'], 'groups': a.get('groups', []), 'enabled': a['enabled']} for a in accounts])
            self.declared[bot] = key
        return acl

    def accounts_changed(self, bot, resource):
        self.inventory_version[bot] = self.inventory_version.get(bot, 0) + 1
        self.account_versions[bot, resource] = self.account_version(bot, resource) + 1

    def account_version(self, bot, resource):
        """Bumped after each save or delete of this account; sends compare it after their last await."""
        return self.account_versions.get((bot, resource), 0)

    def account_unchanged(self, bot, resource, version):
        # A write in flight may already be committed before its version bump.
        return (bot, resource) not in self.account_writes and self.account_version(bot, resource) == version

    # ---------- reply results ----------

    @staticmethod
    def reply_outcome(result):
        """Sender.reply returns the platform response dict; pushes return (ok, data, ...)."""
        if isinstance(result, tuple) and result and isinstance(result[0], bool):
            data = result[1] if len(result) > 1 else None
            if result[0]:
                return 'sent'
            return 'unknown' if isinstance(data, dict) and str(data.get('code')) == '-1' else 'failed'
        if not isinstance(result, dict):
            return 'unknown'
        code = result.get('code')
        if code in (None, 0):
            return 'sent'
        return 'unknown' if str(code) == '-1' else 'failed'

    async def safe_reply(self, event, content, **kwargs):
        sender = self.sender_provider(event.appid)
        try:
            return await sender.reply(event, content, **kwargs) if sender else None
        except Exception:  # noqa: BLE001 - transport failures leave the result unknown
            return {'code': -1}

    # ---------- accounts ----------

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
            cleanup = await self.store.get(bot, 'account_cleanup', key) if operation == 'delete' else None
            if operation in {'update', 'delete', 'preview'} and not old and not cleanup:
                raise BusinessError('账户不存在', 404)
            if operation == 'preview':
                if not old['enabled']:
                    raise BusinessError('账户已停用', 409)
                return old
            if operation == 'delete':
                # Journal first, remove the account before cross-module cleanup. A
                # failed cleanup stays inaccessible and DELETE of this ID retries it.
                await self.store.put(bot, 'account_cleanup', key, {'id': key})
                self.accounts_changed(bot, key)
                self.account_writes.add((bot, key))
                try:
                    await self.store.delete(bot, 'accounts', key)
                finally:
                    self.account_writes.discard((bot, key))
                    self.accounts_changed(bot, key)
                try:
                    await self.cleanup_account(bot, key)
                except (BusinessError, RuntimeError, ValueError, sqlite3.Error, OSError) as error:
                    raise BusinessError(f'账户 {key} 已删除，关联清理未完成；请重试删除此 ID', 503) from error
                await self.store.delete(bot, 'account_cleanup', key)
                log.info(f'[{bot}] 删除账户 {key}')
                return {'id': key}
            if operation not in {'create', 'update'}:
                raise BusinessError('未知账户操作')
            validate_account(data, create=operation == 'create')
            account = old or {'id': uuid4().hex, 'enabled': True, 'description': '', 'url': '', 'groups': []}
            account.update(data)
            self.account_writes.add((bot, account['id']))
            try:
                await self.store.put(bot, 'accounts', account['id'], account)
            finally:
                self.account_writes.discard((bot, account['id']))
                self.accounts_changed(bot, account['id'])
            await self._sync_resources(bot)
            log.info(f'[{bot}] 保存账户 {account["id"]}')
            return account

    async def cleanup_account(self, bot, resource):
        qa = self.qa()
        for rule in await qa.manage(bot, FUNCTION, 'list'):
            if rule['kind'] == 'resource' and rule['target'] == resource:
                await qa.manage(bot, FUNCTION, 'delete', key=rule['id'])
        for menu in await self.store.all(bot, 'menus'):
            if any(a.get('resource') == resource for a in menu['actions'].values()):
                await self.store.delete(bot, 'menus', menu['id'])
        for mode in await self.store.all(bot, 'group_delivery'):
            if mode['resource'] == resource:
                await self.store.delete(bot, 'group_delivery', mode['id'])
                self.group_delivery.modes.pop((bot, mode['id']), None)
                self.group_delivery.reports.pop((bot, mode['id']), None)
        await self.ensure(bot)

    async def _sync_resources(self, bot):
        """Deleting an account removes it as a resource: its direct grants lose it, a new ID inherits nothing."""
        module = self.access_provider()
        if module is not None and module.active:
            try:
                await self.ensure(bot)
            except (RuntimeError, ValueError, sqlite3.Error, OSError) as error:
                log.warning(f'[{bot}] 账户资源同步暂不可用: {error}')

    async def account_batches(self, bot, data):
        if not isinstance(data, dict) or set(data) - {'operation', 'ids', 'data'} or data.get('operation') not in {'update', 'delete'}:
            raise BusinessError('账户批量操作须选择 update/delete、ids 及修改字段 data')
        ids = data.get('ids')
        if not isinstance(ids, list) or not 1 <= len(ids) <= 500 or any(not isinstance(key, str) or not key for key in ids):
            raise BusinessError('请选择 1 至 500 个账户 ID')
        if data['operation'] == 'update':
            validate_account(data.get('data'))
        items = []
        for key in dict.fromkeys(ids):
            try:
                await self.manage(bot, data['operation'], data.get('data'), key)
                items.append({'id': key, 'status': 'saved'})
            except BusinessError as error:
                items.append({'id': key, 'status': 'failed', 'error': str(error)})
        success = sum(i['status'] == 'saved' for i in items)
        return {'items': items, 'summary': {'success': success, 'failed': len(items) - success}}

    @staticmethod
    def credentials(account):
        return f'{account["name"]}\n账号：{account["login"]}\n密码：{account["password"]}\n{account["description"]}\n{account["url"]}'

    async def query_content(self, bot, resource, rule_id):
        if rule_id:
            rule = await self.qa().manage(bot, FUNCTION, 'get', key=rule_id)
            return rule['target'] if rule and rule['enabled'] and rule['kind'] == 'text' else None
        account = await self.store.get(bot, 'accounts', resource)
        return self.credentials(account) if account and account['enabled'] else None

    # ---------- rules (Q&A namespace account_sharing) ----------

    async def rule_management(self, bot, operation, data=None, key=''):
        async with self.lock:
            return await self._rule_management(bot, operation, data, key)

    async def _rule_management(self, bot, operation, data=None, key=''):
        try:
            if operation in {'create', 'update'} and isinstance(data, dict):
                old = await self.qa().manage(bot, FUNCTION, 'get', key=key) if key else None
                merged = {**(old or {}), **data}
                # Account rules reference stable account IDs; credentials never go into rules.
                if merged.get('kind', 'resource') == 'resource' and not await self.store.get(bot, 'accounts', merged.get('target', '')):
                    raise BusinessError('规则引用的账户不存在', 404)
            return await self.qa().manage(bot, FUNCTION, operation, data, key)
        except KeyError as error:
            raise BusinessError('规则不存在', 404) from error
        except BusinessError:
            raise
        except ValueError as error:
            raise BusinessError(str(error)) from error

    # ---------- authorization (scoped consumer) ----------

    async def access_management(self, bot, data, *, actor='panel_admin'):
        if not isinstance(data, dict):
            raise BusinessError('授权请求须为对象')
        values = dict(data)
        operation = values.pop('operation', '')
        operation = {'set_expiry': 'expiry'}.get(operation, operation)
        scope = values.pop('scope', None)
        if scope is not None and (operation not in {'deny', 'allow'} or scope not in {'group', 'member'}):
            raise BusinessError('拒绝范围须为 group/member')
        if operation in MOVED:
            raise BusinessError(MOVED[operation])
        values.pop('function', None)
        identity_type = values.pop('identity_type', 'group_member_openid')
        if identity_type != 'group_member_openid':
            raise BusinessError('成员须使用群原生成员 OpenID')
        values.pop('confirmed', None)  # UI acknowledgement never substitutes for group facts.
        if isinstance(values.get('members'), list):
            values['members'] = [
                {k: v for k, v in target.items() if k not in {'identity_type', 'confirmed'}}
                if isinstance(target, dict) and target.get('identity_type', 'group_member_openid') == 'group_member_openid'
                else target
                for target in values['members']
            ]
        values.pop('resource_namespace', None)
        try:
            acl = await self.ensure(bot)
            group, member = values.pop('group', ''), values.pop('member', '')
            if scope == 'group':
                if not group:
                    raise BusinessError('群拒绝须指定群')
                member = ''
            if operation == 'whitelist':
                enabled = values.pop('enabled', None)
                if values or not isinstance(enabled, bool) or not group:
                    raise BusinessError('白名单须提供 group 与 enabled')
                configured = (await acl.access_snapshot(bot, CONSUMER))['functions'][FUNCTION]
                groups = [g for g in configured['groups'] if g != group] + ([group] if enabled else [])
                await acl.manage_access(bot, CONSUMER, 'policy', function=FUNCTION, policy={**configured, 'groups': groups})
                return {'groups': groups}
            if operation == 'explain':
                action = values.pop('action', 'view')
                resource = values.pop('resource', '')
                if values:
                    raise BusinessError('解释只接受 group、member、resource、action')
                if action == 'private':
                    found = await acl.member_context(bot, CONSUMER, FUNCTION, group, member, resource, action='view')
                    return {'allowed': found['context'] is not None, 'reason': found['reason'], 'binding': 'verified' if found['context'] else 'pending'}
                return await acl.evaluate(bot, CONSUMER, FUNCTION, group, member, resource, action=action)
            if operation in {'grant', 'bulk_grant'}:
                resources = values.get('resources', [])
                scope = values.get('resource_scope')
                if isinstance(scope, dict) and scope.get('kind') == 'resources':
                    resources = scope.get('ids')
                if not isinstance(resources, list) or len(resources) > 500:
                    raise BusinessError('资源标识须为列表，最多 500 个')
                for resource in resources:
                    if not isinstance(resource, str) or not await self.store.get(bot, 'accounts', resource):
                        raise BusinessError('账户不存在', 404)
            result = await acl.manage_access(bot, CONSUMER, operation, function=FUNCTION, group=group, member=member, **values)
            if operation == 'grant':
                result['operation_result'] = await self.grant_result(bot, result)
            elif operation in {'bulk_grant', 'bulk_revoke', 'bulk_expiry'}:
                await self.bulk_result(bot, operation, group, result)
            return result
        except BusinessError:
            raise
        except (TypeError, ValueError) as error:
            raise BusinessError(str(error) if isinstance(error, ValueError) else '授权参数不完整') from error

    async def binding_state(self, bot, group, member):
        try:
            snapshot = await self.access().binding_snapshot(bot)
        except (BusinessError, RuntimeError, ValueError):
            return 'unavailable'
        return 'verified' if any(b['group'] == group and b['member'] == member for b in snapshot['bindings']) else 'pending'

    async def grant_result(self, bot, source):
        binding = await self.binding_state(bot, source['group'], source['member'])
        result = await self.operation(
            bot,
            'grant',
            group=source['group'],
            member=source['member'],
            source=source['id'],
            resources=source['resources'],
            authorization='saved',
            binding=binding,
            notification='pending',
            delivery='not_requested',
        )
        if binding == 'pending':
            result['guidance'] = await self.group_guidance(
                bot, source['group'], '授权已保存。需要私聊接收的成员请私聊本 Bot：身份绑定 申请 <群原生ID> <成员原生ID>，再由主人核实。'
            )
        return await self.notify_result(bot, result)

    async def group_guidance(self, bot, group, text):
        sender = self.sender_provider(bot)
        try:
            return self.reply_outcome(await sender.send_to_group(group, text)) if sender else 'unavailable'
        except Exception:  # noqa: BLE001
            return 'failed'

    async def bulk_result(self, bot, operation, group, result):
        pending = False
        for item in result['items']:
            if item['status'] == 'saved' and operation == 'bulk_grant':
                item['binding'] = await self.binding_state(bot, group, item['member'])
                item['delivery'] = 'not_requested'
                pending |= item['binding'] == 'pending'
        summary = result['summary']
        status = 'saved' if not summary['failed'] else 'partial' if summary['success'] else 'failed'
        record = await self.operation(
            bot,
            operation,
            group=group,
            authorization=status,
            binding='pending' if pending else 'not_requested',
            notification='pending',
            delivery='not_requested',
            items=result['items'],
            summary=summary,
        )
        if pending:
            record['guidance'] = await self.group_guidance(
                bot, group, '批量授权已处理。需要私聊接收的成员请私聊本 Bot：身份绑定 申请 <群原生ID> <成员原生ID>，再由主人核实。'
            )
        result['operation_result'] = await self.notify_result(bot, record)
        return result

    async def migrate(self, bot):
        """Owner-run migration of legacy authorization and plugin bindings; never widens access."""
        acl = await self.ensure(bot)
        report = {}

        async def stage(name, run, storage_error):
            # Each stage reports what actually happened; a failed one keeps its own boundary.
            try:
                report[name] = await run()
            except asyncio.CancelledError:
                report[name] = {'status': 'cancelled', 'error': '操作已取消，本阶段是否已写入未知，可重新迁移'}
                raise
            except (ValueError, RuntimeError) as error:
                report[name] = {'status': 'failed', 'error': str(error)}
            except (AttributeError, LookupError, TypeError):
                log.warning(f'[{bot}] migration {name}: malformed legacy data', exc_info=True)
                report[name] = {'status': 'failed', 'error': '旧资料结构无效'}
            except (sqlite3.Error, OSError):
                report[name] = {'status': 'failed', 'error': storage_error}

        async def authorization():
            result = await acl.import_legacy(bot, CONSUMER, FUNCTION)
            changed = any(result[k] for k in ('grants', 'denies', 'revocations', 'policy_groups'))
            return {'status': 'imported' if changed else 'unchanged', **result}

        async def bindings():
            bundle = {name: await self.store.all(bot, name) for name in ('private_identities', 'bindings', 'binding_requests')}
            if not any(bundle.values()):
                return {'status': 'not_run', 'reason': 'no_legacy_records', 'bindings': 0, 'requests': 0}
            return await acl.bindings.import_history(bot, bundle)

        async def rules():
            listed = await self.qa().manage(bot, FUNCTION, 'list')
            accounts = {a['id'] for a in await self.accounts(bot)}
            return {'status': 'checked', 'total': len(listed), 'dangling': [r['id'] for r in listed if r['kind'] == 'resource' and r['target'] not in accounts]}

        async def record(operation_id):
            return await self.operation(
                bot,
                'migration',
                id=operation_id,
                authorization=report['authorization']['status'],
                binding=report['bindings']['status'],
                rules=report['rules']['status'],
                migration=report['status'],
                notification='pending',
                delivery='not_requested',
                report=report,
            )

        stages = (('authorization', authorization, '授权资料写入失败'), ('bindings', bindings, '身份资料写入失败'), ('rules', rules, '规则读取失败'))
        operation_id = uuid4().hex
        try:
            for name, run, storage_error in stages:
                await stage(name, run, storage_error)
        except asyncio.CancelledError as cancel:
            # Earlier stages may have committed: keep a record (retry notification from 待办).
            for name, _, _ in stages:
                report.setdefault(name, {'status': 'not_run', 'reason': 'cancelled'})
            report['status'] = 'cancelled'
            await self.drain_record(bot, operation_id, report, record(operation_id))
            raise cancel
        import_statuses = [report[name]['status'] for name in ('authorization', 'bindings')]
        if 'failed' in [*import_statuses, report['rules']['status']]:
            # The rules stage only checks; success there alone is not a partial import.
            report['status'] = 'partial' if any(s in {'imported', 'unchanged'} for s in import_statuses) else 'failed'
        else:
            report['status'] = 'imported' if 'imported' in import_statuses else 'unchanged'
        write = asyncio.ensure_future(record(operation_id))
        try:
            result = await asyncio.shield(write)
        except asyncio.CancelledError as cancel:
            # Committed stages still get their record before the cancel propagates.
            await self.drain_record(bot, operation_id, report, write)
            raise cancel
        await self.notify_result(bot, result)
        return report

    record_drain_timeout = 10

    async def drain_record(self, bot, operation_id, report, write):
        """Finish a cancelled migration's record without letting it replace the cancel.

        The write runs in its own task; later cancels only interrupt the waits, and
        both waits are bounded. Never raises: the caller re-raises the original cancel.
        """
        task = asyncio.ensure_future(write)  # A task already running is drained as is.
        await self.wait_through_cancel(task, self.record_drain_timeout)
        if not task.done():
            task.cancel()
            await self.wait_through_cancel(task, self.record_drain_timeout)
        if task.done() and not task.cancelled() and task.exception() is None:
            return
        # SQL already handed to the store thread may still commit after an abandoned wait.
        outcome = 'outcome unknown' if not task.done() or task.cancelled() else f'not saved ({type(task.exception()).__name__})'
        statuses = {name: report[name]['status'] for name in ('authorization', 'bindings', 'rules')}
        log.error(f'[{bot}] cancelled migration operation={operation_id} record {outcome}; stages={statuses}')

    @staticmethod
    async def wait_through_cancel(task, timeout):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not task.done() and deadline > loop.time():
            try:
                await asyncio.wait({task}, timeout=deadline - loop.time())
            except asyncio.CancelledError:
                continue

    # ---------- owner results ----------

    async def operation(self, bot, kind, **status):
        result = {'id': uuid4().hex, 'kind': kind, 'created_at': time.time(), **status}
        await self.store.put(bot, 'operations', result['id'], result)
        log.info(f'[{bot}] {kind} operation={result["id"]}')
        return result

    async def notify_result(self, bot, result):
        async with self.notification_locks.setdefault((bot, result['id']), asyncio.Lock()):
            saved = await self.store.get(bot, 'operations', result['id'])
            if saved and saved.get('notification') == 'sent':
                return saved
            owner = await self.store.get(bot, 'owners', 'observed_c2c')
            sender = self.sender_provider(bot)
            owner_ids = (cfg.get_bot_config(bot) or {}).get('owner_ids', [])
            if not owner or sender is None or owner.get('authenticated_id') not in owner_ids or owner['private_id'] in get_blacklist_map(bot, 'user'):
                result['notification'] = 'unavailable'
            else:
                try:
                    response = await sender.send_to_user(
                        owner['private_id'],
                        f'账户共享操作 {result["id"]}：{result["kind"]}；授权={result.get("authorization", "未变更")}，绑定={result.get("binding", "未变更")}，'
                        f'模式={result.get("mode", "未变更")}，发送={result.get("delivery", "未请求")}；详情请在后台查看',
                    )
                    result['notification'] = 'sent' if self.reply_outcome(response) == 'sent' else 'failed'
                except Exception:  # noqa: BLE001
                    result['notification'] = 'failed'
            await self.store.put(bot, 'operations', result['id'], result)
            return result

    async def tasks(self, bot, filters=None):
        """Owner to-dos across modules; each names the page or command that resolves it."""
        filters = filters or {}
        if (
            not isinstance(filters, dict)
            or set(filters) - {'group', 'member', 'resource'}
            or any(not isinstance(v, str) or len(v) > 256 for v in filters.values())
        ):
            raise BusinessError('待办筛选只接受 group、member、resource 文字')
        acl = await self.ensure(bot)
        snapshot = await acl.access_snapshot(bot, CONSUMER)
        identities = await acl.binding_snapshot(bot)
        bound = {(b['group'], b['member']) for b in identities['bindings']}
        facts = acl.group_state()
        tasks = []
        for grant in snapshot['grants']:
            base = {'group': grant['group'], 'member': grant['member'], 'resources': grant['resources'], 'source': grant['id']}
            if grant['cycle'].startswith('legacy:'):
                tasks.append(
                    {
                        **base,
                        'type': 'legacy_adoption',
                        'entry': 'group-state → account-sharing',
                        'action': f'群状态核验成员后：账户管理 恢复来源 {{"group":"{grant["group"]}","member":"{grant["member"]}"}}',
                    }
                )
                continue
            fact = await facts.fact(bot, grant['group'], grant['member']) if facts else None
            if not fact or not fact['usable'] or (fact['member'] or {}).get('status') != 'present':
                tasks.append(
                    {**base, 'type': 'verification', 'entry': 'group-state', 'action': f'群状态 核验 {grant["group"]} {grant["member"]}（或后台“群状态”）'}
                )
            if (grant['group'], grant['member']) not in bound:
                tasks.append({**base, 'type': 'binding', 'entry': 'access-control', 'action': '成员私聊“身份绑定 申请”后在“授权管理”确认'})
        for request in identities['requests']:
            if request['status'] == 'pending':
                tasks.append({**request, 'type': 'binding_request', 'entry': 'access-control', 'action': '在“授权管理”确认或拒绝身份绑定'})
        for mode in await self.group_delivery.list(bot):
            if mode['desired'] == 'group' and mode['actual'] != 'group':
                tasks.append({**mode, 'type': 'group_delivery', 'entry': 'account-sharing', 'action': '查看逐人原因后重新开启群发'})
        for result in await self.store.all(bot, 'operations'):
            if result.get('notification') in {'failed', 'unavailable', 'pending'}:
                tasks.append({**result, 'type': 'notification', 'operation_id': result['id'], 'entry': 'account-sharing', 'action': '重试主人通知'})
        return [
            t
            for t in tasks
            if all(not value or t.get(key) == value or (key == 'resource' and value in t.get('resources', [])) for key, value in filters.items())
        ]

    async def task_action(self, bot, data):
        if not isinstance(data, dict) or set(data) != {'operation', 'id'} or data['operation'] != 'retry_notification' or not isinstance(data['id'], str):
            raise BusinessError('请提供 retry_notification 和操作 id')
        result = await self.store.get(bot, 'operations', data['id'])
        if not result:
            raise BusinessError('操作不存在', 404)
        return await self.notify_result(bot, result)

    # ---------- member queries ----------

    async def decision(self, bot, context, resource, action):
        acl = await self.ensure(bot)
        decision = await acl.evaluate(bot, CONSUMER, FUNCTION, context['group'], context['member'], resource, action=action)
        if decision['allowed'] and decision['reason'] != 'explicit':
            # Group admission or roles never replace an explicit account grant.
            decision = {**decision, 'allowed': False, 'reason': 'explicit_required'}
        return decision

    async def rule_current(self, bot, via_rule):
        """A keyword-triggered query still matches the same enabled rule revision."""
        if via_rule is None:
            return True
        rule_id, revision = via_rule
        try:
            rule = await self.qa().manage(bot, FUNCTION, 'get', key=rule_id)
        except (BusinessError, RuntimeError):
            return False
        return bool(rule and rule['enabled'] and rule.get('revision', 0) == revision)

    async def account_contexts(self, bot, private_id, *, resource=''):
        """One current resource-view context per group that can actually query an account."""
        acl = await self.ensure(bot)
        inventory = self.inventory_version.get(bot, 0)
        candidates = []
        for account in await self.accounts(bot):
            if not account['enabled'] or resource and account['id'] != resource:
                continue
            found = await acl.private_contexts(bot, CONSUMER, FUNCTION, private_id, resource=account['id'], action='view')
            for context in found['candidates']:
                # A view decision includes the required invocation grant as well.
                if context['decision']['reason'] == 'explicit':
                    candidates.append(context)
        if not self.active or self.access_provider() is not acl or inventory != self.inventory_version.get(bot, 0):
            return []
        current = {}
        for context in candidates:
            if acl.context_current(bot, context):
                current.setdefault(context['group'], context)
        return list(current.values())

    async def resolve_context(self, event, *, select='', page=0, resource=''):
        """Group: the asker's native member identity. Private: an authorization-verified group context."""
        if event.is_channel or not (event.is_group or event.is_direct):
            return None
        if event.is_group:
            return {'group': event.group_id, 'member': event.raw_user_id}
        chosen = select or ((await self.store.get(event.appid, 'contexts', event.raw_user_id)) or {}).get('group', '')
        candidates = await self.account_contexts(event.appid, event.raw_user_id, resource=resource)
        selected = next((c for c in candidates if c['group'] == chosen), None)
        if not selected and not select and len(candidates) == 1:
            selected = candidates[0]
        if selected:
            if select:
                key = (event.appid, event.raw_user_id)
                self.context_versions[key] = self.context_versions.get(key, 0) + 1
                await self.store.put(event.appid, 'contexts', event.raw_user_id, {'group': select})
            return {'group': selected['group'], 'member': selected['member'], 'private': True}
        if candidates and not select:
            await self.show_groups(event, candidates, page=page, resource=resource)
            return {'selection_required': True}
        return None

    async def show_groups(self, event, candidates, page=0, *, resource=''):
        items = [(c['group'], {'kind': 'group', 'group': c['group']}) for c in candidates]
        if not items:
            await self.deny(event)
            return
        await self.present_menu(
            event, {'menu': 'groups', 'candidates': [c['group'] for c in candidates], 'resource': resource}, items, page=page, title='请选择授权群'
        )

    async def keyword_query(self, event, *, text=None, page=0, expected_context=None):
        selection_version = self.context_versions.get((event.appid, event.raw_user_id), 0)
        module = self.qa_provider()
        if module is None or not module.active:
            return
        inventory = self.inventory_version.get(event.appid, 0)
        rules_version = module.version(event.appid, FUNCTION)
        match_text = event.content or '' if text is None else text

        # Cheap pre-check: no account rule matches at all → not ours.
        async def live_rule(rule):
            return rule['kind'] != 'resource' or bool(await self.store.get(event.appid, 'accounts', rule['target']))

        if not await module.match(event.appid, FUNCTION, match_text, group=event.group_id if event.is_group else None, admit=live_rule):
            return
        context = await self.resolve_context(event)
        if expected_context and context and any(context.get(k) != expected_context.get(k) for k in ('group', 'member')):
            await self.safe_reply(event, '菜单已失效，请重新发送：账户列表')
            return
        if not context:
            await self.deny(event)
            return
        if context.get('selection_required'):
            return

        if not await module.match(event.appid, FUNCTION, match_text, group=context['group'], admit=live_rule):
            return

        async def admit(rule):
            if rule['kind'] == 'resource':
                account = await self.store.get(event.appid, 'accounts', rule['target'])
                if not account or not account['enabled']:
                    return False
                return (await self.decision(event.appid, context, rule['target'], 'view'))['allowed']
            return (await self.decision(event.appid, context, '', 'invoke'))['allowed']

        matches = await module.match(event.appid, FUNCTION, match_text, group=context['group'], admit=admit)
        selected = {}
        for match in module.best(matches):
            if match['kind'] == 'resource':
                match['name'] = (await self.store.get(event.appid, 'accounts', match['target']) or {}).get('name', match['name'])
            selected.setdefault((match['kind'], match['target']), match)
        candidates = list(selected.values())
        if not candidates:
            await self.deny(event)
        elif len(candidates) == 1:
            match = candidates[0]
            await self.query(
                event,
                match['target'] if match['kind'] == 'resource' else '',
                rule_id=match['rule_id'] if match['kind'] == 'text' else None,
                via_rule=(match['rule_id'], match['revision']),
                expected_context=context,
            )
        else:
            items = [
                (m['name'], {'kind': 'query', 'resource': m['target'], 'via': [m['rule_id'], m['revision']]})
                if m['kind'] == 'resource'
                else (m['name'], {'kind': 'reply', 'rule': m['rule_id'], 'via': [m['rule_id'], m['revision']]})
                for m in candidates
            ]
            await self.present_menu(
                event,
                {
                    **context,
                    'menu': 'candidates',
                    'keyword': match_text,
                    'selection_version': selection_version,
                    'inventory': inventory,
                    'rules_version': rules_version,
                },
                items,
                page=page,
                title='请选择有权候选',
            )

    async def directory(self, event, *, page=0, expected_context=None):
        selection_version = self.context_versions.get((event.appid, event.raw_user_id), 0)
        inventory = self.inventory_version.get(event.appid, 0)
        rules_version = self.qa().version(event.appid, FUNCTION)
        context = await self.resolve_context(event)
        if expected_context and context and any(context.get(k) != expected_context.get(k) for k in ('group', 'member')):
            await self.safe_reply(event, '菜单已失效，请重新发送：账户列表')
            return
        if not context:
            await self.deny(event)
            return
        if context.get('selection_required'):
            return
        visible = []
        for account in await self.accounts(event.appid):
            if account['enabled'] and (await self.decision(event.appid, context, account['id'], 'view'))['allowed']:
                visible.append((account['name'], {'kind': 'query', 'resource': account['id']}))
        if not visible:
            await self.deny(event)
            return
        await self.present_menu(
            event,
            {**context, 'menu': 'accounts', 'selection_version': selection_version, 'inventory': inventory, 'rules_version': rules_version},
            visible,
            page=page,
        )

    async def present_menu(self, event, context, items, *, page=0, title='账户列表'):
        bot = event.appid
        selection_version = context.get('selection_version', self.context_versions.get((bot, event.raw_user_id), 0))
        acl = await self.ensure(bot)
        inventory = context.get('inventory', self.inventory_version.get(bot, 0))
        qa = self.qa()
        rules_version = context.get('rules_version', qa.version(bot, FUNCTION))
        target = None
        menu_event = event
        if event.is_group:
            # A caller's qualification never proves qualification of bystanders.
            await self.safe_reply(event, '将尝试私发个人目录，请留意本 Bot 私聊。')
            found = await acl.member_context(bot, CONSUMER, FUNCTION, context['group'], context['member'], action='invoke')
            target = found['context']
            if target is None:
                await self.safe_reply(event, '目录私发未完成，请先确认私聊身份绑定。')
                return
            menu_event = SimpleNamespace(appid=bot, raw_user_id=target['private_id'], is_group=False, group_id='')
        size = 6
        last = max((len(items) - 1) // size, 0)
        page = max(0, min(page, last))
        selected = list(items[page * size : (page + 1) * size])
        lines = []
        for label, action in selected:
            command = {
                'query': '账户查询 ' + action.get('resource', ''),
                'reply': '账户答复 ' + action.get('rule', ''),
                'group': '账户群 ' + action.get('group', ''),
            }.get(action['kind'], '')
            lines.append(label + ' — ' + command)
        if page > 0:
            selected.append(('上一页', {'kind': 'page', 'page': page - 1}))
        if page < last:
            selected.append(('下一页', {'kind': 'page', 'page': page + 1}))
        text = title + f'（{page + 1}/{last + 1}）\n' + '\n'.join(lines)
        if context['menu'] == 'accounts':
            text += f'\n文字翻页：账户列表 {page + 2}' if page < last else '\n发送“账户列表”刷新当前资格'
        rows = await self.menus.issue(menu_event, context, selected)
        for row in rows:
            for button in row:
                if button['text'] in {'上一页', '下一页'}:
                    text += f'\n{button["text"]}：账户翻页 {button["data"]}'
        # Refresh after menu persistence and all identity lookups. The synchronous
        # gate below checks every decision after the last preparatory await.
        if context['menu'] == 'groups':
            contexts = await self.account_contexts(bot, event.raw_user_id, resource=context.get('resource', ''))
            if [c['group'] for c in contexts] != context['candidates']:
                await self.safe_reply(event, '菜单已失效，请重新发送：账户列表')
                return
            decisions = []
        else:
            if event.is_group:
                found = await acl.member_context(bot, CONSUMER, FUNCTION, context['group'], context['member'], action='invoke')
                fresh = found['context']
                contexts = [fresh] if fresh and fresh['private_id'] == menu_event.raw_user_id else []
            else:
                found = await acl.private_contexts(bot, CONSUMER, FUNCTION, event.raw_user_id, group=context['group'], action='invoke')
                contexts = [found['selected']] if found['selected'] else []
            decisions = [
                await self.decision(bot, context, action.get('resource', ''), 'view' if action['kind'] == 'query' else 'invoke') for _, action in items
            ]

        def current():
            return (
                self.active
                and not any(b == bot for b, _ in self.account_writes)
                and self.access_provider() is acl
                and qa.active
                and self.qa_provider() is qa
                and selection_version == self.context_versions.get((bot, event.raw_user_id), 0)
                and inventory == self.inventory_version.get(bot, 0)
                and rules_version == qa.version(bot, FUNCTION)
                and bool(contexts)
                and all(acl.context_current(bot, c) for c in contexts)
                and all(acl.decision_current(bot, d) for d in decisions)
            )

        if not current():
            await self.safe_reply(event, '菜单已失效，请重新发送：账户列表')
            return
        sender = self.sender_provider(bot)

        async def send(content, **kwargs):
            try:
                return (
                    await sender.send_to_user(menu_event.raw_user_id, content, **kwargs) if event.is_group else await self.safe_reply(event, content, **kwargs)
                )
            except Exception:  # noqa: BLE001 - a push failure never falls back to the group
                return {'code': -1}

        if cfg.get_bot_setting(bot, 'message.use_markdown', True):
            outcome = self.reply_outcome(await send(text, buttons=rows))
            if outcome != 'failed':
                if event.is_group and outcome == 'unknown':
                    await self.safe_reply(event, '目录私发结果未知，请先查看本 Bot 私聊，核对接收许可或身份绑定后再重试。')
                return
        # A rejected platform send is another await; do not fall back with stale text.
        if current():
            outcome = self.reply_outcome(await send(text + '\n按钮暂不可用，请使用以上文字命令。'))
            if event.is_group and outcome != 'sent':
                await self.safe_reply(event, '目录私发未完成，请检查私聊接收许可或身份绑定。')

    async def query(self, event, resource, *, rule_id=None, expected_context=None, via_rule=None):
        context_version = self.context_versions.get((event.appid, event.raw_user_id), 0)
        context = await self.resolve_context(event, resource=resource)
        if expected_context and context and any(context.get(k) != expected_context.get(k) for k in ('group', 'member')):
            await self.safe_reply(event, '菜单已失效，请重新发送：账户列表')
            return
        if context is None:
            await self.deny(event)
            return
        if context.get('selection_required'):
            return
        bot, group, member = event.appid, context['group'], context['member']
        action = 'invoke' if rule_id else 'view'
        decision = await self.decision(bot, context, resource, action)
        if not decision['allowed']:
            await self.deny(event)
            return
        if event.is_group and resource and await self.group_delivery.try_send(event, context, resource, via_rule):
            return
        acl = self.access()
        if event.is_group:
            await self.safe_reply(event, '将尝试私发账户信息，请留意本 Bot 私聊；发送结果以实际状态为准。')
            found = await acl.member_context(bot, CONSUMER, FUNCTION, group, member, resource, action=action)
            target = found['context']
            if target is None:
                await self.operation(bot, 'delivery', group=group, member=member, resource=resource, delivery='pending_binding', reason=found['reason'])
                await self.safe_reply(event, '私发未完成：尚未确认私聊身份。请私聊本 Bot：' + BIND_HINT.format(group=group, member=member) + '，由主人核实。')
                return
        else:
            found = await acl.private_contexts(bot, CONSUMER, FUNCTION, event.raw_user_id, group=group, resource=resource, action=action)
            target = found['selected']
            if not target or target['group'] != group:
                await self.deny(event)
                return
        version = self.account_version(bot, resource)
        content = await self.query_content(bot, resource, rule_id)
        if content is None or not await self.rule_current(bot, via_rule):
            await self.deny(event)
            return
        # Final gate right before sending, after the last await: unchanged account content,
        # binding, private denial and an explicit (never admission/role) current decision.
        if (
            not self.active
            or target['decision'].get('reason') != 'explicit'
            or not acl.context_current(bot, target)
            or context_version != self.context_versions.get((bot, event.raw_user_id), 0)
        ):
            await self.deny(event)
            return
        if not self.account_unchanged(bot, resource, version):
            await self.safe_reply(event, ACCOUNT_CHANGED)
            return
        sender = self.sender_provider(bot)
        try:
            result = await sender.send_to_user(target['private_id'], content) if event.is_group else await sender.reply(event, content)
            state = self.reply_outcome(result)
            data = result[1] if isinstance(result, tuple) and len(result) > 1 else result
            code = str(data.get('code', ''))[:80] if isinstance(data, dict) else ''
        except Exception:  # noqa: BLE001
            state, code = 'unknown', 'network_error'
        await self.operation(bot, 'delivery', group=group, member=member, resource=resource, delivery=state, error=code if state != 'sent' else '')
        if event.is_group:
            if state == 'sent':
                await self.safe_reply(event, '账户信息已私发。')
            elif state == 'failed':
                await self.safe_reply(event, f'私发失败（{code or "平台拒绝"}），请检查好友关系、接收许可或身份绑定；账密未公开。')
            else:
                await self.safe_reply(event, '私发结果未知，请先查看私聊，勿重复索取；账密未公开。')

    async def deny(self, event):
        key = (event.appid, event.group_id, event.raw_user_id)
        now = time.monotonic()
        # Bound retained rate-limit entries without exposing denial details to members.
        if len(self.denial_times) > 10000:
            self.denial_times = {k: t for k, t in self.denial_times.items() if now - t < 10}
        if now - self.denial_times.get(key, -100) >= 10:
            self.denial_times[key] = now
            await self.safe_reply(event, DENIED)
        log.info(f'[{event.appid}] 查询拒绝 group={event.group_id} member={event.raw_user_id}')

    # ---------- buttons ----------

    async def on_interaction(self, event):
        payload = event.content or ''
        if event.event_type != 'INTERACTION_CREATE' or not payload.startswith('as:'):
            return False
        raw = event.interaction_data or {}
        if raw.get('type') not in {11, 12}:
            return False
        event.set_callback_code(0)  # Receipt only. The transport sends ACK independently from business messages.
        valid_scene = (raw.get('scene') == 'group' and raw.get('chat_type', 1) == 1 and event.group_id and event.raw_user_id) or (
            raw.get('scene') == 'c2c' and raw.get('chat_type', 2) == 2 and not event.group_id and event.raw_user_id
        )
        blocked = event.raw_user_id in get_blacklist_map(event.appid, 'user') or event.group_id in get_blacklist_map(event.appid, 'group')
        if not valid_scene or event.channel_id or raw.get('application_id', event.appid) != event.appid or blocked:
            await self.deny(event)
            return True
        return await self.activate_menu(event, payload)

    async def activate_menu(self, event, payload):
        if event.is_channel or not (event.is_group or event.is_direct):
            await self.deny(event)
            return True
        try:
            if payload == 'as:list':
                await self.directory(event)
                return True
            context, action = await self.menus.resolve(event, payload)
            if event.is_group and context.get('group') and (context['group'] != event.group_id or context['member'] != event.raw_user_id):
                raise PermissionError('群上下文不匹配')
            if not event.is_group and context.get('group'):
                chosen = await self.resolve_context(event)
                if not chosen or chosen.get('group') != context['group'] or chosen.get('member') != context['member']:
                    raise ValueError('菜单已失效，请重新发送：账户列表')
            # Every action re-resolves the clicker's own context and current eligibility.
            via = tuple(action['via']) if action.get('via') else None
            if action['kind'] == 'query':
                await self.query(event, action['resource'], expected_context=context, via_rule=via)
            elif action['kind'] == 'reply':
                await self.query(event, '', rule_id=action['rule'], expected_context=context, via_rule=via)
            elif action['kind'] == 'group':
                if await self.resolve_context(event, select=action['group'], resource=context.get('resource', '')):
                    await self.directory(event)
                else:
                    await self.deny(event)
            elif action['kind'] == 'page':
                if context['menu'] == 'candidates':
                    await self.keyword_query(event, text=context['keyword'], page=action['page'], expected_context=context)
                elif context['menu'] == 'groups':
                    await self.resolve_context(event, page=action['page'], resource=context.get('resource', ''))
                else:
                    await self.directory(event, page=action['page'], expected_context=context)
        except PermissionError:
            await self.deny(event)
        except (BusinessError, RuntimeError, sqlite3.Error, OSError):
            await self.deny(event)
        except ValueError as error:
            await self.safe_reply(event, str(error))
        return True

    # ---------- QQ entry ----------

    async def on_event(self, event):
        content = (event.content or '').strip()
        if content == '账户列表' or content.startswith(('账户列表 ', '账户查询 ', '账户群 ', '账户绑定 ', '账户答复 ', '账户翻页 ')):
            try:
                if content == '账户列表' or content.startswith('账户列表 '):
                    page = int(content.split()[1]) - 1 if len(content.split()) > 1 else 0
                    await self.directory(event, page=page)
                elif content.startswith('账户查询 '):
                    await self.query(event, content.split(maxsplit=1)[1])
                elif content.startswith('账户翻页 '):
                    await self.activate_menu(event, content.split(maxsplit=1)[1])
                elif content.startswith('账户答复 '):
                    await self.query(event, '', rule_id=content.split(maxsplit=1)[1])
                elif content.startswith('账户群 '):
                    if await self.resolve_context(event, select=content.split(maxsplit=1)[1]):
                        await self.directory(event)
                    else:
                        await self.deny(event)
                else:
                    # Identity binding is owned by the authorization module now.
                    await self.safe_reply(event, '身份绑定已改由授权模块处理：请私聊本 Bot 发送“身份绑定 申请 <群原生ID> <成员原生ID>”，由主人核实。')
            except PermissionError:
                await self.deny(event)
            except BusinessError as error:
                if error.status == 503:
                    await self.deny(event)
                else:
                    await self.safe_reply(event, str(error))
            except (ValueError, RuntimeError, sqlite3.Error, OSError):
                await self.deny(event)
            return
        if not content.startswith('账户管理'):
            try:
                await self.keyword_query(event)
            except (BusinessError, RuntimeError, ValueError, sqlite3.Error, OSError):
                await self.deny(event)
            return
        await self.owner_command(event, content)

    async def owner_command(self, event, content):
        bot = event.appid
        blocked = event.user_id in get_blacklist_map(bot, 'user') or event.group_id in get_blacklist_map(bot, 'group')
        if blocked or event.user_id not in (cfg.get_bot_config(bot) or {}).get('owner_ids', []):
            # Group delegates of ordinary Q&A have no account management.
            await self.deny(event)
            return
        try:
            if event.is_direct and not event.is_channel:
                await self.store.put(
                    bot, 'owners', 'observed_c2c', {'private_id': event.raw_user_id, 'authenticated_id': event.user_id, 'source': 'observed_owner_c2c'}
                )
            words = content.split(maxsplit=2)
            action = words[1] if len(words) > 1 else '帮助'
            args = words[2] if len(words) > 2 else ''
            detail = event.is_direct and not event.is_channel
            if action in {'待办', '重试通知'}:
                result = (
                    await self.tasks(bot, json.loads(args) if args else {})
                    if action == '待办'
                    else await self.task_action(bot, {'operation': 'retry_notification', 'id': args})
                )
                await self.safe_reply(event, json.dumps(result, ensure_ascii=False) if detail else '请在主人私聊或后台查看待办和通知详情')
                return
            if action == '迁移':
                result = await self.migrate(bot)
                await self.safe_reply(event, json.dumps(result, ensure_ascii=False) if detail else '迁移已处理，详情请在主人私聊或后台查看')
                return
            if action == '群发':
                result = await self.group_delivery.configure(bot, json.loads(args))
                await self.safe_reply(event, json.dumps(result, ensure_ascii=False) if detail else '群发检查已处理，逐人原因请在主人私聊或后台查看')
                return
            if action == '批量账户':
                result = await self.account_batches(bot, json.loads(args))
                await self.safe_reply(event, json.dumps(result, ensure_ascii=False) if detail else '账户批量操作已处理，详情请在主人私聊或后台查看')
                return
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
                await self.safe_reply(event, json.dumps(result, ensure_ascii=False) if detail else '规则操作已完成，详情请在主人私聊或后台查看')
                return
            if action == '确认绑定':
                raise BusinessError('身份绑定确认已移至授权管理：授权管理 {"operation":"binding_confirm",...}')
            if action == '结果':
                results = await self.store.all(bot, 'operations')
                await self.safe_reply(event, json.dumps(results, ensure_ascii=False) if detail else '请在主人私聊或后台查看结果')
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
                        raise BusinessError('@成员授权须使用当前群上下文；其他群请明确填写成员原生 ID')
                    targets = [m for m in event.mentions if isinstance(m, dict) and m.get('id') and not m.get('is_you')]
                    if len(targets) == 1:
                        data['member'] = targets[0]['id']
                result = await self.access_management(bot, data, actor=event.raw_user_id)
                await self.safe_reply(event, json.dumps(result, ensure_ascii=False) if detail else '授权操作已保存，详情请在主人私聊或后台查看')
                return
            if action == '预览':
                if not detail:
                    raise BusinessError('请在主人私聊中预览账户')
                account = await self.manage(bot, 'preview', key=args)
                await self.safe_reply(event, self.credentials(account))
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
                await self.safe_reply(
                    event,
                    '\n'.join(f'{a["id"]} {a["name"]} {"启用" if a["enabled"] else "停用"}' for a in accounts) if detail else '请在主人私聊或后台查看账户详情',
                )
                return
            else:
                await self.safe_reply(
                    event, '账户管理 新增 <JSON> / 修改 <ID> <JSON> / 删除 <ID> / 列表 / 预览 <ID>（仅私聊）/ 迁移 / 授权 <JSON> / 恢复来源 <JSON>'
                )
                return
            await self.safe_reply(event, '账户操作已保存；可在主人私聊或后台继续查看')
        except (ValueError, BusinessError) as error:
            await self.safe_reply(event, str(error) if isinstance(error, BusinessError) else '命令参数无效，请检查 JSON 和账户 ID')
        except (sqlite3.Error, OSError, RuntimeError):
            log.error(f'[{bot}] 账户共享持久化不可用')
            await self.safe_reply(event, UNAVAILABLE)

    # ---------- HTTP ----------

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
            if parts == ['tasks']:
                if request.method == 'GET':
                    result = await self.tasks(bot, {k: request.query[k] for k in ('group', 'member', 'resource') if k in request.query})
                elif request.method == 'POST':
                    result = await self.task_action(bot, await request.json())
                else:
                    raise BusinessError('方法不适用', 405)
                return web.json_response({'success': True, 'data': result})
            if parts == ['migration'] and request.method == 'POST':
                report = await self.migrate(bot)
                if report['status'] == 'failed':
                    return web.json_response({'success': False, 'error': '迁移未完成，各阶段原因见 data', 'data': report}, status=422)
                return web.json_response({'success': True, 'data': report})
            if parts == ['group-delivery']:
                if request.method == 'GET':
                    result = await self.group_delivery.list(bot)
                elif request.method == 'POST':
                    try:
                        result = await self.group_delivery.configure(bot, await request.json())
                    except BusinessError:
                        raise
                    except ValueError as error:
                        raise BusinessError(str(error)) from error
                else:
                    raise BusinessError('方法不适用', 405)
                return web.json_response({'success': True, 'data': result})
            if parts == ['account-batches'] and request.method == 'POST':
                return web.json_response({'success': True, 'data': await self.account_batches(bot, await request.json())})
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
                if request.method != 'GET':
                    raise BusinessError('身份绑定确认已移至“授权管理”页面', 410)
                return web.json_response({'success': True, 'data': await self.access().binding_snapshot(bot)})
            if parts == ['access']:
                if request.method == 'GET':
                    result = await (await self.ensure(bot)).access_snapshot(bot, CONSUMER)
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
