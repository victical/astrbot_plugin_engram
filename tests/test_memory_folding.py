import datetime
from concurrent.futures import ThreadPoolExecutor

import pytest

from astrbot_plugin_engram.core.memory_manager import MemoryManager


@pytest.mark.asyncio
async def test_weekly_folding_llm_failure(mocker, temp_db):
    mock_context = mocker.MagicMock()
    mock_provider = mocker.AsyncMock()
    mock_context.get_provider_by_id.return_value = None
    mock_context.get_using_provider.return_value = mock_provider

    mock_resp = mocker.MagicMock()
    mock_resp.completion_text = ""
    mock_provider.text_chat.return_value = mock_resp

    now = datetime.datetime(2026, 2, 18, 12, 0, 0)
    for i in range(5):
        temp_db.save_memory_index(
            index_id=f"test_{i}",
            summary=f"Day {i}",
            ref_uuids="[]",
            prev_index_id=None,
            source_type="daily_summary",
            user_id="user_123",
            created_at=now - datetime.timedelta(days=i),
        )

    config = {
        "folding_min_samples": 3,
        "weekly_folding_prompt": "test {{memory_texts}}",
    }

    with ThreadPoolExecutor(max_workers=1) as executor:
        manager = MemoryManager(mock_context, config, temp_db.data_dir, executor, temp_db)
        mocker.patch.object(manager, "_ensure_chroma_initialized", new=mocker.AsyncMock())
        mocker.patch("astrbot_plugin_engram.core.memory_manager.asyncio.sleep", new=mocker.AsyncMock())

        result = await manager.fold_weekly_summaries("user_123", days=7)

    assert result is None
    assert mock_provider.text_chat.call_count == 3


@pytest.mark.asyncio
async def test_weekly_folding_success_write_weekly_source_type(mocker, temp_db):
    mock_context = mocker.MagicMock()
    mock_provider = mocker.AsyncMock()
    mock_context.get_provider_by_id.return_value = None
    mock_context.get_using_provider.return_value = mock_provider

    mock_resp = mocker.MagicMock()
    mock_resp.completion_text = "这是本周总结"
    mock_provider.text_chat.return_value = mock_resp

    now = datetime.datetime.now()
    for i in range(4):
        temp_db.save_memory_index(
            index_id=f"daily_{i}",
            summary=f"Day {i}",
            ref_uuids="[]",
            prev_index_id=None,
            source_type="daily_summary",
            user_id="user_abc",
            created_at=now - datetime.timedelta(days=i),
        )

    config = {
        "folding_min_samples": 3,
        "weekly_folding_prompt": "test {{memory_texts}}",
        "ai_name": "助手",
    }

    with ThreadPoolExecutor(max_workers=1) as executor:
        manager = MemoryManager(mock_context, config, temp_db.data_dir, executor, temp_db)

        class DummyCollection:
            def __init__(self):
                self.add_calls = []

            def add(self, **kwargs):
                self.add_calls.append(kwargs)

        dummy_collection = DummyCollection()
        manager.collection = dummy_collection
        manager._chroma_initialized = True
        mocker.patch.object(manager, "_ensure_chroma_initialized", new=mocker.AsyncMock())

        result = await manager.fold_weekly_summaries("user_abc", days=7)

    assert result == "这是本周总结"
    weekly_rows = temp_db.get_summaries_by_type("user_abc", "weekly", days=30)
    assert len(weekly_rows) == 1
    assert weekly_rows[0].source_type == "weekly"
    assert weekly_rows[0].summary == "这是本周总结"

    assert len(dummy_collection.add_calls) == 1
    metadata = dummy_collection.add_calls[0]["metadatas"][0]
    assert metadata["source_type"] == "weekly"
