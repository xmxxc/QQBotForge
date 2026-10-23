"""Reliable identity association is shared; consumer qualification never is."""

import asyncio
import copy
import sqlite3
from uuid import uuid4

from core.plugin._blacklist import get_blacklist_map

from .policy import identifier, identity_key


class Bindings:
    def __init__(self, access):
        self.access = access
        self.states = {}
        self.failed = set()
        self.pending = set()
        self.locks = {}

    @staticmethod
    def validate(state):
        if (
            not isinstance(state, dict)
            or state.get('schema_version') != 1
            or type(state.get('version')) is not int
            or any(not isinstance(state.get(k), dict) for k in ('private', 'bindings', 'requests'))
        ):
            raise ValueError('身份资料结构不兼容')
        for key, record in state['private'].items():
            identifier(key)
            if not isinstance(record, dict) or record.get('private_id') != key or record.get('identity_observed') is not True:
                raise ValueError('私聊观察资料无效')
        pairs = set()
        for field in ('bindings', 'requests'):
            for key, record in state[field].items():
                if not isinstance(record, dict):
                    raise ValueError('身份关联记录无效')
                for name in ('private_id', 'group', 'member', 'id'):
                    identifier(record.get(name))
                pair = record['group'], record['private_id']
                if field == 'bindings' and pair in pairs:
                    raise ValueError('同群私聊身份不能关联多个成员')
                if field == 'bindings':
                    pairs.add(pair)
                if field == 'bindings' and record['private_id'] not in state['private']:
                    raise ValueError('绑定缺少已观察私聊身份')
                if field == 'bindings' and (
                    key != identity_key(record['group'], record['member'])
                    or record.get('source') != 'owner_confirmed'
                    or not record.get('evidence')
                    or not record.get('confirmed_by')
                ):
                    raise ValueError('绑定须有主人核验依据')
                if field == 'requests' and (key != record['id'] or record.get('status') not in {'pending', 'confirmed', 'rejected', 'revoked'}):
                    raise ValueError('申请状态无效')

        if not isinstance(state.get('revocation_history', {}), dict):
            raise ValueError('身份撤销档案结构无效')
        for key, record in state.get('revocation_history', {}).items():
            if not isinstance(record, dict) or record.get('id') != key:
                raise ValueError('身份撤销档案ID无效')
        if not isinstance(state.get('revoked', {}), dict):
            raise ValueError('身份撤销历史结构无效')

    async def state(self, bot):
        identifier(bot)
        if not self.access.active or bot in self.failed or self.access.shared_failure:
            raise RuntimeError('该Bot身份资料暂不可用')
        if bot not in self.states:
            async with self.locks.setdefault(bot, asyncio.Lock()):
                if bot not in self.states:
                    try:
                        missing = object()
                        row = await self.access.store.get(bot, 'identity_state', 'state', missing=missing)
                        state = row if row is not missing else {'schema_version': 1, 'version': 0, 'private': {}, 'bindings': {}, 'requests': {}}
                        if not isinstance(state, dict):
                            raise ValueError('身份资料记录无效')
                        self.validate(state)
                        state.setdefault('revoked', {})
                        state.setdefault(
                            'revocation_history',
                            {record['id']: copy.deepcopy(record) for record in state['revoked'].values() if isinstance(record, dict) and record.get('id')},
                        )
                        self.states[bot] = state
                    except BaseException as error:
                        self.failed.add(bot)
                        if isinstance(error, (sqlite3.OperationalError, OSError)):
                            self.access.shared_failure = True
                        raise
        return self.states[bot]

    async def save(self, bot, state):
        state['version'] += 1
        self.pending.add(bot)
        try:
            await self.access.store.put(bot, 'identity_state', 'state', state)
        except BaseException as error:
            self.failed.add(bot)
            if isinstance(error, (sqlite3.OperationalError, OSError)):
                self.access.shared_failure = True
            raise
        finally:
            self.pending.discard(bot)

    async def observe(self, event):
        if event.event_type != 'C2C_MESSAGE_CREATE':
            return
        # Only the real native C2C author is an observation. A request body or
        # equal group/QQ/union strings cannot manufacture one.
        private_id = identifier(event.get('d/author/id'))
        state = await self.state(event.appid)
        async with self.locks.setdefault(event.appid, asyncio.Lock()):
            old = state['private'].get(private_id, {})
            union = event.get('d/author/union_openid')
            union = union if isinstance(union, str) and union.strip() and len(union) <= 256 else None
            if old.get('identity_observed') and old.get('union_id') == union:
                return
            state['private'][private_id] = {
                **old,
                'private_id': private_id,
                'identity_type': 'c2c_openid',
                'identity_observed': True,
                'source': 'observed_c2c',
                'observed_at': self.access.clock(),
                'union_id': union,
            }
            await self.save(event.appid, state)

    async def request(self, bot, private_id, group, member):
        identifier(private_id), identifier(group), identifier(member)
        state = await self.state(bot)
        async with self.locks.setdefault(bot, asyncio.Lock()):
            if not state['private'].get(private_id, {}).get('identity_observed'):
                raise ValueError('须先从真实私聊事件观察该原生身份')
            for record in state['requests'].values():
                if record['private_id'] == private_id and record['group'] == group and record['member'] == member and record['status'] == 'pending':
                    return copy.deepcopy(record)
            record = {
                'id': uuid4().hex,
                'private_id': private_id,
                'group': group,
                'member': member,
                'status': 'pending',
                'created_at': self.access.clock(),
                'source': 'observed_c2c_request',
                'notification': 'not_requested',
            }
            state['requests'][record['id']] = record
            await self.save(bot, state)
            return copy.deepcopy(record)

    async def confirm(self, bot, request_id, *, confirmed, evidence, actor):
        identifier(actor)
        if confirmed is not True or not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 1000:
            raise ValueError('主人须明确 confirmed:true 并记录两侧身份核对依据')
        state = await self.state(bot)
        async with self.locks.setdefault(bot, asyncio.Lock()):
            record = state['requests'].get(identifier(request_id))
            if not record or record['status'] not in {'pending', 'confirmed'}:
                raise ValueError('绑定申请不存在或已拒绝')
            if not state['private'].get(record['private_id'], {}).get('identity_observed'):
                raise ValueError('私聊身份尚未可靠观察')
            proof = await self.access._facts().fact(bot, record['group'], record['member'])
            if not proof['usable'] or proof['member']['status'] != 'present':
                raise ValueError('请先核验群原生成员，绑定不会自动授权')
            key = identity_key(record['group'], record['member'])
            old = state['bindings'].get(key)
            if any(
                value['group'] == record['group'] and value['private_id'] == record['private_id'] and value['member'] != record['member']
                for value in state['bindings'].values()
            ):
                raise ValueError('该私聊身份在此群已有另一成员关联，请先核实并撤销原关联')
            if old and old['private_id'] != record['private_id']:
                raise ValueError('该群成员已关联另一私聊身份，请先明确撤销原绑定')
            binding = {k: record[k] for k in ('private_id', 'group', 'member')}
            binding.update(
                id=old['id'] if old else uuid4().hex, source='owner_confirmed', confirmed_by=actor, confirmed_at=self.access.clock(), evidence=evidence
            )
            state['bindings'][key] = binding
            record.update(status='confirmed', confirmed_by=actor, confirmed_at=binding['confirmed_at'], evidence=evidence)
            await self.save(bot, state)
            return copy.deepcopy(binding)

    async def delivery(self, bot, request_id, status, error=None):
        state = await self.state(bot)
        async with self.locks.setdefault(bot, asyncio.Lock()):
            state['requests'][request_id]['notification'] = status
            state['requests'][request_id]['notification_error'] = error
            await self.save(bot, state)

    async def import_history(self, bot, bundle):
        if (
            not isinstance(bundle, dict)
            or set(bundle) != {'private_identities', 'bindings', 'binding_requests'}
            or any(not isinstance(records, list) for records in bundle.values())
        ):
            raise ValueError('迁移须提供明确的三类身份记录列表')
        state = await self.state(bot)
        async with self.locks.setdefault(bot, asyncio.Lock()):
            candidate = copy.deepcopy(state)
            imported = {'private_identities': 0, 'bindings': 0, 'requests': 0}
            existing = dict(imported)
            # Records already present are kept: only the same semantic identity
            # is compatible, never whole-record equality across formats.
            for record in bundle['private_identities']:
                if not isinstance(record, dict) or record.get('identity_observed') is not True or record.get('source') != 'observed_c2c':
                    raise ValueError('迁移私聊身份须有原观察来源')
                key = identifier(record.get('private_id'))
                if key in candidate['private']:
                    # Same Bot, same native C2C author: one observation. Legacy
                    # union aliases stay source metadata and never alter it.
                    if candidate['private'][key].get('source') != 'observed_c2c':
                        raise ValueError('迁移私聊身份与现存记录冲突')
                    existing['private_identities'] += 1
                    continue
                candidate['private'][key] = copy.deepcopy(record)
                imported['private_identities'] += 1
            for record in bundle['bindings']:
                if not isinstance(record, dict):
                    raise ValueError('绑定迁移结构无效')
                key = identity_key(identifier(record.get('group')), identifier(record.get('member')))
                private_id = identifier(record.get('private_id'))
                if key in candidate.get('revoked', {}):
                    raise ValueError('该关联已撤销，历史迁移不能恢复，须新申请并由主人核实')
                if private_id not in candidate['private']:
                    raise ValueError('旧绑定缺少已观察私聊身份')
                if key in candidate['bindings']:
                    if candidate['bindings'][key]['private_id'] != private_id:
                        raise ValueError('旧绑定与现存关联冲突')
                    existing['bindings'] += 1
                    continue
                candidate['bindings'][key] = {**copy.deepcopy(record), 'id': record.get('id', key)}
                imported['bindings'] += 1
            for record in bundle['binding_requests']:
                if not isinstance(record, dict):
                    raise ValueError('旧申请结构无效')
                key = identifier(record.get('id'))
                if key in candidate['requests']:
                    # Later status (confirmed, rejected, revoked) wins over history.
                    if any(candidate['requests'][key].get(name) != record.get(name) for name in ('private_id', 'group', 'member')):
                        raise ValueError('旧申请ID冲突')
                    existing['requests'] += 1
                    continue
                candidate['requests'][key] = copy.deepcopy(record)
                imported['requests'] += 1
            self.validate(candidate)
            # No grants, denies or membership facts are imported by this operation.
            if any(imported.values()):
                state.update(candidate)
                await self.save(bot, state)
            return {
                'status': 'imported' if any(imported.values()) else 'unchanged',
                'bindings': len(candidate['bindings']),
                'requests': len(candidate['requests']),
                'imported': imported,
                'existing': existing,
            }

    async def manage(self, bot, operation, data, actor):
        if operation == 'binding_confirm':
            if set(data) != {'request', 'confirmed', 'evidence'}:
                raise ValueError('绑定确认字段无效')
            return await self.confirm(bot, data['request'], confirmed=data['confirmed'], evidence=data['evidence'], actor=actor)
        state = await self.state(bot)
        if operation == 'binding_list':
            if data:
                raise ValueError('身份查看不接受额外字段')
            return {
                'version': state['version'],
                'private_identities': copy.deepcopy(list(state['private'].values())),
                'bindings': copy.deepcopy(list(state['bindings'].values())),
                'requests': copy.deepcopy(list(state['requests'].values())),
                'revocations': copy.deepcopy(list(state.get('revocation_history', {}).values())),
            }
        if operation not in {'binding_revoke', 'binding_reject'}:
            raise ValueError('未知身份管理操作')
        async with self.locks.setdefault(bot, asyncio.Lock()):
            if operation == 'binding_revoke':
                if set(data) != {'group', 'member'}:
                    raise ValueError('绑定撤销须明确群和原生成员')
                key = identity_key(identifier(data['group']), identifier(data['member']))
                old = state['bindings'].pop(key, None)
                revoked = {**(copy.deepcopy(old) if old else data), 'revoked_by': actor, 'revoked_at': self.access.clock()}
                state['revoked'].setdefault(key, revoked)
                if old:
                    state['revoked'][key] = revoked
                    state.setdefault('revocation_history', {})[old['id']] = copy.deepcopy(revoked)
                for request in state['requests'].values():
                    if request['group'] == data['group'] and request['member'] == data['member']:
                        request.update(status='revoked', handled_by=actor)
                result = {'revoked': bool(old)}
            else:
                if set(data) != {'request'}:
                    raise ValueError('拒绝须明确申请')
                record = state['requests'].get(identifier(data['request']))
                if not record or record['status'] != 'pending':
                    raise ValueError('待审申请不存在')
                record.update(status='rejected', handled_by=actor)
                result = copy.deepcopy(record)
            await self.save(bot, state)
            return result

    async def contexts(self, bot, consumer, function, private_id, *, group='', resource='', action='invoke'):
        identifier(private_id)
        try:
            scoped = await self.access._scope(bot, consumer)
        except ValueError:
            return {'candidates': [], 'selected': None, 'reason': 'consumer_unregistered'}
        configured = scoped['functions'].get(function)
        if not configured or not configured['private']:
            return {'candidates': [], 'selected': None, 'reason': 'private_scope_disabled'}
        direct = await self.access.evaluate(bot, consumer, function, '', private_id, resource, action=action)
        if direct['reason'] in {'denied', 'framework_denied'}:
            return {'candidates': [], 'selected': None, 'reason': direct['reason']}
        if direct['allowed']:
            return {'candidates': [], 'selected': {'group': '', 'member': private_id, 'decision': direct}, 'reason': 'public_private'}
        state = await self.state(bot)
        version = state['version']
        candidates = []
        for binding in tuple(state['bindings'].values()):
            if binding['private_id'] != private_id:
                continue
            decision = await self.access.evaluate(
                bot,
                consumer,
                function,
                binding['group'],
                binding['member'],
                resource,
                action=action,
                framework_allowed=private_id not in get_blacklist_map(bot, 'user'),
            )
            if decision['allowed']:
                candidates.append(
                    {
                        'group': binding['group'],
                        'member': binding['member'],
                        'private_id': private_id,
                        'binding_id': binding['id'],
                        'binding_version': version,
                        'decision': decision,
                    }
                )
        candidates = [row for row in candidates if self.current(bot, row)]
        counts: dict[str, int] = {}
        for candidate in candidates:
            counts[candidate['group']] = counts.get(candidate['group'], 0) + 1
        candidates = [candidate for candidate in candidates if counts[candidate['group']] == 1]
        chosen = next((row for row in candidates if row['group'] == group), None) if group else candidates[0] if len(candidates) == 1 else None
        if state['version'] != version or bot in self.pending:
            return {'candidates': [], 'selected': None, 'reason': 'binding_changed'}
        return {'candidates': candidates, 'selected': chosen, 'reason': 'selected' if chosen else 'choose_group' if candidates else 'no_eligible_context'}

    async def member_context(self, bot, decision):
        """Verified private identity of an allowed group-member decision, for private delivery."""
        try:
            state = await self.state(bot)
        except (RuntimeError, ValueError):
            return {'context': None, 'reason': 'binding_unavailable'}
        version = state['version']
        binding = state['bindings'].get(identity_key(decision['group'], decision['member']))
        if not binding:
            return {'context': None, 'reason': 'binding_missing'}
        context = {
            'group': decision['group'],
            'member': decision['member'],
            'private_id': binding['private_id'],
            'binding_id': binding['id'],
            'binding_version': version,
            'decision': decision,
        }
        # Same final gate as private contexts: private denial, binding version, decision.
        return {'context': context, 'reason': 'selected'} if self.current(bot, context) else {'context': None, 'reason': 'private_denied_or_changed'}

    def current(self, bot, context):
        if (
            not isinstance(context, dict)
            or context.get('private_id') in get_blacklist_map(bot, 'user')
            or not self.access.decision_current(bot, context.get('decision', {}))
        ):
            return False
        if not context.get('group'):
            return True
        decision = context['decision']
        scoped = self.access.scope_states.get((bot, decision['consumer']), {})
        if any(
            deny['group'] == ''
            and deny['member'] in {'', context.get('private_id')}
            and decision['function'] in self.access._function_members(scoped, deny.get('function_scope', {'kind': 'function', 'id': deny['function']}))
            for deny in scoped.get('denies', [])
        ):
            return False
        state = self.states.get(bot)
        if not state or bot in self.failed or bot in self.pending or state['version'] != context.get('binding_version'):
            return False
        binding = state['bindings'].get(identity_key(context['group'], context['member']))
        return bool(binding and binding['id'] == context.get('binding_id') and binding['private_id'] == context.get('private_id'))
