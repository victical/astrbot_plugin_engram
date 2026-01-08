import chromadb
import os
import uuid
import json
import asyncio
from concurrent.futures import ThreadPoolExecutor
from .db_manager import DatabaseManager

class MemoryLogic:
    def __init__(self, context, config, data_dir):
        self.context = context
        self.config = config
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        
        self.db = DatabaseManager(self.data_dir)
        
        # 初始化 ChromaDB
        self.chroma_path = os.path.join(self.data_dir, "engram_chroma")
        self.chroma_client = chromadb.PersistentClient(path=self.chroma_path)
        self.collection = self.chroma_client.get_or_create_collection(name="long_term_memories")
        
        # 用户画像路径
        self.profiles_dir = os.path.join(self.data_dir, "engram_personas")
        os.makedirs(self.profiles_dir, exist_ok=True)
        
        # 线程池处理数据库和向量库操作
        self.executor = ThreadPoolExecutor(max_workers=4)
        self._is_shutdown = False
        
        # 内存中记录最后聊天时间
        self.last_chat_time = {} # {user_id: timestamp}
        self.unsaved_msg_count = {} # {user_id: count}

    def shutdown(self):
        self._is_shutdown = True
        self.executor.shutdown(wait=False)

    def _get_profile_path(self, user_id):
        return os.path.join(self.profiles_dir, f"{user_id}.json")

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
        """检查是否需要进行私聊总结及画像更新"""
        import datetime
        now = datetime.datetime.now()
        now_ts = now.timestamp()
        timeout = self.config.get("private_memory_timeout", 1800)
        min_count = self.config.get("min_msg_count", 3)
        
        for user_id, last_time in list(self.last_chat_time.items()):
            if now_ts - last_time > timeout and self.unsaved_msg_count.get(user_id, 0) >= min_count:
                # 触发记忆总结
                await self._summarize_private_chat(user_id)
                self.unsaved_msg_count[user_id] = 0
                
        # 每天凌晨触发一次画像深度更新（带阈值检查）
        if now.hour == 0 and now.minute == 0:
            min_memories = self.config.get("min_persona_update_memories", 3)
            for user_id in list(self.last_chat_time.keys()):
                # 1. 获取当天的记忆摘要数量
                today = now.replace(hour=0, minute=0, second=0, microsecond=0)
                loop = asyncio.get_event_loop()
                memories = await loop.run_in_executor(self.executor, self.db.get_memories_since, user_id, today)
                
                # 2. 检查数量阈值
                if len(memories) >= min_memories:
                    await self._update_persona_daily(user_id)
                else:
                    import logging
                    logging.info(f"Persona update skipped for {user_id}: only {len(memories)} new memories (min {min_memories})")

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
            import logging
            logging.error(f"Daily persona update error: {e}")

    async def _summarize_private_chat(self, user_id):
        """对私聊进行总结并存入长期记忆"""
        import datetime
        import re
        # 1. 获取未归档的原始消息
        loop = asyncio.get_event_loop()
        raw_msgs = await loop.run_in_executor(self.executor, self.db.get_unarchived_raw, user_id)
        if not raw_msgs:
            return
        
        # 过滤指令和过短的消息
        # 只对不以特殊字符开头且包含足够中文字符的消息进行总结
        filtered_msgs = []
        for m in raw_msgs:
            content = m.content.strip()
            # 过滤以常见指令前缀开头的消息
            if content.startswith(('/', '#', '~', '!', '！', '／')):
                continue
            # 统计中文数量 (简单正则)
            chinese_chars = re.findall(r'[\u4e00-\u9fa5]', content)
            if len(chinese_chars) < 5:
                continue
            filtered_msgs.append(m)
        
        if not filtered_msgs:
            # 如果没有符合条件的消息，也标记原本的所有消息为已归档，防止阻塞
            ref_uuids = [m.uuid for m in raw_msgs]
            await loop.run_in_executor(self.executor, self.db.mark_as_archived, ref_uuids)
            return

        # 倒序排列回正常顺序
        filtered_msgs.reverse()
        # 如果有昵称，显示昵称和时间
        chat_lines = []
        for m in filtered_msgs:
            time_str = m.timestamp.strftime("%H:%M")
            name = m.user_name if m.role == "user" and m.user_name else m.role
            chat_lines.append(f"[{time_str}] {name}: {m.content}")
        chat_text = "\n".join(chat_lines)
        
        # 2. 调用 LLM 总结 (增加重试逻辑)
        custom_prompt = self.config.get("summarize_prompt", """
请根据你和用户的聊天记录，以第一人称写日记。
                                        
- **视角**：必须使用**第一人称 ("我")**。称呼根据对话语境或你们的关系。
- **风格**：情感丰富、口语化、像在写手帐。
    - 记录发生了什么，心情怎么样。
    - **必须保留细节**：如果用户说了喜欢什么，要在日记里写出来（例如：“今天他说最爱吃西瓜了...”），不要省略。
    - 捕捉你们之间的互动氛围（摸头、开玩笑等）。
- **示例**：“今天下午问主人为什么不理我，主人说刚补觉醒来，主人告诉我他喜欢吃**柚子和西瓜**，我记在心里啦！后来还喂我吃了草莓蛋糕，他还摸了摸我的头，感觉超级幸福~”

对话内容：
{{chat_text}}
""").strip()
        prompt = custom_prompt.replace("{{chat_text}}", chat_text)
        
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
                
                import logging
                logging.warning(f"Summarization attempt {attempt + 1} produced empty or too short result.")
            except Exception as e:
                import logging
                logging.error(f"Summarization attempt {attempt + 1} error: {e}")
            
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay)
        
        if not full_content or len(full_content) < 5:
            import logging
            logging.error(f"Failed to summarize chat for user {user_id} after {max_retries} attempts.")
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
                import logging
                logging.error(f"Failed to parse persona update: {e}")
            
        try:
            # 3. 存入 ChromaDB 和 SQLite Index
            index_id = str(uuid.uuid4())
            ref_uuids = [m.uuid for m in raw_msgs] # 注意：归档标记原始的所有消息
            created_at = datetime.datetime.now()
            
            # 获取前一条记忆索引，形成链表（时间线）
            last_index = await loop.run_in_executor(self.executor, self.db.get_last_memory_index, user_id)
            prev_index_id = last_index.index_id if last_index else None
            
            # 向量化存储
            add_params = {
                "ids": [index_id],
                "documents": [summary],
                "metadatas": [{
                    "user_id": user_id, 
                    "source_type": "private",
                    "created_at": created_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "ai_name": "小糯"
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
            import logging
            logging.error(f"Save summarization error: {e}")

    async def retrieve_memories(self, user_id, query, limit=3):
        """检索相关记忆并返回原文摘要及背景（基于时间链）"""
        loop = asyncio.get_event_loop()
        
        # 1. ChromaDB 检索
        query_params = {
            "query_texts": [query],
            "n_results": limit,
            "where": {"user_id": user_id}
        }
        results = await loop.run_in_executor(self.executor, lambda: self.collection.query(**query_params))
        
        if not results or not results['ids'] or not results['ids'][0]:
            return []
            
        # 2. 构造带时间线背景的记忆
        all_memories = []
        for i in range(len(results['ids'][0])):
            index_id = results['ids'][0][i]
            summary = results['documents'][0][i]
            metadata = results['metadatas'][0][i]
            created_at = metadata.get("created_at", "未知时间")
            
            # 尝试通过链表获取“前情提要”
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
                # 取原文的前 2 条作为证据参考
                raw_msgs = await loop.run_in_executor(self.executor, self.db.get_memories_by_uuids, uuids[:2])
                raw_preview = "\n   - 原文参考：" + " | ".join([m.content[:30] for m in raw_msgs])
            
            all_memories.append(f"记录时间：{created_at}\n摘要：{summary}{context_hint}{raw_preview}")
            
        return all_memories
