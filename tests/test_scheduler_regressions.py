import asyncio
import datetime
import json
import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from astrbot_plugin_engram.core import scheduler as scheduler_module
from astrbot_plugin_engram.core.scheduler import MemoryScheduler


class DummyExecutor:
    _shutdown = False


class DummyDB:
    def __init__(self):
        self.decay_rates = []

    def decay_active_scores(self, decay_rate):
        self.decay_rates.append(decay_rate)

    def get_cold_memory_ids(self, threshold):
        return []


def test_memory_maintenance_coerces_string_decay_rate():
    db = DummyDB()
    logic = SimpleNamespace(
        executor=None,
        db=db,
        _ensure_chroma_initialized=lambda: None,
        collection=SimpleNamespace(delete=lambda ids: None),
    )
    scheduler = MemoryScheduler(
        logic,
        {
            "enable_memory_decay": True,
            "memory_decay_rate": "2",
            "enable_memory_prune": False,
        },
    )

    asyncio.run(scheduler._execute_memory_maintenance())

    assert db.decay_rates == [2]


def test_calculate_next_check_time_snapshots_last_chat_time_items():
    class MutatingItems(dict):
        def items(self):
            for item in list(super().items()):
                self["new-user"] = 1
                yield item

    logic = SimpleNamespace(
        last_chat_time=MutatingItems({"u1": 1}),
        unsaved_msg_count={"u1": 3},
        _get_archive_timeout=lambda: 1,
        _get_archive_min_msg_count=lambda: 1,
    )
    scheduler = MemoryScheduler(logic, {})

    wait = scheduler._calculate_next_check_time()

    assert wait >= 30


def test_scheduler_start_is_idempotent_and_tracks_task_failures():
    async def run_case():
        async def failing_retry():
            raise RuntimeError("retry boom")

        logic = SimpleNamespace(
            ensure_pending_vector_retry_started=failing_retry,
            last_chat_time={},
            unsaved_msg_count={},
            executor=DummyExecutor(),
        )
        scheduler = MemoryScheduler(
            logic,
            {
                "enable_memory_folding": False,
                "enable_monthly_folding": False,
                "enable_yearly_folding": False,
            },
        )

        await scheduler.start()
        first_tasks = list(scheduler._tasks)
        await scheduler.start()

        assert scheduler._tasks == first_tasks

        for _ in range(5):
            await asyncio.sleep(0)
            if "ensure_pending_vector_retry_started" in scheduler._task_metrics:
                break
        metric = scheduler._task_metrics["ensure_pending_vector_retry_started"]
        assert metric["fail_total"] == 1
        assert "retry boom" in metric["last_error"]

        scheduler.shutdown()
        for task in scheduler._tasks:
            task.cancel()
        await asyncio.gather(*scheduler._tasks, return_exceptions=True)

    asyncio.run(run_case())


def test_monthly_and_yearly_run_calculators_coerce_invalid_config_values():
    scheduler = MemoryScheduler(SimpleNamespace(), {})
    now = datetime.datetime(2026, 5, 19, 12, 0, 0)

    monthly = scheduler._calculate_next_monthly_run(now, "bad-day", "bad-hour")
    yearly = scheduler._calculate_next_yearly_run(now, "bad-month", "bad-day", "bad-hour")

    assert monthly.day == 1
    assert monthly.hour == 3
    assert yearly.month == 1
    assert yearly.day == 1
    assert yearly.hour == 4


def test_folding_execution_coerces_string_delay_and_jitter_values():
    class FoldingLogic:
        def __init__(self):
            self.last_chat_time = {"u1": 1}
            self.executor = DummyExecutor()
            self.folded = []

        async def fold_weekly_summaries(self, user_id, days):
            self.folded.append((user_id, days))

    async def run_case():
        logic = FoldingLogic()
        scheduler = MemoryScheduler(
            logic,
            {
                "enable_memory_folding": True,
                "weekly_folding_days": "7",
                "weekly_folding_delay": "bad",
                "weekly_folding_jitter": "bad",
            },
        )

        await scheduler._execute_weekly_folding()

        assert logic.folded == [("u1", 7)]

    asyncio.run(run_case())


def test_scheduler_uses_timezone_aware_local_datetimes():
    scheduler = MemoryScheduler(SimpleNamespace(), {})

    now = scheduler._now()
    monthly = scheduler._calculate_next_monthly_run(now, 1, 3)
    yearly = scheduler._calculate_next_yearly_run(now, 1, 1, 4)

    assert now.tzinfo is not None
    assert now.utcoffset() is not None
    assert monthly.tzinfo is now.tzinfo
    assert yearly.tzinfo is now.tzinfo


def test_scheduler_persists_and_skips_completed_run_windows(tmp_path):
    logic = SimpleNamespace(data_dir=str(tmp_path))
    scheduler = MemoryScheduler(logic, {})
    run_at = datetime.datetime(2026, 5, 19, 0, 0, tzinfo=datetime.datetime.now().astimezone().tzinfo)

    assert scheduler._should_run_window("daily_persona_scheduler", "2026-05-19", run_at) is True
    scheduler._mark_run_window_complete("daily_persona_scheduler", "2026-05-19", run_at)

    state_path = tmp_path / "scheduler_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["daily_persona_scheduler"]["last_run_key"] == "2026-05-19"

    reloaded = MemoryScheduler(logic, {})
    assert reloaded._should_run_window("daily_persona_scheduler", "2026-05-19", run_at) is False
    assert reloaded._should_run_window("daily_persona_scheduler", "2026-05-20", run_at) is True


def test_scheduler_loop_failures_are_observed_once():
    async def run_case():
        logic = SimpleNamespace(executor=DummyExecutor())
        scheduler = MemoryScheduler(logic, {})
        sleep_calls = 0

        async def fake_sleep(_seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 2:
                scheduler._is_shutdown = True

        scheduler._calculate_sleep_until = lambda *_args, **_kwargs: 0
        scheduler._should_run_window = lambda *_args, **_kwargs: True
        scheduler._mark_run_window_complete = lambda *_args, **_kwargs: None
        scheduler._execute_memory_maintenance = AsyncMock(side_effect=RuntimeError("maintenance boom"))

        original_sleep = asyncio.sleep
        asyncio.sleep = fake_sleep
        try:
            await scheduler.daily_memory_maintenance()
        finally:
            asyncio.sleep = original_sleep

        metric = scheduler._task_metrics["daily_memory_maintenance"]
        assert metric["fail_total"] == 1
        assert metric["runs_total"] == 1

    asyncio.run(run_case())


def test_background_worker_failure_does_not_bubble_to_outer_exception_logger():
    async def run_case():
        errors = []
        original_error = scheduler_module.logger.error
        original_sleep = asyncio.sleep
        sleep_calls = 0

        async def fake_sleep(_seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 2:
                scheduler._is_shutdown = True

        logic = SimpleNamespace(
            last_chat_time={"u1": 1},
            unsaved_msg_count={"u1": 1},
            executor=DummyExecutor(),
            check_and_summarize=AsyncMock(side_effect=RuntimeError("archive boom")),
        )
        scheduler = MemoryScheduler(logic, {})
        scheduler._calculate_next_check_time = lambda: 0

        asyncio.sleep = fake_sleep
        scheduler_module.logger.error = lambda *args, **kwargs: errors.append(args)
        try:
            await scheduler.background_worker()
        finally:
            asyncio.sleep = original_sleep
            scheduler_module.logger.error = original_error

        assert errors == []
        metric = scheduler._task_metrics["background_worker"]
        assert metric["fail_total"] == 1
        assert metric["runs_total"] == 1

    asyncio.run(run_case())
