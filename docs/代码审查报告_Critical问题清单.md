# 代码审查报告 — Critical 问题清单

**审查日期**：2026-05-17
**审查范围**：`E:\AI\shouban\astrbot_plugin_engram` 全部 Python 源码（不含 `tests/`、`docs/`、`__pycache__/`）
**审查方法**：并行多 Agent 横向扫描 + 人工抽样验证（行号、调用链、副作用、SQL 注入面）
**审查依据**：用户提供的 6 项 Critical 候选 + 全项目交叉检查

---

## 0. 总览（验证后真实状态）

| # | 候选问题 | 验证结论 | 严重程度 |
|---|---|---|---|
| 1 | `_derive_scope_fields` 未定义 | **不成立** — 方法在 `core/memory_manager.py:247` 已正确定义 | — |
| 2 | `_format_export_output` 未定义 | **成立** | Critical |
| 3 | `webui_server.py:1077` `usry_params` 拼写错误 + `user_id` 未定义 | **成立** | Critical |
| 4 | `intent_classifier.py:244` `str.format` 注入 | **成立**（被 try/except 静默吞掉，降级到关键词检测） | High |
| 5 | `profile_guardian.py:132-135` 污染调用方 `new_profile` | **成立**（当前调用方未再读，仅破坏契约） | Medium |
| 6 | `db_manager.py` 多处 f-string 拼接 SQL → SQL 注入 | **部分成立** — 仅风格问题，无真实注入面；FTS MATCH 双引号转义有效；仅有 DoS 等次要风险 | Info |
| **+** | **额外发现** `profile_manager.py:329-355` `_merge_profile_meta` 原地写脏 → 快照-回滚失效 | **新发现** | Critical |

最终：**3 处 Critical（运行时崩溃或回滚失效）** + **1 处 High（功能静默退化）** + **1 处 Medium（潜在副作用）** + **2 处 Info（防御加固建议）**。

---

## 1. Critical 级问题（运行时崩溃 / 数据完整性破坏）

### C1. `_format_export_output` 方法未定义 → 全量导出空数据时 AttributeError

**位置**：`core/memory_manager.py:2992`
**严重性**：Critical（功能不可用）

**代码片段**：
```python
# core/memory_manager.py: 2966-2992
async def export_all_users_messages(self, format="jsonl", start_date=None, end_date=None, limit=None):
    ...
    raw_msgs = await loop.run_in_executor(
        self.executor, self.db.get_all_users_messages,
        start_date, end_date, limit,
    )

    if not raw_msgs:
        return self._format_export_output(format, [], {})  # ← 方法在整个项目中从未 def
```

**验证**：
- 在整个项目（含父类链、所有模块）中 grep `def _format_export_output` 与 `def format_export_output` **无任何匹配**。
- 该分支仅在"调用全量导出但库中无符合时间范围的数据"时触发。一旦触发立即 `AttributeError`。
- 在外层 `except Exception as e` 内被捕获后返回 `(False, "导出失败：...", {})`，**不会让进程崩溃**，但**导出功能在空数据场景下永远报错**。

**修复建议**：
1. 实现 `_format_export_output(format, items, stats)` 用于"空数据"统一返回；或
2. 简化为 `return True, "", {"exported": 0}`（与 `format` 已有的 jsonl/json/txt 路径返回结构对齐）。

**注意**：审查清单中"导出功能完全不可用"的描述偏激进——**仅"无数据"分支不可用**，有数据时走的是 `_export_as_jsonl/_export_as_json/_export_as_txt/_export_as_alpaca/_export_as_sharegpt`，这些方法均已定义。

---

### C2. WebUI `/api/stats` 双重 NameError

**位置**：`webui_server.py:1077` + `webui_server.py:1079`
**严重性**：Critical（接口 100% 崩溃）

**代码片段**：
```python
# webui_server.py: 1074-1083
@self._app.get("/api/stats")
async def get_stats(request: Request, token: str = Depends(self._auth_dependency())):
    del token
    usry_params.get("user_id")                                      # ← BUG 1: NameError: name 'usry_params' is not defined
    try:
        stats = await self._collect_stats(self.db, user_id=user_id) # ← BUG 2: NameError: name 'user_id' is not defined
        return {"success": True, "data": stats}
    except Exception as exc:
        logger.error("Engram WebUI 获取统计信息失败: %s", exc, exc_info=True)
        return {"success": False, "error": str(exc)}
```

**验证**：
- `usry_params` 显然是 `request.query_params` 或 `user_params` 的拼写错误，函数作用域内无任何赋值/参数/import 注入该名。
- 第 1079 行 `user_id=user_id` 中等号右侧的 `user_id` 同样未在函数作用域内定义。
- 整个项目仅此一处出现 `usry_params`（用 ruff F821 严格扫描验证）。
- **影响传播**：`/api/stats/alias` 路由（行 1111）会转发到 `get_stats(request, token)`，**两个端点同时报废**。

**修复建议**（一行修复）：
```python
async def get_stats(request: Request, token: str = Depends(self._auth_dependency())):
    del token
    user_id = (request.query_params.get("user_id") or "").strip() or None
    try:
        stats = await self._collect_stats(self.db, user_id=user_id)
        return {"success": True, "data": stats}
    ...
```

---

### C3. `_merge_profile_meta` 原地写脏 `current_persona` → 回滚快照失效

**位置**：`core/profile_manager.py:329-360` 与 `core/profile_manager.py:519-530`
**严重性**：Critical（数据完整性 / 回滚语义错误）
**性质**：本次审查新发现，原始清单未列。

**代码片段**：
```python
# core/profile_manager.py:329-355
def _merge_profile_meta(self, old_meta, accepted_updates, evidence_ref):
    meta = old_meta if isinstance(old_meta, dict) else {}
    fields = meta.get("fields", {})              # ← 与 current_persona["_meta"]["fields"] 同一引用
    ...
    for field_path in accepted_updates or []:
        field_meta = fields.get(field_path, {})  # ← 共享子对象
        ...
        field_meta["last_seen_at"] = now_iso
        field_meta["evidence_count"] = ...       # ← 原地写脏
        refs.append(evidence_ref)                # ← 原地 append
        fields[field_path] = field_meta          # ← 写回同一 fields dict

# core/profile_manager.py:519-530
validated_persona["_meta"] = self._merge_profile_meta(
    current_persona.get("_meta", {}),            # ← 传入了 current_persona 的内嵌引用
    decisions.get("accepted_fields", []),
    evidence_ref,
)
...
def _write():
    self._snapshot_profile(user_id, current_persona)   # ← 此时 current_persona 已被污染
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(validated_persona, f, ...)
```

**问题链**：
1. `_merge_profile_meta` 接收 `current_persona["_meta"]`（按引用传递），返回 `{"updated_at": ..., "fields": fields}`，其中 `fields` 是同一对象。
2. 在该函数内部对 `fields[field_path]` 的写入直接修改了 `current_persona["_meta"]["fields"]`。
3. 紧接着第 530 行 `_snapshot_profile(user_id, current_persona)` 将"理论上是更新前的状态"快照到回滚历史，但 `current_persona["_meta"]` 已被本次更新污染（`evidence_count` 累加、`evidence_refs` 末尾追加）。
4. 用户日后调用 `rollback_profile` 时，恢复出来的"旧版本"实际上是被静默篡改过的版本，**rollback 语义被破坏**。

**修复建议**：在 `_merge_profile_meta` 入口做深拷贝：
```python
def _merge_profile_meta(self, old_meta, accepted_updates, evidence_ref):
    meta = copy.deepcopy(old_meta) if isinstance(old_meta, dict) else {}
    fields = meta.get("fields", {})
    ...
```
或调整 `update_persona_daily` 内的顺序：先 snapshot，再 merge meta。

---

## 2. High 级问题（功能降级 / 静默吞错）

### H1. `_LLM_INTENT_PROMPT.format(query=text)` 在含 `{` 的用户消息上 KeyError

**位置**：`services/intent_classifier.py:244`
**严重性**：High（功能静默降级）

**代码片段**：
```python
# services/intent_classifier.py:16-23 + 244
_LLM_INTENT_PROMPT = (
    "判断以下用户消息是否需要调用长期记忆来回答。"
    ...
    "用户消息：{query}\n\n"
    "请只回答一个字：是 或 否"
)

# 行 244
prompt = _LLM_INTENT_PROMPT.format(query=text)   # ← text 是用户原文
```

**触发条件**：用户消息含 `{`、`{0}`、`{name}`、`{a.b}` 等 `str.format` 解析模式。例：
- `"{` → `ValueError: Single '{' encountered in format string`
- `"{abc}"` → `KeyError: 'abc'`
- `"{0}"` → `IndexError: tuple index out of range`
- `"{a.b}"` → `AttributeError`（更危险，可能访问对象属性，但在此模板中无可利用对象，仅崩）

**当前行为**：抛出后被 `services/intent_classifier.py:257-259` 的外层 `try/except` 静默捕获，回退到关键词检测（`_keyword_check`）。**不会崩进程，但 LLM 意图分类对这类消息永久失效**——而恰巧含花括号的消息往往是代码/JSON/配置相关的用户提问，其语义判定能力受损。

**修复建议**：
```python
# 选项 A（最小修改）：转义用户输入
prompt = _LLM_INTENT_PROMPT.format(query=text.replace("{", "{{").replace("}", "}}"))

# 选项 B（推荐）：与项目其他 prompt 一致，改用 replace
_LLM_INTENT_PROMPT = "...用户消息：{{query}}\n\n..."   # 模板用双大括号占位
prompt = _LLM_INTENT_PROMPT.replace("{{query}}", text)
```

**辐射检查**：项目中其他 prompt 模板（`core/memory_manager.py:1296/1445`、`core/profile_manager.py:480-481`）已采用 `replace("{{xxx}}", value)` 模式，对用户输入中的 `{` 免疫，**仅 intent_classifier 是孤例**。

---

## 3. Medium 级问题（潜在副作用 / 契约违反）

### M1. `ProfileGuardian.validate_update` 原地修改入参 `new_profile`

**位置**：`services/profile_guardian.py:131-135`
**严重性**：Medium（当前未引发外部 bug，但破坏函数契约）

**代码片段**：
```python
# services/profile_guardian.py:130-135
# social_graph 保留系统维护字段
old_stats = current_profile.get("social_graph", {}).get("interaction_stats", {})
new_social = new_profile.get("social_graph", {})            # ← 引用，不是副本
if old_stats:
    new_social["interaction_stats"] = old_stats             # ← 原地修改 new_profile["social_graph"]
validated["social_graph"] = new_social
```

**问题**：`new_profile["social_graph"]` 若存在则 `new_social` 与之共享同一对象，向其写键会污染调用方传入的 `new_profile`。

**当前调用方**：`core/profile_manager.py:510-514`
```python
validated_persona, conflicts, decisions = self._guardian.validate_update(
    current_persona, proposal, memory_texts,
)
```
`proposal` 来自当前 `update_persona_daily` 内 LLM 解析的临时变量，调用 `validate_update` 后不再读取，**当前不会引发用户可见的 bug**。

**风险**：未来若 `proposal` 被其他逻辑复用（例如要 dump 入审计日志、传给下游服务），就会读到被静默篡改的对象。

**修复建议**：
```python
new_social = dict(new_profile.get("social_graph", {}))   # 浅拷贝即可
```

---

## 4. Info 级（防御加固，非真实漏洞）

### I1. `db_manager.py` 多处 f-string 拼接 SQL 标识符

**位置**：`db_manager.py:186, 241-285, 290, 294, 303, 326, 508, 527-534`

**所有插值变量来源审计**：

| 行号 | f-string 模板 | 插值变量 | 来源 | 是否注入面 |
|---|---|---|---|---|
| 186 | `ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}` | `table_name` `column_name` `column_type` | 模型 `_meta.table_name` + 硬编码 `migration_plan` 字典 | **否** |
| 241-285 | `CREATE VIRTUAL TABLE / TRIGGER {fts_table}` | `fts_table = f"{table_name}_fts"` | 同上 | **否** |
| 290 | `SELECT COUNT(1) FROM {fts_table}` | `fts_table` | 同上 | **否** |
| 294 | `INSERT INTO {fts_table}({fts_table}) VALUES('rebuild')` | `fts_table` | 同上 | **否** |
| 303 | `CREATE INDEX IF NOT EXISTS {index_name} ON {table_name}({column_name})` | 全部内部硬编码 | 同上 | **否** |
| 326 | `PRAGMA table_info({table_name})` | `_meta.table_name` | 同上 | **否** |
| 508/522/527-534 | `WHERE {fts_table} MATCH ? AND mi.source_type IN ({placeholders})` | 表名为内部，过滤值经 `?` 参数绑定 | 同上 | **否** |

**FTS5 MATCH 表达式专项审计**（`db_manager.py:500-509`）：
```python
match_tokens = []
for token in normalized_keywords[:24]:
    safe = token.replace('"', '""').strip()        # 双引号转义
    if safe:
        match_tokens.append(f'"{safe}"')           # 包成 phrase literal
match_expr = " OR ".join(match_tokens)

where_sql = ["mi.user_id = ?", f"{fts_table} MATCH ?"]
params = [str(user_id), match_expr]                # 参数绑定，不是 f-string 插值进 SQL
```

**结论**：
- **SQL 层无注入**：`match_expr` 作为 SQL 绑定参数传入，不会被 SQLite 解析为 SQL 语法。
- **FTS5 表达式层**：双引号 phrase literal 会阻止内部内容被解析为 column filter (`col:`) / `NEAR` / `*` / `AND/OR/NOT`，**审查清单中"构造 ` OR 1=1 --` 可注入"在此处不成立**——它会变成查询不到任何文档的 phrase `"\" OR 1=1 --\""`。
- **唯一次要风险**：`.strip()` 仅去首尾空白，未过滤 `\x00`/`\r`/`\n` 等控制字符。NUL 字节可能让 sqlite3 抛 `ProgrammingError`，但外层 try/except 会回退到 LIKE 检索，**不构成 DoS 实际影响**。

**修复建议**（防御加固，可选）：
```python
import re
safe = re.sub(r'[\x00-\x1f\x7f"]', ' ', token).replace('"', '""').strip()
if len(safe) > 64:
    safe = safe[:64]
```

### I2. WebUI 其他端点用户输入均经 peewee ORM 参数化

经全文件扫描，`webui_server.py` 中**没有任何 raw SQL** / `execute_sql` 调用。所有用户输入（query、user_id、group_id 等）通过 peewee 表达式（`.where(...)`、`.contains(query)`）参数化，无注入风险。

---

## 5. 修复优先级建议

| 优先级 | 编号 | 操作 | 工作量 |
|---|---|---|---|
| 🔴 P0 立即 | C2 | 修复 `webui_server.py:1077-1079` `usry_params` / `user_id`（接口 100% 崩溃，每个请求都触发）| 1 行 |
| 🔴 P0 立即 | C3 | 修复 `_merge_profile_meta` 原地写脏（rollback 语义错误，静默数据污染） | deepcopy 或调换 snapshot 顺序 |
| 🟠 P1 本周 | C1 | 实现 `_format_export_output` 或简化空数据返回 | 5-10 行 |
| 🟠 P1 本周 | H1 | 修 `intent_classifier.py:244` 的 `.format` 注入（与项目其他 prompt 对齐成 `replace` 模式） | 1-2 行 |
| 🟡 P2 顺便 | M1 | `profile_guardian.py:132` 浅拷贝避免污染入参 | 1 行 |
| 🟢 P3 加固 | I1 | FTS5 token 控制字符过滤 + 长度截断 | 2 行 |

---

## 6. 审查清单原始 6 项的复核结论

> 原清单：CRITICAL (6) — 运行时崩溃 / 安全漏洞

| # | 原描述 | 复核结论 |
|---|---|---|
| 1 | `derivescope_fields` / `_derive_scope_fields` 缺失 | ❌ **不成立**。方法在 `core/memory_manager.py:247` 定义，调用在 `1513` 行（清单中 `1440` 行号有误）。weekly/monthly/yearly 折叠**不会**因此崩溃。 |
| 2 | `formatexport_output` / `_format_export_output` 缺失 | ✅ **成立**。仅"空数据"分支触发；导出有数据时正常。 |
| 3 | WebUI `/api/stats` `usryparams` 拼写错误 | ✅ **成立**。NameError 双重命中（`usry_params` + `user_id`）。 |
| 4 | `str.format()` 注入 | ✅ **成立但被 except 静默吞掉**。后果是意图分类降级到关键词，不会让进程崩溃。 |
| 5 | `profile_guardian` 修改入参 dict | ✅ **成立但当前无可见副作用**。破坏契约，属潜在炸药。 |
| 6 | SQL 注入 | ⚠️ **降级为 Info**。所有 f-string 插值均为内部模型元数据，FTS MATCH 已用双引号 + `""` 转义 + 参数绑定，`OR 1=1 --` 注入路径不成立。仅需做控制字符过滤的纵深防御。 |

---

**审查人**：claude-sonnet-4-6（基于多 Agent 并行扫描 + 人工抽样）
**生成时间**：2026-05-17
