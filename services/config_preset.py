"""
配置预设服务

提供稳定 / 均衡 / 激进三套参数预设，降低大规模配置调优门槛。
"""

from typing import Dict, Any

from astrbot.api import logger


class ConfigPresetService:
    """配置预设服务。"""

    PRESETS: Dict[str, Dict[str, Any]] = {
        # 偏稳：更少漂移、更保守触发
        "stable": {
            "memory_intent_mode": "keyword",
            "max_recent_memories": 3,
            "memory_query_max_results": 40,
            "memory_similarity_threshold": 1.3,
            "keyword_boost_weight": "0.5",
            "rank_strategy": "rrf",
            "memory_context_window": 1,
            "enable_memory_topic_cache": True,
            "memory_topic_cache_ttl": 180,
            "memory_topic_cache_similarity_threshold": 0.28,
            "memory_tool_hint_mode": "on_insufficient_evidence",
            "memory_tool_hint_min_memories": 1,
            "enable_memory_decay": True,
            "memory_decay_rate": 1,
            "memory_reinforce_bonus": 15,
            "enable_memory_prune": True,
            "memory_prune_threshold": 0,
            "enable_profile_meta": True,
            "profile_history_limit": 8,
            "profile_preference_ttl_days": 60,
            "profile_confidence_threshold": 3,
            "show_profile_evidence_in_image": False,
        },
        # 均衡：默认推荐
        "balanced": {
            "memory_intent_mode": "keyword",
            "max_recent_memories": 3,
            "memory_query_max_results": 60,
            "memory_similarity_threshold": 1.5,
            "keyword_boost_weight": "0.5",
            "rank_strategy": "rrf",
            "memory_context_window": 2,
            "enable_memory_topic_cache": True,
            "memory_topic_cache_ttl": 120,
            "memory_topic_cache_similarity_threshold": 0.25,
            "memory_tool_hint_mode": "on_insufficient_evidence",
            "memory_tool_hint_min_memories": 1,
            "enable_memory_decay": True,
            "memory_decay_rate": 1,
            "memory_reinforce_bonus": 20,
            "enable_memory_prune": True,
            "memory_prune_threshold": 0,
            "enable_profile_meta": True,
            "profile_history_limit": 5,
            "profile_preference_ttl_days": 90,
            "profile_confidence_threshold": 2,
            "show_profile_evidence_in_image": False,
        },
        # 偏激进：更高召回和更强探索
        "aggressive": {
            "memory_intent_mode": "disabled",
            "max_recent_memories": 5,
            "memory_query_max_results": 120,
            "memory_similarity_threshold": 1.9,
            "keyword_boost_weight": "0.7",
            "rank_strategy": "hybrid",
            "memory_context_window": 3,
            "enable_memory_topic_cache": True,
            "memory_topic_cache_ttl": 90,
            "memory_topic_cache_similarity_threshold": 0.2,
            "memory_tool_hint_mode": "always",
            "memory_tool_hint_min_memories": 2,
            "enable_memory_decay": True,
            "memory_decay_rate": 1,
            "memory_reinforce_bonus": 25,
            "enable_memory_prune": False,
            "enable_profile_meta": True,
            "profile_history_limit": 3,
            "profile_preference_ttl_days": 120,
            "profile_confidence_threshold": 2,
            "show_profile_evidence_in_image": False,
        },
    }

    def __init__(self, config: Dict[str, Any] = None):
        self.config = dict(config or {})

    def _flatten_grouped_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """兼容 object 分组 schema：将分组值拍平成一层键值。"""
        if not isinstance(config, dict) or not config:
            return {}

        grouped_markers = {
            "preset_and_basic", "archive", "retrieval_ranking", "tool_search",
            "intent", "decay", "folding", "persona", "command_filter",
            "embedding_misc"
        }
        if not any(k in config for k in grouped_markers):
            return dict(config)

        flattened: Dict[str, Any] = {}
        for key, value in config.items():
            if key in grouped_markers and isinstance(value, dict):
                # 支持两种输入形态：
                # 1) 运行时分组值：{"preset_and_basic": {"config_preset_mode": "balanced", ...}}
                # 2) 容错 schema 形态：{"preset_and_basic": {"items": {...}}}
                nested = value.get("items") if isinstance(value.get("items"), dict) else value
                flattened.update(nested)
            else:
                # 保留非分组字段，避免信息丢失
                flattened[key] = value
        return flattened

    def apply(self) -> Dict[str, Any]:
        """应用预设，返回合并后的配置（预设值覆盖同名键）。"""
        base_config = self._flatten_grouped_config(self.config)

        mode = str(base_config.get("config_preset_mode", "balanced")).strip().lower()
        if mode in {"", "custom", "manual"}:
            return dict(base_config)

        if mode not in self.PRESETS:
            logger.warning("Engram：未知配置预设模式 config_preset_mode=%s，已回退到 balanced", mode)
            mode = "balanced"

        merged = dict(base_config)
        merged.update(self.PRESETS[mode])

        logger.info("Engram：已应用配置预设 mode=%s（覆盖 %d 个参数）", mode, len(self.PRESETS[mode]))
        return merged
