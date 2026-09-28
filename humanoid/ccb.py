"""ccb —— 一个默认关闭的独立开关，打开之后体验会明显不一样。

设计上的几条硬规矩：

1. **绝对高阈值。** `libido` 必须到 38/50（76%），而且是**涨上来的**（相对基线 +4），
   不是初始值就高。
2. **确定性触发。** 条件全中就出，不掷骰子——到了该触发的时候真的会触发。
3. **群里永远不出。** 这条不参与打分，是硬闸。
4. **只对女角色。** 从人设正文判，判不出来按「女」算（整个 Core 的措辞都写死了「她」）。
5. **她睡着没关系**——只要还有精力。精力见底就不硬来。
6. **可以当天重复，但冷却很短**：门槛已经很高了，再等 20 小时是双重收费。
7. **涨上来就激烈，降下去就没了**。强度直接跟着 `libido` 走——它掉了，这一维自然
   就不再竞争，不需要额外的计时器。

强度分三档，措辞是**感受**不是描述（不写器官结构、不写情节、不第三人称）：
具体怎么写、写到哪一步，由人设和模型决定，这个模块只给「此刻到哪一步」。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

# 绝对高阈值：45/50。下面的「涨了多少」是防止初始值被设高之后天天满足。
CCB_LIBIDO_MIN = 38.0
# 涨了多少才算「真的涨上来」。
#
# 8 太松：初始值被运营设成 40（`mood_initial_libido` 可配）的话，亲近欲在 45~48 之间
# 晃一晃就够到了，那不是「涨上来」，那是从一开始就高。收到 10：
#   · 基线是默认的 34 → 要到 45（绝对值那条已经卡着）
#   · 基线被设成 40   → 要到 50，而那已经是满格
# 于是「初始值高」这条路基本走不通，只有真的聊上来了才会触发。
# 涨了多少才算「真的涨上来」。
#
# 绝对值降到 38 之后，涨幅必须同步降——两条是**与**的关系，涨的要求比绝对值还高的话
# 就自相矛盾了（默认基线 34，涨到 38 只 +4，卡在 +8 上永远过不了）。
#
# 涨幅必须**跟绝对值配套**——两条是与的关系，涨的要求比绝对值还高就自相矛盾
# （默认基线 34 时，涨到 38 只 +4，卡在 +8 上永远过不了那条绝对值线）。
#
# 取 4 的理由：绝对值 38 配涨幅 4，等于「基线之上再涨 4 点」。
#   · 基线是默认的 34 → 38 刚好够到，**正常聊得到就会触发**
#   · 基线被运营设成 40   → 要涨到 44。初始值高的人不会天天碰到——
#     这条挡的是「从一开始就高」，不是「涨了一点就够」。
CCB_LIBIDO_RISE = 4.0
CCB_AFFECTION_MIN = 78.0
CCB_AFFECTION_RISE = 12.0
# 有精力就行——睡不睡不管（用户明确说了），但见底了不硬来
CCB_ENERGY_MIN = 30.0
# 门槛已经很高，冷却就该短：当天可以再来一次，只要条件仍然全中
CCB_COOLDOWN_SECONDS = 5 * 3600.0

STATE_KEYS = ("ccb_stage", "ccb_last_at", "ccb_peak", "ccb_turns")

# 三档强度。措辞只说「此刻的感受」，越往上越近，但**不越过感受**去写具体情节。
# 具体留给人设和模型。
# 三档的**下限必须和触发阈值 38 对齐**。
#
# 原来档位从 45 起、触发也在 45，两者是配套的。触发降到 38 而档位没动的话，
# 38~44 这一段能过门槛、却没有任何一档匹配得到——结果就是「到了该触发的时候他不触发」，
# 比原来还糟。所以这里按 38 / 43 / 48 重排。
_BANDS = (
    (38.0, 0.80, ("有点想离你近一点", "今天特别想挨着你")),
    (43.0, 0.92, ("想让你抱着我", "不太想一个人待着")),
    (48.0, 1.00, ("今天不想一个人", "很想让你再靠近一点")),
)

# 从人设正文判性别。她/他在中文里足够明显；判不出来按女（Core 通篇是「她」）。
_FEMALE = re.compile(r"她|少女|女孩|女子|女王|公主|姐姐|妹妹|夫人|小姐")
_MALE = re.compile(r"他|少年|男孩|男子|国王|王子|哥哥|弟弟|先生")


def looks_female(persona_prompt: str) -> bool:
    """从人设正文判是不是女角色。判不出来一律当女。"""
    head = str(persona_prompt or "")[:400]
    if not head:
        return True
    female = len(_FEMALE.findall(head))
    male = len(_MALE.findall(head))
    if male > female:
        return False
    return True


def read_state(user_state: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for key in STATE_KEYS:
        try:
            out[key] = float(user_state.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            out[key] = 0.0
    return out


def reset_state(user_state: Dict[str, Any]) -> None:
    for key in STATE_KEYS:
        user_state[key] = 0.0


def _band_for(libido: float) -> Optional[Tuple[float, Tuple[str, ...]]]:
    picked = None
    for floor, score, words in _BANDS:
        if libido >= floor:
            picked = (score, words)
    return picked


def evaluate(
    *,
    enabled: bool,
    is_group: bool,
    persona_prompt: str,
    affection: float,
    base_affection: float,
    libido: float,
    base_libido: float,
    energy: float,
    last_at: float,
    now: float,
    index: int = 0,
) -> Optional[Tuple[float, str, int]]:
    """条件全中就返回 `(显著度, 内心话, 阶段)`；任何一条不满足都返回 None。

    群里是**硬闸**：直接 None，不参与后面的竞争——按语气说的话没法在群里收场。
    """
    if not enabled:
        return None
    if is_group:
        return None
    if not looks_female(persona_prompt):
        return None
    if libido < CCB_LIBIDO_MIN:
        return None
    if (libido - base_libido) < CCB_LIBIDO_RISE:
        return None
    if affection < CCB_AFFECTION_MIN:
        return None
    if (affection - base_affection) < CCB_AFFECTION_RISE:
        return None
    if energy < CCB_ENERGY_MIN:
        return None
    # 冷却很短（门槛已经很高），但同一条连着两轮也是「刚说过」
    if last_at > 0 and (now - last_at) < CCB_COOLDOWN_SECONDS:
        return None

    band = _band_for(libido)
    if not band:
        return None
    score, words = band
    stage = 1 if libido < 43.0 else (2 if libido < 48.0 else 3)
    return score, words[index % len(words)], stage
