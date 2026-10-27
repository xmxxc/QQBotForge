"""Authorization on production wiring: real BotInstance sources (see test_group_state_wiring for the platform boundary)."""

import shutil
from pathlib import Path

from test_group_state_wiring import confirm, overlapping_credential_sync, owners, trusted, wired  # noqa: F401  (wired is the fixture)


async def test_authorization_follows_ws_trust_and_actual_webhook_receipt(wired):  # noqa: F811
    destination = Path(wired.modules._dir) / 'access_control'
    shutil.copytree(Path(__file__).absolute().parents[2] / 'modules/access_control', destination, ignore=shutil.ignore_patterns('data', '__pycache__'))
    wired.modules.discover_module('access_control')
    await wired.load_module()
    assert await wired.modules.enable('access_control', _skip_deps=True, _persist=False)
    await wired.start_bots()
    acl = wired.modules.get('access_control')
    for bot in ('bot-a', 'bot-b'):
        await acl.declare(bot, 'reports', owner='plugin:reports', functions={'read': {'kind': 'explicit', 'groups': ['group-a']}})
        await acl.declare_resources(bot, 'reports', [{'id': 'report-r'}])
    await trusted(wired, 'bot-a', 'bot-b')
    decisions = {}
    for bot in ('bot-a', 'bot-b'):
        await acl.manage_access(bot, 'reports', 'grant', function='read', group='group-a', member='member-a', resources=['report-r'])
        decisions[bot] = await acl.evaluate(bot, 'reports', 'read', 'group-a', 'member-a', 'report-r')
        assert decisions[bot]['allowed'] and acl.decision_current(bot, decisions[bot]), (decisions[bot]['reason'], decisions[bot]['retryable'])
    await wired.post_webhook('bot-a', 7, lifecycle=False)
    assert not acl.decision_current('bot-a', decisions['bot-a'])
    denied = await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r')
    assert (denied['allowed'], denied['reason'], denied['retryable']) == (False, 'group_facts_recovery_required', False)
    assert acl.decision_current('bot-b', decisions['bot-b'])
    assert (await acl.evaluate('bot-b', 'reports', 'read', 'group-a', 'member-a', 'report-r'))['allowed']


async def test_authorization_denied_while_http_and_ws_sources_belong_to_different_instances(wired):  # noqa: F811
    destination = Path(wired.modules._dir) / 'access_control'
    shutil.copytree(Path(__file__).absolute().parents[2] / 'modules/access_control', destination, ignore=shutil.ignore_patterns('data', '__pycache__'))
    wired.modules.discover_module('access_control')
    await wired.load_module()
    assert await wired.modules.enable('access_control', _skip_deps=True, _persist=False)
    await wired.start_bots()
    acl = wired.modules.get('access_control')
    for bot in ('bot-a', 'bot-b'):
        await acl.declare(bot, 'reports', owner='plugin:reports', functions={'read': {'kind': 'explicit', 'groups': ['group-a']}})
        await acl.declare_resources(bot, 'reports', [{'id': 'report-r'}])
    await trusted(wired, 'bot-a', 'bot-b')
    decisions = {}
    for bot in ('bot-a', 'bot-b'):
        await acl.manage_access(bot, 'reports', 'grant', function='read', group='group-a', member='member-a', resources=['report-r'])
        decisions[bot] = await acl.evaluate(bot, 'reports', 'read', 'group-a', 'member-a', 'report-r')
        assert decisions[bot]['allowed'] and acl.decision_current(bot, decisions[bot])
    # Overlapping BotRegistry._sync runs for one credential change split bot-a's sources across two instances.
    extra = await overlapping_credential_sync(wired, 'bot-a', 'fake-secret-bot-a-2')
    serving, webhook_owner, ws_owner = owners(wired, 'bot-a')
    assert serving == ws_owner != webhook_owner
    assert not acl.decision_current('bot-a', decisions['bot-a'])
    await wired.ready_ws('bot-a', extra, session='session-a-2')
    await wired.settle('bot-a')
    await confirm(wired.state, 'bot-a')
    assert await wired.state.rebuild('bot-a') is False
    # A member-left callback is served (200) but not counted publicly: no decision may survive or be issued.
    await wired.post_webhook('bot-a', 9, kind='GROUP_MEMBER_REMOVE', member='member-a', counted=False)
    denied = await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r')
    assert (denied['allowed'], denied['reason'], denied['retryable']) == (False, 'group_facts_recovery_required', False)
    assert acl.decision_current('bot-b', decisions['bot-b'])
    # A formal replacement restores source ownership. R17 preserves the old grant
    # after READY, current owner confirmation and a baseline with no observed member transition.
    await wired.change_secret('bot-a', 'fake-secret-bot-a-3')
    await wired.ready_ws('bot-a', session='session-a-3')
    await trusted(wired, 'bot-a')
    allowed = await acl.evaluate('bot-a', 'reports', 'read', 'group-a', 'member-a', 'report-r')
    assert allowed['allowed'] and acl.decision_current('bot-a', allowed), allowed['reason']
    assert [source['id'] for source in allowed['sources']] == [source['id'] for source in decisions['bot-a']['sources']]
    assert not acl.decision_current('bot-a', decisions['bot-a'])
