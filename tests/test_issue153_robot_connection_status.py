"""后台机器人详情与顶部栏的连接状态取自连接状态服务 (#153)

用真实 WSClient 向 ConnectionStatusManager 发布阶段，检查 /api/robot/info 的 connection_status
与 /api/bots 的 connected；外部机器人资料接口被替换为失败，不访问网络。
"""

import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import make_mocked_request

from core.module.connection_status import ConnectionStatusManager
from core.network.websocket import WSClient
from web.tools import _bots
from web.tools._bot import info

APPID = '102000001'


def _ws_bot(*phases):
    manager = ConnectionStatusManager()
    client = WSClient(APPID, token_manager=None, on_event=None, ack_interaction=None, connection_status=manager)
    for phase in phases:
        if phase == 'connecting':
            client._status.update_context(connection_generation='g1')
        client._status.publish(phase, reason='test')
    if 'ready' in phases:
        client._session_id = 'session-1'
    return SimpleNamespace(appid=APPID, robot_qq='', union_openid='', ws_client=client, connection_status=manager)


def _webhook_bot():
    return SimpleNamespace(appid=APPID, robot_qq='', union_openid='', ws_client=None, connection_status=ConnectionStatusManager())


CASES = {
    'connected': (_ws_bot('connecting', 'connected', 'identifying', 'ready'), '已连接', True),
    'disconnected': (_ws_bot('connecting', 'connected', 'identifying', 'ready', 'stopped'), '未连接', False),
    'reconnecting': (_ws_bot('connecting', 'connected', 'identifying', 'ready', 'disconnected', 'reconnecting'), '断线重连中', False),
    'webhook': (_webhook_bot(), '等待接收中', False),
}


class _Offline:
    def __init__(self, *args, **kwargs):
        raise OSError('offline')


@pytest.fixture
def robot_info(monkeypatch):
    async def call(bot):
        monkeypatch.setattr(info, '_bot_manager', SimpleNamespace(_bots={APPID: bot}))
        monkeypatch.setattr(info._aiohttp, 'ClientSession', _Offline)
        monkeypatch.setattr(info, 'cfg', SimpleNamespace(get=lambda name, key=None, default=None: default))
        resp = await info.handle_get_robot_info(make_mocked_request('GET', f'/api/robot/info?appid={APPID}'))
        return json.loads(resp.body)

    return call


@pytest.mark.parametrize('case', CASES)
async def test_robot_info_connection_status(case, robot_info):
    bot, expected, _ = CASES[case]
    data = await robot_info(bot)
    assert data['connection_status'] == expected
    assert data['connection_type'] == ('Webhook' if case == 'webhook' else 'WebSocket')


@pytest.mark.parametrize('case', CASES)
def test_bots_payload_connected(case):
    bot, _, connected = CASES[case]
    payload = _bots.running_bot_payload(APPID, bot)
    assert payload['connected'] is connected
    assert payload['connection_status'] == CASES[case][1]


def test_protocol_unknown_on_open_socket_stays_connected():
    bot = _ws_bot('connecting', 'connected', 'identifying', 'ready', 'unknown')
    assert _bots.ws_connection_state(bot) == 'connected'


def test_created_but_not_started_is_disconnected():
    assert _bots.ws_connection_state(_ws_bot()) == 'disconnected'


def test_configured_payload_has_unconnected_status():
    payload = _bots.configured_bot_payload({'appid': APPID, 'secret': 's', 'enabled': True})
    assert payload['connection_status'] == '未连接'
    assert payload['connected'] is False


async def test_bots_http_payload_keeps_status_and_compatibility(monkeypatch):
    from web import api

    connected = _ws_bot('connecting', 'connected', 'ready')
    manager2 = ConnectionStatusManager()
    client2 = WSClient('102000002', token_manager=None, on_event=None, ack_interaction=None, connection_status=manager2)
    client2._status.update_context(connection_generation='g2')
    for phase in ('connecting', 'connected', 'disconnected', 'reconnecting'):
        client2._status.publish(phase, reason='test')
    reconnecting = SimpleNamespace(appid='102000002', robot_qq='', union_openid='', ws_client=client2, connection_status=manager2)
    manager = SimpleNamespace(_bots={APPID: connected, reconnecting.appid: reconnecting})
    monkeypatch.setattr(api, '_bot_manager', manager)
    monkeypatch.setattr(api, 'cfg', SimpleNamespace(get_bot_configs=lambda: [
        {'appid': '102000003', 'secret': 's', 'enabled': False},
    ]))
    response = await api.handle_get_bots(make_mocked_request('GET', '/api/bots'))
    body = json.loads(response.body)
    bots = {item['appid']: item for item in body['data']['bots']}
    assert bots[APPID]['connected'] is True
    assert bots[APPID]['connection_status'] == '已连接'
    assert bots['102000002']['connected'] is False
    assert bots['102000002']['connection_status'] == '断线重连中'
    assert bots['102000003']['connected'] is False
    assert bots['102000003']['connection_status'] == '未连接'
