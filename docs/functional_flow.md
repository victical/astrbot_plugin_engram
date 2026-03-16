# astrbot_plugin_engram 功能流程文档

> 基于项目源码梳理的运行流程与模块职责（非微服务架构，单插件内聚）。

## 1. 架构总览

- **入口与路由**：`main.py`（AstrBot 插件注册 + 事件/命令路由）
- **核心门面**：`core/memory_facade.py`（统一对外 API，组合 MemoryManager + ProfileManager）
- **记忆核心**：`core/memory_manager.py`（归档、检索、向量库、删除/撤销、导出）
- **画像核心**：`core/profile_manager.py`（画像 CRUD、每日更新、证据/衰减）
- **调度器**：`core/scheduler.py`（后台归档、每日画像、折叠总结、记忆维护）
- **命令处理器**：`handlers/*.py`（mem_* / profile_* / 工具检索 / OneBot 同步）
- **服务层**：`services/*.py`（注入、意图判定、话题缓存、时间解析、画像防护、配置预设）
- **存储层**：`db_manager.py`（SQLite / Peewee 模型 + 稳定接口层）
- **渲染层**：`profile_renderer.py`（画像图片渲染）

## 2. 数据与存储

- **SQLite**：`engram_memories.db`
  - `RawMemory`：原始对话（用户/助手）
  - `MemoryIndex`：长期记忆索引（summary + 链表 prev_index_id）
- **ChromaDB**：`engram_chroma/`（向量检索）
- **画像**：`engram_personas/{user_id}.json` + `history/{user_id}.json`
- **导出**：`exports/engram_export_*`

## 3. 启动流程（初始化）

1. **AstrBot 注册**（`main.py`）
2. 合并配置预设：`ConfigPresetService.apply()`
3. 初始化核心：`MemoryFacade`（内部创建线程池、DB、ProfileManager、MemoryManager）
4. 初始化处理器：
   - `MemoryCommandHandler`（mem_*）
   - `ProfileCommandHandler`（profile_*）
   - `MemoryToolHandler`（mem_search_tool 等）
   - `OneBotSyncHandler`（OneBot 用户信息同步）
   - `ExportHandler`（导出）
5. 启动调度器：`MemoryScheduler.start()`

## 4. 消息与记忆主流程

### 4.1 私聊消息进入（原文入库）

入口：`@filter.event_message_type(PRIVATE_MESSAGE)` → `on_private_message`

流程：
1. **指令过滤**：`_is_command_message()`
2. **原文写入**：`MemoryFacade.record_message()` → `MemoryManager.record_message()`
3. **缓存计数**：`last_chat_time` + `unsaved_msg_count`
4. **OneBot 同步**（节流 12h）：`OneBotSyncHandler.sync_user_info()`

### 4.2 LLM 回复后（记录助手消息）

入口：`@filter.after_message_sent` → `after_message_sent`

流程：
1. 私聊且非指令
2. 仅处理 LLM 回复
3. 记录助手原文
4. 更新互动统计：`ProfileManager.update_interaction_stats()`

### 4.3 后台归档（定时触发）

入口：`MemoryScheduler.background_worker()` → `MemoryManager.check_and_summarize()`

触发条件：
- 距离最后消息超过 `private_memory_timeout`
- 未归档消息数 ≥ `min_msg_count`

归档流程（按天分组）：
1. 拉取未归档原文：`db.get_unarchived_raw()`
2. 按日期分组 → 生成摘要 prompt
3. 调用 LLM 总结（可输出结构化 JSON）
4. 写入 `MemoryIndex`（SQLite）
5. 标记原文已归档
6. 写入 ChromaDB（失败进入 pending 队列）

## 5. 记忆检索与上下文注入

### 5.1 触发入口

入口：`@filter.on_llm_request` → `on_llm_request`

流程：
1. 读取画像：`ProfileManager.get_user_profile()`
2. 构造画像块：`LLMContextInjector.build_profile_block()`
3. 意图判断：`IntentClassifier.should_retrieve_memory()`
4. 话题缓存：`TopicMemoryCacheService.get_cached()`
5. 检索记忆：`MemoryManager.retrieve_memories()`
6. 构造注入块：记忆 + 工具提示（`ToolHintStrategyService`）
7. 注入 system_prompt：`LLMContextInjector.inject_context()`

### 5.2 记忆检索核心（MemoryManager.retrieve_memories）

- **向量检索**（ChromaDB）
- **关键词增强**（BM25 / n-gram）
- **排序策略**：RRF / Hybrid（向量 + 关键词 + 时效 + 活跃度）
- **时间/类型过滤**：`time_expr` + `source_types`
- **链路上下文**：`prev_index_id` 时间线提示
- **原文预览**：从 RawMemory 中抽取片段
- **召回增强**：成功检索后提高 active_score

兜底：向量不可用 → SQLite 关键词检索 + 本地重排。

## 6. 用户画像流程

### 6.1 每日画像更新（00:00）

入口：`MemoryScheduler.daily_persona_scheduler()`

流程：
1. 拉取「昨日记忆」范围（00:00~24:00）
2. 记忆数 ≥ `min_persona_update_memories` 才执行
3. 生成画像更新 prompt → LLM 输出 JSON
4. 画像防护：`ProfileGuardian.validate_update()`
   - 强证据保护（性别/年龄/地点/职业）
   - 偏好冲突检测
   - 置信度提案晋升
5. 写入画像 + 记录历史 + 证据元数据
6. TTL 衰减：`likes/dislikes`

### 6.2 用户指令

- `/profile show`：渲染图片（`ProfileRenderer`）
- `/profile set`：人工覆盖字段（如：`/profile set 职业 程序员`）
- `/profile delete`：删除画像记忆碎片（如：`/profile delete 爱好 篮球`）
- `/profile rollback`：回滚历史
- `/profile evidence`：证据摘要
- `/profile clear`：清空画像

## 7. 记忆管理指令

入口：`handlers/memory_commands.py`

- `/mem_list`：列出记忆（含短 ID）
- `/mem_view`：查看原文详情（序号或 ID）
- `/mem_search`：强制检索
- `/mem_delete` / `/mem_delete_all`：删除（支持撤销）
- `/mem_undo`：撤销删除（恢复向量）
- `/mem_clear_raw` / `/mem_clear_archive` / `/mem_clear_all`
- `/mem_rebuild_vector`：向量库重建

## 8. 工具检索（LLM Tool）

入口：`mem_search_tool / mem_get_detail_tool`

- 统一由 `MemoryToolHandler.build_memory_search_output()` 构建
- 支持 `time_expr`、`source_types`、返回 ID 供二次追查

## 9. 导出与统计

- `/mem_export`：导出当前用户对话（jsonl/json/txt/alpaca/sharegpt）
- `/mem_export_all`：管理员导出全量
- `/mem_stats`：当前用户 + 全局统计

## 10. 维护与折叠总结

调度器定时任务：
- **记忆衰减**（1:00）：`decay_active_scores()`
- **冷记忆修剪**：active_score 低于阈值从向量库移除
- **周/月/年折叠**：合并 lower-level 摘要生成更高层总结

---

如需更细粒度的“序列图/状态图/接口明细”，可以告诉我你关注的具体子流程（例如检索排序、画像防护规则、导出格式等）。
