"""
记忆系统门面类 (Memory System Facade)

统一封装 MemoryManager 和 ProfileManager，提供与旧版 MemoryLogic 完全兼容的接口。
这是重构的过渡层，使 main.py 可以无缝切换到新架构。

主要功能：
- 组合 MemoryManager 和 ProfileManager
- 暴露统一的 API 接口
- 保持向后兼容性
"""

import os
from concurrent.futures import ThreadPoolExecutor
from astrbot.api import logger
from ..db_manager import DatabaseManager
from .memory_manager import MemoryManager
from .profile_manager import ProfileManager


class MemoryFacade:
    """
    记忆系统门面类
    
    提供与旧版 MemoryLogic 完全兼容的接口，内部委托给 MemoryManager 和 ProfileManager。
    """
    
    def __init__(self, context, config, data_dir):
        """
        初始化记忆系统门面
        
        Args:
            context: AstrBot API 上下文对象
            config: 插件配置字典
            data_dir: 数据目录路径
        """
        self.context = context
        self.config = config
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        
        # 共享的线程池
        self.executor = ThreadPoolExecutor(max_workers=4)
        
        # 数据库管理器
        self.db = DatabaseManager(self.data_dir)
        
        # 初始化 ProfileManager（先初始化，因为 MemoryManager 可能需要它）
        self._profile_manager = ProfileManager(
            context=context,
            config=config,
            data_dir=data_dir,
            executor=self.executor,
            db_manager=self.db
        )
        
        # 初始化 MemoryManager
        self._memory_manager = MemoryManager(
            context=context,
            config=config,
            data_dir=data_dir,
            executor=self.executor,
            db_manager=self.db,
            profile_manager=self._profile_manager
        )
        
        # 暴露常用属性（保持兼容性）
        self._is_shutdown = False
    
    # ========== 暴露 MemoryManager 的属性 ==========
    
    @property
    def last_chat_time(self):
        """最后聊天时间字典"""
        return self._memory_manager.last_chat_time
    
    @property
    def unsaved_msg_count(self):
        """未保存消息计数字典"""
        return self._memory_manager.unsaved_msg_count
    
    @property
    def collection(self):
        """ChromaDB 集合"""
        return self._memory_manager.collection
    
    # ========== 静态方法 ==========
    
    @staticmethod
    def _ensure_datetime(timestamp):
        """确保时间戳是 datetime 对象"""
        return MemoryManager._ensure_datetime(timestamp)
    
    def _is_valid_message_content(self, content: str) -> bool:
        """验证消息内容是否有效"""
        return self._memory_manager._is_valid_message_content(content)
    
    # ========== 生命周期管理 ==========
    
    def shutdown(self):
        """关闭记忆系统"""
        self._is_shutdown = True
        self._memory_manager.shutdown()
        self.executor.shutdown(wait=False)
    
    # ========== 记忆管理方法（委托给 MemoryManager） ==========
    
    async def _ensure_chroma_initialized(self):
        """确保 ChromaDB 已初始化"""
        return await self._memory_manager._ensure_chroma_initialized()
    
    async def record_message(self, user_id, session_id, role, content, msg_type="text", user_name=None):
        """记录原始消息"""
        return await self._memory_manager.record_message(
            user_id=user_id,
            session_id=session_id,
            role=role,
            content=content,
            msg_type=msg_type,
            user_name=user_name
        )
    
    async def check_and_summarize(self):
        """检查并归档"""
        return await self._memory_manager.check_and_summarize()
    
    async def _summarize_private_chat(self, user_id):
        """对私聊进行总结并存入长期记忆"""
        return await self._memory_manager._summarize_private_chat(user_id)
    
    async def retrieve_memories(self, user_id, query, limit=3):
        """检索相关记忆"""
        return await self._memory_manager.retrieve_memories(user_id, query, limit)
    
    async def get_memory_detail(self, user_id, sequence_num):
        """获取记忆详情（按序号）"""
        return await self._memory_manager.get_memory_detail(user_id, sequence_num)
    
    async def get_memory_detail_by_id(self, user_id, short_id):
        """获取记忆详情（按ID）"""
        return await self._memory_manager.get_memory_detail_by_id(user_id, short_id)
    
    async def delete_memory_by_sequence(self, user_id, sequence_num, delete_raw=False):
        """按序号删除记忆"""
        return await self._memory_manager.delete_memory_by_sequence(user_id, sequence_num, delete_raw)
    
    async def delete_memory_by_id(self, user_id, short_id, delete_raw=False):
        """按ID删除记忆"""
        return await self._memory_manager.delete_memory_by_id(user_id, short_id, delete_raw)
    
    async def undo_last_delete(self, user_id):
        """撤销最近一次删除"""
        return await self._memory_manager.undo_last_delete(user_id)
    
    async def export_raw_messages(self, user_id, format="jsonl", start_date=None, end_date=None, limit=None):
        """导出原始消息"""
        return await self._memory_manager.export_raw_messages(user_id, format, start_date, end_date, limit)
    
    async def export_all_users_messages(self, format="jsonl", start_date=None, end_date=None, limit=None):
        """导出所有用户消息"""
        return await self._memory_manager.export_all_users_messages(format, start_date, end_date, limit)

    async def summarize_all_users(self):
        """强制归档所有用户的未归档消息"""
        return await self._memory_manager.summarize_all_users()

    async def rebuild_vector_collection(self, full_rebuild: bool = False, batch_size: int = 200):
        """手动重建向量库"""
        return await self._memory_manager.rebuild_vector_collection(full_rebuild, batch_size)
    
    # ========== 用户画像方法（委托给 ProfileManager） ==========
    
    async def get_user_profile(self, user_id):
        """获取用户画像"""
        return await self._profile_manager.get_user_profile(user_id)
    
    async def update_user_profile(self, user_id, update_data):
        """更新用户画像"""
        return await self._profile_manager.update_user_profile(user_id, update_data)
    
    async def clear_user_profile(self, user_id):
        """清除用户画像"""
        return await self._profile_manager.clear_user_profile(user_id)
    
    async def _update_persona_daily(self, user_id, start_time=None, end_time=None):
        """每日画像深度更新"""
        return await self._profile_manager.update_persona_daily(user_id, start_time, end_time)
    
    async def _update_interaction_stats(self, user_id):
        """更新互动统计"""
        return await self._profile_manager.update_interaction_stats(user_id)
