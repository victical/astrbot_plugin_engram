from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig, logger
from astrbot.api.message_components import Image, At

# 核心模块
from .core import AffinityMemoryProvider, MemoryFacade, MemoryScheduler, MemoryManager
from .handlers import MemoryCommandHandler, ProfileCommandHandler, OneBotSyncHandler, MemoryToolHandler
from .export_handler import ExportHandler
from .profile_renderer import ProfileRenderer
from .db_manager import DatabaseManager, StableDatabaseInterface
from .services import (
    BondCalculator,
    LLMContextInjector,
    IntentClassifier,
    TopicMemoryCacheService,
    ToolHintStrategyService,
    ConfigPresetService,
    TimeExpressionService,
    FriendCacheService,
)
from .webui_server import EngramWebServer
from . import utils as utils_module

import asyncio
import re
import os


class FriendAddNoticeFilter(filter.CustomFilter):
    """过滤 OneBot friend_add notice 事件。"""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if not isinstance(raw, dict):
            return False
        return raw.get("post_type") == "notice" and raw.get("notice_type") == "friend_add"


@register("astrbot_plugin_engram", "victical", "仿生双轨记忆系统", "1.6.7")
class EngramPlugin(Star):
    """
    Engram 仿生双轨记忆系统插件
    
    架构说明：
    - main.py 作为纯路由层，仅负责装饰器绑定和参数解析
    - 业务逻辑委托给 handlers/（命令处理）和 core/（核心功能）
    - 调度任务由 MemoryScheduler 统一管理
    """
    
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        # 兼容不同版本的 AstrBot 框架
        self.config = config if config is not None else context.get_config() if hasattr(context, 'get_config') else {}
        self.config = ConfigPresetService(self.config).apply()
        from astrbot.api.star import StarTools
        self.plugin_data_dir = StarTools.get_data_dir()

        # 初始化核心组件（统一使用预设合并后的配置）
        self.logic = MemoryFacade(context, self.config, self.plugin_data_dir)
        self.affinity_memory_provider = self._build_affinity_memory_provider()
        self.export_handler = ExportHandler(self.logic, self.plugin_data_dir)
        self.profile_renderer = ProfileRenderer(self.config, self.plugin_data_dir)

        # 初始化命令处理器（委托业务逻辑）
        self._mem_handler = MemoryCommandHandler(
            self.config, self.logic._memory_manager, self.logic.db, self.logic.executor
        )
        self._profile_handler = ProfileCommandHandler(
            self.config, self.logic._profile_manager, self.logic.db,
            self.profile_renderer, self.logic.executor
        )
        self._onebot_handler = OneBotSyncHandler(self.logic._profile_manager, utils_module=utils_module)
        self._tool_handler = MemoryToolHandler(self.config, self.logic)
        self._llm_injector = LLMContextInjector(config=self.config)
        self._intent_classifier = IntentClassifier(config=self.config, context=context)
        self._topic_cache_service = TopicMemoryCacheService(config=self.config)
        self._tool_hint_strategy = ToolHintStrategyService(config=self.config)
        self._time_parser = TimeExpressionService(config=self.config)
        self._friend_cache = FriendCacheService(config=self.config)
        self._group_memory_manager = None
        self._group_scheduler = None
        self._group_mem_handler = None
        self._group_db = None
        self._group_memory_init_lock = None
        self._group_memory_init_lock_loop = None
        self._startup_tasks = []
        self._startup_task_errors = {}
        self._group_memory_prewarm_started = False

        # 初始化调度器
        self._scheduler = MemoryScheduler(self.logic, self.config)
        self._create_background_task("private_scheduler_start", self._scheduler.start())
        self._maybe_start_group_memory_prewarm()

        # WebUI 服务端
        enable_webui_server, webui_host, webui_port = self._get_webui_settings()
        self._webui_server = None
        if enable_webui_server:
            try:
                logger.info(
                    "Engram：准备启动 WebUI 服务 host=%s port=%s",
                    webui_host,
                    webui_port
                )
                self._webui_server = EngramWebServer(self, host=webui_host, port=webui_port)
                self._create_background_task("webui_server_start", self._webui_server.start())
            except Exception as e:
                logger.error(f"Engram：WebUI 服务启动失败：{e}")
                self._webui_server = None

    @staticmethod
    def _is_profile_affinity_enabled(config: dict) -> bool:
        return bool((config or {}).get("enable_profile_affinity", True))

    def _build_affinity_memory_provider(self):
        # 只读适配器始终初始化，供外部 astrbot_plugin_affinity 读取画像与记忆数据。
        # 它本身是被动数据管道、无副作用，因此不受 enable_profile_affinity（仅控制
        # Engram 自身羁绊展示/注入）影响，避免关闭羁绊展示时连带禁用好感度插件。
        return AffinityMemoryProvider(
            logic=self.logic,
            db=self.logic.db,
            bond_calculator=BondCalculator(),
        )

    def _handle_background_task_done(self, task_name: str, task: asyncio.Task):
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is None:
            return

        self._startup_task_errors[task_name] = str(exc)
        logger.error("Engram 后台启动任务[%s]异常退出：%s", task_name, exc, exc_info=exc)

    def _create_background_task(self, task_name: str, coro):
        task = asyncio.create_task(coro)
        task.add_done_callback(lambda done_task, name=task_name: self._handle_background_task_done(name, done_task))
        self._startup_tasks.append(task)
        return task

    def _maybe_start_group_memory_prewarm(self):
        if not self.config.get("enable_group_memory", False):
            return None
        if getattr(self, "_group_memory_prewarm_started", False):
            return None
        if getattr(self, "_group_memory_manager", None) is not None:
            return None
        self._group_memory_prewarm_started = True
        return self._create_background_task("group_memory_prewarm", self._ensure_group_memory_manager())

    def _get_webui_settings(self):
        enabled = bool(self.config.get("enable_webui_server", False))
        host = str(self.config.get("webui_host", "0.0.0.0") or "0.0.0.0").strip() or "0.0.0.0"
        try:
            port = int(self.config.get("webui_port", 8080))
        except (TypeError, ValueError):
            port = 8080
        port = max(1, min(65535, port))
        return enabled, host, port

    def _get_group_memory_init_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._group_memory_init_lock is None or self._group_memory_init_lock_loop is not loop:
            self._group_memory_init_lock = asyncio.Lock()
            self._group_memory_init_lock_loop = loop
        return self._group_memory_init_lock

    def _iter_database_handles(self):
        seen = set()
        for db in (
            getattr(getattr(self, "logic", None), "db", None),
            getattr(self, "_group_db", None),
        ):
            if db is None:
                continue
            key = id(db)
            if key in seen:
                continue
            seen.add(key)
            yield db

    @staticmethod
    def _close_db_handle_for_current_thread(db):
        close_current = getattr(db, "close_thread_connection", None)
        if callable(close_current):
            close_current()
            return

        raw_db = getattr(db, "db", None)
        if raw_db is None:
            return
        try:
            if hasattr(raw_db, "is_closed") and raw_db.is_closed():
                return
            raw_db.close()
        except Exception as e:
            logger.debug("Engram：关闭当前线程数据库连接失败：%s", e)

    def _close_worker_database_connections(self):
        executor = getattr(getattr(self, "logic", None), "executor", None)
        if executor is None or getattr(executor, "_shutdown", False):
            return

        dbs = list(self._iter_database_handles())
        if not dbs:
            return

        try:
            max_workers = max(1, int(getattr(executor, "_max_workers", 1) or 1))
        except (TypeError, ValueError):
            max_workers = 1

        def _close_all_for_worker():
            for db in dbs:
                self._close_db_handle_for_current_thread(db)

        futures = []
        for _ in range(max_workers):
            try:
                futures.append(executor.submit(_close_all_for_worker))
            except Exception as e:
                logger.debug("Engram：提交数据库连接关闭任务失败：%s", e)
                break

        for future in futures:
            try:
                future.result(timeout=2.0)
            except Exception as e:
                logger.debug("Engram：等待数据库连接关闭任务失败：%s", e)

    def _is_command_message(self, content: str) -> bool:
        """检测消息是否为指令"""
        if not self.config.get("enable_command_filter", True):
            logger.debug(f"Engram：指令过滤已关闭，不进行过滤：{content[:30]}")
            return False
        
        text = content.strip()
        
        # 1. 检查指令前缀
        command_prefixes = self.config.get("command_prefixes", ["/", "!", "#", "~"])
        logger.debug(f"Engram：正在检查消息是否匹配指令前缀 {command_prefixes}：{text[:30]}")
        for prefix in command_prefixes:
            if text.startswith(prefix):
                logger.debug(f"Engram：消息命中前缀 '{prefix}'，已过滤")
                return True
        
        # 2. 检查完整指令匹配
        if self.config.get("enable_full_command_detection", False):
            full_commands = self.config.get("full_command_list", [])
            cleaned_text = "".join(text.split())
            for cmd in full_commands:
                if cleaned_text == "".join(str(cmd).split()):
                    return True
        
        return False

    def _parse_time_expr(self, text: str):
        """解析工具时间表达式，返回 (start_dt, end_dt, desc)。"""
        return self._time_parser.parse_time_expr(text)

    def _normalize_source_types(self, source_types, default_types=None):
        """归一化 source_types，支持 array 与逗号分隔字符串。"""
        return self._time_parser.normalize_source_types(source_types, default_types=default_types)

    def _get_topic_cache_service(self) -> TopicMemoryCacheService:
        """延迟获取话题缓存服务（兼容 __new__ 场景测试）。"""
        service = getattr(self, "_topic_cache_service", None)
        if service is None:
            service = TopicMemoryCacheService(config=self.config)
            self._topic_cache_service = service
        return service

    def _get_tool_hint_service(self) -> ToolHintStrategyService:
        """延迟获取工具提示策略服务（兼容 __new__ 场景测试）。"""
        service = getattr(self, "_tool_hint_strategy", None)
        if service is None:
            service = ToolHintStrategyService(config=self.config)
            self._tool_hint_strategy = service
        return service

    async def _ensure_group_memory_manager(self):
        """确保群聊记忆管理器已初始化（延迟创建）。"""
        if self._group_memory_manager is not None:
            return self._group_memory_manager

        if not self.config.get("enable_group_memory", False):
            return None

        async with self._get_group_memory_init_lock():
            if self._group_memory_manager is not None:
                return self._group_memory_manager

            group_db_path = os.path.join(self.plugin_data_dir, "engram_memories_group.db")
            raw_db = DatabaseManager(self.plugin_data_dir, db_path=group_db_path)
            group_db = StableDatabaseInterface(raw_db)
            group_db.verify_contract(stage="GroupMemoryManager.__init__")
            self._group_db = group_db

            group_chroma_path = os.path.join(self.plugin_data_dir, "engram_chroma_group")
            group_source_type = str(self.config.get("group_memory_source_type", "group")).strip() or "group"

            self._group_memory_manager = MemoryManager(
                context=self.context,
                config=self.config,
                data_dir=self.plugin_data_dir,
                executor=self.logic.executor,
                db_manager=group_db,
                profile_manager=None,
                chroma_path=group_chroma_path,
                default_source_type=group_source_type,
            )
            self._group_mem_handler = MemoryCommandHandler(
                self.config,
                self._group_memory_manager,
                self._group_db,
                self.logic.executor,
            )

            group_scheduler_config = dict(self.config)
            group_scheduler_config["enable_memory_folding"] = False
            group_scheduler_config["enable_monthly_folding"] = False
            group_scheduler_config["enable_yearly_folding"] = False

            self._group_scheduler = MemoryScheduler(self._group_memory_manager, group_scheduler_config)
            self._create_background_task("group_scheduler_start", self._group_scheduler.start())

        return self._group_memory_manager

    def _extract_at_user_ids(
        self, event: AstrMessageEvent, exclude_ids: set, max_count: int = 3
    ) -> list:
        """
        从消息链提取被 @ 的用户 ID 列表（去重 + 排除 bot 自身和发言者）。
        默认上限 3 人，避免水群刷屏型 @ 灌爆 token。
        """
        try:
            chain = getattr(getattr(event, "message_obj", None), "message", None) or []
        except Exception:
            return []

        try:
            self_id = str(event.get_self_id() or "").strip()
        except Exception:
            self_id = ""

        excluded = {str(x).strip() for x in (exclude_ids or set()) if x}
        if self_id:
            excluded.add(self_id)

        seen = set()
        result = []
        for seg in chain:
            if not isinstance(seg, At):
                continue
            qq = getattr(seg, "qq", None)
            if qq is None:
                continue
            uid = str(qq).strip()
            if not uid or uid in excluded or uid in seen:
                continue
            seen.add(uid)
            result.append(uid)
            if len(result) >= max_count:
                break
        return result

    def _resolve_group_storage_id(self, group_id: str, sender_id: str) -> str:
        """根据配置决定群聊记忆的 session/user 绑定方式。"""
        if self.config.get("group_memory_private_session_only", False):
            storage_id = sender_id or group_id
            logger.debug(
                "Engram：resolve_group_storage_id private_only=True group_id=%s sender_id=%s storage_id=%s",
                group_id,
                sender_id,
                storage_id,
            )
            return storage_id
        mode = str(self.config.get("group_memory_store_session_as", "group_id")).strip().lower()
        if mode == "user_id":
            storage_id = sender_id or group_id
        else:
            storage_id = group_id
        logger.debug(
            "Engram：resolve_group_storage_id mode=%s group_id=%s sender_id=%s storage_id=%s",
            mode,
            group_id,
            sender_id,
            storage_id,
        )
        return storage_id

    async def _group_memory_friend_allowed(self, event: AstrMessageEvent) -> bool:
        """群聊好友白名单判断。"""
        if not self.config.get("group_memory_only_friends", True):
            return True
        bot = getattr(event, "bot", None)
        return await self._friend_cache.is_friend(event.get_sender_id(), bot=bot)

    async def _get_group_mem_handler(self):
        """获取群聊记忆命令处理器。"""
        if not self.config.get("enable_group_memory", False):
            return None
        if self._group_mem_handler is not None:
            return self._group_mem_handler
        try:
            await self._ensure_group_memory_manager()
        except Exception as e:
            logger.error("Engram：初始化群聊记忆命令处理器失败：%s", e, exc_info=True)
            return None
        return self._group_mem_handler

    @staticmethod
    def _rewrite_group_command_hints(text: str) -> str:
        """将私聊指令提示替换为群聊指令提示。"""
        if not text:
            return text
        replacements = {
            "/查看记忆详情": "/查看群记忆详情",
            "/删除全部记忆": "/删除全部群记忆",
            "/删除记忆": "/删除群记忆",
            "/查看记忆": "/查看群记忆",
            "/撤销删除记忆": "/撤销删除群记忆",
        }
        for src, dst in replacements.items():
            text = text.replace(src, dst)
        return text

    async def _run_plain_command(self, event: AstrMessageEvent, command_name: str, handler_call, transform=None):
        try:
            result = await handler_call()
            if transform is not None:
                result = transform(result)
            return event.plain_result(result)
        except Exception as e:
            logger.error("Engram command %s failed: %s", command_name, e, exc_info=True)
            return event.plain_result("❌ 命令执行失败，请稍后重试。")

    # 兼容保留：以下方法由 main 转发到 services.injection_strategy
    def _extract_topic_tokens(self, query: str):
        return self._get_topic_cache_service().extract_topic_tokens(query)

    @staticmethod
    def _topic_similarity(left_tokens, right_tokens) -> float:
        return TopicMemoryCacheService.topic_similarity(set(left_tokens or []), set(right_tokens or []))

    def _build_topic_cache_key(self, query: str) -> str:
        return self._get_topic_cache_service().build_topic_cache_key(query)

    def _get_topic_cache_ttl(self) -> int:
        return self._get_topic_cache_service()._get_ttl()

    def _get_topic_cache_max_topics(self) -> int:
        return self._get_topic_cache_service()._get_max_topics()

    def _prune_topic_cache(self, user_id: str):
        self._get_topic_cache_service()._prune(user_id)

    def _get_cached_topic_memories(self, user_id: str, query: str):
        return self._get_topic_cache_service().get_cached(user_id, query)

    def _set_cached_topic_memories(self, user_id: str, query: str, topic_key: str, memories):
        self._get_topic_cache_service().set_cached(user_id, query, topic_key, memories)

    def _should_inject_tool_hint(self, memory_count: int, should_retrieve: bool) -> bool:
        return self._get_tool_hint_service().should_inject(memory_count=memory_count, should_retrieve=should_retrieve)

    def _build_tool_hint_block(self, memory_count: int, should_retrieve: bool) -> str:
        if not self._should_inject_tool_hint(memory_count=memory_count, should_retrieve=should_retrieve):
            return ""
        return self._get_tool_hint_service().build_hint_text()

    async def _build_memory_search_output(
        self,
        event: AstrMessageEvent,
        query: str,
        limit: int,
        time_expr: str,
        source_types,
        mode: str = "hybrid",
        default_types=None,
        title: str = "🧠 工具检索结果",
        extra_hint: str = ""
    ) -> str:
        """统一构建记忆检索工具输出（委托给 handler）。"""
        async def _get_logic(evt: AstrMessageEvent):
            if not evt.get_group_id():
                return self.logic
            if not self.config.get("enable_group_memory", False):
                return self.logic
            return await self._ensure_group_memory_manager()

        def _resolve_user_id(evt: AstrMessageEvent):
            if not evt.get_group_id():
                return evt.get_sender_id()
            storage_id = self._resolve_group_storage_id(evt.get_group_id(), evt.get_sender_id())
            return storage_id or evt.get_sender_id()

        if event.get_group_id() and default_types is None:
            group_source_type = str(self.config.get("group_memory_source_type", "group")).strip() or "group"
            default_types = [group_source_type]
            if self.config.get("group_memory_allow_private_recall", False):
                default_types = [group_source_type, "private"]

        return await self._tool_handler.build_memory_search_output(
            event=event,
            query=query,
            limit=limit,
            time_expr=time_expr,
            source_types=source_types,
            mode=mode,
            default_types=default_types,
            title=title,
            extra_hint=extra_hint,
            parse_time_expr=self._parse_time_expr,
            normalize_source_types=self._normalize_source_types,
            get_logic=_get_logic,
            resolve_user_id=_resolve_user_id,
        )

    async def _build_session_search_output(
        self,
        event: AstrMessageEvent,
        query: str,
        window: int = 5,
        limit: int = 3,
    ) -> str:
        """构建原始会话检索工具输出（委托给 handler）。"""
        async def _get_search_results(evt: AstrMessageEvent, q: str, final_limit: int, final_window: int):
            loop = asyncio.get_running_loop()

            def _tag_results(rows, scope: str):
                tagged = []
                for row in rows or []:
                    if isinstance(row, dict):
                        item = dict(row)
                        item["scope"] = scope
                        tagged.append(item)
                return tagged

            if evt.get_group_id() and self.config.get("enable_group_memory", False):
                group_manager = await self._ensure_group_memory_manager()
                storage_id = self._resolve_group_storage_id(evt.get_group_id(), evt.get_sender_id())
                group_db = getattr(group_manager, "db", None) if group_manager is not None else None
                results = []
                if group_db is not None and callable(getattr(group_db, "search_raw_memory_sessions", None)):
                    group_rows = await loop.run_in_executor(
                        self.logic.executor,
                        group_db.search_raw_memory_sessions,
                        storage_id,
                        q,
                        final_limit,
                        final_window,
                    )
                    results.extend(_tag_results(group_rows, "group"))

                if self.config.get("group_memory_allow_private_recall", False):
                    private_rows = await loop.run_in_executor(
                        self.logic.executor,
                        self.logic.db.search_raw_memory_sessions,
                        evt.get_sender_id(),
                        q,
                        final_limit,
                        final_window,
                    )
                    results.extend(_tag_results(private_rows, "private"))
                return results[:final_limit]

            private_rows = await loop.run_in_executor(
                self.logic.executor,
                self.logic.db.search_raw_memory_sessions,
                evt.get_sender_id(),
                q,
                final_limit,
                final_window,
            )
            return _tag_results(private_rows, "private")

        return await self._tool_handler.build_session_search_output(
            event=event,
            query=query,
            window=window,
            limit=limit,
            get_search_results=_get_search_results,
        )

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """在调用 LLM 前注入长期记忆和用户画像"""
        if event.get_group_id():
            await self._handle_group_llm_request(event, req)
            return
        user_id = event.get_sender_id()
        query = event.message_str
        profile = await self.logic.get_user_profile(user_id)
        profile_block = self._llm_injector.build_profile_block(profile)
        
        memory_block = ""
        memories = []
        try:
            should_retrieve = await self._intent_classifier.should_retrieve_memory(query)
        except Exception as e:
            logger.warning(f"Engram：意图检查失败，已回退为跳过检索：{e}")
            should_retrieve = False

        if should_retrieve:
            cache_hit = False
            topic_key = ""
            try:
                cache_hit, memories, topic_key = self._get_cached_topic_memories(user_id, query)
            except Exception as e:
                logger.debug(f"Engram：话题缓存读取失败，已回退为直接检索：{e}")
                cache_hit, memories, topic_key = False, [], ""

            if not cache_hit:
                try:
                    memories = await self.logic.retrieve_memories(user_id, query)
                except Exception as e:
                    logger.error(f"Engram：on_llm_request 中 retrieve_memories 调用失败：{e}")
                    memories = []

                try:
                    self._set_cached_topic_memories(user_id, query, topic_key, memories)
                except Exception as e:
                    logger.debug(f"Engram：话题缓存写入失败，已忽略：{e}")
            else:
                logger.debug(f"Engram：话题缓存命中，user_id={user_id}，query={query[:30]}")

            if memories:
                memory_prompt = "\n".join(memories)
                memory_block = f"【长期记忆回溯】：\n{memory_prompt}\n"
        else:
            logger.debug(f"Engram：当前查询较弱，已跳过记忆检索：{query[:30]}")

        tool_hint_block = self._build_tool_hint_block(
            memory_count=len(memories),
            should_retrieve=should_retrieve
        )

        combined_memory_block = f"{memory_block}{tool_hint_block}"
        if profile_block or combined_memory_block:
            self._llm_injector.inject_context(
                req,
                profile_block,
                combined_memory_block,
                target=self.config.get("llm_injection_target", "system"),
            )
            
            # 调试模式：输出注入的内容
            if self.config.get("debug_injection", False):
                logger.info(f"=== Engram 调试模式 [用户: {user_id}] ===")
                if profile_block:
                    logger.info(f"📋 注入的用户画像:\n{profile_block}")
                if memory_block:
                    logger.info(f"🧠 注入的长期记忆:\n{memory_block}")
                if tool_hint_block:
                    logger.info(f"🛠️ 注入的工具提示:\n{tool_hint_block}")
                logger.info(f"=== Engram 调试结束 ===")

    async def _handle_group_llm_request(self, event: AstrMessageEvent, req):
        """群聊记忆注入与缓存（仅 LLM 触发时）。"""
        if not self.config.get("enable_group_memory", False):
            return

        if not await self._group_memory_friend_allowed(event):
            return

        content = event.message_str or ""
        if self._is_command_message(content):
            return

        try:
            min_len = int(self.config.get("group_memory_min_text_length", 6))
        except (TypeError, ValueError):
            min_len = 6

        if len(content.strip()) < max(1, min_len):
            return

        group_manager = await self._ensure_group_memory_manager()
        if group_manager is None:
            return

        group_id = event.get_group_id()
        sender_id = event.get_sender_id()
        user_name = event.get_sender_name()
        storage_id = self._resolve_group_storage_id(group_id, sender_id)
        group_source_type = str(self.config.get("group_memory_source_type", "group")).strip() or "group"

        event.set_extra("group_memory_pending", {
            "storage_id": storage_id,
            "group_id": group_id,
            "sender_id": sender_id,
            "user_name": user_name,
            "content": content,
            "source_type": group_source_type,
        })

        memory_block = ""
        memories = []
        try:
            should_retrieve = await self._intent_classifier.should_retrieve_memory(content)
        except Exception as e:
            logger.warning(f"Engram：群聊意图检查失败，已回退为跳过检索：{e}")
            should_retrieve = False

        if should_retrieve:
            cache_hit = False
            topic_key = ""
            group_memories = []
            private_memories = []
            try:
                cache_hit, memories, topic_key = self._get_cached_topic_memories(storage_id, content)
            except Exception as e:
                logger.debug(f"Engram：群聊话题缓存读取失败，已回退为直接检索：{e}")
                cache_hit, memories, topic_key = False, [], ""

            if cache_hit and self.config.get("group_memory_allow_private_recall", False):
                cache_hit = False

            if not cache_hit:
                try:
                    group_memories = await group_manager.retrieve_memories(
                        storage_id,
                        content,
                        source_types=[group_source_type]
                    )
                    memories = list(group_memories or [])
                    private_memories = []
                    if self.config.get("group_memory_allow_private_recall", False):
                        private_memories = await self.logic.retrieve_memories(
                            sender_id,
                            content,
                            source_types=["private"]
                        )
                        memories.extend([m for m in private_memories if m not in memories])
                except Exception as e:
                    logger.error(f"Engram：群聊 retrieve_memories 调用失败：{e}")
                    memories = []
                    group_memories = []
                    private_memories = []

                try:
                    self._set_cached_topic_memories(storage_id, content, topic_key, memories)
                except Exception as e:
                    logger.debug(f"Engram：群聊话题缓存写入失败，已忽略：{e}")
            else:
                group_memories = list(memories or [])
                logger.debug(f"Engram：群聊话题缓存命中，storage_id={storage_id}，query={content[:30]}")

            if memories:
                tagged_memories = []
                for item in memories:
                    if item in (group_memories or []):
                        tagged_memories.append(f"【群聊】{item}")
                    elif item in (private_memories or []):
                        tagged_memories.append(f"【私聊】{item}")
                    else:
                        tagged_memories.append(item)
                memory_prompt = "\n".join(tagged_memories)
                memory_block = f"【长期记忆回溯】：\n{memory_prompt}\n"
        else:
            logger.debug(f"Engram：群聊查询较弱，已跳过记忆检索：{content[:30]}")

        tool_hint_block = self._build_tool_hint_block(
            memory_count=len(memories),
            should_retrieve=should_retrieve
        )

        profile_block = ""
        try:
            profile = await self.logic.get_user_profile(sender_id)
            profile_block = self._llm_injector.build_profile_block(profile)
        except Exception as e:
            logger.debug(f"Engram：群聊画像读取失败，已跳过：{e}")

        # 当前消息中被 @ 的人（去重 + 排除 bot 自身和发言者，默认上限 3 人），
        # 各取一行精简画像并入"被提及的人"块。无画像则跳过。
        at_block = ""
        try:
            try:
                at_max = int(self.config.get("group_at_profile_max", 3))
            except (TypeError, ValueError):
                at_max = 3
            at_max = max(0, min(at_max, 10))
            if at_max > 0:
                at_user_ids = self._extract_at_user_ids(
                    event, exclude_ids={sender_id}, max_count=at_max,
                )
                compact_lines = []
                for uid in at_user_ids:
                    try:
                        at_profile = await self.logic.get_user_profile(uid)
                        line = self._llm_injector.build_compact_profile_block(at_profile)
                        if line:
                            compact_lines.append(line)
                    except Exception as e:
                        logger.debug(f"Engram：群聊被@用户画像读取失败 uid={uid}：{e}")
                at_block = self._llm_injector.build_at_target_block(compact_lines)
        except Exception as e:
            logger.debug(f"Engram：群聊@画像注入失败，已跳过：{e}")

        combined_memory_block = f"{memory_block}{tool_hint_block}"
        full_profile_block = f"{profile_block}\n{at_block}" if at_block else profile_block
        if full_profile_block or combined_memory_block:
            self._llm_injector.inject_context(
                req,
                full_profile_block,
                combined_memory_block,
                target=self.config.get("llm_injection_target", "system"),
            )

            if self.config.get("debug_injection", False):
                logger.info(f"=== Engram 群聊调试模式 [群: {group_id}] ===")
                if profile_block:
                    logger.info(f"📋 注入的用户画像:\n{profile_block}")
                if memory_block:
                    logger.info(f"🧠 注入的群聊记忆:\n{memory_block}")
                if tool_hint_block:
                    logger.info(f"🛠️ 注入的工具提示:\n{tool_hint_block}")
                logger.info("=== Engram 群聊调试结束 ===")

    async def _handle_group_after_message_sent(self, event: AstrMessageEvent):
        """群聊 LLM 回复后记录记忆。"""
        if not self.config.get("enable_group_memory", False):
            return

        pending = event.get_extra("group_memory_pending")
        if not pending:
            return

        result = event.get_result()
        if not result or not result.is_llm_result():
            return

        content = "".join([c.text for c in result.chain if hasattr(c, "text")])
        if not content:
            return

        group_manager = await self._ensure_group_memory_manager()
        if group_manager is None:
            return

        storage_id = pending.get("storage_id")
        sender_id = pending.get("sender_id")
        user_name = pending.get("user_name")
        user_content = pending.get("content")
        group_id = pending.get("group_id")

        if not storage_id or not user_content:
            return

        try:
            await group_manager.record_message(
                user_id=storage_id,
                session_id=storage_id,
                role="user",
                content=user_content,
                user_name=f"{user_name}({sender_id})" if user_name else str(sender_id or ""),
                group_id=group_id,
                member_id=sender_id,
            )
            await group_manager.record_message(
                user_id=storage_id,
                session_id=storage_id,
                role="assistant",
                content=content,
                user_name=str(self.config.get("ai_name") or "").strip(),
                group_id=group_id,
                member_id=sender_id,
            )
            logger.debug(
                "Engram：群聊记忆已记录 group_id=%s storage_id=%s",
                group_id,
                storage_id
            )
        except Exception as e:
            logger.error(f"Engram：群聊记忆记录失败：{e}")

    @filter.custom_filter(FriendAddNoticeFilter)
    async def on_friend_add_notice(self, event: AstrMessageEvent):
        """OneBot 好友添加通知：更新好友缓存。"""
        if not self.config.get("enable_group_memory", False):
            return

        user_id = event.get_sender_id()
        if not user_id:
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            if isinstance(raw, dict):
                user_id = raw.get("user_id")

        if not user_id:
            return

        self._friend_cache.add_friend(user_id)
        logger.debug("Engram：好友缓存新增 user_id=%s", user_id)

    @filter.llm_tool(name="memory_recall")
    async def memory_recall(
        self,
        event: AstrMessageEvent,
        query: str,
        limit: int = 3,
        time_expr: str = "",
        mode: str = "hybrid"
    ) -> str:
        '''检索长期记忆摘要。用于回忆用户历史偏好、事实、计划或已归档对话总结；如需具体原文片段，请改用 session_search。

        Args:
            query(string): 检索关键词或问题
            limit(number): 返回条数上限，默认 3
            time_expr(string): 可选时间范围表达式，如 2026-01 / 2026-01-01~2026-01-31
            mode(string): 检索模式，可选 hybrid/semantic/keyword/recent
        '''
        return await self._build_memory_search_output(
            event=event,
            query=query,
            limit=limit,
            time_expr=time_expr,
            source_types=None,
            mode=mode,
            default_types=None,
            title="🧠 长期记忆召回结果"
        )

    @filter.llm_tool(name="session_search")
    async def session_search(
        self,
        event: AstrMessageEvent,
        query: str,
        window: int = 5,
        limit: int = 3,
    ) -> str:
        '''检索原始对话片段。用于查找具体说过的话、精确关键词、上下文窗口；不做语义总结，直接返回命中消息及前后文。

        Args:
            query(string): 原文关键词或短语
            window(number): 每条命中前后各返回多少条同会话消息，默认 5
            limit(number): 命中片段数量上限，默认 3
        '''
        return await self._build_session_search_output(
            event=event,
            query=query,
            window=window,
            limit=limit,
        )

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        """在消息发送后记录 AI 的回复到原始记忆，并更新互动统计"""
        if event.get_group_id():
            await self._handle_group_after_message_sent(event)
            return
        
        # 检查用户原始消息是否为指令，是则跳过记录 AI 回复
        user_message = event.message_str
        if self._is_command_message(user_message):
            logger.debug(f"Engram：检测到指令消息，跳过记录 AI 回复：{user_message[:30]}")
            return
        
        # 获取结果对象
        result = event.get_result()
        # 必须是 LLM 结果才记录 (过滤掉指令回复、报错信息等)
        if not result or not result.is_llm_result():
            return

        user_id = event.get_sender_id()
        # 提取纯文本内容
        content = "".join([c.text for c in result.chain if hasattr(c, "text")])
        
        if content:
            await self.logic.record_message(user_id=user_id, session_id=user_id, role="assistant", content=content)
            
            # v2.1 优化：更新互动统计（有效聊天 = 一问一答）
            # AI 成功回复后才算一次有效互动
            try:
                await self.logic._update_interaction_stats(user_id)
            except Exception as e:
                logger.debug(f"Engram：更新用户 {user_id} 的互动统计失败：{e}")

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent):
        """在收到私聊消息时记录原始记忆并被动同步 OneBot 用户信息"""
        user_id = event.get_sender_id()
        content = event.message_str
        
        # 检查是否为指令消息，是则跳过记录
        if self._is_command_message(content):
            return
        
        user_name = event.get_sender_name()
        await self.logic.record_message(user_id=user_id, session_id=user_id, role="user", content=content, user_name=user_name)
        
        # 被动更新基础信息（委托给 OneBotSyncHandler，内部自带频率控制）
        await self._onebot_handler.sync_user_info(event, user_id=user_id, user_name=user_name)

    @filter.command("查看记忆")
    async def mem_list(self, event: AstrMessageEvent, count: str = ""):
        """查看最近生成的长期记忆归档"""
        user_id = event.get_sender_id()
        yield await self._run_plain_command(
            event,
            "mem_list",
            lambda: self._mem_handler.handle_mem_list(user_id=user_id, count=count),
        )

    @filter.command("查看记忆详情")
    async def mem_view(self, event: AstrMessageEvent, index: str):
        """查看指定序号或 ID 记忆的完整对话原文"""
        user_id = event.get_sender_id()
        yield await self._run_plain_command(
            event,
            "mem_view",
            lambda: self._mem_handler.handle_mem_view(user_id=user_id, index=index),
        )

    @filter.command("搜索记忆")
    async def mem_search(self, event: AstrMessageEvent, query: str):
        """搜索与关键词相关的长期记忆（按相关性排序）"""
        user_id = event.get_sender_id()

        handler = getattr(self, "_mem_handler", None)
        if handler is not None:
            yield await self._run_plain_command(
                event,
                "mem_search",
                lambda: handler.handle_mem_search(user_id=user_id, query=query),
            )
            return

        try:
            # 兼容 __new__ 场景测试：回退到直连逻辑
            memories = await self.logic.retrieve_memories(user_id, query, limit=3, force_retrieve=True)
            if not memories:
                yield event.plain_result(f"🔍 未找到与 '{query}' 相关的记忆。")
                return
            result = [f"🔍 搜索关键词 '{query}' 的结果（按相关性排序）：\n"] + memories
            result.append("\n💡 使用 /删除记忆 <ID> 可根据记忆 ID 删除指定记忆。")
            yield event.plain_result("\n".join(result))
        except Exception as e:
            logger.error("Engram command mem_search failed: %s", e, exc_info=True)
            yield event.plain_result("❌ 命令执行失败，请稍后重试。")

    @filter.command("删除记忆")
    async def mem_delete(self, event: AstrMessageEvent, index: str):
        """删除指定序号或 ID 的总结记忆（保留原始消息）"""
        user_id = event.get_sender_id()
        yield await self._run_plain_command(
            event,
            "mem_delete",
            lambda: self._mem_handler.handle_mem_delete(user_id=user_id, index=index, delete_raw=False),
        )

    @filter.command("删除全部记忆")
    async def mem_delete_all(self, event: AstrMessageEvent, index: str):
        """删除指定序号或 ID 的总结记忆及其关联的原始消息"""
        user_id = event.get_sender_id()
        yield await self._run_plain_command(
            event,
            "mem_delete_all",
            lambda: self._mem_handler.handle_mem_delete(user_id=user_id, index=index, delete_raw=True),
        )

    @filter.command("撤销删除记忆")
    async def mem_undo(self, event: AstrMessageEvent):
        """撤销最近一次删除操作"""
        user_id = event.get_sender_id()
        yield await self._run_plain_command(
            event,
            "mem_undo",
            lambda: self._mem_handler.handle_mem_undo(user_id=user_id),
        )

    @filter.command("清理记忆原文")
    async def mem_clear_raw(self, event: AstrMessageEvent, confirm: str = ""):
        """清除所有未归档的原始消息数据"""
        user_id = event.get_sender_id()
        yield await self._run_plain_command(
            event,
            "mem_clear_raw",
            lambda: self._mem_handler.handle_mem_clear_raw(user_id=user_id, confirm=confirm),
        )

    @filter.command("清理记忆归档")
    async def mem_clear_archive(self, event: AstrMessageEvent, confirm: str = ""):
        """清除所有长期记忆归档（保留原始消息）"""
        user_id = event.get_sender_id()
        yield await self._run_plain_command(
            event,
            "mem_clear_archive",
            lambda: self._mem_handler.handle_mem_clear_archive(user_id=user_id, confirm=confirm),
        )

    @filter.command("清空记忆")
    async def mem_clear_all(self, event: AstrMessageEvent, confirm: str = ""):
        """清除所有原始消息和长期记忆数据"""
        user_id = event.get_sender_id()
        yield await self._run_plain_command(
            event,
            "mem_clear_all",
            lambda: self._mem_handler.handle_mem_clear_all(user_id=user_id, confirm=confirm),
        )

    async def profile_clear(self, event: AstrMessageEvent, confirm: str = ""):
        """清除用户画像数据"""
        user_id = event.get_sender_id()
        yield await self._run_plain_command(
            event,
            "profile_clear",
            lambda: self._profile_handler.handle_profile_clear(user_id=user_id, confirm=confirm),
        )

    async def profile_show(self, event: AstrMessageEvent):
        """显示手账风格的用户深度画像"""
        user_id = event.get_sender_id()
        try:
            success, result = await self._profile_handler.handle_profile_show(user_id=user_id)
            if success:
                from astrbot.api.message_components import Image as MsgImage
                yield event.chain_result([MsgImage.fromBytes(result)])
            else:
                yield event.plain_result(result)
        except Exception as e:
            logger.error("Engram command profile_show failed: %s", e, exc_info=True)
            yield event.plain_result("❌ 命令执行失败，请稍后重试。")

    async def profile_set(self, event: AstrMessageEvent, key: str, value: str):
        """手动设置画像字段的值 (如: /设置画像 职业 程序员)"""
        user_id = event.get_sender_id()
        yield await self._run_plain_command(
            event,
            "profile_set",
            lambda: self._profile_handler.handle_profile_set(user_id=user_id, key=key, value=value),
        )

    async def profile_rollback(self, event: AstrMessageEvent, steps: str = "1"):
        """回滚用户画像到历史版本（默认回滚 1 步）"""
        user_id = event.get_sender_id()
        yield await self._run_plain_command(
            event,
            "profile_rollback",
            lambda: self._profile_handler.handle_profile_rollback(user_id=user_id, steps=steps),
        )

    async def profile_delete(self, event: AstrMessageEvent, category: str, value: str):
        """删除画像记忆碎片 (如: /删除画像 爱好 篮球)"""
        user_id = event.get_sender_id()
        yield await self._run_plain_command(
            event,
            "profile_delete",
            lambda: self._profile_handler.handle_profile_delete(user_id=user_id, category=category, value=value),
        )

    async def profile_evidence(self, event: AstrMessageEvent, top_n: str = "8"):
        """查看画像证据摘要"""
        user_id = event.get_sender_id()
        yield await self._run_plain_command(
            event,
            "profile_evidence",
            lambda: self._profile_handler.handle_profile_evidence(user_id=user_id, top_n=top_n),
        )

    @filter.command("查看画像")
    async def profile_show_cn(self, event: AstrMessageEvent):
        """查看当前用户画像"""
        async for result in self.profile_show(event):
            yield result

    @filter.command("设置画像")
    async def profile_set_cn(self, event: AstrMessageEvent, key: str, value: str):
        """手动设置画像字段的值"""
        async for result in self.profile_set(event, key, value):
            yield result

    @filter.command("回滚画像")
    async def profile_rollback_cn(self, event: AstrMessageEvent, steps: str = "1"):
        """回滚用户画像到历史版本"""
        async for result in self.profile_rollback(event, steps):
            yield result

    @filter.command("删除画像")
    async def profile_delete_cn(self, event: AstrMessageEvent, category: str, value: str):
        """删除画像记忆碎片"""
        async for result in self.profile_delete(event, category, value):
            yield result

    @filter.command("查看画像证据")
    async def profile_evidence_cn(self, event: AstrMessageEvent, top_n: str = "8"):
        """查看画像证据摘要"""
        async for result in self.profile_evidence(event, top_n):
            yield result

    @filter.command("清空画像")
    async def profile_clear_cn(self, event: AstrMessageEvent, confirm: str = ""):
        """清除用户画像数据"""
        async for result in self.profile_clear(event, confirm):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("归档记忆")
    async def force_summarize(self, event: AstrMessageEvent):
        """[管理员] 立即对当前所有未处理对话进行记忆归档"""
        user_id = event.get_sender_id()
        try:
            start_msg, done_msg = self._mem_handler.get_force_summarize_messages()
            yield event.plain_result(start_msg)
            await self._mem_handler.handle_force_summarize(user_id=user_id)
            yield event.plain_result(done_msg)
        except Exception as e:
            logger.error("Engram command engram_force_summarize failed: %s", e, exc_info=True)
            yield event.plain_result("❌ 命令执行失败，请稍后重试。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("归档全部记忆")
    async def force_summarize_all(self, event: AstrMessageEvent):
        """[管理员] 立即对所有用户未处理对话进行记忆归档（含群聊记忆）"""
        yield event.plain_result("⏳ 正在强制执行全局记忆归档，请稍候...")

        private_total = 0
        group_total = 0
        group_enabled = bool(self.config.get("enable_group_memory", False))

        try:
            private_total = await self.logic.summarize_all_users()
        except Exception as e:
            logger.error("Engram：强制归档全部私聊记忆失败：%s", e, exc_info=True)
            yield event.plain_result("❌ 私聊记忆归档失败，请稍后重试。")
            return

        if group_enabled:
            try:
                group_manager = await self._ensure_group_memory_manager()
                if group_manager is not None:
                    group_total = await group_manager.summarize_all_users()
            except Exception as e:
                logger.error("Engram：强制归档全部群聊记忆失败：%s", e, exc_info=True)
                yield event.plain_result(
                    f"⚠️ 私聊记忆归档已完成（{private_total}），但群聊记忆归档失败，请稍后重试。"
                )
                return

        if group_enabled:
            yield event.plain_result(
                f"✅ 全局记忆归档完成。\n- 私聊已处理：{private_total}\n- 群聊已处理：{group_total}"
            )
        else:
            yield event.plain_result(
                f"✅ 全局记忆归档完成。\n- 私聊已处理：{private_total}\n- 群聊记忆未启用，未执行"
            )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("更新画像")
    async def force_persona(self, event: AstrMessageEvent, days: str = ""):
        """[管理员] 立即基于指定天数的记忆强制深度更新画像"""
        user_id = event.get_sender_id()

        try:
            ok, err_msg, days_int = self._profile_handler.resolve_force_persona_days(days)
            if not ok:
                yield event.plain_result(err_msg)
                return

            start_msg, done_msg = self._profile_handler.build_force_persona_messages(days_int)
            yield event.plain_result(start_msg)
            await self._profile_handler.handle_force_persona(user_id=user_id, days_int=days_int)
            yield event.plain_result(done_msg)
        except Exception as e:
            logger.error("Engram command engram_force_persona failed: %s", e, exc_info=True)
            yield event.plain_result("❌ 命令执行失败，请稍后重试。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重建记忆向量")
    async def mem_rebuild_vector(self, event: AstrMessageEvent, mode: str = ""):
        """[管理员] 备份并重建向量库（full 表示重建并回灌）

        重建说明：
            当 embedding_provider 变更或提示向量维度不一致时，
            请执行 /重建记忆向量 full 重新嵌入全部记忆，否则旧记忆将无法检索。
        """
        full_rebuild_flag = str(mode or "").strip().lower() == "full"
        mode_text = "全量重建" if full_rebuild_flag else "增量重建"
        yield event.plain_result(f"⏳ 正在执行向量库{mode_text}，请稍候...")

        try:
            result = await self._mem_handler.handle_rebuild_vectors(full_rebuild_flag=full_rebuild_flag, batch_size=200)
            yield event.plain_result(self._mem_handler.build_rebuild_vector_result_text(full_rebuild_flag, result))
        except Exception as e:
            logger.error("Engram：重建向量库失败：%s", e, exc_info=True)
            yield event.plain_result("❌ 向量库重建失败，请稍后重试。")

    @filter.command("查看群记忆")
    async def group_mem_list(self, event: AstrMessageEvent, count: str = ""):
        """查看本群最近生成的长期记忆归档"""
        if not event.get_group_id():
            yield event.plain_result("仅群聊可用。")
            return
        handler = await self._get_group_mem_handler()
        if handler is None:
            yield event.plain_result("群聊记忆未开启或未初始化。")
            return
        storage_id = self._resolve_group_storage_id(event.get_group_id(), event.get_sender_id())
        yield await self._run_plain_command(
            event,
            "group_mem_list",
            lambda: handler.handle_mem_list(user_id=storage_id, count=count),
            transform=self._rewrite_group_command_hints,
        )

    @filter.command("查看群记忆详情")
    async def group_mem_view(self, event: AstrMessageEvent, index: str):
        """查看本群指定序号或 ID 的记忆详情"""
        if not event.get_group_id():
            yield event.plain_result("仅群聊可用。")
            return
        handler = await self._get_group_mem_handler()
        if handler is None:
            yield event.plain_result("群聊记忆未开启或未初始化。")
            return
        storage_id = self._resolve_group_storage_id(event.get_group_id(), event.get_sender_id())
        yield await self._run_plain_command(
            event,
            "group_mem_view",
            lambda: handler.handle_mem_view(user_id=storage_id, index=index),
            transform=self._rewrite_group_command_hints,
        )

    @filter.command("搜索群记忆")
    async def group_mem_search(self, event: AstrMessageEvent, query: str):
        """搜索本群的长期记忆"""
        if not event.get_group_id():
            yield event.plain_result("仅群聊可用。")
            return
        handler = await self._get_group_mem_handler()
        if handler is None:
            yield event.plain_result("群聊记忆未开启或未初始化。")
            return
        storage_id = self._resolve_group_storage_id(event.get_group_id(), event.get_sender_id())
        yield await self._run_plain_command(
            event,
            "group_mem_search",
            lambda: handler.handle_mem_search(user_id=storage_id, query=query),
            transform=self._rewrite_group_command_hints,
        )

    @filter.command("删除群记忆")
    async def group_mem_delete(self, event: AstrMessageEvent, index: str):
        """删除本群指定序号或 ID 的总结记忆"""
        if not event.get_group_id():
            yield event.plain_result("仅群聊可用。")
            return
        handler = await self._get_group_mem_handler()
        if handler is None:
            yield event.plain_result("群聊记忆未开启或未初始化。")
            return
        storage_id = self._resolve_group_storage_id(event.get_group_id(), event.get_sender_id())
        yield await self._run_plain_command(
            event,
            "group_mem_delete",
            lambda: handler.handle_mem_delete(user_id=storage_id, index=index, delete_raw=False),
            transform=self._rewrite_group_command_hints,
        )

    @filter.command("删除全部群记忆")
    async def group_mem_delete_all(self, event: AstrMessageEvent, index: str):
        """删除本群指定序号或 ID 的总结记忆及原始消息"""
        if not event.get_group_id():
            yield event.plain_result("仅群聊可用。")
            return
        handler = await self._get_group_mem_handler()
        if handler is None:
            yield event.plain_result("群聊记忆未开启或未初始化。")
            return
        storage_id = self._resolve_group_storage_id(event.get_group_id(), event.get_sender_id())
        yield await self._run_plain_command(
            event,
            "group_mem_delete_all",
            lambda: handler.handle_mem_delete(user_id=storage_id, index=index, delete_raw=True),
            transform=self._rewrite_group_command_hints,
        )

    @filter.command("撤销删除群记忆")
    async def group_mem_undo(self, event: AstrMessageEvent):
        """撤销本群最近一次删除操作"""
        if not event.get_group_id():
            yield event.plain_result("仅群聊可用。")
            return
        handler = await self._get_group_mem_handler()
        if handler is None:
            yield event.plain_result("群聊记忆未开启或未初始化。")
            return
        storage_id = self._resolve_group_storage_id(event.get_group_id(), event.get_sender_id())
        yield await self._run_plain_command(
            event,
            "group_mem_undo",
            lambda: handler.handle_mem_undo(user_id=storage_id),
            transform=self._rewrite_group_command_hints,
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("归档群记忆")
    async def group_mem_force_summarize(self, event: AstrMessageEvent):
        """[管理员] 强制归档本群未处理对话"""
        if not event.get_group_id():
            yield event.plain_result("仅群聊可用。")
            return
        handler = await self._get_group_mem_handler()
        if handler is None:
            yield event.plain_result("群聊记忆未开启或未初始化。")
            return
        storage_id = self._resolve_group_storage_id(event.get_group_id(), event.get_sender_id())
        try:
            start_msg, done_msg = handler.get_force_summarize_messages()
            yield event.plain_result(self._rewrite_group_command_hints(start_msg))
            await handler.handle_force_summarize(user_id=storage_id)
            yield event.plain_result(self._rewrite_group_command_hints(done_msg))
        except Exception as e:
            logger.error("Engram command group_mem_force_summarize failed: %s", e, exc_info=True)
            yield event.plain_result("❌ 命令执行失败，请稍后重试。")

    @filter.command("导出记忆")
    async def mem_export(self, event: AstrMessageEvent, format: str = "jsonl", days: str = ""):
        """导出原始消息数据用于模型微调"""
        try:
            async for result in self.export_handler.handle_export_command(event, format, days):
                yield result
        except Exception as e:
            logger.error("Engram command mem_export failed: %s", e, exc_info=True)
            yield event.plain_result("❌ 命令执行失败，请稍后重试。")

    @filter.command("统计记忆")
    async def mem_stats(self, event: AstrMessageEvent):
        """查看消息统计信息"""
        user_id = event.get_sender_id()
        group_id = event.get_group_id()
        storage_id = None
        if group_id:
            storage_id = self._resolve_group_storage_id(group_id, user_id)
        db_path = getattr(self.logic.db, "db_path", None) or getattr(
            getattr(self.logic.db, "_backend", None), "db_path", None
        )
        inode = None
        db_size = None
        try:
            if db_path:
                stat = os.stat(db_path)
                inode = getattr(stat, "st_ino", None)
                db_size = getattr(stat, "st_size", None)
        except Exception as e:
            logger.debug("Engram：mem_stats 读取 db_path 失败：%s", e)
        logger.info(
            "Engram：mem_stats user_id=%s group_id=%s storage_id=%s db_path=%s inode=%s size=%s",
            user_id,
            group_id,
            storage_id,
            db_path,
            inode,
            db_size,
        )
        try:
            async for result in self.export_handler.handle_stats_command(event):
                yield result
        except Exception as e:
            logger.error("Engram command mem_stats failed: %s", e, exc_info=True)
            yield event.plain_result("❌ 命令执行失败，请稍后重试。")
    
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("导出全部记忆")
    async def mem_export_all(self, event: AstrMessageEvent, format: str = "jsonl", days: str = ""):
        """[管理员] 导出所有用户的原始消息数据"""
        try:
            async for result in self.export_handler.handle_export_all_command(event, format, days):
                yield result
        except Exception as e:
            logger.error("Engram command mem_export_all failed: %s", e, exc_info=True)
            yield event.plain_result("❌ 命令执行失败，请稍后重试。")

    async def terminate(self):
        """优雅关闭插件：先设置标志，再取消任务，最后关闭资源"""
        # 步骤1：设置关闭标志（但不关闭线程池）
        self.logic._is_shutdown = True
        if hasattr(self, "_scheduler"):
            self._scheduler._is_shutdown = True
        
        # 步骤2：取消所有后台任务
        if hasattr(self, "_scheduler"):
            for task in self._scheduler._tasks:
                if not task.done():
                    task.cancel()

            # 等待任务清理完成（最多5秒，让正在执行的 LLM/DB 调用尽量优雅结束）
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._scheduler._tasks, return_exceptions=True),
                    timeout=5.0
                )
                logger.debug("Engram：所有调度任务已优雅停止")
            except asyncio.TimeoutError:
                logger.debug("Engram：部分调度任务未在限定时间内完成")
            except Exception as e:
                logger.debug(f"Engram：等待调度任务结束时发生异常：{e}")

        if getattr(self, "_group_scheduler", None):
            self._group_scheduler._is_shutdown = True
            for task in self._group_scheduler._tasks:
                if not task.done():
                    task.cancel()

            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._group_scheduler._tasks, return_exceptions=True),
                    timeout=5.0
                )
                logger.debug("Engram：群聊调度任务已优雅停止")
            except asyncio.TimeoutError:
                logger.debug("Engram：群聊调度任务未在限定时间内完成")
            except Exception as e:
                logger.debug(f"Engram：等待群聊调度任务结束时发生异常：{e}")

        startup_tasks = [task for task in getattr(self, "_startup_tasks", []) if not task.done()]
        for task in startup_tasks:
            task.cancel()
        if startup_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*startup_tasks, return_exceptions=True),
                    timeout=5.0
                )
            except asyncio.TimeoutError:
                logger.debug("Engram：部分启动后台任务未在限定时间内完成")
            except Exception as e:
                logger.debug(f"Engram：等待启动后台任务结束时发生异常：{e}")

        # 步骤3：关闭 WebUI 服务
        if getattr(self, "_webui_server", None):
            try:
                await self._webui_server.stop()
            except Exception as e:
                logger.error(f"Engram：停止 WebUI 服务失败：{e}")

        # 步骤4：最后关闭线程池和其他资源
        self.logic._memory_manager.shutdown()
        if getattr(self, "_group_memory_manager", None):
            self._group_memory_manager.shutdown()
        self._close_worker_database_connections()
        # 等待 worker 线程完成正在执行的 SQL 写入，避免半提交事务
        try:
            self.logic.executor.shutdown(wait=True, cancel_futures=False)
        except TypeError:
            # Python < 3.9 不支持 cancel_futures 参数
            self.logic.executor.shutdown(wait=True)
        except Exception as e:
            logger.error(f"Engram：关闭线程池异常：{e}")
        await self.profile_renderer.close()
