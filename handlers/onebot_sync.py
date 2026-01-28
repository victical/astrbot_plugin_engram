"""
OneBot 用户信息同步处理器 (OneBot Sync Handler)

负责通过 OneBot V11 接口获取用户详细信息并同步到画像。
从 main.py 提取而来，遵循单一职责原则。

主要功能：
- 获取 QQ 头像 URL
- 调用 get_stranger_info API 获取用户详细信息
- 解析并转换字段（性别、年龄、生日、星座、生肖等）
- 更新用户画像

设计理念：
- 频率控制（默认 12 小时同步一次）
- 容错处理（API 不可用时优雅降级）
- 字段映射标准化
"""

import time
from astrbot.api import logger


class OneBotSyncHandler:
    """OneBot 用户信息同步处理器"""
    
    def __init__(self, profile_manager, utils_module=None):
        """
        初始化 OneBot 同步处理器
        
        Args:
            profile_manager: ProfileManager 实例
            utils_module: utils 模块（包含 get_constellation, get_zodiac, get_career）
        """
        self.profile = profile_manager
        self.utils = utils_module
        
        # 频率控制：每 12 小时最多同步一次
        self._last_sync = {}  # {user_id: timestamp}
        self._sync_interval = 12 * 3600  # 12 小时
    
    def should_sync(self, user_id: str) -> bool:
        """
        检查是否应该执行同步
        
        Args:
            user_id: 用户ID
            
        Returns:
            bool: 是否应该同步
        """
        now = time.time()
        last_sync = self._last_sync.get(user_id, 0)
        return now - last_sync >= self._sync_interval
    
    async def sync_user_info(self, event, user_id: str, user_name: str) -> bool:
        """
        同步 OneBot 用户信息到画像
        
        Args:
            event: AstrMessageEvent 对象
            user_id: 用户ID
            user_name: 用户昵称
            
        Returns:
            bool: 是否成功同步
        """
        if not self.should_sync(user_id):
            return False
        
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
                    try: 
                        uid_int = int(user_id)
                    except: 
                        uid_int = user_id
                    
                    stranger_info = await bot.get_stranger_info(user_id=uid_int)
                    if stranger_info:
                        # 解析详细信息
                        self._parse_stranger_info(stranger_info, update_payload, user_name)
                        logger.info(f"Engram: Synced OneBot info for {user_id}: gender={update_payload['basic_info'].get('gender', 'unknown')}, age={update_payload['basic_info'].get('age', 'unknown')}")
            except Exception as api_err:
                logger.debug(f"Engram: OneBot API call skipped or failed: {api_err}")

            await self.profile.update_user_profile(user_id, update_payload)
            self._last_sync[user_id] = time.time()
            return True
            
        except Exception as e:
            logger.error(f"Auto update basic info failed: {e}")
            return False
    
    def _parse_stranger_info(self, stranger_info: dict, update_payload: dict, user_name: str):
        """
        解析 OneBot get_stranger_info 返回的数据
        
        Args:
            stranger_info: OneBot API 返回的用户信息
            update_payload: 要更新的画像数据
            user_name: 默认用户名
        """
        basic_info = update_payload["basic_info"]
        
        # 映射 OneBot V11 字段到画像结构
        # sex: male, female, unknown
        sex_map = {"male": "男", "female": "女", "unknown": "未知"}
        gender = sex_map.get(stranger_info.get("sex"), "未知")
        age = stranger_info.get("age", "未知")
        nickname = stranger_info.get("nickname", user_name)
        
        basic_info["gender"] = gender
        basic_info["age"] = age
        basic_info["nickname"] = nickname
        
        # 补充生日、生肖、签名 (OneBot V11 扩展)
        if "birthday" in stranger_info: 
            basic_info["birthday"] = stranger_info["birthday"]
        
        # 解析生日并计算星座和生肖
        self._parse_birthday(stranger_info, basic_info)
        
        if "zodiac" in stranger_info: 
            basic_info["zodiac"] = stranger_info["zodiac"]
        if "signature" in stranger_info: 
            basic_info["signature"] = stranger_info["signature"]
        
        # 补充职业
        career_id = stranger_info.get("makeFriendCareer")
        if career_id and career_id != "0" and self.utils:
            basic_info["job"] = self.utils.get_career(int(career_id))

        # 某些 OneBot 扩展实现可能会提供 location
        if "location" in stranger_info:
            basic_info["location"] = stranger_info["location"]
        elif stranger_info.get("country") == "中国":
            prov = stranger_info.get("province", "")
            city = stranger_info.get("city", "")
            basic_info["location"] = f"{prov}-{city}".strip("-")
    
    def _parse_birthday(self, stranger_info: dict, basic_info: dict):
        """
        解析生日并计算星座和生肖
        
        Args:
            stranger_info: OneBot API 返回的用户信息
            basic_info: 画像基础信息
        """
        if not self.utils:
            return
        
        b_year = stranger_info.get("birthday_year")
        b_month = stranger_info.get("birthday_month")
        b_day = stranger_info.get("birthday_day")

        if b_year and b_month and b_day:
            basic_info["birthday"] = f"{b_year}-{b_month}-{b_day}"
            basic_info["constellation"] = self.utils.get_constellation(int(b_month), int(b_day))
            basic_info["zodiac"] = self.utils.get_zodiac(int(b_year), int(b_month), int(b_day))
        elif "birthday" in stranger_info and str(stranger_info["birthday"]).isdigit():
            b_str = str(stranger_info["birthday"])
            if len(b_str) == 8:
                b_year, b_month, b_day = b_str[:4], b_str[4:6], b_str[6:]
                basic_info["birthday"] = f"{b_year}-{b_month}-{b_day}"
                basic_info["constellation"] = self.utils.get_constellation(int(b_month), int(b_day))
                basic_info["zodiac"] = self.utils.get_zodiac(int(b_year), int(b_month), int(b_day))
