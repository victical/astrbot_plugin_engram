"""
时间表达式解析服务

将 main.py 中的时间解析与来源类型归一化逻辑下沉，
由路由层委托调用，减少主文件复杂度。
"""

import datetime
import re


class TimeExpressionService:
    """时间表达式解析与 source_types 归一化服务。"""

    def __init__(self, config=None):
        self.config = config or {}

    def parse_time_expr(self, text: str):
        """解析工具时间表达式，返回 (start_dt, end_dt, desc)。

        支持：
        1) LLM 显式时间范围（如 2026-02-23~2026-03-01）
        2) 未携带年份的范围/日期（如 02-23~03-01），默认按当前年份解析
        """
        text = str(text or "").strip()
        if not text:
            return None, None, ""

        now = datetime.datetime.now()
        current_year = now.year

        def _next_month_start(dt: datetime.datetime) -> datetime.datetime:
            if dt.month == 12:
                return dt.replace(year=dt.year + 1, month=1, day=1)
            return dt.replace(month=dt.month + 1, day=1)

        def _safe_datetime(year: int, month: int, day: int = 1):
            try:
                return datetime.datetime(year=year, month=month, day=day)
            except ValueError:
                return None

        def _parse_date_or_month(raw: str):
            """返回 (dt, kind, normalized_text, used_default_year)。"""
            raw = str(raw or "").strip()
            if not raw:
                return None, "", "", False

            # yyyy-mm-dd / yyyy/mm/dd / yyyy.mm.dd
            for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
                try:
                    dt = datetime.datetime.strptime(raw, fmt)
                    return dt, "day", dt.strftime("%Y-%m-%d"), False
                except ValueError:
                    pass

            # yyyy-mm / yyyy/mm / yyyy.mm
            for fmt in ("%Y-%m", "%Y/%m", "%Y.%m"):
                try:
                    dt = datetime.datetime.strptime(raw, fmt)
                    return dt, "month", dt.strftime("%Y-%m"), False
                except ValueError:
                    pass

            # 中文：yyyy年m月d日(号)
            m = re.fullmatch(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})(?:日|号)?", raw)
            if m:
                dt = _safe_datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                if dt:
                    return dt, "day", dt.strftime("%Y-%m-%d"), False

            # 中文：yyyy年m月
            m = re.fullmatch(r"(\d{4})年\s*(\d{1,2})月", raw)
            if m:
                dt = _safe_datetime(int(m.group(1)), int(m.group(2)), 1)
                if dt:
                    return dt, "month", dt.strftime("%Y-%m"), False

            # 中文：m月d日(号)（默认今年）
            m = re.fullmatch(r"(\d{1,2})月\s*(\d{1,2})(?:日|号)?", raw)
            if m:
                dt = _safe_datetime(current_year, int(m.group(1)), int(m.group(2)))
                if dt:
                    return dt, "day", dt.strftime("%Y-%m-%d"), True

            # 中文：m月（默认今年）
            m = re.fullmatch(r"(\d{1,2})月", raw)
            if m:
                dt = _safe_datetime(current_year, int(m.group(1)), 1)
                if dt:
                    return dt, "month", dt.strftime("%Y-%m"), True

            # m-d / m/d / m.d（默认今年）
            m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})", raw)
            if m:
                dt = _safe_datetime(current_year, int(m.group(1)), int(m.group(2)))
                if dt:
                    return dt, "day", dt.strftime("%Y-%m-%d"), True

            return None, "", "", False

        # 1) 显式区间（支持含/不含年份；未写年份默认今年）
        token_pattern = (
            r"(?:\d{4}[-/.]\d{1,2}(?:[-/.]\d{1,2})?|"
            r"\d{1,2}[-/.]\d{1,2}|"
            r"(?:\d{4}年)?\d{1,2}月(?:\d{1,2}(?:日|号)?)?)"
        )
        range_match = re.search(
            rf"({token_pattern})\s*(?:~|～|到|至|-)\s*({token_pattern})",
            text
        )
        if range_match:
            left_raw, right_raw = range_match.group(1), range_match.group(2)
            left_dt, left_kind, left_desc, left_default_year = _parse_date_or_month(left_raw)
            right_dt, right_kind, right_desc, right_default_year = _parse_date_or_month(right_raw)
            if left_dt and right_dt:
                # 统一为左闭右开
                if right_kind == "day":
                    right_dt = right_dt.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
                else:
                    right_dt = _next_month_start(right_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0))

                if left_kind == "month":
                    left_dt = left_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                else:
                    left_dt = left_dt.replace(hour=0, minute=0, second=0, microsecond=0)

                if right_dt > left_dt:
                    desc = f"{left_desc}~{right_desc}"
                    if left_default_year or right_default_year:
                        desc += f"（未写年份按{current_year}年）"
                    return left_dt, right_dt, desc

        # 2) 单个日期 / 单个月份（支持未写年份，默认今年）
        single_match = re.search(token_pattern, text)
        if single_match:
            raw = single_match.group(1)
            dt, kind, norm_desc, used_default_year = _parse_date_or_month(raw)
            if dt:
                if kind == "day":
                    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
                    end = start + datetime.timedelta(days=1)
                else:
                    start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                    end = _next_month_start(start)

                desc = norm_desc
                if used_default_year:
                    desc += f"（未写年份按{current_year}年）"
                return start, end, desc

        return None, None, ""

    def normalize_source_types(self, source_types, default_types=None):
        """归一化 source_types，支持 array 与逗号分隔字符串。"""
        allowed_source_types = {"private", "daily_summary", "weekly", "monthly", "yearly"}
        normalized_types = []

        if isinstance(source_types, list):
            raw_types = source_types
        elif isinstance(source_types, str) and source_types.strip():
            raw_types = re.split(r"[\s,，]+", source_types.strip())
        else:
            raw_types = []

        for item in raw_types:
            token = str(item or "").strip().lower()
            if token in allowed_source_types and token not in normalized_types:
                normalized_types.append(token)

        if normalized_types:
            return normalized_types

        if default_types:
            return [t for t in default_types if t in allowed_source_types]

        return []
