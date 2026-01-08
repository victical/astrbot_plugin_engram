# Astrbot Engram 仿生双轨记忆系统

[![Astrbot Plugin](https://img.shields.io/badge/Ast rbot-Plugin-blue.svg)](https://github.com/AstrBotDevs/AstrBot)
[![Version](https://img.shields.io/badge/Version-1.0.0-green.svg)](https://github.com/yourusername/astrbot_plugin_engram)

`astrbot_plugin_engram` 是一款为 Astrbot 设计的工业级长期记忆增强插件。它模仿人类大脑的记忆机制，通过“双轨制”架构解决了 LLM 在即时通讯场景下记忆碎片化、遗忘快、无法回溯原文等痛点。

## 🌟 核心特性

### 1. 仿生双轨记忆架构
- **叙事性总结 (L2 记忆)**：自动将零散的私聊对话总结为结构化的记忆摘要。
- **原文指针回溯 (Index-to-Raw)**：记忆不仅有摘要，还通过 UUID 指针精准关联原始对白。AI 回复时可直接调取“原话”，彻底消除幻觉。
- **时间线链表**：在数据库底层建立链表结构，使每一条记忆都带有“前情提要”，让 AI 具备因果感知能力。

### 2. 深度用户画像 (User Persona)
- **多维建模**：自动维护包含基础信息、性格特征、偏好、禁忌、重大事件及关系阶段的结构化画像。
- **每日深度构建**：系统会在每日凌晨自动回顾全天记忆，当信息积累达到阈值（可配置）时，触发一次深度的画像重塑。
- **上下文注入**：按照 `用户画像 > 长期记忆 > 当前上下文` 的优先级自动注入 System Prompt，实现极高的人格化交互。

### 3. 高性能与成本优化
- **全异步执行**：基于 `ThreadPoolExecutor` 和 `asyncio`，所有数据库 (SQLite) 和向量库 (ChromaDB) 操作均不阻塞主线程。
- **归档机制**：消息总结后自动标记为“已归档”，防止重复计算，最大限度节省 Token。
- **语义搜索**：基于向量检索 (ChromaDB)，支持模糊语义查询，而不仅仅是关键词匹配。

## 🛠️ 指令说明

| `/mem_list` | 查看最近生成的 5 条长期记忆摘要 |
| `/mem_source <编号>` | 查看指定编号记忆的原始对话记录（如：`/mem_source 1`） |
| `/mem_search <关键词>` | 搜索相关的长期记忆（含时间戳、背景及原文参考） |
| `/mem_clear` | 清除所有长期记忆与原始对话（需二次确认） |
| `/profile show` | 查看当前的结构化用户画像（Markdown 格式） |
| `/profile set <键> <值>` | 手动校准画像（如：`/profile set basic.name 小明`） |
| `/profile clear` | 重置用户画像（需二次确认） |
| `/engram_test force_summarize` | (管理员) 强制触发当前对话的总结归档 |
| `/engram_test force_persona` | (管理员) 强制根据今日记忆更新用户画像 |

## ⚙️ 配置项说明

在 Astrbot WebUI 的插件配置页面，您可以自定义以下参数：
- **私聊记忆总结触发时间**：距离最后一次对话超过此时间触发总结。
- **触发总结最少消息数**：未总结消息达到此数量才触发总结。
- **总结记忆使用的模型**：支持下拉选择已配置的 LLM 提供商（建议使用低成本模型）。
- **画像更新阈值**：每日新增记忆达到多少条才触发画像深度更新。
- **自定义总结提示词**：支持自定义记忆提取逻辑。

## 🚀 安装

1. 在 Astrbot 插件市场搜索并安装。
2. 或手动克隆到 `data/plugins/` 目录：
   ```bash
   cd data/plugins
   git clone https://github.com/yourusername/astrbot_plugin_engram.git
   ```
3. 重启 Astrbot。

## 🧪 功能测试指南

如果您需要验证插件的所有核心功能，请按照以下步骤操作：

### 1. 记忆录入与召回测试
- **第一步：建立记忆**。发送几条包含具体信息的私聊消息。例如：
  - `我最近在准备考研，压力好大。`
  - `我超级喜欢吃冰美式，但是不喜欢香菜。`
  - `我的猫叫奥利奥。`
- **第二步：触发总结**。根据您的配置（默认 30 分钟且 > 3 条消息），您可以等待超时，或者在测试时临时调小配置项中的 `private_memory_timeout` 为 `60`。
- **第三步：验证召回**。过一会后问 AI：`我刚才提到了什么？` 或者 `你知道我的猫叫什么吗？`
  - **预期结果**：AI 应该能在回复中正确回溯上述信息，并显示 `【长期记忆回溯】`。

### 2. 用户画像（全息侧写）测试
- **第一步：产生足够记忆**。确保今天有至少 3 条（由 `min_persona_update_memories` 决定）长期记忆生成。
- **第二步：模拟凌晨更新**。正常情况下画像在凌晨 00:00 更新。开发测试时，您可以手动修改 `memory_logic.py` 中的时间判断或使用指令（如果已实现）。
- **第三步：查看画像**。输入 `/profile show`。
  - **预期结果**：应该能看到 `interests` 里出现了 `冰美式`，`social_graph` 里出现了 `猫:奥利奥`，`current_state` 显示 `考研压力大`。

### 3. 语义搜索与列表测试
- 输入 `/mem_list`：应列出最近的记忆摘要。
- 输入 `/mem_search 考研`：应精准召回关于考研的记忆原文和背景。

### 4. 动态交互注入测试
- **第一步**：手动设置一个语气偏好：`/profile set communication_style.tone 毒舌`。
- **第二步**：设置一个称呼：`/profile set communication_style.addressing 老大`。
- **第三步**：随便发一条消息。
  - **预期结果**：AI 的回复语气应该变得“毒舌”，并称呼你为“老大”。

## 📂 存储说明
本插件遵循 Astrbot 规范，所有持久化数据均存储在由 `StarTools.get_data_dir()` 自动生成的插件数据目录下（通常为 `data/plugins_data/astrbot_plugin_engram/`）：
- **SQLite**: `engram_memories.db` (存储原文、索引及时间链)。
- **ChromaDB**: `engram_chroma/` (存储语义向量)。
- **JSON Persona**: `engram_personas/{user_id}.json` (存储用户画像)。
- **PillowMD Styles**: `data/engram_styles/` (默认样式目录，可在配置中更改)。

---
*Inspired by the concept of biological engrams.*
