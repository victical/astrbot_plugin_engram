"""
羁绊系统计算模块
从 profile_renderer.py 中拆分出来，专注于羁绊等级和画像深度的计算逻辑
"""
import math
from typing import Dict, List, Any, Tuple


class BondCalculator:
    """羁绊系统计算器 - 负责所有与羁绊等级相关的计算"""
    
    # 7级羁绊等级名称
    LEVEL_NAMES = {
        1: "萍水相逢",
        2: "初识",
        3: "相识",
        4: "熟悉",
        5: "知己",
        6: "挚友",
        7: "灵魂共鸣"
    }
    
    def __init__(self):
        pass
    
    def calculate_profile_depth(self, profile: Dict[str, Any]) -> int:
        """
        计算画像深度百分比（v2.0 - 区分自动获取和主动提供）
        
        主动提供信息占 86%，自动获取仅占 14%
        画像深度百分比 = 主动提供得分 / 21.5 × 100%
        
        Args:
            profile: 用户画像数据
            
        Returns:
            画像深度百分比 (0-100)
        """
        attrs = profile.get("attributes", {})
        prefs = profile.get("preferences", {})
        social = profile.get("social_graph", {})
        
        # 主动提供的信息评分（满分 21.5）
        score = 0.0
        
        # 性格标签：1.5分/个，最多4个 = 6分
        personality_tags = attrs.get("personality_tags", [])
        score += min(6, len(personality_tags) * 1.5)
        
        # 爱好：1分/个，最多5个 = 5分
        hobbies = attrs.get("hobbies", [])
        score += min(5, len(hobbies) * 1)
        
        # 技能：1分/个，最多3个 = 3分
        skills = attrs.get("skills", [])
        score += min(3, len(skills) * 1)
        
        # 重要的人：2分/人，最多3人 = 6分（高权重）
        important_people = social.get("important_people", [])
        score += min(6, len(important_people) * 2)
        
        # 分享心事/秘密：1.5分
        if profile.get("shared_secrets", False):
            score += 1.5
        
        # 计算百分比（基于主动提供的满分 21.5）
        depth_pct = min(100, int(score / 21.5 * 100))
        
        return depth_pct
    
    def calculate_bond_level(
        self, 
        memory_count: int, 
        profile: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        计算羁绊等级和进度（v2.1 - 累计聊天天数版）
        
        7级体系 + 多维度评分系统
        
        Args:
            memory_count: 记忆总数
            profile: 用户画像数据
            
        Returns:
            包含等级、进度、分数明细等信息的字典
        """
        social = profile.get("social_graph", {})
        stats = social.get("interaction_stats", {})
        prefs = profile.get("preferences", {})
        
        # 获取累计聊天天数
        total_chat_days = stats.get("total_chat_days", 0)
        
        # 获取喜好/禁忌数量（包括新分类）
        likes_count = (
            len(prefs.get("likes", [])) +
            len(prefs.get("favorite_foods", [])) +
            len(prefs.get("favorite_items", [])) +
            len(prefs.get("favorite_activities", []))
        )
        dislikes_count = len(prefs.get("dislikes", []))
        
        # 获取重要的人
        important_people = social.get("important_people", [])
        
        # 检测是否分享过秘密/心事
        shared_secrets = profile.get("shared_secrets", False)
        
        # 计算画像深度百分比
        depth_pct = self.calculate_profile_depth(profile)
        
        # ========== 计算各维度分数（满分100） ==========
        
        # 1. 记忆深度评分（满分25，对数曲线增长）
        if memory_count > 0:
            memory_score = min(25, 25 * math.log(1 + memory_count / 150) / math.log(1 + 3000 / 150))
        else:
            memory_score = 0
        
        # 2. 累计聊天天数评分（满分25，阶梯增长）
        days_score = self._calculate_days_score(total_chat_days)
        
        # 3. 画像深度评分（满分25）
        depth_score = min(25, depth_pct / 100 * 25)
        
        # 4. 喜好掌握评分（满分15）
        pref_score = min(15, (likes_count + dislikes_count) * 1.5)
        
        # 5. 成就系统（满分10）
        achievements = self._calculate_achievements(
            memory_count, total_chat_days, likes_count, important_people
        )
        achievement_score = len(achievements) / 6 * 10
        
        # 总分
        total_score = memory_score + days_score + depth_score + pref_score + achievement_score
        
        # ========== 等级判定（必须同时满足多个条件） ==========
        level, level_name = self._determine_level(
            memory_count, total_chat_days, depth_pct,
            likes_count, dislikes_count, important_people,
            shared_secrets, achievements
        )
        
        # 获取升级提示
        next_hints = self.get_next_level_hints(
            level, memory_count, total_chat_days, depth_pct,
            likes_count, dislikes_count, important_people, shared_secrets, achievements
        )
        
        return {
            "level": level,
            "level_name": level_name,
            "progress": int(total_score),
            "breakdown": {
                "memory_score": round(memory_score, 1),
                "days_score": round(days_score, 1),
                "depth_score": round(depth_score, 1),
                "pref_score": round(pref_score, 1),
                "achievement_score": round(achievement_score, 1),
                "achievements": achievements
            },
            "next_level_hint": next_hints
        }
    
    def _calculate_days_score(self, total_chat_days: int) -> float:
        """计算累计聊天天数评分（满分25）"""
        if total_chat_days >= 180:
            return 25
        elif total_chat_days >= 60:
            return 20 + (total_chat_days - 60) / 120 * 5
        elif total_chat_days >= 30:
            return 15 + (total_chat_days - 30) / 30 * 5
        elif total_chat_days >= 14:
            return 10 + (total_chat_days - 14) / 16 * 5
        elif total_chat_days >= 7:
            return 5 + (total_chat_days - 7) / 7 * 5
        else:
            return total_chat_days / 7 * 5
    
    def _calculate_achievements(
        self,
        memory_count: int,
        total_chat_days: int,
        likes_count: int,
        important_people: List[str]
    ) -> List[str]:
        """计算已解锁的成就"""
        achievements = []
        
        if memory_count >= 100:
            achievements.append("百次对话")
        if memory_count >= 500:
            achievements.append("记忆达人")
        if total_chat_days >= 30:
            achievements.append("月度陪伴")
        if total_chat_days >= 100:
            achievements.append("百日相守")
        if likes_count >= 10:
            achievements.append("知心者")
        if len(important_people) >= 1:
            achievements.append("知己之交")
        
        return achievements
    
    def _determine_level(
        self,
        memory_count: int,
        total_chat_days: int,
        depth_pct: int,
        likes_count: int,
        dislikes_count: int,
        important_people: List[str],
        shared_secrets: bool,
        achievements: List[str]
    ) -> Tuple[int, str]:
        """判定羁绊等级（必须同时满足多个条件）"""
        
        # Lv.7 灵魂共鸣：3000记忆 + 180天聊天 + 画像100% + 6成就
        if memory_count >= 3000 and total_chat_days >= 180 and depth_pct >= 100 and len(achievements) >= 6:
            return 7, self.LEVEL_NAMES[7]
        
        # Lv.6 挚友：1200记忆 + 60天聊天 + 重要的人 + 5禁忌
        if memory_count >= 1200 and total_chat_days >= 60 and len(important_people) >= 1 and dislikes_count >= 5:
            return 6, self.LEVEL_NAMES[6]
        
        # Lv.5 知己：600记忆 + 30天聊天 + 分享秘密 + 5喜好
        if memory_count >= 600 and total_chat_days >= 30 and shared_secrets and likes_count >= 5:
            return 5, self.LEVEL_NAMES[5]
        
        # Lv.4 熟悉：350记忆 + 14天聊天 + 画像30%
        if memory_count >= 350 and total_chat_days >= 14 and depth_pct >= 30:
            return 4, self.LEVEL_NAMES[4]
        
        # Lv.3 相识：180记忆 + 7天聊天 + 3喜好
        if memory_count >= 180 and total_chat_days >= 7 and likes_count >= 3:
            return 3, self.LEVEL_NAMES[3]
        
        # Lv.2 初识：50记忆 + 1项主动信息
        if memory_count >= 50 and depth_pct > 0:
            return 2, self.LEVEL_NAMES[2]
        
        # Lv.1 萍水相逢（默认）
        return 1, self.LEVEL_NAMES[1]
    
    def get_next_level_hints(
        self,
        level: int,
        memory_count: int,
        total_chat_days: int,
        depth_pct: int,
        likes_count: int,
        dislikes_count: int,
        important_people: List[str],
        shared_secrets: bool,
        achievements: List[str]
    ) -> List[str]:
        """获取升级到下一等级的提示"""
        hints = []
        
        if level == 1:
            if memory_count < 50:
                hints.append(f"再积累 {50 - memory_count} 条有效聊天")
            if depth_pct == 0:
                hints.append("告诉我一些关于你的事情")
        
        elif level == 2:
            if memory_count < 180:
                hints.append(f"再积累 {180 - memory_count} 条有效聊天")
            if total_chat_days < 7:
                hints.append(f"累计聊天 ({total_chat_days}/7 天)")
            if likes_count < 3:
                hints.append(f"让我知道更多你喜欢的 ({likes_count}/3)")
        
        elif level == 3:
            if memory_count < 350:
                hints.append(f"再积累 {350 - memory_count} 条有效聊天")
            if total_chat_days < 14:
                hints.append(f"累计聊天 ({total_chat_days}/14 天)")
            if depth_pct < 30:
                hints.append(f"画像深度需达到 30% (当前 {depth_pct}%)")
        
        elif level == 4:
            if memory_count < 600:
                hints.append(f"再积累 {600 - memory_count} 条有效聊天")
            if total_chat_days < 30:
                hints.append(f"累计聊天 ({total_chat_days}/30 天)")
            if not shared_secrets:
                hints.append("试着和我分享一些心事")
            if likes_count < 5:
                hints.append(f"让我知道更多你喜欢的 ({likes_count}/5)")
        
        elif level == 5:
            if memory_count < 1200:
                hints.append(f"再积累 {1200 - memory_count} 条有效聊天")
            if total_chat_days < 60:
                hints.append(f"累计聊天 ({total_chat_days}/60 天)")
            if len(important_people) < 1:
                hints.append("告诉我对你重要的人")
            if dislikes_count < 5:
                hints.append(f"让我知道你的禁忌 ({dislikes_count}/5)")
        
        elif level == 6:
            if memory_count < 3000:
                hints.append(f"再积累 {3000 - memory_count} 条有效聊天")
            if total_chat_days < 180:
                hints.append(f"半年相伴 ({total_chat_days}/180 天)")
            if depth_pct < 100:
                hints.append(f"画像深度需达到 100% (当前 {depth_pct}%)")
            if len(achievements) < 6:
                hints.append(f"解锁更多成就 ({len(achievements)}/6)")
        
        return hints if hints else ["已达最高羁绊等级！"]
