import asyncio
import json
import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from astrbot_plugin_engram.handlers.tool_commands import MemoryToolHandler


class DummyEvent:
    def get_sender_id(self):
        return "u1"


def parse_time_expr(_value):
    return None, None, ""


def normalize_source_types(source_types, default_types=None):
    return source_types or default_types or ["private"]


def test_memory_tool_forces_retrieve_and_returns_structured_fields():
    logic = SimpleNamespace(
        retrieve_memories=AsyncMock(
            return_value=[
                "🎯 88% | 🆔 abcdef12 | ⏰ 2026-05-01 12:30:00 | 🗂️ private\n📝 归档：用户喜欢结构化结果"
            ]
        )
    )
    handler = MemoryToolHandler({"enable_memory_search_tool": True}, logic)

    output = asyncio.run(
        handler.build_memory_search_output(
            event=DummyEvent(),
            query="用户喜欢什么",
            limit=3,
            time_expr="",
            source_types=None,
            parse_time_expr=parse_time_expr,
            normalize_source_types=normalize_source_types,
        )
    )

    logic.retrieve_memories.assert_awaited_once()
    assert logic.retrieve_memories.await_args.kwargs["force_retrieve"] is True

    payload = json.loads(output)
    assert payload["type"] == "memory_search_result"
    assert payload["query"] == "用户喜欢什么"
    assert payload["results"] == [
        {
            "memory_id": "abcdef12",
            "source_type": "private",
            "created_at": "2026-05-01 12:30:00",
            "score": 88,
            "summary": "用户喜欢结构化结果",
        }
    ]
    assert "mem_get_detail_tool" in payload["usage_hint"]


def test_memory_tool_uses_normalized_type_as_source_fallback():
    logic = SimpleNamespace(
        retrieve_memories=AsyncMock(
            return_value=[
                "🆔 feedbeef | ⏰ 2026-05-02 08:00:00\n📝 归档：群聊记忆摘要"
            ]
        )
    )
    handler = MemoryToolHandler({"enable_memory_search_tool": True}, logic)

    output = asyncio.run(
        handler.build_memory_search_output(
            event=DummyEvent(),
            query="群聊摘要",
            limit=1,
            time_expr="",
            source_types=["group"],
            parse_time_expr=parse_time_expr,
            normalize_source_types=normalize_source_types,
        )
    )

    payload = json.loads(output)
    assert payload["results"][0]["memory_id"] == "feedbeef"
    assert payload["results"][0]["source_type"] == "group"
    assert payload["results"][0]["score"] is None
