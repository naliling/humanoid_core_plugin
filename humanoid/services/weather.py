"""天气服务 - 使用 RoleScope 版本。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Awaitable
from typing import Any
from urllib.parse import quote

from ..clock import format_state_timestamp, parse_state_timestamp
from ..config import HumanoidConfig
from ..role_scope import RoleScope

API_URL = "https://api.openweathermap.org/data/2.5/weather"
MIN_KEY_LENGTH = 10
REQUEST_TIMEOUT = 10.0
FetchJson = Callable[[str, float], Awaitable[dict[str, Any]]]

# 还没拿到真天气时 snapshot() 给的是「为什么没有天气」的说明书。这类句子是给主人
# 看的，不能当状态递出去：模型与社交层会把它当成一句天气话接着说。
NOTICE_WORDS = ("未填", "未开启", "获取中", "没配天气")


def is_notice(text: str) -> bool:
    """这句是配置说明书还是真天气。"""
    return any(word in text for word in NOTICE_WORDS)


def build_url(location: str, api_key: str) -> str:
    return f"{API_URL}?q={quote(location)}&appid={quote(api_key)}&units=metric&lang=zh_cn"

def parse_payload(payload: dict[str, Any]) -> dict[str, str] | None:
    try:
        desc = str(payload["weather"][0]["description"])
        temp = float(payload["main"]["temp"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    weather_str = f"{desc} 🌡️ {temp:g}°C"
    # env 是给模型看的：不再拼 location——那是配置里的英文名+国家码（如
    # 「Zelenogradsk,RU」），而场景层已经说过一次「你在泽列诺格拉茨克」，
    # 同一个地方在一句话里出现两次，其中一次还是英文，看着像两处。
    env = f"天气：{desc}，气温 {temp:g}℃"
    humid = payload.get("main", {}).get("humidity")
    if humid is not None:
        env += f"，湿度 {humid}%"
    return {"weather": weather_str, "env": env}


class WeatherService:
    def __init__(self, scope: RoleScope, config_provider, clock, fetch_json: FetchJson | None = None, logger=None):
        self._scope = scope
        self._config = config_provider
        self._clock = clock
        self._fetch = fetch_json
        self._log = logger

    @property
    def config(self) -> HumanoidConfig:
        return self._config()

    def effective_location(self) -> str:
        """天气要查哪个城市。

        `weather_location` 留空时用**这个角色生效的城市**（`clock.city`，含角色级时区
        覆盖）：她感知的所在城市和报天气的城市对不上，比不报天气更奇怪；多个机器人各设
        各的城市时，天气也得跟着各自的城市走，不能都用全局那一个。
        中文城市名 OpenWeather 认不了，那种情况直接算未配置。
        """
        cfg = self.config
        explicit = cfg.weather_location.strip()
        if explicit:
            return explicit
        # 生效城市优先取 clock（含角色级时区覆盖）；拿不到时回退全局配置。
        city = (getattr(self._clock, "city", "") or cfg.timezone_city or "").strip()
        if city and city.isascii():
            return city
        return ""

    def snapshot(self) -> dict:
        """当前天气。真天气才给可用的 `env`；拿不到时给的是**给人看的说明书**。

        拿不到的那些情况统一标 `notice=True`：调用方（尤其是往上下文里注入的那个）靠这个
        字段判断「这句是说明书，别递进模型」，而不是猜字符串里有没有「未开启」这类词——
        之前靠黑名单，任何人新加一句不含这些词的提示就会漏进上下文。
        """
        cfg = self.config
        now = self._clock.now()
        if not cfg.weather_enabled:
            # 没取到天气就是没天气：报「晴朗 ☀️」是插件替她编了一句瞎话。
            return self._notice("天气未开启")
        location = self.effective_location()
        if not location:
            return self._notice(
                "没配天气城市：把 weather_location 或 timezone_city 填成英文名+国家码（如 Beijing,CN）"
            )
        if len(cfg.weather_api_key) < MIN_KEY_LENGTH:
            return self._notice(f"当前城市 [{location}]（未填 API Key）")
        cached = self._scope.get_self("_cached_weather_obj")
        if isinstance(cached, dict) and self._scope.get_self("_cached_location") == location:
            # 缓存再对也不能拿陈的当现在的：停机一天重启，第一条消息就会看到昨天的天气，
            # 而注入里它是现在时陈述（「外头多云，气温 12℃」）。超过刷新间隔就当没数据。
            fetched = parse_state_timestamp(
                str(self._scope.get_self("_last_weather_fetch", "") or ""), now
            )
            if fetched is None:
                return self._notice("天气还没取到")
            # 宽限期：刷新失败（网络抖动）时宁可拿一小时前的天气，也别让她突然没天气可讲；
            # 但停机几天再回来还拿三天前的数据说「外头多云」就是骗人了。超过 3 倍刷新
            # 间隔才作废。
            grace = max(1, cfg.weather_refresh_minutes) * 60 * 3
            if (now - fetched).total_seconds() >= grace:
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
        now = self._clock.now()
        fetched = parse_state_timestamp(str(self._scope.get_self("_last_weather_fetch", "") or ""), now)
        if fetched is None:
            return True
        return (now - fetched).total_seconds() >= max(1, cfg.weather_refresh_minutes) * 60

    async def refresh_async(self, force: bool = False) -> bool:
        cfg = self.config
        location = self.effective_location()
        if not cfg.weather_enabled or self._fetch is None or not location:
            return False
        if len(cfg.weather_api_key) < MIN_KEY_LENGTH:
            return False
        if not force and not self.is_stale():
            return False
        url = build_url(location, cfg.weather_api_key)
        if cfg.debug_mode and self._log:
            self._log.debug(f"[humanoid_core] 天气请求: {_redacted(url)}")
        try:
            payload = await self._fetch(url, REQUEST_TIMEOUT)
            parsed = parse_payload(payload)
            if parsed is None:
                raise ValueError("天气接口返回格式无效")
            weather_str = parsed["weather"]
            env = parsed["env"]
            self._scope.update_self(
                _cached_weather_obj={"weather": weather_str, "env": env},
                _cached_location=location,
                _last_weather_fetch=format_state_timestamp(self._clock.now())
            )
            if cfg.debug_mode and self._log:
                self._log.debug(f"[humanoid_core] 天气刷新成功: {parsed}")
            return True
        except Exception as e:
            if self._log:
                self._log.warning(f"[humanoid_core] 天气刷新失败: {e}")
            return False


def _redacted(url: str) -> str:
    """debug 日志里不回显 API Key。"""
    start = url.find("appid=")
    if start == -1:
        return url
    end = url.find("&", start)
    return url[: start + 6] + "***" + (url[end:] if end != -1 else "")