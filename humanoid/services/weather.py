"""天气服务 - 使用 RoleScope 版本。"""

from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Awaitable
from typing import Any
from urllib.parse import quote

from ..clock import parse_state_timestamp
from ..config import HumanoidConfig
from ..data.cities import (
    CITY_TO_TIMEZONE,
    DEFAULT_CITY_PLACEHOLDER,
    WEATHER_ZONE_CITY,
    resolve_zone_name,
)
from ..role_scope import RoleScope

API_URL = "https://api.openweathermap.org/data/2.5/weather"
MIN_KEY_LENGTH = 10
REQUEST_TIMEOUT = 10.0
FetchJson = Callable[[str, float], Awaitable[dict[str, Any]]]

# 取天气失败后的退避：5 分钟起步、每次翻倍、封顶 60 分钟。
# 原来失败路径**不写 `_last_weather_fetch`**，于是 `is_stale()` 永远为真 ——
# 而消息路径是每条消息调一次 `is_stale()`，用户看到的是「这个插件在疯狂请求接口」。
RETRY_BACKOFF_BASE_SECONDS = 300.0
RETRY_BACKOFF_MAX_SECONDS = 3600.0

# 还没拿到真天气时 snapshot() 给的是「为什么没有天气」的说明书。这类句子是给主人
# 看的，不能当状态递出去：模型与社交层会把它当成一句天气话接着说。
#
# 判据的主路径是 `snapshot()` 的 `notice` 字段（见它的 docstring）；这个黑名单是
# `link.py` 那类**只拿到 env 字符串**的调用方的兜底，所以必须覆盖 `_notice()` 的
# 每一句文案——「天气数据过期了」「天气还没取到」曾经漏在里面，被当成真天气导出。
NOTICE_WORDS = ("未填", "未开启", "获取中", "没配天气", "过期", "还没取到")


def is_notice(text: str) -> bool:
    """这句是配置说明书还是真天气。"""
    return any(word in text for word in NOTICE_WORDS)


# 「天气这一句这一轮该不该再说一遍」的判定状态。**按会话/用户分桶**，不是角色级单值：
# 旧写法是 PromptBuilder 上的一个 (文本, 时刻)，整个角色共用一份——一个会话把
# 「阴，气温 8℃」讲过后，另一个会话在 4 小时内再也拿不到天气句（跨用户互相压制，
# 审核 P-08 #41）。每桶存的是**天气指纹**（城市 + 描述 + 温度），不是「已说过」这个
# 布尔：指纹变了就说明天气变了，该说；指纹没变才看时间间隔。
WEATHER_REPEAT_SECONDS = 4 * 3600.0
# 桶上限：只放内存、不落盘。满了淘汰最久没被问到的那一桶（LRU），
# 而不是整表 clear()——整表清空会让所有会话立刻再看到同一句天气。
WEATHER_SEEN_LIMIT = 64


class WeatherSayGate:
    """天气去重门：每轮问一次「这个会话现在该不该再听一遍天气」。

    **纯内存、无锁**：调用方是消息路径上的单个事件循环，dict 操作没有 await 点。
    """

    def __init__(
        self,
        repeat_seconds: float = WEATHER_REPEAT_SECONDS,
        limit: int = WEATHER_SEEN_LIMIT,
    ) -> None:
        self._repeat = float(repeat_seconds)
        self._limit = max(1, int(limit))
        # key -> (指纹, 上次说出的时刻)
        self._seen: "OrderedDict[str, tuple[str, float]]" = OrderedDict()

    def worth_saying(
        self, text: str, key: str = "", now: float = 0.0, extra: str = ""
    ) -> bool:
        """要不要把 `text` 写进这一轮的注入；`key` 是会话/用户标识。

        `extra` 是句子之外、但同样算「天气变了」的部分（城市名）。
        时刻未知（now<=0）时不做时间判断，只说指纹变了才说——宁可少说一句，
        也不要在时钟不可信时每轮复读。
        """
        body = str(text or "").strip()
        if not body:
            return False
        extra_text = str(extra or "").strip()
        fingerprint = f"{extra_text}\x1f{body}"
        bucket = str(key or "")
        try:
            now_v = float(now)
        except (TypeError, ValueError):
            now_v = 0.0
        prev = self._seen.get(bucket)
        if prev is not None:
            prev_fp, prev_at = prev
            if prev_fp == fingerprint and now_v > 0 and prev_at > 0:
                if (now_v - prev_at) < self._repeat:
                    self._seen.move_to_end(bucket)
                    return False
        self._seen[bucket] = (fingerprint, now_v)
        self._seen.move_to_end(bucket)
        while len(self._seen) > self._limit:
            self._seen.popitem(last=False)
        return True

    def forget(self, key: str = "") -> None:
        """丢掉一个桶（配置改了城市、或者测试要重置时用）。"""
        self._seen.pop(str(key or ""), None)


# OpenWeather 的 `q=` 只认「英文城市名[,国家码]」这种写法。旧判据是 `str.isascii()`：
# 它把「Asia/Shanghai」也当成合法英文城市名（ASCII 不等于城市名），于是按文档填 IANA
# 时区名的人拿到的是一个查不到的 q=，接口 404、缓存永远为空、天气永久静默失效。
_EN_CITY = re.compile(r"^[A-Za-z][A-Za-z .,'-]*$")


def _normalize_location(raw: str) -> str:
    """配置里的城市/时区写法 → OpenWeather 查得到的英文城市名；认不出返回空串。

    * 表内地名（中文）与 IANA 时区名：先归一到 IANA 名，再查 `WEATHER_ZONE_CITY`
      翻成英文城市（`Asia/Shanghai` → `Shanghai,CN`）。表里没有的时区不猜。
    * 已经写成英文城市名（`Beijing,CN`）：原样保留。
    * 其余（中文但表里没有、拼错的地名）：空串，由 `snapshot()` 说明原因。
    """
    value = (raw or "").strip()
    if not value:
        return ""
    if "/" in value or value in CITY_TO_TIMEZONE:
        zone = resolve_zone_name(value) or ""
        return WEATHER_ZONE_CITY.get(zone, "")
    return value if _EN_CITY.match(value) else ""


# 凭证参数名。日志/异常文本里出现这些键，后面的值一律打码。
# 只匹配「键名 + =」，且键名前必须是词边界：这样 `keyword=` 这类业务参数不会被误伤，
# 而 `appid=`/`api_key=`/`token=` 无论出现在 URL、aiohttp 异常文本还是别的 provider
# 报错里都能命中。aiohttp 的 `ClientResponseError.__str__` 带 `real_url`（含完整 query），
# 401/429/5xx 时它就是一条把 key 写进日志的路径（审核 P1-2）。
_SECRET_PARAM_RE = re.compile(
    r"(?i)\b(appid|api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)=([^&'\")\s]+)"
)


def redact_secrets(text: str) -> str:
    """把文本里的凭证值打码：`appid=xxx` → `appid=***`。

    **可复用**：天气失败日志（本文件）、main.py 的 `_fetch_json` 异常上浮、
    未来的诊断/异常展示都应该先过这一层。脱敏不依赖 `debug_mode`——日志是长期
    留存物，泄漏一次就收不回来。
    """
    try:
        return _SECRET_PARAM_RE.sub(lambda m: f"{m.group(1)}=***", str(text or ""))
    except Exception:
        return ""


def build_url(location: str, api_key: str) -> str:
    return f"{API_URL}?q={quote(location)}&appid={quote(api_key)}&units=metric&lang=zh_cn"

def parse_payload(payload: dict[str, Any]) -> dict[str, str] | None:
    try:
        # 描述可能缺失或为空串（上游字段缺失、lang 参数没生效）。以前直接 str() 拼进
        # env，空串就得到「天气：，气温 8℃」，消费端 replace 后是「外头，气温 8℃」
        # ——一个悬空逗号，模型照着念就是病句（审核 注入取舍 #87）。
        desc = str(payload["weather"][0].get("description") or "").strip()
        temp = float(payload["main"]["temp"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    # 温度取整：`{temp:g}` 会把 8.44 原样写进注入，而**没有人知道自己那边的气温是
    # 8.44 度**——那句话一眼就是机器读数，模型照着念就露馅（「外头多云，气温 8.44℃」）。
    # 真人说的是「八度多」。天气服务的精度本来就是整数级别的体感。
    temp_i = round(temp)
    # 没描述就只说气温：`外头，气温 8℃` 的病句来自「描述槽位永远存在」这个假设。
    weather_str = f"{desc} 🌡️ {temp_i}°C" if desc else f"{temp_i}°C"
    # env 是给模型看的：不再拼 location——那是配置里的英文名+国家码（如
    # 「Zelenogradsk,RU」），而场景层已经说过一次「你在泽列诺格拉茨克」，
    # 同一个地方在一句话里出现两次，其中一次还是英文，看着像两处。
    env = f"天气：{desc}，气温 {temp_i}℃" if desc else f"天气：气温 {temp_i}℃"
    humid = payload.get("main", {}).get("humidity")
    if humid is not None:
        try:
            env += f"，湿度 {round(float(humid)):.0f}%"
        except (TypeError, ValueError):
            env += f"，湿度 {humid}%"
    return {"weather": weather_str, "env": env}


def effective_location(clock: Any, cfg: Any) -> str:
    """天气要查哪个城市（OpenWeather 认的英文写法）；认不出时返回空串。

    **模块级纯函数**：天气子系统与诊断页必须问同一个问题（审核 引擎与诊断 M3）。
    以前诊断自己写了一套「weather_location or 所在城市」，于是面板说「天气城市：
    河源（记得改~）」而天气模块判定「未配置」——同一件事两处各算一遍，结论就不一样。

    只读 `clock.city` 与 `cfg`，不碰状态：`Clock` 和 `ZoneState` 都有 `.city`，
    诊断页手里只有后者，也能直接传进来。

    `weather_location` 留空时用**这个角色生效的城市**（`clock.city`，含角色级时区
    覆盖）：她感知的所在城市和报天气的城市对不上，比不报天气更奇怪；多个机器人各设
    各的城市时，天气也得跟着各自的城市走，不能都用全局那一个。

    两条路径都先过 `_normalize_location`：显式值填中文地名或 IANA 时区名（配置
    hint 就是这么教的）也要能用；显式值填了却认不出时不回退到 `timezone_city`
    ——那是「我特意指定了另一个城市」，拿别的城市冒充比没有天气更糟。
    """
    explicit = (getattr(cfg, "weather_location", "") or "").strip()
    if explicit:
        return _normalize_location(explicit)
    # 生效城市优先取 clock（含角色级时区覆盖）；拿不到时回退全局配置。
    city = getattr(clock, "city", "") or getattr(cfg, "timezone_city", "") or ""
    return _normalize_location(city)


# 天气新鲜度的落盘口径（R5）：一律 epoch 秒。
#
# 旧实现把「取到天气的时刻」写成 `%Y-%m-%d %H:%M:%S` 的**无时区墙钟**，读的时候用
# 当前时区反解（`parse_state_timestamp`）。于是换城市/改系统时区之后，同一串数字被
# 解释成另一个时刻；系统时钟回拨时它甚至落在未来，`now - fetched` 变负，缓存就
# **永久判新鲜**——注入里一直说「外头多云」，却再也不去刷新（审核 时钟与天气 4）。
# 现在新数据只写 epoch 秒，比较不再依赖任何时区。
FETCHED_AT_KEY = "_last_weather_fetch_ts"
# 旧存档的墙钟字符串键：只在迁移时读一次，读后回写成 epoch。
LEGACY_FETCHED_AT_KEY = "_last_weather_fetch"


def _as_epoch(value: Any) -> float | None:
    """盘上的时间戳 → epoch 秒；读不出（空/坏值/非正数/NaN）返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out > 0 else None


def _now_epoch(clock: Any) -> float:
    """当前时刻的 epoch 秒。`Clock.timestamp()` 是唯一口径；只实现 `now()` 的测试替身也走得通。"""
    ts = getattr(clock, "timestamp", None)
    if callable(ts):
        return float(ts())
    return clock.now().timestamp()


def fetched_at(scope: Any, clock: Any) -> float | None:
    """缓存写入时刻（epoch 秒）——**唯一读法**，`snapshot()` 与 `is_stale()` 都问它。

    优先读 epoch 键。旧存档只有墙钟字符串：按**写入时**的时区解析（换城市时
    `core_instance._rebase_after_move` 会先把它清空），转成 epoch 后回写，
    下一次起就再也不会因为换时区而漂。
    """
    epoch = _as_epoch(scope.get_self(FETCHED_AT_KEY))
    if epoch is not None:
        return epoch
    legacy = str(scope.get_self(LEGACY_FETCHED_AT_KEY, "") or "")
    if not legacy:
        return None
    parsed = parse_state_timestamp(legacy, clock.now())
    if parsed is None:
        return None
    epoch = parsed.timestamp()
    try:
        # 迁移只做一次：写回 epoch 并清掉旧字符串，之后读到的永远是绝对值。
        scope.update_self(**{FETCHED_AT_KEY: epoch, LEGACY_FETCHED_AT_KEY: ""})
    except Exception:
        pass
    return epoch


class WeatherService:
    def __init__(self, scope: RoleScope, config_provider, clock, fetch_json: FetchJson | None = None, logger=None):
        self._scope = scope
        self._config = config_provider
        self._clock = clock
        self._fetch = fetch_json
        self._log = logger
        # 请求风暴的两道闸（见 should_refresh）：一个请求在飞时不再发第二个；
        # 上一次失败后按指数退避，别每条消息都去撞接口。
        self._inflight = False
        self._fail_streak = 0
        # 退避到期的时刻，**单调钟**秒（不是墙钟 datetime）：系统时间被回拨一小时时
        # 墙钟会把退避拉长到一小时以上，天气在两小时内都不再尝试，而 `snapshot()`
        # 同时把缓存判过期 → 注入里一直说「天气数据过期了」。
        self._next_attempt_at: float | None = None
        # 时钟回拨只告警一次：每次读取都刷一条 warning 会把日志淹掉。
        self._warned_clock_skew = False

    @property
    def config(self) -> HumanoidConfig:
        return self._config()

    def effective_location(self) -> str:
        """这个服务实例要查的城市。判定本身在模块级 `effective_location()`。

        保留这个方法只为现有调用方（`snapshot()` / `is_stale()` / 门面 / 测试）；
        判定逻辑**只有一份**，诊断页调的是同一个模块级函数。
        """
        return effective_location(self._clock, self.config)

    def _fetched_at(self) -> float | None:
        """缓存写入时刻（epoch 秒）；旧存档的墙钟字符串在这里迁移一次。"""
        return fetched_at(self._scope, self._clock)

    def _reanchor(self, now_ts: float) -> None:
        """把取到时刻改写成「现在」——只用于时钟回拨后的自愈（见 `_age_seconds`）。"""
        try:
            self._scope.update_self(**{FETCHED_AT_KEY: now_ts})
        except Exception:
            pass

    def _age_seconds(self, fetched: float) -> float:
        """缓存已经放了多久（秒），**不小于 0**。

        取到时刻落在未来只可能来自系统时钟回拨（或旧存档按新时区反解）：只把差值
        夹到 0 会让缓存**永久判新鲜**（永远不刷新、也永远不过期），所以顺手把锚点
        改写成「现在」，让它在正常刷新间隔后自然过期——机制自愈，而不是卡死。
        """
        now_ts = _now_epoch(self._clock)
        age = now_ts - fetched
        if age < 0:
            self._reanchor(now_ts)
            if not self._warned_clock_skew:
                self._warned_clock_skew = True
                if self._log:
                    self._log.warning(
                        "[humanoid_core] 天气缓存的取到时刻在未来（系统时钟回拨过？）："
                        "按刚取到处理并重新起算"
                    )
            return 0.0
        return age

    def snapshot(self) -> dict:
        """当前天气。真天气才给可用的 `env`；拿不到时给的是**给人看的说明书**。

        拿不到的那些情况统一标 `notice=True`：调用方（尤其是往上下文里注入的那个）靠这个
        字段判断「这句是说明书，别递进模型」，而不是猜字符串里有没有「未开启」这类词——
        之前靠黑名单，任何人新加一句不含这些词的提示就会漏进上下文。
        """
        cfg = self.config
        if not cfg.weather_enabled:
            # 没取到天气就是没天气：报「晴朗 ☀️」是插件替她编了一句瞎话。
            return self._notice("天气未开启")
        location = self.effective_location()
        if not location:
            # 配置里写了东西但归一化不出来（中文地名表里没有、拼错、表外时区）：
            # 说清楚「填的是什么、要填什么」，否则用户只看到永远没有天气。
            configured = (cfg.weather_location or "").strip() or str(
                getattr(self._clock, "city", "") or cfg.timezone_city or ""
            ).strip()
            if configured and configured != DEFAULT_CITY_PLACEHOLDER:
                return self._notice(
                    f"天气城市「{configured}」认不出英文名：weather_location 要填英文城市名+国家码"
                    "（如 Beijing,CN），或填 IANA 时区名（如 Asia/Shanghai）"
                )
            return self._notice(
                "没配天气城市：把 weather_location 或 timezone_city 填成英文名+国家码（如 Beijing,CN）"
            )
        if len(cfg.weather_api_key) < MIN_KEY_LENGTH:
            return self._notice(f"当前城市 [{location}]（未填 API Key）")
        cached = self._scope.get_self("_cached_weather_obj")
        if isinstance(cached, dict) and self._scope.get_self("_cached_location") == location:
            # 缓存再对也不能拿陈的当现在的：停机一天重启，第一条消息就会看到昨天的天气，
            # 而注入里它是现在时陈述（「外头多云，气温 12℃」）。超过刷新间隔就当没数据。
            fetched = self._fetched_at()
            if fetched is None:
                return self._notice("天气还没取到")
            # 宽限期：刷新失败（网络抖动）时宁可拿一小时前的天气，也别让她突然没天气可讲；
            # 但停机几天再回来还拿三天前的数据说「外头多云」就是骗人了。超过 3 倍刷新
            # 间隔才作废。
            grace = max(1, cfg.weather_refresh_minutes) * 60 * 3
            if self._age_seconds(fetched) >= grace:
                return self._notice("天气数据过期了")
            result = dict(cached)
            result.setdefault("notice", False)
            return result
        return self._notice(f"当前城市 [{location}]（获取中）")

    @staticmethod
    def _notice(text: str) -> dict:
        return {"weather": "", "env": text, "notice": True}

    def is_stale(self) -> bool:
        cfg = self.config
        location = self.effective_location()
        if not cfg.weather_enabled or not location or len(cfg.weather_api_key) < MIN_KEY_LENGTH:
            return False
        if self._scope.get_self("_cached_location") != location:
            return True
        fetched = self._fetched_at()
        if fetched is None:
            return True
        return self._age_seconds(fetched) >= max(1, cfg.weather_refresh_minutes) * 60

    def should_refresh(self) -> bool:
        """现在该不该真的去取一次天气——**消息路径用这个，不要用 `is_stale()`**。

        两者分工：`is_stale()` 说的是「缓存过期了」（纯事实，诊断与面板也看它），
        这里再问「现在去取有意义吗」：
        · 已经有一个请求在飞 → 不必再发一个（消息连发时每条都会问到这里）；
        · 上一次刚失败过 → 按退避等一会儿，否则失败会变成每条消息一次重试。
        """
        if self._inflight or not self.is_stale():
            return False
        if self._next_attempt_at is not None and time.monotonic() < self._next_attempt_at:
            return False
        return True

    def _note_failure(self) -> None:
        """失败也要记账：安排下一次尝试的时间（指数退避）。

        原实现失败路径只打一行日志、不动 `_last_weather_fetch`，`is_stale()` 于是
        永远为真 —— 而消息路径每条消息都会问一次，等于「取不到天气」直接变成
        「每条消息一个 HTTP 请求」。这里把失败也记进尝试时间。
        """
        self._fail_streak = min(self._fail_streak + 1, 20)
        delay = min(
            RETRY_BACKOFF_BASE_SECONDS * (2 ** (self._fail_streak - 1)),
            RETRY_BACKOFF_MAX_SECONDS,
        )
        self._next_attempt_at = time.monotonic() + delay

    async def refresh_async(self, force: bool = False) -> bool:
        cfg = self.config
        location = self.effective_location()
        if not cfg.weather_enabled or self._fetch is None or not location:
            return False
        if len(cfg.weather_api_key) < MIN_KEY_LENGTH:
            return False
        # 同一时刻只允许一个请求在飞。检查与置位之间没有 await，所以「查完就占」是原子的。
        if self._inflight:
            return False
        if not force and not self.should_refresh():
            return False
        self._inflight = True
        try:
            url = build_url(location, cfg.weather_api_key)
            if cfg.debug_mode and self._log:
                self._log.info(f"[humanoid_core] 天气请求: {_redacted(url)}")
            payload = await self._fetch(url, REQUEST_TIMEOUT)
            parsed = parse_payload(payload)
            if parsed is None:
                raise ValueError("天气接口返回格式无效")
            weather_str = parsed["weather"]
            env = parsed["env"]
            self._scope.update_self(
                _cached_weather_obj={"weather": weather_str, "env": env},
                _cached_location=location,
                # 顺手清掉旧墙钟串：迁移/刷新之后盘上只留绝对值。
                **{FETCHED_AT_KEY: _now_epoch(self._clock), LEGACY_FETCHED_AT_KEY: ""}
            )
            self._fail_streak = 0
            self._next_attempt_at = None
            if cfg.debug_mode and self._log:
                self._log.info(f"[humanoid_core] 天气刷新成功: {parsed}")
            return True
        except Exception as e:
            self._note_failure()
            if self._log:
                # 异常文本可能带完整 URL（aiohttp 的 real_url 含 appid=）——日志是长期
                # 留存物，必须在这里脱敏（审核 P1-2）。不只 debug：warning 才是默认会写盘的。
                self._log.warning(f"[humanoid_core] 天气刷新失败: {redact_secrets(str(e))}")
            return False
        finally:
            self._inflight = False


def _redacted(url: str) -> str:
    """debug 日志里不回显 API Key。

    保留这个名字给既有调用/测试；实现统一走 `redact_secrets`（覆盖 appid= 位于末尾、
    被引号包裹、以及 api_key=/token= 等其它凭证参数）。
    """
    return redact_secrets(url)