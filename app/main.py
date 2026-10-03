import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import Settings
from app.database import Database
from app.metadata import Metadata
from app.streamer import router as video_router
from app.stremio import router as stremio_router
from app.telegram import Telegram


def create_app(settings: Settings | None = None, telegram_factory=Telegram) -> FastAPI:
    settings = settings or Settings()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(settings.data_dir / "index.sqlite3")
        await db.open()
        metadata = Metadata(db, settings.metadata_lookup)
        telegram = telegram_factory(settings, db, metadata)
        app.state.settings = settings
        app.state.db = db
        app.state.telegram = telegram
        app.state.stream_slots = asyncio.Semaphore(settings.max_streams)
        try:
            await telegram.connect()
            telegram.start()
            yield
        finally:
            await telegram.close()
            await metadata.close()
            await db.close()

    app = FastAPI(title="Telegram Stremio", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "HEAD", "OPTIONS"],
        allow_headers=["Range", "If-Range", "Content-Type"],
        expose_headers=[
            "Accept-Ranges", "Content-Range", "Content-Length", "ETag", "Last-Modified"
        ],
    )
    app.include_router(stremio_router, prefix=settings.route_prefix)
    app.include_router(video_router, prefix=settings.route_prefix)

    @app.get(f"{settings.route_prefix}/health")
    async def health():
        telegram = app.state.telegram
        rows = await app.state.db.execute("SELECT COUNT(*) AS count FROM files")
        return {
            "status": "ok" if telegram.client.is_connected() else "disconnected",
            "indexed_files": rows[0]["count"], "last_sync": telegram.last_sync,
            "sync_error": telegram.sync_error,
        }

    return app
