import json

from astrbot_plugin_engram.services.config_preset import ConfigPresetService


def test_apply_balanced_preset_from_flat_config():
    config = {
        "config_preset_mode": "balanced",
        "memory_query_max_results": 10,
        "memory_intent_mode": "disabled",
    }

    applied = ConfigPresetService(config).apply()

    assert applied["memory_query_max_results"] == 60
    assert applied["memory_intent_mode"] == "keyword"
    assert applied["config_preset_mode"] == "balanced"


def test_custom_mode_keeps_manual_values():
    config = {
        "config_preset_mode": "custom",
        "memory_query_max_results": 33,
        "memory_intent_mode": "llm",
    }

    applied = ConfigPresetService(config).apply()

    assert applied["memory_query_max_results"] == 33
    assert applied["memory_intent_mode"] == "llm"


def test_apply_preset_from_grouped_runtime_config_values():
    grouped_runtime = {
        "preset_and_basic": {
            "config_preset_mode": "aggressive",
            "ai_name": "助手",
        },
        "retrieval_ranking": {
            "memory_query_max_results": 20,
            "rank_strategy": "rrf",
        },
        "tool_search": {
            "memory_tool_hint_mode": "never",
        },
    }

    applied = ConfigPresetService(grouped_runtime).apply()

    assert applied["config_preset_mode"] == "aggressive"
    assert applied["memory_query_max_results"] == 120
    assert applied["rank_strategy"] == "hybrid"
    assert applied["memory_tool_hint_mode"] == "always"


def test_apply_preset_from_grouped_schema_like_items_shape():
    grouped_schema_like = {
        "preset_and_basic": {
            "items": {
                "config_preset_mode": "stable",
                "ai_name": {"type": "string", "default": "助手"},
            }
        },
        "retrieval_ranking": {
            "items": {
                "memory_query_max_results": 999,
            }
        },
    }

    applied = ConfigPresetService(grouped_schema_like).apply()

    assert applied["config_preset_mode"] == "stable"
    assert applied["memory_query_max_results"] == 40


def test_grouped_schema_contains_preset_field_and_sections():
    with open("astrbot_plugin_engram/_conf_schema.json", "r", encoding="utf-8") as f:
        schema = json.load(f)

    assert "preset_and_basic" in schema
    assert schema["preset_and_basic"]["type"] == "object"
    assert "config_preset_mode" in schema["preset_and_basic"]["items"]
    assert "retrieval_ranking" in schema
    assert "tool_search" in schema
    assert "embedding_misc" in schema
