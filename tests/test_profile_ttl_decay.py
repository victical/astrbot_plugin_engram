import datetime
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
async def test_decay_stale_preferences_removes_expired_like_dislike(tmp_path):
    data_dir = tmp_path / "engram_profile_ttl"
    data_dir.mkdir(parents=True, exist_ok=True)

    with ThreadPoolExecutor(max_workers=1) as executor:
        manager = ProfileManager(
            context=_DummyContext(),
            config={"profile_preference_ttl_days": 90},
            data_dir=str(data_dir),
            executor=executor,
            db_manager=_DummyDB(),
        )

        now = datetime.datetime.now()
        stale_time = (now - datetime.timedelta(days=120)).isoformat()
        fresh_time = (now - datetime.timedelta(days=10)).isoformat()

        profile = {
            "preferences": {
                "likes": ["猫", "咖啡"],
                "dislikes": ["香菜", "辣椒"],
                "favorite_foods": [],
                "favorite_items": [],
                "favorite_activities": [],
            },
            "_meta": {
                "fields": {
                    "preferences.likes.猫": {"last_seen_at": stale_time, "evidence_count": 2},
                    "preferences.likes.咖啡": {"last_seen_at": fresh_time, "evidence_count": 1},
                    "preferences.dislikes.香菜": {"last_seen_at": stale_time, "evidence_count": 1},
                    "preferences.dislikes.辣椒": {"last_seen_at": fresh_time, "evidence_count": 1},
                }
            }
        }

        decayed = manager._decay_stale_preferences(profile, now)

        assert decayed["preferences"]["likes"] == ["咖啡"]
        assert decayed["preferences"]["dislikes"] == ["辣椒"]
        assert "preferences.likes.猫" not in decayed["_meta"]["fields"]
        assert "preferences.dislikes.香菜" not in decayed["_meta"]["fields"]
