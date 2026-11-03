"""从人设正文里认出「她是哪一种人」——只认明确的生活特质，不做性格分析。

为什么需要这一层：日程 prompt 的默认语气是「把一天排满、白天做事、夜里睡觉」，
`ProcessService` 的模板池也是固定的「认真工作 / 整理资料」。但人设里明明可能写着
夜猫子、倒班、懒散、宅——这些词以前**没有任何一处被读过**，于是所有角色都被当成
同一个作息规律、一样勤快的模板人。

识别原则（保守）：

* 只认**明确正向词**（「夜猫子」「熬夜」「拖延」），不猜语义；
* 简单否定不算命中（「她不熬夜」「不爱出门」），词本身自带否定的除外（「不爱动」）；
* 拿不到人设、正文为空、全是脏数据时返回全 False 的 `PersonaTraits`，
  下游按「没有任何特质」走默认路径——行为与旧版完全一致。

这一层不写状态、不读 IO：`detect()` 是纯函数，注入热路径上每条消息调一次也没关系。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from .injection import Fact, select

# ── 词表 ──────────────────────────────────────────────────────────────
# 每张表都只放**明确指向这种生活状态**的词。宁可漏（顶多回到默认语气），
# 不要误伤（把「认真工作」的角色认成懒散，或反过来）。
_NIGHT_OWL_WORDS = (
    "夜猫子", "熬夜", "晚睡", "凌晨", "夜里活动", "昼伏夜出", "白天补觉",
    "夜班", "倒班", "通宵",
)
_EARLY_BIRD_WORDS = ("早起", "早睡早起", "清晨", "晨跑", "作息规律")
_DILIGENT_WORDS = ("勤快", "自律", "努力", "认真", "闲不住", "上进", "工作狂")
# 「懒」单字会命中「偷懒」「懒得」等，但人设里出现「懒」几乎都在说这个人的状态，
# 且它比「懒散/拖延」更常出现在短人设里，所以保留，只靠否定词挡「不懒」。
_LAZY_WORDS = ("懒", "懒散", "摸鱼", "拖延", "躺平", "不爱动", "随性", "拖延症")
_HOMEBODY_WORDS = ("宅", "家里蹲", "不爱出门", "社恐", "内向")
_OUTGOING_WORDS = ("外向", "爱出门", "爱社交", "朋友多", "热闹")
_STUDENT_WORDS = ("学生", "上课", "大学")
_WORKER_WORDS = ("上班", "工位", "夜班", "倒班")

# 简单否定词。只看关键词前面同一分句里的几个字：跨过逗号/句号就换了一句话，
# 上一句的「不」不该否掉下一句的「熬夜」。
_NEGATIONS = ("不", "没", "别", "勿", "从未", "从不", "并不", "不算", "不太", "不怎么")
_CLAUSE_BREAK = re.compile(r"[，。！？；、,.!?;:：\s\n]")
# 关键词前看几个字。够覆盖「她不是很」这种 4 字否定，又不会跨太远误伤。
_NEGATION_WINDOW = 6


@dataclass(frozen=True)
class PersonaTraits:
    """从一份人设里读出来的生活特质。

    全部为 False 是合法结果（人设没写、或写得很中性），下游会走默认行为。
    `hints` 是给 prompt 的中文线索，已经处理过互相矛盾的组合。
    """

    night_owl: bool = False
    early_bird: bool = False
    diligent: bool = False
    lazy: bool = False
    homebody: bool = False
    outgoing: bool = False
    shift_worker: bool = False
    student: bool = False
    worker: bool = False
    hints: tuple[str, ...] = ()


def _contains(text: str, word: str) -> bool:
    """`word` 在 `text` 里是否**有一次未被否定**的出现。

    找遍所有出现位置而不是只看第一处：人设里可能先写「不熬夜」再写「最近总熬夜」，
    只看第一处会整条漏掉。
    """
    start = 0
    while True:
        index = text.find(word, start)
        if index < 0:
            return False
        if not _negated(text, index, word):
            return True
        start = index + 1


def _negated(text: str, index: int, word: str) -> bool:
    """关键词这一次出现是不是被简单否定掉了。

    词本身带否定的（「不爱动」「不爱出门」）不做前缀判断：那正是要命中的表达，
    而且「爱出门」是「不爱出门」的子串，前缀判断已经在那一条上生效了。
    """
    if word.startswith(("不", "没", "无", "别", "勿")):
        return False
    window = text[max(0, index - _NEGATION_WINDOW):index]
    chunk = _CLAUSE_BREAK.split(window)[-1]
    return any(neg in chunk for neg in _NEGATIONS)


def _hits(text: str, words: tuple[str, ...]) -> int:
    """命中了几个**不同的**词。用于勤快/懒散同时出现时挑更明确的那一个。"""
    return sum(1 for word in words if _contains(text, word))


def _hints(traits: PersonaTraits, lazy_hits: int, diligent_hits: int) -> tuple[str, ...]:
    """把特质翻成给模型看的「她的状态」，不是给模型的指令。

    两条纪律（审核 #46 / #55）：

    1. **同组只留一条**：倒班、夜猫子、早起型说的是同一个「作息」事实，各自成句
       会互相重叠（「夜班」人设原来同时拿到「夜猫子」「倒班/夜班」「上班族」三行）。
       互斥交给 injection.select() 的 group，不在这里写 if/elif 拼装。
    2. **只陈述，不命令**：这里递的是「她是什么样的人」，渲染进 prompt 的是背景。
       出现「请 / 要 / 应该 / 记得 / 别 / 可以」就把背景变成了指令——模型会照着
       执行而不是按她这个人生成，且用户改人设也改不动这几句。
    """
    facts: list[Fact] = []
    # 作息组：倒班最强（它同时解释「白天补觉」和「凌晨清醒」），其次夜猫子，再次早起型。
    if traits.shift_worker:
        facts.append(
            Fact(
                "sleep.shift",
                "倒班/夜班：作息不按白天走，睡眠常在白天补，00:00-06:00 也可能是她清醒的时间。",
                group="sleep",
                priority=8.0,
            )
        )
    if traits.night_owl:
        facts.append(
            Fact(
                "sleep.night_owl",
                "夜猫子：凌晨 00:00-06:00 反而更清醒，白天补觉是常态。",
                group="sleep",
                priority=7.0,
            )
        )
    if traits.early_bird:
        facts.append(
            Fact(
                "sleep.early_bird",
                "早起型：清晨最清醒，晚上容易困，凌晨在睡是正常的。",
                group="sleep",
                priority=6.0,
            )
        )
    # 勤快与懒散互斥：只给一条，否则 prompt 自己打架。
    prefer_lazy = traits.lazy and (not traits.diligent or lazy_hits > diligent_hits)
    if prefer_lazy:
        facts.append(
            Fact(
                "diligence.lazy",
                "不算勤快：赖床、拖延、发呆、躺一整天都是她会有的日子。",
                group="diligence",
                priority=6.0,
            )
        )
    if traits.diligent:
        facts.append(
            Fact(
                "diligence.diligent",
                "勤快自律：工作与学习占得比较多，日子过得有条理。",
                group="diligence",
                priority=5.0,
            )
        )
    if traits.homebody:
        facts.append(
            Fact("social.homebody", "偏宅：更常待在家里或附近，出门少。", group="social", priority=6.0)
        )
    if traits.outgoing:
        facts.append(
            Fact(
                "social.outgoing",
                "外向爱出门：常出门见人，喜欢热闹的地方。",
                group="social",
                priority=5.0,
            )
        )
    if traits.student:
        facts.append(
            Fact(
                "job.student",
                "学生：上课、自习、作业是她日常的一部分。",
                group="job",
                priority=6.0,
            )
        )
    # 倒班本身就是工作状态：再递一句「上班族」只是同一件事换个说法。
    if traits.worker and not traits.shift_worker:
        facts.append(
            Fact(
                "job.worker",
                "上班族：有工作和通勤，日子按上班的节奏走。",
                group="job",
                priority=5.0,
            )
        )
    return tuple(fact.text for fact in select(facts))


def detect(text: Any) -> PersonaTraits:
    """从人设正文里读出生活特质。纯函数，任何脏输入都退化成「没有特质」。"""
    try:
        body = str(text or "")
    except Exception:
        return PersonaTraits()
    if not body.strip():
        return PersonaTraits()

    # 勤快与懒散要按命中词数比较，所以单独留计数。
    diligent_hits = _hits(body, _DILIGENT_WORDS)
    lazy_hits = _hits(body, _LAZY_WORDS)
    traits = PersonaTraits(
        night_owl=_hits(body, _NIGHT_OWL_WORDS) > 0,
        early_bird=_hits(body, _EARLY_BIRD_WORDS) > 0,
        diligent=diligent_hits > 0,
        lazy=lazy_hits > 0,
        homebody=_hits(body, _HOMEBODY_WORDS) > 0,
        outgoing=_hits(body, _OUTGOING_WORDS) > 0,
        shift_worker=_contains(body, "夜班") or _contains(body, "倒班"),
        student=_hits(body, _STUDENT_WORDS) > 0,
        worker=_hits(body, _WORKER_WORDS) > 0,
    )
    return replace(traits, hints=_hints(traits, lazy_hits, diligent_hits))


def trait_hints(traits: PersonaTraits | None) -> tuple[str, ...]:
    """取一份特质对应的 prompt 线索；`None` 或坏对象返回空元组。"""
    if traits is None:
        return ()
    try:
        return tuple(traits.hints)
    except Exception:
        return ()
