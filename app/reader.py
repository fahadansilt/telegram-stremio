"""Bounded-memory random access to Telegram documents (Telethon 1.45.0 adapter)."""

import asyncio
import hashlib
import hmac
from collections.abc import AsyncIterator, Awaitable, Callable

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from telethon import errors, functions, types
from telethon.client.downloads import _CdnRedirect

CHUNK_SIZE = 512 * 1024


async def timed(awaitable, timeout: float):
    async with asyncio.timeout(timeout):
        return await awaitable


async def cdn_chunks(client, sender, redirect, start: int, end: int, timeout: float):
    """Decrypt and authenticate complete hash blocks before exposing any CDN bytes.

    Hash/reupload requests go to the document's origin sender, not the account's DC.
    Telethon's public iter_download does not implement this redirect path. The small
    private-API surface here is why Telethon is pinned in requirements.txt.
    """
    cdn = await timed(client._get_cdn_client(redirect), timeout)
    hashes = {item.offset: item for item in redirect.file_hashes}
    position = start
    try:
        while position <= end:
            block = next((item for item in hashes.values()
                          if item.offset <= position < item.offset + item.limit), None)
            if block is None:
                items = await timed(client._call(sender, functions.upload.GetCdnFileHashesRequest(
                    redirect.file_token, position
                )), timeout)
                hashes = {item.offset: item for item in items}
                block = next((item for item in hashes.values()
                              if item.offset <= position < item.offset + item.limit), None)
            if block is None or not (0 < block.limit <= 1024 * 1024) or block.offset % 4096:
                raise OSError("Telegram CDN supplied an invalid or missing hash block")
            plaintext = bytearray()
            offset = block.offset
            while offset < block.offset + block.limit:
                # Respect Telegram's 1 MiB boundary, 4 KiB alignment and size constraints.
                request_size = CHUNK_SIZE
                while offset % request_size:
                    request_size //= 2
                request = functions.upload.GetCdnFileRequest(
                    redirect.file_token, offset, request_size
                )
                result = await timed(cdn._call(cdn._sender, request), timeout)
                if isinstance(result, types.upload.CdnFileReuploadNeeded):
                    request_reupload = functions.upload.ReuploadCdnFileRequest(
                        redirect.file_token, result.request_token
                    )
                    items = await timed(client._call(sender, request_reupload), timeout)
                    hashes.update({item.offset: item for item in items})
                    result = await timed(cdn._call(cdn._sender, request), timeout)
                if not isinstance(result, types.upload.CdnFile) or not result.bytes:
                    raise OSError("Telegram CDN returned no file data")
                iv = redirect.encryption_iv[:12] + (offset // 16).to_bytes(4, "big")
                cipher = Cipher(algorithms.AES(redirect.encryption_key), modes.CTR(iv))
                decryptor = cipher.decryptor()
                data = decryptor.update(result.bytes) + decryptor.finalize()
                needed = min(len(data), block.offset + block.limit - offset)
                plaintext.extend(data[:needed])
                offset += needed
                if offset % 4096 and offset < block.offset + block.limit:
                    raise OSError("Telegram CDN returned a truncated block")
            if not hmac.compare_digest(hashlib.sha256(plaintext).digest(), block.hash):
                raise OSError("Telegram CDN integrity check failed")
            stop = min(block.offset + block.limit, end + 1)
            yield bytes(plaintext[position - block.offset:stop - block.offset])
            position = stop
            # Keep hash memory bounded during long downloads.
            hashes = {offset: item for offset, item in hashes.items() if offset >= position}
    finally:
        await cdn.disconnect()


async def read_document(
    client,
    document,
    refresh: Callable[[], Awaitable],
    start: int,
    end: int,
    timeout: float,
) -> AsyncIterator[bytes]:
    position = start
    recovery_attempts = 0
    while position <= end:
        aligned = position - position % CHUNK_SIZE
        iterator = client.iter_download(
            document, offset=aligned, request_size=CHUNK_SIZE,
            chunk_size=CHUNK_SIZE, file_size=document.size,
        )
        # Telethon initializes this lazily. A timeout during sender creation must
        # still be safe to close, without masking the original failure.
        iterator._sender = None
        try:
            offset = aligned
            while position <= end:
                try:
                    chunk = await timed(anext(iterator), timeout)
                except StopAsyncIteration as exc:
                    raise OSError("Telegram document ended before the requested range") from exc
                if not chunk:
                    raise OSError("Telegram returned an empty chunk")
                skip = max(0, position - offset)
                data = bytes(chunk[skip:skip + end - position + 1])
                offset += len(chunk)
                if data:
                    position += len(data)
                    yield data
        except _CdnRedirect as exc:
            cdn_reader = cdn_chunks(
                client, iterator._sender, exc.cdn_redirect, position, end, timeout
            )
            try:
                async for chunk in cdn_reader:
                    position += len(chunk)
                    yield chunk
            except errors.RPCError as rpc_exc:
                # These CDN errors aren't named classes in Telethon's error table.
                if rpc_exc.message not in {"FILE_TOKEN_INVALID", "REQUEST_TOKEN_INVALID"}:
                    raise
                recovery_attempts += 1
                if recovery_attempts > 3:
                    raise
                # Re-enter getFile at the current offset to obtain a new redirect/token.
            finally:
                await cdn_reader.aclose()
        except (errors.FileReferenceExpiredError, errors.FilerefUpgradeNeededError):
            recovery_attempts += 1
            if recovery_attempts > 3:
                raise
            fresh = await refresh()
            if fresh.document.id != document.id or fresh.document.size != document.size:
                raise OSError("Telegram document changed during playback") from None
            document = fresh.document
        finally:
            await iterator.close()
