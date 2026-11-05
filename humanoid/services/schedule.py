"""日程服务 - 滚动分段：一次只决定「从现在起的这一段」。

与旧版整表日程的区别：未来不预排。她的下一段做什么、做多久，到了时候看着她
当时的身体与人设现决定；每天每 15 分钟一个决策窗，生成或不生成都可能——这一段
过完了、身体跟手上的事明显打架、或掷中了变动概率，才会重新问一次模型，其余
时候她接着做手上的事，一次模型都不调。
"""

from __future__ import annotations

import asyncio
import math
import random
import re
import time
from collections.abc import Callable, Mapping
from typing import Any

from ..clock import Clock
from ..config import HumanoidConfig
from ..jsonx import extract_json_array, extract_json_object
from ..llm import OUTCOME_GATED, OUTCOME_OK, LLMGateway, LLMResult, ProviderResolver
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
    slot_sleeps,
)
from ..traits import detect as detect_traits, trait_hints
from ..wording import pick
from .habit import archive_day, habit_lines, history_of, render_habits
from .life_material import (
    CAPABILITY_WORDS as _EXAMPLE_NEEDS,
    materials_for,
    render_materials,
)
from .plans import load_plans, note_plans, render_plans
from .role_materials import (
    ROLE_MATERIALS_COUNT,
    ROLE_MATERIALS_MIN,
    ROLE_MATERIALS_RETRY_SECONDS,
    accepted as _role_accepted,
    build_prompt as _role_build_prompt,
    load as _role_load,
    mark_try as _role_mark_try,
    needs_build as _role_needs_build,
    parse_materials as _role_parse_materials,
    persona_fingerprint as _role_fingerprint,
    store as _role_store,
)

PURPOSE = "日程生成"
SOURCE_TEMPLATE = "template"
SOURCE_LLM = "llm"
MIN_RETRY_BACKOFF_SECONDS = 60.0

SLEEP_EVENT = "睡眠"

# 一段的时长边界：最短 3 分钟；清醒的事最长 3 小时；一觉最长 12 小时（跨午夜）。
#
# 下限原来是 15 分钟，是「微动作被写成半小时」那条链的第一环，三处叠在一起：
#   ① prompt 写着「最短 15 分钟」，模型不敢写更短；
#   ② `_segment_minutes` 把模型写的 5 分钟抬到 15（写了也没用）；
#   ③ 起点按 15 分钟刻度向下对齐，几分钟的事也要占掉一个刻度。
# 于是「在沙发上喝东西」必然是 30 分钟一段。下限改 3：洗把脸、倒杯水、回条消息
# 本来就是几分钟的事；短段不再对齐刻度（见 `_step_for`）。
SEGMENT_MIN_MINUTES = 3
SEGMENT_MAX_MINUTES = 180
SEGMENT_SLEEP_MAX_MINUTES = 720
SEGMENT_DEFAULT_MINUTES = 60
# 睡段的 end 写在过去、又不像「明天那个点」时，按一小觉接上（见 _segment_minutes）。
SEGMENT_SLEEP_FALLBACK_MINUTES = 90
# 今天累计睡到这个数就算睡够了：再排睡就不是缺觉，是没别的事可做。
# 90 分钟取的是「一整段午觉」的量——真困到需要补觉的人会同时越过敏意线
# （CHANGE_TRIGGER_SLEEP_PRESSURE），那条规则仍然放行，这里只拦「睡够了还睡」。
NAP_REDUNDANT_MINUTES = 90
# 白天一觉最长到这儿：白天睡四小时以上不是「补觉」，是这一天没了。
# 夜里那一觉不受这条约束（睡多久由夜里那段决定），困意越线时也不拦。
DAY_SLEEP_MAX_MINUTES = 240
# 一天最多留这么多段（超出丢最早的：三十段之前的事她自己也记不清）。
# 这只是**硬天花板**：真正生效的是配置项 `schedule_max_slots`（面板上限就是 96）。
# 以前这里写死 48，把用户填 96 砍成 48——面板上写着却不起作用的数比没有更糟。
SEGMENTS_DAY_CAP = 96
# prompt 里带多少段「已经过完的时段」。
SEGMENTS_PROMPT_HISTORY = 8

# 身体越线：不等这一段结束，当场重新决定（饿到发慌还在做事、困到不行还醒着）。
CHANGE_TRIGGER_HUNGER = 78.0
# 困意越线从 88 降到 80：用户原话是「困意 86 还在厨房煮饭」。88 那一档实际上
# 只有快撑不住时才够得着，而 80 已经明显不该再做需要精神的事了（prompt 里也
# 用同一条线告诉模型「这一段优先睡一会儿/闭眼休息」）。
CHANGE_TRIGGER_SLEEP_PRESSURE = 80.0
# 本地兜底里「夜里该睡了」的困意门槛：比模型路径宽，因为没有模型替她权衡。
LOCAL_NIGHT_SLEEP_PRESSURE = 70.0
# 本地兜底里「白天困到该补一觉」的困意门槛：比夜里那条高，避免有点困就躺下。
# 触发后安排 45~90 分钟小睡/补觉——用户原话是「白天确实该困，但是白天不睡觉，
# 而是困意 86 在厨房煮饭」，所以白天的高困意必须有地方去，不能靠"再撑撑"。
LOCAL_DAY_NAP_PRESSURE = 82.0

# 睡觉跨午夜时，今天这半截之后接到明天的那一截。
CARRY_KEY = "segment_carry"

# 「被打断」的三道比例尺：
#
# - 安静间隔：上一条私聊与这条之间至少隔这么久，才算「打破了一段安静、有人来访」。
#   很多人同时聊的部署里消息源源不断，这一条会让它自然不再触发——她正忙着，
#   不该被「又有人发消息了」整天重排。这是多用户下它不失控的关键。
# - 冷却：两次被算作「来访」之间至少隔这么久（安静间隔之外的第二道闸）。
# - 保鲜期：来访标记超过这么久还没用上就不算数——「刚刚」不能对着几小时前的事说。
# 只看私聊：群消息不是「有人来找她说话」。
INTERRUPT_QUIET_GAP_SECONDS = 2700.0
INTERRUPT_COOLDOWN_SECONDS = 3600.0
INTERRUPT_TTL_SECONDS = 3600.0

# ─────────────────────────────────────────────────────────────
# 事件：别人说的话真的能改写她的日子
#
# 上面那三道闸只管「有人来过」，不管「她经历了什么」。于是日程只能被身体读数
# （饿/困）推动，用户骂她一句、夸她一句，日程照走——她正做着的事不会因为
# 一句话停下来。那不是一个人的一天。
#
# 事件闸是分开的、宽得多的三条：
# - 极性分向：正向（被夸/被关心）与负向（被骂/被敷衍）都算，方向决定提示词
#   往哪边写；中性的（问句、陈述）不计入——不然每句话都重排一次。
# - 强度门槛：只有账本认得出轻重的事才算（kind 由 mood 层的 _event_kind 给）。
# - 保鲜期短：事件是「刚刚」，隔了两小时就不该再影响她这会儿在做什么。
#
# 事件**不绕过**闸门/频率/预算：它只是多一条进入 refresh_due 的路，
# 该省的调用一次都不多花。
EVENT_TTL_SECONDS = 1800.0
EVENT_COOLDOWN_SECONDS = 300.0

# ─────────────────────────────────────────────────────────────
# 可配的「保鲜期 / 打断 / 续做」（v2.28.9）
#
# 用户原话：「日程生成还是有限制，所谓的保鲜期，就不是可以中途打断换一个，
# 或在当前日程接下去的。我需要加，加逻辑与功能」。
#
# 上面那三道比例尺与事件保鲜期原来全是写死的秒数，面板上改不了——用户说的
# 「保鲜期限制了打断与继续」就是这里。现在每一项都有配置键，常量只留作默认值
# 来源（旧测试与旧行为都引用它们）。读取一律走 `getattr`：旧配置、坏值、
# 只实现了部分字段的 cfg 对象都退回默认值，绝不因为缺一个键就抛。
# ─────────────────────────────────────────────────────────────

# 私聊里这些是「明确在叫住她」的话：即使还没熬过安静间隔，也算一次来访。
# 仍要过冷却（两次打断之间的最小间隔）；群聊永远不算。
_INTERRUPT_CALL_PHRASES: tuple[str, ...] = (
    "在吗",
    "你在干嘛",
    "干嘛呢",
    "过来",
    "陪我",
    "别弄了",
    "停一下",
    "理我",
    "别写了",
    "别忙了",
)


def _cfg_number(cfg: Any, name: str, default: float, low: float = 0.0) -> float:
    """读一个数值型配置：坏值/缺键退回默认值，并夹到低限。"""
    try:
        value = float(getattr(cfg, name, default))
    except (TypeError, ValueError):
        value = float(default)
    if value != value:  # NaN
        value = float(default)
    return max(float(low), value)


def _interrupt_ttl_seconds(cfg: Any) -> float:
    """来访保鲜期（秒）：有人来找过她这件事，超过这么久没被用上就作废。"""
    return _cfg_number(
        cfg, "schedule_interrupt_ttl_minutes", INTERRUPT_TTL_SECONDS / 60.0
    ) * 60.0


def _event_ttl_seconds(cfg: Any) -> float:
    """事件保鲜期（秒）：隔了这么久，「刚刚」就不再是刚刚。"""
    return _cfg_number(cfg, "schedule_event_ttl_minutes", EVENT_TTL_SECONDS / 60.0) * 60.0


def _interrupt_quiet_seconds(cfg: Any) -> float:
    """安静间隔（秒）：0 = 不要求安静，任何一条私聊都算来访。"""
    return _cfg_number(
        cfg, "schedule_interrupt_quiet_minutes", INTERRUPT_QUIET_GAP_SECONDS / 60.0
    ) * 60.0


def _interrupt_cooldown_seconds(cfg: Any) -> float:
    """两次「算作来访」之间最小间隔（秒）；0 = 不冷却。"""
    return _cfg_number(
        cfg, "schedule_interrupt_cooldown_minutes", INTERRUPT_COOLDOWN_SECONDS / 60.0
    ) * 60.0


def _continue_minutes(cfg: Any) -> int:
    """`/拟人 日程继续` 一次把当前段延长多少分钟。"""
    return max(1, int(_cfg_number(cfg, "schedule_continue_minutes", 45.0)))


def _pause_ttl_minutes(cfg: Any) -> int:
    """被打断的那件事保留多久（分钟），期间可以回去接着做。"""
    return max(1, int(_cfg_number(cfg, "schedule_pause_ttl_minutes", 180.0)))


def _on_call_enabled(cfg: Any) -> bool:
    """「叫住她就打断」开关：缺键/坏值按默认开。"""
    value = getattr(cfg, "schedule_interrupt_on_call", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "n", "off", "否", "关", "")
    return bool(value)


def _is_call_out(text: Any, cfg: Any) -> bool:
    """这句话是不是「明确在叫住她」（在吗/你在干嘛/陪我/别弄了…）。"""
    if not _on_call_enabled(cfg):
        return False
    content = str(text or "")
    if not content:
        return False
    return any(phrase in content for phrase in _INTERRUPT_CALL_PHRASES)


# 事件的措辞。kind 来自 mood 层的账本（_event_kind），那边负责判定轻重，
# 这边只负责把它说成「她刚刚经历了什么」。同一个 kind 备几套说法，
# 由 (角色, 事件, 日期) 抽签——同一天不会把同一句话说两遍。
_EVENT_PHRASES: dict[str, tuple[str, ...]] = {
    "很上心": ("刚才被人好好放在心上", "刚才那句被人接住了"),
    "温和": ("刚才那句话让她挺舒服", "刚才有人对她很温和"),
    "有点冷": ("刚才那句话有点噎人", "刚才那句让她不太痛快"),
    "很敷衍": ("刚才被人敷衍了", "刚才那句很难听"),
    "一直没被理": ("刚才她说的没人接", "刚才等了个空"),
}


def event_phrase(kind: str, seed: list[Any]) -> str:
    """把账本里的 kind 翻成一句「她刚刚经历了什么」。认不出的 kind 返回空串。"""
    options = _EVENT_PHRASES.get(str(kind or "").strip())
    if not options:
        return ""
    return pick("sched_event", seed, options)


def night_window_minutes(cfg: HumanoidConfig) -> tuple[int, int] | None:
    """她的夜间窗口：**唯一的一份**。

    `routine_prompt`（写进分段 prompt 的作息条）与 `dynamic_segment`（模型不可用时的
    本地兜底）都从这里取，不再各读一遍 `night_start_hour/night_end_hour`。

    关掉夜间模式、或窗口长度为 0（config 校验里就是「关闭夜间窗口」）时返回 None：
    **不再**退回 23:00-07:00 的硬编码窗口。旧兜底正是这么做的——用户关掉夜间模式，
    兜底反而替她排一觉到早上 7 点，比配置的 23:00-06:00 还长，而同一份 prompt 里
    写着「几点睡由她是谁决定」。
    """
    if not bool(getattr(cfg, "night_mode_enabled", True)):
        return None
    start = int(getattr(cfg, "night_start_hour", 23)) * 60
    end = int(getattr(cfg, "night_end_hour", 6)) * 60
    if start == end:
        return None
    return start, end


def routine_prompt(cfg: HumanoidConfig, first_index: int = 10) -> str:
    """作息那一条怎么写进分段 prompt。

    默认让模型从人设里读她几点睡；只有用户显式打开「日程贴合夜间窗口」时，才把
    夜间窗口当成硬约束递进去（分段生成时同样生效：到了点就该排睡眠）。
    """
    window = night_window_minutes(cfg)
    if window is None or not cfg.schedule_follow_night_window:
        return (
            f"{first_index}. 几点睡、几点起由她是谁决定：从人设里读她的年纪感、工作或学业、"
            "生活习惯，结合当前的身体数值（困意、睡眠债）由你判断；"
            "人设里写着的作息保持不动。"
            # B2：别把一套固定作息当默认（有人设是半夜活动/下午才起）；午睡不是必须。
            # 不点名具体作息类型——那等于替模型做决定（test_v2142_routine 盯着）。
            "人设没写清楚时，按她的年纪感与生活习惯推，不要套用一套固定作息；"
            "午睡/小憩不是每天必须有的一站。\n"
        )
    start, end = window[0] // 60, window[1] // 60
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
    return [slot for slot in slots if slot_sleeps(slot)]


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
# 这里曾经还有一个 `DAY_UPCOMING_MINUTES`（「接下来要做的事只算三小时以内」）。
# 滚动分段不预排未来（`segments()` 只返回 `start <= now` 的段），未来分支永远
# 收不到一条数据——那个常量与它服务的分支一起删掉了（#73）。


def period_of(minutes: int) -> str:
    hour = (minutes // 60) % 24
    for start, end, name in _PERIODS:
        if start <= hour < end:
            return name
    return "白天"


# 日程事件名是模型写的自由文本，有的自带动词与介词（"坐在客厅沙发上看书"）。外面再套
# 一个「在」就成「现在在坐在客厅沙发上看书」——模型会照着这句病句往下说。
# 拼接前先看一眼开头：动词/介词开头的直接接，其它才补「在」。
_ACTION_HEADS = frozenset("在坐躺站靠趴蹲爬走跑跳吃喝做玩看读写听说聊打开学忙醒起洗梳穿等逛买煮烤拖练画唱骑划")

_EVENT_PHRASE_MAX = 24
# 事件名一律削到 24 字（对外契约不变），但**怎么削**有讲究：原来解析时是 `raw_event[:24]`
# 硬切，切在词中间（“在厨房做红烧肉和清炒时”），后面按标点收尾也补不回来——那个字已经
# 不在数据里了。现在存和渲染都走 `_clip_event`：先按标点断句，整句没标点才硬截（#156）。

# 她睡着的时候听到的话，最多留几条：醒来那一段的 prompt 里逐条递进去。
# 多留就成了任务清单（她得挨个回应），3 条已经够说明「那会儿有人来过」。
ASLEEP_HEARD_MAX = 3

# 梦：模型给的是自由文本，可能带空串、重复、超长、甚至非字符串。
# 只留能说出口的一小句：每条封顶 20 字（prompt 里教的是 15 字，留一点余量），
# 一觉最多 3 条（白天睡的那一觉给 3 条，夜里给 2 条，见 `segment_prompt`）。
DREAM_MAX_ITEMS = 3
DREAM_MAX_CHARS = 20
# 模型常把梦写成「梦到…」「梦见…」。契约与社交层约定的是**梦的内容本身**
# （社交层会自己加「梦到」做由头），这里统一剥掉句首的引导词，避免出现
# 「她刚从梦里醒过来（梦到梦到一片海）」这种叠字。
_DREAM_PREFIXES = ("我梦见", "我梦到", "梦见", "梦到")


def _clean_dreams(raw: Any) -> list[str]:
    """把 `dreams` 字段洗成「去空、去重、限长、限条数」的字符串列表。

    旧状态的 slot 没有这个键，坏数据、单条字符串、数字混在里面都可能出现。
    所有读取路径（parse / carry / migrate / day_phrases / 契约）都走这里，
    保证外面拿到的一定是 `list[str]`，不会因为一条脏数据把契约导出整段带崩。
    """
    if isinstance(raw, str):
        items: list[Any] = [raw]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        return []
    out: list[str] = []
    for item in items:
        # 非字符串一律忽略：模型偶尔会把「梦」写成对象/数字，那种不是能说出口的话。
        if not isinstance(item, str):
            continue
        text = item.strip()
        for prefix in _DREAM_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break
        text = text[:DREAM_MAX_CHARS]
        if not text or text in out:
            continue
        out.append(text)
        if len(out) >= DREAM_MAX_ITEMS:
            break
    return out


def _clip_event(text: str, limit: int = _EVENT_PHRASE_MAX) -> str:
    """把事件名削到 `limit` 字以内，且不留半个词、不悬空标点。

    存储侧曾经在解析时就按 24 字硬切（`raw_event[:24]`），切在词中间
    （“在厨房做红烧肉和清炒时”之后那个字已经没了），渲染侧再按标点收尾也补不回来。
    现在解析存盘和渲染一律走这里：先看长度，超了就在最后一个标点/空格处收；
    整句没有标点才硬截（并把尾部的悬空标点抹掉）。
    """
    text = str(text or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max((head.rfind(ch) for ch in "，。；、,; "), default=-1)
    if cut >= limit // 2:
        return head[:cut].strip(" -·、，。/~")
    return head.rstrip(" -·、，。/~")


def _event_phrase(event: str) -> str:
    """把时段名削成能放进一句口语里的动作。

    原来直接按 16 字硬截，「坐在客厅沙发看书并吃点水果」会被砍成「坐在客厅沙发看书并吃点水」
    这种断句。改成先剥掉「安排/活动/时间」这类尾巴，再交给 `_clip_event` 按标点收尾。
    """
    text = str(event or "").strip()
    for affix in ("安排", "活动", "时间", "（", "("):
        cut = text.find(affix)
        if cut > 1:
            text = text[:cut]
    return _clip_event(text.strip(" -·、，。/~"))


def _with_at(lead: str, phrase: str) -> str:
    """把时段词或「现在」与动作拼成一句，不出现「在坐在」这种叠字。"""
    body = str(phrase or "").strip()
    if not body:
        return ""
    if body.startswith("在") or body[0] in _ACTION_HEADS:
        return f"{lead}{body}"
    return f"{lead}在{body}"


def _at_phrase(lo: int, phrase: str, slot: Slot) -> str:
    """把「时段 + 这件事」拼成一句，认出处境词与动作词的区别。

    兜底分段（`dynamic_segment`）的 `event` 是**处境词**（“有点饿了”“肚子空着”），
    不是动作。`_with_at()` 只认动词开头，对这类词会补一个「在」，拼出
    「上午在有点饿了」「夜里在还没什么困意」这种病句——而这句会经 `day_phrases`
    进入跨插件契约的 day 块，被当成她今天的既成事实。带 `status` 标记的段直接接在
    时段词后面，不补「在」。
    """
    if slot.get("status"):
        return f"{period_of(lo)}{phrase}"
    return _with_at(period_of(lo), phrase)


def day_phrases(
    slots: list[Slot],
    now_minutes: int,
    *,
    past_limit: int = 2,
    future_limit: int = 1,  # 历史参数：滚动分段下「接下来」恒为空，恒被忽略（见下）
) -> dict[str, Any]:
    """把日程拆成她能直接说出口的三段：刚做过什么 / 正在做什么 / 接下来做什么。

    滚动分段日程里没有预排的未来，所以「接下来」**永远是空列表**：`segments()`
    只返回 `start <= 现在` 的段（未来段进不来），而她的下一段要等到时候看着当时
    的身体与人设现决定——提前宣布等下要去哪，那本来就是排出来之前不知道的事。

    `future_limit` 是给旧调用方（`link.py:166` 还在传 `future_limit=2`）留的兼容
    参数：收下但不生效，返回的 `next` 恒为 `[]`。等 R1 批次把那个调用点一起清掉，
    这个参数就可以删了（#73）。

    `dreams` 是当天**最近一段睡眠**的梦：契约和社交层拿它说「她昨晚梦到什么」。
    旧日程没有这个字段，所有旧 slot 读出来都是空列表——调用方不需要区分
    「没有梦」和「旧版本没记」。
    """
    doing = ""
    done: list[str] = []
    dreams: list[str] = []
    # 把 slot 本体一起带上：梦要从**同一段**里取，不能只留事件名再去反查。
    ordered: list[tuple[int, int, str, Slot]] = []
    for slot in slots:
        lo = parse_time(slot.get("start"))
        hi = parse_time(slot.get("end"))
        if lo is None or hi is None or hi <= lo:
            continue
        ordered.append((lo, hi, str(slot.get("event") or "").strip(), slot))

    for lo, hi, event, slot in ordered:
        if lo <= now_minutes < hi:
            phrase = _event_phrase(event)
            if phrase:
                doing = "睡觉" if slot_sleeps(slot) else phrase
            continue
        if hi <= now_minutes:
            if now_minutes - hi > DAY_RECENT_PAST_MINUTES:
                continue
            if slot_sleeps(slot):
                # 睡眠不报成「刚在睡觉」：那是刚醒的人说的话，醒三小时就不算了。
                if now_minutes - hi <= 90:
                    done.insert(0, "才醒")
                continue
            phrase = _event_phrase(event)
            if phrase:
                done.insert(0, _at_phrase(lo, phrase, slot))
            continue

    # 梦：按开始时间排序后取**最后一段睡眠**（跨午夜结转的那一截也算）。
    # 不按 end 取：23:00→24:00 与 00:00→07:00 是同一觉拆成的两段，按 end 比
    # 24:00 会永远赢过 07:00；按开始时间取最后一段就对了（两段的梦本来就该一致）。
    for _lo, _hi, event, slot in ordered:
        if slot_sleeps(slot):
            dreams = _clean_dreams(slot.get("dreams"))

    # 「她自己想做的」：今天（以及接下来）标了 wants 的那几段。
    wants: list[str] = []
    for slot in slots or []:
        if not isinstance(slot, dict) or not slot.get("wants"):
            continue
        event = str(slot.get("event", "") or "").strip()
        if event:
            wants.append(event)
    return {
        "doing": doing,
        "done": done[:max(0, int(past_limit))],
        # 滚动分段没有预排的未来：恒为空列表。**键必须留着**——社交侧按
        # `day.get("next")` 取，缺键会被当成契约坏了（#73）。
        "next": [],
        # 今天想做的事。**空列表也要给这个键**——社交侧按 `day.get("wants")` 取，
        # 缺键和空列表要能分开处理（今天是合法的「没有」）。
        "wants": wants[:3],
        # 当天最近一段睡眠的梦。同样**空列表也要给键**：社交侧用它派「梦」由头，
        # 旧日程没有 dreams 时这里返回 []，调用方按「今天没有可分享的梦」处理。
        "dreams": dreams,
    }


def persona_block(persona: Persona | None, limit: int = PERSONA_PROMPT_MAX) -> str:
    """把 AstrBot 人格设定写成分段 prompt 的开头：她是谁，先立住再决定这一段。"""
    if persona is not None and persona.usable:
        prompt = truncate(persona.prompt, max(200, int(limit or PERSONA_PROMPT_MAX)))
        # 人设面板里没填名字时 name 是空串——原样拼出去就是「她是「」。」
        # 日程一天生成几次，这句话会反复出现在她眼前。改成不提名字。
        who = (
            f"她是「{persona.name}」。"
            if str(persona.name or "").strip()
            else "她在 AstrBot 里的人格设定是下面这份，"
        )
        return (
            who
            + "这就是她本人，不是她在扮谁：\n"
            + f"{prompt}\n\n"
            "先从这里读出她的身份、年纪感、职业或在读状态、住在哪里、独居还是跟人住、"
            "平时跟谁来往、喜欢什么讨厌什么、花钱和精力的习惯。\n"
            # 原来这里是「设定里没提到的，不要替他编：那里留白即可。…不要替她定身份。」
            # 那不是防造身份，是**在命令模型不发挥**，而效果跟字面完全一致：
            # 每段都挑最安全最平庸的那件事，于是她的一天高度雷同，设定里写明的那些
            # 特征（长什么样、什么物种、什么年纪、有什么偏好）一条也用不上。
            # 画出来的小马会拿手指拿手机，就是因为「通用模板」比「按这个人」更好写。
            #
            # 「别替她定身份」要保留——那是对的。**「留白」要撤掉**——日常本来就该
            # 由她自己讲，而「自己讲」的前提是模型按这个人去想象，不是按模板去填。
            "设定里明确写到的（外貌、体型、是什么、有什么习惯），必须照着来，别写成通用的。\n"
            "设定里没写到的地方，按她这个人的性格和处境合理地安排就行。"
            "**别每天都挑同一个最省事的选择**——她的一天应该像她的一天，不是一张模板。\n"
            "不要给她硬造第二份身份；她是什么样的人，上面这份设定说了算。\n"
        )
    return (
        "没有现成的人设资料可读。\n"
        # 原句「按一份普通上班族/学生的真实日子补」直接预设了她是上班族还是学生。
        # 退休的人、自由职业者、全职带孩子的角色都会被凭空安一份工位。
        "不要假设她的职业或身份。排一段中性、多数人都成立的日子：有自己的住处、"
        "有事要做、会累。这些怎么安排、她具体在做什么，由她自己说。\n"
    )


def persona_traits_block(persona: Persona | None) -> str:
    """从人设里识别出的生活特质，拼成 prompt 里的一小块线索。

    为什么要有这一块：用户原话「不是每一个角色都是勤劳的」「夜猫子凌晨活动被砍」。
    人设里明明写着「夜猫子」「懒散」，以前的日程 prompt 一个字都不读，于是默认
    按「作息规律、勤快」去排。这里只递**从她自己的设定里读出来的线索**，不是
    插件新加的性格标签；识别不出来时整块不出现，行为与旧版完全一致。
    """
    if persona is None or not persona.usable:
        return ""
    try:
        hints = trait_hints(detect_traits(persona.prompt))
    except Exception:
        return ""
    if not hints:
        return ""
    return (
        "【她这个人的作息与劲头】（从人设里读出来的，安排这一段时参考；没提到的别替她补）\n"
        + "\n".join(f"- {hint}" for hint in hints)
        + "\n\n"
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


# 举例池：每次给模型看的范例从这里按天抽。
#
# 这块踩过两个坑，都记在这里：
#   一是范例写得越具体，模型抄得越像。原来那批写的是「拐去那家一直想去的书店」
#     「接着整理那份个案笔记」「跟朋友打一通电话」——那不是范例，是给她安了一个身份，
#     于是不管人设写的是谁，所有角色都长成同一个孤独文人。
#   二是整池洗牌抽四条，抽到什么全看当天运气：一天可能全是二十分钟的小事，
#     也可能全是两小时的大件，模型对时长的判断跟着当天的范例漂。
# 现在按「这件事本身占多久」分档，每档抽一条，所以任何一天递出去的范例都同时有短、
# 有长、有歇着的；事件本身写成中性、能落到任何角色身上的样子——
# 她具体做什么，从人设和她当下的处境里长出来，不从范例里抄。
#
# v3.0.6 扩到 30 条，按「这件事属于哪一类」标注，责任（工作/照护/宠物）与社交两类
# 只在人设里确实提到时才发（见 examples_for）：范例是最容易被照着抄的东西，一条
# 「给猫添粮」的范例递给没有猫的角色，就是在替她编一只猫。
EXAMPLE_BANDS: tuple[tuple[dict[str, Any], ...], ...] = (
    # 短：十几二十分钟就完事，做完自然换下一件
    (
        {"event": "洗了把脸，把厨房台面擦了一遍", "location": "厨房", "emotion": "清爽了点", "energy_rate": -0.02, "minutes": 20},
        {"event": "把攒着的几条消息回完", "location": "客厅", "emotion": "清掉一件事", "energy_rate": -0.02, "minutes": 25},
        {"event": "下楼走了一圈，顺手把垃圾扔了", "location": "楼下", "emotion": "透了口气", "energy_rate": 0.02, "minutes": 25},
        {"event": "烧了壶水，站在窗边慢慢喝完", "location": "厨房", "emotion": "缓了缓", "energy_rate": 0.01, "minutes": 10},
        {"event": "把桌上的东西归了归位", "location": "书桌前", "emotion": "眼前清爽", "energy_rate": -0.01, "minutes": 15},
        {"event": "给猫添了粮，把水碗洗了", "location": "厨房", "emotion": "顺手的事", "energy_rate": -0.01, "minutes": 10, "needs": ("pet",)},
        {"event": "回了几条工作上的消息，把明天要用的东西理出来", "location": "书桌前", "emotion": "先清掉一件", "energy_rate": -0.03, "minutes": 25, "needs": ("job",)},
        {"event": "把家里要添的东西列了张单子", "location": "客厅", "emotion": "心里有数", "energy_rate": -0.01, "minutes": 15, "needs": ("care",)},
    ),
    # 中：要坐下来做、要动手的一件完整的事
    (
        {"event": "做了点吃的，吃完把锅洗了", "location": "厨房", "emotion": "踏实", "energy_rate": 0.04, "minutes": 55},
        {"event": "把手头的事往下推了一段，中间歇了一次", "location": "书桌前", "emotion": "有点进展", "energy_rate": -0.05, "minutes": 75},
        {"event": "洗了个澡，把头发吹干，换了身衣服", "location": "浴室", "emotion": "松下来", "energy_rate": 0.05, "minutes": 45},
        {"event": "去楼下超市把缺的东西补上", "location": "超市", "emotion": "例行公事", "energy_rate": -0.06, "minutes": 50},
        {"event": "把攒下的衣服洗了晾好，顺手把地也扫了", "location": "家里", "emotion": "屋里清爽了", "energy_rate": -0.05, "minutes": 60},
        {"event": "把手上的活儿收了个尾，顺手记下明天要接的地方", "location": "书桌前", "emotion": "告一段落", "energy_rate": -0.06, "minutes": 80, "needs": ("job",)},
        {"event": "带狗下楼转了一圈，回来给它擦了脚", "location": "楼下", "emotion": "被拽着走", "energy_rate": -0.04, "minutes": 40, "needs": ("pet",)},
        {"event": "收拾了一遍屋子，把该洗的泡上了", "location": "家里", "emotion": "屋里清爽了", "energy_rate": -0.05, "minutes": 60, "needs": ("care",)},
        {"event": "跟人聊了一阵，把要说的事说完了", "location": "客厅", "emotion": "说开了", "energy_rate": -0.03, "minutes": 45, "needs": ("social",)},
    ),
    # 长：要出门、要等人、要占掉整块时间
    (
        {"event": "出门跑了一趟，来回都在路上，事情办完才回来", "location": "外面", "emotion": "总算办完", "energy_rate": -0.09, "minutes": 130},
        {"event": "把一件要花整块时间的事认真做了一轮", "location": "书桌前", "emotion": "顾不上别的", "energy_rate": -0.07, "minutes": 110},
        {"event": "去了一趟不算近的地方，办完事在外面吃了饭才回", "location": "外面", "emotion": "有点累", "energy_rate": -0.08, "minutes": 150},
        {"event": "把家里从上到下收拾了一遍，中途没停", "location": "家里", "emotion": "累但痛快", "energy_rate": -0.08, "minutes": 120},
        {"event": "一头扎进手头那件事，做完才抬头看时间", "location": "书桌前", "emotion": "耗掉了半天", "energy_rate": -0.09, "minutes": 140, "needs": ("job",)},
        {"event": "出去跟人见了一面，回来的路上顺道买了东西", "location": "外面", "emotion": "见完有点空", "energy_rate": -0.07, "minutes": 150, "needs": ("social",)},
    ),
    # 歇着：什么都不干也是一种状态，但它是「歇着」，不是一个动作
    (
        {"event": "靠在椅背上放空，什么也没干", "location": "客厅", "emotion": "什么都没想", "energy_rate": 0.03, "minutes": 30, "wants": True},
        {"event": "戴上耳机听歌，躺着没动", "location": "卧室", "emotion": "缓过来一点", "energy_rate": 0.04, "minutes": 40, "wants": True},
        {"event": "坐在窗边看外面，发了一会儿呆", "location": "窗边", "emotion": "空落落的", "energy_rate": 0.02, "minutes": 30, "wants": True},
        {"event": "躺着刷手机，什么正事也没做", "location": "卧室", "emotion": "有点空", "energy_rate": 0.02, "minutes": 50, "wants": True},
        {"event": "泡了杯东西，慢慢喝完", "location": "客厅", "emotion": "缓下来了", "energy_rate": 0.02, "minutes": 20, "wants": True},
        {"event": "把要做的事往后推了推，先缓了一会儿", "location": "客厅", "emotion": "不想动", "energy_rate": 0.03, "minutes": 40, "needs": ("job",), "wants": True},
        {"event": "猫凑过来，就顺手摸了半天", "location": "客厅", "emotion": "被哄好了", "energy_rate": 0.03, "minutes": 30, "needs": ("pet",), "wants": True},
    ),
)

# 扁平的池子：测试和别处要「所有范例」，不关心分档。
EXAMPLE_POOL: tuple[dict[str, Any], ...] = tuple(item for band in EXAMPLE_BANDS for item in band)
EXAMPLE_COUNT = len(EXAMPLE_BANDS)


# 递进 prompt 的范例只带输出契约里的字段：`needs` 是代码侧的适用条件，
# 递过去等于告诉模型「你可以有宠物」。
_EXAMPLE_FIELDS = ("event", "location", "emotion", "energy_rate", "minutes", "wants")

# 生活条件词表搬到了 life_material（日程素材要用同一套判据）：两处各写一份，迟早
# 有一处走偏。这里只保留这个名字——范例过滤与素材筛选认的是同一张表。
# 它只用来**过滤**，不参与事实判定（那是 invented_facts 的事）。


def example_capabilities(text: str) -> set[str]:
    """从「她是谁」的原文里认出她确实有的生活条件（工作/宠物/照护/社交）。"""
    body = str(text or "")
    return {name for name, words in _EXAMPLE_NEEDS if any(word in body for word in words)}


def _example_allowed(item: Mapping[str, Any], caps: set[str]) -> bool:
    needs = item.get("needs") or ()
    if isinstance(needs, str):
        needs = (needs,)
    return all(need in caps for need in needs)


def _example_evidence(persona: Persona | None, cfg: HumanoidConfig | None = None) -> str:
    """抽范例时可用的「她是谁」原文：人设正文 + 用户补充偏好（都没有就是空串）。"""
    parts = []
    if persona is not None:
        parts.append(str(getattr(persona, "prompt", "") or ""))
    if cfg is not None:
        parts.append(str(getattr(cfg, "schedule_prompt_extra", "") or ""))
    return "\n".join(part for part in parts if str(part).strip())


def examples_for(
    seed: list[Any] | None,
    persona_text: str = "",
    count: int = EXAMPLE_COUNT,
) -> list[dict[str, Any]]:
    """按（角色+日期）抽一批范例：每档一条，同一天稳定、不同天不同。

    每档抽一条而不是整池洗牌，是为了让模型每次看到的范例都同时有短的、有长的——
    只看得到一档，它会把当天所有事都往那个时长上凑。

    `persona_text` 是「她是谁」的原文：池子里责任（上班/照护/宠物）与社交两类范例
    只在她的人设里确实提到这些时才发——范例是最容易被照着抄的东西，一条「给猫添粮」
    递给没有猫的角色，就是在替她编一只猫。没有依据时退回不依赖身份条件的通用条。
    """
    caps = example_capabilities(persona_text)
    rnd = random.Random("|".join(str(x) for x in (seed or [])) or "default")
    picked: list[dict[str, Any]] = []
    for band in EXAMPLE_BANDS:
        pool = [item for item in band if _example_allowed(item, caps)]
        if len(pool) < 2:
            # 这一档有依据的太少：补上通用条，别让某一档从今天的范例里消失
            # （少了哪一档，模型对那一档时长的判断就跟着塌掉）。
            pool = [item for item in band if not item.get("needs")] or list(band)
        picked.append(rnd.choice(pool))
    rnd.shuffle(picked)
    return picked[: max(1, int(count))]


def _render_examples(items: list[dict[str, Any]]) -> str:
    rows = []
    for item in items:
        clean = {key: item[key] for key in _EXAMPLE_FIELDS if key in item}
        rows.append("   · " + json_dumps_example(clean))
    # continue 是格式的一部分，不是活动范例——每次都给。
    rows.append('   · {"continue": true}')
    return "\n".join(rows)


def json_dumps_example(item: dict[str, Any]) -> str:
    import json

    return json.dumps(item, ensure_ascii=False)


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
    ongoing: list[str] | None = None,
    is_night: bool = False,
    interrupted: bool = False,
    # 刚刚发生的事够重（被夸/被骂/被敷衍）——一句「她经历了什么」。
    # 与 interrupted 分开：那个是「有人来过」，这个是「那句话是什么分量」。
    event_phrase: str = "",
    # 上一段是不是睡眠：决定要不要提醒她「刚醒，不必马上起」（C1 的赖床规则）。
    prev_sleep: bool = False,
    # 困意是否已经越线（>= CHANGE_TRIGGER_SLEEP_PRESSURE）。调用方从身体快照算，
    # 这里不重复取数——prompt 里那句「困意已经 80+/100」必须和本地兜底同一条线。
    body_pressure: bool = False,
    weather: str = "",
    holiday: str = "",
    day_frame: str = "",
    seed: list[Any] | None = None,
    want_day_frame: bool = False,
    # 被打断之前她在做的那件事（挂起快照）：给模型一个「回去接着做」的选项。
    # 默认 None：旧调用一个字都不变。
    suspended: Slot | dict | None = None,
    # 这次必须换一件事（`/拟人 日程打断`）：不许 continue 原地接着做。
    force_switch: bool = False,
    # 她睡着期间发生过什么（#66）：睡着那一段不重掷，事件与来访以前就在保鲜期里
    # 过期了。醒来这一段把那些短句递进去，听没听见由她判断。默认 None：旧调用不变。
    heard_asleep: list[str] | None = None,
    # 今天的素材（v3.0.10）：这个钟点她可能做的事，已按人设与近期去重筛过。
    # 只给可能性，不给「所以该做什么」——挑哪件、做多久还是她的事。
    materials: str = "",
    # 今天已经睡了多少（分钟）：判断这一段要不要睡时的客观输入。0 表示不给这一段。
    day_sleep_minutes: int = 0,
    # 过去几周她自己长出来的习惯（v3.0.10）：只在她真的重复过时才给，空串表示没有。
    habits: str = "",
    # 她自己在对话里说过的「今天要办的事」（v3.0.10）：只认带明确日期/时间的计划，
    # 已经从原话里抽好、按天存过。空表表示没有——那就只当普通的一天。
    plans: list[str] | None = None,
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
            f"（{(prev or {}).get('location', '')}），到现在已经 {int(elapsed_minutes)} 分钟。"
        )
        # 这一段预计什么时候结束、还剩多久：要判断「接着做还是换一件」，得先知道
        # 手上这件事还剩多少时间。旧 prompt 只说了已经做了多久。
        _start_minute = parse_time((prev or {}).get("start"))
        _end_minute = parse_time((prev or {}).get("end"))
        if _start_minute is not None and _end_minute is not None:
            _remaining = max(0, _end_minute - (_start_minute + int(elapsed_minutes)))
            prev_line += f"这一段预计到 {format_time(_end_minute)}（还剩 {_remaining} 分钟）。"
        prev_line += "\n"
    else:
        prev_line = "这是今天的第一次决定，手上还没有正在做的事。\n"

    rows = []
    for slot in (history or [])[-SEGMENTS_PROMPT_HISTORY:]:
        if not isinstance(slot, dict):
            continue
        rows.append(
            f"{slot.get('start', '')}-{slot.get('end', '')} "
            # 存的是 48 字以内（#156），递进 prompt 的这部分仍削到 24：一行一条，长了读不动。
            f"{_clip_event(str(slot.get('event') or ''))}（{slot.get('location', '')}）"
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
    # 有人突然找她说话/事情临时插进来：给模型一个「现在可以改主意」的时机，
    # 但改不改仍由它按人设与身体判断——不替它决定"被打断了就一定得停下手里的"。
    interrupt_line = (
        "【刚刚发生了个小插曲】刚才有人来找她说话、或者有件小事插了进来。"
        "她可以放下手上的事去应付，也可以先继续做、回头再说——看哪样更像她。\n\n"
        if interrupted else ""
    )
    # 别人刚说的话够重：这是用户的话唯一能进日程的路。
    # 只递「她经历了什么」这一句事实，不写「所以她应该……」——
    # 停下、改主意、还是装作没往心里去都由她自己定（test_v2151_freedom 的规矩）。
    event_line = (
        f"【刚刚的事】{event_phrase}。"
        "这件事可能让她想放下手上正在做的，也可能让她更有劲头做完，"
        "也可能她压根没往心里去——看她是谁，也看这件事的分量。\n\n"
        if event_phrase else ""
    )
    # 她睡着的时候发生过什么（#66）：睡着那段不重掷，事件与来访以前会在保鲜期里
    # 悄悄过期，一觉醒来她什么都不知道。只递事实（谁说了什么、有人来过），
    # 不写「所以醒来该……」——听没听见、要不要提起都由她判断。
    _heard = [
        str(x).strip() for x in (heard_asleep or []) if str(x or "").strip()
    ][:ASLEEP_HEARD_MAX]
    asleep_heard_block = ""
    if _heard:
        asleep_heard_block = (
            "【她睡着的时候】\n"
            + "\n".join(f"  - {x}" for x in _heard)
            + "\n这些发生在她睡着期间：她可能压根没听见，也可能是被这些弄醒的——"
            "要不要提起、要不要因此改变这一段，都由她判断。\n\n"
        )
    # `suspended`：被打断之前她在做的那件事。以前中断之后原来在做的事没有任何
    # 记录，回不去——用户原话「所谓的保鲜期，就不是可以中途打断换一个，或在
    # 当前日程接下去的」。这里只把那段事实递进去，回不回去由她按人设决定。
    _susp = suspended if isinstance(suspended, Mapping) else {}
    _susp_event = str((_susp or {}).get("event") or "").strip()
    suspended_block = ""
    if _susp_event:
        try:
            _susp_left = max(0, int(float((_susp or {}).get("remaining_minutes", 0) or 0)))
        except (TypeError, ValueError):
            _susp_left = 0
        _susp_lines = [
            f"【被打断之前她在做「{_clip_event(_susp_event)}」（还剩 {_susp_left} 分钟）】"
        ]
        _susp_location = str((_susp or {}).get("location") or "").strip()
        _susp_emotion = str((_susp or {}).get("emotion") or "").strip()
        _susp_ongoing = str((_susp or {}).get("ongoing") or "").strip()
        if _susp_location:
            _susp_lines.append(f"当时在：{_susp_location}")
        if _susp_emotion:
            _susp_lines.append(f"当时的心情：{_susp_emotion}")
        if _susp_ongoing:
            _susp_lines.append(f"当时的进度：{_susp_ongoing}")
        _susp_lines.append(
            "如果她这会儿想回去接着做这件事，输出 {\"resume\": true} 就行，"
            "不用再写 event/location/emotion（minutes 按剩下的时间算）；"
            "不想回去就正常给一件新的事。"
        )
        suspended_block = "\n".join(_susp_lines) + "\n\n"
    # `/拟人 日程打断`：这次是**换一件事**，不是「接着做」。这句话必须写死在
    # prompt 里——不靠模型自己从「插曲」那段读出「也可以继续」。
    switch_block = (
        "【这次是换一件事】刚才有人明确叫停了她手上的事：这一段必须是**另一件**事，"
        "不能 continue=true 接着做手上这件。\n"
        "   就算你觉得她本该继续，也要给出一个具体的新事件（event/location）。\n\n"
        if force_switch else ""
    )
    # 「还没做完的事」——她要能有一件跨天的事，今天接着昨天做。
    _ongoing = [str(x).strip() for x in (ongoing or []) if str(x or "").strip()][:3]
    _ongoing_block = (
        "【你还没做完的事（可以接着做，也可以先放一放）】\n"
        + "\n".join(f"  - {x}" for x in _ongoing) + "\n"
        if _ongoing else ""
    )
    # 外界（天气/节日）：她身处的环境。只递事实，不说「所以该做什么」——
    # 下雨要不要改行程、过节要不要出门，由她按人设自己决定。
    world_lines = []
    if weather:
        world_lines.append(f"- 外头：{weather}")
    if holiday:
        world_lines.append(f"- 今天：{holiday}")
    world_block = (
        "【今天的外界】（她身处的环境，安排这一段时可参考，不是必须围绕它来）\n"
        + "\n".join(world_lines) + "\n\n"
        if world_lines else ""
    )
    # 今天的大致样子：每天开头定一句，后续每段都带着它，给「最安全的平均」一个锚。
    frame_block = (
        "【今天大概是这样的】（一整天的大致样子，安排这一段时参考，别当任务清单）\n"
        f"{day_frame}\n\n"
        if day_frame else ""
    )
    # 今天的素材：把「这个钟点她可能做的事」摆出来。没有这一段时模型手里只有人设 +
    # 四条范例，能挑的事太少，于是天天那几件（睡、吃、收拾、看手机）——这不是模型
    # 笨，是素材不够。明写「不是清单」：素材只扩大可能性，不替她决定。
    materials_block = (
        "【今天她可能做的事】（这个钟点她这样的人可能会做的事，不是清单）\n"
        + materials + "\n"
        "  可以挑一件、可以改成更贴她的、也可以一件都不做；做多久按这件事本身算，"
        "不用逐条回应。\n\n"
        if materials else ""
    )
    # 她自己的习惯（v3.0.10）：过去几周她这个钟点常做什么、一般几点起睡。
    # 用户原话：「没有从她过去几周日程长出来的习惯」——生成侧以前只看得到今天和
    # 昨天，每天从零挑事，于是天天那几件。习惯只说明她偏向怎么过，不替她决定今天。
    habits_block = (
        "【她这段时间的习惯】（过去几周她自己的日程里反复出现的样子，不是今天必须照做）\n"
        + habits + "\n"
        "  习惯说明她平时偏向怎么过，今天可以有例外；也别为了贴合习惯把今天排成昨天的复制。\n\n"
        if habits else ""
    )
    # 她今天确实要办的事（v3.0.10）：只来自对话里带明确日期的原话片段。
    # 这一段补的是「今天跟她有关」这一层——没有它，模型只能让她一个人在屋里绕圈。
    # 明写「不必全做、也别当清单」：事实是约束，不是任务书。
    _plans = [str(x).strip() for x in (plans or []) if str(x or "").strip()][:3]
    plans_block = (
        "【她自己说过今天要办的事】（她或对方在对话里提到、带明确日期的原话片段）\n"
        + render_plans(_plans) + "\n"
        "  这些是她今天确实要面对的，安排这一段时按时间顺序带上；"
        "没到点就先做别的，别把一整天都围着它们排。\n\n"
        if _plans else ""
    )
    # 今天睡了多久：中午睡不睡、下午还困不困，得先知道她已经睡过多少。
    sleep_block = (
        "【今天睡了多少】\n"
        f"- 今天到现在她已经睡了 {int(day_sleep_minutes)} 分钟。\n\n"
        if int(day_sleep_minutes) > 0 else ""
    )
    # 困意越线：调用方传的 flag 优先；没传时这里自己从 body 读一次（C1 要求
    # 「body_pressure 或困意 >=80」都算），两条线必须和 `CHANGE_TRIGGER_SLEEP_PRESSURE` 同值。
    try:
        _pressure = float((body or {}).get("sleep_pressure", 0.0) or 0.0)
    except (TypeError, ValueError):
        _pressure = 0.0
    pressure_crossed = bool(body_pressure) or _pressure >= CHANGE_TRIGGER_SLEEP_PRESSURE
    # 「怎么决定」从 9 起顺延。9/10 是每个角色都该看到的规则（凌晨也是她的一天、
    # 勤快不是默认值）；后面两条只在对应处境下出现，编号顺延，免得平时白占 prompt。
    rule_no = 9
    extra_rules = (
        # 当天去重（v3.0.6）：history_block 早就把今天过完的时段列出来了，但从没说过
        # 「别再排一遍」。于是「上午在厨房弄吃的」到下午能再出现一次——看起来不是
        # 她过日子，是模型在填表。
        f"{rule_no}. 上面【今天到这会她已经过完的时段】里出现过的事，别原样再排一遍；"
        "同一类事一天最多两段（是同一件事接着做就 continue，不算新的一段）。\n"
    )
    rule_no += 1
    extra_rules += (
        f"{rule_no}. 00:00-06:00 也是完整的一天，不是天然的睡觉时段：\n"
        "   人设是夜猫子/夜班/白天补觉的，凌晨可以排夜里的活动"
        "（出门、值班、打游戏、写东西、散步、吃夜宵）；\n"
        "   别因为过了零点就默认她在睡。早起型的人凌晨在睡是正常的。\n"
    )
    rule_no += 1
    extra_rules += (
        f"{rule_no}. 勤快不是默认值：人设没写她勤快/自律，就别把一天排满工作、学习、家务；\n"
        "   懒散、拖延、躺一天、刷手机、临时改主意都是正常的一天。\n"
    )
    if prev_sleep:
        rule_no += 1
        extra_rules += (
            f"{rule_no}. 她刚醒，上一段是睡眠：不必马上起。\n"
            "   没急事、睡得不够、或人设不勤快，可以先赖床、回笼觉、"
            "在被窝里刷一会儿手机——这也是她的一天。\n"
        )
    if pressure_crossed:
        rule_no += 1
        extra_rules += (
            f"{rule_no}. 困意已经 80+/100：这一段优先睡一会儿/闭眼休息；\n"
            "   除非人设里有非做不可的理由（值班、赶工），"
            "否则别排做饭、工作、出门、社交这类要精神的事。\n"
        )
    # 睡够了还睡：原来「中午」在模型眼里是默认的睡觉点，一天能睡出两三段。真正
    # 缺觉时上面那条（困意越线）已经说了；这里管的是另一头——今天已经睡够了、
    # 困意也没越线，就别再排睡，把这一段给她别的日子。
    if int(day_sleep_minutes) >= NAP_REDUNDANT_MINUTES and not pressure_crossed:
        rule_no += 1
        extra_rules += (
            f"{rule_no}. 今天她已经睡过 {int(day_sleep_minutes)} 分钟，困意也没越线："
            "这一段别再排睡。\n"
            "   睡觉是「实在没别的事可做」时的默认值，不是中午的固定一站——"
            "不困就做点别的（上面那些素材里挑一件，或者她自己想做的事）。\n"
        )

    return (
        persona_block(persona, cfg.schedule_persona_max_chars)
        # 人设线索紧跟在人设原文后面：模型先读到「她是谁」，再读从这段文字里
        # 认出来的作息与劲头，两段不会隔着别的规则各说各的。
        + persona_traits_block(persona)
        + f"\n现在是 {now_text}（星期{weekday}）。请决定她【从现在开始的这一段】在做什么——"
        "只这一段：之后的事到了时候会再决定，现在不要排。\n\n"
        + prev_line
        + "\n"
        + body_reference_block(body, now_text)
        + frame_block
        + sleep_block
        + materials_block
        + habits_block
        + plans_block
        + world_block
        + history_block
        + extra_block
        + night_line
        + interrupt_line
        + event_line
        + asleep_heard_block
        + switch_block
        + suspended_block
        + "【怎么决定】\n"
        "1. 身体读数是决策输入：饿到不行就去吃，困到不行就去睡——怎么权衡由你决定。\n"
        "2. 一件事做到一半接着做很正常（continue=true）；做很久了、或身体不允许时就换。\n"
        # 「怎么决定」原来只有上面两条，全是「什么时候该换」。这等于只教了它何时停下，
        # 没教它一段能有多短——于是模型把每段都当成一件大事，minutes 天然偏长。
        # 改的时候又踩了第二个坑：为了教「短」，把「喝口水、吃两口」写成了 15~30 分钟的段，
        # 等于告诉它一个动作也能占半小时；而 _segment_minutes 那边又有 15 分钟硬下限，
        # 两处一叠，「在沙发上喝东西」就必然被写成 30 分钟的一段。
        "3. minutes 要真的反映这件事要多久——是这件事本身占多久，不是把这一段时间填满：\n"
        "   一件事十几二十分钟就做完，就写十几二十分钟，做完自然换下一件；\n"
        "   要坐下来做、要出门、要等人的事才写到 1~3 小时；\n"
        "   一个动作不算一件事：喝一口水、看一眼手机、回一句话撑不起一段，别单独成段——\n"
        "   要么接着上一段（continue），要么写成真的在做的样子（在厨房弄点吃的，而不是「喝东西」）；\n"
        "   一次出门可以是一整段（路上→店里→回来），也可以中途换成另一件事。\n"
        "   **别因为「反正要填一段时间」就往长的填。**\n"
        # 粒度变细之后，段与段之间「接不接得上」就比时长更要紧了：20 分钟一段，
        # 一天就是几十段，任何一次跳跃都会被看见。数据层已经防住了时间重叠和窟窿，
        # 但「上一段在游泳、下一段在另一条街的书店里」这种语义跳跃只能靠这里说。
        "4. 换一件事要接得上：\n"
        "   上一段在哪儿、刚做完什么，上面给了。换地方最好有个说得过去的由头"
        "（吃完了→出门、写到一半→起身倒水），别一段之内从一个地方跳到另一个；\n"
        "   在同一个地方待着通常就是接着做，用 continue=true 就好。\n"
        # 「中途被打断/临时起意」以前完全没有：每段都像一件排好的任务，
        # 做完了才换。真人不是这样的——她会被事情打断、会临时想干点别的。
        "5. 她的一天是可以被打断的：\n"
        "   能被一件小事打断（快递到了下楼取、朋友顺路叫住、锅里水开了、看见一条想回的消息）；\n"
        "   也可以临时起意（突然想出去走走、拐进一家店、给谁打个电话）——\n"
        "   这种段不用长，十几到四十分钟都正常，做完再回去继续原来那件事也行。\n"
        "6. 别把一天排成流程模板（起床→上班→午休→下班→晚饭→睡觉那一条龙），"
        "每天也不该长得差不多。**午睡/小憩不是每天必须有的一站**："
        "没到那个困劲就别排睡，改成歇会儿、发发呆、听两首歌也行。\n"
        # ongoing：让她能有一件**跨天**的事。没有它，她的每一天都是互不相关的——
        # 今天改方案、明天改方案，两边没有「还没弄完」这层，每段都像新任务。
        "7. 一天里可以有一两段是**她自己想做的**——不是工作、不是必须，是她自己挑的"
        "（想去哪家店、想去看什么、想学点什么）。这种时候在 JSON 里带上 \"wants\": true。"
        "别把整天都排成工作，她得有点自己的日子。\n"
        "8. ongoing 只在这件事**要跨好几天**时才写（一份要整理三天的个案笔记），"
        "一次能做完的别写。写了它就一直跟着你，明天你也会接着这件事——"
        + (_ongoing_block or "（目前没有还没做完的事。）") + "\n"
        + extra_rules
        + "\n"
        "【输出格式】\n"
        "1. 只输出一个 JSON 对象，不要 Markdown 代码块，不要解释文字。\n"
        # 这里原来只有一个占位式示例 {"event": "<她在做的事>"}，理由写的是
        # 「模型对 few-shot 的模仿远强于对抽象规则的服从，会被当成范式反复复用」。
        # 那条顾虑是对的，但**矫枉过正**：把画面全删掉之后，模型手里就只剩人设 + 一堆
        # 规则 + 一个空模板，于是每段都挑最平庸的那件事，一天下来高度雷同。
        #
        # 所以给回几个**有画面、长度和内容都不一样**的范例，并明说这是举例不是填空。
        # 长度不一这件事本身就是示范：它告诉模型 22 分钟和 2 小时都是对的。
        "2. 下面是几个**举例**，看格式和详略，不是让你照抄，也不是填空题：\n"
        + _render_examples(examples_for(seed, _example_evidence(persona, cfg)))
        + "\n"
        # 范例只示范「一段话该长成什么样」。范例里的事跟她是谁无关——原来那批范例
        # 是带身份的（书店、个案笔记、朋友电话），模型照着抄，所有角色就都长成同一个人。
        "   这几条只示范形状和粒度：一条要具体到这个程度、长短要差这么多；\n"
        "   里面的事跟她是谁无关，别照着抄——她做什么要从人设和她今天的处境里长出来。\n"
        "   event 写具体在做什么、location 写她在哪，两者都要具体到能说出口的程度；\n"
        "   不要写「翻个身」「换个姿势」这种没有信息量的微动作——睡眠就直接写「睡眠」，\n"
        "   其它事要是一件说得出口的事（做了什么、去了哪），不是姿势或状态。\n"
        "3. continue：true = 接着做手上这件事（event/location/emotion 留空，速率沿用）；"
        "false = 换一件事。\n"
        + ("   这次**不能** continue=true：必须给出一件新的事。\n" if force_switch else "")
        + (
            "   如果上面给了【被打断之前】那一段，想回去接着做时输出 {\"resume\": true}。\n"
            if _susp_event else ""
        )
        + f"4. minutes：这一段做多久。**{SEGMENT_MIN_MINUTES} 分钟也是一段**——洗把脸、"
        "倒杯水、回条消息本来就是几分钟的事，写几分钟就行，别为了填满时间把它抬长；"
        "要坐下来做、要出门、要等人的事才写到 1~3 小时；"
        f"一般的事最长 {SEGMENT_MAX_MINUTES} 分钟；睡觉可以睡一整夜（最长 "
        f"{SEGMENT_SLEEP_MAX_MINUTES // 60} 小时，跨过半夜也没关系）。"
        "这个是**上下限，不是目标值**。\n"
        "5. energy_rate 是这一段对精力的作用：睡眠/休息为正（0.05~0.2），"
        "工作/外出/社交为负（-0.05~-0.15）。\n"
        # 梦：夜里 2 条、白天睡的那一觉 3 条。不是睡眠就不写这个字段——写空数组
        # 会让契约/社交层以为「她做了个没有内容的梦」。
        "6. 如果这一段是睡眠，额外给 \"dreams\" 字段：普通夜里睡给 2 条梦，"
        "白天（06:00-20:00）睡的这一觉给 3 条；\n"
        "   每条 15 字以内，从她最近经历、惦记的事（上面「还没做完的事」、今天的时段）"
        "里长出来，不要解释梦、不要写「梦见」。不是睡眠就不要写 dreams。\n"
        + (
            # 当天第一次决定时顺带把「今天大概是什么日子」定下来（不另开一次调用）。
            "7. 这是今天第一次决定：再多给一个 \"today\" 字段——一句话说今天大概是什么日子"
            "（30 字内，不要引号，别排任务清单）。\n"
            if want_day_frame else ""
        )
        + "\n"
        # 作息那条排在 extra_rules 后面，编号必须跟着 rule_no 走：写死一个数会与
        # 「怎么决定」里已有的条目重号（原来写死 6，与第 6 条重号；改成动态顺延）。
        + routine_prompt(cfg, rule_no + 1)
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


def _truthy_flag(value: Any) -> bool:
    """模型返回的布尔标记：真布尔与 "true"/"1"/"是"/"继续" 这类字符串都认。"""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "y", "是", "继续", "回去")
    return bool(value)


def _force_switch_raw(raw: Any, prev: Slot | None) -> Mapping[str, Any] | None:
    """`/拟人 日程打断` 的解析：只接受「另一件事」。

    - `continue=true`：忽略原意，改成「它明确给出的那件新事」；没给新事件
      （或给的就是手上这件）就跳过——这次不算换事。
    - `resume=true`：允许（回去接着做被打断之前那件事），交给 `parse_segment`。
    - 普通回复：event 必须与手上这件不同。
    全都不满足返回 None：调用方按失败处理（不能原地续）。
    """
    previous_event = str((prev or {}).get("event") or "").strip()
    items = raw if isinstance(raw, list) else [raw]
    for item in items:
        if not isinstance(item, Mapping):
            continue
        candidate = dict(item)
        if _truthy_flag(candidate.get("resume")):
            return candidate
        event = str(candidate.get("event") or "").strip()
        if _truthy_flag(candidate.get("continue")):
            if not event or event == previous_event:
                continue
            # 改判：模型说了 continue，但它给了一件新的事——按新事件排。
            candidate["continue"] = False
            return candidate
        if not event:
            continue
        if previous_event and event == previous_event:
            continue
        return candidate
    return None


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
                elif now_minute - end >= DAY_MINUTES // 2:
                    # 过去得够远（比如现在是 00:30，end 写 06:00 之前的钟点），
                    # 才可能真的是「明天那个点醒」。
                    end += DAY_MINUTES
                else:
                    # 钟点写错：现在 14:00，end 却写 13:00。旧写法一律 +24h，再被
                    # 12 小时上限截断，于是一句写错的 end 能让她睡满 12 小时。
                    end = now_minute + SEGMENT_SLEEP_FALLBACK_MINUTES
            minutes = end - now_minute
    if minutes is None:
        minutes = SEGMENT_DEFAULT_MINUTES
    cap = SEGMENT_SLEEP_MAX_MINUTES if sleeping else SEGMENT_MAX_MINUTES
    return max(SEGMENT_MIN_MINUTES, min(cap, minutes))


def _ongoing_stem(text: str) -> str:
    """同一件事的归一键：先剥掉「还差 4 份」这类进度数字，再取前 12 个字。

    模型每次报同一件事会带进度（「还差 4 份」→「还差 2 份」），按字面比会堆成好几条；
    但只取前 6 个字又会把「把那份合同改完」「把那份合同签了」当成一件事互相覆盖。
    数字必须跟着量词一起剥：「第 3 件不同的事」里的 3 是任务身份，剥掉就全归一了。
    """
    stripped = re.sub(r"[0-9０-９]+(?:份|个|页|章|条|张|分钟|小时)", "", text)
    stripped = re.sub(r"[一二三四五六七八九十]+(?:份|个|页|章|条|张|分钟|小时)", "", stripped)
    return stripped.strip()[:12]


# 事实来源闸（v3.0.6）：事件里一旦出现下面这些词，就说明她在做一件「需要人设里
# 确实有」的事。每一条 = (触发词, 依据词)：触发词进了 event、依据词却不在人设与补充
# 偏好里，这一句就是凭空给她加了一个身份/家人/宠物/约定。
#
# 为什么不靠 prompt 保证：prompt 里早就写着「从人设里长出来」，但那是**请求**。
# 模型对着「今天大概是这样的」和一整段人设，照样能写出「下午去接孩子放学」——
# 合法 JSON 只说明格式对。这里只判能客观判定的东西，不判「自不自然」。
_FACT_CHECKS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("猫", "狗", "宠物", "猫粮", "狗粮", "铲屎", "遛狗"),
        ("猫", "狗", "宠物", "仓鼠", "鹦鹉", "兔子", "养了", "养着"),
    ),
    (
        # 「公司」不算触发词：去一趟公司楼下不等于在那儿上班（取快递、办事都会经过）。
        ("同事", "老板", "领导", "客户", "开会", "会议", "工位",
         "上班", "下班", "加班", "汇报", "述职", "项目"),
        ("工作", "上班", "公司", "职业", "同事", "老板", "领导", "客户", "开会",
         "店里", "开店", "老师", "医生", "护士", "程序", "工程师", "学生", "上学",
         "学校", "学业", "自由职业", "画师", "写手", "店员", "服务", "创业", "兼职",
         "实习", "打工", "项目"),
    ),
    (
        ("孩子", "女儿", "儿子", "家人", "妈妈", "爸爸", "爸妈", "老公", "老婆",
         "丈夫", "妻子", "老人", "妹妹", "弟弟", "姐姐", "哥哥"),
        ("孩子", "女儿", "儿子", "家人", "妈妈", "爸爸", "爸妈", "老公", "老婆",
         "丈夫", "妻子", "老人", "妹妹", "弟弟", "姐姐", "哥哥", "家庭", "照护"),
    ),
    (
        ("预约", "约好", "挂号", "复诊", "家长会", "面试", "体检"),
        ("预约", "约好", "挂号", "复诊", "家长会", "面试", "体检", "医院", "看病"),
    ),
)


def invented_facts(event: str, evidence: str) -> list[str]:
    """事件里出现的、依据里找不到出处的身份/关系/约定词（没有就返回空列表）。

    `evidence` 为空时一律返回空：**「人设没读到」不等于「人设里没有这只猫」**，
    没有依据文本就没有判定基准，这时候不判比误判好。
    """
    text = str(event or "")
    basis = str(evidence or "")
    if not text or not basis:
        return []
    hits: list[str] = []
    for triggers, proofs in _FACT_CHECKS:
        if not any(trigger in text for trigger in triggers):
            continue
        if any(proof in basis for proof in proofs):
            continue
        hits.append(next(trigger for trigger in triggers if trigger in text))
    return hits


def _event_key(text: str) -> str:
    """同一件事的比对键：去掉标点与进度数字后取前 12 字（与 `_ongoing_stem` 同口径）。"""
    cleaned = re.sub(r"[，。、；：！？…·\-—~～\s（）()「」“”\"'’]+", "", str(text or ""))
    return _ongoing_stem(cleaned)


def _quality_fix_block(issues: list[str], invented: list[str], reason: str = "") -> str:
    """重写时追加的那一段：只说哪里不对，不重写整份规则（规则已经在上面的 prompt 里）。

    `reason` 是模型自己审查时说出的原因（v3.0.11）：它比任何固定条目都具体，
    原样递回去比让模型猜「到底哪里不对」有效得多。
    """
    lines = ["", "【上一版没通过】"]
    if "repeat" in issues:
        lines.append("- 上面【今天到这会她已经过完的时段】里已经有这件事了，换一件别的。")
    if invented:
        lines.append(
            "- 这件事里的"
            + "、".join(invented)
            + "在她的人设里找不到依据。不要凭空给她加身份、家人、宠物或约定，"
            "换成不需要这些东西也能做的事。"
        )
    if "needless_sleep" in issues:
        lines.append(
            "- 今天她已经睡够了，困意也没越线，这一段别再排睡。"
            "睡觉不是中午的固定一站，换成一件醒着做的事。"
        )
    if "overlong_nap" in issues:
        lines.append(
            "- 白天这一觉太长了。她并不困的时候，小睡最多一两个小时；"
            "醒着做点什么，真的累了再歇。"
        )
    if reason:
        lines.append(f"- {reason}")
    lines.append("只输出一个 JSON 对象，不要解释。")
    return "\n".join(lines) + "\n"


def _slot_minutes(slot: Mapping[str, Any]) -> int:
    """这一段有多长（分钟）。时间读不出来时给 0，调用方按「没写」处理。"""
    lo = parse_time(slot.get("start"))
    hi = parse_time(slot.get("end"))
    if lo is None or hi is None or hi <= lo:
        return 0
    return hi - lo


def _review_prompt(
    *,
    now_text: str,
    prev: Mapping[str, Any] | None,
    slot: Mapping[str, Any],
    persona_text: str,
) -> str:
    """审查这一段的提示词（v3.0.11）：只判「接不接得上」，不合格就顺手改好。

    为什么要有这一步：规则写在生成 prompt 里是「请求」，模型写完不会回头检查。
    而「上一段 09:05 在卧室结束，这一段 09:06 在超市」「倒杯水占了半小时」这种
    错，只有把两段摆在一起才看得见。判不通过时不降级成中性段，而是让它按自己
    说出的原因改一版——判定与修改由同一个模型完成，改完直接用。
    """
    lines = ["【审查这一段】"]
    lines.append(f"现在 {now_text}。")
    if prev:
        lines.append(
            "她刚过完的上一段："
            f"{str(prev.get('event') or '').strip()}"
            f"（{str(prev.get('location') or '家中').strip()}，"
            f"{str(prev.get('start') or '')}–{str(prev.get('end') or '')}）"
        )
    else:
        lines.append("今天还没有上一段。")
    lines.append(
        "这一版生成的下一段："
        f"{str(slot.get('event') or '').strip()}"
        f"（{str(slot.get('location') or '家中').strip()}，"
        f"{str(slot.get('start') or '')}–{str(slot.get('end') or '')}，"
        f"约 {_slot_minutes(slot)} 分钟）"
    )
    lines.append("")
    lines.append("只判一件事：这一段接上去说不说得通。看五点：")
    lines.append("- 时间：上一段结束到这一段开始，够不够她收拾、走路、准备？（她不会瞬移）")
    lines.append("- 地点：从上一段的地点到这里，是不是可能？（在家要去外面得出门，外面回来要进门）")
    lines.append("- 时长：这个动作值不值得这么久？（倒杯水、回一条消息不该占半小时；通勤、做饭、开会才可能长）")
    lines.append("- 事实：有没有凭空多出来的家人、宠物、同事、预约、约定？")
    lines.append("- 重复：是不是把上一段同一件事换个说法又排了一遍？")
    if persona_text.strip():
        lines.append("")
        lines.append("她是谁（判断依据，不要照抄）：")
        lines.append(persona_text.strip()[:600])
    lines.append("")
    lines.append('合格就只回 {"ok": true}。')
    lines.append(
        '不合格就回 {"ok": false, "reason": "一句话说清哪里不对", '
        '"fixed": {"event": "改好的事", "location": "地点", "minutes": 20}}——'
        "能留的都留着，只改不对的地方，时长按这件事本身算。"
    )
    lines.append("只输出一个 JSON 对象，不要解释。")
    return "\n".join(lines)


def parse_segment(
    raw: Any,
    *,
    now_minute: int,
    step: int = 1,
    prev: Slot | None = None,
    suspended: Slot | dict | None = None,
) -> Slot | None:
    """把模型返回的这一段规范化成一个 slot。

    `continue=true` 时沿用上一件事的名字与地点，只往前延长——这是「她决定再做
    一会儿」，不是插件替她改主意。睡觉可以跨过午夜：今天这半截截到 24:00，
    剩下的写进 `carry_end`，由 `_append_segment` 结转到明天。

    `resume=true`（优先级高于 continue）时用 `suspended` 里那件被打断的事重建
    这一段的开头：start = 现在，minutes = 剩余分钟数（下限 SEGMENT_MIN_MINUTES），地点/心情/进度
    沿用。没有 `suspended` 时 resume 不生效，按普通回复解析。
    """
    if isinstance(raw, list):
        raw = _pick_from_array(raw, now_minute)
    if not isinstance(raw, Mapping):
        return None
    prev = prev or {}
    # 回去接着做被打断之前那件事：continue 与 resume 同时出现时以 resume 优先。
    susp = suspended if isinstance(suspended, Mapping) else {}
    suspended_event = _clip_event(str((susp or {}).get("event") or ""))
    want_resume = _truthy_flag(raw.get("resume")) and bool(suspended_event)
    if want_resume:
        # 挂起记录里带着解析时的睡着判定：醒来前被打断的那件事可能没有睡词。
        sleeping = is_sleep_event(suspended_event) or bool((susp or {}).get("sleep"))
        try:
            remaining = int(float((susp or {}).get("remaining_minutes", 0) or 0))
        except (TypeError, ValueError):
            remaining = SEGMENT_DEFAULT_MINUTES
        cap = SEGMENT_SLEEP_MAX_MINUTES if sleeping else SEGMENT_MAX_MINUTES
        minutes = max(SEGMENT_MIN_MINUTES, min(cap, remaining))
        start = _start_after(prev, now_minute, _step_for(minutes, step))
        end = start + minutes
        slot: Slot = {
            "start": format_time(start),
            "end": format_time(min(DAY_MINUTES, end)),
            "event": suspended_event,
            "location": str((susp or {}).get("location") or "").strip()[:16] or "家中",
            "emotion": str((susp or {}).get("emotion") or "").strip()[:12] or "平常",
            # 挂起记录里带着被截断那段的能量速率（#70）：模型 resume 时通常只回
            # resume=true，不给 energy_rate。旧写法默认 0.0，于是「回去接着做」的
            # 那一段对体力零消耗——接着做的是同一件事，速率该沿用。
            "energy_rate": clamp_rate(
                raw.get("energy_rate", (susp or {}).get("energy_rate", 0.0))
            ),
        }
        if (susp or {}).get("status"):
            # 挂起的是兜底给的处境词：标记跟着 event 一起回来，否则渲染时又会被套上「在」。
            slot["status"] = True
        ongoing = str((susp or {}).get("ongoing") or "").strip()[:40]
        if ongoing:
            slot["ongoing"] = ongoing
        if (susp or {}).get("wants"):
            slot["wants"] = True
        if sleeping:
            slot["sleep"] = True
            raw_dreams = raw.get("dreams")
            if raw_dreams is None:
                raw_dreams = (susp or {}).get("dreams")
            slot["dreams"] = _clean_dreams(raw_dreams)
        if end > DAY_MINUTES:
            slot["carry_end"] = format_time(end - DAY_MINUTES)
        return slot
    want_continue = raw.get("continue")
    if isinstance(want_continue, str):
        want_continue = want_continue.strip().lower() in ("true", "1", "yes", "是", "继续")
    want_continue = bool(want_continue) and bool(str(prev.get("event") or "").strip())

    # 原文留着判「是不是睡着」：截断之后，睡字常常正好被切掉（存的是 `_clip_event`
    # 削过的 24 字，判睡一律看原文）。
    raw_event = str(raw.get("event") or "").strip()
    event = _clip_event(raw_event)
    if want_continue:
        raw_event = str(prev.get("event") or "").strip()
        event = _clip_event(raw_event)
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

    sleeping = is_sleep_event(raw_event)
    minutes = _segment_minutes(raw, now_minute, sleeping)
    start = _start_after(prev, now_minute, _step_for(minutes, step))
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
    if want_continue and prev.get("status"):
        # 接着上一段（兜底的处境词）继续：标记一起继承，渲染时才知道这不是动作。
        slot["status"] = True
    if not sleeping and slot_sleeps({**slot, "event": raw_event}):
        # 关键词没命中，但整段就是一段安静的卧床休息（模型写「阖眼，呼吸渐沉」这类）。
        # 按睡着处理，并把这当成睡眠段重算时长上限——不然一夜八小时会被 180 分钟截断，
        # 剩下七小时变成「她醒着在床上躺着」。
        sleeping = True
        minutes = _segment_minutes(raw, now_minute, True)
        end = start + minutes
        slot["end"] = format_time(min(DAY_MINUTES, end))
    if sleeping:
        slot["sleep"] = True
        # 梦只属于睡眠段。`continue=true` 接着睡时模型通常不会把梦再写一遍，
        # 沿用上一段的梦——否则同一觉被拆成两段后，后一段会把梦洗掉。
        raw_dreams = raw.get("dreams")
        if raw_dreams is None and want_continue:
            raw_dreams = prev.get("dreams")
        slot["dreams"] = _clean_dreams(raw_dreams)
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


def _start_after(prev: Slot | None, now_minute: int, step: int) -> int:
    """这一段的起点：向下对齐到刻度，但**不早于上一段的结束**（#65）。

    只看刻度会退回到上一段里：上一段 09:22–10:07（`continue_now` 按配置延长，
    结束点不在刻度上），10:10 决定下一段时向下取整得 10:00，新段起点落进上一段
    中间。`_append_segment` 只好把上一段的 end 收回到 10:00 并当成「被打断」——
    其实那 7 分钟她做完了，`remaining_minutes` 还被算多了 7 分钟挂起来。

    上一段的 end 在「现在」之后（理论上不该发生）时不夹：那样起点会越过此刻。
    """
    start = segment_start(now_minute, step)
    prev_end = parse_time((prev or {}).get("end"))
    if prev_end is not None and prev_end <= now_minute:
        start = max(start, prev_end)
    return start


def _step_for(minutes: int, step: int) -> int:
    """起点对齐用的刻度：比这一段本身还粗的刻度不用。

    15 分钟粒度下，一段 5 分钟的事若也向下对齐到 15 分钟刻度，起点会退回到上一段里
    （或让这一段罩不住此刻），`_append_segment` 只好把上一段的 end 收回来——于是
    「几分钟的事」又被拉成十几分钟。短段直接用现在这一刻当起点。
    """
    try:
        step = max(1, int(step))
    except (TypeError, ValueError):
        step = 1
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return step
    return step if minutes >= step else 1


def _in_window(minutes: int, start: int, end: int) -> bool:
    """分钟数是否落在 [start, end) 窗口内；start > end 表示跨午夜。"""
    if start == end:
        return False
    if start < end:
        return start <= minutes < end
    return minutes >= start or minutes < end


def in_scheduled_night(slots: list[Slot], moment: Any) -> bool:
    """给定时刻是不是在她**日程**推出来的夜里（日程没排睡眠 → False）。

    「现在正处在她的生物钟夜里」这句提示词用它判，不用配置里的固定窗口——
    否则夜猫子在 23:00-06:00 会被按固定窗口告知「这是你的生物钟夜」，去排睡眠。
    """
    window = sleep_window_minutes(slots)
    if window is None:
        return False
    start, end = window
    return _in_window(moment.hour * 60 + moment.minute, start, end)


# 兜底分段里「睡够了」的下限（v3.0.2）。连续睡了这么久（且不少于她需要的睡眠量）
# 之后，即使身体还标着 asleep，兜底也该给一段醒着的时段——否则「睡着」会自我延续：
# 兜底每被问一次就再排一段睡，模型不可用时她能一整天只做睡觉这一件事。
WAKE_AFTER_SLEEP_MIN_MINUTES = 240.0


def _wake_up_minutes(cfg: Any) -> float:
    """兜底判「睡够了」的时长：她需要的睡眠（`sleep_need_hours`），下限 4 小时。"""
    try:
        need = float(getattr(cfg, "sleep_need_hours", 8.0) or 8.0)
    except (TypeError, ValueError):
        need = 8.0
    if not (need > 0) or need != need:
        need = 8.0
    return max(WAKE_AFTER_SLEEP_MIN_MINUTES, need * 60.0)


def dynamic_segment(
    cfg: HumanoidConfig,
    body: dict[str, Any] | None,
    *,
    now_minute: int,
    step: int = 1,
    prev: Slot | None = None,
    seed: list[Any] | None = None,
    asleep_minutes: float = 0.0,
) -> Slot:
    """没有可用模型时的兜底：给的是**处境词 + 中性地点**，不是事件。

    分支的**选择**确实由身体读数决定（饿了走饿、累了走累）——这部分是真的。
    兜底曾经写着 36 条具体事件（“看书做笔记”“下楼买饭团”“叫了份外卖”）和 11 个
    地点（“工位/书房/教室”），它们会经 day_phrases 变成「她今天上午在看书做笔记」这样
    一句**第一人称事实**，而注入块开头写着“以下是她当前的**真实处境**”——等于把编出来
    的一天当成既成事实交给模型。更麻烦的是地点：工位与教室直接预设了她是上班族还是学生，
    退休的人、自由职业者会凭空多出一张工位。

    现在剩下的是**处境词**（“有点饿了”“小睡一会儿”“忙自己的事”“歇着”）与两个中性
    地点（“家里/卧室”）：她饿着、困着、到饭点了、夜里还醒着。具体在做什么、看到什么、
    经历了什么，由她自己说——同文件的 current_slot() 早就写着「拿不到段时给一个中性
    『自由活动』，不编造她在做什么」，这里跟它对齐。

    但 `event` 字段仍必须填（slot schema 与下游渲染都要求非空），填进去的是处境词，
    所以返回的 slot 带 `"status": True`：**它读起来像动作，其实是状态**。下游渲染
    （`day_phrases`）必须据此换成不含「在」的模板——旧代码没这个标记，一律按动作套
    「在」，于是把“有点饿了”拼成「上午在有点饿了」。凡是把这个 event 抄到别处的地方
    （parse_segment 的 continue/resume、`_append_segment` 的挂起记录）都要把标记一起带走。

    措辞仍按 (角色, 日期, 钟点) 抽签，同一时刻不会每天给出同一个答案。

    `asleep_minutes` 是「这一觉已经睡了多久」（调用方从日程里连着的睡段算出来）：
    睡过该睡的量之后就不再续排睡觉，给一段醒着的时段把 `asleep` 这个状态解开。
    """
    body = body or {}
    prev = prev or {}
    hunger = float(body.get("hunger", 0.0) or 0.0)
    pressure = float(body.get("sleep_pressure", 0.0) or 0.0)
    energy = float(body.get("energy", 60.0) or 60.0)
    discomfort = float(body.get("discomfort", 0.0) or 0.0)
    asleep = float(body.get("asleep", 0.0) or 0.0) >= 1.0

    # 夜间窗口只有一份来源（`night_window_minutes`）：关掉夜间模式 / 窗口长度为 0
    # 就是「她没有生物钟夜」，兜底不再退回 23:00-07:00 替她排一觉到早上。
    window = night_window_minutes(cfg)
    in_night = window is not None and _in_window(now_minute, window[0], window[1])

    # 睡够了就该醒。`asleep` 只由「装上的一段不是睡眠」来解（soma 记到非睡段才清
    # asleep_since），而兜底分段每被问一次就再排一段睡：睡着了 → 再排 90 分钟睡 →
    # 仍然睡着 → 再排……模型不可用（provider 冷却、抽不到调用间隔）时她能这样睡一整天，
    # 日程里除了睡觉什么都没有。所以超过该睡的量就主动给醒着的段。
    # 「睡够了」只有一个口径：睡过她需要的量（`_wake_up_minutes`，下限 4 小时）。
    # 旧写法在生物钟夜里还要求「再多睡 3 小时」（`+ 180.0`）才算够：默认窗口
    # 23:00–06:00 只有 420 分钟，而 480+180=660 永远到不了——06:00 该醒的时候
    # 她还在睡，兜底再补 90 分钟写到 07:30（#67）。夜里要不要接着睡由下面的
    # `in_night and pressure` 决定（困意没消就接着睡、消了就醒），不必在这里
    # 再加一条「夜里不准醒」。
    if asleep_minutes >= _wake_up_minutes(cfg):
        asleep = False

    seed_key = list(seed or []) + [now_minute // 60]

    # 身体优先：饿到线就吃，困到线或在夜里就睡，不等哪张表写着。
    if asleep or (in_night and pressure >= LOCAL_NIGHT_SLEEP_PRESSURE):
        if in_night:
            # 睡到生物钟夜结束（她的自然醒点）；跨午夜的部分由结转接到明天。
            night_end = window[1] if window is not None else now_minute
            logical_end = night_end if night_end > now_minute else night_end + DAY_MINUTES
            minutes = max(SEGMENT_MIN_MINUTES, min(SEGMENT_SLEEP_MAX_MINUTES, logical_end - now_minute))
        else:
            # 不在生物钟夜里却还睡着（白天补觉、或生物钟夜里已经睡过点）：再睡多久
            # 按「还差多少才睡够」给，不写死 90 分钟（#67）。默认 need=480、窗口
            # 23:00–06:00（420 分钟）时，06:00 这一觉还差 60 分钟 → 补到 07:00 醒；
            # `sleep_need_hours=7` 的用户 06:00 准时醒。上限仍是 90：滚动分段的语义
            # 是醒了再重新判断，不能一次排一整段白天觉。
            short = int(_wake_up_minutes(cfg) - asleep_minutes)
            minutes = max(SEGMENT_MIN_MINUTES, min(90, short))
        event, location, emotion, rate = "睡一觉", "卧室", "沉睡", 0.15
    elif pressure >= LOCAL_DAY_NAP_PRESSURE and not in_night:
        # 白天困到越线：不能继续排「在厨房煮饭」这种要精神的事，但也不用睡一整天。
        # 45~90 分钟的小睡/补觉，时长按 (角色, 日期, 钟点) 抽签，同一天稳定。
        minutes = random.Random("|".join(str(x) for x in seed_key)).randint(45, 90)
        event = pick("seg_nap", seed_key, ("小睡一会儿", "补觉睡一会儿", "趴着睡一会儿"))
        location, emotion, rate = "家里", "困", 0.12
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
        event = pick("seg_low", seed_key, ("歇着", "发呆", "瘫着"))
        location, emotion, rate, minutes = "家里", "有点累", 0.05, 30
    else:
        # 按钟点给**处境**而不是事件：她这个钟点在忙 / 到饭点了 / 闲下来了，
        # 但具体在做什么不编——那得她自己说
        hour = (now_minute // 60) % 24
        if 7 <= hour < 9:
            event = pick("seg_morning", seed_key, ("醒神", "赖床"))
            emotion, rate, minutes = "清醒中", -0.05, 60
        elif 9 <= hour < 12:
            event = pick("seg_forenoon", seed_key, ("忙自己的事", "手头有东西要处理"))
            emotion, rate, minutes = "专注", -0.07, 90
        elif 12 <= hour < 14:
            event = pick("seg_noon", seed_key, ("吃饭", "吃午饭"))
            emotion, rate, minutes = "放松", 0.08, 45
        elif 14 <= hour < 18:
            event = pick("seg_afternoon", seed_key, ("做事", "忙手里的活"))
            emotion, rate, minutes = "平稳", -0.06, 90
        elif 18 <= hour < 20:
            event = pick("seg_evening_meal", seed_key, ("做饭", "弄吃的"))
            emotion, rate, minutes = "惬意", 0.06, 60
        else:
            event = pick("seg_evening", seed_key, ("歇着", "窝着"))
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
        # 处境词标记：event 里放的是状态不是动作，渲染时不能套「在」（见 docstring）。
        # 它不是模型产出的字段，parse_segment 不认它——只在抄这个 event 的路径上跟着走。
        "status": True,
    }
    if end > DAY_MINUTES:
        slot["carry_end"] = format_time(end - DAY_MINUTES)
    return slot


class ScheduleService:
    """滚动分段日程：未来不预排，每 15 分钟一个「生成或不生成」的决策窗。

    - 到期（这一段过完了 / 今天还没有段）必须生成；
    - 身体跟手上的事明显打架（饿到发慌还在做事、困到不行还醒着）当场生成；
    - 有人打破一段安静来找她（私聊，见 `note_interruption`）：给一次「要不要
      停下手上的事」的时机，整天消息不断的部署里这条自然很少触发；
    - 其余时候按 `schedule_change_chance` 的概率掷骰子：掷中才生成（她临时改
      主意），没掷中就接着做手上的事，一次模型都不调；
    - 睡着的那一段不重掷：一觉睡到这段结束为止，夜里不会被打扰（消息也不算，
      多用户部署里夜里总有人发消息）；跨午夜的一觉由结转（carry）原样接到明天，
      零点不需要再问一次模型。
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
        world_provider=None,
    ) -> None:
        self._scope = scope
        self._config = config_provider
        self._clock = clock
        self._log = logger
        self._spawn = spawn_fn
        self._generating = False
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        # 角色专属素材的生成任务（v3.0.10）：和日程生成分开跑，单飞，不占生成闸。
        self._role_task: asyncio.Task | None = None
        # 上一次「检查她的人设有没有变」的时刻（monotonic）。None = 还没查过。
        # 每次检查都要读一次人设，所以按退避期节流：后台每 30 秒问一次，不节流的话
        # 就是每 30 秒读一次人格库。
        self._role_checked_at: float | None = None
        self.last_error = ""
        self._retry_after = 0.0
        # 上一次 `_generate_segment` 的结局（#75）。`ensure_fresh` 据此判断「这一段没
        # 装上，是因为被节流挡回（事件/来访已在里面原样还回），还是生成真的失败了
        # （取走的事件/来访必须还回去）」。空串 = 还没生成过。
        self._last_outcome: str = ""
        # 质量闸计数（v3.0.6）：repeat=今天重复放行、invented=编造、rewrite=触发重写、
        # fallback=降级成中性段。会话内累计，只报计数不报分数（没有标注就没有分母）。
        self._quality: dict[str, int] = {
            "repeat": 0,
            "invented": 0,
            "rewrite": 0,
            "fallback": 0,
        }
        # 失败退避的计时源：必须可注入，否则测试无法验证「退避窗口内不再投递」。
        self._monotonic = monotonic or time.monotonic
        # 日程得按她是谁来排：递一个 async () -> Persona 进来，不递就不读人设。
        self._persona = persona_provider
        # 身体参考数值的来源：递一个 () -> dict 进来，每次决策现取活值。
        self._body = body_provider
        # 外界（天气/节日）的来源：同样是 () -> dict，每次决策现取，别用开机那一刻的。
        self._world = world_provider
        self.last_persona = ""
        # 概率重估的骰子：每个决策窗只掷一次，掷完的结果本窗内保持不变。
        self._random = random_source or random.random
        self._roll_window = -1
        self._roll_hit = False
        # 待处理的「来访」（有人打破安静来找她）：一次性，refresh_due 消费掉。
        self._interrupt_at = 0.0
        # 上一条私聊消息的时刻 / 上一次「算作来访」的时刻。用于安静间隔与冷却；
        # None = 还没有。**不拿 0 当哨兵**：开机后第一小时 monotonic 还很小，
        # 拿 0 当基准会把第一条来访白白挡掉。
        self._last_message_at: float | None = None
        self._last_interrupt_at: float | None = None
        # 这两个基准跨重启要接着用（#74）：读回来再算安静间隔与冷却。
        self._restore_interrupt_baseline()
        # 待处理的事件（别人刚说的话够重）：一次性，refresh_due 消费掉。
        self._event_at = 0.0
        self._event_phrase = ""
        self._last_event_at: float | None = None
        # 刚装上的一段是不是「回去接着做被打断之前那件事」（resume）：`_append_segment`
        # 要据此清掉挂起、并且不把这次被截断的段再挂起来。
        self._resumed_event = ""
        # 她睡着的时候用户说过的话 / 有人来找过（#66）。睡着时 `_due` 直接返回，
        # 事件与来访就这么在保鲜期里过期——一觉醒来，夜里那几句关心一个字都不剩。
        # 这里把睡着期间发生的收成短句，等醒来那一段的 prompt 里递给她（见
        # `_capture_heard_while_asleep` / `heard_while_asleep`）。只在内存里：
        # 跨重启的「睡着时听到的话」没有意义（那时她早醒了，见 `_woke_at` 同理）。
        self._heard_while_asleep: list[dict[str, Any]] = []

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
            # 梦跟着这一觉跨过午夜：旧结转没有这个键，洗成空列表即可。
            "dreams": _clean_dreams(carry.get("dreams")),
        }

    def sleeping_minutes(self, now_minute: int | None = None) -> float:
        """这一觉已经睡了多久（分钟）；此刻不在睡返回 0。

        兜底分段用它判「睡够了没有」。只数**今天连着**的那几段：模型常把一夜拆成好几段，
        跨午夜的那一截由结转段从 00:00 起算。
        """
        segs = self.segments()
        if not segs:
            return 0.0
        if now_minute is None:
            now = self._clock.now()
            now_minute = now.hour * 60 + now.minute
        index = -1
        for i, slot in enumerate(segs):
            lo = parse_time(slot.get("start"))
            hi = parse_time(slot.get("end"))
            if lo is None or hi is None:
                continue
            if lo <= now_minute < hi:
                index = i
                break
        if index < 0 or not slot_sleeps(segs[index]):
            return 0.0
        start = parse_time(segs[index].get("start"))
        if start is None:
            return 0.0
        total = max(0.0, float(now_minute - start))
        cursor = start
        j = index - 1
        while j >= 0 and slot_sleeps(segs[j]) and slot_sleeps(segs[j + 1]):
            lo = parse_time(segs[j].get("start"))
            hi = parse_time(segs[j].get("end"))
            if lo is None or hi is None or hi != cursor:
                break
            total += float(hi - lo)
            cursor = lo
            j -= 1
        return total

    # 「还没做完的事」最多留 3 件、每件最多记 4 天。
    ONGOING_KEY = "ongoing_tasks"
    ONGOING_MAX = 3
    ONGOING_TTL_DAYS = 4
    # 被打断之前她在做的那件事（只存一件）：中断之后原来在做的事要有个记录，
    # 下一次安排时可以选「回去接着做」。结构：
    # `{"event", "location", "emotion", "ongoing", "wants",
    #   "remaining_minutes", "end_at", "at", "ttl_minutes"}`
    # 新的覆盖旧的；`schedule_pause_ttl_minutes` 之后过期作废（读取时顺手清）。
    # `end_at` 是「原定结束时刻」的 epoch 秒（R5：盘上存时刻，不存会过期的数字）：
    # 挂起快照里那句「还剩 N 分钟」必须按现在现算，否则 TTL 内都播报同一个数字。
    SUSPENDED_KEY = "schedule_suspended"
    # 打断闸门的两个基准（上一条私聊 / 上一次算作来访）也落盘（#74）：只存内存的话，
    # 每次重启后的第一条私聊都会被当成「有人打破安静来找她」。值一律 epoch 秒，
    # 启动时换算回 monotonic 并夹到 ≤ now（时钟被回拨时盘上的时刻可能是「未来」）。
    INTERRUPT_BASELINE_KEY = "schedule_interrupt_baseline"
    # 昨天那份日程存哪儿：跨天时 `daily_schedule` 被今天的第一段覆盖，昨天做了什么
    # 就没了着落。留一份尾部给 `_history_for_prompt` 当「昨天的收尾」。
    PREV_DAY_KEY = "prev_day_schedule"
    PREV_DAY_KEEP = 8

    def _wall_clock(self) -> float:
        """墙钟秒（epoch）。走**注入的时钟**：冻结时钟的测试才验证得到 TTL 过期，
        系统时间跳变时过期判断也跟着同一台钟走。没接时钟时退回 `time.time()`。"""
        clock = getattr(self, "_clock", None)
        try:
            stamp = getattr(clock, "timestamp", None)
            if callable(stamp):
                return float(stamp())
            return float(clock.now().timestamp())
        except Exception:
            return time.time()


    def _restore_interrupt_baseline(self) -> None:
        """从盘上读回打断闸门的两个基准（#74）。

        盘上是 epoch 秒、内存里是 monotonic 秒：同一个瞬间两套刻度差一个常量，
        而重启后 monotonic 归零。所以存「相对现在多久以前」，读回时用现在的
        monotonic 减掉它。盘上的时刻晚于现在（时钟被回拨）就夹到 now——未来的
        基准会让安静间隔/冷却永远判不过去。读不到（旧存档/坏值）就当没有，
        不拿 now 编一个基准出来。
        """
        try:
            raw = self._scope.get_self(self.INTERRUPT_BASELINE_KEY)
        except Exception:
            return
        if not isinstance(raw, Mapping):
            return
        try:
            wall_now = float(self._wall_clock())
            mono_now = float(self._monotonic())
        except Exception:
            return

        def _to_mono(value: Any) -> float | None:
            try:
                ts = float(value)
            except (TypeError, ValueError):
                return None
            if ts <= 0:
                return None
            ts = min(ts, wall_now)
            return mono_now - (wall_now - ts)

        message_at = _to_mono(raw.get("last_message_at"))
        interrupt_at = _to_mono(raw.get("last_interrupt_at"))
        if message_at is not None:
            self._last_message_at = message_at
        if interrupt_at is not None:
            self._last_interrupt_at = interrupt_at

    def _save_interrupt_baseline(self) -> None:
        """把两个基准按 epoch 秒写回（#74）；写失败不该让私聊处理炸掉。"""
        if self._last_message_at is None and self._last_interrupt_at is None:
            return
        try:
            wall_now = float(self._wall_clock())
            mono_now = float(self._monotonic())
        except Exception:
            return

        def _to_wall(value: float | None) -> float | None:
            if value is None:
                return None
            return wall_now - (mono_now - float(value))

        try:
            self._scope.set_self(
                self.INTERRUPT_BASELINE_KEY,
                {
                    "last_message_at": _to_wall(self._last_message_at),
                    "last_interrupt_at": _to_wall(self._last_interrupt_at),
                },
            )
        except Exception:
            pass

    def ongoing_tasks(self) -> list[str]:
        """她手上还没做完的那几件事（跨天跟着她）。"""
        raw = self._scope.get_self(self.ONGOING_KEY)
        if not isinstance(raw, list):
            return []
        items: list[tuple[float, str]] = []
        cutoff = self._wall_clock() - self.ONGOING_TTL_DAYS * 86400.0
        for item in raw:
            if not isinstance(item, dict):
                continue
            text = str(item.get("what", "") or "").strip()[:40]
            try:
                seen = float(item.get("at", 0.0) or 0.0)
            except (TypeError, ValueError):
                seen = 0.0
            if text and seen >= cutoff:
                items.append((seen, text))
        # 超量时留**最近**报的几件：旧写法直接取列表前三个，而列表按插入顺序排，
        # 留下的正好是最旧的三件——最该被顶掉的反倒先喂给模型。
        # 同一毫秒报的几件 at 相同（Windows 的 time.time 粒度约 15ms），先倒转存档顺序
        # 再用稳定排序兜底：并列时越靠后（越新）的排前面。
        items.reverse()
        items.sort(key=lambda pair: pair[0], reverse=True)
        return [text for _, text in items[: self.ONGOING_MAX]]

    def _note_ongoing(self, slot: Slot) -> None:
        """把这一段报的「还没做完」记下来。做完的事由模型不再报就自然过期。

        时间戳只盖在**本次真正重报**的那一条上，其余条目保留原来的 `at`。旧写法对所有
        条目一律写 `time.time()`（包括那些没被重报的），于是 `ONGOING_TTL_DAYS` 只对
        「她主动停止报告」的条目生效：同一件事一直报就一直续命，别的条目也跟着续命。
        """
        text = str((slot or {}).get("ongoing", "") or "").strip()[:40]
        if not text:
            return
        raw = self._scope.get_self(self.ONGOING_KEY)
        items = [i for i in raw if isinstance(i, dict)] if isinstance(raw, list) else []
        # 同一件事模型每次报的措辞会带进度（「还差 4 份」→「还差 2 份」），按字面比会
        # 堆成好几条同一件事。归一见 _ongoing_stem：剥掉进度数字再取前 12 字。
        stem = _ongoing_stem(text)
        kept: list[dict[str, Any]] = []
        for item in items:
            what = str(item.get("what", "") or "").strip()
            if not what or _ongoing_stem(what) == stem:
                continue
            try:
                at = float(item.get("at", 0.0) or 0.0)
            except (TypeError, ValueError):
                at = 0.0
            kept.append({"what": what, "at": at})
        kept.append({"what": text, "at": self._wall_clock()})
        # 列表顺序是「报告先后」，不是时间先后；超量时按 at 留最近的几件。
        kept.sort(key=lambda item: item["at"])
        self._scope.set_self(self.ONGOING_KEY, kept[-self.ONGOING_MAX :])
        self._scope.mark_dirty()

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

    def _ordered_segments(self) -> list[Slot]:
        """按起点升序的段。

        存档会乱序：carry 拼接、旧版整表迁移、`_migrate_new_day` 的部分写入。
        读路径原本盲取 `segs[-1]`，乱序时那可能是**更早**的一段。
        """
        segs = self.segments()
        segs.sort(key=lambda item: parse_time(item.get("start")) or 0)
        return segs

    def _active_and_prev(
        self, minutes: int, segs: list[Slot] | None = None
    ) -> tuple[Slot | None, Slot | None]:
        """覆盖「现在」的那一段，和「她现在正处在的那一段」（没有覆盖就是最后一段）。

        读路径（prompt 里的「她这一段从 X 开始…已经 N 分钟」、continue 判定）与写路径
        （截断/顺延）共用这一份口径：乱序存档里 `segs[-1]` 可能是**更早**的一段。
        `segs` 传入时按已排序处理。
        """
        ordered = self._ordered_segments() if segs is None else segs
        for item in ordered:
            lo = parse_time(item.get("start"))
            hi = parse_time(item.get("end"))
            if lo is None or hi is None:
                continue
            if lo <= minutes < hi:
                return item, item
        return None, (ordered[-1] if ordered else None)

    def active_segment(self, minutes: int | None = None) -> Slot | None:
        """覆盖「现在」这一段。没决定过就是 None——她此刻在做的事还没被问出来。"""
        if minutes is None:
            now = self._clock.now()
            minutes = now.hour * 60 + now.minute
        return self._covering(self.segments(), minutes)

    def last_segment(self) -> Slot | None:
        segs = self._ordered_segments()
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

    def _world_snapshot(self) -> dict[str, Any]:
        if self._world is None:
            return {}
        try:
            world = self._world()
        except Exception:
            return {}
        return world if isinstance(world, dict) else {}

    def _seed(self) -> list[Any]:
        try:
            return [self._scope.role_id, self._clock.today_str()]
        except Exception:
            return [self._scope.role_id]

    # ------------------------------------------------------------------
    # 日框：每天开头定一句「今天大概是什么日子」，之后每段都带着它
    # ------------------------------------------------------------------

    DAY_FRAME_KEY = "day_frame"
    DAY_FRAME_DATE_KEY = "day_frame_date"

    def _cached_day_frame(self) -> str:
        if str(self._scope.get_self(self.DAY_FRAME_DATE_KEY, "") or "") != self._clock.today_str():
            return ""
        return str(self._scope.get_self(self.DAY_FRAME_KEY, "") or "").strip()

    def _fallback_day_frame(self) -> str:
        """模型不可用时的确定句：星期 + 节日 + 天气。"""
        world = self._world_snapshot()
        bits = [f"周{self._clock.weekday()}"]
        holiday = str(world.get("holiday") or "").strip()
        weather = str(world.get("weather") or "").strip()
        if holiday:
            bits.append(f"是{holiday}")
        if weather:
            bits.append(f"外头{weather}")
        return "，".join(bits) + "。"

    async def _ensure_day_frame(self) -> str:
        """拿今天的日框（不在时给空串，由当天第一段顺带定下来）。"""
        return self._cached_day_frame()

    def _store_day_frame(self, text: str) -> None:
        """把今天第一段顺带给出的日框存下来（拿不到就用确定句，不再重复问）。"""
        if self._cached_day_frame():
            return
        frame = str(text or "").strip().strip("\"'“”「」 　")[:60] or self._fallback_day_frame()
        self._scope.update_self(
            **{self.DAY_FRAME_KEY: frame, self.DAY_FRAME_DATE_KEY: self._clock.today_str()}
        )
        self._scope.mark_dirty()

    # ------------------------------------------------------------------
    # 跨天与结转
    # ------------------------------------------------------------------

    def _archive_previous_day(self, today: str) -> None:
        """跨天前把昨天那份日程压进归档——习惯层唯一的数据来源（v3.0.10）。

        以前跨天只把昨天挪进 `prev_day_schedule`，再往后一天就被覆盖了：生成侧
        永远只看得到「今天 + 昨天」，没有「她平时这个钟点在哪、做什么」。于是每天
        从零挑事，模型只能挑最通用的几件。

        幂等：同一天重复调用只是把同一条记录再写一遍。写不进去就算了，习惯是
        锦上添花，不能让它把跨天本身弄失败。
        """
        try:
            stored_date = str(self._scope.get_self("today_date", "") or "")
        except Exception:
            return
        if not stored_date or stored_date == str(today or ""):
            return
        archive_day(self._scope, stored_date, self._stored_slots())

    def _migrate_new_day(self, today: str) -> None:
        """跨天：把结转到今天的那截觉接成今天的第一段。

        没有结转时不在这里生成——今天的第一段由后台循环按到期规则现决定，
        昨天那份留在原地当 prompt 的上下文（「昨天的收尾」）。

        有结转时昨天那份**不丢**：先挪到 `prev_day_schedule` 再覆盖。旧写法直接
        `daily_schedule=[slot]`，昨天做了什么当场消失，`_history_for_prompt` 的
        「昨天的收尾」分支（它按 `today_date` 判断）也就永远不可达。
        """
        if str(self._scope.get_self("today_date", "") or "") == today:
            return
        self._archive_previous_day(today)
        previous = self._stored_slots()
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
            # 跨夜那一觉的梦要跟着接过来：否则她早上醒来时，昨天夜里做的梦
            # 在今天的日程里没有落点，社交层也没得分享。
            "dreams": _clean_dreams(carry.get("dreams")),
        }
        updates: dict[str, Any] = {
            "today_date": today,
            "daily_schedule": [slot],
            CARRY_KEY: None,
        }
        if previous:
            updates[self.PREV_DAY_KEY] = previous[-self.PREV_DAY_KEEP :]
        self._scope.update_self(**updates)

    def _prev_day_slots(self) -> list[Slot]:
        """昨天那份日程的尾部（跨天时由 `_migrate_new_day` 留下）。"""
        stored = self._scope.get_self(self.PREV_DAY_KEY)
        if not isinstance(stored, list):
            return []
        return [slot for slot in stored if isinstance(slot, dict)]

    def _sleep_today(self, minutes: int) -> int:
        """今天到现在她一共睡了多久（分钟）。

        只看日程里已经发生的部分：睡段的起点必须已经过去。用来回答「这一段要不要
        睡」——中午之所以老在睡，一半原因是生成时没人知道她已经睡过多少。
        """
        total = 0
        for slot in self.segments():
            if not slot_sleeps(slot):
                continue
            lo = parse_time(slot.get("start"))
            hi = parse_time(slot.get("end"))
            if lo is None or hi is None or hi <= lo:
                continue
            total += max(0, min(hi, minutes) - lo)
        return total

    def _history_for_prompt(self) -> tuple[list[Slot], str]:
        """生成 prompt 用的「最近过完的段」：今天的不够看就接昨天的收尾。"""
        now = self._clock.now()
        minutes = now.hour * 60 + now.minute
        today = self._clock.today_str()
        if str(self._scope.get_self("today_date", "") or "") == today:
            segs = self.segments()
            done = [
                slot for slot in segs
                if (parse_time(slot.get("end")) or 0) <= minutes
            ]
            if done:
                return done, "今天到这会她已经过完的时段"
            # 今天还没过完任何一段（刚跨过零点、结转的那截觉还罩着现在）：接上昨天的
            # 收尾。旧写法这里返回空表——而它本来的「昨天」分支因为有结转时
            # `_migrate_new_day` 已经把 `today_date` 改成今天而不可达，于是跨年夜
            # 之后的第一段 prompt 里 `history_block` 只有一条睡眠段。
            previous = self._prev_day_slots()
            if previous:
                return previous, "昨天的收尾（刚跨过零点）"
            return done, "今天到这会她已经过完的时段"
        stored = self._stored_slots()
        return stored, "昨天的收尾（刚跨过零点）"

    # ------------------------------------------------------------------
    # 何时该重新决定
    # ------------------------------------------------------------------

    def _gate_allows(self, *, ignore_interval: bool = False) -> bool:
        """节流闸门：没人看着 / 间隔没到 / 今日预算用完时，都算「不需要决定」。

        问在「本来该生成」之后而不是之前：正常无事可做的那几百个 tick 不该去
        反复查闸门，更不该把 _roll_change 的骰子白白掷掉。

        `ignore_interval=True` 只越过「距上次调用还没到间隔」这一条（空闲静默与
        每日预算照旧）——空档接续是同一条时间线上的补位，不是提前再问一次。
        """
        gate = getattr(self.gateway, "gate", None) if self.gateway is not None else None
        if gate is None:
            return True
        # 按角色问「有没有人在看着她」：闸门全局一份，不传角色就会让别的 bot 的
        # 对话松开这个角色的空闲静默（审核 main.py 6）。
        role_id = str(getattr(self._scope, "role_id", "") or "")
        return gate.check(
            PURPOSE, role_id=role_id, ignore_interval=ignore_interval
        ).allowed

    def note_interruption(
        self,
        *,
        is_group: bool = False,
        now: float | None = None,
        text: str = "",
    ) -> None:
        """一条私聊消息来了——满足条件时记一次「有人打破安静来找她」。

        两道比例尺（都可配）：距上一条私聊至少隔 `schedule_interrupt_quiet_minutes`
        （否则算「消息还热着」，很多人同时聊时这个口子就自然不再触发；0 = 不要求
        安静，任何一条私聊都算来访），且距上次算作来访至少隔
        `schedule_interrupt_cooldown_minutes`（0 = 不冷却）。群消息不算「来找她」。

        `text`：消息正文。私聊里出现「在吗/你在干嘛/陪我/别弄了…」这类**明确叫住
        她**的话时，即使没熬过安静间隔也算一次来访（`schedule_interrupt_on_call`，
        仍受冷却；群聊永远不算）。旧调用不传 text 时行为与以前完全一致。

        只设不排：真正的决定在下一次 refresh_due 里做（还要过闸门/频率/预算）。
        冷却基准 `_last_interrupt_at` 与待处理的标记分开：消费掉标记不能
        顺手把冷却也清了。
        """
        if is_group:
            return
        cfg = self.config
        t = float(self._monotonic() if now is None else now)
        previous = self._last_message_at
        self._last_message_at = t
        quiet_seconds = _interrupt_quiet_seconds(cfg)
        # 安静间隔没熬过时，只有「明确在叫住她」的话能破例；冷却照旧。
        if (
            previous is not None
            and t - previous < quiet_seconds
            and not _is_call_out(text, cfg)
        ):
            self._save_interrupt_baseline()
            return
        if (
            self._last_interrupt_at is not None
            and t - self._last_interrupt_at < _interrupt_cooldown_seconds(cfg)
        ):
            self._save_interrupt_baseline()
            return
        self._last_interrupt_at = t
        self._interrupt_at = t
        self._save_interrupt_baseline()

    # ------------------------------------------------------------------
    # 事件：一句话改写她这一天
    # ------------------------------------------------------------------

    def note_event(
        self,
        *,
        kind: str,
        seed: list[Any] | None = None,
        is_group: bool = False,
        now: float | None = None,
    ) -> bool:
        """别人刚说的话够重，记一次事件，让日程有机会因此停下或改主意。

        与 `note_interruption` 的分工：

        - `note_interruption` 管「有人来过」——三道宽闸，多用户部署里自然很少触发。
        - `note_event` 管「她经历了什么」——只看这句话的**性质**（kind 由 mood
          层的账本给，那边做本地词典 + 模型融合的判定，这里不重复判一遍）。

        群消息也记：群里发生的事她一样该知道。真正的闸在下面：保鲜期短
        （`EVENT_TTL_SECONDS`，隔了两小时就不该再影响她这会儿在做什么），
        且两次事件之间至少隔 `EVENT_COOLDOWN_SECONDS`——被连骂三句只算一件事，
        否则一句话就能让她一整天都在重排。

        只设不排：要不要真停下来由 refresh_due 与闸门（频率/预算/冷却）决定。
        """
        phrase = event_phrase(kind, list(seed or []) + [self._clock.today_str()])
        if not phrase:
            return False
        t = float(self._monotonic() if now is None else now)
        if (
            self._last_event_at is not None
            and t - self._last_event_at < EVENT_COOLDOWN_SECONDS
        ):
            return False
        self._last_event_at = t
        self._event_at = t
        self._event_phrase = phrase
        return True

    def _fresh_event(self, *, clear: bool = True) -> str:
        """待处理的事件措辞；没有或过期了返回空串。

        保鲜期读 `schedule_event_ttl_minutes`（默认 30 分钟，原写死
        `EVENT_TTL_SECONDS`）：隔了两小时，「刚刚」就不再是刚刚。
        """
        if not self._event_phrase or self._event_at <= 0:
            return ""
        if self._monotonic() - self._event_at > _event_ttl_seconds(self.config):
            if clear:
                self._event_at = 0.0
                self._event_phrase = ""
            return ""
        return self._event_phrase

    def _consume_event_stamped(self) -> tuple[str, float]:
        """取走一次待处理事件，连同它**被记下的时刻**（单调秒）。

        生成被闸门挡回时要原样还回（措辞与时间戳一起），否则反复被挡就能把
        保鲜期一次次续满——以前归还时把 `_event_at` 重置成「现在 - 1 秒」，
        等于每次被挡都免费续命 30 分钟，事件永远不会过期（日程 F1）。
        """
        phrase = self._fresh_event()
        if not phrase:
            return "", 0.0
        ts = self._event_at
        self._event_at = 0.0
        self._event_phrase = ""
        return phrase, ts

    def _consume_event(self) -> str:
        """取走一次待处理事件（一次性）。返回的是原始措辞。"""
        return self._consume_event_stamped()[0]

    # ------------------------------------------------------------------
    # 睡着时听见的话
    # ------------------------------------------------------------------

    def _heard_while_asleep_add(self, phrase: str) -> None:
        """记下一条「她睡着的时候发生的」。空串、重复都不记。"""
        text = str(phrase or "").strip()
        if not text:
            return
        if any(item.get("phrase") == text for item in self._heard_while_asleep):
            return
        self._heard_while_asleep.append({"phrase": text, "at": self._monotonic()})
        del self._heard_while_asleep[:-ASLEEP_HEARD_MAX]

    def _capture_heard_while_asleep(self) -> None:
        """睡着时把待处理的事件与来访收进记忆，别让它们在保鲜期里悄悄过期。

        睡着的那一段里 `_due` 一律返回 False（不重掷、不掀被子），以前它**什么都不做**：
        `note_event` / `note_interruption` 设的标记就躺在那儿等 TTL（默认 30 / 60 分钟）
        过期，用户夜里说的每句话都被静默丢弃，连一句记录都不留。

        这里只做记账：把事件措辞与「有人来过」收成短句，等醒来那一段生成时递给她
        （`segment_prompt(heard_asleep=...)`），醒来装上新段后清空
        （`_append_segment`）。她听没听见、要不要提，仍由她决定——这里只说「发生过」。
        """
        phrase, _ts = self._consume_event_stamped()
        if phrase:
            self._heard_while_asleep_add(phrase)
        if self._fresh_interruption(clear=True):
            self._heard_while_asleep_add("有人来找过她")

    def heard_while_asleep(self) -> list[str]:
        """睡着期间听到的话（给 prompt 用）。没有就是空列表。"""
        return [str(item.get("phrase") or "") for item in self._heard_while_asleep]

    def pending_event(self) -> dict[str, Any]:
        """待处理事件的一份只读快照。没有就返回空 dict。

        给契约导出用：社交层要靠它说得出「她刚被人敷衍了」，而当它正在决定
        下一段时不能把这个事件**取走**——取了这一轮就没了。所以这里只读。
        """
        phrase = self._fresh_event(clear=False)
        if not phrase:
            return {}
        return {
            "phrase": phrase,
            "age_seconds": max(0.0, self._monotonic() - self._event_at),
            "ttl": _event_ttl_seconds(self.config),
        }

    def _fresh_interruption(self, *, clear: bool = True) -> bool:
        """有没有还新鲜（未过期）的待处理「来访」；过期的顺手清掉。

        不设保鲜期时，标记可以一睡几个小时（睡着时不被消费）或卡在闸门后面，
        等到下一次重排才被取用——那时提示词里那句「刚刚有人来找她」就是假话。
        保鲜期读 `schedule_interrupt_ttl_minutes`（默认 60 分钟，原写死
        `INTERRUPT_TTL_SECONDS`）。

        `clear=False` 供只读路径（`_due_peek` / `status`）用：连「把过期标记置 0」
        这个写操作都不做。
        """
        if self._interrupt_at <= 0:
            return False
        if self._monotonic() - self._interrupt_at > _interrupt_ttl_seconds(self.config):
            if clear:
                self._interrupt_at = 0.0
            return False
        return True

    def _consume_interruption(self) -> float:
        """取走一次待处理的来访（一次性）；过期的/没有的返回 0.0。不改冷却基准。

        返回的是**原始时间戳**：生成被闸门挡回时要原样还回（见 `_generate_segment`），
        只把标记放回去、不重新计时，否则反复被挡就能把保鲜期一次次续满。
        """
        if not self._fresh_interruption():
            return 0.0
        ts = self._interrupt_at
        self._interrupt_at = 0.0
        return ts

    # ------------------------------------------------------------------
    # 被打断之前她在做的那件事：挂起、读取、回来接着做
    # ------------------------------------------------------------------

    def _clear_suspended(self) -> None:
        """清掉挂起的那件事（走 scope 写路径，会 mark_dirty）。"""
        if self._scope.get_self(self.SUSPENDED_KEY):
            self._scope.set_self(self.SUSPENDED_KEY, None)

    def _suspended_slot(self) -> dict[str, Any]:
        """被打断那件事的规范化快照；没有/过期返回 {}。

        过期（`schedule_pause_ttl_minutes`）顺手清掉——旧的挂起不该在一天后
        还冒出来说「她打断之前在做……」。
        """
        raw = self._scope.get_self(self.SUSPENDED_KEY)
        if not isinstance(raw, dict) or not raw:
            return {}
        event = str(raw.get("event") or "").strip()
        if not event:
            return {}
        try:
            at = float(raw.get("at") or 0.0)
        except (TypeError, ValueError):
            at = 0.0
        try:
            ttl_minutes = int(float(raw.get("ttl_minutes") or 0))
        except (TypeError, ValueError):
            ttl_minutes = 0
        if ttl_minutes <= 0:
            ttl_minutes = _pause_ttl_minutes(self.config)
        if at > 0 and self._wall_clock() - at > ttl_minutes * 60.0:
            self._clear_suspended()
            return {}
        try:
            remaining = max(0, int(float(raw.get("remaining_minutes") or 0)))
        except (TypeError, ValueError):
            remaining = 0
        # #69：剩余时间按「原定结束时刻 - 现在」现算。旧快照没有 `end_at`，
        # 用「挂起时刻 + 当时剩下的分钟」补出来；连 `at` 都没有才退回那个数字。
        try:
            end_at = float(raw.get("end_at") or 0.0)
        except (TypeError, ValueError):
            end_at = 0.0
        if end_at <= 0 and at > 0:
            end_at = at + remaining * 60.0
        if end_at > 0:
            left = end_at - self._wall_clock()
            if left <= 0:
                # 原定结束时刻已经过去：这件事已经结束了，不该再说「还剩 0 分钟」。
                self._clear_suspended()
                return {}
            remaining = max(1, int(math.ceil(left / 60.0)))
        return {
            "event": _clip_event(event),
            "location": str(raw.get("location") or "").strip()[:16],
            "emotion": str(raw.get("emotion") or "").strip()[:12],
            "ongoing": str(raw.get("ongoing") or "").strip()[:40],
            "wants": bool(raw.get("wants")),
            "remaining_minutes": min(remaining, DAY_MINUTES),
            # 被截断那段的能量速率（#70）：resume 分支拿它当默认值。快照不带上就等于
            # 没存——模型 resume 时通常只回 resume=true，不带 energy_rate。
            "energy_rate": clamp_rate(raw.get("energy_rate", 0.0)),
            "sleep": bool(raw.get("sleep")),
            # 处境词标记：挂起的是兜底给的“有点饿了”，渲染与 resume 都要知道它不是动作。
            "status": bool(raw.get("status")),
            "at": at,
            "ttl_minutes": ttl_minutes,
        }

    def suspended_info(self) -> dict[str, Any]:
        """被打断之前她在做的那件事（只读快照）；没有返回 {}。

        给状态页/诊断用：`/查看日程` 与 `/拟人 诊断` 要能看出「她被打断后还
        记着哪件事、还剩多少分钟、还能回去多久」。快照里额外补两个算出来的
        只读字段（`age_minutes` / `ttl_left_minutes`），不改存下来的数据。
        """
        try:
            slot = self._suspended_slot()
        except Exception:
            return {}
        if not slot:
            return {}
        out = dict(slot)
        try:
            age = max(0.0, self._wall_clock() - float(slot.get("at") or 0.0))
            out["age_minutes"] = int(age // 60.0)
            out["ttl_left_minutes"] = max(
                0, int(float(slot.get("ttl_minutes") or 0) - age / 60.0)
            )
        except (TypeError, ValueError):
            out["age_minutes"] = 0
            out["ttl_left_minutes"] = 0
        return out

    def _interrupt_status(self) -> dict[str, Any]:
        """打断闸门的只读快照（给 `status()` 用）：还剩多少保鲜期/冷却/安静。

        全部只读：不改标记、不掷骰子。任何异常都退化成「什么都不剩」——
        状态页不能因为一个坏配置把 /查看日程 整条带崩。
        """
        try:
            cfg = self.config
            now = self._monotonic()
            ttl = _interrupt_ttl_seconds(cfg)
            quiet = _interrupt_quiet_seconds(cfg)
            cooldown = _interrupt_cooldown_seconds(cfg)
            pending = self._fresh_interruption(clear=False)
            ttl_left = (
                max(0.0, ttl - (now - self._interrupt_at))
                if pending and self._interrupt_at > 0
                else 0.0
            )
            cooldown_left = (
                max(0.0, cooldown - (now - self._last_interrupt_at))
                if self._last_interrupt_at is not None
                else 0.0
            )
            quiet_left = (
                max(0.0, quiet - (now - self._last_message_at))
                if self._last_message_at is not None
                else 0.0
            )
            return {
                "pending": bool(pending),
                "ttl_left": round(ttl_left, 1),
                "cooldown_left": round(cooldown_left, 1),
                "quiet_left": round(quiet_left, 1),
                "ttl_minutes": int(ttl // 60),
                "cooldown_minutes": int(cooldown // 60),
                "quiet_minutes": int(quiet // 60),
                "on_call": _on_call_enabled(cfg),
                "event_pending": bool(self._fresh_event(clear=False)),
            }
        except Exception:
            return {
                "pending": False,
                "ttl_left": 0.0,
                "cooldown_left": 0.0,
                "quiet_left": 0.0,
            }

    def refresh_due(self) -> bool:
        """该不该决定下一段：没段/这一段过完了/身体打架/刚被打断/还没排上大模型/掷中骰子。

        **会写状态**（掷骰子、清过期的「来访」）。只读查询请用 `_due_peek()`。
        """
        return self._due(roll=True)

    def _due_peek(self) -> bool:
        """`refresh_due()` 的**只读**版本，给 `status()` 这类查询用。

        `refresh_due()` 经 `_roll_change()` 会写 `_roll_window`/`_roll_hit`，而
        `status()` 在每条消息组 prompt 的路径上都会被调到 —— 用户在 15 分钟决策窗里
        说一句话就把本窗的「临时改主意」骰子吃掉，且结果缓存到窗口结束。这里只**读**
        本窗已经掷出的结果（还没掷过就是 False），也不清过期的「来访」标记。
        """
        return self._due(roll=False)

    def _due(self, *, roll: bool) -> bool:
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
        if slot_sleeps(active):
            # 睡着：这一觉睡到这段结束为止，夜里不重掷、不越线打断。
            # **消息也不算**：多用户部署里夜里总有人发消息，让消息能撕被子，
            # 她就没有一个完整的觉了（这个口子原来只按「一个话多的人」设计，
            # 没扛住「很多人」——现在只走睡觉保护，起床由这段自然结束）。
            # 但「不掀被子」不等于「听不见」：睡着期间的这句话与这次来访要记下来，
            # 醒来那一段的 prompt 会递给她（#66）。旧代码在这里直接返回，事件与
            # 来访只在保鲜期里躺到过期，一觉醒来什么都不剩。
            # 只在 `roll=True`（真的在走一次决策）时收：`_due_peek` 是每条消息都会
            # 调的只读路径，在那里消费等于把这一轮要用的东西提前吃掉。
            if roll:
                self._capture_heard_while_asleep()
            return False
        body = self._body_snapshot()
        if body:
            hunger = float(body.get("hunger", 0.0) or 0.0)
            pressure = float(body.get("sleep_pressure", 0.0) or 0.0)
            if hunger >= CHANGE_TRIGGER_HUNGER and not is_meal_event(event):
                return True
            if pressure >= CHANGE_TRIGGER_SLEEP_PRESSURE:
                return True
        # 刚有人打破安静来找过她：给她一个「要不要停下手上这件事」的时机。
        # 这是「中途被打断 / 去干点别的」唯一的人工触发口，以前完全没有，
        # 所以一天下来每段都像排好的任务，从来没有中途变过。
        # 只有走大模型日程时才有意义：纯模板兜底没有「让模型改主意」这一层，
        # 放它进来只会白白重排一次模板。
        if cfg.use_llm_schedule and self._fresh_interruption(clear=roll) and self._gate_allows():
            return True
        # 刚发生的事够重（被夸/被骂/被敷衍）：给她一次因此停下、改主意、
        # 或反过来被激励的时机。这是用户的话唯一能进日程的路。
        # 睡着的那段已经在上面 return 了——消息不掀被子，被夸也一样。
        if cfg.use_llm_schedule and self._fresh_event(clear=roll) and self._gate_allows():
            return True
        if cfg.use_llm_schedule and self.source != SOURCE_LLM:
            # 本地兜底段还占着位：尽快让大模型接手（退避会挡住砸模型的频率）。
            due = True
        elif roll:
            due = self._roll_change()
        else:
            due = self._roll_peek()
        return due and self._gate_allows()

    def _roll_peek(self) -> bool:
        """本窗已经掷出的骰子；本窗还没掷过就是 False（只读路径不替它掷）。"""
        interval = max(60, int(self.config.schedule_refresh_minutes) * 60)
        window = int(self._clock.now().timestamp() // interval)
        if window != self._roll_window:
            return False
        return self._roll_hit

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
    # 打断与续做：`/拟人 日程打断` 与 `/拟人 日程继续`
    # ------------------------------------------------------------------

    def continue_now(self) -> bool:
        """让她把当前这一段接着做下去：不调模型，直接把这一段的 end 往后延。

        延长量读 `schedule_continue_minutes`（默认 45 分钟）。规则：

        - 覆盖此刻的那一段不存在 → False（她手上没有正在做的事）；
        - 已经到 24:00 / 延长会跨过午夜 → False（跨午夜是睡眠结转的事）；
        - 后面已经有下一段 → 最多延长到它的 start，不重叠、不吞掉后面的安排；
        - 延长后 `segments()` / `active_segment()` / `current_activity()` /
          契约与 `status()` 立刻反映新的 end（它们都从同一份 `daily_schedule` 读）。

        能量速率**沿用**这一段原值：这是「接着做」，不是重新决定这件事。
        """
        try:
            cfg = self.config
            extend = _continue_minutes(cfg)
            now = self._clock.now()
            minutes_now = now.hour * 60 + now.minute
            active = self.active_segment(minutes_now)
            if active is None:
                return False
            lo = parse_time(active.get("start"))
            hi = parse_time(active.get("end"))
            if lo is None or hi is None or hi <= lo:
                return False
            if hi >= DAY_MINUTES:
                return False
            desired = hi + extend
            if desired > DAY_MINUTES:
                # 超过 24:00 不延长：跨午夜那半截是睡眠结转（carry）的活。
                return False
            stored = self._stored_slots()
            # 不越过后面已经排好的段：有后续段时最多延长到它的 start。
            for item in stored:
                if item is active:
                    continue
                next_lo = parse_time(item.get("start"))
                if next_lo is not None and next_lo >= hi and next_lo < desired:
                    desired = next_lo
            if desired <= hi:
                return False
            updated: list[Slot] = []
            replaced = False
            for item in stored:
                if item is active:
                    slot = dict(item)
                    slot["end"] = format_time(desired)
                    # energy_rate 显式沿用这一段自己的值（不是新段、不重算）。
                    slot["energy_rate"] = active.get("energy_rate", slot.get("energy_rate", 0.0))
                    updated.append(slot)
                    replaced = True
                else:
                    updated.append(dict(item))
            if not replaced:
                return False
            self._scope.update_self(daily_schedule=updated)
            # 本窗的骰子不该因为这次延长立刻把刚延长的段又截掉。
            self._roll_hit = False
            if self._log and cfg.debug_mode:
                self._log.info(
                    f"[humanoid_core] 日程继续：{active.get('event')} "
                    f"{format_time(hi)} → {format_time(desired)}"
                )
            return True
        except Exception as exc:
            if self._log:
                self._log.warning(f"[humanoid_core] 日程继续失败: {exc}")
            return False

    def interrupt_now(self) -> bool:
        """立刻让她放下手上的事、换成另一件事（`/拟人 日程打断`）。

        与 `/拟人 重置日程` 同样的力度：`force=True, ignore_cooldown=True`
        ——不看该不该重排、不看冷却/退避，但照样受单飞闸约束。区别只在生成时
        明确要求「换一件事」：`force_switch=True` 会让提示词写死不许 continue，
        解析时 `continue=true` 也会被改判/判失败（不允许原地续）。

        返回是否成功排入后台任务：已经有一轮生成在跑时返回 False（不排队重复）。
        没有正在进行的段时也能用——那就直接决定下一段。
        """
        if self._task and not self._task.done():
            return False
        if self._generating:
            return False
        coro = self.ensure_fresh(force=True, ignore_cooldown=True, force_switch=True)
        name = f"humanoid-schedule-interrupt-{self._scope.role_id}"
        try:
            task = self._spawn(coro, name) if self._spawn else asyncio.create_task(coro, name=name)
        except Exception as exc:
            if asyncio.iscoroutine(coro):
                coro.close()
            if self._log:
                self._log.warning(f"[humanoid_core] 日程打断投递失败: {exc}")
            return False
        if task is None:
            if asyncio.iscoroutine(coro):
                coro.close()
            return False
        self._task = task
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

    async def ensure_fresh(
        self,
        force: bool = False,
        ignore_cooldown: bool = False,
        force_switch: bool = False,
    ) -> bool:
        """决定（并装上）下一段。

        模型不可用、未配置或还在退避期时：手上没有能接着做的段就按身体现算一段
        （不留白）；有段就先做着手上的事，等退避过去再让模型接手。

        `force_switch=True`（`/拟人 日程打断` 用）：这次必须换成另一件事——提示词
        写死「不能 continue」，解析时 continue=true 会被改判/判失败。

        **模型调用在锁外**：它带超时与重试（多 provider 链最坏 ≈ 超时 × 尝试次数 ×
        provider 数），持锁期间任何并发 `ensure_fresh` 都得排队，一条 `/日程` 强制刷新
        可能排在一次卡住的生成后面等几分钟。锁只覆盖「读状态 → 决策」与「写回」两段。
        """
        cfg = self.config
        now = self._clock.now()
        minutes = now.hour * 60 + now.minute
        today = self._clock.today_str()

        async with self._lock:
            # 单飞闸：模型调用挪到锁外之后，没有这道闸，10 个并发 `ensure_fresh`
            # 会各调一次模型（旧实现靠持锁串行化 + 第二次 refresh_due() 返回 False
            # 才只有一个真的去调）。
            if self._generating:
                return False
            # 拿锁期间可能已被别的路径决定过：再确认一次。
            if not force and not self.refresh_due():
                return False
            # 「现在这一段」和「它前面那段」与写路径同一份口径：存档乱序时盲取
            # `segs[-1]` 会把更早的一段当成她正在做的事递给 prompt（时间跨度、
            # 「上一段是不是睡眠」、continue 判定全跟着错）。
            active, prev = self._active_and_prev(minutes)
            # 空档：现在没有任何一段罩着此刻。这种时候的生成是「接上断掉的地方」，
            # 不是「提前再问一次」，所以允许越过最小间隔（空闲静默与每日预算照旧）。
            gap = active is None
            elapsed = 0
            if prev is not None:
                lo = parse_time(prev.get("start"))
                if lo is not None:
                    elapsed = max(0, minutes - lo)

            attempt_llm = bool(
                cfg.use_llm_schedule
                and self.gateway is not None
                and (force or ignore_cooldown or self.retry_after <= 0)
            )
            interrupted_ts = 0.0
            event_phrase = ""
            if attempt_llm:
                # 「这次是不是被人打断触发的」只在**真的要去生成**时才取走；
                # 退避中不取，留到下一轮。取走时拿到原始时间戳，被闸门挡回时
                # 原样还回（不重新计时，保鲜期才拦得住无限续命）。
                interrupted_ts = self._consume_interruption()
                # 同理：事件也在「真的要去生成」时才取走。被闸门挡回时还回。
                event_phrase, event_ts = self._consume_event_stamped()

        slot: Slot | None = None
        if attempt_llm:
            self._generating = True
        try:
            if attempt_llm:
                slot = await self._generate_segment(
                    cfg, now, minutes, prev, elapsed, ignore_cooldown,
                    interrupted_ts=interrupted_ts, event_phrase=event_phrase,
                    event_ts=event_ts, force_switch=force_switch,
                    ignore_interval=gap,
                )
                # 生成真的失败了（超时/异常/解析失败/换事失败），把刚取走的事件与
                # 「来访」**原样还回去**（#75）：它们一次提示词都没进过，就这么丢掉
                # 等于「有人来找过她」这件事没发生。被节流挡回的分支（`OUTCOME_GATED`）
                # 已经在 `_generate_segment` 里还过，这里不能重复还（会覆盖新事件）。
                if slot is None and self._last_outcome != OUTCOME_GATED:
                    if interrupted_ts > 0.0:
                        self._interrupt_at = interrupted_ts
                    if event_phrase:
                        self._event_phrase = event_phrase
                        self._event_at = event_ts
            async with self._lock:
                if slot is None and (active is None or force or not cfg.use_llm_schedule):
                    slot = dynamic_segment(
                        cfg, self._body_snapshot(), now_minute=minutes,
                        step=cfg.granularity_minutes, prev=prev, seed=self._seed(),
                        asleep_minutes=self.sleeping_minutes(minutes),
                    )
                    self._set_source(SOURCE_TEMPLATE)
                if slot is None:
                    return False
                self._append_segment(slot, today)
                return True
        finally:
            if attempt_llm:
                self._generating = False

    def _fail_backoff(self, cfg: HumanoidConfig) -> None:
        backoff = max(MIN_RETRY_BACKOFF_SECONDS, float(cfg.schedule_provider_cooldown_minutes) * 60)
        self._retry_after = self._monotonic() + backoff

    def _today_plans(self) -> list[str]:
        """她今天确实要办的事（v3.0.10）：读盘上抽好的原话片段，读不到就是空表。"""
        try:
            today = self._clock.now().date()
        except Exception:
            return []
        return load_plans(self._scope, today)

    def note_today_plans(self, text: str) -> list[str]:
        """从一句对话里抽「她今天要办的事」（v3.0.10）。

        抽取规则全在 humanoid/services/plans.py：带明确日期、主语是她、不是疑问或
        否定、只留原话片段。宁可漏抽也不能把对方的安排记成她的——漏抽只是少一条素材，
        记错就是日程在编造。这里不抛异常：抽计划失败不该影响这一轮回复。
        """
        said = str(text or "").strip()
        if not said:
            return []
        try:
            today = self._clock.now().date()
        except Exception:
            return []
        try:
            return note_plans(self._scope, said, today)
        except Exception:
            return []

    def ensure_role_materials(self) -> bool:
        """排一次「按人设生成专属素材」（v3.0.10）。

        和日程生成分开跑：它要单独一次模型调用，塞进 _generate_segment 会让每次换段
        都可能多等一次往返，也会把「换段只调一次模型」这条不变量弄坏（测试、节流预算
        都靠它）。所以由后台循环来问，单飞；生成好了下一次换段自然用上，生成失败就
        继续用原来的池子。返回是否真的排了任务。
        """
        cfg = self._config()
        if self.gateway is None or not cfg.use_llm_schedule:
            return False
        if self._role_task is not None and not self._role_task.done():
            return False
        now = float(self._monotonic())
        if (
            self._role_checked_at is not None
            and now - self._role_checked_at < ROLE_MATERIALS_RETRY_SECONDS
        ):
            return False
        self._role_checked_at = now
        coro = self._build_role_materials(cfg)
        name = f"humanoid-role-materials-{self._scope.role_id}"
        try:
            task = self._spawn(coro, name) if self._spawn else asyncio.create_task(coro, name=name)
        except Exception as exc:
            if asyncio.iscoroutine(coro):
                coro.close()
            if self._log:
                self._log.warning(f"[humanoid_core] 角色专属素材投递失败: {exc}")
            return False
        if task is None:
            if asyncio.iscoroutine(coro):
                coro.close()
            return False
        self._role_task = task
        return True

    async def _build_role_materials(self, cfg: HumanoidConfig) -> list[dict[str, Any]]:
        """她自己的素材（v3.0.10）：按人设一次性生成、按人设指纹缓存。

        内置的 49 条是「人可能做的事」，不是「她会做的事」——人设是兽医的人，池子里
        也不会出现给猫量体温。所以人设第一次出现（或人设改了）时让模型按她写一批，
        存进 scope；指纹没变就一直用缓存，一次都不再调模型。
        失败、被节流、生成得太少都退回缓存（缓存为空时退回内置池）：素材是锦上添花，
        不值得让日程生成跟着失败。
        """
        cached = _role_load(self._scope)
        if self.gateway is None or not cfg.use_llm_schedule:
            return cached
        persona = await self._resolve_persona()
        persona_text = _example_evidence(persona, cfg)
        if not persona_text.strip():
            return cached
        fingerprint = _role_fingerprint(persona_text)
        now_ts = time.time()
        if not _role_needs_build(self._scope, fingerprint, now_ts, ROLE_MATERIALS_RETRY_SECONDS):
            return cached
        # 先记账再调用：调用失败（网络、被节流）也要等退避时间到了才重试，
        # 否则每次换段都白问一次模型。
        _role_mark_try(self._scope, now_ts, fingerprint)
        try:
            result = await self.gateway.generate(
                prompt=_role_build_prompt(persona_text, ROLE_MATERIALS_COUNT),
                chain=cfg.schedule_provider_ids,
                allow_global=cfg.schedule_allow_global_fallback,
                timeout=float(cfg.schedule_llm_timeout_seconds),
                attempts_per_provider=1,
                retry_interval=float(cfg.schedule_retry_interval_seconds),
                purpose=PURPOSE,
                ignore_cooldown=True,
                role_label=str(self._scope.role_id or ""),
            )
        except Exception:
            return cached
        if str(getattr(result, "outcome", "") or "") != OUTCOME_OK:
            return cached
        items = _role_parse_materials(str(getattr(result, "text", "") or ""), ROLE_MATERIALS_COUNT)
        # 人设里没有的能力/兴趣一律丢掉：模型很容易给她加一只猫、一份工作。
        items = _role_accepted(items, persona_text)
        if len(items) < ROLE_MATERIALS_MIN:
            if self._log:
                self._log.info(
                    f"[humanoid_core] 角色专属素材只生成出 {len(items)} 条"
                    f"（少于 {ROLE_MATERIALS_MIN} 条），这次仍用原来的素材"
                )
            return cached
        _role_store(self._scope, fingerprint, items)
        if self._log:
            self._log.info(f"[humanoid_core] 角色专属素材已生成 {len(items)} 条")
        return items

    async def _generate_segment(
        self,
        cfg: HumanoidConfig,
        now,
        minutes: int,
        prev: Slot | None,
        elapsed: int,
        ignore_cooldown: bool,
        interrupted_ts: float = 0.0,
        event_phrase: str = "",
        event_ts: float = 0.0,
        force_switch: bool = False,
        ignore_interval: bool = False,
    ) -> Slot | None:
        interrupted = interrupted_ts > 0.0
        self._resumed_event = ""
        self._last_outcome = ""
        if self.gateway is None:
            self.last_error = "LLM Gateway 未初始化"
            self._last_outcome = "no_gateway"
            return None

        persona = await self._resolve_persona()
        frame = await self._ensure_day_frame()
        history, history_note = self._history_for_prompt()
        world = self._world_snapshot()
        body = self._body_snapshot()
        # 被打断之前她在做的那件事（没有 → {}）：递进 prompt，允许它输出 resume。
        suspended = self._suspended_slot()
        # 困意越线与「上一段是不是睡眠」都是这一刻的处境：现取一次，和本地兜底走
        # 同一条阈值（CHANGE_TRIGGER_SLEEP_PRESSURE），不让 prompt 与兜底各说各的。
        try:
            sleep_pressure = float(body.get("sleep_pressure", 0.0) or 0.0)
        except (TypeError, ValueError):
            sleep_pressure = 0.0
        prev_sleep = bool(prev) and slot_sleeps(prev)
        # 今天的素材：模型手里原来只有人设正文 + 四条范例，能挑的事太少，于是
        # 天天那几件。素材按「这个钟点 + 她这样的人 + 最近做过什么」筛过，
        # 去重用的是今天已过完的段与昨天的收尾。
        recent_events = [
            str(slot.get("event") or "")
            for slot in list(history) + list(self._prev_day_slots())
        ]
        # 她自己的素材（v3.0.10）：按人设生成过一批就用那批，没有就只剩内置池。
        # 生成在后台（ensure_role_materials），这里只读缓存——换段仍然只调一次模型。
        role_items = _role_load(self._scope)
        materials = render_materials(
            materials_for(
                self._seed(),
                _example_evidence(persona, cfg),
                minutes,
                recent_events,
                extra=role_items,
            ),
            self._seed(),
        )
        day_sleep = self._sleep_today(minutes)
        # 她自己长出来的习惯：过去几周这个钟点常做的事 + 作息。样本不够时是空串，
        # prompt 里连标题都不会出现。
        habits = render_habits(habit_lines(history_of(self._scope), minutes))
        prompt = segment_prompt(
            cfg,
            now_text=now.strftime("%H:%M"),
            weekday=self._clock.weekday(),
            persona=persona,
            body=body,
            prev=prev,
            elapsed_minutes=elapsed,
            history=history,
            history_note=history_note,
            ongoing=self.ongoing_tasks(),
            is_night=in_scheduled_night(self.current_slots(), now),
            interrupted=interrupted,
            event_phrase=event_phrase,
            prev_sleep=prev_sleep,
            body_pressure=sleep_pressure >= CHANGE_TRIGGER_SLEEP_PRESSURE,
            weather=str(world.get("weather") or ""),
            holiday=str(world.get("holiday") or ""),
            day_frame=frame,
            seed=self._seed(),
            want_day_frame=not frame,
            suspended=suspended or None,
            force_switch=force_switch,
            # 睡着期间听到的话（#66）：醒来这一段递给她，装上新段后清空。
            heard_asleep=self.heard_while_asleep(),
            materials=materials,
            day_sleep_minutes=day_sleep,
            habits=habits,
            plans=self._today_plans(),
        )
        if self._log and cfg.debug_mode:
            self._log.info(
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
                ignore_interval=ignore_interval,
                role_label=str(self._scope.role_id or ""),
            )
        except Exception as exc:
            self.last_error = f"生成异常：{exc}"
            self._last_outcome = "exception"
            self._fail_backoff(cfg)
            return None
        finally:
            self._generating = False

        # 结果对象可能来自替身/旧适配层，没有 outcome 字段时别炸掉整段换段流程（#75）：
        # 少了这个字段只影响「失败原因」这一句诊断，不该让日程停摆。
        outcome = str(getattr(result, "outcome", "") or "error")
        self._last_outcome = outcome
        if not result.ok:
            if outcome == OUTCOME_GATED:
                # 被节流跳过不是 provider 故障，不能进 30 分钟退避——那会让
                # 「用户一说话就恢复」也一起失效。也不往 last_error 上挂：
                # 那是给用户看的诊断位，不该长期显示一个无须处理的节流提示。
                if interrupted:
                    # 这次「来访」只是没挤进队列（闸门被别的调用占着），**原样**还回去：
                    # 保留原始时间戳，不重新计时——否则反复被挡就能一直续命、突破保鲜期。
                    self._interrupt_at = interrupted_ts
                if event_phrase:
                    # 同理：事件被挡回时原样还回——措辞与**原始时间戳**一起还，
                    # 不重新计时（否则反复被挡就等于永久续命）。
                    self._event_phrase = event_phrase
                    self._event_at = event_ts
                if self._log and cfg.debug_mode:
                    self._log.info(f"[humanoid_core] 日程被节流跳过: {result.detail}")
                return None
            self.last_error = result.summary()
            self._fail_backoff(cfg)
            if self._log:
                self._log.warning(
                    f"[humanoid_core] 日程生成失败（{self._scope.role_id}）：{self.last_error}"
                )
            return None

        raw = extract_json_object(result.text)
        if raw is None:
            raw = extract_json_array(result.text)
        if force_switch:
            # `/拟人 日程打断`：必须换成另一件事。continue=true 一律改判/丢弃
            # （模型必须给出 event），拿不到新事件就失败返回——不能原地续。
            raw = _force_switch_raw(raw, prev)
            if raw is None:
                self.last_error = "这次要求换一件事，但模型只说了继续、没给出新事件"
                self._last_outcome = "force_switch_failed"
                self._fail_backoff(cfg)
                if self._log and cfg.debug_mode:
                    self._log.info(f"[humanoid_core] 日程打断失败: {self.last_error}")
                return None
        slot = parse_segment(
            raw,
            now_minute=minutes,
            step=cfg.granularity_minutes,
            prev=prev,
            suspended=suspended or None,
        )
        if slot is None:
            # 解析失败同样进退避：不挡的话每个后台周期都会再砸一次模型。
            self._last_outcome = "parse_failed"
            self.last_error = f"无法解析这一段：{result.text[:160]}"
            self._fail_backoff(cfg)
            if self._log and cfg.debug_mode:
                self._log.info(f"[humanoid_core] 下一段解析失败: {self.last_error}")
            return None
        if (
            force_switch
            and prev is not None
            and str(slot.get("event") or "") == str(prev.get("event") or "")
        ):
            # 换事失败：它把原来那件事又排了一遍。按失败处理（不能假装换过）。
            self.last_error = "这次要求换一件事，但模型又排回了同一件事"
            self._last_outcome = "force_switch_failed"
            self._fail_backoff(cfg)
            if self._log and cfg.debug_mode:
                self._log.info(f"[humanoid_core] 日程打断失败: {self.last_error}")
            return None
        # ---- 质量闸（v3.0.6）：装进日程之前独立判一遍 ----
        # prompt 里的规则是「请求」，不是「保证」。这里只判两件能客观判定的事：
        #   ① 今天是不是已经排过同一件事（重复）；
        #   ② 事件里的身份/关系/约定，人设里有没有依据（编造）。
        # 判中了先带提示重写一次；仍然编造就降级成中性段——宁可只给处境词，
        # 也不让「下午去接孩子放学」这种凭空事实进她的日程。
        degraded = False
        if getattr(cfg, "schedule_quality_gate", False):
            evidence = self._fact_evidence(persona, cfg)
            issues, invented = self._quality_issues(slot, evidence, prev)
            # 「睡够了还睡」也算一条：它不是重复、也不是编造，但同样是「这一段
            # 本不该这样」。三条一起进重写，重写不动时只有编造会降级。
            if self._needless_sleep(slot, minutes):
                issues = list(issues) + ["needless_sleep"]
            if self._overlong_nap(slot):
                issues = list(issues) + ["overlong_nap"]
            if issues:
                self._bump_quality("rewrite")
                retried = await self._rewrite_once(
                    cfg,
                    prompt,
                    issues,
                    invented,
                    ignore_cooldown,
                    now_minute=minutes,
                    prev=prev,
                    suspended=suspended or None,
                )
                if retried is not None:
                    raw, slot = retried
                    issues, invented = self._quality_issues(slot, evidence, prev)
                    # 重写之后要重新判一遍「睡够了还睡」：只重算 repeat/invented
                    # 的话，重写版照样排睡会被静默放行（闸门等于没装）。
                    if self._needless_sleep(slot, minutes):
                        issues = list(issues) + ["needless_sleep"]
                    if self._overlong_nap(slot):
                        issues = list(issues) + ["overlong_nap"]
                if "invented" in issues:
                    slot = self._degrade_to_neutral(cfg, minutes, prev)
                    raw = {}
                    degraded = True
                    self._bump_quality("invented")
                    self._bump_quality("fallback")
                    if self._log:
                        self._log.info(
                            "[humanoid_core] 日程质量闸：人设里找不到依据的"
                            f"「{'/'.join(invented)}」，已降级为中性段"
                            f"（{self._scope.role_id}）"
                        )
                elif "overlong_nap" in issues:
                    self._bump_quality("overlong_nap")
                    if self._log:
                        self._log.info(
                            "[humanoid_core] 日程质量闸：白天这一觉太长，"
                            f"重写后仍是「{slot.get('event')}」，放行"
                            f"（{self._scope.role_id}）"
                        )
                elif "needless_sleep" in issues:
                    self._bump_quality("needless_sleep")
                    if self._log:
                        self._log.info(
                            "[humanoid_core] 日程质量闸：今天已经睡够，"
                            f"重写后仍在排睡「{slot.get('event')}」，放行"
                            f"（{self._scope.role_id}）"
                        )
                elif "repeat" in issues:
                    self._bump_quality("repeat")
                    if self._log and cfg.debug_mode:
                        self._log.info(
                            "[humanoid_core] 日程质量闸：今天已经排过"
                            f"「{slot.get('event')}」，重写后仍重复，放行"
                        )

        # ---- 模型审查（v3.0.11）：写完之后回头看一眼「接不接得上」 ----
        # 上面那道闸只判得了能客观计数的事（今天重复、人设里没有依据）。而
        # 「上一段 09:05 在卧室结束，这一段 09:06 在超市」「倒杯水占了半小时」
        # 这类问题要两段摆在一起才看得见，只有模型判得了。判不通过不降级成
        # 中性段（那会把一天磨平成处境词），而是把原因交给它自己改一版再用。
        # 只审「换了一件事」的段：continue/resume 是接着做同一件事，没有转场可审。
        if getattr(cfg, "schedule_quality_gate", False) and not degraded:
            # 「接着做同一件事」有两种写法：模型自己说 continue/resume，或者它给的
            # 事件名与上一段一样（`_append_segment` 会把同名段并进上一段，不构成新
            # 段，也就没有转场可审）。两种都跳过审查；真换了一件事才审。
            raw_map = raw if isinstance(raw, Mapping) else {}
            continuing = (
                _truthy_flag(raw_map.get("continue"))
                or _truthy_flag(raw_map.get("resume"))
                or (
                    bool(prev)
                    and str(slot.get("event") or "") == str(prev.get("event") or "")
                )
            )
            if not continuing:
                verdict, payload = await self._review_segment(
                    cfg, slot, prev, self._fact_evidence(persona, cfg), minutes
                )
                if verdict == "fixed":
                    slot = payload
                    raw = {}
                    self._bump_quality("review_fixed")
                    if self._log:
                        self._log.info(
                            "[humanoid_core] 日程审查：这一段接不上，已按模型自己"
                            f"给的理由改成「{slot.get('event')}」"
                            f"（{self._scope.role_id}）"
                        )
                elif verdict == "reason":
                    retried = await self._rewrite_once(
                        cfg, prompt, ["review"], [], ignore_cooldown,
                        now_minute=minutes, prev=prev,
                        suspended=suspended or None, reason=payload,
                    )
                    if retried is not None:
                        raw, slot = retried
                        self._bump_quality("review_rewritten")
                        if self._log:
                            self._log.info(
                                "[humanoid_core] 日程审查：接不上（"
                                f"{payload or '模型未说明'}），已按原因重写"
                                f"（{self._scope.role_id}）"
                            )

        if suspended and str(slot.get("event") or "") == str(suspended.get("event") or ""):
            # resume 成功（或模型自己又排了同一件事）：挂起结束。
            self._clear_suspended()
            self._resumed_event = str(slot.get("event") or "").strip()

        self.last_error = ""
        self._retry_after = 0.0
        self._last_outcome = OUTCOME_OK
        if not degraded:
            self._set_source(SOURCE_LLM)
        if self._log:
            # 带上是哪个 bot：多开时日志里分不清谁生成了谁没生成。
            self._log.info(
                f"[humanoid_core] 日程已生成（{self._scope.role_id}）："
                f"{slot.get('event')} {slot.get('start')}–{slot.get('end')}"
            )
        if not frame:
            # 这一段是当天第一次：把顺带给出的日框存下来（没给就用确定句）。
            self._store_day_frame(
                str(raw.get("today") or "") if isinstance(raw, dict) else ""
            )
        return slot

    # ------------------------------------------------------------------
    # 质量闸（v3.0.6）：生成之后、装进日程之前的独立判定
    # ------------------------------------------------------------------

    def _bump_quality(self, name: str) -> None:
        """记一次质量闸命中。只计数，不打分——没有标注就没有分母。"""
        self._quality[name] = int(self._quality.get(name, 0)) + 1

    def _fact_evidence(self, persona: Persona | None, cfg: HumanoidConfig) -> str:
        """判「有没有依据」的原文：人设正文 + 用户补充偏好。"""
        parts = [
            str(getattr(persona, "prompt", "") or ""),
            str(getattr(cfg, "schedule_prompt_extra", "") or ""),
        ]
        return "\n".join(part for part in parts if part.strip())

    def _overlong_nap(self, slot: Slot) -> bool:
        """白天这一觉长得不像话（超过 DAY_SLEEP_MAX_MINUTES），且她并不困。

        「能一直睡」的另一半原因在这里：时长只有 12 小时的天花板，中午写一段
        四五个小时的觉也不算违规，于是模型一困就把下午整段划给睡觉。
        """
        if not slot_sleeps(slot):
            return False
        if in_scheduled_night(self.current_slots(), self._clock.now()):
            return False
        lo = parse_time(slot.get("start"))
        hi = parse_time(slot.get("end"))
        if lo is None or hi is None or hi <= lo:
            return False
        if hi - lo <= DAY_SLEEP_MAX_MINUTES:
            return False
        # 今天还没睡过：这一觉就是她今天的头一觉，很可能就是夜里那觉拖到现在
        # （夜猫子、周末补觉）。判它「太长」会把她正常的作息当成违规——这正是
        # REGRESSIONS 里「睡眠窗口不能按配置硬套」那条的老毛病。
        if self._sleep_today(self._clock.now().hour * 60 + self._clock.now().minute) <= 0:
            return False
        try:
            pressure = float(self._body_snapshot().get("sleep_pressure", 0.0) or 0.0)
        except (TypeError, ValueError):
            pressure = 0.0
        return pressure < CHANGE_TRIGGER_SLEEP_PRESSURE

    def _needless_sleep(self, slot: Slot, minutes: int) -> bool:
        """这一段排睡，但她根本不缺觉。

        三条同时成立才算：白天（不在日程里的夜里）、今天已经睡够了
        （NAP_REDUNDANT_MINUTES）、困意没越线。真困的时候困意会越线，prompt 里
        那条规则仍然放行——这里只拦「没事就睡」，不拦补觉。

        这条判据存在的原因：prompt 里从 v3.0.5 起就写着「午睡不是每天必须有的
        一站」，可中午还是照睡。规则是请求，不是保证；能不能不睡，得先让生成时
        知道她已经睡过多少（sleep_block），生成后再独立判一遍。
        """
        if not slot_sleeps(slot):
            return False
        if in_scheduled_night(self.current_slots(), self._clock.now()):
            return False
        if self._sleep_today(minutes) < NAP_REDUNDANT_MINUTES:
            return False
        try:
            pressure = float(self._body_snapshot().get("sleep_pressure", 0.0) or 0.0)
        except (TypeError, ValueError):
            pressure = 0.0
        return pressure < CHANGE_TRIGGER_SLEEP_PRESSURE

    def _quality_issues(
        self,
        slot: Slot,
        evidence: str,
        prev: Mapping[str, Any] | None = None,
    ) -> tuple[list[str], list[str]]:
        """这一段客观上的两个问题：今天重复 / 事实没有依据。"""
        if slot.get("status") or slot.get("sleep"):
            # 兜底的处境词不是「一件事」；睡段不涉及身份。
            return [], []
        issues: list[str] = []
        event = str(slot.get("event") or "")
        if self._is_repeat_today(event, prev):
            issues.append("repeat")
        invented = invented_facts(event, evidence)
        if invented:
            issues.append("invented")
        return issues, invented

    def _is_repeat_today(self, event: str, prev: Mapping[str, Any] | None = None) -> bool:
        """今天是不是已经把这件事排过了？（按 `_event_key` 归一化后比，不看标点）

        两种**不算**重复的情形，否则闸门会把正常的日子判成重复：
        * 紧接上一段接着做同一件事（continue/resume）——prompt 里明写着这不算新的一段；
        * 同一类事今天才第二次出现——规则给的上限是「一天最多两段」。
        第三次起才算重复。
        """
        key = _event_key(event)
        if not key:
            return False
        prev_key = _event_key(str((prev or {}).get("event") or ""))
        matched: list[Mapping[str, Any]] = []
        for seg in self.segments():
            if not isinstance(seg, Mapping):
                continue
            if seg.get("sleep") or seg.get("status"):
                continue
            if _event_key(str(seg.get("event") or "")) == key:
                matched.append(seg)
        if not matched:
            return False
        if len(matched) >= 2:
            return True
        only = matched[0]
        if prev_key == key and str(only.get("start") or "") == str((prev or {}).get("start") or ""):
            return False  # 就是紧接着的那一段，属于接着做
        return True

    async def _rewrite_once(
        self,
        cfg: HumanoidConfig,
        prompt: str,
        issues: list[str],
        invented: list[str],
        ignore_cooldown: bool,
        *,
        now_minute: int,
        prev: Mapping[str, Any] | None,
        suspended: Mapping[str, Any] | None,
        reason: str = "",
    ) -> tuple[Any, Slot] | None:
        """带着「哪里不对」再问一次模型；失败返回 None（由调用方决定放行还是降级）。

        `reason` 是模型审查时说出的原话（v3.0.11），原样递回去。
        """
        if self.gateway is None:
            return None
        try:
            result = await self.gateway.generate(
                prompt=prompt + _quality_fix_block(issues, invented, reason),
                chain=cfg.schedule_provider_ids,
                allow_global=cfg.schedule_allow_global_fallback,
                timeout=float(cfg.schedule_llm_timeout_seconds),
                # 重写只问一次：这条路每段都可能走到，放大 attempts 等于放大额度消耗。
                attempts_per_provider=1,
                retry_interval=float(cfg.schedule_retry_interval_seconds),
                purpose=PURPOSE,
                ignore_cooldown=ignore_cooldown,
                # 重写属于同一次决定：生成那一下已经刷新了闸门间隔，这里再
                # 按间隔判会当场 GATED——真实环境里这条重写路径从来没跑到过
                # （测试替身不查闸门，所以一直是绿的）。
                ignore_interval=True,
                role_label=str(self._scope.role_id or ""),
            )
        except Exception:
            return None
        if not result.ok:
            return None
        raw = extract_json_object(result.text)
        if raw is None:
            raw = extract_json_array(result.text)
        if raw is None:
            return None
        slot = parse_segment(
            raw,
            now_minute=now_minute,
            step=cfg.granularity_minutes,
            prev=prev,
            suspended=suspended,
        )
        if slot is None:
            return None
        return raw, slot

    async def _review_segment(
        self,
        cfg: HumanoidConfig,
        slot: Slot,
        prev: Mapping[str, Any] | None,
        persona_text: str,
        now_minute: int,
    ) -> tuple[str, Any]:
        """让模型审一遍这一段（v3.0.11）：返回 (结果, 载荷)。

        结果四种：
        * "ok"     —— 判它接得上，原样用；
        * "fixed"  —— 判它接不上，模型在同一份回答里给了改好的版本，载荷是新 slot；
        * "reason" —— 判它接不上，但没给改好的版本，载荷是原因原话（交给 _rewrite_once）；
        * "skip"   —— 调不通、超时、返回不是 JSON：保持原样，不让审查本身拖垮日程。

        审查调用同样绕开最小间隔：它属于同一次决定，生成那一下已经把闸门间隔
        刷新成 30~60 分钟，按间隔判会当场 GATED——这条路径就永远跑不到。
        """
        if self.gateway is None:
            return "skip", None
        prompt = _review_prompt(
            now_text=format_time(now_minute),
            prev=prev,
            slot=slot,
            persona_text=persona_text,
        )
        try:
            result: LLMResult = await self.gateway.generate(
                prompt=prompt,
                chain=cfg.schedule_provider_ids,
                allow_global=cfg.schedule_allow_global_fallback,
                timeout=float(cfg.schedule_llm_timeout_seconds),
                # 审查只问一次：它每段都跑，放大尝试次数等于放大额度消耗。
                attempts_per_provider=1,
                retry_interval=float(cfg.schedule_retry_interval_seconds),
                purpose=PURPOSE,
                ignore_interval=True,
                role_label=str(self._scope.role_id or ""),
            )
        except Exception:
            return "skip", None
        if not result.ok:
            return "skip", None
        raw = extract_json_object(result.text)
        if not isinstance(raw, Mapping):
            return "skip", None
        if "ok" not in raw:
            # 没给结论（模型答了别的东西、或旧版本提示词）——当成没审，不改原样。
            # 不能把「没回答」判成「不合格」：那会让每次生成都多一次重写。
            return "skip", None
        ok = raw.get("ok")
        if isinstance(ok, str):
            ok = ok.strip().lower() in ("true", "1", "yes", "是")
        if bool(ok):
            return "ok", None
        reason = str(raw.get("reason") or "").strip()
        fixed = raw.get("fixed")
        if isinstance(fixed, Mapping):
            new_slot = parse_segment(
                dict(fixed),
                now_minute=now_minute,
                step=cfg.granularity_minutes,
                prev=prev,
            )
            if new_slot is not None:
                return "fixed", new_slot
        return "reason", reason

    def _degrade_to_neutral(
        self,
        cfg: HumanoidConfig,
        now_minute: int,
        prev: Mapping[str, Any] | None,
    ) -> Slot:
        """降级：给中性处境词（与模型不可用时同一条兜底路径）。"""
        slot = dynamic_segment(
            cfg,
            self._body_snapshot(),
            now_minute=now_minute,
            step=cfg.granularity_minutes,
            prev=prev,
            seed=self._seed(),
            asleep_minutes=self.sleeping_minutes(now_minute),
        )
        self._set_source(SOURCE_TEMPLATE)
        return slot

    def _set_source(self, source: str) -> None:
        if str(self._scope.get_self("schedule_source", "") or "") != source:
            self._scope.set_self("schedule_source", source)

    def _append_segment(self, slot: Slot, today: str) -> None:
        """把这一段接在今天后面，并处理与上一段的衔接。

        - 上一段还没到点就被换掉（她提前结束去做别的）：end 收到新段起点，不留重叠；
        - 上一段过完了、到新段开始之间有空档：**空档就空着，不拿旧事件去填**。以前这里
          会把上一段的 end 顺延到新段起点（间隔 ≤30 分钟时），理由是「窟窿里 soma 读不到
          「她在吃饭」」。代价是编造时长：模型写 5 分钟的「倒杯水」，而下一次决策要等节流
          间隔（schedule_min_interval_minutes，默认 30 分钟），5 分钟后没人接手，空档被顺延
          进去，于是她「喝了 30 分钟」。而 soma 的饥饿只在**用餐段**里归零
          （is_eating_time → is_meal_event），顺延一件非用餐的事对饥饿毫无作用。
          空档期 current_slot() 本来就给中性「自由活动」，不需要谁去补。
        - 睡觉跨午夜：今天截到 24:00，剩下的结转到明天（`segment_carry`）。
        """
        now = self._clock.now()
        minutes = now.hour * 60 + now.minute
        # 跨天第一段会走这里（没有结转时 `_migrate_new_day` 提前返回）：先把昨天归档，
        # 再覆盖 `daily_schedule`，否则昨天那份在这里被就地丢掉。
        self._archive_previous_day(today)
        # 排序与「覆盖现在的那一段」都走 `_ordered_segments` / `_active_and_prev`：
        # 与读路径同一份口径（盲取 `segs[-1]` 会截错段、留下重叠，
        # `active_segment`/`day_phrases` 于是看错她在做什么）。
        segs = self._ordered_segments()
        previous, _ = self._active_and_prev(minutes, segs=segs)
        target = next(
            (index for index, item in enumerate(segs) if item is previous),
            len(segs) - 1,
        )

        carry_end = str(slot.pop("carry_end", "") or "")
        new_start = parse_time(slot.get("start"))
        # 旧的挂起（如果有）：装完这一段后，如果她回去接着做原来那件事，挂起结束。
        suspended_before = self._suspended_slot()
        suspended_event_before = str(suspended_before.get("event") or "").strip()
        truncated: Slot | None = None
        truncated_remaining = 0
        if segs and new_start is not None:
            last = dict(segs[target])
            hi = parse_time(last.get("end"))
            lo = parse_time(last.get("start"))
            if hi is not None and lo is not None:
                # 只有「这一段本来还没到点」才算被打断（#65）：`new_start` 是向下
                # 对齐到刻度的，比「现在」早最多一个刻度。上一段 10:05 已经做完、
                # 现在 10:10 时，`hi > new_start`（605 > 600）照样成立，于是她把一件
                # 已经做完的事当成「被打断，还剩 5 分钟」挂起来，下次生成还会真的
                # 去「接着做」那件早就结束的事。
                if hi > new_start and hi > minutes:
                    # 她提前结束去做别的：收到新段起点，不留重叠。
                    if new_start > lo:
                        # 这一件事被从中间截断了：记下它（新段换了事才挂起，见下）。
                        truncated = last
                        truncated_remaining = max(0, hi - new_start)
                        last["end"] = format_time(new_start)
                        segs[target] = last
                    else:
                        # 上一段刚开始就被换掉：它没有存在过，丢掉比留一条零长度记录干净。
                        segs.pop(target)


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
                # 同一件事接着做时，新一段的 location/emotion/ongoing 是**更新过的**
                # （「专注」→「有点累」、「还差 4 份」→「还差 2 份」）。旧写法只同步
                # `end`/`energy_rate`，于是进度文字永远停在第一版，`_note_ongoing(installed)`
                # 拿到的也是旧段。
                for key in ("location", "emotion", "ongoing", "wants", "dreams", "status"):
                    if key in slot:
                        last[key] = slot[key]
                segs[-1] = last
                installed = last

        if installed is not slot:
            slot = None
        else:
            segs.append(slot)
        if installed:
            self._note_ongoing(installed)

        # 被从中间截断的那件事：新段换了事才挂起（同一件事只是把 end 接上，不算
        # 被打断）。只存这一件，新的覆盖旧的；`schedule_pause_ttl_minutes` 之后
        # 过期作废（读取时由 `_suspended_slot` 顺手清）。
        suspended_update: dict[str, Any] | None = None
        if (
            truncated is not None
            and str(installed.get("event") or "") != str(truncated.get("event") or "")
        ):
            truncated_event = _clip_event(str(truncated.get("event") or ""))
            if truncated_event:
                suspended_update = {
                    "event": truncated_event,
                    "location": str(truncated.get("location") or "").strip()[:16],
                    "emotion": str(truncated.get("emotion") or "").strip()[:12],
                    "ongoing": str(truncated.get("ongoing") or "").strip()[:40],
                    "wants": bool(truncated.get("wants")),
                    "remaining_minutes": int(truncated_remaining),
                    # 被截断那段的能量速率一起带走（#70）：resume 时用它当默认值，
                    # 否则「回去接着做」的段按 0.0 算，等于白做不耗体力。
                    "energy_rate": clamp_rate(truncated.get("energy_rate", 0.0)),
                    # 渲染时按 `end_at - now` 现算剩余（#69）：只存分钟数的话，
                    # 三小时内每次注入都念同一个数字，和她实际剩下的时间对不上。
                    "end_at": float(self._wall_clock()) + float(truncated_remaining) * 60.0,
                    # 解析时判过的睡着标记一起带走：被打断的这事可能是段没有睡词的睡，
                    # 只看事件名会把「回去接着睡」重建成一个醒着的段。
                    "sleep": bool(slot_sleeps(truncated)),
                    # 处境词标记也带走：挂起的是兜底给的“有点饿了”，回去接着做时
                    # 不能又被当成动作渲染成「上午在有点饿了」。
                    "status": bool(truncated.get("status")),
                    "at": float(self._wall_clock()),
                    "ttl_minutes": _pause_ttl_minutes(self.config),
                }
        # 她回去接着做原来那件事了（resume，或模型自己又排了同一件事）：挂起结束。
        # 两种来源都要认：`_generate_segment` 已经清掉 state 时，`_resumed_event` 记着
        # 这次装上的就是原来那件事；直接调 `_append_segment` 的旧路径则按 state 判。
        installed_event = str(installed.get("event") or "").strip()
        resumed_previous = bool(
            (suspended_event_before and installed_event == suspended_event_before)
            or (self._resumed_event and installed_event == self._resumed_event)
        )
        cap = max(6, min(SEGMENTS_DAY_CAP, int(self.config.schedule_max_slots)))
        if len(segs) > cap:
            # 只留「最晚的 cap 段」会先把 00:00 起的跨夜觉丢掉：结转段恒为
            # `start="00:00"`（`_migrate_new_day` / `_carry_slot`），按时间序排在
            # 最前。丢了它，「她今天没睡」——起床点为空、`sleeping_minutes` 数不到
            # 这一觉、asleep 永远 False。所以先保睡段，再按时间序取最晚的几段。
            sleeps = [item for item in segs if slot_sleeps(item)]
            rest = [item for item in segs if not slot_sleeps(item)]
            if len(sleeps) >= cap:
                keep = sleeps[-cap:]
            else:
                keep = sleeps + rest[-(cap - len(sleeps)) :]
            segs = sorted(keep, key=lambda item: parse_time(item.get("start")) or 0)
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
                # 梦跟着跨夜结转走（见 `_migrate_new_day` 与 `_carry_slot`）。
                "dreams": _clean_dreams(installed.get("dreams")),
            }
            updates[CARRY_KEY] = carry
        elif str(self._scope.get_self(CARRY_KEY, "") or ""):
            # 新的一段不是跨夜睡眠：旧的结转不该再留着
            updates[CARRY_KEY] = None
        if resumed_previous:
            # 回去接着做了：被挂起的那件事已经结束，不再保留。
            updates[self.SUSPENDED_KEY] = None
        elif suspended_update is not None:
            # 刚被从中间截断的那件事：记下来，下一次安排时可以选回去接着做。
            updates[self.SUSPENDED_KEY] = suspended_update
        self._scope.update_self(**updates)
        # 醒来（上一段在睡、这一段不是睡）：睡着期间听到的话已经随这一段的 prompt
        # 递进去了，装上就清空——否则下一段还会把夜里那几句当「刚刚」再说一遍（#66）。
        # 仍然在睡就留着继续累积。
        if (
            self._heard_while_asleep
            and isinstance(previous, dict)
            and slot_sleeps(previous)
            and not slot_sleeps(installed)
        ):
            self._heard_while_asleep = []
        # 一次 resume 只影响刚装上的这一段。
        self._resumed_event = ""

        # 本窗的骰子已经用掉了：刚装上的这一段，本窗内不再重掷。
        self._roll_hit = False

        if self.on_install is not None:
            try:
                self.on_install(previous, installed)
            except Exception:
                pass
        if self._log and self.config.debug_mode:
            self._log.info(
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

    def last_dreams(self) -> list[str]:
        """当天最近一段睡眠的梦（没有/旧日程没记 → 空列表）。

        给聊天注入的「刚醒时可提梦」与契约导出共用这一个口径；读取失败一律退化成
        空列表——注入热路径上的一个坏日程不该把整轮上下文带走。
        """
        try:
            now = self._clock.now()
            phrases = day_phrases(
                self.current_slots(), now.hour * 60 + now.minute
            )
            return _clean_dreams(phrases.get("dreams"))
        except Exception:
            return []

    def status(self) -> dict:
        today = self._clock.today_str()
        segs = self.segments()
        active = self.active_segment()
        # 「被打断之前她在做的那件事」与打断闸门的只读快照。任何一边坏了都不能
        # 把 /查看日程 整条带崩：分别兜底成 {} / 空闸门。
        try:
            suspended = self.suspended_info()
        except Exception:
            suspended = {}
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
            # 当天最近一段梦：`/查看日程`、诊断与契约都从这里取（旧日程 → []）。
            "dreams": self.last_dreams(),
            "source": self.source,
            "source_text": self.source_text,
            "persona": str(self._scope.get_self("schedule_persona", "") or "") or self.last_persona,
            "generated_at": self._scope.get_self("schedule_generated_at", ""),
            "last_error": self.last_error,
            "generating": self.generating,
            "retry_after": self.retry_after,
            "due": self._due_peek(),
            "pending_today": str(self._scope.get_self("today_date", "") or "") != today,
            # 被打断之前她在做的那件事（没有 → {}）；`/拟人 诊断` 与状态页用。
            "suspended": suspended,
            # 打断闸门：pending/ttl_left/cooldown_left/quiet_left 等只读字段。
            "interrupt": self._interrupt_status(),
            # 质量闸计数（v3.0.6）：repeat/invented/rewrite/fallback。
            "quality": dict(self._quality),
        }

    async def aclose(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
