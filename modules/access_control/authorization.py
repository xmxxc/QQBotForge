"""Consumer-owned policy rows and current group facts, independent of account data."""

import asyncio
import copy
import json
import sqlite3
from uuid import uuid4

from core.base.config import cfg
from core.plugin._blacklist import get_blacklist_map

from .bindings import Bindings
from .policy import AccessControl, expiry, identifier

COLLECTION = 'access_consumers'
_MISSING = object()


def identifiers(values, *, maximum=500):
    if not isinstance(values, list) or len(values) > maximum:
        raise ValueError('标识须为有界列表')
    return list(dict.fromkeys(identifier(value) for value in values))


def policy(value):
    if not isinstance(value, dict) or set(value) - {'kind', 'groups', 'private', 'members', 'roles'}:
        raise ValueError('策略字段无效')
    kind = value.get('kind')
    if kind not in {'public', 'group', 'members', 'role', 'explicit', 'owner'}:
        raise ValueError('策略须为 public/group/members/role/explicit/owner')
    if type(value.get('private', False)) is not bool:
        raise ValueError('private 须为布尔值')
    result = {'kind': kind, 'groups': identifiers(value.get('groups', [])), 'private': value.get('private', False)}
    if kind == 'members':
        result['members'] = identifiers(value.get('members', []))
    elif 'members' in value:
        raise ValueError('只有 members 策略可指定成员')
    if kind == 'role':
        roles = identifiers(value.get('roles', []), maximum=3)
        if not roles or set(roles) - {'owner', 'admin', 'member'}:
            raise ValueError('角色须明确选择 owner/admin/member')
        result['roles'] = roles
    elif 'roles' in value:
        raise ValueError('只有 role 策略可指定角色')
    return result


class Authorization(AccessControl):
    """Legacy APIs remain readable during migration; scoped API owns new policies.

    Each scoped consumer is a distinct SQLite row. Failure of that row does not
    corrupt another consumer; database-wide failures pause every scoped decision.
    """

    def __init__(self, path, **kwargs):
        super().__init__(path, **kwargs)
        self.generation = uuid4().hex
        self.scope_states = {}
        self.scope_locks = {}
        self.scope_failed = set()
        self.scope_pending = set()
        self.pending_functions = {}
        self.shared_failure = False
        self.fact_subscription = None
        self.fact_source = None
        self.bindings = Bindings(self)

    @staticmethod
    def group_state():
        from core.application import get_app

        app = get_app()
        source = app.module_manager.get('group_state') if app and app.module_manager else None
        return source if source and source.active else None

    async def initialize(self):
        await super().initialize()
        source = self.group_state()
        if source is not None:
            self._attach(source)

    async def group_state_available(self, source):
        if self.active:
            self._attach(source)

    def _attach(self, source):
        if self.fact_source is not source:
            if self.fact_subscription:
                self.fact_subscription.cancel()
            self.fact_subscription = source.subscribe(self._facts_changed)
            self.fact_source = source

    def _facts(self):
        source = self.group_state()
        if source is None:
            raise RuntimeError('群状态模块未启用')
        self._attach(source)
        return source

    def close(self):
        if self.fact_subscription:
            self.fact_subscription.cancel()
        super().close()

    def _scope_ready(self, key):
        return self.active and not self.shared_failure and key not in self.scope_failed

    async def _read_row(self, bot, consumer):
        try:
            row = await self.store.get(bot, COLLECTION, consumer, missing=_MISSING)
        except (json.JSONDecodeError, TypeError):
            self.scope_failed.add((bot, consumer))
            raise RuntimeError('该消费方记录无法解码') from None
        except (sqlite3.Error, OSError, RuntimeError):
            self.shared_failure = True
            raise
        if row is None:
            self.scope_failed.add((bot, consumer))
            raise RuntimeError('该消费方记录为无效空值')
        return None if row is _MISSING else row

    def _validate_scope(self, state, consumer):
        if not isinstance(state, dict) or state.get('schema_version') != 1 or state.get('id') != consumer or type(state.get('version')) is not int:
            raise ValueError('消费方数据版本或身份无效')
        identifier(state.get('owner'))
        if not isinstance(state.get('functions'), dict) or not isinstance(state.get('resources'), dict):
            raise ValueError('功能与资源结构无效')
        if len(state['functions']) > 100 or len(state['resources']) > 5000:
            raise ValueError('每消费方最多100功能及5000资源')
        for function, configured in state['functions'].items():
            identifier(function)
            normalized = policy(configured)
            if set(normalized) - configured.keys():
                raise ValueError('已保存策略缺少范围字段')
        for resource, metadata in state['resources'].items():
            identifier(resource)
            if not isinstance(metadata, dict) or metadata.get('id') != resource or type(metadata.get('enabled')) is not bool:
                raise ValueError('资源结构无效')
            identifiers(metadata.get('groups'))
        versions = state.get('function_versions', {})
        if (
            not isinstance(versions, dict)
            or any(name not in state['functions'] or type(value) is not int or value < 0 for name, value in versions.items())
            or type(state.get('shared_version', 0)) is not int
        ):
            raise ValueError('资格版本结构无效')
        groups = state.get('function_groups', {})
        if not isinstance(groups, dict):
            raise ValueError('功能组结构无效')
        for key, group in groups.items():
            identifier(key)
            if (
                not isinstance(group, dict)
                or group.get('id') != key
                or any(name not in state['functions'] for name in identifiers(group.get('functions'), maximum=100))
            ):
                raise ValueError('功能组结构无效')
        selection_state = {**state, 'function_groups': groups}
        for field in ('grants', 'denies', 'revocations'):
            if not isinstance(state.get(field), list):
                raise ValueError('来源结构无效')
            for record in state[field]:
                if not isinstance(record, dict):
                    raise ValueError('来源结构无效')
                identifier(record.get('id'))
                if record.get('function') not in state['functions']:
                    raise ValueError('来源功能无效')
                if 'function_scope' in record:
                    self._function_members(selection_state, record['function_scope'])
                if record.get('mode', 'snapshot') not in {'snapshot', 'continuous'}:
                    raise ValueError('来源模式无效')
                if record.get('resource_namespace', consumer) != consumer:
                    raise ValueError('来源资源不属于当前消费方')
                if record.get('mode') == 'continuous' and not {'function_scope', 'resource_scope'} <= record.keys():
                    raise ValueError('持续来源缺少显式范围')
                if 'functions' in record and any(name not in state['functions'] for name in identifiers(record['functions'], maximum=100)):
                    raise ValueError('来源功能范围无效')
                if 'resource_scope' in record:
                    self._resource_selection(selection_state, consumer, record['resource_scope'])
                for name in ('group', 'member'):
                    if not isinstance(record.get(name), str) or len(record[name]) > 256:
                        raise ValueError('来源身份无效')
                if field != 'denies':
                    identifier(record.get('cycle'))
                    identifiers(record.get('resources'), maximum=5000)
                    if 'expires_at' not in record:
                        raise ValueError('授权来源缺少明确期限')
                    expiry(record['expires_at'])
                    if type(record.get('invoke')) is not bool:
                        raise ValueError('调用资格无效')

    async def _scope(self, bot, consumer):
        key = identifier(bot), identifier(consumer)
        if not self._scope_ready(key):
            raise RuntimeError('该消费方授权暂不可用，需要重载并核验')
        if key not in self.scope_states:
            async with self.scope_locks.setdefault(key, asyncio.Lock()):
                if key not in self.scope_states:
                    state = await self._read_row(bot, consumer)
                    if state is None:
                        raise ValueError('消费方尚未注册')
                    try:
                        self._validate_scope(state, consumer)
                    except (ValueError, TypeError, KeyError):
                        self.scope_failed.add(key)
                        raise RuntimeError('该消费方记录结构无效') from None
                    self.scope_states[key] = state
        state = self.scope_states[key]
        state.setdefault('function_groups', {})
        state.setdefault('function_versions', {})
        state.setdefault('shared_version', 0)
        return state

    @staticmethod
    def _resource_selection(state, consumer, scope):
        if scope == {'kind': 'resources', 'ids': []}:
            return []  # explicit invoke-only source, preserved from scoped v1
        return AccessControl._resource_members({**state, 'resources': {consumer: state['resources']}}, consumer, scope)

    def _scoped_resource(self, state, consumer, grant, resource):
        if grant.get('mode', 'snapshot') == 'continuous':
            return resource in self._resource_selection(state, consumer, grant['resource_scope'])
        return resource in grant['resources']

    async def _persist(self, bot, consumer, state, *, functions=None):
        key = bot, consumer
        state['version'] += 1  # audit revision; only relevant qualification revisions change
        if functions is None:
            state['shared_version'] = state['version']
        else:
            for function in functions:
                state.setdefault('function_versions', {})[function] = state['version']
        self.pending_functions[key] = None if functions is None else set(functions)
        self.scope_pending.add(key)
        try:
            await self.store.put(bot, COLLECTION, consumer, state)
        except BaseException as error:
            self.scope_failed.add(key)
            if isinstance(error, (sqlite3.OperationalError, sqlite3.DatabaseError, OSError, RuntimeError)) and not isinstance(error, sqlite3.IntegrityError):
                self.shared_failure = True
            raise
        finally:
            self.scope_pending.discard(key)
            self.pending_functions.pop(key, None)

    async def declare(self, bot, consumer, *, owner, functions):
        identifier(bot), identifier(consumer), identifier(owner)
        if not isinstance(functions, dict) or not 1 <= len(functions) <= 100:
            raise ValueError('请声明 1 至 100 个功能及默认策略')
        defaults = {identifier(name): policy(value) for name, value in functions.items()}
        key = bot, consumer
        if not self._scope_ready(key):
            raise RuntimeError('授权存储未就绪')
        async with self.scope_locks.setdefault(key, asyncio.Lock()):
            state = self.scope_states.get(key) or await self._read_row(bot, consumer)
            if state is None:
                state = {
                    'schema_version': 1,
                    'id': consumer,
                    'owner': owner,
                    'version': 0,
                    'functions': {},
                    'function_groups': {},
                    'resources': {},
                    'grants': [],
                    'denies': [],
                    'revocations': [],
                }
            try:
                self._validate_scope(state, consumer)
            except (ValueError, TypeError, KeyError):
                self.scope_failed.add(key)
                raise RuntimeError('该消费方记录结构无效') from None
            if len(set(state['functions']) | set(defaults)) > 100:
                raise ValueError('每消费方最多100个已声明功能')
            if state['owner'] != owner:
                raise ValueError('消费方标识已由其他安装者拥有')
            added = set()
            for function, default in defaults.items():
                if function not in state['functions']:
                    state['functions'][function] = default
                    added.add(function)
            state.setdefault('function_groups', {})
            self.scope_states[key] = state
            if added:
                await self._persist(bot, consumer, state, functions=added)
        return await self.access_snapshot(bot, consumer)

    async def declare_resources(self, bot, consumer, resources):
        if not isinstance(resources, list) or len(resources) > 5000:
            raise ValueError('资源清单须为最多 5000 项列表')
        inventory = {}
        for row in resources:
            if not isinstance(row, dict) or set(row) - {'id', 'groups', 'enabled'} or type(row.get('enabled', True)) is not bool:
                raise ValueError('资源字段无效')
            native = identifier(row.get('id'))
            if native in inventory:
                raise ValueError('资源标识重复')
            inventory[native] = {'id': native, 'groups': identifiers(row.get('groups', [])), 'enabled': row.get('enabled', True)}
        state = await self._scope(bot, consumer)
        async with self.scope_locks.setdefault((bot, consumer), asyncio.Lock()):
            if state['resources'] != inventory:
                removed = set(state['resources']) - set(inventory)
                for grant in tuple(state['grants']):
                    lost = removed.intersection(grant['resources'])
                    if grant.get('mode', 'snapshot') == 'snapshot' and lost:
                        state['revocations'].append(
                            {**copy.deepcopy(grant), 'resources': sorted(lost), 'partial': True, 'reason': 'resource_removed', 'revoked_at': self.clock()}
                        )
                        grant['resources'] = [key for key in grant['resources'] if key not in removed]
                state['resources'] = inventory
                await self._persist(bot, consumer, state)

    async def access_snapshot(self, bot, consumer):
        result = copy.deepcopy(await self._scope(bot, consumer))
        result['generation'] = self.generation
        return result

    def _retire(self, state, grants, reason):
        for grant in grants:
            state['revocations'].append({**copy.deepcopy(grant), 'reason': reason, 'revoked_at': self.clock()})
            state['grants'].remove(grant)

    async def _bulk_access(self, bot, consumer, operation, function, group, member, data):
        if member:
            raise ValueError('批量成员不可与单成员字段混用')
        values = dict(data)
        members = values.pop('members', None)
        if not isinstance(members, list) or not 1 <= len(members) <= 500:
            raise ValueError('批量须有 1 至 500 个明确成员')
        items = []
        for target in members:
            native = ''
            try:
                if not isinstance(target, dict) or set(target) != {'member'}:
                    raise ValueError('批量目标须为 member 群原生身份对象')
                native = identifier(target['member'])
                result = await self.manage_access(bot, consumer, operation, function=function, group=group, member=native, **values)
                items.append({'member': native, 'status': 'saved', 'result': result})
            except (ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error, OSError) as error:
                items.append({'member': native, 'status': 'failed', 'error': str(error)})
        success = sum(item['status'] == 'saved' for item in items)
        return {'items': items, 'summary': {'success': success, 'failed': len(items) - success}}

    async def manage_access(self, bot, consumer, operation, *, function, group='', member='', **data):
        if operation in {'bulk_grant', 'bulk_revoke', 'bulk_expiry'}:
            return await self._bulk_access(bot, consumer, operation[5:], function, group, member, data)
        state = await self._scope(bot, consumer)
        if identifier(function) not in state['functions']:
            raise ValueError('消费方功能尚未声明')
        if group:
            identifier(group)
        if member:
            identifier(member)
        allowed = {
            'policy': {'policy'},
            'grant': {'resources', 'invoke', 'expires_at', 'function_scope', 'resource_scope', 'mode'},
            'function_group': {'id', 'functions'},
            'revoke': {'source'},
            'deny': {'function_scope'},
            'allow': {'function_scope', 'source'},
            'expiry': {'source', 'expires_at'},
        }
        if operation not in allowed or set(data) - allowed[operation]:
            raise ValueError('授权操作或字段无效')
        proof = None
        if operation == 'grant':
            identifier(group), identifier(member)
            proof = await self._facts().fact(bot, group, member)
            if not proof['usable'] or proof['member']['status'] != 'present':
                raise ValueError('请先通过群状态核验成员当前在群')
        async with self.scope_locks.setdefault((bot, consumer), asyncio.Lock()):
            result = None
            affected = {function}
            if operation == 'policy':
                state['functions'][function] = policy(data.get('policy'))
            elif operation == 'function_group':
                group_id = identifier(data.get('id'))
                functions = identifiers(data.get('functions'), maximum=100)
                if any(name not in state['functions'] for name in functions):
                    raise ValueError('功能组只能包含该消费方已声明功能')
                affected = set(functions) | set(state['function_groups'].get(group_id, {}).get('functions', []))
                state['function_groups'][group_id] = {'id': group_id, 'functions': functions}
            elif operation == 'grant':
                assert proof is not None
                mode = data.get('mode', 'snapshot')
                if mode not in {'snapshot', 'continuous'} or 'resources' in data and 'resource_scope' in data:
                    raise ValueError('须选择 snapshot/continuous，资源表达不可混用')
                function_scope = copy.deepcopy(data.get('function_scope', {'kind': 'function', 'id': function}))
                resource_scope = copy.deepcopy(data.get('resource_scope', {'kind': 'resources', 'ids': identifiers(data.get('resources', []))}))
                functions = self._function_members(state, function_scope)
                resources = self._resource_selection(state, consumer, resource_scope)
                if (
                    mode == 'snapshot'
                    and not functions
                    or any(resource not in state['resources'] for resource in resources)
                    or type(data.get('invoke', True)) is not bool
                ):
                    raise ValueError('一次授权须含当前功能，资源须已声明，invoke 须为布尔值')
                if not self._facts().fact_current(proof):
                    raise ValueError('群事实已变化，请重新核验')
                result = {
                    'id': uuid4().hex,
                    'function': function,
                    'group': group,
                    'member': member,
                    'cycle': proof['member']['cycle'],
                    'resources': resources,
                    'functions': functions,
                    'function_scope': function_scope,
                    'resource_scope': resource_scope,
                    'resource_namespace': consumer,
                    'mode': mode,
                    'invoke': data.get('invoke', True),
                    'expires_at': expiry(data.get('expires_at')),
                    'created_at': self.clock(),
                }
                state['grants'].append(result)
                affected = set(functions)
            elif operation in {'revoke', 'expiry'}:
                grants = [
                    g
                    for g in state['grants']
                    if (g['id'] == data['source'] if data.get('source') else self._source_matches_function(state, g, function))
                    and g['group'] == group
                    and g['member'] == member
                    and (not data.get('source') or g['id'] == data['source'])
                ]
                affected = {name for name in state['functions'] if any(self._source_matches_function(state, grant, name) for grant in grants)}
                if operation == 'revoke':
                    self._retire(state, grants, 'owner_revoked')
                else:
                    if 'expires_at' not in data or not grants:
                        raise ValueError('须明确现存来源和 expires_at')
                    value = expiry(data['expires_at'])
                    for grant in grants:
                        grant['expires_at'] = value
            else:
                function_scope = copy.deepcopy(data.get('function_scope', {'kind': 'function', 'id': function}))
                affected = set(self._function_members(state, function_scope))
                target = {'function': function, 'group': group, 'member': member, 'function_scope': function_scope}
                matches = [
                    d
                    for d in state['denies']
                    if d['group'] == group
                    and d['member'] == member
                    and (
                        d['id'] == data['source']
                        if data.get('source')
                        else d.get('function_scope', {'kind': 'function', 'id': d['function']}) == function_scope
                    )
                ]
                if operation == 'allow':
                    affected |= {
                        name
                        for name in state['functions']
                        if any(name in self._function_members(state, d.get('function_scope', {'kind': 'function', 'id': d['function']})) for d in matches)
                    }
                if operation == 'deny' and not matches:
                    state['denies'].append({'id': uuid4().hex, **target})
                elif operation == 'allow':
                    state['denies'] = [d for d in state['denies'] if d not in matches]
            await self._persist(bot, consumer, state, functions=affected)
            return copy.deepcopy(result) if result else {'version': state['version']}

    async def _facts_changed(self, change):
        bot, group = change['bot'], change['group']
        source = self.fact_source
        if source is None:
            raise RuntimeError('群事实来源已失效')
        native = change.get('native', {})
        kind, member = native.get('event_type'), native.get('member_openid')
        # Read all owned rows, including consumers not yet used in this process.
        try:
            consumers = await self.store.keys(bot, COLLECTION)
        except (sqlite3.Error, OSError, RuntimeError):
            self.shared_failure = True
            raise
        for consumer in consumers:
            try:
                identifier(consumer)
            except ValueError:
                self.scope_failed.add((bot, consumer))
                continue
            if (bot, consumer) in self.scope_failed:
                continue
            try:
                state = await self._scope(bot, consumer)
            except RuntimeError:
                if self.shared_failure:
                    raise
                continue
            async with self.scope_locks.setdefault((bot, consumer), asyncio.Lock()):
                targets = [
                    g
                    for g in state['grants']
                    if g['group'] == group
                    and (kind in {'GROUP_ADD_ROBOT', 'GROUP_DEL_ROBOT'} or kind in {'GROUP_MEMBER_ADD', 'GROUP_MEMBER_REMOVE'} and member == g['member'])
                ]
                for grant in state['grants']:
                    if grant['group'] == group and grant not in targets:
                        proof = await source.fact(bot, group, grant['member'])
                        fact = proof['member'] or {}
                        if fact.get('status') == 'left' or fact.get('cycle') and fact['cycle'] != grant['cycle']:
                            targets.append(grant)
                if targets:
                    self._retire(state, targets, 'group_lifecycle')
                    try:
                        await self._persist(
                            bot,
                            consumer,
                            state,
                            functions={name for name in state['functions'] if any(self._source_matches_function(state, grant, name) for grant in targets)},
                        )
                    except sqlite3.IntegrityError:
                        # This consumer remains quarantined; shared group facts
                        # and unrelated consumers can still finish their update.
                        continue

    async def evaluate(self, bot, consumer, function, group, member, resource='', *, action='view', framework_allowed=True):
        if action not in {'invoke', 'view'}:
            raise ValueError('动作须为 invoke/view')
        state = await self._scope(bot, consumer)
        configured = state['functions'].get(function)
        if configured is None:
            raise ValueError('消费方功能尚未声明')
        result = {
            'bot': bot,
            'consumer': consumer,
            'function': function,
            'group': group,
            'member': member,
            'resource': resource,
            'action': action,
            'generation': self.generation,
            'version': state['version'],
            'function_revision': state.get('function_versions', {}).get(function, 0),
            'shared_revision': state.get('shared_version', 0),
            'allowed': False,
            'reason': '',
            'sources': [],
            'evaluated_at': self.clock(),
            'valid_until': None,
        }
        blocked = not framework_allowed or member in get_blacklist_map(bot, 'user') or group and group in get_blacklist_map(bot, 'group')
        denies = [
            d
            for d in state['denies']
            if function in self._function_members(state, d.get('function_scope', {'kind': 'function', 'id': d['function']}))
            and d['group'] in {'', group}
            and d['member'] in {'', member}
        ]
        if blocked:
            result['reason'] = 'framework_denied'
        elif denies:
            result.update(reason='denied', denies=copy.deepcopy(denies))
        elif group not in configured['groups'] if group else not configured['private']:
            result['reason'] = 'scope_disabled'
        elif resource and (resource not in state['resources'] or not state['resources'][resource]['enabled']):
            result['reason'] = 'resource_disabled'
        elif configured['kind'] == 'public':
            result.update(allowed=True, reason='public')
        elif configured['kind'] == 'owner':
            result.update(allowed=member in (cfg.get_bot_config(bot) or {}).get('owner_ids', []), reason='owner_required')
        elif not group:
            result['reason'] = 'group_context_required'
        else:
            try:
                source = self._facts()
                proof = await source.fact(bot, group, member)
            except RuntimeError:
                result['reason'] = 'group_facts_unavailable'
                return result
            result['fact'] = proof
            row = proof['member'] or {}
            result.update(membership=row.get('status', 'unknown'), cycle=row.get('cycle'), role=row.get('member_role'))
            if not proof['usable'] or row.get('status') != 'present':
                result['reason'] = 'membership_unverified'
            elif configured['kind'] == 'group':
                result.update(allowed=True, reason='group_member')
            elif configured['kind'] == 'members':
                result.update(allowed=member in configured['members'], reason='member_policy')
            elif configured['kind'] == 'role':
                result.update(allowed=row.get('member_role') in configured['roles'], reason='role_policy')
            else:
                grants = [
                    g
                    for g in state['grants']
                    if self._source_matches_function(state, g, function) and g['group'] == group and g['member'] == member and g['cycle'] == row.get('cycle')
                ]
                current = [g for g in grants if g['expires_at'] is None or self.clock() < g['expires_at']]
                result['sources'] = [{**copy.deepcopy(g), 'status': 'active' if g in current else 'expired'} for g in grants]
                invoke = [g for g in current if g['invoke']]
                viewing = [g for g in current if self._scoped_resource(state, consumer, g, resource)]
                if invoke and (action == 'invoke' or viewing):
                    deadlines = [max(float('inf') if g['expires_at'] is None else g['expires_at'] for g in invoke)]
                    if action != 'invoke':
                        deadlines.append(max(float('inf') if g['expires_at'] is None else g['expires_at'] for g in viewing))
                    deadline = min(deadlines)
                    result.update(allowed=True, reason='explicit', valid_until=None if deadline == float('inf') else deadline)
                else:
                    result['reason'] = 'expired' if grants and not current else 'no_explicit_permission'
        if result['allowed'] and not self.decision_current(bot, result):
            result.update(allowed=False, reason='state_changed')
        return result

    async def binding_snapshot(self, bot):
        return await self.bindings.manage(bot, 'binding_list', {}, 'internal_owner')

    async def private_contexts(self, bot, consumer, function, private_id, *, group='', resource='', action='invoke'):
        return await self.bindings.contexts(bot, consumer, function, private_id, group=group, resource=resource, action=action)

    def context_current(self, bot, context):
        return self.bindings.current(bot, context)

    def decision_current(self, bot, decision):
        if not isinstance(decision, dict) or not decision.get('allowed'):
            return False
        if 'consumer' not in decision:
            return super().decision_current(bot, decision)
        key = bot, decision['consumer']
        state = self.scope_states.get(key)
        if not (
            self._scope_ready(key)
            and (key not in self.scope_pending or self.pending_functions.get(key) is not None and decision.get('function') not in self.pending_functions[key])
            and state
            and decision.get('bot') == bot
            and decision.get('generation') == self.generation
            and decision.get('function_revision') == state.get('function_versions', {}).get(decision.get('function'), 0)
            and decision.get('shared_revision') == state.get('shared_version', 0)
            and decision.get('allowed')
            and (decision.get('valid_until') is None or self.clock() < decision['valid_until'])
        ):
            return False
        if decision.get('member') in get_blacklist_map(bot, 'user') or decision.get('group') in get_blacklist_map(bot, 'group'):
            return False
        if state['functions'][decision['function']]['kind'] == 'owner' and decision['member'] not in (cfg.get_bot_config(bot) or {}).get('owner_ids', []):
            return False
        if 'fact' in decision:
            source = self.group_state()
            return bool(source and source.fact_current(decision['fact']))
        return True
