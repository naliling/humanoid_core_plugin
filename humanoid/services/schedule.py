"""日程服务 - 使用 RoleScope 版本。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from ..clock import Clock
from ..config import HumanoidConfig
from ..data.schedule_templates import get_fallback_schedule
from ..jsonx import extract_json_array
from ..llm import LLMGateway, LLMResult, ProviderResolver
from ..persona import EMPTY as EMPTY_PERSONA, PERSONA_PROMPT_MAX, Persona, truncate
from ..role_scope import RoleScope
from ..slots import (
    DAY_MINUTES,
    Slot,
    coverage_is_complete,
    find_slot,
    format_time,
    is_sleep_event,
    normalize_slots,
    parse_time,
    sleep_window_minutes,
)

PURPOSE = "日程生成"
SOURCE_TEMPLATE = "template"
SOURCE_LLM = "llm"
MIN_RETRY_BACKOFF_SECONDS = 60.0


SLEEP_EVENT = "睡眠"
WAKE_SIDE_EVENT = "赖床与洗漱"     # 起床点后、日程还写着睡的那截零头
BEDSIDE_EVENT = "夜间洗漱"         # 入睡点前、日程已写着睡的那截零头


def routine_prompt(cfg: HumanoidConfig, first_index: int = 10) -> str:
    """作息那一条怎么写进日程 prompt。

    v2.16.3 之前这里无条件写「她的作息必须遵守：23:00 上床，生物钟夜到 6:00 结束」——
    那等于一个写死的窗口替所有人决定几点睡，夜猫子人格根本排不出来。现在默认让模型从
    人设里读她几点睡；只有用户显式打开「日程贴合夜间窗口」时，才把它当成硬约束递进去。
    """
    if not cfg.night_mode_enabled or not cfg.schedule_follow_night_window:
        return (
            f"{first_index}. 几点睡、几点起由她是谁决定：从人设里读她的年纪感、工作或学业、"
            "生活习惯。夜猫子就排凌晨睡晚起，早起的就排晚上九点困。"
            "别把所有人都排成 00:00→08:00，也别因为「正常人应该这样」就改掉人设里写着的作息。\n"
        )
    start, end = cfg.night_start_hour, cfg.night_end_hour
    if start == end:
        return ""
    span = cfg.night_span_hours
    lines = [
        f"{first_index}. 用户把她的作息锁定了：{start:02d}:00 上床，生物钟夜到 {end:02d}:00 结束。",
        f"   睡眠排成首尾相接的两段：{start:02d}:00→24:00 与 00:00→{end:02d}:00，"
        "这两段的 event 里要带「睡眠」二字；",
        f"   {end:02d}:00 往后从起床、洗漱开始排。",
    ]
    if cfg.sleep_need_hours > span + 0.5:
        lines.append(
            f"   她一晚需要睡 {cfg.sleep_need_hours:g} 小时，比这个窗口更长，"
            "所以早上会赖床、不太起得来。"
        )
    return "\n".join(lines) + "\n"


def _intersect(a: tuple[int, int], b: tuple[int, int]) -> tuple[int, int] | None:
    lo, hi = max(a[0], b[0]), min(a[1], b[1])
    return (lo, hi) if hi > lo else None


def _subtract(seg: tuple[int, int], spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    parts = [seg]
    for span in spans:
        nxt: list[tuple[int, int]] = []
        for part in parts:
            overlap = _intersect(part, span)
            if overlap is None:
                nxt.append(part)
                continue
            if part[0] < overlap[0]:
                nxt.append((part[0], overlap[0]))
            if overlap[1] < part[1]:
                nxt.append((overlap[1], part[1]))
        parts = nxt
    return parts


def align_sleep_to_night(slots: list[Slot], cfg: HumanoidConfig) -> list[Slot]:
    """把一份日程里的睡眠区间挪到夜间窗口上。

    默认不再跑这一步（`schedule_follow_night_window` 默认关）：作息该由人设决定，身体
    反过来跟着她的日程走。留着这一手是因为确实有人希望「她的夜就是 23:00→06:00」，
    开了就是显式覆盖：模型不听 prompt 也能对上。
    """
    if not cfg.night_mode_enabled or not cfg.schedule_follow_night_window:
        return slots
    start, end = cfg.night_start_hour * 60, cfg.night_end_hour * 60
    if start == end:
        return slots
    spans = [(start, DAY_MINUTES), (0, end)] if start > end else [(start, end)]

    pieces: list[Slot] = []
    for slot in slots:
        lo = parse_time(slot.get("start"))
        hi = parse_time(slot.get("end"))
        if lo is None or hi is None or hi <= lo:
            pieces.append(slot)
            continue
        was_sleep = is_sleep_event(slot.get("event"))
        covered = [span for span in spans if _intersect((lo, hi), span) is not None]
        if not covered:
            # 完全落在夜间窗口外：原样保留。午休就是午休，不该被改名。
            pieces.append(dict(slot))
            continue
        rest = _subtract((lo, hi), covered)
        if not rest:
            # 整段都在窗口内，没有被切开，事件名也不用动。
            pieces.append(_sleep_piece((lo, hi), slot, was_sleep))
            continue
        for span in covered:
            overlap = _intersect((lo, hi), span)
            if overlap is not None:
                pieces.append(_sleep_piece(overlap, slot, was_sleep))
        for part in rest:
            pieces.append(_awake_piece(part, slot, was_sleep, start, end))

    aligned = normalize_slots(
        pieces,
        max_slots=max(8, cfg.schedule_max_slots),
        align_minutes=cfg.granularity_minutes,
    )
    return aligned or slots


def _sleep_piece(seg: tuple[int, int], slot: Slot, was_sleep: bool) -> Slot:
    piece = dict(slot)
    piece["start"], piece["end"] = format_time(seg[0]), format_time(seg[1])
    if not was_sleep:
        # 本来不是睡眠的时段落进了夜间窗口：改成真睡，否则身体不会把它当觉。
        piece["event"] = SLEEP_EVENT
        piece["location"] = "卧室"
        piece["emotion"] = "沉睡"
        piece["energy_rate"] = 0.15
    return piece


def _awake_piece(
    seg: tuple[int, int],
    slot: Slot,
    was_sleep: bool,
    night_start: int,
    night_end: int,
) -> Slot:
    piece = dict(slot)
    piece["start"], piece["end"] = format_time(seg[0]), format_time(seg[1])
    if was_sleep:
        # 从睡眠里切出来的零头：名字里不能再带「睡」，否则身体会把它当成还在睡。
        # 紧贴起床点之后的是赖床，紧贴入睡点之前的是睡前洗漱，两者不是一回事。
        if seg[0] == night_end:
            piece["event"] = WAKE_SIDE_EVENT
            piece["emotion"] = "慢慢清醒"
        elif seg[1] == night_start:
            piece["event"] = BEDSIDE_EVENT
            piece["emotion"] = "困倦"
        else:
            piece["event"] = WAKE_SIDE_EVENT
            piece["emotion"] = "慢慢清醒"
    return piece


def sleep_spans(slots: list[Slot]) -> list[Slot]:
    """日程里算「她在睡」的时段（与身体层同一套判定）。"""
    return [slot for slot in slots if is_sleep_event(slot.get("event"))]


def schedule_wake_minute(slots: list[Slot]) -> int | None:
    """从日程里读出她的起床时间：最长那段觉的结束点。

    以前只认「从 00:00 开始的那段」，于是夜猫子（04:00→11:30）根本没有起床时间，
    诊断与契约里那一格永远空着。现在与身体层同一个窗口来源。"""
    window = sleep_window_minutes(slots)
    return window[1] if window else None


def schedule_wake_text(slots: list[Slot]) -> str:
    minute = schedule_wake_minute(slots)
    return format_time(minute) if minute is not None else ""


# 「今天到这会她做过什么」的时间词。日程里存的是分钟，这里只负责把它说成人话。
_PERIODS: tuple[tuple[int, int, str], ...] = (
    (5, 8, "清晨"),
    (8, 11, "上午"),
    (11, 13, "中午"),
    (13, 17, "下午"),
    (17, 19, "傍晚"),
    (19, 22, "晚上"),
    (22, 24, "夜里"),
    (0, 5, "凌晨"),
)

# 刚做完的事只算这个时长以内：早上八点吃过饭，晚上八点再说「刚吃完饭」就穿帮了。
DAY_RECENT_PAST_MINUTES = 210
# 接下来要做的事只算这个时长以内：太远的安排不算「等下」。
DAY_UPCOMING_MINUTES = 210


def period_of(minutes: int) -> str:
    hour = (minutes // 60) % 24
    for start, end, name in _PERIODS:
        if start <= hour < end:
            return name
    return "白天"


def _event_phrase(event: str) -> str:
    """把时段名削成能放进一句口语里的动作。"""
    text = str(event or "").strip()
    for affix in ("安排", "活动", "时间", "（", "("):
        cut = text.find(affix)
        if cut > 1:
            text = text[:cut]
    return text.strip(" -·、，。/~")[:16]


def day_phrases(
    slots: list[Slot],
    now_minutes: int,
    *,
    past_limit: int = 2,
    future_limit: int = 1,
) -> dict[str, Any]:
    """把日程拆成她能直接说出口的三段：刚做过什么 / 正在做什么 / 接下来做什么。

    这是「说得出今天干了什么」的数据源。拿的不是当前时段那一个孤零零的标签，而是
    一条时线：模型可以说「上午一直在弄那个报表，刚坐下，等下还得去趟银行」，
    而不是只能说「我在休息」。
    """
    doing = ""
    done: list[str] = []
    upcoming: list[str] = []
    ordered: list[tuple[int, int, str]] = []
    for slot in slots:
        lo = parse_time(slot.get("start"))
        hi = parse_time(slot.get("end"))
        if lo is None or hi is None or hi <= lo:
            continue
        ordered.append((lo, hi, str(slot.get("event") or "").strip()))

    for lo, hi, event in ordered:
        if lo <= now_minutes < hi:
            phrase = _event_phrase(event)
            if phrase:
                doing = "睡觉" if is_sleep_event(event) else phrase
            continue
        if hi <= now_minutes:
            if now_minutes - hi > DAY_RECENT_PAST_MINUTES:
                continue
            if is_sleep_event(event):
                # 睡眠不报成「刚在睡觉」：那是刚醒的人说的话，醒三小时就不算了。
                if now_minutes - hi <= 90:
                    done.insert(0, "才醒")
                continue
            phrase = _event_phrase(event)
            if phrase:
                done.insert(0, f"{period_of(lo)}在{phrase}")
            continue
        if lo - now_minutes <= DAY_UPCOMING_MINUTES:
            phrase = _event_phrase(event)
            if phrase:
                # 说「夜里要睡了」而不是「夜里就该睡了」：后者听着像谁在管她。
                upcoming.append(
                    f"{period_of(lo)}要睡了" if is_sleep_event(event)
                    else f"{period_of(lo)}还要{phrase}"
                )

    return {
        "doing": doing,
        "done": done[:max(0, int(past_limit))],
        "next": upcoming[:max(0, int(future_limit))],
    }


def done_between(
    slots: list[Slot], start_minutes: int, end_minutes: int, limit: int = 2
) -> list[str]:
    """落在 [start, end) 之间结束掉的时段，说成「上午在改海报」这种能接进句子里的话。

    【刚刚】那块要的是「TA不在的这段时间她自己在干什么」，而【今天】要的是「今天到这会
    过了什么」——同一份日程的两个切法。两处都直接取 done 会撞出完全一样的句子，所以这里
    按时间窗取，调用方再把这些从【今天】里挑掉。
    """
    out: list[str] = []
    for slot in slots or []:
        lo = parse_time(slot.get("start"))
        hi = parse_time(slot.get("end"))
        if lo is None or hi is None or hi <= lo:
            continue
        if not (start_minutes <= hi <= end_minutes):
            continue
        event = str(slot.get("event") or "").strip()
        if not event or is_sleep_event(event):
            continue
        phrase = _event_phrase(event)
        if phrase:
            out.append(f"{period_of(lo)}在{phrase}")
    return out[: max(0, int(limit))]


def day_lines(
    phrases: dict[str, Any],
    max_items: int = 2,
    include_doing: bool = True,
) -> list[str]:
    """把 day_phrases 的结果拼成一句第一人称的话。没内容时返回空列表（不注入）。

    `include_doing=False` 用于「手上在做的」已经由过程系统说过一次的场合：同一份注入里
    两处各说一遍此刻在干什么，模型会以为那是两件事。
    """
    doing = str(phrases.get("doing") or "")
    # day_phrases 给的是「最近做过的排在最前」；说出来要按时间顺序，
    # 「上午在改海报、中午在吃饭」才像回忆，反过来像在做清单核对。
    done = [x for x in (phrases.get("done") or []) if x]
    done = list(reversed(done[-max_items:])) if max_items else []
    upcoming = [x for x in (phrases.get("next") or []) if x][:1]
    bits: list[str] = []
    if done:
        bits.append("、".join(done))
    if include_doing and doing:
        bits.append(f"现在在{doing}")
    elif include_doing and done:
        bits.append("现在空着")
    if upcoming:
        bits.append(upcoming[0])
    if not bits:
        return []
    head = "今天到这会：" if include_doing else "今天到这会做过："
    return [head + "；".join(bits)]


def persona_block(persona: Persona | None) -> str:
    """把 AstrBot 人格设定写成日程 prompt 的开头：她是谁，先立住再排一天。

    没接到人设时给一段中性身份，而不是回退到插件里那个十二选一的标签——标签会把她
    压成「温柔体贴」这种形容词，排出来的日程没有职业、没有住处、没有来往的人。

    长度在这里再卡一道：`resolve_persona` 已经截过，但日程生成不该信任调用方递进来的
    东西——有人把 AstrBot 人设写成 5000 字时，不卡这一道会把一个每天要跑的请求撑到
    七千 token。
    """
    if persona is not None and persona.usable:
        prompt = truncate(persona.prompt, PERSONA_PROMPT_MAX)
        return (
            f"她是「{persona.name}」。下面是她在 AstrBot 里的人格设定，"
            "这就是她本人，不是她在扮谁：\n"
            f"{prompt}\n\n"
            "先从这里读出她的身份、年纪感、职业或在读状态、住在哪里、独居还是跟人住、"
            "平时跟谁来往、喜欢什么讨厌什么、花钱和精力的习惯。\n"
            "设定里没写的，按最合理、最省心的方式补一个出来，然后每天都沿用同一个答案："
            "这份日程每天都会重排，但同一天里她不能今天在读大学、明天在上班。\n"
        )
    return (
        "她是一个有自己生活的普通人，没有现成的人设资料可读。\n"
        "按一份普通上班族/学生的真实日子补：有自己的住处、有自己的事要做、"
        "有自己的开销和疲惫。补出来的身份每天保持一致。\n"
    )


def build_prompt(
    cfg: HumanoidConfig,
    today: str,
    weekday: str,
    persona: Persona | None = None,
) -> str:
    max_slots = cfg.schedule_max_slots
    min_slots = max(4, min(max_slots, max_slots // 2))
    step = cfg.granularity_minutes
    if step > 1:
        align_hint = f"所有 start / end 必须对齐到 {step} 分钟的整数倍。"
    else:
        align_hint = "时间点可以自然决定，不必对齐。"

    extra = cfg.schedule_prompt_extra.strip()
    extra_line = f"额外的偏好：{extra}\n" if extra else ""

    return (
        persona_block(persona)
        + f"\n请为她规划 {today}（星期{weekday}）这一整天的生活日程。\n"
        + f"{extra_line}"
        + "\n排日程的要求：\n"
        "1. 写她真的会去做的一件件事，不要写「工作」「休息」这种笼统标签："
        "「改第三季度的报表」比「上班」有用，「楼下便利店买饭团」比「吃饭」有用。\n"
        "2. 时段要能让她说得出自己此刻在干什么，location 与 emotion 跟着事件走，"
        "不要一整天都填同一个值。\n"
        "3. 一天里要有真的会打断聊天的事（开会、通勤、排队、跟人吃饭），"
        "也要有闲下来的空档；不要排成每分钟都在做有意义的事。\n"
        "4. 只输出一个 JSON 数组，不要 Markdown 代码块。\n"
        "5. 每个元素："
        "{\"start\": \"00:00\", \"end\": \"07:30\", \"event\": \"睡眠休息\", "
        "\"location\": \"卧室\", \"emotion\": \"平静\", \"energy_rate\": 0.15}\n"
        f"6. 总共输出 {min_slots}~{max_slots} 个时段，把连续同类活动合并。\n"
        "7. 时段必须首尾相连：00:00 开始，24:00 结束，不重叠。\n"
        f"8. {align_hint}\n"
        "9. energy_rate：睡眠/休息为正（0.05~0.2），工作/外出/社交为负（-0.05~-0.15）。\n"
        + routine_prompt(cfg, 10)
    )


class ScheduleService:
    def __init__(
        self,
        scope: RoleScope,
        config_provider: Callable[[], HumanoidConfig],
        clock: Clock,
        spawn_fn=None,
        logger=None,
        monotonic: Callable[[], float] | None = None,
        persona_provider=None,
    ):
        self._scope = scope
        self._config = config_provider
        self._clock = clock
        self._log = logger
        self._spawn = spawn_fn
        self._generating = False
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self.last_error = ""
        self._retry_after = 0.0
        # 失败退避的计时源：必须可注入，否则测试无法验证「退避窗口内不再投递」。
        self._monotonic = monotonic or time.monotonic
        # 跨天时先保留上一份有效日程，等待新日程成功生成；绝不把持久化的 LLM 日程覆盖成模板。
        self._pending_date = ""
        self._pending_slots: list[Slot] | None = None
        # 日程得按她是谁来排：递一个 async () -> Persona 进来，不递就不读人设。
        self._persona = persona_provider
        self.last_persona = ""

        self.resolver = None
        self.gateway = None
        # 新日程装上后的一次性回调：身体需要知道「睡眠区间换了」，不能把它当成真的醒了。
        self.on_install = None

    def set_resolver_gateway(self, resolver, gateway):
        self.resolver = resolver
        self.gateway = gateway

    @property
    def config(self) -> HumanoidConfig:
        return self._config()

    def current_slots(self) -> list[Slot]:
        """返回今天可用的日程。

        重要：跨天/升级时不能把上一份已生成日程直接覆盖成内置模板。
        模板只作为“等待今天 LLM 日程生成”的临时兜底，不写入持久状态。
        """
        today = self._clock.today_str()
        data = self._scope.self_state
        slots = data.get("daily_schedule")
        stored_date = str(data.get("today_date") or "")

        if stored_date == today and isinstance(slots, list) and slots:
            self._pending_date = ""
            self._pending_slots = None
            return slots

        if self._pending_date != today or not self._pending_slots:
            self._pending_date = today
            self._pending_slots = self._template_slots(today)

        return self._pending_slots

    def current_slot(self, minutes: int | None = None) -> Slot:
        if minutes is None:
            now = self._clock.now()
            minutes = now.hour * 60 + now.minute
        return find_slot(self.current_slots(), minutes)

    @property
    def source(self) -> str:
        return str(self._scope.get_self("schedule_source", SOURCE_TEMPLATE))

    @property
    def source_text(self) -> str:
        return "大模型生成" if self.source == SOURCE_LLM else "内置模板"

    @property
    def generating(self) -> bool:
        return self._generating

    @property
    def retry_after(self) -> float:
        return max(0.0, self._retry_after - self._monotonic())

    def _template_slots(self, today: str) -> list[Slot]:
        cfg = self.config
        raw = get_fallback_schedule(today)
        base = normalize_slots(raw, max_slots=max(8, cfg.schedule_max_slots)) or raw
        return align_sleep_to_night(base, cfg)

    def _install(self, slots: list[Slot], today: str, source: str) -> list[Slot]:
        # 模型不一定听约束（也常见它把睡眠写成 00:00–08:00），装上去之前再按夜间窗口切一次。
        slots = align_sleep_to_night(slots, self.config)
        self._scope.update_self(
            today_date=today,
            daily_schedule=slots,
            schedule_source=source,
            schedule_generated_at=self._clock.now().strftime("%Y-%m-%d %H:%M:%S"),
            schedule_persona=self.last_persona,
        )
        if self.on_install is not None:
            try:
                self.on_install()
            except Exception:
                pass
        if self._log and self.config.debug_mode:
            self._log.debug(f"[humanoid_core] 日程写入存储: source={source}, slots={len(slots)}")
        return slots

    def _today_changed(self, today: str) -> bool:
        """今天是否还没有一份属于今天的日程，且今天还没试过生成。

        不能只看 `today_date`：它只在生成成功时写入。若只看它，模型持续不可用（配错
        provider、超时）时 `today_date` 永远不是今天，于是每个后台周期都被当成「跨天」
        而绕过退避与冷却，变成每 30 秒一次永久重试。因此「今天试过但没有结果」不算跨天。
        """
        if str(self._scope.get_self("today_date", "") or "") == today:
            return False
        return str(self._scope.get_self("schedule_attempt_date", "") or "") != today

    def request_refresh(self, force: bool = False, ignore_cooldown: bool = False) -> bool:
        if self._task and not self._task.done():
            return False
        cfg = self.config
        if not cfg.use_llm_schedule:
            return False

        today = self._clock.today_str()
        date_changed = self._today_changed(today)
        # 新的一天必须尝试生成，即使上一天的 provider 失败冷却还没结束。
        effective_force = bool(force or date_changed)
        effective_ignore = bool(ignore_cooldown or date_changed)

        if not effective_force and self._scope.get_self("schedule_source") == SOURCE_LLM:
            return False
        if not effective_force and self.retry_after > 0:
            return False

        coro = self.ensure_fresh(force=effective_force, ignore_cooldown=effective_ignore)
        name = f"humanoid-schedule-refresh-{self._scope.role_id}"
        if self._spawn:
            self._task = self._spawn(coro, name)
        else:
            self._task = asyncio.create_task(coro, name=name)
        return True

    async def ensure_fresh(self, force: bool = False, ignore_cooldown: bool = False) -> bool:
        cfg = self.config
        if not cfg.use_llm_schedule:
            return False

        today = self._clock.today_str()
        if self._today_changed(today):
            force = True
            ignore_cooldown = True
        # 先把「今天试过」记下来：失败时 today_date 不会变，不记这一笔的话下一次调用
        # 仍然被当成跨天，退避窗口形同不存在。
        if str(self._scope.get_self("schedule_attempt_date", "") or "") != today:
            self._scope.set_self("schedule_attempt_date", today)

        if (
            not force
            and str(self._scope.get_self("today_date", "") or "") == today
            and self._scope.get_self("schedule_source") == SOURCE_LLM
        ):
            return False
        if not force and self.retry_after > 0:
            return False

        # 只在需要时创建临时模板，不落盘。
        self.current_slots()

        async with self._lock:
            stored_date = str(self._scope.get_self("today_date", "") or "")
            if not force and stored_date == today and self._scope.get_self("schedule_source") == SOURCE_LLM:
                return False
            return await self._generate(cfg, today, ignore_cooldown)

    async def _generate(self, cfg: HumanoidConfig, today: str, ignore_cooldown: bool) -> bool:
        self._generating = True
        try:
            ok = await self._generate_inner(cfg, today, ignore_cooldown)
        finally:
            self._generating = False
        if ok:
            self._retry_after = 0.0
        else:
            backoff = max(MIN_RETRY_BACKOFF_SECONDS, float(cfg.schedule_provider_cooldown_minutes) * 60)
            self._retry_after = self._monotonic() + backoff
        return ok

    async def _generate_inner(self, cfg: HumanoidConfig, today: str, ignore_cooldown: bool) -> bool:
        if self.gateway is None:
            self.last_error = "LLM Gateway 未初始化"
            return False

        persona = await self._resolve_persona()
        prompt = build_prompt(cfg, today, self._clock.weekday(), persona)
        if self._log and cfg.debug_mode:
            self._log.debug(
                f"[humanoid_core] 日程生成提示词（人设："
                f"{persona.label if persona else '未读'}）:\n{prompt}"
            )

        result: LLMResult = await self.gateway.generate(
            prompt=prompt,
            chain=cfg.schedule_provider_ids,
            allow_global=cfg.schedule_allow_global_fallback,
            timeout=float(cfg.schedule_llm_timeout_seconds),
            attempts_per_provider=cfg.schedule_generation_max_attempts,
            retry_interval=float(cfg.schedule_retry_interval_seconds),
            purpose=PURPOSE,
            ignore_cooldown=ignore_cooldown,
        )
        if not result.ok:
            self.last_error = result.summary()
            if self._log and cfg.debug_mode:
                self._log.debug(f"[humanoid_core] 日程生成失败: {self.last_error}")
            return False

        parsed = extract_json_array(result.text)
        slots = (
            normalize_slots(parsed, max_slots=cfg.schedule_max_slots, align_minutes=cfg.granularity_minutes)
            if parsed is not None
            else None
        )
        if not slots or not coverage_is_complete(slots):
            self.last_error = f"无法解析日程：{result.text[:160]}"
            if self._log and cfg.debug_mode:
                self._log.debug(f"[humanoid_core] 日程解析失败: {self.last_error}")
            return False

        self._install(slots, today, SOURCE_LLM)
        self._pending_date = ""
        self._pending_slots = None
        self.last_error = ""
        if self._log:
            self._log.info(f"[humanoid_core] 日程生成成功，共 {len(slots)} 个时段")
            if cfg.debug_mode:
                self._log.debug(f"[humanoid_core] 新日程: {slots}")
        return True

    async def _resolve_persona(self) -> Persona | None:
        """取该角色当前生效的 AstrBot 人格。没接上人格源或显式关掉时返回 None。"""
        if self._persona is None or not self.config.schedule_use_persona:
            return None
        try:
            persona = await self._persona()
        except Exception as exc:
            if self._log:
                self._log.warning(f"[humanoid_core] 日程生成读人设失败，按无设 persona 排: {exc}")
            return None
        self.last_persona = persona.label if persona and persona.usable else ""
        return persona

    def status(self) -> dict:
        today = self._clock.today_str()
        pending = str(self._scope.get_self("today_date", "") or "") != today
        source = self.source
        source_text = self.source_text
        if pending and self.config.use_llm_schedule:
            source_text = "等待大模型生成（临时模板仅作过渡）"
        return {
            "date": today,
            "stored_date": self._scope.get_self("today_date", ""),
            "slots": len(self.current_slots()),
            "sleep_spans": [
                f"{s.get('start')}-{s.get('end')} {s.get('event')}" for s in sleep_spans(self.current_slots())
            ],
            "wake_at": schedule_wake_text(self.current_slots()),
            "source": source,
            "source_text": source_text,
            "persona": str(self._scope.get_self("schedule_persona", "") or "") or self.last_persona,
            "generated_at": self._scope.get_self("schedule_generated_at", ""),
            "last_error": self.last_error,
            "generating": self.generating,
            "retry_after": self.retry_after,
            "pending_today": pending,
        }

    async def aclose(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass