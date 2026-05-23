import asyncio
import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from astrbot_plugin_engram.main import EngramPlugin
from astrbot_plugin_engram.core.memory_facade import MemoryFacade


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
