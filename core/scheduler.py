"""
调度器模块
从 main.py 中拆分出来，负责所有后台任务和定时调度
"""
import asyncio
import datetime
import time
from typing import Dict, Callable, Awaitable
from astrbot.api import logger


class MemoryScheduler:
    """记忆系统调度器 - 管理后台归档任务和每日画像更新"""
    
    def __init__(self, logic, config):
        """
        初始化调度器
        
        Args:
            logic: MemoryFacade 或 MemoryLogic 实例
            config: 配置对象
        """
        self.logic = logic
        self.config = config
        self._is_shutdown = False
    
    async def start(self):
        """启动所有调度任务"""
        asyncio.create_task(self.background_worker())
        asyncio.create_task(self.daily_persona_scheduler())
    
    def shutdown(self):
        """停止调度器"""
        self._is_shutdown = True
    
    async def background_worker(self):
        """智能休眠：根据最早需要处理的时间动态调整检测间隔"""
        while not self._is_shutdown:
            try:
                # 计算下一次需要检测的时间
                sleep_time = self._calculate_next_check_time()
                await asyncio.sleep(sleep_time)
                if self._is_shutdown or getattr(self.logic, "_is_shutdown", False):
                    break
                await self.logic.check_and_summarize()
            except Exception as e:
                if not self._is_shutdown:
                    logger.error(f"Engram background worker error: {e}")
    
    def _calculate_next_check_time(self) -> int:
        """计算下一次检测的休眠时间（秒）"""
        now_ts = time.time()
        timeout = self.config.get("private_memory_timeout", 1800)
        
        # 如果没有活跃用户，休眠较长时间（5分钟）
        if not self.logic.last_chat_time:
            return 300
        
        # 找出最早需要触发归档的时间
        earliest_trigger = float('inf')
        for user_id, last_time in self.logic.last_chat_time.items():
            if self.logic.unsaved_msg_count.get(user_id, 0) >= self.config.get("min_msg_count", 3):
                trigger_time = last_time + timeout
                earliest_trigger = min(earliest_trigger, trigger_time)
        
        if earliest_trigger == float('inf'):
            # 有用户但消息数不够，每2分钟检测一次
            return 120
        
        # 计算距离最早触发时间的秒数，最少30秒，最多5分钟
        wait_seconds = max(30, min(300, int(earliest_trigger - now_ts) + 5))
        return wait_seconds
    
    async def daily_persona_scheduler(self):
        """独立的每日画像更新调度器：精准在00:00执行，避免依赖轮询，支持并发控制"""
        while not self._is_shutdown:
            try:
                # 计算距离下一个00:00的秒数
                now = datetime.datetime.now()
                tomorrow = (now + datetime.timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                sleep_seconds = (tomorrow - now).total_seconds()
                
                logger.info(f"Engram: Daily persona update scheduled in {sleep_seconds/3600:.1f} hours")
                await asyncio.sleep(sleep_seconds)
                
                if self._is_shutdown or getattr(self.logic, "_is_shutdown", False):
                    break
                
                # 执行画像更新 - 带并发控制和延迟
                await self._execute_daily_persona_update()
                    
            except Exception as e:
                if not self._is_shutdown:
                    logger.error(f"Engram daily persona scheduler error: {e}")
                await asyncio.sleep(60)  # 出错后短暂休眠再重试
    
    async def _execute_daily_persona_update(self):
        """执行每日画像更新（带并发控制）"""
        min_memories = self.config.get("min_persona_update_memories", 3)
        max_concurrent = self.config.get("persona_update_max_concurrent", 3)
        update_delay = self.config.get("persona_update_delay", 5)
        
        # 创建信号量控制并发数
        semaphore = asyncio.Semaphore(max_concurrent)
        
        async def update_user_persona(user_id: str):
            """带并发控制的单用户画像更新"""
            async with semaphore:
                try:
                    # 关键修复：00:00 执行时应该查询的是【昨天】的记忆，而不是【今天】
                    # 因为 00:00 时"今天"刚开始，还没有任何记忆
                    now = datetime.datetime.now()
                    yesterday_start = (now - datetime.timedelta(days=1)).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    
                    loop = asyncio.get_event_loop()
                    # 查询昨天一整天的记忆（从昨天00:00到今天00:00）
                    memories = await loop.run_in_executor(
                        self.logic.executor,
                        lambda: self.logic.db.get_memories_in_range(
                            user_id, yesterday_start, today_start
                        )
                    )
                    
                    if len(memories) >= min_memories:
                        # 使用昨天的时间范围进行画像更新
                        await self.logic._update_persona_daily(
                            user_id, yesterday_start, today_start
                        )
                        logger.info(
                            f"Engram: Daily persona updated for {user_id} "
                            f"(memories from yesterday: {len(memories)})"
                        )
                        # 更新后延迟，避免瞬时压力
                        if update_delay > 0:
                            await asyncio.sleep(update_delay)
                    else:
                        logger.debug(
                            f"Engram: Skipped persona update for {user_id} "
                            f"(only {len(memories)} memories, need {min_memories})"
                        )
                except Exception as e:
                    logger.error(f"Engram: Failed to update persona for {user_id}: {e}")
        
        # 收集所有需要更新的用户
        user_ids = list(self.logic.last_chat_time.keys())
        if user_ids:
            logger.info(
                f"Engram: Starting daily persona update for {len(user_ids)} users "
                f"(max concurrent: {max_concurrent}, delay: {update_delay}s)"
            )
            
            # 并发执行所有用户的画像更新（受信号量限制）
            tasks = [update_user_persona(user_id) for user_id in user_ids]
            await asyncio.gather(*tasks, return_exceptions=True)
            
            logger.info(f"Engram: Daily persona update completed for {len(user_ids)} users")
