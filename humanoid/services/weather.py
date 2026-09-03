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


def build_url(location: str, api_key: str) -> str:
    return f"{API_URL}?q={quote(location)}&appid={quote(api_key)}&units=metric&lang=zh_cn"

def parse_payload(payload: dict[str, Any], location: str) -> dict[str, str] | None:
    try:
        desc = str(payload["weather"][0]["description"])
        temp = float(payload["main"]["temp"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    weather_str = f"{desc} 🌡️ {temp:g}°C"
    env = f"当前城市 [{location}] 天气：{desc}，气温 {temp:g}℃"
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

    def snapshot(self) -> dict[str, str]:
        cfg = self.config
        if not cfg.weather_enabled:
            return {"weather": "晴朗 ☀️", "env": "天气未开启"}
        if len(cfg.weather_api_key) < MIN_KEY_LENGTH:
            return {"weather": "晴朗 ☀️", "env": f"当前城市 [{cfg.weather_location}]（未填 API Key）"}
        cached = self._scope.get_self("_cached_weather_obj")
        if isinstance(cached, dict) and self._scope.get_self("_cached_location") == cfg.weather_location:
            return dict(cached)
        return {"weather": "晴朗 ☀️", "env": f"当前城市 [{cfg.weather_location}]（获取中）"}

    def is_stale(self) -> bool:
        cfg = self.config
        if not cfg.weather_enabled or len(cfg.weather_api_key) < MIN_KEY_LENGTH:
            return False
        if self._scope.get_self("_cached_location") != cfg.weather_location:
            return True
        now = self._clock.now()
        fetched = parse_state_timestamp(str(self._scope.get_self("_last_weather_fetch", "") or ""), now)
        if fetched is None:
            return True
        return (now - fetched).total_seconds() >= max(1, cfg.weather_refresh_minutes) * 60

    async def refresh_async(self, force: bool = False) -> bool:
        cfg = self.config
        if not cfg.weather_enabled or self._fetch is None or len(cfg.weather_api_key) < MIN_KEY_LENGTH:
            return False
        if not force and not self.is_stale():
            return False
        url = build_url(cfg.weather_location, cfg.weather_api_key)
        if cfg.debug_mode and self._log:
            self._log.debug(f"[humanoid_core] 天气请求 URL: {url}")
        try:
            payload = await self._fetch(url, REQUEST_TIMEOUT)
            parsed = parse_payload(payload, cfg.weather_location)
            if parsed is None:
                raise ValueError("天气接口返回格式无效")
            weather_str = parsed["weather"]
            env = parsed["env"]
            self._scope.update_self(
                _cached_weather_obj={"weather": weather_str, "env": env},
                _cached_location=cfg.weather_location,
                _last_weather_fetch=format_state_timestamp(self._clock.now())
            )
            if cfg.debug_mode and self._log:
                self._log.debug(f"[humanoid_core] 天气刷新成功: {parsed}")
            return True
        except Exception as e:
            if self._log:
                self._log.warning(f"[humanoid_core] 天气刷新失败: {e}")
            return False