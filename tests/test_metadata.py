import httpx
import pytest

from app.metadata import Metadata, parse_filename


def test_filename_parsing_and_episode_exclusion():
    value = parse_filename("The.Matrix.1999.1080p.BluRay.x264.mkv", "https://imdb.com/title/tt0133093")
    assert value["title"] == "The Matrix"
    assert value["year"] == 1999
    assert value["imdb_id"] == "tt0133093"
    assert "1080p" in value["quality"]
    assert parse_filename("Example.Show.S01E02.1080p.mkv", "") is None
    assert parse_filename("Movie.2026.mkv", "tt0133093 tt0234215")["imdb_id"] is None


def test_varavu_release_filename_is_a_movie():
    filename = "[CK] - Varavu (2026) Malayalam HQ HDRip - 1080p - x2.mkv"
    value = parse_filename(filename, "")
    assert value is not None
    assert value["title"] == "Varavu"
    assert value["year"] == 2026
    assert value["quality"] == "1080p"


@pytest.mark.parametrize("candidates,expected", [
    ([{"id": "tt0133093", "name": "The Matrix", "releaseInfo": "1999"}], "tt0133093"),
    ([{"id": "tt0133093", "name": "The Matrix Reloaded", "releaseInfo": "1999"}], None),
    ([{"id": "tt0133093", "name": "The Matrix", "releaseInfo": "2003"}], None),
    ([{"id": "tt0133093", "name": "The Matrix", "releaseInfo": "1999"},
      {"id": "tt1234567", "name": "The Matrix", "releaseInfo": "1999"}], None),
])
async def test_matching_is_conservative_and_cached(service, candidates, expected):
    metadata = Metadata(service.db, enabled=True)
    await metadata.http.aclose()
    requests = []

    def handle(request):
        requests.append(request.url)
        value = {"metas": candidates} if "catalog" in request.url.path else {
            "meta": {"name": "The Matrix", "behaviorHints": {"defaultVideoId": "wrong"}}
        }
        return httpx.Response(200, json=value)

    metadata.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        parsed = parse_filename("The.Matrix.1999.1080p.mkv", "")
        imdb_id, meta = await metadata.match(parsed)
        assert imdb_id == expected
        assert "behaviorHints" not in meta
        count = len(requests)
        assert (await metadata.match(parsed))[0] == expected
        assert len(requests) == count
    finally:
        await metadata.close()


async def test_explicit_imdb_and_manual_override_work_without_network(service):
    metadata = Metadata(service.db, enabled=False)
    try:
        assert await metadata.match(parse_filename("Movie.mkv", "tt0133093")) == ("tt0133093", {})
        await service.db.set_override(-1001234567890, 456, "tt0234215", {})
        assert await service.db.override(-1001234567890, 456) == "tt0234215"
        assert (await service.db.get_file(-1001234567890, 456))["imdb_id"] == "tt0234215"
    finally:
        await metadata.close()


async def test_lookup_failure_keeps_local_movie_metadata(service):
    metadata = Metadata(service.db, enabled=True)
    await metadata.http.aclose()
    metadata.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    try:
        assert await metadata.match(parse_filename("The.Matrix.1999.mkv", "")) == (None, {})
    finally:
        await metadata.close()
