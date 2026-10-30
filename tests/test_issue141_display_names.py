import asyncio
from types import SimpleNamespace

import pytest

from core.application import Application
from core.bot.event import EventHandlerMixin
from core.display import display_group, display_member
from core.message.event import GROUP_MESSAGE_CREATE, Event
from core.message.parsers.channel import ChannelMessageParser
from core.message.sender import MessageSender
from core.storage.log import LogService


class _FakeLogService:
    def __init__(self):
        self.users = {}
        self.groups = {}
        self.queries = []

    def db_queue(self, sql, params=()):
        self.queries.append((sql, params))
        if 'users' in sql:
            uid, name = params
            self.users[uid] = name
        else:
            raise AssertionError(f'unexpected SQL: {sql}')

    async def group_member_touch(self, *args, **kwargs):
        return False

    async def db_fetch_one(self, sql, params=()):
        if 'FROM users' in sql:
            name = self.users.get(str(params[0]))
            return {'name': name} if name is not None else None
        if 'FROM groups_users' in sql:
            name = self.groups.get(str(params[0]))
            return {'group_name': name} if name is not None else None
        raise AssertionError(f'unexpected SQL: {sql}')


@pytest.mark.asyncio
async def test_display_member_unknown_and_cleaned_name(monkeypatch):
    service = _FakeLogService()
    service.users['member'] = 'chair\nowner\u2028team'
    service.users['long'] = 'x' * 100
    bot = SimpleNamespace(log_service=service)
    app = SimpleNamespace(get_bot=lambda appid: bot if appid == 'app' else None)
    monkeypatch.setattr('core.application._app', app)

    assert await display_member('app', 'member') == 'chairownerteam（…mber）'
    assert await display_member('app', 'long') == f"{'x' * 64}（…long）"
    assert await display_member('app', 'missing') == '未知成员（…sing）'


@pytest.mark.asyncio
async def test_display_group_fills_missing_name_once_and_degrades(monkeypatch):
    service = _FakeLogService()
    calls = []

    async def get_group_info(group_id, *, return_error=False):
        calls.append(group_id)
        service.groups[group_id] = '白\n子'
        return ({'group_name': '白\n子'}, None) if return_error else {'group_name': '白\n子'}

    bot = SimpleNamespace(log_service=service, sender=SimpleNamespace(get_group_info=get_group_info))
    app = SimpleNamespace(get_bot=lambda appid: bot if appid == 'app' else None)
    monkeypatch.setattr('core.application._app', app)

    assert await display_group('app', 'group') == '白子'
    assert calls == ['group']
    assert await display_group('app', 'group') == '白子'
    assert calls == ['group']

    async def fail_group_info(group_id, *, return_error=False):
        calls.append(group_id)
        raise RuntimeError('unavailable')

    bot.sender.get_group_info = fail_group_info
    assert await display_group('app', 'unknown') == '未知群（…nown）'


def test_mention_names_ignore_bots_and_keep_id_variants():
    event = SimpleNamespace(
        mentions=[
            {'id': 'self', 'username': '自己', 'is_you': True},
            {'member_openid': 'bot', 'username': '机器人', 'bot': True},
            {'openid': 'member', 'username': '成员\n名'},
        ]
    )

    assert EventHandlerMixin._mention_names(event) == (('member', '成员名'),)


def test_channel_parser_preserves_mentions_for_tracking():
    event = Event()
    ChannelMessageParser().parse(
        event,
        {
            'author': {'id': 'sender', 'username': '发送者'},
            'content': '<@member> hi',
            'mentions': [{'id': 'member', 'username': '被提及'}],
        },
    )

    assert event.mentions == [{'id': 'member', 'username': '被提及'}]


@pytest.mark.asyncio
async def test_message_entrypoint_queues_mention_names_without_waiting_for_db():
    handler = EventHandlerMixin()
    handler._init_event_state()
    service = _FakeLogService()
    bot = SimpleNamespace(log_service=service)
    event = SimpleNamespace(
        user_id='sender',
        group_id='group',
        is_group=True,
        member_role='',
        username='发送者',
        is_bot=False,
        is_direct=False,
        event_type=GROUP_MESSAGE_CREATE,
        mentions=[
            {'id': 'member', 'username': '被提及'},
            {'id': 'bot', 'username': '机器人', 'bot': True},
        ],
    )

    handler._enqueue_track(bot, event, 'app')
    assert handler._track_queue.qsize() == 1
    await handler._track_queue.join()

    assert service.users == {'sender': '发送者', 'member': '被提及'}
    for worker in handler._track_workers:
        worker.cancel()
    await asyncio.gather(*handler._track_workers, return_exceptions=True)


@pytest.mark.asyncio
async def test_application_display_member_uses_bot_scoped_read_only_api():
    service = _FakeLogService()
    service.users['member'] = '成员'
    app = Application()
    app._bot_registry = SimpleNamespace(get=lambda appid: SimpleNamespace(log_service=service))

    assert await app.display_member('app', 'member') == '成员（…mber）'
    assert service.queries == []


@pytest.mark.asyncio
async def test_latest_name_survives_repeated_group_message_state(monkeypatch):
    handler = EventHandlerMixin()
    handler._init_event_state()
    service = _FakeLogService()
    bot = SimpleNamespace(log_service=service)
    monkeypatch.setattr('core.application._app', SimpleNamespace(get_bot=lambda appid: bot))
    event = SimpleNamespace(
        user_id='sender', group_id='group', is_group=True, member_role='',
        username='旧名', is_bot=False, is_direct=False,
        event_type=GROUP_MESSAGE_CREATE, mentions=[],
    )
    try:
        for name in ('旧名', '新名', '旧名'):
            event.username = name
            handler._enqueue_track(bot, event, 'app')
            await handler._track_queue.join()
        assert await display_member('app', 'sender') == '旧名（…nder）'
    finally:
        for worker in handler._track_workers:
            worker.cancel()
        await asyncio.gather(*handler._track_workers, return_exceptions=True)


@pytest.fixture
async def stored_names(tmp_path, monkeypatch):
    service = LogService(str(tmp_path), 'app')
    bot = SimpleNamespace(log_service=service)
    handler = Application()
    handler._bot_registry = SimpleNamespace(get=lambda appid: bot if appid in ('app', '123456') else None)
    monkeypatch.setattr('core.application._app', handler)
    monkeypatch.setattr('core.plugin._dispatch._DispatchMixin._cached_app', None)
    try:
        yield handler, bot, service
    finally:
        for worker in handler._track_workers:
            worker.cancel()
        await asyncio.gather(*handler._track_workers, return_exceptions=True)
        await service.shutdown()


def _message(username, mentions=(), user_id='sender'):
    return SimpleNamespace(
        user_id=user_id, group_id='group', is_group=True, member_role='',
        username=username, is_bot=False, is_direct=False,
        event_type=GROUP_MESSAGE_CREATE, mentions=list(mentions),
    )


@pytest.mark.asyncio
async def test_queued_names_persist_and_rename_without_changing_native_ids(stored_names):
    handler, bot, service = stored_names
    # Same sender/group and nonempty names exercise the existing dedup path.
    for name in ('旧名', '新名', '旧名'):
        event = _message(name, [
            {'id': 'member-81CB', 'username': name + '\n\t\x00\u2028'},
            {'id': 'robot', 'username': '机器人', 'bot': True},
            {'id': 'self', 'username': '自身', 'is_you': True},
        ])
        handler._enqueue_track(bot, event, 'app')
        await handler._track_queue.join()
        # Production SQL and queue, flushed only by this test's storage harness.
        await service._flush_data_queue()
        assert await display_member('app', 'sender') == name + '（…nder）'
        assert await display_member('app', 'member-81CB') == name + '（…81CB）'
        assert event.user_id == 'sender'
    assert await display_member('app', 'robot') == '未知成员（…obot）'
    assert await display_member('app', 'self') == '未知成员（…self）'
    assert await display_member('another-app', 'member-81CB') == '未知成员（…81CB）'


@pytest.mark.asyncio
async def test_name_writes_are_queued_before_any_tracking_worker_runs(stored_names):
    handler, bot, service = stored_names
    handler._enqueue_track(bot, _message('发送者', [{'id': 'member', 'username': '成员'}]), 'app')
    # No await has let a worker or DB operation run yet.
    assert service._data_write_queue.qsize() == 2
    await service._flush_data_queue()
    assert await display_member('app', 'member') == '成员（…mber）'


@pytest.mark.asyncio
async def test_read_only_names_preserve_stored_permission_state(stored_names):
    _, _, service = stored_names
    await service.db_execute('INSERT INTO users (user_id, name, state) VALUES (?, ?, ?)', ('owner', '普通成员', 7))
    await service.db_execute('INSERT INTO users (user_id, name, state) VALUES (?, ?, ?)', ('member', 'owner', 0))
    before = await service.db_fetch_all('SELECT * FROM users ORDER BY user_id')
    assert await display_member('app', 'owner') == '普通成员（…wner）'
    assert await display_member('app', 'member') == 'owner（…mber）'
    assert await service.db_fetch_all('SELECT * FROM users ORDER BY user_id') == before


@pytest.mark.asyncio
async def test_display_lookup_preserves_owner_only_dispatch(stored_names, api_config_dir, tmp_path):
    from core.base.config import cfg
    from core.plugin.manager import PluginManager

    _, bot, service = stored_names
    cfg._ready = False
    cfg.init(f'{api_config_dir}/config')
    cfg.set_bot_setting('123456', 'owner_ids', ['owner'])
    plugin_dir = tmp_path / 'plugins' / 'display_permission'
    plugin_dir.mkdir(parents=True)
    (plugin_dir / 'check.py').write_text(
        "from core.plugin.decorators import handler\n"
        "@handler(r'^check$', owner_only=True)\n"
        "async def check(event, match):\n"
        "    await event.reply('allowed')\n",
    )
    manager = PluginManager(str(plugin_dir.parent), '123456')
    await manager.load('display_permission')
    manager._rebuild_handler_list()
    replies = asyncio.Queue()

    async def reply(event, content=None, **kwargs):
        await replies.put((content, kwargs.get('template_name')))

    bot.sender = SimpleNamespace(reply=reply)
    await service.db_execute('INSERT INTO users (user_id, name) VALUES (?, ?)', ('owner', '同名'))
    await service.db_execute('INSERT INTO users (user_id, name) VALUES (?, ?)', ('member', '同名'))
    for query_display in (False, True):
        for member_id, expected in (('owner', ('allowed', None)), ('member', (None, 'owner_only'))):
            event = Event()
            event.appid = '123456'
            event.event_type = 'C2C_MESSAGE_CREATE'
            event.user_id = member_id
            event.username = '同名'
            event.content = 'check'
            if query_display:
                await display_member('app', member_id)
            assert await manager.dispatch(event, bot.sender)
            assert await asyncio.wait_for(replies.get(), timeout=1) == expected
            assert event.user_id == member_id
    await manager.unload('display_permission')


@pytest.mark.parametrize('msg_type', [0, 2])
@pytest.mark.parametrize(('name', 'literal'), [
    ('**系统提示** <@member>', '＊＊系统提示＊＊ ＜＠member＞'),
    ('@全体 @all @everyone <@!123>', '＠全体 ＠all ＠everyone ＜＠！123＞'),
    ('[确认](https://example.invalid)', '［确认］（https：／／example．invalid）'),
    ('![图片](//example.invalid/a)', '！［图片］（／／example．invalid／a）'),
    ('https://x.invalid www.x.invalid a@x.invalid', 'https：／／x．invalid www．x．invalid a＠x．invalid'),
    ('_*~`[]()#>|\\+-=!', '＿＊～｀［］（）＃＞｜＼＋－＝！'),
])
@pytest.mark.asyncio
async def test_display_names_are_literal_text_in_real_message_payloads(stored_names, monkeypatch, msg_type, name, literal):
    from core.display import format_member

    _, _, service = stored_names
    await service.db_execute('INSERT INTO users(user_id,name) VALUES(?,?)', ('member-81CB', name))
    await service.db_execute('INSERT INTO groups_users(group_id,group_name) VALUES(?,?)', ('group', name))
    monkeypatch.setattr('core.message.sender.cfg.get_bot_setting', lambda appid, key, default=None: default)
    sender = MessageSender(SimpleNamespace(appid='app'))
    labels = [format_member('member-81CB', name), await display_member('app', 'member-81CB'), await display_group('app', 'group')]
    for label, expected in zip(labels, (literal + '（…81CB）', literal + '（…81CB）', literal), strict=True):
        payload = sender._build_core_payload('成员：' + label, None, None, msg_type)
        text = payload['markdown']['content'] if msg_type == 2 else payload['content']
        assert payload['msg_type'] == msg_type
        assert text == '成员：' + expected


@pytest.mark.parametrize('use_union_id', [False, True])
@pytest.mark.asyncio
async def test_application_observes_names_before_async_hooks_reorder_events(stored_names, api_config_dir, monkeypatch, use_union_id):
    from core.base.config import cfg

    cfg._ready = False
    cfg.init(f'{api_config_dir}/config')
    app, bot, service = stored_names
    bot.name = '测试 Bot'
    bot.appid = 'app'
    entered, release = asyncio.Event(), asyncio.Event()
    seen_ids = []

    async def delay_old(event):
        seen_ids.append(event.user_id)
        if event.username == '旧名':
            entered.set()
            await release.wait()
        return event

    app._hook_manager.register('before_event', delay_old, owner='test-module')
    monkeypatch.setattr('core.bot.event.cfg.get_bot_setting',
                        lambda appid, key, default=None: use_union_id if key == 'identity.use_union_id_for_group' else default)

    def make_event(name):
        event = Event()
        event.appid = 'app'
        event.event_type = 'C2C_MESSAGE_CREATE'
        event.user_id = event.raw_user_id = 'native'
        event.union_openid = 'union'
        event.username = name
        event.mentions = [{'id': 'mentioned', 'username': name}]
        return event

    old_event, new_event = make_event('旧名'), make_event('新名')
    old_task = asyncio.create_task(app._on_event(old_event))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await app._on_event(new_event)
    finally:
        release.set()
        await old_task
    await app._track_queue.join()
    await service._flush_data_queue()
    effective_id = 'union' if use_union_id else 'native'
    assert await display_member('app', effective_id) == '新名（…' + effective_id[-4:] + '）'
    assert await display_member('app', 'mentioned') == '新名（…oned）'
    assert seen_ids == [effective_id, effective_id]
    assert old_event.user_id == new_event.user_id == effective_id
    assert old_event.raw_user_id == new_event.raw_user_id == 'native'
    assert old_event.union_openid == new_event.union_openid == ('native' if use_union_id else 'union')


@pytest.mark.parametrize('mode', ['empty', 'error', 'exception'])
@pytest.mark.asyncio
async def test_missing_group_name_retries_only_after_negative_cache_expires(stored_names, monkeypatch, mode):
    app, bot, service = stored_names
    bot.sender = MessageSender(SimpleNamespace(appid='app'))
    bot.sender.bind_instance(log_service=service)
    calls = []
    clock = [1000.0]
    monkeypatch.setattr('core.display.monotonic', lambda: clock[0], raising=False)

    async def request_group(sender, group_id, endpoint, **kwargs):
        calls.append(group_id)
        if mode == 'exception':
            raise RuntimeError('platform unavailable')
        if mode == 'error':
            return False, {'message': 'platform unavailable'}
        return True, {'group_name': ''}

    monkeypatch.setattr(MessageSender, '_request_group', request_group)
    for _ in range(20):
        assert await app.display_group('app', 'group') == '未知群（…roup）'
    assert calls == ['group']
    clock[0] += 299
    assert await display_group('app', 'group') == '未知群（…roup）'
    assert calls == ['group']
    clock[0] += 2
    assert await display_group('app', 'group') == '未知群（…roup）'
    assert calls == ['group', 'group']


@pytest.mark.asyncio
async def test_cancelled_group_name_waiter_does_not_cancel_shared_lookup(stored_names, monkeypatch):
    _, bot, service = stored_names
    bot.sender = MessageSender(SimpleNamespace(appid='app'))
    bot.sender.bind_instance(log_service=service)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def request_group(sender, group_id, endpoint, **kwargs):
        calls.append(group_id)
        entered.set()
        await release.wait()
        return True, {'group_name': '群名'}

    monkeypatch.setattr(MessageSender, '_request_group', request_group)
    first = asyncio.create_task(display_group('app', 'group'))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        remaining = asyncio.create_task(display_group('app', 'group'))
    finally:
        release.set()
    assert await remaining == '群名'
    assert calls == ['group']


@pytest.mark.asyncio
async def test_concurrent_group_name_misses_share_one_request_and_are_bot_scoped(stored_names, monkeypatch):
    from core.display import display_group_for_bot

    _, bot, service = stored_names
    bot.sender = MessageSender(SimpleNamespace(appid='app'))
    bot.sender.bind_instance(log_service=service)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def request_group(sender, group_id, endpoint, **kwargs):
        calls.append((sender._appid, group_id))
        entered.set()
        await release.wait()
        return True, {'group_name': ''}

    monkeypatch.setattr(MessageSender, '_request_group', request_group)
    queries = [asyncio.create_task(display_group('app', 'group')) for _ in range(20)]
    try:
        await asyncio.wait_for(entered.wait(), 1)
    finally:
        release.set()
    assert await asyncio.gather(*queries) == ['未知群（…roup）'] * 20
    assert calls == [('app', 'group')]
    other = SimpleNamespace(log_service=_FakeLogService(), sender=MessageSender(SimpleNamespace(appid='other-app')))
    assert await display_group_for_bot(other, 'group') == '未知群（…roup）'
    assert calls == [('app', 'group'), ('other-app', 'group')]


@pytest.mark.asyncio
async def test_group_name_observed_during_negative_cache_is_immediately_readable(stored_names, monkeypatch):
    _, bot, service = stored_names
    bot.sender = MessageSender(SimpleNamespace(appid='app'))
    bot.sender.bind_instance(log_service=service)
    names = iter(('', '新群名'))
    calls = []

    async def request_group(sender, group_id, endpoint, **kwargs):
        calls.append(group_id)
        return True, {'group_name': next(names)}

    monkeypatch.setattr(MessageSender, '_request_group', request_group)
    assert await display_group('app', 'group') == '未知群（…roup）'
    await bot.sender.get_group_info('group')
    assert await display_group('app', 'group') == '新群名'
    assert calls == ['group', 'group']
