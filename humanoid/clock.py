"""时间与地点：时区解析、当前时间、星期、节日。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, tzinfo
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

# `system_timezone_city()` 认不出宿主时区对应哪个城市时给的显示名。它**不是城市名**：
# 旧写法把它当城市名传下去，`resolve_zone` 于是走「认不出城市」分支，给诊断面板塞一条
# 误导性说明（她其实正常按宿主机时钟过日子，并没有填错什么）。`resolve_zone` 认得这个
# 哨兵，直接返回「没有时区、也没有问题」。
SYSTEM_TIMEZONE_LABEL = "系统时区"
# `_tzdata_usable()` 的**失败**结果只保留这么久（秒）；成功则一直算数。
_TZDATA_RETRY_SECONDS = 300.0


def weekday_cn(moment: datetime) -> str:
    return WEEKDAY_NAMES[moment.weekday()]


@dataclass(frozen=True, slots=True)
class ZoneState:
    """她此刻按哪个钟过日子，以及为什么。"""

    city: str
    zone_name: str      # 生效的 IANA 名；"" = 没时区，用宿主机
    offset_minutes: int
    note: str           # 退化原因；"" = 正常


def _resolve_zone_uncached(city: str) -> tuple[tzinfo | None, str, bool]:
    """城市 → (tzinfo 或 None, 说明, 这个结果以后会不会变)。None 表示用宿主机时钟。

    v2.15.1 删掉了旧版静默退回 `Asia/Shanghai` 那一手：机器上没有 tzdata 时，
    设成东京的她其实按北京时间过一天，而注入里还写着「你在东京」——错得看不出来。
    现在宁可退回宿主机时间，也要把原因说清楚（诊断与 /时间 都看得到）。

    第三位是给缓存用的：**只有「这台机器缺 tzdata」是会变的**（插件可能先于 tzdata
    启动：容器里后装、AstrBot 的依赖还没加载完），这种结果不能记住，否则
    `_tzdata_usable()` 的 300 秒重试永远轮不到执行。其余结果（认出的城市、认不出的
    城市名、拼错的时区名）都是确定的，可以一直记住。
    """
    name = resolve_zone_name(city)
    if not name:
        raw = (city or "").strip()
        if raw and raw != DEFAULT_CITY_PLACEHOLDER and raw != SYSTEM_TIMEZONE_LABEL:
            return None, f"认不出城市「{raw}」，她现在按这台机器的时间过日子（可填表内城市，或直接填 Asia/Shanghai 这类时区名）", False
        return None, "", False
    try:
        return ZoneInfo(name), "", False
    except (ZoneInfoNotFoundError, ValueError, OSError):
        if _tzdata_usable():
            return None, f"「{name}」这个时区名用不了（可能拼错了），她现在按这台机器的时间过日子", False
        return None, f"这台机器缺时区数据库（tzdata），{name} 用不了，她现在按这台机器的时间过日子", True


_tzdata_state: tuple[bool, float] | None = None


def _tzdata_usable() -> bool:
    """区分「机器没时区库」与「她填错了名字」：两者的改法完全不同。

    结果**不能永久缓存**：插件启动可能早于 tzdata 可用（容器里后装、AstrBot 的依赖
    还没加载完），一次 False 被 `lru_cache(maxsize=1)` 记一辈子，之后即使 tzdata
    可用了也永远说「这台机器缺时区数据库」。成功永久算数，失败只保留
    `_TZDATA_RETRY_SECONDS` 秒，到点重新探测（`ZoneInfo("UTC")` 本身很便宜）。
    """
    global _tzdata_state
    now = time.monotonic()
    if _tzdata_state is not None:
        usable, checked = _tzdata_state
        if usable or now - checked < _TZDATA_RETRY_SECONDS:
            return usable
    usable = True
    try:
        ZoneInfo("UTC")
    except Exception:
        usable = False
    _tzdata_state = (usable, now)
    return usable


# 城市名在每条消息上都会被查（ZoneInfo 本身有缓存，但正则与字典型查找仍不必重做）。
#
# 这里**不能用 `lru_cache`**：它会把整个 `(tz, 说明)` 元组按城市名永久记住，包括
# 「这台机器缺 tzdata」那一条 —— 于是 `_tzdata_usable()` 那 300 秒的重试永远轮不到
# 执行，tzdata 在插件启动之后才可用时，面板上会永远写着「缺时区数据库」。改成手动
# 缓存：会变的结果不进表。
_ZONE_CACHE: dict[str, tuple[tzinfo | None, str]] = {}
_ZONE_CACHE_MAX = 64


def _clear_zone_cache() -> None:
    """清掉城市→时区的记忆。测试用；对外保留 `resolve_zone.cache_clear()` 这个旧名字。"""
    _ZONE_CACHE.clear()


def resolve_zone(city: str) -> tuple[tzinfo | None, str]:
    """城市 → (tzinfo 或 None, 说明)。None 表示用宿主机时钟。"""
    key = str(city or "")
    cached = _ZONE_CACHE.get(key)
    if cached is not None:
        return cached
    tz, note, retryable = _resolve_zone_uncached(key)
    if not retryable:
        if len(_ZONE_CACHE) >= _ZONE_CACHE_MAX:
            _ZONE_CACHE.clear()
        _ZONE_CACHE[key] = (tz, note)
    return tz, note


# 旧名字：既有测试用 `resolve_zone.cache_clear()` 清缓存。
resolve_zone.cache_clear = _clear_zone_cache  # type: ignore[attr-defined]


def resolve_tzinfo(city: str) -> tzinfo | None:
    return resolve_zone(city)[0]


def now_in_city(city: str) -> datetime:
    tz, _ = resolve_zone(city)
    if tz is None:
        return datetime.now().astimezone()
    return datetime.now(tz)


def system_timezone_city() -> str:
    """宿主机时区对应的城市名（`resolve_zone_name` 认得出的那种）。

    认不出来时返回 `SYSTEM_TIMEZONE_LABEL` 这个哨兵，而**不是**一个会被当成城市名
    继续解析的字符串：`resolve_zone` 认得它，直接返回「没有时区、也没有问题」。
    """
    try:
        local_tz = datetime.now().astimezone().tzinfo
        tz_name = getattr(local_tz, "key", None)
        if tz_name:
            for city, name in CITY_TO_TIMEZONE.items():
                if name == tz_name:
                    return city
    except Exception:
        pass
    return SYSTEM_TIMEZONE_LABEL


def format_offset(moment: datetime) -> str | None:
    """`UTC+08:00` 这样的偏移文本；**算不出来时返回 None，不猜 UTC**。

    naive datetime 的 `%z` 是空串，旧写法返回「UTC」——那是把「不知道时区」说成了
    UTC，正是 `link.py` 的 `utc_offset_minutes` 极力避免的错（「算不出来时必须是
    None 而不是 0：当成 0 等于把她的城市当 UTC，错得静悄悄」）。
    """
    raw = moment.strftime("%z")
    if len(raw) < 5:
        return None
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