"""
轻量级意图过滤器
用于判断用户消息是否需要进行长期记忆检索

支持三种模式（通过配置项 memory_intent_mode 控制）：
  - disabled : 禁用过滤，每条消息都触发检索（向后兼容）
  - keyword  : 仅当消息包含强触发关键词时才检索（默认，零成本）
  - llm      : 调用小模型判断是否需要检索（高精度，有少量 Token 成本）
"""
import re
from typing import Any, Optional, Set

from astrbot.api import logger

# LLM 判断提示词（精简，节省 Token）
_LLM_INTENT_PROMPT = (
    "判断以下用户消息是否需要调用长期记忆来回答。"
    "长期记忆包含用户的历史对话、个人偏好、过去事件等信息。\n"
    "只有当用户在询问过去的事情、引用之前的对话、或问题需要了解用户历史才能正确回答时，才需要调用。\n"
    "日常寒暄、简单问候、即时性问题（如天气、时间）不需要调用。\n\n"
    "用户消息：{query}\n\n"
    "请只回答一个字：是 或 否"
)

# 默认强触发词——明确指向"过去"的词汇
_DEFAULT_STRONG_TRIGGERS: Set[str] = {
    "记得", "之前", "以前", "上次", "上回",
    "回忆", "提醒", "你说", "告诉过",
    "承诺", "答应", "说过", "聊过",
}


class IntentClassifier:
    """
    意图过滤器：判断查询是否需要召回长期记忆

    Args:
        config:  插件配置字典，读取 memory_intent_mode / intent_llm_model 等
        context: AstrBot Context，用于获取 LLM provider（仅 llm 模式需要）
    """

    def __init__(self, config: Optional[dict] = None, context: Any = None):
        self._config = config or {}
        self._context = context

        # 从配置读取模式，默认 keyword
        self._mode: str = str(self._config.get("memory_intent_mode", "keyword")).lower()
        if self._mode not in ("disabled", "keyword", "llm"):
            logger.warning(f"Engram IntentClassifier: unknown mode '{self._mode}', falling back to 'keyword'")
            self._mode = "keyword"

        # 关键词模式参数（防御性转换：空字符串、非法值均回退默认 4）
        raw_min_len = self._config.get("intent_min_length", 4)
        try:
            val = int(raw_min_len) if str(raw_min_len).strip() else 4
        except (ValueError, TypeError):
            val = 4
        self._min_length: int = max(1, val)
        self._strong_triggers: Set[str] = set(_DEFAULT_STRONG_TRIGGERS)

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def should_retrieve_memory(self, query: str) -> bool:
        """判断是否需要检索长期记忆（异步，兼容 LLM 模式）"""
        if self._mode == "disabled":
            return True

        if not query:
            return False

        text = str(query).strip()
        if not text:
            return False

        # 公共前置：过滤极短文本
        compact = re.sub(r"[\s\W]+", "", text, flags=re.UNICODE)
        if len(compact) < self._min_length:
            return False

        if self._mode == "llm":
            return await self._llm_check(text)

        # keyword 模式（默认）
        return self._keyword_check(text)

    # ------------------------------------------------------------------
    # 关键词匹配（零成本快速路径）
    # ------------------------------------------------------------------

    def _keyword_check(self, text: str) -> bool:
        """通过强触发词判断"""
        for trigger in self._strong_triggers:
            if trigger in text:
                return True
        return False

    # ------------------------------------------------------------------
    # LLM 判断（高精度路径）
    # ------------------------------------------------------------------

    async def _llm_check(self, text: str) -> bool:
        """调用小模型判断是否需要记忆检索"""
        if not self._context:
            logger.warning("Engram IntentClassifier: LLM mode enabled but no context, falling back to keyword")
            return self._keyword_check(text)

        try:
            # 优先使用配置的意图判断模型，其次归档模型，最后默认模型
            model_id = (
                self._config.get("intent_llm_model", "").strip()
                or self._config.get("summarize_model", "").strip()
            )
            if model_id:
                provider = self._context.get_provider_by_id(model_id)
                if not provider:
                    provider = self._context.get_using_provider()
            else:
                provider = self._context.get_using_provider()

            if not provider:
                logger.warning("Engram IntentClassifier: no LLM provider available, falling back to keyword")
                return self._keyword_check(text)

            prompt = _LLM_INTENT_PROMPT.format(query=text)
            resp = await provider.text_chat(prompt=prompt)
            answer = resp.completion_text.strip() if resp.completion_text else ""

            # 宽容解析：兼容中英文模型各种回答格式
            if not answer:
                result = False
            else:
                cleaned = answer.strip().replace("。", "").replace(".", "").replace("，", "").replace(",", "")
                result = cleaned in ("是", "Yes", "yes", "Y", "y", "需要", "true", "True")
            logger.debug(f"Engram IntentClassifier LLM: query='{text[:30]}' -> {answer[:5]} -> retrieve={result}")
            return result

        except Exception as e:
            logger.warning(f"Engram IntentClassifier: LLM check failed ({e}), falling back to keyword")
            return self._keyword_check(text)
