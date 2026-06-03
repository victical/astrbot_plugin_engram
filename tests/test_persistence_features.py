import datetime
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from db_manager import DatabaseManager


def test_delete_history_persist_and_restore_status(tmp_path):
    manager = DatabaseManager(str(tmp_path))

    record_id = manager.save_delete_history(
        scope_key="private:u1",
        user_id="u1",
        group_id="",
        source_type="private",
        index_id="idx-1",
        summary="summary",
        ref_uuids='["r1"]',
        prev_index_id="",
        created_at=datetime.datetime(2026, 1, 1, 12, 0, 0),
        active_score=100,
        delete_raw=False,
        deleted_uuids='["r1"]',
        vector_data={"embedding": [0.1, 0.2], "document": "summary", "metadata": {"user_id": "u1"}},
    )

    row = manager.get_last_delete_history(scope_key="private:u1")
    assert row is not None
    assert row.id == record_id
    assert row.index_id == "idx-1"

    manager.mark_delete_history_restored(record_id)
    assert manager.get_last_delete_history(scope_key="private:u1") is None


def test_pending_vector_jobs_crud(tmp_path):
    manager = DatabaseManager(str(tmp_path))

    manager.enqueue_pending_vector_jobs([
        {
            "index_id": "idx-a",
            "user_id": "u1",
            "source_type": "private",
            "summary": "s1",
            "metadata": {"x": 1},
            "reason": "test",
            "retry_count": 0,
            "queued_at": "2026-01-01 12:00:00",
        },
        {
            "index_id": "idx-b",
            "user_id": "u2",
            "source_type": "group",
            "summary": "s2",
            "metadata": {"x": 2},
            "reason": "test",
            "retry_count": 1,
            "queued_at": "2026-01-01 12:01:00",
        },
    ])

    rows = manager.get_pending_vector_jobs(limit=10)
    assert [row.index_id for row in rows] == ["idx-a", "idx-b"]

    manager.delete_pending_vector_jobs(["idx-a"])
    rows = manager.get_pending_vector_jobs(limit=10)
    assert [row.index_id for row in rows] == ["idx-b"]


def test_search_memory_indexes_by_keywords_bm25_and_like_fallback(tmp_path):
    manager = DatabaseManager(str(tmp_path))

    manager.save_memory_index(
        index_id="idx-1",
        summary="yesterday talked with siraku about exam study plan",
        ref_uuids='[]',
        prev_index_id=None,
        source_type="private",
        user_id="u1",
        created_at=datetime.datetime(2026, 4, 7, 10, 0, 0),
    )
    manager.save_memory_index(
        index_id="idx-2",
        summary="today talked about cooking and walking",
        ref_uuids='[]',
        prev_index_id=None,
        source_type="private",
        user_id="u1",
        created_at=datetime.datetime(2026, 4, 8, 10, 0, 0),
    )

    bm25_rows = manager.search_memory_indexes_by_keywords(
        user_id="u1",
        keywords=["siraku", "exam"],
        limit=5,
        use_bm25=True,
    )
    assert bm25_rows
    assert bm25_rows[0].index_id == "idx-1"

    like_rows = manager.search_memory_indexes_by_keywords(
        user_id="u1",
        keywords=["siraku", "exam"],
        limit=5,
        use_bm25=False,
    )
    assert like_rows
    assert like_rows[0].index_id == "idx-1"


def test_search_memory_indexes_by_keywords_sanitizes_fts_tokens(tmp_path, monkeypatch):
    manager = DatabaseManager(str(tmp_path))
    captured = {}

    class EmptyRows:
        def fetchall(self):
            return []

    def fake_execute_sql(sql, params=None, *args, **kwargs):
        captured["match_expr"] = params[1]
        return EmptyRows()

    monkeypatch.setattr(manager.db, "execute_sql", fake_execute_sql)

    manager.search_memory_indexes_by_keywords(
        user_id="u1",
        keywords=['bad\x00\x1f"token' + "x" * 100],
        limit=5,
        use_bm25=True,
    )

    match_expr = captured["match_expr"]
    assert "\x00" not in match_expr
    assert "\x1f" not in match_expr
    assert len(match_expr.strip('"')) <= 64


def test_search_raw_memory_sessions_returns_same_session_window_and_scopes_user(tmp_path):
    manager = DatabaseManager(str(tmp_path))
    base = datetime.datetime(2026, 5, 20, 12, 0, 0)

    rows = [
        ("r1", "s1", "u1", "user", "before project note", 0),
        ("r2", "s1", "u1", "assistant", "project phoenix deadline is Friday", 1),
        ("r3", "s1", "u1", "user", "after project note", 2),
        ("r4", "s1", "u2", "user", "project phoenix secret from another user", 3),
        ("r5", "s2", "u1", "user", "project phoenix in another session", 4),
    ]
    for uuid, session_id, user_id, role, content, offset in rows:
        manager.save_raw_memory(
            uuid=uuid,
            session_id=session_id,
            user_id=user_id,
            role=role,
            content=content,
            msg_type="text",
            timestamp=base + datetime.timedelta(minutes=offset),
        )

    results = manager.search_raw_memory_sessions(
        user_id="u1",
        query="deadline",
        limit=1,
        window=1,
    )

    assert len(results) == 1
    assert results[0]["match"]["uuid"] == "r2"
    assert [item["uuid"] for item in results[0]["context"]] == ["r1", "r2", "r3"]
    assert all(item["user_id"] == "u1" for item in results[0]["context"])


def test_search_raw_memory_sessions_falls_back_to_like_for_chinese_substring(tmp_path):
    manager = DatabaseManager(str(tmp_path))
    base = datetime.datetime(2026, 5, 20, 12, 0, 0)

    manager.save_raw_memory(
        uuid="zh-1",
        session_id="s1",
        user_id="u1",
        role="user",
        content="今天聊了考试计划和报名材料",
        msg_type="text",
        timestamp=base,
    )

    results = manager.search_raw_memory_sessions(
        user_id="u1",
        query="考试计划",
        limit=3,
        window=0,
    )

    assert [row["match"]["uuid"] for row in results] == ["zh-1"]
