import asyncio
import json
import logging
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException
from telethon import TelegramClient, errors, events, utils

from app.config import Settings
from app.database import Database
from app.metadata import Metadata, parse_filename

log = logging.getLogger(__name__)
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v", ".ts"}
MIME_TYPES = {".mkv": "video/x-matroska", ".mp4": "video/mp4", ".m4v": "video/mp4"}


def make_client(settings: Settings) -> TelegramClient:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return TelegramClient(
        settings.session_path, settings.telegram_api_id,
        settings.telegram_api_hash.get_secret_value(),
        flood_sleep_threshold=0, request_retries=2,
    )


class Telegram:
    def __init__(self, settings: Settings, db: Database, metadata: Metadata):
        self.settings = settings
        self.db = db
        self.metadata = metadata
        self.client = make_client(settings)
        self.entities = {}
        self.index_lock = asyncio.Lock()
        self.sync_error = None
        self.last_sync = None
        self.task = None

    @property
    def allowed_chats(self) -> list[int]:
        return list(self.entities)

    async def connect(self):
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("Telegram login required. Run: python -m app.cli login")
        # Populate access hashes so configured numeric private-channel IDs can resolve.
        async for _ in self.client.iter_dialogs():
            pass
        if not self.settings.chats:
            raise RuntimeError("Set TELEGRAM_CHATS to the channels/groups you want to index")
        for source in self.settings.chats:
            entity = await self.client.get_entity(source)
            chat_id = utils.get_peer_id(entity)
            if chat_id >= 0:
                raise RuntimeError("TELEGRAM_CHATS must reference channels or groups, not users")
            self.entities[chat_id] = await self.client.get_input_entity(entity)

    def start(self):
        self.client.add_event_handler(self.on_message, events.NewMessage(chats=self.allowed_chats))
        self.client.add_event_handler(
            self.on_message, events.MessageEdited(chats=self.allowed_chats)
        )
        self.client.add_event_handler(self.on_deleted, events.MessageDeleted())
        self.task = asyncio.create_task(self.sync_loop(), name="telegram-indexer")

    async def close(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        await self.client.disconnect()

    async def on_message(self, event):
        try:
            async with self.index_lock:
                await self.index_message(event.chat_id, event.message)
        except Exception:
            log.exception("Could not index Telegram update")

    async def on_deleted(self, event):
        # Telegram does not identify the chat for some basic-group deletions.
        # Never delete IDs from other groups when a deletion's source is unknown.
        if event.chat_id in self.entities:
            async with self.index_lock:
                for message_id in event.deleted_ids:
                    await self.db.delete(event.chat_id, message_id)

    async def index_message(self, chat_id: int, message):
        document = message.document
        if not document:
            await self.db.delete(chat_id, message.id)
            return
        filename = message.file.name or f"video-{message.id}{message.file.ext or '.mp4'}"
        extension = Path(filename).suffix.lower()
        if not (document.mime_type.startswith("video/") or extension in VIDEO_EXTENSIONS):
            await self.db.delete(chat_id, message.id)
            return
        parsed = await asyncio.to_thread(parse_filename, filename, message.raw_text or "")
        if not parsed or document.size <= 0:
            await self.db.delete(chat_id, message.id)
            return
        override = await self.db.override(chat_id, message.id)
        if override:
            parsed["imdb_id"] = override
        imdb_id, meta = await self.metadata.match(parsed)
        await self.db.upsert({
            "chat_id": chat_id, "message_id": message.id, "document_id": document.id,
            "filename": filename, "size": document.size,
            "mime_type": MIME_TYPES.get(extension, document.mime_type),
            "title": parsed["title"], "year": parsed["year"], "quality": parsed["quality"],
            "caption": message.raw_text or "", "imdb_id": imdb_id, "metadata": json.dumps(meta),
            "posted_at": message.date.isoformat(),
        })

    async def sync(self, full: bool = False):
        async with self.index_lock:
            for chat_id, entity in self.entities.items():
                last_id = await self.db.cursor(chat_id)
                if full or last_id is None:
                    highest = 0
                    limit = (
                        None if full or self.settings.history_limit == 0
                        else self.settings.history_limit
                    )
                    async for message in self.client.iter_messages(entity, limit=limit):
                        await self.index_message(chat_id, message)
                        highest = max(highest, message.id)
                    # Checkpoint only after the initial window was successfully indexed.
                    await self.db.checkpoint(chat_id, highest)
                else:
                    async for message in self.client.iter_messages(
                        entity, min_id=last_id, reverse=True, limit=None
                    ):
                        await self.index_message(chat_id, message)
                        await self.db.checkpoint(chat_id, message.id)
                # Reconcile the latest window to catch edits/deletes missed while offline.
                rows = await self.db.execute(
                    "SELECT message_id FROM files WHERE chat_id=? "
                    "ORDER BY message_id DESC LIMIT 100",
                    (chat_id,),
                )
                if rows:
                    messages = await self.client.get_messages(
                        entity, ids=[row["message_id"] for row in rows]
                    )
                    for row, message in zip(rows, messages, strict=True):
                        if message is None or not getattr(message, "document", None):
                            await self.db.delete(chat_id, row["message_id"])
                        else:
                            await self.index_message(chat_id, message)
            self.last_sync = datetime.now(UTC).isoformat()
            self.sync_error = None

    async def sync_loop(self):
        while True:
            try:
                await self.sync()
            except errors.FloodWaitError as exc:
                self.sync_error = f"Telegram rate limited indexing for {exc.seconds} seconds"
                log.warning("%s", self.sync_error)
                await asyncio.sleep(exc.seconds)
            except Exception:
                self.sync_error = "Indexing failed; see server logs"
                log.exception("Telegram indexing failed")
            await asyncio.sleep(self.settings.sync_interval)

    async def get_message(self, chat_id: int, message_id: int):
        if chat_id not in self.entities:
            raise HTTPException(404, "Chat is not configured")
        try:
            async with asyncio.timeout(self.settings.telegram_timeout):
                message = await self.client.get_messages(self.entities[chat_id], ids=message_id)
        except errors.FloodWaitError as exc:
            raise HTTPException(
                429, "Telegram rate limit", headers={"Retry-After": str(exc.seconds)}
            ) from exc
        except (errors.ChannelPrivateError, errors.ChatAdminRequiredError) as exc:
            raise HTTPException(404, "Telegram message is inaccessible") from exc
        except (errors.RPCError, OSError, TimeoutError) as exc:
            raise HTTPException(502, "Telegram is unavailable") from exc
        if message is None or not message.document:
            await self.db.delete(chat_id, message_id)
            raise HTTPException(404, "Telegram video no longer exists")
        return message
