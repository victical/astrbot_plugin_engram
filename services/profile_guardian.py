"""
画像更新防护器 (Profile Guardian)

负责画像更新时的幻觉阻断、冲突检测、置信度管理。
防止 LLM 产生的错误信息污染用户画像。
"""

import re
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple
from astrbot.api import logger


# 冲突词对定义（互斥关系）
CONFLICT_PAIRS = [
    # 喜好 vs 讨厌
    ({"喜欢", "爱", "最爱", "超爱"}, {"讨厌", "不喜欢", "恨", "反感", "厌恶"}),
    # 性格冲突
    ({"外向", "活泼", "开朗"}, {"内向", "安静", "害羞"}),
    ({"严谨", "认真", "细心"}, {"粗心", "随意", "马虎"}),
    # 饮食冲突
    ({"吃肉", "肉食"}, {"素食", "吃素"}),
    # 动物过敏冲突
    ({"猫", "养猫", "喜欢猫"}, {"猫毛过敏", "对猫过敏"}),
    ({"狗", "养狗", "喜欢狗"}, {"狗毛过敏", "对狗过敏"}),
]

STRONG_EVIDENCE_PATTERNS = {
    "gender": [
        r"我是(男|女|男生|女生|男孩子|女孩子|男人|女人)(?!朋友|票|神|生朋友)",
        r"我是个(男|女)(?!朋友|票|神)",
        r"我的性别[是为](男|女)",
        r"性别[是为](男|女)",
    ],
    "age": [
        r"我(\d+)岁[了]?",
        r"今年(\d+)岁",
        r"出生于(\d{4})年",
        r"生日[是为]?(\d{4}[-年]\d{1,2}[-月]\d{1,2}[日]?)",
    ],
    "location": [
        r"我在(.+?)(居住|生活|工作|上学|读书)",
        r"我家在(.+?)(居住|生活)?",
        r"住在(.+?)(市|区|县|省)",
        r"我是(.+?)(人|本地人)",
        r"来自(.+?)(省|市|区|县)",
    ],
    "job": [
        r"我是(.*)(工程师|程序员|设计师|老师|医生|学生|护士|警察|律师)",
        r"我在(.+?)(工作|上班|当差|服役)",
        r"我的职业[是为](.+)",
        r"我做(.+?)(工作|职业|行业)",
        r"我是做(.+?)的",
        r"当(.+?)(工程师|老师|医生|司机)",
    ],
}

PROTECTED_BASIC_FIELDS = [
    "qq_id", "nickname", "avatar_url", "signature",
    "birthday", "constellation", "zodiac",
    "gender", "age", "location", "job"
]

ATTRIBUTE_CATEGORIES = {"personality_tags", "hobbies", "skills"}
PREFERENCE_CATEGORIES = {"favorite_foods", "favorite_items", "favorite_activities", "likes", "dislikes"}


class ProfileGuardian:
    """画像更新防护器"""

    FIELD_LAYERS = {
        "basic_info": "fact",
        "attributes": "inference",
        "preferences": "preference",
        "social_graph": "fact",
        "dev_metadata": "fact",
        "shared_secrets": "inference",
    }

    def __init__(self, config: Optional[dict] = None):
        self._config = config or {}
        self._enable_confidence = self._config.get("enable_profile_confidence", True)
        self._confidence_threshold = self._config.get("profile_confidence_threshold", 2)
        self._enable_conflict_detection = self._config.get("enable_conflict_detection", True)
        self._enable_strong_evidence = self._config.get("enable_strong_evidence_protection", True)

    def validate_update(
        self,
        current_profile: Dict[str, Any],
        new_profile: Dict[str, Any],
        memory_texts: str
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]]:
        """验证并过滤画像更新，返回结构化决策结果。"""
        validated: Dict[str, Any] = {}
        conflicts: List[Dict[str, Any]] = []

        decisions: Dict[str, Any] = {
            "accepted_fields": [],
            "rejected_fields": [],
            "pending_fields": [],
            "reasons": [],
            "field_layers": self.FIELD_LAYERS,
            "evidence_snippets": {},
        }

        basic_info, basic_reasons, evidence_snippets = self._protect_basic_info(
            current_profile.get("basic_info", {}),
            new_profile.get("basic_info", {}),
            memory_texts,
        )
        validated["basic_info"] = basic_info
        decisions["reasons"].extend(basic_reasons)
        decisions["evidence_snippets"].update(evidence_snippets)

        current_proposals = current_profile.get("pending_proposals", [])
        new_attributes, updated_proposals = self._process_attributes_with_confidence(
            current_profile.get("attributes", {}),
            new_profile.get("attributes", {}),
            current_proposals,
        )
        validated["attributes"] = new_attributes

        merged_preferences, pref_conflicts, pref_pending = self._merge_preferences_with_conflict_detection(
            current_profile.get("preferences", {}),
            new_profile.get("preferences", {}),
        )
        validated["preferences"] = merged_preferences
        conflicts.extend(pref_conflicts)

        # social_graph 保留系统维护字段
        old_stats = current_profile.get("social_graph", {}).get("interaction_stats", {})
        new_social = new_profile.get("social_graph", {})
        if old_stats:
            new_social["interaction_stats"] = old_stats
        validated["social_graph"] = new_social

        # 其他字段
        for key in ["dev_metadata", "shared_secrets"]:
            if key in new_profile:
                validated[key] = new_profile[key]
            elif key in current_profile:
                validated[key] = current_profile[key]

        # pending_proposals: 属性提案 + 冲突挂起提案
        all_pending = self._dedupe_proposals(updated_proposals + pref_pending)
        validated["pending_proposals"] = all_pending

        accepted_fields, rejected_fields = self._derive_accept_reject_paths(
            current_profile,
            new_profile,
            validated,
        )
        decisions["accepted_fields"] = sorted(set(accepted_fields))
        decisions["rejected_fields"] = sorted(set(rejected_fields))

        pending_fields = [self._proposal_to_field_path(p) for p in all_pending]
        decisions["pending_fields"] = sorted(set([p for p in pending_fields if p]))

        for proposal in all_pending:
            field_path = self._proposal_to_field_path(proposal)
            if not field_path:
                continue
            reason = proposal.get("reason") or "等待后续证据确认"
            decisions["reasons"].append({
                "field": field_path,
                "decision": "pending",
                "reason": reason,
            })

        for conflict in pref_conflicts:
            decisions["reasons"].append({
                "field": f"preferences.{conflict.get('category', 'unknown')}.{conflict.get('new_value', '')}",
                "decision": "pending",
                "reason": conflict.get("detail", "偏好冲突，已挂起"),
            })

        # 去重 reasons
        unique_reasons = []
        seen_reason_key = set()
        for item in decisions["reasons"]:
            key = (item.get("field"), item.get("decision"), item.get("reason"))
            if key in seen_reason_key:
                continue
            seen_reason_key.add(key)
            unique_reasons.append(item)
        decisions["reasons"] = unique_reasons

        if conflicts:
            logger.warning(f"Engram：检测到画像更新冲突，共 {len(conflicts)} 项")
            for c in conflicts:
                logger.warning(f"  - 冲突类型={c['type']}：{c['detail']}")

        return validated, conflicts, decisions

    def _protect_basic_info(
        self,
        old_basic: Dict[str, Any],
        new_basic: Dict[str, Any],
        memory_texts: str,
    ) -> Tuple[Dict[str, Any], List[Dict[str, str]], Dict[str, str]]:
        """保护 basic_info 核心字段。"""
        result = dict(new_basic)
        reasons: List[Dict[str, str]] = []
        evidence_snippets: Dict[str, str] = {}

        fully_protected = [
            "qq_id", "nickname", "avatar_url", "signature",
            "birthday", "constellation", "zodiac",
        ]
        for field in fully_protected:
            old_val = old_basic.get(field)
            new_val = new_basic.get(field)
            if old_val and old_val != "未知" and old_val != "":
                if new_val is not None and new_val != old_val:
                    reasons.append({
                        "field": f"basic_info.{field}",
                        "decision": "rejected",
                        "reason": "系统保护字段，禁止 LLM 覆盖",
                    })
                result[field] = old_val

        evidence_protected = ["gender", "age", "location", "job"]
        if self._enable_strong_evidence:
            for field in evidence_protected:
                old_val = old_basic.get(field)
                new_val = new_basic.get(field)
                if old_val and old_val != "未知" and old_val != "" and new_val != old_val:
                    snippet = self._extract_strong_evidence(field, memory_texts)
                    if snippet:
                        logger.info(f"Engram：字段 basic_info.{field} 命中强证据，已更新：{old_val} -> {new_val}")
                        evidence_snippets[f"basic_info.{field}"] = snippet
                    else:
                        result[field] = old_val
                        reasons.append({
                            "field": f"basic_info.{field}",
                            "decision": "rejected",
                            "reason": "缺少强证据，变更已拦截",
                        })
                        logger.debug(
                            f"Engram：字段 basic_info.{field} 变更已拦截（缺少强证据）：{old_val} vs {new_val}"
                        )

        return result, reasons, evidence_snippets

    def _extract_strong_evidence(self, field: str, memory_texts: str) -> Optional[str]:
        patterns = STRONG_EVIDENCE_PATTERNS.get(field, [])
        for pattern in patterns:
            m = re.search(pattern, memory_texts, re.IGNORECASE)
            if m:
                return m.group(0)
        return None

    def _process_attributes_with_confidence(
        self,
        old_attrs: Dict[str, List[str]],
        new_attrs: Dict[str, List[str]],
        current_proposals: List[Dict[str, Any]],
    ) -> Tuple[Dict[str, List[str]], List[Dict[str, Any]]]:
        """属性置信度晋升机制。"""
        result_attrs: Dict[str, List[str]] = {}
        next_proposals: List[Dict[str, Any]] = []

        proposal_map: Dict[str, Dict[str, Any]] = {}
        for proposal in current_proposals:
            category = proposal.get("category")
            value = proposal.get("value")
            if not category or value is None:
                continue
            proposal_map[f"{category}:{value}"] = dict(proposal)

        categories = ["personality_tags", "hobbies", "skills"]
        now_iso = datetime.now().isoformat()

        for category in categories:
            current_set = set(old_attrs.get(category, []) or [])
            llm_suggested_set = set(new_attrs.get(category, []) or [])
            final_list = list(current_set)

            for item in llm_suggested_set:
                if item in current_set:
                    continue

                if not self._enable_confidence:
                    final_list.append(item)
                    continue

                key = f"{category}:{item}"
                if key in proposal_map:
                    prop = proposal_map[key]
                    prop["confidence"] = int(prop.get("confidence", 0)) + 1
                    prop["last_seen"] = now_iso

                    if prop["confidence"] >= self._confidence_threshold:
                        logger.info(
                            f"Engram：提案已晋升为正式属性：{category}.{item}（置信度={prop['confidence']}）"
                        )
                        final_list.append(item)
                    else:
                        next_proposals.append(prop)
                else:
                    new_prop = {
                        "category": category,
                        "value": item,
                        "confidence": 1,
                        "first_seen": now_iso,
                        "last_seen": now_iso,
                        "layer": "inference",
                        "reason": "属性首次出现，等待置信度晋升",
                    }
                    if self._confidence_threshold > 1:
                        next_proposals.append(new_prop)
                        logger.debug(f"Engram：新增属性提案：{category}.{item}")
                    else:
                        final_list.append(item)

            result_attrs[category] = final_list

        current_keys = {f"{p.get('category')}:{p.get('value')}" for p in next_proposals}
        for key, prop in proposal_map.items():
            if key in current_keys:
                continue
            cat = prop.get("category")
            val = prop.get("value")
            if val in result_attrs.get(cat, []):
                continue
            next_proposals.append(prop)

        return result_attrs, next_proposals

    def _merge_preferences_with_conflict_detection(
        self,
        old_prefs: Dict[str, Any],
        new_prefs: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
        """合并偏好并将冲突项转入 pending。"""
        result: Dict[str, Any] = {}
        conflicts: List[Dict[str, Any]] = []
        pending: List[Dict[str, Any]] = []
        now_iso = datetime.now().isoformat()

        for category in ["favorite_foods", "favorite_items", "favorite_activities", "likes", "dislikes"]:
            old_list = set(old_prefs.get(category, []) or [])
            new_list = set(new_prefs.get(category, []) or [])

            if not self._enable_conflict_detection:
                result[category] = list(old_list | new_list)
                continue

            filtered_new = set()
            for new_item in new_list:
                is_conflict = False
                for old_item in old_list:
                    conflict_type = self._check_item_conflict(old_item, new_item)
                    if conflict_type:
                        detail = f"'{old_item}' vs '{new_item}' in {category}"
                        conflicts.append({
                            "type": "preference_conflict",
                            "category": category,
                            "old_value": old_item,
                            "new_value": new_item,
                            "conflict_type": conflict_type,
                            "detail": detail,
                        })
                        pending.append({
                            "category": category,
                            "value": new_item,
                            "confidence": 1,
                            "first_seen": now_iso,
                            "last_seen": now_iso,
                            "layer": "preference",
                            "reason": f"偏好冲突挂起：{detail}",
                        })
                        is_conflict = True
                        break

                if not is_conflict:
                    filtered_new.add(new_item)

            result[category] = list(old_list | filtered_new)

        return result, conflicts, pending

    def _check_item_conflict(self, old_item: str, new_item: str) -> Optional[str]:
        old_lower = str(old_item).lower()
        new_lower = str(new_item).lower()

        for positive_set, negative_set in CONFLICT_PAIRS:
            old_positive = any(p in old_lower for p in positive_set)
            old_negative = any(n in old_lower for n in negative_set)
            new_positive = any(p in new_lower for p in positive_set)
            new_negative = any(n in new_lower for n in negative_set)

            if (old_positive and new_negative) or (old_negative and new_positive):
                return "sentiment_conflict"

            if "过敏" in old_lower and "过敏" not in new_lower:
                for positive_terms in [{"猫", "狗", "花生", "海鲜", "芒果"}]:
                    if any(p in old_lower for p in positive_terms) and any(p in new_lower for p in positive_terms):
                        return "allergy_conflict"

        return None

    def _flatten_leaf_values(self, data: Any, prefix: str = "") -> Dict[str, Any]:
        """展开为 path -> value，列表展开为 item path。"""
        result: Dict[str, Any] = {}
        if isinstance(data, dict):
            for k, v in data.items():
                child_prefix = f"{prefix}.{k}" if prefix else str(k)
                result.update(self._flatten_leaf_values(v, child_prefix))
            return result

        if isinstance(data, list):
            for item in data:
                item_path = f"{prefix}.{item}"
                result[item_path] = item
            return result

        if prefix:
            result[prefix] = data
        return result

    def _derive_accept_reject_paths(
        self,
        current_profile: Dict[str, Any],
        new_profile: Dict[str, Any],
        validated_profile: Dict[str, Any],
    ) -> Tuple[List[str], List[str]]:
        current_flat = self._flatten_leaf_values(current_profile)
        new_flat = self._flatten_leaf_values(new_profile)
        validated_flat = self._flatten_leaf_values(validated_profile)

        accepted: List[str] = []
        rejected: List[str] = []
        sentinel = object()

        for path, new_val in new_flat.items():
            current_val = current_flat.get(path, sentinel)
            validated_val = validated_flat.get(path, sentinel)

            if current_val == new_val:
                continue

            if validated_val == new_val:
                accepted.append(path)
            elif validated_val == current_val:
                rejected.append(path)
            elif validated_val is sentinel:
                rejected.append(path)

        return accepted, rejected

    def _proposal_to_field_path(self, proposal: Dict[str, Any]) -> str:
        category = proposal.get("category")
        value = proposal.get("value")
        if not category or value is None:
            return ""

        if category in ATTRIBUTE_CATEGORIES:
            return f"attributes.{category}.{value}"
        if category in PREFERENCE_CATEGORIES:
            return f"preferences.{category}.{value}"
        return f"{category}.{value}"

    def _dedupe_proposals(self, proposals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        merged: Dict[str, Dict[str, Any]] = {}
        for p in proposals:
            key = f"{p.get('category')}:{p.get('value')}"
            if key not in merged:
                merged[key] = dict(p)
                continue

            exist = merged[key]
            exist["confidence"] = max(int(exist.get("confidence", 0)), int(p.get("confidence", 0)))
            exist["last_seen"] = max(str(exist.get("last_seen", "")), str(p.get("last_seen", "")))
            if not exist.get("first_seen"):
                exist["first_seen"] = p.get("first_seen")
            if p.get("reason"):
                exist["reason"] = p.get("reason")
            if p.get("layer") and not exist.get("layer"):
                exist["layer"] = p.get("layer")

        return list(merged.values())
