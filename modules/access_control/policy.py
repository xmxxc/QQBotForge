"""Reusable Bot/group/native-member authorization; no account data or QQ sends."""

import asyncio
import copy
import json
import math
import sqlite3
import time
from uuid import uuid4

from core.base.logger import EXTENSION, get_logger

from .storage import ExtensionStore

log = get_logger(EXTENSION, '黑白名单授权')


def identity_key(*parts):
    return json.dumps(parts, ensure_ascii=False)


def identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError('请提供明确的 Bot、功能、群和原生身份标识')
    return value


def expiry(value):
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value > 253402300799 or not math.isfinite(value)):
        raise ValueError('到期时间须为非负有限 Unix 秒数或 null（长期）')
    return value


class AccessControl:
    """decide returns a reason/source/state/version, never consumer credentials.

    Owners explicitly confirm group-native identities on grant. Group admission,
    invoke capability, resource permission and member verification are independent.
    Mutations invalidate versions before persistence; storage failure fails closed.
    """

    def __init__(self, path, *, clock=time.time, lifecycle_available=False, reader=None):
        self.store = ExtensionStore(path)
        self.clock = clock
        self.lifecycle_available = lifecycle_available
        self.reader = reader
        self.roster_locks = {}
        self.states = {}
        self.locks = {}
        self.active = True
        self.healthy = True

    async def initialize(self):
        await self.store.ready()

    def close(self):
        self.active = False
        self.store.close()

    async def _state(self, bot):
        identifier(bot)
        if not self.active or not self.healthy:
            raise RuntimeError('授权模块未就绪')
        if bot not in self.states:
            async with self.locks.setdefault(bot, asyncio.Lock()):
                if bot not in self.states:
                    persisted = await self.store.get(bot, 'access', 'state')
                    state = persisted or {
                        'version': 0,
                        'whitelist': {},
                        'members': {},
                        'grants': [],
                        'denies': [],
                    }
                    if state.get('schema_version', 1) > 5:
                        raise RuntimeError('授权数据版本高于当前模块')
                    if persisted:
                        for membership in state['members'].values():
                            if membership.get('status') == 'present':
                                membership.update(status='pending', verification_reason='process_restart')
                        for roster in state.get('rosters', {}).values():
                            roster.update(complete=False, reason='process_restart')
                        state['version'] += 1
                        await self._save(bot, state)
                    self.states[bot] = state
        state = self.states[bot]
        if state.get('schema_version', 1) > 5:
            raise RuntimeError('授权数据版本高于当前模块')
        state['schema_version'] = 5
        state.setdefault('functions', {})
        state.setdefault('function_groups', {})
        state.setdefault('resources', {})
        state.setdefault('revocations', [])
        state.setdefault('group_versions', {})
        state.setdefault('qualification_version', state['version'])
        state.setdefault('qualification_groups', {})
        state.setdefault('groups', {})
        state.setdefault('rosters', {})
        state.setdefault('verifications', [])
        for deny in state['denies']:
            deny.setdefault('id', uuid4().hex)
            deny.setdefault('function_scope', {'kind': 'function', 'id': deny['function']})
        for grant in state['grants']:
            grant.setdefault('expires_at', None)
            state['functions'].setdefault(grant['function'], {'id': grant['function']})
        for key in state['whitelist']:
            function = json.loads(key)[1]
            state['functions'].setdefault(function, {'id': function})
        return state

    async def snapshot(self, bot):
        state = copy.deepcopy(await self._state(bot))
        state['capabilities'] = {'lifecycle_events': self.lifecycle_available}
        for roster in state['rosters'].values():
            if roster.get('version') != state['version']:
                roster.update(complete=False, reason='state_changed')
        return state

    async def _save(self, bot, state, *, qualification_change=True, qualification_group=None):
        if qualification_change:
            if qualification_group:
                state.setdefault('qualification_groups', {})[qualification_group] = state['version']
            else:
                state['qualification_version'] = state['version']
        try:
            await self.store.put(bot, 'access', 'state', state)
        except Exception:
            self.healthy = False
            raise

    async def register_function(self, bot, function):
        identifier(function)
        state = await self._state(bot)
        async with self.locks.setdefault(bot, asyncio.Lock()):
            if function not in state['functions']:
                state['functions'][function] = {'id': function}
                state['version'] += 1
                await self._save(bot, state)

    async def register_resources(self, bot, namespace, resources):
        identifier(namespace)
        inventory = {}
        for resource in resources:
            key = identifier(resource.get('id'))
            groups = resource.get('groups', [])
            if not isinstance(groups, list) or any(not isinstance(g, str) or not g for g in groups):
                raise ValueError('资源分组须为明确标识列表')
            inventory[key] = {'id': key, 'groups': sorted(set(groups)), 'enabled': resource.get('enabled', True)}
        state = await self._state(bot)
        async with self.locks.setdefault(bot, asyncio.Lock()):
            old = state['resources'].get(namespace, {})
            if inventory == old and namespace in state['resources']:
                return
            removed = set(old) - set(inventory)
            kept = []
            for source in state['grants']:
                if source.get('resource_namespace', source['function']) == namespace and removed:
                    source['resources'] = [r for r in source['resources'] if r not in removed]
                    scope = source.get('resource_scope', {'kind': 'resources', 'ids': source['resources']})
                    if scope['kind'] == 'resources':
                        scope['ids'] = [r for r in scope['ids'] if r not in removed]
                    if not source['resources'] and (source.get('mode', 'snapshot') == 'snapshot' or scope['kind'] == 'resources'):
                        continue
                kept.append(source)
            state['grants'] = kept
            state['resources'][namespace] = inventory
            state['version'] += 1
            await self._save(bot, state)

    @staticmethod
    def _function_members(state, scope):
        if not isinstance(scope, dict) or set(scope) - {'kind', 'id'}:
            raise ValueError('功能范围须明确选择单功能、功能授权组或全部功能')
        if scope.get('kind') == 'all' and set(scope) != {'kind'}:
            raise ValueError('全部功能范围不能同时指定其他目标')
        if scope.get('kind') in {'function', 'group'} and set(scope) != {'kind', 'id'}:
            raise ValueError('单功能和功能授权组须明确一个标识')
        if scope.get('kind') == 'function':
            key = identifier(scope.get('id'))
            if key not in state['functions']:
                raise ValueError('功能尚未注册')
            return [key]
        if scope.get('kind') == 'group':
            group = state['function_groups'].get(identifier(scope.get('id')))
            if group is None:
                raise ValueError('功能授权组不存在')
            return list(group['functions'])
        if scope.get('kind') == 'all':
            return sorted(state['functions'])
        raise ValueError('功能范围无效')

    @staticmethod
    def _resource_members(state, namespace, scope):
        if not isinstance(scope, dict) or set(scope) - {'kind', 'id', 'ids'}:
            raise ValueError('资源范围须明确选择资源、资源分组或全部资源')
        expected = {'resources': {'kind', 'ids'}, 'group': {'kind', 'id'}, 'all': {'kind'}}
        if scope.get('kind') not in expected or set(scope) != expected[scope['kind']]:
            raise ValueError('资源范围不能混用单个、分组和全部选择')
        if scope.get('kind') == 'resources':
            ids = scope.get('ids')
            if not isinstance(ids, list) or not 1 <= len(ids) <= 500:
                raise ValueError('请选择 1 至 500 个资源标识')
            return list(dict.fromkeys(identifier(key) for key in ids))
        inventory = state['resources'].get(namespace, {})
        if scope.get('kind') == 'group':
            key = identifier(scope.get('id'))
            return [r for r, meta in inventory.items() if key in meta['groups']]
        if scope.get('kind') == 'all':
            return list(inventory)
        raise ValueError('资源范围无效')

    def _source_matches_function(self, state, source, function):
        if source.get('mode', 'snapshot') == 'continuous':
            return function in self._function_members(state, source['function_scope'])
        return function in source.get('functions', [source['function']])

    def _source_matches_resource(self, state, source, namespace, resource):
        if source.get('resource_namespace', source['function']) != namespace:
            return False
        if source.get('mode', 'snapshot') == 'continuous':
            return resource in self._resource_members(state, namespace, source['resource_scope'])
        return resource in source['resources']

    async def _bulk(self, bot, operation, function, group, data):
        members = data.get('members')
        if not isinstance(members, list) or not 1 <= len(members) <= 500:
            raise ValueError('批量操作须提供 1 至 500 个成员对象')
        common = {key: value for key, value in data.items() if key != 'members'}
        results = []
        for target in members:
            try:
                if not isinstance(target, dict) or set(target) - {'member', 'identity_type', 'confirmed'}:
                    raise ValueError('批量目标须为明确的群原生成员对象')
                if target.get('identity_type', 'group_member_openid') != 'group_member_openid':
                    raise ValueError('批量目标须使用 group_member_openid 类型')
                native = identifier(target.get('member'))
                values = {**common, **{k: v for k, v in target.items() if k != 'member'}}
                if operation == 'bulk_verify':
                    values.setdefault('identity_type', 'group_member_openid')
                if operation not in {'bulk_grant', 'bulk_verify'}:
                    values.pop('identity_type', None)
                    values.pop('confirmed', None)
                result = await self.execute(
                    bot,
                    {'bulk_grant': 'grant', 'bulk_revoke': 'revoke', 'bulk_expiry': 'set_expiry', 'bulk_verify': 'verify'}[operation],
                    function=function,
                    group=group,
                    member=native,
                    **values,
                )
                results.append({'member': native, 'status': 'saved', 'result': result})
            except (ValueError, RuntimeError, sqlite3.Error, OSError) as error:
                results.append({'member': target.get('member', '') if isinstance(target, dict) else '', 'status': 'failed', 'error': str(error)})
        success = sum(r['status'] == 'saved' for r in results)
        return {'items': results, 'summary': {'success': success, 'failed': len(results) - success}}

    async def execute(self, bot, operation, *, function, group='', member='', **data):
        if operation in {'verify_auto', 'roster_sync'}:
            if data:
                raise ValueError('自动核验不接受手工结论或额外字段')
            identifier(group)
            return await self.revalidate_member(bot, group, identifier(member)) if operation == 'verify_auto' else await self.sync_roster(bot, group)
        if operation in {'bulk_grant', 'bulk_revoke', 'bulk_expiry', 'bulk_verify'}:
            return await self._bulk(bot, operation, function, identifier(group), data)
        allowed_fields = {
            'whitelist': {'enabled'},
            'function_group': {'id', 'functions'},
            'members': set(),
            'verify': {'identity_type', 'confirmed', 'state', 'evidence', 'actor'},
            'grant': {'resources', 'identity_type', 'confirmed', 'invoke', 'function_scope', 'resource_scope', 'resource_namespace', 'mode', 'expires_at'},
            'revoke': {'source'},
            'set_expiry': {'source', 'expires_at'},
            'deny': {'scope', 'function_scope'},
            'allow': {'scope', 'function_scope', 'source'},
            'explain': {'resource', 'framework_allowed', 'resource_namespace', 'action'},
        }
        if operation not in allowed_fields or set(data) - allowed_fields[operation]:
            raise ValueError('未知授权操作或字段')
        identifier(function)
        if operation != 'function_group':
            identifier(group)
        state = await self._state(bot)
        if operation == 'members':
            return copy.deepcopy(
                {
                    'members': [m for m in state['members'].values() if m['group'] == group and (not member or m['member'] == member)],
                    'revocations': [g for g in state['revocations'] if g['group'] == group and (not member or g['member'] == member)],
                    'group_version': state['group_versions'].get(group, 0),
                }
            )
        if operation == 'explain':
            return await self.decide(
                bot,
                function,
                group,
                identifier(member),
                data.get('resource', ''),
                framework_allowed=data.get('framework_allowed', True),
                resource_namespace=data.get('resource_namespace'),
                action=data.get('action', 'view'),
            )
        expected_version = state['version']
        async with self.locks.setdefault(bot, asyncio.Lock()):
            if operation in {'grant', 'verify'} and state['version'] != expected_version:
                raise ValueError('成员或授权状态已变化，请重新核对后授权')
            source = None
            candidate_functions = {**state['functions'], function: {'id': function}}
            if operation == 'function_group':
                group_id = identifier(data.get('id'))
                functions = data.get('functions')
                if not isinstance(functions, list) or len(functions) > 100 or any(not isinstance(f, str) or f not in candidate_functions for f in functions):
                    raise ValueError('功能授权组仅可选择已注册功能')
                state['function_groups'][group_id] = {'id': group_id, 'functions': list(dict.fromkeys(functions))}
            elif operation == 'whitelist':
                if not isinstance(data.get('enabled'), bool):
                    raise ValueError('白名单 enabled 须为布尔值')
                state['whitelist'][identity_key(group, function)] = data['enabled']
            elif operation == 'grant':
                identifier(member)
                if data.get('identity_type') != 'group_member_openid' or data.get('confirmed') is not True:
                    raise ValueError('须明确 group_member_openid 类型并由主人确认成员当前在群')
                mode = data.get('mode', 'snapshot')
                if mode not in {'snapshot', 'continuous'}:
                    raise ValueError('请选择一次 snapshot 或包含未来范围的 continuous 授权')
                if 'resource_scope' in data and 'resources' in data:
                    raise ValueError('请只选择 resources 或 resource_scope 一种资源表达')
                function_scope = copy.deepcopy(data.get('function_scope', {'kind': 'function', 'id': function}))
                resource_scope = copy.deepcopy(data.get('resource_scope', {'kind': 'resources', 'ids': data.get('resources')}))
                namespace = identifier(data.get('resource_namespace', function))
                functions = self._function_members({**state, 'functions': candidate_functions}, function_scope)
                resources = self._resource_members(state, namespace, resource_scope)
                if not functions or (mode == 'snapshot' and not resources):
                    raise ValueError('一次授权须包含当前功能及资源；未来空范围须明确选择持续授权')
                invoke = data.get('invoke', True)
                if not isinstance(invoke, bool):
                    raise ValueError('invoke 须为布尔值')
                expires_at = expiry(data.get('expires_at'))
                key = identity_key(group, member)
                membership = state['members'].setdefault(key, {'group': group, 'member': member, 'cycle': uuid4().hex, 'status': 'unverified'})
                if membership.get('status') in {'left', 'pending'}:
                    membership.pop('platform_joined_at', None)
                    self._retire_sources(state, group, member, 'owner_new_cycle')
                    membership['cycle'] = uuid4().hex
                membership.update(status='present', verified_by='owner', verified_at=self.clock(), verification_reason='owner_confirmed')
                source = {
                    'id': uuid4().hex,
                    'group': group,
                    'member': member,
                    'cycle': membership['cycle'],
                    'function': function,
                    'invoke': invoke,
                    'expires_at': expires_at,
                    'resources': resources,
                    'functions': functions,
                    'function_scope': function_scope,
                    'resource_scope': resource_scope,
                    'resource_namespace': namespace,
                    'mode': mode,
                    'kind': 'continuous'
                    if mode == 'continuous'
                    else 'direct'
                    if function_scope['kind'] == 'function' and resource_scope['kind'] == 'resources'
                    else 'snapshot',
                }
                state['grants'].append(source)
            elif operation == 'verify':
                identifier(member)
                if data.get('identity_type') != 'group_member_openid' or data.get('confirmed') is not True or data.get('state') not in {'present', 'absent'}:
                    raise ValueError('核验须明确群原生身份、confirmed:true 及 present/absent 结果')
                evidence = data.get('evidence')
                if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 1000:
                    raise ValueError('请记录核验依据，确认同一入群周期（最多 1000 字）')
                actor = identifier(data.get('actor'))
                membership = state['members'].get(identity_key(group, member))
                if not membership:
                    raise ValueError('没有可核验的成员记录；新成员请先明确授权')
                if data['state'] == 'present':
                    if membership.get('status') in {'left', 'unverified'}:
                        raise ValueError('旧周期已结束或尚未授权，请明确重新授权')
                    membership.update(status='present', verified_by=actor, verified_at=self.clock(), verification_reason='owner_verified')
                else:
                    self._retire_sources(state, group, member, 'verified_absent')
                    membership.update(status='left', verification_reason='verified_absent', left_at=self.clock())
                source = {
                    'id': uuid4().hex,
                    'group': group,
                    'member': member,
                    'actor': actor,
                    'at': self.clock(),
                    'state': data['state'],
                    'evidence': evidence,
                    'cycle': membership['cycle'],
                }
                state['verifications'].append(source)
            elif operation == 'set_expiry':
                identifier(member)
                if 'expires_at' not in data:
                    raise ValueError('须明确 expires_at，长期请设置 null')
                expires_at = expiry(data['expires_at'])
                targets = [
                    g
                    for g in state['grants']
                    if g['group'] == group and g['member'] == member and g['function'] == function and (not data.get('source') or g['id'] == data['source'])
                ]
                if not targets:
                    raise ValueError('没有可修改的授权来源')
                for target_source in targets:
                    target_source['expires_at'] = expires_at
                source = {'updated': len(targets), 'sources': copy.deepcopy(targets)}
            elif operation == 'revoke':
                identifier(member)
                state['grants'] = [
                    g
                    for g in state['grants']
                    if not (
                        g['group'] == group and g['member'] == member and g['function'] == function and (not data.get('source') or g['id'] == data['source'])
                    )
                ]
            elif operation in {'deny', 'allow'}:
                scope = data.get('scope')
                if scope not in {'group', 'member'}:
                    raise ValueError('拒绝范围须为 group 或 member')
                target = identifier(member) if scope == 'member' else ''
                function_scope = copy.deepcopy(data.get('function_scope', {'kind': 'function', 'id': function}))
                self._function_members({**state, 'functions': candidate_functions}, function_scope)
                matching = [
                    d
                    for d in state['denies']
                    if d['group'] == group
                    and d['member'] == target
                    and d['function'] == function
                    and (
                        d.get('id') == data['source']
                        if data.get('source')
                        else d.get('function_scope', {'kind': 'function', 'id': d['function']}) == function_scope
                    )
                ]
                if operation == 'deny':
                    source = (
                        matching[0]
                        if matching
                        else {
                            'id': uuid4().hex,
                            'group': group,
                            'member': target,
                            'function': function,
                            'function_scope': function_scope,
                        }
                    )
                    if not matching:
                        state['denies'].append(source)
                else:
                    state['denies'] = [d for d in state['denies'] if d not in matching]
            state['functions'].setdefault(function, {'id': function})
            state['version'] += 1
            await self._save(bot, state, qualification_group=None if operation == 'function_group' else group)
            log.info(f'[{bot}] {operation} group={group} member={member} version={state["version"]}')
            return copy.deepcopy(source) if source else {'version': state['version']}

    async def revalidate_member(self, bot, group, member):
        state = await self._state(bot)
        membership = state['members'].get(identity_key(group, member))
        if not membership:
            raise ValueError('没有可核验的成员记录')
        version, cycle = state['version'], membership['cycle']
        result = await self.reader.member(bot, group, member) if self.reader else {'state': 'unknown', 'reason': 'unavailable'}
        async with self.locks.setdefault(bot, asyncio.Lock()):
            if state['version'] != version or membership['cycle'] != cycle:
                return {'state': 'unknown', 'reason': 'state_changed'}
            if result['state'] == 'present':
                joined = result['joined_at']
                anchor = membership.get('platform_joined_at')
                same_cycle = joined == anchor if anchor is not None else joined <= membership.get('verified_at', 0)
                if membership.get('status') in {'left', 'unverified'} or not same_cycle:
                    self._retire_sources(state, group, member, 'new_membership_cycle')
                    membership.update(status='unverified', cycle=uuid4().hex, verification_reason='new_membership_cycle')
                    membership['platform_joined_at'] = joined
                    result.update(state='new_cycle', reason='new_membership_cycle')
                else:
                    membership.update(
                        status='present',
                        verification_reason='platform_verified',
                        platform_joined_at=joined,
                        name=result.get('name', ''),
                        verified_by='platform',
                    )
            elif membership.get('status') != 'left':
                membership.update(status='pending', verification_reason=result['reason'])
            record = {'id': uuid4().hex, 'group': group, 'member': member, 'actor': 'platform', 'at': self.clock(), **result}
            state['verifications'].append(record)
            state['version'] += 1
            await self._save(bot, state, qualification_group=group)
            return copy.deepcopy(record)

    async def sync_roster(self, bot, group):
        async with self.roster_locks.setdefault((bot, group), asyncio.Lock()):
            state = await self._state(bot)
            version = state['version']
            result = await self.reader.roster(bot, group) if self.reader else {'complete': False, 'members': [], 'reason': 'unavailable'}
            async with self.locks.setdefault(bot, asyncio.Lock()):
                if state['version'] != version:
                    result.update(complete=False, reason='state_changed')
                observed = {m['member'] for m in state['members'].values() if m['group'] == group and m.get('status') != 'left'}
                returned = {m['member'] for m in result['members']}
                if result['complete'] and observed - returned:
                    result.update(complete=False, reason='known_member_missing', missing_known_members=sorted(observed - returned))
                result.update(id=uuid4().hex, group=group, checked_at=self.clock(), version=state['version'])
                state['rosters'][group] = copy.deepcopy(result)
                await self._save(bot, state, qualification_change=False)
                return result

    async def decide(self, bot, function, group, member, resource='', *, action='view', framework_allowed=True, resource_namespace=None):
        if action not in {'view', 'invoke'}:
            raise ValueError('授权动作须为 view 或 invoke')
        state = await self._state(bot)
        membership = state['members'].get(identity_key(group, member), {})
        now = self.clock()
        grants = [
            g
            for g in state['grants']
            if g['group'] == group and g['member'] == member and self._source_matches_function(state, g, function) and g['cycle'] == membership.get('cycle')
        ]
        current = [g for g in grants if g.get('expires_at') is None or now < g['expires_at']]
        result = {
            'bot': bot,
            'allowed': False,
            'reason': '',
            'sources': [{**copy.deepcopy(g), 'status': 'active' if g in current else 'expired'} for g in grants],
            'membership': membership.get('status', 'unknown'),
            'cycle': membership.get('cycle'),
            'verification_reason': membership.get('verification_reason', ''),
            'version': state['version'],
            'evaluated_at': now,
            'denies': copy.deepcopy(
                [
                    d
                    for d in state['denies']
                    if d['group'] == group
                    and d['member'] in {'', member}
                    and function in self._function_members(state, d.get('function_scope', {'kind': 'function', 'id': d['function']}))
                ]
            ),
        }
        if not framework_allowed:
            result['reason'] = 'framework_denied'
        elif result['denies']:
            result['reason'] = 'denied'
        elif not state['whitelist'].get(identity_key(group, function), False):
            result['reason'] = 'group_not_allowed'
        elif not self.lifecycle_available:
            result['reason'] = 'event_integration_unavailable'
        elif state['groups'].get(group, {}).get('bot_present') is False:
            result['reason'] = 'bot_not_in_group'
        elif membership.get('status') == 'left':
            result['reason'] = 'left_group'
        elif membership.get('status') != 'present':
            result['reason'] = 'pending_verification'
        elif not any(g['invoke'] for g in current):
            result['reason'] = 'expired' if any(g['invoke'] for g in grants) else 'no_function_permission'
        elif action != 'invoke' and not any(self._source_matches_resource(state, g, resource_namespace or function, resource) for g in current):
            result['reason'] = (
                'expired'
                if any(self._source_matches_resource(state, g, resource_namespace or function, resource) for g in grants)
                else 'no_resource_permission'
            )
        elif action != 'invoke' and not state['resources'].get(resource_namespace or function, {}).get(resource, {}).get('enabled', True):
            result['reason'] = 'resource_disabled'
        else:
            result.update(allowed=True, reason='allowed')
            invoke_until = max(g.get('expires_at') if g.get('expires_at') is not None else math.inf for g in current if g['invoke'])
            resource_until = (
                math.inf
                if action == 'invoke'
                else max(
                    g.get('expires_at') if g.get('expires_at') is not None else math.inf
                    for g in current
                    if self._source_matches_resource(state, g, resource_namespace or function, resource)
                )
            )
            until = min(invoke_until, resource_until)
            result['valid_until'] = None if math.isinf(until) else until
        return result

    def decision_current(self, bot, decision):
        """Non-awaiting final gate after asynchronous consumer work, before dispatch."""
        state = self.states.get(bot)
        return bool(
            self.active
            and self.healthy
            and self.lifecycle_available
            and state
            and decision['allowed']
            and decision.get('bot') == bot
            and state['version'] == decision['version']
            and (decision.get('valid_until') is None or self.clock() < decision['valid_until'])
        )

    def roster_current(self, bot, group, version, roster_id):
        """A sync round is usable only in the same known authorization state."""
        state = self.states.get(bot, {})
        roster = state.get('rosters', {}).get(group, {})
        return bool(
            self.active
            and self.healthy
            and self.lifecycle_available
            and state.get('version') == version
            and roster.get('complete')
            and roster.get('id') == roster_id
            and roster.get('version') == version
        )

    def qualification_revision(self, bot, group):
        """Mode continuity marker; it never authorizes sending without fresh decisions."""
        state = self.states.get(bot)
        return (
            [state.get('qualification_version'), state.get('qualification_groups', {}).get(group, 0)]
            if self.active and self.healthy and self.lifecycle_available and state
            else None
        )

    async def subject_identity(self, bot, group, member):
        state = await self._state(bot)
        return copy.deepcopy(state['members'].get(identity_key(group, member), {}))

    async def subjects(self, bot, function, *, resource_namespace=None):
        """Current scoped inventory for management, including continuous future resources."""
        state = await self._state(bot)
        namespace = resource_namespace or function
        subjects = {}
        for source in state['grants']:
            membership = state['members'].get(identity_key(source['group'], source['member']), {})
            if (
                source['cycle'] != membership.get('cycle')
                or not self._source_matches_function(state, source, function)
                or source.get('resource_namespace', source['function']) != namespace
                or (source.get('expires_at') is not None and self.clock() >= source['expires_at'])
            ):
                continue
            key = identity_key(source['group'], source['member'])
            subject = subjects.setdefault(key, {'group': source['group'], 'member': source['member'], 'resources': []})
            for resource in state['resources'].get(namespace, {}):
                if resource not in subject['resources'] and self._source_matches_resource(state, source, namespace, resource):
                    subject['resources'].append(resource)
        return list(subjects.values())

    def _retire_sources(self, state, group, member, reason):
        removed = [g for g in state['grants'] if g['group'] == group and g['member'] == member]
        state['revocations'].extend({**copy.deepcopy(g), 'revoked_at': self.clock(), 'reason': reason} for g in removed)
        state['grants'] = [g for g in state['grants'] if g not in removed]

    async def _observe_membership(self, event):
        group = event.get('d/group_openid')
        if not isinstance(group, str) or not group:
            return
        state = await self._state(event.appid)
        member = event.get('d/member_openid')
        if not isinstance(member, str) or not member:
            for membership in state['members'].values():
                if membership['group'] == group and membership.get('status') != 'left':
                    membership.update(status='pending', verification_reason='native_identity_unavailable')
        else:
            new_member = identity_key(group, member) not in state['members']
            membership = state['members'].setdefault(
                identity_key(group, member), {'group': group, 'member': member, 'cycle': uuid4().hex, 'status': 'unverified'}
            )
            timestamp = event.get('d/timestamp')
            valid_time = isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool) and 0 <= timestamp <= 253402300799
            if not valid_time:
                timestamp = None
            event_id = event.event_id or event.message_id
            key = event_id or identity_key(event.event_type, timestamp)
            record = {'key': key, 'kind': event.event_type, 'timestamp': timestamp}
            history = membership.setdefault('lifecycle_events', [])
            if event_id and record in history:
                return
            conflict = any(old['key'] == key for old in history)
            history.append(record)
            del history[:-128]
            previous = membership.get('last_lifecycle', {}).get('timestamp', 0)
            anchor = max(previous or 0, membership.get('verified_at', 0))
            if conflict or timestamp is None or timestamp <= anchor:
                if new_member and event.event_type == 'GROUP_MEMBER_ADD':
                    membership.update(status='unverified', verification_reason='new_membership_cycle')
                else:
                    membership.update(status='pending', verification_reason='ambiguous_lifecycle')
            else:
                membership['last_lifecycle'] = record
                self._retire_sources(state, group, member, 'member_left' if event.event_type == 'GROUP_MEMBER_REMOVE' else 'new_membership_cycle')
                if event.event_type == 'GROUP_MEMBER_REMOVE':
                    membership.update(status='left', verification_reason='member_left', left_at=timestamp)
                else:
                    membership.pop('platform_joined_at', None)
                    membership.update(cycle=uuid4().hex, status='unverified', verification_reason='new_membership_cycle', joined_at=timestamp)
        # All in-memory eligibility changes happen without a durable await, even if
        # another owner mutation currently holds the persistence lock.
        state['version'] += 1
        state['group_versions'][group] = state['group_versions'].get(group, 0) + 1
        state['qualification_groups'][group] = state['version']
        async with self.locks.setdefault(event.appid, asyncio.Lock()):
            await self._save(event.appid, state, qualification_group=group)

    async def suspend(self, bot, *, reason, group=''):
        """Trusted adapter entry point; reconnection alone cannot undo suspension."""
        identifier(reason)
        if group:
            identifier(group)
        state = await self._state(bot)
        for membership in state['members'].values():
            if (not group or membership['group'] == group) and membership.get('status') != 'left':
                membership.update(status='pending', verification_reason=reason)
        for key, roster in state['rosters'].items():
            if not group or key == group:
                roster.update(complete=False, reason=reason)
        state['version'] += 1
        if group:
            state['qualification_groups'][group] = state['version']
        else:
            state['qualification_version'] = state['version']
        async with self.locks.setdefault(bot, asyncio.Lock()):
            await self._save(bot, state, qualification_group=group)

    async def _observe_bot_membership(self, event):
        group = event.get('d/group_openid')
        if not isinstance(group, str) or not group:
            await self.suspend(event.appid, reason='bot_group_identity_unknown')
            return
        state = await self._state(event.appid)
        status = state['groups'].setdefault(group, {'events': []})
        event_id = event.event_id or event.message_id
        record = {'id': event_id, 'kind': event.event_type, 'time': event.get('d/timestamp')}
        if event_id and record in status['events']:
            return
        status['events'].append(record)
        del status['events'][:-128]
        removed = event.event_type == 'GROUP_DEL_ROBOT'
        reason = 'bot_removed' if removed else 'bot_rejoined_requires_new_grants'
        status.update(bot_present=not removed, reason=reason)
        for membership in state['members'].values():
            if membership['group'] == group:
                self._retire_sources(state, group, membership['member'], reason)
        await self.suspend(event.appid, group=group, reason=reason)

    async def observe(self, event):
        if event.event_type in {'GROUP_DEL_ROBOT', 'GROUP_ADD_ROBOT'}:
            await self._observe_bot_membership(event)
            return
        if event.event_type in {'GROUP_MEMBER_ADD', 'GROUP_MEMBER_REMOVE'}:
            await self._observe_membership(event)
            return
        if not event.group_id or not event.raw_user_id:
            return
        state = await self._state(event.appid)
        async with self.locks.setdefault(event.appid, asyncio.Lock()):
            key = identity_key(event.group_id, event.raw_user_id)
            changed = key not in state['members']
            qualification_changed = changed
            membership = state['members'].setdefault(
                key, {'group': event.group_id, 'member': event.raw_user_id, 'name': event.username or '', 'cycle': uuid4().hex, 'status': 'unverified'}
            )
            # A platform group-message author supplies this relation. It is not a C2C binding.
            if event.event_type in {'GROUP_AT_MESSAGE_CREATE', 'GROUP_MESSAGE_CREATE'}:
                if not membership.get('identity_observed'):
                    membership['identity_observed'] = True
                    changed = True
                union_id = event.get('d/author/union_openid')
                if isinstance(union_id, str) and 0 < len(union_id) <= 256 and union_id != event.raw_user_id:
                    aliases = membership.setdefault('union_ids', [])
                    if union_id not in aliases:
                        aliases.append(union_id)
                        changed = True
                        qualification_changed = True
                    if membership.get('current_union_id') != union_id:
                        membership['current_union_id'] = union_id
                        changed = True
                        qualification_changed = True
                elif membership.get('current_union_id') != event.raw_user_id:
                    qualification_changed |= bool(membership.get('current_union_id'))
                    membership['current_union_id'] = event.raw_user_id
                    changed = True
            if changed:
                state['version'] += 1
                await self._save(event.appid, state, qualification_change=qualification_changed, qualification_group=event.group_id)
