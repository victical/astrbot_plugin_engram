"""
用户画像管理器 (Profile Manager)

负责用户画像的 CRUD 操作、每日深度更新、互动统计维护等。
"""

import os
import json
import asyncio
import datetime
from datetime import date
from typing import Any, Dict, List

from astrbot.api import logger

from ..services.profile_guardian import ProfileGuardian


class ProfileManager:
    """用户画像管理器"""

    def __init__(self, context, config, data_dir, executor, db_manager):
        self.context = context
        self.config = config
        self.data_dir = data_dir
        self.executor = executor
        self.db = db_manager

        self.profiles_dir = os.path.join(self.data_dir, "engram_personas")
        self.history_dir = os.path.join(self.profiles_dir, "history")
        os.makedirs(self.profiles_dir, exist_ok=True)
        os.makedirs(self.history_dir, exist_ok=True)

        self._enable_profile_meta = self.config.get("enable_profile_meta", True)
        self._profile_history_limit = int(self.config.get("profile_history_limit", 5) or 5)
        self._profile_preference_ttl_days = int(self.config.get("profile_preference_ttl_days", 90) or 90)

        self._guardian = ProfileGuardian(config=config)

    def _build_default_profile(self, user_id: str) -> Dict[str, Any]:
        profile = {
            "basic_info": {
                "qq_id": user_id,
                "nickname": "未知",
                "gender": "未知",
                "age": "未知",
                "location": "未知",
                "job": "未知",
                "avatar_url": "",
                "birthday": "未知",
                "constellation": "未知",
                "zodiac": "未知",
                "signature": "暂无个性签名"
            },
            "attributes": {
                "personality_tags": [],
                "hobbies": [],
                "skills": []
            },
            "preferences": {
                "favorite_foods": [],
                "favorite_items": [],
                "favorite_activities": [],
                "likes": [],
                "dislikes": []
            },
            "social_graph": {
                "relationship_status": "萍水相逢",
                "important_people": [],
                "interaction_stats": {
                    "first_chat_date": None,
                    "last_chat_date": None,
                    "total_chat_days": 0,
                    "total_valid_chats": 0
                }
            },
            "dev_metadata": {
                "os": [],
                "tech_stack": []
            },
            "shared_secrets": False,
            "pending_proposals": []
        }

        if self._enable_profile_meta:
            profile["_meta"] = {
                "updated_at": None,
                "fields": {}
            }

        return profile

    def _get_profile_path(self, user_id):
        return os.path.join(self.profiles_dir, f"{user_id}.json")

    def _get_profile_history_path(self, user_id):
        return os.path.join(self.history_dir, f"{user_id}.json")

    async def get_user_profile(self, user_id):
        loop = asyncio.get_event_loop()
        path = self._get_profile_path(user_id)

        def _read():
            default_profile = self._build_default_profile(user_id)

            if not os.path.exists(path):
                return default_profile

            try:
                with open(path, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
            except Exception as e:
                logger.debug(f"Engram 画像管理器：读取画像失败（{path}），已回退为空画像：{e}")
                return default_profile

            if not isinstance(loaded, dict):
                return default_profile

            for top_key, default_val in default_profile.items():
                if top_key not in loaded:
                    loaded[top_key] = default_val

            return loaded

        return await loop.run_in_executor(self.executor, _read)

    async def update_user_profile(self, user_id, update_data):
        if not update_data:
            return

        loop = asyncio.get_event_loop()
        path = self._get_profile_path(user_id)

        def _update():
            profile = self._build_default_profile(user_id)
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        loaded = json.load(f)
                        if isinstance(loaded, dict):
                            profile.update(loaded)
                except Exception as e:
                    logger.debug(f"Engram 画像管理器：加载已有画像失败（{path}），继续使用默认画像：{e}")

            for key, value in update_data.items():
                if isinstance(value, list):
                    old_list = profile.get(key, [])
                    if not isinstance(old_list, list):
                        old_list = [old_list]
                    profile[key] = list(set(old_list + value))
                elif isinstance(value, dict):
                    old_dict = profile.get(key, {})
                    if not isinstance(old_dict, dict):
                        old_dict = {}
                    old_dict.update(value)
                    profile[key] = old_dict
                else:
                    profile[key] = value

            with open(path, 'w', encoding='utf-8') as f:
                json.dump(profile, f, ensure_ascii=False, indent=4)
            return profile

        return await loop.run_in_executor(self.executor, _update)

    async def remove_profile_list_item(self, user_id: str, field_path: str, value: str) -> tuple:
        loop = asyncio.get_event_loop()
        path = self._get_profile_path(user_id)

        field_path = str(field_path or "").strip()
        value = str(value or "").strip()

        if not field_path or not value:
            return False, "字段和值不能为空。"

        def _remove():
            profile = self._build_default_profile(user_id)
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        loaded = json.load(f)
                        if isinstance(loaded, dict):
                            profile.update(loaded)
                except Exception as e:
                    logger.debug(f"Engram 画像管理器：加载已有画像失败（{path}），继续使用默认画像：{e}")

            keys = field_path.split(".")
            target = profile
            for k in keys[:-1]:
                if not isinstance(target, dict) or k not in target:
                    return False, "字段不存在。"
                target = target.get(k)

            last_key = keys[-1]
            if not isinstance(target, dict) or last_key not in target:
                return False, "字段不存在。"

            lst = target.get(last_key)
            if not isinstance(lst, list):
                return False, "该字段不是列表类型，无法删除单项。"

            if value not in lst:
                return False, "未找到要删除的值。"

            target[last_key] = [item for item in lst if str(item) != value]

            proposals = profile.get("pending_proposals")
            if isinstance(proposals, list):
                profile["pending_proposals"] = [
                    p for p in proposals
                    if not (p.get("category") == last_key and str(p.get("value")) == value)
                ]

            if self._enable_profile_meta:
                meta = profile.get("_meta", {}) if isinstance(profile.get("_meta"), dict) else {}
                fields = meta.get("fields", {}) if isinstance(meta.get("fields"), dict) else {}
                fields.pop(f"{field_path}.{value}", None)
                meta["fields"] = fields
                profile["_meta"] = meta

            with open(path, 'w', encoding='utf-8') as f:
                json.dump(profile, f, ensure_ascii=False, indent=4)

            return True, "删除成功"

        return await loop.run_in_executor(self.executor, _remove)

    async def clear_user_profile(self, user_id):
        loop = asyncio.get_event_loop()
        path = self._get_profile_path(user_id)
        history_path = self._get_profile_history_path(user_id)

        def _delete():
            if os.path.exists(path):
                os.remove(path)
            if os.path.exists(history_path):
                os.remove(history_path)

        await loop.run_in_executor(self.executor, _delete)

    def _load_profile_history(self, user_id: str) -> List[Dict[str, Any]]:
        history_path = self._get_profile_history_path(user_id)
        if not os.path.exists(history_path):
            return []

        try:
            with open(history_path, 'r', encoding='utf-8') as f:
                history = json.load(f)
                if isinstance(history, list):
                    return history
        except Exception as e:
            logger.warning(f"Engram：读取画像历史失败（{history_path}）：{e}")

        return []

    def _save_profile_history(self, user_id: str, history: List[Dict[str, Any]]):
        history_path = self._get_profile_history_path(user_id)
        limit = max(1, int(self._profile_history_limit))
        trimmed = history[-limit:]

        with open(history_path, 'w', encoding='utf-8') as f:
            json.dump(trimmed, f, ensure_ascii=False, indent=2)

    def _snapshot_profile(self, user_id: str, profile: Dict[str, Any]):
        if not isinstance(profile, dict):
            return

        history = self._load_profile_history(user_id)
        history.append({
            "snapshot_at": datetime.datetime.now().isoformat(),
            "profile": profile,
        })
        self._save_profile_history(user_id, history)

    async def rollback_user_profile(self, user_id: str, steps: int = 1) -> Dict[str, Any]:
        loop = asyncio.get_event_loop()

        def _rollback():
            try:
                steps_int = int(steps)
            except (TypeError, ValueError):
                steps_int = 1
            steps_int = max(1, steps_int)

            history = self._load_profile_history(user_id)
            if not history:
                return {
                    "success": False,
                    "message": "无可回滚的历史版本",
                    "remaining": 0,
                }

            if steps_int > len(history):
                steps_int = len(history)

            target = history[-steps_int].get("profile")
            if not isinstance(target, dict):
                return {
                    "success": False,
                    "message": "历史版本损坏，无法回滚",
                    "remaining": len(history),
                }

            path = self._get_profile_path(user_id)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(target, f, ensure_ascii=False, indent=4)

            remain_history = history[:-steps_int]
            self._save_profile_history(user_id, remain_history)

            return {
                "success": True,
                "message": "回滚成功",
                "remaining": len(remain_history),
                "rolled_back_steps": steps_int,
            }

        return await loop.run_in_executor(self.executor, _rollback)

    def _merge_profile_meta(self, old_meta: Dict[str, Any], accepted_updates: List[str], evidence_ref: str) -> Dict[str, Any]:
        meta = old_meta if isinstance(old_meta, dict) else {}
        fields = meta.get("fields", {})
        if not isinstance(fields, dict):
            fields = {}

        now_iso = datetime.datetime.now().isoformat()

        for field_path in accepted_updates or []:
            if not field_path:
                continue

            field_meta = fields.get(field_path, {})
            if not isinstance(field_meta, dict):
                field_meta = {}

            field_meta["last_seen_at"] = now_iso
            field_meta["evidence_count"] = int(field_meta.get("evidence_count", 0)) + 1

            refs = field_meta.get("evidence_refs", [])
            if not isinstance(refs, list):
                refs = []
            if evidence_ref:
                refs.append(evidence_ref)
            field_meta["evidence_refs"] = refs[-10:]

            fields[field_path] = field_meta

        return {
            "updated_at": now_iso,
            "fields": fields,
        }

    def _decay_stale_preferences(self, profile: Dict[str, Any], now: datetime.datetime) -> Dict[str, Any]:
        ttl_days = max(1, int(self._profile_preference_ttl_days))
        ttl_delta = datetime.timedelta(days=ttl_days)

        prefs = profile.get("preferences", {})
        if not isinstance(prefs, dict):
            return profile

        meta = profile.get("_meta", {})
        fields_meta = meta.get("fields", {}) if isinstance(meta, dict) else {}
        if not isinstance(fields_meta, dict):
            fields_meta = {}

        removed_paths = []
        for category in ["likes", "dislikes"]:
            values = prefs.get(category, [])
            if not isinstance(values, list):
                continue

            keep_values = []
            for item in values:
                item_path = f"preferences.{category}.{item}"
                item_meta = fields_meta.get(item_path, {})
                last_seen = item_meta.get("last_seen_at") if isinstance(item_meta, dict) else None

                if not last_seen:
                    keep_values.append(item)
                    continue

                try:
                    last_seen_dt = datetime.datetime.fromisoformat(last_seen)
                except Exception:
                    keep_values.append(item)
                    continue

                if now - last_seen_dt > ttl_delta:
                    removed_paths.append(item_path)
                    continue

                keep_values.append(item)

            prefs[category] = keep_values

        for path in removed_paths:
            fields_meta.pop(path, None)

        if isinstance(meta, dict):
            meta["fields"] = fields_meta
            profile["_meta"] = meta
        profile["preferences"] = prefs
        return profile

    def _build_evidence_ref(self, memories: List[Any], now: datetime.datetime) -> str:
        ids = []
        for m in memories[:5]:
            idx = getattr(m, "index_id", None)
            if idx:
                ids.append(str(idx))
        if ids:
            return f"memory_index:{','.join(ids)}"
        return f"persona_daily:{now.date().isoformat()}"

    async def get_profile_evidence_summary(self, user_id: str, top_n: int = 8) -> List[Dict[str, Any]]:
        profile = await self.get_user_profile(user_id)
        meta = profile.get("_meta", {}) if isinstance(profile, dict) else {}
        fields = meta.get("fields", {}) if isinstance(meta, dict) else {}
        if not isinstance(fields, dict):
            return []

        entries = []
        for field_path, info in fields.items():
            if not isinstance(info, dict):
                continue
            entries.append({
                "field": field_path,
                "last_seen_at": info.get("last_seen_at"),
                "evidence_count": int(info.get("evidence_count", 0)),
                "latest_evidence": (info.get("evidence_refs") or [None])[-1],
            })

        entries.sort(key=lambda x: (x.get("evidence_count", 0), x.get("last_seen_at") or ""), reverse=True)

        try:
            top_n_int = max(1, int(top_n))
        except (TypeError, ValueError):
            top_n_int = 8

        return entries[:top_n_int]

    async def update_persona_daily(self, user_id, start_time=None, end_time=None):
        loop = asyncio.get_event_loop()

        if start_time is not None:
            if end_time is not None:
                memories = await loop.run_in_executor(
                    self.executor,
                    lambda: self.db.get_memories_in_range(user_id, start_time, end_time)
                )
            else:
                memories = await loop.run_in_executor(
                    self.executor,
                    self.db.get_memories_since,
                    user_id,
                    start_time
                )
        else:
            today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            memories = await loop.run_in_executor(self.executor, self.db.get_memories_since, user_id, today)

        if not memories:
            return

        current_persona = await self.get_user_profile(user_id)
        memory_texts = "\n".join([f"- {m.summary}" for m in memories])

        custom_prompt = self.config.get("persona_update_prompt", "{{current_persona}}\n{{memory_texts}}")
        prompt = (
            custom_prompt
            .replace("{{current_persona}}", json.dumps(current_persona, ensure_ascii=False, indent=2))
            .replace("{{memory_texts}}", memory_texts)
        )

        logger.debug(f"Engram：开始更新画像，user_id={user_id}，memory_count={len(memories)}")
        if len(memories) <= 5:
            logger.debug(f"Engram：用于画像更新的记忆文本：\n{memory_texts}")

        try:
            persona_model = self.config.get("persona_model", "").strip()
            if persona_model:
                provider = self.context.get_provider_by_id(persona_model)
                if not provider:
                    provider = self.context.get_using_provider()
            else:
                provider = self.context.get_using_provider()

            if not provider:
                return

            resp = await provider.text_chat(prompt=prompt)
            content = resp.completion_text

            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "{" in content:
                content = content[content.find("{"):content.rfind("}") + 1]

            proposal = json.loads(content)

            validated_persona, conflicts, decisions = self._guardian.validate_update(
                current_persona,
                proposal,
                memory_texts,
            )

            now = datetime.datetime.now()
            if self._enable_profile_meta:
                evidence_ref = self._build_evidence_ref(memories, now)
                validated_persona["_meta"] = self._merge_profile_meta(
                    current_persona.get("_meta", {}),
                    decisions.get("accepted_fields", []),
                    evidence_ref,
                )

            validated_persona = self._decay_stale_preferences(validated_persona, now)

            path = self._get_profile_path(user_id)

            def _write():
                self._snapshot_profile(user_id, current_persona)
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(validated_persona, f, ensure_ascii=False, indent=4)

            await loop.run_in_executor(self.executor, _write)

            if conflicts:
                logger.warning(f"Engram：user_id={user_id} 本次画像更新存在冲突项，已转入 pending")

        except Exception as e:
            logger.error(f"Engram：每日画像更新异常：{e}")

    async def update_interaction_stats(self, user_id):
        loop = asyncio.get_event_loop()
        profile = await self.get_user_profile(user_id)
        social = profile.get("social_graph", {})
        stats = social.get("interaction_stats", {})

        today = date.today().isoformat()
        last_date = stats.get("last_chat_date")

        stats["total_valid_chats"] = stats.get("total_valid_chats", 0) + 1

        if last_date is None:
            stats["first_chat_date"] = today
            stats["total_chat_days"] = 1
        elif last_date != today:
            stats["total_chat_days"] = stats.get("total_chat_days", 0) + 1

        stats["last_chat_date"] = today

        await self.update_user_profile(user_id, {
            "social_graph": {
                "interaction_stats": stats
            }
        })

        logger.debug(
            f"Engram：用户 {user_id} 互动统计已更新：days={stats.get('total_chat_days', 0)}, "
            f"chats={stats.get('total_valid_chats', 0)}"
        )

        return stats
