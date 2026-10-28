import asyncio
from types import SimpleNamespace

import pytest

from core.application import Application
from core.bot.event import EventHandlerMixin, _TrackItem
from core.display import display_group, display_member
from core.message.event import GROUP_MESSAGE_CREATE, Event
from core.message.parsers.channel import ChannelMessageParser


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
        raise AssertionError('name tracking should not require group membership writes')

    async def db_fetch_one(self, sql, params=()):
        if 'FROM users' in sql:
            name = self.users.get(str(params[0]))
            return {'name': name} if name is not None else None
        if 'FROM groups_users' in sql:
            name = self.groups.get(str(params[0]))
            return {'group_name': name} if name is not None else None
        raise AssertionError(f'unexpected SQL: {sql}')


@pytest.mark.asyncio
async def test_track_user_records_mentions_and_latest_clean_name():
    handler = EventHandlerMixin()
    handler._init_event_state()
    service = _FakeLogService()
    bot = SimpleNamespace(log_service=service)

    item = _TrackItem(
        bot=bot,
        appid='app',
        uid='sender',
        gid='',
        username='old\nname',
        member_role='',
        is_bot=False,
        is_direct=False,
        reply_event=None,
        mentions=(('mentioned', 'mention\tname'),),
    )
    await handler._track_user(item)
    assert service.users == {'sender': 'oldname', 'mentioned': 'mentionname'}

    item.username = 'new name'
    await handler._track_user(item)
    assert service.users['sender'] == 'new name'


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


def test_track_item_merge_keeps_latest_mention_per_id():
    first = _TrackItem(
        bot=None,
        appid='app',
        uid='sender',
        gid='group',
        username='',
        member_role='',
        is_bot=False,
        is_direct=False,
        reply_event=None,
        mentions=(('member-a', '旧名'), ('member-b', '保留名')),
    )
    newer = _TrackItem(
        bot=None,
        appid='app',
        uid='sender',
        gid='group',
        username='',
        member_role='',
        is_bot=False,
        is_direct=False,
        reply_event=None,
        mentions=(('member-a', '新名'),),
    )

    first.merge(newer)

    assert first.mentions == (('member-a', '新名'), ('member-b', '保留名'))


@pytest.mark.asyncio
async def test_application_display_member_uses_bot_scoped_read_only_api():
    service = _FakeLogService()
    service.users['member'] = '成员'
    app = Application()
    app._bot_registry = SimpleNamespace(get=lambda appid: SimpleNamespace(log_service=service))

    assert await app.display_member('app', 'member') == '成员（…mber）'
    assert service.queries == []
