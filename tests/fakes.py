from datetime import UTC, datetime
from types import SimpleNamespace

from telethon import errors

from app.reader import CHUNK_SIZE


def data_at(offset: int, length: int) -> bytes:
    pattern = bytes(range(251))
    start = offset % 251
    return (pattern * ((length + start + 250) // 251))[start:start + length]


def record(chat_id=-1001234567890, message_id=456, **kwargs):
    value = {
        "chat_id": chat_id, "message_id": message_id, "document_id": 42,
        "filename": "The.Matrix.1999.1080p.H264.mkv", "size": CHUNK_SIZE * 2 + 137,
        "mime_type": "video/x-matroska", "title": "The Matrix", "year": 1999,
        "quality": "1080p • H.264", "caption": "The Matrix tt0133093", "imdb_id": "tt0133093",
        "metadata": '{"poster":"https://example.com/poster.jpg","name":"The Matrix"}',
        "posted_at": "2026-01-01T00:00:00+00:00",
    }
    value.update(kwargs)
    return value


class FakeIterator:
    def __init__(self, client, document, offset):
        self.client = client
        self.document = document
        self.offset = offset
        self.closed = False
        self._sender = None

    async def __anext__(self):
        self._sender = self.client.sender
        if self.client.failure:
            raise self.client.failure
        if self.client.expire_at == self.offset:
            self.client.expire_at = None
            raise errors.FileReferenceExpiredError(None)
        if self.offset >= self.document.size:
            raise StopAsyncIteration
        self.client.reads.append(self.offset)
        chunk = data_at(self.offset, min(CHUNK_SIZE, self.document.size - self.offset))
        self.offset += len(chunk)
        return memoryview(chunk)

    async def close(self):
        self.closed = True
        self._sender = None


class FakeClient:
    def __init__(self):
        self.iterators = []
        self.reads = []
        self.failure = None
        self.expire_at = None
        self.sender = object()

    def is_connected(self):
        return True

    def iter_download(self, document, **kwargs):
        iterator = FakeIterator(self, document, kwargs["offset"])
        self.iterators.append(iterator)
        return iterator


class FakeTelegram:
    def __init__(self, settings, db, metadata):
        self.db = db
        self.client = FakeClient()
        self.allowed_chats = [-1001234567890]
        self.last_sync = None
        self.sync_error = None
        self.message_reads = 0
        self.changed = False
        self.deleted = False

    async def connect(self):
        pass

    def start(self):
        pass

    async def close(self):
        pass

    async def get_message(self, chat_id, message_id):
        from fastapi import HTTPException

        self.message_reads += 1
        if self.deleted:
            await self.db.delete(chat_id, message_id)
            raise HTTPException(404, "Deleted")
        value = await self.db.get_file(chat_id, message_id)
        return SimpleNamespace(
            document=SimpleNamespace(id=value["document_id"] + self.changed, size=value["size"]),
            date=datetime(2026, 1, 1, tzinfo=UTC),
        )
