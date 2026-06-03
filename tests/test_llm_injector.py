import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services.llm_injector import LLMContextInjector


PROFILE_BLOCK = "【用户档案】\n- 称呼: Alice"
MEMORY_BLOCK = "【长期记忆回溯】：\n喜欢 Python\n"


def test_inject_context_defaults_to_system_prompt():
    req = SimpleNamespace(system_prompt="base", prompt="hello")

    LLMContextInjector().inject_context(req, PROFILE_BLOCK, MEMORY_BLOCK)

    assert req.system_prompt.startswith("base")
    assert PROFILE_BLOCK in req.system_prompt
    assert MEMORY_BLOCK in req.system_prompt
    assert req.prompt == "hello"


def test_inject_context_system_target_uses_existing_fallback():
    req = SimpleNamespace(system_prompt="", prompt="hello")

    LLMContextInjector().inject_context(req, PROFILE_BLOCK, MEMORY_BLOCK, target="system")

    assert req.system_prompt.startswith("你是一个有记忆的助手。以下是关于用户的信息：")
    assert PROFILE_BLOCK in req.system_prompt
    assert MEMORY_BLOCK in req.system_prompt
    assert req.prompt == "hello"


def test_inject_context_user_target_appends_to_prompt():
    req = SimpleNamespace(system_prompt="base", prompt="hello")

    LLMContextInjector().inject_context(req, PROFILE_BLOCK, MEMORY_BLOCK, target="user")

    assert req.system_prompt == "base"
    assert req.prompt.startswith("hello")
    assert PROFILE_BLOCK in req.prompt
    assert MEMORY_BLOCK in req.prompt


def test_inject_context_user_target_handles_none_prompt():
    req = SimpleNamespace(system_prompt="base", prompt=None)

    LLMContextInjector().inject_context(req, PROFILE_BLOCK, MEMORY_BLOCK, target="user")

    assert req.system_prompt == "base"
    assert req.prompt.startswith(PROFILE_BLOCK)
    assert MEMORY_BLOCK in req.prompt


def test_inject_context_user_target_falls_back_to_user_prompt():
    req = SimpleNamespace(system_prompt="base", user_prompt="hello")

    LLMContextInjector().inject_context(req, PROFILE_BLOCK, MEMORY_BLOCK, target="user")

    assert req.system_prompt == "base"
    assert req.user_prompt.startswith("hello")
    assert PROFILE_BLOCK in req.user_prompt
    assert MEMORY_BLOCK in req.user_prompt


def test_inject_context_invalid_target_falls_back_to_system_prompt():
    req = SimpleNamespace(system_prompt="base", prompt="hello")

    LLMContextInjector().inject_context(req, PROFILE_BLOCK, MEMORY_BLOCK, target="bad-value")

    assert req.system_prompt.startswith("base")
    assert PROFILE_BLOCK in req.system_prompt
    assert MEMORY_BLOCK in req.system_prompt
    assert req.prompt == "hello"


def test_build_profile_block_omits_affinity_when_disabled():
    profile = {
        "basic_info": {"nickname": "Alice", "qq_id": "10001"},
        "attributes": {},
        "preferences": {},
        "dev_metadata": {},
        "social_graph": {"relationship_status": "知己"},
    }

    block = LLMContextInjector(config={"enable_profile_affinity": False}).build_profile_block(profile)

    assert "当前羁绊" not in block
    assert "知己" not in block


def test_build_compact_profile_block_omits_relationship_when_affinity_disabled():
    profile = {
        "basic_info": {"nickname": "Bob", "qq_id": "10002", "job": "程序员"},
        "attributes": {},
        "preferences": {},
        "social_graph": {"relationship_status": "老朋友"},
    }

    block = LLMContextInjector(config={"enable_profile_affinity": False}).build_compact_profile_block(profile)

    assert block == "Bob (QQ: 10002)：程序员"
    assert "老朋友" not in block
