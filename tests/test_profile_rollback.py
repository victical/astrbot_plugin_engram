from concurrent.futures import ThreadPoolExecutor

import pytest

from astrbot_plugin_engram.core.profile_manager import ProfileManager


class _DummyContext:
    def get_provider_by_id(self, provider_id):
        return None

    def get_using_provider(self):
        return None


class _DummyDB:
    def get_memories_since(self, user_id, since_time):
        return []

    def get_memories_in_range(self, user_id, start_time, end_time):
        return []


@pytest.mark.asyncio
async def test_profile_rollback_restores_previous_snapshot(tmp_path):
    data_dir = tmp_path / "engram_profile_rollback"
    data_dir.mkdir(parents=True, exist_ok=True)

    with ThreadPoolExecutor(max_workers=1) as executor:
        manager = ProfileManager(
            context=_DummyContext(),
            config={"profile_history_limit": 5},
            data_dir=str(data_dir),
            executor=executor,
            db_manager=_DummyDB(),
        )

        user_id = "u_rb"

        original = await manager.get_user_profile(user_id)
        manager._snapshot_profile(user_id, original)

        await manager.update_user_profile(user_id, {"basic_info": {"job": "学生"}})
        v1 = await manager.get_user_profile(user_id)
        manager._snapshot_profile(user_id, v1)

        await manager.update_user_profile(user_id, {"basic_info": {"job": "老师"}})
        v2 = await manager.get_user_profile(user_id)
        assert v2["basic_info"]["job"] == "老师"

        result_1 = await manager.rollback_user_profile(user_id, steps=1)
        assert result_1["success"] is True
        rolled_1 = await manager.get_user_profile(user_id)
        assert rolled_1["basic_info"]["job"] == "学生"

        result_2 = await manager.rollback_user_profile(user_id, steps=1)
        assert result_2["success"] is True
        rolled_2 = await manager.get_user_profile(user_id)
        assert rolled_2["basic_info"]["job"] == "未知"
