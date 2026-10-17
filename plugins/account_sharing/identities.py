"""Owner-confirmed mapping of observed C2C identities to native group subjects."""

import time
from uuid import uuid4

from modules.access_control.policy import identifier, identity_key

FUNCTION = 'account_sharing'


class IdentityBindings:
    def __init__(self, store, access):
        self.store = store
        self.access = access

    async def request(self, bot, private_id, group, member):
        for value in (private_id, group, member):
            identifier(value)
        if not (await self.access().decide(bot, FUNCTION, group, member, action='invoke'))['allowed']:
            raise PermissionError('未授权')
        for request in await self.store.all(bot, 'binding_requests'):
            if request['private_id'] == private_id and request['group'] == group and request['member'] == member and request['status'] == 'pending':
                return request
        request = {
            'id': uuid4().hex,
            'group': group,
            'member': member,
            'private_id': private_id,
            'status': 'pending',
            'source': 'observed_c2c_request',
            'created_at': time.time(),
        }
        await self.store.put(bot, 'binding_requests', request['id'], request)
        return request

    async def confirm(self, bot, data, actor):
        if not isinstance(data, dict) or set(data) - {'operation', 'request', 'confirmed', 'evidence'}:
            raise ValueError('绑定确认字段无效')
        if data.get('operation') != 'confirm' or data.get('confirmed') is not True:
            raise ValueError('须由主人核对群成员与本次私聊申请为同一人后明确确认')
        evidence = data.get('evidence')
        if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 1000:
            raise ValueError('请记录核对两侧身份的依据（不含账密）')
        request = await self.store.get(bot, 'binding_requests', identifier(data.get('request')))
        if not request:
            raise ValueError('绑定申请不存在')
        if not (await self.access().decide(bot, FUNCTION, request['group'], request['member'], action='invoke'))['allowed']:
            raise ValueError('成员资格已失效，请先重新核验或授权')
        binding = {key: request[key] for key in ('group', 'member', 'private_id')}
        binding.update(source='owner_confirmed', confirmed_by=actor, confirmed_at=time.time(), evidence=evidence)
        await self.store.put(bot, 'bindings', identity_key(binding['group'], binding['member']), binding)
        request.update(status='confirmed', confirmed_by=actor, confirmed_at=binding['confirmed_at'], evidence=evidence)
        await self.store.put(bot, 'binding_requests', request['id'], request)
        return binding

    async def lookup(self, bot, group, member):
        return await self.store.get(bot, 'bindings', identity_key(group, member))

    async def contexts(self, bot, private_id):
        result = []
        for binding in await self.store.all(bot, 'bindings'):
            if binding['private_id'] != private_id:
                continue
            if (await self.access().decide(bot, FUNCTION, binding['group'], binding['member'], action='invoke'))['allowed']:
                result.append(binding)
        return result
