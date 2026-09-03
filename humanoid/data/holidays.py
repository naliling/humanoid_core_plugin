"""内置公历节日表 + 节日查询。"""

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


def resolve_holiday(date_obj: datetime, custom: Iterable[object] | None = None) -> str:
    date_str = date_obj.strftime("%Y-%m-%d")
    for item in custom or ():
        if isinstance(item, Mapping) and item.get("date") == date_str:
            return str(item.get("name", ""))
    return BUILTIN_HOLIDAYS.get(date_obj.strftime("%m-%d"), "")