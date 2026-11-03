"""单一真源（B3 / R1）：同一个事实只在一个地方算，别处只读它。

## 这个文件解决的是什么

审核里有一整类问题反复出现：**同一件事被算了三遍，三遍的口径各不相同**。

* 关系阶段：注入用「好感 + 相处」，`api.py` / `emotion.py` / `mood.py` 只传好感，
  于是同一个用户在同一轮里既「熟到可以直说」又「还在熟悉」；
* 睡没睡：`soma.wake_state()`、`soma.sleep_state()`、日程里的睡段、夜间窗口
  各说各的，注入里写着「正睡着」，面板里写着「醒着」；
* 精力/饥饿的档位：同一张梯子在两处各写一份，改了一处忘了另一处；
* 攻击性默认值：28 / 15 / 25 三个数分别写在 config、api、prompt_builder、emotion、ccb 里。

它们不是「算错了」，是**没有唯一真源**：每多一个消费方，就多一份可能漂移的副本。
这个模块把这几件事收成一处，其余模块改成「读快照」。

## 纪律

1. **只算不写**。这里不推进身体、不建档案、不 mark_dirty——它被注入路径每轮调用，
   带副作用就会把「读一眼」变成「改一次状态」。
2. **三态优先**。不知道就返回 `available=False` / `state="unknown"`，
   **不要**用 0、50、False 冒充「正常」。旧代码正是这么做的：身体模块关掉时导出一组
   中性值，消费方读成「她状态正常」。
3. **默认值只有一份**。`DEFAULT_AGGRESSION` 与 `config.mood_initial_aggression` 的默认值
   必须相等；有配置时一律读配置。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping, Optional

from . import clock as _clock
from . import relstage

# ── 睡眠三态 ────────────────────────────────────────────────────────────
SLEEP_AWAKE = "awake"
SLEEP_ASLEEP = "asleep"
SLEEP_UNKNOWN = "unknown"

# 拿不到的原因。消费方拿它区分「关掉了」和「坏了」，不要一律当成「没有」。
REASON_RECORD = "record"
REASON_MOOD_DISABLED = "mood_disabled"
REASON_SOMA_DISABLED = "soma_disabled"
REASON_NO_STATE = "no_state"
REASON_NO_SLOTS = "no_slots"

#: 攻击性的出厂默认值。**必须**与 `config.py` 里 `mood_initial_aggression` 的默认值一致
#: （审核 P-10：这个数曾在 28 / 15 / 25 三个地方各写一份，面板显示的和注入里说的不是同一个）。
DEFAULT_AGGRESSION = 28.0


def _num(value: Any, default: float = 0.0) -> float:
    """坏值容错：state.json 是用户能手改的文件，NaN/inf/字符串都要挡住。"""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Relation:
    """她对某个人此刻的关系位置：**唯一的一份**。

    `stage_id/name/phrase` 来自 `relstage.stage_of(affection, turns, days)`，
    `turns`/`days` 是「相处」那一半证据。三个消费方（注入 / 门面 / 情绪）读同一份，
    不再出现「注入说她熟到可以直说、门面说她还在熟悉」。
    """

    affection: float = 0.0
    turns: int = 0
    days: float = 0.0
    stage_id: int = 0
    stage_name: str = ""
    stage_phrase: str = ""
    available: bool = False
    reason: str = REASON_NO_STATE

    @property
    def known(self) -> bool:
        return self.available


#: 拿不到关系时用它，**不要**临时造一个 `Relation(0,0,...)` 再当成事实。
UNKNOWN_RELATION = Relation()


def tz_of(core: Any) -> Any:
    """角色时区（Clock.tz）；拿不到返回 None（= 宿主本地）。

    关系天数、CCB 日键、天气新鲜度都要按**她的**时区算日历天。这个函数是唯一
    入口：谁需要时区就问它，别在别处自己 fromtimestamp 或猜宿主时区。
    """
    try:
        return core.clock.tz
    except Exception:
        return None


def relation_of(
    profile: Optional[Mapping[str, Any]],
    now_epoch: float = 0.0,
    *,
    available: bool = True,
    reason: str = REASON_RECORD,
    tz: Any = None,
) -> Relation:
    """从一份情绪档案算出关系位置。

    `turn_count` 会被清理逻辑归零（长期不联系的用户），而 `first_met` 保留——
    直接拿归零后的轮数去算，关系阶段会**倒退**（审核 状态与人设 2）。所以这里取
    `turn_count` 与 `turn_count_peak`（旧档案没有这个键）的较大者：相处过的证据一旦
    成立就不该被清理抹掉。
    """
    data: Mapping[str, Any] = profile or {}
    if not available:
        return Relation(available=False, reason=reason)
    affection = _num(data.get("affection"), 0.0)
    turns = max(_int(data.get("turn_count"), 0), _int(data.get("turn_count_peak"), 0))
    first_met = _num(data.get("first_met"), 0.0)
    now = _num(now_epoch, 0.0)
    # 认识几天按**日历天**算（她的时区），不是 24 小时整块截断：23:59 认识、
    # 次日 00:01 已经是「认识 2 天」，按秒差却还是 0 天。注入侧（prompt_builder
    # 的 _met_days_line）用的是同一把尺（审核 注入生成 P-09）。
    days = 0.0
    if first_met > 0 and now > 0:
        try:
            days = float(max(0, _clock.day_gap(first_met, now, tz)))
        except Exception:
            days = max(0.0, (now - first_met) / 86400.0)
    try:
        stage_id, stage_name, stage_phrase = relstage.stage_of(affection, turns, days)
    except Exception:
        stage_id, stage_name, stage_phrase = 0, relstage.BANDS[0][1], relstage.BANDS[0][2]
    return Relation(
        affection=affection,
        turns=turns,
        days=days,
        stage_id=int(stage_id),
        stage_name=str(stage_name),
        stage_phrase=str(stage_phrase),
        available=True,
        reason=reason,
    )


def relation(
    core: Any,
    user_id: str,
    is_group: bool = False,
    now_epoch: Optional[float] = None,
) -> Relation:
    """读某个人的关系位置。**只读**：不可读（开关关掉 / 群里不给陌生人建档）时返回
    `available=False`，不调用 `mood.profile()` 现建一份档案。"""
    try:
        cfg = core.prompt_builder.config
    except Exception:
        cfg = getattr(core, "config", None)
    try:
        readable = bool(
            core.mood.readable(
                user_id,
                is_group,
                enabled=getattr(cfg, "mood_enabled", True),
                in_group=getattr(cfg, "mood_enabled_in_group", False),
            )
        )
    except Exception:
        readable = False
    if not readable:
        return Relation(available=False, reason=REASON_MOOD_DISABLED)
    now = _num(now_epoch if now_epoch is not None else core.now_epoch(), 0.0)
    try:
        profile = core.mood.profile(user_id) or {}
    except Exception:
        return Relation(available=False, reason=REASON_NO_STATE)
    return relation_of(profile, now, tz=tz_of(core))


@dataclass(frozen=True)
class Sleep:
    """她此刻的睡眠态：**唯一的一份**。

    `state` 三态；`reason` 说明为什么是这个态（睡着是夜里的觉还是白天的小睡、
    拿不到是身体模块关掉了还是日程空窗）。`asleep` 是给旧消费方的方便属性：
    三态里只有 `SLEEP_ASLEEP` 才是 True，`UNKNOWN` 返回 `None`。
    """

    state: str = SLEEP_UNKNOWN
    reason: str = REASON_NO_STATE
    asleep_since: float = 0.0
    wake_at: float = 0.0
    inertia_until: float = 0.0
    disturbed: bool = False
    planned_wake: float = 0.0

    @property
    def asleep(self) -> Optional[bool]:
        if self.state == SLEEP_ASLEEP:
            return True
        if self.state == SLEEP_AWAKE:
            return False
        return None

    @property
    def known(self) -> bool:
        return self.state != SLEEP_UNKNOWN


def _local_hour(core: Any) -> Optional[float]:
    try:
        moment = core.clock.now()
        return float(moment.hour) + float(moment.minute) / 60.0
    except Exception:
        return None


def _planned_wake(core: Any) -> float:
    """日程里这一觉的结束时刻（epoch 秒）。拿不到就是 0.0 = 不知道。"""
    try:
        slot = core.schedule.current_slot() or {}
        end = str(slot.get("end", "") or "").strip()
        if not end:
            return 0.0
        hour, minute = (int(part) for part in end.split(":")[:2])
        moment = core.clock.now()
        wake = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if wake <= moment:
            # 结束点在墙钟上已经过去 → 是明天那个点（跨午夜的觉）。
            wake = wake + timedelta(days=1)
        return float(wake.timestamp())
    except Exception:
        return 0.0


def sleep_of(core: Any, now_epoch: Optional[float] = None) -> Sleep:
    """读她此刻的睡眠态。**只读**，不推进身体。

    旧实现里 `wake_state()` 的 `asleep` 只来自 `asleep_since`，而「睡着」本身是
    日程积分写进去的——两处都能回答「她睡没睡」，于是注入、面板、契约各读一份。
    这里统一：`state` 只从身体模块取，`reason` 由日程推出来（夜里那段觉 / 白天小睡），
    拿不到就是 `UNKNOWN`，不再用 `False` 冒充「醒着」。
    """
    cfg = getattr(core, "config", None)
    if not bool(getattr(cfg, "soma_enabled", False)):
        return Sleep(state=SLEEP_UNKNOWN, reason=REASON_SOMA_DISABLED)
    try:
        state = core.soma.wake_state()
    except Exception:
        return Sleep(state=SLEEP_UNKNOWN, reason=REASON_NO_STATE)
    if not isinstance(state, dict):
        return Sleep(state=SLEEP_UNKNOWN, reason=REASON_NO_STATE)
    asleep = bool(state.get("asleep"))
    now = _num(now_epoch if now_epoch is not None else core.now_epoch(), 0.0)
    reason = REASON_RECORD
    if asleep:
        hour = _local_hour(core)
        if hour is not None:
            # 「夜里那觉」还是「白天小睡」按**同一个夜窗口**判（`night()`），
            # 不再单独问 `soma.in_scheduled_night`——那是只认日程的第二个答案。
            reason = "night" if _in_night_now(core) else "nap"
    try:
        since = float(getattr(core.soma, "data", {}).get("asleep_since", 0.0) or 0.0)
    except Exception:
        since = 0.0
    return Sleep(
        state=SLEEP_ASLEEP if asleep else SLEEP_AWAKE,
        reason=reason,
        asleep_since=since,
        wake_at=_num(state.get("wake_at"), 0.0),
        inertia_until=_num(state.get("inertia_until"), 0.0),
        disturbed=bool(state.get("disturbed")),
        planned_wake=_planned_wake(core) if asleep else 0.0,
    )


def _scheduled_night(core: Any) -> Optional[tuple[float, float]]:
    """日程推出来的睡段（她**自己的**生物钟夜）。拿不到返回 None。"""
    soma = getattr(core, "soma", None)
    if soma is None:
        return None
    for name in ("scheduled_night", "biological_night"):
        getter = getattr(soma, name, None)
        if not callable(getter):
            continue
        try:
            night = getter()
        except Exception:
            return None
        if not night:
            return None
        try:
            return (float(night[0]), float(night[1]))
        except (TypeError, ValueError, IndexError):
            return None
    return None


def night(core: Any, cfg: Any = None) -> dict:
    """她的夜窗口：**唯一一份推导**，交给 `clock.night_window()` 算。

    契约 `routine`/`time` 块、门面 `clock()`、注入的夜间行、状态快照的 `in_night`
    都从这里取。以前四处的答案不同：`Clock.is_night`（开关 AND 配置窗口）、
    `soma.in_scheduled_night`（只看日程，没日程就是「不是夜」）、契约 `routine`
    （日程 → 配置兜底）、门面 `clock()`（读落盘契约）。夜猫子人格的日程是 4:00-11:30，
    同一条 payload 里「现在是不是夜里」就能有两个答案（审核 跨插件契约 8）。
    """
    cfg = cfg if cfg is not None else getattr(core, "config", None)
    return _clock.night_window(cfg, _scheduled_night(core))


def _in_night_now(core: Any) -> bool:
    """此刻在不在她的夜里（不知道就按「不是」处理，调用方需要三态请用 `in_night`）。"""
    hour = _local_hour(core)
    if hour is None:
        return False
    try:
        return bool(_clock.in_night_window(hour, night(core)))
    except Exception:
        return False


def in_night(core: Any) -> Optional[bool]:
    """现在是不是她自己的夜里。**三态**：夜间模式关掉时返回 None（不是 False）。

    `clock.is_night` 曾把「夜间模式开关」和「小时在窗口里」两个条件 AND 在一起，
    同一条 payload 里的 `night_window` 却只说窗口——于是「23 点算不算夜」有两个答案
    （审核 模型与导出 7 / 跨插件契约 8）。
    """
    cfg = getattr(core, "config", None)
    if not bool(getattr(cfg, "night_mode_enabled", False)):
        return None
    hour = _local_hour(core)
    if hour is None:
        return None
    return bool(_clock.in_night_window(hour, night(core, cfg)))


def aggression_default(cfg: Any = None) -> float:
    """攻击性的「出厂默认值」，只有一份。

    有配置读配置；配置缺字段/坏值时退回 `DEFAULT_AGGRESSION`（与 dataclass 默认相等）。
    消费方**不要**再写 `profile.get("aggression", 25.0)` 这种自己的默认值。
    """
    if cfg is None:
        return DEFAULT_AGGRESSION
    value = _num(getattr(cfg, "mood_initial_aggression", DEFAULT_AGGRESSION), DEFAULT_AGGRESSION)
    return value


def affection_default(cfg: Any = None) -> float:
    """好感的出厂默认值（与 `config.mood_initial_affection` 相等）。"""
    if cfg is None:
        return 35.0
    return _num(getattr(cfg, "mood_initial_affection", 35.0), 35.0)


def libido_default(cfg: Any = None) -> float:
    """亲近欲的出厂默认值（与 `config.mood_initial_libido` 相等）。"""
    if cfg is None:
        return 34.0
    return _num(getattr(cfg, "mood_initial_libido", 34.0), 34.0)


def _clamp_100(value: Any) -> float:
    """身体轴是 0..100。越界值（结算误差、手改的存档）夹回边界，
    否则最底下的 (0, ...) 档匹配不到，档位词变成空字符串——注入里直接少一句。"""
    return max(0.0, min(100.0, _num(value, 0.0)))


def energy_word(value: Any, seed: Optional[list] = None) -> str:
    """精力档位词：走 `services.energy.ENERGY_WORDS` 那一张梯子，不另写一份。"""
    try:
        from .services.energy import describe_energy

        return describe_energy(_clamp_100(value), seed)
    except Exception:
        return ""


def hunger_word(value: Any, seed: Optional[list] = None) -> str:
    """饥饿档位词：走 `wording.FEELING_WORDS["hunger"]`，不另写一份。"""
    try:
        from . import wording

        ladder = wording.FEELING_WORDS["hunger"]
        clamped = _clamp_100(value)
        word = wording.scale_word(clamped, ladder)
        return wording.pick("hunger", list(seed or []) + [wording.band_index(clamped, ladder)], word)
    except Exception:
        return ""


@dataclass(frozen=True)
class HerState:
    """一轮请求里她的事实快照。消费方按需取字段，不要各自再去问一遍各服务。"""

    now: float
    user_id: str
    relation: Relation
    sleep: Sleep
    night: Optional[bool]


def build(core: Any, user_id: str, is_group: bool = False, now_epoch: Optional[float] = None) -> HerState:
    """本轮快照。纯读：不推进身体、不建档、不落盘。"""
    now = _num(now_epoch if now_epoch is not None else core.now_epoch(), 0.0)
    return HerState(
        now=now,
        user_id=str(user_id or ""),
        relation=relation(core, user_id, is_group, now),
        sleep=sleep_of(core, now),
        night=in_night(core),
    )
