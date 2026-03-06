import time

import pytest

from astrbot_plugin_engram.main import EngramPlugin


class _DummyEvent:
    def __init__(self, sender_id="u1", message="test"):
        self._sender_id = sender_id
        self.message_str = message

    def get_group_id(self):
        return None

    def get_sender_id(self):
        return self._sender_id

    def plain_result(self, text):
        return text


def _make_plugin_for_cache_tests():
    plugin = EngramPlugin.__new__(EngramPlugin)
    plugin.config = {
        "enable_memory_topic_cache": True,
        "memory_topic_cache_ttl": 120,
        "memory_topic_cache_max_topics": 3,
        "memory_topic_cache_similarity_threshold": 0.1,
        "enable_memory_search_tool": True,
        "memory_tool_hint_mode": "on_insufficient_evidence",
        "memory_tool_hint_min_memories": 1,
    }
    plugin._memory_topic_cache = {}
    return plugin


def test_topic_cache_store_and_hit():
    plugin = _make_plugin_for_cache_tests()

    user_id = "u1"
    query = "你还记得我喜欢喝冰美式吗"
    memories = ["m1", "m2"]

    topic_key = plugin._build_topic_cache_key(query)
    plugin._set_cached_topic_memories(user_id, query, topic_key, memories)

    hit, got, _ = plugin._get_cached_topic_memories(user_id, query)
    assert hit is True
    assert got == memories


def test_topic_cache_ttl_expired_then_miss():
    plugin = _make_plugin_for_cache_tests()

    user_id = "u1"
    query = "你还记得我喜欢喝冰美式吗"
    topic_key = plugin._build_topic_cache_key(query)

    plugin._set_cached_topic_memories(user_id, query, topic_key, ["m1"])
    plugin._get_topic_cache_service()._cache[user_id][topic_key]["expire_at"] = time.time() - 1

    hit, got, _ = plugin._get_cached_topic_memories(user_id, query)
    assert hit is False
    assert got == []


def test_topic_cache_similar_query_reuse():
    plugin = _make_plugin_for_cache_tests()

    user_id = "u1"
    first_query = "remember my favorite drink is iceamericano"
    follow_query = "do i still like iceamericano recently"

    first_key = plugin._build_topic_cache_key(first_query)
    plugin._set_cached_topic_memories(user_id, first_query, first_key, ["cached-memory"])

    hit, got, matched_key = plugin._get_cached_topic_memories(user_id, follow_query)
    assert hit is True
    assert got == ["cached-memory"]
    assert matched_key in {first_key, plugin._build_topic_cache_key(follow_query)}


@pytest.mark.parametrize(
    "mode,memory_count,should_retrieve,expected",
    [
        ("always", 10, False, True),
        ("never", 0, True, False),
        ("on_insufficient_evidence", 0, True, True),
        ("on_insufficient_evidence", 2, True, False),
        ("on_insufficient_evidence", 0, False, False),
    ],
)
def test_tool_hint_mode(mode, memory_count, should_retrieve, expected):
    plugin = _make_plugin_for_cache_tests()
    plugin.config["memory_tool_hint_mode"] = mode
    plugin.config["memory_tool_hint_min_memories"] = 1

    assert plugin._should_inject_tool_hint(memory_count=memory_count, should_retrieve=should_retrieve) is expected


def test_tool_hint_min_memories_threshold():
    plugin = _make_plugin_for_cache_tests()
    plugin.config["memory_tool_hint_mode"] = "on_insufficient_evidence"
    plugin.config["memory_tool_hint_min_memories"] = 2

    assert plugin._should_inject_tool_hint(memory_count=1, should_retrieve=True) is True
    assert plugin._should_inject_tool_hint(memory_count=2, should_retrieve=True) is False


@pytest.mark.asyncio
async def test_main_mem_search_force_retrieve_true():
    plugin = EngramPlugin.__new__(EngramPlugin)

    class _DummyLogic:
        def __init__(self):
            self.called_kwargs = None

        async def retrieve_memories(self, user_id, query, limit=3, **kwargs):
            self.called_kwargs = kwargs
            return []

    logic = _DummyLogic()
    plugin.logic = logic

    event = _DummyEvent(sender_id="u1", message="mem_search 犬夜叉")
    outputs = [item async for item in plugin.mem_search(event, "犬夜叉")]

    assert outputs
    assert "未找到" in outputs[0]
    assert logic.called_kwargs.get("force_retrieve") is True
