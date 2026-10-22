"""Optional bridge to the authorization module for restricted rules and delegation.

Q&A keeps no member, role or grant lists of its own. When the authorization
module is enabled, Q&A registers as consumer ``self_qa``:

- ``public``: kind public; only explicit or framework denial blocks a public rule.
- ``answer``: default policy of restricted rules (explicit, no groups enabled).
- ``rule-<id>-policy``: optional per-rule policy chosen by the owner.
- ``manage``: delegation; role owner/admin in the groups the owner opened.

Private chat may answer one Q&A enabled group's rules through a group context:
an owner-confirmed binding of the private identity to a member currently and
reliably present in that group. A context grants nothing; every rule still
needs its own decision.

Restricted rules are resources ``rule-<id>``. Without authorization, public
rules keep answering and restricted rules and delegation are refused.
"""

import sqlite3
from collections import Counter

from core.plugin._blacklist import get_blacklist_map

CONSUMER = 'self_qa'
OWNER = 'module:self_qa'
BASE = {
    'public': {'kind': 'public', 'groups': [], 'private': False},
    'answer': {'kind': 'explicit', 'groups': [], 'private': False},
    'manage': {'kind': 'role', 'groups': [], 'roles': ['owner', 'admin'], 'private': False},
}
FAILURES = (RuntimeError, ValueError, KeyError, sqlite3.Error, OSError)


def resource(rule):
    return 'rule-' + rule['id']


def rule_function(rule):
    return rule.get('function') or 'answer'


class AccessBridge:
    def __init__(self, qa, namespace):
        self.qa = qa
        self.namespace = namespace
        self.ready = {}

    @staticmethod
    def module():
        from core.application import get_app

        app = get_app()
        source = app.module_manager.get('access_control') if app and app.module_manager else None
        return source if source is not None and getattr(source, 'active', False) else None

    async def ensure(self, bot):
        """Declare functions and the restricted-rule inventory once per authorization instance and rule version."""
        acl = self.module()
        if acl is None:
            raise RuntimeError('授权模块未启用')
        key = (id(acl), acl.generation, self.qa.version(bot, self.namespace))
        if self.ready.get(bot) != key:
            rules = await self.qa.manage(bot, self.namespace, 'list')
            await acl.declare(bot, CONSUMER, owner=OWNER, functions=BASE)
            await acl.declare_resources(bot, CONSUMER, [{'id': resource(r)} for r in rules if r.get('access') == 'restricted'])
            self.ready[bot] = key
        return acl

    async def status(self, bot):
        try:
            acl = await self.ensure(bot)
            snapshot = await acl.access_snapshot(bot, CONSUMER)
        except FAILURES:
            return {'authorization': 'unavailable', 'restricted': 'refused', 'delegation': 'paused', 'delegated_groups': []}
        return {
            'authorization': 'available',
            'restricted': 'authorization',
            'delegation': 'role_owner_admin',
            'delegated_groups': snapshot['functions']['manage']['groups'],
        }

    async def group_contexts(self, bot, private_id, groups):
        """Private chat group contexts of ``private_id`` among the Q&A enabled ``groups``, by group.

        Each needs an owner-confirmed binding (one member per group) and that
        member's current reliable presence. ``decision`` holds the public
        evaluation as that member, so ``context_current`` re-checks the binding,
        the group facts, denies and the private identity before every send.
        """
        try:
            acl = await self.ensure(bot)
            source = acl.group_state()
            if source is None or not groups:
                return {}
            snapshot = await acl.binding_snapshot(bot)
            rows = [b for b in snapshot['bindings'] if b['private_id'] == private_id and b['group'] in groups]
            counts = Counter(b['group'] for b in rows)
            contexts = {}
            for binding in rows:
                if counts[binding['group']] != 1:
                    continue  # ambiguous in this group: never pick a member
                group, member = binding['group'], binding['member']
                evaluated = await acl.evaluate(bot, CONSUMER, 'public', group, member, framework_allowed=private_id not in get_blacklist_map(bot, 'user'))
                proof = await source.fact(bot, group, member)
                if not proof['usable'] or (proof['member'] or {}).get('status') != 'present':
                    continue
                contexts[group] = {
                    'group': group,
                    'member': member,
                    'private_id': private_id,
                    'binding_id': binding['id'],
                    'binding_version': snapshot['version'],
                    # Not a grant: currency of the public evaluation and the group facts behind this context.
                    'decision': {**evaluated, 'allowed': True, 'fact': proof},
                    'evaluated': evaluated,
                }
        except FAILURES:
            return {}
        return {group: context for group, context in contexts.items() if acl.context_current(bot, context)}

    async def admit(self, bot, rule, group, member, *, context=None, groups=()):
        """Decision for one rule and asker, or None. Public rules without authorization pass.

        In private chat (group == '') ``context`` is the selected group context
        and ``groups`` the groups with a valid context. A public group rule is
        decided as the bound member in that group and as the private identity;
        restricted rules use the authorization module's private contexts within
        ``groups``: ``{'context': ...}`` when one is selected,
        ``{'allowed': False, 'choose': [groups]}`` when the asker must pick
        another or one of several.
        """
        if rule.get('access', 'public') == 'public':
            in_context = bool(context and rule.get('groups'))
            if self.module() is None:
                return None if in_context else {'allowed': True, 'reason': 'public'}
            try:
                acl = await self.ensure(bot)
                decision = await acl.evaluate(bot, CONSUMER, 'public', group, member)
            except FAILURES:
                return None if in_context else {'allowed': True, 'reason': 'public'}
            if decision['reason'] in {'denied', 'framework_denied'}:
                return None
            if in_context:
                # A group's own denies follow its member into that group's private context.
                if context['evaluated']['reason'] in {'denied', 'framework_denied'}:
                    return None
                return {'allowed': True, 'reason': 'public', 'evaluated': decision, 'scope': context}
            return {'allowed': True, 'reason': 'public', 'evaluated': decision}
        try:
            acl = await self.ensure(bot)
            if not group:
                function, target = rule_function(rule), resource(rule)
                found = await acl.private_contexts(bot, CONSUMER, function, member, group=context['group'] if context else '', resource=target, action='view')
                selected = found['selected']
                if selected and (not selected['group'] or selected['group'] in groups):
                    return {'allowed': True, 'reason': found['reason'], 'context': selected}
                # A group rule is only answered in its own groups: never point to another one.
                choices = [c['group'] for c in found['candidates'] if c['group'] in groups and (not rule.get('groups') or c['group'] in rule['groups'])]
                if not choices:
                    return None
                if context or len(choices) > 1:
                    return {'allowed': False, 'choose': choices}
                found = await acl.private_contexts(bot, CONSUMER, function, member, group=choices[0], resource=target, action='view')
                return {'allowed': True, 'reason': found['reason'], 'context': found['selected']} if found['selected'] else None
            decision = await acl.evaluate(bot, CONSUMER, rule_function(rule), group, member, resource(rule), action='view')
        except FAILURES:
            return None
        return decision if decision['allowed'] else None

    def current(self, bot, decision):
        acl = self.module()
        if 'scope' in decision and not (acl and acl.context_current(bot, decision['scope'])):
            # A group rule answered in private chat also needs its group context to stay current.
            return False
        if decision.get('reason') == 'public' and 'consumer' not in decision:
            if 'evaluated' not in decision:
                return True
            # Not a grant: only that the public evaluation (denies, policy, generation) is still current.
            return bool(acl and acl.decision_current(bot, {**decision['evaluated'], 'allowed': True}))
        if acl is None:
            return False
        return bool(acl.context_current(bot, decision['context']) if 'context' in decision else acl.decision_current(bot, decision))

    async def member_context(self, bot, rule, group, member):
        """The asker's verified private identity for a restricted group answer."""
        try:
            acl = await self.ensure(bot)
            return await acl.member_context(bot, CONSUMER, rule_function(rule), group, member, resource(rule), action='view')
        except FAILURES:
            return {'context': None, 'reason': 'authorization_unavailable'}

    async def manager_context(self, bot, group, member):
        """A delegated manager's verified private identity, for details not shown to the group."""
        try:
            acl = await self.ensure(bot)
            return await acl.member_context(bot, CONSUMER, 'manage', group, member, action='invoke')
        except FAILURES:
            return {'context': None, 'reason': 'authorization_unavailable'}

    def context_current(self, bot, context):
        acl = self.module()
        return bool(acl and acl.context_current(bot, context))

    @staticmethod
    def view_context(rule, group):
        return dict(consumer=CONSUMER, function=rule_function(rule), group=group, resource=resource(rule), actions=['view'])

    def report_current(self, bot, rule, group, report):
        """Non-awaiting re-check of a ``group_view`` report for this rule and group."""
        acl = self.module()
        return bool(acl and acl.report_current(bot, report, **self.view_context(rule, group)))

    async def group_view(self, bot, rule, group):
        """Whole-group view eligibility for a restricted answer (A04 report)."""
        context = self.view_context(rule, group)
        try:
            acl = await self.ensure(bot)
            report = await acl.audience_report(bot, context['consumer'], context['function'], group, context['resource'], actions=context['actions'])
        except FAILURES:
            return None
        return report if report['eligible'] and acl.report_current(bot, report, **context) else None

    async def delegate(self, bot, group, member):
        """Current delegated-manager decision for this group, or None."""
        try:
            acl = await self.ensure(bot)
            decision = await acl.evaluate(bot, CONSUMER, 'manage', group, member, action='invoke')
        except FAILURES:
            return None
        return decision if decision['allowed'] else None

    async def set_delegation(self, bot, groups):
        acl = await self.ensure(bot)
        await acl.manage_access(bot, CONSUMER, 'policy', function='manage', policy={**BASE['manage'], 'groups': groups})
        return (await self.status(bot))['delegated_groups']

    async def set_rule_policy(self, bot, rule, policy):
        """Give one restricted rule its own authorization policy (or None to inherit ``answer``)."""
        acl = await self.ensure(bot)
        if policy is None:
            return None
        function = f'rule-{rule["id"]}-policy'
        await acl.declare(bot, CONSUMER, owner=OWNER, functions={function: policy})
        await acl.manage_access(bot, CONSUMER, 'policy', function=function, policy=policy)
        return function
