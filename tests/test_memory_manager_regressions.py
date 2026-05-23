import asyncio
import datetime
import pathlib
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from astrbot_plugin_engram.db_manager import DatabaseManager
from astrbot_plugin_engram.core.memory_manager import MemoryManager


class DummyContext:
    def get_provider_by_id(self, provider_id):
        return None

    def get_using_provider(self):
        return None


class DummyDB:
    def verify_contract(self, required_methods, stage="startup"):
        return None

    def get_memory_indexes_by_ids(self, index_ids):
        return {
            index_ids[0]: SimpleNamespace(
                index_id=index_ids[0],
                created_at=int(datetime.datetime(2026, 4, 19, 2, 0, 0).timestamp()),
                active_score=100,
                prev_index_id=None,
                ref_uuids='[]',
                summary='remembered study plan',
            )
        }

    def update_active_score(self, index_id, bonus=10):
        return None

    def get_all_users_messages(self, start_date=None, end_date=None, limit=None):
        return []

    def get_all_users_stats(self):
        return {}

    def enqueue_pending_vector_jobs(self, rows):
        return len(rows or [])

    def get_pending_vector_jobs(self, limit=200):
        return []

    def delete_pending_vector_jobs(self, index_ids):
        return len(index_ids or [])


def make_manager(tmp_path, db=None):
    manager = MemoryManager(
        context=DummyContext(),
        config={"show_relevance_score": False},
        data_dir=str(tmp_path),
        executor=None,
        db_manager=db or DummyDB(),
    )
    manager._ensure_chroma_initialized = AsyncMock()
    manager._collection_query_text = AsyncMock(
        return_value={
            "ids": [["idx-1"]],
            "documents": [["remembered study plan"]],
            "distances": [[0.1]],
            "metadatas": [[{
                "user_id": "u1",
                "source_type": "private",
                "created_at": "2026-04-19 02:00:00",
            }]],
        }
    )
    manager._intent_classifier = SimpleNamespace(classify_query=lambda query: ("recall", 1.0))
    manager._get_memory_context = AsyncMock(return_value={})
    return manager


def test_memory_manager_async_locks_are_created_lazily(tmp_path):
    manager = MemoryManager(
        context=DummyContext(),
        config={"show_relevance_score": False},
        data_dir=str(tmp_path),
        executor=None,
        db_manager=DummyDB(),
    )

    assert manager._chroma_init_lock is None
    assert manager._pending_retry_lock is None

    async def run_case():
        chroma_lock = manager._get_chroma_init_lock()
        pending_lock = manager._get_pending_retry_lock()

        assert isinstance(chroma_lock, asyncio.Lock)
        assert isinstance(pending_lock, asyncio.Lock)
        assert manager._chroma_init_lock is chroma_lock
        assert manager._pending_retry_lock is pending_lock

    asyncio.run(run_case())


class FallbackDB(DummyDB):
    def __init__(self):
        self.updated_ids = []

    def search_memory_indexes_by_keywords(self, user_id, keyword_tokens, limit, start_time, end_time, source_types, enable_bm25):
        return [
            SimpleNamespace(
                index_id="fallback-idx-1",
                created_at=datetime.datetime(2026, 4, 20, 9, 0, 0),
                active_score=100,
                prev_index_id=None,
                ref_uuids='[]',
                summary="fallback remembered study plan",
                source_type="private",
            )
        ]

    def update_active_score(self, index_id, bonus=10):
        self.updated_ids.append(index_id)
        return None


def test_save_memory_index_persists_scope_fields(tmp_path):
    manager = DatabaseManager(str(tmp_path))

    manager.save_memory_index(
        index_id="idx-1",
        summary="group summary",
        ref_uuids='[]',
        prev_index_id=None,
        source_type="group",
        user_id="group-42",
        group_id="group-42",
        member_id="member-7",
        created_at=datetime.datetime(2026, 4, 19, 2, 0, 0),
    )

    row = manager.get_memory_index_by_id("idx-1")
    assert row is not None
    assert row.group_id == "group-42"
    assert row.member_id == "member-7"


def test_retrieve_memories_accepts_integer_created_at(tmp_path):
    manager = make_manager(tmp_path)

    memories = asyncio.run(manager.retrieve_memories("u1", "考试计划", limit=1, force_retrieve=True))

    assert len(memories) == 1
    assert "remembered study plan" in memories[0]
    assert "🗂️ private" in memories[0]


def test_retrieve_memories_force_retrieve_bypasses_skip_intent(tmp_path):
    manager = make_manager(tmp_path)
    manager._intent_classifier = SimpleNamespace(classify_query=lambda query: ("skip", 0.0))

    skipped = asyncio.run(manager.retrieve_memories("u1", "随便聊聊", limit=1))
    forced = asyncio.run(manager.retrieve_memories("u1", "随便聊聊", limit=1, force_retrieve=True))

    assert skipped == []
    assert len(forced) == 1
    assert "🆔 idx-1" in forced[0]
    assert "🗂️ private" in forced[0]


def test_retrieve_memories_falls_back_when_vector_query_raises(tmp_path):
    db = FallbackDB()
    manager = make_manager(tmp_path, db=db)
    manager._collection_query_text = AsyncMock(side_effect=RuntimeError("vector unavailable"))

    memories = asyncio.run(manager.retrieve_memories("u1", "study plan", limit=1, force_retrieve=True))

    assert len(memories) == 1
    assert "fallback remembered study plan" in memories[0]
    assert "🆔 fallback" in memories[0]
    assert "🗂️ private" in memories[0]
    assert db.updated_ids == ["fallback-idx-1"]


def test_retrieve_memories_falls_back_when_vector_result_empty(tmp_path):
    db = FallbackDB()
    manager = make_manager(tmp_path, db=db)
    manager._collection_query_text = AsyncMock(
        return_value={"ids": [[]], "documents": [[]], "distances": [[]], "metadatas": [[]]}
    )

    memories = asyncio.run(manager.retrieve_memories("u1", "study plan", limit=1, force_retrieve=True))

    assert len(memories) == 1
    assert "fallback remembered study plan" in memories[0]
    assert "🆔 fallback" in memories[0]
    assert "🗂️ private" in memories[0]


class ExportDB(DummyDB):
    def get_all_users_messages(self, start_date=None, end_date=None, limit=None):
        return []


def test_export_all_users_messages_empty_json_uses_valid_empty_payload(tmp_path):
    manager = make_manager(tmp_path, db=ExportDB())

    success, data, stats = asyncio.run(manager.export_all_users_messages(format="json"))

    assert success is True
    assert data == "[]"
    assert stats == {"exported": 0}


class PendingRow:
    index_id = "idx-pending"
    summary = "pending summary"
    metadata = {"user_id": "u1"}
    retry_count = 0
    user_id = "u1"
    source_type = "private"
    reason = "test"


class PendingDB(DummyDB):
    def __init__(self):
        self.deleted_ids = []
        self.enqueued = []

    def get_pending_vector_jobs(self, limit=200):
        return [PendingRow()]

    def delete_pending_vector_jobs(self, index_ids):
        self.deleted_ids.extend(index_ids)
        return len(index_ids)

    def enqueue_pending_vector_jobs(self, rows):
        self.enqueued.extend(rows)
        return len(rows)


def test_retry_pending_vector_jobs_keeps_failed_job_until_retry_limit(tmp_path):
    db = PendingDB()
    manager = make_manager(tmp_path, db=db)
    manager._collection_add_texts = AsyncMock(return_value=False)

    result = asyncio.run(manager.retry_pending_vector_jobs(batch_size=1, max_retry=3))

    assert result == {"loaded": 1, "success": 0, "failed": 1}
    assert db.deleted_ids == []
    assert db.enqueued[0]["index_id"] == "idx-pending"
    assert db.enqueued[0]["retry_count"] == 1


class DetailDB(DummyDB):
    def __init__(self):
        self.MemoryIndex = SimpleNamespace()
        self.db = SimpleNamespace(connection_context=lambda: self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get_memory_list(self, user_id, limit):
        return [
            SimpleNamespace(
                index_id="idx-bad",
                summary="bad refs",
                ref_uuids="{broken",
                prev_index_id=None,
                source_type="private",
                created_at=datetime.datetime(2026, 1, 1),
                active_score=100,
            )
        ]

    def get_memories_by_uuids(self, uuids):
        raise AssertionError("bad ref_uuids should be treated as empty")


def test_get_memory_detail_tolerates_corrupt_ref_uuids(tmp_path):
    manager = make_manager(tmp_path, db=DetailDB())

    memory, raw_msgs = asyncio.run(manager.get_memory_detail("u1", 1))

    assert memory.index_id == "idx-bad"
    assert raw_msgs == []


class DummySummaryProvider:
    def __init__(self):
        self.prompts = []

    async def text_chat(self, prompt):
        self.prompts.append(prompt)
        return SimpleNamespace(completion_text='{"summary":"用户讨论考试计划","key_facts":["考试计划"],"keywords":["考试"],"entities":[],"mood":"平静"}')


class SummaryContext(DummyContext):
    def __init__(self, provider):
        self.provider = provider

    def get_using_provider(self):
        return self.provider


def test_summary_batch_uses_default_prompt_when_config_missing(tmp_path):
    provider = DummySummaryProvider()
    manager = make_manager(tmp_path)
    manager.context = SummaryContext(provider)
    manager.config = {"ai_name": "助手"}
    raw_msgs = [
        SimpleNamespace(
            uuid="raw-1",
            role="user",
            user_name="用户",
            content="今天我们聊了考试计划",
            timestamp=datetime.datetime(2026, 5, 1, 10, 0, 0),
        )
    ]

    result = asyncio.run(
        manager._process_single_summary_batch(
            "u1",
            raw_msgs,
            datetime.date(2026, 5, 1),
        )
    )

    assert result["summary"].startswith("用户讨论考试计划")
    assert "考试计划" in result["summary"]
    assert "今天我们聊了考试计划" in provider.prompts[0]


class EmptyQuery:
    def order_by(self, *_args, **_kwargs):
        return []


class SortField:
    def asc(self):
        return self


class RebuildDB(DummyDB):
    def __init__(self):
        self.db = SimpleNamespace(connection_context=lambda: self)
        self.MemoryIndex = SimpleNamespace(select=lambda: EmptyQuery(), created_at=SortField())

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class ProbeLock:
    def __init__(self):
        self.entered = False
        self.was_locked_during_delete = False

    def __enter__(self):
        self.entered = True

    def __exit__(self, exc_type, exc, tb):
        self.entered = False
        return False


class ProbeChromaClient:
    def __init__(self, probe):
        self.probe = probe

    def delete_collection(self, name):
        self.probe.was_locked_during_delete = self.probe.entered

    def get_or_create_collection(self, name):
        return SimpleNamespace(name=name)


def test_rebuild_vector_full_deletes_collection_under_swap_lock(tmp_path):
    manager = make_manager(tmp_path, db=RebuildDB())
    probe = ProbeLock()
    manager._collection_swap_lock = probe
    manager.chroma_client = ProbeChromaClient(probe)

    result = asyncio.run(manager.rebuild_vector_collection(full_rebuild=True, batch_size=1))

    assert result["success"] is True
    assert probe.was_locked_during_delete is True


class DeleteFailureDB(DummyDB):
    def __init__(self):
        self.saved_delete_history = []
        self.deleted_indexes = []
        self.raw_deleted = []

    def save_delete_history(self, **kwargs):
        self.saved_delete_history.append(kwargs)
        return len(self.saved_delete_history)

    def delete_memory_index(self, index_id):
        self.deleted_indexes.append(index_id)
        return 1

    def delete_raw_memories_by_uuids(self, uuids):
        self.raw_deleted.extend(uuids)
        return len(uuids)


def test_delete_memory_does_not_write_history_when_delete_fails(tmp_path):
    db = DeleteFailureDB()
    manager = make_manager(tmp_path, db=db)
    manager.collection = SimpleNamespace(
        get=lambda ids, include=None: {"ids": ids, "embeddings": [], "metadatas": [], "documents": []},
        delete=lambda ids: (_ for _ in ()).throw(RuntimeError("chroma delete failed")),
    )
    target = SimpleNamespace(
        index_id="idx-delete-fail",
        summary="delete should fail",
        ref_uuids='["raw-1"]',
        prev_index_id=None,
        source_type="private",
        user_id="u1",
        created_at=datetime.datetime(2026, 5, 19, 10, 0, 0),
        active_score=100,
    )

    success, message, summary = asyncio.run(manager._delete_memory_entry("u1", target))

    assert success is False
    assert "chroma delete failed" in message
    assert summary == "delete should fail"
    assert db.saved_delete_history == []
    assert db.deleted_indexes == []
    assert manager._delete_history == {}


def test_cleanup_inactive_users_snapshots_activity_dicts(tmp_path):
    class MutatingItems(dict):
        def items(self):
            for item in super().items():
                self["new-user"] = time.time()
                yield item

    manager = make_manager(tmp_path)
    old_ts = time.time() - manager._inactive_threshold - 1
    manager.last_chat_time = MutatingItems({"inactive": old_ts})
    manager.unsaved_msg_count = {"inactive": 0}

    manager._cleanup_inactive_users()

    assert "inactive" not in manager.last_chat_time


def test_check_and_summarize_resets_unsaved_count_under_activity_lock(tmp_path):
    class ProbeLock:
        def __init__(self):
            self.entered = False
            self.was_locked_on_set = False

        def __enter__(self):
            self.entered = True

        def __exit__(self, exc_type, exc, tb):
            self.entered = False
            return False

    class CountDict(dict):
        def __init__(self, probe, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.probe = probe

        def __setitem__(self, key, value):
            self.probe.was_locked_on_set = self.probe.entered
            super().__setitem__(key, value)

    async def run_case():
        manager = make_manager(tmp_path)
        probe = ProbeLock()
        manager._activity_lock = probe
        manager.last_chat_time = {"u1": time.time() - 10}
        manager.unsaved_msg_count = CountDict(probe, {"u1": 1})
        manager._get_archive_timeout = lambda: 1
        manager._get_archive_min_msg_count = lambda: 1
        manager._summarize_private_chat = AsyncMock()

        await manager.check_and_summarize()

        assert manager._summarize_private_chat.await_count == 1
        assert manager.unsaved_msg_count["u1"] == 0
        assert probe.was_locked_on_set is True

    asyncio.run(run_case())
