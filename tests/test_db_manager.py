import datetime

from freezegun import freeze_time


def test_memory_list_desc_order(temp_db):
    base_time = datetime.datetime(2026, 2, 18, 12, 0, 0)

    temp_db.save_memory_index(
        index_id="idx_1",
        summary="old",
        ref_uuids="[]",
        prev_index_id=None,
        source_type="daily_summary",
        user_id="user_1",
        created_at=base_time - datetime.timedelta(days=2),
    )
    temp_db.save_memory_index(
        index_id="idx_2",
        summary="mid",
        ref_uuids="[]",
        prev_index_id=None,
        source_type="daily_summary",
        user_id="user_1",
        created_at=base_time - datetime.timedelta(days=1),
    )
    temp_db.save_memory_index(
        index_id="idx_3",
        summary="new",
        ref_uuids="[]",
        prev_index_id=None,
        source_type="daily_summary",
        user_id="user_1",
        created_at=base_time,
    )

    memories = temp_db.get_memory_list("user_1", limit=3)

    assert [m.index_id for m in memories] == ["idx_3", "idx_2", "idx_1"]


@freeze_time("2026-02-18 12:00:00")
def test_get_summaries_by_type_desc_and_days(temp_db):
    now = datetime.datetime.now()

    temp_db.save_memory_index(
        index_id="recent",
        summary="recent",
        ref_uuids="[]",
        prev_index_id=None,
        source_type="daily_summary",
        user_id="user_1",
        created_at=now - datetime.timedelta(days=1),
    )
    temp_db.save_memory_index(
        index_id="mid",
        summary="mid",
        ref_uuids="[]",
        prev_index_id=None,
        source_type="daily_summary",
        user_id="user_1",
        created_at=now - datetime.timedelta(days=3),
    )
    temp_db.save_memory_index(
        index_id="old",
        summary="old",
        ref_uuids="[]",
        prev_index_id=None,
        source_type="daily_summary",
        user_id="user_1",
        created_at=now - datetime.timedelta(days=10),
    )

    summaries = temp_db.get_summaries_by_type("user_1", "daily_summary", days=7)

    assert [m.index_id for m in summaries] == ["recent", "mid"]
