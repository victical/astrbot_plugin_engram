"""
画像命令处理器 (Profile Command Handler)

负责处理所有用户画像相关命令的业务逻辑。
"""

import asyncio
import json
import datetime
from astrbot.api import logger


class ProfileCommandHandler:
    """画像命令处理器"""

    def __init__(self, config, profile_manager, db_manager, profile_renderer, executor):
        self.config = config
        self.profile = profile_manager
        self.db = db_manager
        self.renderer = profile_renderer
        self.executor = executor

    async def handle_profile_show(self, user_id: str) -> tuple:
        profile = await self.profile.get_user_profile(user_id)

        if not profile or not profile.get("basic_info"):
            return False, "👤 您当前还没有建立深度画像。"

        try:
            loop = asyncio.get_event_loop()
            memories = await loop.run_in_executor(self.executor, self.db.get_memory_list, user_id, 100)
            memory_count = len(memories)

            evidence_summary = None
            if self.config.get("show_profile_evidence_in_image", False):
                try:
                    evidence_summary = await self.profile.get_profile_evidence_summary(user_id, top_n=8)
                except Exception as e:
                    logger.debug(f"Engram：读取画像证据摘要失败，已忽略：{e}")
                    evidence_summary = None

            img_bytes = await self.renderer.render(
                user_id,
                profile,
                memory_count,
                evidence_summary=evidence_summary,
            )
            return True, img_bytes
        except Exception as e:
            logger.error(f"Engram：画像渲染失败：{e}")
            import traceback
            logger.debug(traceback.format_exc())
            return False, f"⚠️ 档案绘制失败，转为文本模式：\n{json.dumps(profile, indent=2, ensure_ascii=False)}"

    async def handle_profile_clear(self, user_id: str, confirm: str = "") -> str:
        if confirm != "confirm":
            return "⚠️ 危险操作：此指令将永久删除您的用户画像文件，所有侧写特征将被重置。\n\n如果您确定要执行，请发送：\n/profile clear confirm"

        await self.profile.clear_user_profile(user_id)
        return "🗑️ 您的用户画像已成功重置。"

    async def handle_profile_set(self, user_id: str, key: str, value: str) -> str:
        keys = key.split('.')
        update_data = {}
        curr = update_data
        for k in keys[:-1]:
            curr[k] = {}
            curr = curr[k]
        curr[keys[-1]] = value

        await self.profile.update_user_profile(user_id, update_data)
        return f"✅ 已更新画像：{key} = {value}"

    async def handle_profile_rollback(self, user_id: str, steps: str = "1") -> str:
        try:
            steps_int = int(steps)
        except (TypeError, ValueError):
            return "⚠️ 参数错误：steps 必须是正整数。"

        if steps_int <= 0:
            return "⚠️ 参数错误：steps 必须大于 0。"

        result = await self.profile.rollback_user_profile(user_id, steps=steps_int)
        if not isinstance(result, dict) or not result.get("success"):
            message = "无可回滚的历史版本"
            if isinstance(result, dict):
                message = result.get("message") or message
            return f"⚠️ 画像回滚失败：{message}"

        rolled = result.get("rolled_back_steps", steps_int)
        remaining = result.get("remaining", 0)
        return f"✅ 画像已回滚 {rolled} 步。剩余可用历史版本：{remaining}"

    async def handle_profile_evidence(self, user_id: str, top_n: str = "8") -> str:
        try:
            top_n_int = int(top_n)
        except (TypeError, ValueError):
            return "⚠️ 参数错误：top_n 必须是正整数。"

        if top_n_int <= 0:
            return "⚠️ 参数错误：top_n 必须大于 0。"

        top_n_int = min(top_n_int, 50)
        summary = await self.profile.get_profile_evidence_summary(user_id, top_n=top_n_int)

        if not summary:
            return "📭 当前暂无可展示的画像证据。"

        lines = ["🧾 画像证据摘要："]
        for i, item in enumerate(summary, start=1):
            field = item.get("field", "(unknown)")
            count = item.get("evidence_count", 0)
            last_seen = item.get("last_seen_at") or "未知"
            ref = item.get("latest_evidence") or "无"
            lines.append(f"{i}. {field}")
            lines.append(f"   - evidence_count: {count}")
            lines.append(f"   - last_seen_at: {last_seen}")
            lines.append(f"   - latest_ref: {ref}")

        return "\n".join(lines)

    def resolve_force_persona_days(self, days: str = "") -> tuple:
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
        time_desc = f"前 {days_int} 天"
        return (
            f"⏳ 正在基于{time_desc}的记忆强制更新用户画像，请稍候...",
            f"✅ 画像更新完成（基于{time_desc}的记忆）。您可以使用 /profile show 查看。"
        )

    async def handle_force_persona(self, user_id: str, days_int: int) -> tuple:
        now = datetime.datetime.now()
        start_time = (now - datetime.timedelta(days=days_int)).replace(hour=0, minute=0, second=0, microsecond=0)
        end_time = now

        await self.profile.update_persona_daily(user_id, start_time, end_time)
        return self.build_force_persona_messages(days_int)
