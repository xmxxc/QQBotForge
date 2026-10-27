"""Bundled SQLite records (user_version 1; 2 once password tasks exist); independent of other extensions and account_sharing data."""

import asyncio
import json
import os
import sqlite3
import threading
from pathlib import Path

CREDENTIALS = ('login', 'password')


class AccountLockedError(RuntimeError):
    """Account has an unresolved password task; credential edits and deletion wait for it."""


class ExtensionStore:
    """Bot-partitioned records; committed writes and serialized connection shutdown."""

    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.Lock()
        self._connection = None
        self._closed = False

    def _open(self):
        Path(self.path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass  # Existing database permissions remain the operator's responsibility.
        else:
            os.close(descriptor)
        conn = sqlite3.connect(self.path, check_same_thread=False)
        try:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            if version > 2:
                raise RuntimeError('扩展数据库版本高于当前程序')
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute(
                'CREATE TABLE IF NOT EXISTS extension_records (bot TEXT, collection TEXT, id TEXT, payload TEXT NOT NULL, PRIMARY KEY(bot, collection, id))'
            )
            if version == 0:
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
        return await asyncio.to_thread(self._run, sql, params, read)

    def _atomic(self, action):
        with self._lock:
            if self._closed:
                raise RuntimeError('扩展存储已关闭')
            if self._connection is None:
                self._connection = self._open()
            conn = self._connection
            conn.execute('BEGIN IMMEDIATE')
            try:
                result = action(conn)
                conn.commit()
                return result
            except BaseException:
                conn.rollback()
                raise

    async def atomic(self, action):
        """One SQLite transaction; cancellation cannot recall work already running in its thread."""
        return await asyncio.to_thread(self._atomic, action)

    async def ready(self):
        await self._execute('SELECT 1', ())

    async def all(self, bot, collection):
        return await self._execute('SELECT payload FROM extension_records WHERE bot=? AND collection=? ORDER BY rowid', (bot, collection), True)

    async def project(self, bot, collection, fields):
        """Only ``fields`` of each record: SQLite extracts them, other values never reach Python.

        Same rows as ``all`` (no second copy to keep in sync); a missing field reads as None.
        """
        if not fields or not all(isinstance(f, str) and f.isidentifier() for f in fields):
            raise ValueError('投影字段无效')
        columns = ', '.join(f"'{f}', payload -> '$.{f}'" for f in fields)
        return await self._execute(f'SELECT json_object({columns}) FROM extension_records WHERE bot=? AND collection=? ORDER BY rowid', (bot, collection), True)

    async def get(self, bot, collection, key):
        rows = await self._execute('SELECT payload FROM extension_records WHERE bot=? AND collection=? AND id=?', (bot, collection, key), True)
        return rows[0] if rows else None

    async def put(self, bot, collection, key, record, base=None):
        """base: the account as the caller read it; credentials changed since then (e.g. a
        committed password task) are never overwritten by a stale read-modify-write."""
        if collection == 'accounts':

            def save(conn):
                row = conn.execute('SELECT payload FROM extension_records WHERE bot=? AND collection=? AND id=?', (bot, collection, key)).fetchone()
                old = json.loads(row[0]) if row else {}
                if row and base is not None and any(old.get(field) != base.get(field) for field in CREDENTIALS):
                    raise AccountLockedError('账号凭据刚刚变化，请刷新后重试')
                # Scope/enablement edits stay possible; credentials belong to the open intent.
                if row and any(old.get(field) != record.get(field) for field in CREDENTIALS):
                    self._guard_account(conn, bot, key)
                conn.execute(
                    'INSERT INTO extension_records VALUES (?,?,?,?) ON CONFLICT(bot,collection,id) DO UPDATE SET payload=excluded.payload',
                    (bot, collection, key, json.dumps(record, ensure_ascii=False)),
                )

            return await self.atomic(save)
        await self._execute(
            'INSERT INTO extension_records VALUES (?,?,?,?) ON CONFLICT(bot,collection,id) DO UPDATE SET payload=excluded.payload',
            (bot, collection, key, json.dumps(record, ensure_ascii=False)),
        )

    @staticmethod
    def _guard_account(conn, bot, key):
        if (
            conn.execute("SELECT 1 FROM sqlite_master WHERE name='ninja3_password_tasks'").fetchone()
            and conn.execute('SELECT 1 FROM ninja3_password_tasks WHERE bot=? AND account=? AND active=1', (bot, key)).fetchone()
        ):
            raise AccountLockedError('账号改密任务未解决，禁止修改登录凭据或删除')

    async def delete(self, bot, collection, key):
        if collection == 'accounts':

            def remove(conn):
                self._guard_account(conn, bot, key)
                conn.execute('DELETE FROM extension_records WHERE bot=? AND collection=? AND id=?', (bot, collection, key))

            return await self.atomic(remove)
        await self._execute('DELETE FROM extension_records WHERE bot=? AND collection=? AND id=?', (bot, collection, key))

    def close(self):
        with self._lock:
            self._closed = True
            if self._connection is not None:
                self._connection.close()
                self._connection = None
