"""
调度器模块
从 main.py 中拆分出来，负责所有后台任务和定时调度
"""
import asyncio
import datetime
import time
import random
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
        self._tasks = []  # 追踪后台任务
    
    async def start(self):
        """启动所有调度任务"""
        # 保存任务引用，以便关闭时取消
        task1 = asyncio.create_task(self.background_worker())
        task2 = asyncio.create_task(self.daily_persona_scheduler())
        task3 = asyncio.create_task(self.daily_memory_maintenance())
        task4 = asyncio.create_task(self.weekly_folding_task())
        task5 = asyncio.create_task(self.monthly_folding_task())
        self._tasks.extend([task1, task2, task3, task4, task5])
    
    def shutdown(self):
        """停止调度器（设置关闭标志）"""
        self._is_shutdown = True
    
    async def background_worker(self):
        """智能休眠：根据最早需要处理的时间动态调整检测间隔"""
        while not self._is_shutdown:
            try:
                # 计算下一次需要检测的时间
                sleep_time = self._calculate_next_check_time()
                await asyncio.sleep(sleep_time)
                
                # 关闭检查：在执行任何操作前检查状态
                if self._is_shutdown or getattr(self.logic, "_is_shutdown", False):
                    logger.debug("Engram: Background worker shutdown signal received")
                    break
                
                # 若线程池已关闭，直接退出
                if getattr(self.logic.executor, "_shutdown", False):
                    logger.debug("Engram: Executor shutdown detected in background worker")
                    self._is_shutdown = True
                    break
                
                await self.logic.check_and_summarize()
            except asyncio.CancelledError:
                # 任务被取消（插件关闭）
                logger.debug("Engram: Background worker task cancelled")
                break
            except Exception as e:
                # 线程池已关闭时，终止任务避免重复报错
                if "cannot schedule new futures after shutdown" in str(e):
                    logger.debug("Engram: Background worker detected executor shutdown via exception")
                    self._is_shutdown = True
                    break
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
                
                # 关闭检查：在执行更新前检查状态
                if self._is_shutdown or getattr(self.logic, "_is_shutdown", False):
                    logger.debug("Engram: Daily persona scheduler shutdown signal received")
                    break
                
                # 若线程池已关闭，直接退出
                if getattr(self.logic.executor, "_shutdown", False):
                    logger.debug("Engram: Executor shutdown detected in daily persona scheduler")
                    self._is_shutdown = True
                    break
                
                # 执行画像更新 - 带并发控制和延迟
                await self._execute_daily_persona_update()
                    
            except asyncio.CancelledError:
                # 任务被取消（插件关闭）
                logger.debug("Engram: Daily persona scheduler task cancelled")
                break
            except Exception as e:
                if "cannot schedule new futures after shutdown" in str(e):
                    logger.debug("Engram: Daily persona scheduler detected executor shutdown via exception")
                    self._is_shutdown = True
                    break
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
                    # 在执行前检查是否应该停止
                    if self._is_shutdown or getattr(self.logic, "_is_shutdown", False):
                        return
                    
                    # 检查线程池状态
                    if getattr(self.logic.executor, "_shutdown", False):
                        logger.debug(f"Engram: Skipping persona update for {user_id} - executor shutdown")
                        return
                    
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

    # ========== 周/月总结任务 ==========

    async def weekly_folding_task(self):
        """每周总结任务（周日 02:00）"""
        while not self._is_shutdown:
            try:
                now = datetime.datetime.now()
                # 找到下一个周日 02:00
                days_ahead = (6 - now.weekday()) % 7  # 周日=6
                next_run = (now + datetime.timedelta(days=days_ahead)).replace(
                    hour=2, minute=0, second=0, microsecond=0
                )
                if next_run <= now:
                    next_run += datetime.timedelta(days=7)

                sleep_seconds = (next_run - now).total_seconds()
                logger.info(f"Engram: Weekly folding scheduled in {sleep_seconds/3600:.1f} hours")
                await asyncio.sleep(sleep_seconds)

                if self._is_shutdown or getattr(self.logic, "_is_shutdown", False):
                    break
                if getattr(self.logic.executor, "_shutdown", False):
                    self._is_shutdown = True
                    break

                await self._execute_weekly_folding()
            except asyncio.CancelledError:
                logger.debug("Engram: Weekly folding task cancelled")
                break
            except Exception as e:
                if "cannot schedule new futures after shutdown" in str(e):
                    self._is_shutdown = True
                    break
                logger.error(f"Engram weekly folding error: {e}")
                await asyncio.sleep(60)

    async def monthly_folding_task(self):
        """每月总结任务（每月 1 号 03:00）"""
        while not self._is_shutdown:
            try:
                now = datetime.datetime.now()
                # 下一个月 1 号 03:00
                if now.day == 1 and now.hour < 3:
                    next_run = now.replace(hour=3, minute=0, second=0, microsecond=0)
                else:
                    year = now.year + (1 if now.month == 12 else 0)
                    month = 1 if now.month == 12 else now.month + 1
                    next_run = datetime.datetime(year, month, 1, 3, 0, 0)

                sleep_seconds = (next_run - now).total_seconds()
                logger.info(f"Engram: Monthly folding scheduled in {sleep_seconds/3600:.1f} hours")
                await asyncio.sleep(sleep_seconds)

                if self._is_shutdown or getattr(self.logic, "_is_shutdown", False):
                    break
                if getattr(self.logic.executor, "_shutdown", False):
                    self._is_shutdown = True
                    break

                await self._execute_monthly_folding()
            except asyncio.CancelledError:
                logger.debug("Engram: Monthly folding task cancelled")
                break
            except Exception as e:
                if "cannot schedule new futures after shutdown" in str(e):
                    self._is_shutdown = True
                    break
                logger.error(f"Engram monthly folding error: {e}")
                await asyncio.sleep(60)

    async def _execute_weekly_folding(self):
        """执行周总结（带抖动 + 串行）"""
        if not self.config.get("enable_memory_folding", True):
            return

        loop = asyncio.get_event_loop()
        user_ids = list(self.logic.last_chat_time.keys())
        if not user_ids:
            return

        # 抖动：0-1800 秒（推迟整个批次启动）
        jitter = random.randint(0, 1800)
        logger.info(f"Engram: Jittering {jitter}s before starting weekly fold batch")
        await asyncio.sleep(jitter)

        for user_id in user_ids:
            if self._is_shutdown or getattr(self.logic, "_is_shutdown", False):
                return
            if getattr(self.logic.executor, "_shutdown", False):
                return

            try:
                await self.logic.fold_weekly_summaries(user_id, days=7)
            except Exception as e:
                logger.error(f"Engram: Weekly fold failed for {user_id}: {e}")

            await asyncio.sleep(5)

    async def _execute_monthly_folding(self):
        """执行月总结（带抖动 + 串行）"""
        loop = asyncio.get_event_loop()
        user_ids = list(self.logic.last_chat_time.keys())
        if not user_ids:
            return

        # 抖动：0-1800 秒（推迟整个批次启动）
        jitter = random.randint(0, 1800)
        logger.info(f"Engram: Jittering {jitter}s before starting monthly fold batch")
        await asyncio.sleep(jitter)

        for user_id in user_ids:
            if self._is_shutdown or getattr(self.logic, "_is_shutdown", False):
                return
            if getattr(self.logic.executor, "_shutdown", False):
                return

            try:
                await self.logic.fold_monthly_summaries(user_id, days=30)
            except Exception as e:
                logger.error(f"Engram: Monthly fold failed for {user_id}: {e}")

            await asyncio.sleep(5)

    # ========== 记忆衰减与修剪 ==========

    async def daily_memory_maintenance(self):
        """每日记忆维护：衰减 active_score + 修剪冷记忆（在凌晨 01:00 执行，避开画像更新）"""
        while not self._is_shutdown:
            try:
                # 计算距离下一个 01:00 的秒数
                now = datetime.datetime.now()
                next_run = now.replace(hour=1, minute=0, second=0, microsecond=0)
                if now >= next_run:
                    next_run += datetime.timedelta(days=1)
                sleep_seconds = (next_run - now).total_seconds()

                logger.info(f"Engram: Memory maintenance scheduled in {sleep_seconds/3600:.1f} hours")
                await asyncio.sleep(sleep_seconds)

                if self._is_shutdown or getattr(self.logic, "_is_shutdown", False):
                    break
                if getattr(self.logic.executor, "_shutdown", False):
                    self._is_shutdown = True
                    break

                await self._execute_memory_maintenance()

            except asyncio.CancelledError:
                logger.debug("Engram: Memory maintenance task cancelled")
                break
            except Exception as e:
                if "cannot schedule new futures after shutdown" in str(e):
                    self._is_shutdown = True
                    break
                if not self._is_shutdown:
                    logger.error(f"Engram memory maintenance error: {e}")
                await asyncio.sleep(60)

    async def _execute_memory_maintenance(self):
        """执行衰减 + 修剪"""
        loop = asyncio.get_event_loop()

        enable_decay = self.config.get("enable_memory_decay", True)
        decay_rate = self.config.get("memory_decay_rate", 1)
        enable_prune = self.config.get("enable_memory_prune", True)
        prune_threshold = self.config.get("memory_prune_threshold", 0)

        # 1. Decay：全局衰减
        if enable_decay and decay_rate > 0:
            try:
                await loop.run_in_executor(
                    self.logic.executor,
                    self.logic.db.decay_active_scores,
                    decay_rate
                )
                logger.info(f"Engram: Decayed all memory active_scores by {decay_rate}")
            except Exception as e:
                logger.error(f"Engram: Memory decay failed: {e}")

        # 2. Prune：从 ChromaDB 删除冷记忆（SQLite 保留）
        if enable_prune:
            try:
                cold_ids = await loop.run_in_executor(
                    self.logic.executor,
                    self.logic.db.get_cold_memory_ids,
                    prune_threshold
                )
                if cold_ids:
                    # 确保 ChromaDB 已初始化
                    await self.logic._ensure_chroma_initialized()
                    
                    # 批量从 ChromaDB 删除（每批最多 100 条，避免单次操作过大）
                    pruned = 0
                    for i in range(0, len(cold_ids), 100):
                        batch = cold_ids[i:i+100]
                        try:
                            await loop.run_in_executor(
                                self.logic.executor,
                                lambda ids=batch: self.logic.collection.delete(ids=ids)
                            )
                            pruned += len(batch)
                        except Exception as e:
                            logger.warning(f"Engram: Failed to prune batch {i//100+1}: {e}")

                    logger.info(f"Engram: Pruned {pruned}/{len(cold_ids)} cold memories from ChromaDB (threshold={prune_threshold})")
                else:
                    logger.debug("Engram: No cold memories to prune")
            except Exception as e:
                logger.error(f"Engram: Memory prune failed: {e}")
