import datetime
import types

import pytest
from freezegun import freeze_time

from astrbot_plugin_engram.core.scheduler import MemoryScheduler


@freeze_time("2026-02-22 02:00:00")
@pytest.mark.asyncio
async def test_scheduler_trigger(mocker):
    now = datetime.datetime.now()
    assert now.weekday() == 6
    assert now.hour == 2

    logic = mocker.MagicMock()
    logic.last_chat_time = {"user_1": 1, "user_2": 2}
    logic.executor = types.SimpleNamespace(_shutdown=False)
    logic.fold_weekly_summaries = mocker.AsyncMock()
    logic._is_shutdown = False

    scheduler = MemoryScheduler(logic, {"enable_memory_folding": True})

    mocker.patch("astrbot_plugin_engram.core.scheduler.random.randint", return_value=0)
    mocker.patch("astrbot_plugin_engram.core.scheduler.asyncio.sleep", new=mocker.AsyncMock())

    await scheduler._execute_weekly_folding()

    assert logic.fold_weekly_summaries.await_count == 2
