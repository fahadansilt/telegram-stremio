from urllib.parse import quote

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from tests.fakes import FakeTelegram, record


async def test_manifest_catalog_meta_and_imdb_streams(service):
    manifest = (await service.http.get("/manifest.json")).json()
    assert manifest["catalogs"][0]["id"] == "telegram"
    assert manifest["resources"][2]["idPrefixes"] == ["tg:", "tt"]
    catalog = (await service.http.get("/catalog/movie/telegram.json")).json()
    item = catalog["metas"][0]
    assert item["id"] == "tg:-1001234567890:456"
    assert item["poster"] == "https://example.com/poster.jpg"
    meta = (await service.http.get(f"/meta/movie/{item['id']}.json")).json()["meta"]
    assert meta["behaviorHints"]["defaultVideoId"] == item["id"]
    for item_id in (item["id"], "tt0133093"):
        streams = (await service.http.get(f"/stream/movie/{item_id}.json")).json()["streams"]
        assert len(streams) == 1
        assert streams[0]["url"] == "http://localhost:8000/video/-1001234567890/456"
        assert streams[0]["behaviorHints"]["notWebReady"] is True


@pytest.mark.parametrize("item_id", ["tg:-1001234567890:40", "tt37963237"])
async def test_stream_description_alias_is_not_duplicated(service, item_id):
    for message_id, filename in (
        (40, "[CK] - Varavu (2026) Malayalam HQ HDRip - 1080p - x2.mkv"),
        (38, "[CK] - Varavu (2026) HQ HDRip - 1080p - x264.mkv"),
    ):
        await service.db.upsert(record(
            message_id=message_id, filename=filename, title="Varavu", year=2026,
            imdb_id="tt37963237", metadata='{"name":"Varavu","releaseInfo":"2026"}',
        ))
    response = await service.http.get(f"/stream/movie/{item_id}.json")
    assert response.status_code == 200
    streams = response.json()["streams"]
    assert len(streams) == 2
    for stream in streams:
        # Stremio's Stream.description uses serde(alias = "title"): providing
        # both keys is a duplicate-field error and rejects the resource response.
        assert len({"title", "description"}.intersection(stream)) == 1
        assert stream.get("title", stream.get("description"))
        assert stream["url"].startswith("http://localhost:8000/video/")


async def test_catalog_freshness_after_initial_empty_index(service):
    await service.db.execute("DELETE FROM files")
    response = await service.http.get("/catalog/movie/telegram.json")
    assert response.json() == {"metas": []}
    assert response.headers["cache-control"] == "no-store, max-age=0"
    filename = "[CK] - Varavu (2026) Malayalam HQ HDRip - 1080p - x2.mkv"
    await service.db.upsert(record(
        filename=filename, title="Varavu", year=2026, imdb_id="tt37963237",
        metadata='{"name":"Varavu","releaseInfo":"2026"}',
    ))
    for path in (
        "/catalog/movie/telegram.json", "/catalog/movie/telegram/search=Varavu.json"
    ):
        response = await service.http.get(path)
        assert response.json()["metas"][0]["name"] == "Varavu"
        assert response.headers["cache-control"] == "no-store, max-age=0"
    for path in (
        "/manifest.json", "/meta/movie/tg:-1001234567890:456.json",
        "/stream/movie/tt37963237.json",
    ):
        response = await service.http.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store, max-age=0"
    streams = response.json()["streams"]
    assert filename in streams[0]["title"]


async def test_unknown_types_and_ids(service):
    assert (await service.http.get("/stream/movie/tt9999999.json")).json() == {"streams": []}
    assert (await service.http.get("/stream/series/tt0133093:1:1.json")).json() == {"streams": []}
    assert (await service.http.get("/meta/movie/tg:-1001234567890:999.json")).status_code == 404
    assert (await service.http.get("/catalog/movie/other.json")).json() == {"metas": []}


@pytest.mark.parametrize("title", ["The Matrix", "AT&T", "100% Fun", "A+B", "A/B", "A=B", "Amélie"])
async def test_encoded_catalog_search(service, title):
    await service.db.upsert(record(title=title, filename=title + ".mkv"))
    extra = f"search={quote(title, safe='')}&skip=0"
    response = await service.http.get(f"/catalog/movie/telegram/{extra}.json")
    assert response.status_code == 200
    assert len(response.json()["metas"]) == 1


async def test_pagination_and_literal_search_wildcards(service):
    assert (await service.http.get("/catalog/movie/telegram/skip=100.json")).json() == {"metas": []}
    assert (await service.http.get("/catalog/movie/telegram/skip=-1.json")).status_code == 400
    response = await service.http.get("/catalog/movie/telegram/search=%25.json")
    assert response.json() == {"metas": []}


async def test_hidden_sources_filtered_and_variants_sorted(service):
    await service.db.upsert(record(chat_id=-1009999999999))
    await service.db.upsert(record(message_id=457, quality="2160p", size=10000))
    catalog = (await service.http.get("/catalog/movie/telegram.json")).json()
    assert len(catalog["metas"]) == 2
    streams = (await service.http.get("/stream/movie/tt0133093.json")).json()["streams"]
    assert len(streams) == 2
    assert streams[0]["url"].endswith("/457")


async def test_optional_secret_prefix_protects_addon_and_video(tmp_path):
    settings = Settings(
        _env_file=None, telegram_api_id=123, telegram_api_hash="fake", data_dir=tmp_path,
        metadata_lookup=False, addon_token="test_shared_secret", public_base_url="https://stream.example.com",
    )
    app = create_app(settings, FakeTelegram)
    async with app.router.lifespan_context(app):
        await app.state.db.upsert(record())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            for path in ("/manifest.json", "/video/-1001234567890/456", "/health"):
                assert (await http.get(path)).status_code == 404
            assert (await http.get("/wrong_secret/manifest.json")).status_code == 404
            assert (await http.get("/test_shared_secret/manifest.json")).status_code == 200
            response = await http.get("/test_shared_secret/stream/movie/tt0133093.json")
            streams = response.json()["streams"]
            assert streams[0]["url"] == "https://stream.example.com/test_shared_secret/video/-1001234567890/456"
            response = await http.options("/test_shared_secret/video/-1001234567890/456", headers={
                "Origin": "https://web.stremio.com", "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "range,if-range",
            })
            assert response.status_code == 200
            assert response.headers["access-control-allow-origin"] == "*"
