"""Single-attempt account-center protocol. No persistence, CLI or application wiring."""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ORIGIN = 'https://user.pandadastudio.com'
CHANGE_PATH = '/user_center/change_pwd'


class Stage(StrEnum):
    PREPARE = 'prepare'
    CONFIG = 'config'
    LOGIN = 'login'
    IDENTITY = 'identity'
    PREPARE_SUBMIT = 'prepare_submit'
    SUBMIT = 'submit'
    VERIFY = 'verify'


class RemoteStatus(StrEnum):
    NOT_SUBMITTED = 'not_submitted'
    REPORTED_SUCCESS = 'reported_success'
    REJECTED = 'rejected'
    UNKNOWN = 'unknown'
    VERIFIED_SUCCESS = 'verified_success'


class ErrorCode(StrEnum):
    NONE = 'none'
    INVALID_INPUT = 'invalid_input'
    CONFIG = 'config'
    RESPONSE = 'response'
    REJECTED = 'rejected'
    VERIFICATION = 'verification'
    IDENTITY = 'identity'
    TRANSPORT = 'transport'
    TIMEOUT = 'timeout'
    CANCELLED = 'cancelled'
    PREPARATION = 'preparation'


@dataclass(frozen=True)
class ChangeResult:
    stage: Stage
    remote_status: RemoteStatus
    error_code: ErrorCode


@dataclass(frozen=True)
class ChangeInput:
    """Resolved secrets only; task authorization and protected storage belong to caller.

    Identity contract: userinfo.data must have exact, unmasked string phone/area.
    This schema is deliberately fail-closed pending sanitized platform evidence.
    """

    phone: str = field(repr=False)
    area: str = field(repr=False)
    old_password: str = field(repr=False)
    new_password: str = field(repr=False)


@dataclass(frozen=True)
class VerifyInput:
    """The fixed intent and identity used to check an uncertain change result."""

    phone: str = field(repr=False)
    area: str = field(repr=False)
    password: str = field(repr=False)


@dataclass(frozen=True)
class VerifyResult:
    verified: bool
    error_code: ErrorCode


class _ProtocolError(Exception):
    def __init__(self, code: ErrorCode):
        self.code = code
        super().__init__(code.value)


def _encrypt(password: str, secret: str, iv: bytes) -> str:
    key = base64.b64decode(secret, validate=True)[:32].ljust(32, b'\0')
    padder = padding.PKCS7(128).padder()
    padded = padder.update(password.encode('utf-8')) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(iv + encryptor.update(padded) + encryptor.finalize()).decode('ascii')


def _headers(config: dict[str, str], method: str, path: str, body: bytes, date: str, nonce: bytes, trace: bytes) -> dict[str, str]:
    headers = {
        'x-yh-date': date,
        'x-yh-nonce': base64.b64encode(nonce).decode('ascii'),
        'x-yh-traceid': base64.b64encode(trace).decode('ascii'),
        'x-yh-appid': config['appId'],
        'x-yh-web-platform': 'official-website',
    }
    canonical_headers = '\n'.join(f'{key}:{headers[key]}' for key in sorted(headers))
    digest = base64.b64encode(hashlib.sha1(body).digest()).decode('ascii')
    canonical = '\n'.join((method.upper(), config['backend'], path, '', canonical_headers, digest))
    signature = hmac.new(base64.b64decode(config['signSecret'], validate=True), canonical.encode('utf-8'), hashlib.sha1).digest()
    headers['Authorization'] = 'YH ' + base64.b64encode(signature).decode('ascii')
    headers.update({'Origin': ORIGIN, 'Referer': ORIGIN + '/'})
    return headers


def _config(response: httpx.Response) -> dict[str, str]:
    if response.status_code != 200:
        raise _ProtocolError(ErrorCode.CONFIG)
    config = {}
    for key in ('appId', 'baseUrl', 'backend', 'signSecret', 'encryptoSecret'):
        match = re.search(rf'^\s*{key}\s*:\s*["\x27]([^"\x27\r\n]+)["\x27]', response.text, re.MULTILINE)
        if not match:
            raise _ProtocolError(ErrorCode.CONFIG)
        config[key] = match.group(1)
    if config['backend'] != 'user.pandadastudio.com' or config['baseUrl'] != '/api/web':
        raise _ProtocolError(ErrorCode.CONFIG)
    try:
        for key in ('signSecret', 'encryptoSecret'):
            if not base64.b64decode(config[key], validate=True):
                raise ValueError
    except ValueError:
        raise _ProtocolError(ErrorCode.CONFIG) from None
    return config


def _data(response: httpx.Response) -> object:
    # Redirects, HTTP errors, malformed responses and verification are not refusals.
    if response.status_code != 200:
        raise _ProtocolError(ErrorCode.RESPONSE)
    if response.headers.get('x-yh-captcha-trigger'):
        raise _ProtocolError(ErrorCode.VERIFICATION)
    try:
        result = response.json()
    except ValueError:
        raise _ProtocolError(ErrorCode.RESPONSE) from None
    if not isinstance(result, dict) or type(result.get('code')) is not int:
        raise _ProtocolError(ErrorCode.RESPONSE)
    if result['code'] != 100:
        raise _ProtocolError(ErrorCode.REJECTED)
    return result.get('data')


def _validate(values: ChangeInput) -> None:
    if not re.fullmatch(r'[0-9]{6,15}', values.phone) or not re.fullmatch(r'[0-9]{1,4}', values.area):
        raise _ProtocolError(ErrorCode.INVALID_INPUT)
    new = values.new_password
    if (
        not values.old_password
        or new == values.old_password
        or not 6 <= len(new.encode('utf-16-le')) // 2 <= 16
        or any(c.isspace() or c == '\ufeff' for c in new)
    ):
        raise _ProtocolError(ErrorCode.INVALID_INPUT)


async def change_password(
    values: ChangeInput,
    *,
    before_submit: Callable[[], Awaitable[None]],
    transport: httpx.AsyncBaseTransport | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    random_bytes: Callable[[int], bytes] = os.urandom,
) -> ChangeResult:
    """Perform at most one change POST, using a fresh cookie jar and no retries.

    before_submit must durably mark this intent as potentially submitted; failure
    stops here. Only a cancellation checkpoint occurs between its return and dispatch. Cancellation is consumed into a safe result; after
    dispatch it cannot recall the remote request. Process death has no result.
    Test transports are trusted dependencies and must not retry requests either.
    """
    stage = Stage.PREPARE
    submitted = False
    decisive: ChangeResult | None = None
    try:
        _validate(values)
        async with httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False, timeout=30) as client:
            stage = Stage.CONFIG
            config = _config(await client.get(ORIGIN + '/env.js', timeout=20))

            def request(method: str, path: str, payload: dict[str, str] | None = None) -> httpx.Request:
                body = b'' if payload is None else json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                headers = _headers(config, method, path, body, now().astimezone(UTC).strftime('%Y%m%dT%H%M%SZ'), random_bytes(16), random_bytes(16))
                if payload is not None:
                    headers['Content-Type'] = 'application/json;charset=UTF-8'
                return client.build_request(method, ORIGIN + config['baseUrl'] + path, content=body, headers=headers)

            stage = Stage.LOGIN
            _data(
                await client.send(
                    request(
                        'POST',
                        '/login/pwd',
                        {
                            'phone': values.phone,
                            'pwd': _encrypt(values.old_password, config['encryptoSecret'], random_bytes(16)),
                            'area': values.area,
                        },
                    )
                )
            )
            stage = Stage.IDENTITY
            identity = _data(await client.send(request('GET', '/user_center/userinfo')))
            if not isinstance(identity, dict) or identity.get('phone') != values.phone or identity.get('area') != values.area:
                raise _ProtocolError(ErrorCode.IDENTITY)
            prepared = request(
                'POST',
                CHANGE_PATH,
                {
                    'old_pwd': _encrypt(values.old_password, config['encryptoSecret'], random_bytes(16)),
                    'new_pwd': _encrypt(values.new_password, config['encryptoSecret'], random_bytes(16)),
                },
            )
            stage = Stage.PREPARE_SUBMIT
            await before_submit()
            await asyncio.sleep(0)  # Deliver pending cancellation before crossing the boundary.
            stage = Stage.SUBMIT
            submitted = True  # Conservative: even failures before bytes leave now mean UNKNOWN.
            try:
                _data(await client.send(prepared))
            except _ProtocolError as exc:
                if exc.code != ErrorCode.REJECTED:
                    raise
                decisive = ChangeResult(stage, RemoteStatus.REJECTED, ErrorCode.REJECTED)
            else:
                decisive = ChangeResult(stage, RemoteStatus.REPORTED_SUCCESS, ErrorCode.NONE)
            return decisive
    except _ProtocolError as exc:
        code = exc.code
    except (asyncio.CancelledError, KeyboardInterrupt):
        code = ErrorCode.CANCELLED
    except (httpx.TimeoutException, TimeoutError):
        code = ErrorCode.TIMEOUT
    except httpx.RequestError:
        code = ErrorCode.TRANSPORT
    except Exception:
        # Never propagate arbitrary response/transport/callback errors with secrets.
        code = ErrorCode.PREPARATION
    if decisive is not None:
        # Session cleanup cannot erase an already observed remote outcome.
        return decisive
    status = RemoteStatus.UNKNOWN if submitted else RemoteStatus.NOT_SUBMITTED
    return ChangeResult(stage, status, code)


async def verify_password(
    values: VerifyInput,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    random_bytes: Callable[[int], bytes] = os.urandom,
) -> VerifyResult:
    """Log in once with the fixed new password and verify the same account identity.

    A successful password login alone is insufficient: the account-center userinfo
    response must contain the exact phone and area from the intent.  Every failure is
    deliberately inconclusive and leaves the durable task paused as ``unknown``.
    """
    try:
        if not re.fullmatch(r'[0-9]{6,15}', values.phone) or not re.fullmatch(r'[0-9]{1,4}', values.area):
            raise _ProtocolError(ErrorCode.INVALID_INPUT)
        if not values.password or not 6 <= len(values.password.encode('utf-16-le')) // 2 <= 16 or any(c.isspace() or c == '\ufeff' for c in values.password):
            raise _ProtocolError(ErrorCode.INVALID_INPUT)
        async with httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False, timeout=30) as client:
            config = _config(await client.get(ORIGIN + '/env.js', timeout=20))

            def request(method: str, path: str, payload: dict[str, str] | None = None) -> httpx.Request:
                body = b'' if payload is None else json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                headers = _headers(config, method, path, body, now().astimezone(UTC).strftime('%Y%m%dT%H%M%SZ'), random_bytes(16), random_bytes(16))
                if payload is not None:
                    headers['Content-Type'] = 'application/json;charset=UTF-8'
                return client.build_request(method, ORIGIN + config['baseUrl'] + path, content=body, headers=headers)

            _data(
                await client.send(
                    request(
                        'POST',
                        '/login/pwd',
                        {
                            'phone': values.phone,
                            'pwd': _encrypt(values.password, config['encryptoSecret'], random_bytes(16)),
                            'area': values.area,
                        },
                    )
                )
            )
            identity = _data(await client.send(request('GET', '/user_center/userinfo')))
            if not isinstance(identity, dict) or identity.get('phone') != values.phone or identity.get('area') != values.area:
                return VerifyResult(False, ErrorCode.IDENTITY)
            return VerifyResult(True, ErrorCode.NONE)
    except _ProtocolError as exc:
        return VerifyResult(False, exc.code)
    except (asyncio.CancelledError, KeyboardInterrupt):
        return VerifyResult(False, ErrorCode.CANCELLED)
    except (httpx.TimeoutException, TimeoutError):
        return VerifyResult(False, ErrorCode.TIMEOUT)
    except httpx.RequestError:
        return VerifyResult(False, ErrorCode.TRANSPORT)
    except Exception:
        return VerifyResult(False, ErrorCode.PREPARATION)
