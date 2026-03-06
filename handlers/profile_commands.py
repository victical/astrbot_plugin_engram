"""
画像命令处理器 (Profile Command Handler)

负责处理所有用户画像相关命令的业务逻辑。
从 main.py 提取而来，遵循单一职责原则。

主要功能：
- profile show: 显示用户画像
- profile clear: 清除画像
- profile set: 设置画像字段
- engram_force_persona: 强制更新画像
"""

import asyncio
import json
import datetime
from astrbot.api import logger


class ProfileCommandHandler:
    """画像命令处理器"""
    
    def __init__(self, config, profile_manager, db_manager, profile_renderer, executor):
        """
        初始化画像命令处理器
        
        Args:
            config: 插件配置
            profile_manager: ProfileManager 实例
            db_manager: DatabaseManager 实例
            profile_renderer: ProfileRenderer 实例
            executor: ThreadPoolExecutor 实例
        """
        self.config = config
        self.profile = profile_manager
        self.db = db_manager
        self.renderer = profile_renderer
        self.executor = executor
    
    async def handle_profile_show(self, user_id: str) -> tuple:
        """
        处理 profile show 命令
        
        Args:
            user_id: 用户ID
            
        Returns:
            tuple: (success: bool, result: bytes/str)
                   success=True 时 result 是图片字节
                   success=False 时 result 是错误消息或文本画像
        """
        profile = await self.profile.get_user_profile(user_id)
        
        if not profile or not profile.get("basic_info"):
            return (False, "👤 您当前还没有建立深度画像。")
        
        try:
            # 获取记忆数量
            loop = asyncio.get_event_loop()
            memories = await loop.run_in_executor(self.executor, self.db.get_memory_list, user_id, 100)
            memory_count = len(memories)
            
            # 渲染画像
            img_bytes = await self.renderer.render(user_id, profile, memory_count)
            
            return (True, img_bytes)
        except Exception as e:
            logger.error(f"Engram：画像渲染失败：{e}")
            import traceback
            logger.debug(traceback.format_exc())
            return (False, f"⚠️ 档案绘制失败，转为文本模式：\n{json.dumps(profile, indent=2, ensure_ascii=False)}")
    
    async def handle_profile_clear(self, user_id: str, confirm: str = "") -> str:
        """
        处理 profile clear 命令
        
        Args:
            user_id: 用户ID
            confirm: 确认参数
            
        Returns:
            str: 格式化的命令结果
        """
        if confirm != "confirm":
            return "⚠️ 危险操作：此指令将永久删除您的用户画像文件，所有侧写特征将被重置。\n\n如果您确定要执行，请发送：\n/profile clear confirm"
        
        await self.profile.clear_user_profile(user_id)
        return "🗑️ 您的用户画像已成功重置。"
    
    async def handle_profile_set(self, user_id: str, key: str, value: str) -> str:
        """
        处理 profile set 命令
        
        Args:
            user_id: 用户ID
            key: 画像字段路径（如 basic_info.job）
            value: 字段值
            
        Returns:
            str: 格式化的命令结果
        """
        keys = key.split('.')
        update_data = {}
        curr = update_data
        for k in keys[:-1]:
            curr[k] = {}
            curr = curr[k]
        curr[keys[-1]] = value
        
        await self.profile.update_user_profile(user_id, update_data)
        return f"✅ 已更新画像：{key} = {value}"
    
    def resolve_force_persona_days(self, days: str = "") -> tuple:
        """解析 engram_force_persona 的天数参数。"""
        if days and days.isdigit():
            days_int = int(days)
            if days_int <= 0:
                return False, "⚠️ 天数必须大于 0。", 0
            if days_int > 365:
                return False, "⚠️ 天数不能超过 365 天。", 0
        else:
            days_int = 3

        return True, "", days_int

    def build_force_persona_messages(self, days_int: int) -> tuple[str, str]:
        """构建 engram_force_persona 开始/完成文案。"""
        time_desc = f"前 {days_int} 天"
        return (
            f"⏳ 正在基于{time_desc}的记忆强制更新用户画像，请稍候...",
            f"✅ 画像更新完成（基于{time_desc}的记忆）。您可以使用 /profile show 查看。"
        )

    async def handle_force_persona(self, user_id: str, days_int: int) -> tuple:
        """
        执行 engram_force_persona 更新。

        Args:
            user_id: 用户ID
            days_int: 已校验的回溯天数

        Returns:
            tuple: (start_message, done_message)
        """
        now = datetime.datetime.now()
        start_time = (now - datetime.timedelta(days=days_int)).replace(hour=0, minute=0, second=0, microsecond=0)
        end_time = now

        await self.profile.update_persona_daily(user_id, start_time, end_time)
        return self.build_force_persona_messages(days_int)
