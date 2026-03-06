import datetime

import pytest

from astrbot_plugin_engram.handlers.memory_commands import MemoryCommandHandler


@pytest.mark.asyncio
async def test_handle_mem_search_force_retrieve_true():
    class _DummyMemory:
        def __init__(self):
            self.called_kwargs = None

        async def retrieve_memories(self, user_id, query, limit=3, **kwargs):
            self.called_kwargs = kwargs
            return []

    handler = MemoryCommandHandler(config={}, memory_manager=_DummyMemory(), db_manager=None, executor=None)

    result = await handler.handle_mem_search("u1", "犬夜叉")

    assert "未找到" in result
    assert handler.memory.called_kwargs.get("force_retrieve") is True


@pytest.mark.asyncio
async def test_handle_mem_list_contains_short_id():
    class _DummyMemory:
        @staticmethod
        def _ensure_datetime(value):
            return value

    class _DummyIndex:
        def __init__(self):
            self.index_id = "abcdef123456"
            self.created_at = datetime.datetime(2026, 3, 5, 8, 30, 0)
            self.summary = "测试记忆"

    class _DummyDb:
        @staticmethod
        def get_memory_list(user_id, limit):
            return [_DummyIndex()]

    class _DummyExecutor:
        pass

    handler = MemoryCommandHandler(
        config={"list_memory_count": 5},
        memory_manager=_DummyMemory(),
        db_manager=_DummyDb(),
        executor=_DummyExecutor(),
    )

    class _DummyLoop:
        async def run_in_executor(self, executor, func, *args):
            return func(*args)

    original_get_event_loop = __import__("asyncio").get_event_loop
    try:
        __import__("asyncio").get_event_loop = lambda: _DummyLoop()
        result = await handler.handle_mem_list("u1")
    finally:
        __import__("asyncio").get_event_loop = original_get_event_loop

    assert "🆔 abcdef12" in result
    assert "/mem_view <序号或ID>" in result
