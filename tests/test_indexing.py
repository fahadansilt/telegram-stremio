from types import SimpleNamespace

import pytest

from app.telegram import Telegram


class HistoryClient:
    def __init__(self, ids, fail_at=None):
        self.ids = ids
        self.fail_at = fail_at

    async def iter_messages(self, entity, limit=None, min_id=0, reverse=False):
        ids = sorted([item for item in self.ids if item > min_id], reverse=not reverse)
        if limit:
            ids = ids[:limit]
        for message_id in ids:
            if message_id == self.fail_at:
                raise OSError("Telegram unavailable")
            yield SimpleNamespace(id=message_id)


def indexer(service, ids, fail_at=None):
    # No live Telegram session is opened for checkpoint tests.
    value = object.__new__(Telegram)
    value.db = service.db
    value.settings = service.settings
    value.client = HistoryClient(ids, fail_at)
    value.entities = {-1002222222222: "source"}
    value.index_lock = service.db.lock.__class__()
    value.indexed = []

    async def index_message(chat_id, message):
        value.indexed.append(message.id)

    value.index_message = index_message
    return value


async def test_initial_failure_does_not_skip_unindexed_history(service):
    value = indexer(service, [1, 2, 3], fail_at=2)
    with pytest.raises(OSError):
        await value.sync()
    assert value.indexed == [3]
    assert await service.db.cursor(-1002222222222) is None
    value.client.fail_at = None
    await value.sync()
    assert value.indexed == [3, 3, 2, 1]
    assert await service.db.cursor(-1002222222222) == 3


async def test_incremental_checkpoint_commits_only_completed_messages(service):
    await service.db.checkpoint(-1002222222222, 3)
    value = indexer(service, [2, 3, 4, 5, 6], fail_at=5)
    with pytest.raises(OSError):
        await value.sync()
    assert value.indexed == [4]
    assert await service.db.cursor(-1002222222222) == 4
    value.client.fail_at = None
    await value.sync()
    assert value.indexed == [4, 5, 6]
    assert await service.db.cursor(-1002222222222) == 6
    await service.db.checkpoint(-1002222222222, 2)
    assert await service.db.cursor(-1002222222222) == 6


async def test_initial_history_window_is_bounded_and_full_scan_backfills(service):
    service.settings.history_limit = 2
    value = indexer(service, [1, 2, 3, 4])
    await value.sync()
    assert value.indexed == [4, 3]
    await value.sync(full=True)
    assert value.indexed == [4, 3, 4, 3, 2, 1]
