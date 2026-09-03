"""纯数据表与其查询函数。"""

from __future__ import annotations

from .cities import CITY_TO_TIMEZONE, DEFAULT_CITY_PLACEHOLDER, lookup_timezone
from .holidays import BUILTIN_HOLIDAYS, resolve_holiday
from .mood_map import AFFECTION_MAP, generate_mood_tag, get_mood_label
from .schedule_templates import FALLBACK_TEMPLATES, get_fallback_schedule

__all__ = [
    "AFFECTION_MAP",
    "BUILTIN_HOLIDAYS",
    "CITY_TO_TIMEZONE",
    "DEFAULT_CITY_PLACEHOLDER",
    "FALLBACK_TEMPLATES",
    "generate_mood_tag",
    "get_fallback_schedule",
    "get_mood_label",
    "lookup_timezone",
    "resolve_holiday",
]
