"""QQ socket protocol substitute; drives real WSClient, never status internals."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


class Socket:
    def __init__(self):
        self.frames = asyncio.Queue()
        self.sent = []

    def feed(self, op, *, t=None, s=None, d=None, event_id=None):
        self.frames.put_nowait(json.dumps(dict(op=op, t=t, s=s, d=d, id=event_id)))

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self.frames.get()
        if isinstance(frame, Exception):
            raise frame
        if frame is None:
            raise StopAsyncIteration
        return frame

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def close(self):
        self.frames.put_nowait(None)


class Gateway:
    api_base = 'https://api.bot.qq.com'

    def __init__(self):
        self.sockets = asyncio.Queue()

    async def get_token(self):
        return 'fake-token'

    async def get_client(self):
        return self

    async def get(self, url, **kwargs):
        return SimpleNamespace(json=lambda: {'url': 'wss://api.bot.qq.com/websocket/'})

    @asynccontextmanager
    async def connect(self, url, **kwargs):
        socket = await self.sockets.get()
        try:
            yield socket
        finally:
            await socket.close()

    def add(self):
        socket = Socket()
        self.sockets.put_nowait(socket)
        return socket
