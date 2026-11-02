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
# 吃饭优先于「午休」：「午餐与午休发呆」是在吃饭，不是在睡觉。
MEAL_WORDS = ("餐", "吃", "饭", "外卖", "食堂")


def is_sleep_event(event: Any) -> bool:
    text = str(event or "")
    if any(word in text for word in MEAL_WORDS):
        return False
    return any(word in text for word in SLEEP_WORDS)


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
    "打扫", "做饭", "洗", "化妆", "换衣", "起床", "醒来",
    # 「回消息」是清醒动作：少了这几个词，「靠着枕头回消息」会被判成睡着。
    "消息", "回复", "打字", "电话",
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
    span = (hi - lo) % DAY_MINUTES
    if span <= 0:
        return False
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


def _snap(minutes: int, step: int) -> int:
    if step <= 1:
        return minutes
    if minutes in (0, DAY_MINUTES):
        return minutes
    # 向下对齐，与 `schedule.segment_start`（655-662）同一立场：四舍五入会把起点
    # 推到未来（20:40 对齐成 20:45），那一段就罩不住「现在」。
    snapped = (minutes // step) * step
    return max(0, min(DAY_MINUTES, snapped))


def _make_slot(start: int, end: int, template: Mapping[str, Any]) -> Slot:
    slot = {
        "start": format_time(start),
        "end": format_time(end),
        "event": str(template.get("event", "") or FILLER_SLOT["event"]),
        "location": str(template.get("location", "") or FILLER_SLOT["location"]),
        "emotion": str(template.get("emotion", "") or FILLER_SLOT["emotion"]),
        "energy_rate": clamp_rate(template.get("energy_rate", 0.0)),
    }
    # 「还没做完」：这件是不是一件要跨几天的事（整理那份个案笔记）。
    #
    # 没有它，她的每一天都是**互不相关**的——今天改方案、明天改方案，两边没有
    # 「还没弄完」这层，于是每段都像新任务。有它，跨天就有连续性，而且它也是
    # social 那边最好的由头来源（比天气有意思得多）。
    ongoing = str(template.get("ongoing", "") or "").strip()[:40]
    if ongoing:
        slot["ongoing"] = ongoing
    # 「她自己想做的」——一天里可以有一两段不是工作、不是必须，是她自己挑的。
    # 以前她的日程全是 duty（门诊接待、写记录、外出），于是她像个只有日程没有生活的人。
    if template.get("wants"):
        slot["wants"] = True
    # 解析时按**完整原文**判出来的睡着标记要留下来：规范化会重建 slot，丢了它，
    # 被截断成 24 字的事件名就再也说不清那一段是睡了还是醒着。
    if template.get("sleep"):
        slot["sleep"] = True
    dreams = template.get("dreams")
    if isinstance(dreams, (list, tuple)) and dreams:
        slot["dreams"] = list(dreams)[:3]
    return slot


def _parse_entries(raw: Iterable[Any]) -> list[tuple[int, int, Mapping[str, Any]]]:
    parsed: list[tuple[int, int, Mapping[str, Any]]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        start = parse_time(item.get("start"))
        end = parse_time(item.get("end"))
        if start is None or end is None:
            continue
        if end == 0 and start > 0:
            end = DAY_MINUTES
        if end <= start:
            continue
        parsed.append((start, end, item))
    return parsed


def _resolve_overlaps(
    entries: list[tuple[int, int, Mapping[str, Any]]],
) -> list[tuple[int, int, Mapping[str, Any]]]:
    entries.sort(key=lambda item: (item[0], item[1]))
    out: list[tuple[int, int, Mapping[str, Any]]] = []
    cursor = 0
    for start, end, payload in entries:
        start = max(start, cursor)
        if end <= start:
            continue
        out.append((start, end, payload))
        cursor = end
    return out


def _fill_gaps(entries: list[tuple[int, int, Mapping[str, Any]]]) -> list[Slot]:
    slots: list[Slot] = []
    cursor = 0
    for start, end, payload in entries:
        if start > cursor:
            filler = NIGHT_FILLER_SLOT if start <= 6 * 60 else FILLER_SLOT
            slots.append(_make_slot(cursor, start, filler))
        slots.append(_make_slot(start, end, payload))
        cursor = end
    if cursor < DAY_MINUTES:
        filler = NIGHT_FILLER_SLOT if cursor >= 21 * 60 else FILLER_SLOT
        slots.append(_make_slot(cursor, DAY_MINUTES, filler))
    return slots


def _merge_neighbours(slots: list[Slot], max_slots: int) -> list[Slot]:
    if max_slots < 1:
        return slots
    while len(slots) > max_slots:
        durations = [parse_time(s["end"]) - parse_time(s["start"]) for s in slots]
        victim = min(range(len(slots)), key=lambda i: durations[i])
        left = victim - 1
        right = victim + 1
        if left < 0:
            keeper = right
        elif right >= len(slots):
            keeper = left
        else:
            keeper = left if durations[left] >= durations[right] else right

        low, high = min(victim, keeper), max(victim, keeper)
        span_start = parse_time(slots[low]["start"])
        span_end = parse_time(slots[high]["end"])
        total = max(1, durations[low] + durations[high])
        blended = (
            slots[low]["energy_rate"] * durations[low] + slots[high]["energy_rate"] * durations[high]
        ) / total
        merged = dict(slots[keeper])
        merged["start"] = format_time(span_start)
        merged["end"] = format_time(span_end)
        merged["energy_rate"] = clamp_rate(round(blended, 4))
        slots[low : high + 1] = [merged]
    return slots


def normalize_slots(
    raw: Any,
    *,
    max_slots: int = 48,
    align_minutes: int = 0,
) -> list[Slot] | None:
    if not isinstance(raw, Iterable) or isinstance(raw, (str, bytes, Mapping)):
        return None

    entries = _parse_entries(raw)
    if not entries:
        return None

    if align_minutes > 1:
        aligned: list[tuple[int, int, Mapping[str, Any]]] = []
        for start, end, payload in entries:
            snapped_start = _snap(start, align_minutes)
            snapped_end = _snap(end, align_minutes)
            if snapped_end <= snapped_start:
                snapped_end = min(DAY_MINUTES, snapped_start + align_minutes)
            if snapped_end > snapped_start:
                aligned.append((snapped_start, snapped_end, payload))
        entries = aligned or entries

    entries = _resolve_overlaps(entries)
    if not entries:
        return None

    slots = _fill_gaps(entries)
    slots = _merge_neighbours(slots, max(1, int(max_slots)))
    return slots or None


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
        lo, hi = parse_time(slot.get("start")), parse_time(slot.get("end"))
        if lo is None or hi is None or hi == lo:
            continue
        if hi < lo:
            # 跨午夜写法（`{'start': '23:00', 'end': '06:00'}`）：拆成当天的两段，
            # 下面的接缝合并会把它们接回一条。旧写法在这里静默丢弃，于是得到
            # 「她今天没睡」：起床点为空、生物钟夜退回配置、asleep 永远 False。
            segs.append([lo, DAY_MINUTES])
            if hi > 0:
                segs.append([0, hi])
            continue
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