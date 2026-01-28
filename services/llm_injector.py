"""
LLM上下文注入服务
从 main.py 的 on_llm_request 方法中拆分出来
负责构建和注入用户画像、长期记忆到LLM请求中
"""
from typing import Dict, List, Any, Optional


class LLMContextInjector:
    """LLM上下文注入器 - 负责构建画像和记忆文本块并注入到LLM请求"""
    
    def __init__(self):
        pass
    
    def build_profile_block(self, profile: Dict[str, Any]) -> str:
        """
        构建用户画像文本块
        
        Args:
            profile: 用户画像数据
            
        Returns:
            格式化的画像文本块
        """
        if not profile or not profile.get("basic_info"):
            return ""
        
        basic = profile.get("basic_info", {})
        attrs = profile.get("attributes", {})
        prefs = profile.get("preferences", {})
        dev = profile.get("dev_metadata", {})
        social = profile.get("social_graph", {})
        
        # 构建列表字段
        hobbies = self._join_list(attrs.get("hobbies", []))
        skills = self._join_list(attrs.get("skills", []))
        tech = self._join_list(dev.get("tech_stack", []))
        
        # v2.1 优化：细分喜好类别
        favorite_foods = self._join_list(prefs.get("favorite_foods", []))
        favorite_items = self._join_list(prefs.get("favorite_items", []))
        favorite_activities = self._join_list(prefs.get("favorite_activities", []))
        likes = self._join_list(prefs.get("likes", []))
        dislikes = self._join_list(prefs.get("dislikes", []))
        
        # 构建画像文本块
        lines = [
            "【用户档案】",
            f"- 称呼: {basic.get('nickname', '用户')} (QQ: {basic.get('qq_id')})"
        ]
        
        # 基础信息（只添加非空且非"未知"的字段）
        self._add_if_valid(lines, "性别", basic.get('gender'))
        self._add_if_valid(lines, "年龄", basic.get('age'))
        self._add_if_valid(lines, "生日", basic.get('birthday'))
        self._add_if_valid(lines, "职业", basic.get('job'))
        self._add_if_valid(lines, "所在地", basic.get('location'))
        self._add_if_valid(lines, "星座", basic.get('constellation'))
        self._add_if_valid(lines, "生肖", basic.get('zodiac'))
        
        # 爱好和技能
        if hobbies:
            lines.append(f"- 爱好: {hobbies}")
        if skills or tech:
            skill_text = f"{skills} {tech}".strip()
            lines.append(f"- 技能/技术栈: {skill_text}")
        
        # v2.1 优化：注入细分喜好
        if favorite_foods:
            lines.append(f"- 喜欢的美食: {favorite_foods}")
        if favorite_items:
            lines.append(f"- 喜欢的事物: {favorite_items}")
        if favorite_activities:
            lines.append(f"- 喜欢的活动: {favorite_activities}")
        if likes:
            lines.append(f"- 其他喜好: {likes}")
        if dislikes:
            lines.append(f"- 讨厌: {dislikes}")
        
        # v2.1 优化：显示羁绊等级
        status = social.get("relationship_status", "萍水相逢")
        lines.append(f"- 当前羁绊: {status}")
        
        # 交互指令
        lines.append("")
        lines.append("【交互指令】")
        lines.append("请基于以上档案事实，以最契合用户期望的方式与其交流。")
        
        return "\n".join(lines)
    
    def build_memory_block(self, memories: List[str]) -> str:
        """
        构建长期记忆文本块
        
        Args:
            memories: 记忆文本列表
            
        Returns:
            格式化的记忆文本块
        """
        if not memories:
            return ""
        
        memory_prompt = "\n".join(memories)
        return f"【长期记忆回溯】：\n{memory_prompt}\n"
    
    def inject_context(
        self,
        req: Any,
        profile_block: str,
        memory_block: str
    ) -> None:
        """
        将画像和记忆块注入到LLM请求的system_prompt中
        
        Args:
            req: LLM请求对象
            profile_block: 画像文本块
            memory_block: 记忆文本块
        """
        if not profile_block and not memory_block:
            return
        
        inject_text = f"\n\n{profile_block}{memory_block}"
        
        if req.system_prompt:
            req.system_prompt += inject_text
        else:
            req.system_prompt = f"你是一个有记忆的助手。以下是关于用户的信息：{inject_text}"
    
    def _join_list(self, items: Any) -> str:
        """安全地连接列表项为字符串"""
        if isinstance(items, list) and items:
            return ", ".join(str(item) for item in items)
        return ""
    
    def _add_if_valid(self, lines: List[str], label: str, value: Any) -> None:
        """如果值有效（非空且非"未知"），添加到行列表"""
        if value and str(value) != "未知":
            lines.append(f"- {label}: {value}")
