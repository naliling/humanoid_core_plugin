"""跨轮、但会自己结束的事：她手上还开着什么。

为什么要单独一层：长期记忆由记忆插件负责，短期记忆由 AstrBot 的会话历史负责
（默认只留十几轮，超了就压缩掉），中间那层「跨轮、但会自己结束的事」两边都不管。
历史一被压缩，模型手上就只剩用户刚说的那一句，于是每轮都绕着最后一句转。

三条设计约束：

* **不是记录，是索引。** 存的是「和 TA 有个话题没收尾」，不是那段对话的原文。
  原文会让模型当成逐字记录接着往下念；索引只让它知道有这么件事还开着。
* **必须能结束。** 社交层发布的那份列表**就是**「还开着」的定义——它不再发布某一条，
  那条就算结了（`merge_open` 整体替换）。只增不减会退化成一份永远清不掉的账，
  表现就是「她一直在准备某件事」，那正是过程系统以前锁死过的样子。
* **纯逻辑，不碰 IO。** 读写 state 由 `core_instance.py` 负责，这里只做解析、合并、
  过期与措辞，单测可以直接喂数据。

条目的形状（`src` 目前恒为 `"social"`，留着它是为了以后 Core 自己也要往里写东西时
不必再改形状）：:

    {"text": str, "uid": str, "at": float, "until": float, "src": "social"}
"""

from __future__ import annotations

import math
from typing import Any, Callable, Iterable

SRC_SOCIAL = "social"

# 账本存在 `roles[bid]["self"]` 下的哪个键。放在 self 里而不是 users 里：这些事
# 是**她**记着的，不是某个用户的档案；`state.py` 的角色自留地正好是它该待的地方。
LIFE_KEY = "life_log"

# 还开着的事最多留几条。多了就变成一份待办清单，模型会逐条追着问。
MAX_OPEN = 4
# 一条索引最长多少字。它进 system_prompt，长句子会按字数和缓存一起收税。
TEXT_MAX_CHARS = 40

# 保鲜期：一条「还开着的事」三天没被社交层续上就不再算了（对方多半早答过了）。
# 它同时也是社交层整个停摆时的兜底——那种情况下没人来删，只能靠它自己到期。
OPEN_TTL_HOURS = 72.0


def _text(raw: Any) -> str:
    """一条索引的正文：折叠空白、去首尾、截断。非字符串一律当空。"""
    body = " ".join(str(raw or "").split())
    if len(body) > TEXT_MAX_CHARS:
        body = body[:TEXT_MAX_CHARS].rstrip()
    return body


def _epoch(raw: Any) -> float:
    """时间戳：非数值、NaN、无穷一律当 0（=「不知道」），不抛异常。

    「不知道」与「已经过期」必须分得开——见 `prune`。
    """
    try:
        value = float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def _deadline(now: Any, ttl_hours: float) -> float:
    """到期时刻。now 不可信（0/NaN）时给 0 = 永不过期，宁可多留也别凭空清记忆。"""
    base = _epoch(now)
    if base <= 0:
        return 0.0
    return base + max(0.0, float(ttl_hours)) * 3600.0


def _normalize(raw: Any) -> dict[str, Any] | None:
    """一个原始 dict → 内部条目。认不出（不是 dict、没有正文）就返回 None。"""
    if not isinstance(raw, dict):
        return None
    body = _text(raw.get("text"))
    if not body:
        return None
    return {
        "text": body,
        "uid": str(raw.get("uid") or "").strip(),
        "at": _epoch(raw.get("at")),
        "until": _epoch(raw.get("until")),
        "src": SRC_SOCIAL,
    }


def _as_list(raw: Any) -> list[dict[str, Any]]:
    """把盘上/传来的东西读成条目列表。非 list、非 dict 元素一律丢掉。"""
    if not isinstance(raw, Iterable) or isinstance(raw, (str, bytes, dict)):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        entry = _normalize(item)
        if entry is not None:
            out.append(entry)
    return out


def parse_open(raw: Any) -> list[dict[str, Any]]:
    """解析社交层发布的 `open_items`（`link.SocialSignals.open_items` 用它）。

    这里**不算** `until`：写方给的是发布时刻，到期时刻得由 Core 在摄取时按她自己的
    钟算（见 `merge_open`）。两边的钟本来就可能差着时区，拿对方的时刻当到期时刻
    会算出一个偏几小时的结果。

    社交层在每条上还带一个 `kind`（`"loop"` / `"promise"`），这里不保留：两种情况
    在注入里说的是同一句「还有件事没完」，留着它只会多一个要维护的分支。
    正因为 `kind` 被丢了，措辞必须是两种都能套的中性说法——写成「还没聊完」的话，
    一条她自己许下的诺（「明天给你看那个」）会被读成「刚才那段话被打断了」。
    """
    if not isinstance(raw, list):
        return []
    return [entry for entry in (_normalize(item) for item in raw) if entry][:MAX_OPEN]


def merge_open(
    existing: Any,
    incoming: Any,
    now: Any,
    ttl_hours: float = OPEN_TTL_HOURS,
) -> list[dict[str, Any]]:
    """把社交层发布的「还开着的事」**整体换进来**。

    整体替换是这一层的核心：那份列表就是「还开着」的定义，它不再发布某一条，
    就意味着那条已经结了。改成合并/只增不减，「结了就删」当场失效。

    调用方只在**真的读到了社交层的新鲜信号**时才该调它：文件过期或压根不存在时
    读回来是空的，那种「空」表示「不知道」，不表示「全结了」——拿它当结论会把
    她的未完事项在她不知情的时候整本清掉。社交层停摆的兜底是 `OPEN_TTL_HOURS`。
    """
    fresh: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in parse_open(incoming):
        if item["text"] in seen:
            # 同一件事被社交层列了两遍（它那边 loop 与 promise 可能同时命中）：
            # 留一条，否则同一个话题会占掉两个名额。
            continue
        seen.add(item["text"])
        item["until"] = _deadline(now, ttl_hours)
        fresh.append(item)
    return prune(fresh, now)


def prune(items: Any, now: Any) -> list[dict[str, Any]]:
    """丢掉已经到期（`until < now`）的条目。

    **时间戳不可信的条目留着**：`until` 是 0 表示「不知道什么时候到期」，不是
    「已经过期」。拿 0 当过期会在状态文件被写坏一次之后静默清空整本账——
    `recall.py:_fresh` 对同一个坑是同一个态度。`now` 本身不可信时干脆一条不动。
    """
    entries = _as_list(items)
    now_v = _epoch(now)
    if now_v <= 0:
        return entries
    return [
        entry
        for entry in entries
        if not (entry["until"] > 0 and entry["until"] < now_v)
    ]


def ago_text(at: Any, now: Any) -> str:
    """这件事有多久了，一句话。

    **只用天级说法**：这段文本跟别的事实片段一样，进的是每轮重发的事实块，措辞每轮
    变一次就多占一份预算、还会让「她还开着这件事」看起来像刚发生。
    所以不给钟点、不给日期，只给「刚刚 / 几小时前 / 昨天 / 前天 / N 天前」——
    一天最多跳一次。未来时间戳（钟被回拨过）说不出来，就不说。
    """
    at_v, now_v = _epoch(at), _epoch(now)
    if at_v <= 0 or now_v <= 0:
        return ""
    elapsed = now_v - at_v
    if elapsed < 0:
        return ""
    hours = elapsed / 3600.0
    if hours < 1:
        return "刚刚"
    if hours < 12:
        return "几小时前"
    if hours < 36:
        return "昨天"
    if hours < 60:
        return "前天"
    return f"{int(elapsed // 86400)} 天前"


def _who(uid: str, name_of: Callable[[str], str] | None) -> str:
    """这件事是跟谁有关的。认不出名字就退回「TA」——注入里对对方的称呼就是这个。"""
    if not uid:
        return ""
    if name_of is None:
        return "TA"
    try:
        return str(name_of(uid) or "").strip() or "TA"
    except Exception:
        return "TA"


def render_lines(
    items: Any,
    now: Any,
    max_open: int = 3,
    name_of: Callable[[str], str] | None = None,
) -> list[str]:
    """写成注入用的几段**纯事实**，交给 `prompt_builder` 拼进本轮那句事实里。

    **不带句号**：事实块由 `_sentence` 用「，」把这些片段拼成一句、`_finish` 在末尾
    统一补「。」；这里自带句号会拼出「…。，…。」这种断句。

    **不另起标题**：`_sentence` 的规矩是「没有标题、没有标签——标签是束缚词」。一个
    带标题的独立段落最像「要念出来的内容」，模型会照着报；这里它只是跟「TA之前说过
    …」并列的一句事实。

    **顺序必须确定**：先按 `at` 降序，`at` 相同再按 `text` 排。社交侧发布时 `at`
    是稳定的，但这里不能依赖它——顺序不定的话同一批内容每轮渲染出的文本可能不一样，
    事实块每轮重发、每轮都不一样就等于白占预算。`at` 不可信（0）的条目排在最后，
    因此最可能被 `max_open` 截掉——说不清什么时候的事，最不该占位置。

    **同一个人的几件事合成一句**：连着写两遍「和TA之间还有件事没完：…」读起来就是一份
    清单，而这是她心里的一句话。合成「和TA之间还有事没完：甲、乙」之后，它仍旧只是一段
    事实，念不出来也报不出来。
    """
    entries = sorted(_as_list(items), key=lambda entry: (-entry["at"], entry["text"]))
    # 按**身份**（uid）聚成一组，相邻的同一个人合成一句。分组只认相邻：entries 已按时间排好，
    # 跨过去把同一个人的事凑到一起会打乱「新的在前」。按 uid 而不是按渲染出来的名字分组，
    # 是因为两个都认不出名字的人渲染出来都是「TA」——按名字分会把两个人的事并成一句。
    groups: list[tuple[str, str, list[str]]] = []
    for entry in entries[: max(0, int(max_open))]:
        ago = ago_text(entry["at"], now)
        piece = f"{entry['text']}（{ago}）" if ago else entry["text"]
        uid = str(entry.get("uid") or "")
        if groups and groups[-1][0] == uid:
            groups[-1][2].append(piece)
        else:
            groups.append((uid, _who(entry["uid"], name_of), [piece]))

    lines: list[str] = []
    for _uid, who, pieces in groups:
        body = "、".join(pieces)
        if who:
            lines.append(
                f"和{who}之间还有件事没完：{body}" if len(pieces) == 1
                else f"和{who}之间还有事没完：{body}"
            )
        else:
            # 不知道是跟谁的事（多人群里 uid 缺失）就不硬套一个人称，否则指错人。
            lines.append(
                f"她手上还有件事没完：{body}" if len(pieces) == 1
                else f"她手上还有事没完：{body}"
            )
    return lines
