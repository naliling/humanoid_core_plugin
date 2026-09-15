"""生理层（soma）：多轴稳态系统。

设计要点，与 v2.13.2 之前的「一个精力标量」的区别：

1. **时间自己流动。** 轴的值由 `advance()` 按真实经过的时间积分，推进来自一个独立的
   后台 tick 与冷启动补算，不依赖用户发消息。没人聊天的那 12 小时里，她依然会困、
   会饿、会坐太久肩膀发僵。
2. **轴之间耦合。** 睡眠债压低次日精力基线；不适感放大精力消耗；饥饿与久坐互相加成；
   天气的冷热并进不适感；周期阶段给的是「感受」而不只是一个系数。
3. **产出的是体感，不是数字。** `feelings()` 返回第一人称短句与显著度，`form_policy()`
   返回这一轮说话的形式倾向。数值只用于内部积分与 contract 导出。

不建模的东西：口渴、排尿、精确体温。它们只会让注入变长、让角色像在背生理书。
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from ..config import HumanoidConfig
from ..role_scope import RoleScope
from ..slots import is_meal_event, is_sleep_event

# 一次补算最多推进多久。插件停机一周后重启，不该攒出「睡眠压力 5000」。
MAX_CATCHUP_HOURS = 72.0
# 积分步长（分钟）。日程时段以分钟为粒度，10 分钟足够平滑，72 小时也只有 432 步。
STEP_MINUTES = 10.0

SLEEP_PRESSURE_PER_HOUR = 7.5      # 清醒时每小时攒的睡眠压力
# 睡着时的清除速率不在这里写定：它由 sleep_need_hours 推出来（见 config.sleep_relief_per_hour），
# 否则「一晚该睡多久」那项配置根本进不了计算。
SLEEP_DEBT_RELIEF_PER_HOUR = 0.5   # 生物钟夜里睡着时，每小时抵掉多少小时的债
SLEEP_DEBT_CAP = 20.0              # 睡眠债上限（小时）：人会补觉，不会欠 80 小时
SITTING_DISCOMFORT_PER_HOUR = 6.0  # 久坐每小时累积的僵硬感
COLD_DISCOMFORT_PER_DEGREE = 1.6   # 低于舒适温度每摄氏度累积的不适
HEAT_DISCOMFORT_PER_DEGREE = 1.2
DESIRE_CAP = 100.0
# 身体量落盘的最小间隔（秒）。饥饿这类轴每分钟都在动，不节流就等于每分钟写一次全量文件。
MIN_PERSIST_INTERVAL_SECONDS = 300.0


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _rounded(value: float) -> float:
    return round(value, 2)


class SomaService:
    """一个角色的身体。所有轴都存在 role self 的 `soma` 子字典里。"""

    def __init__(
        self,
        scope: RoleScope,
        config_provider: Callable[[], HumanoidConfig],
        clock,
        schedule_provider: Callable[[], list[dict]] | None = None,
        weather_provider: Callable[[], dict] | None = None,
        time_source: Callable[[], float] = time.time,
    ) -> None:
        self._scope = scope
        self._config = config_provider
        self._clock = clock
        self._schedule = schedule_provider
        self._weather = weather_provider
        self._time = time_source
        # 上一次落盘的快照：只有它变了才 mark_dirty，否则 60 秒一次的 tick 会把
        # 状态文件写成常驻磁盘写入。
        self._persisted: dict[str, Any] | None = None
        self._last_persist = 0.0
        # 新角色当场定下计时基准。不能等第一次 advance 才写：那时「现在」已经往前走了，
        # 启动后第一次补算会被当成零时长，把开机期间真正该补的那段体感全弄丢。
        self.data.setdefault("last_tick", _rounded(self._time()))
        self._persist_if_changed()

    # ------------------------------------------------------------------
    # 存储
    # ------------------------------------------------------------------

    @property
    def config(self) -> HumanoidConfig:
        return self._config()

    @property
    def now(self) -> float:
        """当前时刻（走注入的时间源，仿真里可信）。"""
        return float(self._time())

    @property
    def data(self) -> dict[str, Any]:
        raw = self._scope.get_self("soma")
        if not isinstance(raw, dict):
            raw = {}
            self._scope.self_state["soma"] = raw
        raw.setdefault("last_tick", self._time())
        return raw

    def _get(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.data.get(key, default))
        except (TypeError, ValueError):
            return default

    def _set(self, key: str, value: float) -> None:
        self.data[key] = _rounded(_clamp(value))

    # ------------------------------------------------------------------
    # 推进
    # ------------------------------------------------------------------

    def slot_at(self, moment: datetime) -> dict:
        minutes = moment.hour * 60 + moment.minute
        for slot in self._slots():
            start = slot.get("start")
            end = slot.get("end")
            if not start or not end:
                continue
            s = self._to_minutes(start)
            e = self._to_minutes(end)
            if e <= s:
                e = 1440
            if s <= minutes < e:
                return slot
        return {}

    @staticmethod
    def _to_minutes(text: Any) -> int:
        try:
            parts = str(text).split(":")
            return int(parts[0]) * 60 + int(parts[1])
        except (TypeError, ValueError, IndexError):
            return 0

    def is_sleep_time(self, moment: datetime) -> bool:
        """日程里写着睡才算睡着。

        不能把夜间窗口当成「她在睡」：熬夜的时候她 3 点仍然醒着，该记的是睡眠债而不是
        把困意清零。没有日程可用时才退到夜间窗口。

        同时提到吃饭的时段算吃饭：「午餐与午休发呆」里那个「午休」把她判成睡着的话，
        中午就再也不会主动找人了。
        """
        slots = self._slots()
        if slots:
            return is_sleep_event(self.slot_at(moment).get("event", ""))
        cfg = self.config
        return bool(cfg.night_mode_enabled and cfg.is_night_hour(moment.hour))

    def _slots(self) -> list[dict]:
        if self._schedule is None:
            return []
        try:
            return list(self._schedule() or [])
        except Exception:
            return []

    def is_eating_time(self, moment: datetime) -> bool:
        return is_meal_event(self.slot_at(moment).get("event", ""))

    def advance(self, now: float | None = None) -> dict[str, float]:
        """把身体从上次推进点积分到现在。返回推进后的轴快照。"""
        now = self._time() if now is None else float(now)
        data = self.data
        last = self._get("last_tick", now)
        if now <= last:
            # 时钟回拨（NTP 校正、时区改动）：只把基准挪回来，不倒推身体。
            if now < last:
                data["last_tick"] = _rounded(now)
                self._persist_if_changed()
            return self.snapshot()

        elapsed_hours = (now - last) / 3600.0
        capped = min(elapsed_hours, MAX_CATCHUP_HOURS)
        steps = max(1, int(math.ceil(capped * 60.0 / STEP_MINUTES)))
        step_seconds = (capped * 3600.0) / steps

        moment = self._moment_at(last)
        for _ in range(steps):
            point = moment + timedelta(seconds=step_seconds)
            self._integrate(step_seconds / 60.0, point)
            moment = point

        data["last_tick"] = _rounded(now)
        self._persist_if_changed()
        return self.snapshot()

    def _moment_at(self, epoch: float) -> datetime:
        try:
            return datetime.fromtimestamp(epoch, tz=self._clock.now().tzinfo)
        except (OSError, OverflowError, ValueError, AttributeError):
            return self._clock.now()

    def _integrate(self, minutes: float, moment: datetime) -> None:
        cfg = self.config
        hours = minutes / 60.0
        asleep = self.is_sleep_time(moment)
        hour_of_day = moment.hour + moment.minute / 60.0

        # --- 睡眠压力与睡眠债 ---
        in_night = self._in_biological_night(hour_of_day)
        if asleep:
            self._set(
                "sleep_pressure",
                self._get("sleep_pressure", 20.0) - cfg.sleep_relief_per_hour * hours,
            )
            slept = self._get("sleep_hours_today", 0.0) + hours
            self._set("sleep_hours_today", _clamp(slept, 0.0, 24.0))
            if self.data.get("asleep_since") is None:
                # 记下这一觉从哪开始，醒来时才知道睡了多久、什么时候醒的。
                self.data["asleep_since"] = _rounded(moment.timestamp())
            if in_night:
                # 债的回收只在生物钟夜里计时，且按半速：天天凌晨两点睡的人不该永远零负债。
                self.data["sleep_debt"] = _rounded(
                    max(0.0, self._get("sleep_debt", 0.0) - hours * SLEEP_DEBT_RELIEF_PER_HOUR)
                )
        else:
            pressure = self._get("sleep_pressure", 20.0) + SLEEP_PRESSURE_PER_HOUR * hours
            # 昼夜相位叠加：生物钟低谷时段困意涨得更快。
            pressure += self.circadian_dip(hour_of_day) * 4.0 * hours
            self._set("sleep_pressure", pressure)
            if in_night:
                # 该睡的时候醒着 = 在欠睡。夜间窗口比 sleep_need_hours 短的人，同样熬一小时
                # 更补不回来，所以按 cfg.sleep_debt_gain_per_hour 放大。封顶 SLEEP_DEBT_CAP：
                # 欠到一定程度之后多欠的部分已经反映在困意里，再堆只会让后面的系数失去区分度。
                debt = min(
                    SLEEP_DEBT_CAP,
                    self._get("sleep_debt", 0.0) + hours * cfg.sleep_debt_gain_per_hour,
                )
                self.data["sleep_debt"] = _rounded(debt)
            if self.data.get("asleep_since") is not None:
                self._record_wake(moment)

        # --- 饥饿 ---
        interval = max(1.0, float(cfg.meal_interval_hours))
        hunger_rate = 100.0 / interval
        if self.is_eating_time(moment):
            # 吃饭就是把这一顿吃了，不是“饱腹感慢慢回升”：一个用餐时段就该归零。
            self.data["hunger"] = 0.0
            self.data["last_meal_at"] = _rounded(moment.timestamp())
        else:
            self._set("hunger", self._get("hunger", 0.0) + hunger_rate * hours)

        # --- 久坐与躯体不适 ---
        sitting_limit = max(15.0, float(cfg.sitting_discomfort_minutes))
        active = self._is_active_now(moment)
        if active:
            self.data["sitting_since"] = _rounded(moment.timestamp())
        sitting_hours = max(0.0, (moment.timestamp() - self._get("sitting_since", moment.timestamp())) / 3600.0)
        stiffness = 0.0
        if sitting_hours * 60.0 > sitting_limit:
            stiffness = min(45.0, (sitting_hours * 60.0 - sitting_limit) / 60.0 * SITTING_DISCOMFORT_PER_HOUR * 6.0)

        thermal = self._thermal_discomfort()
        cycle = self._cycle_discomfort(moment)
        discomfort = _clamp(stiffness * 0.45 + thermal * 0.35 + cycle * 0.4 + self._get("hunger", 0.0) * 0.12)
        self._set("discomfort", discomfort)

        # --- 唤醒度：白天高、深夜低，深夜还醒着会留下「累但睡不着」的尾巴 ---
        target = self._arousal_target(moment, hour_of_day)
        current = self._get("arousal", 50.0)
        self._set("arousal", current + (target - current) * min(1.0, hours * 1.5))

        # --- 想说话的程度：独处时攒，聊过之后泄掉 ---
        refill = max(0.5, float(cfg.desire_refill_hours))
        last_chat = self._get("last_chat_at", 0.0)
        if last_chat <= 0:
            self.data["last_chat_at"] = _rounded(moment.timestamp())
        desire = self._get("social_desire", 0.0)
        # 渐近趋向上限，而不是线性堆到顶后卡死：线性模型下只要独处够久就永远是 100，
        # 社交层拿到的就是一个不随时间变化的常数。
        growth = (DESIRE_CAP - desire) / refill * hours
        growth *= self._desire_modifier()
        self._set("social_desire", desire + growth)

    def _record_wake(self, moment: datetime) -> None:
        """一觉醒来：记下这觉睡了多久、什么时候醒的。

        这里**不再动睡眠压力**：睡着的每一个积分步都已经按 sleep_relief_per_hour 在清，
        醒来时再减一次等于把同一夜睡了两遍。小睡清不掉困意这件事，由积分本身保证。

        这里也**不记睡眠债**。债完全由「生物钟夜里醒着多久 / 睡着多久」对称积分出来。
        以前在这里用「睡够没够 sleep_need_hours」补一笔债，会撞上一个真实现象：
        大模型每天重新生成日程，午夜那一刻当日的睡眠区间可能从 23:00–07:00 变成
        01:00–09:00，正在睡的身体会当场被判为「醒了」，只睡了两小时就被记成欠了六小时。
        """
        since = self._get("asleep_since", 0.0)
        self.data["asleep_since"] = None
        if since <= 0:
            return
        slept_hours = max(0.0, (moment.timestamp() - since) / 3600.0)
        self.data["last_sleep_hours"] = _rounded(min(slept_hours, 24.0))
        self.data["sleep_hours_today"] = 0.0
        self.data["last_wake_at"] = _rounded(moment.timestamp())
        self.data["sitting_since"] = _rounded(moment.timestamp())

    def _in_biological_night(self, hour_of_day: float) -> bool:
        cfg = self.config
        if not cfg.night_mode_enabled:
            return False
        start, end = cfg.night_start_hour, cfg.night_end_hour
        if start == end:
            return False
        if start < end:
            return start <= hour_of_day < end
        return hour_of_day >= start or hour_of_day < end

    def _is_active_now(self, moment: datetime) -> bool:
        """日程里写着运动/通勤/家务/社交 → 没有久坐。"""
        event = str(self.slot_at(moment).get("event", ""))
        return any(k in event for k in ("运动", "跑步", "健身", "通勤", "外出", "家务", "散步", "社交", "聚会"))

    def _thermal_discomfort(self) -> float:
        if not self.config.weather_affects_body or self._weather is None:
            return 0.0
        try:
            snapshot = self._weather() or {}
            text = str(snapshot.get("env", ""))
        except Exception:
            return 0.0
        temp = _extract_celsius(text)
        if temp is None:
            return 0.0
        if temp <= 12.0:
            return _clamp((12.0 - temp) * COLD_DISCOMFORT_PER_DEGREE)
        if temp >= 30.0:
            return _clamp((temp - 30.0) * HEAT_DISCOMFORT_PER_DEGREE)
        return 0.0

    def _cycle_discomfort(self, moment: datetime) -> float:
        cfg = self.config
        if not cfg.enable_cycle:
            return 0.0
        day = int(self._scope.get_self("current_cycle_day", 1) or 1)
        phase = cfg.cycle_phase_index(day)
        # 0=经期 5=经前期：这两段身体本身不舒服，其余阶段基本没有。
        return {0: 60.0, 5: 28.0, 4: 18.0}.get(phase, 6.0)

    def _desire_modifier(self) -> float:
        pressure = self._get("sleep_pressure", 0.0)
        discomfort = self._get("discomfort", 0.0)
        factor = 1.0
        if pressure > 75.0:
            factor *= 0.45
        elif pressure > 55.0:
            factor *= 0.75
        if discomfort > 55.0:
            factor *= 0.6
        return factor

    # ------------------------------------------------------------------
    # 只读视图
    # ------------------------------------------------------------------

    def circadian_dip(self, hour_of_day: float) -> float:
        """昼夜低谷强度 0~1：生物钟夜里中点后最低，午后有个次低谷，其余时段清醒。"""
        cfg = self.config
        anchor = cfg.night_mid_hour
        # 主低谷：生物钟夜里中点后 2 小时左右（核心体温最低点）。
        main = (hour_of_day - (anchor + 3.0)) % 24.0
        main_cost = min(main, 24.0 - main)
        primary = max(0.0, 1.0 - main_cost / 5.0)
        # 次低谷：午饭后。
        lunch = abs(((hour_of_day - 14.0 + 12.0) % 24.0) - 12.0)
        secondary = max(0.0, 1.0 - lunch / 2.0) * 0.35
        return _clamp(max(primary, secondary), 0.0, 1.0)

    def _arousal_target(self, moment: datetime, hour_of_day: float) -> float:
        dip = self.circadian_dip(hour_of_day)
        base = 78.0 - 55.0 * dip
        if self._get("sleep_pressure", 0.0) > 70.0 and dip < 0.5:
            base += 12.0  # 累但亢奋，睡不着的那种
        if self._get("discomfort", 0.0) > 50.0:
            base += 8.0
        return _clamp(base)

    def snapshot(self) -> dict[str, float]:
        data = self.data
        return {
            "sleep_pressure": _rounded(self._get("sleep_pressure", 20.0)),
            "sleep_debt": _rounded(self._get("sleep_debt", 0.0)),
            "hunger": _rounded(self._get("hunger", 0.0)),
            "discomfort": _rounded(self._get("discomfort", 0.0)),
            "arousal": _rounded(self._get("arousal", 50.0)),
            "social_desire": _rounded(self._get("social_desire", 0.0)),
            "asleep": 1.0 if data.get("asleep_since") else 0.0,
            "last_sleep_hours": _rounded(self._get("last_sleep_hours", 0.0)),
        }

    def _persist_if_changed(self) -> None:
        """身体漂移落盘节流。

        tick 是 60 秒一次，饥饿每分钟都在动；按原值比较会把 state.json 写成常驻磁盘
        写入。这里按 1 分档位比较，并且两次落盘至少间隔 MIN_PERSIST_INTERVAL：身体量
        本来就慢，插件停止时本来就会强制 flush 一次，节流不会丢东西。
        """
        current = {key: round(value) for key, value in self.snapshot().items()}
        if current == self._persisted:
            return
        now = self._time()
        if self._persisted is not None and now - self._last_persist < MIN_PERSIST_INTERVAL_SECONDS:
            return
        self._persisted = current
        self._last_persist = now
        self._scope.mark_dirty()

    # ------------------------------------------------------------------
    # 体感与形式倾向
    # ------------------------------------------------------------------

    def feelings(self, energy: float) -> list[tuple[float, str]]:
        """(显著度 0~1, 第一人称体感)。显著度用于门控，只挑最高的几条注入。"""
        out: list[tuple[float, str]] = []
        snap = self.snapshot()
        cfg = self.config

        pressure = snap["sleep_pressure"]
        if pressure >= 88:
            out.append((0.95, "眼皮很沉，句子不想写长，脑子转不动"))
        elif pressure >= 72:
            out.append((0.75, "困意上来了，说话开始有气无力"))
        elif pressure >= 55:
            out.append((0.45, "有点想睡了"))

        debt = snap["sleep_debt"]
        if debt >= 4.0:
            out.append((0.85, f"欠的觉还没补回来，整个人是钝的"))
        elif debt >= 1.5:
            out.append((0.55, "昨晚没睡够，今天有点缓不过来"))

        if snap["asleep"] >= 1.0:
            out.append((0.9, "正在睡着，被吵醒的话只会迷迷糊糊说一两句"))

        hunger = snap["hunger"]
        if hunger >= 88:
            out.append((0.8, "肚子饿得有点难受，没什么耐心聊复杂的事"))
        elif hunger >= 60:
            out.append((0.45, "有点饿了"))

        discomfort = snap["discomfort"]
        if discomfort >= 62:
            out.append((0.85, "身上不太舒服，只想安静待着"))
        elif discomfort >= 38:
            out.append((0.5, _pick_discomfort_text(self, cfg)))

        if snap["arousal"] >= 78 and energy < 45:
            out.append((0.6, "其实很累，但精神还有点亢，安静不下来"))
        elif snap["arousal"] <= 22 and energy >= 55:
            out.append((0.4, "提不起劲，得有人说点什么我才动得了"))

        if int(self.data.get("ignored_streak", 0) or 0) >= 3:
            out.append((0.5, "主动找TA几次都没回，不太想先开口了"))

        if cfg.enable_cycle:
            day = int(self._scope.get_self("current_cycle_day", 1) or 1)
            phase = cfg.cycle_phase_index(day)
            if phase == 0 and discomfort >= 30:
                out.append((0.7, "肚子坠着疼，情绪也比平时薄"))
            elif phase == 2:
                out.append((0.35, "今天身上比较松快，做事有劲"))

        return out

    def form_policy(self, energy: float, social_energy: float) -> dict[str, Any]:
        """身体这一轮够得着多大份量：只算状态，不是给她的规矩，也不碰内容。"""
        snap = self.snapshot()
        max_chars = 120
        if snap["sleep_pressure"] >= 88 or snap["asleep"] >= 1.0:
            max_chars = 20
        elif snap["sleep_pressure"] >= 72:
            max_chars = 40
        elif snap["discomfort"] >= 62:
            max_chars = 45
        elif energy < 25:
            max_chars = 60
        if snap["sleep_debt"] >= 4.0:
            max_chars = min(max_chars, 50)

        question_bias = 0.35
        if snap["hunger"] >= 88 or snap["discomfort"] >= 62:
            question_bias = 0.05
        if snap["sleep_pressure"] >= 72:
            question_bias = 0.1
        if snap["social_desire"] >= 70 and energy >= 50:
            question_bias = 0.5

        return {
            "max_chars": max_chars,
            "question_bias": round(question_bias, 2),
            "long_reply_ok": max_chars >= 90,
            "burst_ok": max_chars <= 45,
        }

    def note_chat(self, now: float | None = None) -> None:
        """刚聊过：把想说话的程度泄掉，并重置独处计时。"""
        now = self._time() if now is None else float(now)
        self.data["last_chat_at"] = _rounded(now)
        self._set("social_desire", self._get("social_desire", 0.0) - 55.0)
        self._set("arousal", self._get("arousal", 50.0) + 6.0)
        self._persist_if_changed()

    def note_message(self) -> None:
        """每条消息：久坐计时不动，但连续说话会磨掉一点精神。"""
        self._set("arousal", self._get("arousal", 50.0) + 1.5)
        self._persist_if_changed()

    def note_proactive(self, at: float) -> None:
        """社交层刚替她主动开过口：想说的心思说出去一部分。

        不泄掉的话 contract 里的 social_desire 会一直顶格，社交层也就看不出「这句已经说过了」。
        比真人聊一句泄得少：没人接话的那句其实没说透。
        """
        self._set("social_desire", self._get("social_desire", 0.0) - 40.0)
        self.data["last_proactive_at"] = _rounded(at)
        self._persist_if_changed()

    def set_social_feedback(self, ignored_streak: int) -> None:
        """社交层回写的「连发几次没人接」，会变成一句压着的挂念。"""
        try:
            streak = max(0, int(ignored_streak))
        except (TypeError, ValueError):
            return
        if int(self.data.get("ignored_streak", 0) or 0) == streak:
            return
        self.data["ignored_streak"] = streak
        self._persist_if_changed()

    def sleep_debt_penalty(self) -> float:
        """欠睡对次日精力基线的折损系数（0.5~1.0）。"""
        debt = self._get("sleep_debt", 0.0)
        return _clamp(1.0 - debt * 0.04, 0.5, 1.0)

    def energy_ceiling(self) -> float:
        """今天最多能有多少精力。

        这是身体与日程之间最关键的一条耦合：日程只说得上这一天做了些什么，而欠着
        一身债的人就算整个下午都在休息也回不满。没有这个封顶时会出现「睡眠压力 100
        而精力 100」两个轴互相打脸的状态。
        """
        pressure = self._get("sleep_pressure", 0.0)
        discomfort = self._get("discomfort", 0.0)
        ceiling = (
            100.0
            - max(0.0, pressure - 45.0) * 0.9
            - max(0.0, discomfort - 50.0) * 0.25
        )
        return _clamp(ceiling, 15.0, 100.0)

    def note_schedule_changed(self) -> None:
        """日程重新生成后调用：睡眠区间变了不算真的醒来。"""
        self.data["asleep_since"] = None
        self._persist_if_changed()

    def discomfort_factor(self) -> float:
        """不适感对精力消耗的放大系数。"""
        return 1.0 + self._get("discomfort", 0.0) / 220.0

    def reset(self) -> None:
        self._scope.self_state["soma"] = {"last_tick": _rounded(self._time())}
        self._persisted = None
        self._scope.mark_dirty()


def _extract_celsius(text: str) -> float | None:
    for index, char in enumerate(text):
        if char in "℃C" and index:
            start = index - 1
            while start > 0 and (text[start - 1].isdigit() or text[start - 1] in ".-"):
                start -= 1
            try:
                return float(text[start:index].strip())
            except ValueError:
                return None
    return None


def _pick_discomfort_text(soma: SomaService, cfg: HumanoidConfig) -> str:
    sitting = soma.data.get("sitting_since", 0.0)
    thermal = soma._thermal_discomfort()
    if thermal >= 18:
        return "身上凉飕飕的，缩着"
    if thermal >= 12:
        return "有点热，闷得慌"
    if sitting:
        return "坐太久了，肩颈发僵"
    return "身上不太得劲"
