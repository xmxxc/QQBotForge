"""入口与框架 HTTPS 客户端保持证书与主机名校验 (#122)

在子进程中导入真实 main.py 与 web/tools/_bot/info.py，确认导入后标准库默认上下文、
httpx 默认客户端以及机器人信息查询的 aiohttp 上下文都要求校验；不访问网络。
"""

import json
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_PROBE = """
import json, ssl, sys
sys.path.insert(0, sys.argv[1])
orig = ssl.create_default_context
import main
import httpx
from web.tools._bot import info

def describe(ctx):
    return {'verify_mode': int(ctx.verify_mode), 'check_hostname': ctx.check_hostname}

print(json.dumps({
    'patched': ssl.create_default_context is not orig,
    'default': describe(ssl.create_default_context()),
    'httpx': describe(httpx.create_ssl_context()),
    'info': describe(info._SSL_CTX),
}))
"""


def _probe():
    env = {k: v for k, v in os.environ.items() if not k.startswith(('SSL_CERT', 'REQUESTS_CA'))}
    out = subprocess.run(
        [sys.executable, '-c', _PROBE, _ROOT], cwd=_ROOT, env=env,
        capture_output=True, text=True, timeout=60, check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_importing_main_keeps_tls_verification():
    result = _probe()

    assert result['patched'] is False
    required = {'verify_mode': 2, 'check_hostname': True}  # ssl.CERT_REQUIRED
    assert result['default'] == required
    assert result['httpx'] == required
    assert result['info'] == required
