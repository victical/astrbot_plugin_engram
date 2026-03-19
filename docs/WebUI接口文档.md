# astrbot_plugin_engram WebUI 接口文档

## 1. 文档说明

本文档说明 `astrbot_plugin_engram` 当前内置 WebUI 的后端接口设计，覆盖：

- 鉴权机制
- 请求/响应约定
- 各 API 的参数与返回结构
- 常见错误码
- 前端调用注意事项

实现文件：`E:/AI/shouban/astrbot_plugin_engram/webui_server.py`

---

## 2. 服务概览

WebUI 服务由 `EngramWebServer` 基于 FastAPI 提供，默认能力包括：

- 登录 / 登出
- 用户列表
- 记忆列表、详情、搜索、删除、撤销
- 统计概览与近期活动
- 用户画像查看、更新、删除、渲染
- 向量库重建

---

## 3. 基础信息

## 3.1 默认监听配置

由插件配置决定：

- `webui_host`：默认 `0.0.0.0`
- `webui_port`：默认 `8080`

典型访问地址：

```text
http://127.0.0.1:8080/
```

## 3.2 静态页面

WebUI 静态资源位于：

- `webui/static/index.html`
- `webui/static/dashboard.html`
- `webui/static/memories.html`
- `webui/static/memory-detail.html`
- `webui/static/profile.html`
- `webui/static/password.html`

---

## 4. 鉴权机制

## 4.1 鉴权模式

WebUI 支持两种模式：

### 模式 A：开启鉴权

当 `enable_webui_auth = true` 时：

- 需先调用 `POST /api/login`
- 拿到 token 后，通过请求头传递
- 未提供 token 或 token 失效会返回 `401`

### 模式 B：关闭鉴权

当 `enable_webui_auth = false` 时：

- `/api/login` 会直接返回公共 token
- 其余接口无需实际校验密码

## 4.2 Token 传递方式

服务端支持以下两种方式：

### 方式 1：Authorization Header

```http
Authorization: Bearer <token>
```

### 方式 2：X-Auth-Token Header

```http
X-Auth-Token: <token>
```

前端当前默认使用 `Authorization: Bearer ...`。

## 4.3 Session 规则

相关配置：

- `webui_session_timeout`：会话空闲超时，默认 3600 秒
- token 最大生命周期：代码固定为 86400 秒
- 后台每 300 秒清理一次过期 token

## 4.4 登录频率限制

相关配置：

- `webui_login_max_attempts`：默认 5 次
- `webui_login_window_seconds`：默认 300 秒

超过限制后，`/api/login` 返回：

- `429 Too Many Requests`

---

## 5. 通用响应约定

大部分业务接口采用如下格式：

### 成功

```json
{
  "success": true,
  "data": {}
}
```

### 失败

```json
{
  "success": false,
  "error": "错误信息"
}
```

但有少数接口直接返回 FastAPI 风格错误，例如：

```json
{
  "detail": "需要提供 user_id"
}
```

因此前端或调用方应同时兼容：

1. `success=false`
2. HTTPException 的 `detail`

---

## 6. 认证接口

## 6.1 健康检查

### `GET /api/health`

用于检查服务是否在线。

#### 请求示例

```http
GET /api/health
```

#### 响应示例

```json
{
  "status": "ok",
  "version": "1.0.0"
}
```

---

## 6.2 登录

### `POST /api/login`

#### 说明

使用访问密码换取 token。

#### 请求体

```json
{
  "password": "your-password"
}
```

#### 成功响应

```json
{
  "token": "xxxxx",
  "expires_in": 3600,
  "force_change": false
}
```

#### 字段说明

| 字段 | 类型 | 说明 |
|---|---|---|
| `token` | string | 会话 token |
| `expires_in` | int | 空闲超时秒数 |
| `force_change` | bool | 是否强制修改密码；当前通常为 `false` |

#### 失败场景

| HTTP 状态码 | 说明 |
|---:|---|
| `400` | 密码为空 |
| `401` | 密码错误 |
| `429` | 登录尝试次数过多 |

#### 请求示例

```bash
curl -X POST http://127.0.0.1:8080/api/login \
  -H "Content-Type: application/json" \
  -d '{"password":"123456"}'
```

---

## 6.3 登出

### `POST /api/logout`

#### 说明

使当前 token 失效。

#### 请求头

```http
Authorization: Bearer <token>
```

#### 响应示例

```json
{
  "detail": "已退出登录"
}
```

---

## 6.4 修改密码

### `POST /api/password`

#### 说明

当前接口为占位接口，并不真正修改密码。

#### 响应示例

```json
{
  "success": false,
  "error": "当前为配置文件密码模式，请在插件配置中修改 webui_access_password 并重启插件。"
}
```

---

## 7. 用户接口

## 7.1 获取用户列表

### `GET /api/users`

#### 说明

返回当前数据库中出现过的 `user_id` 列表。

#### 响应示例

```json
{
  "success": true,
  "data": {
    "items": ["123456", "10001"],
    "total": 2
  }
}
```

#### 字段说明

| 字段 | 说明 |
|---|---|
| `items` | 用户 ID 数组 |
| `total` | 用户数量 |

---

## 8. 记忆接口

## 8.1 获取记忆列表

### `GET /api/memories`

#### 说明

分页获取长期记忆索引列表。

#### Query 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `user_id` | string | 否 | - | 过滤指定用户 |
| `source_type` | string | 否 | - | 过滤来源类型 |
| `page` | int | 否 | `1` | 页码，最小为 1 |
| `page_size` | int | 否 | `20` | 每页条数，最大 200 |

#### 响应示例

```json
{
  "success": true,
  "data": {
    "items": [
      {
        "id": "70749040-efef-4e50-85a1-5efb82c7a931",
        "summary": "用户提到最近在准备考研，情绪偏紧张。",
        "user_id": "123456",
        "source_type": "private",
        "active_score": 108,
        "created_at": "2026-03-19T23:59:58"
      }
    ],
    "total": 1,
    "page": 1,
    "page_size": 20,
    "has_more": false
  }
}
```

---

## 8.2 获取记忆详情

### `GET /api/memories/{memory_id}`

#### 说明

获取单条长期记忆及其关联原始消息回放。

#### 路径参数

| 参数 | 类型 | 说明 |
|---|---|---|
| `memory_id` | string | `MemoryIndex.index_id` |

#### Query 参数

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `user_id` | string | 否 | 不传时，服务端会先按 `memory_id` 反查 |

#### 成功响应

```json
{
  "success": true,
  "data": {
    "memory": {
      "index_id": "70749040-efef-4e50-85a1-5efb82c7a931",
      "summary": "用户提到最近在准备考研，情绪偏紧张。",
      "user_id": "123456",
      "source_type": "private",
      "active_score": 108,
      "created_at": "2026-03-19T23:59:58"
    },
    "messages": [
      {
        "uuid": "2eb7e91d-87b0-4f95-9c31-8f9b9db6a6a4",
        "role": "user",
        "user_name": "Alice",
        "content": "我最近在准备考研，压力有点大。",
        "timestamp": "2026-03-19T21:13:05"
      }
    ],
    "ai_name": "助手"
  }
}
```

#### 失败场景

| HTTP 状态码 | 说明 |
|---:|---|
| `400` | 无法确定 `user_id` |
| `404` | 记忆不存在 |

---

## 8.3 搜索记忆

### `POST /api/memories/search`

#### 说明

基于 SQLite 摘要关键词进行搜索，是 WebUI 侧的管理型搜索接口。

#### 请求体

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `query` | string | 是 | 搜索词 |
| `user_id` | string | 是 | 用户 ID |
| `limit` | int | 否 | 最多返回条数，默认 50，最大 200 |
| `source_types` | array/string | 否 | 来源类型过滤 |
| `start_time` | string/int | 否 | 起始时间，支持 ISO 字符串或时间戳 |
| `end_time` | string/int | 否 | 结束时间，支持 ISO 字符串或时间戳 |

#### 请求示例

```json
{
  "query": "考研",
  "user_id": "123456",
  "limit": 20,
  "source_types": ["private"],
  "start_time": "2026-03-01T00:00:00",
  "end_time": "2026-03-31T23:59:59"
}
```

#### 响应示例

```json
{
  "success": true,
  "data": {
    "items": [
      {
        "id": "70749040-efef-4e50-85a1-5efb82c7a931",
        "summary": "用户提到最近在准备考研，情绪偏紧张。",
        "user_id": "123456",
        "source_type": "private",
        "active_score": 108,
        "created_at": "2026-03-19T23:59:58"
      }
    ]
  }
}
```

#### 失败场景

| HTTP 状态码 | 说明 |
|---:|---|
| `400` | 缺少 `query` 或 `user_id` |

---

## 8.4 删除记忆

### `DELETE /api/memories/{memory_id}`

#### 说明

删除单条长期记忆。可选是否同时删除原始消息。

#### 路径参数

| 参数 | 类型 | 说明 |
|---|---|---|
| `memory_id` | string | 记忆 ID |

#### Query 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `user_id` | string | 否 | - | 不传时会先按记忆 ID 反查 |
| `delete_raw` | bool string | 否 | `false` | 是否同时删除关联原始消息 |

> `delete_raw=true` 表示同时删除原文；否则仅删除长期记忆索引，并把原始消息恢复为“未归档”。

#### 响应示例

```json
{
  "success": true,
  "data": {
    "message": "删除成功",
    "summary": "用户提到最近在准备考研，情绪偏紧张。"
  }
}
```

#### 失败场景

| HTTP 状态码 | 说明 |
|---:|---|
| `400` | 无法确定 `user_id` |
| `404` | 目标记忆不存在 |

---

## 8.5 撤销最近一次删除

### `POST /api/memories/undo`

#### 说明

按用户维度撤销最近一次删除操作。

#### 请求体

```json
{
  "user_id": "123456"
}
```

#### 成功响应

```json
{
  "success": true,
  "data": {
    "summary": "用户提到最近在准备考研，情绪偏紧张。"
  }
}
```

#### 失败响应

```json
{
  "success": false,
  "error": "没有可撤销的删除记录"
}
```

#### 失败场景

| HTTP 状态码 | 说明 |
|---:|---|
| `400` | 未提供 `user_id` |

---

## 9. 统计接口

## 9.1 获取统计信息

### `GET /api/stats`

#### 说明

获取私聊数据库的统计信息，可按用户过滤。

#### Query 参数

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `user_id` | string | 否 | 不传则返回全库统计 |

#### 响应示例

```json
{
  "success": true,
  "data": {
    "user_count": 12,
    "total": 3000,
    "archived": 2200,
    "unarchived": 800,
    "user_messages": 1500,
    "assistant_messages": 1500,
    "memory_index_count": 460,
    "db_path": "data/plugins_data/astrbot_plugin_engram/engram_memories.db"
  }
}
```

#### 说明

当传 `user_id` 时，统计结构会根据底层实现变为单用户统计，但仍会附加：

- `memory_index_count`
- `db_path`

---

## 9.2 获取统计概览

### `GET /api/stats/overview`

#### 说明

返回 Dashboard 所需的总览信息，包括：

- 私聊统计
- 群聊统计（若启用）
- 是否启用群聊记忆
- 近 7 日消息趋势

#### 响应示例

```json
{
  "success": true,
  "data": {
    "private": {
      "user_count": 12,
      "total": 3000,
      "archived": 2200,
      "unarchived": 800,
      "user_messages": 1500,
      "assistant_messages": 1500,
      "memory_index_count": 460,
      "db_path": ".../engram_memories.db"
    },
    "group": {
      "user_count": 3,
      "total": 900,
      "archived": 500,
      "unarchived": 400,
      "user_messages": 600,
      "assistant_messages": 300,
      "memory_index_count": 120,
      "db_path": ".../engram_memories_group.db"
    },
    "group_enabled": true,
    "history": [
      {
        "date": "2026-03-13",
        "private": 120,
        "group": 60,
        "total": 180
      }
    ]
  }
}
```

---

## 9.3 统计别名接口

### `GET /api/stat`

#### 说明

这是 `/api/stats` 的别名接口，兼容旧调用。

---

## 9.4 获取近期活动

### `GET /api/activities`

#### Query 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `limit` | int | 否 | `8` | 返回条数 |

#### 响应示例

```json
{
  "success": true,
  "data": [
    {
      "title": "私聊归档完成 3 条",
      "category": "task",
      "source": "private",
      "meta": {
        "user_id": "123456"
      }
    }
  ]
}
```

#### 说明

活动数据来自 `MemoryManager` 内存中的近期活动列表，属于非持久化数据。

---

## 10. 画像接口

## 10.1 获取画像 JSON

### `GET /api/profile/{user_id}`

#### 说明

返回指定用户的当前画像结构。

#### 响应示例

```json
{
  "success": true,
  "data": {
    "basic_info": {
      "qq_id": "123456",
      "nickname": "Alice"
    },
    "attributes": {
      "hobbies": ["羽毛球"]
    }
  }
}
```

---

## 10.2 获取画像渲染图

### `GET /api/profile/{user_id}/render`

#### 说明

将当前画像渲染为 PNG 图片。

#### 返回类型

- `Content-Type: image/png`

#### 说明

内部流程：

1. 读取用户画像
2. 临时创建 `ProfileRenderer`
3. 调用 `render()` 生成图片字节流
4. 返回 PNG

#### 错误情况

- 渲染失败时返回 `500`

---

## 10.3 更新画像

### `POST /api/profile/{user_id}`

#### 说明

对画像执行增量更新。

#### 请求体示例

```json
{
  "basic_info": {
    "job": "程序员"
  },
  "attributes": {
    "hobbies": ["摄影"]
  }
}
```

#### 响应示例

```json
{
  "success": true,
  "data": {
    "basic_info": {
      "job": "程序员"
    },
    "attributes": {
      "hobbies": ["摄影"]
    }
  }
}
```

#### 行为说明

- 字典字段递归 merge
- 列表字段去重合并
- 不需要传完整画像

---

## 10.4 删除画像中的列表项

### `POST /api/profile/{user_id}/remove-item`

#### 说明

从画像某个列表字段中删除指定值。

#### 请求体

```json
{
  "field_path": "attributes.hobbies",
  "value": "摄影"
}
```

#### 成功响应

```json
{
  "success": true,
  "data": {
    "basic_info": {},
    "attributes": {
      "hobbies": []
    }
  },
  "message": "删除成功"
}
```

#### 失败响应

```json
{
  "success": false,
  "error": "该字段不是列表类型，无法删除单项。"
}
```

#### 使用限制

- 目标字段必须存在
- 目标字段必须是列表
- 值必须实际存在于列表中

---

## 10.5 清空画像

### `DELETE /api/profile/{user_id}`

#### 说明

删除：

- 当前画像文件
- 画像历史文件

#### 响应示例

```json
{
  "success": true,
  "data": null
}
```

---

## 11. 运维接口

## 11.1 重建向量库

### `POST /api/maintenance/rebuild-vectors`

#### 说明

从 SQLite `MemoryIndex` 全量重建 Chroma 向量索引。

#### 请求体

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `full_rebuild` | bool | 否 | `false` | 是否执行全量重建 |
| `batch_size` | int | 否 | `200` | 批大小，范围 50~500 |

#### 请求示例

```json
{
  "full_rebuild": true,
  "batch_size": 200
}
```

#### 响应示例

```json
{
  "success": true,
  "data": {
    "success": true,
    "total": 460,
    "rebuilt": 460,
    "failed": 0
  }
}
```

#### 适用场景

- embedding provider 切换
- 向量维度不匹配
- Chroma 数据损坏
- 大量 pending vector jobs 后的人工修复

---

## 12. 常见错误码

| HTTP 状态码 | 说明 | 常见接口 |
|---:|---|---|
| `400` | 请求参数不完整或不合法 | login / search / detail / delete / undo |
| `401` | 未认证、token 无效、token 过期 | 所有受保护接口 |
| `404` | 目标资源不存在 | memory detail / delete |
| `429` | 登录频率限制触发 | login |
| `500` | 服务内部错误 | render 等 |

---

## 13. 前端调用建议

## 13.1 统一鉴权头

建议前端统一通过：

```js
Authorization: `Bearer ${token}`
```

当前前端辅助函数见：`webui/static/auth.js`

## 13.2 错误处理建议

调用方应同时兼容：

- `response.ok === false`
- 返回体中的 `detail`
- 返回体中的 `success === false`
- 返回体中的 `error`

## 13.3 时间格式建议

对于搜索接口中的 `start_time` / `end_time`，建议统一使用 ISO 8601：

```text
2026-03-19T00:00:00
```

---

## 14. 当前接口特点与限制

## 14.1 特点

- 接口覆盖了插件主要运维与浏览需求
- 结构简单，适合内置后台
- 线程池封装了阻塞型数据库操作
- 私聊与群聊统计可统一展示

## 14.2 限制

- `/api/password` 目前只是占位接口
- 记忆搜索为 SQLite 关键词搜索，不是完整语义搜索 API
- 活动数据与撤销历史均为内存态，重启后丢失
- 目前没有 OpenAPI 风格的正式 schema 文档输出文件

---

## 15. 一句话总结

`astrbot_plugin_engram` 的 WebUI API 是一组面向内置管理后台的轻量接口，核心职责是：

**让管理员可以用浏览器查看、搜索、维护长期记忆与用户画像，并进行基本的运维操作。**
