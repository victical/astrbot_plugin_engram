import datetime
from concurrent.futures import ThreadPoolExecutor

import pytest

from astrbot_plugin_engram.core.memory_manager import MemoryManager
from astrbot_plugin_engram.db_manager import DatabaseManager, MemoryIndex, RawMemory


@pytest.mark.asyncio
async def test_summarize_persists_index_even_if_vector_write_fails(tmp_path, mocker):
    data_dir = tmp_path / "engram_summarize_persist"
    data_dir.mkdir(parents=True, exist_ok=True)
    db = DatabaseManager(str(data_dir))

    user_id = "u_summary"
    now = datetime.datetime.now()

    db.save_raw_memory(
        uuid="raw-s-1",
        session_id=user_id,
        user_id=user_id,
        user_name="tester",
        role="user",
        content="今天我去了图书馆学习",
        msg_type="text",
        is_archived=False,
        timestamp=now,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        manager = MemoryManager(
            context=mocker.MagicMock(),
            config={"ai_name": "助手"},
            data_dir=str(data_dir),
            executor=executor,
            db_manager=db,
            profile_manager=None,
        )

        async def _fake_batch(_uid, group_msgs, _date_key):
            return {
                "summary": "用户今天去图书馆学习",
                "created_at": now,
                "ref_uuids": [m.uuid for m in group_msgs],
                "archive": False,
            }

        mocker.patch.object(manager, "_process_single_summary_batch", side_effect=_fake_batch)
        mocker.patch.object(manager, "_ensure_chroma_initialized", new=mocker.AsyncMock())
        mocker.patch.object(manager, "_collection_add_texts", new=mocker.AsyncMock(return_value=False))

        await manager._summarize_private_chat(user_id)

        # 向量失败不应影响 SQLite 索引落库
        with db.db.connection_context():
            idx_count = MemoryIndex.select().where(MemoryIndex.user_id == user_id).count()
            assert idx_count == 1

            raw = RawMemory.get(RawMemory.uuid == "raw-s-1")
            assert raw.is_archived is True

        # 失败索引应进入待补偿队列
        assert len(manager._pending_vector_jobs) >= 1
        assert manager._pending_vector_jobs[-1]["user_id"] == user_id
