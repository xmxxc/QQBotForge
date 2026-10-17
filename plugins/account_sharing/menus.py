"""Short-lived personal query references; callback payloads never contain credentials."""

from uuid import uuid4


class MenuSessions:
    def __init__(self, store, clock):
        self.store = store
        self.clock = clock

    async def issue(self, event, context, items):
        now = self.clock()
        for old in await self.store.all(event.appid, 'menus'):
            if old['expires_at'] <= now:
                await self.store.delete(event.appid, 'menus', old['id'])
        token = uuid4().hex
        session = {
            'id': token,
            'caller': event.raw_user_id,
            'scene': 'group' if event.is_group else 'private',
            'origin_group': event.group_id or '',
            'context': context,
            'expires_at': now + 900,
            'actions': {str(i): item[1] for i, item in enumerate(items)},
        }
        await self.store.put(event.appid, 'menus', token, session)
        buttons = [
            {'text': label, 'type': 1, 'data': f'as:{token}:{i}', 'list': [event.raw_user_id], 'tips': '请使用消息中的文字命令'}
            for i, (label, _) in enumerate(items)
        ]
        rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
        rows.append([{'text': '账户列表', 'type': 1, 'data': 'as:list', 'tips': '发送：账户列表'}])
        return rows

    async def resolve(self, event, payload):
        parts = payload.split(':')
        if len(parts) != 3 or parts[0] != 'as' or len(parts[1]) != 32:
            raise ValueError('菜单引用无效，请重新发送：账户列表')
        session = await self.store.get(event.appid, 'menus', parts[1])
        if not session or session['expires_at'] <= self.clock() or parts[2] not in session['actions']:
            raise ValueError('菜单已失效，请重新发送：账户列表')
        scene = 'group' if event.is_group else 'private'
        if session['caller'] != event.raw_user_id or session['scene'] != scene or session['origin_group'] != (event.group_id or ''):
            raise PermissionError('个人菜单上下文不匹配')
        return session['context'], session['actions'][parts[2]]
