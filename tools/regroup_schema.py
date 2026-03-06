import json
from pathlib import Path

SCHEMA_PATH = Path("astrbot_plugin_engram/_conf_schema.json")

PRESET_FIELD = {
    "description": "配置预设模式",
    "type": "string",
    "options": ["stable", "balanced", "aggressive", "custom"],
    "labels": ["稳定（低抖动）", "均衡（推荐）", "激进（高召回）", "自定义（完全手调）"],
    "default": "balanced",
    "hint": "一键应用记忆策略参数组合。custom=不覆盖你手动填写的细项；其余预设会覆盖相关检索/提示/衰减参数。",
}

GROUPS = [
    (
        "preset_and_basic",
        "预设与基础",
        "快速选择整体行为并配置基础信息。",
        [
            "config_preset_mode",
            "ai_name",
            "debug_injection",
            "list_memory_count",
        ],
    ),
    (
        "archive",
        "归档与总结",
        "原始消息归档与日级总结生成相关配置。",
        [
            "private_memory_timeout",
            "min_msg_count",
            "max_history_days",
            "summarize_model",
            "summarize_prompt",
        ],
    ),
    (
        "retrieval_ranking",
        "检索与排序",
        "长期记忆召回、关键词增强与排序融合策略。",
        [
            "max_recent_memories",
            "memory_similarity_threshold",
            "enable_keyword_boost",
            "enable_ngram_keyword_rank",
            "keyword_ngram_min",
            "keyword_ngram_max",
            "keyword_boost_weight",
            "rank_strategy",
            "rank_weight_vector",
            "rank_weight_keyword",
            "rank_weight_recency",
            "rank_weight_activity",
            "rank_recency_half_life_days",
            "show_relevance_score",
            "enable_memory_context_hint",
            "memory_context_window",
            "memory_query_max_results",
        ],
    ),
    (
        "tool_search",
        "工具检索与话题缓存",
        "工具化检索提示、证据策略与同话题缓存复用。",
        [
            "enable_memory_search_tool",
            "memory_search_tool_max_results",
            "memory_tool_hint_mode",
            "memory_tool_hint_min_memories",
            "enable_memory_topic_cache",
            "memory_topic_cache_ttl",
            "memory_topic_cache_max_topics",
            "memory_topic_cache_similarity_threshold",
        ],
    ),
    (
        "intent",
        "意图判定",
        "控制何时触发长期记忆检索。",
        [
            "memory_intent_mode",
            "intent_llm_model",
            "intent_min_length",
            "intent_weak_triggers",
            "intent_pattern_mode",
            "intent_trigger_score_threshold",
        ],
    ),
    (
        "decay",
        "衰减与修剪",
        "活跃度衰减、召回增强与冷记忆修剪策略。",
        [
            "enable_memory_decay",
            "memory_decay_rate",
            "memory_reinforce_bonus",
            "enable_memory_prune",
            "memory_prune_threshold",
        ],
    ),
    (
        "folding",
        "多级折叠总结",
        "周/月/年多层摘要折叠调度与提示词。",
        [
            "enable_memory_folding",
            "weekly_folding_days",
            "folding_min_samples",
            "weekly_folding_weekday",
            "weekly_folding_hour",
            "weekly_folding_delay",
            "weekly_folding_jitter",
            "weekly_folding_model",
            "weekly_folding_prompt",
            "enable_monthly_folding",
            "monthly_folding_days",
            "monthly_folding_min_samples",
            "monthly_folding_day",
            "monthly_folding_hour",
            "monthly_folding_delay",
            "monthly_folding_jitter",
            "monthly_folding_model",
            "monthly_folding_prompt",
            "enable_yearly_folding",
            "yearly_folding_days",
            "yearly_folding_min_samples",
            "yearly_folding_month",
            "yearly_folding_day",
            "yearly_folding_hour",
            "yearly_folding_delay",
            "yearly_folding_jitter",
            "yearly_folding_model",
            "yearly_folding_prompt",
        ],
    ),
    (
        "persona",
        "画像更新与保护",
        "用户画像更新策略、并发控制与防幻觉保护。",
        [
            "persona_model",
            "persona_update_prompt",
            "min_persona_update_memories",
            "persona_update_max_concurrent",
            "persona_update_delay",
            "enable_profile_confidence",
            "profile_confidence_threshold",
            "enable_conflict_detection",
            "enable_strong_evidence_protection",
        ],
    ),
    (
        "command_filter",
        "指令过滤",
        "过滤指令消息，避免污染记忆。",
        [
            "enable_command_filter",
            "command_prefixes",
            "enable_full_command_detection",
            "full_command_list",
        ],
    ),
    (
        "embedding_misc",
        "向量与渲染",
        "嵌入模型和渲染样式等杂项配置。",
        [
            "embedding_provider",
            "pillowmd_style_path",
        ],
    ),
]


def main() -> None:
    data = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("schema root must be object")

    grouped = {}
    used = set()

    for group_key, desc, hint, keys in GROUPS:
        items = {}
        for key in keys:
            if key == "config_preset_mode":
                items[key] = PRESET_FIELD
                continue
            if key not in data:
                raise KeyError(f"missing source key: {key}")
            items[key] = data[key]
            used.add(key)

        grouped[group_key] = {
            "description": desc,
            "type": "object",
            "hint": hint,
            "items": items,
        }

    remaining = [k for k in data.keys() if k not in used]
    if remaining:
        raise RuntimeError(f"unmapped keys: {remaining}")

    SCHEMA_PATH.write_text(json.dumps(grouped, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"grouped schema written: {SCHEMA_PATH}")


if __name__ == "__main__":
    main()
