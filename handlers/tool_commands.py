"""
LLM 工具命令处理器 (Memory Tool Handler)

负责 mem_search_tool / overview / detail 等工具检索输出构建。
将 main.py 中的工具输出业务逻辑下沉，main 仅保留路由与参数收口。
"""

import json
import re
from astrbot.api import logger

from ..core.memory_models import MemorySearchResult


class MemoryToolHandler:
    """记忆工具处理器。"""

    def __init__(self, config, logic):
        self.config = config
        self.logic = logic

    @staticmethod
    def _normalize_mode(mode: str) -> str:
        token = str(mode or "hybrid").strip().lower()
        return token if token in {"hybrid", "semantic", "keyword", "recent"} else "hybrid"

    def _build_structured_output(self, *, title: str, query: str, mode: str, results, time_desc: str, normalized_types, extra_hint: str) -> str:
        payload = {
            "type": "memory_search_result",
            "title": title,
            "query": query,
            "mode": mode,
            "count": len(results),
            "time_filter": time_desc or "",
            "source_types": list(normalized_types or []),
            "results": [item.to_dict() if hasattr(item, "to_dict") else item for item in results],
            "usage_hint": "如需查看某条记忆的完整原始对话，请使用 mem_get_detail_tool 并传入 memory_id 或 memory_ids。",
        }
        if extra_hint:
            payload["extra_hint"] = extra_hint
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @staticmethod
    def _parse_legacy_memory_result(text: str, fallback_source_type: str = "private") -> dict:
        """Convert the legacy rendered memory text into structured tool output."""
        raw = str(text or "")
        header, _, body = raw.partition("\n")

        score = None
        score_match = re.search(r"🎯\s*(\d+)%", header)
        if score_match:
            try:
                score = int(score_match.group(1))
            except (TypeError, ValueError):
                score = None

        memory_id = ""
        id_match = re.search(r"🆔\s*([^\s|]+)", header)
        if id_match:
            memory_id = id_match.group(1).strip()

        created_at = ""
        created_match = re.search(r"⏰\s*([^|]+?)(?:\s*\||$)", header)
        if created_match:
            created_at = created_match.group(1).strip()

        source_type = fallback_source_type
        source_match = re.search(r"🗂️\s*([^\s|]+)", header)
        if source_match:
            source_type = source_match.group(1).strip() or fallback_source_type

        summary = body.strip() or raw.strip()
        if summary.startswith("📝"):
            summary = summary[1:].strip()
        if summary.startswith("归档："):
            summary = summary[len("归档："):].strip()

        return {
            "memory_id": memory_id,
            "source_type": source_type,
            "created_at": created_at,
            "score": score,
            "summary": summary,
        }

    async def build_memory_search_output(
        self,
        *,
        event,
        query: str,
        limit: int,
        time_expr: str,
        source_types,
        mode: str = "hybrid",
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
        normalized_mode = self._normalize_mode(mode)

        try:
            if callable(getattr(logic, "retrieve_memory_search_results", None)):
                results = await logic.retrieve_memory_search_results(
                    user_id,
                    query,
                    limit=final_limit,
                    start_time=start_time,
                    end_time=end_time,
                    source_types=normalized_types or None,
                    force_retrieve=True,
                    mode=normalized_mode,
                )
            else:
                legacy_results = await logic.retrieve_memories(
                    user_id,
                    query,
                    limit=final_limit,
                    start_time=start_time,
                    end_time=end_time,
                    source_types=normalized_types or None,
                    force_retrieve=True,
                    mode=normalized_mode,
                )
                fallback_source = (normalized_types or ["private"])[0]
                results = [
                    self._parse_legacy_memory_result(item, fallback_source_type=fallback_source)
                    for item in legacy_results
                ]
        except Exception as e:
            logger.error(f"Engram mem_search_tool 异常：{e}")
            return "工具检索失败，请稍后重试。"

        if not results:
            return f"未检索到与“{query}”相关的长期记忆。"

        return self._build_structured_output(
            title=title,
            query=query,
            mode=normalized_mode,
            results=results[:final_limit],
            time_desc=time_desc,
            normalized_types=normalized_types,
            extra_hint=extra_hint,
        )
