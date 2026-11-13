"""时间与地点：时区解析、当前时间、星期、节日。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api import logger

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
            note = f"认不出城市「{raw}」，她现在按这台机器的时间过日子（可填表内城市，或直接填 Asia/Shanghai 这类时区名）"
            logger.warning("时区退回宿主机（认不出城市）：%s", note)
            return None, note, False
        # 没配城市 / 配的是占位符：这是正常配置，不是退化，只留 debug。
        logger.debug("未配置城市（或占位符），按宿主机时钟：%r", raw)
        return None, "", False
    try:
        return ZoneInfo(name), "", False
    except (ZoneInfoNotFoundError, ValueError, OSError):
        if _tzdata_usable():
            note = f"「{name}」这个时区名用不了（可能拼错了），她现在按这台机器的时间过日子"
            logger.warning("时区退回宿主机（时区名不可用）：%s", note)
            return None, note, False
        note = f"这台机器缺时区数据库（tzdata），{name} 用不了，她现在按这台机器的时间过日子"
        logger.warning("时区退回宿主机（缺 tzdata）：%s", note)
        return None, note, True


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


def _host_zone_name() -> str:
    """宿主机时区的 IANA 名；拿不到（Windows 上 tzinfo 常常没有 `key`）时返回空串。"""
    try:
        return str(getattr(datetime.now().astimezone().tzinfo, "key", None) or "")
    except Exception:
        return ""


def system_timezone_city() -> str:
    """宿主机时区**唯一**对应的城市名；不能唯一确定时返回空串。

    一个 IANA 名往往对应表里几十个城市（`Asia/Shanghai` 下面有整个中国的地名）。
    旧写法返回字典里第一个命中的城市，于是默认配置下她自称的城市是任意的、用户从未
    选择过——「你在河源」这种无依据陈述就是这么来的。现在只在恰好一个候选时才说，
    否则返回空串（再由 `display_city` 转 None，注入层省略「你在…」）。

    也不再把 `SYSTEM_TIMEZONE_LABEL` 当返回值：它是给 `resolve_zone` 认的哨兵，
    不是城市名。空串就是「说不出来」。
    """
    tz_name = _host_zone_name()
    if not tz_name:
        return ""
    hits = [city for city, name in CITY_TO_TIMEZONE.items() if name == tz_name]
    return hits[0] if len(hits) == 1 else ""


# 「你在…」只能接地名。IANA_DISPLAY_NAMES 里除了城市名，还有时区标签（北京时间、
# UTC+08:00）和带注解的说明（乌鲁木齐（当地作息））：那些是给 /时间 面板看的标签，
# 塞进「你在北京时间」就是病句。判断不出来时按「说不出来」处理。
_NON_PLACE_MARKERS = ("时间", "（", "(", "UTC", "GMT")


def _speakable_city(name: str) -> str | None:
    """能当「你在…」说的地名；时区标签、带注解的说明一律返回 None。"""
    text = (name or "").strip()
    if not text:
        return None
    if any(marker in text for marker in _NON_PLACE_MARKERS):
        return None
    return text


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
    def display_city(self) -> str | None:
        """能说出口的城市名；**说不出来时返回 None，调用方必须省略整句**。

        返回值直接进「你在{city}」这类句子，所以不能有哨兵、原始 IANA 名、拼错的
        地名或「按宿主机时间过日子」的降级说明——那些都不是城市名。三种情况返回 None：
        没定城市（placeholder）、城市/时区认不出（resolve_zone 有 note）、表里没有
        中文显示名。
        """
        city = self.city
        if city == DEFAULT_CITY_PLACEHOLDER:
            return system_timezone_city() or None
        _, note = resolve_zone(city)
        if note:
            # 时间按宿主机走（缺 tzdata / 名字拼错）：再说「你在东京」就是两处打架。
            return None
        # 填的是 IANA 名时翻成中文城市名：「你在雷克雅未克」，不是「你在Atlantic/Reykjavik」。
        # 翻出来的是时区标签（北京时间、UTC）就还是说不出口，返回 None 让调用方省略整句。
        return _speakable_city(display_city_name(city, resolve_zone_name(city)))

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

    @property
    def tz(self) -> tzinfo | None:
        """当前生效的时区；None = 按宿主机（城市认不出或缺 tzdata 时的退化）。"""
        return resolve_tzinfo(self.city)

    def local(self, ts: float) -> datetime:
        """epoch 秒 → 她的墙钟时间（按角色城市，不是宿主时区）。"""
        return local(ts, self.tz)

    def day_key(self, ts: float | None = None) -> str:
        """`ts` 落在她时区的哪一天（`YYYY-MM-DD`）；默认取现在。"""
        return day_key(self.timestamp() if ts is None else ts, self.tz)

    def day_ordinal(self, ts: float | None = None) -> int:
        return day_ordinal(self.timestamp() if ts is None else ts, self.tz)

    def day_gap(self, then: float, now: float | None = None) -> int:
        return day_gap(then, self.timestamp() if now is None else now, self.tz)

    def today_str(self) -> str:
        return self.now().strftime("%Y-%m-%d")

    def weekday(self) -> str:
        return weekday_cn(self.now())

    def city_time_text(self) -> str | None:
        result = lookup_city_time(self.city)
        return result.text if result else None

    def holiday(self, moment: datetime | None = None) -> str:
        return resolve_holiday(moment or self.now(), self._config().holidays)

    def is_night_hour(self, moment: datetime | None = None) -> bool:
        """这个钟点落在配置的夜间窗口里吗——**只问钟点**，不看夜间模式开关。

        与 `is_night()` 的分工：`is_night()` 回答「夜间模式现在生效吗」（开关 AND
        钟点），而契约与面板里给出的 `night_window` 只说窗口。两个条件 AND 在一起
        之后，同一个 23:00 在「夜间模式关着」时会得到 `is_night=False`、而
        `night_window` 仍写着 23-7——同一条 payload 里两个字段互相矛盾
        （审核 模型与导出 7）。要问「算不算夜里」用这个方法，要问「夜间模式开没开」
        直接读 `cfg.night_mode_enabled`。
        """
        return bool(self._config().is_night_hour((moment or self.now()).hour))

    def is_night(self, moment: datetime | None = None) -> bool:
        """【已弃用】夜间模式**生效中**：开关打开 且 钟点在窗口里。

        名字读作「现在是不是夜里」，实际多了一个开关条件（`night_mode_enabled`
        关掉时恒 False），于是它和 `night_window` 对「23 点算不算夜」给出不同答案。
        新代码请用 `is_night_hour()` + `cfg.night_mode_enabled` 分别表达这两件事；
        本方法保留原行为只为不静默改变旧调用方。
        """
        cfg = self._config()
        if not cfg.night_mode_enabled:
            return False
        return cfg.is_night_hour((moment or self.now()).hour)

    def is_deep_sleep(self, moment: datetime | None = None) -> bool:
        cfg = self._config()
        if not cfg.night_mode_enabled:
            return False
        return cfg.is_deep_sleep((moment or self.now()).hour)


# ---------------------------------------------------------------------------
# 时间戳 → 墙钟 / 日键（R5 唯一口径）
# ---------------------------------------------------------------------------
# 落盘一律存 epoch 秒（不带时区、不带格式），渲染时再换成本地墙钟。这样换城市、
# 系统时区被改、机器时钟回拨，都不会让盘上的旧字符串突然指到别的时刻。
# 「哪一天」也必须按**角色所在时区**算：同一个 epoch 秒在东京与洛杉矶不是同一天。


def local(ts: float, tz: tzinfo | None = None) -> datetime:
    """epoch 秒 → 该时区的墙钟时间；tz 为 None 时用宿主机时区。

    渲染（给人看的字符串）一律走这里，不要自己 `datetime.fromtimestamp`：那个用
    宿主机时区，角色设了城市就会差出一整天。
    """
    return datetime.fromtimestamp(float(ts), tz)


def day_key(ts: float, tz: tzinfo | None = None) -> str:
    """epoch 秒 → 角色时区里的「YYYY-MM-DD」。每日额度/每日衰减的日闸用它。"""
    return local(ts, tz).strftime("%Y-%m-%d")


def day_ordinal(ts: float, tz: tzinfo | None = None) -> int:
    """epoch 秒 → 角色时区里的日期序数（同一天相等，差一天差 1）。"""
    return local(ts, tz).date().toordinal()


def day_gap(then: float, now: float, tz: tzinfo | None = None) -> int:
    """`now` 比 `then` 晚几个**日历天**（0=今天，1=昨天，-1=明天）。

    「认识 N 天」「昨晚」这类说法必须按日历天算：`(now-then)/86400` 会把 23:59
    到次日 00:01 算成 0 天，也会把「三天前的中午」算成 3 天前的夜里。
    """
    return day_ordinal(now, tz) - day_ordinal(then, tz)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out else default


def night_window(cfg: Any, scheduled: Optional[tuple[float, float]] = None) -> dict:
    """她的「夜」：**唯一一份推导**。契约 / 门面 / 注入 / 状态快照都读这里。

    优先级：日程里推出来的睡段（`scheduled`，由调用方从 `soma.scheduled_night()` 取）
    → 配置窗口（`cfg.night_start_hour/night_end_hour`）。夜间模式关掉、或窗口长度为 0
    （起止设成同一时刻 = 关掉这个窗口）时就是**没有夜**：`span` 为 0、`source` 为 "none"。

    返回 `{"start", "end", "span", "source"}`，`source` ∈ `schedule`/`config`/`none`。

    为什么要有这一个函数：在这之前「现在是不是夜里」有四个答案——`Clock.is_night`
    （开关 AND 配置窗口）、`soma.in_scheduled_night`（只看日程）、契约 `routine` 块
    （日程 → 配置兜底）、门面 `clock()`（读落盘契约）。夜猫子人格的日程是 4:00-11:30，
    同一条 payload 里 `is_night` 却按配置的 23:00-6:00 判（审核 跨插件契约 8）。
    """
    start = _as_float(getattr(cfg, "night_start_hour", 0.0))
    end = _as_float(getattr(cfg, "night_end_hour", 0.0))
    if not bool(getattr(cfg, "night_mode_enabled", True)):
        # 关掉夜间模式 = 不再有生物钟夜这一层（与 `soma.biological_night()` 同口径）。
        return {"start": start, "end": end, "span": 0.0, "source": "none"}

    if scheduled:
        try:
            start_h, end_h = float(scheduled[0]), float(scheduled[1])
        except (TypeError, ValueError, IndexError):
            start_h, end_h = 0.0, 0.0
        span = (end_h - start_h) % 24.0
        # 跨度不足半小时的「夜」是退化值（`(0.0, 0.0)` 会算出 24 小时），不采信。
        if span > 0.5:
            return {"start": start_h, "end": end_h, "span": span, "source": "schedule"}

    span = _as_float(getattr(cfg, "night_span_hours", 0.0))
    if span <= 0.0:
        return {"start": start, "end": end, "span": 0.0, "source": "none"}
    return {"start": start, "end": end, "span": span, "source": "config"}


def in_night_window(hour: float, window: Any) -> bool:
    """给定钟点（0~24 的浮点小时）在不在 `night_window()` 那段里。"""
    if not isinstance(window, dict) or window.get("source") == "none":
        return False
    try:
        start = float(window.get("start", 0.0)) % 24.0
        end = float(window.get("end", 0.0)) % 24.0
        moment = float(hour) % 24.0
    except (TypeError, ValueError):
        return False
    if start == end:
        return False
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end


def in_deep_window(hour: float, window: Any, ratio: Any) -> bool:
    """给定钟点是不是在「夜的前 `ratio` 那一段」（深睡段），与 `night_window()` 同源。"""
    if not isinstance(window, dict) or window.get("source") == "none":
        return False
    try:
        start = float(window.get("start", 0.0)) % 24.0
        span = float(window.get("span", 0.0))
        moment = float(hour) % 24.0
        keep = max(0.1, min(1.0, float(ratio)))
    except (TypeError, ValueError):
        return False
    if span <= 0.0:
        return False
    offset = (moment - start) % 24.0
    return 0.0 <= offset < span * keep


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