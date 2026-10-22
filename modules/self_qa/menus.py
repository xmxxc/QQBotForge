"""Short-lived candidate menus bound to Bot, caller and chat context.

A menu stores only the asked text and rule IDs, never answers. Choosing re-runs
the match for the same caller and context, so deleted, disabled, rescoped or
no-longer-admitted rules make old entries fail instead of answering.
"""

import time
from uuid import uuid4

PAGE = 5
TTL = 900
MAX_MENUS = 1000


def context_of(event):
    return {
        'bot': event.appid,
        'caller': event.raw_user_id or event.user_id or '',
        'scene': 'group' if event.is_group else 'private',
        'group': event.group_id if event.is_group else '',
    }


class Menus:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.menus = {}
        self.latest = {}

    def issue(self, event, namespace, text, candidates):
        now = self.clock()
        for token in [t for t, m in self.menus.items() if m['expires_at'] <= now][:MAX_MENUS]:
            self.menus.pop(token)
        while len(self.menus) >= MAX_MENUS:
            self.menus.pop(next(iter(self.menus)))
        self.latest = {key: token for key, token in self.latest.items() if token in self.menus}
        token = uuid4().hex
        context = context_of(event)
        self.menus[token] = {
            **context,
            'token': token,
            'kind': 'choices',
            'namespace': namespace,
            'text': text,
            'rules': [c['rule_id'] for c in candidates],
            'expires_at': now + TTL,
        }
        self.latest[tuple(context.values())] = token
        return token

    def issue_links(self, event, namespace, rule_ids):
        """Session for rule buttons inside one answer: index → rule ID, nothing else."""
        token = self.issue(event, namespace, '', [])
        self.menus[token].update(kind='links', links=list(rule_ids))
        return token

    def resolve(self, event, token=None):
        """The menu for this exact caller and context, or a reason it cannot be used."""
        context = context_of(event)
        token = token if token is not None else self.latest.get(tuple(context.values()))
        menu = self.menus.get(token or '')
        if not menu or menu['expires_at'] <= self.clock():
            return None, 'expired'
        if any(menu[key] != value for key, value in context.items()):
            # Another user or chat cannot borrow the original caller's menu.
            return None, 'context_mismatch'
        return menu, ''

    @staticmethod
    def page(candidates, page):
        last = max(0, (len(candidates) - 1) // PAGE)
        page = min(max(page, 0), last)
        return page, last, candidates[page * PAGE : (page + 1) * PAGE]
