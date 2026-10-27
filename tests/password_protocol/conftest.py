"""Protocol evidence must fail if any code tries to use real DNS or TCP."""

import socket

import pytest


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('offline protocol tests prohibit network')

    for name in ('connect', 'connect_ex'):
        monkeypatch.setattr(socket.socket, name, blocked)
    for name in ('create_connection', 'getaddrinfo'):
        monkeypatch.setattr(socket, name, blocked)


@pytest.fixture
def original(monkeypatch):
    import sys

    from test_issue70 import SOURCE, load

    monkeypatch.setitem(sys.modules, 'change_password', load('change_password', SOURCE / 'change_password.py'))
    return load('issue70_tests', SOURCE / 'test_change_password.py')
