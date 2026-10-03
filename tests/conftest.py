from types import SimpleNamespace

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from tests.fakes import FakeTelegram, record


@pytest.fixture
async def service(tmp_path):
    settings = Settings(
        _env_file=None, telegram_api_id=123, telegram_api_hash="fake",
        telegram_chats="-1001234567890", data_dir=tmp_path, metadata_lookup=False,
    )
    app = create_app(settings, FakeTelegram)
    async with app.router.lifespan_context(app):
        await app.state.db.upsert(record())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            yield SimpleNamespace(app=app, http=http, telegram=app.state.telegram,
                                  db=app.state.db, settings=settings)
