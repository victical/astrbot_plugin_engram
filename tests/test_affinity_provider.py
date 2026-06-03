import datetime as dt

import pytest

from astrbot_plugin_engram.core.affinity_provider import AffinityMemoryProvider


class FakeLogic:
    async def get_user_profile(self, user_id):
        return {
            "user_id": user_id,
            "preferences": {"likes": ["tea"], "dislikes": ["noise"]},
            "shared_secrets": True,
            "social_graph": {"interaction_stats": {"total_chat_days": 8}},
        }


class MemoryItem:
    index_id = "idx-1"
    summary = "用户喜欢茶"
    source_type = "daily"
    created_at = dt.datetime(2026, 5, 23, 10, 0, 0)


class FakeDB:
    def get_message_stats(self, user_id):
        return {"total": 10, "user_messages": 6}

    def get_all_raw_messages(self, user_id, start_date=None, end_date=None, limit=None):
        return []

    def get_summaries_by_type(self, user_id, source_type, days=7):
        return [MemoryItem()]

    def get_memory_list(self, user_id, limit=5):
        return [MemoryItem()]

    def get_memories_in_range(self, user_id, start_time, end_time):
        return [MemoryItem()]

    def get_all_user_ids(self):
        return ["u1"]


class FakeBondCalculator:
    def calculate_profile_depth(self, profile):
        return 40

    def calculate_bond_level(self, memory_count, profile):
        return {"level": 3, "breakdown": {"days_score": 4.5}}


@pytest.mark.asyncio
async def test_provider_get_user_profile():
    provider = AffinityMemoryProvider(FakeLogic(), FakeDB(), bond_calculator=None)

    assert await provider.get_user_profile("u1") == {
        "user_id": "u1",
        "preferences": {"likes": ["tea"], "dislikes": ["noise"]},
        "shared_secrets": True,
        "social_graph": {"interaction_stats": {"total_chat_days": 8}},
    }


@pytest.mark.asyncio
async def test_provider_exposes_required_read_methods():
    provider = AffinityMemoryProvider(FakeLogic(), FakeDB(), FakeBondCalculator())

    assert (await provider.get_profile_delta("u1", dt.date(2026, 5, 23)))["available"] is False
    assert (await provider.get_interaction_stats("u1", dt.date(2026, 5, 23)))["valid_messages"] == 6
    assert (await provider.get_daily_summary("u1", dt.date(2026, 5, 23)))["summary"] == "用户喜欢茶"
    assert (await provider.get_memory_refs("u1", dt.date(2026, 5, 23)))[0]["memory_id"] == "idx-1"
    assert await provider.get_memory_count("u1") == 1
    snapshot = await provider.get_legacy_bond_snapshot("u1")
    assert snapshot["old_level"] == 3
    assert snapshot["likes_count"] == 1
    assert await provider.get_all_known_user_ids() == ["u1"]
