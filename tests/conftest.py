import os
import sys
from pathlib import Path
from types import ModuleType


def _ensure_astrbot_stub():
    """当测试环境未安装 astrbot 时，注入最小可用桩模块。"""
    try:
        import astrbot.api  # type: ignore # noqa: F401
        return
    except Exception:
        pass

    astrbot_mod = ModuleType("astrbot")
    api_mod = ModuleType("astrbot.api")
    event_mod = ModuleType("astrbot.api.event")
    star_mod = ModuleType("astrbot.api.star")
    msg_mod = ModuleType("astrbot.api.message_components")

    class _Logger:
        @staticmethod
        def debug(*args, **kwargs):
            return None

        @staticmethod
        def info(*args, **kwargs):
            return None

        @staticmethod
        def warning(*args, **kwargs):
            return None

        @staticmethod
        def error(*args, **kwargs):
            return None

    class _DummyFilter:
        class EventMessageType:
            PRIVATE_MESSAGE = "private"

        class PermissionType:
            ADMIN = "admin"

        @staticmethod
        def _decorator(*args, **kwargs):
            def _wrap(func):
                return func
            return _wrap

        @classmethod
        def command(cls, *args, **kwargs):
            return cls._decorator(*args, **kwargs)

        @classmethod
        def command_group(cls, *args, **kwargs):
            def _decorate(func):
                def _sub_command(*a, **k):
                    def _wrap(sub_func):
                        return sub_func
                    return _wrap

                setattr(func, "command", _sub_command)
                return func

            return _decorate

        @classmethod
        def llm_tool(cls, *args, **kwargs):
            return cls._decorator(*args, **kwargs)

        @classmethod
        def on_llm_request(cls, *args, **kwargs):
            return cls._decorator(*args, **kwargs)

        @classmethod
        def after_message_sent(cls, *args, **kwargs):
            return cls._decorator(*args, **kwargs)

        @classmethod
        def event_message_type(cls, *args, **kwargs):
            return cls._decorator(*args, **kwargs)

        @classmethod
        def permission_type(cls, *args, **kwargs):
            return cls._decorator(*args, **kwargs)

    class _AstrMessageEvent:
        pass

    class _MessageEventResult:
        pass

    class _Context:
        pass

    class _Star:
        def __init__(self, context=None):
            self.context = context

    def _register(*args, **kwargs):
        def _wrap(cls):
            return cls
        return _wrap

    class _StarTools:
        @staticmethod
        def get_data_dir():
            return "./data"

    class _Image:
        @staticmethod
        def fromBytes(content):
            return content

    api_mod.logger = _Logger()
    api_mod.AstrBotConfig = dict

    event_mod.filter = _DummyFilter
    event_mod.AstrMessageEvent = _AstrMessageEvent
    event_mod.MessageEventResult = _MessageEventResult

    star_mod.Context = _Context
    star_mod.Star = _Star
    star_mod.register = _register
    star_mod.StarTools = _StarTools

    msg_mod.Image = _Image

    sys.modules["astrbot"] = astrbot_mod
    sys.modules["astrbot.api"] = api_mod
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod
    sys.modules["astrbot.api.message_components"] = msg_mod

    astrbot_mod.api = api_mod


_ensure_astrbot_stub()

# 允许在仓库根目录直接执行 `pytest -q` 导入 `astrbot_plugin_engram`
PROJECT_PARENT = Path(__file__).resolve().parents[2]   # .../shouban
PROJECT_ROOT = Path(__file__).resolve().parents[1]     # .../astrbot_plugin_engram

if str(PROJECT_PARENT) not in sys.path:
    sys.path.insert(0, str(PROJECT_PARENT))

# 兼容测试中使用的相对路径（例如 astrbot_plugin_engram/_conf_schema.json）
# 统一切到父目录执行，避免 cwd 差异导致找不到文件。
os.chdir(PROJECT_PARENT)
