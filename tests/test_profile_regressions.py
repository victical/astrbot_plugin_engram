import copy
import asyncio
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from astrbot_plugin_engram.core.profile_manager import ProfileManager
from astrbot_plugin_engram.profile_renderer import ProfileRenderer
from astrbot_plugin_engram.services.profile_guardian import ProfileGuardian


def test_profile_guardian_does_not_mutate_new_profile_social_graph():
    guardian = ProfileGuardian(config={})
    current = {
        "basic_info": {},
        "attributes": {},
        "preferences": {},
        "social_graph": {"interaction_stats": {"total_valid_chats": 7}},
        "pending_proposals": [],
    }
    new_profile = {
        "basic_info": {},
        "attributes": {},
        "preferences": {},
        "social_graph": {"relationship_status": "熟悉"},
    }
    original = copy.deepcopy(new_profile)

    validated, _, _ = guardian.validate_update(current, new_profile, "")

    assert new_profile == original
    assert validated["social_graph"]["interaction_stats"] == {"total_valid_chats": 7}


def test_profile_guardian_ignores_non_numeric_confidence_values():
    guardian = ProfileGuardian(config={"profile_confidence_threshold": 2})
    current = {
        "basic_info": {},
        "attributes": {},
        "preferences": {},
        "social_graph": {},
        "pending_proposals": [
            {"category": "hobbies", "value": "摄影", "confidence": "high"}
        ],
    }
    new_profile = {
        "basic_info": {},
        "attributes": {"hobbies": ["摄影"]},
        "preferences": {},
        "social_graph": {},
    }

    validated, _, _ = guardian.validate_update(current, new_profile, "我喜欢摄影")

    assert validated["pending_proposals"][0]["confidence"] == 1


def test_profile_manager_writes_profile_with_atomic_replace(tmp_path, monkeypatch):
    manager = ProfileManager(
        context=None,
        config={},
        data_dir=str(tmp_path),
        executor=None,
        db_manager=None,
    )
    real_replace = os.replace
    replace_calls = []

    def tracking_replace(src, dst):
        replace_calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr("astrbot_plugin_engram.core.profile_manager.os.replace", tracking_replace)

    profile = asyncio.run(
        manager.update_user_profile("u1", {"basic_info": {"nickname": "Alice"}})
    )

    assert profile["basic_info"]["nickname"] == "Alice"
    assert replace_calls
    assert replace_calls[-1][1] == manager._get_profile_path("u1")
    assert not os.path.exists(replace_calls[-1][0])


def test_profile_renderer_closes_cached_avatar_file(tmp_path, monkeypatch):
    renderer = ProfileRenderer(config={}, plugin_data_dir=str(tmp_path))
    cache_file = pathlib.Path(renderer.avatar_cache_dir) / "u1.png"
    cache_file.write_bytes(b"x" * 2048)
    opened = []

    class FakeImage:
        def __init__(self):
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self.closed = True

        def load(self):
            return None

        def convert(self, mode):
            return {"mode": mode}

    def fake_open(path):
        img = FakeImage()
        opened.append(img)
        return img

    monkeypatch.setattr("astrbot_plugin_engram.profile_renderer.Image.open", fake_open)

    avatar = asyncio.run(renderer._get_cached_avatar("u1", "https://example.test/avatar.png"))

    assert avatar == {"mode": "RGBA"}
    assert opened and opened[0].closed is True
