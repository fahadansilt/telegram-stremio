import asyncio
import json
import time
from pathlib import Path

import aiosqlite

from app.media import episode_number, series_key

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
    media_type TEXT NOT NULL DEFAULT 'movie',
    season INTEGER,
    episode INTEGER,
    series_key TEXT,
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
        await self.migrate()
        await self.connection.commit()

    async def migrate(self):
        async with self.connection.execute("PRAGMA table_info(files)") as cursor:
            columns = {row["name"] for row in await cursor.fetchall()}
        for name, definition in {
            "media_type": "TEXT NOT NULL DEFAULT 'movie'", "season": "INTEGER",
            "episode": "INTEGER", "series_key": "TEXT",
        }.items():
            if name not in columns:
                await self.connection.execute(f"ALTER TABLE files ADD COLUMN {name} {definition}")
        async with self.connection.execute("PRAGMA user_version") as cursor:
            version = (await cursor.fetchone())[0]
        if version < 2:
            # Preserve episode metadata written by the earlier partial series
            # implementation, without needing SQLite's optional JSON extension.
            async with self.connection.execute(
                "SELECT chat_id,message_id,title,year,imdb_id,metadata FROM files"
            ) as cursor:
                async for row in cursor:
                    try:
                        meta = json.loads(row["metadata"])
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(meta, dict) or meta.get("type") != "series":
                        continue
                    season = meta.get("season", 1)
                    episode = meta.get("episode")
                    valid = episode_number(season) and episode_number(episode)
                    await self.connection.execute(
                        "UPDATE files SET media_type='series',season=?,episode=?,series_key=? "
                        "WHERE chat_id=? AND message_id=?",
                        (season if valid else None, episode if valid else None,
                         series_key(row["title"], row["year"], row["imdb_id"]),
                         row["chat_id"], row["message_id"]),
                    )
            await self.connection.execute("PRAGMA user_version=2")
        await self.connection.execute(
            "CREATE INDEX IF NOT EXISTS files_series ON files(series_key,season,episode)"
        )

    async def close(self):
        await self.connection.close()

    async def execute(self, sql: str, params=()):
        async with self.lock:
            async with self.connection.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
            await self.connection.commit()
            return [dict(row) for row in rows]

    async def upsert(self, record: dict):
        record = dict(record)
        record.setdefault("media_type", "movie")
        record.setdefault("season", None)
        record.setdefault("episode", None)
        record["series_key"] = (
            series_key(record["title"], record["year"], record["imdb_id"])
            if record["media_type"] == "series" else None
        )
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
        await self.execute(
            "UPDATE files SET series_key=? WHERE chat_id=? AND message_id=? "
            "AND media_type='series'",
            (imdb_id, chat_id, message_id),
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

    async def catalog(
        self, allowed: list[int], search: str, skip: int, media_type: str = "movie"
    ) -> list[dict]:
        if not allowed:
            return []
        pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = (
            f"SELECT * FROM files WHERE chat_id IN ({','.join('?' for _ in allowed)}) "
            "AND media_type=? "
            "AND (title LIKE ? ESCAPE '\\' OR filename LIKE ? ESCAPE '\\') "
        )
        if media_type == "series":
            query = (
                "SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY series_key "
                "ORDER BY posted_at DESC,chat_id,message_id DESC) AS group_rank FROM ("
                + query + " AND season IS NOT NULL AND episode IS NOT NULL)) WHERE group_rank=1 "
            )
        query += "ORDER BY posted_at DESC, chat_id, message_id DESC LIMIT 100 OFFSET ?"
        return await self.execute(query, (*allowed, media_type, pattern, pattern, skip))

    async def by_imdb(
        self, imdb_id: str, allowed: list[int], media_type: str = "movie",
        season: int | None = None, episode: int | None = None,
    ) -> list[dict]:
        if not allowed:
            return []
        query = (
            f"SELECT * FROM files WHERE imdb_id=? AND media_type=? AND chat_id IN "
            f"({','.join('?' for _ in allowed)})"
        )
        params = (imdb_id, media_type, *allowed)
        if season is not None and episode is not None:
            query += " AND season=? AND episode=?"
            params += (season, episode)
        return await self.execute(query + " ORDER BY size DESC", params)

    async def series_files(
        self, key: str, allowed: list[int],
        season: int | None = None, episode: int | None = None,
    ) -> list[dict]:
        if not allowed:
            return []
        query = (
            "SELECT * FROM files WHERE series_key=? AND media_type='series' "
            f"AND chat_id IN ({','.join('?' for _ in allowed)}) "
            "AND season IS NOT NULL AND episode IS NOT NULL"
        )
        params = (key, *allowed)
        if season is not None and episode is not None:
            query += " AND season=? AND episode=?"
            params += (season, episode)
        return await self.execute(
            query + " ORDER BY season,episode,size DESC,posted_at DESC", params
        )
