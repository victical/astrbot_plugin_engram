import asyncio
import datetime
import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from astrbot_plugin_engram.db_manager import DatabaseManager
from astrbot_plugin_engram.core.memory_manager import MemoryManager


class DummyContext:
    def get_provider_by_id(self, provider_id):
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
