"""Read-only platform adapter; errors and missing results never prove absence."""

from datetime import datetime
from urllib.parse import quote


def joined_time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


class MembershipReader:
    def __init__(self, sender_provider=None):
        self.sender_provider = sender_provider or self._sender

    @staticmethod
    def _sender(bot):
        from core.application import get_app

        app = get_app()
        instance = app.get_bot(bot) if app else None
        return instance.sender if instance else None

    async def request(self, bot, path, **kwargs):
        sender = self.sender_provider(bot)
        if sender is None:
            return None, 'unavailable'
        try:
            success, data = await sender.get_json(path, **kwargs)
        except Exception:
            return None, 'network_error'
        if not success:
            return None, 'permission_denied' if isinstance(data, dict) and str(data.get('code')) == '11253' else 'response_unknown'
        return (data, '') if isinstance(data, dict) else (None, 'response_unknown')

    async def member(self, bot, group, member):
        data, error = await self.request(bot, f'/v2/groups/{quote(group, safe="")}/members/{quote(member, safe="")}')
        if error:
            return {'state': 'unknown', 'reason': error}
        joined_at = joined_time(data.get('joined_at'))
        if data.get('member_openid') != member or joined_at is None:
            return {'state': 'unknown', 'reason': 'response_unknown'}
        return {'state': 'present', 'member': member, 'joined_at': joined_at, 'name': str(data.get('username') or '')[:200]}

    async def roster(self, bot, group):
        members, cursors, cursor = {}, set(), ''
        # A bounded sync fails closed rather than following an unending response.
        for _ in range(1000):
            data, error = await self.request(bot, f'/v2/groups/{quote(group, safe="")}/members', params={'cursor': cursor} if cursor else None)
            if error:
                return {'complete': False, 'members': list(members.values()), 'reason': error}
            rows, next_cursor = data.get('members'), data.get('next_cursor')
            if not isinstance(rows, list) or len(rows) > 30 or not isinstance(next_cursor, str) or len(next_cursor) > 2048:
                return {'complete': False, 'members': list(members.values()), 'reason': 'response_unknown'}
            for row in rows:
                native = row.get('member_openid') if isinstance(row, dict) else None
                if not isinstance(native, str) or not native.strip() or len(native) > 256 or native in members:
                    return {'complete': False, 'members': list(members.values()), 'reason': 'pagination_conflict'}
                members[native] = {'member': native, 'name': str(row.get('username') or '')[:200], 'bot': row.get('bot') is True}
            if not next_cursor:
                return {'complete': bool(members), 'members': list(members.values()), 'reason': '' if members else 'empty_roster'}
            if next_cursor in cursors or not rows:
                return {'complete': False, 'members': list(members.values()), 'reason': 'pagination_conflict'}
            cursors.add(next_cursor)
            cursor = next_cursor
        return {'complete': False, 'members': list(members.values()), 'reason': 'pagination_limit'}
