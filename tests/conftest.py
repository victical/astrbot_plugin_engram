import sys
import types
import tempfile
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
PARENT_DIR = ROOT_DIR.parent
if str(PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(PARENT_DIR))

try:
    import astrbot.api  # type: ignore
except Exception:
    astrbot_module = types.ModuleType("astrbot")
    api_module = types.ModuleType("astrbot.api")
    star_module = types.ModuleType("astrbot.api.star")

    class DummyLogger:
        def debug(self, *args, **kwargs):
            pass

        def info(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

    class DummyStarTools:
        @staticmethod
        def get_data_dir():
            return ""

    api_module.logger = DummyLogger()
    star_module.StarTools = DummyStarTools

    astrbot_module.api = api_module
    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = api_module
    sys.modules["astrbot.api.star"] = star_module

from astrbot_plugin_engram.db_manager import DatabaseManager


@pytest.fixture
def temp_db():
    """创建一个临时的内存/文件数据库用于测试"""
    with tempfile.TemporaryDirectory() as temp_dir:
        db = DatabaseManager(temp_dir)
        yield db
