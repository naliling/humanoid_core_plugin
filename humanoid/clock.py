"""时间与地点：时区解析、当前时间、星期、节日。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, tzinfo
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import HumanoidConfig
from .data.cities import (
    CITY_TO_TIMEZONE,
    DEFAULT_CITY_PLACEHOLDER,
    display_city_name,
    lookup_city_time,
    resolve_zone_name,
)
from .data.holidays import resolve_holiday

WEEKDAY_NAMES = ("一", "二", "三", "四", "五", "六", "日")


def weekday_cn(moment: datetime) -> str:
    return WEEKDAY_NAMES[moment.weekday()]


@dataclass(frozen=True, slots=True)
class ZoneState:
    """她此刻按哪个钟过日子，以及为什么。"""

    city: str
    zone_name: str      # 生效的 IANA 名；"" = 没时区，用宿主机
    offset_minutes: int
    note: str           # 退化原因；"" = 正常


def resolve_zone(city: str) -> tuple[tzinfo | None, str]:
    """城市 → (tzinfo 或 None, 说明)。None 表示用宿主机时钟。

    v2.15.1 删掉了旧版静默退回 `Asia/Shanghai` 那一手：机器上没有 tzdata 时，
    设成东京的她其实按北京时间过一天，而注入里还写着「你在东京」——错得看不出来。
    现在宁可退回宿主机时间，也要把原因说清楚（诊断与 /时间 都看得到）。
    """
    name = resolve_zone_name(city)
    if not name:
        raw = (city or "").strip()
        if raw and raw != DEFAULT_CITY_PLACEHOLDER:
            return None, f"认不出城市「{raw}」，她现在按这台机器的时间过日子（可填表内城市，或直接填 Asia/Shanghai 这类时区名）"
        return None, ""
    try:
        return ZoneInfo(name), ""
    except (ZoneInfoNotFoundError, ValueError, OSError):
        if _tzdata_usable():
            return None, f"「{name}」这个时区名用不了（可能拼错了），她现在按这台机器的时间过日子"
        return None, f"这台机器缺时区数据库（tzdata），{name} 用不了，她现在按这台机器的时间过日子"


@lru_cache(maxsize=1)
def _tzdata_usable() -> bool:
    """区分「机器没时区库」与「她填错了名字」：两者的改法完全不同。"""
    try:
        ZoneInfo("UTC")
        return True
    except Exception:
        return False


# 城市名在每条消息上都会被查（ZoneInfo 本身有缓存，但正则与字典型查找仍不必重做）。
resolve_zone = lru_cache(maxsize=64)(resolve_zone)


def resolve_tzinfo(city: str) -> tzinfo | None:
    return resolve_zone(city)[0]


def now_in_city(city: str) -> datetime:
    tz, _ = resolve_zone(city)
    if tz is None:
        return datetime.now().astimezone()
    return datetime.now(tz)


def system_timezone_city() -> str:
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
    raw = moment.strftime("%z")
    if len(raw) < 5:
        return "UTC"
    return f"UTC{raw[:3]}:{raw[3:5]}"


class Clock:
    """按当前配置提供「插件所在地」的时间视图。配置热重载后自动跟随。

    时区支持**角色级覆盖**：多个机器人跑在同一份全局配置上，但各自可以设自己的
    城市。`city_provider` 返回这个角色单独设的城市（空则表示没单独设），Clock 优先
    用它，没有才回退全局 `timezone_city`。不接 `city_provider` 时就是纯全局行为。
    """

    __slots__ = ("_config", "_city_provider")

    def __init__(
        self,
        config_provider: Callable[[], HumanoidConfig],
        city_provider: Callable[[], str] | None = None,
    ) -> None:
        self._config = config_provider
        self._city_provider = city_provider

    @property
    def city(self) -> str:
        if self._city_provider is not None:
            try:
                override = (self._city_provider() or "").strip()
            except Exception:
                override = ""
            if override:
                return override
        return self._config().timezone_city

    @property
    def display_city(self) -> str:
        city = self.city
        if city == DEFAULT_CITY_PLACEHOLDER:
            return system_timezone_city()
        # 填的是 IANA 名时翻成中文城市名：「你在雷克雅未克」，不是「你在Atlantic/Reykjavik」。
        return display_city_name(city, resolve_zone_name(city))

    def now(self) -> datetime:
        return now_in_city(self.city)

    def timestamp(self) -> float:
        """当前时刻的 epoch 秒，与 `now()` 同一瞬。

        身体积分、间隔计算这类「过了多久」的时间源统一从这里出：core 的钟被换掉
        （测试冻结、角色换城市）时，身体与措辞跟着同一台钟走，不会出现场景写着
        下午三点、身体却按另一台钟睡着的分叉。
        """
        return self.now().timestamp()

    def zone_state(self) -> ZoneState:
        """当前配置生效的时区与退化原因（诊断用）。"""
        city = self.city
        tz, note = resolve_zone(city)
        name = resolve_zone_name(city) or ""
        moment = self.now()
        offset = moment.utcoffset()
        return ZoneState(
            city=city,
            zone_name=name if tz is not None else "",
            offset_minutes=int(offset.total_seconds() // 60) if offset else 0,
            note=note,
        )

    def today_str(self) -> str:
        return self.now().strftime("%Y-%m-%d")

    def weekday(self) -> str:
        return weekday_cn(self.now())

    def city_time_text(self) -> str | None:
        result = lookup_city_time(self.city)
        return result.text if result else None

    def holiday(self, moment: datetime | None = None) -> str:
        return resolve_holiday(moment or self.now(), self._config().holidays)

    def is_night(self, moment: datetime | None = None) -> bool:
        cfg = self._config()
        if not cfg.night_mode_enabled:
            return False
        return cfg.is_night_hour((moment or self.now()).hour)

    def is_deep_sleep(self, moment: datetime | None = None) -> bool:
        cfg = self._config()
        if not cfg.night_mode_enabled:
            return False
        return cfg.is_deep_sleep((moment or self.now()).hour)


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