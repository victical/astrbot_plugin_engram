import asyncio
import pathlib
import sys
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from astrbot_plugin_engram.core.memory_manager import MemoryManager


class DummyContext:
    def get_provider_by_id(self, provider_id):
        return None


class DummyDB:
    def verify_contract(self, required_methods, stage="startup"):
        return None


def make_manager(tmp_path):
    return MemoryManager(
        context=DummyContext(),
        config={},
        data_dir=str(tmp_path),
        executor=None,
        db_manager=DummyDB(),
    )


def test_ensure_chroma_initialized_raises_import_error_details(tmp_path):
    manager = make_manager(tmp_path)

    with patch("astrbot_plugin_engram.core.memory_manager.chromadb", None), patch(
        "astrbot_plugin_engram.core.memory_manager._CHROMADB_IMPORT_ERROR",
        ImportError("missing optional dependency")
    ):
        try:
            asyncio.run(manager._ensure_chroma_initialized())
        except RuntimeError as exc:
            assert "chromadb import failed" in str(exc)
            assert "missing optional dependency" in str(exc)
        else:
            raise AssertionError("expected RuntimeError when chromadb import is unavailable")
