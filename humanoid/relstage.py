"""关系阶段：好感是个连续的数，但它**不是连续地改变行为**的。

好感 60 和好感 90 现在看起来一样——措辞、主动性、能聊到哪，都没区别。
真人不是这样：熟到一定程度才会开 certain 的玩笑、才会在对方不说话的时候
直接问「你怎么了」而不是「在吗」。

所以从好感算出一个**阶段**，只做几件事：给注入一句「现在到什么程度了」、
给社交层一个能查的字段。**不新增状态**——它是算出来的，掉回基线它自己就回去了。
"""

from __future__ import annotations

from typing import Tuple

# 门槛按「这段关系真的到了哪一步」定，不是等距切分：
# 70 以下还是「认识的人」，70 才是「能说点私事」，85 才是「熟到可以直说」。
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


def stage_of(affection: float) -> Tuple[int, str, str]:
    """返回 `(档位序号, 短标签, 给模型看的一句话)`。"""
    try:
        value = float(affection)
    except (TypeError, ValueError):
        value = 0.0
    picked = BANDS[0]
    for band in BANDS:
        if value >= band[0]:
            picked = band
    return BANDS.index(picked), picked[1], picked[2]


def line_for(affection: float) -> str:
    """注入里那一行。只说状态，不对模型下指令。"""
    _idx, _short, line = stage_of(affection)
    return line
