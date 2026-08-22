"""天气：后台定时刷新 + 读缓存。

v2.10.2 把 OpenWeather 请求内联在消息路径上（main.py:815 重试 2 次 × 10 秒超时），
接口不通时每条消息最坏多等 20 秒；`_ensure_session()` 还没有锁保护，并发首次调用会
创建两个 session 并泄漏一个。

这里改为：读路径 `snapshot()` 完全同步只取缓存；抓取由后台循环按
`weather_refresh_minutes` 触发。HTTP 细节通过注入的 `fetch_json` 回调完成，
所以本模块（以及整个 humanoid 包）不依赖 aiohttp。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

from ..clock import Clock, format_state_timestamp, parse_state_timestamp
from ..config import HumanoidConfig
from ..state import StateStore

API_URL = "https://api.openweathermap.org/data/2.5/weather"
MIN_KEY_LENGTH = 10
FETCH_RETRIES = 2
REQUEST_TIMEOUT = 10.0

DISABLED = {"weather": "晴朗 ☀️", "env": "天气未开启"}

FetchJson = Callable[[str, float], Awaitable[dict[str, Any]]]


def _no_key(location: str) -> dict[str, str]:
    return {"weather": "晴朗 ☀️", "env": f"当前城市 [{location}]（未填 API Key）"}


def build_url(location: str, api_key: str) -> str:
    return (
        f"{API_URL}?q={quote(location)}&appid={quote(api_key)}"
        "&units=metric&lang=zh_cn"
    )


def parse_payload(payload: dict[str, Any], location: str) -> dict[str, str] | None:
    try:
        description = str(payload["weather"][0]["description"])
        temperature = float(payload["main"]["temp"])
        humidity = payload["main"].get("humidity")
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    env = f"当前城市 [{location}] 天气：{description}，气温 {temperature:g}℃"
    if humidity is not None:
        env += f"，湿度 {humidity}%"
    return {"weather": f"{description} 🌡️ {temperature:g}°C", "env": env}


class WeatherService:
    __slots__ = ("_clock", "_config", "_fetch", "_log", "_state")

    def __init__(
        self,
        state: StateStore,
        config_provider: Callable[[], HumanoidConfig],
        clock: Clock,
        fetch_json: FetchJson | None = None,
        logger: Any = None,
    ) -> None:
        self._state = state
        self._config = config_provider
        self._clock = clock
        self._fetch = fetch_json
        self._log = logger

    # ---------- 读路径：同步 ----------

    def snapshot(self) -> dict[str, str]:
        cfg = self._config()
        if not cfg.weather_enabled:
            return dict(DISABLED)
        location = cfg.weather_location
        if len(cfg.weather_api_key) < MIN_KEY_LENGTH:
            return _no_key(location)
        cached = self._state.get("_cached_weather_obj")
        if isinstance(cached, dict) and self._state.get("_cached_location") == location:
            return dict(cached)
        return {"weather": "晴朗 ☀️", "env": f"当前城市 [{location}]（天气数据获取中）"}

    def is_stale(self) -> bool:
        cfg = self._config()
        if not cfg.weather_enabled or len(cfg.weather_api_key) < MIN_KEY_LENGTH:
            return False
        if self._state.get("_cached_location") != cfg.weather_location:
            return True
        if not isinstance(self._state.get("_cached_weather_obj"), dict):
            return True
        now = self._clock.now()
        fetched = parse_state_timestamp(str(self._state.get("_last_weather_fetch", "") or ""), now)
        if fetched is None:
            return True
        return (now - fetched).total_seconds() >= max(1, int(cfg.weather_refresh_minutes)) * 60

    # ---------- 抓取 ----------

    async def refresh(self, force: bool = False) -> bool:
        cfg = self._config()
        if not cfg.weather_enabled or len(cfg.weather_api_key) < MIN_KEY_LENGTH:
            return False
        if not force and not self.is_stale():
            return False
        if self._fetch is None:
            return False

        location = cfg.weather_location
        url = build_url(location, cfg.weather_api_key)
        for attempt in range(1, FETCH_RETRIES + 1):
            try:
                payload = await self._fetch(url, REQUEST_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._warn(f"天气请求失败（第 {attempt} 次）：{exc}")
                continue
            parsed = parse_payload(payload or {}, location)
            if parsed is None:
                self._warn(f"天气返回内容无法解析（第 {attempt} 次）")
                continue
            data = self._state.data
            data["_cached_weather_obj"] = parsed
            data["_cached_location"] = location
            data["_last_weather_fetch"] = format_state_timestamp(self._clock.now())
            self._state.mark_dirty()
            if cfg.debug_mode:
                self._debug(f"天气已更新：{parsed['weather']}")
            return True
        return False

    async def run_refresh_loop(self, stop_event: asyncio.Event) -> None:
        """按配置间隔刷新；关停时立刻退出。"""
        while not stop_event.is_set():
            await self.refresh()  # 内部已吞掉除 CancelledError 以外的异常
            cfg = self._config()
            # 检查周期取刷新间隔的 1/4，配置调小后能较快跟上，最少 60 秒
            interval = max(60.0, min(900.0, max(1, int(cfg.weather_refresh_minutes)) * 60 / 4))
            with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=interval)

    def _warn(self, message: str) -> None:
        if self._log is not None:
            self._log.warning(f"[humanoid_core] {message}")

    def _debug(self, message: str) -> None:
        if self._log is not None:
            self._log.debug(f"[humanoid_core] {message}")
