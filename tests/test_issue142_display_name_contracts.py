"""#141 final review P3: display name contracts for Unicode at signs, QQ face/CQ codes, the bounded
negative cache, Bot reload, hook-consumed events and lifecycle-deferred / replayed events."""

import asyncio
from types import SimpleNamespace

import pytest

from core.application import Application
from core.display import display_group, display_group_for_bot, display_member, format_group, format_member
from core.message.event import Event
from core.message.sender import MessageSender
from tests.core.test_connection_status import Platform, until
from tests.core.test_group_lifecycle_subscription import context, eventually, lifecycle, lifecycle_app, webhook, ws_client  # noqa: F401
from tests.test_issue141_display_names import _FakeLogService, stored_names  # noqa: F401


def payload_text(label, msg_type):
    sender = MessageSender(SimpleNamespace(appid='app'))
    payload = sender._build_core_payload('成员：' + label, None, None, msg_type)
    assert payload['msg_type'] == msg_type
    return payload['markdown']['content'] if msg_type == 2 else payload['content']


@pytest.mark.parametrize('msg_type', [0, 2])
@pytest.mark.parametrize(('name', 'literal'), [
    # Already non-ASCII at signs are kept as plain characters; they never become an ASCII mention.
    ('＠全体 ＠all ＠everyone', '＠全体 ＠all ＠everyone'),
    ('﹫全体 ﹫all ﹫everyone', '﹫全体 ﹫all ﹫everyone'),
    ('＜＠123＞ ﹤@!456﹥', '＜＠123＞ ﹤＠！456﹥'),
    # QQ face and CQ codes lose their ASCII delimiters.
    ('<faceType=1,faceId="13",ext="eyJ0Ijoi">', '＜faceType＝1，faceId＝＂13＂，ext＝＂eyJ0Ijoi＂＞'),
    ('[CQ:at,qq=all][CQ:face,id=1]', '［CQ：at，qq＝all］［CQ：face，id＝1］'),
])
@pytest.mark.asyncio
async def test_unicode_at_signs_and_platform_codes_are_literal_in_real_payloads(stored_names, monkeypatch, msg_type, name, literal):  # noqa: F811
    _, _, service = stored_names
    await service.db_execute('INSERT INTO users(user_id,name) VALUES(?,?)', ('member-81CB', name))
    await service.db_execute('INSERT INTO groups_users(group_id,group_name) VALUES(?,?)', ('group', name))
    monkeypatch.setattr('core.message.sender.cfg.get_bot_setting', lambda appid, key, default=None: default)
    labels = (format_member('member-81CB', name), await display_member('app', 'member-81CB'), format_group('group', name), await display_group('app', 'group'))
    for label, expected in zip(labels, (literal + '（…81CB）',) * 2 + (literal,) * 2, strict=True):
        text = payload_text(label, msg_type)
        assert text == '成员：' + expected
        for syntax in ('@', '<@', '<faceType', '[CQ:', '="'):
            assert syntax not in text


@pytest.mark.asyncio
async def test_negative_cache_keeps_the_newest_1024_misses(monkeypatch):
    calls = []

    async def get_group_info(group_id, *, return_error=False):
        calls.append(group_id)
        raise RuntimeError('platform unavailable')

    monkeypatch.setattr('core.display.monotonic', lambda: 1000.0)
    bot = SimpleNamespace(log_service=_FakeLogService(), sender=SimpleNamespace(get_group_info=get_group_info))
    groups = [f'group-{index:04d}' for index in range(1025)]
    for group in groups:
        assert await display_group_for_bot(bot, group) == '未知群（…' + group[-4:] + '）'
    assert calls == groups
    # The newest 1024 misses stay cached; the oldest was evicted and is looked up again.
    for group in (groups[1], groups[512], groups[-1]):
        await display_group_for_bot(bot, group)
    assert calls == groups
    await display_group_for_bot(bot, groups[0])
    assert calls == [*groups, groups[0]]


@pytest.mark.asyncio
async def test_reloaded_bot_instance_never_uses_the_old_instance_cache(api_config_dir, tmp_path, monkeypatch):
    from core.base.config import cfg
    from core.bot.instance import BotInstance

    cfg._ready = False
    cfg.init(f'{api_config_dir}/config')
    calls = []

    def platform(label, name):
        async def get_group_info(group_id, *, return_error=False):
            calls.append((label, group_id))
            return ({'group_name': name}, None)

        return SimpleNamespace(get_group_info=get_group_info)

    instances = []
    try:
        for label, name in (('old', ''), ('new', '新群名')):
            instance = BotInstance({'appid': 'app', 'secret': 'fake-secret'}, str(tmp_path / label))
            instances.append(instance)
            assert instance._display_group_lookup is None
            instance.sender = platform(label, name)
        old, new = instances
        registry = {'app': old}
        monkeypatch.setattr('core.application._app', SimpleNamespace(get_bot=registry.get))
        assert await display_group('app', 'group') == '未知群（…roup）'
        assert await display_group('app', 'group') == '未知群（…roup）'
        assert calls == [('old', 'group')]
        # A reload replaces the instance; the old instance's negative cache is not consulted.
        registry['app'] = new
        assert await display_group('app', 'group') == '新群名'
        assert calls == [('old', 'group'), ('new', 'group')]
        assert new._display_group_lookup is not old._display_group_lookup
    finally:
        for instance in instances:
            await instance.log_service.shutdown()
        cfg._ready = False


@pytest.mark.asyncio
async def test_events_consumed_or_dropped_by_hooks_still_record_names(stored_names, api_config_dir):  # noqa: F811
    from core.base.config import cfg

    cfg._ready = False
    cfg.init(f'{api_config_dir}/config')
    app, bot, service = stored_names
    bot.name, bot.appid = '测试 Bot', 'app'
    consumed = []

    async def drop(event):
        consumed.append(event.user_id)
        return None

    app._hook_manager.register('before_event', drop, owner='test-module')
    event = Event()
    event.appid, event.event_type = 'app', 'C2C_MESSAGE_CREATE'
    event.user_id = event.raw_user_id = 'native-sender'
    event.username = '发送者'
    event.mentions = [{'id': 'mentioned', 'username': '被提及'}]
    await app._on_event(event)
    await service._flush_data_queue()
    assert consumed == ['native-sender']
    assert await display_member('app', 'native-sender') == '发送者（…nder）'
    assert await display_member('app', 'mentioned') == '被提及（…oned）'
    cfg._ready = False


class _NameLog(_FakeLogService):
    """Records name upserts; other tracking writes are outside this contract."""

    def db_queue(self, sql, params=()):
        if 'INTO users' in sql:
            super().db_queue(sql, params)


def name_writes(store, member):
    return [call.args[1][1] for call in store.db_queue.call_args_list if 'INTO users' in call.args[0] and call.args[1][0] == member]


def message(name, group='g', member='member-x'):
    return {'op': 0, 't': 'GROUP_AT_MESSAGE_CREATE', 'id': 'message-' + name, 'd': {'id': 'message-' + name, 'group_openid': group, 'content': 'hello', 'author': {'member_openid': member, 'username': name}}}


@pytest.mark.parametrize('transport', ['websocket', 'webhook'])
@pytest.mark.asyncio
async def test_lifecycle_deferred_events_record_names_in_receipt_order(lifecycle_app, transport):  # noqa: F811
    app, manager = lifecycle_app
    ctx = await context(app, manager)
    store = app.get_bot('123456').log_service
    entered, release = asyncio.Event(), asyncio.Event()

    async def busy(event):
        entered.set()
        await release.wait()

    ctx.subscribe_group_lifecycle(busy)
    client = ws_client(app)

    async def send(payload):
        if transport == 'websocket':
            await client._on_dispatch(payload)
        else:
            await webhook(app, payload)

    try:
        await send(lifecycle())
        await entered.wait()
        sent = [asyncio.create_task(send(message(name))) for name in ('旧名', '新名')]
        await asyncio.sleep(0.02)
        assert name_writes(store, 'member-x') in ([], ['旧名', '新名'])
        release.set()
        await asyncio.gather(*sent)
        await eventually(lambda: len(name_writes(store, 'member-x')) == 2)
        assert name_writes(store, 'member-x') == ['旧名', '新名']
    finally:
        release.set()
        await client.close()


@pytest.mark.asyncio
async def test_resume_replayed_events_record_names(monkeypatch, tmp_path, sample_config_dir):
    from core.base.config import cfg
    from core.network.websocket import WSClient

    cfg._ready = False
    cfg.init(sample_config_dir)
    app = Application()
    service = _NameLog()
    bot = SimpleNamespace(appid='sample-bot', name='sample-bot', log_service=service, sender=SimpleNamespace())
    app._bot_registry = SimpleNamespace(get=lambda appid: bot if appid == 'sample-bot' else None)
    platform = Platform(monkeypatch)
    client = WSClient(
        'sample-bot', platform, app._on_event, ack_interaction=app._on_event, reconnect_interval=0,
        connection_status=app.hook_manager.connection_status, on_event_received=app._prepare_event,
    )
    task = asyncio.create_task(client.connect())
    try:
        first = platform.add()
        first.feed(10, d={'heartbeat_interval': 45000})
        await until(lambda: any(frame['op'] == 2 for frame in first.sent))
        first.feed(0, t='READY', s=1, d={'session_id': 'sample-session'})
        await until(lambda: app.hook_manager.connection_status.get('sample-bot').phase == 'ready')
        first.frames.put_nowait(OSError('sample disconnect'))
        second = platform.add()
        second.feed(10, d={'heartbeat_interval': 45000})
        await until(lambda: any(frame['op'] == 6 for frame in second.sent))
        # Events replayed after RESUME go through the same entry and are observed in order.
        for sequence, name in ((2, '旧名'), (3, '新名')):
            second.feed(0, t='C2C_MESSAGE_CREATE', s=sequence, d={'id': f'm{sequence}', 'content': 'hi', 'author': {'id': 'replayed', 'username': name}})
        second.feed(0, t='RESUMED', s=4, d='')
        await until(lambda: app.hook_manager.connection_status.get('sample-bot').phase == 'replay_complete')
        await until(lambda: service.users.get('replayed') == '新名')
        assert [params[1] for sql, params in service.queries if params[0] == 'replayed'] == ['旧名', '新名']
    finally:
        await client.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for worker in app._track_workers:
            worker.cancel()
        await asyncio.gather(*app._track_workers, return_exceptions=True)
        cfg._ready = False
