"""Whole-group qualification reports from group-state roster proofs.

Consumers submit function, resource and actions; they never read platform
responses, roster internals or authorization versions. ``current`` is the
non-awaiting final gate before a group send.
"""

import copy

from .policy import identifier

ACTIONS = ('invoke', 'view')


class Audience:
    def __init__(self, access):
        self.access = access

    async def report(self, bot, consumer, function, group, resource='', *, actions=('view',)):
        identifier(group)
        if not isinstance(actions, (list, tuple)) or not actions or set(actions) - set(ACTIONS):
            raise ValueError('动作须为 invoke/view 的非空列表')
        actions = [action for action in ACTIONS if action in actions]
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
            '_proof': None,
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
        result['_proof'] = {
            'roster': {**group_proof, 'group_state': None, 'usable': result['complete']},
            'roster_id': roster.get('id'),
            'decisions': decisions,
            'members': {row['member']: [d for d in decisions if d['member'] == row['member']] for row in result['members']},
        }
        if not result['complete']:
            result['reason'] = 'roster_incomplete'
        elif result['failures']:
            result['reason'] = 'member_ineligible'
        elif not self.current(bot, result):
            result['reason'] = 'state_changed'
        else:
            result.update(eligible=True, reason='eligible')
        return result

    def _roster_current(self, bot, report):
        proof = report.get('_proof') if isinstance(report, dict) else None
        source = self.access.group_state()
        return bool(proof and proof['roster']['usable'] and source and source.fact_current(proof['roster'])), proof

    def current(self, bot, report):
        """Every recipient decision and the roster proof are still the latest."""
        current, proof = self._roster_current(bot, report)
        return bool(
            current
            and report.get('bot') == bot
            and proof['decisions']
            and (report['valid_until'] is None or self.access.clock() < report['valid_until'])
            and all(self.access.decision_current(bot, decision) for decision in proof['decisions'])
        )

    def member_current(self, bot, report, member):
        """One recipient, for example the asker, without reading report internals."""
        proof = report.get('_proof') if isinstance(report, dict) else None
        decisions = proof['members'].get(member) if proof else None
        return bool(
            decisions
            and report.get('bot') == bot
            and len(decisions) == len(report['actions'])
            and all(self.access.decision_current(bot, decision) for decision in decisions)
        )

    @staticmethod
    def owner_view(report):
        """Management copy without decision internals; only owner entry points use it."""
        return {key: copy.deepcopy(value) for key, value in report.items() if key != '_proof'}
