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
_LAZY_WORDS = ("懒", "懒散", "摸鱼", "拖延", "躺平", "宅", "不爱动", "随性", "拖延症")
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
    """把特质翻成给模型看的中文线索。

    同一条线索只说一次；互相矛盾的组合只留一条（勤快 × 懒散按命中词数取更明确的，
    打平算勤快——「工作狂/自律」这类多字词比单字「懒」更像在描述她的日常）。
    """
    hints: list[str] = []
    if traits.night_owl:
        # 用户原话：「日程不包括凌晨，夜猫子凌晨活动被砍」。这条线索就是那件事的解法：
        # 把 00:00-06:00 从「天然睡觉时段」里摘出来，但保留早起型凌晨睡觉的正常情况。
        hints.append("夜猫子：凌晨更清醒，白天补觉，别把 00:00-06:00 默认当睡觉时段。")
    elif traits.early_bird:
        hints.append("早起型：清晨清醒，晚上容易困，凌晨不是她的活动时间。")
    if traits.shift_worker:
        hints.append("倒班/夜班：作息不按常见的白天走，睡眠常常在白天补。")
    # 勤快与懒散互斥：只给一条，否则 prompt 自己打架。
    prefer_lazy = traits.lazy and (not traits.diligent or lazy_hits > diligent_hits)
    if prefer_lazy:
        hints.append("不算勤快：允许赖床、拖延、发呆、躺一天，别把一天排满。")
    elif traits.diligent:
        hints.append("勤快自律：可以安排工作/学习，但别排成打卡表。")
    if traits.homebody:
        hints.append("偏宅：更常待在家里或附近，出门少。")
    elif traits.outgoing:
        hints.append("外向爱出门：更常出门见人、去热闹的地方。")
    if traits.student:
        hints.append("学生：上课、自习、作业是真实的生活内容，但别排成一张课程表。")
    if traits.worker:
        hints.append("上班族：有工作和通勤，但别排成打卡表。")
    return tuple(hints)


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
