import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from web.tools._updater import handlers


@pytest.mark.asyncio
async def test_manual_start_rejects_non_object_json(tmp_path):
    handlers.set_context(str(tmp_path))
    app = web.Application()
    app.router.add_post('/start', handlers.handle_start_update)
    async with TestClient(TestServer(app)) as client:
        response = await client.post('/start', json=['invalid'])
        assert response.status == 400
        assert (await response.json())['success'] is False


@pytest.mark.asyncio
async def test_removed_upload_never_reads_or_writes_payload(tmp_path):
    handlers.set_context(str(tmp_path))
    result = await handlers.handle_upload_update(SimpleNamespace())
    assert json.loads(result.body)['success'] is False
    assert not (tmp_path / 'data/temp_update').exists()


@pytest.fixture
def market(tmp_path):
    """Plugin market mirror preference as stored before the updater change."""
    from web.tools._market import shared

    (tmp_path / 'data').mkdir()
    settings = tmp_path / 'data/update_settings.json'
    settings.write_text(json.dumps({'custom_mirror': 'https://old-mirror.example/'}))
    handlers.set_context(str(tmp_path))
    shared.set_context(str(tmp_path))
    return settings


@pytest.mark.asyncio
async def test_market_mirror_preference_is_listed_and_can_be_cleared(market):
    from web.tools._market import shared

    listed = json.loads((await handlers.handle_get_mirrors(None)).body)['data']
    assert listed['custom_mirror'] == 'https://old-mirror.example/'
    assert listed['mirrors']

    async def body():
        return {'mirror': ''}

    reset = json.loads((await handlers.handle_set_custom_mirror(SimpleNamespace(json=body))).body)
    assert reset['success'] is True
    assert json.loads(market.read_text())['custom_mirror'] == ''
    assert json.loads((await handlers.handle_get_mirrors(None)).body)['data']['custom_mirror'] == ''
    assert not shared._ranked_mirror_urls('https://github.com/example/plugin/archive/main.zip')[0].startswith('https://old-mirror.example/')


@pytest.mark.asyncio
async def test_market_mirror_speed_test_streams_sse(market, monkeypatch):
    async def probe(mirror, timeout=3):
        return {'mirror': mirror, 'latency': 0.1, 'success': bool(mirror)}

    monkeypatch.setattr(handlers, '_test_one_mirror', probe)
    monkeypatch.setattr(handlers, 'GITHUB_FILE_MIRRORS', ['https://a.example/', 'https://b.example/'])
    app = web.Application()
    app.router.add_get('/test-mirrors', handlers.handle_test_mirrors)
    async with TestClient(TestServer(app)) as client:
        response = await client.get('/test-mirrors')
        assert response.headers['Content-Type'].startswith('text/event-stream')
        events = [json.loads(line[6:]) for line in (await response.text()).splitlines() if line.startswith('data: ')]
    assert events[-1] == {'done': True}
    assert {e['mirror'] for e in events[:-1]} == {'https://a.example/', 'https://b.example/', ''}
    cache = json.loads((market.parent / 'mirror_cache.json').read_text())['mirrors'] if (market.parent / 'mirror_cache.json').exists() else None
    assert cache is None or [m['mirror'] for m in cache] == ['https://a.example/', 'https://b.example/']


@pytest.mark.asyncio
async def test_framework_fetch_ignores_market_mirror(market, monkeypatch):
    """The market preference must never reach the updater's Git fetch."""
    import subprocess

    commands = []
    real_run = subprocess.run

    class StopFetchError(Exception):
        pass

    def run(command, *args, **kwargs):
        commands.append(command)
        if 'fetch' in command:
            raise StopFetchError
        if 'init' in command:
            return real_run(command, *args, **kwargs)
        return subprocess.CompletedProcess(command, 0, 'f' * 40, '')

    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(StopFetchError):
        handlers.get_updater()._fetch()
    fetch = next(c for c in commands if 'fetch' in c)
    assert 'https://github.com/xmxxc/QQBotForge.git' in fetch
    assert not any('old-mirror' in str(part) for c in commands for part in c)
