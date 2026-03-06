from concurrent.futures import ThreadPoolExecutor

import pytest

from astrbot_plugin_engram.core.memory_manager import MemoryManager
from astrbot_plugin_engram.db_manager import DatabaseManager, StableDatabaseInterface


class _BrokenRetrieveBackend:
    """故意缺少 get_prev_indices_by_ids，用于契约漂移测试。"""

    def get_memory_indexes_by_ids(self, index_ids):
        return {}

    def get_raw_memories_map_by_uuid_lists(self, index_uuid_map):
        return {}

    def get_memories_by_uuids(self, uuids):
        return []

    def update_active_score(self, index_id, bonus=10):
        return None


class _BrokenDbNoVerifier:
    """不提供 verify_contract，走 MemoryManager 的兜底自检分支。"""

    # 只提供极少方法，故意制造契约缺失
    def save_raw_memory(self, **kwargs):
        return None

    def get_unarchived_raw(self, session_id, limit=None):
        return []


class _DbWithVerifierSpy:
    """带 verify_contract 的桩对象，用于验证 MemoryManager 是否在启动时调用自检。"""

    def __init__(self):
        self.calls = []

    def verify_contract(self, required_methods=None, stage="startup", raise_on_error=True):
        self.calls.append(
            {
                "required_methods": tuple(required_methods or ()),
                "stage": stage,
                "raise_on_error": raise_on_error,
            }
        )
        return True, []


@pytest.mark.parametrize("method_name", StableDatabaseInterface.RETRIEVE_MEMORY_METHODS)
def test_retrieve_contract_methods_exist_in_database_manager(tmp_path, method_name):
    data_dir = tmp_path / "engram_contract_ok"
    data_dir.mkdir(parents=True, exist_ok=True)

    backend = DatabaseManager(str(data_dir))
    stable_db = StableDatabaseInterface(backend)

    assert callable(getattr(stable_db, method_name, None)), f"Missing retrieve contract method: {method_name}"


def test_stable_interface_retrieve_contract_check_fail_fast():
    stable_db = StableDatabaseInterface(_BrokenRetrieveBackend())

    with pytest.raises(AttributeError) as exc:
        stable_db.verify_contract(
            required_methods=StableDatabaseInterface.RETRIEVE_MEMORY_METHODS,
            stage="test.retrieve",
            raise_on_error=True,
        )

    err = str(exc.value)
    assert "get_prev_indices_by_ids" in err
    assert "test.retrieve" in err


def test_memory_manager_contract_check_fail_fast_without_verifier(tmp_path):
    data_dir = tmp_path / "engram_contract_fail"
    data_dir.mkdir(parents=True, exist_ok=True)

    broken_db = _BrokenDbNoVerifier()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with pytest.raises(AttributeError) as exc:
            MemoryManager(
                context=None,
                config={},
                data_dir=str(data_dir),
                executor=executor,
                db_manager=broken_db,
                profile_manager=None,
            )

    assert "missing methods" in str(exc.value)


def test_memory_manager_calls_verify_contract_on_startup(tmp_path):
    data_dir = tmp_path / "engram_contract_spy"
    data_dir.mkdir(parents=True, exist_ok=True)

    spy_db = _DbWithVerifierSpy()

    with ThreadPoolExecutor(max_workers=1) as executor:
        manager = MemoryManager(
            context=None,
            config={},
            data_dir=str(data_dir),
            executor=executor,
            db_manager=spy_db,
            profile_manager=None,
        )

    assert manager is not None
    assert len(spy_db.calls) == 1
    assert spy_db.calls[0]["stage"] == "MemoryManager.__init__"
    assert set(spy_db.calls[0]["required_methods"]) == set(MemoryManager.REQUIRED_DB_METHODS)
