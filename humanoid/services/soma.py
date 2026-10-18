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
import random
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from ..config import HumanoidConfig
from ..role_scope import RoleScope
from ..slots import is_meal_event, is_sleep_event, sleep_window_minutes
from ..wording import FEELING_WORDS, band_index, pick, scale_word

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
# ── 着凉/感冒 ──────────────────────────────────────────────
# 受冻或长时间缺觉之后小概率着凉；一旦着了，持续一两天。这套是**状态**，
# 不是给她的台词：只变体感与形式倾向，说什么由她自己决定。
# 每次推进的着凉概率：基础率 × 暴露程度（取受冻/缺觉里更重的那个，按分钟计）。
CHILL_CHANCE_BASE = 0.00008     # 每分钟基础概率（暴露满值时）
CHILL_CHANCE_DOUBLE = 0.00006   # 又冷又缺觉时每分钟额外加这么多
CHILL_EXPOSURE_MIN = 0.35       # 暴露低于这个值不会着凉
CHILL_MIN_UNCOMFORT = 25.0      # 不适低于这个值不会着凉——不能只因为冷就生病
CHILL_RISE_PER_HOUR = 6.0       # 病程前半：着凉往感冒的顶上走，比恢复快（病来如山倒）
CHILL_RECOVER_PER_HOUR = 1.6    # 病程后半：从顶上慢慢好转（40 分约 25 小时好完）
CHILL_COMFORT_COOL = 12.0       # 低于这个温度算「冻着」
# 身体量落盘的最小间隔（秒）。饥饿这类轴每分钟都在动，不节流就等于每分钟写一次全量文件。
MIN_PERSIST_INTERVAL_SECONDS = 300.0
# 短于这个时长的一觉算**碎片**，不覆盖 `last_sleep_hours`。
# 日程是模型每天重排的，相邻两步之间 `is_sleep_event` 可能翻转（「夜间睡眠」→
# 「起床洗漱」→「回笼觉」→「早餐」），每一步都记一次醒来就会把「昨晚睡了多久」
# 写成最后那一小段（实测 0.17 小时），prompt 里于是出现「昨晚睡了 0.17 小时」。
# 阈值取 0.5 与 `prompt_builder` 的「睡了 >= 0.5 小时才算睡了一觉」同一口径。
SLEEP_FRAGMENT_MIN_HOURS = 0.5


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    # NaN 必须单独拦：`min(100.0, nan)` 在 Python 下返回 **100.0**（nan 与任何数比较
    # 都是 False），于是任何一个 NaN 经 `_set` 就被静默钉成上限，此后永不回落。
    # `state._clamp_float` 与 `mood._num_or` 都判 NaN，这里漏了。
    if value != value:
        return low
    return max(low, min(high, value))


def _rounded(value: float) -> float:
    return round(value, 2)


def _sleep_window(slots: list[dict]) -> tuple[float, float] | None:
    """日程里那段觉的小时表示；判定与合并都在 `slots.sleep_window_minutes`。"""
    window = sleep_window_minutes(slots)
    if window is None:
        return None
    start, end = window
    if end == start:
        return None
    return start / 60.0, end / 60.0


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
        self._random = random.random
        # 上一次落盘的快照：只有它变了才 mark_dirty，否则 60 秒一次的 tick 会把
        # 状态文件写成常驻磁盘写入。
        self._persisted: dict[str, Any] | None = None
        self._last_persist = 0.0
        # 新角色当场定下计时基准。不能等第一次 advance 才写：那时「现在」已经往前走了，
        # 启动后第一次补算会被当成零时长，把开机期间真正该补的那段体感全弄丢。
        self._ensure_data()
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

    def _drowsy_peak_now(self) -> bool:
        """现在是不是「可以说困得不行」的时候。

        两条任一成立：已经 21 点以后 / 凌晨 6 点以前；或者醒了 16 小时以上。
        下午五点、醒了十三小时——那是「有点困」，不是「困得不行」。
        """
        try:
            moment = self._moment_at(self.now)
        except Exception:
            return True  # 拿不到时间就别因为这一条把体感吞掉
        if moment.hour >= 21 or moment.hour < 6:
            return True
        try:
            awake_hours = (self.now - float(self.data.get("last_wake_at", 0.0) or 0.0)) / 3600.0
        except (TypeError, ValueError):
            return True
        return awake_hours >= 16.0

    def _ensure_data(self) -> dict[str, Any]:
        """拿到 `soma` 子字典，必要时建一个。**写路径**才用它。"""
        raw = self._scope.get_self("soma")
        if not isinstance(raw, dict):
            raw = {}
            self._scope.self_state["soma"] = raw
        return raw

    @property
    def data(self) -> dict[str, Any]:
        """身体状态字典。**只读**：读它不该创建键、也不该写 `last_tick`。

        旧写法在 property 里 `setdefault("last_tick", ...)` 并可能创建整个 `soma` 键，
        于是 `snapshot()`、`_get()` 这类只读路径（诊断面板、每条消息的注入）都会改状态
        并把状态标脏。计时基准现在由 `__init__` 与 `_tick_base()` 显式初始化。
        """
        raw = self._scope.get_self("soma")
        return raw if isinstance(raw, dict) else {}

    def _get(self, key: str, default: float = 0.0) -> float:
        try:
            value = float(self.data.get(key, default))
        except (TypeError, ValueError):
            return default
        # 非有限值不能进积分：NaN 会让 `int(math.ceil(...))` 抛 ValueError（advance 整条
        # 炸掉），也会经 `snapshot()` 进 `json.dumps` 写出非法 JSON，还会让
        # `_persist_if_changed` 的 `round(value)` 比较每 tick 都不相等 → 每 tick 都落盘。
        if not math.isfinite(value):
            return default
        return value

    def _tick_base(self, now: float) -> float:
        """上次推进点。缺失/NaN/inf 时不能拿它做差：就地修好基准并返回 `now`。"""
        try:
            value = float(self.data.get("last_tick"))
        except (TypeError, ValueError):
            value = float("nan")
        if not math.isfinite(value):
            self._ensure_data()["last_tick"] = _rounded(now)
            return now
        return value

    def _set(self, key: str, value: float) -> None:
        self._ensure_data()[key] = _rounded(_clamp(value))

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

        同样不能把「有日程」当成「现在被某段盖着」——刚跨过零点而新的一段还没排出来时，
        `slot_at` 拿不到东西，旧写法会把它当成「日程里没写睡」→ 不睡，而那时真正的
        问题是日程空窗。空窗就按「不知道」处理：两边都不假定（既不记睡了，也不硬说她醒着）。

        同时提到吃饭的时段算吃饭：「午餐与午休发呆」里那个「午休」把她判成睡着的话，
        中午就再也不会主动找人了。
        """
        slots = self._slots()
        if not slots:
            # 完全没日程（新装、或跨天且新段还没排出来）：不看夜间窗口。
            # 那几分钟里积分出来的「假睡眠」比少记一段睡更难看。
            return False
        current = self.slot_at(moment)
        event = str(current.get("event", "") or "") if current else ""
        if not event:
            return False
        return is_sleep_event(event)

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
        if not math.isfinite(now):
            # 时间源本身坏了（NaN/inf）：不推进也不写，别让 `int(math.ceil(nan))` 把
            # 整条 advance 炸掉。
            return self.snapshot()
        data = self._ensure_data()
        last = self._tick_base(now)
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
                # 该睡的时候醒着 = 在欠睡。窗口比她一晚需要的睡眠短的人，同样熬一小时
                # 更补不回来。封顶 SLEEP_DEBT_CAP：欠到一定程度后多欠的部分已经反映在
                # 困意里，再堆只会让后面的系数失去区分度。
                #
                # 增速**只从配置取**：这里曾经内联重写了一份
                # `min(3.0, max(1.0, sleep_need_hours / self._night_span_hours()))`，
                # 分母用的是「日程推出来的夜」（`biological_night()`），而
                # `config.sleep_debt_gain_per_hour` 用的是配置窗口 —— 只要模型排的睡眠
                # 窗口不等于配置的 23–6，两份就分叉（实测日程 00:00–08:00：配置 1.1429
                # 对 soma 1.0000），面板上调 `night_start_hour/night_end_hour` 影响不到
                # 欠债，`sleep_need_hours` 也被日程窗口改写。配置项是唯一来源。
                gain = float(cfg.sleep_debt_gain_per_hour)
                debt = min(
                    SLEEP_DEBT_CAP,
                    self._get("sleep_debt", 0.0) + hours * gain,
                )
                self.data["sleep_debt"] = _rounded(debt)
            if self.data.get("asleep_since") is not None:
                # 睡着→醒着的**翻转**（`asleep_since` 只在上面那一支里置位、只在这里
                # 被 `_record_wake` 清掉），不是「每一步都记一次醒来」。
                # 这一小段到底算不算一觉由 `_record_wake` 按碎片阈值判定。
                self._record_wake(moment)

        # --- 饥饿 ---
        interval = max(1.0, float(cfg.meal_interval_hours))
        if self.is_eating_time(moment):
            # 吃饭就是把这一顿吃了，不是“饱腹感慢慢回升”：一个用餐时段就该归零。
            self.data["hunger"] = 0.0
            self.data["last_meal_at"] = _rounded(moment.timestamp())
        elif asleep:
            # 睡着不进食，代谢也慢：饥饿缓慢回落，而不是整夜顶格饿到早餐。
            self._set("hunger", self._get("hunger", 0.0) - 8.0 * hours)
        else:
            # 渐近趋向上限：越饿涨得越慢，不会线性堆到 100 后卡死一整晚。
            remaining = 100.0 - self._get("hunger", 0.0)
            self._set("hunger", self._get("hunger", 0.0) + remaining * hours / interval)

        # --- 久坐与躯体不适 ---
        sitting_limit = max(15.0, float(cfg.sitting_discomfort_minutes))
        active = self._is_active_now(moment)
        if active:
            self.data["sitting_since"] = _rounded(moment.timestamp())
        sitting_hours = max(0.0, (moment.timestamp() - self._get("sitting_since", moment.timestamp())) / 3600.0)
        stiffness = 0.0
        if sitting_hours * 60.0 > sitting_limit:
            # `(sitting_hours*60 - sitting_limit)/60` 已经是「超出的小时数」，
            # 再乘 SITTING_DISCOMFORT_PER_HOUR 语义就对了。旧写法多乘了一个 6.0，
            # 实际速率 36 分/小时：坐 1.5 小时就顶格 45，超出门槛 75 分钟撞上限，
            # 于是她几乎整天都在喊不舒服。
            stiffness = min(45.0, (sitting_hours * 60.0 - sitting_limit) / 60.0 * SITTING_DISCOMFORT_PER_HOUR)

        thermal = self._thermal_discomfort()
        cycle = self._cycle_discomfort(moment)
        discomfort = _clamp(stiffness * 0.45 + thermal * 0.35 + cycle * 0.4 + self._get("hunger", 0.0) * 0.12)
        self._set("discomfort", discomfort)
        self._maybe_chill(minutes, moment, discomfort)

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

        **只有真正的一觉才改写 `last_sleep_hours`**：日程重排让 `is_sleep_event` 在
        相邻两步之间翻转时，会切出一小段「睡着」（实测 0.17 小时），它不是一觉。
        这种碎片照样清 `asleep_since`（她确实醒了），但不覆盖「昨晚睡了多久」，
        否则 schedule 的 prompt 里会出现「昨晚睡了：0.17 小时」。
        """
        since = self._get("asleep_since", 0.0)
        self.data["asleep_since"] = None
        if since <= 0:
            return
        slept_hours = max(0.0, (moment.timestamp() - since) / 3600.0)
        if slept_hours < SLEEP_FRAGMENT_MIN_HOURS:
            self.data["sitting_since"] = _rounded(moment.timestamp())
            return
        self.data["last_sleep_hours"] = _rounded(min(slept_hours, 24.0))
        self.data["sleep_hours_today"] = 0.0
        self.data["last_wake_at"] = _rounded(moment.timestamp())
        self.data["sitting_since"] = _rounded(moment.timestamp())
        # 睡够了一晚：困意落到低位，醒来该是清醒的（起落要看得出来）。
        # 睡着时积分里本来就在按 sleep_relief_per_hour 清，这里只是把「睡够」后
        # 的残留按上限夹低——是夹上限，不是再减一次，不会把同一夜算两遍。
        need = max(1.0, float(self.config.sleep_need_hours))
        if slept_hours >= need * 0.85:
            self._set("sleep_pressure", min(self._get("sleep_pressure", 20.0), 12.0))

    def biological_night(self) -> tuple[float, float] | None:
        """她的生物钟夜里是哪一段：**(起始小时, 结束小时)**，跨午夜用 start > end 表示。

        优先按她自己今天那份日程里最长的连续睡眠段推，日程没排睡眠（或还没生成）才退回
        配置里的夜间窗口。以前是反过来的：日程被 `align_sleep_to_night` 切到 23:00→06:00
        上去，夜猫子人格凌晨四点睡、十一点起，身体却按早上六点算“该醒了”，低谷也摆在
        下午——那等于一个固定窗口替所有人决定几点睡。

        `night_mode_enabled=False` 时**整个返回 None**：关掉夜间模式就是「不再有生物钟夜
        这一层」。旧写法只在**日程没排睡眠**时才看这个开关，于是日程里只要有一段觉，
        这个开关对核心积分就形同虚设——夜间照样加睡眠债、低谷照样锚定。
        """
        cfg = self.config
        if not cfg.night_mode_enabled:
            return None
        window = _sleep_window(self._slots())
        if window is None:
            if cfg.night_start_hour == cfg.night_end_hour:
                return None
            return float(cfg.night_start_hour), float(cfg.night_end_hour)
        return window

    def scheduled_night(self) -> tuple[float, float] | None:
        """她**日程里**推出来的夜；日程没排睡眠（或还没生成）时返回 None。

        与 `biological_night()` 的区别：这个**不退回配置窗口**。判断「现在是不是她
        自己的夜里」必须用这个——配置窗口只是没日程时的兑底，拿它硬套会让夜猫子在
        23:00-06:00 被当成「她的生物钟夜」（生成日程时去排睡眠、注入时说睡眠时段）。
        """
        return _sleep_window(self._slots())

    def in_scheduled_night(self, hour_of_day: float) -> bool:
        """给定钟点是不是在她日程推出来的夜里。没有日程 → False。"""
        night = self.scheduled_night()
        if night is None:
            return False
        start, end = night
        if start == end:
            return False
        if start < end:
            return start <= hour_of_day < end
        return hour_of_day >= start or hour_of_day < end

    def in_scheduled_deep_sleep(self, hour_of_day: float) -> bool:
        """她日程推出来的夜里前 `night_deep_sleep_ratio` 比例那一段。没日程 → False。"""
        night = self.scheduled_night()
        if night is None:
            return False
        start, end = night
        if start == end:
            return False
        span = end - start if end > start else (24.0 - start) + end
        ratio = max(0.1, min(1.0, self.config.night_deep_sleep_ratio))
        offset = (hour_of_day - start) % 24.0
        return 0.0 <= offset < span * ratio

    def _night_mid_hour(self) -> float | None:
        """生物钟夜的中点；没有生物钟夜（夜间模式关掉、窗口长度为 0）时是 None。"""
        night = self.biological_night()
        if night is None:
            return None
        start, end = night
        return ((start + (end + 24.0 if end < start else end)) / 2.0) % 24.0

    def _in_biological_night(self, hour_of_day: float) -> bool:
        night = self.biological_night()
        if night is None:
            return False
        start, end = night
        if start == end:
            return False
        if start < end:
            return start <= hour_of_day < end
        return hour_of_day >= start or hour_of_day < end

    def _is_active_now(self, moment: datetime) -> bool:
        """日程里写着运动/通勤/家务/社交 → 没有久坐。"""
        event = str(self.slot_at(moment).get("event", ""))
        return any(k in event for k in ("运动", "跑步", "健身", "通勤", "外出", "家务", "散步", "社交", "聚会"))

    def _temp_celsius(self) -> float | None:
        """当前气温（℃）。拿不到（没开天气/没取到）返回 None。"""
        if not self.config.weather_affects_body or self._weather is None:
            return None
        try:
            snapshot = self._weather() or {}
            text = str(snapshot.get("env", ""))
        except Exception:
            return None
        return _extract_celsius(text)

    def _thermal_discomfort(self) -> float:
        temp = self._temp_celsius()
        if temp is None:
            return 0.0
        if temp <= 12.0:
            return _clamp((12.0 - temp) * COLD_DISCOMFORT_PER_DEGREE)
        if temp >= 30.0:
            return _clamp((temp - 30.0) * HEAT_DISCOMFORT_PER_DEGREE)
        return 0.0

    def _maybe_chill(self, minutes: float, moment: datetime, discomfort: float) -> None:
        """受冻或长时间缺觉之后小概率着凉；着了就走完整个病程：着凉→感冒→恢复。

        起头靠的是**客观量**：暴露程度取「受冻有多重」与「欠觉有多重」里更重的那个，
        两者同时成立再加一点。一旦着了，先往上养几天到「感冒」的顶（`chill_peak`），
        再从顶上慢慢好转——所以它不是一根见了就降的斜坡，而是一段有起有落的病。
        它只变体感与形式倾向，说什么由她自己决定。
        """
        level = self._get("chill", 0.0)
        if level > 0.0:
            # 一次推进可能跨过峰顶（插件停机后补算、测试一次推进几小时）：
            # 先把小时数喂给「加重」那段，用不完的再喂给「恢复」那段。
            # 用显式的「在加重」标记，而不是拿 level < peak 判——到了峰顶会先回落
            # 一点点，那个判据会让它每轮又被拉回峰顶，永远下不来。
            hours = minutes / 60.0
            if self._get("chill_rising", 0.0) >= 1.0:
                peak = self._get("chill_peak", 0.0)
                rise = min(max(0.0, peak - level), CHILL_RISE_PER_HOUR * hours)
                level += rise
                hours -= rise / CHILL_RISE_PER_HOUR
                if level >= peak - 1e-9:
                    level = peak
                    self._set("chill_rising", 0.0)
            level -= CHILL_RECOVER_PER_HOUR * hours
            if level <= 0.0:
                self._set("chill", 0.0)
                self._set("chill_peak", 0.0)
                self._set("chill_rising", 0.0)
            else:
                self._set("chill", level)
            return
        if discomfort < CHILL_MIN_UNCOMFORT:
            return
        temp = self._temp_celsius()
        cold = (
            min(1.0, (CHILL_COMFORT_COOL - temp) / CHILL_COMFORT_COOL)
            if temp is not None and temp <= CHILL_COMFORT_COOL
            else 0.0
        )
        debt = min(1.0, self._get("sleep_debt", 0.0) / 8.0)
        exposure = max(cold, debt)
        if exposure < CHILL_EXPOSURE_MIN:
            return
        chance = CHILL_CHANCE_BASE * exposure * minutes
        if cold >= 0.5 and debt >= 0.5:
            chance += CHILL_CHANCE_DOUBLE * minutes
        if chance > 0 and self._random() < chance:
            # 起头只是「着凉」：轻。峰值（感冒）由当时的不适定——越虚病得越重。
            self._set("chill", _clamp(24.0 + discomfort * 0.1, 24.0, 32.0))
            self._set("chill_peak", _clamp(48.0 + discomfort * 0.3, 48.0, 78.0))
            self._set("chill_rising", 1.0)

    def _cycle_discomfort(self, moment: datetime) -> float:
        cfg = self.config
        if not cfg.enable_cycle:
            return 0.0
        day = int(self._scope.get_self("current_cycle_day", 1) or 1)
        phase = cfg.cycle_phase_index(day)
        # 8 阶段里身体本身不舒服的几段（按 28 天标准轴）：
        # 0 月经期最重、7 黄体晚期（PMS）次之、6 黄体中（乳房胀/犯困）轻一些、
        # 4 排卵期可能有一侧小腹的隐痛；其余阶段基本没有。
        return {0: 60.0, 4: 12.0, 6: 22.0, 7: 38.0}.get(phase, 6.0)

    def _desire_modifier(self) -> float:
        """很困/很不舒服时，「想找人说话」涨得慢一点。

        但压制只留给**真难受**：原来 sleep_pressure > 55 就乘 0.75，而夜里正是她既独处
        （所以在攒）又最容易困（所以被按住）的时段——两相一抵，晚上几乎攒不起来，
        社交插件那个「挺想找人说说话」（desire>=75）就永远轮不到她。

        所以门槛提高：真的困到不行（>85）才明显压制，中等程度的困不影响她想找人。
        """
        pressure = self._get("sleep_pressure", 0.0)
        discomfort = self._get("discomfort", 0.0)
        factor = 1.0
        if pressure > 85.0:
            factor *= 0.55
        elif pressure > 70.0:
            factor *= 0.85
        if discomfort > 70.0:
            factor *= 0.7
        return factor

    # ------------------------------------------------------------------
    # 只读视图
    # ------------------------------------------------------------------

    def circadian_dip(self, hour_of_day: float) -> float:
        """昼夜低谷强度 0~1：生物钟夜里中点后最低，午后有个次低谷，其余时段清醒。

        锚点用的是 `biological_night()`——她自己的日程里那段觉的中点，不是配置里那个
        写死的 23→6。低谷摆错位会让她下午 5 点最想睡、凌晨 3 点最清醒。"""
        # 次低谷：午饭后。与夜间窗口无关，先算出来。
        lunch = abs(((hour_of_day - 14.0 + 12.0) % 24.0) - 12.0)
        secondary = max(0.0, 1.0 - lunch / 2.0) * 0.35
        anchor = self._night_mid_hour()
        if anchor is None:
            # 没有生物钟夜（夜间模式关掉、或窗口长度为 0）：主低谷不锚定，
            # 只留午后的次低谷——否则「关掉夜间模式」对核心积分等于没关。
            return _clamp(secondary, 0.0, 1.0)
        # 主低谷：生物钟夜里中点后 3 小时左右（核心体温最低点）。
        main = (hour_of_day - (anchor + 3.0)) % 24.0
        main_cost = min(main, 24.0 - main)
        primary = max(0.0, 1.0 - main_cost / 5.0)
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
            "chill": _rounded(self._get("chill", 0.0)),
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
        """(显著度 0~1, 体感一句话)。只保留中性的身体事实，移除所有替AI说话或带情绪倾向的台词。

        身体轴里实际会用的是 sleepy / debt / hunger / discomfort / arousal。后两条的词表
        早就写好了（wording.FEELING_WORDS）但一直没被调用——她算了「多清醒」「多想找人
        说话」却一句都没进过上下文。energy 也终于用上了：之前它只是个摆设参数，精力 5 和
        精力 95 的体感输出完全一样。

        social_desire 的词表已删：想不想说话由 behavior 的 initiative 独占。
        """
        out: list[tuple[float, str]] = []
        snap = self.snapshot()
        seed = [self._scope.role_id, self._today_key()]
        try:
            energy = float(energy)
        except (TypeError, ValueError):
            energy = 60.0

        def say(kind: str, value: float, floor: float, span: float, top: float,
                reverse: bool = False) -> str:
            """挂一条体感：**分数跟着词走，不跟数值走。**

            原来分数是 `(value - floor) / span` 算出来的，而词是按**档位**查表的——
            两边各走各的，于是会出现「说得很重但说不出口」：

                hunger=88 → 词已经是「饿得发慌」，分数 0.733，门槛 0.75 → 被滤掉
                arousal=30 → 词已经是「精力不太够」，分数 0.333 → 被滤掉

            越难受越不说，和这层存在的理由正好相反。改成按档位给分：
            顶档拿满 `top`（那一档本来就是「值得说」的），越往下越低。
            `reverse=True` 用于「值越低越严重」的那种轴（没精神）。
            """
            ladder = FEELING_WORDS.get(kind) or []
            band = band_index(value, ladder)
            word = pick(kind, seed + [band], scale_word(value, ladder))
            if not (word and value >= floor):
                return word
            steps = max(1, len(ladder) - 1)
            pos = (steps - band) if reverse else band
            # 顶档 1.0 → 拿满；每往下一档掉 0.22，最下面那档约 0.34
            score = top * (0.34 + 0.66 * (pos / steps))
            out.append((_clamp(score, 0.0, top), word))
            return word

        # 睡眠压力顶格以后，措辞就永远停在「困得不行」——下午五点和凌晨两点说同一句，
        # 用户的原话是「困意太随意」。真实的人在下午只是「有点困」，那句「困得不行」要等
        # 真的撑不住：很晚了（21 点后），或者已经醒了 16 小时以上。
        # 只压**说法**，不压数值：这一项照旧往上走，睡眠判定与日程不受影响。
        pressure = float(snap["sleep_pressure"])
        if pressure >= 88.0 and not self._drowsy_peak_now():
            pressure = 87.0
        say("sleepy", pressure, 50.0, 45.0, 0.95)

        debt = snap["sleep_debt"]
        if debt >= 1.0:
            say("debt", debt, 1.0, 5.0, 0.85)

        if snap["asleep"] >= 1.0:
            out.append((0.9, "这会儿在睡"))

        say("hunger", snap["hunger"], 55.0, 45.0, 0.8)

        # 状态降温：同一个饥饿值，刚起的和持续了几小时的不是一句话。
        # 持续时间从 last_meal_at 算出来，是客观数，不是查表。
        if snap["hunger"] >= 55.0:
            last_meal = float(self.data.get("last_meal_at", 0.0) or 0.0)
            if last_meal > 0:
                hunger_hours = (self.now - last_meal) / 3600.0
                if hunger_hours >= 2.5:
                    out.append(
                        (
                            0.85,
                            pick(
                                "hunger_long",
                                seed + [min(3, int(hunger_hours))],
                                ("饿了好一阵了", "肚子空了有一阵", "饿了有一会儿了"),
                            ),
                        )
                    )

        discomfort = snap["discomfort"]
        if discomfort >= 62:
            say("discomfort", discomfort, 60.0, 40.0, 0.85)
        elif discomfort >= 38:
            out.append((0.5, "略有不适"))

        chill = snap.get("chill", 0.0)
        if chill >= 28:
            say("chill", chill, 28.0, 60.0, 0.8)

        # 清醒度：精力掉了人会发懵，精力足了人才清醒。两者现在合在一起看，
        # 因为 arousal 本身也是从精力和作息推出来的，单独用会让同一件事说两遍。
        arousal = float(snap.get("arousal", 50.0) or 0.0)
        if energy <= 35.0 or arousal <= 30.0:
            # 显著度要**反向**：这一支是「她很没精神」才进的，值越低越该说。
            #
            # 原来直接 `say(..., 0.0, 45.0, 0.7)`，算出来是 `(value-0)/45`——
            # 于是 arousal=10 得 0.22、arousal=20 得 0.44，全被 prompt 侧那道 0.7 的
            # 门槛滤掉。**越难受越说不出来**，正好和这一支存在的理由相反。
            # 高档那边（下面 `energy>=80 and arousal>=70`）是同一个毛病：写死 0.6，
            # 同样进不去。两处一起修的。
            # `reverse=True`：这一支是「很没精神」才进的，值越低越该说。
            # 原来写死 `0.6`、低档写 `1 - level/45`，两处都跟档位脱钩。
            say("arousal", min(arousal, energy), 0.0, 45.0, 0.8, reverse=True)
        elif energy >= 80.0 and arousal >= 70.0:
            # 显著度必须**高过** prompt 侧那道 0.7 的门槛，否则这一整档永远进不了上下文。
            #
            # 原来写的是 0.6 —— 于是「她很精神」这件事她算了、词表也写了、注释还专门
            # 解释「终于用上了」，但 `_feelings_lines(threshold=0.7)` 把它全滤掉了。
            # 而且方向正好是反的：低唤醒那档走 `say(..., top=0.7)` 能到 0.7 进得去，
            # 高唤醒这档固定 0.6 进不去。一个「arousal 的词表一次都没被调用过」的旧
            # 断言就是这么被别的词蒙混过关、一直没修的。
            out.append((0.78, pick("arousal_high", seed + [min(2, int(arousal // 10))],
                                   ("人挺精神的", "脑子转得挺快"))))

        # 「想不想找人说话」不进注入：它与行为倾向的 initiative 是同一件事，而后者综合了
        # 精力、社交能量与刚发生的事件，信息更多。两边都说就是「有交流意愿，她有点想
        # 说话」——模型会当成两回事。数值仍留在 snapshot 里给契约（link.py）用。
        return out

    def _today_key(self) -> str:
        """措辞抽签用的「今天」：拿她自己城市的日期，跨午夜那天换一套说法。"""
        try:
            return self._clock.today_str()
        except Exception:
            return ""

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
        elif snap.get("chill", 0.0) >= 45:
            max_chars = 60
        elif energy < 25:
            max_chars = 60
        if snap["sleep_debt"] >= 4.0:
            max_chars = min(max_chars, 50)

        # `question_bias` 一律「取最严」：身体压制是一条条**收紧**的，社交渴望只允许在
        # 完全没有身体压制时把它抬起来。
        #
        # 旧写法两种口径混用（前两条直接赋值、第三条用 min、第四条又直接赋值），
        # 于是 740 行的 0.5 会**无条件覆盖**前面所有压制：实测「睡眠压力 90 + 社交渴望 70」
        # → `max_chars=20` 却 `question_bias=0.5`，等于「只准写 20 字、但一半概率在反问」。
        caps: list[float] = []
        if snap["hunger"] >= 88 or snap["discomfort"] >= 62:
            caps.append(0.05)
        if snap.get("chill", 0.0) >= 45:
            caps.append(0.15)
        if snap["sleep_pressure"] >= 72:
            caps.append(0.1)
        question_bias = min([0.35] + caps)
        if not caps and snap["social_desire"] >= 70 and energy >= 50:
            question_bias = 0.5

        return {
            "max_chars": max_chars,
            "question_bias": round(question_bias, 2),
            "long_reply_ok": max_chars >= 90,
            # 「能不能连着发」不再从 max_chars 推。旧写法是 `max_chars <= 45`，于是它
            # **只在她最难受的时候为真**（20/40/45 三档），而精力充沛时为假——语义正好
            # 倒过来：有余力才谈得上连着发，累到 20 字上限的人更可能发完就没电。
            # 社交层拿它决定「主动开话题时能不能接二条」，判反了就是越难受越被连着戳。
            "burst_ok": bool(
                energy >= 55
                and snap["sleep_pressure"] < 72
                and snap["discomfort"] < 62
                and snap.get("chill", 0.0) < 45
                and snap["asleep"] < 1.0
            ),
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
            - max(0.0, self._get("chill", 0.0) - 30.0) * 0.15
        )
        return _clamp(ceiling, 15.0, 100.0)

    def note_schedule_changed(self) -> None:
        """日程重新生成后调用：**结算**这一觉，而不是把它悄悄抹掉。

        调用点（`core_instance._on_schedule_installed`）只在「上一段是睡、这一段不是睡」
        时触发，那正是真正的睡醒边界。旧实现只把 `asleep_since` 置 None 就返回，而
        `_record_wake` 是**唯一**会写 `last_sleep_hours` / 清 `sleep_hours_today` /
        写 `last_wake_at` 的地方，于是这三个值永远停在上一次真正走积分路径的醒来时刻
        （很可能停在昨天甚至更早），prompt 里「昨晚睡了多久」不再更新、`sleep_hours_today`
        跨天累加。现在统一走 `_record_wake`，碎片仍然由它按 SLEEP_FRAGMENT_MIN_HOURS
        判定为「不是一觉」——**睡眠区间被重排不算醒来**这条语义没有丢。
        """
        self._record_wake(self._clock.now())
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
