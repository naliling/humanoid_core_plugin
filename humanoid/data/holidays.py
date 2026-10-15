"""内置公历节日表 + 节日查询。

自定义节日支持两种写法：`MM-DD`（每年这一天都算）与 `YYYY-MM-DD`（只认那一天）。
内置节日用的是 MM-DD，所以早年只写完整日期的自定义节日，第二年同一天就失效了。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime

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


def resolve_holiday(date_obj: datetime, custom: Iterable[object] | None = None) -> str:
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
    return BUILTIN_HOLIDAYS.get(month_day, "")