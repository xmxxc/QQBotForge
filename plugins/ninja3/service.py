"""忍者必须死3: owner-maintained accounts answered publicly in open groups.

The plugin owns accounts, open groups and which groups each account is open in
(``open_groups``: native QQ group IDs, never authorization resource groups).
Eligibility comes from the authorization module (consumer ``ninja3``), which
reads group state. One owner grant is a continuous source over all of this
consumer's resources; the plugin's own open-group rules narrow it per group.
Group replies are public; the plugin never claims a complete roster.
"""

import asyncio
import contextlib
import json
import re
import sqlite3
import time
from uuid import uuid4

from aiohttp import web

from core.base.config import cfg
from core.base.logger import PLUGIN, get_logger
from core.display import display_group, display_member, format_member
from core.plugin._blacklist import get_blacklist_map

from .emergency import EmergencyError, EmergencyMode
from .password_batches import OUTCOMES, PasswordBatches
from .password_tasks import PasswordTasks, TaskError
from .storage import AccountLockedError, ExtensionStore

log = get_logger(PLUGIN, '忍者必须死3')
PREFIX = '/api/ext/ninja3/'
UNAVAILABLE = '忍者必须死3 存储暂不可用，请联系主人检查后台日志'
CONSUMER = 'ninja3'
FUNCTION = 'query'
OWNER = 'plugin:ninja3'
# New groups are closed: the policy group list mirrors the plugin's open groups.
DEFAULT_POLICY = {'kind': 'explicit', 'groups': [], 'private': False}
GRANT = {
    'function_scope': {'kind': 'function', 'id': FUNCTION},
    'resource_scope': {'kind': 'all'},
    'mode': 'continuous',
    'invoke': True,
    'expires_at': None,
}
NOTICE_WINDOW = 600
ACCOUNT_FIELDS = {'name', 'login', 'password', 'description', 'url', 'enabled', 'open_groups'}
GROUP_FIELDS = {'group', 'name', 'enabled', 'auto_grant'}
PAUSED = {'recovery_processing', 'persistence_pending', 'recovery_required'}
REASONS = {
    'no_explicit_permission': '未授权',
    'expired': '授权已到期',
    'membership_unverified': '成员在群事实待核验',
    'member_left': '成员已离群',
    'group_facts_unavailable': '群状态未启用',
    'group_facts_recovery_required': '群状态需恢复基线',
    'group_facts_processing': '群状态处理中',
    'scope_disabled': '本群未在授权范围',
    'resource_disabled': '账号未启用',
    'state_changed': '检查期间状态变化',
}
CHANGED = '账号或授权刚刚变化，请重新发送查询'
# Directory rows: SQLite projects these fields, so login/password never reach the plugin.
DIRECTORY = ('id', 'name', 'enabled', 'open_groups')
# Owner page fields holding native QQ IDs; names are looked up for display only.
GROUP_KEYS = {'group', 'groups', 'open_groups', 'policy_before', 'policy_after'}
MEMBER_KEYS = {'member'}
NAMES_MAX = 200


def native_ids(value, groups, members):
    if isinstance(value, dict):
        for key, item in value.items():
            target = groups if key in GROUP_KEYS else members if key in MEMBER_KEYS else None
            if target is not None:
                target.update(v for v in (item if isinstance(item, list) else [item]) if isinstance(v, str) and v)
            native_ids(item, groups, members)
    elif isinstance(value, list):
        for item in value:
            native_ids(item, groups, members)


async def display_names(bot, value):
    """Display text for the IDs on an owner page; the response data keeps the native IDs."""
    groups, members = set(), set()
    native_ids(value, groups, members)
    groups, members = sorted(groups)[:NAMES_MAX], sorted(members)[:NAMES_MAX]
    shown = await asyncio.gather(*(display_group(bot, g) for g in groups), *(display_member(bot, m) for m in members))
    split = len(groups)
    return {'groups': dict(zip(groups, shown[:split], strict=True)), 'members': dict(zip(members, shown[split:], strict=True))}


async def member_label(bot, member, name=''):
    # A name carried by the triggering event is newer than the queued data.db write.
    return format_member(member, name) if (name or '').strip() else await display_member(bot, member)


def unknown_code(code):
    return str(code) == '-1' or isinstance(code, int) and 500 <= code <= 599


class BusinessError(ValueError):
    def __init__(self, message, status=422):
        self.status = status
        super().__init__(message)


def native(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise BusinessError(f'{label}须为原生标识')
    return value


def text(value, label, *, required=False):
    if not isinstance(value, str) or len(value) > 4000 or (required and not value.strip()):
        raise BusinessError(f'{label}须为{"非空" if required else ""}文字（最多 4000 字符）')
    return value


class Ninja3:
    def __init__(self, path, *, access_provider=None, sender_provider=None, clock=time.time):
        self.store = ExtensionStore(path)
        self.lock = asyncio.Lock()
        self.active = True
        self.access_provider = access_provider or self._access_module
        self.sender_provider = sender_provider or self._bot_sender
        self.clock = clock
        self.revisions = {}
        self.writes = {}
        self.notice_locks = {}
        self.menus = {}  # Process-local references: reload invalidates every old menu.
        self.password_tasks = PasswordTasks(self.store, notify=self.password_receipt, on_change=self._task_changed, clock=clock)
        self.password_batches = PasswordBatches(self.store, self.password_tasks, notify=self.password_batch_receipt, clock=clock)
        self.emergency = EmergencyMode(self.store, self.password_tasks, notify=self.emergency_receipt, clock=clock)

    @staticmethod
    def _access_module():
        from core.application import get_app

        app = get_app()
        return app.module_manager.get('access_control') if app and app.module_manager else None

    @staticmethod
    def _bot_sender(bot):
        from core.application import get_app

        app = get_app()
        instance = app.get_bot(bot) if app else None
        return instance.sender if instance else None

    async def initialize(self):
        await self.store.ready()
        await self.password_tasks.initialize()
        await self.password_batches.initialize()
        await self.emergency.initialize()

    def close(self):
        self.active = False
        self.emergency.closed = True
        self.password_tasks.closed = True
        self.password_batches.closed = True
        self.store.close()

    def access(self):
        module = self.access_provider()
        if module is None or not module.active:
            raise BusinessError('黑白名单授权模块未启用，查询暂停，请联系主人', 503)
        return module

    # ---------- versions ----------

    @contextlib.contextmanager
    def writing(self, bot):
        # Bumped before and after: a read that started earlier, or overlaps the write, is stale.
        self.revisions[bot] = self.revisions.get(bot, 0) + 1
        self.writes[bot] = self.writes.get(bot, 0) + 1
        try:
            yield
        finally:
            self.writes[bot] -= 1
            self.revisions[bot] += 1

    def revision(self, bot):
        return self.revisions.get(bot, 0)

    def unchanged(self, bot, revision):
        return self.active and not self.writes.get(bot) and self.revision(bot) == revision

    def _task_changed(self, bot):
        """Durable task commits invalidate in-flight account queries like owner writes."""
        self.revisions[bot] = self.revisions.get(bot, 0) + 1

    @staticmethod
    async def declare(acl, bot):
        # The declaration interface never overwrites an existing owner policy.
        await acl.declare(bot, CONSUMER, owner=OWNER, functions={FUNCTION: DEFAULT_POLICY})

    async def sync(self, bot, change, apply):
        """Run one owner-configuration sync and record it; member queries never write authorization.

        ``apply(acl)`` changes only what this save needs. It reads the authorization
        snapshot as its last await before its single write, so an owner edit made in
        the authorization page in between is never reverted (no await separates them).
        """
        acl = self.access_provider()
        result = {'id': uuid4().hex, 'kind': 'acl_sync', 'created_at': self.clock(), **change}
        if acl is None or not acl.active:
            result['status'] = 'unavailable'
        else:
            try:
                await self.declare(acl, bot)
                result.update(await apply(acl), status='saved')
            except (RuntimeError, ValueError, sqlite3.Error, OSError) as error:
                result.update(status='failed', error=type(error).__name__)
        await self.store.put(bot, 'operations', result['id'], result)
        log.info(f'[{bot}] 授权同步 {change} status={result["status"]}')
        return result

    async def sync_account(self, bot, key):
        """Only this account's resource entry (ID and enablement, no category groups); others keep their authorization state."""

        async def apply(acl):
            account = await self.store.get(bot, 'accounts', key)
            resources = (await acl.access_snapshot(bot, CONSUMER))['resources']
            before = resources.get(key)
            inventory = {**resources, key: {'id': key, 'groups': [], 'enabled': account['enabled']}} if account else dict(resources)
            if not account:
                inventory.pop(key, None)
            after = inventory.get(key)
            if before != after:
                await acl.declare_resources(bot, CONSUMER, list(inventory.values()))
            return {'resource_before': before and before['enabled'], 'resource_after': after and after['enabled']}

        return await self.sync(bot, {'account': key}, apply)

    async def sync_group(self, bot, group):
        """Only this group in the policy scope and its native-join rule; kind, private switch, other groups, sources and denials are kept."""

        async def apply(acl):
            record = await self.store.get(bot, 'groups', group)
            # Automatic grants on trusted native joins only for an open group the owner opted in.
            wanted = bool(record and record['enabled'] and record.get('auto_grant'))
            rules = (await acl.access_snapshot(bot, CONSUMER)).get('join_rules', [])
            had = any((r['function'], r['group']) == (FUNCTION, group) for r in rules)
            if had and not wanted:
                # Stop automatic grants before anything else changes.
                await acl.manage_access(bot, CONSUMER, 'join_rule', function=FUNCTION, group=group, enabled=False)
            configured = (await acl.access_snapshot(bot, CONSUMER))['functions'][FUNCTION]
            groups = [g for g in configured['groups'] if g != group] + ([group] if record and record['enabled'] else [])
            if groups != configured['groups']:
                await acl.manage_access(bot, CONSUMER, 'policy', function=FUNCTION, policy={**configured, 'groups': groups})
            if wanted and not had:
                await acl.manage_access(bot, CONSUMER, 'join_rule', function=FUNCTION, group=group, enabled=True)
            return {'policy_before': configured['groups'], 'policy_after': groups, 'auto_grant_before': had, 'auto_grant_after': wanted}

        return await self.sync(bot, {'group': group}, apply)

    # ---------- owner management (HTTP) ----------

    @staticmethod
    def public(account):
        return {k: v for k, v in account.items() if k != 'password'}

    async def validate_account(self, bot, data, key='', *, create=False):
        if not isinstance(data, dict) or set(data) - ACCOUNT_FIELDS:
            raise BusinessError('账号字段无效')
        if create and not {'name', 'login', 'password'} <= data.keys():
            raise BusinessError('请填写名称、登录账号和密码')
        for field, label in (('name', '名称'), ('login', '登录账号'), ('password', '密码')):
            if field in data:
                text(data[field], label, required=True)
        for field, label in (('description', '说明'), ('url', '地址')):
            if field in data:
                text(data[field], label)
        if 'enabled' in data and not isinstance(data['enabled'], bool):
            raise BusinessError('启用状态须为布尔值')
        if 'open_groups' in data:
            groups = data['open_groups']
            if not isinstance(groups, list) or len(groups) > 100:
                raise BusinessError('开放群须为最多 100 个群原生 ID')
            known = {g['group'] for g in await self.store.all(bot, 'groups')}
            for group in groups:
                if native(group, '开放群') not in known:
                    raise BusinessError('开放群须先在“群”中添加', 404)
            data['open_groups'] = list(dict.fromkeys(groups))
        if 'name' in data:
            data['name'] = data['name'].strip()
            if any(a['name'] == data['name'] and a['id'] != key for a in await self.store.all(bot, 'accounts')):
                raise BusinessError('同一 Bot 下账号名称不能重复', 409)
        return data

    async def manage_account(self, bot, operation, data=None, key=''):
        if not self.active:
            raise BusinessError('忍者必须死3 未加载', 503)
        async with self.lock:
            if operation == 'list':
                return [self.public(a) for a in await self.store.all(bot, 'accounts')]
            old = await self.store.get(bot, 'accounts', key) if key else None
            if operation in {'update', 'delete'} and not old:
                raise BusinessError('账号不存在', 404)
            if operation == 'delete':
                with self.writing(bot):
                    await self.store.delete(bot, 'accounts', key)
                log.info(f'[{bot}] 删除账号 {key}')
                return {'id': key, 'acl_sync': await self.sync_account(bot, key)}
            if operation not in {'create', 'update'}:
                raise BusinessError('未知账号操作')
            data = await self.validate_account(bot, dict(data) if isinstance(data, dict) else data, key, create=operation == 'create')
            # New accounts are closed until explicitly enabled and opened in groups.
            account = old or {'id': uuid4().hex, 'description': '', 'url': '', 'enabled': False, 'open_groups': []}
            was_enabled = old['enabled'] if old else None
            account.update(data)
            with self.writing(bot):
                await self.store.put(bot, 'accounts', account['id'], account)
            log.info(f'[{bot}] 保存账号 {account["id"]} 启用={account["enabled"]} 开放群={len(account["open_groups"])}')
            # Only creation or an actual enablement change touches authorization; editing
            # other fields never lifts a restriction the owner set in the authorization page.
            if was_enabled == account['enabled']:
                return {**self.public(account), 'acl_sync': {'status': 'not_needed'}}
            return {**self.public(account), 'acl_sync': await self.sync_account(bot, account['id'])}

    async def manage_group(self, bot, operation, data=None, key=''):
        if not self.active:
            raise BusinessError('忍者必须死3 未加载', 503)
        async with self.lock:
            if operation == 'list':
                return await self.store.all(bot, 'groups')
            if operation == 'delete':
                if not await self.store.get(bot, 'groups', key):
                    raise BusinessError('群不存在', 404)
                with self.writing(bot):
                    # Drop references first so a re-added group starts closed for every account.
                    for account in await self.store.all(bot, 'accounts'):
                        if key in account['open_groups']:
                            account['open_groups'] = [g for g in account['open_groups'] if g != key]
                            await self.store.put(bot, 'accounts', account['id'], account)
                    await self.store.delete(bot, 'groups', key)
                log.info(f'[{bot}] 删除群 {key}')
                return {'group': key, 'acl_sync': await self.sync_group(bot, key)}
            if operation != 'save' or not isinstance(data, dict) or set(data) - GROUP_FIELDS:
                raise BusinessError('群字段无效')
            group = native(data.get('group'), '群')
            if 'enabled' in data and not isinstance(data['enabled'], bool):
                raise BusinessError('开放状态须为布尔值')
            if 'auto_grant' in data and not isinstance(data['auto_grant'], bool):
                raise BusinessError('入群自动授权须为布尔值')
            record = await self.store.get(bot, 'groups', group) or {'group': group, 'name': '', 'enabled': False}
            record.setdefault('auto_grant', False)
            record['auto_grant'] = data.get('auto_grant', record['auto_grant'])
            if 'name' in data:
                record['name'] = text(data['name'], '群备注')[:100]
            record['enabled'] = data.get('enabled', record['enabled'])
            with self.writing(bot):
                await self.store.put(bot, 'groups', group, record)
            log.info(f'[{bot}] 保存群 {group} 开放={record["enabled"]} 入群自动授权={record["auto_grant"]}')
            return {**record, 'acl_sync': await self.sync_group(bot, group)}

    # ---------- replies ----------

    @staticmethod
    def outcome(result):
        """A message ID is required to confirm delivery for replies and pushes."""
        if isinstance(result, tuple) and result and isinstance(result[0], bool):
            data = result[1] if len(result) > 1 else None
            if not result[0]:
                return 'unknown' if isinstance(data, dict) and unknown_code(data.get('code')) else 'failed'
            result = data
        if not isinstance(result, dict):
            return 'unknown'
        code = result.get('code')
        if code not in (None, 0):
            return 'unknown' if unknown_code(code) else 'failed'
        return 'sent' if any(isinstance(result.get(key), str) and result[key].strip() for key in ('id', 'msg_id')) else 'unknown'

    async def reply(self, event, content, *, gate=None, **kwargs):
        """``gate`` is a synchronous freshness check for sensitive content.

        It runs here and again as Sender's ``send_check``, right before each business
        request (after before_send, client and token preparation). Stale content is
        blocked before any transport attempt and replaced by a notice without it; an
        attempted send is never repeated.
        """
        sender = self.sender_provider(event.appid)
        if sender is None:
            return 'unavailable'
        if gate is None:
            return await self._send(sender, event, content, **kwargs)
        pending = {'check': gate, 'blocked': False}
        if self.fresh(pending):
            state = await self._send(sender, event, content, send_check=lambda: self.fresh(pending), **kwargs)
        else:
            state = 'stale'
        if state != 'sent' and pending['blocked']:
            await self.reply(event, CHANGED)
            return 'stale'
        return state

    async def _send(self, sender, event, content, **kwargs):
        try:
            return self.outcome(await sender.reply(event, content, **kwargs))
        except Exception:  # noqa: BLE001 - transport failures leave the result unknown
            return 'unknown'

    @staticmethod
    def fresh(pending):
        try:
            current = bool(pending['check']())
        except Exception:  # noqa: BLE001 - a failing check is stale, never a pass
            current = False
        pending['blocked'] = pending['blocked'] or not current
        return current

    @staticmethod
    def credentials(account):
        first = [f'名称：{account["name"]}']
        if account.get('description', '').strip():
            first.append(f'说明：{account["description"].strip()}')
        if account.get('url', '').strip():
            first.append(f'地址：{account["url"].strip()}')
        return ['\n'.join(first), account['login'], account['password']]

    # ---------- owner grant ----------

    @staticmethod
    def is_owner(event):
        bot = event.appid
        blocked = event.user_id in get_blacklist_map(bot, 'user') or event.group_id in get_blacklist_map(bot, 'group')
        return not blocked and event.raw_user_id in (cfg.get_bot_config(bot) or {}).get('owner_ids', [])

    async def authorize(self, event):
        bot, group = event.appid, event.group_id
        if self.emergency.active(bot):
            if event.is_group and self.is_owner(event):
                await self.owner_private(bot, '紧急模式已开启，成员查询与授权操作暂停')
            return
        if not event.is_group or event.is_channel or not group:
            await self.reply(event, '请在要开放的群内发送：授权 @成员')
            return
        if not self.is_owner(event):
            await self.reply(event, '仅主人可以授权')
            return
        targets = {
            m['id']: m.get('username') or ''
            for m in event.mentions
            if isinstance(m, dict) and isinstance(m.get('id'), str) and m['id'] and not m.get('is_you') and not m.get('bot') and m.get('scope') != 'all'
        }
        if (event.content or '').strip() != '授权' or len(targets) != 1:
            # Display names and typed IDs are never identities; only the native mention payload is.
            await self.reply(event, '请发送“授权”并 @ 一位要授权的成员')
            return
        member, name = targets.popitem()
        try:
            acl = self.access()
            # The owner's grant; declaration never touches an existing policy scope.
            await self.declare(acl, bot)
            facts = acl.group_state()
            if facts is None:
                await self.reply(event, '授权待处理：群状态模块未启用')
                return
            proof = await facts.fact(bot, group, member)
            if proof['reason'] in PAUSED:
                await self.reply(event, '授权待处理：群状态恢复未完成，请先发送“群状态 恢复”，完成后重新授权')
                return
            if not proof['usable'] or (proof['member'] or {}).get('status') != 'present':
                # The owner's explicit command in this group is the evidence; a member's message never is.
                evidence = f'主人在本群发送“授权 @成员”（消息 {event.message_id or "未知"}）'
                confirmed = await facts.confirm_member(bot, group, member, evidence=evidence[:1000])
                if not confirmed.get('reliable'):
                    await self.reply(event, '授权待处理：成员在群事实未能确认，请稍后重试')
                    return
                proof = await facts.fact(bot, group, member)
            cycle = (proof['member'] or {}).get('cycle')
            snapshot = await acl.access_snapshot(bot, CONSUMER)
            existing = next(
                (
                    g
                    for g in snapshot['grants']
                    if g['group'] == group
                    and g['member'] == member
                    and g['cycle'] == cycle
                    and g.get('mode') == 'continuous'
                    and g.get('resource_scope') == GRANT['resource_scope']
                    and g['expires_at'] is None
                    # An automatic join source lapses with its join basis; the owner's grant stands alone.
                    and g.get('basis') != 'native_join'
                ),
                None,
            )
            source = existing or await acl.manage_access(bot, CONSUMER, 'grant', function=FUNCTION, group=group, member=member, **GRANT)
            decision = await acl.evaluate(bot, CONSUMER, FUNCTION, group, member, action='invoke')
            opened = await self.store.get(bot, 'groups', group)
        except BusinessError as error:
            await self.reply(event, f'授权待处理：{error}')
            return
        except ValueError as error:
            await self.reply(event, f'授权未完成：{error}')
            return
        except (RuntimeError, sqlite3.Error, OSError) as error:
            log.error(f'[{bot}] 授权失败: {type(error).__name__}')
            await self.reply(event, '授权未完成：授权或群状态存储暂不可用，请检查后台')
            return
        log.info(f'[{bot}] 授权 group={group} member={member} source={source["id"]} existing={existing is not None}')
        who = await member_label(bot, member, name)
        lines = [f'{who} 已有本群授权' if existing else f'已授权 {who} 查询本群开放账号，长期有效']
        if decision['reason'] in {'denied', 'framework_denied'}:
            lines.append('该成员在拒绝名单中，拒绝优先，当前仍不能查询')
        if not (opened and opened['enabled']):
            lines.append('本群尚未开放，请在后台开放本群及账号后生效')
        await self.reply(event, '\n'.join(lines))

    def account_paused(self, bot, account):
        """Current durable task pause; future emergency checks must be ORed, never reset here."""
        return self.password_tasks.is_paused(bot, account) or self.emergency.active(bot)

    async def password_receipt(self, bot, task):
        return await self.owner_private(bot, self.password_summary(task))

    async def password_batch_receipt(self, bot, batch):
        return await self.owner_private(bot, self.batch_summary(batch, detailed=True))

    async def emergency_receipt(self, bot, state):
        counts = {}
        for item in state.get('accounts', []):
            outcome = item.get('status', 'pending')
            counts[outcome] = counts.get(outcome, 0) + 1
        summary = '，'.join(f'{key} {value}' for key, value in counts.items()) or '无账号'
        return await self.owner_private(bot, f'忍3紧急模式：{state.get("phase", "未知")}；{summary}。详细记录可在已登录后台查看。')

    async def owner_private(self, bot, content):
        owner = await self.store.get(bot, 'owners', 'observed_c2c')
        sender = self.sender_provider(bot)
        if (
            not owner
            or sender is None
            or owner.get('authenticated_id') not in (cfg.get_bot_config(bot) or {}).get('owner_ids', [])
            or owner['private_id'] in get_blacklist_map(bot, 'user')
        ):
            return 'unavailable'
        return self.outcome(await sender.send_to_user(owner['private_id'], content))

    async def management_reply(self, event, content):
        """Route owner management results privately while emergency mode is active."""
        if event.is_group and self.emergency.active(event.appid):
            return await self.owner_private(event.appid, content)
        return await self.reply(event, content)

    async def emergency_command(self, event, command=None):
        """Enter, inspect, or leave persistent emergency mode.

        Group invocations deliberately have no public acknowledgement.  A
        non-owner is silent so the command cannot become a management oracle.
        """
        if not self.is_owner(event):
            return
        if event.is_channel:
            return
        bot = event.appid
        command = command or (event.content or '').strip().split(maxsplit=1)[0]
        try:
            if command == '忍3紧急模式':
                result = await self.emergency.enter(bot)
                message = f'紧急模式已开启（{result["generation"]}），成员查询已暂停；全账号改密已开始，结果将在主人私聊或后台查看。'
            elif command == '忍3紧急状态':
                result = await self.emergency.status(bot)
                message = f'紧急模式：{result["state"]}；阶段：{result["phase"]}。详细记录可在已登录后台查看。'
            else:
                result = await self.emergency.exit(bot)
                message = '紧急模式已退出；未解决改密账号仍保持暂停，其他账号恢复查询。'
        except EmergencyError as error:
            message = str(error)
        if event.is_group:
            await self.owner_private(bot, message)
        else:
            await self.reply(event, message)

    @staticmethod
    def password_summary(task):
        labels = {
            'not_submitted': '尚未提交',
            'reported_success': '远端报告成功',
            'verified_success': '登录核验确认成功',
            'rejected': '远端明确拒绝',
            'unknown': '远端结果未知',
        }
        return f'忍3改密任务 {task["id"]}：{labels[task["remote"]]}；本地 {task["local"]}；状态 {task["state"]}。详细记录可在已登录后台查看。'

    @staticmethod
    def batch_summary(batch, *, detailed):
        """Per-item outcomes; account names only in private chat or the backend."""
        counts = '，'.join(f'{OUTCOMES[k]} {v}' for k, v in batch['counts'].items())
        head = f'忍3批量改密批次 {batch["id"]}（{"指定密码" if batch["mode"] == "specified" else "每账号随机"}）：{batch["state"]}；{counts}。'
        if not detailed:
            return head + '逐项结果请在主人私聊或后台查看。'
        lines = [f'{item["name"]}：{OUTCOMES[item["outcome"]]}' for item in batch['items']]
        return head + '\n' + '\n'.join(lines) + '\n通知结果可在已登录后台查看。'

    async def batch_command(self, event, command, argument, actor):
        bot = event.appid
        # A specified password is accepted only from the owner's real C2C chat (never group,
        # channel or channel DM). Refusals never echo the input.
        private = event.event_type == 'C2C_MESSAGE_CREATE'
        if command == '忍3批量改密':
            names, specified = argument, None
            if '指定' in argument:
                if not private:
                    await self.management_reply(event, '指定密码只接受主人私聊或已登录后台，本条未使用')
                    return
                match = re.fullmatch(r'(.*?)\s*(?<!\S)指定\s+(\S+)\s*', argument)
                if not match:
                    raise TaskError('请按“忍3批量改密 名称… 指定 新密码”发送，“指定”前后留空格')
                names, specified = match.group(1), match.group(2)
            tokens = [t for t in names.replace('，', ' ').replace(',', ' ').replace('、', ' ').split() if t]
            if not tokens:
                raise TaskError('尚未选择账号。发送“忍3批量改密 名称1 名称2”或“忍3批量改密 全部”；默认不选择')
            accounts = await self.store.all(bot, 'accounts')
            if tokens == ['全部']:
                chosen = None
            else:
                by_name = {a['name']: a['id'] for a in accounts}
                if any(t not in by_name for t in tokens):
                    raise TaskError(
                        '有账号名称不存在：' + '、'.join(t for t in tokens if t not in by_name) if private else '有账号名称不存在，请在私聊或后台核对'
                    )
                chosen = [by_name[t] for t in tokens]
            batch = await self.password_batches.select(bot, actor, chosen, new_password=specified)
            confirm = f'发送“忍3批量确认 {batch["id"]}”确认；未确认不会开始。'
            if private:
                scope = '\n'.join(f'{i["name"]}{"（指定密码与旧密码相同，将跳过）" if i["outcome"] == "input_rejected" else ""}' for i in batch['items'])
                await self.management_reply(event, f'已选择 {len(batch["items"])} 个账号：\n{scope}\n{confirm}')
            else:
                await self.management_reply(event, f'已选择 {len(batch["items"])} 个账号，范围请在主人私聊或后台查看。{confirm}')
            return
        if not argument or len(argument) != 32:
            raise TaskError('请提供有效批次编号')
        if command == '忍3批量确认':
            batch = await self.password_batches.confirm(bot, argument, actor)
        elif command == '忍3批量取消':
            batch = await self.password_batches.cancel(bot, argument)
        else:
            batch = await self.password_batches.get(bot, argument)
        # Accepted is not success: the summary reports each account separately.
        await self.management_reply(event, self.batch_summary(batch, detailed=private))

    async def password_command(self, event):
        if not self.is_owner(event):
            if self.emergency.active(event.appid):
                return
            await self.reply(event, '仅主人可以操作改密任务')
            return
        if event.is_channel:
            await self.reply(event, '请使用主人群聊或私聊')
            return
        bot = event.appid
        pieces = (event.content or '').strip().split(maxsplit=1)
        command, argument = pieces[0], pieces[1].strip() if len(pieces) > 1 else ''
        actor = 'qq:' + event.user_id
        try:
            if command.startswith('忍3批量'):
                await self.batch_command(event, command, argument, actor)
                return
            if command == '忍3改密':
                accounts = await self.store.all(bot, 'accounts')
                if not argument:
                    # Group replies are public: account names are listed only in private chat.
                    names = '可选：' + '、'.join(a['name'] for a in accounts) if event.is_direct else '可选名称请在主人私聊或后台查看'
                    await self.management_reply(event, '尚未选择账号。发送“忍3改密 名称”选择一个账号；再普通确认。' + names)
                    return
                account = next((a for a in accounts if a['name'] == argument), None)
                if account is None:
                    raise TaskError('账号不存在，请只提供单个账号名称')
                task = await self.password_tasks.select(bot, account['id'], actor)
                if task['state'] == 'selected':
                    await self.management_reply(event, f'已选择账号。发送“忍3确认 {task["id"]}”确认随机改密；未确认不会开始。')
                    return
            elif not argument or len(argument) != 32:
                raise TaskError('请提供有效改密任务编号')
            elif command == '忍3确认':
                task = await self.password_tasks.confirm(bot, argument, actor)
            elif command == '忍3重试':
                task = await self.password_tasks.retry(bot, argument, actor)
            elif command == '忍3放弃':
                task = await self.password_tasks.release(bot, argument)
            elif command == '忍3取消':
                task = await self.password_tasks.cancel(bot, argument)
            elif command == '忍3恢复':
                task = await self.password_tasks.recover(bot, argument)
            elif command == '忍3核验':
                task = await self.password_tasks.verify(bot, argument)
            else:
                task = await self.password_tasks.get(bot, argument)
            # Group acknowledgement contains no detailed remote result or credentials.
            content = f'改密任务 {task["id"]} 已记录，请在主人私聊或后台查看结果。' if event.is_group else self.password_summary(task)
            await self.management_reply(event, content)
        except TaskError as error:
            await self.management_reply(event, str(error))
        except (sqlite3.Error, OSError, RuntimeError) as error:
            log.error(f'[{bot}] 改密任务操作失败: {type(error).__name__}')
            await self.management_reply(event, '改密任务暂不可用，账号保持原有暂停状态，请查看后台')

    # ---------- member queries ----------

    async def query(self, event, name=None, *, resource=None, page=0, menu=None):
        """``name=None`` lists the names; otherwise answers that account in this group."""
        bot, group, member = event.appid, event.group_id, event.raw_user_id
        if not self.emergency.current(bot):
            if self.is_owner(event):
                await self.owner_private(bot, '紧急模式已开启，成员查询暂停')
            return
        if not event.is_group or event.is_channel or not group:
            await self.reply(event, '请在已开放的群内发送：账号列表')
            return
        revision = self.revision(bot)
        try:
            # Read-only path: no declaration or policy write.
            acl = self.access()
            opened = await self.store.get(bot, 'groups', group)
            if not opened or not opened['enabled']:
                await self.reply(event, '本群未开放账号查询')
                return
            invoke = await acl.evaluate(bot, CONSUMER, FUNCTION, group, member, action='invoke')
            if not (invoke['allowed'] and invoke['reason'] == 'explicit'):
                await self.refuse(event, invoke, 'list' if name is None and resource is None else 'query')
                return
            decisions = [invoke]
            # Directory metadata only; credentials are read for one selected, allowed account.
            paused = self.password_tasks.paused_accounts(bot)
            if paused is None:
                await self.reply(event, UNAVAILABLE)
                return
            directory = [
                a for a in await self.store.project(bot, 'accounts', DIRECTORY) if a['enabled'] and group in (a['open_groups'] or [])
            ]
            accounts = [a for a in directory if a['id'] not in paused]
            if name is None and resource is None:
                available = []
                for account in accounts:
                    view = await acl.evaluate(bot, CONSUMER, FUNCTION, group, member, account['id'], action='view')
                    if view['allowed'] and view['reason'] == 'explicit':
                        available.append(account)
                        decisions.append(view)
                if not self.current(event, revision, acl, decisions, menu, account_ids=[a['id'] for a in accounts]):
                    await self.reply(event, CHANGED)
                    return
                if not available:
                    await self.reply(event, '本群暂无开放账号')
                    return
                last = (len(available) - 1) // 6
                if not 0 <= page <= last:
                    await self.reply(event, '菜单已变化，请重新发送：账号列表')
                    return
                shown = available[page * 6 : (page + 1) * 6]
                items = [(a['name'], {'resource': a['id']}) for a in shown]
                if page:
                    items.append(('上一页', {'page': page - 1}))
                if page < last:
                    items.append(('下一页', {'page': page + 1}))
                title = f'本群开放账号（{page + 1}/{last + 1}）：'
                await self.offer(event, title, items, lambda: self.current(event, revision, acl, decisions, menu, account_ids=[a['id'] for a in accounts]))
                return
            account = next((a for a in directory if (a['id'] == resource if resource else a['name'] == name)), None)
            if account is None:
                await self.reply(event, '该账号暂不可查询，请重新发送：账号列表' if resource else f'本群没有开放名为“{name}”的账号，发送“账号列表”查看')
                return
            if account['id'] in paused:
                if self.is_owner(event):
                    task_id = self.password_tasks.active_task_id(bot, account['id'])
                    task_id = (await self.password_batches.batch_for_task(bot, task_id) or task_id) if task_id else '未知'
                    message = f'该账号正在改密（批量任务 {task_id}），暂不可查询'
                else:
                    message = '该账号暂不可查询，请稍后再试'
                await self.reply(event, message)
                return
            view = await acl.evaluate(bot, CONSUMER, FUNCTION, group, member, account['id'], action='view')
            if not (view['allowed'] and view['reason'] == 'explicit'):
                await self.refuse(event, view, 'query')
                return
            decisions.append(view)
            record = await self.store.get(bot, 'accounts', account['id'])
            # Any write since the directory read bumps the revision; this only rejects a vanished row.
            if record is None:
                await self.reply(event, CHANGED)
                return
            contents = self.credentials(record)
        except BusinessError as error:
            await self.reply(event, str(error) if error.status != 503 else '账号查询暂不可用，请联系主人')
            return
        except (RuntimeError, ValueError, sqlite3.Error, OSError) as error:
            if not self.active:  # unloaded or reloaded mid-query: its store is closed
                await self.reply(event, CHANGED)
                return
            log.error(f'[{bot}] 账号查询暂不可用: {type(error).__name__}')
            await self.reply(event, '账号查询暂不可用，请联系主人')
            return
        # Final gate: after this plugin's last await and again after Sender's preparation.
        state = 'sent'
        for content in contents:
            state = await self.reply(event, content, msg_type=0, gate=lambda: self.current(event, revision, acl, decisions, menu, account_snapshot=[record]))
            if state != 'sent':
                break
        log.info(f'[{bot}] 账号{"列表" if name is None and resource is None else "查询"} group={group} member={member} result={state}')

    def current(self, event, revision, acl, decisions, menu=None, *, account_snapshot=None, account_ids=()):
        # Unchanged accounts, open groups and module instances, and current decisions
        # (sources, denial, resource enablement, policy scope, group facts, reloads).
        # #75 account pause and #78 persistent emergency checks must join this gate; it
        # also runs as Sender's send_check, so it must stay synchronous.
        return (
            self.unchanged(event.appid, revision)
            and self.plugin_available(event.appid)
            and self.access_provider() is acl
            and self.emergency.current(event.appid)
            and all(acl.decision_current(event.appid, d) for d in decisions)
            and (menu is None or self.menu_current(event, menu))
            and (not account_ids or self._accounts_current(event.appid, account_ids))
            and (account_snapshot is None or self.password_tasks.query_current(event.appid, account_snapshot))
        )

    def _accounts_current(self, bot, account_ids):
        paused = self.password_tasks.paused_accounts(bot)
        return self.emergency.current(bot) and paused is not None and not (set(account_ids) & paused)

    @staticmethod
    def plugin_available(bot):
        # Interceptors and non-message dispatch do not enforce plugin Bot bindings.
        from core.application import get_app

        app = get_app()
        manager = app.plugin_manager if app else None
        if manager is None:
            return False
        plugin = manager.plugins.get('ninja3')
        bindings = manager.get_plugin_bots().get('ninja3', [])
        return bool(plugin and plugin.enabled and (not bindings or bot in bindings))

    def menu_current(self, event, menu):
        return (
            self.active
            and self.plugin_available(event.appid)
            and event.is_group
            and not event.is_channel
            and self.emergency.current(event.appid)
            and menu['bot'] == event.appid
            and menu['group'] == event.group_id
            and menu['expires_at'] > self.clock()
        )

    async def offer(self, event, title, items, current):
        now = self.clock()
        self.menus = {key: value for key, value in self.menus.items() if value['expires_at'] > now}
        token = uuid4().hex
        session = self.menus[token] = {
            'bot': event.appid,
            'group': event.group_id,
            'expires_at': now + 300,
            'actions': [action for _, action in items],
        }

        # Shared group menu: no creator identity or credentials are retained.
        def check():
            return self.menu_current(event, session) and current()

        content = title + '\n' + '\n'.join(f'{i}. {label}' for i, (label, _) in enumerate(items, 1))
        buttons = [{'text': label[:20], 'type': 1, 'data': f'n3:{token}:{i}', 'tips': f'发送：忍3选择 {token} {i}'} for i, (label, _) in enumerate(items, 1)]
        rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
        markdown = cfg.get_bot_setting(event.appid, 'message.use_markdown', True)
        if markdown:
            state = await self.reply(event, content, buttons=rows, gate=check)
            if state != 'failed':  # sent, stale, unavailable or unknown: never duplicate.
                return
        # Only an explicit rejection falls back; it rechecks before and after Sender's preparation.
        fallback = content + f'\n五分钟内发送“忍3选择 {token} <编号>”'
        if markdown:
            fallback = content + f'\n按钮暂不可用，请使用以上文字编号。\n五分钟内发送“忍3选择 {token} <编号>”'
        await self.reply(event, fallback, msg_type=0, gate=check)

    async def menu(self, event):
        bot, group = event.appid, event.group_id
        revision = self.revision(bot)
        try:
            acl = self.access()
            opened = await self.store.get(bot, 'groups', group)
            snapshot = await acl.access_snapshot(bot, CONSUMER)
            policy = snapshot['functions'].get(FUNCTION)
            if not opened or not opened['enabled'] or not policy or group not in policy['groups']:
                return False

            def current():
                state = acl.scope_states.get((bot, CONSUMER))
                return (
                    self.unchanged(bot, revision)
                    and self.plugin_available(bot)
                    and self.emergency.current(bot)
                    and self.access_provider() is acl
                    and acl.active
                    and acl.generation == snapshot['generation']
                    and state is not None
                    and state['version'] == snapshot['version']
                    and (bot, CONSUMER) not in acl.scope_pending
                    and event.raw_user_id not in get_blacklist_map(bot, 'user')
                    and group not in get_blacklist_map(bot, 'group')
                )

            items = [('账号列表', {'kind': 'list'})]
            if self.is_owner(event):
                items += [('修改密码', {'manage': '修改密码'}), ('紧急模式', {'manage': '紧急模式'})]
            await self.offer(event, '忍者必须死3 菜单', items, current)
            return True
        except (BusinessError, RuntimeError, ValueError, sqlite3.Error, OSError):
            return False

    async def select(self, event, token, number):
        menu = self.menus.get(token)
        if not (menu and self.menu_current(event, menu) and 1 <= number <= len(menu['actions'])):
            if event.is_group and self.emergency.active(event.appid):
                if self.is_owner(event):
                    await self.owner_private(event.appid, '菜单已失效，请重新发送：账号列表')
            else:
                await self.reply(event, '菜单已失效，请重新发送：账号列表')
            return
        action = menu['actions'][number - 1]
        if 'manage' in action:
            if not self.is_owner(event):
                await self.reply(event, '仅主人可以管理')
                return
            # Validate current plugin/ACL group scope even for unimplemented actions.
            if not await self.scope_open(event):
                await self.reply(event, '本群未开放账号查询')
                return
            # Capture a synchronous scope snapshot for the final send gate.  The
            # preliminary async check above only admits the action; a group close,
            # ACL scope edit, reload or blacklist change while Sender prepares must
            # suppress even this placeholder prompt.
            revision = self.revision(event.appid)
            acl = self.access()
            snapshot = await acl.access_snapshot(event.appid, CONSUMER)

            def gate():
                return self.menu_current(event, menu) and self.is_owner(event) and self.scope_current(event, revision, acl, snapshot)

            if action['manage'] == '紧急模式':
                if not gate():
                    await self.reply(event, CHANGED)
                    return
                await self.emergency_command(event, '忍3紧急模式')
            else:
                await self.reply(event, action['manage'] + '尚未交付，当前不可用', gate=gate)
        else:
            # Resolve eligibility and credentials anew using the actual sender/clicker.
            await self.query(event, resource=action.get('resource'), page=action.get('page', 0), menu=menu)

    async def scope_open(self, event):
        try:
            revision = self.revision(event.appid)
            opened = await self.store.get(event.appid, 'groups', event.group_id)
            acl = self.access()
            snapshot = await acl.access_snapshot(event.appid, CONSUMER)
            return bool(
                self.plugin_available(event.appid)
                and self.access_provider() is acl
                and acl.active
                and self.emergency.current(event.appid)
                and (event.appid, CONSUMER) not in acl.scope_pending
                and self.unchanged(event.appid, revision)
                and opened
                and opened['enabled']
                and event.group_id in snapshot['functions'].get(FUNCTION, {}).get('groups', [])
            )
        except (BusinessError, RuntimeError, ValueError, sqlite3.Error, OSError):
            return False

    def scope_current(self, event, revision, acl, snapshot):
        """Synchronous scope freshness check used after the final async read."""
        state = acl.scope_states.get((event.appid, CONSUMER))
        policy = snapshot['functions'].get(FUNCTION)
        return bool(
            self.unchanged(event.appid, revision)
            and self.plugin_available(event.appid)
            and self.access_provider() is acl
            and acl.active
            and self.emergency.current(event.appid)
            and state is not None
            and acl.generation == snapshot['generation']
            and state['version'] == snapshot['version']
            and (event.appid, CONSUMER) not in acl.scope_pending
            and event.raw_user_id not in get_blacklist_map(event.appid, 'user')
            and event.group_id not in get_blacklist_map(event.appid, 'group')
            and policy is not None
            and event.group_id in policy.get('groups', [])
        )

    async def route(self, event):
        if not self.plugin_available(event.appid) or not event.is_group or event.is_channel:
            return False
        content = (event.content or '').strip()
        # Exact commands only. Interceptors precede the framework non-@ admission gate.
        if event.event_type == 'GROUP_MESSAGE_CREATE':
            if event.is_at_other_bot or event.is_at_other_user:
                return False
            if not event.is_at_self and not (
                cfg.get_bot_setting(event.appid, 'non_at_message.enabled', False)
                or event.group_id in (cfg.get_bot_setting(event.appid, 'non_at_message.group_whitelist', []) or [])
            ):
                return False
        elif event.event_type != 'GROUP_AT_MESSAGE_CREATE':
            return False
        match = re.fullmatch(r'忍3选择 ([0-9a-f]{32}) ([1-9][0-9]?)', content)
        if match:
            await self.select(event, match[1], int(match[2]))
            return True
        if content not in {'', '菜单', '账号列表'} or (not content and not event.is_at_self):
            return False
        if not await self.scope_open(event):
            return False
        if content == '账号列表':
            await self.query(event)
            return True
        return await self.menu(event)

    async def on_interaction(self, event):
        raw = event.interaction_data or {}
        event.set_callback_code(0)  # Receipt only; business authorization is separate.
        if not (
            raw.get('type') in {11, 12}
            and raw.get('scene') == 'group'
            and raw.get('chat_type', 1) == 1
            and event.group_id
            and event.raw_user_id
            and raw.get('application_id', event.appid) == event.appid
            and not event.channel_id
            and not event.is_channel
            and event.raw_user_id not in get_blacklist_map(event.appid, 'user')
            and event.group_id not in get_blacklist_map(event.appid, 'group')
        ):
            return
        match = re.fullmatch(r'n3:([0-9a-f]{32}):([1-9][0-9]?)', event.content or '')
        if match:
            await self.select(event, match[1], int(match[2]))
        else:
            await self.reply(event, '菜单引用无效，请重新发送：账号列表')

    async def refuse(self, event, decision, action):
        bot, group, member = event.appid, event.group_id, event.raw_user_id
        reason = decision['reason']
        log.info(f'[{bot}] 查询拒绝 group={group} member={member} reason={reason}')
        # Explicit owner restrictions are not notified: denial, framework blacklist, and
        # a group the owner keeps out of the authorization scope (closed there).
        if decision.get('retryable'):
            await self.reply(event, '暂时无法确认查询资格，请稍后重试')
            return
        if reason in {'denied', 'framework_denied'}:
            await self.reply(event, '你暂无查询权限')
            return
        if reason == 'scope_disabled':
            await self.reply(event, '本群未开放账号查询')
            return
        if reason == 'resource_disabled':
            await self.reply(event, '该账号暂不可查询，请联系主人')
            return
        record = await self.notice(bot, group, member, action, reason, name=event.username)
        # Claim a notice only when this attempt actually reached the owner.
        sent = record['count'] == 1 and record['notification'] == 'sent'
        told = '已通知主人' if sent else '已记录给主人'
        # Unreliable group facts (recovery required, module off) are for the owner to judge.
        unsure = reason in {'group_facts_recovery_required', 'group_facts_unavailable'}
        await self.reply(event, f'暂时无法确认查询资格，{told}' if unsure else f'你暂无查询权限，{told}')

    # ---------- owner notices ----------

    async def notice(self, bot, group, member, action, reason, *, name=''):
        key = json.dumps([group, member], ensure_ascii=False)
        async with self.notice_locks.setdefault((bot, key), asyncio.Lock()):
            now = self.clock()
            record = await self.store.get(bot, 'notices', key)
            if record and now - record['first_at'] < NOTICE_WINDOW:
                # Merged: no new message within ten minutes per Bot, group and member.
                record.update(count=record['count'] + 1, last_at=now, action=action, reason=reason)
                await self.store.put(bot, 'notices', key, record)
                return record
            record = {'id': key, 'group': group, 'member': member, 'action': action, 'reason': reason, 'first_at': now, 'last_at': now, 'count': 1}
            return await self.deliver_notice(bot, record, name=name)

    async def deliver_notice(self, bot, record, *, name=''):
        owner = await self.store.get(bot, 'owners', 'observed_c2c')
        sender = self.sender_provider(bot)
        owner_ids = (cfg.get_bot_config(bot) or {}).get('owner_ids', [])
        if not owner or sender is None or owner.get('authenticated_id') not in owner_ids or owner['private_id'] in get_blacklist_map(bot, 'user'):
            record['notification'] = 'unavailable'
        else:
            what = '查看账号列表' if record['action'] == 'list' else '查询账号'
            group, member = await display_group(bot, record['group']), await member_label(bot, record['member'], name)
            content = (
                f'忍者必须死3：群 {group} 的成员 {member} 尝试{what}，{REASONS.get(record["reason"], record["reason"])}。\n'
                '如需授权，请在该群发送“授权 @该成员”；十分钟内同一成员的后续尝试合并记录，可在后台查看。'
            )
            try:
                record['notification'] = self.outcome(await sender.send_to_user(owner['private_id'], content))
            except Exception:  # noqa: BLE001
                record['notification'] = 'unknown'
        await self.store.put(bot, 'notices', record['id'], record)
        log.info(f'[{bot}] 未授权通知 group={record["group"]} member={record["member"]} notification={record["notification"]}')
        return record

    async def notice_action(self, bot, data):
        if not isinstance(data, dict) or set(data) != {'operation', 'id'} or data['operation'] != 'retry' or not isinstance(data['id'], str):
            raise BusinessError('请提供 retry 和通知 id')
        key = data['id']
        async with self.notice_locks.setdefault((bot, key), asyncio.Lock()):
            record = await self.store.get(bot, 'notices', key)
            if not record:
                raise BusinessError('通知不存在', 404)
            return record if record['notification'] == 'sent' else await self.deliver_notice(bot, record)

    async def owner_status(self, event):
        bot = event.appid
        if not self.is_owner(event):
            return
        if event.is_direct and not event.is_channel:
            await self.store.put(
                bot, 'owners', 'observed_c2c', {'private_id': event.raw_user_id, 'authenticated_id': event.raw_user_id, 'source': 'observed_owner_c2c'}
            )
        accounts = await self.store.all(bot, 'accounts')
        groups = await self.store.all(bot, 'groups')
        pending = [n for n in await self.store.all(bot, 'notices') if n.get('notification') != 'sent']
        lines = [f'忍者必须死3：账号 {len(accounts)} 个（启用 {sum(a["enabled"] for a in accounts)}），开放群 {sum(g["enabled"] for g in groups)} 个']
        if pending:
            lines.append(f'未送达的未授权通知 {len(pending)} 条，请在后台查看')
        if event.is_direct and not event.is_channel:
            lines.append('已记录本私聊为主人通知地址')
        await self.management_reply(event, '\n'.join(lines))

    async def status(self, bot):
        """Read-only comparison of the plugin's open scope with the authorization module's."""
        groups = await self.store.all(bot, 'groups')
        accounts = await self.store.all(bot, 'accounts')
        acl = self.access_provider()
        try:
            snapshot = await acl.access_snapshot(bot, CONSUMER) if acl is not None and acl.active else None
        except (RuntimeError, ValueError, sqlite3.Error, OSError) as error:
            snapshot, reason = None, type(error).__name__
        else:
            reason = '' if snapshot else 'unavailable'
        policy = snapshot['functions'][FUNCTION] if snapshot else None
        resources = snapshot['resources'] if snapshot else {}
        rules = {r['group'] for r in snapshot.get('join_rules', []) if r['function'] == FUNCTION} if snapshot else set()
        rows = [
            {
                'group': g['group'],
                'plugin_open': g['enabled'],
                'acl_open': bool(policy and g['group'] in policy['groups']),
                'plugin_auto_grant': g['enabled'] and g.get('auto_grant', False),
                'acl_auto_grant': g['group'] in rules,
            }
            for g in groups
        ]
        items = [
            {
                'id': a['id'],
                'name': a['name'],
                'plugin_enabled': a['enabled'],
                'acl_resource': 'missing' if a['id'] not in resources else 'enabled' if resources[a['id']]['enabled'] else 'disabled',
            }
            for a in accounts
        ]
        # Both must pass; a mismatch only ever restricts. Saving the group/account again syncs it.
        mismatched = (
            [r['group'] for r in rows if r['plugin_open'] and not r['acl_open']]
            + [r['group'] + '（入群自动授权不一致）' for r in rows if r['plugin_open'] and r['acl_open'] and r['plugin_auto_grant'] != r['acl_auto_grant']]
            + [i['name'] for i in items if i['plugin_enabled'] and i['acl_resource'] != 'enabled']
        )
        return {'acl': 'available' if snapshot else reason, 'policy': policy, 'groups': rows, 'accounts': items, 'restricted_by_acl': mismatched}

    # ---------- HTTP ----------

    async def http(self, request):
        from web.auth import authorize_request

        denied = authorize_request(request)
        if denied is not None:
            return denied
        bot = request.query.get('bot', '')
        if not cfg.get_bot_config(bot):
            return web.json_response({'success': False, 'error': '请选择有效 Bot'}, status=404)
        parts = request.path[len(PREFIX) :].strip('/').split('/')
        try:
            if parts[0] == 'password-tasks':
                tasks = self.password_tasks
                if request.method == 'GET' and len(parts) <= 2:
                    result = await tasks.list(bot) if len(parts) == 1 else await tasks.get(bot, parts[1])
                elif request.method == 'POST' and len(parts) == 1:
                    body = await request.json()
                    if not isinstance(body, dict) or set(body) != {'account'} or not isinstance(body['account'], str):
                        raise TaskError('请明确选择一个账号，不接收指定密码')
                    result = await tasks.select(bot, body['account'], 'backend')
                elif request.method == 'POST' and len(parts) == 3 and parts[2] in {'confirm', 'retry', 'cancel', 'recover', 'release', 'verify'}:
                    body = await request.json()
                    if body != {}:
                        raise TaskError('任务操作不接收额外参数')
                    if parts[2] in {'confirm', 'retry'}:
                        result = await getattr(tasks, parts[2])(bot, parts[1], 'backend')
                    else:
                        result = await getattr(tasks, parts[2])(bot, parts[1])
                else:
                    raise BusinessError('方法不适用', 405)
                return web.json_response({'success': True, 'data': result})
            if parts[0] == 'emergency':
                if request.method == 'GET' and len(parts) == 1:
                    result = await self.emergency.status(bot)
                elif request.method == 'POST' and len(parts) == 1:
                    if await request.json() != {}:
                        raise TaskError('紧急模式入口不接收额外参数')
                    result = await self.emergency.enter(bot)
                elif request.method == 'POST' and parts[1:] == ['exit']:
                    if await request.json() != {}:
                        raise TaskError('紧急模式入口不接收额外参数')
                    result = await self.emergency.exit(bot)
                else:
                    raise BusinessError('方法不适用', 405)
                return web.json_response({'success': True, 'data': result})
            if parts[0] == 'password-batches':
                batches = self.password_batches
                if request.method == 'GET' and len(parts) <= 2:
                    result = await batches.list(bot) if len(parts) == 1 else await batches.get(bot, parts[1])
                elif request.method == 'POST' and len(parts) == 1:
                    body = await request.json()
                    if not isinstance(body, dict) or not {'accounts'} <= set(body) <= {'accounts', 'password'}:
                        raise TaskError('请提供 accounts（账号列表或 "all"），可选 password')
                    chosen = body['accounts']
                    if chosen != 'all' and not (isinstance(chosen, list) and chosen and all(isinstance(a, str) for a in chosen)):
                        raise TaskError('请明确选择账号或 "all"，默认不选择')
                    if 'password' in body and not isinstance(body['password'], str):
                        raise TaskError('指定密码须为文字')
                    result = await batches.select(bot, 'backend', None if chosen == 'all' else chosen, new_password=body.get('password'))
                elif request.method == 'POST' and len(parts) == 3 and parts[2] in {'confirm', 'cancel'}:
                    if await request.json() != {}:
                        raise TaskError('批次操作不接收额外参数')
                    result = await batches.confirm(bot, parts[1], 'backend') if parts[2] == 'confirm' else await batches.cancel(bot, parts[1])
                else:
                    raise BusinessError('方法不适用', 405)
                return web.json_response({'success': True, 'data': result})
            if parts in (['status'], ['operations']) and request.method == 'GET':
                data = await self.status(bot) if parts == ['status'] else await self.store.all(bot, 'operations')
                return web.json_response({'success': True, 'data': data, 'names': await display_names(bot, data)})
            if parts == ['notices']:
                if request.method == 'GET':
                    data = await self.store.all(bot, 'notices')
                    return web.json_response({'success': True, 'data': data, 'names': await display_names(bot, data)})
                if request.method == 'POST':
                    return web.json_response({'success': True, 'data': await self.notice_action(bot, await request.json())})
                raise BusinessError('方法不适用', 405)
            if parts[0] in {'accounts', 'groups'} and len(parts) <= 2:
                key = parts[1] if len(parts) == 2 else ''
                if parts[0] == 'accounts':
                    operation = {'GET': 'list', 'POST': 'create', 'PATCH': 'update', 'DELETE': 'delete'}.get(request.method)
                    keyed = {'update', 'delete'}
                else:
                    operation = {'GET': 'list', 'POST': 'save', 'DELETE': 'delete'}.get(request.method)
                    keyed = {'delete'}
                if not operation or bool(key) != (operation in keyed):
                    raise BusinessError('方法不适用', 405)
                data = await request.json() if request.method in {'POST', 'PATCH'} else None
                manage = self.manage_account if parts[0] == 'accounts' else self.manage_group
                result = await manage(bot, operation, data, key)
                body = {'success': True, 'data': result}
                if operation == 'list':
                    body['names'] = await display_names(bot, result)
                return web.json_response(body, status=201 if operation == 'create' else 200)
            raise BusinessError('入口不存在', 404)
        except (BusinessError, TaskError, EmergencyError, json.JSONDecodeError) as error:
            return web.json_response({'success': False, 'error': str(error)}, status=getattr(error, 'status', 422))
        except AccountLockedError as error:
            return web.json_response({'success': False, 'error': str(error)}, status=409)
        except (sqlite3.Error, OSError, RuntimeError):
            log.error(f'[{bot}] 忍者必须死3 持久化不可用')
            return web.json_response({'success': False, 'error': UNAVAILABLE}, status=503)
