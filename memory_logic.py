import chromadb
import os
import uuid
import json
import re
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from astrbot.api import logger
from .db_manager import DatabaseManager

# 预编译正则表达式
_CHINESE_PATTERN = re.compile(r'[\u4e00-\u9fa5]')

class MemoryLogic:
    def __init__(self, context, config, data_dir):
        self.context = context
        self.config = config
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        
        self.db = DatabaseManager(self.data_dir)
        
        # ChromaDB 延迟初始化（避免构造函数阻塞）
        self.chroma_path = os.path.join(self.data_dir, "engram_chroma")
        self.chroma_client = None
        self.collection = None
        self._chroma_init_lock = asyncio.Lock()
        self._chroma_initialized = False
        
        # 用户画像路径
        self.profiles_dir = os.path.join(self.data_dir, "engram_personas")
        os.makedirs(self.profiles_dir, exist_ok=True)
        
        # 线程池处理数据库和向量库操作
        self.executor = ThreadPoolExecutor(max_workers=4)
        self._is_shutdown = False
        
        # 内存中记录最后聊天时间（带自动清理机制）
        self.last_chat_time = {}     # {user_id: timestamp}
        self.unsaved_msg_count = {}  # {user_id: count}
        self._max_inactive_users = 100  # 最大缓存用户数
        self._inactive_threshold = 7 * 24 * 3600  # 7天无活动则清理
        
        # 撤销删除缓存：{user_id: [最近删除的记忆列表]}
        self._delete_history = {}  # 每个用户保留最近3次删除
        self._max_undo_history = 3

    def shutdown(self):
        self._is_shutdown = True
        self.executor.shutdown(wait=False)
    
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

    def _get_profile_path(self, user_id):
        return os.path.join(self.profiles_dir, f"{user_id}.json")

    @staticmethod
    def _is_valid_message_content(content: str) -> bool:
        """
        统一的消息内容过滤逻辑，用于判断消息是否应被纳入归档/检索。
        
        过滤规则：
        1. 以常见指令前缀开头的消息
        2. 带下划线且无空格的内部指令
        3. 中文字符不足2个且总长度不足10的短消息
        
        返回 True 表示消息有效，False 表示应被过滤。
        """
        import re
        content = content.strip()
        
        # 1. 过滤以常见指令前缀开头的消息
        if content.startswith(('/', '#', '~', '!', '！', '／', '&', '*')):
            return False
        
        # 2. 专门清洗带下划线的内部指令
        if "_" in content and " " not in content:
            return False
        
        # 3. 统计中文数量或检查总长度
        chinese_chars = _CHINESE_PATTERN.findall(content)
        if len(chinese_chars) < 2 and len(content) < 10:
            return False
        
        return True

    async def get_user_profile(self, user_id):
        """获取用户画像"""
        loop = asyncio.get_event_loop()
        path = self._get_profile_path(user_id)
        
        def _read():
            if not os.path.exists(path):
                # 新的、更具体的画像结构
                return {
                    "basic_info": {
                        "qq_id": user_id,
                        "nickname": "未知",
                        "gender": "未知",
                        "age": "未知",
                        "location": "未知",
                        "job": "未知",
                        "avatar_url": "",
                        "birthday": "未知",
                        "constellation": "未知",
                        "zodiac": "未知",
                        "signature": "暂无个性签名"
                    },
                    "attributes": {
                        "personality_tags": [], # 例如：严谨、幽默 (仅当明显表现时)
                        "hobbies": [],          # 例如：编程、看电影
                        "skills": []            # 例如：Python、钢琴
                    },
                    "preferences": {
                        "likes": [],            # 明确喜欢的：生椰拿铁
                        "dislikes": []          # 明确讨厌的：美式
                    },
                    "social_graph": {
                        "relationship_status": "初识", # 当前与 AI 的关系：陌生 -> 熟悉 -> 依赖
                        "important_people": []   # 提到的朋友/家人
                    },
                    "dev_metadata": {           # 专门为开发者保留的元数据
                        "os": [],
                        "tech_stack": []
                    }
                }
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                return {}
        
        return await loop.run_in_executor(self.executor, _read)

    async def update_user_profile(self, user_id, update_data):
        """更新用户画像 (Sidecar 模式)"""
        if not update_data:
            return
            
        loop = asyncio.get_event_loop()
        path = self._get_profile_path(user_id)
        
        def _update():
            profile = {}
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        profile = json.load(f)
                except:
                    pass
            
            # 合并逻辑
            for key, value in update_data.items():
                if isinstance(value, list):
                    # 列表处理：去重并合并
                    old_list = profile.get(key, [])
                    if not isinstance(old_list, list): old_list = [old_list]
                    new_list = list(set(old_list + value))
                    profile[key] = new_list
                elif isinstance(value, dict):
                    # 字典处理：递归一级合并
                    old_dict = profile.get(key, {})
                    if not isinstance(old_dict, dict): old_dict = {}
                    old_dict.update(value)
                    profile[key] = old_dict
                else:
                    # 基本属性：直接覆盖
                    profile[key] = value
            
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(profile, f, ensure_ascii=False, indent=4)
            return profile

        return await loop.run_in_executor(self.executor, _update)

    async def clear_user_profile(self, user_id):
        """清除用户画像"""
        loop = asyncio.get_event_loop()
        path = self._get_profile_path(user_id)
        def _delete():
            if os.path.exists(path):
                os.remove(path)
        await loop.run_in_executor(self.executor, _delete)

    async def record_message(self, user_id, session_id, role, content, msg_type="text", user_name=None):
        import datetime
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

    async def check_and_summarize(self):
        """检查是否需要进行私聊归档（画像更新由独立调度器处理）"""
        import datetime
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

    async def _update_persona_daily(self, user_id):
        """每日画像深度更新 (用户画像架构)"""
        # 1. 获取该用户当天的所有记忆索引
        import datetime
        today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        
        loop = asyncio.get_event_loop()
        memories = await loop.run_in_executor(self.executor, self.db.get_memories_since, user_id, today)
        
        if not memories:
            return

        # 2. 结合现有画像和今日记忆进行深度更新
        current_persona = await self.get_user_profile(user_id)
        memory_texts = "\n".join([f"- {m.summary}" for m in memories])
        
        # 全新的 Prompt，强调事实提取
        prompt = f"""
你是一个严谨的【用户信息档案员】。你的任务是根据今日的新增记忆，更新用户的档案数据。

【当前档案】：
{json.dumps(current_persona, ensure_ascii=False, indent=2)}

【今日新增记忆】：
{memory_texts}

【更新规则】：
1. **绝对客观**：仅从【今日新增记忆】中提取明确的事实。不要进行心理分析，不要脑补用户没说过的话。
2. **增量更新**：
   - 如果记忆中没有提到某项信息（如所在地、职业），请保持【当前档案】中的原值，**不要**将其覆盖为"未知"或null。
   - 如果有新信息冲突，以【今日新增记忆】为准。
   - 列表类型（如 hobbies, likes）请追加新内容，并去重。
3. **字段定义**：
   - basic_info: 仅更新 gender(性别), age(年龄), location(所在地), job(职业)。
   - attributes: hobbies(具体爱好), skills(技能), personality_tags(性格关键词，如"急躁","温和")。
   - preferences: likes(喜欢的食物/事物), dislikes(讨厌的)。
   - social_graph: relationship_status(推测当前与AI的关系阶段，如: 开发者与测试员/朋友/搭档)。
   - dev_metadata: 如果用户提及代码、技术栈、操作系统，存入 tech_stack。

【输出要求】：
请直接返回更新后的完整 JSON 数据。不要包含 Markdown 标记，不要包含其他解释。
"""
        try:
            # 获取指定的模型或默认模型
            persona_model = self.config.get("persona_model", "").strip()
            if persona_model:
                provider = self.context.get_provider_by_id(persona_model)
                if not provider:
                    provider = self.context.get_using_provider()
            else:
                provider = self.context.get_using_provider()

            if not provider:
                return

            resp = await provider.text_chat(prompt=prompt)
            content = resp.completion_text
            
            # 解析并保存
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "{" in content:
                content = content[content.find("{"):content.rfind("}")+1]
                
            new_persona = json.loads(content)
            
            # 写入文件
            path = self._get_profile_path(user_id)
            def _write():
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(new_persona, f, ensure_ascii=False, indent=4)
            await loop.run_in_executor(self.executor, _write)
            
        except Exception as e:
            logger.error(f"Daily persona update error: {e}")

    async def _summarize_private_chat(self, user_id):
        """对私聊进行总结并存入长期记忆（按天分组处理）"""
        import datetime
        from itertools import groupby
        
        # 1. 获取未归档的原始消息
        loop = asyncio.get_event_loop()
        # 获取所有未归档消息，不设限制
        # 使用 lambda 传递参数以避免 run_in_executor 的关键字参数限制
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
            return m.timestamp.date()
            
        for date_key, group in groupby(raw_msgs, key=get_date_key):
            # 将 group 转为列表，因为 groupby 的迭代器只能用一次
            group_msgs = list(group)
            
            # 检查是否超过回溯天数限制
            if cutoff_date and date_key < cutoff_date:
                # 超过限制，直接标记为已归档，不进行总结
                ref_uuids = [m.uuid for m in group_msgs]
                await loop.run_in_executor(self.executor, self.db.mark_as_archived, ref_uuids)
                continue
                
            await self._process_single_summary_batch(user_id, group_msgs, date_key)

    async def _process_single_summary_batch(self, user_id, raw_msgs, date_key):
        """处理单批次（单日）消息的总结"""
        import datetime
        
        # 使用公共过滤方法
        filtered_msgs = [m for m in raw_msgs if self._is_valid_message_content(m.content)]
        
        loop = asyncio.get_event_loop()
        
        if not filtered_msgs:
            # 如果没有符合条件的消息，也标记原本的所有消息为已归档
            ref_uuids = [m.uuid for m in raw_msgs]
            await loop.run_in_executor(self.executor, self.db.mark_as_archived, ref_uuids)
            return

        # 构造对话文本
        chat_lines = [f"【日期：{date_key.strftime('%Y-%m-%d')}】"]
        for m in filtered_msgs:
            time_str = m.timestamp.strftime("%H:%M")
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
            return

        # 解析日记和画像
        summary = full_content
        persona_update = {}
        if "[JSON_START]" in full_content and "[JSON_END]" in full_content:
            try:
                summary = full_content.split("[JSON_START]")[0].strip()
                json_str = full_content.split("[JSON_START]")[1].split("[JSON_END]")[0].strip()
                persona_update = json.loads(json_str)
                # 实时更新画像
                if persona_update:
                    await self.update_user_profile(user_id, persona_update)
            except Exception as e:
                logger.error(f"Failed to parse persona update: {e}")
            
        try:
            # 确保 ChromaDB 已初始化
            await self._ensure_chroma_initialized()
            
            # 3. 存入 ChromaDB 和 SQLite Index
            index_id = str(uuid.uuid4())
            ref_uuids = [m.uuid for m in raw_msgs] # 注意：归档标记原始的所有消息
            
            # 使用该批次最后一条消息的时间作为归档时间，确保历史重构时的顺序正确
            created_at = raw_msgs[-1].timestamp
            
            # 获取前一条记忆索引，形成链表（时间线）
            last_index = await loop.run_in_executor(self.executor, self.db.get_last_memory_index, user_id)
            prev_index_id = last_index.index_id if last_index else None
            
            # 向量化存储（使用配置的 AI 名称）
            ai_name = self.config.get("ai_name", "助手")
            add_params = {
                "ids": [index_id],
                "documents": [summary],
                "metadatas": [{
                    "user_id": user_id,
                    "source_type": "private",
                    "created_at": created_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "ai_name": ai_name
                }]
            }
            await loop.run_in_executor(self.executor, lambda: self.collection.add(**add_params))
            
            # 索引存储
            index_params = {
                "index_id": index_id,
                "summary": summary,
                "ref_uuids": json.dumps(ref_uuids),
                "prev_index_id": prev_index_id, # 链接到前一条
                "source_type": "private",
                "user_id": user_id,
                "created_at": created_at
            }
            await loop.run_in_executor(self.executor, lambda: self.db.save_memory_index(**index_params))
            
            # 4. 标记这些消息为已归档，防止重复总结
            await loop.run_in_executor(self.executor, self.db.mark_as_archived, ref_uuids)
            
        except Exception as e:
            logger.error(f"Save summarization error: {e}")

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
        
        # 解析关键词权重（新格式直接是数值字符串 "0.5"）
        weight_config = self.config.get("keyword_boost_weight", "0.5")
        try:
            keyword_boost_weight = float(weight_config)
        except (ValueError, TypeError):
            # 向后兼容旧格式 "均衡模式 (0.5)"
            import re
            match = re.search(r'\(([\d.]+)\)', str(weight_config))
            keyword_boost_weight = float(match.group(1)) if match else 0.5
        
        # 2. 预处理结果并计算关键词匹配度
        distances = results.get('distances', [[]])[0] if 'distances' in results else []
        memory_data = []
        
        # 提取查询关键词（简单分词：按空格和标点分割）
        query_keywords = set()
        for char in ['，', '。', '！', '？', '、', ' ', ',', '.', '!', '?']:
            query = query.replace(char, ' ')
        query_keywords = set([w.strip().lower() for w in query.split() if len(w.strip()) > 0])
        
        for i in range(len(results['ids'][0])):
            distance = distances[i] if distances and i < len(distances) else float('inf')
            
            # 过滤低相关性结果
            if distance > similarity_threshold:
                logger.debug(f"Skipping memory with distance {distance:.3f} (threshold: {similarity_threshold})")
                continue
            
            index_id = results['ids'][0][i]
            summary = results['documents'][0][i]
            metadata = results['metadatas'][0][i]
            
            # 计算关键词匹配度（关键词在summary中出现的次数）
            keyword_score = 0
            summary_lower = summary.lower()
            for keyword in query_keywords:
                # 精确匹配得分更高
                if keyword in summary_lower:
                    # 统计出现次数
                    count = summary_lower.count(keyword)
                    keyword_score += count * len(keyword)  # 长关键词权重更高
            
            # 归一化关键词得分（0-1之间）
            keyword_score_normalized = min(1.0, keyword_score / max(1, len(query) * 2))
            
            memory_data.append({
                'index_id': index_id,
                'summary': summary,
                'metadata': metadata,
                'distance': distance,
                'keyword_score': keyword_score_normalized
            })
        
        # 3. 混合排序：结合向量相似度和关键词匹配度
        if enable_keyword_boost and query_keywords:
            # 计算综合得分（距离越小越好，关键词得分越高越好）
            for data in memory_data:
                # 向量得分：将距离转换为0-1的得分（距离越小得分越高）
                vector_score = max(0, 1 - data['distance'] / 2.0)
                
                # 综合得分 = 向量得分 * (1 - weight) + 关键词得分 * weight
                data['combined_score'] = (
                    vector_score * (1 - keyword_boost_weight) +
                    data['keyword_score'] * keyword_boost_weight
                )
            
            # 按综合得分排序（得分越高越靠前）
            memory_data.sort(key=lambda x: x['combined_score'], reverse=True)
        else:
            # 仅按向量距离排序
            memory_data.sort(key=lambda x: x['distance'])
        
        # 4. 只保留前 limit 条
        memory_data = memory_data[:limit]
        
        # 5. 构造带时间线背景和评分的记忆文本
        all_memories = []
        
        for data in memory_data:
            index_id = data['index_id']
            summary = data['summary']
            metadata = data['metadata']
            distance = data['distance']
            keyword_score = data.get('keyword_score', 0)
            created_at = metadata.get("created_at", "未知时间")
            
            # 计算显示的相关性百分比
            if enable_keyword_boost and query_keywords:
                # 使用综合得分
                relevance_percent = int(data['combined_score'] * 100)
            else:
                # 使用向量得分
                relevance_percent = max(0, min(100, int((1 - distance / 2.0) * 100)))
            
            # 尝试通过链表获取"前情提要"
            context_hint = ""
            db_index = await loop.run_in_executor(self.executor, self.db.get_memory_index_by_id, index_id)
            if db_index and db_index.prev_index_id:
                prev_index = await loop.run_in_executor(self.executor, self.db.get_memory_index_by_id, db_index.prev_index_id)
                if prev_index:
                    context_hint = f"（前情提要：{prev_index.summary[:50]}...）"
            
            # 获取原文 UUID 列表
            raw_preview = ""
            if db_index and db_index.ref_uuids:
                uuids = json.loads(db_index.ref_uuids)
                # 获取该总结对应的所有原文
                raw_msgs = await loop.run_in_executor(self.executor, self.db.get_memories_by_uuids, uuids)
                
                # 使用公共过滤方法，取前 3 条有效原文作为证据参考
                filtered_raw = [
                    m.content[:30] for m in raw_msgs
                    if self._is_valid_message_content(m.content)
                ][:3]
                
                if filtered_raw:
                    raw_preview = "\n   └ 📄 相关原文：" + " | ".join(filtered_raw)
            
            # 添加 ID 信息（UUID 前 8 位）和相关性评分
            short_id = index_id[:8]
            
            # 根据配置决定是否显示相关性评分
            if show_relevance_score:
                relevance_badge = f"🎯 {relevance_percent}% | "
            else:
                relevance_badge = ""
            
            all_memories.append(f"{relevance_badge}🆔 {short_id} | ⏰ {created_at}\n📝 归档：{summary}{context_hint}{raw_preview}")
            
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
                        from .db_manager import RawMemory
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
                    from .db_manager import RawMemory
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
                from .db_manager import MemoryIndex
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
    
    def _export_as_jsonl(self, raw_msgs):
        """导出为 JSONL 格式（每行一个 JSON 对象）"""
        lines = []
        for msg in raw_msgs:
            if not self._is_valid_message_content(msg.content):
                continue
            obj = {
                "role": "assistant" if msg.role == "assistant" else "user",
                "content": msg.content,
                "timestamp": msg.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
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
            messages.append({
                "role": "assistant" if msg.role == "assistant" else "user",
                "content": msg.content,
                "timestamp": msg.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
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
            role_name = "助手" if msg.role == "assistant" else (msg.user_name or "用户")
            time_str = msg.timestamp.strftime("%Y-%m-%d %H:%M:%S")
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
