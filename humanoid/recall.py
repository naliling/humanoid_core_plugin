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
# 超过这么久没被引用的原话就彻底作废（读取与落盘共用同一个窗口）。
STALE_HOURS = 12.0

_STRIP = re.compile(r"\s+")


def _fresh(item: dict[str, Any], now: float, stale_hours: float = STALE_HOURS) -> bool:
    """这条摘录还在时间窗内吗。时间戳不可信（NaN/非数值）时一律**留着**，别凭空清记忆。"""
    try:
        age = float(now) - float(item.get("at", 0) or 0)
        window = float(stale_hours) * 3600.0
    except (TypeError, ValueError):
        return True
    if age != age or window != window:
        return True
    return age <= window


_OPENERS = "([（［【「『"
_CLOSERS = {")": "(", "）": "（", "]": "[", "］": "［", "】": "【", "」": "「", "』": "『"}
_TAIL_PUNCT = "，。！？、,.!?;；:： "


def _close_brackets(text: str) -> str:
    """把括号配平：丢掉落单的右括号，未闭合的左括号及其后面全部截掉。

    线上真实注入过：`TA之前说过「抱着亲爱的灵灵摸摸头)」` —— 摘录是从一句语C动作
    的第 8 个字起截的，左括号早就落在窗口外，右括号却留下来了。这句被当成「TA
    说过的话」递给模型，读起来像坏掉的数据；模型照着它接，接出来的也是半截动作。
    """
    body = str(text or "").rstrip()
    if not body:
        return ""
    stack: list[int] = []
    drop: set[int] = set()
    for i, ch in enumerate(body):
        if ch in _OPENERS:
            stack.append(i)
        elif ch in _CLOSERS:
            if stack and body[stack[-1]] == _CLOSERS[ch]:
                stack.pop()
            else:
                drop.add(i)          # 落单的右括号：删掉它，别删它后面的正文
    # 未闭合的左括号：它后面全是半句话，从最早的那个左括号起截掉
    cut = stack[0] if stack else len(body)
    kept = "".join(
        ch for i, ch in enumerate(body) if i < cut and i not in drop
    )
    return kept.rstrip(_TAIL_PUNCT)


def snippet(text: str, limit: int = SNIPPET_LEN) -> str:
    """把一句话收成一段能引用的短摘录。"""
    body = _STRIP.sub(" ", str(text or "")).strip()
    if len(body) <= limit:
        return _close_brackets(body)
    cut = body[:limit]
    # 在标点处收尾，别把词切一半。搜索下界是「截断点的一半」：更靠前的标点离得
    # 太远，为它丢掉半句话不值得（原写法 `max(limit // 2, 0)` 的 0 是多余的）。
    floor = max(0, limit // 2)
    for i in range(len(cut) - 1, floor - 1, -1):
        if cut[i] in "，。！？、,.!?;；:：":
            return _close_brackets(cut[:i].rstrip("，。！？、,.!?;；:："))
    closed = _close_brackets(cut.rstrip())
    return (closed + "…") if closed else ""


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

    顺带把**已经出了时间窗**的旧摘录丢掉：它们永远不会再被注入（`recall_lines` 会
    过滤掉），留着只是让 state.json 里多几条死数据。
    """
    current = [item for item in _as_list(existing) if _fresh(item, now)]
    body = str(text or "").strip()
    if len(body) < min_len:
        return current
    piece = snippet(body)
    if not piece:
        return current
    # 括号配平之后可能只剩几个字（整条都是语C动作时几乎必然如此）。那种半句话
    # 存下来比不存更糟：它会在下一轮以「TA之前说过「…」」的身份出现。
    if len(piece) < 4:
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
    stale_hours: float = STALE_HOURS,
) -> list[str]:
    """把采样到的原话写成注入用的一行。太久以前的不算「之前说过」。

    窗口从 60 小时收到 12 小时：60 小时里挂着的是「两天半前说过的话」，真人早忘了，
    而模型看到它就会顺着翻旧账。「之前说过」本来就该只覆盖最近的一小段时间。
    `behavior._touches_recalled` 也走这里，所以「TA是不是又提起旧事」同样只看窗口内。

    **读的时候也要过一遍括号配平**：盘上已经存着的条目是旧版写进去的，
    其中有 `抱着亲爱的灵灵摸摸头)` 这种从语C动作中间截出来的半句。只在写入侧修
    治不好已有的数据——而这份数据每一天都在被注入到模型眼前。
    """
    items = [x for x in _as_list(existing) if str(x.get("said", "")).strip()]
    fresh = [x for x in items if _fresh(x, now, stale_hours)]
    if not fresh:
        return []
    quotes: list[str] = []
    for item in fresh[: max(0, int(max_items))]:
        cleaned = _close_brackets(str(item.get("said", "")).strip())
        if len(cleaned) < 4:
            continue
        quotes.append(f"「{cleaned}」")
    if not quotes:
        return []
    return [f"TA之前说过{'、'.join(quotes)}"]
