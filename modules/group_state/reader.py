"""Read-only platform adapter; preserve missing fields instead of completing them."""

import asyncio
from datetime import datetime
from urllib.parse import quote

ROLES = {'owner', 'admin', 'member'}


def joined_time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def native(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 256


def member_record(row):
    if not isinstance(row, dict) or not native(row.get('member_openid')):
        return None
    role = row.get('member_role')
    return {
        'member': row['member_openid'],
        'name': str(row.get('username') or '')[:200],
        'member_role': role if isinstance(role, str) and role in ROLES else None,
        'bot': row.get('bot') if isinstance(row.get('bot'), bool) else None,
        'joined_at': joined_time(row.get('joined_at')),
    }


class MembershipReader:
    def __init__(self, sender_provider):
        self.sender_provider = sender_provider

    async def request(self, bot, path, **kwargs):
        sender = self.sender_provider(bot)
        if sender is None or not hasattr(sender, 'get_json'):
            return None, 'unsupported'
        try:
            success, data = await asyncio.wait_for(sender.get_json(path, **kwargs), timeout=10)
        except TimeoutError:
            return None, 'timeout'
        except Exception:
            return None, 'query_failed'
        if not success:
            return None, 'permission_denied' if isinstance(data, dict) and str(data.get('code')) == '11253' else 'response_unknown'
        return (data, '') if isinstance(data, dict) else (None, 'response_unknown')

    async def member(self, bot, group, member):
        data, error = await self.request(bot, f'/v2/groups/{quote(group, safe="")}/members/{quote(member, safe="")}')
        result = member_record(data)
        if error or result is None or result['member'] != member or result['member_role'] is None or result['joined_at'] is None:
            return {'state': 'unknown', 'reason': error or 'response_unknown'}
        return {**result, 'state': 'present', 'reason': ''}

    async def roster(self, bot, group):
        members, cursors, cursor = {}, set(), ''
        result = {'complete': False, 'members': [], 'reason': 'pagination_limit', 'self_member': None}
        for _ in range(1000):
            data, error = await self.request(bot, f'/v2/groups/{quote(group, safe="")}/members', params={'cursor': cursor} if cursor else None)
            if error:
                result['reason'] = error
                break
            rows, next_cursor = data.get('members'), data.get('next_cursor')
            if not isinstance(rows, list) or len(rows) > 30 or not isinstance(next_cursor, str) or len(next_cursor) > 2048:
                result['reason'] = 'response_unknown'
                break
            conflict = False
            for row in rows:
                record = member_record(row)
                if record is None or record['member'] in members:
                    result['reason'], conflict = 'pagination_conflict', True
                    break
                members[record['member']] = record
            if conflict:
                break
            if not next_cursor:
                if not members:
                    result['reason'] = 'empty_roster'
                    break
                if any(m['member_role'] is None for m in members.values()):
                    result['reason'] = 'role_missing'
                    break
                bot_state, error = await self.request(bot, f'/v2/groups/{quote(group, safe="")}/bot_state')
                own = bot_state.get('member_openid') if bot_state else None
                if error or not native(own) or own not in members:
                    result['reason'] = error or 'bot_identity_unknown'
                    break
                if bot_state.get('member_role') != members[own]['member_role'] or members[own]['bot'] is False:
                    result['reason'] = 'bot_identity_conflict'
                    break
                members[own]['bot'] = True
                result.update(complete=True, reason='', self_member=own)
                break
            if next_cursor in cursors or not rows:
                result['reason'] = 'pagination_conflict'
                break
            cursors.add(next_cursor)
            cursor = next_cursor
        result['members'] = list(members.values())
        return result
