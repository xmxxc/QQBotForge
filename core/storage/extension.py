"""Extension records on the framework SQLite pool; caller owns collection schemas."""

import asyncio
import json
import sqlite3
from pathlib import Path

from core.storage._sqlite import SQLiteConnectionPool
from core.storage._workers import SQLITE_EXECUTOR


class ExtensionStore:
    """Bot-partitioned records. Writes are committed before returning; failures propagate."""

    def __init__(self, path):
        self.path = str(path)
        self.pool = SQLiteConnectionPool(self._open, self._open)

    @staticmethod
    def _open(path, _kind):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        if version > 1:
            conn.close()
            raise RuntimeError('扩展数据库版本高于当前程序')
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute(
            'CREATE TABLE IF NOT EXISTS extension_records (bot TEXT, collection TEXT, id TEXT, payload TEXT NOT NULL, PRIMARY KEY(bot, collection, id))'
        )
        conn.execute('PRAGMA user_version=1')
        conn.commit()
        return conn

    def _run(self, sql, params, read):
        with self.pool.connection(self.path, 'extension') as conn, conn:
            cursor = conn.execute(sql, params)
            return [json.loads(row[0]) for row in cursor.fetchall()] if read else None

    async def _execute(self, sql, params, read=False):
        return await asyncio.get_running_loop().run_in_executor(SQLITE_EXECUTOR, self._run, sql, params, read)

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
        self.pool.close()
