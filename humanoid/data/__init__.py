"""纯数据表与其查询函数。"""

from __future__ import annotations

from .cities import CITY_TO_TIMEZONE, DEFAULT_CITY_PLACEHOLDER, lookup_timezone
from .holidays import BUILTIN_HOLIDAYS, resolve_holiday
from .mood_map import AFFECTION_MAP, generate_mood_tag, get_mood_label

__all__ = [
    "AFFECTION_MAP",
    "BUILTIN_HOLIDAYS",
    "CITY_TO_TIMEZONE",
    "DEFAULT_CITY_PLACEHOLDER",
    "generate_mood_tag",
    "get_mood_label",
    "lookup_timezone",
    "resolve_holiday",
]