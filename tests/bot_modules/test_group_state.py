"""Independent module installation and owner management behavior."""

import asyncio
import json
import shutil
import sqlite3
import subprocess
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


def test_group_state_page_request_ordering():
    node = shutil.which('node')
    if node is None:
        pytest.skip('页面脚本回归需要 Node.js；不使用额外 npm 依赖')
    result = subprocess.run([node, '--test', str(Path(__file__).with_name('test_group_state_page.cjs'))], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


async def test_independent_known_groups_owner_entry_refresh_and_restart(group_host):
    host = group_host
    assert (await host.api(authenticated=False)).status == 401
    await host.event('普通聊天', user='member-a', group='group-a')
    state = (await (await host.api()).json())['data']
    assert state['scope'] == 'known' and state['complete'] is False
    assert [g['group'] for g in state['groups']] == ['group-a']
    assert state['groups'][0]['reliable'] is False
    assert (await (await host.api(bot='bot-b')).json())['data']['groups'] == []
    assert (await host.api('POST', 'refresh', body={'group': 'group-a'}, origin='https://external.example')).status == 403
    host.sender.messages.clear()
    await host.event('群状态', user='ordinary')
    assert host.sender.messages == []
    await host.event('群状态 刷新 group-a')
    refreshed = await host.state.snapshot('bot-a')
    group = refreshed['groups'][0]
    assert group['reliable'] and group['bot_state']['member_role'] == 'admin'
    assert group['bot_state']['in_group'] is True
    assert 'group-a' in host.sender.messages[-1][2]
    group['bot_state']['member_role'] = 'forged'
    assert (await host.state.snapshot('bot-a'))['groups'][0]['bot_state']['member_role'] == 'admin'
    assert await host.modules.reload('group_state')
    restored = (await host.state.snapshot('bot-a'))['groups'][0]
    assert restored['group'] == 'group-a' and not restored['reliable']
    assert restored['bot_state']['member_role'] == 'admin'
    assert restored['reason'] == 'process_restart'
    host.sender.messages.clear()
    await host.event('群状态')
    assert len(host.sender.messages) == 1
    from core.plugin.web_pages import get_page_html, match_route

    assert '已知群' in get_page_html('group-state')
    await host.modules.disable('group_state', _persist=False)
    assert get_page_html('group-state') is None
    assert match_route('GET', '/api/ext/group-state/') is None


@pytest.mark.parametrize('response,reason', [(None, 'response_unknown'), ((None, {'code': '11253'}), 'permission_denied'), (({}, None), 'response_unknown')])
async def test_failed_refresh_retains_history_and_never_claims_departure(group_host, response, reason):
    host = group_host
    assert (await host.api('POST', 'refresh', body={'group': 'group-a'})).status == 200
    host.sender.response = response
    result = (await (await host.api('POST', 'refresh', body={'group': 'group-a'})).json())['data']
    assert not result['reliable'] and result['reason'] == reason
    assert result['bot_state']['in_group'] is True  # explicitly historical; error is not absence
    assert result['status'] == 'historical'


async def test_observation_during_refresh_invalidates_older_result(group_host):
    host = group_host

    async def change():
        await host.event('普通聊天', user='member-a', group='group-a')

    host.sender.after_query = change
    result = (await (await host.api('POST', 'refresh', body={'group': 'group-a'})).json())['data']
    assert result['reason'] == 'state_changed' and not result['reliable']


async def test_refresh_completion_changes_snapshot_version(group_host):
    host = group_host
    pending = []

    async def capture():
        pending.append(await host.state.snapshot('bot-a'))

    host.sender.after_query = capture
    await host.api('POST', 'refresh', body={'group': 'group-a'})
    final = await host.state.snapshot('bot-a')
    assert pending[0]['groups'][0]['status'] == 'pending'
    assert final['version'] > pending[0]['version']
    assert final['groups'][0]['version'] > pending[0]['groups'][0]['version']


@pytest.mark.parametrize('entry', ['http', 'qq'])
async def test_failed_commit_keeps_previous_bot_state_untrusted_until_retry(group_host, entry):
    host = group_host
    host.sender.response = ({'member_role': 'member', 'recv_msg_setting': 'all', 'allow_proactive_msg': False}, None)
    await host.api('POST', 'refresh', body={'group': 'group-a'})
    database = host.modules.get_context('group_state').get_data_path('group-state.db')
    with sqlite3.connect(database) as connection:
        connection.execute("""CREATE TRIGGER reject_verified BEFORE INSERT ON extension_records
            WHEN EXISTS (SELECT 1 FROM json_each(NEW.payload, '$.groups') WHERE json_extract(value, '$.reliable') = 1)
            BEGIN SELECT RAISE(ABORT, 'fake disk failure'); END""")
    host.sender.response = ({'member_role': 'admin', 'recv_msg_setting': 'all', 'allow_proactive_msg': True}, None)
    if entry == 'http':
        response = await host.api('POST', 'refresh', body={'group': 'group-a'})
        assert response.status == 503
    else:
        await host.event('群状态 刷新 group-a')
        assert '存储暂不可用' in host.sender.messages[-1][2]
    failed = (await (await host.api()).json())['data']['groups'][0]
    assert not failed['reliable']
    assert failed['status'] == 'historical' and failed['reason'] == 'persistence_failed'
    assert failed['bot_state']['member_role'] == 'member'
    assert (await (await host.api(bot='bot-b')).json())['data']['groups'] == []
    with sqlite3.connect(database) as connection:
        connection.execute('DROP TRIGGER reject_verified')
    result = (await (await host.api('POST', 'refresh', body={'group': 'group-a'})).json())['data']
    assert result['reliable'] and result['bot_state']['member_role'] == 'admin'
    assert result['version'] > failed['version']


@pytest.mark.parametrize('observed_group', ['', 'group-a', 'group-b'])
async def test_pending_commit_does_not_publish_or_overwrite_new_observations(group_host, observed_group):
    host = group_host
    entered = asyncio.Event()
    database = host.modules.get_context('group_state').get_data_path('group-state.db')
    blocker = sqlite3.connect(database)

    async def lock_final_write():
        blocker.execute('BEGIN IMMEDIATE')
        entered.set()

    host.sender.after_query = lock_final_write
    pending = asyncio.create_task(host.api('POST', 'refresh', body={'group': 'group-a'}))
    observation = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        during = (await (await host.api()).json())['data']
        assert not during['groups'][0]['reliable'] and during['groups'][0]['status'] == 'pending'
        if observed_group:
            observation = asyncio.create_task(host.event('普通消息', user='member-a', group=observed_group))
            await asyncio.sleep(0)
            observed = (await (await host.api()).json())['data']
            assert any(group['group'] == observed_group and group['source'] == 'native_event' for group in observed['groups'])
    finally:
        blocker.rollback()
        blocker.close()
        response = await pending
        if observation:
            await observation
    result = (await response.json())['data']
    final = (await (await host.api()).json())['data']
    if observed_group == 'group-a':
        assert not result['reliable'] and result['reason'] == 'state_changed'
        assert final['groups'][0]['source'] == 'native_event' and not final['groups'][0]['reliable']
    else:
        assert result['reliable'] and final['version'] > during['version']
        assert final['groups'][0]['version'] > during['groups'][0]['version']
    if observed_group == 'group-b':
        assert [group['group'] for group in final['groups']] == ['group-a', 'group-b']
    assert await host.modules.reload('group_state')
    restored = (await (await host.api()).json())['data']
    assert [group['group'] for group in restored['groups']] == [group['group'] for group in final['groups']]
    assert all(not group['reliable'] for group in restored['groups'])


@pytest.mark.parametrize('command', ['群状态', '群状态 刷新 group-a'])
async def test_group_owner_command_reports_observation_storage_failure_and_can_retry(group_host, command):
    host = group_host
    previous = (await (await host.api('POST', 'refresh', body={'group': 'group-a'})).json())['data']
    other_bot = await host.state.snapshot('bot-b')
    database = host.modules.get_context('group_state').get_data_path('group-state.db')
    with sqlite3.connect(database) as connection:
        connection.execute("""CREATE TRIGGER reject_observation BEFORE INSERT ON extension_records
            WHEN NEW.bot = 'bot-a' BEGIN SELECT RAISE(ABORT, 'fake disk failure'); END""")
    try:
        host.sender.messages.clear()
        await host.event(command, group='group-a')
        assert host.sender.messages == [('bot-a', 'group-a', '群状态存储暂不可用，请检查后台')]
        failed = (await (await host.api()).json())['data']['groups'][0]
        assert not failed['reliable'] and failed['status'] == 'historical'
        assert failed['reason'] == 'persistence_failed' and failed['source'] == 'native_event'
        assert failed['bot_state'] == previous['bot_state'] and failed['version'] > previous['version']
        assert await host.state.snapshot('bot-b') == other_bot
    finally:
        with sqlite3.connect(database) as connection:
            connection.execute('DROP TRIGGER reject_observation')
    host.sender.messages.clear()
    await host.event('群状态 刷新 group-a', group='group-a')
    assert len(host.sender.messages) == 1 and '已处理' in host.sender.messages[0][2]
    assert (await host.state.snapshot('bot-a'))['groups'][0]['reliable']


async def test_observation_storage_failure_preserves_admission_and_message_dispatch(group_host):
    from core.application import get_app
    from core.plugin._blacklist import get_blacklist_map, set_blacklist_map

    host = group_host
    await host.event('ordinary', group='group-a', user='member-a')
    database = host.modules.get_context('group_state').get_data_path('group-state.db')
    downstream = []

    async def receive(event):
        downstream.append((event.appid, event.user_id, event.content))
        return event

    get_app().hook_manager.register('before_event', receive, owner='observation-failure-test', priority=20)
    with sqlite3.connect(database) as connection:
        connection.execute("""CREATE TRIGGER reject_observation BEFORE INSERT ON extension_records
            WHEN NEW.bot = 'bot-a' BEGIN SELECT RAISE(ABORT, 'fake disk failure'); END""")
    users = get_blacklist_map('bot-a', 'user')
    try:
        host.sender.messages.clear()
        await host.event('ordinary', group='group-a', user='member-a')
        await host.event('群状态', group='group-a', user='member-a')
        await host.event('群状态', bot='bot-b', group='group-b', user='owner-a')
        assert downstream == [('bot-a', 'member-a', 'ordinary'), ('bot-a', 'member-a', '群状态'), ('bot-b', 'owner-a', '群状态')]
        assert host.sender.messages == []
        set_blacklist_map('bot-a', 'user', {**users, 'owner-a': 'test denial'})
        await host.event('群状态', group='group-a')
        assert len(downstream) == 3 and host.sender.messages == []
        assert (await host.state.snapshot('bot-a'))['groups'][0]['reason'] == 'persistence_failed'
    finally:
        set_blacklist_map('bot-a', 'user', users)
        get_app().hook_manager.unregister_owner('observation-failure-test')
        with sqlite3.connect(database) as connection:
            connection.execute('DROP TRIGGER reject_observation')


@pytest.mark.parametrize('newer_action', ['observe', 'refresh'])
async def test_late_observation_save_failure_preserves_newer_same_group_facts(group_host, monkeypatch, newer_action):
    host = group_host
    await host.api('POST', 'refresh', body={'group': 'group-a'})
    entered, release = threading.Event(), threading.Event()
    attempts = []
    connect = sqlite3.connect

    def fail_first_write():
        attempts.append(True)
        if len(attempts) == 1:
            entered.set()
            if not release.wait(2):
                raise RuntimeError('fake storage scheduling timeout')
            return 1
        return 0

    def test_connection(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.create_function('audit_fail_first_write', 0, fail_first_write)
        return connection

    monkeypatch.setattr(sqlite3, 'connect', test_connection)
    assert await host.modules.reload('group_state')
    database = host.modules.get_context('group_state').get_data_path('group-state.db')
    with sqlite3.connect(database) as connection:
        connection.execute("""CREATE TRIGGER fail_first_observation BEFORE INSERT ON extension_records
            WHEN audit_fail_first_write() = 1 BEGIN SELECT RAISE(ABORT, 'fake disk failure'); END""")
    old = asyncio.create_task(host.event('old observation', group='group-a', user='member-a'))
    newer = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        before = await host.state.snapshot('bot-a')
        newer = asyncio.create_task(
            host.state.refresh('bot-a', 'group-a') if newer_action == 'refresh' else host.event('new observation', group='group-a', user='member-a')
        )
        await asyncio.sleep(0)
        assert (await host.state.snapshot('bot-a'))['version'] > before['version']
    finally:
        release.set()
        await old
        if newer is not None:
            await newer
        with sqlite3.connect(database) as connection:
            connection.execute('DROP TRIGGER fail_first_observation')
    fact = (await host.state.snapshot('bot-a'))['groups'][0]
    if newer_action == 'refresh':
        assert fact['reliable'] and fact['status'] == 'verified' and fact['reason'] == ''
    else:
        assert not fact['reliable'] and fact['reason'] == 'observed_event_requires_refresh'


async def test_initial_save_failure_ends_refresh_and_allows_retry(group_host):
    host = group_host
    previous = (await (await host.api('POST', 'refresh', body={'group': 'group-a'})).json())['data']
    database = host.modules.get_context('group_state').get_data_path('group-state.db')
    with sqlite3.connect(database) as connection:
        connection.execute("""CREATE TRIGGER reject_all BEFORE INSERT ON extension_records
            BEGIN SELECT RAISE(ABORT, 'fake disk failure'); END""")
    try:
        assert (await host.api('POST', 'refresh', body={'group': 'group-a'})).status == 503
        failed = (await (await host.api()).json())['data']['groups'][0]
        assert failed['status'] == 'historical' and failed['reason'] == 'persistence_failed'
        assert not failed['reliable'] and failed['bot_state'] == previous['bot_state']
        assert failed['version'] > previous['version']
        assert (await (await host.api(bot='bot-b')).json())['data']['groups'] == []
    finally:
        with sqlite3.connect(database) as connection:
            connection.execute('DROP TRIGGER reject_all')
    retried = (await (await host.api('POST', 'refresh', body={'group': 'group-a'})).json())['data']
    assert retried['reliable'] and retried['version'] > failed['version']


@pytest.mark.parametrize('observed_group', ['', 'group-a', 'group-b'])
async def test_cancelled_query_ends_refresh_without_overwriting_new_facts(group_host, observed_group):
    host = group_host
    previous = (await (await host.api('POST', 'refresh', body={'group': 'group-a'})).json())['data']
    entered = asyncio.Event()

    async def wait_for_cancellation():
        entered.set()
        await asyncio.Event().wait()

    host.sender.after_query = wait_for_cancellation
    pending = asyncio.create_task(host.state.refresh('bot-a', 'group-a'))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        during = await host.state.snapshot('bot-a')
        if observed_group:
            await host.event('普通消息', user='member-a', group=observed_group)
    finally:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    ended = await host.state.snapshot('bot-a')
    fact = ended['groups'][0]
    assert not fact['reliable'] and fact['status'] == 'historical'
    assert fact['bot_state'] == previous['bot_state']
    assert ended['version'] > during['version']
    if observed_group == 'group-a':
        assert fact['reason'] == 'observed_event_requires_refresh' and fact['source'] == 'native_event'
    else:
        assert fact['reason'] == 'refresh_cancelled'
    assert [group['group'] for group in ended['groups']] == (['group-a', 'group-b'] if observed_group == 'group-b' else ['group-a'])
    assert (await host.state.snapshot('bot-b'))['groups'] == []
    host.sender.after_query = None
    assert (await host.state.refresh('bot-a', 'group-a'))['reliable']


@pytest.mark.parametrize('role', [[], {}])
@pytest.mark.parametrize('entry', ['http', 'qq'])
async def test_malformed_platform_role_is_a_historical_error(group_host, role, entry):
    host = group_host
    previous = (await (await host.api('POST', 'refresh', body={'group': 'group-a'})).json())['data']
    host.sender.response = ({'member_role': role, 'recv_msg_setting': 'all', 'allow_proactive_msg': True}, None)
    if entry == 'http':
        response = await host.api('POST', 'refresh', body={'group': 'group-a'})
        assert response.status == 200
        result = (await response.json())['data']
    else:
        host.sender.messages.clear()
        await host.event('群状态 刷新 group-a')
        assert len(host.sender.messages) == 1
        result = json.loads(host.sender.messages[-1][2].split('\n', 1)[1])
    assert not result['reliable'] and result['status'] == 'historical'
    assert result['reason'] == 'response_unknown' and result['bot_state'] == previous['bot_state']
    assert (await host.state.snapshot('bot-a'))['groups'][0] == result


async def test_reload_during_final_commit_rejects_old_http_result(group_host, monkeypatch):
    host = group_host
    entered, release = asyncio.Event(), asyncio.Event()
    old = host.state
    original = old.store.put

    async def delayed_return(bot, collection, key, record):
        await original(bot, collection, key, record)
        if record['groups']['group-a']['reliable']:
            entered.set()
            await release.wait()

    monkeypatch.setattr(old.store, 'put', delayed_return)
    pending = asyncio.create_task(host.api('POST', 'refresh', body={'group': 'group-a'}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert await host.modules.reload('group_state')
        current = (await (await host.api()).json())['data']
        assert not current['groups'][0]['reliable'] and current['groups'][0]['reason'] == 'process_restart'
    finally:
        release.set()
        response = await pending
    assert response.status == 503
    with pytest.raises(RuntimeError):
        await old.snapshot('bot-a')


@pytest.mark.parametrize('entry', ['http', 'snapshot'])
@pytest.mark.parametrize('lifecycle', ['disable', 'reload'])
async def test_cold_and_queued_reads_are_rejected_across_unload(group_host, monkeypatch, entry, lifecycle):
    host = group_host
    await host.state.refresh('bot-a', 'group-a')
    entered, release = threading.Event(), threading.Event()
    connect = sqlite3.connect

    class DelayedReadConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            cursor = super().execute(sql, parameters)
            if sql.startswith('SELECT payload') and parameters[0] == 'bot-a':
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('fake storage scheduling timeout')
            return cursor

    def test_connection(*args, **kwargs):
        return connect(*args, **kwargs, factory=DelayedReadConnection)

    monkeypatch.setattr(sqlite3, 'connect', test_connection)
    assert await host.modules.reload('group_state')
    old = host.state

    async def read():
        return await host.api() if entry == 'http' else await old.snapshot('bot-a')

    first = asyncio.create_task(read())
    queued = transition = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        queued = asyncio.create_task(old.snapshot('bot-a'))
        await asyncio.sleep(0)
        assert not queued.done()
        transition = asyncio.create_task(host.modules.reload('group_state') if lifecycle == 'reload' else host.modules.disable('group_state', _persist=False))
        await asyncio.sleep(0)
        assert (await host.api()).status == 404
    finally:
        release.set()
        results = await asyncio.gather(first, *([queued] if queued is not None else []), return_exceptions=True)
        if transition is not None:
            assert await transition
    assert len(results) == 2
    if entry == 'http':
        assert results[0].status == 503
        assert (await results[0].json())['success'] is False
    else:
        assert isinstance(results[0], RuntimeError)
    assert isinstance(results[1], RuntimeError)
    with pytest.raises(RuntimeError):
        await old.snapshot('bot-a')
    if lifecycle == 'disable':
        assert (await host.api()).status == 404
        assert await host.modules.enable('group_state', _skip_deps=True, _persist=False)
    current = await host.state.snapshot('bot-a')
    assert current['generation'] != old.generation
    assert current['groups'][0]['group'] == 'group-a'
    assert not current['groups'][0]['reliable'] and current['groups'][0]['reason'] == 'process_restart'
    assert (await host.state.refresh('bot-a', 'group-a'))['reliable']


async def test_disable_allows_event_loop_to_run_while_sqlite_finishes(group_host, monkeypatch):
    host = group_host
    entered, release = threading.Event(), threading.Event()
    released_before_timeout = []
    connect = sqlite3.connect

    def pause_write():
        entered.set()
        released_before_timeout.append(release.wait(2))
        return 1

    def test_connection(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.create_function('audit_pause', 0, pause_write)
        return connection

    monkeypatch.setattr(sqlite3, 'connect', test_connection)
    assert await host.modules.reload('group_state')
    database = host.modules.get_context('group_state').get_data_path('group-state.db')
    with sqlite3.connect(database) as connection:
        connection.execute("""CREATE TRIGGER delay_verified BEFORE INSERT ON extension_records
            WHEN EXISTS (SELECT 1 FROM json_each(NEW.payload, '$.groups') WHERE json_extract(value, '$.reliable') = 1)
            BEGIN SELECT audit_pause(); END""")
    pending = asyncio.create_task(host.api('POST', 'refresh', body={'group': 'group-a'}))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        asyncio.get_running_loop().call_later(0.01, release.set)
        assert await host.modules.disable('group_state', _persist=False)
    finally:
        release.set()
        response = await pending
    assert released_before_timeout == [True]
    assert response.status == 503
    assert (await host.api()).status == 404


@pytest.mark.parametrize('cancellations', [1, 2])
async def test_cancelled_refresh_finishes_its_write_before_new_facts_are_saved(group_host, monkeypatch, cancellations):
    host = group_host
    entered, release = threading.Event(), threading.Event()
    original = host.state.store._run

    def pause_before_database_lock(sql, params, read):
        # Fix thread scheduling at the SQLite boundary while retaining real writes.
        if sql.startswith('INSERT') and json.loads(params[3])['groups']['group-a']['reliable']:
            entered.set()
            if not release.wait(2):
                raise RuntimeError('fake storage scheduling timeout')
        return original(sql, params, read)

    monkeypatch.setattr(host.state.store, '_run', pause_before_database_lock)
    pending = asyncio.create_task(host.state.refresh('bot-a', 'group-a'))
    observation = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        for _ in range(cancellations):
            pending.cancel()
            await asyncio.sleep(0)
        observation = asyncio.create_task(host.event('普通消息', user='member-a', group='group-b'))
        during = (await (await host.api()).json())['data']
        assert not during['groups'][0]['reliable']
        assert not pending.done()  # Cancellation must drain the underlying write.
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        if observation:
            await observation
    assert await host.modules.reload('group_state')
    restored = (await (await host.api()).json())['data']
    assert [group['group'] for group in restored['groups']] == ['group-a', 'group-b']
    assert all(not group['reliable'] for group in restored['groups'])


@pytest.mark.parametrize('command', ['群状态', '群状态 刷新 group-a'])
async def test_owner_reply_failure_keeps_command_consumed_without_resending(group_host, caplog, command):
    from core.application import get_app

    host = group_host
    downstream = []
    sent = []

    async def receive(event):
        downstream.append(event.content)
        return event

    async def failing_reply(event, content=None, **kwargs):
        sent.append(content)
        raise RuntimeError('fake transport reset')

    get_app().hook_manager.register('before_event', receive, owner='reply-failure-test', priority=20)
    host.sender.reply = failing_reply
    try:
        with caplog.at_level('WARNING', logger='ElainaBot'):
            await host.event(command, group='group-a')
    finally:
        del host.sender.reply
        get_app().hook_manager.unregister_owner('reply-failure-test')
    assert downstream == [] and len(sent) == 1
    assert any('fake transport reset' in record.getMessage() for record in caplog.records)
    group = (await host.state.snapshot('bot-a'))['groups'][0]
    assert group['reliable'] is (command != '群状态')
    host.sender.messages.clear()
    await host.event('群状态', group='group-a')
    assert len(host.sender.messages) == 1


@pytest.mark.parametrize('failure', ['page', 'cancel'])
async def test_setup_failure_after_registration_releases_only_its_own_resources(group_host, monkeypatch, failure):
    from core.application import get_app
    from core.plugin import web_pages

    host = group_host
    await host.modules.disable('group_state', _persist=False)
    other = AsyncMock()
    web_pages.register_route('GET', '/api/ext/other-module/', other, auth=True)
    web_pages.register_page('other-module', 'Other', source='module', source_name='other')
    newer = AsyncMock()
    closed = []

    def failing_register_page(key, *args, **kwargs):
        closed.extend(entry[2].__self__ for entry in get_app().hook_manager._hooks['before_event'] if entry[1] == '群状态')
        if failure == 'cancel':
            raise asyncio.CancelledError
        # A newer instance replaced the shared route before this failed attempt unwinds.
        web_pages.register_route('*', '/api/ext/group-state/*', newer, auth=True)
        raise RuntimeError('fake page failure')

    monkeypatch.setattr(web_pages, 'register_page', failing_register_page)
    try:
        if failure == 'cancel':
            with pytest.raises(asyncio.CancelledError):
                await host.modules.enable('group_state', _skip_deps=True, _persist=False)
        else:
            assert not await host.modules.enable('group_state', _skip_deps=True, _persist=False)
        hooks = get_app().hook_manager.list_hooks().get('before_event', [])
        assert [entry for entry in hooks if entry['owner'] == '群状态'] == []
        assert web_pages.get_page_html('group-state') is None
        route = web_pages.match_route('GET', '/api/ext/group-state/')
        assert (route is None) if failure == 'cancel' else (route['handler'] is newer)
        assert web_pages.match_route('GET', '/api/ext/other-module/')['handler'] is other
        assert web_pages.get_page_html('other-module') is not None
        assert host.modules.get('group_state') is None
        assert len(closed) == 1 and closed[0].active is False
    finally:
        web_pages.unregister_route('GET', '/api/ext/other-module/')
        web_pages.unregister_page('other-module')
        web_pages.unregister_route('*', '/api/ext/group-state/*')
    monkeypatch.undo()
    assert await host.modules.enable('group_state', _skip_deps=True, _persist=False)
    await host.event('ordinary', group='group-a', user='member-a')
    assert (await host.state.snapshot('bot-a'))['groups'][0]['group'] == 'group-a'
