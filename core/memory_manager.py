"""
记忆管理器 (Memory Manager)

负责记忆的存储、检索、归档、删除等核心操作。
从 memory_logic.py 提取而来，遵循单一职责原则。

主要功能：
- ChromaDB 向量库的延迟初始化与管理
- 原始消息记录
- 记忆归档与总结（按天分组）
- 语义检索（支持关键词重排序）
- 记忆删除与撤销
- 数据导出（多格式支持）

依赖：
- context: AstrBot API 上下文（用于 LLM 调用）
- config: 插件配置
- db_manager: 数据库管理器
- profile_manager: 用户画像管理器（用于实时更新画像）
"""

try:
    import chromadb
except ImportError:
    chromadb = None

import os
import shutil
import uuid
import json
import re
import asyncio
import time
import datetime
from threading import Lock
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from astrbot.api import logger
from ..services.intent_classifier import IntentClassifier

# 预编译正则表达式
_CHINESE_PATTERN = re.compile(r'[\u4e00-\u9fa5]')
_ENGLISH_WORD_PATTERN = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")
_CHINESE_BLOCK_PATTERN = re.compile(r"[\u4e00-\u9fa5]+")


class MemoryManager:
    """记忆管理器"""

    # MemoryManager 运行所需的 DB 契约（用于启动阶段自检）
    REQUIRED_DB_METHODS = (
        "save_raw_memory",
        "get_unarchived_raw",
        "get_last_memory_index",
        "mark_as_archived",
        "save_memory_index",
        "get_all_user_ids",
        "get_summaries_by_type",
        "get_memory_list",
        "get_memory_indexes_by_ids",
        "get_prev_indices_by_ids",
        "get_raw_memories_map_by_uuid_lists",
        "get_memories_by_uuids",
        "update_active_score",
        "delete_raw_memories_by_uuids",
        "delete_memory_index",
        "get_all_raw_messages",
        "get_message_stats",
        "get_all_users_messages",
        "get_all_users_stats",
    )

    def __init__(self, context, config, data_dir, executor, db_manager, profile_manager=None, chroma_path: str = None, default_source_type: str = "private"):
        """
        初始化记忆管理器

        Args:
            context: AstrBot API 上下文对象
            config: 插件配置字典
            data_dir: 数据目录路径
            executor: ThreadPoolExecutor 实例
            db_manager: DatabaseManager 实例
            profile_manager: ProfileManager 实例（可选，用于实时画像更新）
            chroma_path: ChromaDB 存储路径（可选，默认使用 data_dir/engram_chroma）
            default_source_type: 默认写入的 source_type（默认 private）
        """
        self.context = context
        self.config = config
        self.data_dir = data_dir
        self.executor = executor
        self.db = db_manager
        self.profile_manager = profile_manager
        self._intent_classifier = IntentClassifier(config=self.config, context=self.context)
        self.default_source_type = str(default_source_type or "private").strip() or "private"

        # 启动阶段接口自检：避免 DB 契约漂移导致运行时 AttributeError
        self._verify_db_contract(stage="MemoryManager.__init__")

        # 近期动态（A/B）
        self._recent_events = []
        self._recent_events_lock = Lock()
        self._recent_events_max = 50

        # ChromaDB 延迟初始化（避免构造函数阻塞）
        self.chroma_path = chroma_path or os.path.join(self.data_dir, "engram_chroma")
        self.chroma_client = None
        self.collection = None
        self._chroma_init_lock = asyncio.Lock()
        self._chroma_initialized = False

        # 内存中记录最后聊天时间（带自动清理机制）
        self.last_chat_time = {}     # {user_id: timestamp}
        self.unsaved_msg_count = {}  # {user_id: count}
        self._max_inactive_users = 100  # 最大缓存用户数
        self._inactive_threshold = 7 * 24 * 3600  # 7天无活动则清理

        # 撤销删除缓存：{user_id: [最近删除的记忆列表]}
        self._delete_history = {}  # 每个用户保留最近3次删除
        self._max_undo_history = 3

        self._is_shutdown = False
        self._embedding_provider_id = str(self.config.get("embedding_provider", "")).strip()
        self._embedding_unavailable_logged = False

        # 向量写入失败补偿队列（内存态）：用于后续重建/告警
        self._pending_vector_jobs = []
        self._max_pending_vector_jobs = 5000

    def shutdown(self):
        """关闭记忆管理器"""
        self._is_shutdown = True

    def _verify_db_contract(self, stage="startup"):
        """校验 DB 接口契约，优先复用稳定接口层的 verify_contract。"""
        if hasattr(self.db, "verify_contract"):
            self.db.verify_contract(required_methods=self.REQUIRED_DB_METHODS, stage=stage)
            return

        missing = [
            name for name in self.REQUIRED_DB_METHODS
            if not callable(getattr(self.db, name, None))
        ]
        if missing:
            missing_sorted = sorted(set(missing))
            message = (
                f"Engram DB 契约检查失败（missing methods），阶段={stage}："
                f"缺失方法 -> {', '.join(missing_sorted)}"
            )
            logger.error(message)
            raise AttributeError(message)

    # ========== ChromaDB 管理 ==========

    async def _ensure_chroma_initialized(self):
        """确保 ChromaDB 已初始化（延迟初始化，避免构造函数阻塞）"""
        # 仅日志告警：向量模型未配置时不抛错，后续检索/写入链路会优雅降级
        self._embedding_provider_id = str(self.config.get("embedding_provider", "")).strip()
        if not self._embedding_provider_id:
            self._warn_embedding_unavailable("未配置 embedding_provider，将跳过向量检索与写入")
        elif not self.context or not self.context.get_provider_by_id(self._embedding_provider_id):
            self._warn_embedding_unavailable(
                f"embedding_provider '{self._embedding_provider_id}' 不可用，将跳过向量检索与写入"
            )

        if self._chroma_initialized:
            return

        async with self._chroma_init_lock:
            # 双重检查
            if self._chroma_initialized:
                return

            # 在线程池中初始化 ChromaDB（避免构造函数阻塞）
            loop = asyncio.get_event_loop()

            def _init_chroma():
                client = chromadb.PersistentClient(path=self.chroma_path)
                collection = client.get_or_create_collection(name="long_term_memories")
                return client, collection

            try:
                self.chroma_client, self.collection = await loop.run_in_executor(
                    self.executor, _init_chroma
                )
                self._chroma_initialized = True
                logger.info(
                    "Engram：ChromaDB 初始化成功（embedding_provider=%s）",
                    self._embedding_provider_id
                )
            except Exception as e:
                logger.error(f"Engram：初始化 ChromaDB 失败：{e}")
                raise

    # ========== 辅助方法 ==========

    def _cleanup_inactive_users(self):
        """清理长期不活跃的用户缓存，防止内存泄漏"""
        now_ts = time.time()

        # 找出所有超过阈值的不活跃用户
        inactive_users = [
            user_id for user_id, last_time in self.last_chat_time.items()
            if now_ts - last_time > self._inactive_threshold
        ]

        # 清理不活跃用户（但只有在已归档后才清理）
        for user_id in inactive_users:
            if self.unsaved_msg_count.get(user_id, 0) == 0:
                self.last_chat_time.pop(user_id, None)
                self.unsaved_msg_count.pop(user_id, None)

        # 如果用户数仍然过多，按最后活跃时间排序，保留最近的
        if len(self.last_chat_time) > self._max_inactive_users:
            sorted_users = sorted(self.last_chat_time.items(), key=lambda x: x[1], reverse=True)
            users_to_keep = set(u[0] for u in sorted_users[:self._max_inactive_users])

            for user_id in list(self.last_chat_time.keys()):
                if user_id not in users_to_keep and self.unsaved_msg_count.get(user_id, 0) == 0:
                    self.last_chat_time.pop(user_id, None)
                    self.unsaved_msg_count.pop(user_id, None)

    @staticmethod
    def _ensure_datetime(timestamp):
        """
        确保时间戳是 datetime 对象。
        如果是整数或浮点数（Unix 时间戳），则转换为 datetime 对象。
        """
        if isinstance(timestamp, (int, float)):
            return datetime.datetime.fromtimestamp(timestamp)
        return timestamp

    def _is_valid_message_content(self, content: str) -> bool:
        """
        统一的消息内容过滤逻辑，用于判断消息是否应被纳入归档/检索。

        过滤规则：
        1. 以配置的指令前缀开头的消息
        2. 带下划线且无空格的内部指令
        3. 中文字符不足2个且总长度不足10的短消息

        返回 True 表示消息有效，False 表示应被过滤。
        """
        content = content.strip()

        # 1. 过滤以配置的指令前缀开头的消息
        if self.config.get("enable_command_filter", True):
            command_prefixes = self.config.get("command_prefixes", ["/", "!", "#", "~"])
            if isinstance(command_prefixes, str):
                command_prefixes = [command_prefixes]
            command_prefixes = [str(p) for p in command_prefixes if str(p)]
            if command_prefixes and content.startswith(tuple(command_prefixes)):
                return False

        # 2. 专门清洗带下划线的内部指令
        if "_" in content and " " not in content:
            return False

        # 3. 统计中文数量或检查总长度
        chinese_chars = _CHINESE_PATTERN.findall(content)
        if len(chinese_chars) < 2 and len(content) < 10:
            return False

        return True

    def _get_allowed_source_types(self):
        """获取允许的 source_type 列表（含默认/群聊配置）。"""
        allowed = {"private", "daily_summary", "weekly", "monthly", "yearly"}
        default_type = str(self.default_source_type or "").strip().lower()
        if default_type:
            allowed.add(default_type)
        extra_type = str(self.config.get("group_memory_source_type", "")).strip().lower()
        if extra_type:
            allowed.add(extra_type)
        return allowed

    def _generate_query_keywords(self, query: str):
        """生成中英混合关键词：英文按词切分，中文按 2~4 gram 切分。"""
        min_n = max(2, int(self.config.get("keyword_ngram_min", 2)))
        max_n = max(min_n, int(self.config.get("keyword_ngram_max", 4)))
        max_n = min(max_n, 6)  # 防御性上限，避免极端配置导致组合爆炸

        common_stopwords = {
            "a", "an", "the", "to", "of", "in", "on", "at", "is", "are", "i", "you", "he", "she", "it",
            "我", "你", "他", "她", "它", "这", "那", "了", "啊", "呀", "吗", "呢", "吧", "和", "与", "及", "就", "也"
        }
        protected_tokens = {"ai", "ml", "db", "go", "c", "r"}

        english_tokens = _ENGLISH_WORD_PATTERN.findall(query.lower())
        query_keywords = set()

        for token in english_tokens:
            if not token:
                continue
            if len(token) <= 1 and token not in protected_tokens:
                continue
            if token in common_stopwords:
                continue
            query_keywords.add(token)

        chinese_blocks = _CHINESE_BLOCK_PATTERN.findall(query)
        for block in chinese_blocks:
            block_len = len(block)
            if block_len == 0:
                continue
            for n in range(min_n, max_n + 1):
                if block_len < n:
                    continue
                for i in range(0, block_len - n + 1):
                    gram = block[i:i + n]
                    if gram in common_stopwords:
                        continue
                    query_keywords.add(gram)

        return query_keywords

    def _count_keyword_matches(self, keyword: str, summary_tokens_en, summary_ngrams_zh):
        """边界感知匹配：英文按词边界，中文按 n-gram 精确计数。"""
        if not keyword:
            return 0

        if _CHINESE_PATTERN.search(keyword):
            return summary_ngrams_zh.get(keyword, 0)

        return summary_tokens_en.get(keyword.lower(), 0)

    def _calc_keyword_score(self, query: str, summary: str, corpus_stats: dict):
        """计算关键词得分（边界感知匹配 + 近似 IDF）。"""
        query_keywords = self._generate_query_keywords(query)
        if not query_keywords or not summary:
            return 0.0, query_keywords

        summary_lower = summary.lower()
        summary_tokens_en = Counter(_ENGLISH_WORD_PATTERN.findall(summary_lower))

        min_n = max(2, int(self.config.get("keyword_ngram_min", 2)))
        max_n = max(min_n, int(self.config.get("keyword_ngram_max", 4)))
        max_n = min(max_n, 6)

        summary_ngrams_zh = Counter()
        chinese_blocks = _CHINESE_BLOCK_PATTERN.findall(summary)
        for block in chinese_blocks:
            block_len = len(block)
            for n in range(min_n, max_n + 1):
                if block_len < n:
                    continue
                for i in range(0, block_len - n + 1):
                    summary_ngrams_zh[block[i:i + n]] += 1

        matched_tf_sum = 0
        doc_len = max(1, len(summary_tokens_en) + sum(summary_ngrams_zh.values()))

        _bm25_k1 = 1.2
        _bm25_b = 0.75
        _avg_doc_len = 80

        keyword_score = 0.0
        total_docs = max(1, int(corpus_stats.get("total_docs", 1)))
        keyword_df = corpus_stats.get("keyword_doc_freq", {})

        for keyword in query_keywords:
            tf = self._count_keyword_matches(keyword, summary_tokens_en, summary_ngrams_zh)
            if tf <= 0:
                continue

            matched_tf_sum += tf
            norm_tf = (tf * (_bm25_k1 + 1)) / (tf + _bm25_k1 * (1 - _bm25_b + _bm25_b * doc_len / _avg_doc_len))

            # 稀有词提升（近似 IDF）：出现越少，权重越高
            df = keyword_df.get(keyword, 0)
            idf = 1.0 + ((total_docs + 1.0) / (df + 1.0))
            keyword_score += norm_tf * min(4.0, idf)

        coverage_bonus = min(1.5, matched_tf_sum / max(1, len(query_keywords)))
        return keyword_score * (1.0 + 0.15 * coverage_bonus), query_keywords

    def _calc_keyword_score_legacy(self, query_keywords, summary: str):
        """旧版关键词打分：子串匹配 + BM25 风格 TF 饱和。"""
        if not query_keywords or not summary:
            return 0.0

        _bm25_k1 = 1.2
        _bm25_b = 0.75
        _avg_doc_len = 80

        score = 0.0
        summary_lower = summary.lower()
        doc_len = max(1, len(summary_lower))

        for keyword in query_keywords:
            if keyword in summary_lower:
                tf = summary_lower.count(keyword)
                norm_tf = (tf * (_bm25_k1 + 1)) / (tf + _bm25_k1 * (1 - _bm25_b + _bm25_b * doc_len / _avg_doc_len))
                keyword_weight = max(1.0, min(3.0, len(keyword) / 2.0))
                score += norm_tf * keyword_weight

        return score

    def _normalize_str_list(self, value, max_len=None):
        """将输入归一为字符串列表（去空、去重、保序）"""
        if value is None:
            return []
        if isinstance(value, str):
            items = [value]
        elif isinstance(value, list):
            items = value
        else:
            items = [str(value)]

        seen = set()
        result = []
        for item in items:
            text = str(item).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
            if max_len and len(result) >= max_len:
                break
        return result

    def _build_structured_summary(self, payload: dict) -> str:
        """从结构化 JSON 中拼装可检索摘要"""
        summary = str(payload.get("summary", "")).strip()
        key_facts = self._normalize_str_list(payload.get("key_facts"), max_len=4)
        keywords = self._normalize_str_list(payload.get("keywords"), max_len=10)
        entities = self._normalize_str_list(payload.get("entities"), max_len=6)
        mood = str(payload.get("mood", "")).strip()

        if not summary:
            if key_facts:
                summary = "；".join(key_facts[:3])
            elif keywords:
                summary = "、".join(keywords[:6])

        extras = []
        if key_facts:
            extras.append("要点:" + "；".join(key_facts))
        if keywords:
            extras.append("关键词:" + "、".join(keywords))
        if entities:
            extras.append("涉及:" + "、".join(entities))
        if mood:
            extras.append("情绪:" + mood)

        if extras:
            summary = (summary + "\n" + " | ".join(extras)).strip()

        # 防御性裁剪，避免过长
        if len(summary) > 220:
            summary = summary[:217] + "..."
        return summary

    def _warn_embedding_unavailable(self, message: str):
        """向量模型不可用时仅记录日志，不抛异常。"""
        if not self._embedding_unavailable_logged:
            logger.warning(f"Engram：{message}")
            self._embedding_unavailable_logged = True
        else:
            logger.debug(f"Engram：{message}")

    def _enqueue_pending_vector_jobs(self, rows, reason: str = ""):
        """记录向量写入失败的索引，便于后续重建补偿。"""
        if not rows:
            return

        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for row in rows:
            idx = str(row.get("index_id", "")).strip()
            if not idx:
                continue
            self._pending_vector_jobs.append({
                "index_id": idx,
                "user_id": str(row.get("user_id", "")),
                "source_type": str(row.get("source_type", "")),
                "created_at": str(row.get("created_at", "")),
                "reason": str(reason or "embedding_unavailable"),
                "queued_at": now,
            })

        # 防止队列无限增长
        if len(self._pending_vector_jobs) > self._max_pending_vector_jobs:
            self._pending_vector_jobs = self._pending_vector_jobs[-self._max_pending_vector_jobs:]

    def _clear_pending_vector_jobs(self, index_ids):
        """在向量补齐成功后清除补偿队列记录。"""
        if not index_ids or not self._pending_vector_jobs:
            return
        id_set = {str(i) for i in index_ids if str(i)}
        if not id_set:
            return
        self._pending_vector_jobs = [
            item for item in self._pending_vector_jobs
            if str(item.get("index_id", "")) not in id_set
        ]

    @staticmethod
    def _is_dimension_mismatch_error(error: Exception) -> bool:
        """判断是否为向量维度不匹配错误。"""
        msg = str(error or "").lower()
        return "expecting embedding with dimension" in msg and "got" in msg

    async def _get_embedding_provider(self):
        """获取配置的嵌入 Provider（不可用时返回 None，仅日志告警）。"""
        provider_id = str(self.config.get("embedding_provider", "")).strip()
        if not provider_id:
            self._warn_embedding_unavailable("未配置 embedding_provider")
            return None, ""

        provider = self.context.get_provider_by_id(provider_id) if self.context else None
        if not provider:
            self._warn_embedding_unavailable(f"embedding_provider '{provider_id}' 不可用")
            return None, provider_id

        return provider, provider_id

    def _normalize_embeddings_result(self, result):
        """兼容多种 provider 返回格式，标准化为 List[List[float]]。"""
        if result is None:
            return []

        if hasattr(result, "embeddings"):
            result = getattr(result, "embeddings")

        if isinstance(result, dict):
            if "embeddings" in result:
                result = result.get("embeddings")
            elif "data" in result:
                result = result.get("data")

        if hasattr(result, "data") and not isinstance(result, (list, tuple, dict, str, bytes)):
            result = getattr(result, "data")

        if not isinstance(result, (list, tuple)):
            return []

        if result and isinstance(result[0], dict) and "embedding" in result[0]:
            result = [item.get("embedding") for item in result]

        vectors = []
        for vec in result:
            if not isinstance(vec, (list, tuple)):
                continue
            try:
                vectors.append([float(x) for x in vec])
            except (TypeError, ValueError):
                continue

        return vectors

    @staticmethod
    def _extract_max_batch_size_from_error(error: Exception) -> int:
        """从 provider 错误信息中提取最大 batch 限制（提取失败返回 0）。"""
        msg = str(error or "")
        if not msg:
            return 0

        patterns = (
            r"maximum\s+allowed\s+batch\s+size\s*(?:is\s*)?(\d+)",
            r"batch\s+size\s*\d+\s*>\s*maximum\s+allowed\s+batch\s+size\s*(\d+)",
            r"max(?:imum)?\s+batch\s+size[^\d]*(\d+)",
        )
        lower_msg = msg.lower()
        for pattern in patterns:
            match = re.search(pattern, lower_msg)
            if not match:
                continue
            try:
                return max(1, int(match.group(1)))
            except (TypeError, ValueError):
                continue
        return 0

    async def _ensure_embeddings(self, texts):
        """使用配置的嵌入 Provider 生成向量，禁止回退到 Chroma 内置模型。"""
        if not texts:
            return []

        provider, provider_id = await self._get_embedding_provider()
        if not provider:
            return []

        method_names = (
            "text_embedding",
            "embeddings",
            "embed_texts",
            "embed_documents",
            "embed",
            "get_embeddings",
        )

        last_error = None
        for method_name in method_names:
            method = getattr(provider, method_name, None)
            if not callable(method):
                continue

            call_variants = ("texts", "input", "documents", "text", "positional")

            async def _invoke(variant: str, payload):
                if variant == "texts":
                    result = method(texts=payload)
                elif variant == "input":
                    result = method(input=payload)
                elif variant == "documents":
                    result = method(documents=payload)
                elif variant == "text":
                    result = method(text=payload)
                else:
                    result = method(payload)

                if asyncio.iscoroutine(result):
                    result = await result
                return self._normalize_embeddings_result(result)

            for variant in call_variants:
                try:
                    vectors = await _invoke(variant, texts)
                    if vectors:
                        return vectors
                except TypeError as e:
                    last_error = e
                    continue
                except Exception as e:
                    last_error = e
                    max_batch_size = self._extract_max_batch_size_from_error(e)
                    if max_batch_size > 0 and len(texts) > max_batch_size:
                        merged_vectors = []
                        chunk_failed = False
                        for i in range(0, len(texts), max_batch_size):
                            chunk = texts[i:i + max_batch_size]
                            try:
                                chunk_vectors = await _invoke(variant, chunk)
                                if not chunk_vectors or len(chunk_vectors) != len(chunk):
                                    chunk_failed = True
                                    last_error = ValueError(
                                        f"embedding chunk result mismatch: chunk={len(chunk)}, vecs={len(chunk_vectors) if chunk_vectors else 0}"
                                    )
                                    break
                                merged_vectors.extend(chunk_vectors)
                            except Exception as chunk_error:
                                last_error = chunk_error
                                chunk_failed = True
                                break

                        if not chunk_failed and len(merged_vectors) == len(texts):
                            logger.info(
                                "Engram：embedding 请求超出 provider 批量上限，已自动分片（total=%s, chunk=%s）",
                                len(texts),
                                max_batch_size,
                            )
                            return merged_vectors
                    break

        self._warn_embedding_unavailable(
            f"embedding_provider '{provider_id or 'N/A'}' 未返回可用向量，last_error={last_error}"
        )
        return []

    async def _collection_add_texts(self, ids, documents, metadatas, embeddings=None):
        """统一写入 Chroma，强制使用外部 embeddings。不可用时跳过并返回 False。"""
        if embeddings is None:
            embeddings = await self._ensure_embeddings(documents)

        if not embeddings or len(embeddings) != len(documents):
            self._warn_embedding_unavailable(
                f"写入向量已跳过：embedding 数量与文档数量不一致（embeddings={len(embeddings) if embeddings else 0}, docs={len(documents)}）"
            )
            return False

        add_params = {
            "ids": ids,
            "documents": documents,
            "metadatas": metadatas,
            "embeddings": embeddings,
        }
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(self.executor, lambda: self.collection.add(**add_params))
            return True
        except Exception as e:
            if self._is_dimension_mismatch_error(e):
                logger.warning(
                    "Engram：Chroma 向量维度不匹配（旧库维度与当前 embedding 维度不同）。"
                    "请执行管理员指令 /mem_rebuild_vector full 重建向量库，"
                    "或切回原 embedding_provider。"
                )
                return False
            raise

    async def _collection_query_text(self, query, n_results, where):
        """统一查询 Chroma，强制使用外部 query_embeddings。不可用时返回 None。"""
        query_vectors = await self._ensure_embeddings([query])
        if not query_vectors:
            self._warn_embedding_unavailable("查询向量生成失败，已跳过本次记忆检索")
            return None

        query_params = {
            "query_embeddings": [query_vectors[0]],
            "n_results": n_results,
            "where": where,
        }
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(self.executor, lambda: self.collection.query(**query_params))
        except Exception as e:
            if self._is_dimension_mismatch_error(e):
                logger.warning(
                    "Engram：Chroma 检索维度不匹配（旧库维度与当前 embedding 维度不同）。"
                    "请执行管理员指令 /mem_rebuild_vector full 重建向量库，"
                    "或切回原 embedding_provider。"
                )
                return None
            raise

    # ========== 消息记录 ==========

    async def record_message(self, user_id, session_id, role, content, msg_type="text", user_name=None, **extra_fields):
        """记录原始消息"""
        normalized_content = str(content or "").strip()
        if not self._is_valid_message_content(normalized_content):
            logger.debug("Engram：已跳过空白/无效原始消息 role=%s user_id=%s", role, user_id)
            return

        msg_uuid = str(uuid.uuid4())

        # 异步保存到 SQLite
        loop = asyncio.get_event_loop()
        params = {
            "uuid": msg_uuid,
            "session_id": session_id,
            "user_id": user_id,
            "user_name": user_name,
            "role": role,
            "content": normalized_content,
            "msg_type": msg_type,
            "timestamp": datetime.datetime.now()
        }
        await loop.run_in_executor(self.executor, lambda: self.db.save_raw_memory(**params))

        # 更新记录
        if role == "user":
            self.last_chat_time[user_id] = datetime.datetime.now().timestamp()
            self.unsaved_msg_count[user_id] = self.unsaved_msg_count.get(user_id, 0) + 1

    # ========== 近期动态 ==========

    def add_activity(self, title: str, *, category: str = "task", source: str = "private", meta: dict | None = None):
        event = {
            "title": str(title or "").strip() or "-",
            "category": category,
            "source": source,
            "meta": meta or {},
            "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        with self._recent_events_lock:
            self._recent_events.insert(0, event)
            if len(self._recent_events) > self._recent_events_max:
                self._recent_events = self._recent_events[: self._recent_events_max]

    def get_recent_activities(self, limit: int = 8) -> list:
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = 8
        with self._recent_events_lock:
            return list(self._recent_events[:limit])

    def _record_memory_event(self, summary: str, user_id: str, source_type: str):
        summary = str(summary or "").strip()
        if not summary:
            return
        title = summary[:36] + ("..." if len(summary) > 36 else "")
        self.add_activity(
            title=title,
            category="memory",
            source=str(source_type or "private"),
            meta={"user_id": str(user_id or "")},
        )

    # ========== 记忆归档与总结 ==========
    
    async def check_and_summarize(self):
        """检查是否需要进行私聊归档（画像更新由独立调度器处理）"""
        now_ts = datetime.datetime.now().timestamp()
        timeout = self.config.get("private_memory_timeout", 1800)
        min_count = self.config.get("min_msg_count", 3)

        for user_id, last_time in list(self.last_chat_time.items()):
            if now_ts - last_time > timeout and self.unsaved_msg_count.get(user_id, 0) >= min_count:
                # 触发记忆归档
                await self._summarize_private_chat(user_id)
                self.unsaved_msg_count[user_id] = 0

        # 定期清理不活跃用户缓存，防止内存泄漏
        self._cleanup_inactive_users()

    async def _summarize_private_chat(self, user_id):
        """对私聊进行总结并存入长期记忆（按天分组处理）"""
        from itertools import groupby
        source_type = str(self.default_source_type or "private").strip() or "private"

        # 1. 获取未归档的原始消息
        loop = asyncio.get_event_loop()
        # 获取所有未归档消息，不设限制
        raw_msgs = await loop.run_in_executor(self.executor, lambda: self.db.get_unarchived_raw(user_id, limit=None))
        if not raw_msgs:
            return

        # 按时间正序排列（数据库返回的是倒序）
        raw_msgs.reverse()

        # 计算回溯截止时间
        max_days = self.config.get("max_history_days", 0)
        cutoff_date = None
        if max_days > 0:
            cutoff_date = (datetime.datetime.now() - datetime.timedelta(days=max_days)).date()

        # 按日期分组
        def get_date_key(m):
            timestamp = m.timestamp
            # 处理时间戳可能是整数或浮点数的情况
            if isinstance(timestamp, (int, float)):
                timestamp = datetime.datetime.fromtimestamp(timestamp)
            return timestamp.date()

        # 仅查询一次最近的记忆索引，构建新批次的链表
        last_index = await loop.run_in_executor(self.executor, self.db.get_last_memory_index, user_id)
        prev_index_id = last_index.index_id if last_index else None

        batch_add = {
            "ids": [],
            "documents": [],
            "metadatas": []
        }
        index_params_list = []
        archive_uuids_forced = []
        archive_uuids_summarized = []

        for date_key, group in groupby(raw_msgs, key=get_date_key):
            # 将 group 转为列表，因为 groupby 的迭代器只能用一次
            group_msgs = list(group)
            ref_uuids = [m.uuid for m in group_msgs]

            # 检查是否超过回溯天数限制
            if cutoff_date and date_key < cutoff_date:
                # 超过限制，直接标记为已归档，不进行总结
                archive_uuids_forced.extend(ref_uuids)
                continue

            summary_result = await self._process_single_summary_batch(user_id, group_msgs, date_key)
            if not summary_result:
                continue

            summary = summary_result.get("summary")
            if not summary:
                if summary_result.get("archive", False):
                    archive_uuids_forced.extend(summary_result.get("ref_uuids", ref_uuids))
                continue

            created_at = summary_result["created_at"]
            ref_uuids = summary_result["ref_uuids"]

            index_id = str(uuid.uuid4())
            ai_name = str(self.config.get("ai_name") or "").strip()
            batch_add["ids"].append(index_id)
            batch_add["documents"].append(summary)
            batch_add["metadatas"].append({
                "user_id": user_id,
                "source_type": source_type,
                "created_at": created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "ai_name": ai_name
            })

            index_params_list.append({
                "index_id": index_id,
                "summary": summary,
                "ref_uuids": json.dumps(ref_uuids),
                "prev_index_id": prev_index_id,
                "source_type": source_type,
                "user_id": user_id,
                "created_at": created_at
            })
            prev_index_id = index_id
            archive_uuids_summarized.extend(ref_uuids)

        # 先归档无需总结的消息
        if archive_uuids_forced:
            await loop.run_in_executor(self.executor, self.db.mark_as_archived, archive_uuids_forced)

        if not batch_add["ids"]:
            return

        # 先落库（SQLite）再尝试向量写入，避免 embedding 问题导致总结丢失
        for index_params in index_params_list:
            await loop.run_in_executor(self.executor, lambda p=index_params: self.db.save_memory_index(**p))
            self._record_memory_event(
                summary=index_params.get("summary"),
                user_id=index_params.get("user_id"),
                source_type=index_params.get("source_type"),
            )

        if index_params_list:
            self.add_activity(
                title=f"私聊归档完成 {len(index_params_list)} 条",
                category="task",
                source=source_type,
                meta={"user_id": str(user_id)},
            )

        # 归档已总结的消息
        if archive_uuids_summarized:
            await loop.run_in_executor(self.executor, self.db.mark_as_archived, archive_uuids_summarized)

        # 最后写入向量库；失败时记录待补偿任务，不影响主链路成功
        max_retries = 3
        retry_delay = 2
        vector_write_ok = False
        vector_fail_reason = ""

        for attempt in range(1, max_retries + 1):
            try:
                await self._ensure_chroma_initialized()
                added = await self._collection_add_texts(
                    ids=batch_add["ids"],
                    documents=batch_add["documents"],
                    metadatas=batch_add["metadatas"]
                )
                if added:
                    vector_write_ok = True
                    logger.info(
                        "Engram：已为用户 %s 批量写入 %d 条记忆向量",
                        user_id,
                        len(batch_add["ids"])
                    )
                else:
                    vector_fail_reason = "embedding_unavailable_or_dimension_mismatch"
                break
            except Exception as e:
                vector_fail_reason = str(e)
                if attempt >= max_retries:
                    logger.error(f"Engram：记忆向量写入失败（已落库，待补偿）：{e}")
                    break
                logger.warning(
                    "Engram：向量批量写入失败（第 %d/%d 次），%ss 后重试：%s",
                    attempt,
                    max_retries,
                    retry_delay,
                    e
                )
                await asyncio.sleep(retry_delay)
                retry_delay *= 2

        if vector_write_ok:
            self._clear_pending_vector_jobs(batch_add["ids"])
        else:
            pending_rows = []
            for idx, index_params in zip(batch_add["ids"], index_params_list):
                created_at = index_params.get("created_at")
                created_str = (
                    created_at.strftime("%Y-%m-%d %H:%M:%S")
                    if hasattr(created_at, "strftime")
                    else str(created_at)
                )
                pending_rows.append({
                    "index_id": idx,
                    "user_id": user_id,
                    "source_type": index_params.get("source_type", "private"),
                    "created_at": created_str,
                })

            self._enqueue_pending_vector_jobs(pending_rows, reason=vector_fail_reason)
            logger.warning(
                "Engram：用户 %s 的 %d 条总结已落库，但向量写入失败，已加入待补偿队列（当前队列=%d）",
                user_id,
                len(pending_rows),
                len(self._pending_vector_jobs)
            )

    async def _process_single_summary_batch(self, user_id, raw_msgs, date_key):
        """处理单批次（单日）消息的总结"""
        # 使用公共过滤方法
        filtered_msgs = [m for m in raw_msgs if self._is_valid_message_content(m.content)]

        loop = asyncio.get_event_loop()

        if not filtered_msgs:
            # 如果没有符合条件的消息，也标记原本的所有消息为已归档
            ref_uuids = [m.uuid for m in raw_msgs]
            return {
                "summary": None,
                "created_at": None,
                "ref_uuids": ref_uuids,
                "archive": True
            }

        # 构造对话文本
        chat_lines = [f"【日期：{date_key.strftime('%Y-%m-%d')}】"]
        ai_name = str(self.config.get("ai_name") or "").strip()
        for m in filtered_msgs:
            # 确保时间戳是 datetime 对象
            ts = self._ensure_datetime(m.timestamp)
            time_str = ts.strftime("%H:%M")
            if m.role == "user":
                name = m.user_name if m.user_name else "user"
            elif m.role == "assistant":
                name = m.user_name if m.user_name else ai_name
            else:
                name = m.role
            chat_lines.append(f"[{time_str}] {name}: {m.content}")
        chat_text = "\n".join(chat_lines)

        # 2. 调用 LLM 总结
        # 从配置获取提示词模板并替换占位符
        custom_prompt = self.config.get("summarize_prompt")
        ai_name = str(self.config.get("ai_name") or "").strip()
        prompt = custom_prompt.replace("{{chat_text}}", chat_text).replace("{{ai_name}}", ai_name)

        max_retries = 3
        retry_delay = 2
        full_content = ""

        for attempt in range(max_retries):
            try:
                # 获取指定的模型或默认模型
                summarize_model = self.config.get("summarize_model", "").strip()
                if summarize_model:
                    provider = self.context.get_provider_by_id(summarize_model)
                    if not provider:
                        provider = self.context.get_using_provider()
                else:
                    provider = self.context.get_using_provider()

                if not provider:
                    break

                resp = await provider.text_chat(prompt=prompt)
                full_content = resp.completion_text

                if full_content and len(full_content) >= 5:
                    break # 成功获取总结

                logger.warning(f"Engram：第 {attempt + 1} 次总结结果为空或过短。")
            except Exception as e:
                logger.error(f"Engram：第 {attempt + 1} 次总结失败：{e}")

            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay)

        if not full_content or len(full_content) < 5:
            logger.error(f"Engram：用户 {user_id} 在重试 {max_retries} 次后仍总结失败。")
            return None

        # 总结仅用于归档，不在此处做画像更新
        summary = full_content

        # 尝试解析结构化 JSON，生成可检索摘要
        try:
            content = full_content.strip()
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "{" in content and "}" in content:
                content = content[content.find("{"):content.rfind("}") + 1]

            payload = json.loads(content)
            if isinstance(payload, dict):
                summary = self._build_structured_summary(payload)
        except Exception as e:
            logger.debug(f"Engram：结构化总结解析失败，已回退原始文本：{e}")

        ref_uuids = [m.uuid for m in raw_msgs]
        created_at = self._ensure_datetime(raw_msgs[-1].timestamp)

        return {
            "summary": summary,
            "created_at": created_at,
            "ref_uuids": ref_uuids,
            "archive": False
        }

    async def summarize_all_users(self):
        """强制归档所有用户的未归档消息"""
        loop = asyncio.get_event_loop()
        user_ids = await loop.run_in_executor(self.executor, self.db.get_all_user_ids)
        if not user_ids:
            return 0

        summarized = 0
        for uid in user_ids:
            if self._is_shutdown or getattr(self.executor, "_shutdown", False):
                logger.debug("Engram：全局归档因关闭信号已中止")
                break

            # 跳过空值或系统内置账号
            if uid is None:
                continue
            uid_str = str(uid).lower()
            if uid_str in {"system", "astrbot"}:
                continue

            try:
                await self._summarize_private_chat(uid)
                summarized += 1
            except Exception as e:
                logger.error(f"Engram：强制归档失败，uid={uid}：{e}")
        return summarized

    async def _fold_summaries(
        self,
        user_id,
        *,
        days,
        min_samples,
        source_types,
        output_source_type,
        prompt_template,
        model_config_key,
        level_label
    ):
        """通用折叠逻辑：将 lower-level 摘要折叠为 higher-level 摘要。"""
        loop = asyncio.get_event_loop()

        try:
            days = max(1, int(days))
        except (TypeError, ValueError):
            days = 7

        try:
            min_samples = max(1, int(min_samples))
        except (TypeError, ValueError):
            min_samples = 3

        # 依次按 source_type 拉取候选
        selected = []
        for source_type in source_types:
            selected = await loop.run_in_executor(
                self.executor,
                self.db.get_summaries_by_type,
                user_id,
                source_type,
                days
            )
            if selected:
                break

        # 兜底：若时间窗口内为空，按最近记忆列表回溯
        if not selected:
            recent = await loop.run_in_executor(self.executor, self.db.get_memory_list, user_id, 300)
            allowed_types = set(source_types)
            selected = [
                item for item in recent
                if getattr(item, "source_type", "") in allowed_types
            ]

        if len(selected) < min_samples:
            return None

        # db 返回倒序，这里改为时间正序，便于 LLM 归纳事件脉络
        selected = list(reversed(selected))

        memory_texts = "\n".join([
            f"- [{self._ensure_datetime(item.created_at).strftime('%Y-%m-%d %H:%M:%S')}] {item.summary}"
            for item in selected
        ])

        prompt = str(prompt_template).replace("{{memory_texts}}", memory_texts)

        max_retries = 3
        retry_delay = 2
        summary_text = ""

        for attempt in range(max_retries):
            try:
                preferred_model = str(self.config.get(model_config_key, "")).strip()
                summarize_model = str(self.config.get("summarize_model", "")).strip()

                provider = None
                if preferred_model:
                    provider = self.context.get_provider_by_id(preferred_model)
                if not provider and summarize_model:
                    provider = self.context.get_provider_by_id(summarize_model)
                if not provider:
                    provider = self.context.get_using_provider()

                if not provider:
                    break

                resp = await provider.text_chat(prompt=prompt)
                summary_text = (resp.completion_text or "").strip()
                if len(summary_text) >= 5:
                    break

                logger.warning(
                    "Engram：%s 折叠第 %d 次结果为空或过短",
                    level_label,
                    attempt + 1
                )
            except Exception as e:
                logger.error(f"Engram：{level_label} 折叠第 {attempt + 1} 次失败：{e}")

            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay)
                retry_delay *= 2

        if not summary_text or len(summary_text) < 5:
            logger.error(f"Engram：用户 {user_id} 的 {level_label} 折叠在重试 {max_retries} 次后仍失败")
            return None

        created_at = datetime.datetime.now()
        index_id = str(uuid.uuid4())
        ai_name = str(self.config.get("ai_name") or "").strip()
        source_ids = [item.index_id for item in selected]

        # 维持时间链：新总结挂在该用户当前最新索引之后
        last_index = await loop.run_in_executor(self.executor, self.db.get_last_memory_index, user_id)
        prev_index_id = last_index.index_id if last_index else None

        add_params = {
            "ids": [index_id],
            "documents": [summary_text],
            "metadatas": [{
                "user_id": user_id,
                "source_type": output_source_type,
                "created_at": created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "ai_name": ai_name,
                "folding_days": days,
                "folding_level": output_source_type,
                "source_count": len(source_ids),
                "source_types": ",".join(source_types)
            }]
        }

        # 先落索引，向量失败不阻断折叠主流程
        scope_fields = self._derive_scope_fields(selected)
        index_params = {
            "index_id": index_id,
            "summary": summary_text,
            "ref_uuids": json.dumps(source_ids),
            "prev_index_id": prev_index_id,
            "source_type": output_source_type,
            "user_id": user_id,
            "group_id": scope_fields.get("group_id"),
            "member_id": scope_fields.get("member_id"),
            "created_at": created_at
        }
        await loop.run_in_executor(self.executor, lambda: self.db.save_memory_index(**index_params))
        self._record_memory_event(
            summary=index_params.get("summary"),
            user_id=index_params.get("user_id"),
            source_type=index_params.get("source_type"),
        )

        vector_write_ok = False
        vector_fail_reason = ""
        try:
            await self._ensure_chroma_initialized()
            added = await self._collection_add_texts(
                ids=add_params["ids"],
                documents=add_params["documents"],
                metadatas=add_params["metadatas"]
            )
            if added:
                vector_write_ok = True
                self._clear_pending_vector_jobs([index_id])
            else:
                vector_fail_reason = "embedding_unavailable_or_dimension_mismatch"
        except Exception as e:
            vector_fail_reason = str(e)

        if not vector_write_ok:
            self._enqueue_pending_vector_jobs([
                {
                    "index_id": index_id,
                    "user_id": user_id,
                    "source_type": output_source_type,
                    "created_at": created_at.strftime("%Y-%m-%d %H:%M:%S"),
                }
            ], reason=vector_fail_reason)
            logger.warning(
                "Engram：%s 折叠向量写入失败，索引已落库，已加入待补偿队列（index=%s）",
                level_label,
                index_id[:8],
            )

        logger.info(
            "Engram：已保存 %s 折叠结果，user=%s（来源=%d，index=%s）",
            level_label.capitalize(),
            user_id,
            len(source_ids),
            index_id[:8]
        )
        return summary_text

    async def fold_weekly_summaries(self, user_id, days=7):
        """将近 N 天的日级总结折叠为一条周总结，写入 SQLite + ChromaDB。"""
        min_samples = self.config.get("folding_min_samples", 3)
        prompt_template = self.config.get(
            "weekly_folding_prompt",
            "你是一名记忆整理助手。请根据下方【daily_summary 列表】生成一段周总结。\n\n"
            "要求：\n"
            "1. 只基于给定内容，不编造；\n"
            "2. 保留关键人物/地点/事件/数值；\n"
            "3. 语言简洁，120~220字。\n\n"
            "【daily_summary 列表】\n{{memory_texts}}"
        )
        return await self._fold_summaries(
            user_id,
            days=days,
            min_samples=min_samples,
            source_types=["daily_summary", "private"],
            output_source_type="weekly",
            prompt_template=prompt_template,
            model_config_key="weekly_folding_model",
            level_label="weekly"
        )

    async def fold_monthly_summaries(self, user_id, days=30):
        """将近 N 天的周级总结折叠为一条月总结，写入 SQLite + ChromaDB。"""
        min_samples = self.config.get("monthly_folding_min_samples", 4)
        prompt_template = self.config.get(
            "monthly_folding_prompt",
            "你是一名记忆整理助手。请根据下方【weekly_summary 列表】生成一段月总结。\n\n"
            "要求：\n"
            "1. 只基于给定内容，不编造；\n"
            "2. 保留关键人物/地点/事件/数值；\n"
            "3. 提炼本月主题、关键变化与持续偏好；\n"
            "4. 语言简洁，160~280字。\n\n"
            "【weekly_summary 列表】\n{{memory_texts}}"
        )
        return await self._fold_summaries(
            user_id,
            days=days,
            min_samples=min_samples,
            source_types=["weekly", "daily_summary", "private"],
            output_source_type="monthly",
            prompt_template=prompt_template,
            model_config_key="monthly_folding_model",
            level_label="monthly"
        )

    async def fold_yearly_summaries(self, user_id, days=365):
        """将近 N 天的月级总结折叠为一条年度总结，写入 SQLite + ChromaDB。"""
        min_samples = self.config.get("yearly_folding_min_samples", 6)
        prompt_template = self.config.get(
            "yearly_folding_prompt",
            "你是一名记忆整理助手。请根据下方【monthly_summary 列表】生成一段年度总结。\n\n"
            "要求：\n"
            "1. 只基于给定内容，不编造；\n"
            "2. 归纳全年主线、阶段变化与稳定偏好；\n"
            "3. 保留关键人物/地点/事件/数值；\n"
            "4. 语言简洁，220~420字。\n\n"
            "【monthly_summary 列表】\n{{memory_texts}}"
        )
        return await self._fold_summaries(
            user_id,
            days=days,
            min_samples=min_samples,
            source_types=["monthly", "weekly"],
            output_source_type="yearly",
            prompt_template=prompt_template,
            model_config_key="yearly_folding_model",
            level_label="yearly"
        )

    async def _retrieve_memories_by_keyword_fallback(
        self,
        user_id,
        query,
        limit,
        start_time=None,
        end_time=None,
        source_types=None,
    ):
        """向量不可用时的兜底检索：SQLite 关键词召回 + 本地重排。"""
        loop = asyncio.get_event_loop()

        candidate_limit = min(
            max(10, limit * 8),
            max(20, int(self.config.get("memory_query_max_results", 60)))
        )

        keyword_tokens = list(self._generate_query_keywords(query))
        if query and str(query).strip():
            keyword_tokens.append(str(query).strip())
        keyword_tokens = list(dict.fromkeys([k for k in keyword_tokens if k]))[:30]

        # 优先使用 DB 专用关键词检索接口；旧接口下回退到最近记忆列表过滤
        if hasattr(self.db, "search_memory_indexes_by_keywords"):
            candidates = await loop.run_in_executor(
                self.executor,
                self.db.search_memory_indexes_by_keywords,
                user_id,
                keyword_tokens,
                candidate_limit,
                start_time,
                end_time,
                source_types,
            )
        else:
            candidates = await loop.run_in_executor(self.executor, self.db.get_memory_list, user_id, candidate_limit)

        if not candidates:
            return []

        allowed_types = self._get_allowed_source_types()
        normalized_source_types = []
        if isinstance(source_types, (list, tuple, set)):
            normalized_source_types = [
                str(t).strip().lower() for t in source_types
                if str(t).strip().lower() in allowed_types
            ]

        filtered = []
        for item in candidates:
            created_at = self._ensure_datetime(item.created_at)
            if start_time and created_at < start_time:
                continue
            if end_time and created_at >= end_time:
                continue
            if normalized_source_types and str(getattr(item, "source_type", "")).lower() not in normalized_source_types:
                continue
            filtered.append(item)

        if not filtered:
            return []

        # 构造关键词文档频率用于轻量 IDF
        query_keywords = self._generate_query_keywords(query)
        keyword_doc_freq = {k: 0 for k in query_keywords}
        for item in filtered:
            summary = str(getattr(item, "summary", "") or "")
            summary_lower = summary.lower()
            summary_tokens_en = Counter(_ENGLISH_WORD_PATTERN.findall(summary_lower))

            summary_ngrams_zh = Counter()
            for block in _CHINESE_BLOCK_PATTERN.findall(summary):
                block_len = len(block)
                for n in (2, 3, 4):
                    if block_len < n:
                        continue
                    for i in range(0, block_len - n + 1):
                        summary_ngrams_zh[block[i:i + n]] += 1

            for kw in query_keywords:
                if self._count_keyword_matches(kw, summary_tokens_en, summary_ngrams_zh) > 0:
                    keyword_doc_freq[kw] += 1

        corpus_stats = {
            "total_docs": len(filtered),
            "keyword_doc_freq": keyword_doc_freq,
        }

        rescored = []
        for item in filtered:
            summary = str(getattr(item, "summary", "") or "")
            keyword_score, _ = self._calc_keyword_score(query, summary, corpus_stats)
            recency_ts = self._ensure_datetime(item.created_at).timestamp() if getattr(item, "created_at", None) else 0
            rescored.append({
                "item": item,
                "keyword_score": keyword_score,
                "recency_ts": recency_ts,
            })

        rescored.sort(key=lambda x: (x["keyword_score"], x["recency_ts"]), reverse=True)
        selected = [x["item"] for x in rescored[:limit]]

        show_relevance_score = self.config.get("show_relevance_score", True)
        enable_context_hint = bool(self.config.get("enable_memory_context_hint", True))
        try:
            memory_context_window = int(self.config.get("memory_context_window", 2))
        except (TypeError, ValueError):
            memory_context_window = 2
        memory_context_window = max(0, min(10, memory_context_window))

        db_indices = {item.index_id: item for item in selected}

        # 向前追溯链路上下文
        prev_index_map = {}
        if enable_context_hint and memory_context_window > 0:
            pending_prev_ids = {
                db_indices[idx].prev_index_id
                for idx in db_indices
                if db_indices[idx].prev_index_id
            }
            for _ in range(memory_context_window):
                if not pending_prev_ids:
                    break

                if hasattr(self.db, "get_prev_indices_by_ids"):
                    fetched_prev = await loop.run_in_executor(
                        self.executor,
                        self.db.get_prev_indices_by_ids,
                        list(pending_prev_ids)
                    )
                else:
                    fetched_prev = await loop.run_in_executor(
                        self.executor,
                        self.db.get_memory_indexes_by_ids,
                        list(pending_prev_ids)
                    )

                if not fetched_prev:
                    break
                prev_index_map.update(fetched_prev)
                pending_prev_ids = {
                    it.prev_index_id
                    for it in fetched_prev.values()
                    if it.prev_index_id and it.prev_index_id not in prev_index_map
                }

        # 批量读取原文
        index_uuid_map = {}
        for idx, db_index in db_indices.items():
            if not db_index.ref_uuids:
                continue
            try:
                uuids = json.loads(db_index.ref_uuids)
            except (TypeError, ValueError):
                uuids = []
            if uuids:
                index_uuid_map[idx] = uuids

        raw_map = {}
        if index_uuid_map:
            if hasattr(self.db, "get_raw_memories_map_by_uuid_lists"):
                raw_map = await loop.run_in_executor(
                    self.executor,
                    self.db.get_raw_memories_map_by_uuid_lists,
                    index_uuid_map
                )
            else:
                def _legacy_build_raw_map():
                    _result = {}
                    for _idx, _uuids in index_uuid_map.items():
                        _result[_idx] = self.db.get_memories_by_uuids(_uuids)
                    return _result
                raw_map = await loop.run_in_executor(self.executor, _legacy_build_raw_map)

        best_score = max((x["keyword_score"] for x in rescored[:limit]), default=0.0)
        all_memories = []
        for row in rescored[:limit]:
            item = row["item"]
            idx = item.index_id
            summary = item.summary
            created_at = self._ensure_datetime(item.created_at).strftime("%Y-%m-%d %H:%M:%S")
            short_id = idx[:8]

            if show_relevance_score and best_score > 0:
                relevance_percent = max(1, min(100, int(row["keyword_score"] / best_score * 100)))
                relevance_badge = f"🎯 {relevance_percent}% | "
            else:
                relevance_badge = ""

            context_hint = ""
            if enable_context_hint and memory_context_window > 0 and item.prev_index_id:
                timeline_snippets = []
                prev_id = item.prev_index_id
                step = 0
                while prev_id and step < memory_context_window:
                    prev_item = prev_index_map.get(prev_id)
                    if not prev_item:
                        break
                    timeline_snippets.append(prev_item.summary[:24].replace("\n", " "))
                    prev_id = prev_item.prev_index_id
                    step += 1
                if timeline_snippets:
                    timeline_text = " ⟶ ".join(timeline_snippets)
                    if len(timeline_text) > 80:
                        timeline_text = timeline_text[:77] + "..."
                    context_hint = f"\n   └ ⏪ 前情时间线：{timeline_text}"

            raw_preview = ""
            raw_msgs = raw_map.get(idx, [])
            filtered_raw = [
                m.content[:50] for m in raw_msgs
                if self._is_valid_message_content(m.content)
            ][:1]
            if filtered_raw:
                raw_preview = f"\n   └ 📄 相关原文：{filtered_raw[0]}"

            all_memories.append(
                f"{relevance_badge}🆔 {short_id} | ⏰ {created_at}\n"
                f"📝 归档：{summary}{context_hint}{raw_preview}"
            )

        # 关键词兜底命中时同样增强 active_score
        reinforce_bonus = self.config.get("memory_reinforce_bonus", 20)
        if all_memories and reinforce_bonus > 0:
            for row in rescored[:limit]:
                try:
                    await loop.run_in_executor(
                        self.executor,
                        self.db.update_active_score,
                        row["item"].index_id,
                        reinforce_bonus
                    )
                except Exception as e:
                    logger.debug(f"Engram：fallback 增强记忆 {row['item'].index_id[:8]} 活跃度失败：{e}")

        return all_memories

    # ========== 记忆检索 ==========

    async def retrieve_memories(
        self,
        user_id,
        query,
        limit=None,
        start_time=None,
        end_time=None,
        source_types=None,
        force_retrieve: bool = False,
    ):
        """检索相关记忆并返回原文摘要及背景（基于时间链），支持 RRF/混合策略排序和时间/类型过滤。"""
        # 确保 ChromaDB 已初始化
        await self._ensure_chroma_initialized()

        loop = asyncio.get_event_loop()

        # limit 统一归一：默认读取配置 max_recent_memories
        try:
            configured_limit = int(self.config.get("max_recent_memories", 3))
        except (TypeError, ValueError):
            configured_limit = 3
        configured_limit = max(1, min(50, configured_limit))

        try:
            request_limit = int(limit) if limit is not None else configured_limit
        except (TypeError, ValueError):
            request_limit = configured_limit
        limit = max(1, min(50, request_limit))

        # 查询分类：动态阈值与权重调整
        # 兼容兜底：防止旧版 IntentClassifier 缺失 classify_query 导致崩溃
        intent_type, intent_score = "recall", 0.0
        if hasattr(self._intent_classifier, "classify_query"):
            try:
                intent_type, intent_score = self._intent_classifier.classify_query(query)
            except Exception as e:
                logger.warning(f"Engram：classify_query 调用失败（{e}），已回退默认意图")
                intent_type, intent_score = "recall", 0.0
        else:
            logger.warning("Engram：IntentClassifier 缺少 classify_query，已回退 should_retrieve_memory")
            if hasattr(self._intent_classifier, "should_retrieve_memory"):
                try:
                    should_retrieve = await self._intent_classifier.should_retrieve_memory(query)
                    if not should_retrieve:
                        logger.debug("Engram：should_retrieve_memory=False，已跳过检索")
                        return []
                except Exception as e:
                    logger.warning(f"Engram：should_retrieve_memory 回退调用失败（{e}），继续检索")

        if intent_type == "skip" and not force_retrieve:
            logger.debug("Engram：查询被判定为 skip，已跳过检索")
            return []
        if intent_type == "skip" and force_retrieve:
            logger.debug("Engram：查询被判定为 skip，但 force_retrieve=True，继续检索")

        # 构造 where 过滤：用户维度 + 可选来源类型
        # Chroma 复杂过滤统一走 $and，避免"字段 + $or"混写兼容性问题
        allowed_types = self._get_allowed_source_types()
        normalized_source_types = []
        if isinstance(source_types, (list, tuple, set)):
            for item in source_types:
                token = str(item or "").strip().lower()
                if token in allowed_types and token not in normalized_source_types:
                    normalized_source_types.append(token)
        elif isinstance(source_types, str) and source_types.strip():
            token = source_types.strip().lower()
            if token in allowed_types:
                normalized_source_types = [token]

        where_clauses = [{"user_id": user_id}]
        if len(normalized_source_types) == 1:
            where_clauses.append({"source_type": normalized_source_types[0]})
        elif len(normalized_source_types) > 1:
            where_clauses.append({"$or": [{"source_type": t} for t in normalized_source_types]})

        where_filter = where_clauses[0] if len(where_clauses) == 1 else {"$and": where_clauses}

        # 1. ChromaDB 检索（多取一些结果以便过滤和重排序后仍有足够数据）
        try:
            results = await self._collection_query_text(
                query=query,
                n_results=min(
                    limit * 6,
                    max(10, int(self.config.get("memory_query_max_results", 60)))
                ),
                where=where_filter
            )
        except Exception as e:
            logger.warning(f"Engram：记忆查询异常，已回退关键词检索：{e}")
            return await self._retrieve_memories_by_keyword_fallback(
                user_id=user_id,
                query=query,
                limit=limit,
                start_time=start_time,
                end_time=end_time,
                source_types=normalized_source_types,
            )

        if not results or not results.get('ids') or not results['ids'] or not results['ids'][0]:
            logger.debug("Engram：向量检索结果为空，已回退关键词检索")
            return await self._retrieve_memories_by_keyword_fallback(
                user_id=user_id,
                query=query,
                limit=limit,
                start_time=start_time,
                end_time=end_time,
                source_types=normalized_source_types,
            )

        # 获取配置
        similarity_threshold = float(self.config.get("memory_similarity_threshold", 1.5))
        show_relevance_score = self.config.get("show_relevance_score", True)
        enable_keyword_boost = self.config.get("enable_keyword_boost", True)
        enable_memory_decay = self.config.get("enable_memory_decay", True)
        enable_context_hint = bool(self.config.get("enable_memory_context_hint", True))
        try:
            memory_context_window = int(self.config.get("memory_context_window", 2))
        except (TypeError, ValueError):
            memory_context_window = 2
        memory_context_window = max(0, min(10, memory_context_window))

        rank_strategy = str(self.config.get("rank_strategy", "rrf")).lower()
        if rank_strategy not in {"rrf", "hybrid"}:
            rank_strategy = "rrf"

        # 解析关键词权重（新格式直接是数值字符串 "0.5"）
        weight_config = self.config.get("keyword_boost_weight", "0.5")
        try:
            keyword_boost_weight = float(weight_config)
        except (ValueError, TypeError):
            # 向后兼容旧格式 "均衡模式 (0.5)"
            match = re.search(r'\(([\d.]+)\)', str(weight_config))
            keyword_boost_weight = float(match.group(1)) if match else 0.5

        # 混合排序权重（默认近似现有行为：向量+关键词主导）
        weight_vector = float(self.config.get("rank_weight_vector", max(0.0, 1.0 - keyword_boost_weight)))
        weight_keyword = float(self.config.get("rank_weight_keyword", keyword_boost_weight))
        weight_recency = float(self.config.get("rank_weight_recency", 0.08))
        weight_activity = float(self.config.get("rank_weight_activity", 0.06))

        # 动态阈值与权重调整（按查询类别）
        if intent_type == "recall":
            similarity_threshold *= 1.15  # 放宽阈值
            weight_vector = max(weight_vector, 0.55)
            weight_keyword = min(weight_keyword, 0.35)
            weight_recency = max(weight_recency, 0.1)
        elif intent_type == "preference_fact":
            similarity_threshold *= 0.85  # 收紧阈值
            weight_keyword = max(weight_keyword, 0.65)
            weight_vector = min(weight_vector, 0.3)
            weight_recency = min(weight_recency, 0.05)
        elif intent_type == "event_narrative":
            similarity_threshold *= 0.95
            weight_vector = max(weight_vector, 0.35)
            weight_keyword = max(weight_keyword, 0.65)
            weight_recency = min(weight_recency, 0.05)

        # 仅在启用记忆衰减时保留 activity 强影响，否则降权，避免语义冲突
        if not enable_memory_decay:
            weight_activity *= 0.2

        if self.config.get("debug_injection", False):
            logger.info(
                "Engram：检索调优 intent=%s score=%s threshold=%.3f 权重(v=%.2f,k=%.2f,r=%.2f,a=%.2f)",
                intent_type,
                intent_score,
                similarity_threshold,
                weight_vector,
                weight_keyword,
                weight_recency,
                weight_activity
            )

        # 2. 预处理结果并计算关键词匹配度（BM25 风格）
        distances = results.get('distances', [[]])[0] if 'distances' in results else []
        metadatas = results.get('metadatas', [[]])[0] if 'metadatas' in results else []
        memory_data = []

        # 提取查询关键词
        # - legacy: 正则词切分
        # - ngram: 中英混合 n-gram（配置开关）
        enable_ngram_keyword_rank = bool(self.config.get("enable_ngram_keyword_rank", True))
        if enable_ngram_keyword_rank:
            query_keywords = {k.lower() for k in self._generate_query_keywords(query)}
        else:
            query_keywords = {k.lower() for k in re.split(r'[^\w]+', query) if k.strip()}

        # BM25 参数（legacy 模式）
        _bm25_k1 = 1.2
        _bm25_b = 0.75
        _avg_doc_len = 80  # 摘要的典型长度估计

        # ngram 模式使用轻量 corpus_stats（可逐步增强 df 统计）
        corpus_stats = {
            "total_docs": max(1, len(results.get('ids', [[]])[0] if results.get('ids') else [])),
            "keyword_doc_freq": {},
        }

        for i in range(len(results['ids'][0])):
            distance = distances[i] if distances and i < len(distances) else float('inf')

            # 过滤低相关性结果
            if distance > similarity_threshold:
                logger.debug(f"Engram：记忆距离 {distance:.3f} 超过阈值 {similarity_threshold}，已跳过")
                continue

            index_id = results['ids'][0][i]
            summary = results['documents'][0][i]
            metadata = metadatas[i] if i < len(metadatas) and metadatas[i] else {}

            if enable_ngram_keyword_rank:
                keyword_score, _ = self._calc_keyword_score(query, summary, corpus_stats)
            else:
                # BM25 风格关键词匹配：TF 饱和 + 文档长度归一化
                keyword_score = 0.0
                summary_lower = summary.lower()
                doc_len = max(1, len(summary_lower))

                for keyword in query_keywords:
                    if keyword in summary_lower:
                        tf = summary_lower.count(keyword)
                        # BM25 TF 饱和公式：高频词收益递减
                        norm_tf = (tf * (_bm25_k1 + 1)) / (tf + _bm25_k1 * (1 - _bm25_b + _bm25_b * doc_len / _avg_doc_len))
                        # 长关键词权重更高（近似 IDF），短词保底 1.0（中文单字词如"猫"也很重要）
                        keyword_weight = max(1.0, min(3.0, len(keyword) / 2.0))
                        keyword_score += norm_tf * keyword_weight

            memory_data.append({
                'index_id': index_id,
                'summary': summary,
                'metadata': metadata,
                'distance': distance,
                'keyword_score': keyword_score,
                'rank_score': 0.0,
                'display_score': 0.0
            })

        if not memory_data:
            return []

        # 批量查询索引信息（active_score, created_at）
        index_ids = [item['index_id'] for item in memory_data]
        index_map = await loop.run_in_executor(self.executor, self.db.get_memory_indexes_by_ids, index_ids)

        # 可选：按时间窗口过滤（基于 DB created_at，避免 metadata 时间格式误差）
        if start_time or end_time:
            filtered_by_time = []
            for item in memory_data:
                db_index = index_map.get(item['index_id'])
                created_dt = db_index.created_at if db_index else None
                if not created_dt:
                    continue
                if start_time and created_dt < start_time:
                    continue
                if end_time and created_dt >= end_time:
                    continue
                filtered_by_time.append(item)
            memory_data = filtered_by_time

            if not memory_data:
                return []

            # 过滤后重建索引映射
            index_ids = [item['index_id'] for item in memory_data]
            index_map = await loop.run_in_executor(self.executor, self.db.get_memory_indexes_by_ids, index_ids)

        # 30天半衰期：越近的记忆 recency 越高
        recency_half_life_days = float(self.config.get("rank_recency_half_life_days", 30))
        recency_half_life_days = max(1.0, recency_half_life_days)
        recency_lambda = 0.693 / (recency_half_life_days * 86400)

        active_scores = []
        keyword_scores = [item['keyword_score'] for item in memory_data]

        for item in memory_data:
            db_index = index_map.get(item['index_id'])
            created_dt = db_index.created_at if db_index else None
            active_score = float(db_index.active_score) if db_index else 100.0
            item['created_at_dt'] = created_dt
            item['active_score'] = active_score
            active_scores.append(active_score)

            # 向量分：由 distance 归一化
            item['vector_score'] = max(0.0, min(1.0, 1 - item['distance'] / max(similarity_threshold, 1e-6)))

            # 时间衰减分
            if created_dt:
                age_seconds = max(0.0, now_ts - created_dt.timestamp())
                item['recency_score'] = max(0.0, min(1.0, pow(2.718281828, -recency_lambda * age_seconds)))
            else:
                item['recency_score'] = 0.5

        # 归一化 keyword_score / active_score
        max_keyword = max(keyword_scores) if keyword_scores else 0.0
        min_active = min(active_scores) if active_scores else 0.0
        max_active = max(active_scores) if active_scores else 1.0
        active_range = max(max_active - min_active, 1e-6)

        for item in memory_data:
            item['keyword_score_norm'] = (item['keyword_score'] / max_keyword) if max_keyword > 0 else 0.0
            item['activity_score'] = (item['active_score'] - min_active) / active_range

        # 3. 排序策略：RRF（可回退）或 Hybrid（四路融合）
        rrf_k = 60
        use_keyword = enable_keyword_boost and query_keywords and len(memory_data) > 1

        if rank_strategy == "rrf":
            if use_keyword:
                vector_w = 1.0 - keyword_boost_weight
                keyword_w = keyword_boost_weight

                sorted_by_vector = sorted(range(len(memory_data)), key=lambda idx: memory_data[idx]['distance'])
                vector_rank = {idx: rank + 1 for rank, idx in enumerate(sorted_by_vector)}

                sorted_by_keyword = sorted(range(len(memory_data)), key=lambda idx: memory_data[idx]['keyword_score'], reverse=True)
                keyword_rank = {idx: rank + 1 for rank, idx in enumerate(sorted_by_keyword)}

                for i, data in enumerate(memory_data):
                    rrf_vector = vector_w / (rrf_k + vector_rank[i])
                    rrf_keyword = keyword_w / (rrf_k + keyword_rank[i])
                    data['rank_score'] = rrf_vector + rrf_keyword
                    data['display_score'] = data['rank_score']

                memory_data.sort(key=lambda x: x['rank_score'], reverse=True)
            else:
                for data in memory_data:
                    data['rank_score'] = data['vector_score']
                    data['display_score'] = data['rank_score']
                memory_data.sort(key=lambda x: x['distance'])
        else:
            total_w = weight_vector + weight_keyword + weight_recency + weight_activity
            if total_w <= 0:
                total_w = 1.0

            for data in memory_data:
                data['rank_score'] = (
                    weight_vector * data['vector_score'] +
                    weight_keyword * (data['keyword_score_norm'] if enable_keyword_boost else 0.0) +
                    weight_recency * data['recency_score'] +
                    weight_activity * data['activity_score']
                ) / total_w
                data['display_score'] = data['rank_score']

            memory_data.sort(key=lambda x: x['rank_score'], reverse=True)

        # 4. 只保留前 limit 条
        memory_data = memory_data[:limit]

        # retrieve_memories 的时间过滤不会改变记忆增强逻辑；仅缩小候选范围

        # 5. 批量拉取索引、前序链路、原文，避免循环内多次 run_in_executor
        index_ids = [item['index_id'] for item in memory_data]
        db_indices = {}
        prev_index_map = {}
        raw_map = {}

        if index_ids:
            db_indices = await loop.run_in_executor(self.executor, self.db.get_memory_indexes_by_ids, index_ids)

            # 按窗口宽度批量向前追溯上下文链路
            if enable_context_hint and memory_context_window > 0:
                pending_prev_ids = {
                    db_indices[idx].prev_index_id
                    for idx in index_ids
                    if idx in db_indices and db_indices[idx].prev_index_id
                }
                for _ in range(memory_context_window):
                    if not pending_prev_ids:
                        break

                    if hasattr(self.db, "get_prev_indices_by_ids"):
                        fetched_prev = await loop.run_in_executor(
                            self.executor,
                            self.db.get_prev_indices_by_ids,
                            list(pending_prev_ids)
                        )
                    else:
                        # 兼容旧版 DBManager：退化为通用批量索引查询
                        fetched_prev = await loop.run_in_executor(
                            self.executor,
                            self.db.get_memory_indexes_by_ids,
                            list(pending_prev_ids)
                        )

                    if not fetched_prev:
                        break
                    prev_index_map.update(fetched_prev)
                    pending_prev_ids = {
                        item.prev_index_id
                        for item in fetched_prev.values()
                        if item.prev_index_id and item.prev_index_id not in prev_index_map
                    }

            # 批量解析 ref_uuids 后，一次性获取所有原文
            index_uuid_map = {}
            for idx, db_index in db_indices.items():
                if not db_index.ref_uuids:
                    continue
                try:
                    uuids = json.loads(db_index.ref_uuids)
                except (TypeError, ValueError):
                    uuids = []
                if uuids:
                    index_uuid_map[idx] = uuids

            if index_uuid_map:
                if hasattr(self.db, "get_raw_memories_map_by_uuid_lists"):
                    raw_map = await loop.run_in_executor(
                        self.executor,
                        self.db.get_raw_memories_map_by_uuid_lists,
                        index_uuid_map
                    )
                else:
                    # 兼容旧版 DBManager：按每条索引兜底查询
                    def _legacy_build_raw_map():
                        _result = {}
                        for _idx, _uuids in index_uuid_map.items():
                            try:
                                _result[_idx] = self.db.get_memories_by_uuids(_uuids)
                            except Exception as e:
                                logger.debug(
                                    "Engram：旧版 get_memories_by_uuids 调用失败，index=%s，已回退为空原文映射：%s",
                                    str(_idx)[:8],
                                    e,
                                )
                                _result[_idx] = []
                        return _result

                    raw_map = await loop.run_in_executor(self.executor, _legacy_build_raw_map)

        # 6. 构造带时间线背景和评分的记忆文本
        all_memories = []

        for data in memory_data:
            index_id = data['index_id']
            summary = data['summary']
            metadata = data['metadata']
            distance = data['distance']
            created_at = metadata.get("created_at", "未知时间")

            if rank_strategy == "rrf" and use_keyword and memory_data:
                quality_factor = max(0.0, 1.5 - distance) / 1.5
                best_score = memory_data[0].get('display_score', 1e-9)
                raw_percent = data.get('display_score', 0) / max(best_score, 1e-9) * 100
                relevance_percent = max(0, min(100, int(raw_percent * quality_factor)))
            else:
                relevance_percent = max(0, min(100, int(data.get('display_score', 0) * 100)))

            # 尝试通过链表获取"前情提要"（可配置开关）
            context_hint = ""
            db_index = db_indices.get(index_id)
            if enable_context_hint and memory_context_window > 0 and db_index and db_index.prev_index_id:
                timeline_snippets = []
                prev_id = db_index.prev_index_id
                step = 0
                while prev_id and step < memory_context_window:
                    prev_item = prev_index_map.get(prev_id)
                    if not prev_item:
                        break
                    timeline_snippets.append(prev_item.summary[:24].replace("\n", " "))
                    prev_id = prev_item.prev_index_id
                    step += 1

                if timeline_snippets:
                    timeline_text = " ⟶ ".join(timeline_snippets)
                    if len(timeline_text) > 80:
                        timeline_text = timeline_text[:77] + "..."
                    context_hint = f"\n   └ ⏪ 前情时间线：{timeline_text}"

            # 获取原文预览（控制长度，避免提示词膨胀）
            raw_preview = ""
            raw_msgs = raw_map.get(index_id, [])
            filtered_raw = [
                m.content[:50] for m in raw_msgs
                if self._is_valid_message_content(m.content)
            ][:1]
            if filtered_raw:
                raw_preview = f"\n   └ 📄 相关原文：{filtered_raw[0]}"

            # 添加 ID 信息（UUID 前 8 位）和相关性评分
            short_id = index_id[:8]

            # 根据配置决定是否显示相关性评分
            if show_relevance_score:
                relevance_badge = f"🎯 {relevance_percent}% | "
            else:
                relevance_badge = ""

            all_memories.append(f"{relevance_badge}🆔 {short_id} | ⏰ {created_at}\n📝 归档：{summary}{context_hint}{raw_preview}")

        # 7. Reinforce：被成功召回的记忆增强 active_score
        reinforce_bonus = self.config.get("memory_reinforce_bonus", 20)
        if all_memories and reinforce_bonus > 0:
            for data in memory_data:
                try:
                    await loop.run_in_executor(
                        self.executor,
                        self.db.update_active_score,
                        data['index_id'],
                        reinforce_bonus
                    )
                except Exception as e:
                    logger.debug(f"Engram：增强记忆 {data['index_id'][:8]} 活跃度失败：{e}")

        return all_memories

    async def get_memory_detail(self, user_id, sequence_num):
        """获取指定序号记忆的完整原文详情"""
        loop = asyncio.get_event_loop()

        # 1. 获取最近的 N 条记忆（为了找到对应的序号）
        # 假设用户输入的序号是基于 mem_list 的（最新的为 1）
        limit = sequence_num + 2
        memories = await loop.run_in_executor(self.executor, self.db.get_memory_list, user_id, limit)

        if not memories or len(memories) < sequence_num:
            return None, "找不到该序号的记忆，请确认序号是否存在。"

        # 2. 锁定目标记忆
        target_memory = memories[sequence_num - 1]

        # 3. 解析原文 UUID
        if not target_memory.ref_uuids:
            return target_memory, []

        uuids = json.loads(target_memory.ref_uuids)
        raw_msgs = await loop.run_in_executor(self.executor, self.db.get_memories_by_uuids, uuids)

        return target_memory, raw_msgs

    async def _find_memory_by_short_id(self, user_id, short_id):
        """按短 ID（8位）或完整 ID 查询记忆索引。"""
        loop = asyncio.get_event_loop()

        def _find_memory():
            with self.db.db.connection_context():
                MemoryIndex = self.db.MemoryIndex
                if len(short_id) == 8:
                    query = MemoryIndex.select().where(
                        (MemoryIndex.user_id == user_id) &
                        (MemoryIndex.index_id.startswith(short_id))
                    )
                else:
                    query = MemoryIndex.select().where(
                        (MemoryIndex.user_id == user_id) &
                        (MemoryIndex.index_id == short_id)
                    )
                return query.first()

        return await loop.run_in_executor(self.executor, _find_memory)

    async def get_memory_detail_by_id(self, user_id, short_id):
        """
        根据记忆 ID（短 ID 或完整 UUID）获取记忆详情

        Args:
            user_id: 用户ID
            short_id: 记忆ID（可以是前8位短ID或完整UUID）

        Returns:
            (memory_index, raw_msgs) 或 (None, error_message)
        """
        loop = asyncio.get_event_loop()

        target_memory = await self._find_memory_by_short_id(user_id, short_id)

        if not target_memory:
            return None, f"找不到 ID 为 {short_id} 的记忆，请确认 ID 是否正确。"

        # 2. 解析原文 UUID
        if not target_memory.ref_uuids:
            return target_memory, []

        uuids = json.loads(target_memory.ref_uuids)
        raw_msgs = await loop.run_in_executor(self.executor, self.db.get_memories_by_uuids, uuids)

        return target_memory, raw_msgs

    # ========== 记忆删除与撤销 ==========

    async def _delete_memory_entry(self, user_id, target_memory, delete_raw=False):
        """删除单条记忆索引（统一序号/ID 两种入口），并写入撤销历史。"""
        loop = asyncio.get_event_loop()
        index_id = target_memory.index_id
        summary = target_memory.summary

        try:
            # 确保 ChromaDB 已初始化
            await self._ensure_chroma_initialized()

            # 保存删除前的数据（用于撤销）
            deleted_uuids = json.loads(target_memory.ref_uuids) if target_memory.ref_uuids else []

            # 获取向量数据（用于恢复）
            vector_data = None
            try:
                chroma_result = await loop.run_in_executor(
                    self.executor,
                    lambda: self.collection.get(ids=[index_id], include=['embeddings', 'metadatas', 'documents'])
                )
                if chroma_result and chroma_result['ids']:
                    vector_data = {
                        'embedding': chroma_result['embeddings'][0] if chroma_result.get('embeddings') else None,
                        'metadata': chroma_result['metadatas'][0] if chroma_result.get('metadatas') else {},
                        'document': chroma_result['documents'][0] if chroma_result.get('documents') else summary
                    }
            except Exception as e:
                logger.debug(f"Engram：获取备份向量数据失败：{e}")

            # 创建删除记录
            delete_record = {
                'index_id': index_id,
                'summary': summary,
                'ref_uuids': target_memory.ref_uuids,
                'prev_index_id': target_memory.prev_index_id,
                'source_type': target_memory.source_type,
                'user_id': user_id,
                'created_at': target_memory.created_at,
                'active_score': target_memory.active_score,
                'delete_raw': delete_raw,
                'deleted_uuids': deleted_uuids,
                'vector_data': vector_data
            }

            # 保存到删除历史
            if user_id not in self._delete_history:
                self._delete_history[user_id] = []
            self._delete_history[user_id].insert(0, delete_record)
            self._delete_history[user_id] = self._delete_history[user_id][:self._max_undo_history]

            # 1. 从 ChromaDB 删除向量数据
            await loop.run_in_executor(self.executor, lambda: self.collection.delete(ids=[index_id]))

            # 2. 如果需要，删除关联的原始消息
            if delete_raw and target_memory.ref_uuids:
                uuids = json.loads(target_memory.ref_uuids)
                await loop.run_in_executor(self.executor, self.db.delete_raw_memories_by_uuids, uuids)
            else:
                # 不删除原始消息时，将其标记为未归档，以便重新总结
                if deleted_uuids:
                    def _mark_unarchived():
                        RawMemory = self.db.RawMemory
                        with self.db.db.connection_context():
                            RawMemory.update(is_archived=False).where(RawMemory.uuid << deleted_uuids).execute()
                    await loop.run_in_executor(self.executor, _mark_unarchived)

            # 3. 从 SQLite 删除记忆索引
            await loop.run_in_executor(self.executor, self.db.delete_memory_index, index_id)

            return True, "删除成功", summary
        except Exception as e:
            logger.error(f"Engram：删除记忆失败：{e}")
            return False, f"删除失败：{e}", summary

    async def delete_memory_by_sequence(self, user_id, sequence_num, delete_raw=False):
        """
        删除指定序号的记忆（支持撤销）

        Args:
            user_id: 用户ID
            sequence_num: 记忆序号（基于 mem_list 的序号，最新的为 1）
            delete_raw: 是否同时删除关联的原始消息

        Returns:
            (success: bool, message: str, summary: str)
        """
        loop = asyncio.get_event_loop()

        # 1. 获取目标记忆
        limit = sequence_num + 2
        memories = await loop.run_in_executor(self.executor, self.db.get_memory_list, user_id, limit)

        if not memories or len(memories) < sequence_num:
            return False, "找不到该序号的记忆，请确认序号是否存在。", ""

        target_memory = memories[sequence_num - 1]
        return await self._delete_memory_entry(user_id, target_memory, delete_raw=delete_raw)

    async def undo_last_delete(self, user_id):
        """
        撤销最近一次删除操作

        Args:
            user_id: 用户ID

        Returns:
            (success: bool, message: str, summary: str)
        """
        # 检查是否有删除历史
        if user_id not in self._delete_history or not self._delete_history[user_id]:
            return False, "没有可撤销的删除操作。", ""

        # 获取最近的删除记录
        delete_record = self._delete_history[user_id].pop(0)

        loop = asyncio.get_event_loop()

        try:
            # 1. 恢复 SQLite 中的记忆索引
            index_params = {
                'index_id': delete_record['index_id'],
                'summary': delete_record['summary'],
                'ref_uuids': delete_record['ref_uuids'],
                'prev_index_id': delete_record['prev_index_id'],
                'source_type': delete_record['source_type'],
                'user_id': delete_record['user_id'],
                'group_id': delete_record.get('group_id'),
                'member_id': delete_record.get('member_id'),
                'created_at': delete_record['created_at'],
                'active_score': delete_record.get('active_score', 100)
            }
            await loop.run_in_executor(self.executor, lambda: self.db.save_memory_index(**index_params))
            self._record_memory_event(
                summary=index_params.get("summary"),
                user_id=index_params.get("user_id"),
                source_type=index_params.get("source_type"),
            )

            # 确保 ChromaDB 已初始化
            await self._ensure_chroma_initialized()

            # 2. 恢复 ChromaDB 中的向量数据
            vector_data = delete_record.get('vector_data')
            if vector_data and vector_data.get('embedding'):
                # 有完整的向量数据，直接恢复
                add_params = {
                    'ids': [delete_record['index_id']],
                    'documents': [vector_data.get('document', delete_record['summary'])],
                    'metadatas': [vector_data.get('metadata', {'user_id': user_id})],
                    'embeddings': [vector_data['embedding']]
                }
                await loop.run_in_executor(self.executor, lambda: self.collection.add(**add_params))
            else:
                # 没有向量数据，重新生成
                add_params = {
                    'ids': [delete_record['index_id']],
                    'documents': [delete_record['summary']],
                    'metadatas': [{
                        'user_id': user_id,
                        'source_type': delete_record['source_type'],
                        'created_at': delete_record['created_at'].strftime("%Y-%m-%d %H:%M:%S") if hasattr(delete_record['created_at'], 'strftime') else str(delete_record['created_at'])
                    }]
                }
                added = await self._collection_add_texts(
                    ids=add_params["ids"],
                    documents=add_params["documents"],
                    metadatas=add_params["metadatas"]
                )
                if not added:
                    logger.warning("Engram：撤销操作已跳过向量恢复（embedding provider 不可用）")

            # 3. 恢复原始消息的归档状态
            if delete_record['deleted_uuids']:
                def _mark_archived():
                    RawMemory = self.db.RawMemory
                    with self.db.db.connection_context():
                        RawMemory.update(is_archived=True).where(
                            RawMemory.uuid << delete_record['deleted_uuids']
                        ).execute()
                try:
                    await loop.run_in_executor(self.executor, _mark_archived)
                except Exception as e:
                    logger.debug(f"Engram：恢复原始消息归档状态失败：{e}")

            return True, "撤销成功", delete_record['summary']

        except Exception as e:
            logger.error(f"Engram：撤销删除失败：{e}")
            # 恢复失败，将记录放回历史
            self._delete_history[user_id].insert(0, delete_record)
            return False, f"撤销失败：{e}", delete_record['summary']

    async def delete_memory_by_id(self, user_id, short_id, delete_raw=False):
        """
        根据记忆 ID（短 ID 或完整 UUID）删除记忆

        Args:
            user_id: 用户ID
            short_id: 记忆ID（可以是前8位短ID或完整UUID）
            delete_raw: 是否同时删除关联的原始消息

        Returns:
            (success: bool, message: str, summary: str)
        """
        short_id = str(short_id or "").strip()

        try:
            target_memory = await self._find_memory_by_short_id(user_id, short_id)
            if not target_memory:
                return False, f"找不到 ID 为 {short_id} 的记忆，请确认 ID 是否正确。", ""

            return await self._delete_memory_entry(user_id, target_memory, delete_raw=delete_raw)

        except Exception as e:
            logger.error(f"Engram：按 ID 删除记忆失败：{e}")
            return False, f"删除失败：{e}", ""

    async def rebuild_vector_collection(self, full_rebuild: bool = False, batch_size: int = 200):
        """重建向量库（从 SQLite 的 MemoryIndex 重新写入 ChromaDB）。"""
        await self._ensure_chroma_initialized()

        try:
            batch_size = max(1, int(batch_size))
        except (TypeError, ValueError):
            batch_size = 200

        loop = asyncio.get_event_loop()
        backup_dir = ""

        def _load_all_indexes():
            MemoryIndex = self.db.MemoryIndex
            rows = []
            with self.db.db.connection_context():
                query = MemoryIndex.select().order_by(MemoryIndex.created_at.asc())
                for item in query:
                    summary = str(item.summary or "").strip()
                    if not summary:
                        continue
                    created_at = self._ensure_datetime(item.created_at)
                    rows.append({
                        "index_id": item.index_id,
                        "summary": summary,
                        "user_id": item.user_id,
                        "group_id": getattr(item, "group_id", None),
                        "member_id": getattr(item, "member_id", None),
                        "source_type": item.source_type,
                        "created_at": created_at
                    })
            return rows

        if full_rebuild:
            def _backup_and_reset_collection():
                nonlocal backup_dir
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_dir = os.path.join(self.data_dir, f"engram_chroma_backup_{ts}")

                if os.path.isdir(self.chroma_path):
                    try:
                        shutil.copytree(self.chroma_path, backup_dir)
                    except FileExistsError:
                        pass
                    except Exception as e:
                        logger.warning(f"Engram：备份 ChromaDB 失败，将继续重建：{e}")
                        backup_dir = ""

                try:
                    self.chroma_client.delete_collection(name="long_term_memories")
                except Exception as e:
                    logger.debug(f"Engram：删除旧 Chroma 集合已跳过或失败，将继续重建：{e}")
                self.collection = self.chroma_client.get_or_create_collection(name="long_term_memories")

            await loop.run_in_executor(self.executor, _backup_and_reset_collection)

        all_rows = await loop.run_in_executor(self.executor, _load_all_indexes)
        if not all_rows:
            return {
                "success": True,
                "message": "没有可重建的记忆索引",
                "total": 0,
                "rebuilt": 0,
                "failed": 0,
                "full_rebuild": bool(full_rebuild),
                "backup_dir": backup_dir
            }

        rebuilt = 0
        failed = 0

        for i in range(0, len(all_rows), batch_size):
            batch = all_rows[i:i + batch_size]
            ids = [row["index_id"] for row in batch]
            documents = [row["summary"] for row in batch]
            metadatas = []

            for row in batch:
                created_at = row["created_at"]
                created_str = created_at.strftime("%Y-%m-%d %H:%M:%S") if hasattr(created_at, "strftime") else str(created_at)
                metadata = {
                    "user_id": row["user_id"],
                    "source_type": row["source_type"],
                    "created_at": created_str,
                    "ai_name": str(self.config.get("ai_name") or "").strip()
                }
                if row.get("group_id"):
                    metadata["group_id"] = row["group_id"]
                if row.get("member_id"):
                    metadata["member_id"] = row["member_id"]
                metadatas.append(metadata)

            try:
                ok = await self._collection_add_texts(ids=ids, documents=documents, metadatas=metadatas)
                if ok:
                    rebuilt += len(batch)
                    self._clear_pending_vector_jobs(ids)
                else:
                    failed += len(batch)
                    if failed == len(batch):
                        return {
                            "success": False,
                            "message": "重建中断：检测到向量维度不匹配或 embedding 不可用",
                            "total": len(all_rows),
                            "rebuilt": rebuilt,
                            "failed": failed,
                            "full_rebuild": bool(full_rebuild),
                            "backup_dir": backup_dir
                        }
            except Exception as e:
                logger.error(f"Engram：rebuild_vector_collection 批次失败：{e}")
                failed += len(batch)

        return {
            "success": failed == 0,
            "message": "重建完成" if failed == 0 else "重建完成（部分失败）",
            "total": len(all_rows),
            "rebuilt": rebuilt,
            "failed": failed,
            "full_rebuild": bool(full_rebuild),
            "backup_dir": backup_dir
        }

    # ========== 数据导出 ==========

    async def export_raw_messages(self, user_id, format="jsonl", start_date=None, end_date=None, limit=None):
        """
        导出原始消息数据用于模型微调

        Args:
            user_id: 用户ID
            format: 导出格式 (jsonl, json, txt)
            start_date: 开始日期
            end_date: 结束日期
            limit: 限制数量

        Returns:
            (success: bool, data: str, stats: dict)
        """
        loop = asyncio.get_event_loop()

        try:
            # 获取原始消息
            raw_msgs = await loop.run_in_executor(
                self.executor,
                self.db.get_all_raw_messages,
                user_id,
                start_date,
                end_date,
                limit
            )

            if not raw_msgs:
                return False, "没有找到可导出的消息", {}

            # 获取统计信息
            stats = await loop.run_in_executor(self.executor, self.db.get_message_stats, user_id)
            stats["exported"] = len(raw_msgs)

            # 根据格式导出
            if format == "jsonl":
                data = self._export_as_jsonl(raw_msgs)
            elif format == "json":
                data = self._export_as_json(raw_msgs)
            elif format == "txt":
                data = self._export_as_txt(raw_msgs)
            elif format == "alpaca":
                data = self._export_as_alpaca(raw_msgs)
            elif format == "sharegpt":
                data = self._export_as_sharegpt(raw_msgs)
            else:
                return False, f"不支持的导出格式：{format}", {}

            return True, data, stats

        except Exception as e:
            logger.error(f"Engram：导出原始消息失败：{e}")
            return False, f"导出失败：{e}", {}

    async def export_all_users_messages(self, format="jsonl", start_date=None, end_date=None, limit=None):
        """
        导出所有用户的原始消息数据

        Args:
            format: 导出格式 (jsonl, json, txt, alpaca, sharegpt)
            start_date: 开始日期
            end_date: 结束日期
            limit: 限制数量

        Returns:
            (success: bool, data: str, stats: dict)
        """
        loop = asyncio.get_event_loop()

        try:
            # 获取所有用户的消息
            raw_msgs = await loop.run_in_executor(
                self.executor,
                self.db.get_all_users_messages,
                start_date,
                end_date,
                limit
            )

            if not raw_msgs:
                return self._format_export_output(format, [], {})

            # 获取统计信息
            stats = await loop.run_in_executor(self.executor, self.db.get_all_users_stats)
            stats["exported"] = len(raw_msgs)

            # 根据格式导出
            if format == "jsonl":
                data = self._export_as_jsonl(raw_msgs)
            elif format == "json":
                data = self._export_as_json(raw_msgs)
            elif format == "txt":
                data = self._export_as_txt(raw_msgs)
            elif format == "alpaca":
                data = self._export_as_alpaca(raw_msgs)
            elif format == "sharegpt":
                data = self._export_as_sharegpt(raw_msgs)
            else:
                return False, f"不支持的导出格式：{format}", {}

            return True, data, stats

        except Exception as e:
            logger.error(f"Engram：导出全部用户消息失败：{e}")
            return False, f"导出失败：{e}", {}

    def _export_as_jsonl(self, raw_msgs):
        """导出为 JSONL 格式（每行一个 JSON 对象）"""
        lines = []
        for msg in raw_msgs:
            if not self._is_valid_message_content(msg.content):
                continue
            ts = self._ensure_datetime(msg.timestamp)
            obj = {
                "role": "assistant" if msg.role == "assistant" else "user",
                "content": msg.content,
                "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
                "user_id": msg.user_id,
                "user_name": msg.user_name
            }
            lines.append(json.dumps(obj, ensure_ascii=False))
        return "\n".join(lines)

    def _export_as_json(self, raw_msgs):
        """导出为 JSON 数组格式"""
        messages = []
        for msg in raw_msgs:
            if not self._is_valid_message_content(msg.content):
                continue
            ts = self._ensure_datetime(msg.timestamp)
            messages.append({
                "role": "assistant" if msg.role == "assistant" else "user",
                "content": msg.content,
                "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
                "user_id": msg.user_id,
                "user_name": msg.user_name
            })
        return json.dumps(messages, ensure_ascii=False, indent=2)

    def _export_as_txt(self, raw_msgs):
        """导出为纯文本格式"""
        lines = []
        for msg in raw_msgs:
            if not self._is_valid_message_content(msg.content):
                continue
            ts = self._ensure_datetime(msg.timestamp)
            role_name = "助手" if msg.role == "assistant" else (msg.user_name or "用户")
            time_str = ts.strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"[{time_str}] {role_name}: {msg.content}")
        return "\n".join(lines)

    def _export_as_alpaca(self, raw_msgs):
        """导出为 Alpaca 格式（用于微调）"""
        conversations = []
        current_instruction = None

        for msg in raw_msgs:
            if not self._is_valid_message_content(msg.content):
                continue

            if msg.role == "user":
                current_instruction = msg.content
            elif msg.role == "assistant" and current_instruction:
                conversations.append({
                    "instruction": current_instruction,
                    "input": "",
                    "output": msg.content
                })
                current_instruction = None

        return json.dumps(conversations, ensure_ascii=False, indent=2)

    def _export_as_sharegpt(self, raw_msgs):
        """导出为 ShareGPT 格式（用于微调）"""
        conversations = []
        current_conversation = []

        for msg in raw_msgs:
            if not self._is_valid_message_content(msg.content):
                continue

            role = "gpt" if msg.role == "assistant" else "human"
            current_conversation.append({
                "from": role,
                "value": msg.content
            })

            # 每个对话轮次（一问一答）作为一个完整对话
            if msg.role == "assistant" and len(current_conversation) >= 2:
                conversations.append({
                    "conversations": current_conversation.copy()
                })
                current_conversation = []

        return json.dumps(conversations, ensure_ascii=False, indent=2)
