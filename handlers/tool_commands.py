"""
LLM 工具命令处理器 (Memory Tool Handler)

负责 mem_search_tool / overview / detail 等工具检索输出构建。
将 main.py 中的工具输出业务逻辑下沉，main 仅保留路由与参数收口。
"""

import re
from astrbot.api import logger


class MemoryToolHandler:
    """记忆工具处理器。"""

    def __init__(self, config, logic):
        self.config = config
        self.logic = logic

    async def build_memory_search_output(
        self,
        *,
        event,
        query: str,
        limit: int,
        time_expr: str,
        source_types,
        default_types=None,
        title: str = "🧠 工具检索结果",
        extra_hint: str = "",
        parse_time_expr,
        normalize_source_types,
        get_logic=None,
        resolve_user_id=None,
    ) -> str:
        """统一构建记忆检索工具输出。"""
        if not self.config.get("enable_memory_search_tool", True):
            return "记忆检索工具已关闭。"

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
        logic = self.logic
        if callable(get_logic):
            logic = await get_logic(event) or self.logic

        user_id = event.get_sender_id()
        if callable(resolve_user_id):
            resolved = resolve_user_id(event)
            if resolved:
                user_id = resolved

        # 时间过滤：仅使用显式 time_expr（由 LLM 提供），不再从 query 自动识别
        parse_target = str(time_expr or "").strip()
        try:
            start_time, end_time, time_desc = parse_time_expr(parse_target)
        except re.error as e:
            logger.warning(f"Engram mem_search_tool：time_expr 正则解析失败：{e}")
            start_time, end_time, time_desc = None, None, ""
        except Exception as e:
            logger.warning(f"Engram mem_search_tool：解析 time_expr 失败：{e}")
            start_time, end_time, time_desc = None, None, ""

        normalized_types = normalize_source_types(source_types, default_types=default_types)

        try:
            memories = await logic.retrieve_memories(
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
