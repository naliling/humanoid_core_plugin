"""社交能量 - 使用 RoleScope 版本。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from ..config import HumanoidConfig
from ..role_scope import RoleScope

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
    return "社交能量较低，回复应尽量简短"


class SocialEnergyService:
    def __init__(self, scope: RoleScope, config_provider, clock, logger=None):
        self._scope = scope
        self._config = config_provider
        self._clock = clock
        self._log = logger

    @property
    def config(self):
        return self._config()

    @property
    def value(self) -> float:
        try:
            return max(0.0, min(FULL, float(self._scope.get_self("social_energy", FULL))))
        except (TypeError, ValueError):
            return FULL

    @property
    def text(self) -> str:
        return describe(self.value)

    def hint(self) -> str:
        return prompt_hint(self.value)

    def consume_for_message(self) -> float:
        cfg = self.config
        if not cfg.social_energy_enabled:
            return self.value
        self.maybe_daily_reset()
        amount = max(0.0, float(cfg.social_energy_consumption_per_msg))
        value = max(0.0, self.value - amount)
        self._scope.set_self("social_energy", value)
        return value

    def recover(self, seconds: float) -> float:
        cfg = self.config
        if not cfg.social_energy_enabled:
            return self.value
        gain = max(0.0, float(cfg.social_energy_recovery_per_minute)) * (max(0.0, seconds) / 60.0)
        if gain <= 0:
            return self.value
        value = min(FULL, self.value + gain)
        self._scope.set_self("social_energy", value)
        return value

    def maybe_daily_reset(self) -> bool:
        cfg = self.config
        reset_hour = int(cfg.social_energy_reset_hour)
        if reset_hour < 0:
            return False
        now = self._clock.now()
        today = now.strftime("%Y-%m-%d")
        if self._scope.get_self("_last_social_energy_reset_date", "") == today:
            return False
        if now.hour < reset_hour:
            return False
        self._scope.update_self(
            social_energy=FULL,
            _last_social_energy_reset_date=today,
        )
        return True

    def reset(self) -> float:
        self._scope.set_self("social_energy", FULL)
        return FULL

    async def run_recovery_loop(self, stop_event: asyncio.Event):
        while not stop_event.is_set():
            cfg = self.config
            interval = float(cfg.social_energy_recovery_interval_seconds)
            if cfg.social_energy_enabled:
                self.recover(interval)
                self.maybe_daily_reset()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass