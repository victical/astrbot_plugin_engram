# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.6.5] - 2026-03-19

### Added
- 支持 **WebUI 控制台**：支持登录访问、主页统计、记忆详情、用户画像与基础管理。

### Changed
- 统一版本号到 `1.6.5`。

## [1.6.0] - 2026-03-16

### Added
- 群聊记忆系统：支持群聊记忆独立存储（SQLite+Chroma）、好友白名单与 LLM 触发落库。
- 群聊记忆指令：新增 `/group_mem_list`、`/group_mem_view`、`/group_mem_search`、`/group_mem_delete`、`/group_mem_delete_all`、`/group_mem_undo`、`/group_mem_force_summarize`。
- 群聊配置项：新增 `enable_group_memory`、`group_memory_only_friends`、`group_memory_min_text_length`、`group_memory_source_type`、`group_memory_store_session_as`。
- 群聊权限配置：新增 `group_memory_private_session_only`（群聊记忆按用户隔离）。
- 群聊私聊融合：新增 `group_memory_allow_private_recall`（群聊检索追加私聊记忆）。
- 好友缓存服务：新增 OneBot 好友列表缓存与 `friend_add` 通知更新。

### Changed
- 群聊 LLM 注入流程与私聊一致，支持用户画像注入与工具检索提示。
- mem_search_tool / mem_get_detail_tool 支持群聊场景检索并回退私聊。
- 群聊注入记忆统一为 `【长期记忆回溯】` 格式，并标记 `【群聊】/【私聊】` 来源。
- 记忆归档中 assistant 名称仅使用 `ai_name` 配置，不再回退“助手”。
- 群聊指令输出替换为 group_ 前缀提示，避免误删私聊记忆。

## [Unreleased]

### Added
- 画像系统升级：新增 `_meta` 证据元数据（`last_seen_at` / `evidence_count` / `evidence_refs`）、画像快照历史与 `/profile rollback` 回滚能力。
- 新增画像证据查询指令 `/profile evidence [top_n]`，可查看字段证据摘要。
- 新增测试：`tests/test_profile_meta.py`、`tests/test_profile_rollback.py`、`tests/test_profile_ttl_decay.py`、`tests/test_profile_commands.py`。

### Changed
- `ProfileGuardian.validate_update()` 返回结构化决策：`accepted_fields` / `rejected_fields` / `pending_fields` / `reasons` / `field_layers`。
- 偏好冲突由“直接丢弃”调整为“进入 pending_proposals 等待后续证据”。
- 画像更新流程改为“两步式”：proposal -> guardian 决策 -> snapshot -> 落盘。
- `likes/dislikes` 增加 TTL 衰减机制（基于证据时间）。
- `/profile show` 支持可选证据摘要渲染（`show_profile_evidence_in_image=false` 默认关闭）。
- 配置与预设联动新增画像策略项：`enable_profile_meta`、`profile_history_limit`、`profile_preference_ttl_days`、`show_profile_evidence_in_image`。

## [1.5.4] - 2026-03-07

### Changed
- 统一删除链路：`delete_memory_by_id` 与 `delete_memory_by_sequence` 复用同一核心逻辑，确保 ID/序号删除行为一致（含撤销历史与原始消息归档状态处理）。
- 检索降级增强：向量检索异常或结果为空时，自动回退到 SQLite 关键词检索（保留时间范围与来源类型过滤）。
- 写入链路解耦：`_summarize_private_chat` 调整为“先落 SQLite 索引与归档，再尝试写入向量”；向量失败时仅进入待补偿队列，不阻断主链路。
- 路由瘦身（第三阶段）：`main.py` 的 `mem_*`、`profile_*`、`engram_force_*`、`rebuild_vector` 指令逻辑下沉到 handlers，`on_private_message` 的 OneBot 同步改为委托 `OneBotSyncHandler`，新增 `MemoryToolHandler` 承接工具检索输出构建，并将时间解析/类型归一化拆分到 `TimeExpressionService`。
- 配置一致性核对工具：新增 `tools/check_config_sync.py`，自动生成 `reports/config_sync_report.md`（schema 配置项 ↔ 代码读取点）。
- 工具检索回归：新增 `test_mem_search_tool_works_with_fallback_memories`，覆盖“向量不可用→fallback 后 `mem_search_tool` 仍可返回结果”。
- 测试入口标准化：新增 `tests/conftest.py` 统一导入路径与工作目录，支持在仓库内直接执行 `pytest -q`。
- 新增最小 CI：GitHub Actions 在 `beta/main` 的 push 与 PR 自动执行 pytest（Python 3.11/3.12）。
- 版本号统一：`main.py @register`、`metadata.yaml`、`CHANGELOG.md` 统一到 `1.5.4`。

### Added
- 新增 `DatabaseManager.search_memory_indexes_by_keywords()` 作为向量不可用时的数据库兜底检索接口。
- 新增向量补偿队列：`MemoryManager._pending_vector_jobs`（内存态），用于记录向量写入失败索引并在重建成功后清理。
- 新增测试：`tests/test_memory_delete_by_id.py`、`tests/test_memory_fallback.py`、`tests/test_summarize_persistence.py`（覆盖 ID 删除撤销链路、检索降级与“向量失败不丢总结”）。

## [1.4.3] - 2026-02-17

### Added
- **画像幻觉阻断机制**：新增 `ProfileGuardian` 服务，防止 LLM 产生的错误信息污染用户画像
  - 置信度机制：新属性作为提案暂存，需多次确认才能转正
  - 冲突检测：检测新旧属性矛盾（如"喜欢猫" vs "猫毛过敏"），冲突时保留旧值
  - 强证据保护：basic_info 核心字段（性别、年龄、所在地、职业）需记忆中有明确陈述才能修改
  - 新增配置项：`enable_profile_confidence`、`profile_confidence_threshold`、`enable_conflict_detection`、`enable_strong_evidence_protection`

## [1.4.2] - 2026-02-17

### Changed
- **配置重组**：`_conf_schema.json` 按功能类别重新组织配置项
  - 基础设置：ai_name、debug_injection
  - 记忆归档：private_memory_timeout、min_msg_count、max_history_days、summarize_model、summarize_prompt
  - 记忆检索：max_recent_memories、memory_similarity_threshold、enable_keyword_boost、keyword_boost_weight、show_relevance_score、enable_memory_context_hint
  - 意图过滤：memory_intent_mode、intent_llm_model、intent_min_length
  - 记忆衰减与修剪：enable_memory_decay、memory_decay_rate、memory_reinforce_bonus、enable_memory_prune、memory_prune_threshold
  - 用户画像：persona_model、persona_update_prompt、min_persona_update_memories、persona_update_max_concurrent、persona_update_delay
  - 指令过滤：enable_command_filter、command_prefixes、enable_full_command_detection、full_command_list
  - 其他：embedding_provider、pillowmd_style_path、list_memory_count
- 修复配置项重复问题（memory_intent_mode、intent_llm_model、intent_min_length）

## [1.4.1] - 2026-02-17

### Added
- **仿生遗忘机制**：active_score 衰减 + 召回增强 + 冷记忆修剪
  - Decay：每日凌晨 01:00 全局衰减所有记忆的 active_score（默认 -1/天）
  - Reinforce：被成功召回的记忆获得 active_score 加分（默认 +20）
  - Prune：active_score ≤ 0 的冷记忆从 ChromaDB 物理删除，SQLite 保留用于导出和考古
  - 新增配置项：`enable_memory_decay`、`memory_decay_rate`、`memory_reinforce_bonus`、`enable_memory_prune`、`memory_prune_threshold`
- 新增 `db_manager.get_cold_memory_ids()` 方法
- 新增 `MemoryScheduler.daily_memory_maintenance()` 后台任务

## [1.4.0] - 2026-02-17

### Added
- **意图过滤器**：新增 `IntentClassifier` 服务，支持三种检索触发模式
  - `disabled`：每条消息都触发检索（向后兼容）
  - `keyword`：仅当消息包含回忆类关键词时检索（默认，零成本）
  - `llm`：调用小模型判断是否需要检索（高精度，有少量 Token 消耗）
  - 新增配置项：`memory_intent_mode`、`intent_llm_model`、`intent_min_length`

### Changed
- **RRF 融合算法**：`retrieve_memories()` 使用 Reciprocal Rank Fusion 替代线性加权平均
  - 向量语义排名与关键词排名独立计算后按倒数融合（k=60）
  - BM25 风格关键词评分：TF 饱和 + 文档长度归一化 + 长词权重（短词保底 1.0）
  - 正则表达式分词替换多轮 `replace`，性能提升
  - 相关性百分比增加质量惩罚因子，避免误导性 100% 显示

### Fixed
- 修复 `intent_min_length` 配置项缺失问题
- 修复空字符串 `int("")` 转换崩溃（防御性转换）
- 修复 LLM 输出解析脆弱性（兼容中英文多种回答格式）

## [1.3.1] - 2026-02-09

### Changed
- **统一计算逻辑**：`profile_renderer.py` 使用 `BondCalculator` 替代本地重复方法
  - 删除 `_calculate_profile_depth()`, `_calculate_bond_level()`, `_get_next_level_hints()` 冗余代码
  - 减少约 220 行重复代码

- **重构 main.py 为纯路由层**：
  - 使用 `MemoryScheduler` 替代 main.py 中的调度方法
  - 删除 `_daily_persona_scheduler()`, `background_worker()`, `_calculate_next_check_time()` 冗余代码
  - 初始化 `MemoryCommandHandler`, `ProfileCommandHandler`, `OneBotSyncHandler`, `LLMContextInjector`
  - main.py 现在仅负责装饰器绑定和参数解析，业务逻辑委托给 handlers/
  - 减少约 100 行重复代码

## [1.3.0] - 2026-02-09

### Changed
- **架构重构**：使用 `MemoryFacade` 门面模式统一接口
  - 新增 `core/memory_facade.py` 作为统一入口
  - `MemoryManager` 和 `ProfileManager` 职责分离更清晰
- 版本号从 1.2.5 升级到 1.3.0

### Fixed
- 修复 `core/memory_manager.py` 中的导入路径错误（`from .db_manager` → `from ..db_manager`）

### Internal
- 代码结构优化，遵循单一职责原则
- 新架构与旧版 `MemoryLogic` 接口完全兼容，无需修改任何调用代码

## [1.2.5] - Previous Release

- 仿生双轨记忆系统初始架构
- 双轨记忆（L2叙事归档 + 原文指针回溯）
- 深度用户画像（7级羁绊系统）
- ChromaDB 向量检索
- 多格式数据导出（JSONL/Alpaca/ShareGPT）
