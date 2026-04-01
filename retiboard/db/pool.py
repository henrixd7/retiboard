from __future__ import annotations

import asyncio
import time
from typing import Optional

import aiosqlite

from retiboard.db.database import _SCHEMA_SQL, SCHEMA_VERSION, _migrate, board_chunk_cache_dir, board_db_path, board_payloads_dir


class BoardConnectionPool:
    def __init__(self) -> None:
        self._connections: dict[str, aiosqlite.Connection] = {}
        self._global_lock = asyncio.Lock()

    async def get(self, board_id: str) -> aiosqlite.Connection:
        conn = self._connections.get(board_id)
        if conn is not None:
            try:
                await conn.execute("SELECT 1")
                return conn
            except Exception:
                self._connections.pop(board_id, None)

        async with self._global_lock:
            conn = self._connections.get(board_id)
            if conn is not None:
                return conn
            conn = await self._create_connection(board_id)
            self._connections[board_id] = conn
            return conn

    async def _create_connection(self, board_id: str) -> aiosqlite.Connection:
        db_path = board_db_path(board_id)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        board_payloads_dir(board_id).mkdir(parents=True, exist_ok=True)
        board_chunk_cache_dir(board_id).mkdir(parents=True, exist_ok=True)

        db = await aiosqlite.connect(str(db_path))
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        db.row_factory = aiosqlite.Row
        await db.executescript(_SCHEMA_SQL)
        async with db.execute("SELECT COUNT(*) FROM schema_version") as cur:
            row = await cur.fetchone()
            if row[0] == 0:
                await db.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (SCHEMA_VERSION, int(time.time())),
                )
                await db.commit()
        await _migrate(db)
        return db

    async def release(self, board_id: str) -> None:
        async with self._global_lock:
            conn = self._connections.pop(board_id, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass

    async def close_all(self) -> None:
        async with self._global_lock:
            items = list(self._connections.items())
            self._connections.clear()
        for _, conn in items:
            try:
                await conn.close()
            except Exception:
                pass


_pool: Optional[BoardConnectionPool] = None


def get_pool() -> BoardConnectionPool:
    global _pool
    if _pool is None:
        _pool = BoardConnectionPool()
    return _pool


async def get_board_connection(board_id: str) -> aiosqlite.Connection:
    return await get_pool().get(board_id)
