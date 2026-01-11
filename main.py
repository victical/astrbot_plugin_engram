from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig, logger
from .memory_logic import MemoryLogic
import asyncio
import json
import os
import io
import datetime
import aiohttp
from zhdate import ZhDate
from PIL import Image, ImageDraw, ImageFont

@register("astrbot_plugin_engram", "victical", "仿生双轨记忆系统", "1.1.5")
class EngramPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        from astrbot.api.star import StarTools
        self.plugin_data_dir = StarTools.get_data_dir()
        self.logic = MemoryLogic(context, config, self.plugin_data_dir)
        # 记录上次同步 OneBot 信息的时间，避免每条消息都触发 API 调用
        self._last_onebot_sync = {} 
        asyncio.create_task(self.background_worker())
        asyncio.create_task(self._daily_persona_scheduler())

    async def _daily_persona_scheduler(self):
        """独立的每日画像更新调度器：精准在00:00执行，避免依赖轮询"""
        while not self.logic._is_shutdown:
            try:
                # 计算距离下一个00:00的秒数
                now = datetime.datetime.now()
                tomorrow = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                sleep_seconds = (tomorrow - now).total_seconds()
                
                logger.info(f"Engram: Daily persona update scheduled in {sleep_seconds/3600:.1f} hours")
                await asyncio.sleep(sleep_seconds)
                
                if self.logic._is_shutdown: break
                
                # 执行画像更新
                min_memories = self.config.get("min_persona_update_memories", 3)
                for user_id in list(self.logic.last_chat_time.keys()):
                    today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                    loop = asyncio.get_event_loop()
                    memories = await loop.run_in_executor(self.logic.executor, self.logic.db.get_memories_since, user_id, today)
                    if len(memories) >= min_memories:
                        await self.logic._update_persona_daily(user_id)
                        logger.info(f"Engram: Daily persona updated for {user_id}")
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
        import time
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

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp):
        """在 LLM 响应后记录 AI 的回复到原始记忆"""
        if event.get_group_id(): return
        user_id = event.get_sender_id()
        if resp and resp.completion_text:
            await self.logic.record_message(user_id=user_id, session_id=user_id, role="assistant", content=resp.completion_text)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent):
        """在收到私聊消息时记录原始记忆并被动同步 OneBot 用户信息"""
        user_id = event.get_sender_id()
        content = event.message_str
        user_name = event.get_sender_name()
        await self.logic.record_message(user_id=user_id, session_id=user_id, role="user", content=content, user_name=user_name)
        
        # 频率控制：每 12 小时最多同步一次 OneBot 信息
        import time
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
                            update_payload["basic_info"]["constellation"] = self._get_constellation(int(b_month), int(b_day))
                            update_payload["basic_info"]["zodiac"] = self._get_zodiac(int(b_year), int(b_month), int(b_day))
                        elif "birthday" in stranger_info and str(stranger_info["birthday"]).isdigit():
                            b_str = str(stranger_info["birthday"])
                            if len(b_str) == 8:
                                b_year, b_month, b_day = b_str[:4], b_str[4:6], b_str[6:]
                                update_payload["basic_info"]["birthday"] = f"{b_year}-{b_month}-{b_day}"
                                update_payload["basic_info"]["constellation"] = self._get_constellation(int(b_month), int(b_day))
                                update_payload["basic_info"]["zodiac"] = self._get_zodiac(int(b_year), int(b_month), int(b_day))

                        if "zodiac" in stranger_info: update_payload["basic_info"]["zodiac"] = stranger_info["zodiac"]
                        if "signature" in stranger_info: update_payload["basic_info"]["signature"] = stranger_info["signature"]
                        
                        # 补充职业
                        career_id = stranger_info.get("makeFriendCareer")
                        if career_id and career_id != "0":
                            update_payload["basic_info"]["job"] = self._get_career(int(career_id))

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

    def _get_constellation(self, month: int, day: int) -> str:
        """星座映射"""
        constellations = {
            "白羊座": ((3, 21), (4, 19)),
            "金牛座": ((4, 20), (5, 20)),
            "双子座": ((5, 21), (6, 20)),
            "巨蟹座": ((6, 21), (7, 22)),
            "狮子座": ((7, 23), (8, 22)),
            "处女座": ((8, 23), (9, 22)),
            "天秤座": ((9, 23), (10, 22)),
            "天蝎座": ((10, 23), (11, 21)),
            "射手座": ((11, 22), (12, 21)),
            "摩羯座": ((12, 22), (1, 19)),
            "水瓶座": ((1, 20), (2, 18)),
            "双鱼座": ((2, 19), (3, 20)),
        }
        for constellation, ((start_month, start_day), (end_month, end_day)) in constellations.items():
            if (month == start_month and day >= start_day) or (month == end_month and day <= end_day):
                return constellation
            if start_month > end_month: # 跨年
                if (month == start_month and day >= start_day) or (month == end_month + 12 and day <= end_day):
                    return constellation
        return f"星座{month}-{day}"

    def _get_zodiac(self, year: int, month: int, day: int) -> str:
        """生肖映射"""
        zodiacs = ["鼠", "牛", "虎", "兔", "龙", "蛇", "马", "羊", "猴", "鸡", "狗", "猪"]
        from datetime import date
        current = date(year, month, day)
        try:
            spring = ZhDate(year, 1, 1).to_datetime().date()
            zodiac_year = year if current >= spring else year - 1
        except:
            zodiac_year = year
        index = (zodiac_year - 2020) % 12
        return zodiacs[index]

    def _get_career(self, num: int) -> str:
        """职业映射"""
        career = {1: "计算机/互联网/通信", 2: "生产/工艺/制造", 3: "医疗/护理/制药", 4: "金融/银行/投资/保险", 5: "商业/服务业/个体经营", 
                  6: "文化/广告/传媒", 7: "娱乐/艺术/表演", 8: "律师/法务", 9: "教育/培训", 10: "公务员/行政/事业单位", 
                  11: "模特", 12: "空姐", 13: "学生", 14: "其他职业"}
        return career.get(num, f"职业{num}")

    @filter.command("mem_list")
    async def mem_list(self, event: AstrMessageEvent):
        """查看最近生成的长期记忆归档"""
        user_id = event.get_sender_id()
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
        """搜索与关键词相关的长期记忆"""
        user_id = event.get_sender_id()
        memories = await self.logic.retrieve_memories(user_id, query, limit=3)
        if not memories:
            yield event.plain_result(f"🔍 未找到与 '{query}' 相关的记忆。")
            return
        result = [f"🔍 搜索关键词 '{query}' 的结果："] + memories
        yield event.plain_result("\n".join(result))

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

        basic = profile.get("basic_info", {})
        attrs = profile.get("attributes", {})
        prefs = profile.get("preferences", {})
        social = profile.get("social_graph", {})
        
        # 绘图逻辑
        try:
            # 配色方案 (奶油布丁风)
            colors = {
                "bg": "#FFF9E6",          # 奶油黄背景
                "grid": "#E6DCC3",        # 浅色网格
                "card_bg": "#FFFFFF",     # 卡片白底
                "text_main": "#5D4037",   # 深褐主文字
                "text_dim": "#8D6E63",    # 浅褐副文字
                "accent": "#FFAB91",      # 珊瑚粉装饰
                "tag_bg": "#FFECB3",      # 标签背景
                "shadow": "#E0C39E"       # 阴影色
            }

            W, H = 600, 900
            im = Image.new("RGB", (W, H), colors["bg"])
            draw = ImageDraw.Draw(im)

            # 1. 绘制背景网格 (手账风格)
            grid_size = 30
            for x in range(0, W, grid_size):
                draw.line([(x, 0), (x, H)], fill=colors["grid"], width=1)
            for y in range(0, H, grid_size):
                draw.line([(0, y), (W, y)], fill=colors["grid"], width=1)

            # 2. 绘制主卡片 (带阴影)
            margin = 40
            card_rect = [margin, 120, W-margin, H-margin]
            draw.rounded_rectangle([c + 8 for c in card_rect], radius=20, fill=colors["shadow"]) # 阴影
            draw.rounded_rectangle(card_rect, radius=20, fill=colors["card_bg"]) # 实体层

            # 3. 顶部胶带效果
            tape_w = 120
            draw.rectangle([W/2 - tape_w/2, 110, W/2 + tape_w/2, 125], fill=colors["accent"])

            # 字体加载逻辑优化：优先使用 PillowMD 样式目录下的字体
            font_path = None
            custom_style_path = self.config.get("pillowmd_style_path", "")
            
            # 搜索路径优先级：1. 配置的样式目录, 2. 插件数据目录下的 fonts, 3. 系统字体
            font_search_paths = []
            if custom_style_path and os.path.exists(custom_style_path):
                font_search_paths.append(custom_style_path)
                # 递归一层子目录 (适配 styles/default/ 这种结构)
                try:
                    for sub in os.listdir(custom_style_path):
                        sub_p = os.path.join(custom_style_path, sub)
                        if os.path.isdir(sub_p): font_search_paths.append(sub_p)
                except: pass
            
            font_search_paths.extend([
                os.path.join(self.plugin_data_dir, "fonts"),
                "C:/Windows/Fonts",
                "/usr/share/fonts/truetype/wqy",
                "/usr/share/fonts"
            ])

            for sp in font_search_paths:
                if not sp or not os.path.exists(sp): continue
                try:
                    files = [f for f in os.listdir(sp) if f.lower().endswith(('.ttc', '.ttf', '.otf'))]
                    # 优先选择用户放入的第一个字体，或者包含常见中文关键词的字体
                    best_match = None
                    if files:
                        # 只要有字体文件，就拿第一个
                        best_match = files[0]
                        # 如果有中文字体关键词，则更优
                        for f in files:
                            if any(k in f.lower() for k in ['cute', 'lixia', 'msyh', 'sim', 'wqy', 'noto']):
                                best_match = f; break
                        font_path = os.path.join(sp, best_match)
                        logger.info(f"Engram: Using custom font from style path: {font_path}")
                        break
                except: continue

            def get_f(size):
                try: 
                    if font_path: return ImageFont.truetype(font_path, size)
                    return ImageFont.load_default()
                except: return ImageFont.load_default()

            f_name = get_f(40)
            f_uid = get_f(20)
            f_label = get_f(22)
            f_val = get_f(24)
            f_title = get_f(28)
            f_tag = get_f(20)

            # 4. 绘制头像
            avatar_size = 140
            avatar_url = basic.get("avatar_url")
            if avatar_url:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(avatar_url, timeout=5) as resp:
                            if resp.status == 200:
                                avatar_img = Image.open(io.BytesIO(await resp.read())).convert("RGBA").resize((avatar_size, avatar_size))
                                mask = Image.new('L', (avatar_size, avatar_size), 0)
                                ImageDraw.Draw(mask).ellipse((0, 0, avatar_size, avatar_size), fill=255)
                                av_x, av_y = (W - avatar_size) // 2, 60
                                draw.ellipse((av_x-5, av_y-5, av_x+avatar_size+5, av_y+avatar_size+5), fill="white")
                                im.paste(avatar_img, (av_x, av_y), mask=mask)
                except: pass

            # 5. 文字信息
            curr_y = 220
            # 昵称 (居中)
            name = basic.get("nickname", "未知用户")
            tw = draw.textlength(name, font=f_name)
            draw.text(((W - tw)/2, curr_y), name, fill=colors["text_main"], font=f_name)
            
            curr_y += 55
            # UID (带背景)
            uid_str = f"ID: {basic.get('qq_id', user_id)}"
            uw = draw.textlength(uid_str, font=f_uid)
            draw.rounded_rectangle([(W-uw)/2 - 12, curr_y, (W+uw)/2 + 12, curr_y+32], radius=12, fill=colors["grid"])
            draw.text(((W - uw)/2, curr_y+3), uid_str, fill=colors["text_dim"], font=f_uid)

            # 绘制个性签名
            sig = basic.get('signature')
            if not sig or sig == "暂无个性签名": sig = "暂无个性签名"
            
            if sig:
                if len(sig) > 28: sig = sig[:27] + "..."
                curr_y += 50
                sw = draw.textlength(sig, font=f_tag)
                draw.text(((W - sw)/2, curr_y), sig, fill=colors["text_dim"], font=f_tag)
                curr_y += 50 # 增加垂直间距，防止往上挤
            else:
                curr_y += 20

            # 属性栏
            infos = []
            for label, key in [("性别", "gender"), ("年龄", "age"), ("生日", "birthday"), ("生肖", "zodiac"), ("星座", "constellation"), ("职业", "job"), ("所在地", "location")]:
                val = basic.get(key, "未知")
                if val and val != "未知":
                    infos.append((label, val))
            
            # 如果信息太少，增加基础间距
            if len(infos) <= 4:
                curr_y += 20
            
            # 使用更规整的网格布局
            start_x = margin + 50
            line_height = 45
            label_offset = 80 # 标签到内容的距离
            
            for i, (label, val) in enumerate(infos):
                row, col = i // 2, i % 2
                x_p = start_x + col * (W // 2 - margin - 30)
                y_p = curr_y + row * line_height
                
                draw.text((x_p, y_p), f"{label}：", fill=colors["text_dim"], font=f_label)
                draw.text((x_p + label_offset, y_p), str(val), fill=colors["text_main"], font=f_val)

            if infos:
                curr_y += ((len(infos) + 1) // 2) * line_height + 50 # 增加到分割线的间距
            else:
                curr_y += 30
            
            draw.line([(margin+30, curr_y), (W-margin-30, curr_y)], fill=colors["grid"], width=1)
            
            # 6. 标签区域 (记忆碎片 - 分类展示)
            curr_y += 35 # 增加分割线到标题的间距
            draw.text((margin+35, curr_y), "记忆碎片", fill=colors["accent"], font=f_title)
            curr_y += 55 # 增加标题到内容的间距
            
            # 分类逻辑
            tag_categories = [
                ("性格", attrs.get("personality_tags", [])),
                ("爱好", attrs.get("hobbies", [])),
                ("喜好", prefs.get("likes", [])),
                ("禁忌", prefs.get("dislikes", []))
            ]
            
            has_any_tag = False
            for cat_name, tags in tag_categories:
                if not tags: continue
                has_any_tag = True
                
                # 绘制分类标题
                draw.text((margin+35, curr_y), f"· {cat_name}", fill=colors["text_dim"], font=f_tag)
                curr_y += 35
                
                tag_x = margin + 50
                for tag in tags:
                    t_t = str(tag)
                    tw = draw.textlength(t_t, font=f_tag) + 24
                    if tag_x + tw > W - margin - 35:
                        tag_x = margin + 50; curr_y += 42
                    
                    if curr_y > H - margin - 100: break # 防止超出卡片
                    
                    draw.rounded_rectangle([tag_x, tag_y := curr_y, tag_x+tw, tag_y+32], radius=10, fill=colors["tag_bg"])
                    draw.text((tag_x+12, tag_y+4), t_t, fill=colors["text_main"], font=f_tag)
                    tag_x += tw + 12
                curr_y += 45

            if not has_any_tag:
                draw.text((margin+50, curr_y), "等待探索中...", fill=colors["text_dim"], font=f_tag)

            # 7. 底部羁绊
            bottom_y = H - margin - 80
            status = social.get("relationship_status", "初识")
            draw.text((margin+30, bottom_y), f"羁绊: {status}", fill=colors["text_dim"], font=f_label)
            
            loop = asyncio.get_event_loop()
            memories = await loop.run_in_executor(self.logic.executor, self.logic.db.get_memory_list, user_id, 100)
            sync_rate = min(20 + len(memories) * 5, 100)
            
            bar_x, bar_y, bar_w = margin+30, bottom_y + 35, W - 2*margin - 60
            draw.rounded_rectangle([bar_x, bar_y, bar_x+bar_w, bar_y+10], radius=5, fill="#EEEEEE")
            draw.rounded_rectangle([bar_x, bar_y, bar_x + bar_w * (sync_rate/100), bar_y+10], radius=5, fill=colors["accent"])

            img_byte_arr = io.BytesIO()
            im.save(img_byte_arr, format='PNG')
            from astrbot.api.message_components import Image as MsgImage
            yield event.chain_result([MsgImage.fromBytes(img_byte_arr.getvalue())])

        except Exception as e:
            logger.error(f"Handheld PIL rendering failed: {e}")
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

    async def terminate(self):
        self.logic.shutdown()
