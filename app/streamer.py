import asyncio
import logging
import re
from email.utils import format_datetime
from urllib.parse import quote

import anyio
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import Response, StreamingResponse
from telethon import errors

from app.reader import read_document

log = logging.getLogger(__name__)
router = APIRouter()


class InvalidRange(ValueError):
    pass


def parse_range(value: str | None, size: int) -> tuple[int, int, bool]:
    """Support one byte range; ignore unknown units and valid multipart requests.

    Multipart is deliberately answered with the full representation (200), which
    HTTP permits for a server choosing not to implement multipart byte ranges.
    Unsatisfiable/malformed single byte ranges receive 416 in the route.
    """
    if not value or not value.lower().startswith("bytes="):
        return 0, size - 1, False
    ranges = value[6:].strip()
    if "," in ranges:
        return 0, size - 1, False
    match = re.fullmatch(r"([0-9]*)-([0-9]*)", ranges)
    if not match or not any(match.groups()):
        raise InvalidRange
    first, last = match.groups()
    try:
        if not first:
            suffix = int(last)
            if suffix <= 0 or size <= 0:
                raise InvalidRange
            return max(0, size - suffix), size - 1, True
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
    except ValueError as exc:
        raise InvalidRange from exc
    if start >= size or end < start:
        raise InvalidRange
    return start, end, True


class OwnedStreamingResponse(StreamingResponse):
    """Release the reader and slot even when ASGI sending fails or is cancelled."""

    def __init__(self, content, reader, semaphore, **kwargs):
        super().__init__(content, **kwargs)
        self.reader = reader
        self.semaphore = semaphore

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                # Starlette can cancel a whole AnyIO scope on client disconnect.
                with anyio.CancelScope(shield=True):
                    await self.body_iterator.aclose()
                    await self.reader.aclose()
            finally:
                self.semaphore.release()


@router.api_route("/video/{chat_id}/{message_id}", methods=["GET", "HEAD"])
async def video(request: Request, chat_id: int, message_id: int):
    telegram = request.app.state.telegram
    if chat_id not in telegram.allowed_chats or message_id <= 0:
        raise HTTPException(404, "Video is not indexed")
    record = await request.app.state.db.get_file(chat_id, message_id)
    if record is None:
        raise HTTPException(404, "Video is not indexed")
    message = await telegram.get_message(chat_id, message_id)
    document = message.document
    if document.id != record["document_id"] or document.size != record["size"]:
        raise HTTPException(409, "Video changed; wait for the index to refresh")
    size = document.size
    etag = f'"tg-{document.id}-{size}"'
    headers = {
        "Accept-Ranges": "bytes", "Content-Type": record["mime_type"],
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(record['filename'], safe='')}",
        "ETag": etag, "Last-Modified": format_datetime(message.date, usegmt=True),
        "Cache-Control": "private, no-store", "X-Accel-Buffering": "no",
    }
    range_value = request.headers.get("range") if request.method == "GET" else None
    if_range = request.headers.get("if-range")
    if if_range and if_range not in {etag, headers["Last-Modified"]}:
        range_value = None
    try:
        start, end, partial = parse_range(range_value, size)
    except InvalidRange:
        return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
    headers["Content-Length"] = str(max(0, end - start + 1))
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    status = 206 if partial else 200
    if request.method == "HEAD" or size == 0:
        return Response(status_code=status, headers=headers)
    semaphore = request.app.state.stream_slots
    try:
        async with asyncio.timeout(1):
            await semaphore.acquire()
    except TimeoutError as exc:
        raise HTTPException(
            503, "All streaming slots are in use", headers={"Retry-After": "2"}
        ) from exc

    reader = read_document(
        telegram.client, document, lambda: telegram.get_message(chat_id, message_id),
        start, end, request.app.state.settings.telegram_timeout,
    )
    try:
        # Fail with an HTTP error before committing 206 headers if Telegram can't read.
        first = await anext(reader)
    except BaseException as exc:
        try:
            await reader.aclose()
        finally:
            semaphore.release()
        if isinstance(exc, errors.FloodWaitError):
            raise HTTPException(
                429, "Telegram rate limit", headers={"Retry-After": str(exc.seconds)}
            ) from exc
        if isinstance(exc, (errors.RPCError, OSError, TimeoutError, StopAsyncIteration)):
            raise HTTPException(502, "Could not read Telegram video") from exc
        raise

    async def body():
        yield first
        try:
            async for chunk in reader:
                yield chunk
        except Exception:
            # After headers were sent we must terminate, never inject JSON into video bytes.
            log.exception(
                "Telegram playback interrupted for chat %s message %s", chat_id, message_id
            )
            raise

    return OwnedStreamingResponse(
        body(), reader, semaphore, status_code=status, headers=headers,
    )
