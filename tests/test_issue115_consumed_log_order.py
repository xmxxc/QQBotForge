"""被模块接管事件的接收记录排在 Bot 回复之前 (#115)

真实 Application._on_event、真实 HookManager、真实 LogService (临时 SQLite) 与
MessageSender 的发送日志路径；只读取临时库中的行 id，不访问网络或正式实例。
"""

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import core.storage._base as base_mod
from core.application import Application
from core.base.config import cfg
from core.message.event import Event
from core.message.sender import MessageSender
from core.storage.log import LogService

APPID = '123456'
CONSUMED_BY = '群状态'


@pytest.fixture
async def log_app(tmp_path, sample_config_dir):
    cfg._ready = False
    cfg.init(sample_config_dir)
    service = LogService(str(tmp_path / 'logs'), APPID)
    sender = MessageSender(SimpleNamespace(appid=APPID))
    sender.bind_instance(log_service=service, bot_name='bot')
    sender.refresh_group_info = AsyncMock()
    sender.ack_interaction = AsyncMock()
    bot = SimpleNamespace(appid=APPID, name='bot', robot_qq='', log_service=service, sender=sender)
    app = Application()
    app._bot_registry = SimpleNamespace(get=lambda appid: bot if appid == APPID else None)
    try:
        yield SimpleNamespace(app=app, bot=bot, service=service, sender=sender)
    finally:
        for worker in app._track_workers:
            worker.cancel()
        await asyncio.gather(*app._track_workers, return_exceptions=True)
        await service.shutdown()
        cfg._ready = False


def _c2c(msg_id, content):
    return Event.from_websocket(APPID, {
        'op': 0, 't': 'C2C_MESSAGE_CREATE', 'id': f'evt-{msg_id}',
        'd': {
            'id': msg_id, 'content': content, 'author': {'user_openid': 'owner-1'},
            'timestamp': '2026-10-30T00:00:00+08:00',
        },
    })


def _group_at(msg_id, content):
    return Event.from_websocket(APPID, {
        'op': 0, 't': 'GROUP_AT_MESSAGE_CREATE', 'id': f'evt-{msg_id}',
        'd': {
            'id': msg_id, 'content': content, 'group_openid': 'group-1',
            'author': {'member_openid': 'owner-1'}, 'timestamp': '2026-10-30T00:00:00+08:00',
        },
    })


def _reply(env, event, text='群状态概览'):
    env.sender._emit_log(text, event.user_id, event.group_id or '', '', log_type='reply',
                         plugin_name=CONSUMED_BY, message_id=f'reply-{event.message_id}')


def _consume_with_reply(env):
    async def take(event):
        if CONSUMED_BY in (event.content or ''):
            _reply(env, event)
            return None
        return event

    env.app.hook_manager.register('before_event', take, owner=CONSUMED_BY)


async def _rows(env):
    await env.service._flush_type('message')
    return env.service.query(
        'message', 'SELECT id, message_id, direction, content, context FROM log ORDER BY id')


def _consumed_context(event_type):
    return f'{{"consumed": {{"event_type": "{event_type}", "consumed_by": "{CONSUMED_BY}"}}}}'


@pytest.mark.parametrize('make_event', [_c2c, _group_at], ids=['c2c', 'group'])
async def test_consumed_receive_is_ordered_before_bot_reply(log_app, make_event):
    _consume_with_reply(log_app)
    event = make_event('m-1', '群状态 查看')

    await log_app.app._on_event(event)

    rows = await _rows(log_app)
    assert [(r['direction'], r['message_id']) for r in rows] == [('receive', 'm-1'), ('send', 'reply-m-1')]
    receive, send = rows
    assert receive['id'] < send['id']
    # #60 语义不变：接管记录无正文，带 context.consumed
    assert receive['content'] == ''
    assert receive['context'] == _consumed_context(event.event_type)
    assert send['content'] == '群状态概览'


async def test_released_event_writes_one_normal_receive(log_app):
    _consume_with_reply(log_app)

    await log_app.app._on_event(_group_at('m-2', '账户管理 查看'))

    rows = await _rows(log_app)
    assert [(r['direction'], r['message_id'], r['content'], r['context']) for r in rows] == [
        ('receive', 'm-2', '账户管理 查看', ''),
    ]


async def test_receive_order_holds_when_flush_runs_during_hook(log_app):
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_take(event):
        _reply(log_app, event)
        entered.set()
        await release.wait()
        return None

    log_app.app.hook_manager.register('before_event', slow_take, owner=CONSUMED_BY)
    task = asyncio.create_task(log_app.app._on_event(_c2c('m-3', '群状态')))
    await asyncio.wait_for(entered.wait(), 1)
    # 周期刷写在模块处理中途发生：回复不能越过尚未决定的接收记录
    assert await _rows(log_app) == []
    release.set()
    await task

    rows = await _rows(log_app)
    assert [(r['direction'], r['message_id']) for r in rows] == [('receive', 'm-3'), ('send', 'reply-m-3')]


async def test_raising_hook_releases_without_orphan(log_app):
    async def boom(event):
        _reply(log_app, event, '处理前回复')
        raise RuntimeError('hook boom')

    log_app.app.hook_manager.register('before_event', boom, owner=CONSUMED_BY)

    await log_app.app._on_event(_c2c('m-4', '群状态'))

    rows = await _rows(log_app)
    # 出错的 hook 被跳过，事件按放行处理：只有一条普通接收，没有空正文记录
    assert [(r['direction'], r['content'], r['context']) for r in rows] == [
        ('send', '处理前回复', ''), ('receive', '群状态', ''),
    ]


async def test_cancelled_hook_leaves_no_orphan_and_unblocks_flush(log_app):
    entered = asyncio.Event()

    async def hang(event):
        if event.message_id == 'm-5':
            entered.set()
            await asyncio.Event().wait()
        return event

    log_app.app.hook_manager.register('before_event', hang, owner=CONSUMED_BY)
    task = asyncio.create_task(log_app.app._on_event(_c2c('m-5', '群状态')))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await log_app.app._on_event(_c2c('m-6', '账户管理'))

    rows = await _rows(log_app)
    assert [(r['message_id'], r['content']) for r in rows] == [('m-6', '账户管理')]


async def test_expired_reservation_keeps_consumed_record_after_reply(log_app, monkeypatch):
    monkeypatch.setattr(base_mod, '_LOG_SLOT_TIMEOUT', 0)

    async def take_after_flush(event):
        _reply(log_app, event)
        await log_app.service._flush_type('message')
        return None

    log_app.app.hook_manager.register('before_event', take_after_flush, owner=CONSUMED_BY)

    await log_app.app._on_event(_c2c('m-7', '群状态'))

    rows = await _rows(log_app)
    # 超时后不再阻塞刷写；接管记录仍写入一次，只是退回到回复之后
    assert [(r['direction'], r['message_id'], r['content']) for r in rows] == [
        ('send', 'reply-m-7', '群状态概览'), ('receive', 'm-7', ''),
    ]


async def test_shutdown_does_not_wait_for_undecided_receive(log_app):
    entered = asyncio.Event()

    async def hang(event):
        _reply(log_app, event)
        entered.set()
        await asyncio.Event().wait()

    log_app.app.hook_manager.register('before_event', hang, owner=CONSUMED_BY)
    task = asyncio.create_task(log_app.app._on_event(_c2c('m-8', '群状态')))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        await asyncio.wait_for(log_app.service.shutdown(), 5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    with sqlite3.connect(log_app.service._resolve_db_path('message')) as conn:
        rows = conn.execute('SELECT direction, message_id FROM log ORDER BY id').fetchall()
    assert rows == [('send', 'reply-m-8')]
