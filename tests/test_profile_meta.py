from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from astrbot_plugin_engram.core.profile_manager import ProfileManager


class _DummyProvider:
    def __init__(self, completion_text: str):
        self._completion_text = completion_text

    async def text_chat(self, prompt: str):
        return SimpleNamespace(completion_text=self._completion_text)


class _DummyContext:
    def __init__(self, provider):
        self._provider = provider

    def get_provider_by_id(self, provider_id):
        return None

    def get_using_provider(self):
        return self._provider


class _DummyDB:
    def __init__(self, memories):
        self._memories = memories

    def get_memories_since(self, user_id, since_time):
        return self._memories

    def get_memories_in_range(self, user_id, start_time, end_time):
        return self._memories


@pytest.mark.asyncio
async def test_profile_update_writes_meta_fields(tmp_path):
    data_dir = tmp_path / "engram_profile_meta"
    data_dir.mkdir(parents=True, exist_ok=True)

    llm_json = """{
      "basic_info": {"job": "学生"},
      "attributes": {"personality_tags": [], "hobbies": ["跑步"], "skills": []},
      "preferences": {
        "favorite_foods": [],
        "favorite_items": [],
        "favorite_activities": [],
        "likes": ["猫"],
        "dislikes": []
      },
      "social_graph": {"relationship_status": "朋友", "important_people": []},
      "dev_metadata": {"os": [], "tech_stack": []},
      "shared_secrets": false
    }"""

    memories = [SimpleNamespace(summary="我是学生，我喜欢猫", index_id="idx-meta-1")]

    config = {
        "persona_update_prompt": "{{current_persona}}\n{{memory_texts}}",
        "enable_profile_meta": True,
        "profile_confidence_threshold": 2,
    }

    provider = _DummyProvider(llm_json)
    context = _DummyContext(provider)
    db = _DummyDB(memories)

    with ThreadPoolExecutor(max_workers=1) as executor:
        manager = ProfileManager(
            context=context,
            config=config,
            data_dir=str(data_dir),
            executor=executor,
            db_manager=db,
        )

        await manager.update_persona_daily("u_meta")
        profile = await manager.get_user_profile("u_meta")

    assert "_meta" in profile
    fields_meta = profile["_meta"].get("fields", {})
    assert "basic_info.job" in fields_meta
    assert fields_meta["basic_info.job"].get("last_seen_at")
    assert fields_meta["basic_info.job"].get("evidence_count", 0) >= 1

    # likes 属于可直接接受项，也应进入证据元数据
    assert "preferences.likes.猫" in fields_meta

    # 属性字段默认走置信度提案，不应直接落正式 attributes
    assert "跑步" not in profile.get("attributes", {}).get("hobbies", [])
    assert any(
        p.get("category") == "hobbies" and p.get("value") == "跑步"
        for p in profile.get("pending_proposals", [])
    )
