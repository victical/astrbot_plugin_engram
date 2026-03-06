import datetime
from concurrent.futures import ThreadPoolExecutor

import pytest

from astrbot_plugin_engram.core.memory_manager import MemoryManager
from astrbot_plugin_engram.db_manager import DatabaseManager


@pytest.mark.asyncio
async def test_retrieve_memories_fallback_when_vector_unavailable(tmp_path, mocker):
    data_dir = tmp_path / "engram_fallback"
    data_dir.mkdir(parents=True, exist_ok=True)
    db = DatabaseManager(str(data_dir))

    user_id = "u_fallback"
    now = datetime.datetime.now()

    db.save_raw_memory(
        uuid="raw-fallback-1",
        session_id=user_id,
        user_id=user_id,
        user_name="tester",
        role="user",
        content="我喜欢猫和拿铁",
        msg_type="text",
        is_archived=True,
        timestamp=now,
    )

    db.save_memory_index(
        index_id="idx-fallback-1",
        summary="用户明确提到喜欢猫和拿铁",
        ref_uuids='["raw-fallback-1"]',
        prev_index_id=None,
        source_type="private",
        user_id=user_id,
        created_at=now,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        manager = MemoryManager(
            context=mocker.MagicMock(),
            config={
                "show_relevance_score": True,
                "enable_memory_context_hint": True,
                "memory_context_window": 1,
            },
            data_dir=str(data_dir),
            executor=executor,
            db_manager=db,
            profile_manager=None,
        )

        # 强制走兜底链路：向量查询返回 None
        mocker.patch.object(manager, "_ensure_chroma_initialized", new=mocker.AsyncMock())
        mocker.patch.object(manager, "_collection_query_text", new=mocker.AsyncMock(return_value=None))

        results = await manager.retrieve_memories(
            user_id=user_id,
            query="我喜欢什么",
            limit=3,
            force_retrieve=True,
        )

        assert results
        assert any("喜欢猫" in item for item in results)
