import pathlib
import sys
from types import SimpleNamespace
import asyncio
import datetime

from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from astrbot_plugin_engram import profile_renderer as profile_renderer_module
from astrbot_plugin_engram.db_manager import DatabaseManager
from astrbot_plugin_engram.webui_server import EngramWebServer


class DummyLogic:
    executor = None
    db = SimpleNamespace()

    async def get_user_profile(self, user_id):
        return {"basic_info": {"qq_id": user_id}}

    def _ensure_datetime(self, value):
        if isinstance(value, datetime.datetime):
            return value
        return datetime.datetime.fromtimestamp(value)


class DummyPlugin:
    def __init__(self, config=None):
        self.config = {"enable_webui_auth": False, **(config or {})}
        self.logic = DummyLogic()
        self.plugin_data_dir = "."


def make_server(config=None, host="0.0.0.0", port=8080):
    return EngramWebServer(DummyPlugin(config), host=host, port=port)


def make_group_db(tmp_path):
    manager = DatabaseManager(str(tmp_path))
    manager.save_memory_index(
        index_id="idx-member-1",
        summary="group project note",
        ref_uuids='["raw-1"]',
        prev_index_id=None,
        source_type="group",
        user_id="group-1",
        group_id="group-1",
        member_id="member-1",
        created_at=datetime.datetime(2026, 5, 19, 8, 0, 0),
    )
    manager.save_memory_index(
        index_id="idx-member-2",
        summary="group project note",
        ref_uuids='["raw-2"]',
        prev_index_id=None,
        source_type="group",
        user_id="group-1",
        group_id="group-1",
        member_id="member-2",
        created_at=datetime.datetime(2026, 5, 19, 9, 0, 0),
    )
    return manager


def test_default_cors_origins_include_port_and_skip_unsuitable_host():
    server = make_server(host="0.0.0.0", port=18080)

    origins = server._build_cors_origins()

    assert "http://localhost:18080" in origins
    assert "http://127.0.0.1:18080" in origins
    assert "http://0.0.0.0:18080" not in origins


def test_error_response_hides_internal_exception_details():
    server = make_server()

    response = server._error_response(RuntimeError("C:/secret/path.sqlite is locked"))

    assert response["success"] is False
    assert response["error"] == "内部错误"
    assert "secret" not in str(response)
    assert response["error_id"]


def test_body_size_limit_rejects_large_json_payload():
    server = make_server({"webui_max_body_bytes": 16})
    client = TestClient(server._app)

    response = client.post("/api/login", json={"password": "x" * 100})

    assert response.status_code == 413
    assert response.json()["detail"] == "请求体过大"


def test_profile_render_closes_renderer_when_render_fails(monkeypatch):
    class FakeRenderer:
        last = None

        def __init__(self, config, data_dir):
            self.closed = False
            FakeRenderer.last = self

        async def render(self, user_id, profile, memory_count=0):
            raise RuntimeError("render backend path leaked")

        async def close(self):
            self.closed = True

    monkeypatch.setattr(profile_renderer_module, "ProfileRenderer", FakeRenderer)
    server = make_server()
    client = TestClient(server._app, raise_server_exceptions=False)

    response = client.get("/api/profile/u1/render")

    assert response.status_code == 500
    assert FakeRenderer.last.closed is True
    assert "render backend path leaked" not in response.text


def test_webui_stop_times_out_and_clears_stuck_server_task():
    async def run_case():
        server = make_server({"webui_stop_timeout_seconds": 1})

        async def stuck_server():
            await asyncio.Event().wait()

        server._server = SimpleNamespace(should_exit=False)
        server._server_task = asyncio.create_task(stuck_server())

        await server.stop()

        assert server._server is None
        assert server._server_task is None

    asyncio.run(run_case())


def test_webui_background_task_failures_are_recorded():
    async def run_case():
        server = make_server()

        async def failing_task():
            raise RuntimeError("webui task boom")

        task = server._create_task("failing_webui_task", failing_task())
        for _ in range(5):
            await asyncio.sleep(0)
            if "failing_webui_task" in server._task_errors:
                break

        assert task.done()
        assert server._task_errors["failing_webui_task"] == "webui task boom"

    asyncio.run(run_case())


def test_group_memory_list_filters_member_id_in_sql_without_raw_scan(tmp_path, monkeypatch):
    server = make_server({"enable_group_memory": True})
    server.plugin._group_db = make_group_db(tmp_path)

    def fail_raw_scan(*args, **kwargs):
        raise AssertionError("member_id filter should use MemoryIndex SQL column")

    monkeypatch.setattr(server, "_load_group_memory_raw_messages", fail_raw_scan)
    client = TestClient(server._app)

    response = client.get(
        "/api/group-memories",
        params={"group_id": "group-1", "member_id": "member-1"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["total"] == 1
    assert payload["data"]["items"][0]["id"] == "idx-member-1"


def test_group_memory_search_filters_member_id_in_sql_without_raw_scan(tmp_path, monkeypatch):
    server = make_server({"enable_group_memory": True})
    server.plugin._group_db = make_group_db(tmp_path)

    def fail_raw_scan(*args, **kwargs):
        raise AssertionError("member_id filter should use MemoryIndex SQL column")

    monkeypatch.setattr(server, "_load_group_memory_raw_messages", fail_raw_scan)
    client = TestClient(server._app)

    response = client.post(
        "/api/group-memories/search",
        json={"group_id": "group-1", "member_id": "member-1", "query": "project"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["total"] == 1
    assert payload["data"]["items"][0]["id"] == "idx-member-1"
