"""从过去几周的日程里长出「习惯」。

用户原话（m07427）：「没有从她过去几周日程长出来的习惯。这个我觉得需要」。

之前生成侧只有两份数据：今天的 `daily_schedule` 与昨天的 `prev_day_schedule`。
于是「她平时几点起、这个钟点常在哪、常做什么」在生成时完全不可见，每天从零
开始挑事，模型只能挑最通用的那几件（睡、吃、看手机）——这是「每天高度雷同」
的一半来源。

这里做三件事：

1. 跨天时把昨天压成紧凑记录归档（`archive_day`），只留最近 28 天；
2. 按两小时一格统计「这个钟点她常做什么」（`habit_lines`），只在真的重复时说话；
3. 统计她自己的作息（醒来时间、入睡时间），只在稳定时说话。

只递事实，不写「所以今天应该……」：习惯是背景，不是今天的台词。达不到样本量
就一个字都不说——宁可没有，也不要拿两三天的巧合冒充习惯。
"""

from __future__ import annotations

import statistics
from typing import Any, Iterable

from ..slots import format_time, parse_time, slot_sleeps

#: 归档键与保留天数。28 天≈四周：够看出作息，又不至于让 state 无限膨胀。
HISTORY_KEY = "schedule_history"
HISTORY_KEEP_DAYS = 28
#: 少于这么多天才谈「习惯」：两三天只能算巧合。
HISTORY_MIN_DAYS = 5
#: 统计格子宽度（分钟）与「算重复」的门槛。
BUCKET_MINUTES = 120
REPEAT_MIN_DAYS = 3
REPEAT_RATIO = 0.4
#: 一次最多说几句（多了会把 prompt 挤满，也会变成另一份日程表）。
MAX_LINES = 2
#: 作息稳定判据：中位数两侧的最大偏差超过这个值就不说。
WAKE_TOLERANCE_MINUTES = 90
#: 事件文本归一：这些后缀/助词去掉后，同一天的两种说法才算同一件事。
_NOISE = ("了", "着", "一下", "一会儿", "会儿", "的")


def _norm_event(event: Any) -> str:
    text = str(event or "").strip()
    for noise in _NOISE:
        text = text.replace(noise, "")
    return text[:12]


def compact_slots(slots: Iterable[Any]) -> list[list]:
    """把一段日程压成 `[开始分钟, 结束分钟, 事件, 地点]`。

    只留统计要用的四列：整份 slot 里有情绪、精力速率、梦、进度文字，归档 28 天
    会把这些无关字段乘以几百份写进 state。
    """
    out: list[list] = []
    for slot in slots or []:
        if not isinstance(slot, dict):
            continue
        lo = parse_time(slot.get("start"))
        hi = parse_time(slot.get("end"))
        if lo is None or hi is None or hi <= lo:
            continue
        event = str(slot.get("event") or "").strip()[:24]
        location = str(slot.get("location") or "").strip()[:16]
        out.append([int(lo), int(hi), event, location])
    return out


def archive_day(scope: Any, date: str, slots: Iterable[Any]) -> None:
    """把 `date` 那天的日程归档，并裁到最近 `HISTORY_KEEP_DAYS` 天。

    写不进去（scope 没有该能力、state 只读）就静默放弃：习惯是锦上添花，不能
    因为它让跨天这件事本身失败。
    """
    day = str(date or "").strip()
    rows = compact_slots(slots)
    if not day or not rows:
        return
    try:
        stored = scope.get_self(HISTORY_KEY, {})
    except Exception:
        return
    history = dict(stored) if isinstance(stored, dict) else {}
    history[day] = rows
    keep = sorted(history)[-HISTORY_KEEP_DAYS:]
    pruned = {key: history[key] for key in keep}
    try:
        scope.update_self(**{HISTORY_KEY: pruned})
    except Exception:
        pass


def history_of(scope: Any) -> dict:
    """读出归档（坏值一律当空：习惯层不该因为一份坏存档报错）。"""
    try:
        stored = scope.get_self(HISTORY_KEY, {})
    except Exception:
        return {}
    if not isinstance(stored, dict):
        return {}
    out = {}
    for day, rows in stored.items():
        if isinstance(rows, list) and rows:
            out[str(day)] = [row for row in rows if isinstance(row, list) and len(row) >= 3]
    return out


def _bucket(minute: int) -> int:
    return int(minute) // BUCKET_MINUTES


def _awake_rows(rows: list) -> list:
    """去掉睡段：睡觉是每天都会发生的事，统计它只会得到「她常在床上」。"""
    out = []
    for row in rows:
        slot = {"start": format_time(row[0]), "end": format_time(row[1]), "event": row[2]}
        if slot_sleeps(slot):
            continue
        out.append(row)
    return out


def _bucket_lines(history: dict, now_minute: int) -> list[str]:
    """这个钟点她常做什么：只报真的重复过的那几件。"""
    bucket = _bucket(now_minute)
    counts: dict[tuple, int] = {}
    days = 0
    for rows in history.values():
        awake = _awake_rows(rows)
        if not awake:
            continue
        days += 1
        seen = set()
        for row in awake:
            if _bucket(row[0]) != bucket:
                continue
            key = (_norm_event(row[2]), str(row[3] or ""))
            if key in seen:
                continue
            seen.add(key)
            counts[key] = counts.get(key, 0) + 1
    if days < HISTORY_MIN_DAYS:
        return []
    picked = [
        (key, count) for key, count in counts.items()
        if count >= REPEAT_MIN_DAYS and count / float(days) >= REPEAT_RATIO
    ]
    picked.sort(key=lambda item: (-item[1], item[0]))
    lines = []
    start = bucket * BUCKET_MINUTES
    span = "%s-%s" % (format_time(start), format_time(start + BUCKET_MINUTES))
    for (event, location), count in picked[:MAX_LINES]:
        lines.append(
            "她这个钟点（%s）常在%s%s：最近 %d 天里有 %d 天是这样。"
            % (span, location, event, days, count)
        )
    return lines


def _routine_line(history: dict) -> list[str]:
    """作息：她一般几点起、几点睡。只在稳定时说话。"""
    wakes = []
    sleeps = []
    for rows in history.values():
        awake = _awake_rows(rows)
        for row in awake:
            if 240 <= row[0] <= 840:
                wakes.append(row[0])
                break
        for row in rows:
            slot = {"start": format_time(row[0]), "end": format_time(row[1]), "event": row[2]}
            if not slot_sleeps(slot):
                continue
            minute = row[0]
            if minute >= 20 * 60 or minute <= 6 * 60:
                sleeps.append(minute)
    if len(wakes) < REPEAT_MIN_DAYS:
        return []
    wake_med = int(statistics.median(wakes))
    if max(abs(value - wake_med) for value in wakes) > WAKE_TOLERANCE_MINUTES:
        return []
    parts = ["她一般 %s 前后起" % format_time(wake_med)]
    if len(sleeps) >= REPEAT_MIN_DAYS:
        sleep_med = int(statistics.median(sleeps))
        if max(abs(value - sleep_med) for value in sleeps) <= WAKE_TOLERANCE_MINUTES:
            parts.append("%s 前后睡" % format_time(sleep_med))
    return ["，".join(parts) + "。"]


def habit_lines(history: dict, now_minute: int) -> list[str]:
    """这个钟点 + 作息两类习惯句；样本不够时返回空表。"""
    if not isinstance(history, dict) or len(history) < HISTORY_MIN_DAYS:
        return []
    try:
        minute = int(now_minute)
    except (TypeError, ValueError):
        return []
    lines = _bucket_lines(history, minute)
    if len(lines) < MAX_LINES:
        lines = lines + _routine_line(history)
    return lines[: MAX_LINES + 1]


def render_habits(lines: Iterable[str]) -> str:
    """把习惯句排成 prompt 里的一个块；没有内容就返回空串。"""
    rows = [str(line).strip() for line in (lines or []) if str(line or "").strip()]
    if not rows:
        return ""
    return "\n".join("  - " + row for row in rows)

