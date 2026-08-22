"""精力与生理周期。

修掉 v2.10.2 的两个实现缺陷：

1. **跨天时精力被瞬间拉满**：旧代码把 `last_update` 写成当天 00:00（main.py:723），
   紧接着又重新解析它（:726），于是当天第一次更新按「从午夜到现在」计费。默认自然恢复
   0.9/分钟 = 54/小时，凌晨两点以后的任何一条消息都会把精力顶到上限。
   现在跨天重置后把计费起点设为「当前时刻」。
2. **自然恢复压过日程**：旧代码无条件叠加自然恢复，量级上完全盖住日程驱动的
   ±0.1/分钟。现在自然恢复只在日程本身是休息类时段（energy_rate ≥ 0）时生效，
   工作时段严格只减不加，日程重新主导曲线。

顺带把 `energy_natural_recovery_interval_minutes` 真正接上 —— 它在 v2.10.2 里
只出现在默认值表里，没有任何地方读过。
"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import datetime

from ..clock import Clock, format_state_timestamp, parse_state_timestamp
from ..config import HumanoidConfig
from ..slots import Slot, clamp_rate, parse_time
from ..state import StateStore

# 消耗打折系数：日程给的负速率不会 100% 兑现，留出缓冲
CONSUMPTION_DISCOUNT = 0.7

# 生理周期 6 个阶段的消耗倍率（经期最费、卵泡期最省），与 v2.10.2 一致
CONSUMPTION_PHASE_FACTORS = (1.3, 0.8, 1.0, 1.1, 1.2, 1.15)

DAY_START_ENERGY = 80.0

PHASE_NAMES = ("经期", "卵泡期", "排卵期", "黄体早期", "黄体晚期", "经前期")
PHASE_NOTES = (
    "身体能量消耗较大，宜放慢节奏",
    "能量水平逐步回升",
    "能量储备相对充足",
    "能量开始趋于平稳",
    "能量水平逐渐回落",
    "能量状态略有波动",
)


def describe_energy(energy: float) -> str:
    if energy >= 90:
        return "精力充沛，语气轻快，话比较多"
    if energy >= 70:
        return "状态良好，语气正常，偶尔主动"
    if energy >= 40:
        return "状态一般，语气平和，不太想动"
    if energy >= 20:
        return "有点累，语气偏慵懒，不想说太多"
    return "很疲惫，语气低落，只想安静待着"


class EnergyService:
    """精力累计与生理周期推进。全部为同步计算，不做任何 I/O。"""

    __slots__ = ("_clock", "_config", "_log", "_schedule", "_state")

    def __init__(
        self,
        state: StateStore,
        config_provider: Callable[[], HumanoidConfig],
        clock: Clock,
        schedule_provider: Callable[[], list[Slot]],
        logger: object = None,
    ) -> None:
        self._state = state
        self._config = config_provider
        self._clock = clock
        self._schedule = schedule_provider
        self._log = logger

    # ---------- 读 ----------

    @property
    def energy(self) -> float:
        try:
            return float(self._state.get("energy", DAY_START_ENERGY))
        except (TypeError, ValueError):
            return DAY_START_ENERGY

    @property
    def max_energy(self) -> float:
        return max(1.0, float(self._config().max_energy))

    def describe(self, energy: float | None = None) -> str:
        return describe_energy(self.energy if energy is None else energy)

    @property
    def cycle_day(self) -> int:
        try:
            return max(1, int(self._state.get("current_cycle_day", 1)))
        except (TypeError, ValueError):
            return 1

    # ---------- 精力推进 ----------

    def advance(self, now: datetime | None = None) -> float:
        """按上次更新到现在的时间推进精力。同步、无 I/O，可在消息路径上直接调用。"""
        cfg = self._config()
        now = now or self._clock.now()
        data = self._state.data

        last = parse_state_timestamp(str(data.get("last_update", "") or ""), now)
        if last is None or last > now:
            data["last_update"] = format_state_timestamp(now)
            self._state.mark_dirty()
            return self.energy

        if last.date() < now.date():
            return self._reset_for_new_day(cfg, now)

        start_min = last.hour * 60 + last.minute + last.second / 60.0
        end_min = now.hour * 60 + now.minute + now.second / 60.0
        if end_min <= start_min:
            return self.energy

        delta = self._compute_delta(start_min, end_min, cfg)
        energy = self._clamp(self.energy + delta, cfg)
        data["energy"] = round(energy, 1)
        data["last_update"] = format_state_timestamp(now)
        self._state.mark_dirty()
        if cfg.debug_mode:
            self._debug(
                f"精力推进 {start_min:.1f}→{end_min:.1f} 分钟位，变化 {delta:+.2f}，当前 {energy:.1f}"
            )
        return energy

    def _reset_for_new_day(self, cfg: HumanoidConfig, now: datetime) -> float:
        """新的一天：精力回到 80 上下，并把计费起点设为「现在」而不是午夜。"""
        energy = self._clamp(DAY_START_ENERGY * random.uniform(0.95, 1.05), cfg, floor=5.0)
        data = self._state.data
        data["energy"] = round(energy, 1)
        data["last_update"] = format_state_timestamp(now)
        self._state.mark_dirty()
        if cfg.debug_mode:
            self._debug(f"跨天重置精力为 {energy:.1f}，计费起点 = 当前时刻")
        return energy

    def _compute_delta(self, start_min: float, end_min: float, cfg: HumanoidConfig) -> float:
        """把 [start_min, end_min) 按日程时段切片累加。日程已保证连续覆盖整天。"""
        slots = self._schedule()
        decay = max(0.0, float(cfg.energy_decay_rate))
        if decay == 0.0 or not slots:
            return 0.0

        phase_idx = cfg.cycle_phase_index(self.cycle_day)
        consumption_factor = (
            CONSUMPTION_PHASE_FACTORS[phase_idx]
            if phase_idx < len(CONSUMPTION_PHASE_FACTORS)
            else 1.0
        )
        recovery_per_min = max(0.0, float(cfg.energy_natural_recovery_per_minute))
        recovery_step = max(1, int(cfg.energy_natural_recovery_interval_minutes))
        recovery_factor = cfg.phase_recovery_multiplier(self.cycle_day)

        total = 0.0
        for slot in slots:
            slot_start = parse_time(slot.get("start"))
            slot_end = parse_time(slot.get("end"))
            if slot_start is None or slot_end is None or slot_end <= slot_start:
                continue
            overlap_start = max(float(slot_start), start_min)
            overlap_end = min(float(slot_end), end_min)
            minutes = overlap_end - overlap_start
            if minutes <= 0:
                continue

            rate = clamp_rate(slot.get("energy_rate", 0.0))
            if rate < 0:
                total += rate * CONSUMPTION_DISCOUNT * consumption_factor * decay * minutes
                continue

            total += rate * decay * minutes
            # 自然恢复只在休息类时段生效，否则量级上会完全盖住日程
            if cfg.enable_energy_natural_recovery and recovery_per_min > 0:
                credited = (int(minutes) // recovery_step) * recovery_step
                if credited > 0:
                    total += recovery_per_min * recovery_factor * decay * credited
        return total

    def consume_for_message(self) -> float:
        cfg = self._config()
        amount = max(0.0, float(cfg.energy_consumption_per_msg))
        if amount <= 0:
            return self.energy
        energy = self._clamp(self.energy - amount, cfg)
        self._state.data["energy"] = round(energy, 1)
        self._state.mark_dirty()
        return energy

    def reset(self, value: float = DAY_START_ENERGY) -> float:
        cfg = self._config()
        energy = self._clamp(value, cfg)
        data = self._state.data
        data["energy"] = round(energy, 1)
        data["last_update"] = format_state_timestamp(self._clock.now())
        self._state.mark_dirty()
        return energy

    def _clamp(self, value: float, cfg: HumanoidConfig, floor: float = 0.0) -> float:
        return max(floor, min(max(1.0, float(cfg.max_energy)), value))

    # ---------- 生理周期 ----------

    def advance_cycle(self, today: str | None = None) -> int:
        """按自然日推进周期天数。多天未运行时一次补齐。"""
        cfg = self._config()
        today = today or self._clock.today_str()
        data = self._state.data
        last = str(data.get("last_cycle_update", "") or "")
        if last == today:
            return self.cycle_day
        if not last:
            data["last_cycle_update"] = today
            self._state.mark_dirty()
            return self.cycle_day
        try:
            elapsed = (
                datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(last, "%Y-%m-%d")
            ).days
        except ValueError:
            data["last_cycle_update"] = today
            self._state.mark_dirty()
            return self.cycle_day
        if elapsed <= 0:
            return self.cycle_day
        length = max(1, cfg.cycle_length)
        data["current_cycle_day"] = (self.cycle_day - 1 + elapsed) % length + 1
        data["last_cycle_update"] = today
        self._state.mark_dirty()
        return int(data["current_cycle_day"])

    def reset_cycle(self, cycle_day: int, today: str | None = None) -> int:
        cfg = self._config()
        length = max(1, cfg.cycle_length)
        day = (int(cycle_day) - 1) % length + 1
        data = self._state.data
        data["current_cycle_day"] = day
        data["last_cycle_update"] = today or self._clock.today_str()
        self._state.mark_dirty()
        return day

    def cycle_description(self) -> str:
        cfg = self._config()
        if not cfg.enable_cycle:
            return ""
        day = self.cycle_day
        idx = cfg.cycle_phase_index(day)
        phase = PHASE_NAMES[idx]
        if cfg.cycle_description_style == "simple":
            return f"{phase}（第{day}天）"
        energy = self.energy
        if energy < 10:
            note = "，精力很低"
        elif energy < 30:
            note = "，精力偏低"
        elif energy > 80:
            note = "，精力充沛"
        else:
            note = ""
        return f"处于【{phase}】，{PHASE_NOTES[idx]}{note}"

    def _debug(self, message: str) -> None:
        if self._log is not None:
            self._log.debug(f"[humanoid_core] {message}")

