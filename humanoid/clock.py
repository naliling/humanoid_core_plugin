"""时间与地点：时区解析、当前时间、星期、节日。

原 main.py 里 `get_timezone` / `get_time_in_city` / `get_system_timezone_city` /
`_get_plugin_tz` / `_get_plugin_now` / `_get_holiday_for_date` 的整合版。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import HumanoidConfig
from .data.cities import CITY_TO_TIMEZONE, DEFAULT_CITY_PLACEHOLDER, lookup_timezone
from .data.holidays import resolve_holiday

WEEKDAY_NAMES = ("一", "二", "三", "四", "五", "六", "日")
FALLBACK_TIMEZONE = "Asia/Shanghai"


def weekday_cn(moment: datetime) -> str:
    return WEEKDAY_NAMES[moment.weekday()]


def resolve_tzinfo(city: str) -> tzinfo | None:
    """城市 → tzinfo。占位值或未知城市返回 None，表示「用系统本地时区」。"""
    tz_name = lookup_timezone(city)
    if not tz_name:
        return None
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        try:
            return ZoneInfo(FALLBACK_TIMEZONE)
        except Exception:
            return None


def now_in_city(city: str) -> datetime:
    """指定城市的当前时间；未知城市回退到系统本地时区（带偏移，非 naive）。"""
    tz = resolve_tzinfo(city)
    if tz is None:
        return datetime.now().astimezone()
    return datetime.now(tz)


def system_timezone_city() -> str:
    """从系统本地时区反查一个城市名，查不到就返回「系统时区」。"""
    try:
        local_tz = datetime.now().astimezone().tzinfo
        tz_name = getattr(local_tz, "key", None)
        if tz_name:
            for city, name in CITY_TO_TIMEZONE.items():
                if name == tz_name:
                    return city
    except Exception:
        pass
    return "系统时区"


def format_offset(moment: datetime) -> str:
    """+0800 → UTC+08:00"""
    raw = moment.strftime("%z")
    if len(raw) < 5:
        return "UTC"
    return f"UTC{raw[:3]}:{raw[3:5]}"


@dataclass(frozen=True, slots=True)
class CityTime:
    """一次城市时间查询的结果。"""

    city: str
    display_city: str
    moment: datetime
    text: str
    weekday: str


def lookup_city_time(city: str) -> CityTime | None:
    """`/时间 城市` 用。未收录的城市返回 None，由调用方给出「暂不支持」提示。"""
    city = (city or "").strip()
    if not city:
        return None
    is_placeholder = city == DEFAULT_CITY_PLACEHOLDER
    if not is_placeholder and city not in CITY_TO_TIMEZONE:
        return None
    try:
        moment = now_in_city(city)
    except Exception:
        return None
    return CityTime(
        city=city,
        display_city=system_timezone_city() if is_placeholder else city,
        moment=moment,
        text=f"{moment.strftime('%Y-%m-%d %H:%M:%S')} ({format_offset(moment)})",
        weekday=weekday_cn(moment),
    )


class Clock:
    """按当前配置提供「插件所在地」的时间视图。配置热重载后自动跟随。"""

    __slots__ = ("_config",)

    def __init__(self, config_provider: Callable[[], HumanoidConfig]) -> None:
        self._config = config_provider

    @property
    def city(self) -> str:
        return self._config().timezone_city

    @property
    def display_city(self) -> str:
        """给模型/用户看的城市名。占位值时反查系统时区。"""
        city = self.city
        return system_timezone_city() if city == DEFAULT_CITY_PLACEHOLDER else city

    def now(self) -> datetime:
        return now_in_city(self.city)

    def today_str(self) -> str:
        return self.now().strftime("%Y-%m-%d")

    def weekday(self) -> str:
        return weekday_cn(self.now())

    def city_time_text(self) -> str | None:
        """带 UTC 偏移的完整时间串；解析失败返回 None。"""
        result = lookup_city_time(self.city)
        return result.text if result else None

    def holiday(self, moment: datetime | None = None) -> str:
        return resolve_holiday(moment or self.now(), self._config().holidays)

    def is_night(self, moment: datetime | None = None) -> bool:
        cfg = self._config()
        if not cfg.night_mode_enabled:
            return False
        return cfg.is_night_hour((moment or self.now()).hour)


def parse_state_timestamp(raw: str, reference: datetime) -> datetime | None:
    """解析 state.json 里 `%Y-%m-%d %H:%M:%S` 形式的时间戳，附上参考时区。"""
    if not raw:
        return None
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=reference.tzinfo)
    return parsed


def format_state_timestamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")
