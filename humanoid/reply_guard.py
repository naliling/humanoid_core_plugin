"""出站复读拦截：她自己说过的话，短窗口内不许原样再来一遍（v3.1.31）。

问题不是模型「写错」，是它**不会变通**：同一个地点、同一句话，连着几轮原样搬
出来（线上实例：「我在厨房呢」连着三轮，「刚在沙发养神呢」连着四轮）。这类复读
读起来像报备、像卡带，比说错更不像人。

修法按「不会变通就别念」：在她的话发出去之前过一道**确定性**检查——

* **地点复读**：某个家里地点（厨房/客厅/沙发…）在她最近 `window` 条里已经出现
  `place_hits` 次，这一条里又带着它 → 含这个地点的那一小句整句去掉。
* **小句复读**：这一句（去掉标点后）与最近某条里的某一句相同或互相包含
  （都不短于 `min_chars`）→ 这一句去掉。

两道都只**删句**，不换词、不生成新内容：删完还剩东西就发删过的版本；一句都
不剩就原样发出去（宁可复读，也不许发空消息）。

这里只处理「她自己重复自己」，不碰内容判据（设备/含糊/私事在 schedule_content
那一侧，日程侧还有硬闸）。整段是纯函数，方便单测。
"""

from __future__ import annotations

import re
from typing import Iterable, List, Sequence, Tuple

from astrbot.api import logger

#: 家里会被她当成「我在哪儿」说出口的地点。只认这些，避免把「厨房纸巾」这类
#: 名词误判成地点复读。
PLACES: Tuple[str, ...] = (
    "厨房", "客厅", "卧室", "阳台", "沙发", "窗边", "桌边", "浴室", "卫生间",
    "玄关", "院子", "床边", "书桌", "餐桌",
)

#: 小句分隔符（保留在原文里，重组时按原顺序拼回去）。
_SPLIT = re.compile(r"([，,。！？!?；;…、~～]+)")

#: 少于这个字数的小句不参与复读判定——「嗯」「好呀」本来就会重复。
MIN_CLAUSE_CHARS = 4

#: 互相包含判定时，两边都要够长，否则「我在厨房」会把「厨房里」吃掉。
MIN_DUP_CHARS = 6

#: 纯称呼（拉丁字母/数字的用户名，比如「Regnator」）不参与小句复读：她每句都叫一声
#: 对方的名字是称呼，不是卡带；删掉它句子就变成没头没尾的自言自语。
ADDRESS_MAX_CHARS = 32

_CJK = re.compile(r"[\u4e00-\u9fff]")


def _is_address(norm: str) -> bool:
    """这句是不是纯称呼：不含汉字、且不太长（用户名/英文昵称）。"""
    return bool(norm) and len(norm) <= ADDRESS_MAX_CHARS and not _CJK.search(norm)


#: AstrBot 分段流式渲染时会先把响应对象填成占位文本，真文本要等渲染完才写回来。
#: 这些占位符**不是她说的话**：一旦被当成原话记进 `spoke_log`，情绪分析、提示词、
#: 巡检都会把它当真（线上实例：`(Model response will be rendered here)` 躺在账本里）。
#: 只认死证据，不做模糊匹配——宁可漏一条，也不能把她的真话判成占位符扔了。
PLACEHOLDER_MARKERS: Tuple[str, ...] = (
    "model response will be rendered here",
    "response will be rendered here",
    "will be rendered here",
)


def is_placeholder(text: str) -> bool:
    """这段文本是不是渲染占位符（不是她真正说出来的话）。"""
    low = str(text or "").strip().lower()
    if not low:
        return False
    return any(marker in low for marker in PLACEHOLDER_MARKERS)


#: 模型安全策略拒绝时吐出来的英文样板。这类文本**不是她的话**，是生成失败：
#: 线上实例（2026-11-14 扫 `spoke_log` 384 条时发现）：一条
#: `Your request involves sexually suggestive roleplay ... I am refusing`
#: 被当成她的发言记进账本，还发出去了。用户的原话是「发言看起来很傻」。
REFUSAL_MARKERS: Tuple[str, ...] = (
    "i'm sorry",
    "i am sorry",
    "i cannot",
    "i can't",
    "i won't",
    "i will not",
    "as an ai",
    "your request",
    "i must decline",
    "i'm unable",
    "i am unable",
    "i'm not able",
    "i am not able",
    "sorry, but",
)

#: 一句中文都没有、又不算短语气词时，按「模型在用英文说话」处理——她的角色都说中文，
#: 整句英文不是她。短词（比如 `Wind`）不判，免得把正常的英文昵称/词当拒绝。
NON_CJK_MIN_CHARS = 12

#: 一句中文都没有的长句，只有命中这些拒绝语义特征才当拒绝拦掉（v3.1.42）。
#: 以前「非中文 + 够长」就直接判拒绝，英文/非中文人设的正常句子会被永久静音。
NON_CJK_REFUSAL_HINTS: Tuple[str, ...] = (
    "cannot", "can not", "can't", "unable", "not able", "refuse",
    "won't", "will not", "must decline", "as an ai", "sorry",
)


def looks_like_refusal(text: str) -> bool:
    """这句是不是模型的拒绝/英文样板（不是她该发出去的话）。"""
    raw = str(text or "").strip()
    if not raw:
        return False
    low = raw.lower()
    if any(marker in low for marker in REFUSAL_MARKERS):
        return True
    if not _CJK.search(raw) and len(raw) >= NON_CJK_MIN_CHARS:
        # 英文/非中文人设的正常句子不是拒绝：只有命中拒绝语义特征才拦。
        if any(hint in low for hint in NON_CJK_REFUSAL_HINTS):
            return True
        logger.warning("非中文回复未命中拒绝特征，放行（前 40 字）：" + raw[:40])
    return False


def _clauses(text: str) -> List[Tuple[str, str]]:
    """切成 [(小句, 分隔符), ...]；分隔符为空串表示最后一段。"""
    parts = _SPLIT.split(str(text or ""))
    out: List[Tuple[str, str]] = []
    for i in range(0, len(parts), 2):
        body = parts[i]
        delim = parts[i + 1] if i + 1 < len(parts) else ""
        if body or delim:
            out.append((body, delim))
    return out


def _norm(text: str) -> str:
    return re.sub(r"[\s，,。！？!?；;…、~～\"'“”‘’（）()]+", "", str(text or ""))


def places_in(text: str) -> List[str]:
    body = str(text or "")
    return [p for p in PLACES if p in body]


def trim_repeats(
    text: str,
    recent: Iterable[str],
    *,
    window: int = 3,
    place_hits: int = 2,
    min_chars: int = MIN_CLAUSE_CHARS,
) -> Tuple[str, List[str]]:
    """删掉与最近几条重复的小句。返回 (新文本, 被删的小句)。

    `recent` 是她自己最近说过的话（越靠后越新）。删完一句不剩时原样返回——
    不许因为「怕复读」而发出空消息。
    """
    body = str(text or "").strip()
    if not body:
        return body, []
    window = max(1, int(window))
    recent_texts = [str(t).strip() for t in (recent or []) if str(t).strip()][-window:]
    if not recent_texts:
        return body, []

    hits: dict = {}
    for old in recent_texts:
        for place in places_in(old):
            hits[place] = hits.get(place, 0) + 1
    sticky = {p for p, n in hits.items() if n >= max(2, int(place_hits))}

    old_clauses = []
    for old in recent_texts:
        for clause, _ in _clauses(old):
            norm = _norm(clause)
            if len(norm) >= min_chars:
                old_clauses.append(norm)

    kept: List[Tuple[str, str]] = []
    dropped: List[str] = []
    for clause, delim in _clauses(body):
        norm = _norm(clause)
        place_hit = [p for p in sticky if p in clause]
        dup = False
        if len(norm) >= min_chars and not _is_address(norm):
            for old in old_clauses:
                if norm == old:
                    dup = True
                    break
                if len(norm) >= MIN_DUP_CHARS and len(old) >= MIN_DUP_CHARS:
                    if norm in old or old in norm:
                        dup = True
                        break
        if place_hit or dup:
            dropped.append(clause)
            continue
        kept.append((clause, delim))

    if not dropped:
        # 一句都没删：原文一个字都不动（包括结尾标点）。
        return body, []
    if not kept:
        # 整条都是复读：宁可原样发，也不发空。
        return body, dropped
    rebuilt = "".join(c + d for c, d in kept).strip()
    # 行首和句尾的分隔符都要清：删掉第一句会留下行首「，」，删掉**末尾**那句会让
    # 前一句的分隔符挂在句尾（「刚把碗洗完，」再接原句末标点就成了「刚把碗洗完，。」）。
    # 句尾标点（。！？）不动——下面还要按原句末标点补一次。
    rebuilt = rebuilt.lstrip("，,。！？!?；;…、~～ ").rstrip("，,、；;：:…~～ ")
    if rebuilt and body[-1] in "。！？!?…" and rebuilt[-1] not in "。！？!?…":
        rebuilt += body[-1]
    if len(_norm(rebuilt)) < 2:
        return body, dropped
    return rebuilt, dropped
