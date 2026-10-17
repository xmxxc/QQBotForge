"""Reusable Bot/group/native-member authorization; no account data or QQ sends."""

import asyncio
import copy
import json
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
        return self.states[bot]

    async def snapshot(self, bot):
        return copy.deepcopy(await self._state(bot))

    async def _save(self, bot, state):
        try:
            await self.store.put(bot, 'access', 'state', state)
        except Exception:
            self.healthy = False
            raise

    async def execute(self, bot, operation, *, function, group, member='', **data):
        allowed_fields = {
            'whitelist': {'enabled'},
            'grant': {'resources', 'identity_type', 'confirmed', 'invoke'},
            'revoke': {'source'},
            'deny': {'scope'},
            'allow': {'scope'},
            'explain': {'resource', 'framework_allowed'},
        }
        if operation not in allowed_fields or set(data) - allowed_fields[operation]:
            raise ValueError('未知授权操作或字段')
        identifier(function)
        identifier(group)
        state = await self._state(bot)
        if operation == 'explain':
            return await self.decide(bot, function, group, identifier(member), data.get('resource', ''), framework_allowed=data.get('framework_allowed', True))
        async with self.locks.setdefault(bot, asyncio.Lock()):
            source = None
            if operation == 'whitelist':
                if not isinstance(data.get('enabled'), bool):
                    raise ValueError('白名单 enabled 须为布尔值')
                state['whitelist'][identity_key(group, function)] = data['enabled']
            elif operation == 'grant':
                identifier(member)
                if data.get('identity_type') != 'group_member_openid' or data.get('confirmed') is not True:
                    raise ValueError('须明确 group_member_openid 类型并由主人确认成员当前在群')
                resources = data.get('resources')
                if not isinstance(resources, list) or not resources or len(resources) > 500 or any(not isinstance(r, str) or not r for r in resources):
                    raise ValueError('请选择明确的资源标识列表')
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
                    'resources': list(dict.fromkeys(resources)),
                    'kind': 'direct',
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
            else:
                raise ValueError('未知授权操作')
            state['version'] += 1
            await self._save(bot, state)
            log.info(f'[{bot}] {operation} group={group} member={member} version={state["version"]}')
            return copy.deepcopy(source) if source else {'version': state['version']}

    async def decide(self, bot, function, group, member, resource='', *, action='view', framework_allowed=True):
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
                if g['group'] == group and g['member'] == member and g['function'] == function and g['cycle'] == membership.get('cycle')
            ]
            if not any(g['invoke'] for g in grants):
                result['reason'] = 'no_function_permission'
            elif action != 'invoke' and not any(resource in g['resources'] for g in grants):
                result['reason'] = 'no_resource_permission'
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
