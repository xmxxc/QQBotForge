"""Whole-group qualification reports from group-state roster proofs.

Consumers submit function, resource and actions; they never read platform
responses, roster internals or authorization versions. ``current`` is the
non-awaiting final gate before a group send.

A report is a display copy plus an opaque ``credential``. The roster proof,
per-recipient decisions, deadline and the context the report was built for
live inside that credential, which only its issuing instance accepts, so
rewriting any report field cannot widen or extend it, and a reload
invalidates every earlier credential.
"""

import copy
import weakref

from .policy import identifier

ACTIONS = ('invoke', 'view')


class Credential:
    """Opaque verification material of one report; copies keep the same object."""

    __slots__ = ('_record', '__weakref__')

    def __init__(self, record):
        self._record = record

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __repr__(self):
        return '<audience credential>'


def normalized_actions(actions):
    if not isinstance(actions, (list, tuple)) or not actions or set(actions) - set(ACTIONS):
        return None
    return tuple(action for action in ACTIONS if action in actions)


class Audience:
    def __init__(self, access):
        self.access = access
        self.issued: weakref.WeakSet[Credential] = weakref.WeakSet()

    async def report(self, bot, consumer, function, group, resource='', *, actions=('view',)):
        identifier(group)
        ordered = normalized_actions(actions)
        if ordered is None:
            raise ValueError('动作须为 invoke/view 的非空列表')
        actions = list(ordered)
        state = await self.access._scope(bot, consumer)
        if function not in state['functions']:
            raise ValueError('消费方功能尚未声明')
        result = {
            'bot': bot,
            'consumer': consumer,
            'function': function,
            'group': group,
            'resource': resource,
            'actions': actions,
            'eligible': False,
            'complete': False,
            'reason': '',
            'roster_reason': '',
            'members': [],
            'failures': [],
            'failures_complete': False,
            'checked_at': self.access.clock(),
            'valid_until': None,
            'credential': None,
        }
        try:
            group_proof, member_proofs = await self.access._facts().facts(bot, group)
        except RuntimeError:
            result.update(reason='group_facts_unavailable', roster_reason='group_facts_unavailable')
            return result
        roster = (group_proof['group_state'] or {}).get('roster', {})
        result['complete'] = bool(roster.get('complete'))
        result['roster_reason'] = roster.get('reason') or ('' if result['complete'] else 'unknown_group')
        try:
            bound = {(b['group'], b['member']) for b in (await self.access.bindings.state(bot))['bindings'].values()}
        except (RuntimeError, ValueError):
            bound = None
        # A complete roster lists every recipient, including this and other Bots.
        # Without it only reliably known present members can be confirmed.
        candidates = (
            [row['member'] for row in roster['members']]
            if result['complete']
            else [m for m, proof in member_proofs.items() if proof['usable'] and (proof['member'] or {}).get('status') == 'present']
        )
        decisions, deadlines = [], []
        for member in candidates:
            proof = member_proofs.get(member)
            fact = (proof or {}).get('member') or {}
            row = {
                'member': member,
                'name': fact.get('name', ''),
                'bot': fact.get('bot'),
                'self': member == roster.get('self_member'),
                'role': fact.get('member_role'),
                'private_bound': None if bound is None else (group, member) in bound,
                'eligible': True,
                'actions': {},
            }
            for action in actions:
                decision = await self.access._evaluate(bot, consumer, function, group, member, resource, action=action, proof=proof)
                row['actions'][action] = {'allowed': decision['allowed'], 'reason': decision['reason'], 'valid_until': decision['valid_until']}
                row['eligible'] &= decision['allowed']
                decisions.append(decision)
                if decision['valid_until'] is not None:
                    deadlines.append(decision['valid_until'])
            result['members'].append(row)
            if not row['eligible']:
                result['failures'].append({'member': member, 'bot': row['bot'], 'reasons': {a: row['actions'][a]['reason'] for a in actions}})
        result['failures_complete'] = result['complete']
        result['valid_until'] = min(deadlines) if deadlines else None
        issued = {
            'context': (bot, consumer, function, group, resource, ordered),
            'roster': {**group_proof, 'group_state': None, 'usable': result['complete']},
            'decisions': decisions,
            'members': {row['member']: [d for d in decisions if d['member'] == row['member']] for row in result['members']},
            'valid_until': result['valid_until'],
            'eligible': False,
        }
        if not result['complete']:
            result['reason'] = 'roster_incomplete'
        elif result['failures']:
            result['reason'] = 'member_ineligible'
        elif not self._current(bot, {**issued, 'eligible': True}):
            result['reason'] = 'state_changed'
        else:
            issued['eligible'] = True
            result.update(eligible=True, reason='eligible')
        result['credential'] = Credential(issued)
        self.issued.add(result['credential'])
        return result

    def _issued(self, bot, report, consumer, function, group, resource, actions):
        """The issued record, only for the exact context the caller is about to act on."""
        credential = report.get('credential') if isinstance(report, dict) else None
        if not isinstance(credential, Credential) or credential not in self.issued:
            return None
        issued = credential._record
        return issued if issued['context'] == (bot, consumer, function, group, resource, normalized_actions(actions)) else None

    def _roster_current(self, issued):
        source = self.access.group_state()
        return bool(issued['roster']['usable'] and source and source.fact_current(issued['roster']))

    def _current(self, bot, issued):
        return bool(
            issued['eligible']
            and self._roster_current(issued)
            and issued['decisions']
            and (issued['valid_until'] is None or self.access.clock() < issued['valid_until'])
            and all(self.access.decision_current(bot, decision) for decision in issued['decisions'])
        )

    def facts_current(self, bot, report, *, consumer, function, group, resource, actions):
        """Only the group facts behind this report (roster, members, roles, recovery) are unchanged."""
        issued = self._issued(bot, report, consumer, function, group, resource, actions)
        return bool(issued and self._roster_current(issued))

    def current(self, bot, report, *, consumer, function, group, resource, actions):
        """Every recipient decision and the roster proof still hold for this exact send."""
        issued = self._issued(bot, report, consumer, function, group, resource, actions)
        return bool(issued and self._current(bot, issued))

    def member_current(self, bot, report, member, *, consumer, function, group, resource, actions):
        """One recipient, for example the asker, for this exact context."""
        issued = self._issued(bot, report, consumer, function, group, resource, actions)
        decisions = issued['members'].get(member) if issued else None
        return bool(
            decisions
            and issued
            and {decision['action'] for decision in decisions} == set(actions)
            and all(self.access.decision_current(bot, decision) for decision in decisions)
        )

    @staticmethod
    def owner_view(report):
        """Management copy without the verification credential; only owner entry points use it."""
        return {key: copy.deepcopy(value) for key, value in report.items() if key != 'credential'}
