"""忍者必须死3 #73: trusted native joins grant this membership cycle, through the real app.

Raw WS frames enter the real WSClient and Application, pass the recovery layer and
group state, reach the authorization module's consumer notification and end at a
member query in the group. Only the QQ transport, platform queries and SQLite timing
are substituted; nothing calls a Hook or the grant path directly.
"""

import asyncio
import re
import sqlite3
import threading

import pytest
from account_host import account_host as account_host  # noqa: F401
from socket_platform import until
from test_access_control import access_host as access_host  # noqa: F401
from test_access_control import request
from test_ninja3 import SECRET
from test_ninja3 import ninja as ninja  # noqa: F401

CREDENTIALS = ['名称：账号A', 'fake-login', SECRET]
REFUSED = ['你暂无查询权限，已记录给主人']
UNSURE = ['暂时无法确认查询资格，已记录给主人']
RETRY = ['暂时无法确认查询资格，请稍后重试']
DENIED = ['你暂无查询权限']
UNAVAILABLE = ['账号查询暂不可用，请联系主人']


def assert_listed(reply, names=('账号A',)):
    """Check the paginated directory while ignoring its dynamic menu token."""
    assert len(reply) == 1
    lines = reply[0].splitlines()
    page = re.fullmatch(r'本群开放账号（(\d+)/(\d+)）：', lines[0])
    assert page and (int(page[1]), int(page[2])) == (1, (len(names) + 5) // 6)
    assert lines[1 : 1 + len(names)] == [f'{index}. {name}' for index, name in enumerate(names, 1)]
    assert len(lines) == 1 + len(names)


class Frames:
    """Lifecycle dispatch frames with per-Bot sequence numbers of the current WS session."""

    def __init__(self, host):
        self.host, self.sequence = host, {'bot-a': 1, 'bot-b': 1}

    def feed(self, kind, member, *, group='group-a', bot='bot-a', timestamp=None, event_id='auto'):
        self.sequence[bot] += 1
        sequence = self.sequence[bot]
        self.host.lifecycle(
            kind, member=member, group=group, sequence=sequence, timestamp=1000 + sequence if timestamp is None else timestamp, event_id=event_id, bot=bot
        )
        return sequence


async def settle(host, sequence=None, *, bot='bot-a', group='group-a'):
    """The framework finished the delivery and group state its consumer notifications."""
    state = host.state
    if sequence is None:
        await asyncio.sleep(0.05)
    else:
        await until(lambda: ((state.states.get(bot) or {}).get('groups', {}).get(group) or {}).get('last_native', {}).get('sequence') == sequence)
    await until(lambda: not state.recovery.get_status(bot).pending and not state.notifications.busy(bot) and not state.pending_writes.get(bot))


async def auto(host, *, enabled=True, bot='bot-a', group='group-a', open_group=True):
    status, data = await host.ninja_api('POST', 'groups', {'group': group, 'enabled': open_group, 'auto_grant': enabled}, bot=bot)
    assert status == 200 and data['data']['acl_sync']['status'] == 'saved', data
    return data['data']


async def autos(host, member='MEMBERJ', bot='bot-a'):
    return [g for g in (await host.access.access_snapshot(bot, 'ninja3'))['grants'] if g['member'] == member and g.get('basis') == 'native_join']


def platform_member(host, member, group='group-a'):
    """The platform answers a single-member query: currently in the group."""
    host.sender.json_results[(f'/v2/groups/{group}/members/{member}', '')] = (
        True,
        {'member_openid': member, 'member_role': 'member', 'joined_at': '2020-01-01T00:00:00Z', 'username': 'fake'},
    )


def hold(monkeypatch, store, match, *, after=False):
    """Block the first matching SQLite write in its worker thread, before or after it commits."""
    entered, released = threading.Event(), threading.Event()
    original = store._run

    def run(sql, params, read):
        held = sql.startswith('INSERT') and match(params) and not entered.is_set()
        if held and not after:
            entered.set()
            released.wait(10)
        result = original(sql, params, read)
        if held and after:
            entered.set()
            released.wait(10)
        return result

    monkeypatch.setattr(store, '_run', run)
    return entered, released


def ninja_row(params):
    return params[1:3] == ('access_consumers', 'ninja3')


def group_row(params):
    return params[1] == 'group_state'


async def new_session(host, frames, session='session-new-bot-a'):
    """INVALID_SESSION then a fresh IDENTIFY/READY: continuity before it is unknown."""
    host.connecting_bot[0] = 'bot-a'
    host.sockets['bot-a'].feed(9, d=False)
    socket = host.sockets['bot-a'] = host.gateways['bot-a'].add()
    socket.feed(10, d={'heartbeat_interval': 45000})
    await asyncio.sleep(3.1)  # the client's real back-off before identifying again
    await until(lambda: any(frame['op'] == 2 for frame in socket.sent))
    frames.sequence['bot-a'] += 1
    socket.feed(0, t='READY', s=frames.sequence['bot-a'], d={'session_id': session})
    await until(lambda: host.state.recovery.get_status('bot-a').context.session_id == session)


async def owner_rebuild(host, *members):
    for member in members:
        platform_member(host, member)
    await until(lambda: not host.state.notifications.busy('bot-a'))
    assert await host.state.rebuild('bot-a')


async def test_auto_grant_is_off_by_default_and_a_join_alone_stays_unverified(ninja):
    host, frames = ninja, Frames(ninja)
    await host.setup()
    assert (await host.ninja_api('GET', 'groups'))[1]['data'][0]['auto_grant'] is False
    assert (await host.access.access_snapshot('bot-a', 'ninja3')).get('join_rules', []) == []
    await settle(host, frames.feed('GROUP_MEMBER_ADD', 'MEMBERJ'))
    proof = await host.state.fact('bot-a', 'group-a', 'MEMBERJ')
    # Trusted and ordered, yet not an owner confirmation, platform query or complete roster.
    assert proof['join'] and proof['join']['session_id'] == 'session-bot-a'
    assert not proof['usable'] and proof['member']['status'] == 'pending' and proof['member']['source'] == 'native_lifecycle'
    assert not host.state.states['bot-a']['groups']['group-a']['roster']['complete']
    assert await host.say('账号列表', user='MEMBERJ') == REFUSED
    # Turning the rule on later never back-fills an earlier join; calls never grant.
    await auto(host)
    assert await host.say('账号 账号A', user='MEMBERJ') == REFUSED
    assert not await autos(host)
    assert [n['reason'] for n in (await host.ninja_api('GET', 'notices'))[1]['data']] == ['membership_unverified']


async def test_trusted_join_grants_this_cycle_leave_revokes_and_rejoin_is_a_new_source(ninja):
    host, frames = ninja, Frames(ninja)
    await host.setup()
    assert (await auto(host))['acl_sync']['auto_grant_after'] is True
    first = frames.feed('GROUP_MEMBER_ADD', 'MEMBERJ')
    await settle(host, first)
    (source,) = await autos(host)
    proof = await host.state.fact('bot-a', 'group-a', 'MEMBERJ')
    assert (source['cycle'], source['join']['event_id'], source['join']['session_id']) == (proof['member']['cycle'], f'event-{first}', 'session-bot-a')
    assert (source['mode'], source['resource_scope'], source['expires_at'], source['invoke'], source['functions']) == (
        'continuous',
        {'kind': 'all'},
        None,
        True,
        ['query'],
    )
    assert proof['member']['status'] == 'pending' and not proof['usable']
    assert_listed(await host.say('账号列表', user='MEMBERJ'))
    assert await host.say('账号 账号A', user='MEMBERJ') == CREDENTIALS
    # Accounts opened later in this group are covered by the same continuous source.
    status, _ = await host.ninja_api(
        'POST', 'accounts', {'name': '账号B', 'login': 'fake-login', 'password': 'fake-b', 'enabled': True, 'open_groups': ['group-a']}
    )
    assert status == 201
    assert await host.say('账号 账号B', user='MEMBERJ') == ['名称：账号B', 'fake-login', 'fake-b']
    decision = await host.access.evaluate('bot-a', 'ninja3', 'query', 'group-a', 'MEMBERJ', action='invoke')
    assert decision['allowed'] and decision['fact_kind'] == 'native_join' and host.access.decision_current('bot-a', decision)
    # Another member leaving changes only group facts (no authorization write): in-flight decisions are stale.
    await settle(host, frames.feed('GROUP_MEMBER_REMOVE', 'MEMBERX'))
    assert not host.access.decision_current('bot-a', decision)
    decision = await host.access.evaluate('bot-a', 'ninja3', 'query', 'group-a', 'MEMBERJ', action='invoke')
    assert decision['allowed'] and host.access.decision_current('bot-a', decision)
    # Leaving is immediate for in-flight decisions, then the source is retired.
    left = frames.feed('GROUP_MEMBER_REMOVE', 'MEMBERJ')
    await until(lambda: not host.access.decision_current('bot-a', decision))
    await settle(host, left)
    assert await host.say('账号列表', user='MEMBERJ') == REFUSED
    snapshot = await host.access.access_snapshot('bot-a', 'ninja3')
    assert not await autos(host) and (snapshot['revocations'][-1]['id'], snapshot['revocations'][-1]['reason']) == (source['id'], 'group_lifecycle')
    # The old join replayed (same event and payload) is a duplicate: nothing revives.
    frames.feed('GROUP_MEMBER_ADD', 'MEMBERJ', event_id=f'event-{first}', timestamp=1000 + first)
    await settle(host)
    assert (await host.state.fact('bot-a', 'group-a', 'MEMBERJ'))['member']['status'] == 'left' and not await autos(host)
    # A trusted rejoin is a new cycle with a new source; the old one stays revoked.
    rejoin = frames.feed('GROUP_MEMBER_ADD', 'MEMBERJ')
    await settle(host, rejoin)
    (again,) = await autos(host)
    assert again['id'] != source['id'] and again['cycle'] != source['cycle'] and again['join']['event_id'] == f'event-{rejoin}'
    assert_listed(await host.say('账号列表', user='MEMBERJ'), ('账号A', '账号B'))
    # The owner's revocation stands; re-delivering that join adds nothing back.
    await host.access.manage_access('bot-a', 'ninja3', 'revoke', function='query', group='group-a', member='MEMBERJ', source=again['id'])
    assert await host.say('账号列表', user='MEMBERJ') == REFUSED
    frames.feed('GROUP_MEMBER_ADD', 'MEMBERJ', event_id=f'event-{rejoin}', timestamp=1000 + rejoin)
    await settle(host)
    assert not await autos(host) and await host.say('账号列表', user='MEMBERJ') == REFUSED


async def test_auto_grant_stays_in_its_bot_group_and_consumer_and_never_lifts_denial(ninja):
    from core.plugin._blacklist import get_blacklist_map, set_blacklist_map

    host, frames = ninja, Frames(ninja)
    await host.create(name='旧插件账户')  # the old account consumer is registered
    await host.setup()
    await auto(host)
    await host.ninja_api('POST', 'groups', {'group': 'group-b', 'enabled': True})  # open, no automatic grants
    account = (await host.ninja_api())[1]['data'][0]
    await host.ninja_api('PATCH', 'accounts/' + account['id'], {'open_groups': ['group-a', 'group-b']})
    await host.setup(bot='bot-b')  # bot-b opens group-a without automatic grants
    await host.access.declare('bot-a', 'reports', owner='plugin:reports', functions={'read': {'kind': 'group', 'groups': ['group-a']}})
    await settle(host, frames.feed('GROUP_MEMBER_ADD', 'MEMBERJ', group='group-b'), group='group-b')
    await settle(host, frames.feed('GROUP_MEMBER_ADD', 'MEMBERJ', bot='bot-b'), bot='bot-b')
    assert not await autos(host) and not await autos(host, bot='bot-b')
    await settle(host, frames.feed('GROUP_MEMBER_ADD', 'MEMBERJ'))
    (source,) = await autos(host)
    assert source['group'] == 'group-a'
    assert_listed(await host.say('账号列表', user='MEMBERJ'))
    assert await host.say('账号列表', user='MEMBERJ', group='group-b') == REFUSED
    assert await host.say('账号列表', user='MEMBERJ', bot='bot-b') == REFUSED
    # Other consumers and other policy kinds gain nothing from the join basis.
    assert not (await host.access.access_snapshot('bot-a', 'account_sharing'))['grants']
    assert (await host.access.evaluate('bot-a', 'reports', 'read', 'group-a', 'MEMBERJ'))['reason'] == 'membership_unverified'
    # No management rights come with it.
    assert await host.say('授权', user='MEMBERJ', mentions=['MEMBERK']) == ['仅主人可以授权']
    # A denied or blacklisted joiner gets no source; a later denial wins over an existing one.
    await host.access.manage_access('bot-a', 'ninja3', 'deny', function='query', group='group-a', member='MEMBERD')
    await settle(host, frames.feed('GROUP_MEMBER_ADD', 'MEMBERD'))
    users = get_blacklist_map('bot-a', 'user')
    set_blacklist_map('bot-a', 'user', {**users, 'MEMBERL': 'fake'})
    try:
        await settle(host, frames.feed('GROUP_MEMBER_ADD', 'MEMBERL'))
        assert not await autos(host, 'MEMBERL')
    finally:
        set_blacklist_map('bot-a', 'user', users)
    assert not await autos(host, 'MEMBERD') and await host.say('账号列表', user='MEMBERD') == DENIED
    await host.access.manage_access('bot-a', 'ninja3', 'deny', function='query', group='group-a', member='MEMBERJ')
    assert await host.say('账号列表', user='MEMBERJ') == DENIED
    await host.access.manage_access('bot-a', 'ninja3', 'allow', function='query', group='group-a', member='MEMBERJ')
    assert_listed(await host.say('账号列表', user='MEMBERJ'))


@pytest.mark.parametrize('case', ['missing_event_id', 'missing_timestamp', 'missing_member', 'same_second', 'late', 'conflicting_duplicate', 'new_session'])
async def test_untrusted_or_unordered_joins_never_grant(ninja, case):
    host, frames = ninja, Frames(ninja)
    await host.setup()
    await auto(host)
    if case == 'missing_event_id':
        await settle(host, frames.feed('GROUP_MEMBER_ADD', 'MEMBERJ', event_id=None))
    elif case == 'missing_timestamp':
        await settle(host, frames.feed('GROUP_MEMBER_ADD', 'MEMBERJ', timestamp=0))
    elif case == 'missing_member':
        await settle(host, frames.feed('GROUP_MEMBER_ADD', None))
    elif case in {'same_second', 'late'}:
        await settle(host, frames.feed('GROUP_MEMBER_REMOVE', 'MEMBERJ', timestamp=5000))
        await settle(host, frames.feed('GROUP_MEMBER_ADD', 'MEMBERJ', timestamp=5000 if case == 'same_second' else 4000))
    elif case == 'conflicting_duplicate':
        # MEMBERK's trusted join granted; another payload reusing its event ID suspends the Bot.
        first = frames.feed('GROUP_MEMBER_ADD', 'MEMBERK')
        await settle(host, first)
        assert await autos(host, 'MEMBERK')
        assert_listed(await host.say('账号列表', user='MEMBERK'))
        await settle(host, frames.feed('GROUP_MEMBER_ADD', 'MEMBERJ', event_id=f'event-{first}'))
    else:
        # A new session before the owner rebuilds: continuity is unknown.
        await new_session(host, frames)
        await settle(host, frames.feed('GROUP_MEMBER_ADD', 'MEMBERJ'))
    assert not await autos(host)
    proof = await host.state.fact('bot-a', 'group-a', 'MEMBERJ')
    assert proof['join'] is None and not proof['usable'] and proof['reason'] == 'recovery_required'
    assert await host.say('账号列表', user='MEMBERJ') == UNSURE
    # After the owner rebuilds (the platform confirms current presence) there is still no source to use.
    await owner_rebuild(host, *(m for m in ('MEMBERJ', 'MEMBERK') if m in host.state.states['bot-a']['groups']['group-a']['members']))
    assert await host.say('账号列表', user='MEMBERJ') == REFUSED
    if case == 'conflicting_duplicate':
        # The earlier source does not revive across the rebuild either.
        assert await host.say('账号列表', user='MEMBERK') == REFUSED


@pytest.mark.parametrize('path', ['online', 'resume_replay'])
async def test_slow_consumer_holds_ready_until_the_written_source_is_usable(ninja, path):
    host, frames = ninja, Frames(ninja)
    await host.setup()
    await auto(host)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(change):
        if change['reason'] == 'GROUP_MEMBER_ADD':
            entered.set()
            await release.wait()

    host.state.subscribe(slow, timeout=30)
    if path == 'online':
        frames.feed('GROUP_MEMBER_ADD', 'MEMBERJ')
    else:
        # Same-session RESUME: the join is replayed inside the fixed boundary.
        host.connecting_bot[0] = 'bot-a'
        host.sockets['bot-a'].frames.put_nowait(OSError('fake disconnect'))
        socket = host.sockets['bot-a'] = host.gateways['bot-a'].add()
        socket.feed(10, d={'heartbeat_interval': 45000})
        await until(lambda: any(frame['op'] == 6 for frame in socket.sent))
        replayed = frames.feed('GROUP_MEMBER_ADD', 'MEMBERJ')
        socket.feed(0, t='RESUMED', s=replayed, d='')
    await until(entered.is_set)
    # The source is persisted before the notification (and the recovery ack) completes.
    (source,) = await autos(host)
    assert source['join']['session_id'] == 'session-bot-a'
    proof = await host.state.fact('bot-a', 'group-a', 'MEMBERJ')
    assert proof['reason'] == 'recovery_processing' and proof['join'] is None
    decision = await host.access.evaluate('bot-a', 'ninja3', 'query', 'group-a', 'MEMBERJ', action='invoke')
    assert (decision['allowed'], decision['reason'], decision['retryable']) == (False, 'group_facts_processing', True)
    # The member's query in this group waits behind the same-group lifecycle barrier.
    query = asyncio.create_task(host.say('账号 账号A', user='MEMBERJ'))
    await asyncio.sleep(0.05)
    assert not query.done()
    release.set()
    assert await query == CREDENTIALS
    assert host.state._ready('bot-a') and not (await host.ninja_api('GET', 'notices'))[1]['data']


@pytest.mark.parametrize('fault', ['write_failure', 'reload_access', 'reload_group_state'])
async def test_failed_or_cancelled_source_write_leaves_nothing_usable(ninja, monkeypatch, fault):
    host, frames = ninja, Frames(ninja)
    await host.setup()
    await auto(host)
    store = host.access.store
    if fault == 'write_failure':
        original = store._run
        failed = []

        def run(sql, params, read):
            if sql.startswith('INSERT') and ninja_row(params) and not failed:
                failed.append(True)
                raise sqlite3.OperationalError('fake disk I/O error')
            return original(sql, params, read)

        monkeypatch.setattr(store, '_run', run)
        frames.feed('GROUP_MEMBER_ADD', 'MEMBERJ')
        await until(lambda: failed and host.state.recovery.get_status('bot-a').failed)
        assert await host.say('账号列表', user='MEMBERJ') == UNAVAILABLE
    else:
        # The source reaches SQLite, then the writer is cancelled by a module reload.
        entered, released = hold(monkeypatch, store, ninja_row, after=True)
        frames.feed('GROUP_MEMBER_ADD', 'MEMBERJ')
        await until(entered.is_set)
        reload = asyncio.create_task(host.modules.reload({'reload_access': 'access_control', 'reload_group_state': 'group_state'}[fault]))
        await asyncio.sleep(0.05)
        released.set()
        assert await reload
        await until(lambda: not host.state.notifications.busy('bot-a'))
    if fault != 'reload_access':
        assert await host.modules.reload('access_control')
    host.access = host.modules.get('access_control')
    on_disk = await autos(host)
    assert len(on_disk) == (0 if fault == 'write_failure' else 1)
    assert await host.say('账号列表', user='MEMBERJ') == UNSURE
    await owner_rebuild(host, 'MEMBERJ')
    # Verified present now, but the automatic source never outlives its join basis.
    assert (await host.state.fact('bot-a', 'group-a', 'MEMBERJ'))['usable']
    assert await host.say('账号列表', user='MEMBERJ') == REFUSED


@pytest.mark.parametrize('change', ['rule_off', 'close_group', 'deny', 'blacklist'])
async def test_owner_changes_before_the_join_decision_leave_no_source(ninja, monkeypatch, change):
    from core.plugin._blacklist import get_blacklist_map, set_blacklist_map

    host, frames = ninja, Frames(ninja)
    await host.setup()
    await auto(host)
    entered, released = hold(monkeypatch, host.state.store, group_row)
    sequence = frames.feed('GROUP_MEMBER_ADD', 'MEMBERJ')
    await until(entered.is_set)
    users = get_blacklist_map('bot-a', 'user')
    try:
        if change == 'rule_off':
            await auto(host, enabled=False)
        elif change == 'close_group':
            await auto(host, open_group=False)
            # The owner's switch stays on in the plugin, but a closed group syncs no rule.
            assert (await host.ninja_api('GET', 'groups'))[1]['data'][0]['auto_grant'] is True
            assert (await host.access.access_snapshot('bot-a', 'ninja3'))['join_rules'] == []
        elif change == 'deny':
            await host.access.manage_access('bot-a', 'ninja3', 'deny', function='query', group='group-a', member='MEMBERJ')
        else:
            set_blacklist_map('bot-a', 'user', {**users, 'MEMBERJ': 'fake'})
        released.set()
        await settle(host, sequence)
        assert not await autos(host)
    finally:
        set_blacklist_map('bot-a', 'user', users)
    expected = {'rule_off': REFUSED, 'close_group': ['本群未开放账号查询'], 'deny': DENIED, 'blacklist': REFUSED}[change]
    assert await host.say('账号列表', user='MEMBERJ') == expected


async def test_leave_during_the_source_write_retires_it_and_rule_off_waits_for_it(ninja, monkeypatch):
    host, frames = ninja, Frames(ninja)
    await host.setup()
    await auto(host)
    entered, released = hold(monkeypatch, host.access.store, ninja_row)
    frames.feed('GROUP_MEMBER_ADD', 'MEMBERJ')
    await until(entered.is_set)
    # Queued behind the same-group barrier, then the owner's rule-off waits for the consumer lock.
    left = frames.feed('GROUP_MEMBER_REMOVE', 'MEMBERJ')
    rule_off = asyncio.create_task(auto(host, enabled=False))
    await asyncio.sleep(0.05)
    assert not rule_off.done()
    assert (await host.access.evaluate('bot-a', 'ninja3', 'query', 'group-a', 'MEMBERJ', action='invoke'))['reason'] == 'group_facts_processing'
    released.set()
    await rule_off
    await settle(host, left)
    snapshot = await host.access.access_snapshot('bot-a', 'ninja3')
    assert not await autos(host) and snapshot['revocations'][-1]['reason'] == 'group_lifecycle' and snapshot['join_rules'] == []
    assert await host.say('账号列表', user='MEMBERJ') == REFUSED
    await settle(host, frames.feed('GROUP_MEMBER_ADD', 'MEMBERJ'))
    assert not await autos(host) and await host.say('账号列表', user='MEMBERJ') == REFUSED


@pytest.mark.parametrize('gap', ['reload_group_state', 'restart_modules', 'new_session'])
@pytest.mark.parametrize('verification', ['platform', 'owner'])
async def test_reloads_restart_and_new_session_never_revive_an_automatic_source(ninja, gap, verification):
    host, frames = ninja, Frames(ninja)
    await host.setup()
    await auto(host)
    await settle(host, frames.feed('GROUP_MEMBER_ADD', 'MEMBERJ'))
    (source,) = await autos(host)
    # Reloading the consumer side keeps a current join basis usable.
    assert await host.modules.reload('access_control')
    host.access = host.modules.get('access_control')
    await host.plugins.reload('ninja3')
    assert_listed(await host.say('账号列表', user='MEMBERJ'))
    if gap == 'new_session':
        await new_session(host, frames)
    else:
        # In-process restart: every module re-reads its disk state under a new generation.
        assert await host.modules.reload('group_state')
        if gap == 'restart_modules':
            assert await host.modules.reload('access_control')
            await host.plugins.reload('ninja3')
            host.access = host.modules.get('access_control')
    assert await host.say('账号列表', user='MEMBERJ') == UNSURE
    if verification == 'owner':
        await host.state.confirm_member('bot-a', 'group-a', 'MEMBERJ', evidence='owner checked current presence')
    await owner_rebuild(host, 'MEMBERJ')
    proof = await host.state.fact('bot-a', 'group-a', 'MEMBERJ')
    assert proof['usable'] and proof['member']['cycle'] == source['cycle'] and proof['join'] is None
    assert await autos(host) == [source] and await host.say('账号列表', user='MEMBERJ') == REFUSED
    # Only the owner's own grant restores access, as a separate owner source.
    assert await host.say('授权', user='owner-a', mentions=['MEMBERJ']) == ['已授权该成员查询本群开放账号，长期有效']
    assert_listed(await host.say('账号列表', user='MEMBERJ'))


async def test_old_session_callback_finishing_late_cannot_make_its_source_usable(ninja, monkeypatch):
    host, frames = ninja, Frames(ninja)
    await host.setup()
    await auto(host)
    entered, released = hold(monkeypatch, host.access.store, ninja_row, after=True)
    frames.feed('GROUP_MEMBER_ADD', 'MEMBERJ')
    await until(entered.is_set)
    await new_session(host, frames)
    # The old session's writer is still running: no baseline can be established.
    assert host.state.notifications.busy('bot-a') and not await host.state.rebuild('bot-a')
    released.set()
    await until(lambda: not host.state.notifications.busy('bot-a'))
    # The cancelled write quarantines this consumer until the owner reloads authorization.
    assert await host.say('账号列表', user='MEMBERJ') == UNAVAILABLE
    assert await host.modules.reload('access_control')
    host.access = host.modules.get('access_control')
    assert len(await autos(host)) == 1
    await owner_rebuild(host, 'MEMBERJ')
    assert await host.say('账号列表', user='MEMBERJ') == REFUSED


async def test_owner_rule_narrowed_in_authorization_survives_queries_and_reloads(ninja):
    host, frames = ninja, Frames(ninja)
    await host.setup()
    await auto(host)
    assert (await host.ninja_api('GET', 'status'))[1]['data']['restricted_by_acl'] == []
    await host.access.manage_access('bot-a', 'ninja3', 'join_rule', function='query', group='group-a', enabled=False)
    report = (await host.ninja_api('GET', 'status'))[1]['data']
    assert report['restricted_by_acl'] == ['group-a（入群自动授权不一致）']
    assert (report['groups'][0]['plugin_auto_grant'], report['groups'][0]['acl_auto_grant']) == (True, False)
    await host.plugins.reload('ninja3')
    assert await host.say('账号列表', user='MEMBERJ') == REFUSED
    await settle(host, frames.feed('GROUP_MEMBER_ADD', 'MEMBERJ'))
    assert not await autos(host)
    assert (await host.access.access_snapshot('bot-a', 'ninja3'))['join_rules'] == []
    # Saving the group again is the explicit sync; only later joins are affected.
    await auto(host)
    assert await host.say('账号列表', user='MEMBERJ') == REFUSED
    await settle(host, frames.feed('GROUP_MEMBER_ADD', 'MEMBERK'))
    assert_listed(await host.say('账号列表', user='MEMBERK'))
    # Narrowing the policy scope in authorization leaves no dormant source for a later reopening.
    await host.access.manage_access('bot-a', 'ninja3', 'policy', function='query', policy={'kind': 'explicit', 'groups': []})
    await settle(host, frames.feed('GROUP_MEMBER_ADD', 'MEMBERN'))
    assert not await autos(host, 'MEMBERN')
    await auto(host)
    assert await host.say('账号列表', user='MEMBERN') == REFUSED
    with pytest.raises(ValueError):
        await host.access.manage_access('bot-a', 'ninja3', 'join_rule', function='query', group='group-a', enabled='yes')


async def test_rule_off_alone_waits_for_a_started_write_and_stops_later_joins(ninja, monkeypatch):
    host, frames = ninja, Frames(ninja)
    await host.setup()
    await auto(host)
    entered, released = hold(monkeypatch, host.access.store, ninja_row)
    first = frames.feed('GROUP_MEMBER_ADD', 'MEMBERJ')
    await until(entered.is_set)
    rule_off = asyncio.create_task(auto(host, enabled=False))
    await asyncio.sleep(0.05)
    # Serialized behind the consumer lock: the started decision and write finish first.
    assert not rule_off.done()
    released.set()
    assert (await rule_off)['acl_sync']['auto_grant_after'] is False
    await settle(host, first)
    (source,) = await autos(host)
    snapshot = await host.access.access_snapshot('bot-a', 'ninja3')
    assert snapshot['join_rules'] == [] and source['join']['event_id'] == f'event-{first}'
    # Established like an owner source (revocable in authorization); no join after rule-off adds one.
    assert_listed(await host.say('账号列表', user='MEMBERJ'))
    await settle(host, frames.feed('GROUP_MEMBER_ADD', 'MEMBERK'))
    assert not await autos(host, 'MEMBERK') and await host.say('账号列表', user='MEMBERK') == REFUSED


async def test_owner_management_entry_can_only_turn_join_rules_off(ninja):
    host = ninja
    await host.setup()
    await auto(host)
    body = {'operation': 'join_rule', 'function': 'query', 'group': 'group-a'}
    # Neither for ninja3 nor for the original account plugin can the generic entry enable automatic grants.
    await host.create(name='旧插件账户')
    for consumer in ('ninja3', 'account_sharing'):
        for enabled in (True, 'yes', None):
            response = await request(host, {'consumer': consumer, **body, **({} if enabled is None else {'enabled': enabled})}, consumer=consumer)
            assert response.status == 422, await response.text()
    assert (await host.access.access_snapshot('bot-a', 'account_sharing')).get('join_rules', []) == []
    response = await request(host, {'consumer': 'ninja3', **body, 'enabled': False}, consumer='ninja3')
    assert response.status == 200, await response.text()
    assert (await host.access.access_snapshot('bot-a', 'ninja3'))['join_rules'] == []
