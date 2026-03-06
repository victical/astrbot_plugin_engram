"""
注入策略服务

- TopicMemoryCacheService: 话题级记忆缓存策略（TTL + 近似话题复用）
- ToolHintStrategyService: 工具提示注入策略（证据不足时触发）
"""

import re
import time
from typing import Dict, List, Set, Tuple


class TopicMemoryCacheService:
    """话题级缓存服务：降低同话题连续追问的检索漂移。"""

    def __init__(self, config: dict = None):
        self.config = config or {}
        # {user_id: {topic_key: {tokens, memories, expire_at, cached_at, query}}}
        self._cache: Dict[str, Dict[str, dict]] = {}

    def extract_topic_tokens(self, query: str) -> Set[str]:
        """提取话题 token（英文词 + 中文 2/3-gram）。"""
        text = str(query or "").strip().lower()
        if not text:
            return set()

        tokens: Set[str] = set()

        # 英文/数字词
        for token in re.findall(r"[a-z0-9]+", text):
            if len(token) >= 2:
                tokens.add(token)

        # 中文 n-gram（2/3）
        for block in re.findall(r"[\u4e00-\u9fff]+", text):
            block = block.strip()
            if not block:
                continue
            if len(block) <= 2:
                tokens.add(block)
                continue

            for n in (2, 3):
                if len(block) < n:
                    continue
                for i in range(0, len(block) - n + 1):
                    tokens.add(block[i:i + n])

        return tokens

    @staticmethod
    def topic_similarity(left_tokens: Set[str], right_tokens: Set[str]) -> float:
        """Jaccard 相似度。"""
        if not left_tokens or not right_tokens:
            return 0.0

        inter = len(left_tokens & right_tokens)
        union = len(left_tokens | right_tokens)
        if union <= 0:
            return 0.0
        return inter / union

    def build_topic_cache_key(self, query: str) -> str:
        """根据 token 构造稳定 key（仅用于索引，不直接代表语义）。"""
        tokens = sorted(self.extract_topic_tokens(query))
        if not tokens:
            compact = re.sub(r"[\W_]+", "", str(query or "").lower(), flags=re.UNICODE)
            return compact[:24]
        return "|".join(tokens[:16])

    def _get_ttl(self) -> int:
        try:
            ttl = int(self.config.get("memory_topic_cache_ttl", 120))
        except (TypeError, ValueError):
            ttl = 120
        return max(10, min(1800, ttl))

    def _get_max_topics(self) -> int:
        try:
            value = int(self.config.get("memory_topic_cache_max_topics", 3))
        except (TypeError, ValueError):
            value = 3
        return max(1, min(20, value))

    def _get_similarity_threshold(self) -> float:
        try:
            threshold = float(self.config.get("memory_topic_cache_similarity_threshold", 0.25))
        except (TypeError, ValueError):
            threshold = 0.25
        return max(0.05, min(0.95, threshold))

    def _prune(self, user_id: str) -> None:
        cache_by_topic = self._cache.get(user_id)
        if not isinstance(cache_by_topic, dict) or not cache_by_topic:
            self._cache.pop(user_id, None)
            return

        now_ts = time.time()
        alive = {
            k: v for k, v in cache_by_topic.items()
            if isinstance(v, dict) and float(v.get("expire_at", 0)) > now_ts
        }

        max_topics = self._get_max_topics()
        if len(alive) > max_topics:
            sorted_items = sorted(
                alive.items(),
                key=lambda kv: float(kv[1].get("cached_at", 0)),
                reverse=True,
            )
            alive = dict(sorted_items[:max_topics])

        if alive:
            self._cache[user_id] = alive
        else:
            self._cache.pop(user_id, None)

    def get_cached(self, user_id: str, query: str) -> Tuple[bool, List[str], str]:
        """命中返回 (True, memories, topic_key)，未命中返回 (False, [], topic_key)。"""
        if not self.config.get("enable_memory_topic_cache", True):
            return False, [], ""

        topic_key = self.build_topic_cache_key(query)
        if not topic_key:
            return False, [], ""

        self._prune(user_id)
        cache_by_topic = self._cache.get(user_id, {})

        # 1) 精确 key 命中
        exact = cache_by_topic.get(topic_key)
        if isinstance(exact, dict) and isinstance(exact.get("memories"), list):
            return True, list(exact.get("memories", [])), topic_key

        # 2) 近似话题命中
        query_tokens = self.extract_topic_tokens(query)
        if not query_tokens:
            return False, [], topic_key

        best_key = None
        best_score = 0.0
        for key, payload in cache_by_topic.items():
            tokens = payload.get("tokens") if isinstance(payload, dict) else None
            if not isinstance(tokens, set) or not tokens:
                continue
            score = self.topic_similarity(query_tokens, tokens)
            if score > best_score:
                best_score = score
                best_key = key

        if best_key is not None and best_score >= self._get_similarity_threshold():
            payload = cache_by_topic.get(best_key, {})
            memories = payload.get("memories", []) if isinstance(payload, dict) else []
            if isinstance(memories, list):
                return True, list(memories), best_key

        return False, [], topic_key

    def set_cached(self, user_id: str, query: str, topic_key: str, memories) -> None:
        """写入话题缓存（允许缓存空结果）。"""
        if not self.config.get("enable_memory_topic_cache", True):
            return

        topic_key = topic_key or self.build_topic_cache_key(query)
        if not topic_key:
            return

        cache_by_topic = self._cache.setdefault(user_id, {})
        now_ts = time.time()
        cache_by_topic[topic_key] = {
            "tokens": self.extract_topic_tokens(query),
            "memories": list(memories or []),
            "expire_at": now_ts + self._get_ttl(),
            "cached_at": now_ts,
            "query": str(query or ""),
        }
        self._prune(user_id)


class ToolHintStrategyService:
    """工具提示注入策略服务。"""

    _HINT_TEXT = (
        "【工具提示】\n"
        "仅当当前证据不足以确认用户历史事实时，再调用记忆工具补证。\n"
        "统一使用 mem_search_tool 检索，不再按概览/细节工具分流。\n"
        "参数：query(必填)，limit(可选，1-10)，time_expr(可选，时间筛选)，source_types(可选，类型筛选)。\n"
        "time_expr 示例：2026-01 / 2026-01-01~2026-01-31 / 02-23~03-01（未写年份默认今年）。\n"
        "若范围过大，请自行按二分法缩小 time_expr 后再次调用 mem_search_tool。\n"
        "source_types 可选：private, daily_summary, weekly, monthly, yearly。\n"
        "若需要查看某条记忆对应的更完整原始对话，请继续调用 mem_get_detail_tool。\n"
        "mem_get_detail_tool 参数：memory_id(必填，8位短ID或完整ID)，max_messages(可选，返回原文条数上限)。\n"
    )

    def __init__(self, config: dict = None):
        self.config = config or {}

    def should_inject(self, memory_count: int, should_retrieve: bool) -> bool:
        """是否注入工具提示：always / never / on_insufficient_evidence。"""
        if not self.config.get("enable_memory_search_tool", True):
            return False

        mode = str(self.config.get("memory_tool_hint_mode", "on_insufficient_evidence")).strip().lower()
        if mode == "never":
            return False
        if mode == "always":
            return True

        # 默认 on_insufficient_evidence
        try:
            min_memories = int(self.config.get("memory_tool_hint_min_memories", 1))
        except (TypeError, ValueError):
            min_memories = 1
        min_memories = max(1, min(10, min_memories))

        if not should_retrieve:
            return False

        return int(memory_count or 0) < min_memories

    def build_hint_text(self) -> str:
        return self._HINT_TEXT
