"""
调度器模块
从 main.py 中拆分出来，负责所有后台任务和定时调度
"""
import asyncio
import calendar
import datetime
import random
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
        self._tasks = []  # 追踪后台任务
        self._task_metrics = {}  # 任务可观测指标：耗时/成功率/失败率/跳过原因

    def _push_activity(self, title: str, *, category: str = "task", source: str = "private", meta: dict | None = None):
        manager = getattr(self.logic, "_memory_manager", None)
        if manager and hasattr(manager, "add_activity"):
            manager.add_activity(title=title, category=category, source=source, meta=meta)
    
    async def start(self):
        """启动所有调度任务"""
        if callable(getattr(self.logic, "ensure_pending_vector_retry_started", None)):
            task0 = asyncio.create_task(self.logic.ensure_pending_vector_retry_started())
            self._tasks.append(task0)

        # 保存任务引用，以便关闭时取消
        task1 = asyncio.create_task(self.background_worker())
        self._tasks.append(task1)

        if callable(getattr(self.logic, "_update_persona_daily", None)):
            task2 = asyncio.create_task(self.daily_persona_scheduler())
            self._tasks.append(task2)

        if callable(getattr(self.logic, "_ensure_chroma_initialized", None)):
            task3 = asyncio.create_task(self.daily_memory_maintenance())
            self._tasks.append(task3)

        if self.config.get("enable_memory_folding", True) and callable(getattr(self.logic, "fold_weekly_summaries", None)):
            task4 = asyncio.create_task(self.weekly_folding_scheduler())
            self._tasks.append(task4)

        if self.config.get("enable_monthly_folding", True) and callable(getattr(self.logic, "fold_monthly_summaries", None)):
            task5 = asyncio.create_task(self.monthly_folding_scheduler())
            self._tasks.append(task5)

        if self.config.get("enable_yearly_folding", True) and callable(getattr(self.logic, "fold_yearly_summaries", None)):
            task6 = asyncio.create_task(self.yearly_folding_scheduler())
            self._tasks.append(task6)
    
    def shutdown(self):
        """停止调度器（设置关闭标志）"""
        self._is_shutdown = True

    def _get_metric(self, task_name: str) -> dict:
        metric = self._task_metrics.get(task_name)
        if metric is None:
            metric = {
                "runs_total": 0,
                "success_total": 0,
                "fail_total": 0,
                "skip_total": 0,
                "total_duration_ms": 0.0,
                "last_duration_ms": 0.0,
                "last_run_at": "",
                "last_error": "",
                "last_skip_reason": "",
            }
            self._task_metrics[task_name] = metric
        return metric

    def _observe_skip(self, task_name: str, reason: str):
        metric = self._get_metric(task_name)
        metric["skip_total"] += 1
        metric["last_skip_reason"] = reason
        logger.debug(
            "Engram 调度器[%s] 跳过：原因=%s 运行=%d 成功=%d 失败=%d 跳过=%d",
            task_name,
            reason,
            metric["runs_total"],
            metric["success_total"],
            metric["fail_total"],
            metric["skip_total"],
        )

    def _observe_run(self, task_name: str, started_at: float, success: bool, error: Exception = None):
        metric = self._get_metric(task_name)
        duration_ms = (time.perf_counter() - started_at) * 1000.0
        metric["runs_total"] += 1
        metric["total_duration_ms"] += duration_ms
        metric["last_duration_ms"] = round(duration_ms, 2)
        metric["last_run_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if success:
            metric["success_total"] += 1
            metric["last_error"] = ""
        else:
            metric["fail_total"] += 1
            metric["last_error"] = str(error)[:300] if error else "unknown"

        runs = max(1, metric["runs_total"])
        fail_rate = metric["fail_total"] / runs
        avg_duration = metric["total_duration_ms"] / runs
        logger.debug(
            "Engram 调度器[%s] 执行：成功=%s 耗时ms=%.2f 平均ms=%.2f 失败率=%.2f%% 运行=%d 跳过=%d",
            task_name,
            success,
            duration_ms,
            avg_duration,
            fail_rate * 100,
            metric["runs_total"],
            metric["skip_total"],
        )

    async def background_worker(self):
        """智能休眠：根据最早需要处理的时间动态调整检测间隔"""
        task_name = "background_worker"
        while not self._is_shutdown:
            try:
                # 计算下一次需要检测的时间
                sleep_time = self._calculate_next_check_time()
                await asyncio.sleep(sleep_time)

                # 关闭检查：在执行任何操作前检查状态
                if self._is_shutdown or getattr(self.logic, "_is_shutdown", False):
                    self._observe_skip(task_name, "shutdown_signal")
                    logger.debug("Engram：后台归档任务收到关闭信号")
                    break

                # 若线程池已关闭，直接退出
                if getattr(self.logic.executor, "_shutdown", False):
                    self._observe_skip(task_name, "executor_shutdown")
                    logger.debug("Engram：后台归档任务检测到执行器已关闭")
                    self._is_shutdown = True
                    break

                started_at = time.perf_counter()
                try:
                    await self.logic.check_and_summarize()
                    self._observe_run(task_name, started_at, True)
                except Exception as e:
                    self._observe_run(task_name, started_at, False, e)
                    raise
            except asyncio.CancelledError:
                # 任务被取消（插件关闭）
                self._observe_skip(task_name, "task_cancelled")
                logger.debug("Engram：后台归档任务已取消")
                break
            except Exception as e:
                # 线程池已关闭时，终止任务避免重复报错
                if "cannot schedule new futures after shutdown" in str(e):
                    self._observe_skip(task_name, "executor_shutdown_exception")
                    logger.debug("Engram：后台归档任务通过异常检测到执行器已关闭")
                    self._is_shutdown = True
                    break
                if not self._is_shutdown:
                    logger.error(f"Engram 后台归档任务异常：{e}")
    
    def _calculate_next_check_time(self) -> int:
        """计算下一次检测的休眠时间（秒）"""
        now_ts = time.time()
        timeout = self.logic._get_archive_timeout() if hasattr(self.logic, "_get_archive_timeout") else self.config.get("private_memory_timeout", 1800)
        min_count = self.logic._get_archive_min_msg_count() if hasattr(self.logic, "_get_archive_min_msg_count") else self.config.get("min_msg_count", 3)
        
        # 如果没有活跃用户，休眠较长时间（5分钟）
        if not self.logic.last_chat_time:
            return 300
        
        # 找出最早需要触发归档的时间
        earliest_trigger = float('inf')
        for user_id, last_time in self.logic.last_chat_time.items():
            if self.logic.unsaved_msg_count.get(user_id, 0) >= min_count:
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
        task_name = "daily_persona_scheduler"
        while not self._is_shutdown:
            try:
                # 计算距离下一个00:00的秒数
                now = datetime.datetime.now()
                tomorrow = (now + datetime.timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                sleep_seconds = (tomorrow - now).total_seconds()

                logger.info(f"Engram：每日画像更新已调度，距离执行约 {sleep_seconds/3600:.1f} 小时")
                await asyncio.sleep(sleep_seconds)

                # 关闭检查：在执行更新前检查状态
                if self._is_shutdown or getattr(self.logic, "_is_shutdown", False):
                    self._observe_skip(task_name, "shutdown_signal")
                    logger.debug("Engram：每日画像调度器收到关闭信号")
                    break

                # 若线程池已关闭，直接退出
                if getattr(self.logic.executor, "_shutdown", False):
                    self._observe_skip(task_name, "executor_shutdown")
                    logger.debug("Engram：每日画像调度器检测到执行器已关闭")
                    self._is_shutdown = True
                    break

                # 执行画像更新 - 带并发控制和延迟
                started_at = time.perf_counter()
                try:
                    await self._execute_daily_persona_update()
                    self._observe_run(task_name, started_at, True)
                except Exception as e:
                    self._observe_run(task_name, started_at, False, e)
                    raise

            except asyncio.CancelledError:
                # 任务被取消（插件关闭）
                self._observe_skip(task_name, "task_cancelled")
                logger.debug("Engram：每日画像调度器任务已取消")
                break
            except Exception as e:
                if "cannot schedule new futures after shutdown" in str(e):
                    self._observe_skip(task_name, "executor_shutdown_exception")
                    logger.debug("Engram：每日画像调度器通过异常检测到执行器已关闭")
                    self._is_shutdown = True
                    break
                if not self._is_shutdown:
                    logger.error(f"Engram 每日画像调度器异常：{e}")
                await asyncio.sleep(60)  # 出错后短暂休眠再重试
    
    async def _execute_daily_persona_update(self):
        """执行每日画像更新（带并发控制）"""
        task_name = "execute_daily_persona_update"
        min_memories = self.config.get("min_persona_update_memories", 3)
        max_concurrent = self.config.get("persona_update_max_concurrent", 3)
        update_delay = self.config.get("persona_update_delay", 5)
        had_error = False

        # 创建信号量控制并发数
        semaphore = asyncio.Semaphore(max_concurrent)

        async def update_user_persona(user_id: str):
            """带并发控制的单用户画像更新"""
            nonlocal had_error
            async with semaphore:
                try:
                    # 在执行前检查是否应该停止
                    if self._is_shutdown or getattr(self.logic, "_is_shutdown", False):
                        self._observe_skip(task_name, "shutdown_during_user_update")
                        return

                    # 检查线程池状态
                    if getattr(self.logic.executor, "_shutdown", False):
                        self._observe_skip(task_name, "executor_shutdown_during_user_update")
                        logger.debug(f"Engram：跳过用户 {user_id} 的画像更新（执行器已关闭）")
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
                            f"Engram：已完成用户 {user_id} 的每日画像更新"
                            f"（昨日记忆数：{len(memories)}）"
                        )
                        # 更新后延迟，避免瞬时压力
                        if update_delay > 0:
                            await asyncio.sleep(update_delay)
                    else:
                        self._observe_skip(task_name, "insufficient_memories_for_persona_update")
                        logger.debug(
                            f"Engram：已跳过用户 {user_id} 的画像更新"
                            f"（记忆仅 {len(memories)} 条，至少需要 {min_memories} 条）"
                        )
                except Exception as e:
                    had_error = True
                    logger.error(f"Engram：用户 {user_id} 画像更新失败：{e}")

        # 收集所有需要更新的用户
        user_ids = list(self.logic.last_chat_time.keys())
        if user_ids:
            started_at = time.perf_counter()
            logger.info(
                f"Engram：开始执行每日画像更新，用户数={len(user_ids)} "
                f"（最大并发：{max_concurrent}，间隔：{update_delay}s）"
            )

            # 并发执行所有用户的画像更新（受信号量限制）
            tasks = [update_user_persona(user_id) for user_id in user_ids]
            await asyncio.gather(*tasks, return_exceptions=True)

            logger.info(f"Engram：每日画像更新完成，处理用户数={len(user_ids)}")
            if had_error:
                self._observe_run(task_name, started_at, False, RuntimeError("daily_persona_partial_failure"))
                self._push_activity(
                    title=f"每日画像更新部分失败（{len(user_ids)} 用户）",
                    meta={"users": len(user_ids)},
                )
            else:
                self._observe_run(task_name, started_at, True)
                self._push_activity(
                    title=f"每日画像更新完成（{len(user_ids)} 用户）",
                    meta={"users": len(user_ids)},
                )
        else:
            self._observe_skip(task_name, "no_active_users")

    def _calculate_next_monthly_run(self, now: datetime.datetime, run_day: int, run_hour: int) -> datetime.datetime:
        """计算下一次月总结执行时间。"""
        run_day = max(1, min(31, int(run_day)))
        run_hour = max(0, min(23, int(run_hour)))

        curr_last_day = calendar.monthrange(now.year, now.month)[1]
        curr_day = min(run_day, curr_last_day)
        candidate = now.replace(day=curr_day, hour=run_hour, minute=0, second=0, microsecond=0)
        if candidate > now:
            return candidate

        # 下个月
        if now.month == 12:
            year, month = now.year + 1, 1
        else:
            year, month = now.year, now.month + 1

        next_last_day = calendar.monthrange(year, month)[1]
        next_day = min(run_day, next_last_day)
        return datetime.datetime(year, month, next_day, run_hour, 0, 0)

    def _calculate_next_yearly_run(
        self,
        now: datetime.datetime,
        run_month: int,
        run_day: int,
        run_hour: int
    ) -> datetime.datetime:
        """计算下一次年度总结执行时间。"""
        run_month = max(1, min(12, int(run_month)))
        run_day = max(1, min(31, int(run_day)))
        run_hour = max(0, min(23, int(run_hour)))

        curr_last_day = calendar.monthrange(now.year, run_month)[1]
        curr_day = min(run_day, curr_last_day)
        candidate = datetime.datetime(now.year, run_month, curr_day, run_hour, 0, 0)
        if candidate > now:
            return candidate

        next_last_day = calendar.monthrange(now.year + 1, run_month)[1]
        next_day = min(run_day, next_last_day)
        return datetime.datetime(now.year + 1, run_month, next_day, run_hour, 0, 0)

    async def weekly_folding_scheduler(self):
        """每周调度周总结折叠任务（默认周日 02:00）"""
        task_name = "weekly_folding_scheduler"
        run_weekday = int(self.config.get("weekly_folding_weekday", 6))  # 0=周一, 6=周日
        run_hour = int(self.config.get("weekly_folding_hour", 2))

        while not self._is_shutdown:
            try:
                now = datetime.datetime.now()
                next_run = now.replace(hour=run_hour, minute=0, second=0, microsecond=0)

                days_ahead = (run_weekday - now.weekday()) % 7
                if days_ahead == 0 and now >= next_run:
                    days_ahead = 7
                next_run = (now + datetime.timedelta(days=days_ahead)).replace(
                    hour=run_hour,
                    minute=0,
                    second=0,
                    microsecond=0
                )

                sleep_seconds = max(1, int((next_run - now).total_seconds()))
                logger.info(
                    "Engram：周折叠已调度，距离执行约 %.1f 小时",
                    sleep_seconds / 3600
                )
                await asyncio.sleep(sleep_seconds)

                if self._is_shutdown or getattr(self.logic, "_is_shutdown", False):
                    self._observe_skip(task_name, "shutdown_signal")
                    break
                if getattr(self.logic.executor, "_shutdown", False):
                    self._observe_skip(task_name, "executor_shutdown")
                    self._is_shutdown = True
                    break

                started_at = time.perf_counter()
                try:
                    await self._execute_weekly_folding()
                    self._observe_run(task_name, started_at, True)
                except Exception as e:
                    self._observe_run(task_name, started_at, False, e)
                    raise
            except asyncio.CancelledError:
                self._observe_skip(task_name, "task_cancelled")
                logger.debug("Engram：周折叠调度器任务已取消")
                break
            except Exception as e:
                if "cannot schedule new futures after shutdown" in str(e):
                    self._observe_skip(task_name, "executor_shutdown_exception")
                    self._is_shutdown = True
                    break
                if not self._is_shutdown:
                    logger.error(f"Engram 周折叠调度器异常：{e}")
                await asyncio.sleep(60)

    async def monthly_folding_scheduler(self):
        """每月调度月总结折叠任务（默认每月 1 号 03:00）。"""
        task_name = "monthly_folding_scheduler"
        run_day = int(self.config.get("monthly_folding_day", 1))
        run_hour = int(self.config.get("monthly_folding_hour", 3))

        while not self._is_shutdown:
            try:
                now = datetime.datetime.now()
                next_run = self._calculate_next_monthly_run(now, run_day, run_hour)
                sleep_seconds = max(1, int((next_run - now).total_seconds()))

                logger.info(
                    "Engram：月折叠已调度，距离执行约 %.1f 小时",
                    sleep_seconds / 3600
                )
                await asyncio.sleep(sleep_seconds)

                if self._is_shutdown or getattr(self.logic, "_is_shutdown", False):
                    self._observe_skip(task_name, "shutdown_signal")
                    break
                if getattr(self.logic.executor, "_shutdown", False):
                    self._observe_skip(task_name, "executor_shutdown")
                    self._is_shutdown = True
                    break

                started_at = time.perf_counter()
                try:
                    await self._execute_monthly_folding()
                    self._observe_run(task_name, started_at, True)
                except Exception as e:
                    self._observe_run(task_name, started_at, False, e)
                    raise
            except asyncio.CancelledError:
                self._observe_skip(task_name, "task_cancelled")
                logger.debug("Engram：月折叠调度器任务已取消")
                break
            except Exception as e:
                if "cannot schedule new futures after shutdown" in str(e):
                    self._observe_skip(task_name, "executor_shutdown_exception")
                    self._is_shutdown = True
                    break
                if not self._is_shutdown:
                    logger.error(f"Engram 月折叠调度器异常：{e}")
                await asyncio.sleep(60)

    async def yearly_folding_scheduler(self):
        """每年调度年度总结折叠任务（默认每年 1 月 1 日 04:00）。"""
        task_name = "yearly_folding_scheduler"
        run_month = int(self.config.get("yearly_folding_month", 1))
        run_day = int(self.config.get("yearly_folding_day", 1))
        run_hour = int(self.config.get("yearly_folding_hour", 4))

        while not self._is_shutdown:
            try:
                now = datetime.datetime.now()
                next_run = self._calculate_next_yearly_run(now, run_month, run_day, run_hour)
                sleep_seconds = max(1, int((next_run - now).total_seconds()))

                logger.info(
                    "Engram：年折叠已调度，距离执行约 %.1f 小时",
                    sleep_seconds / 3600
                )
                await asyncio.sleep(sleep_seconds)

                if self._is_shutdown or getattr(self.logic, "_is_shutdown", False):
                    self._observe_skip(task_name, "shutdown_signal")
                    break
                if getattr(self.logic.executor, "_shutdown", False):
                    self._observe_skip(task_name, "executor_shutdown")
                    self._is_shutdown = True
                    break

                started_at = time.perf_counter()
                try:
                    await self._execute_yearly_folding()
                    self._observe_run(task_name, started_at, True)
                except Exception as e:
                    self._observe_run(task_name, started_at, False, e)
                    raise
            except asyncio.CancelledError:
                self._observe_skip(task_name, "task_cancelled")
                logger.debug("Engram：年折叠调度器任务已取消")
                break
            except Exception as e:
                if "cannot schedule new futures after shutdown" in str(e):
                    self._observe_skip(task_name, "executor_shutdown_exception")
                    self._is_shutdown = True
                    break
                if not self._is_shutdown:
                    logger.error(f"Engram 年折叠调度器异常：{e}")
                await asyncio.sleep(60)

    async def _execute_weekly_folding(self):
        """执行所有活跃用户的周总结折叠"""
        task_name = "execute_weekly_folding"
        if not self.config.get("enable_memory_folding", True):
            self._observe_skip(task_name, "weekly_folding_disabled")
            return

        folding_days = int(self.config.get("weekly_folding_days", 7))
        delay = int(self.config.get("weekly_folding_delay", 1))
        jitter = int(self.config.get("weekly_folding_jitter", 0))

        user_ids = list(self.logic.last_chat_time.keys())
        if not user_ids:
            self._observe_skip(task_name, "no_active_users")
            return

        had_error = False
        started_at = time.perf_counter()
        for user_id in user_ids:
            if self._is_shutdown or getattr(self.logic, "_is_shutdown", False):
                self._observe_skip(task_name, "shutdown_during_iteration")
                break

            try:
                await self.logic.fold_weekly_summaries(user_id, days=folding_days)
            except Exception as e:
                had_error = True
                logger.error(f"Engram：用户 {user_id} 周折叠失败：{e}")

            if delay > 0 or jitter > 0:
                await asyncio.sleep(max(0, delay) + max(0, random.randint(0, jitter)))

        if had_error:
            self._observe_run(task_name, started_at, False, RuntimeError("weekly_folding_partial_failure"))
            self._push_activity(
                title=f"周折叠部分失败（{len(user_ids)} 用户）",
                meta={"users": len(user_ids)},
            )
        else:
            self._observe_run(task_name, started_at, True)
            self._push_activity(
                title=f"周折叠完成（{len(user_ids)} 用户）",
                meta={"users": len(user_ids)},
            )

    async def _execute_monthly_folding(self):
        """执行所有用户的月总结折叠。"""
        task_name = "execute_monthly_folding"
        if not self.config.get("enable_monthly_folding", True):
            self._observe_skip(task_name, "monthly_folding_disabled")
            return

        folding_days = int(self.config.get("monthly_folding_days", 30))
        delay = int(self.config.get("monthly_folding_delay", 1))
        jitter = int(self.config.get("monthly_folding_jitter", 0))

        loop = asyncio.get_event_loop()
        try:
            user_ids = await loop.run_in_executor(self.logic.executor, self.logic.db.get_all_user_ids)
        except Exception as e:
            logger.debug(f"Engram：月折叠获取全部用户失败，已回退到活跃用户列表：{e}")
            self._observe_skip(task_name, "get_all_user_ids_failed_fallback_active_users")
            user_ids = list(self.logic.last_chat_time.keys())

        if not user_ids:
            self._observe_skip(task_name, "no_users_to_fold")
            return

        had_error = False
        started_at = time.perf_counter()
        for user_id in user_ids:
            if self._is_shutdown or getattr(self.logic, "_is_shutdown", False):
                self._observe_skip(task_name, "shutdown_during_iteration")
                break
            if user_id is None:
                self._observe_skip(task_name, "skip_none_user_id")
                continue
            uid_str = str(user_id).lower()
            if uid_str in {"system", "astrbot"}:
                self._observe_skip(task_name, "skip_system_user")
                continue

            try:
                await self.logic.fold_monthly_summaries(user_id, days=folding_days)
            except Exception as e:
                had_error = True
                logger.error(f"Engram：用户 {user_id} 月折叠失败：{e}")

            if delay > 0 or jitter > 0:
                await asyncio.sleep(max(0, delay) + max(0, random.randint(0, jitter)))

        if had_error:
            self._observe_run(task_name, started_at, False, RuntimeError("monthly_folding_partial_failure"))
            self._push_activity(
                title=f"月折叠部分失败（{len(user_ids)} 用户）",
                meta={"users": len(user_ids)},
            )
        else:
            self._observe_run(task_name, started_at, True)
            self._push_activity(
                title=f"月折叠完成（{len(user_ids)} 用户）",
                meta={"users": len(user_ids)},
            )

    async def _execute_yearly_folding(self):
        """执行所有用户的年度总结折叠。"""
        task_name = "execute_yearly_folding"
        if not self.config.get("enable_yearly_folding", True):
            self._observe_skip(task_name, "yearly_folding_disabled")
            return

        folding_days = int(self.config.get("yearly_folding_days", 365))
        delay = int(self.config.get("yearly_folding_delay", 1))
        jitter = int(self.config.get("yearly_folding_jitter", 0))

        loop = asyncio.get_event_loop()
        try:
            user_ids = await loop.run_in_executor(self.logic.executor, self.logic.db.get_all_user_ids)
        except Exception as e:
            logger.debug(f"Engram：年折叠获取全部用户失败，已回退到活跃用户列表：{e}")
            self._observe_skip(task_name, "get_all_user_ids_failed_fallback_active_users")
            user_ids = list(self.logic.last_chat_time.keys())

        if not user_ids:
            self._observe_skip(task_name, "no_users_to_fold")
            return

        had_error = False
        started_at = time.perf_counter()
        for user_id in user_ids:
            if self._is_shutdown or getattr(self.logic, "_is_shutdown", False):
                self._observe_skip(task_name, "shutdown_during_iteration")
                break
            if user_id is None:
                self._observe_skip(task_name, "skip_none_user_id")
                continue
            uid_str = str(user_id).lower()
            if uid_str in {"system", "astrbot"}:
                self._observe_skip(task_name, "skip_system_user")
                continue

            try:
                await self.logic.fold_yearly_summaries(user_id, days=folding_days)
            except Exception as e:
                had_error = True
                logger.error(f"Engram：用户 {user_id} 年折叠失败：{e}")

            if delay > 0 or jitter > 0:
                await asyncio.sleep(max(0, delay) + max(0, random.randint(0, jitter)))

        if had_error:
            self._observe_run(task_name, started_at, False, RuntimeError("yearly_folding_partial_failure"))
            self._push_activity(
                title=f"年折叠部分失败（{len(user_ids)} 用户）",
                meta={"users": len(user_ids)},
            )
        else:
            self._observe_run(task_name, started_at, True)
            self._push_activity(
                title=f"年折叠完成（{len(user_ids)} 用户）",
                meta={"users": len(user_ids)},
            )

    # ========== 记忆衰减与修剪 ==========

    async def daily_memory_maintenance(self):
        """每日记忆维护：衰减 active_score + 修剪冷记忆（在凌晨 01:00 执行，避开画像更新）"""
        task_name = "daily_memory_maintenance"
        while not self._is_shutdown:
            try:
                # 计算距离下一个 01:00 的秒数
                now = datetime.datetime.now()
                next_run = now.replace(hour=1, minute=0, second=0, microsecond=0)
                if now >= next_run:
                    next_run += datetime.timedelta(days=1)
                sleep_seconds = (next_run - now).total_seconds()

                logger.info(f"Engram：记忆维护已调度，距离执行约 {sleep_seconds/3600:.1f} 小时")
                await asyncio.sleep(sleep_seconds)

                if self._is_shutdown or getattr(self.logic, "_is_shutdown", False):
                    self._observe_skip(task_name, "shutdown_signal")
                    break
                if getattr(self.logic.executor, "_shutdown", False):
                    self._observe_skip(task_name, "executor_shutdown")
                    self._is_shutdown = True
                    break

                started_at = time.perf_counter()
                try:
                    await self._execute_memory_maintenance()
                    self._observe_run(task_name, started_at, True)
                except Exception as e:
                    self._observe_run(task_name, started_at, False, e)
                    raise

            except asyncio.CancelledError:
                self._observe_skip(task_name, "task_cancelled")
                logger.debug("Engram：记忆维护任务已取消")
                break
            except Exception as e:
                if "cannot schedule new futures after shutdown" in str(e):
                    self._observe_skip(task_name, "executor_shutdown_exception")
                    self._is_shutdown = True
                    break
                if not self._is_shutdown:
                    logger.error(f"Engram 记忆维护调度异常：{e}")
                await asyncio.sleep(60)

    async def _execute_memory_maintenance(self):
        """执行衰减 + 修剪"""
        task_name = "execute_memory_maintenance"
        started_at = time.perf_counter()
        loop = asyncio.get_event_loop()

        enable_decay = self.config.get("enable_memory_decay", True)
        decay_rate = self.config.get("memory_decay_rate", 1)
        enable_prune = self.config.get("enable_memory_prune", True)
        prune_threshold = self.config.get("memory_prune_threshold", 0)
        had_error = False

        if (not enable_decay or decay_rate <= 0) and not enable_prune:
            self._observe_skip(task_name, "decay_and_prune_disabled")
            return

        # 1. Decay：全局衰减
        if enable_decay and decay_rate > 0:
            try:
                await loop.run_in_executor(
                    self.logic.executor,
                    self.logic.db.decay_active_scores,
                    decay_rate
                )
                logger.info(f"Engram：已完成全量记忆活跃度衰减，衰减值={decay_rate}")
            except Exception as e:
                had_error = True
                logger.error(f"Engram：记忆衰减失败：{e}")
        else:
            self._observe_skip(task_name, "decay_disabled_or_invalid_rate")

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
                            had_error = True
                            logger.warning(f"Engram：冷记忆修剪批次 {i//100+1} 失败：{e}")

                    logger.info(f"Engram：已从 ChromaDB 修剪冷记忆 {pruned}/{len(cold_ids)} 条（阈值={prune_threshold}）")
                else:
                    self._observe_skip(task_name, "no_cold_memories_to_prune")
                    logger.debug("Engram：当前无冷记忆可修剪")
            except Exception as e:
                had_error = True
                logger.error(f"Engram：记忆修剪失败：{e}")
        else:
            self._observe_skip(task_name, "prune_disabled")

        if had_error:
            self._observe_run(task_name, started_at, False, RuntimeError("memory_maintenance_partial_failure"))
            self._push_activity(
                title="记忆维护部分失败",
                meta={"decay": enable_decay, "prune": enable_prune},
            )
        else:
            self._observe_run(task_name, started_at, True)
            self._push_activity(
                title="记忆维护完成",
                meta={"decay": enable_decay, "prune": enable_prune},
            )
