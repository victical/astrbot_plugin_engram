"""
工具函数模块
包含星座、生肖、职业等映射方法
"""
from zhdate import ZhDate
from datetime import date


def get_constellation(month: int, day: int) -> str:
    """星座映射"""
    if (month == 12 and day >= 22) or (month == 1 and day <= 19):
        return "摩羯座"
    constellations = [
        ("水瓶座", 1, 20, 2, 18),
        ("双鱼座", 2, 19, 3, 20),
        ("白羊座", 3, 21, 4, 19),
        ("金牛座", 4, 20, 5, 20),
        ("双子座", 5, 21, 6, 20),
        ("巨蟹座", 6, 21, 7, 22),
        ("狮子座", 7, 23, 8, 22),
        ("处女座", 8, 23, 9, 22),
        ("天秤座", 9, 23, 10, 22),
        ("天蝎座", 10, 23, 11, 21),
        ("射手座", 11, 22, 12, 21),
    ]
    for name, sm, sd, em, ed in constellations:
        if (month == sm and day >= sd) or (month == em and day <= ed):
            return name
    return "未知"


def get_zodiac(year: int, month: int, day: int) -> str:
    """生肖映射"""
    zodiacs = ["鼠", "牛", "虎", "兔", "龙", "蛇", "马", "羊", "猴", "鸡", "狗", "猪"]
    current = date(year, month, day)
    try:
        spring = ZhDate(year, 1, 1).to_datetime().date()
        zodiac_year = year if current >= spring else year - 1
    except:
        zodiac_year = year
    index = (zodiac_year - 2020) % 12
    return zodiacs[index]


def get_career(num: int) -> str:
    """职业映射"""
    career = {
        1: "计算机/互联网/通信", 2: "生产/工艺/制造", 3: "医疗/护理/制药",
        4: "金融/银行/投资/保险", 5: "商业/服务业/个体经营", 6: "文化/广告/传媒",
        7: "娱乐/艺术/表演", 8: "律师/法务", 9: "教育/培训",
        10: "公务员/行政/事业单位", 11: "模特", 12: "空姐", 13: "学生", 14: "其他职业"
    }
    return career.get(num, f"职业{num}")
