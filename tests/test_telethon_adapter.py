from datetime import UTC, datetime

from telethon import TelegramClient, types
from telethon.sessions import MemorySession

from app.reader import CHUNK_SIZE, read_document
from tests.fakes import data_at


async def test_pinned_telethon_iterator_obeys_random_access_constraints(monkeypatch):
    session = MemorySession()
    session.set_dc(2, "149.154.167.51", 443)
    client = TelegramClient(session, 123, "fake")
    calls = []

    async def get_file(sender, request):
        assert sender is client._sender
        assert request.offset % 4096 == 0
        assert request.limit == CHUNK_SIZE
        assert request.offset // 1048576 == (request.offset + request.limit - 1) // 1048576
        calls.append(request.offset)
        return types.upload.File(types.storage.FileUnknown(), 0,
                                 data_at(request.offset, request.limit))

    monkeypatch.setattr(client, "_call", get_file)
    document = types.Document(
        id=42, access_hash=123, file_reference=b"ref", date=datetime.now(UTC),
        mime_type="video/x-matroska", size=2147483648, dc_id=2, attributes=[],
    )
    start = 734003207
    end = start + CHUNK_SIZE + 20
    data = b"".join([
        chunk async for chunk in read_document(client, document, None, start, end, 10)
    ])
    assert data == data_at(start, end - start + 1)
    aligned = start - start % CHUNK_SIZE
    assert calls == [aligned, aligned + CHUNK_SIZE]
