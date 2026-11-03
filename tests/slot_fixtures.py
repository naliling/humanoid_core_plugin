"""测试用的「一整天时段表」构造器。

这段代码原本在 `humanoid/slots.py`（`normalize_slots` 及 5 个辅助函数）。审核
「状态与人设 17」确认它**在生产路径上零调用**：`ScheduleService` 是滚动增量生成段，
从不整表规范化；唯一还在用它的是十几个测试，把「若干条 (start, end, event)」摊成
一整天的 slot 列表。生产代码不该为测试保留一条没人走的路径，所以整层搬到这里。

测试用它的方式没变（`from .slot_fixtures import normalize_slots`），生产模块则不再
导出它——以后谁想「顺手」调它，会先发现它只存在于 tests/ 里。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from humanoid.slots import (
    DAY_MINUTES,
    FILLER_SLOT,
    NIGHT_FILLER_SLOT,
    Slot,
    clamp_rate,
    format_time,
    parse_time,
)


# 短于一刻钟的残段不当作一段（与 schedule.MIN_SLOT_MINUTES 同值）。
MIN_SLOT_MINUTES = 15


def _snap(minutes: int, step: int) -> int:
    if step <= 1:
        return minutes
    if minutes in (0, DAY_MINUTES):
        return minutes
    # 向下对齐，与 `schedule.segment_start` 同一立场：四舍五入会把起点推到未来
    # （20:40 对齐成 20:45），那一段就罩不住「现在」。
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
    ongoing = str(template.get("ongoing", "") or "").strip()[:40]
    if ongoing:
        slot["ongoing"] = ongoing
    if template.get("wants"):
        slot["wants"] = True
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
        if end < start:
            # 跨午夜（23:00→07:00）：拆成「当天尾巴 + 凌晨那截」，与 slots.iter_segments
            # 同一口径。旧写法直接 continue，于是「她的那一觉」在夹具里凭空消失，
            # sleep_window_minutes 的跨午夜分支永远测不到（审核 状态10）。
            if DAY_MINUTES - start >= MIN_SLOT_MINUTES:
                parsed.append((start, DAY_MINUTES, item))
            if end >= MIN_SLOT_MINUTES:
                parsed.append((0, end, item))
            continue
        if end == start:
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
            slots[low]["energy_rate"] * durations[low]
            + slots[high]["energy_rate"] * durations[high]
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
    """把「若干条 (start, end, …)」摊成一整天的 slot 列表（测试夹具专用）。"""
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
