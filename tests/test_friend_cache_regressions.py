import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from astrbot_plugin_engram.services.friend_cache import FriendCacheService


def test_friend_cache_refresh_ttl_reads_current_config():
    config = {"group_memory_friend_cache_ttl": 3600}
    service = FriendCacheService(config=config)
    service._friends = {"u1"}
    service._last_refresh = time.time() - 10

    assert service._should_refresh() is False

    config["group_memory_friend_cache_ttl"] = 1

    assert service._should_refresh() is True
