"""主动消息的「已发清单」出处（provenance）：纯函数，不读盘、不写盘。

社交层每天要发好几条主动消息，发过什么、拿哪张卡说的、什么时候说的，得留个记账。
记账本身只存**指针**（card_id），不存正文：正文在卡里，卡会过期、会被重建，把正文
抄进记录就成了第二份会漂移的事实。

这里只做三件事：

1. `append`：把一条发送记录归一化成固定四键（contact_id / card_id / source /
   captured_at），按上限裁剪；坏数据跳过，绝不抛。
2. `with_provenance`：读的时候按 card_id 回卡里补出处；**旧记录没有 provenance 就
   当「不知道出处」，不猜、不编**。
3. `find_by_card`：按 card_id 找回最新一条发送记录；正文请调用方回卡里取。

记录形状故意只认这四个键：多存的字段（正文、联系人名字、时间字符串）会让「同一份
清单」在不同进程里长得不一样，也把过期内容钉死在记录里。
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping

#: 一条发送记录的固定键（顺序即归一化后的顺序）。
RECORD_KEYS: tuple[str, ...] = ("contact_id", "card_id", "source", "captured_at")

#: 默认保留条数：够社交层回溯最近几轮，又不至于把 state.json 撑大。
RECORD_LIMIT = 64


def _clean(value: Any) -> str:
    """压掉换行与多余空白；None / 数字一律转成文本。"""
    return " ".join(str(value or "").split())


def _stamp(value: Any) -> float:
    """采集时间：只认正的有限秒数，其它一律 0.0（0 表示「不知道」）。"""
    if isinstance(value, bool):
        return 0.0
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(out) or out <= 0:
        return 0.0
    return out


def _records(records: Any) -> list[dict[str, Any]]:
    """把入参收成「字典副本」列表；不是序列就当空，非字典条目丢掉。"""
    if not isinstance(records, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for item in records:
        if isinstance(item, Mapping):
            out.append(dict(item))
    return out


def _normalize(entry: Any) -> dict[str, Any] | None:
    """把一条发送记录归一化成固定四键；认不出来就返回 None。"""
    if not isinstance(entry, Mapping):
        return None
    merged = dict(entry)
    provenance = entry.get("provenance")
    if isinstance(provenance, Mapping):
        for key in ("card_id", "source", "captured_at"):
            if not merged.get(key) and provenance.get(key):
                merged[key] = provenance.get(key)
    record = {
        "contact_id": _clean(merged.get("contact_id")),
        "card_id": _clean(merged.get("card_id")),
        "source": _clean(merged.get("source")),
        "captured_at": _stamp(merged.get("captured_at")),
    }
    if not any(record.values()):
        return None
    return record


def _cap(limit: Any) -> int:
    """上限：非整数退回默认值，负数按 0 处理（0 表示不保留）。"""
    if isinstance(limit, bool):
        return RECORD_LIMIT
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return RECORD_LIMIT
    return value if value >= 0 else 0


def append(
    records: Any, entry: Any, *, limit: Any = RECORD_LIMIT
) -> list[dict[str, Any]]:
    """追加一条发送记录，返回新列表（不改入参）；超上限丢最旧的。

    最新记录在**尾部**：社交层读「最近一条」不用倒序，裁剪也只动头部。
    """
    out = _records(records)
    item = _normalize(entry)
    if item is not None:
        out.append(item)
    cap = _cap(limit)
    if len(out) > cap:
        out = out[-cap:] if cap else []
    return out


def with_provenance(records: Any, cards: Any) -> list[dict[str, Any]]:
    """读的时候按 card_id 回卡里补 source / captured_at。

    只补**空着的**字段：记录里已经有出处的，以记录为准（那是当时的事实）。没有
    card_id 的旧记录原样返回——不知道出处就是不知道，不能拿别的卡硬凑。
    """
    index: dict[str, Mapping[str, Any]] = {}
    if isinstance(cards, Iterable) and not isinstance(cards, (str, bytes, Mapping)):
        for card in cards:
            if not isinstance(card, Mapping):
                continue
            card_id = _clean(card.get("id"))
            if card_id and card_id not in index:
                index[card_id] = card
    out: list[dict[str, Any]] = []
    for record in _records(records):
        card_id = _clean(record.get("card_id"))
        card = index.get(card_id) if card_id else None
        if card is not None:
            if not _clean(record.get("source")):
                record["source"] = _clean(card.get("source"))
            if not _stamp(record.get("captured_at")):
                record["captured_at"] = _stamp(card.get("captured_at"))
        out.append(record)
    return out


def find_by_card(records: Any, card_id: Any) -> dict[str, Any]:
    """按 card_id 找最新一条发送记录；找不到返回空字典（不抛）。

    记录只存指针，正文请调用方拿 card_id 回卡里取：卡过期了就是「不知道」，
    而不是把旧正文当新事实再说一遍。
    """
    target = _clean(card_id)
    if not target:
        return {}
    found: dict[str, Any] = {}
    for record in _records(records):
        if _clean(record.get("card_id")) == target:
            found = record
    return found
