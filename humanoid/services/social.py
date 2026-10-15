"""社交能量 - 使用 RoleScope 版本。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from ..config import HumanoidConfig
from ..role_scope import RoleScope

FULL = 100.0


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

    def _write(self, value: float) -> float:
        """统一的写入口径：一律夹到 [0, FULL] 再落盘。

        原来三处各夹各的（读时夹上界、扣时只夹下界、恢复时只夹上界），漏夹的那一处
        会把越界值写进 state.json，而钳制只在 `load` 时做一次。
        """
        clamped = max(0.0, min(FULL, float(value)))
        self._scope.set_self("social_energy", clamped)
        return clamped

    def _warn(self, message: str) -> None:
        if self._log is not None:
            self._log.warning(f"[humanoid_core] {message}")

    @property
    def text(self) -> str:
        if self.value > 70:
            return "充足"
        elif self.value > 40:
            return "一般"
        return "较低"

    def consume_for_message(self) -> float:
        cfg = self.config
        if not cfg.social_energy_enabled:
            return self.value
        self.maybe_daily_reset()
        amount = max(0.0, float(cfg.social_energy_consumption_per_msg))
        return self._write(self.value - amount)

    def recover(self, seconds: float) -> float:
        cfg = self.config
        if not cfg.social_energy_enabled:
            return self.value
        gain = max(0.0, float(cfg.social_energy_recovery_per_minute)) * (max(0.0, seconds) / 60.0)
        if gain <= 0:
            return self.value
        return self._write(self.value + gain)

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
        return self._write(FULL)

    async def run_recovery_loop(self, stop_event: asyncio.Event):
        """恢复循环：**先结算，再等下一个间隔**。

        原来是先 `wait_for(stop_event.wait(), timeout=interval)` 才做第一次恢复：刚启动
        （或刚从一次长时间停机里恢复）的那一整个间隔内社交能量纹丝不动。生产走的
        `core_instance._social_loop` 与这里本来是两份复制品，现在共用这一份。
        """
        while not stop_event.is_set():
            # 下限 1 秒：配置里出现 0/负数时（手改 JSON、旧配置）`wait_for(..., timeout=0)`
            # 会立刻超时 → 循环变成忙循环，把一个核烧满。
            try:
                interval = max(1.0, float(self.config.social_energy_recovery_interval_seconds))
            except Exception as exc:
                self._warn(f"社交能量恢复间隔不可用（{exc!r}），本轮用默认 60 秒")
                interval = 60.0
            try:
                if self.config.social_energy_enabled:
                    self.recover(interval)
                    self.maybe_daily_reset()
            except Exception as exc:
                # 与其它后台循环一致：单轮出错只记一条，别让任务静默死掉。
                self._warn(f"社交能量恢复出错: {exc}")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass