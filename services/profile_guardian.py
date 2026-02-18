"""
画像更新防护器 (Profile Guardian)

负责画像更新时的幻觉阻断、冲突检测、置信度管理。
防止 LLM 产生的错误信息污染用户画像。

主要功能：
- 置信度机制：新属性作为提案，需多次确认才能转正
- 冲突检测：检测新旧属性之间的矛盾
- basic_info 写保护：核心字段需强证据才可修改
- 事实 > 观点：区分客观事实和主观推断

依赖：
- profile_manager: 画像管理器
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

# 强证据关键词（用于 basic_info 修改）
# 修正原则：
# 1. 排除 "男朋友/女朋友" 等干扰词
# 2. 支持 "我是学生" 这种无修饰语的表达
# 3. 增加 "做...工作的" 句式
STRONG_EVIDENCE_PATTERNS = {
    "gender": [
        # 排除 "男朋友", "女朋友", "男神", "女神", "男票", "女票"
        # 匹配 "我是男的", "我是男生", 但不匹配 "我是男朋友"
        r"我是(男|女|男生|女生|男孩子|女孩子|男人|女人)(?!朋友|票|神|生朋友)",
        r"我是个(男|女)(?!朋友|票|神)",
        r"我的性别[是为](男|女)",
        r"性别[是为](男|女)",
    ],
    "age": [
        r"我(\d+)岁[了]?",
        r"今年(\d+)岁",
        r"出生于(\d{4})年",
        # 增加容错：1990-01-01
        r"生日[是为]?(\d{4}[-年]\d{1,2}[-月]\d{1,2}[日]?)",
    ],
    "location": [
        # 优化：支持 "我住在北京" (无后缀)
        r"我在(.+?)(居住|生活|工作|上学|读书)",
        r"我家在(.+?)(居住|生活)?",
        r"住在(.+?)(市|区|县|省)",
        r"我是(.+?)(人|本地人)",
        r"来自(.+?)(省|市|区|县)",
    ],
    "job": [
        # 修复：(.*?) 允许为空，匹配 "我是学生"
        r"我是(.*)(工程师|程序员|设计师|老师|医生|学生|护士|警察|律师)",
        r"我在(.+?)(工作|上班|当差|服役)",
        r"我的职业[是为](.+)",
        r"我做(.+?)(工作|职业|行业)",
        r"我是做(.+?)的",  # 新增："我是做IT的"
        r"当(.+?)(工程师|老师|医生|司机)", # 新增："当老师"
    ],
}

# 需要写保护的 basic_info 字段
PROTECTED_BASIC_FIELDS = [
    "qq_id", "nickname", "avatar_url", "signature",
    "birthday", "constellation", "zodiac",
    "gender", "age", "location", "job"  # 新增保护字段
]


class ProfileGuardian:
    """画像更新防护器"""
    
    def __init__(self, config: Optional[dict] = None):
        """
        初始化防护器
        
        Args:
            config: 插件配置字典
        """
        self._config = config or {}
        
        # 从配置读取防护参数
        self._enable_confidence = self._config.get("enable_profile_confidence", True)
        self._confidence_threshold = self._config.get("profile_confidence_threshold", 2)
        self._enable_conflict_detection = self._config.get("enable_conflict_detection", True)
        self._enable_strong_evidence = self._config.get("enable_strong_evidence_protection", True)
    
    def validate_update(
        self, 
        current_profile: Dict[str, Any], 
        new_profile: Dict[str, Any],
        memory_texts: str
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """
        验证并过滤画像更新
        
        Args:
            current_profile: 当前画像数据
            new_profile: LLM 返回的新画像数据
            memory_texts: 用于更新的记忆文本（用于强证据检测）
            
        Returns:
            Tuple[validated_profile, conflicts]:
            - validated_profile: 经过验证的安全画像数据
            - conflicts: 检测到的冲突列表
        """
        validated = {}
        conflicts = []
        
        # 1. 保护 basic_info 中的核心字段
        validated["basic_info"] = self._protect_basic_info(
            current_profile.get("basic_info", {}),
            new_profile.get("basic_info", {}),
            memory_texts
        )
        
        # 2. 处理属性字段（核心修复：置信度晋升机制）
        current_proposals = current_profile.get("pending_proposals", [])
        new_attributes, updated_proposals = self._process_attributes_with_confidence(
            current_profile.get("attributes", {}),
            new_profile.get("attributes", {}),
            current_proposals
        )
        validated["attributes"] = new_attributes
        validated["pending_proposals"] = updated_proposals
        
        # 3. 处理偏好字段（带冲突检测）
        validated["preferences"] = self._merge_preferences_with_conflict_detection(
            current_profile.get("preferences", {}),
            new_profile.get("preferences", {}),
            conflicts
        )
        
        # 4. 保留 social_graph 中的 interaction_stats（系统维护）
        old_stats = current_profile.get("social_graph", {}).get("interaction_stats", {})
        new_social = new_profile.get("social_graph", {})
        if old_stats:
            new_social["interaction_stats"] = old_stats
        validated["social_graph"] = new_social
        
        # 5. 保留其他字段
        for key in ["dev_metadata", "shared_secrets"]:
            if key in new_profile:
                validated[key] = new_profile[key]
            elif key in current_profile:
                validated[key] = current_profile[key]
        
        # 记录冲突日志
        if conflicts:
            logger.warning(f"Engram: Profile update conflicts detected: {len(conflicts)} issues")
            for c in conflicts:
                logger.warning(f"  - {c['type']}: {c['detail']}")
        
        return validated, conflicts
    
    def _protect_basic_info(
        self,
        old_basic: Dict[str, Any],
        new_basic: Dict[str, Any],
        memory_texts: str
    ) -> Dict[str, Any]:
        """
        保护 basic_info 中的核心字段
        
        规则：
        1. qq_id, nickname, avatar_url, signature, birthday, constellation, zodiac
           这些字段完全由系统维护，LLM 不可修改
        2. gender, age, location, job 需要强证据才能修改
        """
        result = dict(new_basic)  # 复制新数据
        
        # 完全保护的字段：只有原值有效时才保留
        fully_protected = ["qq_id", "nickname", "avatar_url", "signature",
                          "birthday", "constellation", "zodiac"]
        for field in fully_protected:
            old_val = old_basic.get(field)
            if old_val and old_val != "未知" and old_val != "":
                result[field] = old_val
        
        # 需要强证据的字段
        evidence_protected = ["gender", "age", "location", "job"]
        if self._enable_strong_evidence:
            for field in evidence_protected:
                old_val = old_basic.get(field)
                new_val = new_basic.get(field)
                
                # 如果原值有效且新值不同，检查是否有强证据
                if old_val and old_val != "未知" and old_val != "" and new_val != old_val:
                    if self._check_strong_evidence(field, memory_texts):
                        logger.info(f"Engram: basic_info.{field} updated with strong evidence: {old_val} -> {new_val}")
                    else:
                        # 无强证据，保留原值
                        result[field] = old_val
                        logger.debug(f"Engram: basic_info.{field} change blocked (no strong evidence): {old_val} vs {new_val}")
        
        return result
    
    def _check_strong_evidence(self, field: str, memory_texts: str) -> bool:
        """
        检查记忆文本中是否包含修改指定字��的强证据
        
        Args:
            field: 字段名
            memory_texts: 记忆文本
            
        Returns:
            bool: 是否存在强证据
        """
        patterns = STRONG_EVIDENCE_PATTERNS.get(field, [])
        for pattern in patterns:
            if re.search(pattern, memory_texts, re.IGNORECASE):
                return True
        return False
    
    def _process_attributes_with_confidence(
        self,
        old_attrs: Dict[str, List[str]],
        new_attrs: Dict[str, List[str]],
        current_proposals: List[Dict[str, Any]]
    ) -> Tuple[Dict[str, List[str]], List[Dict[str, Any]]]:
        """
        处理属性置信度逻辑（真正的等候室晋升机制）
        
        Returns:
            (validated_attributes, next_proposals)
        """
        result_attrs: Dict[str, List[str]] = {}
        next_proposals: List[Dict[str, Any]] = []

        proposal_map = {}
        for proposal in current_proposals:
            category = proposal.get("category")
            value = proposal.get("value")
            if not category or value is None:
                continue
            proposal_map[f"{category}:{value}"] = proposal

        categories = ["personality_tags", "hobbies", "skills"]
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
                    prop["last_seen"] = datetime.now().isoformat()

                    if prop["confidence"] >= self._confidence_threshold:
                        logger.info(
                            f"Engram: Proposal promoted to attribute: {category}.{item} (conf={prop['confidence']})"
                        )
                        final_list.append(item)
                    else:
                        next_proposals.append(prop)
                else:
                    new_prop = {
                        "category": category,
                        "value": item,
                        "confidence": 1,
                        "first_seen": datetime.now().isoformat(),
                        "last_seen": datetime.now().isoformat()
                    }
                    if self._confidence_threshold > 1:
                        next_proposals.append(new_prop)
                        logger.debug(f"Engram: New attribute proposal: {category}.{item}")
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
        conflicts: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        合并偏好字段，同时检测冲突
        
        检测逻辑：
        1. 如果新属性与旧属性在冲突词对中，标记冲突
        2. 冲突时保留旧值（保守策略）
        """
        result = {}
        
        for category in ["favorite_foods", "favorite_items", "favorite_activities", "likes", "dislikes"]:
            old_list = set(old_prefs.get(category, []))
            new_list = set(new_prefs.get(category, []))
            
            if not self._enable_conflict_detection:
                result[category] = list(old_list | new_list)
                continue
            
            # 检测冲突
            filtered_new = set()
            for new_item in new_list:
                is_conflict = False
                for old_item in old_list:
                    conflict_type = self._check_item_conflict(old_item, new_item)
                    if conflict_type:
                        conflicts.append({
                            "type": "preference_conflict",
                            "category": category,
                            "old_value": old_item,
                            "new_value": new_item,
                            "conflict_type": conflict_type,
                            "detail": f"'{old_item}' vs '{new_item}' in {category}"
                        })
                        is_conflict = True
                        break
                
                if not is_conflict:
                    filtered_new.add(new_item)
            
            # 合并：旧值 + 过滤后的新值
            result[category] = list(old_list | filtered_new)
        
        return result
    
    def _check_item_conflict(self, old_item: str, new_item: str) -> Optional[str]:
        """
        检查两个属性项是否冲突
        
        Args:
            old_item: 旧属性值
            new_item: 新属性值
            
        Returns:
            Optional[str]: 冲突类型，无冲突返回 None
        """
        old_lower = old_item.lower()
        new_lower = new_item.lower()
        
        for positive_set, negative_set in CONFLICT_PAIRS:
            old_positive = any(p in old_lower for p in positive_set)
            old_negative = any(n in old_lower for n in negative_set)
            new_positive = any(p in new_lower for p in positive_set)
            new_negative = any(n in new_lower for n in negative_set)
            
            # 如果旧值是正向，新值是负向（或反之），则冲突
            if (old_positive and new_negative) or (old_negative and new_positive):
                return "sentiment_conflict"
            
            # 检查过敏冲突
            if "过敏" in old_lower and not "过敏" in new_lower:
                # 旧值是过敏，新值是喜欢该物品
                for positive_set in [{"猫", "狗", "花生", "海鲜", "芒果"}]:
                    if any(p in old_lower for p in positive_set) and any(p in new_lower for p in positive_set):
                        return "allergy_conflict"
        
        return None
    
