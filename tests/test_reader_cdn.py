import hashlib
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from telethon import errors, functions, types
from telethon.client.downloads import _CdnRedirect

from app.reader import CHUNK_SIZE, read_document
from tests.fakes import FakeClient, data_at


class RedirectIterator:
    def __init__(self, client):
        self.client = client
        self.closed = False
        self._sender = None

    async def __anext__(self):
        self._sender = self.client.origin_sender
        raise _CdnRedirect(self.client.redirect)

    async def close(self):
        self.closed = True


class CdnClient:
    def __init__(self, owner):
        self.owner = owner
        self._sender = object()
        self.closed = False
        self.reads = []

    async def _call(self, sender, request):
        assert sender is self._sender
        assert isinstance(request, functions.upload.GetCdnFileRequest)
        assert request.offset % 4096 == 0
        assert request.limit % 4096 == 0
        assert 1024 * 1024 % request.limit == 0
        assert request.offset // (1024 * 1024) == (
            request.offset + request.limit - 1
        ) // (1024 * 1024)
        if self.owner.invalid_token:
            self.owner.invalid_token = False
            raise errors.BadRequestError(None, "FILE_TOKEN_INVALID")
        if self.owner.reupload:
            self.owner.reupload = False
            return types.upload.CdnFileReuploadNeeded(b"request-token")
        self.reads.append(request.offset)
        data = self.owner.encrypted[request.offset:request.offset + request.limit]
        if self.owner.corrupt:
            data = bytes([data[0] ^ 1]) + data[1:]
        return types.upload.CdnFile(data)

    async def disconnect(self):
        self.closed = True


class RedirectClient:
    def __init__(self, reupload=False, corrupt=False, invalid_token=False):
        self.origin_sender = object()
        self.calls = []
        self.iterators = []
        self.cdns = []
        self.reupload = reupload
        self.corrupt = corrupt
        self.invalid_token = invalid_token
        self.size = CHUNK_SIZE * 2 + 137
        key, iv = b"k" * 32, b"i" * 12 + b"\x00" * 4
        self.hashes = [types.FileHash(
            offset, min(CHUNK_SIZE, self.size - offset),
            hashlib.sha256(data_at(offset, min(CHUNK_SIZE, self.size - offset))).digest(),
        ) for offset in range(0, self.size, CHUNK_SIZE)]
        self.redirect = types.upload.FileCdnRedirect(5, b"file-token", key, iv, self.hashes[:1])
        encryptor = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
        self.encrypted = encryptor.update(data_at(0, self.size)) + encryptor.finalize()

    def iter_download(self, document, **kwargs):
        value = RedirectIterator(self)
        self.iterators.append(value)
        return value

    async def _get_cdn_client(self, redirect):
        value = CdnClient(self)
        self.cdns.append(value)
        return value

    async def _call(self, sender, request):
        assert sender is self.origin_sender
        self.calls.append(request)
        if isinstance(request, functions.upload.GetCdnFileHashesRequest):
            return [item for item in self.hashes
                    if item.offset <= request.offset < item.offset + item.limit]
        assert isinstance(request, functions.upload.ReuploadCdnFileRequest)
        return self.hashes


@pytest.mark.parametrize("reupload,invalid_token", [(False, False), (True, False), (False, True)])
async def test_cdn_seek_decryption_hashes_and_reupload(reupload, invalid_token):
    client = RedirectClient(reupload=reupload, invalid_token=invalid_token)
    document = SimpleNamespace(id=42, size=client.size)
    start, end = CHUNK_SIZE + 7, CHUNK_SIZE * 2 + 99
    data = b"".join([
        chunk async for chunk in read_document(client, document, None, start, end, 10)
    ])
    assert data == data_at(start, end - start + 1)
    assert all(iterator.closed for iterator in client.iterators)
    assert all(cdn.closed for cdn in client.cdns)
    assert client.cdns[-1].reads == [CHUNK_SIZE, CHUNK_SIZE * 2]
    if reupload:
        assert any(
            isinstance(call, functions.upload.ReuploadCdnFileRequest) for call in client.calls
        )


async def test_corrupt_cdn_bytes_are_never_delivered():
    client = RedirectClient(corrupt=True)
    reader = read_document(client, SimpleNamespace(id=42, size=client.size), None, 0, 100, 10)
    with pytest.raises(OSError, match="integrity"):
        await anext(reader)
    assert all(cdn.closed for cdn in client.cdns)
    assert all(iterator.closed for iterator in client.iterators)


async def test_cdn_reader_is_closed_immediately_on_early_stop():
    client = RedirectClient()
    reader = read_document(
        client, SimpleNamespace(id=42, size=client.size), None, 0, client.size - 1, 10
    )
    await anext(reader)
    await reader.aclose()
    assert client.cdns[0].closed
    assert client.iterators[0].closed


async def test_reference_refresh_rejects_changed_document():
    client = FakeClient()
    client.expire_at = 0

    async def refresh():
        return SimpleNamespace(document=SimpleNamespace(id=43, size=CHUNK_SIZE))

    reader = read_document(client, SimpleNamespace(id=42, size=CHUNK_SIZE), refresh, 0, 100, 10)
    with pytest.raises(OSError, match="changed"):
        await anext(reader)
    assert all(iterator.closed for iterator in client.iterators)
