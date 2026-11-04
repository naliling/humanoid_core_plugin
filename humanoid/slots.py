"""日程时段（slot）的解析、规范化与查询。"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

DAY_MINUTES = 24 * 60
MAX_ENERGY_RATE = 0.3

Slot = dict[str, Any]

_TIME_PATTERN = re.compile(r"^(\d{1,2})\s*[:：.]\s*(\d{1,2})$")

FILLER_SLOT = {
    "event": "自由活动/休息",
    "location": "家中",
    "emotion": "随意",
    "energy_rate": 0.0,
}
NIGHT_FILLER_SLOT = {
    "event": "夜间休息",
    "location": "卧室",
    "emotion": "困倦",
    "energy_rate": 0.1,
}

# 日程里算「她在睡」的事件名关键词。不能只用「觉」：「觉得」会把一句普通话误判成睡着。
SLEEP_WORDS = ("睡", "眠", "午休", "小憩", "懒觉", "入睡")
# 「失眠」「睡不着」「熬夜」都含睡词/眠字，却正相反：她在床上醒着。裸「眠」保留，
# 由这张排除表兜住（审核 状态9）。
SLEEP_EXCLUDE_WORDS = ("失眠", "睡不着", "睡眠不足", "睡眠浅")
# 「熬夜」**不在这张表里**：它是醒着的动作，但句子里往往还带着真正的睡（「熬夜后睡到
# 中午」）。放在排除表里会连后半句的睡一起否掉——排除表在睡词之前生效，等于让一个
# 醒词压掉一句明确的睡。它现在归 AWAKE_WORDS：那里只否掉复合判据，睡词照样赢。
# 吃饭优先于「午休」：「午餐与午休发呆」是在吃饭，不是在睡觉。
MEAL_WORDS = ("餐", "吃", "饭", "外卖", "食堂")


# 睡词与醒词会同时出现在一句话里（「洗漱后上床睡觉」），所以判定顺序固定为：
# **强醒 → 强睡 → 默认醒着**（审核 #58）。「洗」这类睡前也会做的弱醒词不单独成档：
# 它在 is_sleep_event 里等同「没有线索」，也**不**挡 slot_sleeps 的复合判据（见该函数注释）。
# 旧写法没有「强醒」这一档：只要句子里有睡字就算睡，「起床后洗了个澡，准备睡个午觉」
# 会被判成她正在睡——一个刚起床的人被记成睡觉，比漏判更糟。
STRONG_AWAKE_WORDS = ("起床", "醒来", "睁眼", "醒了")
# 「躺下」「上床」是睡的动作而不是睡的词：补上它们，「洗漱后回卧室躺下」这类没有
# 睡字的真实睡段才判得出来（原来要靠 slot_sleeps 的复合判据，而那条被「洗」挡住了）。
STRONG_SLEEP_WORDS = SLEEP_WORDS + ("躺下", "上床")


def is_sleep_event(event: Any) -> bool:
    """事件名是否表示她在睡。顺序：强醒 → 强睡 → 默认（醒着）。"""
    text = str(event or "")
    if any(word in text for word in MEAL_WORDS):
        return False
    if any(word in text for word in SLEEP_EXCLUDE_WORDS):
        return False
    if any(word in text for word in STRONG_AWAKE_WORDS):
        return False
    if any(word in text for word in STRONG_SLEEP_WORDS):
        return True
    # 「洗」与「没有任何线索」在这里同归醒着：洗过澡不构成睡眠证据。它也不挡
    # slot_sleeps 的复合判据（否则「洗漱完在卧室安静待着」这种没有睡字的真睡段会被判醒）。
    return False


def is_meal_event(event: Any) -> bool:
    text = str(event or "")
    return any(word in text for word in MEAL_WORDS)

# 「她其实在睡」的第二道判据（v3.0.2）。只看事件名里有没有睡字时，模型写的安静段落
# 会被判成「醒着」：「回到卧室，将浅蓝瞳孔轻阖，呼吸渐沉」「彻底合上眼，任由困意把身体
# 拖进深海」——一个睡字都没有，可她确实在床上睡着。误判是连锁的：soma 按「醒着」记睡眠
# 压力（每小时 +7.5，一整天下来顶到 82）、按醒着掉精力，提示词里的「她醒着」又和日程里
# 的卧床描述互相打脸。
#
# 所以再给一条能自证的判据：地点在床上类的地方 + 能量消耗是睡着的量级 + 够一段觉的时长 +
# 没有任何「醒着才能做」的动作词。四条同时成立才算睡——单独一条都不足以定案
# （「躺在床上刷手机」被动作词挡下，「躺着发呆」被消耗量挡下）。
BED_WORDS = ("床", "被窝", "枕", "毯", "卧室", "榻", "沙发")
AWAKE_WORDS = (
    "刷", "看", "玩", "吃", "喝", "写", "学", "走", "跑", "聊", "说",
    "工作", "开会", "手机", "电脑", "电视", "出门", "收拾", "整理", "通勤",
    "打扫", "做饭", "化妆", "换衣", "起床", "醒来",
    # 「回消息」是清醒动作：少了这几个词，「靠着枕头回消息」会被判成睡着。
    "消息", "回复", "打字", "电话",
    # 「精神了」「清醒」是醒着的状态词：洗完澡精神了却被判成睡，是 #58 的验收反例。
    "精神", "清醒",
    # 「熬夜」是醒着的动作，但不是睡的反证：「熬夜后睡到中午」里睡词该赢。所以它只在
    # 这一档（否掉复合判据），不进 SLEEP_EXCLUDE_WORDS（那张表在睡词之前生效）。
    "熬夜",
)
SLEEP_RATE_MIN = 0.08
SLEEP_SPAN_MIN_MINUTES = 30
# 兜底填出来的段不是「她在睡」：「夜间休息」「自由活动/休息」只表示这段时间没排日程。
# 判成睡着会把「今天什么都没排」变成「她睡了十六个小时」，睡眠债与精力会跟着一起错。
FILLER_EVENTS = (str(FILLER_SLOT["event"]), str(NIGHT_FILLER_SLOT["event"]))


def slot_sleeps(slot: Any) -> bool:
    """这一个时段她是不是在睡觉。

    三种证据任一成立即可：事件名里有睡词（原来的唯一判据）、解析时已经按完整原文判过并
    打了 `sleep` 标记、或者上面那条「床上 + 睡着量级的消耗 + 够长 + 没有动作词」的复合
    判据。`dreams` 也算证据——只有睡段才会写梦。

    判断睡没睡的地方都该用它，而不是 `is_sleep_event(slot.get("event"))`：后者只拿到
    事件名（还常被截断），判不出模型写的那种安静的卧床段落。
    """
    if not isinstance(slot, Mapping):
        return False
    event = str(slot.get("event") or "").strip()
    if is_meal_event(event):
        return False
    if event in FILLER_EVENTS:
        return False
    if bool(slot.get("sleep")) or slot.get("dreams"):
        return True
    if is_sleep_event(event):
        return True
    # 排除表与 is_sleep_event 共用同一张表，别放两份：「失眠」「睡不着」在 is_sleep_event
    # 里被否掉了，但复合判据不再回头看它，于是「卧室 + 睡量级消耗 + 够长」会把失眠判成睡着。
    if any(word in event for word in SLEEP_EXCLUDE_WORDS):
        return False
    if any(word in event for word in STRONG_AWAKE_WORDS):
        return False
    # 其余清醒动作（刷手机 / 工作 / 回消息…）否掉下面的复合判据；**「洗」不在其中**：
    # 它是睡前也会做的事，当成醒着的证据会把「洗漱完在卧室安静待着」判成醒着。
    if any(word in event for word in AWAKE_WORDS):
        return False
    location = str(slot.get("location") or "")
    if not any(word in location for word in BED_WORDS) and not any(
        word in event for word in BED_WORDS
    ):
        return False
    if clamp_rate(slot.get("energy_rate", 0.0)) < SLEEP_RATE_MIN:
        return False
    lo, hi = parse_time(slot.get("start")), parse_time(slot.get("end"))
    if lo is None or hi is None or lo == hi:
        return False
    # lo == hi 已在上面 return；取模后 span ∈ [1, 1439]，不会 <= 0。
    span = (hi - lo) % DAY_MINUTES
    return span >= SLEEP_SPAN_MIN_MINUTES


def parse_time(raw: Any) -> int | None:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = int(raw)
        return value if 0 <= value <= DAY_MINUTES else None
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    match = _TIME_PATTERN.match(text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if minute > 59:
        return None
    total = hour * 60 + minute
    if total > DAY_MINUTES:
        return None
    return total


def slot_span(slot: Any) -> tuple[int, int] | None:
    """一段的 (起, 止) 墙钟分钟；跨午夜时 止 < 起 表示跨到次日。

    零长段（`start == end`）与坏段返回 None：`08:00-08:00` 是坏数据，不是
    「从 08:00 一直到 24:00」。这里只解析一次，`iter_segments`、
    `sleep_window_minutes`、`energy._compute_delta` 都从这一个口径取——
    以前三处各写一遍，于是「22:00→07:00 到底算不算睡」有两个答案
    （审核 F4）。
    """
    if not isinstance(slot, Mapping):
        return None
    start = parse_time(slot.get("start"))
    end = parse_time(slot.get("end"))
    if start is None or end is None or start == end:
        return None
    return start, end


def iter_segments(slot: Any) -> list[tuple[int, int]]:
    """把一段拆成 [起, 止) 的分钟区间；跨午夜拆成「当天尾巴 + 凌晨那截」。

    `{'start': '22:00', 'end': '07:00'}` → `[(1320, 1440), (0, 420)]`。
    这是**日循环**口径：凌晨那截锚在同一份日程的 00:00–07:00 上，因为这份日程
    每天都重来一遍（今天 22:00 那一觉的尾巴，在明天的同形状日程里就是 00:00–07:00）。

    要按「哪一天」还原成时间戳的调用方（`energy._compute_delta`）也逐截用本函数、
    每截锚在自己的 anchor 当天：`_settle_span` 已经在本地午夜把窗口切开，两截各算
    各的，接起来正好是一整段。若改成「取 `slot_span` 补一天」，第二截（00:00–07:00）
    的 anchor 是次日，前一晚那个结算窗口就盖不上它。
    """
    span = slot_span(slot)
    if span is None:
        return []
    start, end = span
    if end > start:
        return [(start, end)]
    segments = [(start, DAY_MINUTES)]
    if end > 0:
        segments.append((0, end))
    return segments


def format_time(minutes: int) -> str:
    minutes = max(0, min(DAY_MINUTES, int(minutes)))
    if minutes == DAY_MINUTES:
        return "24:00"
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def clamp_rate(raw: Any) -> float:
    try:
        rate = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if rate != rate:
        return 0.0
    return max(-MAX_ENERGY_RATE, min(MAX_ENERGY_RATE, rate))


def coverage_is_complete(slots: Iterable[Slot]) -> bool:
    cursor = 0
    seen = False
    for slot in slots:
        seen = True
        start = parse_time(slot.get("start"))
        end = parse_time(slot.get("end"))
        if start is None or end is None or start != cursor or end <= start:
            return False
        cursor = end
    return seen and cursor == DAY_MINUTES


# 「无论怎么排都该是夜」的那一段（00:00–06:00）。生物钟夜从落在这一段里的睡眠段中
# 挑，而不是挑最长的那一段。
NIGHT_CORE_END_MINUTES = 6 * 60


def _night_overlap(seg: list[int]) -> int:
    """这一段与 00:00–06:00 的相交分钟数。

    跨午夜的一觉在合并后起点可能是负数（23:00→07:00 变成 -60→420），照常相交。
    """
    return max(0, min(seg[1], NIGHT_CORE_END_MINUTES) - max(seg[0], 0))


def sleep_window_minutes(slots: Iterable[Slot]) -> tuple[int, int] | None:
    """日程里的那一段觉，返回 (起始分钟, 结束分钟)；跨午夜时 end < start。

    soma 的昼夜低谷与睡眠债记账、schedule 的「她几点起」、契约导出的生物钟夜都从这里取，
    否则同一个「她的睡眠区间」会有三套算法，早上她睡着没睡就说不清了。
    模型常把一夜拆成 23:30→24:00 与 00:00→07:00 两段，所以先把首尾相接的段并起来，
    再把 24:00 与 00:00 那个缝接上。

    挑选口径是**夜觉优先**：取与 00:00–06:00 相交最多的那一段，全都不相交时才退到
    最长的那一段（值夜班、白天补觉的人也得有个窗口）。只按时长挑会让午睡顶替夜觉——
    实测「夜 02:00–05:00 + 午睡 13:00–17:30」会把生物钟夜判成 13:00–17:30：下午五点
    最困、凌晨三点最清醒，午睡在减债、夜觉在欠债，方向整个反过来。
    """
    segs: list[list[int]] = []
    for slot in slots or []:
        # 判据统一走 slot_sleeps：关键词、解析时的判定、以及「床上 + 睡着量级的消耗 +
        # 够长 + 没有动作词」的复合判据。只看事件名时，模型写的那种安静卧床段落会被漏掉，
        # 生物钟夜跟着退回配置窗口——soma 的昼夜低谷、睡眠债、她几点起，全按错的那一段算。
        if not slot_sleeps(slot):
            continue
        # 跨午夜写法（`{'start': '23:00', 'end': '06:00'}`）拆成当天的两段，
        # 下面的接缝合并会把它们接回一条。旧写法在这里静默丢弃，于是得到
        # 「她今天没睡」：起床点为空、生物钟夜退回配置、asleep 永远 False。
        # 拆分口径与 soma.slot_at / energy 共用 `iter_segments`（审核 F4）。
        for lo, hi in iter_segments(slot):
            segs.append([lo, hi])
    if not segs:
        return None
    segs.sort()
    merged = [segs[0]]
    for lo, hi in segs[1:]:
        if lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    if len(merged) > 1 and merged[-1][1] >= DAY_MINUTES and merged[0][0] == 0:
        merged[0][0] = merged[-1][0] - DAY_MINUTES
        merged.pop()
    best = max(merged, key=lambda seg: (_night_overlap(seg), seg[1] - seg[0]))
    if best[1] - best[0] < 30:
        return None
    return best[0] % DAY_MINUTES, best[1] % DAY_MINUTES


# 生物钟夜的合理跨度（小时）。`sleep_window_minutes` 只保证「挑得对」（夜觉优先、
# 其次最长），不保证「挑出来的是一夜」：当天日程里唯一一段睡眠是 11:15-12:45 的
# 小睡时，它照样返回 11.25→12.75。谁拿这个窗口当生物钟夜，谁就会把上午十一点
# 判成深夜——一天剩下 22.5 小时全算「夜里醒着」，困意与睡眠债只增不减。
# 所以这里给出「一段觉算不算一夜」的跨度区间，两个边界都从「她一晚需要睡多久」推，
# 不写死：短睡型（need=4）与长睡型（need=10）不该用同一个下限。
NIGHT_SPAN_LOW_RATIO = 0.6   # 下限 = sleep_need_hours 的 60%
NIGHT_SPAN_LOW_FLOOR = 2.0   # 但不少于 2 小时（sleep_need_hours 配得很小时）
NIGHT_SPAN_LOW_CEIL = 6.0    # 也不多于 6 小时（否则 14 小时型会把正常的一夜判掉）
NIGHT_SPAN_HIGH_PAD = 6.0    # 上限 = sleep_need_hours + 6 小时
NIGHT_SPAN_HIGH_CEIL = 16.0


def night_span_bounds(sleep_need_hours: float) -> tuple[float, float]:
    """一段觉算「一夜」的跨度区间（小时）：(下限, 上限)。"""
    try:
        need = float(sleep_need_hours)
    except (TypeError, ValueError):
        need = 8.0
    if need != need or need <= 0.0:  # NaN / 非正数：按默认 8 小时
        need = 8.0
    low = min(NIGHT_SPAN_LOW_CEIL, max(NIGHT_SPAN_LOW_FLOOR, need * NIGHT_SPAN_LOW_RATIO))
    high = min(NIGHT_SPAN_HIGH_CEIL, max(low, need + NIGHT_SPAN_HIGH_PAD))
    return low, high


def night_span_hours(window: tuple[float, float] | None) -> float:
    """(起始小时, 结束小时) 的跨度；跨午夜用 `(end - start) % 24`，起点等于终点是 0。"""
    if window is None:
        return 0.0
    try:
        start, end = float(window[0]), float(window[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return 0.0
    return (end - start) % 24.0


def plausible_night(window: tuple[float, float] | None, sleep_need_hours: float) -> bool:
    """这一段觉像不像「她的一夜」：太短（白天小睡）或太长（大半天都在睡）都不算。"""
    span = night_span_hours(window)
    if span <= 0.0:
        return False
    low, high = night_span_bounds(sleep_need_hours)
    return low <= span <= high


def find_slot(slots: Iterable[Slot], minutes: int) -> Slot:
    minutes = max(0, min(DAY_MINUTES - 1, int(minutes)))
    fallback: Slot = {
        "start": "00:00",
        "end": "24:00",
        "event": "休息/自由活动",
        "location": "家中",
        "emotion": "平淡",
        "energy_rate": 0.0,
    }
    for slot in slots:
        start = parse_time(slot.get("start"))
        end = parse_time(slot.get("end"))
        if start is None or end is None:
            continue
        if start <= minutes < end:
            return slot
    return fallback


def rate_at(slots: Iterable[Slot], minutes: int) -> float:
    return clamp_rate(find_slot(slots, minutes).get("energy_rate", 0.0))