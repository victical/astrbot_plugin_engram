# 群聊记忆功能（仅好友 + 仅相关消息）开发文档

> 目标：在群聊场景下，仅记录“已加好友”的用户、且“与 Bot 相关”的消息。默认关闭。

## 1. 设计原则

- **默认关闭**：避免隐私争议与噪声。
- **LLM 实际回复才记录**：仅当消息触发 LLM 回复时才记录（用户+Bot）。
- **好友白名单**：仅记录“已加好友”的用户。
- **群聊隔离**：群聊记忆不写入私聊链路（需单独 source_type）。
- **可配置/可控**：提供开关与阈值配置。

## 2. 配置项（建议新增）

```json
{
  "enable_group_memory": false,
  "group_memory_only_friends": true,
  "group_memory_min_text_length": 6,
  "group_memory_source_type": "group", 
  "group_memory_store_session_as": "group_id"
}
```

说明：
- `enable_group_memory`：总开关
- `group_memory_only_friends`：仅记录好友消息
- `group_memory_min_text_length`：过短噪声过滤
- `group_memory_source_type`：群聊记忆类型（用于检索过滤）
- `group_memory_store_session_as`：session_id 绑定方式（推荐 `group_id`）

## 3. 触发判定逻辑（建议）

### 3.1 入口
- `on_llm_request`：缓存触发 LLM 的群消息
- `after_message_sent`：当 LLM 实际回复后再落库

### 3.2 条件顺序
1. 全局开关 `enable_group_memory` = true
2. **好友过滤**：校验发送者是否为 Bot 好友
3. **LLM 实际回复**：仅当本次群消息触发了 LLM 回复时记录
4. **文本长度**：>= `group_memory_min_text_length`

若全部通过 → 记录（用户消息 + Bot 回复）

## 4. 存储策略（物理隔离）

- **群聊库**：独立 SQLite + ChromaDB 目录（例如 `engram_memories_group.db` + `engram_chroma_group/`）
- **RawMemory**：写入群聊库（`session_id = group_id`）
- **Bot 回复**：同样写入群聊库（role=assistant）
- **MemoryIndex**：归档时仍标记 `source_type=group`（便于检索过滤）
- **检索**：私聊默认不访问群聊库；群聊检索只查群聊库

## 5. 好友判断（OneBot 方案）

### 5.1 好友列表接口

- `get_friend_list(no_cache: boolean|string)`：拉取好友列表
  - `no_cache=true`：强制拉取最新列表
  - 默认使用缓存降低频率

### 5.2 好友新增通知（已同意）

- 监听 OneBot 好友添加通知：

```json
{
  "post_type": "notice",
  "notice_type": "friend_add",
  "user_id": 987654321
}
```

> `friend_add` 表示**双方已成为好友**，可直接加入好友缓存。

### 5.3 缓存策略

- 启动时加载一次好友列表到缓存
- 收到 `friend_add` 通知时更新缓存
- 群聊消息触发时仅查缓存，不直接打接口

## 6. UI/指令（可选）

- `/group_mem_on` / `/group_mem_off`：管理员开关
- `/group_mem_status`：查看当前状态
- `/group_mem_clear`：清理本群相关记忆

## 7. 风险与注意事项

- **隐私合规**：群聊记忆必须明确告知/默认关闭。
- **噪声控制**：严格触发条件 + 字数阈值。
- **检索污染**：群聊记忆默认不参与私聊注入。
- **并发与性能**：群聊消息量大，注意采样与限速。

## 8. 变更位置（建议）

- `main.py`：群聊缓存与 LLM 回复落库（on_llm_request + after_message_sent）
- `core/memory_manager.py`：支持 `source_type=group`
- `db_manager.py`：支持群聊独立数据库（SQLite + Chroma 目录）
- `handlers`：新增群聊命令（可选）
- `_conf_schema.json`：加入配置项
- `README.md`：新增群聊记忆功能说明

## 9. 开发流程优化（建议）

1. **先做好友缓存服务**
   - 封装 OneBot `get_friend_list(no_cache)` 调用与缓存
   - 监听 `friend_add` 通知即时更新
2. **实现“LLM 回复触发记录”**
   - on_llm_request 缓存群消息 → after_message_sent 落库
   - 未触发 LLM 的群消息不记录
3. **接入物理隔离存储**
   - 群聊专用 SQLite/Chroma 目录
   - 归档/检索使用独立 manager
4. **补齐配置与文档**
   - `_conf_schema.json` + README 同步
5. **加最小回归测试**
   - 群聊触发/不触发 LLM 的记录差异
   - 好友/非好友过滤
