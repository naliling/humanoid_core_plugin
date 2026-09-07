"""精力与生理周期 - 使用 RoleScope 版本。"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import datetime

from ..clock import format_state_timestamp, parse_state_timestamp
from ..config import HumanoidConfig
from ..role_scope import RoleScope
from ..slots import Slot, clamp_rate, parse_time

CONSUMPTION_DISCOUNT = 0.7
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
        return "精力充沛，语气轻快"
    if energy >= 70:
        return "状态良好，语气正常"
    if energy >= 40:
        return "状态一般，语气平和"
    if energy >= 20:
        return "有点累，语气慵懒"
    return "很疲惫，语气低落"


class EnergyService:
    __slots__ = ("_scope", "_config", "_clock", "_log", "_schedule")

    def __init__(
        self,
        scope: RoleScope,
        config_provider: Callable[[], HumanoidConfig],
        clock,
        schedule_provider: Callable[[], list[Slot]] | None = None,
        logger=None,
    ) -> None:
        self._scope = scope
        self._config = config_provider
        self._clock = clock
        self._schedule = schedule_provider
        self._log = logger

    @property
    def config(self) -> HumanoidConfig:
        return self._config()

    @property
    def energy(self) -> float:
        try:
            return float(self._scope.get_self("energy", DAY_START_ENERGY))
        except (TypeError, ValueError):
            return DAY_START_ENERGY

    @property
    def max_energy(self) -> float:
        return max(1.0, float(self.config.max_energy))

    def describe(self, energy: float | None = None) -> str:
        return describe_energy(self.energy if energy is None else energy)

    @property
    def cycle_day(self) -> int:
        try:
            return max(1, int(self._scope.get_self("current_cycle_day", 1)))
        except (TypeError, ValueError):
            return 1

    def advance(self, now: datetime | None = None) -> float:
        cfg = self.config
        now = now or self._clock.now()
        data = self._scope.self_state

        last = parse_state_timestamp(str(data.get("last_update", "") or ""), now)
        if last is None or last > now:
            data["last_update"] = format_state_timestamp(now)
            self._scope.mark_dirty()
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
        self._scope.mark_dirty()
        return energy

    def _reset_for_new_day(self, cfg: HumanoidConfig, now: datetime) -> float:
        energy = self._clamp(DAY_START_ENERGY * random.uniform(0.95, 1.05), cfg, floor=5.0)
        data = self._scope.self_state
        data["energy"] = round(energy, 1)
        data["last_update"] = format_state_timestamp(now)
        self._scope.mark_dirty()
        return energy

    def _compute_delta(self, start_min: float, end_min: float, cfg: HumanoidConfig) -> float:
        if self._schedule is None:
            return 0.0
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
            if cfg.enable_energy_natural_recovery and recovery_per_min > 0:
                credited = (int(minutes) // recovery_step) * recovery_step
                if credited > 0:
                    total += recovery_per_min * recovery_factor * decay * credited
        return total

    def consume_for_message(self) -> float:
        cfg = self.config
        amount = max(0.0, float(cfg.energy_consumption_per_msg))
        if amount <= 0:
            return self.energy
        energy = self._clamp(self.energy - amount, cfg)
        self._scope.set_self("energy", round(energy, 1))
        return energy

    def reset(self, value: float = DAY_START_ENERGY) -> float:
        cfg = self.config
        energy = self._clamp(value, cfg)
        self._scope.update_self(
            energy=round(energy, 1),
            last_update=format_state_timestamp(self._clock.now())
        )
        return energy

    def _clamp(self, value: float, cfg: HumanoidConfig, floor: float = 0.0) -> float:
        return max(floor, min(max(1.0, float(cfg.max_energy)), value))

    def advance_cycle(self, today: str | None = None) -> int:
        cfg = self.config
        today = today or self._clock.today_str()
        last = str(self._scope.get_self("last_cycle_update", "") or "")
        if last == today:
            return self.cycle_day
        if not last:
            self._scope.set_self("last_cycle_update", today)
            return self.cycle_day
        try:
            elapsed = (
                datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(last, "%Y-%m-%d")
            ).days
        except ValueError:
            self._scope.set_self("last_cycle_update", today)
            return self.cycle_day
        if elapsed <= 0:
            return self.cycle_day
        length = max(1, cfg.cycle_length)
        day = (self.cycle_day - 1 + elapsed) % length + 1
        self._scope.update_self(
            current_cycle_day=day,
            last_cycle_update=today,
        )
        return int(day)

    def reset_cycle(self, cycle_day: int, today: str | None = None) -> int:
        cfg = self.config
        length = max(1, cfg.cycle_length)
        day = (int(cycle_day) - 1) % length + 1
        self._scope.update_self(
            current_cycle_day=day,
            last_cycle_update=today or self._clock.today_str(),
        )
        return day

    def cycle_description(self) -> str:
        cfg = self.config
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