"""注入取舍层（R7）：两处注入共用的唯一选材与拼接。

## 这一层为什么必须存在

她每一轮拿到的背景来自两处：

1. main._state_note() 拼的「此刻的她」（关系/心境/日程/外界/穿搭）；
2. core.build_injection() → PromptBuilder 拼的「〔她的身体与生活〕」事实块。

这两处**各自**决定「说什么」和「按什么顺序说」，于是同一个事实可以出现在两处、
同一条规则可以有两种写法（「她在睡」与「她正睡着」）、一处有预算一处没有。
历史上「同一件事说两遍」被修过至少两次（docs/REGRESSIONS.md），每次都是把某一
句话挪走，而不是让「取舍」本身只有一个地方——所以它总会从别处回来。

这一层把取舍收敛成三个纯函数：

* Fact：一句话 + 它的身份（key）、互斥组（group）、优先级（priority）、
  生效条件（when）。事实怎么算出来的不归这里管。
* select()：条件过滤 → 按 key 去重 → 组内互斥 → 近似文本去重 → 预算/字数裁剪。
* render()：组内用「，」、组间用「。」，并把相邻同主语的句子合并，避免
  「她……。她……。她……。」这种复读式并列。

**纯函数**：不读配置、不读时钟、不写状态。同样的事实进来，永远同样的文本出去，
所以它可以被直接断言，也可以在诊断页里离线重放。

## 两条纪律

1. **文案与判定分离**：这里只处理「已经写好的句子」。哪句话该不该说，是
   when 与 priority 的事，不是字符串里有没有某个词的事（历史回归：靠
   "在睡" not in line 过滤，同义换个词就漏）。
2. **预算只有一个出口**：budget_tokens（模型侧）与 max_chars（字面红线）都在
   select() 里执行。以前 INJECT_MAX_CHARS 只是测试里的不变量，生产代码从不裁剪。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "Fact",
    "select",
    "render",
    "estimate_tokens",
    "INJECT_MAX_CHARS",
    "HEADER_OVERHEAD_TOKENS",
]

#: 注入正文的**字面**红线（字符数）。以前这个常量只有测试引用，生产代码从不裁剪；
#: 现在它是 select() 的出口参数，PromptBuilder._finish() 与 _state_note()
#: 都走同一条路。数值按注入档位给，mood_only 最窄。
INJECT_MAX_CHARS: Dict[str, int] = {"medium": 2000, "full": 3000, "mood_only": 500}

#: 抬头「〔她的身体与生活 v12〕」+ 换行的粗略 token 成本（中文按 1 字 1 token 估）。
HEADER_OVERHEAD_TOKENS = 16

#: 同一句里可以省略的主语。只有代词能省：省掉「小雨」会把主语变成别人。
_SUBJECTS: Tuple[str, ...] = ("她", "他", "你")

#: 计算「差不多是同一句话」时忽略的字符（标点与空白）。
_NOISE = set(" \t\r\n，。、；：！？…—·「」『』（）()【】[]\"'“”‘’,.!?;:")


def estimate_tokens(text: str) -> int:
    """粗略 token 估算：中文按字、ASCII 按 4 字符 1 token。

    与 PromptBuilder 里的估算同一口径——两边估得不一样，预算就永远对不上。
    """
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    wide_chars = len(text) - ascii_chars
    return wide_chars + (ascii_chars + 3) // 4


@dataclass(frozen=True, slots=True)
class Fact:
    """一句话，以及它凭什么出现在注入里。

    * key：这件事的身份。同一个 key 只留一条（后出现的不会覆盖先出现的，
      除非它优先级更高）——「同一件事说两遍」在这里被结构性挡住。
    * group：互斥组。同组只留优先级最高的一条（例如「她在睡」与「她刚睡醒」
      不该同时出现）。空串表示不参与互斥。
    * priority：越高越先保。预算不够时从低往高丢。
    * when：生效条件标签，由调用方在 flags 里给真假。空串表示无条件。
    * source：出处，只用于排查（诊断页会把它打出来）。
    """

    key: str
    text: str
    group: str = ""
    priority: float = 5.0
    when: str = ""
    source: str = ""


def _clean(text: str) -> str:
    return str(text or "").strip()


def _squash(text: str) -> str:
    """去掉标点与空白，用来判「两句话是不是同一件事」。"""
    return "".join(ch for ch in _clean(text) if ch not in _NOISE)


def _dedupe_keys(facts: Sequence[Fact]) -> List[Fact]:
    """同一 key 只留优先级最高的一条；同优先级留先出现的（顺序稳定）。"""
    best: Dict[str, int] = {}
    out: List[Fact] = []
    for fact in facts:
        idx = best.get(fact.key)
        if idx is None:
            best[fact.key] = len(out)
            out.append(fact)
            continue
        if fact.priority > out[idx].priority:
            out[idx] = fact
    return out


def _dedupe_groups(facts: Sequence[Fact]) -> List[Fact]:
    """互斥组内只留优先级最高的一条。"""
    best: Dict[str, Fact] = {}
    for fact in facts:
        if not fact.group:
            continue
        current = best.get(fact.group)
        if current is None or fact.priority > current.priority:
            best[fact.group] = fact
    out: List[Fact] = []
    for fact in facts:
        if not fact.group:
            out.append(fact)
            continue
        if best.get(fact.group) is fact:
            out.append(fact)
    return out


def _dedupe_text(facts: Sequence[Fact]) -> List[Fact]:
    """近似文本去重：一句是另一句的子串时只留优先级高的那条。

    「她刚睡醒」与「她刚睡醒（7 分钟前）」是同一件事的两种写法，两处各生成一条
    就会在一轮里说两遍。这里按**去掉标点后的包含关系**判，不是按字面相等。
    """
    squashed = [(_squash(f.text), f) for f in facts]
    doomed: set = set()
    for i, (left, fact_i) in enumerate(squashed):
        if not left or i in doomed:
            continue
        for j, (right, fact_j) in enumerate(squashed):
            if i == j or j in doomed or not right:
                continue
            if left == right or left in right or right in left:
                # 长的留下；一样长则优先级高的留下；再一样则先出现的留下。
                if len(left) > len(right):
                    doomed.add(j)
                elif len(left) < len(right):
                    doomed.add(i)
                    break
                elif fact_i.priority >= fact_j.priority:
                    doomed.add(j)
                else:
                    doomed.add(i)
                    break
    return [f for i, f in enumerate(facts) if i not in doomed]


def _fit(
    facts: List[Fact],
    *,
    budget_tokens: Optional[int],
    max_chars: Optional[int],
    estimate: Callable[[str], int],
    header_tokens: int,
) -> List[Fact]:
    """按优先级从高到低收，超预算/超字数就丢低优先级的。

    顺序：先按 token 预算收一遍，再按字面红线收一遍（两条闸都要过）。返回的顺序
    仍是调用方给的原始顺序——**选材看优先级，说话看顺序**，两者不该混。
    """
    kept = list(facts)
    if budget_tokens is not None and budget_tokens > 0:
        used = header_tokens + 1
        keep_idx: List[int] = []
        for i in sorted(range(len(kept)), key=lambda j: -kept[j].priority):
            cost = estimate(kept[i].text) + 1
            if used + cost > budget_tokens:
                continue
            keep_idx.append(i)
            used += cost
        keep_idx.sort()
        kept = [kept[i] for i in keep_idx]
    if max_chars is not None and max_chars > 0:
        # 字数可以精确算：正文 = 各句用「。」连接。
        while kept and len(render(kept)) > max_chars:
            lowest = min(range(len(kept)), key=lambda i: (kept[i].priority, -i))
            del kept[lowest]
    return kept


def select(
    facts: Iterable[Fact],
    *,
    flags: Optional[Mapping[str, bool]] = None,
    budget_tokens: Optional[int] = None,
    max_chars: Optional[int] = None,
    estimate: Callable[[str], int] = estimate_tokens,
    header_tokens: int = HEADER_OVERHEAD_TOKENS,
) -> List[Fact]:
    """从候选事实里选出这一轮要说的话。

    flags 给 when 标签的真假：标签不在里面、或值为假，这条就整条丢——
    「关掉睡眠时段开关」这类开关因此只需要在**生成处**给标签，不需要在每个
    生成函数里各写一遍守卫（历史回归：关掉开关后睡眠信息从另外两条路径漏进来）。
    """
    conditions: Mapping[str, bool] = flags or {}
    alive: List[Fact] = []
    for fact in facts:
        text = _clean(fact.text)
        if not text:
            continue
        if fact.when and not conditions.get(fact.when, False):
            continue
        alive.append(fact if text == fact.text else replace(fact, text=text))
    alive = _dedupe_keys(alive)
    alive = _dedupe_groups(alive)
    alive = _dedupe_text(alive)
    return _fit(
        alive,
        budget_tokens=budget_tokens,
        max_chars=max_chars,
        estimate=estimate,
        header_tokens=header_tokens,
    )


def _merge_subject(prev: str, nxt: str) -> str:
    """把「她有点累」+「她刚睡醒」合成「她有点累，刚睡醒」。

    只合并代词主语，且只合并相邻两句：跨组不合并（组是语义边界），换主语不合并
    （省掉「小雨」会把主语变成别人）。
    """
    for subject in _SUBJECTS:
        if not (prev.startswith(subject) and nxt.startswith(subject)):
            continue
        rest = nxt[len(subject):]
        if not rest or rest[0] in "，。、；：！？…—":
            continue
        return f"{prev}，{rest}"
    return ""


def render(facts: Sequence[Fact]) -> str:
    """把选好的事实拼成注入正文（不含抬头、不含句尾句号）。

    组内用「，」、组间用「。」；相邻同主语的句子合并主语。组是**语义边界**：
    「她在睡」与「外头下雨」不该被拼成一句。
    """
    groups: List[List[Fact]] = []
    order: Dict[str, int] = {}
    for fact in facts:
        name = fact.group or f"__solo_{len(groups)}"
        if name not in order:
            order[name] = len(groups)
            groups.append([])
        groups[order[name]].append(fact)
    sentences: List[str] = []
    for group in groups:
        merged = ""
        for fact in group:
            text = _clean(fact.text)
            if not text:
                continue
            if not merged:
                merged = text
                continue
            combined = _merge_subject(merged, text)
            merged = combined or f"{merged}，{text}"
        if merged:
            sentences.append(merged)
    return "。".join(sentences)
