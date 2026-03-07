"""
用户画像渲染模块
负责生成手账风格的用户画像图片

v2.1 优化版：
- 喜好分类细分（favorite_foods, favorite_items, favorite_activities）
- 动态画布高度
- 7级羁绊系统 + 多维度评分
"""
import io
import os
import asyncio
import aiohttp
from PIL import Image, ImageDraw, ImageFont
from astrbot.api import logger
from .services.bond_calculator import BondCalculator


class ProfileRenderer:
    """画像图片渲染器"""
    
    # 配色方案 (奶油布丁风)
    COLORS = {
        "bg": "#FFF9E6",
        "grid": "#E6DCC3",
        "card_bg": "#FFFFFF",
        "text_main": "#5D4037",
        "text_dim": "#8D6E63",
        "accent": "#FFAB91",
        "tag_bg": "#FFECB3",
        "shadow": "#E0C39E"
    }
    
    # 标签分类配色（多样化）
    TAG_COLORS = {
        "性格": "#FFE4B5",    # 浅杏色
        "爱好": "#E0BBE4",    # 薰衣草紫
        "美食": "#FFD1A9",    # 桃色
        "心头好": "#C7EFCF",  # 薄荷绿
        "休闲": "#B8E0F6",    # 天蓝
        "禁忌": "#FFB3BA",    # 浅珊瑚红
        "喜好": "#FDE4CF",    # 奶油橙
        "成就": "#D4C5F9"     # 淡紫
    }
    
    # 羁绊等级颜色（7级）
    LEVEL_COLORS = {
        1: "#BDBDBD",  # 灰色 - 萍水相逢
        2: "#A5D6A7",  # 浅绿 - 初识
        3: "#81C784",  # 绿色 - 相识
        4: "#4DB6AC",  # 青色 - 熟悉
        5: "#7986CB",  # 紫蓝 - 知己
        6: "#FFB74D",  # 金色 - 挚友
        7: "#FF8A65"   # 橙红 - 灵魂共鸣
    }
    
    # 等级图标（7级）
    LEVEL_ICONS = ["🌱", "🌿", "🌸", "💐", "🌟", "💫", "✨"]
    
    def __init__(self, config, plugin_data_dir):
        self.config = config
        self.plugin_data_dir = plugin_data_dir
        self._font_path = None
        self._session = None  # 复用的 HTTP 会话
        self._bond_calculator = BondCalculator()  # 羁绊计算器（统一计算逻辑）
        
        # 头像缓存目录
        self.avatar_cache_dir = os.path.join(plugin_data_dir, "avatar_cache")
        os.makedirs(self.avatar_cache_dir, exist_ok=True)
    
    def _find_font(self):
        """查找可用字体"""
        if self._font_path:
            return self._font_path
            
        custom_style_path = self.config.get("pillowmd_style_path", "")
        font_search_paths = []
        
        if custom_style_path and os.path.exists(custom_style_path):
            font_search_paths.append(custom_style_path)
            try:
                for sub in os.listdir(custom_style_path):
                    sub_p = os.path.join(custom_style_path, sub)
                    if os.path.isdir(sub_p):
                        font_search_paths.append(sub_p)
            except Exception as e:
                logger.debug(f"Engram 画像渲染器：扫描自定义样式路径失败（{custom_style_path}）：{e}")
        
        font_search_paths.extend([
            os.path.join(self.plugin_data_dir, "fonts"),
            "C:/Windows/Fonts",
            "/usr/share/fonts/truetype/wqy",
            "/usr/share/fonts"
        ])
        
        for sp in font_search_paths:
            if not sp or not os.path.exists(sp):
                continue
            try:
                files = [f for f in os.listdir(sp) if f.lower().endswith(('.ttc', '.ttf', '.otf'))]
                if files:
                    best_match = files[0]
                    for f in files:
                        if any(k in f.lower() for k in ['cute', 'lixia', 'msyh', 'sim', 'wqy', 'noto']):
                            best_match = f
                            break
                    self._font_path = os.path.join(sp, best_match)
                    logger.info(f"Engram：使用字体文件：{self._font_path}")
                    return self._font_path
            except Exception as e:
                logger.debug(f"Engram 画像渲染器：扫描字体路径失败（{sp}）：{e}")
                continue
        return None
    
    def _get_font(self, size):
        """获取指定大小的字体"""
        try:
            font_path = self._find_font()
            if font_path:
                return ImageFont.truetype(font_path, size)
            return ImageFont.load_default()
        except Exception as e:
            logger.debug(f"Engram 画像渲染器：加载字体失败（size={size}），已回退默认字体：{e}")
            return ImageFont.load_default()
    
    async def _ensure_session(self):
        """确保 HTTP 会话已初始化"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session
    
    async def close(self):
        """关闭 HTTP 会话"""
        if self._session and not self._session.closed:
            await self._session.close()
    
    async def _get_cached_avatar(self, user_id, avatar_url):
        """获取缓存的头像，如果不存在则下载并缓存"""
        import hashlib
        
        # 使用 user_id 作为缓存文件名
        cache_file = os.path.join(self.avatar_cache_dir, f"{user_id}.png")
        
        # 如果缓存文件存在且有效，直接使用
        if os.path.exists(cache_file):
            try:
                # 检查文件是否有效（大于 1KB）
                if os.path.getsize(cache_file) > 1024:
                    return Image.open(cache_file).convert("RGBA")
            except Exception as e:
                logger.debug(f"Engram 画像渲染器：加载用户 {user_id} 的头像缓存失败：{e}")
                # 缓存文件损坏，删除它
                try:
                    os.remove(cache_file)
                except Exception as remove_err:
                    logger.debug(f"Engram 画像渲染器：删除损坏头像缓存失败（{cache_file}）：{remove_err}")
        
        # 缓存不存在或无效，下载头像
        try:
            session = await self._ensure_session()
            async with session.get(avatar_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    avatar_data = await resp.read()
                    avatar_img = Image.open(io.BytesIO(avatar_data)).convert("RGBA")
                    
                    # 保存到缓存
                    try:
                        avatar_img.save(cache_file, "PNG")
                        logger.debug(f"Engram 画像渲染器：已缓存用户 {user_id} 的头像")
                    except Exception as e:
                        logger.debug(f"Engram 画像渲染器：缓存用户 {user_id} 头像失败：{e}")
                    
                    return avatar_img
        except Exception as e:
            logger.debug(f"Engram 画像渲染器：下载用户 {user_id} 头像失败：{e}")
        
        return None

    def _get_tag_categories(self, profile):
        """获取标签分类列表（v2.1 优化版：细分喜好类别）"""
        attrs = profile.get("attributes", {})
        prefs = profile.get("preferences", {})
        
        # v2.1 新增：细分喜好分类
        tag_categories = [
            ("性格", attrs.get("personality_tags", [])),
            ("爱好", attrs.get("hobbies", [])),
            ("美食", prefs.get("favorite_foods", [])),      # 新增
            ("心头好", prefs.get("favorite_items", [])),    # 新增
            ("休闲", prefs.get("favorite_activities", [])), # 新增
            ("禁忌", prefs.get("dislikes", []))
        ]
        
        # 兼容旧版：如果新字段为空但 likes 有值，显示 likes
        old_likes = prefs.get("likes", [])
        if old_likes and not any([
            prefs.get("favorite_foods", []),
            prefs.get("favorite_items", []),
            prefs.get("favorite_activities", [])
        ]):
            tag_categories.insert(2, ("喜好", old_likes))
        
        return tag_categories

    def _calculate_required_height(self, profile, memory_count, evidence_summary=None):
        """根据画像内容动态计算所需画布高度"""
        # 基础信息区域高度估算
        basic = profile.get("basic_info", {})
        infos = []
        for key in ["gender", "age", "birthday", "zodiac", "constellation", "job", "location"]:
            val = basic.get(key, "未知")
            if val and val != "未知":
                infos.append(val)
        
        # 头像(200) + 昵称(55) + ID(50) + 签名(50) + 属性行 + 间距
        info_rows = (len(infos) + 1) // 2
        base_height = 200 + 55 + 50 + 50 + (info_rows * 45) + 80
        
        # 标签区域高度估算（每个分类只显示一行）
        tag_categories = self._get_tag_categories(profile)
        tag_section_count = sum(1 for _, tags in tag_categories if tags)
        # 标题"记忆碎片"(55) + 每个分类(分类名20 + 标签38 + 标签行32 + 间距45 = 135)
        tag_height = 55 + (tag_section_count * 85) if tag_section_count > 0 else 95
        
        # 羁绊区域高度（固定）
        # 分隔线(30) + 标题(25) + 进度条(60) + 成就(60) + 提示(45) = 220
        bond_height = 220
        
        # 证据摘要区域高度（可选）
        evidence_height = 0
        if self.config.get("show_profile_evidence_in_image", False) and evidence_summary:
            try:
                evidence_count = len(evidence_summary)
            except Exception:
                evidence_count = 0
            if evidence_count > 0:
                # 标题 + 每行证据 + 区块间距
                evidence_height = 45 + min(evidence_count, 8) * 28 + 30

        # 底部边距
        margin = 80

        total = base_height + tag_height + bond_height + evidence_height + margin

        # 设置最小和最大高度
        return max(1000, min(total, 2200))

    def _render_sync(self, user_id, profile, memory_count, avatar_img, height=900, evidence_summary=None):
        """同步的图像渲染逻辑（CPU密集型操作，在线程池中执行）"""
        basic = profile.get("basic_info", {})
        attrs = profile.get("attributes", {})
        prefs = profile.get("preferences", {})
        social = profile.get("social_graph", {})
        colors = self.COLORS
        
        W, H = 600, height  # 使用动态高度
        im = Image.new("RGB", (W, H), colors["bg"])
        draw = ImageDraw.Draw(im)
        
        margin = 40
        
        # 1. 背景网格
        grid_size = 30
        for x in range(0, W, grid_size):
            draw.line([(x, 0), (x, H)], fill=colors["grid"], width=1)
        for y in range(0, H, grid_size):
            draw.line([(0, y), (W, y)], fill=colors["grid"], width=1)
        
        # 2. 主卡片
        card_rect = [margin, 120, W-margin, H-margin]
        draw.rounded_rectangle([c + 8 for c in card_rect], radius=20, fill=colors["shadow"])
        draw.rounded_rectangle(card_rect, radius=20, fill=colors["card_bg"])
        
        # 3. 顶部胶带
        tape_w = 120
        draw.rectangle([W/2 - tape_w/2, 110, W/2 + tape_w/2, 125], fill=colors["accent"])
        
        # 字体
        f_name = self._get_font(40)
        f_uid = self._get_font(20)
        f_label = self._get_font(22)
        f_val = self._get_font(24)
        f_title = self._get_font(28)
        f_tag = self._get_font(20)
        
        # 4. 头像
        avatar_size = 140
        if avatar_img:
            try:
                avatar_img = avatar_img.resize((avatar_size, avatar_size))
                mask = Image.new('L', (avatar_size, avatar_size), 0)
                ImageDraw.Draw(mask).ellipse((0, 0, avatar_size, avatar_size), fill=255)
                av_x, av_y = (W - avatar_size) // 2, 60
                draw.ellipse((av_x-5, av_y-5, av_x+avatar_size+5, av_y+avatar_size+5), fill="white")
                im.paste(avatar_img, (av_x, av_y), mask=mask)
            except Exception as e:
                logger.debug(f"Engram 画像渲染器：渲染用户 {user_id} 头像失败：{e}")
        
        # 5. 文字信息
        curr_y = 220
        name = basic.get("nickname", "未知用户")
        tw = draw.textlength(name, font=f_name)
        draw.text(((W - tw)/2, curr_y), name, fill=colors["text_main"], font=f_name)
        
        curr_y += 55
        uid_str = f"ID: {basic.get('qq_id', user_id)}"
        uw = draw.textlength(uid_str, font=f_uid)
        draw.rounded_rectangle([(W-uw)/2 - 12, curr_y, (W+uw)/2 + 12, curr_y+32], radius=12, fill=colors["grid"])
        draw.text(((W - uw)/2, curr_y+3), uid_str, fill=colors["text_dim"], font=f_uid)
        
        # 个性签名
        sig = basic.get('signature') or "暂无个性签名"
        if len(sig) > 28:
            sig = sig[:27] + "..."
        curr_y += 50
        sw = draw.textlength(sig, font=f_tag)
        draw.text(((W - sw)/2, curr_y), sig, fill=colors["text_dim"], font=f_tag)
        curr_y += 50
        
        # 属性栏
        infos = []
        for label, key in [("性别", "gender"), ("年龄", "age"), ("生日", "birthday"), ("生肖", "zodiac"), ("星座", "constellation"), ("职业", "job"), ("所在地", "location")]:
            val = basic.get(key, "未知")
            if val and val != "未知":
                infos.append((label, val))
        
        if len(infos) <= 4:
            curr_y += 20
        
        start_x = margin + 50
        line_height = 45
        label_offset = 80
        
        for i, (label, val) in enumerate(infos):
            row, col = i // 2, i % 2
            x_p = start_x + col * (W // 2 - margin - 30)
            y_p = curr_y + row * line_height
            draw.text((x_p, y_p), f"{label}：", fill=colors["text_dim"], font=f_label)
            draw.text((x_p + label_offset, y_p), str(val), fill=colors["text_main"], font=f_val)
        
        if infos:
            curr_y += ((len(infos) + 1) // 2) * line_height + 50
        else:
            curr_y += 30
        
        draw.line([(margin+30, curr_y), (W-margin-30, curr_y)], fill=colors["grid"], width=1)
        
        # 6. 标签区域（v2.1 优化版：细分喜好类别）
        curr_y += 35
        draw.text((margin+35, curr_y), "记忆碎片", fill=colors["accent"], font=f_title)
        curr_y += 55
        
        # 使用新的标签分类
        tag_categories = self._get_tag_categories(profile)
        
        has_any_tag = False
        for cat_name, tags in tag_categories:
            if not tags:
                continue
            has_any_tag = True
            draw.text((margin+35, curr_y), f"· {cat_name}", fill=colors["text_dim"], font=f_tag)
            curr_y += 38  # 分类标题与标签之间的间距
            
            # 根据分类获取对应的标签背景色
            tag_bg_color = self.TAG_COLORS.get(cat_name, colors["tag_bg"])
            
            tag_x = margin + 50
            # 只显示一行标签（最多显示能放下的标签）
            for tag in tags:
                t_t = str(tag)
                tw = draw.textlength(t_t, font=f_tag) + 24
                # 如果这个标签放不下了，就停止（只显示一行）
                if tag_x + tw > W - margin - 35:
                    break
                
                draw.rounded_rectangle([tag_x, curr_y, tag_x+tw, curr_y+32], radius=10, fill=tag_bg_color)
                draw.text((tag_x+12, curr_y+4), t_t, fill=colors["text_main"], font=f_tag)
                tag_x += tw + 12
            curr_y += 45  # 分类之间的间距
        
        if not has_any_tag:
            draw.text((margin+50, curr_y), "等待探索中...", fill=colors["text_dim"], font=f_tag)
            curr_y += 40
        
        # 7. 羁绊模块（v2.1 扩展版：跟随在标签区域后）
        curr_y += 30  # 与标签区域的间距
        draw.line([(margin+30, curr_y), (W-margin-30, curr_y)], fill=colors["grid"], width=1)
        
        bond_info = self._bond_calculator.calculate_bond_level(memory_count, profile)
        level = bond_info["level"]
        level_name = bond_info["level_name"]
        progress = bond_info["progress"]
        breakdown = bond_info["breakdown"]
        achievements = breakdown["achievements"]
        next_hints = bond_info["next_level_hint"]
        
        level_color = self.LEVEL_COLORS.get(level, colors["accent"])
        
        curr_y += 25
        # 第一行：等级名称
        level_text = f"羁绊: Lv.{level} {level_name}"
        draw.text((margin+35, curr_y), level_text, fill=colors["accent"], font=f_title)
        
        # 第二行：进度条（不显示百分比文字）
        bar_y = curr_y + 45
        bar_x = margin + 30
        bar_w = W - 2*margin - 60
        bar_h = 14
        
        draw.rounded_rectangle([bar_x, bar_y, bar_x+bar_w, bar_y+bar_h],
                               radius=7, fill="#EEEEEE")
        if progress > 0:
            progress_width = max(bar_h, bar_w * (progress/100))
            draw.rounded_rectangle([bar_x, bar_y, bar_x + progress_width, bar_y+bar_h],
                                   radius=7, fill=level_color)
        
        # 第三行：成就徽章（使用与标签相同的样式）
        badge_y = bar_y + 30
        if achievements:
            badge_x = margin + 30
            achievement_color = self.TAG_COLORS.get("成就", colors["tag_bg"])
            for ach in achievements[:4]:
                aw = draw.textlength(ach, font=f_tag) + 24
                if badge_x + aw > W - margin - 30:
                    break
                draw.rounded_rectangle([badge_x, badge_y, badge_x+aw, badge_y+32],
                                       radius=10, fill=achievement_color)
                draw.text((badge_x+12, badge_y+4), ach, fill=colors["text_main"], font=f_tag)
                badge_x += aw + 12
            badge_y += 45
        else:
            badge_y += 10
        
        # 第四行：升级提示
        if level < 7 and next_hints:
            hint_text = next_hints[0]
            if len(hint_text) > 35:
                hint_text = hint_text[:34] + "..."
            draw.text((margin+35, badge_y), hint_text, fill=colors["text_dim"], font=f_tag)

        # 8. 证据摘要（可选）
        if self.config.get("show_profile_evidence_in_image", False) and evidence_summary:
            sec_y = badge_y + 45
            draw.line([(margin+30, sec_y), (W-margin-30, sec_y)], fill=colors["grid"], width=1)
            sec_y += 22
            draw.text((margin+35, sec_y), "证据摘要", fill=colors["accent"], font=f_title)
            sec_y += 38

            for item in evidence_summary[:8]:
                field = str(item.get("field", ""))
                count = int(item.get("evidence_count", 0) or 0)
                field_text = field if len(field) <= 34 else field[:33] + "..."
                line = f"• {field_text} ({count})"
                draw.text((margin+45, sec_y), line, fill=colors["text_dim"], font=f_tag)
                sec_y += 28

        # 输出（CPU密集型操作）
        img_byte_arr = io.BytesIO()
        im.save(img_byte_arr, format='PNG')
        return img_byte_arr.getvalue()
    
    async def render(self, user_id, profile, memory_count=0, evidence_summary=None):
        """渲染用户画像图片（异步包装，避免阻塞事件循环）"""
        # 1. 异步获取头像（如果需要）
        basic = profile.get("basic_info", {})
        avatar_url = basic.get("avatar_url")
        avatar_img = None
        if avatar_url:
            avatar_img = await self._get_cached_avatar(user_id, avatar_url)
        
        # 2. 动态计算高度
        required_height = self._calculate_required_height(profile, memory_count, evidence_summary=evidence_summary)
        
        # 3. 在线程池中执行CPU密集型的图像渲染操作
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,  # 使用默认线程池
            self._render_sync,
            user_id,
            profile,
            memory_count,
            avatar_img,
            required_height,
            evidence_summary,
        )
