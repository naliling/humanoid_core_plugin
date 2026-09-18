"""措辞层：把身体与处境的数值翻成一句人话，并且**同一状态不必每次同一句**。

插件里所有要进上下文的话都从这里过一道，理由很直接：把「眼皮很沉，句子不想写长」这种
整句硬编码在 soma 里，她每天每条消息都在念同一份稿子，那就是预制菜。这里做两件事：

1. **程度由数值生成**：`scale_word` 拿轴值挑档位词（「有点困」/「困得发沉」/「眼皮撑不住」），
   所以程度是真的从身体算出来的，不是查表查出来的。
2. **同一档位多套说法按天抽签**：`pick` 的种子是（角色, 日期, 这句话的用途, 相关状态），
   同一天里她不会把同一句话说两遍，不同角色、不同天说的是另一套；但同一个请求内是稳定的
   （否则模型会看到她上一秒说困、下一秒换个说法说困，以为那是两个人格）。

这里**不写台词也不写规矩**：只描述状态（她困、她饿、她把注意力放在哪儿），
说什么、说多长、要不要接，都不在这个文件的职权范围内。
"""

from __future__ import annotations

import hashlib
from typing import Sequence

# 抽签用的固定前缀：同一个 key 在别的模块里不会撞车。
_SEED_NAMESPACE = "humanoid_wording_v1"


def pick(key: str, seed_parts: Sequence[object], options: Sequence[str]) -> str:
    """按 (key, 种子) 稳定地抽一条说法。options 为空时返回空串。

    种子必须包含「今天」而不是「这一秒」：她说「有点饿了」之后，一小时内再渲染一次不该
    突然换成「肚子空得难受」——那是同一次感受的两种写法，模型会当成两件事。
    """
    if not options:
        return ""
    if len(options) == 1:
        return options[0]
    raw = "|".join([_SEED_NAMESPACE, key, *[str(part) for part in seed_parts]])
    digest = hashlib.blake2b(raw.encode("utf-8"), digest_size=8).digest()
    return options[int.from_bytes(digest, "big") % len(options)]


def scale_word(value: float, ladder: Sequence[tuple[float, str]]) -> str:
    """按轴值挑一个档位词。ladder 是 (下界, 词) 从大到小或任意顺序都行。"""
    best: tuple[float, str] | None = None
    for floor, word in ladder:
        if value >= floor and (best is None or floor > best[0]):
            best = (floor, word)
    return best[1] if best else ""


# 程度词表：每档给几条等价说法，抽哪条由 pick 决定。都是状态描述，没有一条在告诉她
# 该说什么、该说多长。
FEELING_WORDS: dict[str, list[tuple[float, tuple[str, ...]]]] = {
    "sleepy": [
        (88, ("很困", "困得不行")),
        (72, ("有些困意", "略显疲惫")),
        (55, ("略微犯困",)),
        (0, ("精神尚可",)),
    ],
    "debt": [
        (4.0, ("睡眠不足", "欠着觉")),
        (1.5, ("休息不够",)),
        (0, ("休息充足",)),
    ],
    "hunger": [
        (88, ("很饿", "饿得发慌")),
        (60, ("有些饿",)),
        (0, ("不饿",)),
    ],
    "discomfort": [
        (62, ("身体不适",)),
        (38, ("略有不适",)),
        (0, ("身体无碍",)),
    ],
    "arousal": [
        (78, ("精力充沛",)),
        (22, ("精力不足",)),
        (0, ("状态平稳",)),
    ],
    "social_desire": [
        (70, ("有交流意愿",)),
        (35, ("交流意愿一般",)),
        (0, ("倾向独处",)),
    ],
}

# 关系档位：从「在意度」挑词。同样是状态，不是「你要对TA热情」。
CARE_WORDS = (
    (0.80, ("关系亲密", "十分在意")),
    (0.62, ("较为熟悉", "相处自然")),
    (0.45, ("关系普通", "正常交往")),
    (0.28, ("较为生疏", "保持客气")),
    (0.00, ("关系疏远", "缺乏交集")),
)



# 隔了多久之后，她这期间在干什么——同一件事的几种说法，避免每次都是「距上次说话 X」。
SINCE_WORDS = (
    "TA上一条消息是 {gap}前发的",
    "离TA上次说话过去 {gap}了",
    "TA有 {gap}没吭声了",
    "上一次听TA说话是 {gap}前",
)
