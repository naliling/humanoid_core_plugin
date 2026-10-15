"""ccb —— 一个默认关闭的独立开关，打开之后体验会明显不一样。

设计上的几条硬规矩：

1. **绝对高阈值。** `libido` 必须到 38/50（76%），而且是**涨上来的**（相对基线 +4），
   不是初始值就高。
2. **确定性触发。** 条件全中就出，不掷骰子——到了该触发的时候真的会触发。
3. **群里永远不出。** 这条不参与打分，是硬闸。
4. **只对女角色。** 从人设正文判，判不出来按「女」算（整个 Core 的措辞都写死了「她」）。
5. **她睡着没关系**——只要还有精力。精力见底就不硬来。
6. **当天可以重复**，但一天封顶 8 次。
7. **涨上来就激烈，降下去就没了**。强度直接跟着 `libido` 走——它掉了，这一维自然
   就不再竞争，不需要额外的计时器。

v2.24.1 改了两件事，都是因为原来那套「达标就一直这样」不成立：

- **它是场景里的状态，不是一次性事件。** `ccb_satisfy` 在场景里累积，到顶算收场，
  按天回落。推进它的是**轮数**不是时间——做爱不是 30 分钟的事，给它配一个分钟级
  的时限是拿「害羞」的逻辑套错地方。
- **收场要付代价。** 到顶那一次扣一次精力，否则她一天进出八次还是满血。
  原来的 `ccb_peak` / `ccb_turns` 是死状态（只写不读），`ccb_turns` 现在接成当日次数。

强度分三档，措辞是**感受**不是描述（不写器官结构、不写情节、不第三人称）：
具体怎么写、写到哪一步，由人设和模型决定，这个模块只给「此刻到哪一步」。
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Dict, Optional, Tuple

from .persona import PERSONA_PROMPT_MAX

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
# E1 三档（v2.27.8）：<50 拒绝/岔开；50~74 擦边暧昧；≥75 深入。
# 原来 78/12 对多数人是永远够不着的静默开关。
CCB_AFFECTION_MIN = 50.0
CCB_AFFECTION_RISE = 8.0
CCB_AFFECTION_DEEP = 75.0
# 有精力就行——睡不睡不管（用户明确说了），但见底了不硬来
CCB_ENERGY_MIN = 30.0
# 门槛已经很高，冷却就该短：当天可以再来一次，只要条件仍然全中
CCB_COOLDOWN_SECONDS = 5 * 3600.0

# ccb_done_count：一辈子的次数（不分天）。社交层拿它判断「有没有和对方色色过」，
# 从而决定要不要主动问一句——它不是当天次数（那是 ccb_turns）。
STATE_KEYS = ("ccb_stage", "ccb_last_at", "ccb_satisfy", "ccb_day", "ccb_turns", "ccb_done_count")

# 满足：场景里累积，到顶算「收场」。
#
# 这是一根**状态**不是一根情绪：它只在场景内累积，场景结束后回落，但回落得比
# libido 慢——所以第二天她还记得昨晚发生过什么，只是不会主动拿出来讲。
# 不能做成 mood 的第四根轴：那样「她今天很满足」会变成常态语气，露馅。
#
# 每次进场景加多少：40 左右能凑三次到顶，也就是一次完整的场景。
CCB_SATISFY_STEP = 40.0
# 到顶了。这一档是「她满意了、到这里正好」——不是「没劲了」。
CCB_SATISFY_FULL = 100.0
# 每天回落多少。比 libido 慢：第二天还在，但不强烈。
CCB_SATISFY_DECAY_PER_DAY = 12.0
# 一天最多进几次。门槛已经很高，但条件全中时它会一直亮着，不封顶就能刷。
CCB_DAILY_MAX = 8
# 收场时扣的精力。和「出门办事」同量级——一次性扣，不是过程中持续掉。
# 持续掉更真实，但会让体力曲线变得难预测，而且累到一定程度又会被精力闸拦住，
# 变成「刚完就说不下去」这种反直觉的结果。
CCB_ENERGY_COST = 12.0

# 三档强度。措辞只说「此刻的感受」，越往上越近，但**不越过感受**去写具体情节。
# 具体留给人设和模型。
# 三档的**下限必须和触发阈值 38 对齐**。
#
# 原来档位从 45 起、触发也在 45，两者是配套的。触发降到 38 而档位没动的话，
# 38~44 这一段能过门槛、却没有任何一档匹配得到——结果就是「到了该触发的时候他不触发」，
# 比原来还糟。所以这里按 38 / 43 / 48 重排。
_BAND_MIN_SCORE = 0.80
# 三档的措辞。强度（score）由档数**线性推导**，不再和 floor 并排写死：
# floor 是「什么时候能触发」（由 `bands()` 从 libido_min 推），
# score 是「这一档有多强」（喂给 emotion 去和别的情绪池比大小）——两者不同源，
# 写在同一行里会让人以为是同一个刻度。
_BAND_WORD_TEXTS = (
    ("有点想离你近一点", "今天特别想挨着你"),
    ("想让你抱着我", "不太想一个人待着"),
    ("今天不想一个人", "很想让你再靠近一点"),
)
_BAND_WORDS = tuple(
    (
        _BAND_MIN_SCORE + (1.0 - _BAND_MIN_SCORE) * index / max(1, len(_BAND_WORD_TEXTS) - 1),
        words,
    )
    for index, words in enumerate(_BAND_WORD_TEXTS)
)
# 档位之间的间隔。原来写死 38/43/48，间隔 5；这里跟着走，免得以后改间距要改两处。
_BAND_STEP = 5.0


def bands(libido_min: float) -> Tuple[Tuple[float, float, Tuple[str, ...]], ...]:
    """档位由**触发阈值**推导，不写死。

    这条是踩出来的：原来触发阈值和档位下限是两处独立的数字。触发降到 38 而档位
    还在 45 的时候，38~44 这一段**能过门槛却匹配不到任何一档**——结果就是
    「到了该触发的时候他不触发」，比原来还糟，而且从外面完全看不出是哪一步坏的。

    推导之后：阈值降到 30，档位就是 30/35/40，怎么调都不会出缝。
    """
    base = max(0.0, float(libido_min))
    return tuple(
        (base + i * _BAND_STEP, score, words)
        for i, (score, words) in enumerate(_BAND_WORDS)
    )

# 从人设正文判性别。她/他在中文里足够明显；判不出来按女（Core 通篇是「她」）。
# `他` 必须排除构词成分：`其他` / `他人` 里的那个字不是性别信号——
# 实测裸 `他` 命中「其他」会把女角色判成男，而 ccb 只对女角色开，判错就是静默永不触发。
_FEMALE = re.compile(r"她|少女|女孩|女子|女王|公主|姐姐|妹妹|夫人|小姐")
_MALE = re.compile(r"(?<!其)他(?!人)|少年|男孩|男子|国王|王子|哥哥|弟弟|先生")


def looks_female(persona_prompt: str) -> bool:
    """从人设正文判是不是女角色。判不出来一律当女。

    扫描上限跟人设注入上限（`persona.PERSONA_PROMPT_MAX`）一致，而不是只看前 400 字：
    女性身份写在第 500 字时，原来会判成男 → ccb 静默永不触发。
    """
    body = str(persona_prompt or "")[:PERSONA_PROMPT_MAX]
    if not body.strip():
        return True
    female = len(_FEMALE.findall(body))
    male = len(_MALE.findall(body))
    if male > female:
        return False
    return True


def _local_utc_offset(now: float) -> float:
    """宿主本地时区相对 UTC 的偏移（秒）。"""
    local = datetime.datetime.fromtimestamp(now)
    utc = datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc).replace(tzinfo=None)
    return (local - utc).total_seconds()


def _day_index(now: float) -> int:
    """自然日序号。按**宿主本地日**算，与 llm 账本的宿主日同源。

    原来直接 `int(now // 86400)`（UTC 天）：角色的一天按她的本地时区算、llm 账本按
    宿主本地日算（`llm._roll_day` 用 `time.localtime`），这里却按 UTC 算——三方在跨日
    时刻各差几个小时，「当天最多 8 次」会在错误的时刻重置。

    做法是把时间戳先按本地 UTC 偏移平移、再取整天数：仍然是「第几天」的整数，
    调用方直接相减的用法（`decay_satisfy` 的 `int(today - last)`）不受影响。
    """
    try:
        offset = _local_utc_offset(now)
    except (OverflowError, OSError, ValueError):
        # 时间戳超出可表示范围（坏数据）：退回旧口径，至少保证「跨天」这件事可靠。
        offset = 0.0
    return int((now + offset) // 86400.0)


def read_state(user_state: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for key in STATE_KEYS:
        try:
            out[key] = float(user_state.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            out[key] = 0.0
    return out


def reset_scene(user_state: Dict[str, Any]) -> None:
    """场景结束：只清「此刻到哪一步」，**不动记录**。

    `ccb_last_at` / `ccb_turns` / `ccb_done_count` 是历史（冷却时间、当日次数、
    一辈子的次数）。场景结束不是它们的生命周期终点——一起清掉会让冷却立刻失效
    （下一轮又能触发），累计次数也归零，社交层再也判断不出「有没有和对方色色过」。
    """
    user_state["ccb_stage"] = 0.0
    user_state["ccb_satisfy"] = 0.0


def decay_satisfy(user_state: Dict[str, Any], now: float) -> None:
    """按天回落。跨了多少天就落多少次，累加在这一次里做。"""
    today = _day_index(now)
    if "ccb_day" not in user_state:
        user_state["ccb_day"] = float(today)
        return
    try:
        last = float(user_state.get("ccb_day", 0.0) or 0.0)
    except (TypeError, ValueError):
        last = 0.0
    days = int(today - last)
    if days <= 0:
        return
    try:
        cur = float(user_state.get("ccb_satisfy", 0.0) or 0.0)
    except (TypeError, ValueError):
        cur = 0.0
    user_state["ccb_satisfy"] = max(0.0, cur - CCB_SATISFY_DECAY_PER_DAY * days)
    user_state["ccb_day"] = float(today)
    # 跨天时次数重新算——不然「一天最多 8 次」会变成「一辈子 8 次」。
    if int(user_state.get("ccb_turns", 0.0) or 0.0) > 0:
        user_state["ccb_turns"] = 0.0


def advance_satisfy(user_state: Dict[str, Any], now: float) -> bool:
    """场景推进一格。**返回 True 表示这一次刚好到顶（收场）**。

    推进的是**轮数**而不是时间：做爱不是 30 分钟的事，给它配一个计时器是拿
    「害羞」的逻辑套错地方。这里每次进场景累积一格，到顶就是一次收场。
    """
    decay_satisfy(user_state, now)
    try:
        cur = float(user_state.get("ccb_satisfy", 0.0) or 0.0)
    except (TypeError, ValueError):
        cur = 0.0
    nxt = min(CCB_SATISFY_FULL, cur + CCB_SATISFY_STEP)
    user_state["ccb_satisfy"] = nxt
    return cur < CCB_SATISFY_FULL and nxt >= CCB_SATISFY_FULL


def _band_for(libido: float, libido_min: float) -> Optional[Tuple[float, Tuple[str, ...]]]:
    picked = None
    for floor, score, words in bands(libido_min):
        if libido >= floor:
            picked = (score, words)
    return picked


def _values_hold(
    *,
    affection: float,
    base_affection: float,
    libido: float,
    base_libido: float,
    libido_min: float = CCB_LIBIDO_MIN,
    libido_rise: float = CCB_LIBIDO_RISE,
    affection_min: float = CCB_AFFECTION_MIN,
    affection_rise: float = CCB_AFFECTION_RISE,
) -> bool:
    """只有**数值门槛**这一组——冷却/当日封顶/精力/群聊都不在这里。

    拆出来是给 `_qualifies`（能不能进）与 `scene_lapsed`（结没结束）共用，
    门槛只此一处定义，改阈值不会两边走歪。
    """
    if libido < float(libido_min):
        return False
    if (libido - base_libido) < float(libido_rise):
        return False
    if affection < float(affection_min):
        return False
    if (affection - base_affection) < float(affection_rise):
        return False
    return True


def _qualifies(
    *,
    is_group: bool,
    persona_prompt: str,
    affection: float,
    base_affection: float,
    libido: float,
    base_libido: float,
    libido_min: float = CCB_LIBIDO_MIN,
    libido_rise: float = CCB_LIBIDO_RISE,
    affection_min: float = CCB_AFFECTION_MIN,
    affection_rise: float = CCB_AFFECTION_RISE,
    coerced: bool = False,
) -> bool:
    """「该不该进入场景」的门槛——上下文闸 + 数值门槛，冷却/当日封顶/精力**不在**。"""
    if coerced or is_group:
        return False
    if not looks_female(persona_prompt):
        return False
    return _values_hold(
        affection=affection,
        base_affection=base_affection,
        libido=libido,
        base_libido=base_libido,
        libido_min=libido_min,
        libido_rise=libido_rise,
        affection_min=affection_min,
        affection_rise=affection_rise,
    )


def scene_lapsed(
    *,
    is_group: bool,
    persona_prompt: str,
    affection: float,
    base_affection: float,
    libido: float,
    base_libido: float,
    libido_min: float = CCB_LIBIDO_MIN,
    libido_rise: float = CCB_LIBIDO_RISE,
    affection_min: float = CCB_AFFECTION_MIN,
    affection_rise: float = CCB_AFFECTION_RISE,
    coerced: bool = False,
) -> bool:
    """场景是否**真的结束**（色欲/好感掉下数值门槛）。

    群聊 / 刚被强迫 / 判不出性别——这些是**上下文**，不是「结束」：她这会儿
    进不去，但场景还在，不该清。冷却、当日已达上限、精力见底同样不算结束。
    """
    if coerced or is_group:
        return False
    if not looks_female(persona_prompt):
        return False
    return not _values_hold(
        affection=affection,
        base_affection=base_affection,
        libido=libido,
        base_libido=base_libido,
        libido_min=libido_min,
        libido_rise=libido_rise,
        affection_min=affection_min,
        affection_rise=affection_rise,
    )


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
    turns_today: float = 0.0,
    libido_min: float = CCB_LIBIDO_MIN,
    libido_rise: float = CCB_LIBIDO_RISE,
    affection_min: float = CCB_AFFECTION_MIN,
    affection_rise: float = CCB_AFFECTION_RISE,
    affection_deep: float = CCB_AFFECTION_DEEP,
    coerced: bool = False,
    index: int = 0,
) -> Optional[Tuple[float, str, int]]:
    """条件全中就返回 `(显著度, 内心话, 阶段)`；任何一条不满足都返回 None。

    群里是**硬闸**：直接 None，不参与后面的竞争——按语气说的话没法在群里收场。
    """
    if not enabled:
        return None
    # E4/E6：刚被强迫过（无视拒绝/胁迫）→ 不进入，谈不上「享受」
    if not _qualifies(
        coerced=coerced,
        is_group=is_group,
        persona_prompt=persona_prompt,
        affection=affection,
        base_affection=base_affection,
        libido=libido,
        base_libido=base_libido,
        libido_min=libido_min,
        libido_rise=libido_rise,
        affection_min=affection_min,
        affection_rise=affection_rise,
    ):
        return None
    if energy < CCB_ENERGY_MIN:
        return None
    # 冷却很短（门槛已经很高），但同一条连着两轮也是「刚说过」
    if last_at > 0 and (now - last_at) < CCB_COOLDOWN_SECONDS:
        return None
    # 一天封顶。门槛全中时它会一直亮着，不封顶就能一直刷下去。
    if turns_today >= CCB_DAILY_MAX:
        return None

    band = _band_for(libido, float(libido_min))
    if not band:
        return None
    score, words = band
    # 阶段跟着档位走，同样从阈值推导——写死 43/48 的话阈值一改就又出缝。
    ladder = bands(float(libido_min))
    stage = 1 + sum(1 for floor, _s, _w in ladder[1:] if libido >= floor)
    # E1：好感没到「深入」那一档时，只给最浅一档（擦边暧昧），不往深走。
    if affection < float(affection_deep):
        stage = 1
        words = ladder[0][2]
    return score, words[index % len(words)], stage
