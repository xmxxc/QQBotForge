"""Optional bridge to the authorization module for restricted rules and delegation.

Q&A keeps no member, role or grant lists of its own. When the authorization
module is enabled, Q&A registers as consumer ``self_qa``:

- ``public``: kind public; only explicit or framework denial blocks a public rule.
- ``answer``: default policy of restricted rules (explicit, no groups enabled).
- ``rule-<id>-policy``: optional per-rule policy chosen by the owner.
- ``manage``: delegation; role owner/admin in the groups the owner opened.

Restricted rules are resources ``rule-<id>``. Without authorization, public
rules keep answering and restricted rules and delegation are refused.
"""

import sqlite3

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

    async def admit(self, bot, rule, group, member, *, private_group=''):
        """Decision for one rule and asker, or None. Public rules without authorization pass.

        In private chat (group == '') restricted rules use the authorization
        module's verified bindings: ``{'context': ...}`` when one group context is
        selected, ``{'allowed': False, 'choose': [groups]}`` when the asker must
        pick among several eligible groups.
        """
        if rule.get('access', 'public') == 'public':
            if self.module() is None:
                return {'allowed': True, 'reason': 'public'}
            try:
                acl = await self.ensure(bot)
                decision = await acl.evaluate(bot, CONSUMER, 'public', group, member)
            except FAILURES:
                return {'allowed': True, 'reason': 'public'}
            return None if decision['reason'] in {'denied', 'framework_denied'} else {'allowed': True, 'reason': 'public'}
        try:
            acl = await self.ensure(bot)
            if not group:
                found = await acl.private_contexts(bot, CONSUMER, rule_function(rule), member, group=private_group, resource=resource(rule), action='view')
                if found['selected']:
                    return {'allowed': True, 'reason': found['reason'], 'context': found['selected']}
                if found['reason'] == 'choose_group':
                    return {'allowed': False, 'choose': [c['group'] for c in found['candidates']]}
                return None
            decision = await acl.evaluate(bot, CONSUMER, rule_function(rule), group, member, resource(rule), action='view')
        except FAILURES:
            return None
        return decision if decision['allowed'] else None

    def current(self, bot, decision):
        if decision.get('reason') == 'public' and 'consumer' not in decision:
            return True
        acl = self.module()
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

    def context_current(self, bot, context):
        acl = self.module()
        return bool(acl and acl.context_current(bot, context))

    async def group_view(self, bot, rule, group):
        """Whole-group view eligibility for a restricted answer (A04 report)."""
        context = dict(consumer=CONSUMER, function=rule_function(rule), group=group, resource=resource(rule), actions=['view'])
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
