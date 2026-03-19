"""
Engram WebUI Server
基于 FastAPI 的内置 Web 服务，提供记忆管理与基础运维接口。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path
from typing import Any, Callable

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from astrbot.api import logger



class EngramWebServer:
    """Engram WebUI 服务端"""

    def __init__(self, plugin, host: str = "0.0.0.0", port: int = 8080):
        self.plugin = plugin
        self.logic = plugin.logic
        self.db = plugin.logic.db
        self.config = plugin.config

        self.host = str(host)
        self.port = int(port)

        self.session_timeout = max(60, int(self.config.get("webui_session_timeout", 3600)))
        self.login_max_attempts = max(
            1, int(self.config.get("webui_login_max_attempts", 5))
        )
        self.login_window_seconds = max(
            60, int(self.config.get("webui_login_window_seconds", 300))
        )

        self._auth_disabled = not bool(self.config.get("enable_webui_auth", True))
        self._access_password = str(
            self.config.get("webui_access_password", "")
        ).strip()
        self._password_generated = False
        self._force_password_change = False

        if not self._auth_disabled and not self._access_password:
            logger.warning("Engram WebUI 已启用登录鉴权，但未配置 webui_access_password")

        self._tokens: dict[str, dict[str, float]] = {}
        self._token_lock = asyncio.Lock()

        self._failed_attempts: dict[str, list[float]] = {}
        self._attempt_lock = asyncio.Lock()

        self._server: uvicorn.Server | None = None
        self._server_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None

        self._app = FastAPI(title="Engram WebUI", version="1.0.0")
        self._setup_routes()

    async def start(self):
        """启动 WebUI 服务"""
        if self._server_task and not self._server_task.done():
            logger.warning("Engram WebUI 服务已经在运行")
            return

        config = uvicorn.Config(
            app=self._app,
            host=self.host,
            port=self.port,
            log_level="info",
            loop="asyncio",
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(self._server.serve())

        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())

        for _ in range(50):
            if getattr(self._server, "started", False):
                logger.info("Engram WebUI 已启动: http://%s:%s", self.host, self.port)
                return
            if self._server_task.done():
                error = self._server_task.exception()
                raise RuntimeError(f"Engram WebUI 启动失败: {error}") from error
            await asyncio.sleep(0.1)

        logger.warning("Engram WebUI 启动耗时较长，仍在后台启动中")

    async def stop(self):
        """停止 WebUI 服务"""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        if self._server:
            self._server.should_exit = True
        if self._server_task:
            await self._server_task

        self._server = None
        self._server_task = None
        self._cleanup_task = None
        logger.info("Engram WebUI 已停止")

    async def _periodic_cleanup(self):
        """定期清理过期 token 与失败记录"""
        while True:
            try:
                await asyncio.sleep(300)
                async with self._token_lock:
                    await self._cleanup_tokens_locked()
                async with self._attempt_lock:
                    await self._cleanup_failed_attempts_locked()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Engram WebUI 清理任务出错: %s", exc)

    async def _cleanup_tokens_locked(self):
        now = time.time()
        expired = []
        for token, info in self._tokens.items():
            created_at = info.get("created_at", 0)
            last_active = info.get("last_active", 0)
            max_lifetime = info.get("max_lifetime", 86400)

            if now - created_at > max_lifetime:
                expired.append(token)
            elif now - last_active > self.session_timeout:
                expired.append(token)

        for token in expired:
            self._tokens.pop(token, None)

    async def _cleanup_failed_attempts_locked(self):
        now = time.time()
        expired_ips = []
        for ip, attempts in self._failed_attempts.items():
            recent = [t for t in attempts if now - t < self.login_window_seconds]
            if recent:
                self._failed_attempts[ip] = recent
            else:
                expired_ips.append(ip)

        for ip in expired_ips:
            self._failed_attempts.pop(ip, None)

    async def _check_rate_limit(self, client_ip: str) -> bool:
        async with self._attempt_lock:
            await self._cleanup_failed_attempts_locked()
            attempts = self._failed_attempts.get(client_ip, [])
            recent = [t for t in attempts if time.time() - t < self.login_window_seconds]
            return len(recent) < self.login_max_attempts

    async def _record_failed_attempt(self, client_ip: str):
        async with self._attempt_lock:
            self._failed_attempts.setdefault(client_ip, []).append(time.time())

    def _auth_dependency(self):
        async def dependency(request: Request) -> str:
            if self._auth_disabled:
                return "public"
            token = self._extract_token(request)
            await self._validate_token(token)
            return token

        return dependency

    async def _validate_token(self, token: str):
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="未提供认证 Token",
            )

        async with self._token_lock:
            info = self._tokens.get(token)
            if not info:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Token 无效或已过期",
                )

            now = time.time()
            created_at = info.get("created_at", 0)
            last_active = info.get("last_active", 0)
            max_lifetime = info.get("max_lifetime", 86400)

            if now - created_at > max_lifetime:
                self._tokens.pop(token, None)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 已过期"
                )

            if now - last_active > self.session_timeout:
                self._tokens.pop(token, None)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail="会话已超时"
                )

            info["last_active"] = now

    def _extract_token(self, request: Request) -> str:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:]
        return request.headers.get("X-Auth-Token", "")

    async def _run_in_executor(self, func: Callable, *args):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self.logic.executor, func, *args)

    async def _collect_stats(self, db, user_id=None):
        MemoryIndex = db.MemoryIndex
        if user_id:
            stats = await self._run_in_executor(db.get_message_stats, user_id)

            def _count_indexes():
                with db.db.connection_context():
                    return MemoryIndex.select().where(MemoryIndex.user_id == user_id).count()
        else:
            stats = await self._run_in_executor(db.get_all_users_stats)

            def _count_indexes():
                with db.db.connection_context():
                    return MemoryIndex.select().count()

        memory_index_count = await self._run_in_executor(_count_indexes)
        stats = dict(stats or {})
        stats["memory_index_count"] = memory_index_count
        if hasattr(db, "get_all_group_ids"):
            try:
                group_ids = await self._run_in_executor(db.get_all_group_ids)
                stats["group_count"] = len(group_ids or [])
            except Exception:
                pass
        stats["db_path"] = getattr(db, "db_path", None) or getattr(
            getattr(db, "_backend", None), "db_path", None
        )
        return stats

    async def _get_history_stats(self):
        """获取近 7 日消息增长趋势"""
        import datetime
        now = datetime.datetime.now()
        history = []
        for i in range(6, -1, -1):
            date = now - datetime.timedelta(days=i)
            start = date.replace(hour=0, minute=0, second=0, microsecond=0)
            end = date.replace(hour=23, minute=59, second=59, microsecond=999999)
            
            def _count_range(db, s, e):
                RawMemory = db.RawMemory
                with db.db.connection_context():
                    return RawMemory.select().where((RawMemory.timestamp >= s) & (RawMemory.timestamp <= e)).count()

            p_count = await self._run_in_executor(_count_range, self.db, start, end)
            g_count = 0
            group_db = await self._get_group_db()
            if group_db:
                g_count = await self._run_in_executor(_count_range, group_db, start, end)
            
            history.append({
                "date": date.strftime("%m-%d"),
                "private_count": p_count,
                "group_count": g_count
            })
        return history

    async def _get_group_db(self):
        if not self.config.get("enable_group_memory", False):
            return None
        group_db = getattr(self.plugin, "_group_db", None)
        if group_db is not None:
            return group_db
        try:
            await self.plugin._ensure_group_memory_manager()
        except Exception as exc:
            logger.warning("Engram WebUI 初始化群聊记忆失败: %s", exc)
        return getattr(self.plugin, "_group_db", None)

    @staticmethod
    def _parse_group_member_snapshot(user_name: str | None) -> tuple[str | None, str | None]:
        text = str(user_name or "").strip()
        if not text:
            return None, None
        if text.endswith(")") and "(" in text:
            left, right = text.rsplit("(", 1)
            member_id = right[:-1].strip()
            if member_id:
                member_name = left.strip() or member_id
                return member_id, member_name
        return None, text

    def _load_group_memory_raw_messages(self, group_db, memory_index):
        if not memory_index or not getattr(memory_index, "ref_uuids", None):
            return []
        try:
            uuids = json.loads(memory_index.ref_uuids)
        except Exception:
            return []
        if not isinstance(uuids, list) or not uuids:
            return []
        return group_db.get_memories_by_uuids(uuids)

    def _extract_group_memory_meta(self, memory_index, raw_msgs=None, fallback_group_id: str = ""):
        group_id = str(
            getattr(memory_index, "group_id", "")
            or fallback_group_id
            or getattr(memory_index, "user_id", "")
            or ""
        )
        member_id = str(getattr(memory_index, "member_id", "") or "")
        member_name = member_id
        participants = []
        seen = set()

        for msg in raw_msgs or []:
            if getattr(msg, "role", "") != "user":
                continue
            current_member_id = str(getattr(msg, "member_id", "") or "")
            current_member_name = None
            if not current_member_id:
                current_member_id, current_member_name = self._parse_group_member_snapshot(
                    getattr(msg, "user_name", None)
                )
            if current_member_id:
                current_member_name = current_member_name or getattr(msg, "user_name", None) or current_member_id
                if not member_id:
                    member_id = current_member_id
                    member_name = current_member_name or current_member_id
                if current_member_id not in seen:
                    participants.append(
                        {
                            "member_id": current_member_id,
                            "member_name": current_member_name or current_member_id,
                        }
                    )
                    seen.add(current_member_id)

        return {
            "group_id": group_id,
            "member_id": member_id,
            "member_name": member_name,
            "participants": participants,
        }

    def _serialize_group_memory_item(self, memory_index, raw_msgs=None, fallback_group_id: str = ""):
        meta = self._extract_group_memory_meta(
            memory_index, raw_msgs=raw_msgs, fallback_group_id=fallback_group_id
        )
        created_at = self.logic._ensure_datetime(memory_index.created_at)
        return {
            "id": memory_index.index_id,
            "group_id": meta["group_id"],
            "member_id": meta["member_id"],
            "member_name": meta["member_name"],
            "summary": memory_index.summary,
            "source_type": memory_index.source_type,
            "active_score": getattr(memory_index, "active_score", 100),
            "created_at": created_at.isoformat(),
        }

    def _group_memory_matches_member(self, member_id: str, raw_msgs) -> bool:
        normalized_member_id = str(member_id or "").strip()
        if not normalized_member_id:
            return True
        for msg in raw_msgs or []:
            if getattr(msg, "role", "") != "user":
                continue
            current_member_id = str(getattr(msg, "member_id", "") or "").strip()
            if not current_member_id:
                current_member_id, _ = self._parse_group_member_snapshot(getattr(msg, "user_name", None))
            if current_member_id == normalized_member_id:
                return True
        return False

    def _model_has_table_column(self, db, model, column_name: str) -> bool:
        backend = getattr(db, "_backend", db)
        getter = getattr(backend, "_get_table_columns", None)
        if callable(getter):
            try:
                return str(column_name) in getter(model)
            except Exception:
                return hasattr(model, column_name)
        return hasattr(model, column_name)

    def _setup_routes(self):
        static_dir = Path(__file__).resolve().parent / "webui" / "static"
        index_path = static_dir / "index.html"

        if not index_path.exists():
            logger.warning("Engram WebUI 未找到前端文件: %s", index_path)

        cors_origin = str(self.config.get("webui_cors_origin", "")).strip()
        allow_origins = [
            f"http://{self.host}:{self.port}",
            "http://localhost",
            "http://127.0.0.1",
        ]
        if cors_origin:
            allow_origins = [cors_origin]

        self._app.add_middleware(
            CORSMiddleware,
            allow_origins=allow_origins,
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["Content-Type", "Authorization", "X-Auth-Token"],
            allow_credentials=True,
        )

        if static_dir.exists():
            self._app.mount("/static", StaticFiles(directory=static_dir), name="static")

        @self._app.get("/", response_class=HTMLResponse)
        async def serve_index():
            if not index_path.exists():
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="前端文件缺失")
            return HTMLResponse(index_path.read_text(encoding="utf-8"))

        @self._app.get("/api/health")
        async def health():
            return {"status": "ok", "version": "1.0.0"}

        @self._app.post("/api/login")
        async def login(request: Request, payload: dict[str, Any]):
            if self._auth_disabled:
                return {"token": "public", "expires_in": 0, "force_change": False}

            password = str(payload.get("password", "")).strip()
            if not password:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="密码不能为空")

            client_ip = "unknown"
            if request.client and request.client.host:
                client_ip = request.client.host

            if not await self._check_rate_limit(client_ip):
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="尝试次数过多，请稍后再试",
                )

            if password != self._access_password:
                await self._record_failed_attempt(client_ip)
                await asyncio.sleep(1.0)
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="认证失败")

            token = secrets.token_urlsafe(32)
            now = time.time()
            max_lifetime = 86400

            async with self._token_lock:
                await self._cleanup_tokens_locked()
                self._tokens[token] = {
                    "created_at": now,
                    "last_active": now,
                    "max_lifetime": max_lifetime,
                }

            return {
                "token": token,
                "expires_in": self.session_timeout,
                "force_change": self._force_password_change,
            }

        @self._app.post("/api/logout")
        async def logout(token: str = Depends(self._auth_dependency())):
            if not self._auth_disabled:
                async with self._token_lock:
                    self._tokens.pop(token, None)
            return {"detail": "已退出登录"}

        @self._app.post("/api/password")
        async def update_password(
            payload: dict[str, Any], token: str = Depends(self._auth_dependency())
        ):
            del payload
            del token
            return {
                "success": False,
                "error": "当前为配置文件密码模式，请在插件配置中修改 webui_access_password 并重启插件。",
            }

        @self._app.get("/api/users")
        async def list_users(token: str = Depends(self._auth_dependency())):
            del token
            try:
                user_ids = await self._run_in_executor(self.db.get_all_user_ids)
                return {
                    "success": True,
                    "data": {"items": user_ids, "total": len(user_ids)},
                }
            except Exception as exc:
                logger.error("Engram WebUI 获取用户列表失败: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @self._app.get("/api/groups")
        async def list_groups(token: str = Depends(self._auth_dependency())):
            del token
            try:
                group_db = await self._get_group_db()
                if group_db is None:
                    return {"success": True, "data": {"items": [], "total": 0}}

                def _fetch_groups():
                    MemoryIndex = group_db.MemoryIndex
                    with group_db.db.connection_context():
                        rows = (
                            MemoryIndex.select(MemoryIndex.user_id)
                            .where(MemoryIndex.user_id.is_null(False))
                            .distinct()
                            .order_by(MemoryIndex.user_id.asc())
                        )
                        return [str(row.user_id) for row in rows if str(row.user_id or "").strip()]

                items = await self._run_in_executor(_fetch_groups)
                return {
                    "success": True,
                    "data": {"items": items, "total": len(items)},
                }
            except Exception as exc:
                logger.error("Engram WebUI 获取群组列表失败: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @self._app.get("/api/memories")
        async def list_memories(
            request: Request,
            token: str = Depends(self._auth_dependency()),
        ):
            del token
            query = request.query_params
            user_id = query.get("user_id")
            source_type = query.get("source_type")
            page = max(1, int(query.get("page", 1)))
            page_size = max(1, int(query.get("page_size", 20)))
            page_size = min(page_size, 200)
            offset = (page - 1) * page_size

            def _fetch():
                MemoryIndex = self.db.MemoryIndex
                with self.db.db.connection_context():
                    db_query = MemoryIndex.select()
                    if user_id:
                        db_query = db_query.where(MemoryIndex.user_id == user_id)
                    if source_type:
                        db_query = db_query.where(MemoryIndex.source_type == source_type)

                    total = db_query.count()
                    items = (
                        db_query.order_by(MemoryIndex.created_at.desc())
                        .limit(page_size)
                        .offset(offset)
                    )
                    data = []
                    for item in items:
                        created_at = self.logic._ensure_datetime(item.created_at)
                        data.append(
                            {
                                "id": item.index_id,
                                "summary": item.summary,
                                "user_id": item.user_id,
                                "source_type": item.source_type,
                                "active_score": item.active_score,
                                "created_at": created_at.isoformat(),
                            }
                        )
                    return total, data

            try:
                total, data = await self._run_in_executor(_fetch)
                return {
                    "success": True,
                    "data": {
                        "items": data,
                        "total": total,
                        "page": page,
                        "page_size": page_size,
                        "has_more": (offset + page_size) < total,
                    },
                }
            except Exception as exc:
                logger.error("Engram WebUI 获取记忆列表失败: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @self._app.get("/api/memories/{memory_id}")
        async def get_memory_detail(
            memory_id: str,
            request: Request,
            token: str = Depends(self._auth_dependency()),
        ):
            del token
            user_id = request.query_params.get("user_id")
            try:
                if not user_id:
                    memory_index = await self._run_in_executor(
                        self.db.get_memory_index_by_id, memory_id
                    )
                    if memory_index:
                        user_id = memory_index.user_id

                if not user_id:
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST, detail="需要提供 user_id"
                    )

                memory_index, raw_msgs = await self.logic.get_memory_detail_by_id(
                    user_id, memory_id
                )
                if not memory_index:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, detail="记忆不存在")

                created_at = self.logic._ensure_datetime(memory_index.created_at)
                payload = {
                    "index_id": memory_index.index_id,
                    "summary": memory_index.summary,
                    "user_id": memory_index.user_id,
                    "source_type": memory_index.source_type,
                    "active_score": memory_index.active_score,
                    "created_at": created_at.isoformat(),
                }

                messages = []
                for msg in raw_msgs or []:
                    ts = self.logic._ensure_datetime(msg.timestamp)
                    messages.append(
                        {
                            "uuid": msg.uuid,
                            "role": msg.role,
                            "user_name": msg.user_name,
                            "content": msg.content,
                            "timestamp": ts.isoformat(),
                        }
                    )

                return {
                    "success": True,
                    "data": {
                        "memory": payload,
                        "messages": messages,
                        "ai_name": str(self.config.get("ai_name") or "助手").strip() or "助手",
                    },
                }
            except HTTPException:
                raise
            except Exception as exc:
                logger.error("Engram WebUI 获取记忆详情失败: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @self._app.post("/api/memories/search")
        async def search_memories(
            payload: dict[str, Any], token: str = Depends(self._auth_dependency())
        ):
            del token
            query = str(payload.get("query", "")).strip()
            user_id = str(payload.get("user_id", "")).strip()
            if not query or not user_id:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail="需要提供 query 与 user_id",
                )

            limit = min(200, max(1, int(payload.get("limit", 50))))
            source_types = payload.get("source_types")
            start_time_raw = payload.get("start_time")
            end_time_raw = payload.get("end_time")

            def _parse_time(value):
                if not value:
                    return None
                if isinstance(value, (int, float)):
                    try:
                        import datetime
                        return datetime.datetime.fromtimestamp(value)
                    except Exception:
                        return None
                if isinstance(value, str):
                    try:
                        import datetime
                        return datetime.datetime.fromisoformat(value)
                    except Exception:
                        return None
                return None

            start_time = _parse_time(start_time_raw)
            end_time = _parse_time(end_time_raw)

            def _search():
                with self.db.db.connection_context():
                    rows = self.db.search_memory_indexes_by_keywords(
                        user_id=user_id,
                        keywords=[query],
                        limit=limit,
                        start_time=start_time,
                        end_time=end_time,
                        source_types=source_types,
                    )
                    items = []
                    for item in rows:
                        created_at = self.logic._ensure_datetime(item.created_at)
                        items.append(
                            {
                                "id": item.index_id,
                                "summary": item.summary,
                                "user_id": item.user_id,
                                "source_type": item.source_type,
                                "active_score": item.active_score,
                                "created_at": created_at.isoformat(),
                            }
                        )
                    return items

            try:
                items = await self._run_in_executor(_search)
                return {"success": True, "data": {"items": items}}
            except Exception as exc:
                logger.error("Engram WebUI 搜索记忆失败: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @self._app.delete("/api/memories/{memory_id}")
        async def delete_memory(
            memory_id: str,
            request: Request,
            token: str = Depends(self._auth_dependency()),
        ):
            del token
            user_id = request.query_params.get("user_id")
            delete_raw = request.query_params.get("delete_raw", "false").lower() == "true"

            try:
                if not user_id:
                    memory_index = await self._run_in_executor(
                        self.db.get_memory_index_by_id, memory_id
                    )
                    if memory_index:
                        user_id = memory_index.user_id

                if not user_id:
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST, detail="需要提供 user_id"
                    )

                success, message, summary = await self.logic.delete_memory_by_id(
                    user_id, memory_id, delete_raw=delete_raw
                )
                if not success:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, detail=message)

                return {
                    "success": True,
                    "data": {"message": message, "summary": summary},
                }
            except HTTPException:
                raise
            except Exception as exc:
                logger.error("Engram WebUI 删除记忆失败: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @self._app.post("/api/memories/undo")
        async def undo_memory(
            payload: dict[str, Any], token: str = Depends(self._auth_dependency())
        ):
            del token
            user_id = str(payload.get("user_id", "")).strip()
            if not user_id:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, detail="需要提供 user_id"
                )

            try:
                success, message, summary = await self.logic.undo_last_delete(user_id)
                if not success:
                    return {"success": False, "error": message}
                return {"success": True, "data": {"summary": summary}}
            except Exception as exc:
                logger.error("Engram WebUI 撤销删除失败: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @self._app.get("/api/group-memories")
        async def list_group_memories(
            request: Request,
            token: str = Depends(self._auth_dependency()),
        ):
            del token
            group_id = str(request.query_params.get("group_id", "") or "").strip()
            member_id = str(request.query_params.get("member_id", "") or "").strip()
            page = max(1, int(request.query_params.get("page", 1)))
            page_size = min(200, max(1, int(request.query_params.get("page_size", 20))))
            offset = (page - 1) * page_size

            try:
                group_db = await self._get_group_db()
                if group_db is None:
                    return {"success": False, "error": "群聊记忆未启用或未初始化"}

                def _fetch():
                    MemoryIndex = group_db.MemoryIndex
                    with group_db.db.connection_context():
                        query = MemoryIndex.select()
                        if group_id and self._model_has_table_column(group_db, MemoryIndex, "group_id"):
                            query = query.where(
                                (MemoryIndex.group_id == group_id)
                                | ((MemoryIndex.group_id.is_null(True)) & (MemoryIndex.user_id == group_id))
                            )
                        elif group_id:
                            query = query.where(MemoryIndex.user_id == group_id)
                        query = query.order_by(MemoryIndex.created_at.desc())

                        if not member_id:
                            total = query.count()
                            items = list(query.limit(page_size).offset(offset))
                            return total, [self._serialize_group_memory_item(item, fallback_group_id=group_id or getattr(item, "user_id", "")) for item in items]

                        matched_items = []
                        for item in query:
                            raw_msgs = self._load_group_memory_raw_messages(group_db, item)
                            if self._group_memory_matches_member(member_id, raw_msgs):
                                matched_items.append(
                                    self._serialize_group_memory_item(
                                        item,
                                        raw_msgs=raw_msgs,
                                        fallback_group_id=group_id or getattr(item, "user_id", ""),
                                    )
                                )
                        total = len(matched_items)
                        return total, matched_items[offset: offset + page_size]

                total, items = await self._run_in_executor(_fetch)
                return {
                    "success": True,
                    "data": {
                        "items": items,
                        "total": total,
                        "page": page,
                        "page_size": page_size,
                        "has_more": (offset + page_size) < total,
                    },
                }
            except HTTPException:
                raise
            except Exception as exc:
                logger.error("Engram WebUI 获取群聊记忆列表失败: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @self._app.post("/api/group-memories/search")
        async def search_group_memories(
            payload: dict[str, Any], token: str = Depends(self._auth_dependency())
        ):
            del token
            query = str(payload.get("query", "") or "").strip()
            group_id = str(payload.get("group_id", "") or "").strip()
            member_id = str(payload.get("member_id", "") or "").strip()
            if not query:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail="需要提供 query",
                )

            limit = min(200, max(1, int(payload.get("limit", 50))))

            try:
                group_db = await self._get_group_db()
                if group_db is None:
                    return {"success": False, "error": "群聊记忆未启用或未初始化"}

                def _search():
                    MemoryIndex = group_db.MemoryIndex
                    with group_db.db.connection_context():
                        rows = MemoryIndex.select()
                        if group_id and self._model_has_table_column(group_db, MemoryIndex, "group_id"):
                            rows = rows.where(
                                ((MemoryIndex.group_id == group_id)
                                 | ((MemoryIndex.group_id.is_null(True)) & (MemoryIndex.user_id == group_id)))
                                & MemoryIndex.summary.contains(query)
                            )
                        elif group_id:
                            rows = rows.where(
                                (MemoryIndex.user_id == group_id) & MemoryIndex.summary.contains(query)
                            )
                        else:
                            rows = rows.where(MemoryIndex.summary.contains(query))
                        rows = rows.order_by(MemoryIndex.created_at.desc()).limit(limit)
                        items = []
                        for item in rows:
                            raw_msgs = None
                            if member_id:
                                raw_msgs = self._load_group_memory_raw_messages(group_db, item)
                                if not self._group_memory_matches_member(member_id, raw_msgs):
                                    continue
                            items.append(
                                self._serialize_group_memory_item(
                                    item,
                                    raw_msgs=raw_msgs,
                                    fallback_group_id=group_id or getattr(item, "user_id", ""),
                                )
                            )
                        return items

                items = await self._run_in_executor(_search)
                return {
                    "success": True,
                    "data": {
                        "items": items,
                        "total": len(items),
                        "has_more": False,
                    },
                }
            except HTTPException:
                raise
            except Exception as exc:
                logger.error("Engram WebUI 搜索群聊记忆失败: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @self._app.get("/api/group-memories/{memory_id}")
        async def get_group_memory_detail(
            memory_id: str,
            request: Request,
            token: str = Depends(self._auth_dependency()),
        ):
            del token
            group_id = str(request.query_params.get("group_id", "") or "").strip()
            if not group_id:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail="需要提供 group_id",
                )

            try:
                group_db = await self._get_group_db()
                if group_db is None:
                    await self.plugin._ensure_group_memory_manager()
                    group_db = await self._get_group_db()
                if group_db is None:
                    return {"success": False, "error": "群聊记忆未启用或未初始化"}

                def _fetch_detail():
                    MemoryIndex = group_db.MemoryIndex
                    with group_db.db.connection_context():
                        query = MemoryIndex.select().where(MemoryIndex.index_id == memory_id)
                        if self._model_has_table_column(group_db, MemoryIndex, "group_id"):
                            query = query.where(
                                (MemoryIndex.group_id == group_id)
                                | ((MemoryIndex.user_id == group_id) & MemoryIndex.group_id.is_null(True))
                            )
                        else:
                            query = query.where(MemoryIndex.user_id == group_id)
                        memory_index = query.first()
                    raw_msgs = self._load_group_memory_raw_messages(group_db, memory_index)
                    return memory_index, raw_msgs

                memory_index, raw_msgs = await self._run_in_executor(_fetch_detail)
                if not memory_index:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, detail="记忆不存在")

                meta = self._extract_group_memory_meta(memory_index, raw_msgs=raw_msgs, fallback_group_id=group_id)
                created_at = self.logic._ensure_datetime(memory_index.created_at)
                payload = {
                    "index_id": memory_index.index_id,
                    "group_id": meta["group_id"],
                    "member_id": meta["member_id"],
                    "summary": memory_index.summary,
                    "source_type": memory_index.source_type,
                    "active_score": memory_index.active_score,
                    "created_at": created_at.isoformat(),
                    "participants": meta["participants"],
                }

                messages = []
                for msg in raw_msgs or []:
                    ts = self.logic._ensure_datetime(msg.timestamp)
                    current_member_id, current_member_name = self._parse_group_member_snapshot(
                        getattr(msg, "user_name", None)
                    )
                    messages.append(
                        {
                            "uuid": msg.uuid,
                            "role": msg.role,
                            "user_name": current_member_name or msg.user_name,
                            "member_id": current_member_id,
                            "content": msg.content,
                            "timestamp": ts.isoformat(),
                        }
                    )

                return {
                    "success": True,
                    "data": {
                        "memory": payload,
                        "messages": messages,
                        "ai_name": str(self.config.get("ai_name") or "助手").strip() or "助手",
                    },
                }
            except HTTPException:
                raise
            except Exception as exc:
                logger.error("Engram WebUI 获取群聊记忆详情失败: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @self._app.delete("/api/group-memories/{memory_id}")
        async def delete_group_memory(
            memory_id: str,
            request: Request,
            token: str = Depends(self._auth_dependency()),
        ):
            del token
            group_id = str(request.query_params.get("group_id", "") or "").strip()
            delete_raw = request.query_params.get("delete_raw", "false").lower() == "true"
            if not group_id:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail="需要提供 group_id",
                )

            try:
                group_db = await self._get_group_db()
                group_manager = getattr(self.plugin, "_group_memory_manager", None)
                if group_db is None or group_manager is None:
                    await self.plugin._ensure_group_memory_manager()
                    group_db = await self._get_group_db()
                    group_manager = getattr(self.plugin, "_group_memory_manager", None)
                if group_db is None or group_manager is None:
                    return {"success": False, "error": "群聊记忆未启用或未初始化"}

                def _find_owner_user_id():
                    MemoryIndex = group_db.MemoryIndex
                    with group_db.db.connection_context():
                        query = MemoryIndex.select().where(MemoryIndex.index_id == memory_id)
                        if self._model_has_table_column(group_db, MemoryIndex, "group_id"):
                            query = query.where(
                                (MemoryIndex.group_id == group_id)
                                | ((MemoryIndex.user_id == group_id) & MemoryIndex.group_id.is_null(True))
                            )
                        else:
                            query = query.where(MemoryIndex.user_id == group_id)
                        item = query.first()
                        return item.user_id if item else ""

                owner_user_id = await self._run_in_executor(_find_owner_user_id)
                if not owner_user_id:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, detail="记忆不存在")

                success, message, summary = await group_manager.delete_memory_by_id(
                    owner_user_id, memory_id, delete_raw=delete_raw
                )
                if not success:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, detail=message)

                return {
                    "success": True,
                    "data": {"message": message, "summary": summary},
                }
            except HTTPException:
                raise
            except Exception as exc:
                logger.error("Engram WebUI 删除群聊记忆失败: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @self._app.get("/api/stats")
        async def get_stats(request: Request, token: str = Depends(self._auth_dependency())):
            del token
            usry_params.get("user_id")
            try:
                stats = await self._collect_stats(self.db, user_id=user_id)
                return {"success": True, "data": stats}
            except Exception as exc:
                logger.error("Engram WebUI 获取统计信息失败: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @self._app.get("/api/stats/overview")
        async def get_stats_overview(token: str = Depends(self._auth_dependency())):
            del token
            try:
                private_stats = await self._collect_stats(self.db)
                group_db = await self._get_group_db()
                group_stats = None
                if group_db is not None:
                    group_stats = await self._collect_stats(group_db)
                
                history = await self._get_history_stats()
                
                return {
                    "success": True,
                    "data": {
                        "private": private_stats,
                        "group": group_stats,
                        "group_enabled": group_db is not None,
                        "history": history
                    },
                }
            except Exception as exc:
                logger.error("Engram WebUI 获取统计概览失败: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @self._app.get("/api/stat")
        async def get_stats_alias(request: Request, token: str = Depends(self._auth_dependency())):
            return await get_stats(request, token)

        @self._app.get("/api/activities")
        async def get_activities(request: Request, token: str = Depends(self._auth_dependency())):
            del token
            try:
                limit = request.query_params.get("limit", 8)
                manager = getattr(self.logic, "_memory_manager", None)
                if manager and hasattr(manager, "get_recent_activities"):
                    data = manager.get_recent_activities(limit=limit)
                else:
                    data = []
                return {"success": True, "data": data}
            except Exception as exc:
                logger.error("Engram WebUI 获取近期动态失败: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @self._app.get("/api/profile/{user_id}")
        async def get_profile(user_id: str, token: str = Depends(self._auth_dependency())):
            del token
            try:
                profile = await self.logic.get_user_profile(user_id)
                return {"success": True, "data": profile}
            except Exception as exc:
                logger.error("Engram WebUI 获取画像失败: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @self._app.get("/api/profile/{user_id}/render")
        async def render_profile_image(user_id: str, token: str = Depends(self._auth_dependency())):
            del token
            try:
                # 调用 ProfileRenderer 渲染图片
                from fastapi.responses import Response
                from .profile_renderer import ProfileRenderer
                renderer = ProfileRenderer(self.config, self.plugin.plugin_data_dir)
                profile = await self.logic.get_user_profile(user_id)
                
                # 获取记忆总数用于显示羁绊等级（可选）
                memory_count = 0
                try:
                    stats = await self._run_in_executor(self.db.get_message_stats, user_id)
                    memory_count = stats.get("total_messages", 0) if stats else 0
                except: pass

                # 直接调用 async 的 render 方法，不要用 _run_in_executor
                image_bytes = await renderer.render(user_id, profile, memory_count=memory_count)
                
                # 关闭 renderer 的 session (虽然目前是单次使用)
                await renderer.close()
                
                return Response(content=image_bytes, media_type="image/png")
            except Exception as exc:
                logger.error("Engram WebUI 渲染画像失败: %s", exc, exc_info=True)
                raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

        @self._app.post("/api/profile/{user_id}")
        async def update_profile(
            user_id: str,
            payload: dict[str, Any],
            token: str = Depends(self._auth_dependency()),
        ):
            del token
            try:
                result = await self.logic.update_user_profile(user_id, payload)
                return {"success": True, "data": result}
            except Exception as exc:
                logger.error("Engram WebUI 更新画像失败: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @self._app.post("/api/profile/{user_id}/remove-item")
        async def remove_profile_item(
            user_id: str,
            payload: dict[str, Any],
            token: str = Depends(self._auth_dependency()),
        ):
            del token
            try:
                field_path = str(payload.get("field_path", "") or "").strip()
                value = str(payload.get("value", "") or "").strip()
                success, message = await self.logic._profile_manager.remove_profile_list_item(
                    user_id=user_id,
                    field_path=field_path,
                    value=value,
                )
                if not success:
                    return {"success": False, "error": message}
                profile = await self.logic.get_user_profile(user_id)
                return {"success": True, "data": profile, "message": message}
            except Exception as exc:
                logger.error("Engram WebUI 删除画像标签失败: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @self._app.delete("/api/profile/{user_id}")
        async def clear_profile(user_id: str, token: str = Depends(self._auth_dependency())):
            del token
            try:
                result = await self.logic.clear_user_profile(user_id)
                return {"success": True, "data": result}
            except Exception as exc:
                logger.error("Engram WebUI 清除画像失败: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}

        @self._app.post("/api/maintenance/rebuild-vectors")
        async def rebuild_vectors(
            payload: dict[str, Any], token: str = Depends(self._auth_dependency())
        ):
            del token
            full_rebuild = bool(payload.get("full_rebuild", False))
            batch_size = int(payload.get("batch_size", 200))
            batch_size = max(50, min(batch_size, 500))

            try:
                result = await self.logic.rebuild_vector_collection(
                    full_rebuild=full_rebuild, batch_size=batch_size
                )
                return {"success": True, "data": result}
            except Exception as exc:
                logger.error("Engram WebUI 重建向量库失败: %s", exc, exc_info=True)
                return {"success": False, "error": str(exc)}


__all__ = ["EngramWebServer"]
