"""
用户画像管理器 (Profile Manager)

负责用户画像的 CRUD 操作、每日深度更新、互动统计维护等。
从 memory_logic.py 提取而来，遵循单一职责原则。

主要功能：
- 画像文件 I/O 操作
- 画像数据的增删改查
- 每日画像深度更新（通过 LLM）
- 互动统计自动维护（聊天天数、次数等）
- 字段保护机制（防止 LLM 覆盖系统字段）

依赖：
- context: AstrBot API 上下文（用于 LLM 调用）
- config: 插件配置（提示词、模型选择等）
- data_dir: 数据目录路径
- executor: 线程池（用于异步文件操作）
- db_manager: 数据库管理器（用于查询记忆索引）
"""

import os
import json
import asyncio
from datetime import date
from astrbot.api import logger
from ..services.profile_guardian import ProfileGuardian


class ProfileManager:
    """用户画像管理器"""
    
    def __init__(self, context, config, data_dir, executor, db_manager):
        """
        初始化画像管理器
        
        Args:
            context: AstrBot API 上下文对象
            config: 插件配置字典
            data_dir: 数据目录路径
            executor: ThreadPoolExecutor 实例
            db_manager: DatabaseManager 实例
        """
        self.context = context
        self.config = config
        self.data_dir = data_dir
        self.executor = executor
        self.db = db_manager
        
        # 用户画像存储目录
        self.profiles_dir = os.path.join(self.data_dir, "engram_personas")
        os.makedirs(self.profiles_dir, exist_ok=True)
        
        # 画像更新防护器（幻觉阻断）
        self._guardian = ProfileGuardian(config=config)
    
    def _get_profile_path(self, user_id):
        """获取用户画像文件路径"""
        return os.path.join(self.profiles_dir, f"{user_id}.json")
    
    async def get_user_profile(self, user_id):
        """
        获取用户画像
        
        如果画像文件不存在，返回默认的空画像结构（v2.1 优化版）。
        
        Args:
            user_id: 用户ID
            
        Returns:
            dict: 用户画像数据
        """
        loop = asyncio.get_event_loop()
        path = self._get_profile_path(user_id)
        
        def _read():
            if not os.path.exists(path):
                # 新的、更具体的画像结构（v2.1 优化版）
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
                        "personality_tags": [],  # 例如：严谨、幽默 (仅当明显表现时)
                        "hobbies": [],           # 例如：编程、看电影
                        "skills": []             # 例如：Python、钢琴
                    },
                    "preferences": {
                        "favorite_foods": [],      # 喜欢的食物：西瓜、奶茶、火锅
                        "favorite_items": [],      # 喜欢的物品：猫咪、手办、机械键盘
                        "favorite_activities": [], # 喜欢的活动：看电影、打游戏、逛街
                        "likes": [],               # 兼容旧版：其他喜欢的事物
                        "dislikes": []             # 明确讨厌的事物
                    },
                    "social_graph": {
                        "relationship_status": "萍水相逢",  # 羁绊等级名称
                        "important_people": [],              # 提到的朋友/家人
                        "interaction_stats": {               # 互动统计（v2.1 新增）
                            "first_chat_date": None,         # 首次聊天日期
                            "last_chat_date": None,          # 最后聊天日期
                            "total_chat_days": 0,            # 累计聊天天数（不要求连续）
                            "total_valid_chats": 0           # 有效聊天总次数
                        }
                    },
                    "dev_metadata": {            # 专门为开发者保留的元数据
                        "os": [],
                        "tech_stack": []
                    },
                    "shared_secrets": False,     # 是否分享过秘密/心事（LLM 检测标记）
                    "pending_proposals": []      # 画像更新提案池（置信度晋升机制）
                }
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                return {}
        
        return await loop.run_in_executor(self.executor, _read)
    
    async def update_user_profile(self, user_id, update_data):
        """
        更新用户画像 (Sidecar 模式)
        
        合并逻辑：
        - 列表字段：去重合并
        - 字典字段：递归一级合并
        - 基本类型：直接覆盖
        
        Args:
            user_id: 用户ID
            update_data: 要更新的数据字典
            
        Returns:
            dict: 更新后的完整画像
        """
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
                    if not isinstance(old_list, list): 
                        old_list = [old_list]
                    new_list = list(set(old_list + value))
                    profile[key] = new_list
                elif isinstance(value, dict):
                    # 字典处理：递归一级合并
                    old_dict = profile.get(key, {})
                    if not isinstance(old_dict, dict): 
                        old_dict = {}
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
        """
        清除用户画像
        
        Args:
            user_id: 用户ID
        """
        loop = asyncio.get_event_loop()
        path = self._get_profile_path(user_id)
        
        def _delete():
            if os.path.exists(path):
                os.remove(path)
        
        await loop.run_in_executor(self.executor, _delete)
    
    async def update_persona_daily(self, user_id, start_time=None, end_time=None):
        """
        每日画像深度更新 (用户画像架构)
        
        通过 LLM 分析用户的记忆索引，深度更新画像信息。
        包含字段保护机制，防止 LLM 覆盖系统维护的字段（如昵称、头像等）。
        
        Args:
            user_id: 用户ID
            start_time: 记忆查询起始时间（可选，默认为今天00:00）
            end_time: 记忆查询结束时间（可选，None表示无上限）
        """
        import datetime
        loop = asyncio.get_event_loop()
        
        # 1. 获取指定时间范围内的记忆索引
        if start_time is not None:
            if end_time is not None:
                # 使用完整的时间范围（用于凌晨00:00调度时查询昨天的记忆，或指定多天范围）
                memories = await loop.run_in_executor(
                    self.executor,
                    lambda: self.db.get_memories_in_range(user_id, start_time, end_time)
                )
            else:
                # 只有起始时间，无结束时间（获取从start_time到现在的所有记忆）
                memories = await loop.run_in_executor(
                    self.executor,
                    self.db.get_memories_since,
                    user_id,
                    start_time
                )
        else:
            # 默认行为：查询今天的记忆（用于手动触发 /engram_force_persona 不带参数时）
            today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            memories = await loop.run_in_executor(self.executor, self.db.get_memories_since, user_id, today)
        
        if not memories:
            return

        # 2. 结合现有画像和今日记忆进行深度更新
        current_persona = await self.get_user_profile(user_id)
        memory_texts = "\n".join([f"- {m.summary}" for m in memories])
        
        # 从配置获取画像更新提示词模板并替换占位符
        custom_prompt = self.config.get("persona_update_prompt")
        prompt = custom_prompt.replace("{{current_persona}}", json.dumps(current_persona, ensure_ascii=False, indent=2)).replace("{{memory_texts}}", memory_texts)
        
        
        # 添加调试日志：记录用于画像更新的用户ID和记忆内容
        logger.debug(f"Engram: Updating persona for user_id={user_id}, memory_count={len(memories)}")
        if len(memories) <= 5:
            logger.debug(f"Engram: Memory texts for persona update:\n{memory_texts}")
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
            
            # 【幻觉阻断】使用 ProfileGuardian 验证更新
            # - 保护 basic_info 核心字段（需强证据才能修改）
            # - 检测偏好冲突（如"喜欢猫" vs "猫毛过敏"）
            # - 置信度机制（新属性作为提案）
            validated_persona, conflicts = self._guardian.validate_update(
                current_persona, new_persona, memory_texts
            )
            
            # 写入文件
            path = self._get_profile_path(user_id)
            def _write():
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(validated_persona, f, ensure_ascii=False, indent=4)
            await loop.run_in_executor(self.executor, _write)
            
        except Exception as e:
            logger.error(f"Daily persona update error: {e}")
    
    async def update_interaction_stats(self, user_id):
        """
        更新用户互动统计（每次有效聊天后调用）
        
        有效聊天定义：用户消息 + AI回复 = 1次有效互动
        累计聊天天数：只要当天有聊天就计1天，无需连续
        
        Args:
            user_id: 用户ID
            
        Returns:
            dict: 更新后的 interaction_stats 字典
        """
        loop = asyncio.get_event_loop()
        profile = await self.get_user_profile(user_id)
        social = profile.get("social_graph", {})
        stats = social.get("interaction_stats", {})
        
        today = date.today().isoformat()
        last_date = stats.get("last_chat_date")
        
        # 更新有效聊天次数
        stats["total_valid_chats"] = stats.get("total_valid_chats", 0) + 1
        
        # 更新累计聊天天数（只要是新的一天就+1，无需连续）
        if last_date is None:
            # 首次聊天
            stats["first_chat_date"] = today
            stats["total_chat_days"] = 1
        elif last_date != today:
            # 新的一天聊天，累计天数+1（无需判断是否连续）
            stats["total_chat_days"] = stats.get("total_chat_days", 0) + 1
        # 如果 last_date == today，说明今天已经聊过，不重复计数
        
        stats["last_chat_date"] = today
        
        # 保存更新
        await self.update_user_profile(user_id, {
            "social_graph": {
                "interaction_stats": stats
            }
        })
        
        logger.debug(f"Engram: Updated interaction stats for {user_id}: days={stats.get('total_chat_days', 0)}, chats={stats.get('total_valid_chats', 0)}")
        
        return stats
