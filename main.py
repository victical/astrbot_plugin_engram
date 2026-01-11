from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig, logger
from .memory_logic import MemoryLogic
from .export_handler import ExportHandler
from .profile_renderer import ProfileRenderer
from .utils import get_constellation, get_zodiac, get_career
import asyncio
import json
import sys
import datetime
import time

@register("astrbot_plugin_engram", "victical", "仿生双轨记忆系统", "1.2.3")
class EngramPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        from astrbot.api.star import StarTools
        self.plugin_data_dir = StarTools.get_data_dir()
        self.logic = MemoryLogic(context, config, self.plugin_data_dir)
        self.export_handler = ExportHandler(self.logic, self.plugin_data_dir)
        self.profile_renderer = ProfileRenderer(config, self.plugin_data_dir)
        self._last_onebot_sync = {}
        asyncio.create_task(self.background_worker())
        asyncio.create_task(self._daily_persona_scheduler())
        
    def _is_command_message(self, content: str) -> bool:
        """检测消息是否为指令"""
        if not self.config.get("enable_command_filter", True):
            return False
        
        text = content.strip()
        
        # 1. 检查指令前缀
        command_prefixes = self.config.get("command_prefixes", ["/", "!", "#"])
        for prefix in command_prefixes:
            if text.startswith(prefix):
                return True
        
        # 2. 检查完整指令匹配
        if self.config.get("enable_full_command_detection", False):
            full_commands = self.config.get("full_command_list", [])
            cleaned_text = "".join(text.split())
            for cmd in full_commands:
                if cleaned_text == "".join(str(cmd).split()):
                    return True
        
        return False

    async def _daily_persona_scheduler(self):
        """独立的每日画像更新调度器：精准在00:00执行，避免依赖轮询，支持并发控制"""
        while not self.logic._is_shutdown:
            try:
                # 计算距离下一个00:00的秒数
                now = datetime.datetime.now()
                tomorrow = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                sleep_seconds = (tomorrow - now).total_seconds()
                
                logger.info(f"Engram: Daily persona update scheduled in {sleep_seconds/3600:.1f} hours")
                await asyncio.sleep(sleep_seconds)
                
                if self.logic._is_shutdown: break
                
                # 执行画像更新 - 带并发控制和延迟
                min_memories = self.config.get("min_persona_update_memories", 3)
                max_concurrent = self.config.get("persona_update_max_concurrent", 3)
                update_delay = self.config.get("persona_update_delay", 5)
                
                # 创建信号量控制并发数
                semaphore = asyncio.Semaphore(max_concurrent)
                
                async def update_user_persona(user_id):
                    """带并发控制的单用户画像更新"""
                    async with semaphore:
                        try:
                            today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                            loop = asyncio.get_event_loop()
                            memories = await loop.run_in_executor(
                                self.logic.executor,
                                self.logic.db.get_memories_since,
                                user_id,
                                today
                            )
                            if len(memories) >= min_memories:
                                await self.logic._update_persona_daily(user_id)
                                logger.info(f"Engram: Daily persona updated for {user_id}")
                                # 更新后延迟，避免瞬时压力
                                if update_delay > 0:
                                    await asyncio.sleep(update_delay)
                        except Exception as e:
                            logger.error(f"Engram: Failed to update persona for {user_id}: {e}")
                
                # 收集所有需要更新的用户
                user_ids = list(self.logic.last_chat_time.keys())
                if user_ids:
                    logger.info(f"Engram: Starting daily persona update for {len(user_ids)} users (max concurrent: {max_concurrent}, delay: {update_delay}s)")
                    
                    # 并发执行所有用户的画像更新（受信号量限制）
                    tasks = [update_user_persona(user_id) for user_id in user_ids]
                    await asyncio.gather(*tasks, return_exceptions=True)
                    
                    logger.info(f"Engram: Daily persona update completed for {len(user_ids)} users")
                    
            except Exception as e:
                if not self.logic._is_shutdown:
                    logger.error(f"Engram daily persona scheduler error: {e}")
                await asyncio.sleep(60)  # 出错后短暂休眠再重试

    async def background_worker(self):
        """智能休眠：根据最早需要处理的时间动态调整检测间隔"""
        while not self.logic._is_shutdown:
            try:
                # 计算下一次需要检测的时间
                sleep_time = self._calculate_next_check_time()
                await asyncio.sleep(sleep_time)
                if self.logic._is_shutdown: break
                await self.logic.check_and_summarize()
            except Exception as e:
                if not self.logic._is_shutdown:
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

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """在调用 LLM 前注入长期记忆和用户画像"""
        if event.get_group_id(): return
        user_id = event.get_sender_id()
        query = event.message_str
        profile = await self.logic.get_user_profile(user_id)
        profile_block = ""
        if profile and profile.get("basic_info"):
            basic = profile.get("basic_info", {})
            attrs = profile.get("attributes", {})
            prefs = profile.get("preferences", {})
            dev = profile.get("dev_metadata", {})
            hobbies = ", ".join(attrs.get("hobbies", [])) if isinstance(attrs.get("hobbies"), list) else ""
            skills = ", ".join(attrs.get("skills", [])) if isinstance(attrs.get("skills"), list) else ""
            likes = ", ".join(prefs.get("likes", [])) if isinstance(prefs.get("likes"), list) else ""
            dislikes = ", ".join(prefs.get("dislikes", [])) if isinstance(prefs.get("dislikes"), list) else ""
            tech = ", ".join(dev.get("tech_stack", [])) if isinstance(dev.get("tech_stack"), list) else ""
            profile_block = f"【用户档案】\n- 称呼: {basic.get('nickname', '用户')} (QQ: {basic.get('qq_id')})\n"
            if basic.get('gender') and basic.get('gender') != "未知": profile_block += f"- 性别: {basic.get('gender')}\n"
            if basic.get('age') and basic.get('age') != "未知": profile_block += f"- 年龄: {basic.get('age')}\n"
            if basic.get('birthday') and basic.get('birthday') != "未知": profile_block += f"- 生日: {basic.get('birthday')}\n"
            if basic.get('job') and basic.get('job') != "未知": profile_block += f"- 职业: {basic.get('job')}\n"
            if basic.get('location') and basic.get('location') != "未知": profile_block += f"- 所在地: {basic.get('location')}\n"
            if basic.get('constellation') and basic.get('constellation') != "未知": profile_block += f"- 星座: {basic.get('constellation')}\n"
            if basic.get('zodiac') and basic.get('zodiac') != "未知": profile_block += f"- 生肖: {basic.get('zodiac')}\n"
            if hobbies: profile_block += f"- 爱好: {hobbies}\n"
            if skills or tech: profile_block += f"- 技能/技术栈: {skills} {tech}\n".strip() + "\n"
            if likes: profile_block += f"- 喜欢: {likes}\n"
            if dislikes: profile_block += f"- 讨厌: {dislikes}\n"
            status = profile.get("social_graph", {}).get("relationship_status", "初识")
            profile_block += f"- 当前关系状态: {status}\n\n【交互指令】\n请基于以上档案事实，以最契合用户期望的方式与其交流。\n"
        
        memories = await self.logic.retrieve_memories(user_id, query)
        memory_block = ""
        if memories:
            memory_prompt = "\n".join(memories)
            memory_block = f"【长期记忆回溯】：\n{memory_prompt}\n"
        
        if profile_block or memory_block:
            inject_text = f"\n\n{profile_block}{memory_block}"
            if req.system_prompt: req.system_prompt += inject_text
            else: req.system_prompt = f"你是一个有记忆的助手。以下是关于用户的信息：{inject_text}"

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        """在消息发送后记录 AI 的回复到原始记忆"""
        # 只处理私聊
        if event.get_group_id(): return
        
        # 获取结果对象
        result = event.get_result()
        # 必须是 LLM 结果才记录 (过滤掉指令回复、报错信息等)
        if not result or not result.is_llm_result():
            return

        user_id = event.get_sender_id()
        # 提取纯文本内容
        content = "".join([c.text for c in result.chain if hasattr(c, "text")])
        
        if content:
            await self.logic.record_message(user_id=user_id, session_id=user_id, role="assistant", content=content)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent):
        """在收到私聊消息时记录原始记忆并被动同步 OneBot 用户信息"""
        user_id = event.get_sender_id()
        content = event.message_str
        
        # 检查是否为指令消息，是则跳过记录
        if self._is_command_message(content):
            return
        
        user_name = event.get_sender_name()
        await self.logic.record_message(user_id=user_id, session_id=user_id, role="user", content=content, user_name=user_name)
        
        # 频率控制：每 12 小时最多同步一次 OneBot 信息
        now = time.time()
        last_sync = self._last_onebot_sync.get(user_id, 0)
        if now - last_sync < 12 * 3600:
            return

        # 被动更新基础信息 (通过 OneBot V11 接口获取更多细节)
        try:
            # 1. 基础 Payload
            avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
            update_payload = {
                "basic_info": {
                    "qq_id": user_id,
                    "nickname": user_name,
                    "avatar_url": avatar_url
                }
            }

            # 2. 尝试调用 OneBot V11 get_stranger_info 接口
            try:
                # 兼容不同版本的 AstrBot 获取 bot 实例的方式
                bot = getattr(event, 'bot', None)
                if bot and hasattr(bot, 'get_stranger_info'):
                    # 某些实现需要整数 ID
                    try: uid_int = int(user_id)
                    except: uid_int = user_id
                    
                    stranger_info = await bot.get_stranger_info(user_id=uid_int)
                    if stranger_info:
                        # 映射 OneBot V11 字段到画像结构
                        # sex: male, female, unknown
                        sex_map = {"male": "男", "female": "女", "unknown": "未知"}
                        gender = sex_map.get(stranger_info.get("sex"), "未知")
                        age = stranger_info.get("age", "未知")
                        nickname = stranger_info.get("nickname", user_name)
                        
                        update_payload["basic_info"]["gender"] = gender
                        update_payload["basic_info"]["age"] = age
                        update_payload["basic_info"]["nickname"] = nickname
                        
                        # 补充生日、生肖、签名 (OneBot V11 扩展)
                        if "birthday" in stranger_info: update_payload["basic_info"]["birthday"] = stranger_info["birthday"]
                        
                        # 解析生日并计算星座和生肖
                        b_year = stranger_info.get("birthday_year")
                        b_month = stranger_info.get("birthday_month")
                        b_day = stranger_info.get("birthday_day")

                        if b_year and b_month and b_day:
                            update_payload["basic_info"]["birthday"] = f"{b_year}-{b_month}-{b_day}"
                            update_payload["basic_info"]["constellation"] = get_constellation(int(b_month), int(b_day))
                            update_payload["basic_info"]["zodiac"] = get_zodiac(int(b_year), int(b_month), int(b_day))
                        elif "birthday" in stranger_info and str(stranger_info["birthday"]).isdigit():
                            b_str = str(stranger_info["birthday"])
                            if len(b_str) == 8:
                                b_year, b_month, b_day = b_str[:4], b_str[4:6], b_str[6:]
                                update_payload["basic_info"]["birthday"] = f"{b_year}-{b_month}-{b_day}"
                                update_payload["basic_info"]["constellation"] = get_constellation(int(b_month), int(b_day))
                                update_payload["basic_info"]["zodiac"] = get_zodiac(int(b_year), int(b_month), int(b_day))

                        if "zodiac" in stranger_info: update_payload["basic_info"]["zodiac"] = stranger_info["zodiac"]
                        if "signature" in stranger_info: update_payload["basic_info"]["signature"] = stranger_info["signature"]
                        
                        # 补充职业
                        career_id = stranger_info.get("makeFriendCareer")
                        if career_id and career_id != "0":
                            update_payload["basic_info"]["job"] = get_career(int(career_id))

                        # 某些 OneBot 扩展实现可能会提供 location
                        if "location" in stranger_info:
                            update_payload["basic_info"]["location"] = stranger_info["location"]
                        elif stranger_info.get("country") == "中国":
                            prov = stranger_info.get("province", "")
                            city = stranger_info.get("city", "")
                            update_payload["basic_info"]["location"] = f"{prov}-{city}".strip("-")
                        
                        logger.info(f"Engram: Synced OneBot info for {user_id}: gender={gender}, age={age}")
            except Exception as api_err:
                logger.debug(f"Engram: OneBot API call skipped or failed: {api_err}")

            await self.logic.update_user_profile(user_id, update_payload)
            self._last_onebot_sync[user_id] = now
        except Exception as e:
            logger.error(f"Auto update basic info failed: {e}")

    @filter.command("mem_list")
    async def mem_list(self, event: AstrMessageEvent, count: str = ""):
        """查看最近生成的长期记忆归档"""
        user_id = event.get_sender_id()
        
        # 支持可选的数量参数，未指定则使用配置项
        if count and count.isdigit():
            limit = int(count)
            if limit <= 0:
                yield event.plain_result("⚠️ 数量必须大于 0。")
                return
            elif limit > 50:
                yield event.plain_result("⚠️ 单次最多查询 50 条记忆。")
                return
        else:
            limit = self.config.get("list_memory_count", 5)
        
        loop = asyncio.get_event_loop()
        memories = await loop.run_in_executor(self.logic.executor, self.logic.db.get_memory_list, user_id, limit)
        if not memories:
            yield event.plain_result("🧐 你目前还没有生成的长期记忆。")
            return
        result = [f"📜 最近的 {len(memories)} 条长期记忆：\n" + "—" * 15]
        for i, m in enumerate(memories):
            result.append(f"{i+1}. ⏰ {m.created_at.strftime('%m-%d %H:%M')}\n   📝 {m.summary}\n")
        
        result.append("\n💡 发送 /mem_view <序号> 可查看某条记忆的完整对话原文。")
        result.append("💡 发送 /mem_list <数量> 可自定义查询条数。")
        yield event.plain_result("\n".join(result))

    @filter.command("mem_view")
    async def mem_view(self, event: AstrMessageEvent, index: str):
        """查看指定序号记忆的完整对话原文"""
        user_id = event.get_sender_id()
        
        if not index.isdigit():
            yield event.plain_result("⚠️ 请输入正确的序号，例如：/mem_view 1")
            return
            
        seq = int(index)
        if seq <= 0:
             yield event.plain_result("⚠️ 序号必须大于 0。")
             return

        # 调用逻辑获取详情
        memory_index, raw_msgs = await self.logic.get_memory_detail(user_id, seq)
        
        if not memory_index:
            yield event.plain_result(raw_msgs) # 这里 raw_msgs 返回的是错误提示字符串
            return
            
        # 格式化输出
        result = [
            f"📖 记忆详情 (序号 {seq})",
            f"⏰ 时间：{memory_index.created_at.strftime('%Y-%m-%d %H:%M')}",
            f"📝 归档：{memory_index.summary}",
            "————————————————",
            "🎙️ 原始对话回溯："
        ]
        
        if not raw_msgs:
            result.append("(暂无关联的原始对话数据)")
        else:
            for m in raw_msgs:
                # 使用公共过滤方法
                if not self.logic._is_valid_message_content(m.content):
                    continue
                    
                time_str = m.timestamp.strftime("%H:%M:%S")
                role_name = "我" if m.role == "assistant" else (m.user_name or "你")
                result.append(f"[{time_str}] {role_name}: {m.content}")
                
        yield event.plain_result("\n".join(result))

    @filter.command("mem_search")
    async def mem_search(self, event: AstrMessageEvent, query: str):
        """搜索与关键词相关的长期记忆（按相关性排序）"""
        user_id = event.get_sender_id()
        memories = await self.logic.retrieve_memories(user_id, query, limit=3)
        if not memories:
            yield event.plain_result(f"🔍 未找到与 '{query}' 相关的记忆。")
            return
        result = [f"🔍 搜索关键词 '{query}' 的结果（按相关性排序）：\n"] + memories
        result.append("\n💡 使用 /mem_delete <ID> 可根据记忆 ID 删除指定记忆。")
        yield event.plain_result("\n".join(result))

    @filter.command("mem_delete")
    async def mem_delete(self, event: AstrMessageEvent, index: str):
        """删除指定序号或 ID 的总结记忆（保留原始消息）"""
        user_id = event.get_sender_id()
        
        # 智能判断：数字且 ≤ 50 使用序号删除，否则使用 ID 删除
        if index.isdigit():
            seq = int(index)
            if seq <= 0:
                yield event.plain_result("⚠️ 序号必须大于 0。")
                return
            if seq > 50:
                yield event.plain_result("⚠️ 序号超过 50，请使用记忆 ID 进行删除。")
                return
            
            # 按序号删除
            success, message, summary = await self.logic.delete_memory_by_sequence(user_id, seq, delete_raw=False)
            
            if success:
                yield event.plain_result(f"🗑️ 已删除记忆 #{seq}：\n📝 {summary[:50]}{'...' if len(summary) > 50 else ''}\n\n💡 原始对话消息已保留，可重新归档。")
            else:
                yield event.plain_result(f"❌ {message}")
        else:
            # 按 ID 删除
            if len(index) < 8:
                yield event.plain_result("⚠️ 记忆 ID 至少需要 8 位，例如：/mem_delete a1b2c3d4")
                return
            
            success, message, summary = await self.logic.delete_memory_by_id(user_id, index, delete_raw=False)
            
            if success:
                yield event.plain_result(f"🗑️ 已删除记忆 ID {index[:8]}：\n📝 {summary[:50]}{'...' if len(summary) > 50 else ''}\n\n💡 原始对话消息已保留，可重新归档。")
            else:
                yield event.plain_result(f"❌ {message}")

    @filter.command("mem_delete_all")
    async def mem_delete_all(self, event: AstrMessageEvent, index: str):
        """删除指定序号或 ID 的总结记忆及其关联的原始消息"""
        user_id = event.get_sender_id()
        
        # 智能判断：数字且 ≤ 50 使用序号删除，否则使用 ID 删除
        if index.isdigit():
            seq = int(index)
            if seq <= 0:
                yield event.plain_result("⚠️ 序号必须大于 0。")
                return
            if seq > 50:
                yield event.plain_result("⚠️ 序号超过 50，请使用记忆 ID 进行删除。")
                return
            
            # 按序号删除
            success, message, summary = await self.logic.delete_memory_by_sequence(user_id, seq, delete_raw=True)
            
            if success:
                yield event.plain_result(f"🗑️ 已彻底删除记忆 #{seq} 及其原始对话：\n📝 {summary[:50]}{'...' if len(summary) > 50 else ''}\n\n💡 如果误删，可使用 /mem_undo 撤销此操作。")
            else:
                yield event.plain_result(f"❌ {message}")
        else:
            # 按 ID 删除
            if len(index) < 8:
                yield event.plain_result("⚠️ 记忆 ID 至少需要 8 位，例如：/mem_delete_all a1b2c3d4")
                return
            
            success, message, summary = await self.logic.delete_memory_by_id(user_id, index, delete_raw=True)
            
            if success:
                yield event.plain_result(f"🗑️ 已彻底删除记忆 ID {index[:8]} 及其原始对话：\n📝 {summary[:50]}{'...' if len(summary) > 50 else ''}\n\n💡 如果误删，可使用 /mem_undo 撤销此操作。")
            else:
                yield event.plain_result(f"❌ {message}")

    @filter.command("mem_undo")
    async def mem_undo(self, event: AstrMessageEvent):
        """撤销最近一次删除操作"""
        user_id = event.get_sender_id()
        
        success, message, summary = await self.logic.undo_last_delete(user_id)
        
        if success:
            yield event.plain_result(f"✅ 撤销成功！已恢复记忆：\n📝 {summary[:80]}{'...' if len(summary) > 80 else ''}\n\n💡 记忆已重新添加到您的记忆库中。")
        else:
            yield event.plain_result(f"❌ {message}")

    @filter.command("mem_clear_raw")
    async def mem_clear_raw(self, event: AstrMessageEvent, confirm: str = ""):
        """清除所有未归档的原始消息数据"""
        user_id = event.get_sender_id()
        if confirm != "confirm":
            yield event.plain_result("⚠️ 危险操作：此指令将永久删除您所有**尚未归档**的聊天原文，且不可恢复。\n\n如果您确定要执行，请发送：\n/mem_clear_raw confirm")
            return
        
        loop = asyncio.get_event_loop()
        try:
            # 仅删除 RawMemory 中未归档的消息
            from .db_manager import RawMemory
            def _clear_raw():
                with self.logic.db.db.connection_context():
                    RawMemory.delete().where((RawMemory.user_id == user_id) & (RawMemory.is_archived == False)).execute()
            
            await loop.run_in_executor(self.logic.executor, _clear_raw)
            # 重置内存计数
            self.logic.unsaved_msg_count[user_id] = 0
            yield event.plain_result("🗑️ 已成功清除您所有未归档的原始对话消息。")
        except Exception as e:
            logger.error(f"Clear raw memory failed: {e}")
            yield event.plain_result(f"❌ 清除失败：{e}")

    @filter.command("mem_clear_archive")
    async def mem_clear_archive(self, event: AstrMessageEvent, confirm: str = ""):
        """清除所有长期记忆归档（保留原始消息）"""
        user_id = event.get_sender_id()
        if confirm != "confirm":
            yield event.plain_result("⚠️ 危险操作：此指令将永久删除您所有的**长期记忆归档**及向量检索数据，但会保留原始聊天记录。\n\n如果您确定要执行，请发送：\n/mem_clear_archive confirm")
            return
        
        loop = asyncio.get_event_loop()
        try:
            # 确保 ChromaDB 已初始化
            await self.logic._ensure_chroma_initialized()
            
            # 1. 清除 SQLite 中的总结索引 (MemoryIndex)
            from .db_manager import MemoryIndex, RawMemory
            def _clear_archive():
                with self.logic.db.db.connection_context():
                    # 删除索引
                    MemoryIndex.delete().where(MemoryIndex.user_id == user_id).execute()
                    # 将所有已归档的消息重新标记为未归档，以便可以重新总结
                    RawMemory.update(is_archived=False).where(RawMemory.user_id == user_id).execute()
            
            await loop.run_in_executor(self.logic.executor, _clear_archive)
            
            # 2. 清除 ChromaDB 中的向量数据
            await loop.run_in_executor(self.logic.executor, lambda: self.logic.collection.delete(where={"user_id": user_id}))
            
            yield event.plain_result("🗑️ 已成功清除您所有的长期记忆归档，原始消息已重置为待归档状态。")
        except Exception as e:
            logger.error(f"Clear archive memory failed: {e}")
            yield event.plain_result(f"❌ 清除失败：{e}")

    @filter.command("mem_clear_all")
    async def mem_clear_all(self, event: AstrMessageEvent, confirm: str = ""):
        """清除所有原始消息和长期记忆数据"""
        user_id = event.get_sender_id()
        if confirm != "confirm":
            yield event.plain_result("⚠️ 警告：此指令将永久删除您所有的聊天原文、长期记忆归档及向量检索数据，且不可恢复。\n\n如果您确定要执行，请发送：\n/mem_clear_all confirm")
            return
        
        loop = asyncio.get_event_loop()
        try:
            # 确保 ChromaDB 已初始化
            await self.logic._ensure_chroma_initialized()
            
            # 清除 SQLite 中的原始消息和索引
            await loop.run_in_executor(self.logic.executor, self.logic.db.clear_user_data, user_id)
            # 清除 ChromaDB 中的向量数据
            await loop.run_in_executor(self.logic.executor, lambda: self.logic.collection.delete(where={"user_id": user_id}))
            # 重置内存计数
            self.logic.unsaved_msg_count[user_id] = 0
            yield event.plain_result("🗑️ 已成功彻底清除您所有的原始对话消息和归档记忆。")
        except Exception as e:
            logger.error(f"Clear all memory failed: {e}")
            yield event.plain_result(f"❌ 清除失败：{e}")

    @filter.command_group("profile")
    def profile_group(self, event: AstrMessageEvent): 
        """用户画像相关指令"""
        pass
    profile_group.__name__ = "profile_group"

    @profile_group.command("clear")
    async def profile_clear(self, event: AstrMessageEvent, confirm: str = ""):
        """清除用户画像数据"""
        user_id = event.get_sender_id()
        if confirm != "confirm":
            yield event.plain_result("⚠️ 危险操作：此指令将永久删除您的用户画像文件，所有侧写特征将被重置。\n\n如果您确定要执行，请发送：\n/profile clear confirm")
            return
        
        await self.logic.clear_user_profile(user_id)
        yield event.plain_result("🗑️ 您的用户画像已成功重置。")

    @profile_group.command("show")
    async def profile_show(self, event: AstrMessageEvent):
        """显示手账风格的用户深度画像"""
        user_id = event.get_sender_id()
        profile = await self.logic.get_user_profile(user_id)
        if not profile or not profile.get("basic_info"):
            yield event.plain_result("👤 您当前还没有建立深度画像。")
            return
        
        try:
            # 获取记忆数量
            loop = asyncio.get_event_loop()
            memories = await loop.run_in_executor(self.logic.executor, self.logic.db.get_memory_list, user_id, 100)
            memory_count = len(memories)
            
            # 渲染画像
            img_bytes = await self.profile_renderer.render(user_id, profile, memory_count)
            
            from astrbot.api.message_components import Image as MsgImage
            yield event.chain_result([MsgImage.fromBytes(img_bytes)])
        except Exception as e:
            logger.error(f"Profile rendering failed: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            yield event.plain_result(f"⚠️ 档案绘制失败，转为文本模式：\n{json.dumps(profile, indent=2, ensure_ascii=False)}")

    @profile_group.command("set")
    async def profile_set(self, event: AstrMessageEvent, key: str, value: str):
        """手动设置画像字段的值 (如: profile set basic_info.job 学生)"""
        user_id = event.get_sender_id()
        keys = key.split('.')
        update_data = {}
        curr = update_data
        for k in keys[:-1]:
            curr[k] = {}
            curr = curr[k]
        curr[keys[-1]] = value
        await self.logic.update_user_profile(user_id, update_data)
        yield event.plain_result(f"✅ 已更新画像：{key} = {value}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("engram_force_summarize")
    async def force_summarize(self, event: AstrMessageEvent):
        """[管理员] 立即对当前所有未处理对话进行记忆归档"""
        user_id = event.get_sender_id()
        yield event.plain_result("⏳ 正在强制执行记忆归档，请稍候...")
        await self.logic._summarize_private_chat(user_id)
        yield event.plain_result("✅ 记忆归档完成。您可以使用 /mem_list 查看。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("engram_force_persona")
    async def force_persona(self, event: AstrMessageEvent):
        """[管理员] 立即基于今日记忆强制深度更新画像"""
        user_id = event.get_sender_id()
        yield event.plain_result("⏳ 正在强制更新用户画像，请稍候...")
        await self.logic._update_persona_daily(user_id)
        yield event.plain_result("✅ 画像更新完成。您可以使用 /profile show 查看。")

    @filter.command("mem_export")
    async def mem_export(self, event: AstrMessageEvent, format: str = "jsonl", days: str = ""):
        """导出原始消息数据用于模型微调"""
        async for result in self.export_handler.handle_export_command(event, format, days):
            yield result

    @filter.command("mem_stats")
    async def mem_stats(self, event: AstrMessageEvent):
        """查看消息统计信息"""
        async for result in self.export_handler.handle_stats_command(event):
            yield result
    
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mem_export_all")
    async def mem_export_all(self, event: AstrMessageEvent, format: str = "jsonl", days: str = ""):
        """[管理员] 导出所有用户的原始消息数据"""
        async for result in self.export_handler.handle_export_all_command(event, format, days):
            yield result

    async def terminate(self):
        self.logic.shutdown()
        await self.profile_renderer.close()
