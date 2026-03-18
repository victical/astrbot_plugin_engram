"""
导出功能处理模块
负责处理原始消息的导出逻辑和命令
"""
import os
import asyncio
import datetime
from astrbot.api import logger


class ExportHandler:
    """处理消息导出的所有逻辑"""
    
    def __init__(self, logic, plugin_data_dir):
        self.logic = logic
        self.plugin_data_dir = plugin_data_dir
        self.export_dir = os.path.join(plugin_data_dir, "exports")
        os.makedirs(self.export_dir, exist_ok=True)
    
    async def handle_export_command(self, event, format: str = "jsonl", days: str = ""):
        """处理导出命令
        
        参数:
            event: 消息事件
            format: 导出格式 (jsonl, json, txt, alpaca, sharegpt)
            days: 导出最近N天的数据（可选，留空则导出全部）
        """
        user_id = event.get_sender_id()
        
        # 支持的格式
        supported_formats = ["jsonl", "json", "txt", "alpaca", "sharegpt"]
        if format not in supported_formats:
            yield event.plain_result(f"⚠️ 不支持的格式。支持的格式：{', '.join(supported_formats)}")
            return
        
        # 解析天数参数
        start_date = None
        if days and days.isdigit():
            days_int = int(days)
            if days_int <= 0:
                yield event.plain_result("⚠️ 天数必须大于 0。")
                return
            start_date = datetime.datetime.now() - datetime.timedelta(days=days_int)
        
        yield event.plain_result(f"⏳ 正在导出数据（格式：{format}），请稍候...")
        
        # 调用导出逻辑
        success, data, stats = await self.logic.export_raw_messages(
            user_id=user_id,
            format=format,
            start_date=start_date
        )
        
        if not success:
            yield event.plain_result(f"❌ {data}")
            return
        
        # 生成文件名
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        file_ext = format if format in ["jsonl", "json", "txt"] else "json"
        filename = f"engram_export_{user_id}_{timestamp}.{file_ext}"
        export_path = os.path.join(self.export_dir, filename)
        
        try:
            with open(export_path, 'w', encoding='utf-8') as f:
                f.write(data)
            
            # 构建统计信息
            stats_text = self._build_export_stats(stats, format, export_path)
            yield event.plain_result(stats_text)
            
        except Exception as e:
            logger.error(f"Engram：保存导出文件失败：{e}")
            yield event.plain_result(f"❌ 保存文件失败：{e}")
    
    async def handle_stats_command(self, event):
        """处理统计命令"""
        user_id = event.get_sender_id()

        loop = asyncio.get_event_loop()

        # 获取当前用户统计
        user_stats = await loop.run_in_executor(
            self.logic.executor,
            self.logic.db.get_message_stats,
            user_id
        )

        # 获取全局统计
        global_stats = await loop.run_in_executor(
            self.logic.executor,
            self.logic.db.get_all_users_stats
        )

        # 长期记忆索引统计
        def _count_user_indexes(uid):
            MemoryIndex = self.logic.db.MemoryIndex
            with self.logic.db.db.connection_context():
                return MemoryIndex.select().where(MemoryIndex.user_id == uid).count()

        def _count_all_indexes():
            MemoryIndex = self.logic.db.MemoryIndex
            with self.logic.db.db.connection_context():
                return MemoryIndex.select().count()

        user_memory_indexes = await loop.run_in_executor(
            self.logic.executor,
            _count_user_indexes,
            user_id
        )
        all_memory_indexes = await loop.run_in_executor(
            self.logic.executor,
            _count_all_indexes
        )

        result = f"""
📊 消息统计

【当前用户】
💬 原始消息：
- 总计：{user_stats.get('total', 0)} 条
- 已归档：{user_stats.get('archived', 0)} 条
- 未归档：{user_stats.get('unarchived', 0)} 条

🧠 长期记忆索引：
- 总计：{user_memory_indexes} 条

👥 角色分布：
- 用户消息：{user_stats.get('user_messages', 0)} 条
- 助手消息：{user_stats.get('assistant_messages', 0)} 条

【全局统计】
👤 用户数：{global_stats.get('user_count', 0)} 人
💬 原始消息：
- 总计：{global_stats.get('total', 0)} 条
- 已归档：{global_stats.get('archived', 0)} 条
- 未归档：{global_stats.get('unarchived', 0)} 条

🧠 长期记忆索引：
- 总计：{all_memory_indexes} 条

👥 角色分布：
- 用户消息：{global_stats.get('user_messages', 0)} 条
- 助手消息：{global_stats.get('assistant_messages', 0)} 条

💡 使用 /mem_export 可导出数据用于模型微调
"""
        yield event.plain_result(result.strip())
    
    async def handle_export_all_command(self, event, format: str = "jsonl", days: str = ""):
        """处理导出所有用户数据命令（管理员专用）
        
        参数:
            event: 消息事件
            format: 导出格式 (jsonl, json, txt, alpaca, sharegpt)
            days: 导出最近N天的数据（可选，留空则导出全部）
        """
        # 支持的格式
        supported_formats = ["jsonl", "json", "txt", "alpaca", "sharegpt"]
        if format not in supported_formats:
            yield event.plain_result(f"⚠️ 不支持的格式。支持的格式：{', '.join(supported_formats)}")
            return
        
        # 解析天数参数
        start_date = None
        if days and days.isdigit():
            days_int = int(days)
            if days_int <= 0:
                yield event.plain_result("⚠️ 天数必须大于 0。")
                return
            start_date = datetime.datetime.now() - datetime.timedelta(days=days_int)
        
        yield event.plain_result(f"⏳ 正在导出所有用户数据（格式：{format}），请稍候...")
        
        # 调用导出逻辑
        success, data, stats = await self.logic.export_all_users_messages(
            format=format,
            start_date=start_date
        )
        
        if not success:
            yield event.plain_result(f"❌ {data}")
            return
        
        # 生成文件名
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        file_ext = format if format in ["jsonl", "json", "txt"] else "json"
        filename = f"engram_export_all_users_{timestamp}.{file_ext}"
        export_path = os.path.join(self.export_dir, filename)
        
        try:
            with open(export_path, 'w', encoding='utf-8') as f:
                f.write(data)
            
            # 构建统计信息
            stats_text = self._build_export_all_stats(stats, format, export_path)
            yield event.plain_result(stats_text)
            
        except Exception as e:
            logger.error(f"Engram：保存导出文件失败：{e}")
            yield event.plain_result(f"❌ 保存文件失败：{e}")
    
    def _build_export_all_stats(self, stats, format, export_path):
        """构建所有用户导出统计信息文本"""
        return f"""
📦 导出成功！

📊 统计信息：
- 用户数：{stats.get('user_count', 0)}
- 总消息数：{stats.get('total', 0)}
- 已导出：{stats.get('exported', 0)}
- 用户消息：{stats.get('user_messages', 0)}
- 助手消息：{stats.get('assistant_messages', 0)}

💾 文件信息：
- 格式：{format}
- 保存路径：{export_path}

💡 格式说明：
- jsonl: 每行一个JSON对象（通用格式）
- json: JSON数组格式（通用格式）
- txt: 纯文本对话格式（人类可读）
- alpaca: Alpaca指令微调格式
- sharegpt: ShareGPT对话格式
""".strip()
    
    def _build_export_stats(self, stats, format, export_path):
        """构建导出统计信息文本"""
        return f"""
📦 导出成功！

📊 统计信息：
- 总消息数：{stats.get('total', 0)}
- 已导出：{stats.get('exported', 0)}
- 用户消息：{stats.get('user_messages', 0)}
- 助手消息：{stats.get('assistant_messages', 0)}
- 已归档：{stats.get('archived', 0)}
- 未归档：{stats.get('unarchived', 0)}

💾 文件信息：
- 格式：{format}
- 保存路径：{export_path}

💡 格式说明：
- jsonl: 每行一个JSON对象（通用格式）
- json: JSON数组格式（通用格式）
- txt: 纯文本对话格式（人类可读）
- alpaca: Alpaca指令微调格式
- sharegpt: ShareGPT对话格式
""".strip()
