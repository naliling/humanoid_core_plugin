"""证据卡（opportunity evidence card）：把「她今天确实有的事」做成一张带出处、
采集时间和有效期的卡，交给社交层当主动开口的由头。

## 为什么要有它

社交层要主动开口，手里得有一件说得出口的事：她今天做了什么、正在做什么、今天
想做什么、天气节日、刚发生的重事件。这些事实 Core 其实都有，但散在契约的
`day` / `ongoing` / `wants` / `event` / `weather` / `holiday` 里，形状各不相同
（字符串 / 列表 / 块字典）。社交层每处都要猜一遍形状，猜错就静默丢一条由头——
历史上 `feelings` 的 `list[str]` 被当成 `list[dict]` 读，那条由头就永远不产出。

卡片把「一条由头」统一成同一个形状：`kind`（哪一类）、`title` / `detail`（说什么）、
`source`（中文出处，社交层可以直接照着说「我记得你日程上……」）、
`captured_at` / `expires_at`（什么时候观察到的、到什么时候还值得说）、
`anchor_key`（同一件事的稳定键，社交层拿它去重，避免反复说同一件）。

## 三条硬规矩

1. **纯函数**：不读盘、不读配置、不依赖 AstrBot，也不看时钟——`now` 一律由调用方
   传进来。这样它能在单测里被穷举，也不会因为读了一次 state 就把契约生成拖慢。
2. **只加不改**：卡片是契约里新增的只读键（`opportunities` / `materials`），既有键
   一个字节都不动；旧社交层读不到这两个键，按「旧版 Core」退回本地兜底卡。
3. **过期不导出**：`expires_at <= now` 的卡留在 Core 里，不往契约里塞——「今天下午
   喝了杯手冲」到半夜还当由头说出来，就是那句「发言很假」。
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Iterable, Mapping

#: 卡片恰好这九个键。社交层按结构读，少一个、多一个都当坏卡丢掉——
#: 形状漂了却没人报错，是这个项目历史上最常见的一类故障。
CARD_KEYS: tuple[str, ...] = (
    "id",
    "kind",
    "purpose",
    "title",
    "detail",
    "source",
    "captured_at",
    "expires_at",
    "anchor_key",
)

#: 卡片种类。前几种是 Core 现在产得出来的（日程 / 未完成 / 想做的 / 天气 / 节日 /
#: 梦 / 刚发生），后面几种留给以后接进来的源（屏幕 / 新闻 / 成长 / 共同记忆 /
#: 日历 / 提醒）：现在不产，但校验认得，接的时候不用改这里。
KINDS: tuple[str, ...] = (
    "life",
    "calendar",
    "weather",
    "holiday",
    "memory",
    "screen",
    "news",
    "activity",
    "growth",
    "journal",
    "reminder",
    "greeting",
)

#: 有效期（秒）：按「这条由头多久之后说出来就已经不像话了」分档。
#: 日程里做过的事能撑到半天（今天下午的手冲，晚上说还成立）；「正在做」的事只在
#: 这一段里成立；天气三小时就换；节日一整天；梦到中午就该停了。
OPP_TTL_SECONDS: dict[str, float] = {
    "life": 6 * 3600.0,
    "activity": 2 * 3600.0,
    "weather": 3 * 3600.0,
    "holiday": 24 * 3600.0,
    "memory": 12 * 3600.0,
    "screen": 2 * 3600.0,
    "news": 6 * 3600.0,
    "growth": 12 * 3600.0,
    "journal": 12 * 3600.0,
    "reminder": 6 * 3600.0,
    "greeting": 3 * 3600.0,
    "calendar": 24 * 3600.0,
}

#: 素材（还没发生、只是「她这样的人可能做」）比既成事实撑得久一点：它是备选，
#: 不是报道，八小时内都能当「要不要去做这件事」的由头。
MATERIAL_TTL_SECONDS = 8 * 3600.0

#: 契约里两个键的上限。上限写在这里而不是调用方：投影只有一个出口，两处各写
#: 一份迟早走偏（这个项目的历史就是「同一个毛病换个地方回来」）。
OPP_LIMIT = 5
MATERIAL_LIMIT = 8

#: 字段长度上限：卡片会进 state.json（线上 2.4 MB），一句话不能带一屏。
TITLE_MAX = 32
DETAIL_MAX = 80
SOURCE_MAX = 12
ANCHOR_MAX = 32

ID_PREFIX = "opp-"
_ID_HEX = 12
HOUR_SECONDS = 3600.0

#: 去标点用：中文属于 \w，所以 \W 只吃标点与空白，不会把汉字吃掉。
_PUNCT = re.compile(r"[\W_]+", re.UNICODE)


def _clean(value: Any) -> str:
    """压掉换行与多余空白：卡片进契约、进 prompt，一条里带换行就把结构撑坏。"""
    return " ".join(str(value or "").split())


def _clip(text: str, limit: int) -> str:
    body = str(text or "")
    if limit <= 0 or len(body) <= limit:
        return body
    return body[:limit]


def to_epoch(now: Any) -> float:
    """把「现在」统一成 epoch 秒：给 datetime 取时间戳，给数字当它已经是秒。"""
    if now is None:
        return 0.0
    stamp = getattr(now, "timestamp", None)
    if callable(stamp):
        try:
            return float(stamp())
        except (TypeError, ValueError):
            return 0.0
    try:
        out = float(now)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def hour_bucket(ts: Any) -> float:
    """采集时间按小时向下取整。

    卡片要进契约，而契约是「变了才重写」。`captured_at` 用秒级 now 的话，同一
    小时内每重建一次契约这两个键就变一次，state.json（线上 2.4 MB）跟着每分钟
    重写。取整到小时后，同一小时内两次 build 出来的卡片逐字节相同。
    """
    return float(int(to_epoch(ts) // HOUR_SECONDS) * HOUR_SECONDS)


def anchor_key_for(kind: str, text: Any) -> str:
    """同一件事的稳定键：种类 + 去掉标点空白后的题眼。

    `anchor_key` 是社交层「这条由头用过了」的记账键，必须在重建契约时保持不变，
    所以只由「种类 + 文本」决定，不含时间。
    """
    name = _clean(kind).lower() or "life"
    body = _PUNCT.sub("", _clean(text))
    return name + ":" + (_clip(body, ANCHOR_MAX) or "unknown")


def make_card(
    *,
    kind: str,
    title: Any,
    detail: Any = "",
    source: Any = "",
    captured_at: Any,
    expires_at: Any = None,
    ttl: Any = None,
    purpose: Any = None,
    anchor_key: Any = None,
) -> dict[str, Any]:
    """造一张卡。形状不对就抛 `ValueError`。

    抛异常而不是返回 `None`：调用方（anchors）逐条 try 掉，一条坏卡不该让整份
    契约生成失败；但「静默造出一张半残的卡」会让社交层读到假事实。
    """
    name = _clean(kind).lower()
    if name not in KINDS:
        raise ValueError("未知卡片种类: " + repr(kind))
    text = _clip(_clean(title), TITLE_MAX)
    if not text:
        raise ValueError("卡片没有 title")
    captured = hour_bucket(captured_at)
    if expires_at is not None:
        expires = to_epoch(expires_at)
    else:
        seconds = float(OPP_TTL_SECONDS.get(name, 6 * 3600.0)) if ttl is None else float(ttl)
        expires = captured + seconds
    if expires <= captured:
        raise ValueError("expires_at 必须晚于 captured_at")
    key = _clean(anchor_key) or anchor_key_for(name, text)
    digest = hashlib.sha1((name + "|" + key).encode("utf-8")).hexdigest()
    return {
        "id": ID_PREFIX + digest[:_ID_HEX],
        "kind": name,
        "purpose": _clip(_clean(purpose), SOURCE_MAX) or name,
        "title": text,
        "detail": _clip(_clean(detail), DETAIL_MAX),
        "source": _clip(_clean(source), SOURCE_MAX),
        "captured_at": captured,
        "expires_at": round(expires, 3),
        "anchor_key": key,
    }


def validate_card(card: Any) -> list[str]:
    """检查一张卡，返回问题清单（空列表 = 合格）。只判结构，不判内容好不好。"""
    problems: list[str] = []
    if not isinstance(card, Mapping):
        return ["不是字典"]
    missing = [key for key in CARD_KEYS if key not in card]
    if missing:
        problems.append("缺键: " + ",".join(missing))
    extra = [str(key) for key in card if key not in CARD_KEYS]
    if extra:
        problems.append("多键: " + ",".join(extra))
    kind = _clean(card.get("kind"))
    if kind not in KINDS:
        problems.append("未知种类: " + repr(card.get("kind")))
    if not _clean(card.get("title")):
        problems.append("title 为空")
    if not _clean(card.get("id")).startswith(ID_PREFIX):
        problems.append("id 前缀不对")
    if not _clean(card.get("anchor_key")):
        problems.append("anchor_key 为空")
    try:
        captured = float(card.get("captured_at"))
        expires = float(card.get("expires_at"))
    except (TypeError, ValueError):
        problems.append("时间戳不是数字")
    else:
        if not (math.isfinite(captured) and math.isfinite(expires)):
            problems.append("时间戳不是有限数")
        elif expires <= captured:
            problems.append("expires_at 不晚于 captured_at")
    return problems


def is_expired(card: Any, now: Any) -> bool:
    """这张卡现在还能不能当由头说。时间戳坏掉一律算过期（宁可不导出）。"""
    if not isinstance(card, Mapping):
        return True
    try:
        expires = float(card.get("expires_at"))
    except (TypeError, ValueError):
        return True
    return expires <= to_epoch(now)


def _newer(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """同一 anchor_key 的两张卡谁更新：采集时间晚的赢，平手取 id 小的（稳定）。"""
    try:
        left_ts = float(left.get("captured_at"))
    except (TypeError, ValueError):
        left_ts = 0.0
    try:
        right_ts = float(right.get("captured_at"))
    except (TypeError, ValueError):
        right_ts = 0.0
    if left_ts != right_ts:
        return left_ts > right_ts
    return _clean(left.get("id")) < _clean(right.get("id"))


def dedupe(cards: Iterable[Any]) -> list[dict[str, Any]]:
    """同一 anchor_key 只留最新的一张。"""
    kept: dict[str, dict[str, Any]] = {}
    for card in cards or ():
        if not isinstance(card, Mapping):
            continue
        key = _clean(card.get("anchor_key"))
        if not key:
            continue
        current = kept.get(key)
        if current is None or _newer(card, current):
            kept[key] = dict(card)
    return list(kept.values())


def fresh_cards(cards: Any, now: Any, limit: int = OPP_LIMIT) -> list[dict[str, Any]]:
    """过滤出「现在还能说」的卡：结构合格、没过期、同 anchor_key 去重、
    按采集时间倒序、截到 limit 条。"""
    stamp = to_epoch(now)
    alive: list[dict[str, Any]] = []
    for card in cards or ():
        if validate_card(card):
            continue
        if is_expired(card, stamp):
            continue
        alive.append(dict(card))
    picked = dedupe(alive)
    picked.sort(
        key=lambda card: (
            -float(card.get("captured_at") or 0.0),
            _clean(card.get("anchor_key")),
            _clean(card.get("id")),
        )
    )
    try:
        cap = max(0, int(limit))
    except (TypeError, ValueError):
        cap = OPP_LIMIT
    return picked[:cap]


def contract_blocks(opportunities: Any, materials: Any, now: Any) -> dict[str, list[dict[str, Any]]]:
    """把卡片投影成契约里的两个只读键。

    * `opportunities`：既成事实的由头，最多 `OPP_LIMIT` 条；
    * `materials`：她这样的人还可能做的事，最多 `MATERIAL_LIMIT` 条。

    两个键**永远都在**（没有卡就是空列表）：社交层按「键在不在」判新旧版本，
    缺键与空列表在它眼里是两件事。
    """
    return {
        "opportunities": fresh_cards(opportunities, now, limit=OPP_LIMIT),
        "materials": fresh_cards(materials, now, limit=MATERIAL_LIMIT),
    }
