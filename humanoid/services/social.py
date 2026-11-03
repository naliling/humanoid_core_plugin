"""社交能量 - 使用 RoleScope 版本。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from ..config import HumanoidConfig
from ..role_scope import RoleScope

FULL = 100.0

# 停机 / 事件循环阻塞之后，一次最多补多少秒的恢复。
#
# 「按真实经过时间补」不能没有上限：停机一周回来按 1.0/分钟补 10080 点，等于
# 「社交能量」这个机制不存在（任何停机都等价于满血复活）。取 1 小时：默认速度下
# 最多 +60 点（FULL 的 60%），足够覆盖一次重启、卡顿、机器睡眠，又不至于让
# 「她累了」永远不出现（审核 时钟与天气 10）。
MAX_SETTLE_SECONDS = 3600.0
# 上次结算时刻（epoch 秒）。落在 state 里，跨重启才能补上停机那段。
SETTLE_ANCHOR_KEY = "_social_energy_settled_at"


def settle_seconds(now_ts: float, last_settle: float) -> float:
    """这一轮该结算多少秒：真实经过时间，夹在 [0, MAX_SETTLE_SECONDS]。"""
    return max(0.0, min(float(now_ts) - float(last_settle), MAX_SETTLE_SECONDS))


def _now_epoch(clock) -> float:
    """当前时刻的 epoch 秒。`Clock.timestamp()` 是唯一口径；只实现 `now()` 的测试替身也走得通。"""
    ts = getattr(clock, "timestamp", None)
    if callable(ts):
        return float(ts())
    return clock.now().timestamp()


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

    def _settle_anchor(self) -> float | None:
        """上次结算时刻（epoch 秒）；没有锚点或坏值返回 None。"""
        raw = self._scope.get_self(SETTLE_ANCHOR_KEY)
        try:
            out = float(raw)
        except (TypeError, ValueError):
            return None
        return out if out == out and out > 0 else None

    async def run_recovery_loop(self, stop_event: asyncio.Event):
        """恢复循环：**先结算，再等下一个间隔**，结算量取真实经过的时间。

        原实现每轮固定 `recover(interval)`：事件循环被一段同步代码卡住 10 分钟、
        或进程停机 8 小时后重启，都只补一个间隔的量——「恢复速度」于是取决于循环
        醒来的次数，而不是时间（审核 时钟与天气 10）。现在按 epoch 差结算。

        锚点落在 state 里（`_social_energy_settled_at`），所以跨重启也能补上停机
        那一段；补的上限见 `MAX_SETTLE_SECONDS`（停机一周不该等于满血复活）。
        """
        last_settle = self._settle_anchor()
        if last_settle is None:
            # 没有锚点（新装 / 旧存档）：不知道上次结算是什么时候，就按**一个间隔**
            # 算，与旧行为一致——不能凭空补出一段没发生过的恢复。
            try:
                first_interval = max(1.0, float(self.config.social_energy_recovery_interval_seconds))
            except Exception:
                first_interval = 60.0
            last_settle = _now_epoch(self._clock) - first_interval
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
                    now_ts = _now_epoch(self._clock)
                    self.recover(settle_seconds(now_ts, last_settle))
                    self.maybe_daily_reset()
                    # 只有这一轮真的跑完才推进锚点：`recover()` 抛异常时这一段留到
                    # 下一轮再补，不会凭空丢掉。
                    last_settle = now_ts
                    self._scope.update_self(**{SETTLE_ANCHOR_KEY: now_ts})
            except Exception as exc:
                # 与其它后台循环一致：单轮出错只记一条，别让任务静默死掉。
                self._warn(f"社交能量恢复出错: {exc}")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass