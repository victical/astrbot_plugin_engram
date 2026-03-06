import datetime
from concurrent.futures import ThreadPoolExecutor

import pytest

from astrbot_plugin_engram.core.memory_manager import MemoryManager
from astrbot_plugin_engram.db_manager import DatabaseManager


class DummyCollection:
    def __init__(self):
        self._store = {}

    def get(self, ids, include=None):
        data = self._store.get(ids[0])
        if not data:
            return {"ids": []}
        return {
            "ids": [ids[0]],
            "embeddings": [data.get("embedding")],
            "metadatas": [data.get("metadata", {})],
            "documents": [data.get("document", "")],
        }

    def delete(self, ids):
        for idx in ids:
            self._store.pop(idx, None)

    def add(self, **kwargs):
        idx = kwargs["ids"][0]
        self._store[idx] = {
            "embedding": kwargs.get("embeddings", [[0.0]])[0],
            "metadata": kwargs.get("metadatas", [{}])[0],
            "document": kwargs.get("documents", [""])[0],
        }


@pytest.mark.asyncio
async def test_delete_memory_by_id_keeps_undo_and_raw_rearchive(tmp_path, mocker):
    data_dir = tmp_path / "engram_delete_by_id"
    data_dir.mkdir(parents=True, exist_ok=True)
    db = DatabaseManager(str(data_dir))

    user_id = "u_delete"
    now = datetime.datetime.now()

    db.save_raw_memory(
        uuid="raw-1",
        session_id=user_id,
        user_id=user_id,
        user_name="tester",
        role="user",
        content="我喜欢猫",
        msg_type="text",
        is_archived=True,
        timestamp=now,
    )

    memory_id = "12345678-abcd-efgh-ijkl-1234567890ab"
    db.save_memory_index(
        index_id=memory_id,
        summary="用户提到喜欢猫",
        ref_uuids='["raw-1"]',
        prev_index_id=None,
        source_type="private",
        user_id=user_id,
        created_at=now,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        manager = MemoryManager(
            context=mocker.MagicMock(),
            config={},
            data_dir=str(data_dir),
            executor=executor,
            db_manager=db,
            profile_manager=None,
        )

        collection = DummyCollection()
        collection._store[memory_id] = {
            "embedding": [0.1, 0.2],
            "metadata": {"user_id": user_id},
            "document": "用户提到喜欢猫",
        }

        manager.collection = collection
        manager._chroma_initialized = True
        mocker.patch.object(manager, "_ensure_chroma_initialized", new=mocker.AsyncMock())

        ok, msg, _ = await manager.delete_memory_by_id(user_id, memory_id[:8], delete_raw=False)
        assert ok is True
        assert "删除成功" in msg

        # 删除历史应写入（ID 删除支持撤销）
        assert user_id in manager._delete_history
        assert len(manager._delete_history[user_id]) == 1

        # 原始消息应被标记为未归档（可重归档）
        with db.db.connection_context():
            from astrbot_plugin_engram.db_manager import RawMemory, MemoryIndex

            raw = RawMemory.get(RawMemory.uuid == "raw-1")
            assert raw.is_archived is False

            exists = MemoryIndex.select().where(MemoryIndex.index_id == memory_id).exists()
            assert exists is False

        # 撤销后应恢复索引，并恢复原始消息归档状态
        ok_undo, undo_msg, _ = await manager.undo_last_delete(user_id)
        assert ok_undo is True
        assert "撤销成功" in undo_msg

        with db.db.connection_context():
            from astrbot_plugin_engram.db_manager import RawMemory, MemoryIndex

            raw = RawMemory.get(RawMemory.uuid == "raw-1")
            assert raw.is_archived is True

            restored = MemoryIndex.get_or_none(MemoryIndex.index_id == memory_id)
            assert restored is not None
