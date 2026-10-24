"""send_check: 首次业务 client.request 前的同步当前性检查 (#101)

真实 MessageSender / HookManager / TokenManager.get_token/_refresh / _HttpMixin._request,
只替换外部 HTTP 客户端 (token 与消息请求共用同一个假传输)。
"""

import asyncio
import copy
import json
import logging
import time
from types import SimpleNamespace

import pytest

import core.message._http as http_mod
from core.message.sender import MessageSender
from core.module.hook import reset_hook_manager
from core.network.access import TokenManager

SECRET = 'fake-password-7f3a'


class Wire:
    """外部 HTTP 传输替身: post=取 token, request=消息业务请求"""

    def __init__(self):
        self.is_closed = False
        self.trace = []
        self.requests = []
        self.token_calls = 0
        self.token_hold = None  # asyncio.Event: 取 token 的 HTTP 在此等待
        self.token_entered = asyncio.Event()
        self.responses = []  # 依次消费: 响应对象 / 异常 / 可调用

    async def post(self, url, **kwargs):
        self.token_calls += 1
        self.trace.append('token')
        self.token_entered.set()
        if self.token_hold is not None:
            await self.token_hold.wait()
        n = self.token_calls
        return SimpleNamespace(status_code=200, json=lambda: {'access_token': f'tok-{n}', 'expires_in': 7200})

    async def request(self, method, url, **kwargs):
        self.trace.append('request')
        self.requests.append({'url': url, 'json': kwargs.get('json'), 'headers': kwargs.get('headers')})
        if self.responses:
            r = self.responses.pop(0)
            if callable(r):
                r = r()
            if isinstance(r, BaseException):
                raise r
            return r
        return ok_response()

    async def aclose(self):
        self.is_closed = True


def ok_response():
    return SimpleNamespace(status_code=200, content=b'{"id":"msg-1"}')


def err_response(code):
    return SimpleNamespace(status_code=400, content=json.dumps({'code': code, 'message': 'err'}).encode())


class Grant:
    """假消费方的当前性状态; 检查本身同步"""

    def __init__(self, wire, name='a'):
        self.wire = wire
        self.name = name
        self.valid = True
        self.calls = 0

    def __call__(self):
        self.calls += 1
        self.wire.trace.append(f'check-{self.name}')
        return self.valid


def make_event(group='G1'):
    return SimpleNamespace(
        reply_endpoint=f'/v2/groups/{group}/messages',
        needs_msg_id=True,
        message_id='m-1',
        needs_event_id=False,
        event_id='',
        user_id='U1',
        group_id=group,
        raw=None,
        content='',
    )


@pytest.fixture
def hooks(sample_config_dir):
    from core.base.config import cfg

    saved = dict(cfg.__dict__)
    cfg._ready = False
    cfg.init(sample_config_dir)
    manager = reset_hook_manager()
    yield manager
    reset_hook_manager()
    cfg.__dict__.clear()
    cfg.__dict__.update(saved)


@pytest.fixture
def no_delay(monkeypatch):
    monkeypatch.setattr(http_mod, '_NET_RETRY_DELAY', 0)


def make_sender(wire, *, valid_token=True, appid='bot-a'):
    tm = TokenManager(appid, 'fake-secret-' + appid)
    tm._client = wire
    if valid_token:
        tm._token = 'tok-0'
        tm._expires_at = time.time() + 7200
    return MessageSender(tm), tm


def sent_secret(wire):
    return [r for r in wire.requests if SECRET in str(r['json'])]


def blocked(data, attempted):
    return isinstance(data, dict) and data.get('send_check') == 'blocked' and data.get('attempted') is attempted and data.get('code') == -1


# ==================== 正常控制 ====================


async def test_valid_token_check_runs_right_before_first_request(hooks):
    wire = Wire()
    sender, _ = make_sender(wire)
    grant = Grant(wire)
    data = await sender.reply(make_event(), SECRET, send_check=grant)
    assert data == {'id': 'msg-1'}
    assert len(sent_secret(wire)) == 1
    assert wire.trace == ['check-a', 'request']


async def test_expired_token_refresh_then_check_then_request(hooks):
    wire = Wire()
    sender, _ = make_sender(wire, valid_token=False)
    grant = Grant(wire)
    data = await sender.reply(make_event(), SECRET, send_check=grant)
    assert data == {'id': 'msg-1'}
    assert wire.trace == ['token', 'check-a', 'request']
    assert wire.requests[0]['headers']['Authorization'] == 'QQBot tok-1'


async def test_without_check_unchanged(hooks):
    wire = Wire()
    sender, _ = make_sender(wire, valid_token=False)
    assert await sender.reply(make_event(), 'hello') == {'id': 'msg-1'}
    ok, data, payload = await sender.send_to_user('U1', 'hi', msg_type=0)
    assert ok and data == {'id': 'msg-1'} and payload['content'] == 'hi'
    assert wire.trace == ['token', 'request', 'request']


async def test_platform_error_without_check_unchanged(hooks):
    wire = Wire()
    wire.responses = [err_response(304003)]
    sender, _ = make_sender(wire)
    data = await sender.reply(make_event(), SECRET)
    assert data == {'code': 304003, 'message': 'err'}


# ==================== 首发前等待中失效 ====================


async def test_revoked_while_token_refresh_awaits(hooks):
    wire = Wire()
    wire.token_hold = asyncio.Event()
    sender, _ = make_sender(wire, valid_token=False)
    grant = Grant(wire)
    task = asyncio.create_task(sender.reply(make_event(), SECRET, send_check=grant))
    await asyncio.wait_for(wire.token_entered.wait(), 5)
    assert wire.requests == []
    grant.valid = False
    wire.token_hold.set()
    data = await task
    assert wire.requests == []
    assert blocked(data, attempted=False)
    assert wire.trace == ['token', 'check-a']


async def test_revoked_while_waiting_for_token_lock(hooks):
    wire = Wire()
    wire.token_hold = asyncio.Event()
    sender, tm = make_sender(wire, valid_token=False)
    other = asyncio.create_task(tm.refresh_token())  # 另一调用方持有锁并在刷新
    await asyncio.wait_for(wire.token_entered.wait(), 5)
    grant = Grant(wire)
    task = asyncio.create_task(sender.reply(make_event(), SECRET, send_check=grant))
    await asyncio.sleep(0.01)
    assert tm._lock.locked() and not task.done() and wire.requests == []
    grant.valid = False
    wire.token_hold.set()
    await other
    assert blocked(await task, attempted=False)
    assert wire.requests == []


async def test_revoked_while_client_preparation_awaits(hooks, monkeypatch):
    # 真实 get_client 不挂起; 这里模拟会挂起的客户端准备, 检查仍在其后
    wire = Wire()
    sender, _ = make_sender(wire)
    entered, release = asyncio.Event(), asyncio.Event()
    real_get_client = TokenManager.get_client

    async def slow_get_client(self):
        entered.set()
        await release.wait()
        return await real_get_client(self)

    monkeypatch.setattr(TokenManager, 'get_client', slow_get_client)
    grant = Grant(wire)
    task = asyncio.create_task(sender.reply(make_event(), SECRET, send_check=grant))
    await asyncio.wait_for(entered.wait(), 5)
    grant.valid = False
    release.set()
    assert blocked(await task, attempted=False)
    assert wire.requests == []


async def test_prefetched_token_expires_during_long_before_send(hooks):
    wire = Wire()
    sender, tm = make_sender(wire)
    await tm.ensure_token()  # 管道前预取: 有效
    entered, release = asyncio.Event(), asyncio.Event()

    async def preparation(packet):
        entered.set()
        await release.wait()
        return packet

    hooks.register('before_send', preparation, owner='prep')
    grant = Grant(wire)
    wire.token_hold = asyncio.Event()
    task = asyncio.create_task(sender.reply(make_event(), SECRET, send_check=grant))
    await asyncio.wait_for(entered.wait(), 5)
    tm._expires_at = 0  # 长 before_send 期间 token 过期
    release.set()
    await asyncio.wait_for(wire.token_entered.wait(), 5)  # _request 再次刷新
    grant.valid = False
    wire.token_hold.set()
    assert blocked(await task, attempted=False)
    assert wire.requests == []
    assert wire.trace == ['token', 'check-a']


@pytest.mark.parametrize('rewrite', ['copy_event', 'remove_event', 'new_packet', 'copy_payload'])
async def test_hook_rewriting_packet_cannot_drop_check(hooks, rewrite):
    wire = Wire()
    sender, _ = make_sender(wire)
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def rewriter(packet):
        seen.append(dict(packet))
        entered.set()
        await release.wait()
        if rewrite == 'copy_event':
            return {**packet, 'event': copy.copy(packet['event'])}
        if rewrite == 'remove_event':
            return {k: v for k, v in packet.items() if k != 'event'}
        if rewrite == 'new_packet':
            return {'payload': dict(packet['payload'])}
        return {**packet, 'payload': copy.deepcopy(packet['payload'])}

    hooks.register('before_send', rewriter, owner='rewrite', priority=500)
    grant = Grant(wire)
    task = asyncio.create_task(sender.reply(make_event(), SECRET, send_check=grant))
    await asyncio.wait_for(entered.wait(), 5)
    grant.valid = False
    release.set()
    assert blocked(await task, attempted=False)
    assert wire.requests == []
    # 检查对象不在 hook 可见数据中
    assert all('send_check' not in p and grant not in p.values() for p in seen)


# ==================== 失败关闭 ====================


async def test_check_exception_blocks_without_leaking(hooks, caplog):
    wire = Wire()
    sender, _ = make_sender(wire)

    def broken():
        raise RuntimeError(f'cannot read {SECRET}')

    with caplog.at_level(logging.DEBUG):
        data = await sender.reply(make_event(), SECRET, send_check=broken)
    assert blocked(data, attempted=False)
    assert wire.requests == []
    assert SECRET not in caplog.text
    assert 'RuntimeError' in caplog.text


@pytest.mark.parametrize('check', ['async', 'none_result', 'not_callable'])
async def test_non_sync_or_falsy_check_blocks(hooks, check):
    wire = Wire()
    sender, _ = make_sender(wire)

    async def async_check():
        return True

    value = {'async': async_check, 'none_result': lambda: None, 'not_callable': True}[check]
    data = await sender.reply(make_event(), SECRET, send_check=value)
    assert blocked(data, attempted=False)
    assert wire.requests == []


async def test_blocked_send_skips_failure_handling_and_reports(hooks, monkeypatch):
    wire = Wire()
    sender, _ = make_sender(wire)
    calls = []

    async def send_failed(packet):
        calls.append('send_failed')
        return packet

    async def after_send(packet):
        calls.append('after_send')

    hooks.register('send_failed', send_failed, owner='t')
    hooks.register('after_send', after_send, owner='t')
    monkeypatch.setattr(sender, '_report_send_error', lambda *a, **k: calls.append('report'))
    reported = []
    monkeypatch.setattr('core.message.sender.report_error_raw', lambda *a, **k: reported.append(k))
    assert blocked(await sender.reply(make_event(), SECRET, send_check=lambda: False), attempted=False)
    ok, data, _ = await sender.send_to_user('U1', SECRET, send_check=lambda: False)
    assert not ok and blocked(data, attempted=False)
    assert calls == [] and reported == []
    assert wire.requests == []


async def test_send_to_group_and_post_json_accept_check(hooks):
    wire = Wire()
    sender, _ = make_sender(wire)
    ok, data, _ = await sender.send_to_group('G1', SECRET, send_check=lambda: False)
    assert not ok and blocked(data, attempted=False)
    ok, data = await sender.post_json('/v2/users/U1/messages', {'content': SECRET}, send_check=lambda: False)
    assert not ok and blocked(data, attempted=False)
    ok, data = await sender.send_to_channel('C1', SECRET, send_check=lambda: False)
    assert not ok and blocked(data, attempted=False)
    assert wire.requests == []
    ok, data, _ = await sender.send_to_group('G1', SECRET, send_check=lambda: True)
    assert ok and data == {'id': 'msg-1'}
    assert 'send_check' not in str(wire.requests)


async def test_unsupported_entry_rejects_check(hooks):
    wire = Wire()
    sender, _ = make_sender(wire)
    for call in (
        lambda: sender.reply_image(make_event(), b'img', SECRET, send_check=lambda: True),
        lambda: sender.reply_ark(make_event(), 23, [], SECRET, send_check=lambda: True),
        lambda: sender.reply_card(make_event(), 'tuwen', {}, SECRET, send_check=lambda: True),
    ):
        with pytest.raises(TypeError):
            await call()
    assert wire.requests == []


# ==================== 隔离 ====================


async def test_concurrent_requests_only_revoked_one_blocked(hooks):
    wire = Wire()
    wire.token_hold = asyncio.Event()
    sender, _ = make_sender(wire, valid_token=False)
    a, b = Grant(wire, 'a'), Grant(wire, 'b')
    ta = asyncio.create_task(sender.reply(make_event('GA'), SECRET + '-A', send_check=a))
    tb = asyncio.create_task(sender.reply(make_event('GB'), SECRET + '-B', send_check=b))
    await asyncio.wait_for(wire.token_entered.wait(), 5)
    await asyncio.sleep(0.01)
    a.valid = False
    wire.token_hold.set()
    ra, rb = await asyncio.gather(ta, tb)
    assert blocked(ra, attempted=False)
    assert rb == {'id': 'msg-1'}
    assert [r['url'] for r in wire.requests] == ['/v2/groups/GB/messages']
    assert SECRET + '-A' not in str(wire.requests)


async def test_two_bots_isolated(hooks):
    wa, wb = Wire(), Wire()
    sa, _ = make_sender(wa, appid='bot-a')
    sb, _ = make_sender(wb, appid='bot-b')
    ra, rb = await asyncio.gather(
        sa.reply(make_event(), SECRET, send_check=lambda: False),
        sb.reply(make_event(), SECRET),
    )
    assert blocked(ra, attempted=False) and rb == {'id': 'msg-1'}
    assert wa.requests == [] and len(wb.requests) == 1


async def test_nested_plain_send_and_safety_notice_not_inherit_check(hooks):
    wire = Wire()
    sender, _ = make_sender(wire)
    grant = Grant(wire)
    nested = []

    async def notifier(packet):
        # hook 内的普通嵌套发送不继承外层敏感检查
        if not nested and SECRET in str(packet['payload']):
            grant.valid = False
            nested.append(await sender.send_to_user('OWNER', '有一条敏感消息待发送'))
        return packet

    hooks.register('before_send', notifier, owner='notify')
    data = await sender.reply(make_event(), SECRET, send_check=grant)
    assert blocked(data, attempted=False)
    # 失效后的安全通知不带检查, 正常发送
    notice = await sender.reply(make_event(), '账号或授权刚刚变化，请重新查询')
    assert notice == {'id': 'msg-1'}
    assert nested[0][0] is True
    assert sent_secret(wire) == []
    assert [r['url'] for r in wire.requests] == ['/v2/users/OWNER/messages', '/v2/groups/G1/messages']


async def test_before_send_block_still_returns_none(hooks):
    wire = Wire()
    sender, _ = make_sender(wire)

    async def stop(packet):
        return None

    hooks.register('before_send', stop, owner='stop')
    assert await sender.reply(make_event(), SECRET, send_check=lambda: True) is None
    assert wire.requests == []


# ==================== 重试边界 ====================


async def test_token_expired_retry_rechecks_and_reports_attempted(hooks):
    wire = Wire()
    sender, _ = make_sender(wire)
    grant = Grant(wire)

    def first():
        grant.valid = False  # 第一次业务请求在途时失效
        return err_response(http_mod._TOKEN_EXPIRED_CODE)

    wire.responses = [first]
    data = await sender.reply(make_event(), SECRET, send_check=grant)
    assert blocked(data, attempted=True)
    assert len(wire.requests) == 1
    assert wire.trace == ['check-a', 'request', 'token', 'check-a']


async def test_token_expired_retry_control_sends_again(hooks):
    wire = Wire()
    sender, _ = make_sender(wire)
    grant = Grant(wire)
    wire.responses = [err_response(http_mod._TOKEN_EXPIRED_CODE)]
    assert await sender.reply(make_event(), SECRET, send_check=grant) == {'id': 'msg-1'}
    assert wire.trace == ['check-a', 'request', 'token', 'check-a', 'request']


async def test_network_retry_rechecks_and_reports_attempted(hooks, no_delay):
    wire = Wire()
    sender, _ = make_sender(wire)
    grant = Grant(wire)

    def first():
        grant.valid = False
        return ConnectionError('connection reset')

    wire.responses = [first]
    data = await sender.reply(make_event(), SECRET, send_check=grant)
    assert blocked(data, attempted=True)
    assert len(wire.requests) == 1


async def test_network_retry_control_and_no_check_unchanged(hooks, no_delay):
    wire = Wire()
    sender, _ = make_sender(wire)
    wire.responses = [ConnectionError('connection reset')]
    assert await sender.reply(make_event(), SECRET, send_check=lambda: True) == {'id': 'msg-1'}
    wire.responses = [ConnectionError('connection reset')] * 3
    data = await sender.reply(make_event(), SECRET)
    assert data['code'] == -1 and 'send_check' not in data
    assert len(wire.requests) == 2 + 3


async def test_legacy_post_json_override_kept_and_check_fails_closed(hooks):
    sent = []

    class LegacySender(MessageSender):
        async def post_json(self, endpoint, payload):
            sent.append(payload)
            return True, {'id': 'legacy'}

    sender = LegacySender(make_sender(Wire())[1])
    assert await sender.reply(make_event(), 'hello') == {'id': 'legacy'}
    with pytest.raises(TypeError):
        await sender.reply(make_event(), SECRET, send_check=lambda: True)
    assert SECRET not in str(sent) and len(sent) == 1
