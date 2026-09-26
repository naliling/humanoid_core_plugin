"""日程服务 - 滚动分段：一次只决定「从现在起的这一段」。

与旧版整表日程的区别：未来不预排。她的下一段做什么、做多久，到了时候看着她
当时的身体与人设现决定；每天每 15 分钟一个决策窗，生成或不生成都可能——这一段
过完了、身体跟手上的事明显打架、或掷中了变动概率，才会重新问一次模型，其余
时候她接着做手上的事，一次模型都不调。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable, Mapping
from typing import Any

from ..clock import Clock
from ..config import HumanoidConfig
from ..jsonx import extract_json_array, extract_json_object
from ..llm import OUTCOME_GATED, LLMGateway, LLMResult, ProviderResolver
from ..persona import PERSONA_PROMPT_MAX, Persona, truncate
from ..role_scope import RoleScope
from ..slots import (
    DAY_MINUTES,
    Slot,
    clamp_rate,
    format_time,
    is_meal_event,
    is_sleep_event,
    parse_time,
    sleep_window_minutes,
)
from ..wording import pick

PURPOSE = "日程生成"
SOURCE_TEMPLATE = "template"
SOURCE_LLM = "llm"
MIN_RETRY_BACKOFF_SECONDS = 60.0

SLEEP_EVENT = "睡眠"

# 一段的时长边界：最短一刻钟；清醒的事最长 3 小时；一觉最长 12 小时（跨午夜）。
SEGMENT_MIN_MINUTES = 15
SEGMENT_MAX_MINUTES = 180
SEGMENT_SLEEP_MAX_MINUTES = 720
SEGMENT_DEFAULT_MINUTES = 60
# 一天最多留这么多段（超出丢最早的：三十段之前的事她自己也记不清）。
SEGMENTS_DAY_CAP = 48
# prompt 里带多少段「已经过完的时段」。
SEGMENTS_PROMPT_HISTORY = 8
# 上一段结束到新一段之间隔了这么久以内，才把它顺延补上；隔得更久说明插件停机了，
# 那段时间她做了什么没人知道，不能拿旧事件把窟窿填上。
SEGMENT_GAP_MERGE_MINUTES = 30

# 身体越线：不等这一段结束，当场重新决定（饿到发慌还在做事、困到不行还醒着）。
CHANGE_TRIGGER_HUNGER = 78.0
CHANGE_TRIGGER_SLEEP_PRESSURE = 88.0
# 本地兜底里「夜里该睡了」的困意门槛：比模型路径宽，因为没有模型替她权衡。
LOCAL_NIGHT_SLEEP_PRESSURE = 70.0

# 睡觉跨午夜时，今天这半截之后接到明天的那一截。
CARRY_KEY = "segment_carry"


def routine_prompt(cfg: HumanoidConfig, first_index: int = 10) -> str:
    """作息那一条怎么写进分段 prompt。

    默认让模型从人设里读她几点睡；只有用户显式打开「日程贴合夜间窗口」时，才把
    夜间窗口当成硬约束递进去（分段生成时同样生效：到了点就该排睡眠）。
    """
    if not cfg.night_mode_enabled or not cfg.schedule_follow_night_window:
        return (
            f"{first_index}. 几点睡、几点起由她是谁决定：从人设里读她的年纪感、工作或学业、"
            "生活习惯，结合当前的身体数值（困意、睡眠债）由你判断；"
            "人设里写着的作息保持不动。\n"
        )
    start, end = cfg.night_start_hour, cfg.night_end_hour
    if start == end:
        return ""
    span = cfg.night_span_hours
    lines = [
        f"{first_index}. 用户把她的作息锁定了：{start:02d}:00 上床，生物钟夜到 {end:02d}:00 结束。",
        f"   到了 {start:02d}:00 就该排睡眠（event 里带「睡眠」二字），一觉睡到 {end:02d}:00；"
        f"   {end:02d}:00 之后从起床、洗漱开始。",
    ]
    if cfg.sleep_need_hours > span + 0.5:
        lines.append(
            f"   她一晚需要睡 {cfg.sleep_need_hours:g} 小时，比这个窗口更长，"
            "所以早上会赖床、不太起得来。"
        )
    return "\n".join(lines) + "\n"


def sleep_spans(slots: list[Slot]) -> list[Slot]:
    """日程里算「她在睡」的时段（与身体层同一套判定）。"""
    return [slot for slot in slots if is_sleep_event(slot.get("event"))]


def schedule_wake_minute(slots: list[Slot]) -> int | None:
    """从日程里读出她的起床时间：最长那段觉的结束点。"""
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

    滚动分段日程里没有预排的未来，所以「接下来」通常只有正在做的这一段的自然
    结束；她不会提前宣布等下要去哪——那本来就是排出来之前不知道的事。
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
    """落在 [start, end) 之间结束掉的时段，说成「上午在改海报」这种能接进句子里的话。"""
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
    """把 day_phrases 的结果拼成一句第一人称的话。没内容时返回空列表（不注入）。"""
    doing = str(phrases.get("doing") or "")
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


def just_done(
    slots: list[Slot], now_minutes: int, window_minutes: float = 45.0
) -> str:
    """刚结束的时段的动作名（不带时段词）。窗口内没有就是空串。

    「余韵」用：刚在做什么和上午在什么是两句话，真人带着上一件事的余韵进对话。
    """
    best: tuple[float, str] | None = None
    for slot in slots or []:
        hi = parse_time(slot.get("end"))
        if hi is None:
            continue
        ago = now_minutes - hi
        if ago <= 0 or ago > window_minutes:
            continue
        event = str(slot.get("event") or "").strip()
        if not event or is_sleep_event(event):
            continue
        phrase = _event_phrase(event)
        if phrase and (best is None or ago < best[0]):
            best = (ago, phrase)
    return best[1] if best else ""


def persona_block(persona: Persona | None) -> str:
    """把 AstrBot 人格设定写成分段 prompt 的开头：她是谁，先立住再决定这一段。"""
    if persona is not None and persona.usable:
        prompt = truncate(persona.prompt, PERSONA_PROMPT_MAX)
        return (
            f"她是「{persona.name}」。下面是她在 AstrBot 里的人格设定，"
            "这就是她本人，不是她在扮谁：\n"
            f"{prompt}\n\n"
            "先从这里读出她的身份、年纪感、职业或在读状态、住在哪里、独居还是跟人住、"
            "平时跟谁来往、喜欢什么讨厌什么、花钱和精力的习惯。\n"
            # 原句是「设定里没写的，按最合理、最省心的方式补一个出来，并且每天都沿用同一个答案」——
            # 那是在命令模型给她造一份身份。改成：设定里有的照用，没有的别编——
            # 留白比凭空多一个身份安全，而“今天在做什么”本来就该由她自己说。
            "设定里没提到的，不要替他编：那里留白即可。她的日常由她自己讲，"
            "你只需要把“她现在这个状态大概会处在什么样的处境”排出来，不要替她定身份。\n"
        )
    return (
        "没有现成的人设资料可读。\n"
        # 原句「按一份普通上班族/学生的真实日子补」直接预设了她是上班族还是学生。
        # 退休的人、自由职业者、全职带孩子的角色都会被凭空安一份工位。
        "不要假设她的职业或身份。排一段中性、多数人都成立的日子：有自己的住处、"
        "有事要做、会累。这些怎么安排、她具体在做什么，由她自己说。\n"
    )


def body_reference_block(body: dict[str, Any] | None, now_text: str) -> str:
    """把身体参考数值写进分段 prompt。

    这些是活值：每次决策现从 soma/energy 取，随时间真实变化。措辞上必须说清
    「是参考，不是条件」——数值本身才是模型该拿到的东西，怎么权衡由它决定。
    """
    if not body:
        return ""
    lines = [
        "【她此刻的身体参考数值】（会随时间真实变化；是决策输入，不是必须满足的条件，怎么权衡由你决定）",
        f"- 当前时间：{now_text}",
    ]
    if "energy" in body:
        lines.append(f"- 精力：{body['energy']}/100")
    if "hunger" in body:
        lines.append(f"- 饥饿：{body['hunger']}/100（越高越饿）")
    if "sleep_pressure" in body:
        lines.append(f"- 睡眠压力：{body['sleep_pressure']}/100（越高越困）")
    if "sleep_debt" in body:
        lines.append(f"- 睡眠债：{body['sleep_debt']} 小时（连续缺觉累积，越高越需要补觉）")
    if "last_sleep_hours" in body:
        lines.append(f"- 昨晚睡了：{body['last_sleep_hours']} 小时")
    if body.get("cycle"):
        lines.append(f"- 生理周期：{body['cycle']}")
    return "\n".join(lines) + "\n\n"


def segment_prompt(
    cfg: HumanoidConfig,
    *,
    now_text: str,
    weekday: str,
    persona: Persona | None = None,
    body: dict[str, Any] | None = None,
    prev: Slot | None = None,
    elapsed_minutes: int = 0,
    history: list[Slot] | None = None,
    history_note: str = "",
    is_night: bool = False,
) -> str:
    """只决定「从现在起的这一段」：不排一整天，不提前安排以后的事。

    把「上一段在做什么、做了多久」和「今天已经过完的时段」连同身体读数一起递
    进去，让她自己判断是接着做还是换一件事；生成或不生成本身由调用方按身体与
    概率决定，这里只负责把「要决定」的那一次问对。
    """
    prev_event = str((prev or {}).get("event") or "").strip()
    if prev_event:
        prev_line = (
            f"她这一段从 {(prev or {}).get('start', '')} 开始在做「{prev_event}」"
            f"（{(prev or {}).get('location', '')}），到现在已经 {int(elapsed_minutes)} 分钟。\n"
        )
    else:
        prev_line = "这是今天的第一次决定，手上还没有正在做的事。\n"

    rows = []
    for slot in (history or [])[-SEGMENTS_PROMPT_HISTORY:]:
        if not isinstance(slot, dict):
            continue
        rows.append(
            f"{slot.get('start', '')}-{slot.get('end', '')} "
            f"{slot.get('event', '')}（{slot.get('location', '')}）"
        )
    history_block = ""
    if rows:
        history_block = (
            f"【{history_note or '今天到这会她已经过完的时段'}】\n" + "\n".join(rows) + "\n\n"
        )

    extra = cfg.schedule_prompt_extra.strip()
    extra_block = (
        "【可选偏好】（用户补充，仅供参考，可与她的人设和身体状态权衡，不必逐字执行）\n"
        f"{extra}\n\n"
        if extra
        else ""
    )
    night_line = "现在正处在她的生物钟夜里。\n\n" if is_night else ""

    return (
        persona_block(persona)
        + f"\n现在是 {now_text}（星期{weekday}）。请决定她【从现在开始的这一段】在做什么——"
        "只这一段：之后的事到了时候会再决定，现在不要排。\n\n"
        + prev_line
        + "\n"
        + body_reference_block(body, now_text)
        + history_block
        + extra_block
        + night_line
        + "【怎么决定】\n"
        "1. 身体读数是决策输入：饿到不行就去吃，困到不行就去睡——怎么权衡由你决定。\n"
        "2. 一件事做到一半接着做很正常（continue=true）；做很久了、或身体不允许时就换。\n"
        "\n"
        "【输出格式】\n"
        "1. 只输出一个 JSON 对象，不要 Markdown 代码块，不要解释文字。\n"
        # 原来这里是 {"event": "去超市买菜", "location": "超市"}——一个有画面感的
        # 具体事件。模型对 few-shot 示例的模仿远强于对抽象规则的服从，它会被当成
        # 「这就是合意的答案范式」反复复用。换成占位式，格式照样清楚，但不给画面。
        '2. 形如：{"continue": false, "event": "<她在做的事>", "location": "<在哪>", '
        '"emotion": "<心情>", "energy_rate": -0.05, "minutes": 85}\n'
        "   event 写具体在做什么、location 写她在哪，两者都要具体到能说出口的程度；\n"
        "3. continue：true = 接着做手上这件事（event/location/emotion 留空，速率沿用）；"
        "false = 换一件事。\n"
        f"4. minutes：这一段做多久。最短 {SEGMENT_MIN_MINUTES} 分钟，一般的事最长 "
        f"{SEGMENT_MAX_MINUTES} 分钟；睡觉可以睡一整夜（最长 {SEGMENT_SLEEP_MAX_MINUTES // 60} 小时，"
        "跨过半夜也没关系）。\n"
        "5. energy_rate 是这一段对精力的作用：睡眠/休息为正（0.05~0.2），"
        "工作/外出/社交为负（-0.05~-0.15）。\n"
        "\n"
        + routine_prompt(cfg, 6)
    )


def _pick_from_array(raw: Any, now_minute: int) -> Mapping[str, Any] | None:
    """模型偶尔会包一层数组（旧版整表的习惯）：取覆盖现在或之后的第一格。"""
    if not isinstance(raw, list):
        return None
    upcoming = None
    last = None
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        lo = parse_time(item.get("start"))
        hi = parse_time(item.get("end"))
        if lo is None or hi is None or hi <= lo:
            continue
        last = item
        if lo <= now_minute < hi:
            return item
        if upcoming is None and lo > now_minute:
            upcoming = item
    return upcoming or last


def _segment_minutes(raw: Mapping[str, Any], now_minute: int, sleeping: bool) -> int:
    """这一段做多久：minutes 与 end 两种写法都认，睡觉的 end 可以写在明天。"""
    minutes: int | None = None
    try:
        minutes = int(float(raw.get("minutes")))
    except (TypeError, ValueError):
        minutes = None
    if minutes is None:
        end = parse_time(raw.get("end"))
        if end is not None:
            if end <= now_minute:
                if not sleeping:
                    # 清醒段把结束点写在过去：没法用，按默认时长接上
                    end = now_minute + SEGMENT_DEFAULT_MINUTES
                else:
                    # 睡觉跨午夜：end 写的是明天醒来的时间
                    end += DAY_MINUTES
            minutes = end - now_minute
    if minutes is None:
        minutes = SEGMENT_DEFAULT_MINUTES
    cap = SEGMENT_SLEEP_MAX_MINUTES if sleeping else SEGMENT_MAX_MINUTES
    return max(SEGMENT_MIN_MINUTES, min(cap, minutes))


def parse_segment(
    raw: Any,
    *,
    now_minute: int,
    step: int = 1,
    prev: Slot | None = None,
) -> Slot | None:
    """把模型返回的这一段规范化成一个 slot。

    `continue=true` 时沿用上一件事的名字与地点，只往前延长——这是「她决定再做
    一会儿」，不是插件替她改主意。睡觉可以跨过午夜：今天这半截截到 24:00，
    剩下的写进 `carry_end`，由 `_append_segment` 结转到明天。
    """
    if isinstance(raw, list):
        raw = _pick_from_array(raw, now_minute)
    if not isinstance(raw, Mapping):
        return None
    prev = prev or {}
    want_continue = raw.get("continue")
    if isinstance(want_continue, str):
        want_continue = want_continue.strip().lower() in ("true", "1", "yes", "是", "继续")
    want_continue = bool(want_continue) and bool(str(prev.get("event") or "").strip())

    event = str(raw.get("event") or "").strip()[:24]
    if want_continue:
        event = str(prev.get("event") or "").strip()[:24]
    if not event:
        return None

    location = (
        str(raw.get("location") or "").strip()[:16]
        or str(prev.get("location") or "").strip()[:16]
        or "家中"
    )
    emotion = (
        str(raw.get("emotion") or "").strip()[:12]
        or str(prev.get("emotion") or "").strip()[:12]
        or "平常"
    )

    sleeping = is_sleep_event(event)
    minutes = _segment_minutes(raw, now_minute, sleeping)
    start = segment_start(now_minute, step)
    end = start + minutes
    rate_raw = raw.get("energy_rate", prev.get("energy_rate", 0.0) if want_continue else 0.0)

    slot: Slot = {
        "start": format_time(start),
        "end": format_time(min(DAY_MINUTES, end)),
        "event": event,
        "location": location,
        "emotion": emotion,
        "energy_rate": clamp_rate(rate_raw),
    }
    if end > DAY_MINUTES:
        slot["carry_end"] = format_time(end - DAY_MINUTES)
    return slot


def segment_start(now_minute: int, step: int) -> int:
    """这一段从哪一刻开始：向下对齐到刻度，绝不越过「现在」。

    用四舍五入会把起点推到未来（20:40 对齐成 20:45），那一段就罩不住此刻，
    她明明在做这件事，却说不出口。
    """
    step = max(1, int(step))
    return (max(0, int(now_minute)) // step) * step


def _in_window(minutes: int, start: int, end: int) -> bool:
    """分钟数是否落在 [start, end) 窗口内；start > end 表示跨午夜。"""
    if start == end:
        return False
    if start < end:
        return start <= minutes < end
    return minutes >= start or minutes < end


def dynamic_segment(
    cfg: HumanoidConfig,
    body: dict[str, Any] | None,
    *,
    now_minute: int,
    step: int = 1,
    prev: Slot | None = None,
    seed: list[Any] | None = None,
) -> Slot:
    """没有可用模型时的兜底：**只给状态，不给事件**。

    分支的**选择**确实由身体读数决定（饿了就走饿、累了就走累）——这部分是真的。
    但分支内曾经写着 36 条具体事件（“看书做笔记”“下楼买饭团”“叫了份外卖”）和 11 个
    地点（“工位/书房/教室”），它们会经 day_phrases 变成「她今天上午在看书做笔记」这样
    一句**第一人称事实**，而注入块开头写着“以下是她当前的**真实处境**”——等于把编出来
    的一天当成既成事实交给模型。更麻烦的是地点：工位与教室直接预设了她是上班族还是学生，
    退休的人、自由职业者会凭空多出一张工位。

    现在只给状态：她饿了、在忙、到饭点了。**做什么、看到什么、经历了什么，由她自己说。**
    同文件的 current_slot() 早就写着「拿不到段时给一个中性『自由活动』，不编造她在做什么」，
    那是这个模块自己的标准，这里跟它对齐。

    措辞仍按 (角色, 日期, 钟点) 抽签，同一时刻不会每天给出同一个答案。
    """
    body = body or {}
    prev = prev or {}
    hunger = float(body.get("hunger", 0.0) or 0.0)
    pressure = float(body.get("sleep_pressure", 0.0) or 0.0)
    energy = float(body.get("energy", 60.0) or 60.0)
    discomfort = float(body.get("discomfort", 0.0) or 0.0)
    asleep = float(body.get("asleep", 0.0) or 0.0) >= 1.0

    night_start = int(cfg.night_start_hour) * 60
    night_end = int(cfg.night_end_hour) * 60
    if not cfg.night_mode_enabled or night_start == night_end:
        night_start, night_end = 23 * 60, 7 * 60
    in_night = _in_window(now_minute, night_start, night_end)

    seed_key = list(seed or []) + [now_minute // 60]

    # 身体优先：饿到线就吃，困到线或在夜里就睡，不等哪张表写着。
    if asleep or (in_night and pressure >= LOCAL_NIGHT_SLEEP_PRESSURE):
        if in_night:
            # 睡到生物钟夜结束；跨午夜的部分由结转接到明天
            logical_end = night_end if night_end > now_minute else night_end + DAY_MINUTES
            minutes = max(SEGMENT_MIN_MINUTES, min(SEGMENT_SLEEP_MAX_MINUTES, logical_end - now_minute))
        else:
            minutes = 120
        event, location, emotion, rate = "睡一觉", "卧室", "沉睡", 0.15
    elif hunger >= CHANGE_TRIGGER_HUNGER:
        event = pick("seg_eat", seed_key, ("有点饿了", "想找点吃的", "肚子空着"))
        location, emotion, rate, minutes = "家里", "饿", 0.08, 40
    elif discomfort >= 62:
        event = pick("seg_rest", seed_key, ("有点不舒服", "想缓一缓", "不太自在"))
        location, emotion, rate, minutes = "家里", "不太舒服", 0.05, 45
    elif in_night:
        # 夜里还醒着、困意又没到线：低刺激地待着，等困意上来
        event = pick("seg_night_idle", seed_key, ("还没什么困意", "安静待着", "夜里醒着"))
        location, emotion, rate, minutes = "家里", "迷糊", 0.02, 45
    elif energy <= 30:
        event = pick("seg_low", seed_key, ("有点提不起劲", "想歇一下", "没什么精神"))
        location, emotion, rate, minutes = "家里", "有点累", 0.05, 30
    else:
        # 按钟点给**处境**而不是事件：她这个钟点在忙 / 到饭点了 / 闲下来了，
        # 但具体在做什么不编——那得她自己说
        hour = (now_minute // 60) % 24
        if 7 <= hour < 9:
            event = pick("seg_morning", seed_key, ("刚起来没多久", "还在醒神"))
            emotion, rate, minutes = "清醒中", -0.05, 60
        elif 9 <= hour < 12:
            event = pick("seg_forenoon", seed_key, ("在忙自己的事", "手头有东西要处理"))
            emotion, rate, minutes = "专注", -0.07, 90
        elif 12 <= hour < 14:
            event = pick("seg_noon", seed_key, ("到饭点了", "中午"))
            emotion, rate, minutes = "放松", 0.08, 45
        elif 14 <= hour < 18:
            event = pick("seg_afternoon", seed_key, ("还在忙", "下午这段在做事"))
            emotion, rate, minutes = "平稳", -0.06, 90
        elif 18 <= hour < 20:
            event = pick("seg_evening_meal", seed_key, ("到饭点了", "天快黑了"))
            emotion, rate, minutes = "惬意", 0.06, 60
        else:
            event = pick("seg_evening", seed_key, ("今天差不多到这儿了", "闲下来了"))
            emotion, rate, minutes = "轻松", -0.02, 75
        # 地点不再按“工位/书房/教室”分类：那是给她安身份（上班还是上学）。
        # 统一给中性的“家里”，与 current_slot() 的兼底一致
        location = "家里"

    start = segment_start(now_minute, step)
    end = start + minutes
    slot: Slot = {
        "start": format_time(start),
        "end": format_time(min(DAY_MINUTES, end)),
        "event": event,
        "location": location,
        "emotion": emotion,
        "energy_rate": clamp_rate(rate),
    }
    if end > DAY_MINUTES:
        slot["carry_end"] = format_time(end - DAY_MINUTES)
    return slot


class ScheduleService:
    """滚动分段日程：未来不预排，每 15 分钟一个「生成或不生成」的决策窗。

    - 到期（这一段过完了 / 今天还没有段）必须生成；
    - 身体跟手上的事明显打架（饿到发慌还在做事、困到不行还醒着）当场生成；
    - 其余时候按 `schedule_change_chance` 的概率掷骰子：掷中才生成（她临时改
      主意），没掷中就接着做手上的事，一次模型都不调；
    - 睡着的那一段不重掷：一觉睡到这段结束为止，夜里不会被打扰；跨午夜的
      一觉由结转（carry）原样接到明天，零点不需要再问一次模型。
    """

    def __init__(
        self,
        scope: RoleScope,
        config_provider: Callable[[], HumanoidConfig],
        clock: Clock,
        spawn_fn=None,
        logger=None,
        monotonic: Callable[[], float] | None = None,
        persona_provider=None,
        body_provider=None,
        random_source: Callable[[], float] | None = None,
    ) -> None:
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
        # 日程得按她是谁来排：递一个 async () -> Persona 进来，不递就不读人设。
        self._persona = persona_provider
        # 身体参考数值的来源：递一个 () -> dict 进来，每次决策现取活值。
        self._body = body_provider
        self.last_persona = ""
        # 概率重估的骰子：每个决策窗只掷一次，掷完的结果本窗内保持不变。
        self._random = random_source or random.random
        self._roll_window = -1
        self._roll_hit = False

        self.resolver = None
        self.gateway = None
        # 新的一段装上后的一次性回调 (previous, current)：身体与过程要跟着换。
        self.on_install = None

    def set_resolver_gateway(self, resolver, gateway):
        self.resolver = resolver
        self.gateway = gateway

    @property
    def config(self) -> HumanoidConfig:
        return self._config()

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def _stored_slots(self) -> list[Slot]:
        stored = self._scope.get_self("daily_schedule")
        if not isinstance(stored, list):
            return []
        return [slot for slot in stored if isinstance(slot, dict)]

    def segments(self) -> list[Slot]:
        """今天她已经过出来的段（含当前段），按时间升序；未来不在里面。

        旧版整表日程存的是 00:00→24:00 的预制表：读的时候把还没到的时段丢掉，
        剩下的就是她今天真实过出来的段——升级当天无缝切到分段模式。
        """
        today = self._clock.today_str()
        self._migrate_new_day(today)
        if str(self._scope.get_self("today_date", "") or "") != today:
            return []
        now = self._clock.now()
        minutes = now.hour * 60 + now.minute
        out: list[Slot] = []
        for slot in self._stored_slots():
            lo = parse_time(slot.get("start"))
            hi = parse_time(slot.get("end"))
            if lo is None or hi is None or hi <= lo:
                continue
            if lo > minutes:
                continue
            out.append(slot)
        return out

    def _carry_slot(self) -> Slot | None:
        """跨夜结转的那一截（今天 00:00 起、明天醒来的那段觉）。"""
        if str(self._scope.get_self("today_date", "") or "") != self._clock.today_str():
            return None
        carry = self._scope.get_self(CARRY_KEY)
        if not isinstance(carry, dict) or not carry:
            return None
        end = parse_time(carry.get("end"))
        if end is None or end <= 0:
            return None
        return {
            "start": "00:00",
            "end": format_time(end),
            "event": str(carry.get("event") or SLEEP_EVENT),
            "location": str(carry.get("location") or "卧室"),
            "emotion": str(carry.get("emotion") or "沉睡"),
            "energy_rate": clamp_rate(carry.get("energy_rate", 0.15)),
        }

    def current_slots(self) -> list[Slot]:
        """兼容旧接口：身体、精力、契约都从这里拿她的段。

        跨夜结转的那一截也并进来：睡眠窗口、起床点要按完整的一觉算，不然
        半夜之后身体会把「23:00→24:00 + 00:00→07:00」拆成两觉分别记账。
        """
        segs = self.segments()
        carry = self._carry_slot()
        if carry is not None:
            segs = segs + [carry]
        return segs

    def _covering(self, segs: list[Slot], minutes: int) -> Slot | None:
        for slot in segs:
            lo = parse_time(slot.get("start"))
            hi = parse_time(slot.get("end"))
            if lo is None or hi is None:
                continue
            if lo <= minutes < hi:
                return slot
        return None

    def active_segment(self, minutes: int | None = None) -> Slot | None:
        """覆盖「现在」这一段。没决定过就是 None——她此刻在做的事还没被问出来。"""
        if minutes is None:
            now = self._clock.now()
            minutes = now.hour * 60 + now.minute
        return self._covering(self.segments(), minutes)

    def last_segment(self) -> Slot | None:
        segs = self.segments()
        return segs[-1] if segs else None

    def current_slot(self, minutes: int | None = None) -> Slot:
        """旧接口：拿不到段时给一个中性「自由活动」，不编造她在做什么。"""
        slot = self.active_segment(minutes)
        if slot is not None:
            return slot
        now = self._clock.now()
        stamp = now.hour * 60 + now.minute
        return {
            "start": format_time(segment_start(stamp, self.config.granularity_minutes)),
            "end": format_time(min(DAY_MINUTES, stamp + SEGMENT_DEFAULT_MINUTES)),
            "event": "自由活动",
            "location": "家中",
            "emotion": "随意",
            "energy_rate": 0.0,
        }

    def current_activity(self) -> dict[str, Any]:
        """她这会儿在做什么（含做了多久、还剩多久），给状态与过程展示用。"""
        now = self._clock.now()
        minutes = now.hour * 60 + now.minute
        slot = self.active_segment(minutes)
        if slot is None:
            return {}
        lo = parse_time(slot.get("start"))
        hi = parse_time(slot.get("end"))
        elapsed = max(0, minutes - lo) if lo is not None else 0
        remaining = max(0, hi - minutes) if hi is not None else 0
        return {
            "name": str(slot.get("event") or "").strip(),
            "location": str(slot.get("location") or "").strip(),
            "emotion": str(slot.get("emotion") or "").strip(),
            "started_at": str(slot.get("start") or ""),
            "expected_end": str(slot.get("end") or ""),
            "duration_minutes": elapsed,
            "remaining_minutes": remaining,
        }

    def carry_end_text(self) -> str:
        """这一觉会睡到明天几点（没有跨夜结转时为空）。"""
        carry = self._carry_slot()
        if carry is None:
            return ""
        return str(carry.get("end") or "")

    @property
    def source(self) -> str:
        return str(self._scope.get_self("schedule_source", SOURCE_TEMPLATE))

    @property
    def source_text(self) -> str:
        return "大模型现排" if self.source == SOURCE_LLM else "按身体现算"

    @property
    def generating(self) -> bool:
        return self._generating

    @property
    def retry_after(self) -> float:
        return max(0.0, self._retry_after - self._monotonic())

    def _body_snapshot(self) -> dict[str, Any]:
        if self._body is None:
            return {}
        try:
            snapshot = self._body()
        except Exception:
            return {}
        return snapshot if isinstance(snapshot, dict) else {}

    def _seed(self) -> list[Any]:
        try:
            return [self._scope.role_id, self._clock.today_str()]
        except Exception:
            return [self._scope.role_id]

    # ------------------------------------------------------------------
    # 跨天与结转
    # ------------------------------------------------------------------

    def _migrate_new_day(self, today: str) -> None:
        """跨天：把结转到今天的那截觉接成今天的第一段。

        没有结转时不在这里生成——今天的第一段由后台循环按到期规则现决定，
        昨天那份留在原地当 prompt 的上下文（「昨天的收尾」）。
        """
        if str(self._scope.get_self("today_date", "") or "") == today:
            return
        carry = self._scope.get_self(CARRY_KEY)
        if not isinstance(carry, dict) or not carry:
            return
        end = parse_time(carry.get("end"))
        if end is None or end <= 0:
            self._scope.set_self(CARRY_KEY, None)
            return
        slot = {
            "start": "00:00",
            "end": format_time(end),
            "event": str(carry.get("event") or SLEEP_EVENT),
            "location": str(carry.get("location") or "卧室"),
            "emotion": str(carry.get("emotion") or "沉睡"),
            "energy_rate": clamp_rate(carry.get("energy_rate", 0.15)),
        }
        self._scope.update_self(
            today_date=today,
            daily_schedule=[slot],
            **{CARRY_KEY: None},
        )

    def _history_for_prompt(self) -> tuple[list[Slot], str]:
        """生成 prompt 用的「最近过完的段」：今天的不够看就接昨天的收尾。"""
        now = self._clock.now()
        minutes = now.hour * 60 + now.minute
        today = self._clock.today_str()
        if str(self._scope.get_self("today_date", "") or "") == today:
            segs = self.segments()
            note = "今天到这会她已经过完的时段"
            done = [
                slot for slot in segs
                if (parse_time(slot.get("end")) or 0) <= minutes
            ]
            return done, note
        stored = self._stored_slots()
        return stored, "昨天的收尾（刚跨过零点）"

    # ------------------------------------------------------------------
    # 何时该重新决定
    # ------------------------------------------------------------------

    def _gate_allows(self) -> bool:
        """节流闸门：没人看着 / 间隔没到 / 今日预算用完时，都算「不需要决定」。

        问在「本来该生成」之后而不是之前：正常无事可做的那几百个 tick 不该去
        反复查闸门，更不该把 _roll_change 的骰子白白掷掉。
        """
        gate = getattr(self.gateway, "gate", None) if self.gateway is not None else None
        if gate is None:
            return True
        return gate.check(PURPOSE).allowed

    def refresh_due(self) -> bool:
        """该不该决定下一段：没段/这一段过完了/身体打架/还没排上大模型/掷中骰子。"""
        cfg = self.config
        segs = self.segments()
        if not segs:
            return True
        now = self._clock.now()
        minutes = now.hour * 60 + now.minute
        active = self._covering(segs, minutes)
        if active is None:
            return True
        event = str(active.get("event") or "")
        if is_sleep_event(event):
            # 睡着：这一觉睡到这段结束为止，夜里不重掷、不越线打断。
            return False
        body = self._body_snapshot()
        if body:
            hunger = float(body.get("hunger", 0.0) or 0.0)
            pressure = float(body.get("sleep_pressure", 0.0) or 0.0)
            if hunger >= CHANGE_TRIGGER_HUNGER and not is_meal_event(event):
                return True
            if pressure >= CHANGE_TRIGGER_SLEEP_PRESSURE:
                return True
        if cfg.use_llm_schedule and self.source != SOURCE_LLM:
            # 本地兜底段还占着位：尽快让大模型接手（退避会挡住砸模型的频率）。
            due = True
        else:
            due = self._roll_change()
        return due and self._gate_allows()

    def _roll_change(self) -> bool:
        """概率重估：每个决策窗掷一次骰子，本窗内结果保持不变。"""
        interval = max(60, int(self.config.schedule_refresh_minutes) * 60)
        window = int(self._clock.now().timestamp() // interval)
        if window < self._roll_window:
            # 时钟回拨/重装：重置后重新掷
            self._roll_window = window
            self._roll_hit = False
            return self._roll_hit
        if window == self._roll_window:
            return self._roll_hit
        self._roll_window = window
        chance = min(100.0, max(0.0, float(self.config.schedule_change_chance))) / 100.0
        self._roll_hit = self._random() < chance
        return self._roll_hit

    def seed_first_segment(self) -> bool:
        """开机时按身体当场种下第一段，不等模型。

        插件刚装好、或重启后的那几分钟里，问「你在干嘛」不该得到「还没决定」。
        这一段之后会被后台循环换成大模型排的（退避窗口一过就换）。
        """
        if self.segments():
            return False
        now = self._clock.now()
        minutes = now.hour * 60 + now.minute
        slot = dynamic_segment(
            self.config, self._body_snapshot(), now_minute=minutes,
            step=self.config.granularity_minutes, seed=self._seed(),
        )
        self._append_segment(slot, self._clock.today_str())
        return True

    # ------------------------------------------------------------------
    # 生成
    # ------------------------------------------------------------------

    def request_refresh(self, force: bool = False, ignore_cooldown: bool = False) -> bool:
        """投递一次后台决定。到点（到期/越线/掷中）才真的投。"""
        if self._task and not self._task.done():
            return False
        if self._generating:
            return False
        if not force and not self.refresh_due():
            return False
        coro = self.ensure_fresh(force=force, ignore_cooldown=ignore_cooldown)
        name = f"humanoid-schedule-refresh-{self._scope.role_id}"
        if self._spawn:
            self._task = self._spawn(coro, name)
        else:
            self._task = asyncio.create_task(coro, name=name)
        return True

    async def ensure_fresh(self, force: bool = False, ignore_cooldown: bool = False) -> bool:
        """决定（并装上）下一段。

        模型不可用、未配置或还在退避期时：手上没有能接着做的段就按身体现算一段
        （不留白）；有段就先做着手上的事，等退避过去再让模型接手。
        """
        cfg = self.config
        now = self._clock.now()
        minutes = now.hour * 60 + now.minute
        today = self._clock.today_str()

        async with self._lock:
            # 拿锁期间可能已被别的路径决定过：再确认一次。
            if not force and not self.refresh_due():
                return False
            segs = self.segments()
            active = self._covering(segs, minutes)
            prev = segs[-1] if segs else None
            elapsed = 0
            if prev is not None:
                lo = parse_time(prev.get("start"))
                if lo is not None:
                    elapsed = max(0, minutes - lo)

            slot: Slot | None = None
            if cfg.use_llm_schedule and self.gateway is not None:
                if force or ignore_cooldown or self.retry_after <= 0:
                    slot = await self._generate_segment(
                        cfg, now, minutes, prev, elapsed, ignore_cooldown
                    )
            if slot is None and (active is None or force or not cfg.use_llm_schedule):
                slot = dynamic_segment(
                    cfg, self._body_snapshot(), now_minute=minutes,
                    step=cfg.granularity_minutes, prev=prev, seed=self._seed(),
                )
                self._set_source(SOURCE_TEMPLATE)
            if slot is None:
                return False
            self._append_segment(slot, today)
            return True

    def _fail_backoff(self, cfg: HumanoidConfig) -> None:
        backoff = max(MIN_RETRY_BACKOFF_SECONDS, float(cfg.schedule_provider_cooldown_minutes) * 60)
        self._retry_after = self._monotonic() + backoff

    async def _generate_segment(
        self,
        cfg: HumanoidConfig,
        now,
        minutes: int,
        prev: Slot | None,
        elapsed: int,
        ignore_cooldown: bool,
    ) -> Slot | None:
        if self.gateway is None:
            self.last_error = "LLM Gateway 未初始化"
            return None

        persona = await self._resolve_persona()
        history, history_note = self._history_for_prompt()
        prompt = segment_prompt(
            cfg,
            now_text=now.strftime("%H:%M"),
            weekday=self._clock.weekday(),
            persona=persona,
            body=self._body_snapshot(),
            prev=prev,
            elapsed_minutes=elapsed,
            history=history,
            history_note=history_note,
            is_night=self._clock.is_night(),
        )
        if self._log and cfg.debug_mode:
            self._log.debug(
                f"[humanoid_core] 下一段提示词（人设："
                f"{persona.label if persona else '未读'}）:\n{prompt}"
            )

        self._generating = True
        try:
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
        except Exception as exc:
            self.last_error = f"生成异常：{exc}"
            self._fail_backoff(cfg)
            return None
        finally:
            self._generating = False

        if not result.ok:
            if result.outcome == OUTCOME_GATED:
                # 被节流跳过不是 provider 故障，不能进 30 分钟退避——那会让
                # 「用户一说话就恢复」也一起失效。也不往 last_error 上挂：
                # 那是给用户看的诊断位，不该长期显示一个无须处理的节流提示。
                if self._log and cfg.debug_mode:
                    self._log.debug(f"[humanoid_core] 日程被节流跳过: {result.detail}")
                return None
            self.last_error = result.summary()
            self._fail_backoff(cfg)
            if self._log and cfg.debug_mode:
                self._log.debug(f"[humanoid_core] 下一段生成失败: {self.last_error}")
            return None

        raw = extract_json_object(result.text)
        if raw is None:
            raw = extract_json_array(result.text)
        slot = parse_segment(
            raw, now_minute=minutes, step=cfg.granularity_minutes, prev=prev
        )
        if slot is None:
            # 解析失败同样进退避：不挡的话每个后台周期都会再砸一次模型。
            self.last_error = f"无法解析这一段：{result.text[:160]}"
            self._fail_backoff(cfg)
            if self._log and cfg.debug_mode:
                self._log.debug(f"[humanoid_core] 下一段解析失败: {self.last_error}")
            return None

        self.last_error = ""
        self._retry_after = 0.0
        self._set_source(SOURCE_LLM)
        return slot

    def _set_source(self, source: str) -> None:
        if str(self._scope.get_self("schedule_source", "") or "") != source:
            self._scope.set_self("schedule_source", source)

    def _append_segment(self, slot: Slot, today: str) -> None:
        """把这一段接在今天后面，并处理与上一段的衔接。

        - 上一段还没到点就被换掉（她提前结束去做别的）：end 收到新段起点，不留重叠；
        - 上一段过完了、隔得不久：顺延到新段起点，不留窟窿（窟窿里 soma 读不到
          「她在吃饭」，刚归零的饥饿会立刻又涨一格）；
        - 隔得太久（插件停机）：不补，那段时间她做了什么没人知道。
        - 睡觉跨午夜：今天截到 24:00，剩下的结转到明天（`segment_carry`）。
        """
        segs = self.segments()
        minutes = self._clock.now().hour * 60 + self._clock.now().minute
        previous = self._covering(segs, minutes) or (segs[-1] if segs else None)

        carry_end = str(slot.pop("carry_end", "") or "")
        new_start = parse_time(slot.get("start"))
        if segs and new_start is not None:
            last = dict(segs[-1])
            hi = parse_time(last.get("end"))
            lo = parse_time(last.get("start"))
            if hi is not None and lo is not None:
                if hi > new_start:
                    # 她提前结束去做别的：收到新段起点，不留重叠。
                    if new_start > lo:
                        last["end"] = format_time(new_start)
                        segs[-1] = last
                    else:
                        # 上一段刚开始就被换掉：它没有存在过，丢掉比留一条零长度记录干净。
                        segs.pop()
                elif hi < new_start and new_start - hi <= SEGMENT_GAP_MERGE_MINUTES:
                    last["end"] = format_time(new_start)
                    segs[-1] = last

        # 接着做同一件事：并回上一段，不拆成两条「上午在改海报」
        installed = slot
        if segs and new_start is not None:
            last = segs[-1]
            if (
                str(last.get("event") or "") == str(slot.get("event") or "")
                and parse_time(last.get("end")) == new_start
            ):
                last["end"] = slot.get("end")
                last["energy_rate"] = slot.get("energy_rate", last.get("energy_rate"))
                segs[-1] = last
                installed = last

        if installed is not slot:
            slot = None
        else:
            segs.append(slot)
        cap = max(6, min(SEGMENTS_DAY_CAP, int(self.config.schedule_max_slots)))
        if len(segs) > cap:
            segs = segs[-cap:]
        updates: dict[str, Any] = {
            "daily_schedule": segs,
            "today_date": today,
            "schedule_generated_at": self._clock.now().strftime("%Y-%m-%d %H:%M:%S"),
            "schedule_persona": self.last_persona,
        }
        if carry_end:
            carry = {
                "end": carry_end,
                "event": installed.get("event"),
                "location": installed.get("location"),
                "emotion": installed.get("emotion"),
                "energy_rate": installed.get("energy_rate"),
            }
            updates[CARRY_KEY] = carry
        elif str(self._scope.get_self(CARRY_KEY, "") or ""):
            # 新的一段不是跨夜睡眠：旧的结转不该再留着
            updates[CARRY_KEY] = None
        self._scope.update_self(**updates)

        # 本窗的骰子已经用掉了：刚装上的这一段，本窗内不再重掷。
        self._roll_hit = False

        if self.on_install is not None:
            try:
                self.on_install(previous, installed)
            except Exception:
                pass
        if self._log and self.config.debug_mode:
            self._log.debug(
                f"[humanoid_core] 新的一段: {installed.get('start')}→{installed.get('end')} "
                f"{installed.get('event')}"
                + (f"（结转到明天 {carry_end}）" if carry_end else "")
            )

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
        segs = self.segments()
        active = self.active_segment()
        return {
            "date": today,
            "stored_date": self._scope.get_self("today_date", ""),
            "slots": len(segs),
            "active": (
                f"{active.get('start')}-{active.get('end')} {active.get('event')}"
                if active else ""
            ),
            "activity": self.current_activity(),
            "sleep_spans": [
                f"{s.get('start')}-{s.get('end')} {s.get('event')}" for s in sleep_spans(self.current_slots())
            ],
            "wake_at": schedule_wake_text(self.current_slots()),
            "source": self.source,
            "source_text": self.source_text,
            "persona": str(self._scope.get_self("schedule_persona", "") or "") or self.last_persona,
            "generated_at": self._scope.get_self("schedule_generated_at", ""),
            "last_error": self.last_error,
            "generating": self.generating,
            "retry_after": self.retry_after,
            "due": self.refresh_due(),
            "pending_today": str(self._scope.get_self("today_date", "") or "") != today,
        }

    async def aclose(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
