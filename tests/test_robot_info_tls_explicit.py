"""Robot info TLS stays verified even if the default context factory is weakened.

Adapted from the offline subprocess probe in b64d36f (personal PR #121).
The distinct filename also allows these assets to overlay personal unchanged.
"""

import json
import os
import ssl
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_PROBE = """
import json
import ssl
import sys

def deny_network(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo', 'socket.bind'}:
        raise AssertionError('TLS context probe must stay offline')

sys.addaudithook(deny_network)
sys.path.insert(0, sys.argv[1])
original_factory = ssl.create_default_context

def weakened(*args, **kwargs):
    context = original_factory(*args, **kwargs)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context

if sys.argv[2] == 'weakened':
    ssl.create_default_context = weakened

from web.tools._bot import info

def describe(context):
    return {
        'verify_mode': int(context.verify_mode),
        'check_hostname': context.check_hostname,
        'protocol': int(context.protocol),
        'ca_count': context.cert_store_stats()['x509_ca'],
    }

print(json.dumps({
    'default': describe(ssl.create_default_context()),
    'info': describe(info._SSL_CTX),
}))
"""


@pytest.mark.parametrize('factory', ['normal', 'weakened'])
def test_robot_info_requires_server_verification(factory):
    env = {key: value for key, value in os.environ.items() if not key.startswith(('SSL_CERT', 'REQUESTS_CA'))}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    result = subprocess.run(
        [sys.executable, '-c', _PROBE, str(_ROOT), factory],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    contexts = json.loads(result.stdout.strip().splitlines()[-1])

    # Control: the injected factory really is weak, and importing info does not
    # repair global state to hide its own reliance on that factory.
    default = contexts['default']
    assert default['verify_mode'] == (ssl.CERT_NONE if factory == 'weakened' else ssl.CERT_REQUIRED)
    assert default['check_hostname'] is (factory == 'normal')
    assert default['ca_count'] > 0

    context = contexts['info']
    assert context['verify_mode'] == ssl.CERT_REQUIRED
    assert context['check_hostname'] is True
    assert context['protocol'] == ssl.PROTOCOL_TLS_CLIENT
    assert context['ca_count'] > 0
