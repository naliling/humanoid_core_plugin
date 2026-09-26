"""隔一段时间留下对方说过的一句原话。

注入里一句「TA之前说过『我猫今天吐了』」比任何关键词抽取都稳：中文没有分词库时，
关键词会从「今天跟产品经理吵了一架」里切出「跟产品经理」这种半截话，模型拿去说反而
更像机器人。原话短摘录不需要理解句子，只需要挑得准、存得少。

也不是把每条消息都存下来——那样它只是最近几条聊天记录，AstrBot 自己的上下文里已经有了。
这里按最小间隔采样，攒的是「半小时前、两小时前」说过什么；超过 12 小时就不再往上下文里递
（采样本身留着，`behavior._touches_recalled` 拿它判断「TA这句是不是又提起旧事」）。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

MAX_SNIPPETS = 4
# 短于这个长度的话没有记住的价值（「嗯」「好的」不算一段记忆）
MIN_LEN = 8
# 摘下来的原话最长多少字
SNIPPET_LEN = 26
# 两条摘录之间至少隔多久，单位秒。太密就退化成「最近几条消息」。
MIN_GAP_SECONDS = 1800.0

_STRIP = re.compile(r"\s+")


def snippet(text: str, limit: int = SNIPPET_LEN) -> str:
    """把一句话收成一段能引用的短摘录。"""
    body = _STRIP.sub(" ", str(text or "")).strip()
    if len(body) <= limit:
        return body
    cut = body[:limit]
    # 在标点处收尾，别把词切一半
    for i in range(len(cut) - 1, max(limit // 2, 0) - 1, -1):
        if cut[i] in "，。！？、,.!?;；:：":
            return cut[:i].rstrip("，。！？、,.!?;；:：")
    return cut.rstrip() + "…"


def note_said(
    existing: Any,
    text: str,
    now: float,
    *,
    limit: int = MAX_SNIPPETS,
    gap: float = MIN_GAP_SECONDS,
    min_len: int = MIN_LEN,
) -> list[dict[str, Any]]:
    """把这条消息按采样规则并入记忆列表，返回新列表（不就地改 existing）。

    列表元素是 `{"said": 原话, "at": epoch}`，最新在前。没到长度门槛、或距上一条
    采样太近时原样返回，调用方可以拿「有没有变化」决定要不要写盘。
    """
    current = _as_list(existing)
    body = str(text or "").strip()
    if len(body) < min_len:
        return current
    piece = snippet(body)
    if not piece:
        return current
    if current and str(current[0].get("said", "")) == piece:
        return current
    try:
        latest = max((float(item.get("at", 0) or 0) for item in current), default=0.0)
    except (TypeError, ValueError):
        latest = 0.0
    if latest and float(now) - latest < max(0.0, float(gap)):
        return current
    cap = max(0, int(limit))
    if cap <= 0:
        return []
    return [{"said": piece, "at": float(now)}] + current[: cap - 1]


def _as_list(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, Iterable) or isinstance(raw, (str, bytes, dict)):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        said = str(item.get("said", "") or "").strip()
        if not said:
            continue
        try:
            at = float(item.get("at", 0) or 0)
        except (TypeError, ValueError):
            at = 0.0
        out.append({"said": said, "at": at})
    return out


def recall_lines(
    existing: Any,
    now: float,
    max_items: int = 1,
    stale_hours: float = 12.0,
) -> list[str]:
    """把采样到的原话写成注入用的一行。太久以前的不算「之前说过」。

    窗口从 60 小时收到 12 小时：60 小时里挂着的是「两天半前说过的话」，真人早忘了，
    而模型看到它就会顺着翻旧账。「之前说过」本来就该只覆盖最近的一小段时间。
    """
    items = [x for x in _as_list(existing) if str(x.get("said", "")).strip()]
    fresh = [x for x in items if float(now) - float(x.get("at", 0) or 0) <= stale_hours * 3600.0]
    if not fresh:
        return []
    quotes = "、".join(f"「{x['said']}」" for x in fresh[: max(0, int(max_items))])
    return [f"TA之前说过{quotes}"]
