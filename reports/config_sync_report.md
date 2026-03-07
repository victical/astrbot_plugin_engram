# 配置项一致性自动核对报告

- schema 配置项数：**88**
- 代码读取配置项数：**79**
- 一致（定义且使用）：**79**
- 仅 schema（定义未使用）：**9**
- 仅代码（使用未定义）：**0**

## 1) 定义且使用

| 配置项 | 所属分组 | 代码位置示例 |
|---|---|---|
| `ai_name` | `preset_and_basic` | `core/memory_manager.py:799`, `core/memory_manager.py:931`, `core/memory_manager.py:1126`, ...(+1) |
| `command_prefixes` | `command_filter` | `core/memory_manager.py:234`, `main.py:79` |
| `debug_injection` | `preset_and_basic` | `core/memory_manager.py:1659`, `main.py:235` |
| `embedding_provider` | `embedding_misc` | `core/memory_manager.py:109`, `core/memory_manager.py:144`, `core/memory_manager.py:480` |
| `enable_command_filter` | `command_filter` | `core/memory_manager.py:233`, `main.py:72` |
| `enable_conflict_detection` | `persona` | `services/profile_guardian.py:100` |
| `enable_full_command_detection` | `command_filter` | `main.py:87` |
| `enable_keyword_boost` | `retrieval_ranking` | `core/memory_manager.py:1610` |
| `enable_memory_context_hint` | `retrieval_ranking` | `core/memory_manager.py:1375`, `core/memory_manager.py:1612` |
| `enable_memory_decay` | `decay` | `core/memory_manager.py:1611`, `core/scheduler.py:705` |
| `enable_memory_folding` | `folding` | `core/scheduler.py:39`, `core/scheduler.py:517` |
| `enable_memory_prune` | `decay` | `core/scheduler.py:707` |
| `enable_memory_search_tool` | `tool_search` | `handlers/tool_commands.py:34`, `main.py:316`, `services/injection_strategy.py:200` |
| `enable_memory_topic_cache` | `tool_search` | `services/injection_strategy.py:120`, `services/injection_strategy.py:161` |
| `enable_monthly_folding` | `folding` | `core/scheduler.py:43`, `core/scheduler.py:554` |
| `enable_profile_confidence` | `persona` | `services/profile_guardian.py:98` |
| `enable_strong_evidence_protection` | `persona` | `services/profile_guardian.py:101` |
| `enable_yearly_folding` | `folding` | `core/scheduler.py:47`, `core/scheduler.py:605` |
| `folding_min_samples` | `folding` | `core/memory_manager.py:1203` |
| `full_command_list` | `command_filter` | `main.py:88` |
| `intent_llm_model` | `intent` | `services/intent_classifier.py:230` |
| `intent_min_length` | `intent` | `services/intent_classifier.py:76` |
| `intent_pattern_mode` | `intent` | `services/intent_classifier.py:84` |
| `intent_trigger_score_threshold` | `intent` | `services/intent_classifier.py:210` |
| `intent_weak_triggers` | `intent` | `services/intent_classifier.py:201` |
| `keyword_boost_weight` | `retrieval_ranking` | `core/memory_manager.py:1624` |
| `keyword_ngram_max` | `retrieval_ranking` | `core/memory_manager.py:255`, `core/memory_manager.py:312` |
| `keyword_ngram_min` | `retrieval_ranking` | `core/memory_manager.py:254`, `core/memory_manager.py:311` |
| `list_memory_count` | `preset_and_basic` | `handlers/memory_commands.py:63` |
| `max_history_days` | `archive` | `core/memory_manager.py:748` |
| `memory_context_window` | `retrieval_ranking` | `core/memory_manager.py:1377`, `core/memory_manager.py:1614` |
| `memory_decay_rate` | `decay` | `core/scheduler.py:706` |
| `memory_intent_mode` | `intent` | `services/intent_classifier.py:70` |
| `memory_prune_threshold` | `decay` | `core/scheduler.py:708` |
| `memory_query_max_results` | `retrieval_ranking` | `core/memory_manager.py:1286`, `core/memory_manager.py:1581` |
| `memory_reinforce_bonus` | `decay` | `core/memory_manager.py:1494`, `core/memory_manager.py:1975` |
| `memory_search_tool_max_results` | `tool_search` | `handlers/tool_commands.py:46` |
| `memory_similarity_threshold` | `retrieval_ranking` | `core/memory_manager.py:1608` |
| `memory_tool_hint_min_memories` | `tool_search` | `services/injection_strategy.py:211` |
| `memory_tool_hint_mode` | `tool_search` | `services/injection_strategy.py:203` |
| `memory_topic_cache_max_topics` | `tool_search` | `services/injection_strategy.py:80` |
| `memory_topic_cache_similarity_threshold` | `tool_search` | `services/injection_strategy.py:87` |
| `memory_topic_cache_ttl` | `tool_search` | `services/injection_strategy.py:73` |
| `min_msg_count` | `archive` | `core/memory_manager.py:722`, `core/scheduler.py:171` |
| `min_persona_update_memories` | `persona` | `core/scheduler.py:238` |
| `monthly_folding_day` | `folding` | `core/scheduler.py:424` |
| `monthly_folding_days` | `folding` | `core/scheduler.py:558` |
| `monthly_folding_delay` | `folding` | `core/scheduler.py:559` |
| `monthly_folding_hour` | `folding` | `core/scheduler.py:425` |
| `monthly_folding_jitter` | `folding` | `core/scheduler.py:560` |
| `monthly_folding_min_samples` | `folding` | `core/memory_manager.py:1226` |
| `persona_model` | `persona` | `core/profile_manager.py:258` |
| `persona_update_delay` | `persona` | `core/scheduler.py:240` |
| `persona_update_max_concurrent` | `persona` | `core/scheduler.py:239` |
| `persona_update_prompt` | `persona` | `core/profile_manager.py:248` |
| `pillowmd_style_path` | `embedding_misc` | `profile_renderer.py:76` |
| `private_memory_timeout` | `archive` | `core/memory_manager.py:721`, `core/scheduler.py:162` |
| `profile_confidence_threshold` | `persona` | `services/profile_guardian.py:99` |
| `rank_recency_half_life_days` | `retrieval_ranking` | `core/memory_manager.py:1751` |
| `rank_strategy` | `retrieval_ranking` | `core/memory_manager.py:1619` |
| `rank_weight_activity` | `retrieval_ranking` | `core/memory_manager.py:1636` |
| `rank_weight_keyword` | `retrieval_ranking` | `core/memory_manager.py:1634` |
| `rank_weight_recency` | `retrieval_ranking` | `core/memory_manager.py:1635` |
| `rank_weight_vector` | `retrieval_ranking` | `core/memory_manager.py:1633` |
| `show_relevance_score` | `retrieval_ranking` | `core/memory_manager.py:1374`, `core/memory_manager.py:1609` |
| `summarize_model` | `archive` | `core/memory_manager.py:941`, `core/memory_manager.py:1090`, `services/intent_classifier.py:231` |
| `summarize_prompt` | `archive` | `core/memory_manager.py:930` |
| `weekly_folding_days` | `folding` | `core/scheduler.py:521` |
| `weekly_folding_delay` | `folding` | `core/scheduler.py:522` |
| `weekly_folding_hour` | `folding` | `core/scheduler.py:369` |
| `weekly_folding_jitter` | `folding` | `core/scheduler.py:523` |
| `weekly_folding_weekday` | `folding` | `core/scheduler.py:368` |
| `yearly_folding_day` | `folding` | `core/scheduler.py:471` |
| `yearly_folding_days` | `folding` | `core/scheduler.py:609` |
| `yearly_folding_delay` | `folding` | `core/scheduler.py:610` |
| `yearly_folding_hour` | `folding` | `core/scheduler.py:472` |
| `yearly_folding_jitter` | `folding` | `core/scheduler.py:611` |
| `yearly_folding_min_samples` | `folding` | `core/memory_manager.py:1250` |
| `yearly_folding_month` | `folding` | `core/scheduler.py:470` |

## 2) 仅 schema（定义未使用）

| 配置项 | 所属分组 |
|---|---|
| `config_preset_mode` | `preset_and_basic` |
| `enable_ngram_keyword_rank` | `retrieval_ranking` |
| `max_recent_memories` | `retrieval_ranking` |
| `monthly_folding_model` | `folding` |
| `monthly_folding_prompt` | `folding` |
| `weekly_folding_model` | `folding` |
| `weekly_folding_prompt` | `folding` |
| `yearly_folding_model` | `folding` |
| `yearly_folding_prompt` | `folding` |

## 3) 仅代码（使用未定义）

✅ 无

## 建议

- 对 `仅 schema` 项：确认是否遗留配置，决定补实现或从 schema 移除。
- 对 `仅代码` 项：补入 schema（若需对外配置）或改为内部常量。
- 建议在 CI 中加入该脚本，作为配置一致性守护。
