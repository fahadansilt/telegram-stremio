import json

import aiosqlite

from app.database import Database
from tests.fakes import record


async def test_movie_database_migration_preserves_rows_and_partial_series_metadata(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    async with aiosqlite.connect(path) as connection:
        await connection.execute("""
            CREATE TABLE files (
                chat_id INTEGER, message_id INTEGER, document_id INTEGER, filename TEXT,
                size INTEGER, mime_type TEXT, title TEXT, year INTEGER, quality TEXT,
                caption TEXT, imdb_id TEXT, metadata TEXT, posted_at TEXT,
                PRIMARY KEY(chat_id,message_id)
            )
        """)
        rows = [record(), record(
            message_id=10, title="Unknown Show", imdb_id=None,
            metadata=json.dumps({"type": "series", "season": 0, "episode": 2}),
        )]
        for row in rows:
            columns = ",".join(row)
            values = ",".join("?" for _ in row)
            await connection.execute(f"INSERT INTO files ({columns}) VALUES ({values})",
                                     tuple(row.values()))
        await connection.commit()
    db = Database(path)
    await db.open()
    try:
        movie = await db.get_file(-1001234567890, 456)
        assert movie["media_type"] == "movie"
        assert movie["imdb_id"] == "tt0133093"
        assert movie["season"] is None and movie["series_key"] is None
        episode = await db.get_file(-1001234567890, 10)
        assert episode["media_type"] == "series"
        assert (episode["season"], episode["episode"]) == (0, 2)
        assert len(episode["series_key"]) == 32
        await db.set_override(-1001234567890, 10, "tt0944947", {})
        await db.checkpoint(-1001234567890, 50)
    finally:
        await db.close()
    await db.open()
    try:
        episode = await db.get_file(-1001234567890, 10)
        assert episode["series_key"] == "tt0944947"
        assert (episode["season"], episode["episode"]) == (0, 2)
        assert await db.cursor(-1001234567890) == 50
        assert await db.override(-1001234567890, 10) == "tt0944947"
    finally:
        await db.close()
