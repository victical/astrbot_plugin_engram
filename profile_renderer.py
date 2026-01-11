"""
用户画像渲染模块
负责生成手账风格的用户画像图片
"""
import io
import os
import asyncio
import aiohttp
from PIL import Image, ImageDraw, ImageFont
from astrbot.api import logger


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
    
    def __init__(self, config, plugin_data_dir):
        self.config = config
        self.plugin_data_dir = plugin_data_dir
        self._font_path = None
        self._session = None  # 复用的 HTTP 会话
        
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
            except:
                pass
        
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
                    logger.info(f"Engram: Using font: {self._font_path}")
                    return self._font_path
            except:
                continue
        return None
    
    def _get_font(self, size):
        """获取指定大小的字体"""
        try:
            font_path = self._find_font()
            if font_path:
                return ImageFont.truetype(font_path, size)
            return ImageFont.load_default()
        except:
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
                logger.debug(f"Failed to load cached avatar for {user_id}: {e}")
                # 缓存文件损坏，删除它
                try:
                    os.remove(cache_file)
                except:
                    pass
        
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
                        logger.debug(f"Cached avatar for {user_id}")
                    except Exception as e:
                        logger.debug(f"Failed to cache avatar for {user_id}: {e}")
                    
                    return avatar_img
        except Exception as e:
            logger.debug(f"Failed to download avatar for {user_id}: {e}")
        
        return None
    
    def _render_sync(self, user_id, profile, memory_count, avatar_img):
        """同步的图像渲染逻辑（CPU密集型操作，在线程池中执行）"""
        basic = profile.get("basic_info", {})
        attrs = profile.get("attributes", {})
        prefs = profile.get("preferences", {})
        social = profile.get("social_graph", {})
        colors = self.COLORS
        
        W, H = 600, 900
        im = Image.new("RGB", (W, H), colors["bg"])
        draw = ImageDraw.Draw(im)
        
        # 1. 背景网格
        grid_size = 30
        for x in range(0, W, grid_size):
            draw.line([(x, 0), (x, H)], fill=colors["grid"], width=1)
        for y in range(0, H, grid_size):
            draw.line([(0, y), (W, y)], fill=colors["grid"], width=1)
        
        # 2. 主卡片
        margin = 40
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
                logger.debug(f"Failed to render avatar for {user_id}: {e}")
        
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
        
        # 6. 标签区域
        curr_y += 35
        draw.text((margin+35, curr_y), "记忆碎片", fill=colors["accent"], font=f_title)
        curr_y += 55
        
        tag_categories = [
            ("性格", attrs.get("personality_tags", [])),
            ("爱好", attrs.get("hobbies", [])),
            ("喜好", prefs.get("likes", [])),
            ("禁忌", prefs.get("dislikes", []))
        ]
        
        has_any_tag = False
        for cat_name, tags in tag_categories:
            if not tags:
                continue
            has_any_tag = True
            draw.text((margin+35, curr_y), f"· {cat_name}", fill=colors["text_dim"], font=f_tag)
            curr_y += 35
            
            tag_x = margin + 50
            for tag in tags:
                t_t = str(tag)
                tw = draw.textlength(t_t, font=f_tag) + 24
                if tag_x + tw > W - margin - 35:
                    tag_x = margin + 50
                    curr_y += 42
                
                if curr_y > H - margin - 100:
                    break
                
                draw.rounded_rectangle([tag_x, curr_y, tag_x+tw, curr_y+32], radius=10, fill=colors["tag_bg"])
                draw.text((tag_x+12, curr_y+4), t_t, fill=colors["text_main"], font=f_tag)
                tag_x += tw + 12
            curr_y += 45
        
        if not has_any_tag:
            draw.text((margin+50, curr_y), "等待探索中...", fill=colors["text_dim"], font=f_tag)
        
        # 7. 底部羁绊
        bottom_y = H - margin - 80
        status = social.get("relationship_status", "初识")
        draw.text((margin+30, bottom_y), f"羁绊: {status}", fill=colors["text_dim"], font=f_label)
        
        sync_rate = min(20 + memory_count * 5, 100)
        bar_x, bar_y, bar_w = margin+30, bottom_y + 35, W - 2*margin - 60
        draw.rounded_rectangle([bar_x, bar_y, bar_x+bar_w, bar_y+10], radius=5, fill="#EEEEEE")
        draw.rounded_rectangle([bar_x, bar_y, bar_x + bar_w * (sync_rate/100), bar_y+10], radius=5, fill=colors["accent"])
        
        # 输出（CPU密集型操作）
        img_byte_arr = io.BytesIO()
        im.save(img_byte_arr, format='PNG')
        return img_byte_arr.getvalue()
    
    async def render(self, user_id, profile, memory_count=0):
        """渲染用户画像图片（异步包装，避免阻塞事件循环）"""
        # 1. 异步获取头像（如果需要）
        basic = profile.get("basic_info", {})
        avatar_url = basic.get("avatar_url")
        avatar_img = None
        if avatar_url:
            avatar_img = await self._get_cached_avatar(user_id, avatar_url)
        
        # 2. 在线程池中执行CPU密集型的图像渲染操作
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,  # 使用默认线程池
            self._render_sync,
            user_id,
            profile,
            memory_count,
            avatar_img
        )
