from concurrent.futures import ThreadPoolExecutor

import pytest

from astrbot_plugin_engram.handlers.profile_commands import ProfileCommandHandler


class _DummyProfileManager:
    def __init__(self):
        self.rollback_called = None

    async def rollback_user_profile(self, user_id, steps=1):
        self.rollback_called = (user_id, steps)
        if steps == 99:
            return {"success": False, "message": "无可回滚的历史版本", "remaining": 0}
        return {"success": True, "rolled_back_steps": steps, "remaining": 2}

    async def get_profile_evidence_summary(self, user_id, top_n=8):
        if top_n == 1:
            return []
        return [
            {
                "field": "basic_info.job",
                "evidence_count": 3,
                "last_seen_at": "2026-03-08T00:00:00",
                "latest_evidence": "memory_index:abc123",
            }
        ]


@pytest.mark.asyncio
async def test_handle_profile_rollback_success():
    handler = ProfileCommandHandler(
        config={},
        profile_manager=_DummyProfileManager(),
        db_manager=None,
        profile_renderer=None,
        executor=None,
    )

    out = await handler.handle_profile_rollback("u1", "2")
    assert "已回滚 2 步" in out


@pytest.mark.asyncio
async def test_handle_profile_rollback_invalid_param():
    handler = ProfileCommandHandler(
        config={},
        profile_manager=_DummyProfileManager(),
        db_manager=None,
        profile_renderer=None,
        executor=None,
    )

    out = await handler.handle_profile_rollback("u1", "abc")
    assert "steps 必须是正整数" in out


@pytest.mark.asyncio
async def test_handle_profile_evidence_empty_and_non_empty():
    handler = ProfileCommandHandler(
        config={},
        profile_manager=_DummyProfileManager(),
        db_manager=None,
        profile_renderer=None,
        executor=None,
    )

    empty_out = await handler.handle_profile_evidence("u1", "1")
    assert "暂无可展示" in empty_out

    out = await handler.handle_profile_evidence("u1", "8")
    assert "画像证据摘要" in out
    assert "basic_info.job" in out


@pytest.mark.asyncio
async def test_handle_profile_show_passes_evidence_summary_when_enabled():
    class _ProfileForShow(_DummyProfileManager):
        async def get_user_profile(self, user_id):
            return {"basic_info": {"qq_id": user_id, "nickname": "u1"}}

        async def get_profile_evidence_summary(self, user_id, top_n=8):
            return [{"field": "basic_info.job", "evidence_count": 2}]

    class _DummyDb:
        @staticmethod
        def get_memory_list(user_id, limit):
            return [1, 2]

    class _DummyRenderer:
        def __init__(self):
            self.received = None

        async def render(self, user_id, profile, memory_count=0, evidence_summary=None):
            self.received = {
                "user_id": user_id,
                "memory_count": memory_count,
                "evidence_summary": evidence_summary,
            }
            return b"img"

    renderer = _DummyRenderer()
    handler = ProfileCommandHandler(
        config={"show_profile_evidence_in_image": True},
        profile_manager=_ProfileForShow(),
        db_manager=_DummyDb(),
        profile_renderer=renderer,
        executor=ThreadPoolExecutor(max_workers=1),
    )

    ok, result = await handler.handle_profile_show("u1")
    assert ok is True
    assert result == b"img"
    assert renderer.received["memory_count"] == 2
    assert renderer.received["evidence_summary"]
