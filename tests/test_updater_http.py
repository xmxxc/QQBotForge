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
