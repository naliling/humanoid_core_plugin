"""社交能量：按消息消耗、后台恢复、每日重置。

恢复循环用 `wait_for(stop_event.wait(), interval)` 而不是 `asyncio.sleep(interval)`，
这样关停时能立刻退出，不必等满一个间隔。配置每轮从 provider 现取，热重载天然生效。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any

from ..clock import Clock
from ..config import HumanoidConfig
from ..state import StateStore

FULL = 100.0


def describe(value: float) -> str:
    if value > 70:
        return "充足"
    if value > 40:
        return "一般"
    return "较低"


def prompt_hint(value: float) -> str:
    if value > 70:
        return "社交能量充足，回复可以热情、话多"
    if value > 40:
        return "社交能量一般，回复保持正常"
    return "社交能量较低，回复应尽量简短，避免长篇大论"


class SocialEnergyService:
    __slots__ = ("_clock", "_config", "_log", "_state")

    def __init__(
        self,
        state: StateStore,
        config_provider: Callable[[], HumanoidConfig],
        clock: Clock,
        logger: Any = None,
    ) -> None:
        self._state = state
        self._config = config_provider
        self._clock = clock
        self._log = logger

    @property
    def value(self) -> float:
        try:
            return max(0.0, min(FULL, float(self._state.get("social_energy", FULL))))
        except (TypeError, ValueError):
            return FULL

    @property
    def text(self) -> str:
        return describe(self.value)

    def hint(self) -> str:
        return prompt_hint(self.value)

    def consume_for_message(self) -> float:
        cfg = self._config()
        if not cfg.social_energy_enabled:
            return self.value
        # 先处理每日重置再扣减，否则当天第一条消息的消耗会被重置直接吃掉
        self.maybe_daily_reset()
        amount = max(0.0, float(cfg.social_energy_consumption_per_msg))
        value = max(0.0, self.value - amount)
        self._state.data["social_energy"] = value
        self._state.mark_dirty()
        return value

    def recover(self, seconds: float) -> float:
        cfg = self._config()
        if not cfg.social_energy_enabled:
            return self.value
        gain = max(0.0, float(cfg.social_energy_recovery_per_minute)) * (max(0.0, seconds) / 60.0)
        if gain <= 0:
            return self.value
        value = min(FULL, self.value + gain)
        self._state.data["social_energy"] = value
        self._state.mark_dirty()
        if cfg.debug_mode:
            self._debug(f"社交能量恢复至 {value:.1f}%")
        return value

    def maybe_daily_reset(self) -> bool:
        cfg = self._config()
        reset_hour = int(cfg.social_energy_reset_hour)
        if reset_hour < 0:
            return False
        now = self._clock.now()
        today = now.strftime("%Y-%m-%d")
        if self._state.get("_last_social_energy_reset_date", "") == today:
            return False
        if now.hour < reset_hour:
            return False
        self._state.data["social_energy"] = FULL
        self._state.data["_last_social_energy_reset_date"] = today
        self._state.mark_dirty()
        self._info(f"社交能量已按每日 {reset_hour} 点重置")
        return True

    def reset(self) -> float:
        self._state.data["social_energy"] = FULL
        self._state.mark_dirty()
        return FULL

    async def run_recovery_loop(self, stop_event: asyncio.Event) -> None:
        """后台恢复循环。异常不会让任务静默消失（由调用方的 supervisor 重启）。"""
        while not stop_event.is_set():
            cfg = self._config()
            interval = float(cfg.social_energy_recovery_interval_seconds)
            if cfg.social_energy_enabled:
                self.recover(interval)
            self.maybe_daily_reset()
            with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=interval)

    def _info(self, message: str) -> None:
        if self._log is not None:
            self._log.info(f"[humanoid_core] {message}")

    def _debug(self, message: str) -> None:
        if self._log is not None:
            self._log.debug(f"[humanoid_core] {message}")
