import asyncio
import json
import time
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    filename TEXT NOT NULL,
    size INTEGER NOT NULL,
    mime_type TEXT NOT NULL,
    title TEXT NOT NULL,
    year INTEGER,
    quality TEXT NOT NULL,
    caption TEXT NOT NULL,
    imdb_id TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    posted_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS files_imdb ON files(imdb_id);
CREATE INDEX IF NOT EXISTS files_date ON files(posted_at DESC);
CREATE TABLE IF NOT EXISTS sync_state (
    chat_id INTEGER PRIMARY KEY,
    last_message_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS overrides (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    imdb_id TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS metadata_cache (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    expires_at REAL NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()

    async def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute("PRAGMA journal_mode=WAL")
        await self.connection.execute("PRAGMA busy_timeout=5000")
        await self.connection.executescript(SCHEMA)
        await self.connection.commit()

    async def close(self):
        await self.connection.close()

    async def execute(self, sql: str, params=()):
        async with self.lock:
            async with self.connection.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
            await self.connection.commit()
            return [dict(row) for row in rows]

    async def upsert(self, record: dict):
        # The caller owns the column names, while all message content is parameterized.
        columns = list(record)
        updates = ", ".join(f"{key}=excluded.{key}" for key in columns)
        await self.execute(
            f"INSERT INTO files ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)}) "
            f"ON CONFLICT(chat_id,message_id) DO UPDATE SET {updates}",
            tuple(record.values()),
        )

    async def get_file(self, chat_id: int, message_id: int) -> dict | None:
        rows = await self.execute(
            "SELECT * FROM files WHERE chat_id=? AND message_id=?", (chat_id, message_id)
        )
        return rows[0] if rows else None

    async def delete(self, chat_id: int, message_id: int):
        await self.execute(
            "DELETE FROM files WHERE chat_id=? AND message_id=?", (chat_id, message_id)
        )

    async def cursor(self, chat_id: int) -> int | None:
        rows = await self.execute("SELECT * FROM sync_state WHERE chat_id=?", (chat_id,))
        return rows[0]["last_message_id"] if rows else None

    async def checkpoint(self, chat_id: int, message_id: int):
        await self.execute(
            "INSERT INTO sync_state VALUES (?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
            "last_message_id=MAX(last_message_id, excluded.last_message_id)",
            (chat_id, message_id),
        )

    async def override(self, chat_id: int, message_id: int) -> str | None:
        rows = await self.execute(
            "SELECT imdb_id FROM overrides WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        )
        return rows[0]["imdb_id"] if rows else None

    async def set_override(self, chat_id: int, message_id: int, imdb_id: str, meta: dict):
        await self.execute(
            "INSERT INTO overrides VALUES (?, ?, ?) ON CONFLICT(chat_id,message_id) "
            "DO UPDATE SET imdb_id=excluded.imdb_id", (chat_id, message_id, imdb_id)
        )
        await self.execute(
            "UPDATE files SET imdb_id=?, metadata=? WHERE chat_id=? AND message_id=?",
            (imdb_id, json.dumps(meta), chat_id, message_id),
        )

    async def cache_get(self, key: str) -> dict | None:
        rows = await self.execute(
            "SELECT value FROM metadata_cache WHERE key=? AND expires_at>?", (key, time.time())
        )
        return json.loads(rows[0]["value"]) if rows else None

    async def cache_set(self, key: str, value: dict, ttl: int = 86400):
        await self.execute(
            "INSERT INTO metadata_cache VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
            "value=excluded.value, expires_at=excluded.expires_at",
            (key, json.dumps(value), time.time() + ttl),
        )

    async def catalog(self, allowed: list[int], search: str, skip: int) -> list[dict]:
        if not allowed:
            return []
        pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        return await self.execute(
            f"SELECT * FROM files WHERE chat_id IN ({','.join('?' for _ in allowed)}) "
            "AND (title LIKE ? ESCAPE '\\' OR filename LIKE ? ESCAPE '\\') "
            "ORDER BY posted_at DESC, chat_id, message_id DESC LIMIT 100 OFFSET ?",
            (*allowed, pattern, pattern, skip),
        )

    async def by_imdb(self, imdb_id: str, allowed: list[int]) -> list[dict]:
        if not allowed:
            return []
        return await self.execute(
            f"SELECT * FROM files WHERE imdb_id=? AND chat_id IN "
            f"({','.join('?' for _ in allowed)}) ORDER BY size DESC", (imdb_id, *allowed)
        )
