from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.metadata import Metadata
from app.telegram import Telegram
from tests.fakes import record


async def seed_episode(service, message_id=10, season=1, episode=1, **kwargs):
    value = record(
        message_id=message_id, title="Game of Thrones", year=2011,
        filename=f"Game.of.Thrones.S{season:02d}E{episode:02d}.1080p.mkv",
        imdb_id="tt0944947", metadata='{"name":"Game of Thrones","releaseInfo":"2011-2019"}',
        media_type="series", season=season, episode=episode,
    )
    value.update(kwargs)
    await service.db.upsert(value)


async def test_manifest_advertises_movie_and_series_routes(service):
    manifest = (await service.http.get("/manifest.json")).json()
    assert manifest["version"] == "1.1.0"
    assert manifest["types"] == ["movie", "series"]
    assert {catalog["type"] for catalog in manifest["catalogs"]} == {"movie", "series"}
    for resource in manifest["resources"][1:]:
        assert "series" in resource["types"]
        assert "tgseries:" in resource["idPrefixes"]


async def test_series_catalog_groups_files_and_metadata_lists_unique_episodes(service):
    await seed_episode(service)
    await seed_episode(service, message_id=11, episode=2)
    await seed_episode(service, message_id=12, episode=1, quality="720p")
    await seed_episode(service, message_id=13, season=2)
    await seed_episode(service, message_id=14, season=0)
    catalog = (await service.http.get("/catalog/series/telegram.json")).json()["metas"]
    assert len(catalog) == 1
    assert catalog[0]["id"] == "tgseries:tt0944947"
    assert catalog[0]["type"] == "series"
    assert "videos" not in catalog[0]
    movies = (await service.http.get("/catalog/movie/telegram.json")).json()["metas"]
    assert len(movies) == 1
    assert movies[0]["name"] == "The Matrix"
    response = await service.http.get("/meta/series/tgseries:tt0944947.json")
    assert response.status_code == 200
    meta = response.json()["meta"]
    assert "defaultVideoId" not in meta["behaviorHints"]
    assert "season" not in meta and "episode" not in meta
    videos = meta["videos"]
    assert len(videos) == 4
    assert {(video["season"], video["episode"]) for video in videos} == {
        (0, 1), (1, 1), (1, 2), (2, 1)
    }
    for video in videos:
        streams = (await service.http.get(f"/stream/series/{video['id']}.json")).json()["streams"]
        assert streams
        assert all("bingeGroup" in stream["behaviorHints"] for stream in streams)
        assert all(len({"title", "description"}.intersection(stream)) == 1 for stream in streams)


@pytest.mark.parametrize("item_id", ["tt0944947:1:1", "tgseries:tt0944947:1:1"])
async def test_episode_streams_do_not_mix_episodes_types_or_unconfigured_chats(service, item_id):
    await seed_episode(service)
    await seed_episode(service, message_id=11, episode=2)
    await seed_episode(service, message_id=12, quality="2160p", size=100)
    await seed_episode(service, message_id=13, season=2)
    await seed_episode(service, message_id=14, chat_id=-1009999999999)
    await service.db.upsert(record(message_id=15, imdb_id="tt0944947", media_type="movie"))
    streams = (await service.http.get(f"/stream/series/{item_id}.json")).json()["streams"]
    assert len(streams) == 2
    assert streams[0]["url"].endswith("/12")
    assert {stream["url"].rsplit("/", 1)[1] for stream in streams} == {"10", "12"}
    assert (await service.http.get("/stream/series/tt0944947:1:99.json")).json() == {"streams": []}
    response = await service.http.get("/stream/series/tgseries:tt0944947.json")
    assert response.json() == {"streams": []}
    assert (await service.http.get("/stream/movie/tt0944947:1:1.json")).json() == {"streams": []}
    movies = (await service.http.get("/stream/movie/tt0944947.json")).json()["streams"]
    assert len(movies) == 1 and movies[0]["url"].endswith("/15")


async def test_unmatched_series_grouping_search_and_pagination(service):
    await seed_episode(service, imdb_id=None, metadata="{}", title="Unknown Show")
    await seed_episode(service, message_id=11, episode=2, imdb_id=None,
                       metadata="{}", title="Unknown.Show")
    await seed_episode(service, message_id=12, imdb_id=None, metadata="{}", title="Another Show")
    items = (await service.http.get("/catalog/series/telegram.json")).json()["metas"]
    assert len(items) == 2
    found = (await service.http.get(
        "/catalog/series/telegram/search=Unknown.json"
    )).json()["metas"]
    assert len(found) == 1
    meta = (await service.http.get(f"/meta/series/{found[0]['id']}.json")).json()["meta"]
    assert len(meta["videos"]) == 2
    page = await service.http.get("/catalog/series/telegram/skip=1.json")
    assert len(page.json()["metas"]) == 1
    assert (await service.http.get("/catalog/series/telegram/skip=2.json")).json() == {"metas": []}


async def test_series_metadata_excludes_unconfigured_sources(service):
    await seed_episode(service)
    await seed_episode(service, message_id=11, episode=2, chat_id=-1009999999999)
    meta = (await service.http.get("/meta/series/tgseries:tt0944947.json")).json()["meta"]
    assert len(meta["videos"]) == 1
    assert (await service.http.get("/meta/movie/tgseries:tt0944947.json")).status_code == 404
    assert (await service.http.get("/meta/series/tg:-1001234567890:456.json")).status_code == 404


async def test_episode_indexing_and_manual_override_survive_reindex(service):
    metadata = Metadata(service.db, enabled=False)
    indexer = object.__new__(Telegram)
    indexer.db = service.db
    indexer.metadata = metadata
    message = SimpleNamespace(
        id=20, document=SimpleNamespace(id=200, size=1024, mime_type="video/x-matroska"),
        file=SimpleNamespace(name="Unknown.Show.S00E02.1080p.mkv", ext=".mkv"),
        raw_text="", date=datetime.now(UTC),
    )
    try:
        await indexer.index_message(-1001234567890, message)
        row = await service.db.get_file(-1001234567890, 20)
        assert row["media_type"] == "series"
        assert (row["season"], row["episode"]) == (0, 2)
        assert row["imdb_id"] is None
        await service.db.set_override(-1001234567890, 20, "tt0944947", {})
        await indexer.index_message(-1001234567890, message)
        row = await service.db.get_file(-1001234567890, 20)
        assert row["series_key"] == row["imdb_id"] == "tt0944947"
        response = await service.http.get("/stream/series/tt0944947:0:2.json")
        assert len(response.json()["streams"]) == 1
        video = await service.http.get("/video/-1001234567890/20", headers={"Range": "bytes=0-99"})
        assert video.status_code == 206 and len(video.content) == 100
    finally:
        await metadata.close()


async def test_cli_mapping_fetches_series_metadata(service, monkeypatch):
    from app import cli

    await seed_episode(service, imdb_id=None, metadata="{}")
    calls = []

    class MappingMetadata:
        def __init__(self, db, enabled):
            pass

        async def get(self, imdb_id, media_type):
            calls.append((imdb_id, media_type))
            return {"name": "Game of Thrones"}

        async def close(self):
            pass

    monkeypatch.setattr(cli, "Settings", lambda: service.settings)
    monkeypatch.setattr(cli, "Metadata", MappingMetadata)
    await cli.run(SimpleNamespace(
        command="map", chat_id=-1001234567890, message_id=10, imdb_id="tt0944947"
    ))
    assert calls == [("tt0944947", "series")]
    row = await service.db.get_file(-1001234567890, 10)
    assert row["series_key"] == "tt0944947"
    assert (row["season"], row["episode"]) == (1, 1)
