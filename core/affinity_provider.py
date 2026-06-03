from __future__ import annotations

import datetime as dt
from typing import Any


def _safe_call(obj: object, method_name: str, *args, default=None, **kwargs):
    method = getattr(obj, method_name, None)
    if not callable(method):
        return default
    try:
        return method(*args, **kwargs)
    except Exception:
        return default


def _to_date_range(value) -> tuple[dt.datetime, dt.datetime]:
    if isinstance(value, dt.datetime):
        day = value.date()
    elif isinstance(value, dt.date):
        day = value
    else:
        day = dt.date.fromisoformat(str(value))
    start = dt.datetime.combine(day, dt.time.min)
    end = start + dt.timedelta(days=1)
    return start, end


class AffinityMemoryProvider:
    """Read-only adapter exposing Engram data to astrbot_plugin_affinity."""

    def __init__(self, logic, db, bond_calculator=None):
        self.logic = logic
        self.db = db
        self.bond_calculator = bond_calculator

    async def get_user_profile(self, user_id: str) -> dict:
        method = getattr(self.logic, "get_user_profile", None)
        if not callable(method):
            return {}
        try:
            profile = await method(user_id)
        except Exception:
            return {}
        return profile if isinstance(profile, dict) else {}

    async def get_profile_delta(self, user_id: str, date) -> dict:
        return {
            "available": False,
            "likes_added": [],
            "dislikes_added": [],
            "nicknames_added": [],
            "metadata": {"reason": "engram_profile_delta_unavailable"},
        }

    async def get_interaction_stats(self, user_id: str, date) -> dict:
        stats = _safe_call(self.db, "get_message_stats", user_id, default={}) or {}
        start, end = _to_date_range(date)
        raw_messages = _safe_call(
            self.db,
            "get_all_raw_messages",
            user_id,
            start,
            end,
            None,
            default=[],
        ) or []
        user_messages = [
            item for item in raw_messages if getattr(item, "role", None) == "user"
        ]
        return {
            "available": True,
            "valid_messages": len(user_messages) or int(stats.get("user_messages") or 0),
            "valid_turns": len(user_messages),
            "first_interaction_at": str(getattr(raw_messages[0], "timestamp", "")) if raw_messages else None,
            "last_interaction_at": str(getattr(raw_messages[-1], "timestamp", "")) if raw_messages else None,
            "total_chat_days": int(
                ((await self.get_user_profile(user_id)).get("social_graph", {}))
                .get("interaction_stats", {})
                .get("total_chat_days", 0)
                or 0
            ),
            "source_stats": stats,
        }

    async def get_conversation_messages(self, user_id, event_date) -> list[dict]:
        start, end = _to_date_range(event_date)
        rows = _safe_call(
            self.db,
            "get_all_raw_messages",
            user_id,
            start,
            end,
            None,
            default=[],
        ) or []
        result = []
        for row in rows:
            result.append(
                {
                    "message_id": str(getattr(row, "uuid", "") or ""),
                    "user_id": str(getattr(row, "user_id", "") or ""),
                    "role": str(getattr(row, "role", "") or ""),
                    "content": str(getattr(row, "content", "") or ""),
                    "created_at": str(getattr(row, "timestamp", "") or ""),
                    "chat_type": "group" if getattr(row, "group_id", None) else "private",
                    "group_id": getattr(row, "group_id", None),
                    "member_id": getattr(row, "member_id", None),
                    "session_id": str(getattr(row, "session_id", "") or ""),
                }
            )
        return result
    async def get_daily_summary(self, user_id: str, date) -> dict:
        summaries = _safe_call(
            self.db,
            "get_summaries_by_type",
            user_id,
            "daily",
            days=7,
            default=[],
        ) or []
        if not summaries:
            return {"available": False, "summary": "", "tags": []}
        item = summaries[0]
        return {
            "available": True,
            "summary": str(getattr(item, "summary", "") or ""),
            "tags": [str(getattr(item, "source_type", "") or "daily")],
            "memory_id": str(getattr(item, "index_id", "") or ""),
        }

    async def get_memory_refs(self, user_id: str, date, limit: int = 5) -> list[dict]:
        start, end = _to_date_range(date)
        memories = _safe_call(
            self.db,
            "get_memories_in_range",
            user_id,
            start,
            end,
            default=None,
        )
        if memories is None:
            memories = _safe_call(self.db, "get_memory_list", user_id, limit, default=[]) or []
        refs = []
        for item in list(memories)[: max(1, int(limit))]:
            refs.append(
                {
                    "memory_id": str(getattr(item, "index_id", "") or ""),
                    "summary": str(getattr(item, "summary", "") or "")[:160],
                    "created_at": str(getattr(item, "created_at", "") or ""),
                    "source_type": str(getattr(item, "source_type", "") or ""),
                }
            )
        return refs

    async def get_memory_count(self, user_id: str) -> int:
        memories = _safe_call(self.db, "get_memory_list", user_id, 10000, default=None)
        if memories is None:
            return 0
        return len(memories)

    async def get_legacy_bond_snapshot(self, user_id: str) -> dict | None:
        profile = await self.get_user_profile(user_id)
        memory_count = await self.get_memory_count(user_id)
        if not profile and memory_count <= 0:
            return None

        preferences = profile.get("preferences", {}) if isinstance(profile, dict) else {}
        likes_count = sum(
            len(preferences.get(key, []) or [])
            for key in ("likes", "favorite_foods", "favorite_items", "favorite_activities")
        )
        dislikes_count = len(preferences.get("dislikes", []) or [])
        social = profile.get("social_graph", {}) if isinstance(profile, dict) else {}
        total_chat_days = int(
            (social.get("interaction_stats", {}) or {}).get("total_chat_days", 0) or 0
        )
        profile_depth_pct = 0
        old_level = 1
        if self.bond_calculator is not None:
            try:
                profile_depth_pct = int(self.bond_calculator.calculate_profile_depth(profile))
                bond = self.bond_calculator.calculate_bond_level(memory_count, profile)
                old_level = int(bond.get("level") or 1)
                old_days_score = float((bond.get("breakdown", {}) or {}).get("days_score") or 0)
            except Exception:
                old_days_score = min(25, total_chat_days / 180 * 25)
        else:
            old_days_score = min(25, total_chat_days / 180 * 25)

        return {
            "memory_count": memory_count,
            "old_days_score": old_days_score,
            "profile_depth_pct": profile_depth_pct,
            "likes_count": likes_count,
            "dislikes_count": dislikes_count,
            "shared_secret": bool(profile.get("shared_secrets", False)) if isinstance(profile, dict) else False,
            "old_level": old_level,
            "legacy_profile": profile,
        }

    async def get_all_known_user_ids(self) -> list[str]:
        users = _safe_call(self.db, "get_all_user_ids", default=[]) or []
        return [str(user_id) for user_id in users if str(user_id or "").strip()]

