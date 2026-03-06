from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig, logger
from astrbot.api.message_components import Image

# 核心模块
from .core import MemoryFacade, MemoryScheduler
from .handlers import MemoryCommandHandler, ProfileCommandHandler, OneBotSyncHandler
from .export_handler import ExportHandler
from .profile_renderer import ProfileRenderer
from .services import (
    LLMContextInjector,
    IntentClassifier,
    TopicMemoryCacheService,
    ToolHintStrategyService,
    ConfigPresetService,
)
from .utils import get_constellation, get_zodiac, get_career

import asyncio
import json
import datetime
import time
import re


@register("astrbot_plugin_engram", "victical", "仿生双轨记忆系统", "1.5.4")
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
        self._onebot_handler = OneBotSyncHandler(self.logic._profile_manager)
        self._llm_injector = LLMContextInjector()
        self._intent_classifier = IntentClassifier(config=self.config, context=context)
        self._topic_cache_service = TopicMemoryCacheService(config=self.config)
        self._tool_hint_strategy = ToolHintStrategyService(config=self.config)

        # 初始化调度器
        self._scheduler = MemoryScheduler(self.logic, self.config)
        asyncio.create_task(self._scheduler.start())
        
        # OneBot 同步时间缓存
        self._last_onebot_sync = {}

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
        """解析工具时间表达式，返回 (start_dt, end_dt, desc)。

        支持：
        1) LLM 显式时间范围（如 2026-02-23~2026-03-01）
        2) 未携带年份的范围/日期（如 02-23~03-01），默认按当前年份解析
        """
        text = str(text or "").strip()
        if not text:
            return None, None, ""

        now = datetime.datetime.now()
        current_year = now.year
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        def _next_month_start(dt: datetime.datetime) -> datetime.datetime:
            if dt.month == 12:
                return dt.replace(year=dt.year + 1, month=1, day=1)
            return dt.replace(month=dt.month + 1, day=1)

        def _safe_datetime(year: int, month: int, day: int = 1):
            try:
                return datetime.datetime(year=year, month=month, day=day)
            except ValueError:
                return None

        def _parse_date_or_month(raw: str):
            """返回 (dt, kind, normalized_text, used_default_year)。"""
            raw = str(raw or "").strip()
            if not raw:
                return None, "", "", False

            # yyyy-mm-dd / yyyy/mm/dd / yyyy.mm.dd
            for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
                try:
                    dt = datetime.datetime.strptime(raw, fmt)
                    return dt, "day", dt.strftime("%Y-%m-%d"), False
                except ValueError:
                    pass

            # yyyy-mm / yyyy/mm / yyyy.mm
            for fmt in ("%Y-%m", "%Y/%m", "%Y.%m"):
                try:
                    dt = datetime.datetime.strptime(raw, fmt)
                    return dt, "month", dt.strftime("%Y-%m"), False
                except ValueError:
                    pass

            # 中文：yyyy年m月d日(号)
            m = re.fullmatch(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})(?:日|号)?", raw)
            if m:
                dt = _safe_datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                if dt:
                    return dt, "day", dt.strftime("%Y-%m-%d"), False

            # 中文：yyyy年m月
            m = re.fullmatch(r"(\d{4})年\s*(\d{1,2})月", raw)
            if m:
                dt = _safe_datetime(int(m.group(1)), int(m.group(2)), 1)
                if dt:
                    return dt, "month", dt.strftime("%Y-%m"), False

            # 中文：m月d日(号)（默认今年）
            m = re.fullmatch(r"(\d{1,2})月\s*(\d{1,2})(?:日|号)?", raw)
            if m:
                dt = _safe_datetime(current_year, int(m.group(1)), int(m.group(2)))
                if dt:
                    return dt, "day", dt.strftime("%Y-%m-%d"), True

            # 中文：m月（默认今年）
            m = re.fullmatch(r"(\d{1,2})月", raw)
            if m:
                dt = _safe_datetime(current_year, int(m.group(1)), 1)
                if dt:
                    return dt, "month", dt.strftime("%Y-%m"), True

            # m-d / m/d / m.d（默认今年）
            m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})", raw)
            if m:
                dt = _safe_datetime(current_year, int(m.group(1)), int(m.group(2)))
                if dt:
                    return dt, "day", dt.strftime("%Y-%m-%d"), True

            return None, "", "", False

        # 1) 显式区间（支持含/不含年份；未写年份默认今年）
        token_pattern = (
            r"(?:\d{4}[-/.]\d{1,2}(?:[-/.]\d{1,2})?|"
            r"\d{1,2}[-/.]\d{1,2}|"
            r"(?:\d{4}年)?\d{1,2}月(?:\d{1,2}(?:日|号)?)?)"
        )
        range_match = re.search(
            rf"({token_pattern})\s*(?:~|～|到|至|-)\s*({token_pattern})",
            text
        )
        if range_match:
            left_raw, right_raw = range_match.group(1), range_match.group(2)
            left_dt, left_kind, left_desc, left_default_year = _parse_date_or_month(left_raw)
            right_dt, right_kind, right_desc, right_default_year = _parse_date_or_month(right_raw)
            if left_dt and right_dt:
                # 统一为左闭右开
                if right_kind == "day":
                    right_dt = right_dt.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
                else:
                    right_dt = _next_month_start(right_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0))

                if left_kind == "month":
                    left_dt = left_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                else:
                    left_dt = left_dt.replace(hour=0, minute=0, second=0, microsecond=0)

                if right_dt > left_dt:
                    desc = f"{left_desc}~{right_desc}"
                    if left_default_year or right_default_year:
                        desc += f"（未写年份按{current_year}年）"
                    return left_dt, right_dt, desc

        # 2) 单个日期 / 单个月份（支持未写年份，默认今年）
        single_match = re.search(token_pattern, text)
        if single_match:
            raw = single_match.group(1)
            dt, kind, norm_desc, used_default_year = _parse_date_or_month(raw)
            if dt:
                if kind == "day":
                    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
                    end = start + datetime.timedelta(days=1)
                else:
                    start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                    end = _next_month_start(start)

                desc = norm_desc
                if used_default_year:
                    desc += f"（未写年份按{current_year}年）"
                return start, end, desc

        return None, None, ""

    def _normalize_source_types(self, source_types, default_types=None):
        """归一化 source_types，支持 array 与逗号分隔字符串。"""
        allowed_source_types = {"private", "daily_summary", "weekly", "monthly", "yearly"}
        normalized_types = []

        if isinstance(source_types, list):
            raw_types = source_types
        elif isinstance(source_types, str) and source_types.strip():
            raw_types = re.split(r"[\s,，]+", source_types.strip())
        else:
            raw_types = []

        for item in raw_types:
            token = str(item or "").strip().lower()
            if token in allowed_source_types and token not in normalized_types:
                normalized_types.append(token)

        if normalized_types:
            return normalized_types

        if default_types:
            return [t for t in default_types if t in allowed_source_types]

        return []

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
        default_types=None,
        title: str = "🧠 工具检索结果",
        extra_hint: str = ""
    ) -> str:
        """统一构建记忆检索工具输出。"""
        if not self.config.get("enable_memory_search_tool", True):
            return "记忆检索工具已关闭。"

        if event.get_group_id():
            return "当前仅支持私聊场景的记忆工具检索。"

        query = str(query or "").strip()
        if not query:
            return "query 不能为空，请提供要检索的问题或关键词。"

        # 工具安全限流：配置值与参数值双重约束，最终范围固定在 1-10
        try:
            max_results = int(self.config.get("memory_search_tool_max_results", 3))
        except (TypeError, ValueError):
            max_results = 3
        max_results = max(1, min(10, max_results))

        try:
            request_limit = int(limit)
        except (TypeError, ValueError):
            request_limit = max_results

        final_limit = max(1, min(10, request_limit, max_results))
        user_id = event.get_sender_id()

        # 时间过滤：仅使用显式 time_expr（由 LLM 提供），不再从 query 自动识别
        parse_target = str(time_expr or "").strip()
        try:
            start_time, end_time, time_desc = self._parse_time_expr(parse_target)
        except re.error as e:
            logger.warning(f"Engram mem_search_tool：time_expr 正则解析失败：{e}")
            start_time, end_time, time_desc = None, None, ""
        except Exception as e:
            logger.warning(f"Engram mem_search_tool：解析 time_expr 失败：{e}")
            start_time, end_time, time_desc = None, None, ""

        normalized_types = self._normalize_source_types(source_types, default_types=default_types)

        try:
            memories = await self.logic.retrieve_memories(
                user_id,
                query,
                limit=final_limit,
                start_time=start_time,
                end_time=end_time,
                source_types=normalized_types or None
            )
        except Exception as e:
            logger.error(f"Engram mem_search_tool 异常：{e}")
            return "工具检索失败，请稍后重试。"

        if not memories:
            return f"未检索到与“{query}”相关的长期记忆。"

        result_lines = [f"{title}（共 {min(len(memories), final_limit)} 条）："]

        if time_desc:
            result_lines.append(f"⏱️ 时间筛选：{time_desc}")
        if normalized_types:
            result_lines.append(f"🗂️ 类型筛选：{', '.join(normalized_types)}")

        for idx, memory in enumerate(memories[:final_limit], start=1):
            result_lines.append(f"{idx}. {memory}")

        if extra_hint:
            result_lines.append(f"\n{extra_hint}")
        result_lines.append("\n💡 如需查看某条记忆的完整原始对话，请使用 mem_get_detail_tool 并传入对应 🆔。")
        return "\n\n".join(result_lines)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """在调用 LLM 前注入长期记忆和用户画像"""
        if event.get_group_id(): return
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
            self._llm_injector.inject_context(req, profile_block, combined_memory_block)
            
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

    @filter.llm_tool(name="mem_search_tool")
    async def mem_search_tool(
        self,
        event: AstrMessageEvent,
        query: str,
        limit: int = 3,
        time_expr: str = "",
        source_types: list = None
    ) -> str:
        '''检索长期记忆（通用），仅返回给 LLM，不直接发送给用户。'''
        output = await self._build_memory_search_output(
            event=event,
            query=query,
            limit=limit,
            time_expr=time_expr,
            source_types=source_types,
            default_types=None,
            title="🧠 工具检索结果"
        )
        return output

    @filter.llm_tool(name="mem_search_overview_tool")
    async def mem_search_overview_tool(
        self,
        event: AstrMessageEvent,
        query: str,
        limit: int = 3,
        time_expr: str = "",
        source_types: list = None
    ) -> str:
        '''检索长期记忆（兼容别名，行为同 mem_search_tool）。'''
        output = await self._build_memory_search_output(
            event=event,
            query=query,
            limit=limit,
            time_expr=time_expr,
            source_types=source_types,
            default_types=None,
            title="🧠 工具检索结果"
        )
        return output

    @filter.llm_tool(name="mem_search_detail_tool")
    async def mem_search_detail_tool(
        self,
        event: AstrMessageEvent,
        query: str,
        limit: int = 3,
        time_expr: str = "",
        source_types: list = None
    ) -> str:
        '''检索长期记忆（兼容别名，行为同 mem_search_tool）。'''
        output = await self._build_memory_search_output(
            event=event,
            query=query,
            limit=limit,
            time_expr=time_expr,
            source_types=source_types,
            default_types=None,
            title="🧠 工具检索结果"
        )
        return output

    @filter.llm_tool(name="mem_get_detail_tool")
    async def mem_get_detail_tool(self, event: AstrMessageEvent, memory_id: str, max_messages: int = 20) -> str:
        '''按记忆 ID 获取更完整的原始对话。

        Args:
            memory_id(string): 记忆 ID，支持 8 位短 ID 或完整 ID
            max_messages(number): 返回原始对话条数上限
        '''
        if not self.config.get("enable_memory_search_tool", True):
            return "记忆检索工具已关闭。"

        if event.get_group_id():
            return "当前仅支持私聊场景的记忆工具检索。"

        memory_id = str(memory_id or "").strip()
        if len(memory_id) < 8:
            return "memory_id 至少需要 8 位，请先通过 mem_search_tool 获取 🆔。"

        try:
            max_messages = int(max_messages)
        except (TypeError, ValueError):
            max_messages = 20
        max_messages = max(1, min(100, max_messages))

        user_id = event.get_sender_id()

        try:
            memory_index, raw_msgs = await self.logic.get_memory_detail_by_id(user_id, memory_id)
        except Exception as e:
            logger.error(f"Engram mem_get_detail_tool 异常：{e}")
            return "工具检索失败，请稍后重试。"

        if not memory_index:
            return str(raw_msgs or f"找不到 ID 为 {memory_id} 的记忆。")

        created_at = self.logic._ensure_datetime(memory_index.created_at)
        detail_lines = [
            f"📖 记忆详情（ID {memory_index.index_id[:8]}）",
            f"⏰ 时间：{created_at.strftime('%Y-%m-%d %H:%M')}",
            f"📝 归档：{memory_index.summary}",
            "————————————————",
            "🎙️ 原始对话回溯："
        ]

        if not raw_msgs:
            detail_lines.append("(暂无关联的原始对话数据)")
            return "\n".join(detail_lines)

        shown = 0
        for m in raw_msgs:
            if not self.logic._is_valid_message_content(m.content):
                continue

            ts = self.logic._ensure_datetime(m.timestamp)
            time_str = ts.strftime("%H:%M:%S")
            role_name = "我" if m.role == "assistant" else (m.user_name or "你")
            detail_lines.append(f"[{time_str}] {role_name}: {m.content}")
            shown += 1

            if shown >= max_messages:
                break

        if shown == 0:
            detail_lines.append("(原始对话均为空或被过滤)")

        return "\n".join(detail_lines)

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        """在消息发送后记录 AI 的回复到原始记忆，并更新互动统计"""
        # 只处理私聊
        if event.get_group_id(): return
        
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
        
        # 频率控制：每 12 小时最多同步一次 OneBot 信息
        now = time.time()
        last_sync = self._last_onebot_sync.get(user_id, 0)
        if now - last_sync < 12 * 3600:
            return

        # 被动更新基础信息 (通过 OneBot V11 接口获取更多细节)
        try:
            # 1. 基础 Payload
            avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
            update_payload = {
                "basic_info": {
                    "qq_id": user_id,
                    "nickname": user_name,
                    "avatar_url": avatar_url
                }
            }

            # 2. 尝试调用 OneBot V11 get_stranger_info 接口
            try:
                # 兼容不同版本的 AstrBot 获取 bot 实例的方式
                bot = getattr(event, 'bot', None)
                if bot and hasattr(bot, 'get_stranger_info'):
                    # 某些实现需要整数 ID
                    try: uid_int = int(user_id)
                    except: uid_int = user_id
                    
                    stranger_info = await bot.get_stranger_info(user_id=uid_int)
                    if stranger_info:
                        # 映射 OneBot V11 字段到画像结构
                        # sex: male, female, unknown
                        sex_map = {"male": "男", "female": "女", "unknown": "未知"}
                        gender = sex_map.get(stranger_info.get("sex"), "未知")
                        age = stranger_info.get("age", "未知")
                        nickname = stranger_info.get("nickname", user_name)
                        
                        update_payload["basic_info"]["gender"] = gender
                        update_payload["basic_info"]["age"] = age
                        update_payload["basic_info"]["nickname"] = nickname
                        
                        # 补充生日、生肖、签名 (OneBot V11 扩展)
                        if "birthday" in stranger_info: update_payload["basic_info"]["birthday"] = stranger_info["birthday"]
                        
                        # 解析生日并计算星座和生肖
                        b_year = stranger_info.get("birthday_year")
                        b_month = stranger_info.get("birthday_month")
                        b_day = stranger_info.get("birthday_day")

                        if b_year and b_month and b_day:
                            update_payload["basic_info"]["birthday"] = f"{b_year}-{b_month}-{b_day}"
                            update_payload["basic_info"]["constellation"] = get_constellation(int(b_month), int(b_day))
                            update_payload["basic_info"]["zodiac"] = get_zodiac(int(b_year), int(b_month), int(b_day))
                        elif "birthday" in stranger_info and str(stranger_info["birthday"]).isdigit():
                            b_str = str(stranger_info["birthday"])
                            if len(b_str) == 8:
                                b_year, b_month, b_day = b_str[:4], b_str[4:6], b_str[6:]
                                update_payload["basic_info"]["birthday"] = f"{b_year}-{b_month}-{b_day}"
                                update_payload["basic_info"]["constellation"] = get_constellation(int(b_month), int(b_day))
                                update_payload["basic_info"]["zodiac"] = get_zodiac(int(b_year), int(b_month), int(b_day))

                        if "zodiac" in stranger_info: update_payload["basic_info"]["zodiac"] = stranger_info["zodiac"]
                        if "signature" in stranger_info: update_payload["basic_info"]["signature"] = stranger_info["signature"]
                        
                        # 补充职业
                        career_id = stranger_info.get("makeFriendCareer")
                        if career_id and career_id != "0":
                            update_payload["basic_info"]["job"] = get_career(int(career_id))

                        # 某些 OneBot 扩展实现可能会提供 location
                        if "location" in stranger_info:
                            update_payload["basic_info"]["location"] = stranger_info["location"]
                        elif stranger_info.get("country") == "中国":
                            prov = stranger_info.get("province", "")
                            city = stranger_info.get("city", "")
                            update_payload["basic_info"]["location"] = f"{prov}-{city}".strip("-")
                        
                        logger.info(f"Engram：已同步 OneBot 用户信息 user_id={user_id}，gender={gender}，age={age}")
            except Exception as api_err:
                logger.debug(f"Engram：OneBot API 调用已跳过或失败：{api_err}")

            await self.logic.update_user_profile(user_id, update_payload)
            self._last_onebot_sync[user_id] = now
        except Exception as e:
            logger.error(f"Engram：自动更新基础信息失败：{e}")

    @filter.command("mem_list")
    async def mem_list(self, event: AstrMessageEvent, count: str = ""):
        """查看最近生成的长期记忆归档"""
        user_id = event.get_sender_id()
        
        # 支持可选的数量参数，未指定则使用配置项
        if count and count.isdigit():
            limit = int(count)
            if limit <= 0:
                yield event.plain_result("⚠️ 数量必须大于 0。")
                return
            elif limit > 50:
                yield event.plain_result("⚠️ 单次最多查询 50 条记忆。")
                return
        else:
            limit = self.config.get("list_memory_count", 5)
        
        loop = asyncio.get_event_loop()
        memories = await loop.run_in_executor(self.logic.executor, self.logic.db.get_memory_list, user_id, limit)
        if not memories:
            yield event.plain_result("🧐 你目前还没有生成的长期记忆。")
            return
        result = [f"📜 最近的 {len(memories)} 条长期记忆：\n" + "—" * 15]
        for i, m in enumerate(memories):
            # 确保时间戳是 datetime 对象
            created_at = self.logic._ensure_datetime(m.created_at)
            short_id = str(getattr(m, "index_id", "") or "")[:8] or "未知ID"
            result.append(
                f"{i+1}. 🆔 {short_id} | ⏰ {created_at.strftime('%m-%d %H:%M')}\n"
                f"   📝 {m.summary}\n"
            )

        result.append("\n💡 发送 /mem_view <序号或ID> 可查看某条记忆的完整对话原文。")
        result.append("💡 发送 /mem_delete <ID> 可按记忆 ID 删除指定记忆。")
        result.append("💡 发送 /mem_list <数量> 可自定义查询条数。")
        yield event.plain_result("\n".join(result))

    @filter.command("mem_view")
    async def mem_view(self, event: AstrMessageEvent, index: str):
        """查看指定序号或 ID 记忆的完整对话原文"""
        user_id = event.get_sender_id()
        
        # 智能判断：数字且 ≤ 50 使用序号查看，否则使用 ID 查看
        if index.isdigit():
            seq = int(index)
            if seq <= 0:
                yield event.plain_result("⚠️ 序号必须大于 0。")
                return
            if seq > 50:
                yield event.plain_result("⚠️ 序号超过 50，请使用记忆 ID 进行查看。")
                return
            
            # 按序号查看
            memory_index, raw_msgs = await self.logic.get_memory_detail(user_id, seq)
            display_label = f"序号 {seq}"
        else:
            # 按 ID 查看
            if len(index) < 8:
                yield event.plain_result("⚠️ 记忆 ID 至少需要 8 位，例如：/mem_view bdd54504")
                return
            
            # 使用新的 API 按 ID 获取详情
            memory_index, raw_msgs = await self.logic.get_memory_detail_by_id(user_id, index)
            
            if not memory_index:
                yield event.plain_result(f"❌ {raw_msgs}")  # raw_msgs 是错误消息
                return
            
            display_label = f"ID {memory_index.index_id[:8]}"
        
        if not memory_index:
            yield event.plain_result(raw_msgs)  # 这里 raw_msgs 返回的是错误提示字符串
            return
            
        # 格式化输出
        created_at = self.logic._ensure_datetime(memory_index.created_at)
        result = [
            f"📖 记忆详情 ({display_label})",
            f"⏰ 时间：{created_at.strftime('%Y-%m-%d %H:%M')}",
            f"📝 归档：{memory_index.summary}",
            "————————————————",
            "🎙️ 原始对话回溯："
        ]
        
        if not raw_msgs:
            result.append("(暂无关联的原始对话数据)")
        else:
            for m in raw_msgs:
                # 使用公共过滤方法
                if not self.logic._is_valid_message_content(m.content):
                    continue
                
                # 确保时间戳是 datetime 对象
                ts = self.logic._ensure_datetime(m.timestamp)
                time_str = ts.strftime("%H:%M:%S")
                role_name = "我" if m.role == "assistant" else (m.user_name or "你")
                result.append(f"[{time_str}] {role_name}: {m.content}")
                
        yield event.plain_result("\n".join(result))

    @filter.command("mem_search")
    async def mem_search(self, event: AstrMessageEvent, query: str):
        """搜索与关键词相关的长期记忆（按相关性排序）"""
        user_id = event.get_sender_id()
        memories = await self.logic.retrieve_memories(user_id, query, limit=3, force_retrieve=True)
        if not memories:
            yield event.plain_result(f"🔍 未找到与 '{query}' 相关的记忆。")
            return
        result = [f"🔍 搜索关键词 '{query}' 的结果（按相关性排序）：\n"] + memories
        result.append("\n💡 使用 /mem_delete <ID> 可根据记忆 ID 删除指定记忆。")
        yield event.plain_result("\n".join(result))

    @filter.command("mem_delete")
    async def mem_delete(self, event: AstrMessageEvent, index: str):
        """删除指定序号或 ID 的总结记忆（保留原始消息）"""
        user_id = event.get_sender_id()
        
        # 智能判断：数字且 ≤ 50 使用序号删除，否则使用 ID 删除
        if index.isdigit():
            seq = int(index)
            if seq <= 0:
                yield event.plain_result("⚠️ 序号必须大于 0。")
                return
            if seq > 50:
                yield event.plain_result("⚠️ 序号超过 50，请使用记忆 ID 进行删除。")
                return
            
            # 按序号删除
            success, message, summary = await self.logic.delete_memory_by_sequence(user_id, seq, delete_raw=False)
            
            if success:
                yield event.plain_result(f"🗑️ 已删除记忆 #{seq}：\n📝 {summary[:50]}{'...' if len(summary) > 50 else ''}\n\n💡 原始对话消息已保留，可重新归档。")
            else:
                yield event.plain_result(f"❌ {message}")
        else:
            # 按 ID 删除
            if len(index) < 8:
                yield event.plain_result("⚠️ 记忆 ID 至少需要 8 位，例如：/mem_delete a1b2c3d4")
                return
            
            success, message, summary = await self.logic.delete_memory_by_id(user_id, index, delete_raw=False)
            
            if success:
                yield event.plain_result(f"🗑️ 已删除记忆 ID {index[:8]}：\n📝 {summary[:50]}{'...' if len(summary) > 50 else ''}\n\n💡 原始对话消息已保留，可重新归档。")
            else:
                yield event.plain_result(f"❌ {message}")

    @filter.command("mem_delete_all")
    async def mem_delete_all(self, event: AstrMessageEvent, index: str):
        """删除指定序号或 ID 的总结记忆及其关联的原始消息"""
        user_id = event.get_sender_id()
        
        # 智能判断：数字且 ≤ 50 使用序号删除，否则使用 ID 删除
        if index.isdigit():
            seq = int(index)
            if seq <= 0:
                yield event.plain_result("⚠️ 序号必须大于 0。")
                return
            if seq > 50:
                yield event.plain_result("⚠️ 序号超过 50，请使用记忆 ID 进行删除。")
                return
            
            # 按序号删除
            success, message, summary = await self.logic.delete_memory_by_sequence(user_id, seq, delete_raw=True)
            
            if success:
                yield event.plain_result(f"🗑️ 已彻底删除记忆 #{seq} 及其原始对话：\n📝 {summary[:50]}{'...' if len(summary) > 50 else ''}\n\n💡 如果误删，可使用 /mem_undo 撤销此操作。")
            else:
                yield event.plain_result(f"❌ {message}")
        else:
            # 按 ID 删除
            if len(index) < 8:
                yield event.plain_result("⚠️ 记忆 ID 至少需要 8 位，例如：/mem_delete_all a1b2c3d4")
                return
            
            success, message, summary = await self.logic.delete_memory_by_id(user_id, index, delete_raw=True)
            
            if success:
                yield event.plain_result(f"🗑️ 已彻底删除记忆 ID {index[:8]} 及其原始对话：\n📝 {summary[:50]}{'...' if len(summary) > 50 else ''}\n\n💡 如果误删，可使用 /mem_undo 撤销此操作。")
            else:
                yield event.plain_result(f"❌ {message}")

    @filter.command("mem_undo")
    async def mem_undo(self, event: AstrMessageEvent):
        """撤销最近一次删除操作"""
        user_id = event.get_sender_id()
        
        success, message, summary = await self.logic.undo_last_delete(user_id)
        
        if success:
            yield event.plain_result(f"✅ 撤销成功！已恢复记忆：\n📝 {summary[:80]}{'...' if len(summary) > 80 else ''}\n\n💡 记忆已重新添加到您的记忆库中。")
        else:
            yield event.plain_result(f"❌ {message}")

    @filter.command("mem_clear_raw")
    async def mem_clear_raw(self, event: AstrMessageEvent, confirm: str = ""):
        """清除所有未归档的原始消息数据"""
        user_id = event.get_sender_id()
        if confirm != "confirm":
            yield event.plain_result("⚠️ 危险操作：此指令将永久删除您所有**尚未归档**的聊天原文，且不可恢复。\n\n如果您确定要执行，请发送：\n/mem_clear_raw confirm")
            return
        
        loop = asyncio.get_event_loop()
        try:
            # 仅删除 RawMemory 中未归档的消息
            from .db_manager import RawMemory
            def _clear_raw():
                with self.logic.db.db.connection_context():
                    RawMemory.delete().where((RawMemory.user_id == user_id) & (RawMemory.is_archived == False)).execute()
            
            await loop.run_in_executor(self.logic.executor, _clear_raw)
            # 重置内存计数
            self.logic.unsaved_msg_count[user_id] = 0
            yield event.plain_result("🗑️ 已成功清除您所有未归档的原始对话消息。")
        except Exception as e:
            logger.error(f"Engram：清理原始记忆失败：{e}")
            yield event.plain_result(f"❌ 清除失败：{e}")

    @filter.command("mem_clear_archive")
    async def mem_clear_archive(self, event: AstrMessageEvent, confirm: str = ""):
        """清除所有长期记忆归档（保留原始消息）"""
        user_id = event.get_sender_id()
        if confirm != "confirm":
            yield event.plain_result("⚠️ 危险操作：此指令将永久删除您所有的**长期记忆归档**及向量检索数据，但会保留原始聊天记录。\n\n如果您确定要执行，请发送：\n/mem_clear_archive confirm")
            return
        
        loop = asyncio.get_event_loop()
        try:
            # 确保 ChromaDB 已初始化
            await self.logic._ensure_chroma_initialized()
            
            # 1. 清除 SQLite 中的总结索引 (MemoryIndex)
            from .db_manager import MemoryIndex, RawMemory
            def _clear_archive():
                with self.logic.db.db.connection_context():
                    # 删除索引
                    MemoryIndex.delete().where(MemoryIndex.user_id == user_id).execute()
                    # 将所有已归档的消息重新标记为未归档，以便可以重新总结
                    RawMemory.update(is_archived=False).where(RawMemory.user_id == user_id).execute()
            
            await loop.run_in_executor(self.logic.executor, _clear_archive)
            
            # 2. 清除 ChromaDB 中的向量数据
            await loop.run_in_executor(self.logic.executor, lambda: self.logic.collection.delete(where={"user_id": user_id}))
            
            yield event.plain_result("🗑️ 已成功清除您所有的长期记忆归档，原始消息已重置为待归档状态。")
        except Exception as e:
            logger.error(f"Engram：清理归档记忆失败：{e}")
            yield event.plain_result(f"❌ 清除失败：{e}")

    @filter.command("mem_clear_all")
    async def mem_clear_all(self, event: AstrMessageEvent, confirm: str = ""):
        """清除所有原始消息和长期记忆数据"""
        user_id = event.get_sender_id()
        if confirm != "confirm":
            yield event.plain_result("⚠️ 警告：此指令将永久删除您所有的聊天原文、长期记忆归档及向量检索数据，且不可恢复。\n\n如果您确定要执行，请发送：\n/mem_clear_all confirm")
            return
        
        loop = asyncio.get_event_loop()
        try:
            # 确保 ChromaDB 已初始化
            await self.logic._ensure_chroma_initialized()
            
            # 清除 SQLite 中的原始消息和索引
            await loop.run_in_executor(self.logic.executor, self.logic.db.clear_user_data, user_id)
            # 清除 ChromaDB 中的向量数据
            await loop.run_in_executor(self.logic.executor, lambda: self.logic.collection.delete(where={"user_id": user_id}))
            # 重置内存计数
            self.logic.unsaved_msg_count[user_id] = 0
            yield event.plain_result("🗑️ 已成功彻底清除您所有的原始对话消息和归档记忆。")
        except Exception as e:
            logger.error(f"Engram：清理全部记忆失败：{e}")
            yield event.plain_result(f"❌ 清除失败：{e}")

    @filter.command_group("profile")
    def profile_group(self, event: AstrMessageEvent): 
        """用户画像相关指令"""
        pass
    profile_group.__name__ = "profile_group"

    @profile_group.command("clear")
    async def profile_clear(self, event: AstrMessageEvent, confirm: str = ""):
        """清除用户画像数据"""
        user_id = event.get_sender_id()
        if confirm != "confirm":
            yield event.plain_result("⚠️ 危险操作：此指令将永久删除您的用户画像文件，所有侧写特征将被重置。\n\n如果您确定要执行，请发送：\n/profile clear confirm")
            return
        
        await self.logic.clear_user_profile(user_id)
        yield event.plain_result("🗑️ 您的用户画像已成功重置。")

    @profile_group.command("show")
    async def profile_show(self, event: AstrMessageEvent):
        """显示手账风格的用户深度画像"""
        user_id = event.get_sender_id()
        profile = await self.logic.get_user_profile(user_id)
        if not profile or not profile.get("basic_info"):
            yield event.plain_result("👤 您当前还没有建立深度画像。")
            return
        
        try:
            # 获取记忆数量
            loop = asyncio.get_event_loop()
            memories = await loop.run_in_executor(self.logic.executor, self.logic.db.get_memory_list, user_id, 100)
            memory_count = len(memories)
            
            # 渲染画像
            img_bytes = await self.profile_renderer.render(user_id, profile, memory_count)
            
            from astrbot.api.message_components import Image as MsgImage
            yield event.chain_result([MsgImage.fromBytes(img_bytes)])
        except Exception as e:
            logger.error(f"Engram：画像渲染失败：{e}")
            import traceback
            logger.debug(traceback.format_exc())
            yield event.plain_result(f"⚠️ 档案绘制失败，转为文本模式：\n{json.dumps(profile, indent=2, ensure_ascii=False)}")

    @profile_group.command("set")
    async def profile_set(self, event: AstrMessageEvent, key: str, value: str):
        """手动设置画像字段的值 (如: profile set basic_info.job 学生)"""
        user_id = event.get_sender_id()
        keys = key.split('.')
        update_data = {}
        curr = update_data
        for k in keys[:-1]:
            curr[k] = {}
            curr = curr[k]
        curr[keys[-1]] = value
        await self.logic.update_user_profile(user_id, update_data)
        yield event.plain_result(f"✅ 已更新画像：{key} = {value}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("engram_force_summarize")
    async def force_summarize(self, event: AstrMessageEvent):
        """[管理员] 立即对当前所有未处理对话进行记忆归档"""
        user_id = event.get_sender_id()
        yield event.plain_result("⏳ 正在强制执行记忆归档，请稍候...")
        await self.logic._summarize_private_chat(user_id)
        yield event.plain_result("✅ 记忆归档完成。您可以使用 /mem_list 查看。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("engram_force_summarize_all")
    async def force_summarize_all(self, event: AstrMessageEvent):
        """[管理员] 立即对所有用户未处理对话进行记忆归档"""
        yield event.plain_result("⏳ 正在强制执行全局记忆归档，请稍候...")
        total = await self.logic.summarize_all_users()
        yield event.plain_result(f"✅ 全局记忆归档完成。已处理 {total} 位用户。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("engram_force_persona")
    async def force_persona(self, event: AstrMessageEvent, days: str = ""):
        """[管理员] 立即基于指定天数的记忆强制深度更新画像
        
        参数:
            days: 回溯天数（可选，默认为1天/今天，设置为7则获取前7天的记忆）
        """
        user_id = event.get_sender_id()
        
        # 解析天数参数
        if days and days.isdigit():
            days_int = int(days)
            if days_int <= 0:
                yield event.plain_result("⚠️ 天数必须大于 0。")
                return
            if days_int > 365:
                yield event.plain_result("⚠️ 天数不能超过 365 天。")
                return
        else:
            days_int = 3  # 默认获取前3天的记忆
        
        # 计算时间范围：获取前N天的记忆
        now = datetime.datetime.now()
        start_time = (now - datetime.timedelta(days=days_int)).replace(hour=0, minute=0, second=0, microsecond=0)
        end_time = now  # 到现在为止
        time_desc = f"前 {days_int} 天"
        
        yield event.plain_result(f"⏳ 正在基于{time_desc}的记忆强制更新用户画像，请稍候...")
        
        # 调用画像更新
        await self.logic._update_persona_daily(user_id, start_time, end_time)
        yield event.plain_result(f"✅ 画像更新完成（基于{time_desc}的记忆）。您可以使用 /profile show 查看。")

    async def _run_rebuild_vectors(self, event: AstrMessageEvent, full_rebuild_flag: bool):
        """执行向量重建并回传统一结果。"""
        batch = 200

        mode_text = "全量重建" if full_rebuild_flag else "增量重建"
        yield event.plain_result(f"⏳ 正在执行向量库{mode_text}，请稍候...")

        try:
            result = await self.logic.rebuild_vector_collection(
                full_rebuild=full_rebuild_flag,
                batch_size=batch
            )

            success = bool(result.get("success", False))
            total = int(result.get("total", 0))
            rebuilt = int(result.get("rebuilt", 0))
            failed = int(result.get("failed", 0))
            message = str(result.get("message", "重建完成"))
            backup_dir = str(result.get("backup_dir", "") or "")

            extra_backup_line = f"\n备份目录：{backup_dir}" if backup_dir else ""

            if success:
                yield event.plain_result(
                    f"✅ {message}\n"
                    f"- 模式：{mode_text}\n"
                    f"- 总索引：{total}\n"
                    f"- 成功写入：{rebuilt}\n"
                    f"- 失败：{failed}"
                    f"{extra_backup_line}"
                )
            else:
                yield event.plain_result(
                    f"⚠️ {message}\n"
                    f"- 模式：{mode_text}\n"
                    f"- 总索引：{total}\n"
                    f"- 成功写入：{rebuilt}\n"
                    f"- 失败：{failed}\n"
                    f"💡 若 embedding_provider 变更或出现向量维度不匹配，请执行 /mem_rebuild_vector full 重新嵌入全部记忆"
                    f"{extra_backup_line}"
                )
        except Exception as e:
            logger.error(f"Engram：重建向量库失败：{e}")
            yield event.plain_result(f"❌ 向量库重建失败：{e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mem_rebuild_vector")
    async def mem_rebuild_vector(self, event: AstrMessageEvent, mode: str = ""):
        """[管理员] 备份并重建向量库（full 表示重建并回灌）

        重建说明：
            当 embedding_provider 变更或提示向量维度不一致时，
            请执行 /mem_rebuild_vector full 重新嵌入全部记忆，否则旧记忆将无法检索。
        """
        full_rebuild_flag = str(mode or "").strip().lower() == "full"
        async for result in self._run_rebuild_vectors(event, full_rebuild_flag):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("engram_rebuild_vectors")
    async def engram_rebuild_vectors(self, event: AstrMessageEvent, full_rebuild: str = "false", batch_size: str = ""):
        """[管理员] 兼容旧指令：重建向量库（仅 full 触发全量，batch 参数已弃用）"""
        full_rebuild_flag = str(full_rebuild or "").strip().lower() == "full"
        async for result in self._run_rebuild_vectors(event, full_rebuild_flag):
            yield result

    @filter.command("mem_export")
    async def mem_export(self, event: AstrMessageEvent, format: str = "jsonl", days: str = ""):
        """导出原始消息数据用于模型微调"""
        async for result in self.export_handler.handle_export_command(event, format, days):
            yield result

    @filter.command("mem_stats")
    async def mem_stats(self, event: AstrMessageEvent):
        """查看消息统计信息"""
        async for result in self.export_handler.handle_stats_command(event):
            yield result
    
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mem_export_all")
    async def mem_export_all(self, event: AstrMessageEvent, format: str = "jsonl", days: str = ""):
        """[管理员] 导出所有用户的原始消息数据"""
        async for result in self.export_handler.handle_export_all_command(event, format, days):
            yield result

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
            
            # 等待任务清理完成（最多0.5秒）
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._scheduler._tasks, return_exceptions=True),
                    timeout=0.5
                )
                logger.debug("Engram：所有调度任务已优雅停止")
            except asyncio.TimeoutError:
                logger.debug("Engram：部分调度任务未在限定时间内完成")
            except Exception as e:
                logger.debug(f"Engram：等待调度任务结束时发生异常：{e}")
        
        # 步骤3：最后关闭线程池和其他资源
        self.logic._memory_manager.shutdown()
        self.logic.executor.shutdown(wait=False)
        await self.profile_renderer.close()
