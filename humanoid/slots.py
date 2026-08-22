"""日程时段（slot）的解析、规范化与查询。

v2.10.2 的 `validate_and_fix_schedule()` 只做了「补空隙 + 钳制 energy_rate」，
不排序、不校验 end>start、不剔除非 dict 元素，也不去重叠 —— 大模型稍微不听话就会
产出让精力计算静默跳过的垃圾时段。这里换成一条完整的规范化流水线：

    解析 → 排序 → 对齐粒度 → 去重叠 → 补空隙 → 限制数量 → 钳制速率

输出保证：非空、按时间升序、首尾相连、严格覆盖 00:00~24:00、时段数不超过上限。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

DAY_MINUTES = 24 * 60
MAX_ENERGY_RATE = 0.3

Slot = dict[str, Any]

_TIME_PATTERN = re.compile(r"^(\d{1,2})\s*[:：.：]\s*(\d{1,2})$")

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


def parse_time(raw: Any) -> int | None:
    """`"HH:MM"` → 距零点的分钟数。`24:00` → 1440。无法解析返回 None。"""
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
    if total < 0 or total > DAY_MINUTES:
        return None
    return total


def format_time(minutes: int) -> str:
    """1440 → `"24:00"`，其余按 `"HH:MM"`。"""
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
    snapped = round(minutes / step) * step
    return max(0, min(DAY_MINUTES, snapped))


def _make_slot(start: int, end: int, template: Mapping[str, Any]) -> Slot:
    return {
        "start": format_time(start),
        "end": format_time(end),
        "event": str(template.get("event", "") or FILLER_SLOT["event"]),
        "location": str(template.get("location", "") or FILLER_SLOT["location"]),
        "emotion": str(template.get("emotion", "") or FILLER_SLOT["emotion"]),
        "energy_rate": clamp_rate(template.get("energy_rate", 0.0)),
    }


def _parse_entries(raw: Iterable[Any]) -> list[tuple[int, int, Mapping[str, Any]]]:
    """抽出可用的 (start, end, 原始字段) 三元组，丢弃一切无法解析的项。"""
    parsed: list[tuple[int, int, Mapping[str, Any]]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        start = parse_time(item.get("start"))
        end = parse_time(item.get("end"))
        if start is None or end is None:
            continue
        if end == 0 and start > 0:
            end = DAY_MINUTES  # "00:00" 当收尾时间用
        if end <= start:
            continue  # 含跨零点写法：本插件按单日建模，直接丢弃
        parsed.append((start, end, item))
    return parsed


def _resolve_overlaps(
    entries: list[tuple[int, int, Mapping[str, Any]]],
) -> list[tuple[int, int, Mapping[str, Any]]]:
    """按 start 排序后裁掉重叠部分；被完全吞掉的时段直接丢弃。"""
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
    """补齐首、尾与中间的空隙，保证严格覆盖 00:00~24:00。"""
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
    """时段过多时，反复把最短的时段并入相邻时段，直到数量达标。"""
    if max_slots < 1:
        return slots
    while len(slots) > max_slots:
        durations = [parse_time(s["end"]) - parse_time(s["start"]) for s in slots]
        victim = min(range(len(slots)), key=lambda i: durations[i])
        # 并入更长的那个邻居，保留邻居的活动描述
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
    """把任意形状的日程数据规范化。无法产出有效日程时返回 None。"""
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
    """自检：时段是否严格首尾相连地覆盖 00:00~24:00。"""
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


def find_slot(slots: Iterable[Slot], minutes: int) -> Slot:
    """按「距零点分钟数」定位当前时段，左闭右开，避免边界命中前一段。"""
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

