"""The account-center client verifies the server even if the default context is weakened (#122)."""

import asyncio
import ssl

import httpx
import pytest

import plugins.ninja3.password_protocol as protocol
from plugins.ninja3.password_protocol import ChangeInput, VerifyInput

VALUES = ChangeInput('16600000000', '86', 'old-test-secret', 'new-test-secret')


def _weakened(original):
    def no_verify(*args, **kwargs):
        context = original(*args, **kwargs)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    return no_verify


def _assert_verifying(context):
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.cert_store_stats()['x509_ca'] > 0


def test_tls_context_requires_verification_despite_global_patch(monkeypatch):
    monkeypatch.setattr(ssl, 'create_default_context', _weakened(ssl.create_default_context))

    _assert_verifying(protocol._tls_context())


@pytest.mark.parametrize('call', ['change', 'verify'])
def test_protocol_clients_use_verifying_context(monkeypatch, call):
    monkeypatch.setattr(ssl, 'create_default_context', _weakened(ssl.create_default_context))
    seen = []
    real = httpx.AsyncClient

    def client(*args, **kwargs):
        seen.append(kwargs)
        inner = real(*args, **kwargs)
        assert kwargs.get('transport') is None
        _assert_verifying(inner._transport._pool._ssl_context)
        raise httpx.ConnectError('stop before network')

    monkeypatch.setattr(protocol.httpx, 'AsyncClient', client)

    async def before_submit():
        raise AssertionError('must not reach submit')

    if call == 'change':
        result = asyncio.run(protocol.change_password(VALUES, before_submit=before_submit))
        assert result == protocol.ChangeResult(protocol.Stage.PREPARE, protocol.RemoteStatus.NOT_SUBMITTED, protocol.ErrorCode.TRANSPORT)
    else:
        result = asyncio.run(protocol.verify_password(VerifyInput(VALUES.phone, VALUES.area, VALUES.new_password)))
        assert result.verified is False

    assert len(seen) == 1
    _assert_verifying(seen[0]['verify'])
