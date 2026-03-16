"""
好友缓存服务

用于 OneBot 适配器缓存好友列表，支持按需刷新与 friend_add 增量更新。
"""

import asyncio
import time
from astrbot.api import logger


class FriendCacheService:
    """OneBot 好友缓存服务。"""

    def __init__(self, config=None):
        self.config = config or {}
        self._friends = set()
        self._last_refresh = 0.0
        self._lock = asyncio.Lock()
        self._refresh_interval = int(self.config.get("group_memory_friend_cache_ttl", 3600) or 3600)

    def add_friend(self, user_id: str) -> None:
        """增量加入好友缓存。"""
        if user_id:
            self._friends.add(str(user_id))

    def _should_refresh(self, force: bool = False) -> bool:
        if force:
            return True
        if not self._friends:
            return True
        return (time.time() - self._last_refresh) >= self._refresh_interval

    async def refresh(self, bot=None, force: bool = False) -> bool:
        """刷新好友列表缓存。"""
        if not bot or not hasattr(bot, "get_friend_list"):
            logger.debug("Engram：当前平台未提供 get_friend_list，好友缓存不可用")
            return False

        if not self._should_refresh(force=force):
            return True

        async with self._lock:
            if not self._should_refresh(force=force):
                return True

            try:
                friend_list = await bot.get_friend_list(no_cache=bool(force))
            except TypeError:
                # 兼容部分平台 no_cache 参数为字符串
                friend_list = await bot.get_friend_list(no_cache="true" if force else "false")
            except Exception as e:
                logger.debug(f"Engram：刷新好友列表失败：{e}")
                return False

            friends = set()
            if isinstance(friend_list, dict):
                friend_list = friend_list.get("data") or friend_list.get("friends") or friend_list.get("list")

            if isinstance(friend_list, (list, tuple)):
                for item in friend_list:
                    if isinstance(item, dict):
                        uid = item.get("user_id") or item.get("id") or item.get("uid")
                    else:
                        uid = item
                    if uid is None:
                        continue
                    friends.add(str(uid))

            if friends:
                self._friends = friends
                self._last_refresh = time.time()
                logger.debug("Engram：好友缓存已刷新，共 %d 人", len(self._friends))
                return True

            logger.debug("Engram：好友列表返回为空，保持旧缓存（数量=%d）", len(self._friends))
            return bool(self._friends)

    async def is_friend(self, user_id: str, bot=None) -> bool:
        """判断 user_id 是否在好友缓存中。"""
        if not user_id:
            return False

        uid = str(user_id)
        if uid in self._friends:
            return True

        await self.refresh(bot=bot, force=False)
        return uid in self._friends
