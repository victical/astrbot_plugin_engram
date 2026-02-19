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

import chromadb
import os
import uuid
import json
import re
import asyncio
import time
import datetime
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from astrbot.api import logger

# 预编译正则表达式
_CHINESE_PATTERN = re.compile(r'[\u4e00-\u9fa5]')
_ENGLISH_WORD_PATTERN = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")
_CHINESE_BLOCK_PATTERN = re.compile(r"[\u4e00-\u9fa5]+")


class MemoryManager:
    """记忆管理器"""
    
    def __init__(self, context, config, data_dir, executor, db_manager, profile_manager=None):
        """
        初始化记忆管理器
        
        Args:
            context: AstrBot API 上下文对象
            config: 插件配置字典
            data_dir: 数据目录路径
            executor: ThreadPoolExecutor 实例
            db_manager: DatabaseManager 实例
            profile_manager: ProfileManager 实例（可选，用于实时画像更新）
        """
        self.context = context
        self.config = config
        self.data_dir = data_dir
        self.executor = executor
        self.db = db_manager
        self.profile_manager = profile_manager
        
        # ChromaDB 延迟初始化（避免构造函数阻塞）
        self.chroma_path = os.path.join(self.data_dir, "engram_chroma")
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
    
    def shutdown(self):
        """关闭记忆管理器"""
        self._is_shutdown = True
    
    # ========== ChromaDB 管理 ==========
    
    async def _ensure_chroma_initialized(self):
        """确保 ChromaDB 已初始化（延迟初始化，避免构造函数阻塞）"""
        if self._chroma_initialized:
            return
        
        async with self._chroma_init_lock:
            # 双重检查
            if self._chroma_initialized:
                return
            
            # 在线程池中初始化 ChromaDB（避免阻塞事件循环）
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
                logger.info("Engram: ChromaDB initialized successfully")
            except Exception as e:
                logger.error(f"Engram: Failed to initialize ChromaDB: {e}")
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

    # ========== 消息记录 ==========
    
    async def record_message(self, user_id, session_id, role, content, msg_type="text", user_name=None):
        """记录原始消息"""
        msg_uuid = str(uuid.uuid4())
        
        # 异步保存到 SQLite
        loop = asyncio.get_event_loop()
        params = {
            "uuid": msg_uuid,
            "session_id": session_id,
            "user_id": user_id,
            "user_name": user_name,
            "role": role,
            "content": content,
            "msg_type": msg_type,
            "timestamp": datetime.datetime.now()
        }
        await loop.run_in_executor(self.executor, lambda: self.db.save_raw_memory(**params))
        
        # 更新记录
        if role == "user":
            self.last_chat_time[user_id] = datetime.datetime.now().timestamp()
            self.unsaved_msg_count[user_id] = self.unsaved_msg_count.get(user_id, 0) + 1
    
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
            ai_name = self.config.get("ai_name", "助手")
            batch_add["ids"].append(index_id)
            batch_add["documents"].append(summary)
            batch_add["metadatas"].append({
                "user_id": user_id,
                "source_type": "private",
                "created_at": created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "ai_name": ai_name
            })

            index_params_list.append({
                "index_id": index_id,
                "summary": summary,
                "ref_uuids": json.dumps(ref_uuids),
                "prev_index_id": prev_index_id,
                "source_type": "private",
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

        max_retries = 3
        retry_delay = 2
        for attempt in range(1, max_retries + 1):
            try:
                # 确保 ChromaDB 已初始化
                await self._ensure_chroma_initialized()
                # 批量写入向量数据
                await loop.run_in_executor(self.executor, lambda: self.collection.add(**batch_add))
                logger.info(
                    "Engram: Batch add %d memories for user %s",
                    len(batch_add["ids"]),
                    user_id
                )
                break
            except Exception as e:
                if attempt >= max_retries:
                    logger.error(f"Save summarization error: {e}")
                    return
                logger.warning(
                    "Engram: Batch add failed (attempt %d/%d), retrying in %ss: %s",
                    attempt,
                    max_retries,
                    retry_delay,
                    e
                )
                await asyncio.sleep(retry_delay)
                retry_delay *= 2

        # 批量写入索引（逐条写入 SQLite）
        for index_params in index_params_list:
            await loop.run_in_executor(self.executor, lambda p=index_params: self.db.save_memory_index(**p))

        # 归档已总结的消息
        if archive_uuids_summarized:
            await loop.run_in_executor(self.executor, self.db.mark_as_archived, archive_uuids_summarized)
    
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
        for m in filtered_msgs:
            # 确保时间戳是 datetime 对象
            ts = self._ensure_datetime(m.timestamp)
            time_str = ts.strftime("%H:%M")
            name = m.user_name if m.role == "user" and m.user_name else m.role
            chat_lines.append(f"[{time_str}] {name}: {m.content}")
        chat_text = "\n".join(chat_lines)
        
        # 2. 调用 LLM 总结
        # 从配置获取提示词模板并替换占位符
        custom_prompt = self.config.get("summarize_prompt")
        ai_name = self.config.get("ai_name")
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
                
                logger.warning(f"Summarization attempt {attempt + 1} produced empty or too short result.")
            except Exception as e:
                logger.error(f"Summarization attempt {attempt + 1} error: {e}")
            
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay)
        
        if not full_content or len(full_content) < 5:
            logger.error(f"Failed to summarize chat for user {user_id} after {max_retries} attempts.")
            return None

        # 总结仅用于归档，不在此处做画像更新
        summary = full_content

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
                logger.debug("Engram: Global summarize aborted due to shutdown")
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
                logger.error(f"Engram: Force summarize failed for {uid}: {e}")
        return summarized
    
    # ========== 记忆检索 ==========
    
    async def retrieve_memories(self, user_id, query, limit=3):
        """检索相关记忆并返回原文摘要及背景（基于时间链），使用关键词重排序提升精确匹配"""
        # 确保 ChromaDB 已初始化
        await self._ensure_chroma_initialized()
        
        loop = asyncio.get_event_loop()
        
        # 1. ChromaDB 检索（多取一些结果以便过滤和重排序后仍有足够数据）
        query_params = {
            "query_texts": [query],
            "n_results": min(limit * 3, 15),  # 多取结果以便重排序
            "where": {"user_id": user_id}
        }
        results = await loop.run_in_executor(self.executor, lambda: self.collection.query(**query_params))
        
        if not results or not results['ids'] or not results['ids'][0]:
            return []
        
        # 获取配置
        similarity_threshold = self.config.get("memory_similarity_threshold", 1.5)
        show_relevance_score = self.config.get("show_relevance_score", True)
        enable_keyword_boost = self.config.get("enable_keyword_boost", True)
        enable_ngram_keyword_rank = self.config.get("enable_ngram_keyword_rank", True)
        
        # 解析关键词权重（新格式直接是数值字符串 "0.5"）
        weight_config = self.config.get("keyword_boost_weight", "0.5")
        try:
            keyword_boost_weight = float(weight_config)
        except (ValueError, TypeError):
            # 向后兼容旧格式 "均衡模式 (0.5)"
            match = re.search(r'\(([\d.]+)\)', str(weight_config))
            keyword_boost_weight = float(match.group(1)) if match else 0.5
        
        # 2. 预处理结果并计算关键词匹配度（边界感知 + 近似 IDF）
        distances = results.get('distances', [[]])[0] if 'distances' in results else []
        memory_data = []

        query_keywords = self._generate_query_keywords(query) if enable_ngram_keyword_rank else {k.lower() for k in re.split(r'[^\w]+', query) if k.strip()}

        # 首轮收集候选（先过滤相似度）
        candidate_memories = []
        for i in range(len(results['ids'][0])):
            distance = distances[i] if distances and i < len(distances) else float('inf')

            # 过滤低相关性结果
            if distance > similarity_threshold:
                logger.debug(f"Skipping memory with distance {distance:.3f} (threshold: {similarity_threshold})")
                continue

            candidate_memories.append({
                'index_id': results['ids'][0][i],
                'summary': results['documents'][0][i],
                'metadata': results['metadatas'][0][i],
                'distance': distance
            })

        if enable_ngram_keyword_rank:
            # 统计候选集中各关键词文档频率（用于稀有词提升）
            keyword_doc_freq = Counter()
            min_n = max(2, int(self.config.get("keyword_ngram_min", 2)))
            max_n = max(min_n, int(self.config.get("keyword_ngram_max", 4)))
            max_n = min(max_n, 6)

            for item in candidate_memories:
                summary = item['summary']
                summary_tokens_en = Counter(_ENGLISH_WORD_PATTERN.findall(summary.lower()))
                summary_ngrams_zh = Counter()
                for block in _CHINESE_BLOCK_PATTERN.findall(summary):
                    for n in range(min_n, max_n + 1):
                        if len(block) < n:
                            continue
                        for pos in range(0, len(block) - n + 1):
                            summary_ngrams_zh[block[pos:pos + n]] += 1

                for keyword in query_keywords:
                    if self._count_keyword_matches(keyword, summary_tokens_en, summary_ngrams_zh) > 0:
                        keyword_doc_freq[keyword] += 1

            corpus_stats = {
                'total_docs': len(candidate_memories),
                'keyword_doc_freq': keyword_doc_freq
            }

            for item in candidate_memories:
                keyword_score, _ = self._calc_keyword_score(query, item['summary'], corpus_stats)
                item['keyword_score'] = keyword_score
                memory_data.append(item)
        else:
            for item in candidate_memories:
                item['keyword_score'] = self._calc_keyword_score_legacy(query_keywords, item['summary'])
                memory_data.append(item)
        
        # 3. RRF (Reciprocal Rank Fusion) 融合排序
        #    RRF_score(d) = w_v / (k + rank_vector(d)) + w_k / (k + rank_keyword(d))
        #    k=60 是标准值，keyword_boost_weight 控制两路信号的权重比例
        rrf_k = 60
        
        if enable_keyword_boost and query_keywords and len(memory_data) > 1:
            vector_w = 1.0 - keyword_boost_weight
            keyword_w = keyword_boost_weight
            
            # 按向量距离排名（距离越小排名越靠前，rank 从 1 开始）
            sorted_by_vector = sorted(range(len(memory_data)), key=lambda idx: memory_data[idx]['distance'])
            vector_rank = {idx: rank + 1 for rank, idx in enumerate(sorted_by_vector)}
            
            # 按关键词得分排名（得分越高排名越靠前）
            sorted_by_keyword = sorted(range(len(memory_data)), key=lambda idx: memory_data[idx]['keyword_score'], reverse=True)
            keyword_rank = {idx: rank + 1 for rank, idx in enumerate(sorted_by_keyword)}
            
            # 计算 RRF 融合得分
            for i, data in enumerate(memory_data):
                rrf_vector = vector_w / (rrf_k + vector_rank[i])
                rrf_keyword = keyword_w / (rrf_k + keyword_rank[i])
                data['rrf_score'] = rrf_vector + rrf_keyword
            
            # 按 RRF 得分排序（得分越高越靠前）
            memory_data.sort(key=lambda x: x['rrf_score'], reverse=True)
        else:
            # 纯向量模式或无关键词：退化为按距离排序
            for data in memory_data:
                data['rrf_score'] = max(0, 1 - data['distance'] / 2.0)
            memory_data.sort(key=lambda x: x['distance'])
        
        # 4. 只保留前 limit 条
        memory_data = memory_data[:limit]
        # 5. 构造带时间线背景和评分的记忆文本（批量查询，避免 N+1）
        all_memories = []

        index_ids = [item['index_id'] for item in memory_data]
        index_map = await loop.run_in_executor(self.executor, self.db.get_memory_indices_by_ids, index_ids)

        enable_context_hint = self.config.get("enable_memory_context_hint", True)
        memory_context_window = max(0, int(self.config.get("memory_context_window", 1)))
        memory_context_window = min(memory_context_window, 5)

        # 按配置窗口收集前情索引 ID（链路展开）
        context_prev_ids = set()
        context_prev_map = {}
        if enable_context_hint and memory_context_window > 0:
            frontier = [index_map[idx].prev_index_id for idx in index_ids if index_map.get(idx) and index_map[idx].prev_index_id]
            for _ in range(memory_context_window):
                if not frontier:
                    break
                prev_map = await loop.run_in_executor(self.executor, self.db.get_memory_indices_by_ids, frontier)
                next_frontier = []
                for pid, pidx in prev_map.items():
                    context_prev_ids.add(pid)
                    if pidx and pidx.prev_index_id:
                        next_frontier.append(pidx.prev_index_id)
                frontier = next_frontier
            if context_prev_ids:
                context_prev_map = await loop.run_in_executor(self.executor, self.db.get_memory_indices_by_ids, list(context_prev_ids))

        # 批量拉取原文消息
        uuid_lists = []
        for idx in index_ids:
            db_index = index_map.get(idx)
            if not db_index or not db_index.ref_uuids:
                continue
            try:
                uuids = json.loads(db_index.ref_uuids)
            except Exception:
                uuids = []
            if uuids:
                uuid_lists.append(uuids)

        raw_map = await loop.run_in_executor(self.executor, self.db.get_memories_by_uuids_map, uuid_lists) if uuid_lists else {}

        for data in memory_data:
            index_id = data['index_id']
            summary = data['summary']
            metadata = data['metadata']
            distance = data['distance']
            created_at = metadata.get("created_at", "未知时间")

            quality_factor = max(0.0, 1.5 - distance) / 1.5
            if enable_keyword_boost and query_keywords and memory_data:
                best_rrf = memory_data[0].get('rrf_score', 1e-9)
                raw_percent = data.get('rrf_score', 0) / max(best_rrf, 1e-9) * 100
                relevance_percent = max(0, min(100, int(raw_percent * quality_factor)))
            else:
                relevance_percent = max(0, min(100, int((1 - distance / 2.0) * 100)))

            db_index = index_map.get(index_id)
            context_hint = ""
            if enable_context_hint and memory_context_window > 0 and db_index:
                snippets = []
                prev_id = db_index.prev_index_id
                for _ in range(memory_context_window):
                    if not prev_id:
                        break
                    prev_index = context_prev_map.get(prev_id)
                    if not prev_index:
                        break
                    snippet = prev_index.summary[:24].replace("\n", " ")
                    snippets.append(snippet)
                    prev_id = prev_index.prev_index_id
                if snippets:
                    timeline_text = " → ".join(snippets)
                    if len(timeline_text) > 80:
                        timeline_text = timeline_text[:77] + "..."
                    context_hint = f"（前情提要：{timeline_text}）"

            raw_preview = ""
            if db_index and db_index.ref_uuids:
                try:
                    uuids = tuple(sorted(str(u) for u in json.loads(db_index.ref_uuids) if u))
                except Exception:
                    uuids = tuple()
                raw_msgs = raw_map.get(uuids, []) if uuids else []
                filtered_raw = [m.content[:60] for m in raw_msgs if self._is_valid_message_content(m.content)][:2]
                if filtered_raw:
                    raw_preview = "\n   └ 📄 相关原文：\n" + "\n".join([f"      {i+1}) {text}" for i, text in enumerate(filtered_raw)])

            short_id = index_id[:8]
            relevance_badge = f"🎯 {relevance_percent}% | " if show_relevance_score else ""
            all_memories.append(f"{relevance_badge}🆔 {short_id} | ⏰ {created_at}\n📝 归档：{summary}{context_hint}{raw_preview}")
        # 6. Reinforce：被成功召回的记忆增强 active_score
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
                    logger.debug(f"Engram: Failed to reinforce memory {data['index_id'][:8]}: {e}")
            
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
        
        # 1. 查找匹配的记忆索引
        def _find_memory():
            with self.db.db.connection_context():
                from ..db_manager import MemoryIndex
                # 如果是短ID（8位），查找匹配的完整UUID
                if len(short_id) == 8:
                    query = MemoryIndex.select().where(
                        (MemoryIndex.user_id == user_id) &
                        (MemoryIndex.index_id.startswith(short_id))
                    )
                else:
                    # 完整UUID
                    query = MemoryIndex.select().where(
                        (MemoryIndex.user_id == user_id) &
                        (MemoryIndex.index_id == short_id)
                    )
                return query.first()
        
        target_memory = await loop.run_in_executor(self.executor, _find_memory)
        
        if not target_memory:
            return None, f"找不到 ID 为 {short_id} 的记忆，请确认 ID 是否正确。"
        
        # 2. 解析原文 UUID
        if not target_memory.ref_uuids:
            return target_memory, []
            
        uuids = json.loads(target_memory.ref_uuids)
        raw_msgs = await loop.run_in_executor(self.executor, self.db.get_memories_by_uuids, uuids)
        
        return target_memory, raw_msgs
    
    # ========== 记忆删除与撤销 ==========
    
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
                logger.debug(f"Failed to get vector data for backup: {e}")
            
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
            # 只保留最近N次删除
            self._delete_history[user_id] = self._delete_history[user_id][:self._max_undo_history]
            
            # 2. 从 ChromaDB 删除向量数据
            await loop.run_in_executor(self.executor, lambda: self.collection.delete(ids=[index_id]))
            
            # 3. 如果需要，删除关联的原始消息
            if delete_raw and target_memory.ref_uuids:
                uuids = json.loads(target_memory.ref_uuids)
                await loop.run_in_executor(self.executor, self.db.delete_raw_memories_by_uuids, uuids)
            else:
                # 不删除原始消息时，将其标记为未归档，以便重新总结
                if deleted_uuids:
                    def _mark_unarchived():
                        from ..db_manager import RawMemory
                        with self.db.db.connection_context():
                            RawMemory.update(is_archived=False).where(RawMemory.uuid << deleted_uuids).execute()
                    await loop.run_in_executor(self.executor, _mark_unarchived)
            
            # 4. 从 SQLite 删除记忆索引
            await loop.run_in_executor(self.executor, self.db.delete_memory_index, index_id)
            
            return True, "删除成功", summary
            
        except Exception as e:
            logger.error(f"Delete memory error: {e}")
            return False, f"删除失败：{e}", summary
    
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
                'created_at': delete_record['created_at'],
                'active_score': delete_record.get('active_score', 100)
            }
            await loop.run_in_executor(self.executor, lambda: self.db.save_memory_index(**index_params))
            
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
                await loop.run_in_executor(self.executor, lambda: self.collection.add(**add_params))
            
            # 3. 恢复原始消息的归档状态
            if delete_record['deleted_uuids']:
                def _mark_archived():
                    from ..db_manager import RawMemory
                    with self.db.db.connection_context():
                        RawMemory.update(is_archived=True).where(
                            RawMemory.uuid << delete_record['deleted_uuids']
                        ).execute()
                try:
                    await loop.run_in_executor(self.executor, _mark_archived)
                except Exception as e:
                    logger.debug(f"Failed to restore raw messages archive status: {e}")
            
            return True, "撤销成功", delete_record['summary']
            
        except Exception as e:
            logger.error(f"Undo delete error: {e}")
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
        loop = asyncio.get_event_loop()
        
        # 1. 查找匹配的记忆索引
        def _find_memory():
            with self.db.db.connection_context():
                from ..db_manager import MemoryIndex
                # 如果是短ID（8位），查找匹配的完整UUID
                if len(short_id) == 8:
                    query = MemoryIndex.select().where(
                        (MemoryIndex.user_id == user_id) &
                        (MemoryIndex.index_id.startswith(short_id))
                    )
                else:
                    # 完整UUID
                    query = MemoryIndex.select().where(
                        (MemoryIndex.user_id == user_id) &
                        (MemoryIndex.index_id == short_id)
                    )
                return query.first()
        
        try:
            target_memory = await loop.run_in_executor(self.executor, _find_memory)
            
            if not target_memory:
                return False, f"找不到 ID 为 {short_id} 的记忆，请确认 ID 是否正确。", ""
            
            index_id = target_memory.index_id
            summary = target_memory.summary
            
            # 确保 ChromaDB 已初始化
            await self._ensure_chroma_initialized()
            
            # 2. 从 ChromaDB 删除向量数据
            await loop.run_in_executor(self.executor, lambda: self.collection.delete(ids=[index_id]))
            
            # 3. 如果需要，删除关联的原始消息
            if delete_raw and target_memory.ref_uuids:
                uuids = json.loads(target_memory.ref_uuids)
                await loop.run_in_executor(self.executor, self.db.delete_raw_memories_by_uuids, uuids)
            
            # 4. 从 SQLite 删除记忆索引
            await loop.run_in_executor(self.executor, self.db.delete_memory_index, index_id)
            
            return True, "删除成功", summary
            
        except Exception as e:
            logger.error(f"Delete memory by ID error: {e}")
            return False, f"删除失败：{e}", ""
    
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
            logger.error(f"Export raw messages error: {e}")
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
                return False, "没有找到可导出的消息", {}
            
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
            logger.error(f"Export all users messages error: {e}")
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
