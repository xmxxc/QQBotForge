"""Bundled SQLite v1 records; independent of other extension installations."""

import asyncio
import json
import sqlite3
import threading
from pathlib import Path


class ExtensionStore:
    """Bot-partitioned records; committed writes and serialized connection shutdown."""

    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.Lock()
        self._connection = None
        self._closed = False

    def _open(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False)
        try:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            if version > 1:
                raise RuntimeError('扩展数据库版本高于当前程序')
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute(
                'CREATE TABLE IF NOT EXISTS extension_records (bot TEXT, collection TEXT, id TEXT, payload TEXT NOT NULL, PRIMARY KEY(bot, collection, id))'
            )
            conn.execute('PRAGMA user_version=1')
            conn.commit()
            return conn
        except Exception:
            conn.close()
            raise

    def _run(self, sql, params, read):
        with self._lock:
            if self._closed:
                raise RuntimeError('扩展存储已关闭')
            if self._connection is None:
                self._connection = self._open()
            with self._connection:
                cursor = self._connection.execute(sql, params)
                return [json.loads(row[0]) for row in cursor.fetchall()] if read else None

    async def _execute(self, sql, params, read=False):
        operation = asyncio.create_task(asyncio.to_thread(self._run, sql, params, read))
        cancellation: asyncio.CancelledError | None = None
        # Cancellation cannot stop SQLite's worker. Drain it before callers release
        # their ordering locks, including when the caller is cancelled repeatedly.
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError as error:
                cancellation = error
            except Exception:
                break
        if cancellation is not None:
            if not operation.cancelled():
                operation.exception()
            raise cancellation
        return operation.result()

    async def ready(self):
        await self._execute('SELECT 1', ())

    async def all(self, bot, collection):
        return await self._execute('SELECT payload FROM extension_records WHERE bot=? AND collection=? ORDER BY rowid', (bot, collection), True)

    async def get(self, bot, collection, key):
        rows = await self._execute('SELECT payload FROM extension_records WHERE bot=? AND collection=? AND id=?', (bot, collection, key), True)
        return rows[0] if rows else None

    async def put(self, bot, collection, key, record):
        await self._execute(
            'INSERT INTO extension_records VALUES (?,?,?,?) ON CONFLICT(bot,collection,id) DO UPDATE SET payload=excluded.payload',
            (bot, collection, key, json.dumps(record, ensure_ascii=False)),
        )

    async def delete(self, bot, collection, key):
        await self._execute('DELETE FROM extension_records WHERE bot=? AND collection=? AND id=?', (bot, collection, key))

    def close(self):
        with self._lock:
            self._closed = True
            if self._connection is not None:
                self._connection.close()
                self._connection = None
