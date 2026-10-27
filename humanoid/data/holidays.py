"""节日表与节日查询：公历固定节日 + 农历节日（2025–2030）+ 二十四节气。

自定义节日支持两种写法：`MM-DD`（每年这一天都算）与 `YYYY-MM-DD`（只认那一天）。
内置节日用的是 MM-DD，所以早年只写完整日期的自定义节日，第二年同一天就失效了。

v2.28.16 起不再只有公历：农历节日的公历日期照抄同平台插件
koishi-plugin-chatluna-spark 的硬表（`lib/data/festivals.js`，天文台数据），
二十四节气也按它那份表来。查询优先级从高到低是：

    自定义节日 > 农历节日（按年份）> 二十四节气 > 公历内置节日

同一命中只返回一个名字——她嘴里一天只该有一个「今天是什么日子」。
农历表只覆盖 2025–2030：未知年份（2031 以后）农历查不到，节气照常可用。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime

# 公历固定节日（MM-DD）。原来只有这一张表，也是「元旦/国庆」这些现代节的来源。
BUILTIN_HOLIDAYS: dict[str, str] = {
    "01-01": "元旦",
    "02-14": "情人节",
    "03-08": "妇女节",
    "03-12": "植树节",
    "04-01": "愚人节",
    "05-01": "劳动节",
    "06-01": "儿童节",
    "07-01": "建党节",
    "08-01": "建军节",
    "09-10": "教师节",
    "10-01": "国庆节",
    "12-25": "圣诞节",
}

# 农历节日对应的**公历**日期（按年份，MM-DD）。
#
# 三件事必须一起写在明处：
# 1. 这是**硬表**，不是逐年推算的农历算法——2030 以后没有新数据就查不到（返回空），
#    节气那条路不受影响。宁可少报一个名字，也不要拿错表算错日子。
# 2. 「腊八节」「除夕」的日期可能落在**上一公历年**：2025 年腊八是 2025-01-07
#    （农历 2024 年腊月初八），2026 年腊八是 2026-12-26（农历 2026 年腊月初八）。
#    表里按「这个公历日子里要报的名字」记，不按农历年归属。
# 3. 除夕永远在春节前一天，但仍然单独列出来：两个名字的优先级相同、命中不同日子。
LUNAR_FESTIVAL_NAMES = ("春节", "元宵节", "端午节", "七夕节", "中秋节", "重阳节", "腊八节", "除夕")

LUNAR_FESTIVAL_DATES: dict[int, dict[str, str]] = {
    2025: {
        "春节": "01-29",
        "元宵节": "02-12",
        "端午节": "05-31",
        "七夕节": "08-29",
        "中秋节": "10-06",
        "重阳节": "10-29",
        "腊八节": "01-07",  # 2025 年腊八是 2025-01-07
        "除夕": "01-28",  # 2025 年除夕是 2025-01-28
    },
    2026: {
        "春节": "02-17",
        "元宵节": "03-03",
        "端午节": "06-19",
        "七夕节": "08-19",
        "中秋节": "09-25",
        "重阳节": "10-18",
        "腊八节": "12-26",  # 2026 年腊八是 2026-12-26（农历 2026 年腊月初八）
        "除夕": "02-16",  # 2026 年除夕是 2026-02-16
    },
    2027: {
        "春节": "02-06",
        "元宵节": "02-20",
        "端午节": "06-08",
        "七夕节": "08-08",
        "中秋节": "09-15",
        "重阳节": "10-08",
        "腊八节": "01-15",
        "除夕": "02-05",
    },
    2028: {
        "春节": "01-26",
        "元宵节": "02-09",
        "端午节": "05-28",
        "七夕节": "08-26",
        "中秋节": "10-03",
        "重阳节": "10-26",
        "腊八节": "01-05",
        "除夕": "01-25",
    },
    2029: {
        "春节": "02-13",
        "元宵节": "02-27",
        "端午节": "06-16",
        "七夕节": "08-16",
        "中秋节": "09-22",
        "重阳节": "10-15",
        "腊八节": "12-25",
        "除夕": "02-12",
    },
    2030: {
        "春节": "02-03",
        "元宵节": "02-17",
        "端午节": "06-05",
        "七夕节": "08-05",
        "中秋节": "09-12",
        "重阳节": "10-05",
        "腊八节": "01-13",
        "除夕": "02-02",
    },
}

# 反查表：源表是「名字 → 日子」（照抄 JS，方便逐行核对），而查询是「日子 → 名字」。
# 构造一次，查的时候不必每年扫 8 行。
_LUNAR_FESTIVAL_BY_DAY: dict[int, dict[str, str]] = {
    year: {month_day: name for name, month_day in table.items()}
    for year, table in LUNAR_FESTIVAL_DATES.items()
}

# 二十四节气（MM-DD → 名字）。公历日期基本固定，误差 1–2 天；这里照抄 spark 那份
# `solarTerms` 硬表。按日期排序，既方便人工核对，也让「同一张表」在报告里一眼看全。
#
# 注意**不收节气时刻**：只要那一天是节气就算。误差 1–2 天对「今天清明」这种
# 一句话提醒没有影响，为它引一套天文算法不值得。
SOLAR_TERMS: dict[str, str] = {
    "01-06": "小寒",
    "01-20": "大寒",
    "02-04": "立春",
    "02-19": "雨水",
    "03-06": "惊蛰",
    "03-21": "春分",
    "04-05": "清明",
    "04-20": "谷雨",
    "05-06": "立夏",
    "05-21": "小满",
    "06-06": "芒种",
    "06-21": "夏至",
    "07-07": "小暑",
    "07-23": "大暑",
    "08-08": "立秋",
    "08-23": "处暑",
    "09-08": "白露",
    "09-23": "秋分",
    "10-08": "寒露",
    "10-23": "霜降",
    "11-08": "立冬",
    "11-22": "小雪",
    "12-07": "大雪",
    "12-22": "冬至",
}


def _canonical(value: str) -> tuple[str, str]:
    """把用户填的日期归一化成 (YYYY-MM-DD, MM-DD)；认不出来就都是空串。

    兼容不补零的写法（“6-1”与“06-01”同一天）——面板里手填的日期什么样都有。
    """
    parts = str(value or "").strip().split("-")
    try:
        nums = [int(p) for p in parts]
    except (TypeError, ValueError):
        return "", ""
    if len(nums) == 3:
        return f"{nums[0]:04d}-{nums[1]:02d}-{nums[2]:02d}", f"{nums[1]:02d}-{nums[2]:02d}"
    if len(nums) == 2:
        return "", f"{nums[0]:02d}-{nums[1]:02d}"
    return "", ""


def canonical_date(value: object) -> tuple[str, str]:
    """归一化用户填的日期，返回 `(YYYY-MM-DD, MM-DD)`；认不出来就都是空串。

    `config._as_mapping_tuple` 用它校验自定义节日 —— 面板产出的可能是一串字符串
    （见那里的注释），两种形态都得能过这一关。
    """
    return _canonical(str(value or ""))


def lunar_holiday(date_obj: datetime) -> str:
    """农历节日名；该年份没数据 / 这天不是农历节日就返回空串。

    单独开一个函数而不是内联在 `resolve_holiday` 里：外部（诊断、测试）要能
    区分「今天没节日」和「今年农历表还没收录」——2031 年以后是后者。
    """
    try:
        year = int(date_obj.year)
    except (AttributeError, TypeError, ValueError):
        return ""
    table = _LUNAR_FESTIVAL_BY_DAY.get(year)
    if not table:
        return ""
    return str(table.get(date_obj.strftime("%m-%d"), "") or "")


def solar_term(date_obj: datetime) -> str:
    """二十四节气名；这天不是节气就返回空串。"""
    try:
        month_day = date_obj.strftime("%m-%d")
    except (AttributeError, ValueError):
        return ""
    return SOLAR_TERMS.get(month_day, "")


def resolve_holiday(date_obj: datetime, custom: Iterable[object] | None = None) -> str:
    """今天是什么节日（没有则空串）。同一天多个来源命中时只返回一个名字。

    优先级 **自定义 > 农历 > 节气 > 公历内置**：

    * 自定义排最前——用户写「我们的纪念日」就是要盖掉一切；
    * 农历排在节气前面：2027-08-08 既是七夕又撞立秋，她该说七夕；2027-10-08
      重阳撞寒露同理。人过的节比天文节点更该占嘴；
    * 节气排在公历内置前面：目前两张表没有同一天，但顺序先定死，将来加节气
      或加节日都不用回来猜。
    """
    full = date_obj.strftime("%Y-%m-%d")
    month_day = date_obj.strftime("%m-%d")
    for item in custom or ():
        if not isinstance(item, Mapping):
            continue
        item_full, item_month_day = _canonical(item.get("date", ""))
        # MM-DD 每年重复；YYYY-MM-DD 只在当年那天命中——两种写法互斥，
        # 带年份的不再靠月日去撞第二年。
        if item_full:
            if item_full == full:
                return str(item.get("name", ""))
        elif item_month_day and item_month_day == month_day:
            return str(item.get("name", ""))
    lunar = lunar_holiday(date_obj)
    if lunar:
        return lunar
    term = solar_term(date_obj)
    if term:
        return term
    return BUILTIN_HOLIDAYS.get(month_day, "")
