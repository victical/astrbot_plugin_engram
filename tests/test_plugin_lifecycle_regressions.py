import asyncio
import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.star_handler import star_handlers_registry

from astrbot_plugin_engram.main import EngramPlugin
from astrbot_plugin_engram.core.memory_facade import MemoryFacade


def _collect_command_filter_names(event_filter) -> set[str]:
    if isinstance(event_filter, CommandFilter):
        return set(event_filter.get_complete_command_names())
    if isinstance(event_filter, CommandGroupFilter):
        names = set(event_filter.get_complete_command_names())
        for child in event_filter.sub_command_filters:
            names.update(_collect_command_filter_names(child))
        return names
    return set()


def _registered_command_names() -> set[str]:
    names = set()
    module_name = EngramPlugin.__module__
    for handler in star_handlers_registry.get_handlers_by_module_name(module_name):
        for event_filter in handler.event_filters:
            names.update(_collect_command_filter_names(event_filter))
    return names


def test_chinese_commands_are_registered():
    names = _registered_command_names()

    expected = {
        "查看记忆",
        "查看记忆详情",
        "搜索记忆",
        "删除记忆",
        "删除全部记忆",
        "撤销删除记忆",
        "查看画像",
        "设置画像",
        "回滚画像",
        "查看画像证据",
        "删除画像",
        "清空画像",
        "查看群记忆",
        "查看群记忆详情",
        "搜索群记忆",
        "删除群记忆",
        "删除全部群记忆",
        "撤销删除群记忆",
        "归档群记忆",
        "导出记忆",
        "统计记忆",
        "导出全部记忆",
        "归档记忆",
        "归档全部记忆",
        "更新画像",
        "重建记忆向量",
    }

    assert expected <= names


def test_english_commands_are_not_registered():
    names = _registered_command_names()

    forbidden = {
        "mem_list",
        "mem_view",
        "mem_search",
        "mem_delete",
        "mem_delete_all",
        "mem_undo",
        "mem_clear_raw",
        "mem_clear_archive",
        "mem_clear_all",
        "group_mem_list",
        "group_mem_view",
        "group_mem_search",
        "group_mem_delete",
        "group_mem_delete_all",
        "group_mem_undo",
        "group_mem_force_summarize",
        "mem_export",
        "mem_stats",
        "mem_export_all",
        "engram_force_summarize",
        "engram_force_summarize_all",
        "engram_force_persona",
        "mem_rebuild_vector",
        "engram_rebuild_vectors",
        "profile",
        "profile clear",
        "profile show",
        "profile set",
        "profile rollback",
        "profile delete",
        "profile evidence",
    }

    leftovers = forbidden & names

    assert leftovers == set(), f"English commands still registered: {sorted(leftovers)}"


def test_user_facing_command_hints_use_chinese_commands():
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    checked_files = [
        "README.md",
        "_conf_schema.json",
        "export_handler.py",
        "core/memory_manager.py",
        "webui/static/memories.js",
    ]
    forbidden = {
        "/mem_list",
        "/mem_view",
        "/mem_search",
        "/mem_delete",
        "/mem_delete_all",
        "/mem_undo",
        "/mem_clear_raw",
        "/mem_clear_archive",
        "/mem_clear_all",
        "/group_mem_list",
        "/group_mem_view",
        "/group_mem_search",
        "/group_mem_delete",
        "/group_mem_delete_all",
        "/group_mem_undo",
        "/group_mem_force_summarize",
        "/mem_export",
        "/mem_stats",
        "/mem_export_all",
        "/engram_force_summarize",
        "/engram_force_summarize_all",
        "/engram_force_persona",
        "/mem_rebuild_vector",
        "/engram_rebuild_vectors",
        "/profile clear",
        "/profile show",
        "/profile set",
        "/profile rollback",
        "/profile delete",
        "/profile evidence",
    }

    leftovers = []
    for relative_path in checked_files:
        content = (repo_root / relative_path).read_text(encoding="utf-8")
        for item in forbidden:
            if item in content:
                leftovers.append(f"{relative_path}: {item}")

    assert leftovers == [], "English command hints still present: " + ", ".join(leftovers)


def test_plugin_background_task_failures_are_recorded():
    async def run_case():
        plugin = EngramPlugin.__new__(EngramPlugin)
        plugin._startup_tasks = []
        plugin._startup_task_errors = {}

        async def failing_task():
            raise RuntimeError("startup boom")

        task = plugin._create_background_task("startup", failing_task())
        for _ in range(5):
            await asyncio.sleep(0)
            if "startup" in plugin._startup_task_errors:
                break

        assert task in plugin._startup_tasks
        assert plugin._startup_task_errors["startup"] == "startup boom"

    asyncio.run(run_case())


def test_group_memory_init_lock_is_created_lazily_per_loop():
    async def run_case():
        plugin = EngramPlugin.__new__(EngramPlugin)
        plugin._group_memory_init_lock = None
        plugin._group_memory_init_lock_loop = None

        lock = plugin._get_group_memory_init_lock()

        assert isinstance(lock, asyncio.Lock)
        assert plugin._group_memory_init_lock is lock

    asyncio.run(run_case())


def test_terminate_waits_for_executor_without_canceling_futures():
    class DummyExecutor:
        def __init__(self):
            self.calls = []
            self.submitted = []

        def shutdown(self, **kwargs):
            self.calls.append(kwargs)

        def submit(self, fn, *args, **kwargs):
            self.submitted.append((fn, args, kwargs))
            fn(*args, **kwargs)
            return SimpleNamespace(result=lambda timeout=None: None)

    class DummyManager:
        def __init__(self):
            self.shutdown_called = False

        def shutdown(self):
            self.shutdown_called = True

    class DummyDB:
        def __init__(self):
            self.closed = 0

        def close_thread_connection(self):
            self.closed += 1

    class DummyRenderer:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    async def run_case():
        executor = DummyExecutor()
        manager = DummyManager()
        private_db = DummyDB()
        group_db = DummyDB()
        renderer = DummyRenderer()
        plugin = EngramPlugin.__new__(EngramPlugin)
        plugin.logic = SimpleNamespace(
            _is_shutdown=False,
            _memory_manager=manager,
            executor=executor,
            db=private_db,
        )
        plugin._group_db = group_db
        plugin.profile_renderer = renderer
        plugin._scheduler = SimpleNamespace(_is_shutdown=False, _tasks=[])
        plugin._startup_tasks = []

        await plugin.terminate()

        assert manager.shutdown_called is True
        assert renderer.closed is True
        assert private_db.closed == 1
        assert group_db.closed == 1
        assert executor.calls == [{"wait": True, "cancel_futures": False}]

    asyncio.run(run_case())


def test_group_memory_enabled_starts_prewarm_background_task():
    async def run_case():
        plugin = EngramPlugin.__new__(EngramPlugin)
        plugin.config = {"enable_group_memory": True}
        created = []

        async def warmup():
            return "ok"

        def create_task(name, coro):
            created.append((name, coro))
            coro.close()

        plugin._create_background_task = create_task
        plugin._ensure_group_memory_manager = warmup

        plugin._maybe_start_group_memory_prewarm()

        assert [name for name, _coro in created] == ["group_memory_prewarm"]

    asyncio.run(run_case())


def test_command_handler_exceptions_return_friendly_message():
    class Event:
        def get_sender_id(self):
            return "u1"

        def plain_result(self, text):
            return text

    class FailingHandler:
        async def handle_mem_list(self, **_kwargs):
            raise RuntimeError("database password leaked")

    async def run_case():
        plugin = EngramPlugin.__new__(EngramPlugin)
        plugin._mem_handler = FailingHandler()

        results = [item async for item in plugin.mem_list(Event())]

        assert len(results) == 1
        assert "失败" in results[0]
        assert "database password leaked" not in results[0]

    asyncio.run(run_case())


def test_force_summarize_exceptions_return_friendly_message():
    class Event:
        def get_sender_id(self):
            return "u1"

        def plain_result(self, text):
            return text

    class FailingHandler:
        def get_force_summarize_messages(self):
            return "start", "done"

        async def handle_force_summarize(self, **_kwargs):
            raise RuntimeError("llm api secret")

    async def run_case():
        plugin = EngramPlugin.__new__(EngramPlugin)
        plugin._mem_handler = FailingHandler()

        results = [item async for item in plugin.force_summarize(Event())]

        assert results == ["start", "❌ 命令执行失败，请稍后重试。"]

    asyncio.run(run_case())


def test_webui_settings_read_live_config_values():
    plugin = EngramPlugin.__new__(EngramPlugin)
    plugin.config = {
        "enable_webui_server": True,
        "webui_host": "127.0.0.1",
        "webui_port": "9000",
    }

    assert plugin._get_webui_settings() == (True, "127.0.0.1", 9000)

    plugin.config["enable_webui_server"] = False
    plugin.config["webui_host"] = "0.0.0.0"
    plugin.config["webui_port"] = "bad"

    assert plugin._get_webui_settings() == (False, "0.0.0.0", 8080)


def test_profile_affinity_is_enabled_by_default_and_can_be_disabled():
    plugin = EngramPlugin.__new__(EngramPlugin)

    assert plugin._is_profile_affinity_enabled({}) is True
    assert plugin._is_profile_affinity_enabled({"enable_profile_affinity": True}) is True
    assert plugin._is_profile_affinity_enabled({"enable_profile_affinity": False}) is False


def test_affinity_memory_provider_is_built_regardless_of_profile_affinity_switch():
    db = object()
    plugin = EngramPlugin.__new__(EngramPlugin)
    plugin.logic = SimpleNamespace(db=db)

    # 关闭羁绊展示时，只读适配器仍然构建，好感度插件才能拿到数据源
    plugin.config = {"enable_profile_affinity": False}
    provider = plugin._build_affinity_memory_provider()
    assert provider is not None
    assert provider.logic is plugin.logic
    assert provider.db is db

    # 默认（开启）同样构建
    plugin.config = {}
    assert plugin._build_affinity_memory_provider() is not None


def test_memory_facade_shutdown_does_not_cancel_queued_futures():
    class DummyExecutor:
        def __init__(self):
            self.calls = []

        def shutdown(self, **kwargs):
            self.calls.append(kwargs)

    class DummyManager:
        def __init__(self):
            self.shutdown_called = False

        def shutdown(self):
            self.shutdown_called = True

    facade = MemoryFacade.__new__(MemoryFacade)
    facade._is_shutdown = False
    facade._memory_manager = DummyManager()
    facade.executor = DummyExecutor()

    facade.shutdown()

    assert facade._is_shutdown is True
    assert facade._memory_manager.shutdown_called is True
    assert facade.executor.calls == [{"wait": True, "cancel_futures": False}]
