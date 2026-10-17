"""Reusable Bot/group/native-member authorization; no account data or QQ sends."""

import asyncio
import copy
import json
import sqlite3
import time
from uuid import uuid4

from core.base.logger import EXTENSION, get_logger
from core.storage.extension import ExtensionStore

log = get_logger(EXTENSION, '黑白名单授权')


def identity_key(*parts):
    return json.dumps(parts, ensure_ascii=False)


def identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError('请提供明确的 Bot、功能、群和原生身份标识')
    return value


class AccessControl:
    """decide returns a reason/source/state/version, never consumer credentials.

    Owners explicitly confirm group-native identities on grant. Group admission,
    invoke capability, resource permission and member verification are independent.
    Mutations invalidate versions before persistence; storage failure fails closed.
    """

    def __init__(self, path, *, clock=time.time):
        self.store = ExtensionStore(path)
        self.clock = clock
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
                    self.states[bot] = await self.store.get(bot, 'access', 'state') or {
                        'version': 0,
                        'whitelist': {},
                        'members': {},
                        'grants': [],
                        'denies': [],
                    }
        state = self.states[bot]
        if state.get('schema_version', 1) > 2:
            raise RuntimeError('授权数据版本高于当前模块')
        state['schema_version'] = 2
        state.setdefault('functions', {})
        state.setdefault('function_groups', {})
        state.setdefault('resources', {})
        for grant in state['grants']:
            state['functions'].setdefault(grant['function'], {'id': grant['function']})
        for key in state['whitelist']:
            function = json.loads(key)[1]
            state['functions'].setdefault(function, {'id': function})
        return state

    async def snapshot(self, bot):
        return copy.deepcopy(await self._state(bot))

    async def _save(self, bot, state):
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
                if operation == 'bulk_revoke':
                    values.pop('identity_type', None)
                    values.pop('confirmed', None)
                result = await self.execute(bot, 'grant' if operation == 'bulk_grant' else 'revoke', function=function, group=group, member=native, **values)
                results.append({'member': native, 'status': 'saved', 'result': result})
            except (ValueError, RuntimeError, sqlite3.Error, OSError) as error:
                results.append({'member': target.get('member', '') if isinstance(target, dict) else '', 'status': 'failed', 'error': str(error)})
        success = sum(r['status'] == 'saved' for r in results)
        return {'items': results, 'summary': {'success': success, 'failed': len(results) - success}}

    async def execute(self, bot, operation, *, function, group='', member='', **data):
        if operation in {'bulk_grant', 'bulk_revoke'}:
            return await self._bulk(bot, operation, function, identifier(group), data)
        allowed_fields = {
            'whitelist': {'enabled'},
            'function_group': {'id', 'functions'},
            'grant': {'resources', 'identity_type', 'confirmed', 'invoke', 'function_scope', 'resource_scope', 'resource_namespace', 'mode'},
            'revoke': {'source'},
            'deny': {'scope'},
            'allow': {'scope'},
            'explain': {'resource', 'framework_allowed', 'resource_namespace'},
        }
        if operation not in allowed_fields or set(data) - allowed_fields[operation]:
            raise ValueError('未知授权操作或字段')
        identifier(function)
        if operation != 'function_group':
            identifier(group)
        state = await self._state(bot)
        if operation == 'explain':
            return await self.decide(
                bot,
                function,
                group,
                identifier(member),
                data.get('resource', ''),
                framework_allowed=data.get('framework_allowed', True),
                resource_namespace=data.get('resource_namespace'),
            )
        async with self.locks.setdefault(bot, asyncio.Lock()):
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
                key = identity_key(group, member)
                membership = state['members'].setdefault(key, {'group': group, 'member': member, 'cycle': uuid4().hex})
                membership.update(status='present', verified_by='owner', verified_at=self.clock())
                source = {
                    'id': uuid4().hex,
                    'group': group,
                    'member': member,
                    'cycle': membership['cycle'],
                    'function': function,
                    'invoke': invoke,
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
                deny = {'group': group, 'member': target, 'function': function}
                if operation == 'deny' and deny not in state['denies']:
                    state['denies'].append(deny)
                if operation == 'allow':
                    state['denies'] = [d for d in state['denies'] if d != deny]
            state['functions'].setdefault(function, {'id': function})
            state['version'] += 1
            await self._save(bot, state)
            log.info(f'[{bot}] {operation} group={group} member={member} version={state["version"]}')
            return copy.deepcopy(source) if source else {'version': state['version']}

    async def decide(self, bot, function, group, member, resource='', *, action='view', framework_allowed=True, resource_namespace=None):
        state = await self._state(bot)
        membership = state['members'].get(identity_key(group, member), {})
        result = {'allowed': False, 'reason': '', 'sources': [], 'membership': membership.get('status', 'unknown'), 'version': state['version']}
        if not framework_allowed:
            result['reason'] = 'framework_denied'
        elif any(d['group'] == group and d['function'] == function and d['member'] in {'', member} for d in state['denies']):
            result['reason'] = 'denied'
        elif not state['whitelist'].get(identity_key(group, function), False):
            result['reason'] = 'group_not_allowed'
        elif membership.get('status') != 'present':
            result['reason'] = 'pending_verification'
        else:
            grants = [
                g
                for g in state['grants']
                if g['group'] == group and g['member'] == member and self._source_matches_function(state, g, function) and g['cycle'] == membership.get('cycle')
            ]
            if not any(g['invoke'] for g in grants):
                result['reason'] = 'no_function_permission'
            elif action != 'invoke' and not any(self._source_matches_resource(state, g, resource_namespace or function, resource) for g in grants):
                result['reason'] = 'no_resource_permission'
            elif action != 'invoke' and not state['resources'].get(resource_namespace or function, {}).get(resource, {}).get('enabled', True):
                result['reason'] = 'resource_disabled'
            else:
                result.update(allowed=True, reason='allowed', sources=copy.deepcopy(grants))
        return result

    async def subject_identity(self, bot, group, member):
        state = await self._state(bot)
        return copy.deepcopy(state['members'].get(identity_key(group, member), {}))

    async def observe(self, event):
        if not event.group_id or not event.raw_user_id:
            return
        state = await self._state(event.appid)
        async with self.locks.setdefault(event.appid, asyncio.Lock()):
            key = identity_key(event.group_id, event.raw_user_id)
            changed = key not in state['members']
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
                    if membership.get('current_union_id') != union_id:
                        membership['current_union_id'] = union_id
                        changed = True
                elif membership.get('current_union_id') != event.raw_user_id:
                    membership['current_union_id'] = event.raw_user_id
                    changed = True
            if changed:
                state['version'] += 1
                await self._save(event.appid, state)
