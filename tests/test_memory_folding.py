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
