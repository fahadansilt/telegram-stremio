import asyncio

import pytest
from starlette.requests import ClientDisconnect
from telethon import errors

from app.reader import CHUNK_SIZE, read_document
from app.streamer import InvalidRange, OwnedStreamingResponse, parse_range
from tests.fakes import data_at

PATH = "/video/-1001234567890/456"


@pytest.mark.parametrize("header,start,end,partial", [
    (None, 0, 99, False), ("bytes=0-0", 0, 0, True), ("bytes=11-23", 11, 23, True),
    ("bytes=73-", 73, 99, True), ("bytes=-20", 80, 99, True),
    ("bytes=-200", 0, 99, True), ("bytes=91-200", 91, 99, True),
    ("bytes=0-9,20-29", 0, 99, False), ("items=1-2", 0, 99, False),
])
def test_range_parser(header, start, end, partial):
    assert parse_range(header, 100) == (start, end, partial)


@pytest.mark.parametrize("header", [
    "bytes=100-", "bytes=70-20", "bytes=-0", "bytes=-", "bytes=abc-def",
    "bytes=0-1junk", "bytes=+1-2", "bytes=1--2", "bytes=" + "9" * 5000 + "-",
])
def test_invalid_ranges(header):
    with pytest.raises(InvalidRange):
        parse_range(header, 100)


@pytest.mark.parametrize("header", [
    "bytes=0-0", "bytes=3-213", "bytes=524281-524299", "bytes=-137", "bytes=1048600-",
])
async def test_http_ranges_exact_bytes(service, header):
    row = await service.db.get_file(-1001234567890, 456)
    start, end, _ = parse_range(header, row["size"])
    response = await service.http.get(PATH, headers={"Range": header, "Origin": "https://web.stremio.com"})
    assert response.status_code == 206
    assert response.content == data_at(start, end - start + 1)
    assert response.headers["content-range"] == f"bytes {start}-{end}/{row['size']}"
    assert int(response.headers["content-length"]) == len(response.content)
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-type"] == "video/x-matroska"
    assert response.headers["access-control-allow-origin"] == "*"
    assert service.telegram.client.reads[0] == start - start % CHUNK_SIZE
    assert all(item.closed for item in service.telegram.client.iterators)
    assert service.app.state.stream_slots._value == service.settings.max_streams


async def test_large_file_seeks_without_reading_preceding_bytes(service):
    await service.db.execute("UPDATE files SET size=2147483648")
    start, end = 734003207, 734004230
    response = await service.http.get(PATH, headers={"Range": f"bytes={start}-{end}"})
    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes {start}-{end}/2147483648"
    assert response.content == data_at(start, 1024)
    assert service.telegram.client.reads == [start - start % CHUNK_SIZE]


async def test_full_get_and_multipart_fallback(service):
    size = (await service.db.get_file(-1001234567890, 456))["size"]
    for header in ({}, {"Range": "bytes=0-1,10-11"}):
        response = await service.http.get(PATH, headers=header)
        assert response.status_code == 200
        assert "content-range" not in response.headers
        assert response.content == data_at(0, size)
        assert int(response.headers["content-length"]) == size


async def test_head_ignores_range_and_does_not_download(service):
    response = await service.http.head(PATH, headers={"Range": "bytes=100-"})
    assert response.status_code == 200
    assert response.content == b""
    assert int(response.headers["content-length"]) == CHUNK_SIZE * 2 + 137
    assert "content-range" not in response.headers
    assert not service.telegram.client.reads


async def test_416_no_chunks_and_exposes_size(service):
    response = await service.http.get(PATH, headers={"Range": "bytes=999999999-"})
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{CHUNK_SIZE * 2 + 137}"
    assert not service.telegram.client.reads


async def test_if_range(service):
    head = await service.http.head(PATH)
    for validator in (head.headers["etag"], head.headers["last-modified"]):
        response = await service.http.get(
            PATH, headers={"Range": "bytes=0-9", "If-Range": validator}
        )
        assert response.status_code == 206
        assert len(response.content) == 10
    response = await service.http.get(
        PATH, headers={"Range": "bytes=0-9", "If-Range": '"different"'}
    )
    assert response.status_code == 200
    assert len(response.content) == CHUNK_SIZE * 2 + 137


@pytest.mark.parametrize("failure,status", [
    (OSError("connection failed"), 502), (errors.FloodWaitError(None, capture=7), 429),
])
async def test_failure_before_headers_releases_resources(service, failure, status):
    service.telegram.client.failure = failure
    response = await service.http.get(PATH)
    assert response.status_code == status
    if status == 429:
        assert response.headers["retry-after"] == "7"
    assert service.telegram.client.iterators[0].closed
    assert service.app.state.stream_slots._value == service.settings.max_streams


async def test_expired_reference_resumes_after_delivered_bytes(service):
    service.telegram.client.expire_at = CHUNK_SIZE
    response = await service.http.get(PATH)
    assert response.status_code == 200
    assert response.content == data_at(0, CHUNK_SIZE * 2 + 137)
    assert service.telegram.message_reads == 2
    assert service.telegram.client.reads == [0, CHUNK_SIZE, CHUNK_SIZE * 2]
    assert all(iterator.closed for iterator in service.telegram.client.iterators)


async def test_changed_and_deleted_documents(service):
    service.telegram.changed = True
    assert (await service.http.get(PATH)).status_code == 409
    service.telegram.changed = False
    service.telegram.deleted = True
    assert (await service.http.get(PATH)).status_code == 404
    assert await service.db.get_file(-1001234567890, 456) is None
    assert not service.telegram.client.reads


async def test_unconfigured_chat_cannot_be_streamed(service):
    from tests.fakes import record

    await service.db.upsert(record(chat_id=-1009999999999))
    assert (await service.http.get("/video/-1009999999999/456")).status_code == 404


async def test_stream_slots_limit(service):
    semaphore = service.app.state.stream_slots
    for _ in range(service.settings.max_streams):
        await semaphore.acquire()
    response = await service.http.get(PATH)
    assert response.status_code == 503
    assert not service.telegram.client.reads


async def test_client_send_failure_closes_preopened_reader(service):
    message = await service.telegram.get_message(-1001234567890, 456)
    reader = read_document(service.telegram.client, message.document, None, 0, 100, 10)
    first = await anext(reader)
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()

    async def body():
        yield first

    response = OwnedStreamingResponse(body(), reader, semaphore)

    async def send(message):
        raise OSError("client closed connection")

    async def receive():
        await asyncio.sleep(100)

    with pytest.raises(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert semaphore._value == 1
    assert service.telegram.client.iterators[0].closed


async def test_client_disconnect_closes_reader_with_anyio_cancellation(service):
    message = await service.telegram.get_message(-1001234567890, 456)
    reader = read_document(service.telegram.client, message.document, None, 0, CHUNK_SIZE * 2, 10)
    first = await anext(reader)
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()
    sent_body = asyncio.Event()

    async def body():
        yield first
        await asyncio.sleep(100)

    async def send(message):
        if message["type"] == "http.response.body":
            sent_body.set()

    async def receive():
        await sent_body.wait()
        return {"type": "http.disconnect"}

    response = OwnedStreamingResponse(body(), reader, semaphore)
    await response({"type": "http", "asgi": {"spec_version": "2.0"}}, receive, send)
    assert semaphore._value == 1
    assert service.telegram.client.iterators[0].closed
