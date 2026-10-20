"""关系阶段：好感是个连续的数，但它**不是连续地改变行为**的。

好感 60 和好感 90 现在看起来一样——措辞、主动性、能聊到哪，都没区别。
真人不是这样：熟到一定程度才会开 certain 的玩笑、才会在对方不说话的时候
直接问「你怎么了」而不是「在吗」。

所以从好感算出一个**阶段**，只做几件事：给注入一句「现在到什么程度了」、
给社交层一个能查的字段。**不新增状态**——它是算出来的，掉回基线它自己就回去了。

## v2.27.43：档位不能只看好感

线上实测：2485 个用户里 **98.4% 落在「还在熟悉」**，其中 179 人已经聊过 30 轮以上，
最多的 `491815361` 聊了 **2088 轮**，好感 39.9 —— 于是注入里永远写「她和TA还在
互相认识，没到能说私事的地步」。模型照这个演，就是一个永远不熟的陌生人。

根因不是好感算错，是**这个档位把两件事混成了一件**：

* **好感** = 情感上的亲近（会不会袒护、会不会吃醋）；
* **熟悉** = 一起待了多久、聊过多少（该不该绕弯子、要不要从头客气）。

一个人跟你聊了两千轮，就算好感只有 40，「还在互相认识」也是**事实错误**。
所以档位取两条路径里更高的那条：好感够了按好感，相处够久按相处。
「很近」（≥88）仍然只认好感——那是亲密，不是资历。
"""

from __future__ import annotations

from typing import Tuple

# 门槛按「这段关系真的到了哪一步」定，不是等距切分：
# 四档——0 起「还在熟悉」，55 起「能说私事」，75 起「熟到可以直说」，88 起「很近」。
#
# 文案有两条纪律（v2.27.2）：
# 1. **人称统一**：用「她/TA」，不用「你/你们」——注入块通篇是第三人称，「你」在
#    system_prompt 里指角色自己，两句混着写让人称指代乱掉。
# 2. **只说状态，不说行为约束**：「说什么都还收着」像是在告诉她「别多说」，
#    改成纯状态描述（「还没到能说私事的地步」）。
BANDS = (
    (0.0, "还在熟悉", "她和TA还在互相认识，没到能说私事的地步"),
    (55.0, "能说私事", "她愿意跟TA讲点自己的事了"),
    (75.0, "熟到可以直说", "她跟TA不用绕弯子，不开心会直接讲"),
    (88.0, "很近", "TA在她这儿几乎没有边界"),
)

# 相处（熟悉度）能顶到的最高档。88 那一档不开放给相处：亲不亲密是感情的事，
# 一起待得再久也不等于「几乎没有边界」。
_FAMILIARITY_CEILING = 2

# (最少聊过多少轮, 最少认识多少天, 顶到第几档)
FAMILIARITY_BANDS = (
    (60, 7.0, 1),      # 聊过 60 轮、认识一周：不该再是「还在互相认识」
    (400, 30.0, 2),    # 聊过 400 轮、认识一个月：说话不用绕弯子
)


def familiarity_floor(turns: int = 0, days: float = 0.0) -> int:
    """按相处时长算出的**最低**档位（0 表示没有任何加成）。

    两个条件必须同时满足：只聊得多但认识一天（一口气刷 300 条）不算，
    只认识很久但没说过几句话也不算。
    """
    try:
        t = int(turns)
        d = float(days)
    except (TypeError, ValueError):
        return 0
    if t < 0 or d != d:          # 负数 / NaN：不参与判定
        return 0
    floor = 0
    for need_turns, need_days, band in FAMILIARITY_BANDS:
        if t >= need_turns and d >= need_days:
            floor = min(band, _FAMILIARITY_CEILING)
    return floor


def stage_of(
    affection: float,
    turns: int = 0,
    days: float = 0.0,
) -> Tuple[int, str, str]:
    """返回 `(档位序号, 短标签, 给模型看的一句话)`。

    `turns` / `days` 是「相处」那一半的证据，缺省为 0 —— 旧调用方（只传好感）
    拿到的结果与改前逐字一致。
    """
    try:
        value = float(affection)
    except (TypeError, ValueError):
        value = 0.0
    picked = BANDS[0]
    for band in BANDS:
        if value >= band[0]:
            picked = band
    index = BANDS.index(picked)
    floor = familiarity_floor(turns, days)
    if floor > index:
        picked = BANDS[floor]
        index = floor
    return index, picked[1], picked[2]


def line_for(affection: float, turns: int = 0, days: float = 0.0) -> str:
    """注入里那一行。只说状态，不对模型下指令。"""
    _idx, _short, line = stage_of(affection, turns, days)
    return line
