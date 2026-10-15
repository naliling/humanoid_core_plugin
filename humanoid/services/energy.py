"""精力与生理周期 - 使用 RoleScope 版本。"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import datetime, time, timedelta
from typing import Any

from ..clock import format_state_timestamp, parse_state_timestamp
from ..config import HumanoidConfig
from ..wording import band_index, pick, scale_word


# 精力五档，每档几套等价说法（同一状态不必每次同一句）。
ENERGY_WORDS: list[tuple[float, tuple[str, ...]]] = [
    (90, ("精力充沛", "身上有劲", "精神足")),
    (70, ("状态挺好", "精力够用", "不算累")),
    (40, ("状态一般", "有点消耗", "平平的")),
    (20, ("有点累", "提不起多少劲", "累了")),
    (0, ("很疲惫", "身上没劲", "累得不想动")),
]
from ..role_scope import RoleScope
from ..slots import Slot, clamp_rate, parse_time

CONSUMPTION_DISCOUNT = 0.7
# 8 个阶段各一段的精力消耗系数（按 28 天标准轴：月经期 / 卵泡早 / 卵泡中 /
# 卵泡晚 / 排卵 / 黄体早 / 黄体中 / 黄体晚）。
CONSUMPTION_PHASE_FACTORS = (1.3, 0.9, 0.8, 0.85, 1.0, 1.05, 1.1, 1.25)
DAY_START_ENERGY = 80.0
# 8 个阶段的名字与角色扮演化的体感描述。
#
# 以前是 6 个阶段 + 几句干巴巴的「能量水平逐步回升」，读起来像仪表盘读数。
# 这份周期本来就该有它自己的样子：激素在涨落，身体跟着起伏——描述要能让人
# 一眼看出她现在在周期的哪一段、身体是什么感觉。仍然只说身体，不写台词。
PHASE_NAMES = (
    "月经期", "卵泡早期", "卵泡中期", "卵泡晚期",
    "排卵期", "黄体早期", "黄体中期", "黄体晚期",
)
PHASE_NOTES = (
    # 1-5：激素谷底，内膜脱落，体能最差
    "激素跌到谷底，小腹一阵阵坠着疼、腰也发酸，人像被抽走了电，只想缩着不动",
    # 6-8：开始重启，一点点回血
    "身体在慢慢重启，精力一点点爬回来，心里比前几天松快，身子还有点发干",
    # 9-11：雌激素爬坡，状态回春
    "激素在往上爬，皮肤和精神都回了春，脑子转得比前阵子快，也开始想亲近人",
    # 12-13：雌激素冲顶，状态最好
    "雌激素快到顶了，体力、心气都拉满，社交的欲望也在抬头，整个人往外放",
    # 14：排卵，激素最盛后垂落
    "激素冲到最高点又骤然回落，身体格外敏感，一侧小腹隐约发酸，体温触底要反弹",
    # 15-19：孕酮升起，安稳
    "孕酮升起来，体温上了一个档，身体进入安稳模式，情绪稳但别惹",
    # 20-22：双峰最满，嗜睡馋碳水
    "激素双高撑到最满，内膜最厚，乳房发胀、容易犯困，嘴馋碳水、对什么都敏感",
    # 23-28：激素跳水，PMS
    "激素开始跳水，身子浮肿、冒痘，乳房碰一下都不舒服，情绪像蒙了层灰",
)


def _day_start(moment: datetime) -> datetime:
    """当天 00:00（同一时区）。跨天结算要把窗口切成「昨天剩下的」与「今天到现在的」。"""
    return datetime.combine(moment.date(), time(0, 0), tzinfo=moment.tzinfo)


def _day_epoch(anchor: datetime, minutes: float) -> float:
    """把「某天的第几分钟」换算成 epoch 秒。

    日程时段是墙钟分钟，而经过时长必须按 epoch 差算：夏令时前进日 01:30→03:30 的
    墙钟差是 120 分钟，真实只过了 60 分钟；回退日反过来。
    """
    base = datetime.combine(anchor.date(), time(0, 0), tzinfo=anchor.tzinfo)
    return (base + timedelta(minutes=minutes)).timestamp()


def _covered_minutes(spans: list[tuple[float, float]], start_ts: float, end_ts: float) -> float:
    """这些区间在 [start_ts, end_ts] 里一共盖了多少分钟（重叠只算一次）。"""
    total = 0.0
    cursor = start_ts
    for lo, hi in sorted(spans):
        lo = max(lo, cursor)
        hi = min(hi, end_ts)
        if hi <= lo:
            continue
        total += hi - lo
        cursor = hi
    return total / 60.0


def describe_energy(energy: float, seed=None) -> str:
    """精力档位词。**只说身体，不说语气**——「精力充沛，语气轻快」那半句是在教她怎么说话。"""
    ladder = ENERGY_WORDS
    word = scale_word(energy, ladder)
    # 种子给档位序号而不是实时值：精力 88 → 92 同属「精力充沛」，换个说法就成了另一个人。
    return pick("energy", list(seed or []) + [band_index(energy, ladder)], word) if word else ""


class EnergyService:
    __slots__ = ("_scope", "_config", "_clock", "_log", "_schedule", "_body")

    def __init__(
        self,
        scope: RoleScope,
        config_provider: Callable[[], HumanoidConfig],
        clock,
        schedule_provider: Callable[[], list[Slot]] | None = None,
        logger=None,
        body_provider: Callable[[], Any] | None = None,
    ) -> None:
        self._scope = scope
        self._config = config_provider
        self._clock = clock
        self._schedule = schedule_provider
        self._log = logger
        # 生理层（SomaService）：欠睡会压低一天的起点，身上不舒服会让消耗更陡。
        self._body = body_provider

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
        try:
            seed = [self._scope.role_id, self._clock.today_str()]
        except Exception:
            seed = None
        return describe_energy(self.energy if energy is None else energy, seed)

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

        energy = self.energy
        if last.date() < now.date():
            # 跨天：先落到新一天的起点，再从 00:00 结算到现在。
            #
            # 旧写法在这里直接 `return self._reset_for_new_day(...)`，00:00→now 那一段
            # 一分未计——实测 `last_update=05-31 23:59`、`now=06-01 08:00`、日程
            # `00:00–24:00 rate=-0.15` 时返回 79.61，八小时的消耗凭空消失。
            #
            # 新起点本身已经代表「睡醒后的状态」（`_reset_for_new_day`），所以这一截
            # 只结算**消耗**，不再叠加恢复：否则同一夜的休息会被算两遍（起点一次、
            # 午夜后一次），凌晨三点就能把精力顶到上限。
            energy = self._new_day_baseline(cfg)
            energy = self._settle(energy, _day_start(now), now, cfg, recovery=False)
        else:
            energy = self._settle(energy, last, now, cfg)
        energy = self._apply_body_ceiling(energy, cfg)
        data["energy"] = round(energy, 1)
        data["last_update"] = format_state_timestamp(now)
        self._scope.mark_dirty()
        return energy

    def _apply_body_ceiling(self, energy: float, cfg: HumanoidConfig) -> float:
        """身体封顶：欠睡与不舒服时，日程里写着休息也回不到满值。"""
        body = self._body_axis()
        if body is None:
            return energy
        try:
            ceiling = float(body.energy_ceiling())
        except Exception:
            return energy
        return min(energy, max(0.0, ceiling))

    def _new_day_baseline(self, cfg: HumanoidConfig) -> float:
        """新一天的起点不是固定 80：欠着睡眠债的时候，起床也不会「满血」。"""
        body = self._body_axis()
        penalty = body.sleep_debt_penalty() if body is not None else 1.0
        return self._clamp(
            DAY_START_ENERGY * random.uniform(0.95, 1.05) * penalty, cfg, floor=5.0
        )

    def _body_axis(self) -> Any:
        if self._body is None:
            return None
        try:
            return self._body()
        except Exception:
            return None

    def _settle(
        self,
        energy: float,
        start: datetime,
        end: datetime,
        cfg: HumanoidConfig,
        *,
        recovery: bool = True,
    ) -> float:
        """把 start→end 这一段按日程结算掉。

        时长一律走 epoch 差。旧写法拿「墙钟分钟差」当经过时长：夏令时前进日
        01:30→03:30 的墙钟差是 120 分钟（真实 60 分钟，虚增 2 倍），回退日
        01:45→01:30 直接 `end_min <= start_min` 早退，45 分钟凭空蒸发。
        """
        start_ts = start.timestamp()
        end_ts = end.timestamp()
        if end_ts <= start_ts:
            return energy
        delta = self._compute_delta(start_ts, end_ts, start, cfg, recovery=recovery)
        return self._clamp(energy + delta, cfg)

    def _compute_delta(
        self,
        start_ts: float,
        end_ts: float,
        anchor: datetime,
        cfg: HumanoidConfig,
        *,
        recovery: bool = True,
    ) -> float:
        if self._schedule is None:
            return 0.0
        slots = self._schedule()
        decay = max(0.0, float(cfg.energy_decay_rate))
        if decay == 0.0:
            return 0.0

        body = self._body_axis()
        discomfort_factor = body.discomfort_factor() if body is not None else 1.0

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
        covered: list[tuple[float, float]] = []
        for slot in slots:
            slot_start = parse_time(slot.get("start"))
            slot_end = parse_time(slot.get("end"))
            if slot_start is None or slot_end is None or slot_end <= slot_start:
                continue
            lo = _day_epoch(anchor, slot_start)
            hi = _day_epoch(anchor, slot_end)
            overlap_start = max(lo, start_ts)
            overlap_end = min(hi, end_ts)
            if overlap_end <= overlap_start:
                continue
            covered.append((overlap_start, overlap_end))
            minutes = (overlap_end - overlap_start) / 60.0

            rate = clamp_rate(slot.get("energy_rate", 0.0))
            if rate < 0:
                # 消耗永远计：跨天之后那一截也要算，旧写法整段丢掉。
                total += (
                    rate * CONSUMPTION_DISCOUNT * consumption_factor * decay * minutes * discomfort_factor
                )
                continue
            if not recovery:
                continue

            total += rate * decay * minutes
            if cfg.enable_energy_natural_recovery and recovery_per_min > 0:
                credited = (int(minutes) // recovery_step) * recovery_step
                if credited > 0:
                    total += recovery_per_min * recovery_factor * decay * credited

        # 空隙：日程非空但没盖到的那几分钟，旧写法既不消耗也不恢复 —— 精力被冻住。
        # 这与 185-187 行「没日程可依时按自然恢复算，不按 0」是同一条口径，只是
        # 旧实现只处理了「完全没有日程」，没处理「有日程但有洞」。
        if recovery:
            gap = (end_ts - start_ts) / 60.0 - _covered_minutes(covered, start_ts, end_ts)
            if gap > 0:
                total += self._idle_recovery(gap, cfg)
        return total

    def _idle_recovery(self, minutes: float, cfg: HumanoidConfig) -> float:
        """没有日程可依的那段空窗：只给自然恢复，不给消耗。

        给恢复是因为「什么也没做」实际是躺着的概率远高于「什么也没做且一直在消耗」；
        幅度按分钟数走，不看是什么时段——反正这段时间本来就不该有结论。
        """
        if not cfg.enable_energy_natural_recovery:
            return 0.0
        per_min = max(0.0, float(cfg.energy_natural_recovery_per_minute))
        if per_min <= 0:
            return 0.0
        step = max(1, int(cfg.energy_natural_recovery_interval_minutes))
        credited = (int(max(0.0, minutes)) // step) * step
        if credited <= 0:
            return 0.0
        return (
            per_min
            * cfg.phase_recovery_multiplier(self.cycle_day)
            * max(0.0, float(cfg.energy_decay_rate))
            * credited
        )

    def consume_for_message(self) -> float:
        cfg = self.config
        amount = max(0.0, float(cfg.energy_consumption_per_msg))
        if amount <= 0:
            return self.energy
        energy = self._clamp(self.energy - amount, cfg)
        self._scope.set_self("energy", round(energy, 1))
        return energy

    def reset(self, value: float | None = None) -> float:
        """重置精力。未指定数值时使用一个小范围随机初始值，避免每次固定为 80。"""
        cfg = self.config
        if value is None:
            # 保持“状态良好”的合理区间，同时给每次手动重置一点自然波动。
            value = DAY_START_ENERGY * random.uniform(0.90, 1.10)
        energy = self._clamp(float(value), cfg)
        self._scope.update_self(
            energy=round(energy, 1),
            last_update=format_state_timestamp(self._clock.now())
        )
        return energy

    def _clamp(self, value: float, cfg: HumanoidConfig, floor: float = 0.0) -> float:
        # floor 也要夹到上限以内：`max_energy=1.0`（配置允许）时旧的
        # `max(5.0, min(1.0, value))` 会返回 5.0 —— 超过 max_energy 四倍。
        ceiling = max(1.0, float(cfg.max_energy))
        return max(min(floor, ceiling), min(ceiling, value))

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
        """周期描述。只说周期，不夹带精力评价——精力由体感（arousal）说，
        这里再补一句「精力充沛」就是同一件事在一句话里说两遍。"""
        cfg = self.config
        if not cfg.enable_cycle:
            return ""
        day = self.cycle_day
        idx = cfg.cycle_phase_index(day)
        phase = PHASE_NAMES[idx]
        if cfg.cycle_description_style == "simple":
            return f"{phase}（第{day}天）"
        return f"处于【{phase}】，{PHASE_NOTES[idx]}"