"""Fake HTTP exercises the actual protocol, never CLI or real accounts."""

import asyncio
import base64
import json
import logging
import socket
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from plugins.ninja3.password_protocol import (
    CHANGE_PATH,
    ChangeInput,
    ErrorCode,
    RemoteStatus,
    Stage,
    VerifyInput,
    change_password,
    verify_password,
)

VALUES = ChangeInput('16600000000', '86', 'old-test-secret', 'new-test-secret')
PATHS = ['/env.js', '/api/web/login/pwd', '/api/web/user_center/userinfo', '/api/web' + CHANGE_PATH]
KEY = base64.b64encode(bytes(range(32))).decode()
CONFIG = {'appId': 'test-app', 'baseUrl': '/api/web', 'backend': 'user.pandadastudio.com', 'signSecret': KEY, 'encryptoSecret': KEY}
SECRETS = [VALUES.phone, VALUES.old_password, VALUES.new_password, 'fake-cookie-secret', 'fake-token-secret', KEY]


async def ready():
    pass


class Platform:
    def __init__(self, *, config=None, identity=None, override=None):
        self.config = CONFIG if config is None else config
        self.identity = {'phone': VALUES.phone, 'area': VALUES.area} if identity is None else identity
        self.override = override or {}
        self.requests = []

    async def handle(self, request):
        self.requests.append(request)
        assert request.url.host == 'user.pandadastudio.com'
        path = request.url.path
        if path in self.override:
            value = self.override[path]
            if isinstance(value, BaseException):
                raise value
            return value
        if path == PATHS[0]:
            text = '\n'.join(f'{key}: "{value}",' for key, value in self.config.items())
            return httpx.Response(200, text=text)
        if path == PATHS[1]:
            return httpx.Response(200, json={'code': 100}, headers={'set-cookie': 'session=fake-cookie-secret; Secure; Path=/'})
        if path == PATHS[2]:
            return httpx.Response(200, json={'code': 100, 'data': self.identity})
        assert path == PATHS[3]
        assert request.headers['cookie'] == 'session=fake-cookie-secret'
        return httpx.Response(200, json={'code': 100})

    async def run(self, values=VALUES, **kwargs):
        return await change_password(values, before_submit=kwargs.pop('before_submit', ready), transport=httpx.MockTransport(self.handle), **kwargs)

    @property
    def changes(self):
        return sum(request.url.path == PATHS[3] for request in self.requests)


def test_network_guard_is_active():
    with socket.socket() as connection, pytest.raises(AssertionError, match='prohibit network'):
        connection.connect(('203.0.113.1', 443))
    with pytest.raises(AssertionError, match='prohibit network'):
        socket.getaddrinfo('example.com', 443)


async def test_success_executes_protocol_and_once_only(original):
    platform = Platform()
    calls = []

    async def mark():
        assert [request.url.path for request in platform.requests] == PATHS[:3]
        calls.append('persist-before-submit')

    date = datetime(2026, 10, 23, 6, 7, 8, tzinfo=UTC)
    result = await platform.run(before_submit=mark, now=lambda: date, random_bytes=lambda n: bytes(range(n)))
    assert result.remote_status == RemoteStatus.REPORTED_SUCCESS
    assert result.stage == Stage.SUBMIT and result.error_code == ErrorCode.NONE
    assert calls == ['persist-before-submit']
    assert [request.url.path for request in platform.requests] == PATHS
    assert platform.changes == 1
    for request in platform.requests[1:]:
        path = request.url.path.removeprefix('/api/web')
        expected = original.app.signed_headers(
            CONFIG,
            request.method,
            path,
            request.content,
            date='20261023T060708Z',
            nonce=base64.b64encode(bytes(range(16))).decode(),
            traceid=base64.b64encode(bytes(range(16))).decode(),
        )
        assert request.headers['Authorization'] == expected['Authorization']
    login = json.loads(platform.requests[1].content)
    changed = json.loads(platform.requests[3].content)
    assert login == {'phone': VALUES.phone, 'area': '86', 'pwd': original.app.encrypt_password(VALUES.old_password, KEY, bytes(range(16)))}
    assert changed == {
        'old_pwd': original.app.encrypt_password(VALUES.old_password, KEY, bytes(range(16))),
        'new_pwd': original.app.encrypt_password(VALUES.new_password, KEY, bytes(range(16))),
    }
    assert platform.requests[2].content == b''


async def test_unknown_result_verification_logs_in_with_new_password_and_checks_identity():
    platform = Platform()
    result = await verify_password(VerifyInput(VALUES.phone, VALUES.area, VALUES.new_password), transport=httpx.MockTransport(platform.handle))
    assert result.verified and result.error_code == ErrorCode.NONE
    assert [request.url.path for request in platform.requests] == PATHS[:3]
    mismatch = Platform(identity={'phone': '16600000001', 'area': VALUES.area})
    result = await verify_password(VerifyInput(VALUES.phone, VALUES.area, VALUES.new_password), transport=httpx.MockTransport(mismatch.handle))
    assert not result.verified and result.error_code == ErrorCode.IDENTITY
    assert [request.url.path for request in mismatch.requests] == PATHS[:3]


@pytest.mark.parametrize('key', [base64.b64encode(b'another-fake-key').decode(), KEY])
async def test_dynamic_config_signing_and_encryption(original, key):
    config = {**CONFIG, 'appId': 'dynamic-test-app', 'signSecret': key, 'encryptoSecret': key}
    platform = Platform(config=config)
    assert (await platform.run(random_bytes=lambda n: b'x' * n)).remote_status == RemoteStatus.REPORTED_SUCCESS
    request = platform.requests[3]
    assert request.headers['x-yh-appid'] == 'dynamic-test-app'
    expected = original.app.signed_headers(
        config,
        'POST',
        CHANGE_PATH,
        request.content,
        date=request.headers['x-yh-date'],
        nonce=request.headers['x-yh-nonce'],
        traceid=request.headers['x-yh-traceid'],
    )
    assert request.headers['Authorization'] == expected['Authorization']
    assert json.loads(request.content)['new_pwd'] == original.app.encrypt_password(VALUES.new_password, key, b'x' * 16)


@pytest.mark.parametrize(
    'identity',
    [
        {},
        {'phone': '16600000001', 'area': '86'},
        {'phone': '166****0000', 'area': '86'},
        {'phone': VALUES.phone},
        {'phone': VALUES.phone, 'area': 86},
        {'phone': VALUES.phone, 'area': '1'},
        {'phone': [VALUES.phone], 'area': '86'},
        [],
    ],
)
async def test_identity_must_match_exact_actual_session(identity):
    platform = Platform(identity=identity)
    called = []

    async def mark():
        called.append(True)

    result = await platform.run(before_submit=mark)
    assert result.remote_status == RemoteStatus.NOT_SUBMITTED
    assert result.error_code == ErrorCode.IDENTITY
    assert platform.changes == 0 and not called


@pytest.mark.parametrize('path', PATHS)
@pytest.mark.parametrize('status', [301, 302, 307, 308])
async def test_redirect_is_never_followed(path, status):
    platform = Platform(override={path: httpx.Response(status, headers={'location': 'https://other.invalid/secret'})})
    result = await platform.run()
    assert len(platform.requests) == PATHS.index(path) + 1
    assert result.remote_status == (RemoteStatus.UNKNOWN if path == PATHS[3] else RemoteStatus.NOT_SUBMITTED)
    assert platform.changes <= 1


@pytest.mark.parametrize('path', PATHS[1:])
@pytest.mark.parametrize(
    ('response', 'error'),
    [
        (httpx.Response(200, json={'code': 1300, 'msg': 'fake-token-secret'}), ErrorCode.REJECTED),
        (httpx.Response(200, text='fake-token-secret'), ErrorCode.RESPONSE),
        (httpx.Response(200, json=[]), ErrorCode.RESPONSE),
        (httpx.Response(200, json={'code': True}), ErrorCode.RESPONSE),
        (httpx.Response(200, json={'code': '100'}), ErrorCode.RESPONSE),
        (httpx.Response(500, json={'code': 100}), ErrorCode.RESPONSE),
        (httpx.Response(403, json={'code': 1300}), ErrorCode.RESPONSE),
        (httpx.Response(200, json={'code': 100}, headers={'x-yh-captcha-trigger': '1'}), ErrorCode.VERIFICATION),
    ],
)
async def test_response_outcomes(path, response, error):
    platform = Platform(override={path: response})
    result = await platform.run()
    status = RemoteStatus.NOT_SUBMITTED
    if path == PATHS[3]:
        status = RemoteStatus.REJECTED if error == ErrorCode.REJECTED else RemoteStatus.UNKNOWN
    assert result.remote_status == status and result.error_code == error
    assert len(platform.requests) == PATHS.index(path) + 1
    assert platform.changes <= 1


@pytest.mark.parametrize('path', PATHS)
@pytest.mark.parametrize(
    ('exception', 'error'),
    [
        (httpx.ReadTimeout('fake-token-secret'), ErrorCode.TIMEOUT),
        (httpx.ConnectTimeout('fake-token-secret'), ErrorCode.TIMEOUT),
        (TimeoutError('fake-token-secret'), ErrorCode.TIMEOUT),
        (httpx.ConnectError('fake-token-secret'), ErrorCode.TRANSPORT),
        (httpx.ReadError('fake-token-secret'), ErrorCode.TRANSPORT),
        (ValueError('fake-token-secret'), ErrorCode.PREPARATION),
        (asyncio.CancelledError('fake-token-secret'), ErrorCode.CANCELLED),
    ],
)
async def test_transport_failure_never_retries(path, exception, error):
    platform = Platform(override={path: exception})
    result = await platform.run()
    assert result.error_code == error
    assert result.remote_status == (RemoteStatus.UNKNOWN if path == PATHS[3] else RemoteStatus.NOT_SUBMITTED)
    assert len(platform.requests) == PATHS.index(path) + 1
    assert platform.changes <= 1


@pytest.mark.parametrize('path', [PATHS[1], PATHS[3]])
async def test_real_task_cancellation_before_and_after_submit(path):
    platform = Platform()
    entered = asyncio.Event()

    async def handle(request):
        if request.url.path == path:
            platform.requests.append(request)
            entered.set()
            await asyncio.Event().wait()
        return await platform.handle(request)

    task = asyncio.create_task(change_password(VALUES, before_submit=ready, transport=httpx.MockTransport(handle)))
    await entered.wait()
    task.cancel()
    result = await task
    assert result.error_code == ErrorCode.CANCELLED
    assert result.remote_status == (RemoteStatus.UNKNOWN if path == PATHS[3] else RemoteStatus.NOT_SUBMITTED)
    assert platform.changes == (1 if path == PATHS[3] else 0)


@pytest.mark.parametrize('exception', [RuntimeError('fake-token-secret'), TimeoutError(), asyncio.CancelledError()])
async def test_before_submit_failure_cannot_send(exception):
    platform = Platform()

    async def fail():
        raise exception

    assert (await platform.run(before_submit=fail)).remote_status == RemoteStatus.NOT_SUBMITTED
    assert platform.changes == 0


async def test_pending_cancellation_after_intent_marker_cannot_send():
    platform = Platform()

    async def cancel():
        asyncio.current_task().cancel()

    task = asyncio.create_task(platform.run(before_submit=cancel))
    result = await task
    assert result.remote_status == RemoteStatus.NOT_SUBMITTED
    assert result.error_code == ErrorCode.CANCELLED and platform.changes == 0


@pytest.mark.parametrize(
    'change',
    [
        {'phone': 'invalid'},
        {'area': 'invalid'},
        {'old_password': ''},
        {'new_password': VALUES.old_password},
        {'new_password': 'tiny'},
        {'new_password': 'x' * 17},
        {'new_password': 'with space'},
        {'new_password': '😀' * 9},
        {'new_password': '\ufeffxxxxxx'},
        {'new_password': '\ud800xxxxxx'},
    ],
)
async def test_invalid_input_stops_before_http(change):
    platform = Platform()
    assert (await platform.run(replace(VALUES, **change))).remote_status == RemoteStatus.NOT_SUBMITTED
    assert not platform.requests


@pytest.mark.parametrize(
    'change',
    [
        {'backend': 'other.invalid'},
        {'baseUrl': '//other.invalid'},
        {'signSecret': '!invalid'},
        {'encryptoSecret': ''},
    ],
)
async def test_invalid_dynamic_config_stops(change):
    platform = Platform(config={**CONFIG, **change})
    result = await platform.run()
    assert result.remote_status == RemoteStatus.NOT_SUBMITTED and result.error_code == ErrorCode.CONFIG
    assert len(platform.requests) == 1


@pytest.mark.parametrize('path', PATHS)
async def test_sensitive_failures_are_not_exposed(path, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    payload = ' '.join(SECRETS)
    platform = Platform(override={path: RuntimeError(payload)})
    result = await platform.run()
    output = capsys.readouterr()
    visible = str(result) + repr(result) + repr(VALUES) + caplog.text + output.out + output.err
    for secret in SECRETS:
        assert secret not in visible


async def test_raw_response_and_cookie_never_appear_in_output(caplog, capsys):
    caplog.set_level(logging.DEBUG)
    platform = Platform(override={PATHS[3]: httpx.Response(200, json={'code': 1300, 'msg': ' '.join(SECRETS)})})
    result = await platform.run()
    output = capsys.readouterr()
    visible = str(result) + caplog.text + output.out + output.err
    for secret in SECRETS:
        assert secret not in visible


@pytest.mark.parametrize('business_code', [100, 1300])
@pytest.mark.parametrize('cleanup_error', [RuntimeError('fake-token-secret'), asyncio.CancelledError(), httpx.ReadTimeout('fake-token-secret')])
async def test_cleanup_failure_preserves_decisive_remote_result(business_code, cleanup_error):
    platform = Platform(override={PATHS[3]: httpx.Response(200, json={'code': business_code})})

    class CloseFailure(httpx.MockTransport):
        async def aclose(self):
            raise cleanup_error

    result = await change_password(VALUES, before_submit=ready, transport=CloseFailure(platform.handle))
    expected = RemoteStatus.REPORTED_SUCCESS if business_code == 100 else RemoteStatus.REJECTED
    assert result.remote_status == expected
    assert result.error_code == (ErrorCode.NONE if business_code == 100 else ErrorCode.REJECTED)
    assert result.stage == Stage.SUBMIT and platform.changes == 1
