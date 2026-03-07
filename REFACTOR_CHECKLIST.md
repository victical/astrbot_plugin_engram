# astrbot_plugin_engram 改造清单（可跟踪版）

> 目的：把当前问题拆成可执行任务，便于逐项推进、打勾验收、记录进度。

---

## 使用说明

- 状态建议：`[ ] 未开始` / `[~] 进行中` / `[x] 已完成` / `[-] 暂缓`
- 每完成一项，补充：
  - 完成日期
  - 负责人
  - 对应 commit / PR
  - 验收结果

---

## 里程碑总览

- **M1（稳定性兜底）**：P0 全部完成
- **M2（工程一致性）**：P1 全部完成
- **M3（质量与竞争力）**：P2 关键项完成

---

## P0（必须优先）

### P0-1 删除语义统一（ID 删除与序号删除一致）

- 状态：`[~] 进行中`
- 优先级：高
- 目标：`/mem_delete`、`/mem_delete_all` 无论传 ID 还是序号，行为一致。

**涉及文件**
- `core/memory_manager.py`
- `main.py`（提示文案一致性）

**改造任务**
- [x] 抽取统一删除核心逻辑（已实现 `_delete_memory_entry(target_memory, delete_raw)`）
- [x] `delete_memory_by_id()` 复用统一逻辑
- [x] 确保 ID 删除也记录 `_delete_history`（可撤销）
- [x] `delete_raw=False` 时统一将关联 raw 标记为未归档
- [ ] 删除成功/失败提示文案对齐（ID 与序号）

**验收标准**
- [x] `/mem_delete <ID>` 后可 `/mem_undo` 成功恢复（已补单测）
- [x] `/mem_delete_all <ID>` 行为与序号分支一致（已统一复用删除核心逻辑）
- [ ] 两条路径日志与数据副作用一致（index/chroma/raw）

**进度记录**
- 完成日期：
- 负责人：
- Commit/PR：
- 备注：已完成第一步代码改造，并新增 `tests/test_memory_delete_by_id.py`。待整体测试链路跑通后转为已完成。

---

### P0-2 检索降级（embedding 不可用时仍可检索）

- 状态：`[~] 进行中`
- 优先级：高
- 目标：向量不可用时不“失忆”，自动回退关键词检索。

**涉及文件**
- `core/memory_manager.py`
- `db_manager.py`

**改造任务**
- [x] 在 `db_manager.py` 增加关键词检索方法（当前为 LIKE 版本，后续可升级 FTS5）
- [x] 在 `retrieve_memories()` 增加 fallback 分支
- [x] 日志明确标注“已降级关键词检索”
- [x] 支持时间范围与 `source_types` 过滤在 fallback 中继续生效

**验收标准**
- [x] 关闭/失效 `embedding_provider` 后，`/mem_search` 仍返回可用结果（已补 fallback 单测）
- [ ] 工具检索链路（`mem_search_tool`）在无向量时也可工作
- [x] 不产生异常中断（只降级，不崩溃）

**进度记录**
- 完成日期：
- 负责人：
- Commit/PR：
- 备注：已完成 fallback 代码并新增 `tests/test_memory_fallback.py`。已通过定向测试：`tests/test_memory_delete_by_id.py`、`tests/test_memory_fallback.py`（2 passed）。

---

### P0-3 写入解耦（先落库，再向量化）

- 状态：`[x] 已完成`
- 优先级：高
- 目标：总结成功不因向量写入失败而丢失。

**涉及文件**
- `core/memory_manager.py`

**改造任务**
- [x] 调整 `_summarize_private_chat()` 顺序：先写 `MemoryIndex` 与归档状态
- [x] 向量写入失败时记录失败任务（待补队列/待重建标记）
- [x] 保留手动补偿入口（`/mem_rebuild_vector`）可回灌缺失向量（并在重建成功后清理待补队列）

**验收标准**
- [x] embedding 不可用时，`/mem_list` 仍可看到新归档（已补单测）
- [x] 恢复 embedding 后可通过重建补齐向量（已接入 pending 队列清理）
- [x] 不出现“总结完成但数据丢失”（已补单测）

**进度记录**
- 完成日期：
- 负责人：
- Commit/PR：
- 备注：新增 `tests/test_summarize_persistence.py` 验证“向量失败但总结落库成功”。

---

### P0-4 版本信息统一

- 状态：`[x] 已完成`
- 优先级：高
- 目标：运行版本、元数据、changelog 一致。

**涉及文件**
- `main.py`（`@register` 版本）
- `metadata.yaml`
- `CHANGELOG.md`

**改造任务**
- [x] 统一三处版本号
- [x] 在 `CHANGELOG.md` 新增对应版本变更记录
- [x] 约定单一版本源（建议 `metadata.yaml`）

**验收标准**
- [x] 三处版本一致
- [x] 发布后版本可追溯

**进度记录**
- 完成日期：
- 负责人：
- Commit/PR：
- 备注：已统一为 `1.5.4`（`main.py`、`metadata.yaml`、`CHANGELOG.md`）。

---

## P1（应尽快完成）

### P1-1 main.py 路由瘦身（业务逻辑下沉到 handlers）

- 状态：`[x] 已完成`
- 优先级：中高
- 目标：避免 main 与 handler 双份逻辑漂移。

**涉及文件**
- `main.py`
- `handlers/memory_commands.py`
- `handlers/profile_commands.py`
- `handlers/onebot_sync.py`

**改造任务**
- [x] 命令实现统一改为 main 转发 handler（mem_* / profile_* / engram_force_* 已迁移）
- [x] 删除 main 中重复业务逻辑（已移除命令、向量重建指令与 OneBot 同步重复实现；LLM 工具输出构建已下沉，时间解析与类型归一化已拆分至 `TimeExpressionService`）
- [x] handler 接口参数标准化（输入/输出）

**验收标准**
- [x] main.py 明显缩短（仅路由+参数校验）
- [x] 功能行为无回归（指令结果一致，pytest 34 passed）

**进度记录**
- 完成日期：
- 负责人：
- Commit/PR：
- 备注：已完成第三阶段路由瘦身：`mem_* / profile_* / engram_force_* / rebuild_vector` 命令逻辑转发至 handlers，`_build_memory_search_output` 下沉到 `MemoryToolHandler`，`_parse_time_expr/_normalize_source_types` 下沉到 `TimeExpressionService`；并保留 `__new__` 测试兼容回退逻辑。

---

### P1-2 配置项一致性治理（定义即生效）

- 状态：`[ ] 未开始`
- 优先级：中
- 目标：schema 中的配置项都能在代码里生效，避免“僵尸配置”。

**涉及文件**
- `_conf_schema.json`
- `services/config_preset.py`
- `core/memory_manager.py`
- `main.py`（若存在配置读取）

**改造任务**
- [ ] 枚举 schema 配置项与代码读取点
- [ ] 未使用项：实现或删除（并写迁移说明）
- [ ] 补充配置读取单测

**验收标准**
- [ ] 配置项与读取点一一对应
- [ ] 文档与行为一致

**进度记录**
- 完成日期：
- 负责人：
- Commit/PR：
- 备注：

---

### P1-3 测试可运行与最小 CI

- 状态：`[~] 进行中`
- 优先级：中高
- 目标：`pytest` 本地可一键运行，CI 自动执行。

**涉及文件**
- 项目包结构（必要时新增 `__init__.py`）
- `tests/*`
- 可选：`pyproject.toml` / `pytest.ini` / CI 配置

**改造任务**
- [x] 修复 `astrbot_plugin_engram` 导入路径问题（已新增项目根 `__init__.py`）
- [x] 明确测试启动方式（README 已增补 pytest 说明）
- [x] 增加最小 CI（push/pr 自动跑 tests）

**验收标准**
- [x] `pytest -q` 可收集并执行（当前 34 passed）
- [ ] CI 状态为绿色

**进度记录**
- 完成日期：
- 负责人：
- Commit/PR：
- 备注：已新增 `.github/workflows/ci.yml`，在 `beta/main` push 与 PR 自动执行测试（Python 3.11/3.12）。

---

## P2（中长期增强）

### P2-1 检索质量评测集

- 状态：`[ ] 未开始`
- 优先级：中
- 目标：量化“是否真的记住了”。

**改造任务**
- [ ] 建 20~50 条真实查询样本（偏好/事件/时间/人物）
- [ ] 记录 Top1/Top3 命中率、事实一致率、幻觉率
- [ ] 每次检索策略改动后做回归

**验收标准**
- [ ] 有固定评测数据与结果表
- [ ] 能横向比较改造前后指标

**进度记录**
- 完成日期：
- 负责人：
- Commit/PR：
- 备注：

---

### P2-2 数据一致性与审计能力

- 状态：`[ ] 未开始`
- 优先级：中
- 目标：关键操作可追踪、可审计。

**改造任务**
- [ ] 删除/撤销/清理操作写审计日志
- [ ] 导出行为（谁、何时、范围）留痕
- [ ] 异常恢复路径可定位

**验收标准**
- [ ] 关键操作均可追溯
- [ ] 审计日志结构化可检索

**进度记录**
- 完成日期：
- 负责人：
- Commit/PR：
- 备注：

---

### P2-3 性能与并发治理

- 状态：`[ ] 未开始`
- 优先级：中
- 目标：在用户增长时保持稳定响应。

**改造任务**
- [ ] embedding 任务批处理/队列化
- [ ] 图像渲染与头像缓存策略优化
- [ ] 调度器关键指标上报（成功率/耗时分位）

**验收标准**
- [ ] 峰值场景下无明显卡顿或阻塞
- [ ] 关键任务失败可重试、可观测

**进度记录**
- 完成日期：
- 负责人：
- Commit/PR：
- 备注：

---

## 建议排期

### Sprint 1（1周）
- P0-1 删除语义统一
- P0-4 版本统一
- P1-3 测试可运行

### Sprint 2（1周）
- P0-2 检索降级
- P0-3 写入解耦

### Sprint 3（1周）
- P1-1 main 路由瘦身
- P1-2 配置项一致性治理

### Sprint 4+（持续）
- P2 全部

---

## 变更记录（维护者填写）

| 日期 | 任务编号 | 变更摘要 | 负责人 | Commit/PR |
|---|---|---|---|---|
|  |  |  |  |  |

---

## 当前快照（基于本轮审查）

- 已识别高优先问题：
  - ID 删除链路与序号删除链路语义不一致
  - embedding 不可用时缺少可靠检索兜底
  - 总结写入与向量写入耦合偏高
  - 版本信息存在多处不一致
- 建议先完成 P0，再进入结构化重构。
