"""
记忆命令处理器 (Memory Command Handler)

负责处理所有记忆相关命令的业务逻辑。
从 main.py 提取而来，遵循单一职责原则。

主要功能：
- mem_list: 查看记忆列表
- mem_view: 查看记忆详情
- mem_search: 搜索记忆
- mem_delete: 删除记忆
- mem_undo: 撤销删除
- mem_clear_*: 各种清理命令

设计理念：
- 业务逻辑与装饰器分离
- 返回格式化的结果消息
- 异常处理统一化
"""

import asyncio
import json
from astrbot.api import logger


class MemoryCommandHandler:
    """记忆命令处理器"""
    
    def __init__(self, config, memory_manager, db_manager, executor):
        """
        初始化记忆命令处理器
        
        Args:
            config: 插件配置
            memory_manager: MemoryManager 实例
            db_manager: DatabaseManager 实例
            executor: ThreadPoolExecutor 实例
        """
        self.config = config
        self.memory = memory_manager
        self.db = db_manager
        self.executor = executor
    
    async def handle_mem_list(self, user_id: str, count: str = "") -> str:
        """
        处理 mem_list 命令
        
        Args:
            user_id: 用户ID
            count: 可选的记忆数量
            
        Returns:
            str: 格式化的命令结果
        """
        # 解析数量参数
        if count and count.isdigit():
            limit = int(count)
            if limit <= 0:
                return "⚠️ 数量必须大于 0。"
            elif limit > 50:
                return "⚠️ 单次最多查询 50 条记忆。"
        else:
            limit = self.config.get("list_memory_count", 5)
        
        loop = asyncio.get_event_loop()
        memories = await loop.run_in_executor(self.executor, self.db.get_memory_list, user_id, limit)
        
        if not memories:
            return "🧐 你目前还没有生成的长期记忆。"
        
        result = [f"📜 最近的 {len(memories)} 条长期记忆：\n" + "—" * 15]
        for i, m in enumerate(memories):
            created_at = self.memory._ensure_datetime(m.created_at)
            result.append(f"{i+1}. ⏰ {created_at.strftime('%m-%d %H:%M')}\n   📝 {m.summary}\n")
        
        result.append("\n💡 发送 /mem_view <序号> 可查看某条记忆的完整对话原文。")
        result.append("💡 发送 /mem_list <数量> 可自定义查询条数。")
        return "\n".join(result)
    
    async def handle_mem_view(self, user_id: str, index: str) -> str:
        """
        处理 mem_view 命令
        
        Args:
            user_id: 用户ID
            index: 记忆序号
            
        Returns:
            str: 格式化的命令结果
        """
        if not index.isdigit():
            return "⚠️ 请输入正确的序号，例如：/mem_view 1"
        
        seq = int(index)
        if seq <= 0:
            return "⚠️ 序号必须大于 0。"
        
        memory_index, raw_msgs = await self.memory.get_memory_detail(user_id, seq)
        
        if not memory_index:
            return raw_msgs  # 这里 raw_msgs 是错误提示字符串
        
        # 格式化输出
        created_at = self.memory._ensure_datetime(memory_index.created_at)
        result = [
            f"📖 记忆详情 (序号 {seq})",
            f"⏰ 时间：{created_at.strftime('%Y-%m-%d %H:%M')}",
            f"📝 归档：{memory_index.summary}",
            "————————————————",
            "🎙️ 原始对话回溯："
        ]
        
        if not raw_msgs:
            result.append("(暂无关联的原始对话数据)")
        else:
            for m in raw_msgs:
                if not self.memory._is_valid_message_content(m.content):
                    continue
                ts = self.memory._ensure_datetime(m.timestamp)
                time_str = ts.strftime("%H:%M:%S")
                role_name = "我" if m.role == "assistant" else (m.user_name or "你")
                result.append(f"[{time_str}] {role_name}: {m.content}")
        
        return "\n".join(result)
    
    async def handle_mem_search(self, user_id: str, query: str) -> str:
        """
        处理 mem_search 命令
        
        Args:
            user_id: 用户ID
            query: 搜索关键词
            
        Returns:
            str: 格式化的命令结果
        """
        memories = await self.memory.retrieve_memories(user_id, query, limit=3)
        
        if not memories:
            return f"🔍 未找到与 '{query}' 相关的记忆。"
        
        result = [f"🔍 搜索关键词 '{query}' 的结果（按相关性排序）：\n"] + memories
        result.append("\n💡 使用 /mem_delete <ID> 可根据记忆 ID 删除指定记忆。")
        return "\n".join(result)
    
    async def handle_mem_delete(self, user_id: str, index: str, delete_raw: bool = False) -> str:
        """
        处理 mem_delete 和 mem_delete_all 命令
        
        Args:
            user_id: 用户ID
            index: 记忆序号或ID
            delete_raw: 是否同时删除原始消息
            
        Returns:
            str: 格式化的命令结果
        """
        cmd_name = "mem_delete_all" if delete_raw else "mem_delete"
        
        # 智能判断：数字且 ≤ 50 使用序号删除，否则使用 ID 删除
        if index.isdigit():
            seq = int(index)
            if seq <= 0:
                return "⚠️ 序号必须大于 0。"
            if seq > 50:
                return "⚠️ 序号超过 50，请使用记忆 ID 进行删除。"
            
            # 按序号删除
            success, message, summary = await self.memory.delete_memory_by_sequence(user_id, seq, delete_raw=delete_raw)
            
            if success:
                if delete_raw:
                    return f"🗑️ 已彻底删除记忆 #{seq} 及其原始对话：\n📝 {summary[:50]}{'...' if len(summary) > 50 else ''}\n\n💡 如果误删，可使用 /mem_undo 撤销此操作。"
                else:
                    return f"🗑️ 已删除记忆 #{seq}：\n📝 {summary[:50]}{'...' if len(summary) > 50 else ''}\n\n💡 原始对话消息已保留，可重新归档。"
            else:
                return f"❌ {message}"
        else:
            # 按 ID 删除
            if len(index) < 8:
                return f"⚠️ 记忆 ID 至少需要 8 位，例如：/{cmd_name} a1b2c3d4"
            
            success, message, summary = await self.memory.delete_memory_by_id(user_id, index, delete_raw=delete_raw)
            
            if success:
                if delete_raw:
                    return f"🗑️ 已彻底删除记忆 ID {index[:8]} 及其原始对话：\n📝 {summary[:50]}{'...' if len(summary) > 50 else ''}\n\n💡 如果误删，可使用 /mem_undo 撤销此操作。"
                else:
                    return f"🗑️ 已删除记忆 ID {index[:8]}：\n📝 {summary[:50]}{'...' if len(summary) > 50 else ''}\n\n💡 原始对话消息已保留，可重新归档。"
            else:
                return f"❌ {message}"
    
    async def handle_mem_undo(self, user_id: str) -> str:
        """
        处理 mem_undo 命令
        
        Args:
            user_id: 用户ID
            
        Returns:
            str: 格式化的命令结果
        """
        success, message, summary = await self.memory.undo_last_delete(user_id)
        
        if success:
            return f"✅ 撤销成功！已恢复记忆：\n📝 {summary[:80]}{'...' if len(summary) > 80 else ''}\n\n💡 记忆已重新添加到您的记忆库中。"
        else:
            return f"❌ {message}"
    
    async def handle_mem_clear_raw(self, user_id: str, confirm: str = "") -> str:
        """
        处理 mem_clear_raw 命令
        
        Args:
            user_id: 用户ID
            confirm: 确认参数
            
        Returns:
            str: 格式化的命令结果
        """
        if confirm != "confirm":
            return "⚠️ 危险操作：此指令将永久删除您所有**尚未归档**的聊天原文，且不可恢复。\n\n如果您确定要执行，请发送：\n/mem_clear_raw confirm"
        
        loop = asyncio.get_event_loop()
        try:
            from ..db_manager import RawMemory
            def _clear_raw():
                with self.db.db.connection_context():
                    RawMemory.delete().where((RawMemory.user_id == user_id) & (RawMemory.is_archived == False)).execute()
            
            await loop.run_in_executor(self.executor, _clear_raw)
            # 重置内存计数
            self.memory.unsaved_msg_count[user_id] = 0
            return "🗑️ 已成功清除您所有未归档的原始对话消息。"
        except Exception as e:
            logger.error(f"Clear raw memory failed: {e}")
            return f"❌ 清除失败：{e}"
    
    async def handle_mem_clear_archive(self, user_id: str, confirm: str = "") -> str:
        """
        处理 mem_clear_archive 命令
        
        Args:
            user_id: 用户ID
            confirm: 确认参数
            
        Returns:
            str: 格式化的命令结果
        """
        if confirm != "confirm":
            return "⚠️ 危险操作：此指令将永久删除您所有的**长期记忆归档**及向量检索数据，但会保留原始聊天记录。\n\n如果您确定要执行，请发送：\n/mem_clear_archive confirm"
        
        loop = asyncio.get_event_loop()
        try:
            # 确保 ChromaDB 已初始化
            await self.memory._ensure_chroma_initialized()
            
            from ..db_manager import MemoryIndex, RawMemory
            def _clear_archive():
                with self.db.db.connection_context():
                    MemoryIndex.delete().where(MemoryIndex.user_id == user_id).execute()
                    RawMemory.update(is_archived=False).where(RawMemory.user_id == user_id).execute()
            
            await loop.run_in_executor(self.executor, _clear_archive)
            await loop.run_in_executor(self.executor, lambda: self.memory.collection.delete(where={"user_id": user_id}))
            
            return "🗑️ 已成功清除您所有的长期记忆归档，原始消息已重置为待归档状态。"
        except Exception as e:
            logger.error(f"Clear archive memory failed: {e}")
            return f"❌ 清除失败：{e}"
    
    async def handle_mem_clear_all(self, user_id: str, confirm: str = "") -> str:
        """
        处理 mem_clear_all 命令
        
        Args:
            user_id: 用户ID
            confirm: 确认参数
            
        Returns:
            str: 格式化的命令结果
        """
        if confirm != "confirm":
            return "⚠️ 警告：此指令将永久删除您所有的聊天原文、长期记忆归档及向量检索数据，且不可恢复。\n\n如果您确定要执行，请发送：\n/mem_clear_all confirm"
        
        loop = asyncio.get_event_loop()
        try:
            # 确保 ChromaDB 已初始化
            await self.memory._ensure_chroma_initialized()
            
            # 清除 SQLite 中的原始消息和索引
            await loop.run_in_executor(self.executor, self.db.clear_user_data, user_id)
            # 清除 ChromaDB 中的向量数据
            await loop.run_in_executor(self.executor, lambda: self.memory.collection.delete(where={"user_id": user_id}))
            # 重置内存计数
            self.memory.unsaved_msg_count[user_id] = 0
            
            return "🗑️ 已成功彻底清除您所有的原始对话消息和归档记忆。"
        except Exception as e:
            logger.error(f"Clear all memory failed: {e}")
            return f"❌ 清除失败：{e}"
    
    async def handle_force_summarize(self, user_id: str) -> tuple:
        """
        处理 engram_force_summarize 命令
        
        Args:
            user_id: 用户ID
            
        Returns:
            tuple: (开始消息, 完成消息)
        """
        await self.memory._summarize_private_chat(user_id)
        return ("⏳ 正在强制执行记忆归档，请稍候...", "✅ 记忆归档完成。您可以使用 /mem_list 查看。")
