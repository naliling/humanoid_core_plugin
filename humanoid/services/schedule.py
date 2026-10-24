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
from ..traits import detect as detect_traits, trait_hints
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
# 这只是**硬天花板**：真正生效的是配置项 `schedule_max_slots`（面板上限就是 96）。
# 以前这里写死 48，把用户填 96 砍成 48——面板上写着却不起作用的数比没有更糟。
SEGMENTS_DAY_CAP = 96
# prompt 里带多少段「已经过完的时段」。
SEGMENTS_PROMPT_HISTORY = 8
# 上一段结束到新一段之间隔了这么久以内，才把它顺延补上；隔得更久说明插件停机了，
# 那段时间她做了什么没人知道，不能拿旧事件把窟窿填上。
SEGMENT_GAP_MERGE_MINUTES = 30

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


def routine_prompt(cfg: HumanoidConfig, first_index: int = 10) -> str:
    """作息那一条怎么写进分段 prompt。

    默认让模型从人设里读她几点睡；只有用户显式打开「日程贴合夜间窗口」时，才把
    夜间窗口当成硬约束递进去（分段生成时同样生效：到了点就该排睡眠）。
    """
    if not cfg.night_mode_enabled or not cfg.schedule_follow_night_window:
        return (
            f"{first_index}. 几点睡、几点起由她是谁决定：从人设里读她的年纪感、工作或学业、"
            "生活习惯，结合当前的身体数值（困意、睡眠债）由你判断；"
            "人设里写着的作息保持不动。"
            # B2：别把一套固定作息当默认（有人设是半夜活动/下午才起）；午睡不是必须。
            # 不点名具体作息类型——那等于替模型做决定（test_v2142_routine 盯着）。
            "人设没写清楚时，按她的年纪感与生活习惯推，不要套用一套固定作息；"
            "午睡/小憩不是每天必须有的一站。\n"
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


# 日程事件名是模型写的自由文本，有的自带动词与介词（"坐在客厅沙发上看书"）。外面再套
# 一个「在」就成「现在在坐在客厅沙发上看书」——模型会照着这句病句往下说。
# 拼接前先看一眼开头：动词/介词开头的直接接，其它才补「在」。
_ACTION_HEADS = frozenset("在坐躺站靠趴蹲爬走跑跳吃喝做玩看读写听说聊打开学忙醒起洗梳穿等逛买煮烤拖练画唱骑划")

_EVENT_PHRASE_MAX = 24

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


def _event_phrase(event: str) -> str:
    """把时段名削成能放进一句口语里的动作。

    原来直接按 16 字硬截，「坐在客厅沙发看书并吃点水果」会被砍成「坐在客厅沙发看书并吃点水」
    这种断句。改成先按标点收尾，实在没有标点才截。
    """
    text = str(event or "").strip()
    for affix in ("安排", "活动", "时间", "（", "("):
        cut = text.find(affix)
        if cut > 1:
            text = text[:cut]
    text = text.strip(" -·、，。/~")
    if len(text) <= _EVENT_PHRASE_MAX:
        return text
    head = text[:_EVENT_PHRASE_MAX]
    cut = max((head.rfind(ch) for ch in "，。；、,; "), default=-1)
    if cut >= _EVENT_PHRASE_MAX // 2:
        return head[:cut].strip(" -·、，。/~")
    return head.rstrip(" -·、，。/~")


def _with_at(lead: str, phrase: str) -> str:
    """把时段词或「现在」与动作拼成一句，不出现「在坐在」这种叠字。"""
    body = str(phrase or "").strip()
    if not body:
        return ""
    if body.startswith("在") or body[0] in _ACTION_HEADS:
        return f"{lead}{body}"
    return f"{lead}在{body}"


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

    `dreams` 是当天**最近一段睡眠**的梦：契约和社交层拿它说「她昨晚梦到什么」。
    旧日程没有这个字段，所有旧 slot 读出来都是空列表——调用方不需要区分
    「没有梦」和「旧版本没记」。
    """
    doing = ""
    done: list[str] = []
    upcoming: list[str] = []
    dreams: list[str] = []
    # 把 slot 本体一起带上：梦要从**同一段**里取，不能只留事件名再去反查。
    ordered: list[tuple[int, int, str, Slot]] = []
    for slot in slots:
        lo = parse_time(slot.get("start"))
        hi = parse_time(slot.get("end"))
        if lo is None or hi is None or hi <= lo:
            continue
        ordered.append((lo, hi, str(slot.get("event") or "").strip(), slot))

    for lo, hi, event, _slot in ordered:
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
                done.insert(0, _with_at(period_of(lo), phrase))
            continue
        if lo - now_minutes <= DAY_UPCOMING_MINUTES:
            phrase = _event_phrase(event)
            if phrase:
                # 说「夜里要睡了」而不是「夜里就该睡了」：后者听着像谁在管她。
                upcoming.append(
                    f"{period_of(lo)}要睡了" if is_sleep_event(event)
                    else f"{period_of(lo)}还要{phrase}"
                )

    # 梦：按开始时间排序后取**最后一段睡眠**（跨午夜结转的那一截也算）。
    # 不按 end 取：23:00→24:00 与 00:00→07:00 是同一觉拆成的两段，按 end 比
    # 24:00 会永远赢过 07:00；按开始时间取最后一段就对了（两段的梦本来就该一致）。
    for _lo, _hi, event, slot in ordered:
        if is_sleep_event(event):
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
        "next": upcoming[:max(0, int(future_limit))],
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
# 原来那 5 条是写死在代码里的，每次调用都一模一样。模型对 few-shot 的模仿远强于
# 对抽象规则的服从，于是她的一天反复长成那几件事（「翻翻最近想去的那几家店」）。
# 改成按（角色+日期）抽一批，每天看到的范例不同；顺带把「翻个身」这类没有信息量
# 的微动作从示范里去掉。
EXAMPLE_POOL: tuple[dict[str, Any], ...] = (
    {"event": "啃两口昨天剩的面包", "location": "厨房", "emotion": "有点没睡醒", "energy_rate": 0.02, "minutes": 22},
    {"event": "翻翻最近想去的那几家店，顺路买双袜子", "location": "商场", "emotion": "有点期待", "energy_rate": -0.08, "minutes": 145},
    {"event": "拐去那家一直想去的书店", "location": "街角书店", "emotion": "有点期待", "energy_rate": -0.05, "minutes": 60, "wants": True},
    {"event": "把阳台那几盆花浇一遍、修掉黄叶", "location": "阳台", "emotion": "踏实", "energy_rate": -0.03, "minutes": 35},
    {"event": "回一封拖了两天的邮件", "location": "书桌前", "emotion": "终于动手了", "energy_rate": -0.04, "minutes": 40},
    {"event": "煮一锅面，就着剩菜吃完", "location": "厨房", "emotion": "饿坏了", "energy_rate": 0.05, "minutes": 30},
    {"event": "出门取个快递，顺便绕小区走两圈", "location": "楼下", "emotion": "透透气", "energy_rate": 0.02, "minutes": 45},
    {"event": "窝在沙发上把攒的两集看完", "location": "客厅", "emotion": "放松下来", "energy_rate": 0.03, "minutes": 95},
    {"event": "跟朋友打一通电话，聊了聊近况", "location": "卧室", "emotion": "有点高兴", "energy_rate": -0.04, "minutes": 55},
    {"event": "把桌上的东西归拢、擦一遍", "location": "书桌前", "emotion": "看着清爽些了", "energy_rate": -0.05, "minutes": 40},
    {"event": "认真做一顿饭，切切炒炒", "location": "厨房", "emotion": "挺享受", "energy_rate": -0.06, "minutes": 80},
    {"event": "去楼下超市补点菜和日用品", "location": "超市", "emotion": "例行公事", "energy_rate": -0.07, "minutes": 50},
    {"event": "坐在窗边发会儿呆，什么也没干", "location": "窗边", "emotion": "空落落的", "energy_rate": 0.04, "minutes": 30, "wants": True},
    {"event": "把攒下的照片挑一挑、删掉重复的", "location": "床上", "emotion": "有点走神", "energy_rate": 0.01, "minutes": 65},
    {"event": "写点东西，开了个头又删了", "location": "书桌前", "emotion": "有点卡", "energy_rate": -0.05, "minutes": 70},
    {"event": "出门办那件拖了一阵的事", "location": "外面", "emotion": "得办完", "energy_rate": -0.09, "minutes": 130},
    {"event": "洗个热水澡，把今天收拾掉", "location": "浴室", "emotion": "慢慢松下来", "energy_rate": 0.06, "minutes": 35},
    {"event": "接着整理那份个案笔记", "location": "书桌前", "emotion": "还差几份", "energy_rate": -0.05, "minutes": 110, "ongoing": "整理那份个案笔记（还差 4 份）"},
)
EXAMPLE_COUNT = 4


def examples_for(seed: list[Any] | None, count: int = EXAMPLE_COUNT) -> list[dict[str, Any]]:
    """按（角色+日期）抽一批范例：同一天稳定，不同天不同。"""
    pool = list(EXAMPLE_POOL)
    rnd = random.Random("|".join(str(x) for x in (seed or [])) or "default")
    rnd.shuffle(pool)
    return pool[: max(1, int(count))]


def _render_examples(items: list[dict[str, Any]]) -> str:
    rows = []
    for item in items:
        rows.append("   · " + json_dumps_example(item))
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
        + world_block
        + history_block
        + extra_block
        + night_line
        + interrupt_line
        + event_line
        + "【怎么决定】\n"
        "1. 身体读数是决策输入：饿到不行就去吃，困到不行就去睡——怎么权衡由你决定。\n"
        "2. 一件事做到一半接着做很正常（continue=true）；做很久了、或身体不允许时就换。\n"
        # 「怎么决定」原来只有上面两条，全是「什么时候该换」。这等于只教了它何时停下，
        # 没教它一段能有多短——于是模型把每段都当成一件大事，minutes 天然偏长。
        "3. minutes 要真的反映这件事要多久：\n"
        "   喝口水、吃两口、回个消息就是 15~30 分钟，不用非撑满；\n"
        "   出门办事、见人、认真做一件事可以 1~3 小时；\n"
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
        + _render_examples(examples_for(seed))
        + "\n"
        "   event 写具体在做什么、location 写她在哪，两者都要具体到能说出口的程度；\n"
        "   不要写「翻个身」「换个姿势」这种没有信息量的微动作——睡眠就直接写「睡眠」，\n"
        "   其它事要是一件说得出口的事（做了什么、去了哪），不是姿势或状态。\n"
        "3. continue：true = 接着做手上这件事（event/location/emotion 留空，速率沿用）；"
        "false = 换一件事。\n"
        f"4. minutes：这一段做多久。最短 {SEGMENT_MIN_MINUTES} 分钟，一般的事最长 "
        f"{SEGMENT_MAX_MINUTES} 分钟；睡觉可以睡一整夜（最长 {SEGMENT_SLEEP_MAX_MINUTES // 60} 小时，"
        "跨过半夜也没关系）。这个是**上下限，不是目标值**——"
        "多数日常小事落在 20~60 分钟，真正要花时间的事才写长。\n"
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
    if sleeping:
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
            # 睡到生物钟夜结束（她的自然醒点）；跨午夜的部分由结转接到明天。
            logical_end = night_end if night_end > now_minute else night_end + DAY_MINUTES
            minutes = max(SEGMENT_MIN_MINUTES, min(SEGMENT_SLEEP_MAX_MINUTES, logical_end - now_minute))
        else:
            # 白天（或不在生物钟夜里）已经睡着：给「睡到自然醒」的兜底，最多 90 分钟。
            # 旧写法对不在夜里的 asleep 写死 120 分钟，一觉被排成两小时的既定事实；
            # 滚动分段的语义是醒了再重新判断，所以收短。
            minutes = 90
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
        self.last_error = ""
        self._retry_after = 0.0
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
        # 待处理的事件（别人刚说的话够重）：一次性，refresh_due 消费掉。
        self._event_at = 0.0
        self._event_phrase = ""
        self._last_event_at: float | None = None

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

    # 「还没做完的事」最多留 3 件、每件最多记 4 天。
    ONGOING_KEY = "ongoing_tasks"
    ONGOING_MAX = 3
    ONGOING_TTL_DAYS = 4
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

    def ongoing_tasks(self) -> list[str]:
        """她手上还没做完的那几件事（跨天跟着她）。"""
        raw = self._scope.get_self(self.ONGOING_KEY)
        if not isinstance(raw, list):
            return []
        out: list[str] = []
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
                out.append(text)
        return out[: self.ONGOING_MAX]

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
        # 堆成好几条同一件事。这里按**前六个字**归一：那是「整理那份/那份个案/写完那
        # 封」这种稳定前缀，进度部分在后面。
        stem = text[:6]
        kept: list[dict[str, Any]] = []
        for item in items:
            what = str(item.get("what", "") or "").strip()
            if not what or what[:6] == stem:
                continue
            try:
                at = float(item.get("at", 0.0) or 0.0)
            except (TypeError, ValueError):
                at = 0.0
            kept.append({"what": what, "at": at})
        kept.append({"what": text, "at": self._wall_clock()})
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

    def _gate_allows(self) -> bool:
        """节流闸门：没人看着 / 间隔没到 / 今日预算用完时，都算「不需要决定」。

        问在「本来该生成」之后而不是之前：正常无事可做的那几百个 tick 不该去
        反复查闸门，更不该把 _roll_change 的骰子白白掷掉。
        """
        gate = getattr(self.gateway, "gate", None) if self.gateway is not None else None
        if gate is None:
            return True
        return gate.check(PURPOSE).allowed

    def note_interruption(self, *, is_group: bool = False, now: float | None = None) -> None:
        """一条私聊消息来了——满足条件时记一次「有人打破安静来找她」。

        两道比例尺：距上一条私聊至少隔 `INTERRUPT_QUIET_GAP_SECONDS`（否则算
        「消息还热着」，很多人同时聊时这个口子就自然不再触发），且距上次
        算作来访至少隔 `INTERRUPT_COOLDOWN_SECONDS`。群消息不算「来找她」。

        只设不排：真正的决定在下一次 refresh_due 里做（还要过闸门/频率/预算）。
        冷却基准 `_last_interrupt_at` 与待处理的标记分开：消费掉标记不能
        顺手把冷却也清了。
        """
        if is_group:
            return
        t = float(self._monotonic() if now is None else now)
        previous = self._last_message_at
        self._last_message_at = t
        if previous is not None and t - previous < INTERRUPT_QUIET_GAP_SECONDS:
            return
        if (
            self._last_interrupt_at is not None
            and t - self._last_interrupt_at < INTERRUPT_COOLDOWN_SECONDS
        ):
            return
        self._last_interrupt_at = t
        self._interrupt_at = t

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
        """待处理的事件措辞；没有或过期了返回空串。"""
        if not self._event_phrase or self._event_at <= 0:
            return ""
        if self._monotonic() - self._event_at > EVENT_TTL_SECONDS:
            if clear:
                self._event_at = 0.0
                self._event_phrase = ""
            return ""
        return self._event_phrase

    def _consume_event(self) -> str:
        """取走一次待处理事件（一次性）。返回的是原始措辞。

        生成被闸门挡回时要原样还回（只把标记放回去、不重新计时），
        否则反复被挡就能把保鲜期一次次续满——与 `_consume_interruption` 同理。
        """
        phrase = self._fresh_event()
        if not phrase:
            return ""
        self._event_at = 0.0
        self._event_phrase = ""
        return phrase

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
            "ttl": EVENT_TTL_SECONDS,
        }

    def _fresh_interruption(self, *, clear: bool = True) -> bool:
        """有没有还新鲜（未过期）的待处理「来访」；过期的顺手清掉。

        不设保鲜期时，标记可以一睡几个小时（睡着时不被消费）或卡在闸门后面，
        等到下一次重排才被取用——那时提示词里那句「刚刚有人来找她」就是假话。

        `clear=False` 供只读路径（`_due_peek`）用：连「把过期标记置 0」这个写操作都不做。
        """
        if self._interrupt_at <= 0:
            return False
        if self._monotonic() - self._interrupt_at > INTERRUPT_TTL_SECONDS:
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
        if is_sleep_event(event):
            # 睡着：这一觉睡到这段结束为止，夜里不重掷、不越线打断。
            # **消息也不算**：多用户部署里夜里总有人发消息，让消息能撕被子，
            # 她就没有一个完整的觉了（这个口子原来只按「一个话多的人」设计，
            # 没扛住「很多人」——现在只走睡觉保护，起床由这段自然结束）。
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
            segs = self.segments()
            active = self._covering(segs, minutes)
            prev = segs[-1] if segs else None
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
                event_phrase = self._consume_event()

        slot: Slot | None = None
        if attempt_llm:
            self._generating = True
        try:
            if attempt_llm:
                slot = await self._generate_segment(
                    cfg, now, minutes, prev, elapsed, ignore_cooldown,
                    interrupted_ts=interrupted_ts, event_phrase=event_phrase,
                )
            async with self._lock:
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
        finally:
            if attempt_llm:
                self._generating = False

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
        interrupted_ts: float = 0.0,
        event_phrase: str = "",
    ) -> Slot | None:
        interrupted = interrupted_ts > 0.0
        if self.gateway is None:
            self.last_error = "LLM Gateway 未初始化"
            return None

        persona = await self._resolve_persona()
        frame = await self._ensure_day_frame()
        history, history_note = self._history_for_prompt()
        world = self._world_snapshot()
        body = self._body_snapshot()
        # 困意越线与「上一段是不是睡眠」都是这一刻的处境：现取一次，和本地兜底走
        # 同一条阈值（CHANGE_TRIGGER_SLEEP_PRESSURE），不让 prompt 与兜底各说各的。
        try:
            sleep_pressure = float(body.get("sleep_pressure", 0.0) or 0.0)
        except (TypeError, ValueError):
            sleep_pressure = 0.0
        prev_sleep = bool(prev) and is_sleep_event(str((prev or {}).get("event") or ""))
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
                role_label=str(self._scope.role_id or ""),
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
                if interrupted:
                    # 这次「来访」只是没挤进队列（闸门被别的调用占着），**原样**还回去：
                    # 保留原始时间戳，不重新计时——否则反复被挡就能一直续命、突破保鲜期。
                    self._interrupt_at = interrupted_ts
                if event_phrase:
                    # 同理：事件被挡回时原样还回，不重新计时。
                    self._event_phrase = event_phrase
                    self._event_at = self._monotonic() - 1.0
                if self._log and cfg.debug_mode:
                    self._log.debug(f"[humanoid_core] 日程被节流跳过: {result.detail}")
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
        now = self._clock.now()
        minutes = now.hour * 60 + now.minute
        # 日程可能乱序（carry 拼接、旧版整表迁移、`_migrate_new_day` 的部分写入）：
        # `_covering` 取**第一个**命中，而下面的截断原本盲取 `segs[-1]`，两者不是同一段
        # 时会截错段、留下重叠（`active_segment`/`day_phrases` 于是看错她在做什么）。
        # 先按起点排序，再显式定位「覆盖现在的那一段」。
        segs.sort(key=lambda item: parse_time(item.get("start")) or 0)
        previous = self._covering(segs, minutes) or (segs[-1] if segs else None)
        target = next(
            (index for index, item in enumerate(segs) if item is previous),
            len(segs) - 1,
        )

        carry_end = str(slot.pop("carry_end", "") or "")
        new_start = parse_time(slot.get("start"))
        if segs and new_start is not None:
            last = dict(segs[target])
            hi = parse_time(last.get("end"))
            lo = parse_time(last.get("start"))
            if hi is not None and lo is not None:
                if hi > new_start:
                    # 她提前结束去做别的：收到新段起点，不留重叠。
                    if new_start > lo:
                        last["end"] = format_time(new_start)
                        segs[target] = last
                    else:
                        # 上一段刚开始就被换掉：它没有存在过，丢掉比留一条零长度记录干净。
                        segs.pop(target)
                elif hi < new_start and new_start - hi <= SEGMENT_GAP_MERGE_MINUTES:
                    last["end"] = format_time(new_start)
                    segs[target] = last

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
                for key in ("location", "emotion", "ongoing", "wants", "dreams"):
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
                # 梦跟着跨夜结转走（见 `_migrate_new_day` 与 `_carry_slot`）。
                "dreams": _clean_dreams(installed.get("dreams")),
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
        }

    async def aclose(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
