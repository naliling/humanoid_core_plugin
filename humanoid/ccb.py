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
import math
import re
from typing import Any, Dict, List, Optional, Tuple

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
# 情绪层一天最多能给亲近欲加多少（与 `services.mood.DAILY_LIBIDO_GAIN_CAP` 同源）。
# 见 `reachable_libido_min`：绝对门槛高于「基线 + 这个量」时，那条线**一辈子够不着**，
# 这个开关就永远不会打开。线上实测（2026-10-26）8562 个用户条目里没有一个人进过场景。
# v2.28.14：4.5 → 6.0，与 services.mood.DAILY_LIBIDO_GAIN_CAP 一起抬（亲近欲难涨的反馈）。
LIBIDO_DAILY_REACH = 6.0
# 亲近欲量程上限（与 `services.mood.LIBIDO_RANGE` 的右端一致）。只用于判断
# 「已经顶到量程、不可能再涨」这一种饱和态。
LIBIDO_MAX = 50.0
# 攻击性量程上限（与 `services.mood.AGGRESSION_RANGE` 的右端一致）。ccb 的结算函数
# 以前把上限写成 100（审核 P2-23 同类问题）——写回档案后状态页会出现越界数值。
AGGRESSION_MAX = 50.0


def reachable_libido_min(libido_min: float, base_libido: float) -> float:
    """把绝对门槛夹到「她这具身体真能到的高度」。

    设计里 38/50 配的是默认基线 34：34 + 每日可涨 6 = 40，本来就够得到，不夹。
    面板把初始亲近欲调低（例如 30）时才够不着：30 + 6 = 36 < 38，这时绝对门槛
    夹到 36；再低（20 + 6 = 26）同理——不夹的话功能看起来就是坏的（不报错、
    只是从来不动）。

    这里只**夹低**、不抬高：配置本来就被设得比可达上限低时原样保留；只有设得
    够不着时才按可达上限算，并在 `scene_status`（/亲密状态）里如实显示这个数。
    与 `libido_rise` 配合：夹完等于「基线 + 6」，涨 4 的要求同样只有到顶才满足，
    仍然是「真的到了今天这一档」才进，不是随便开。
    """
    try:
        low = float(libido_min)
        base = float(base_libido)
    except (TypeError, ValueError):
        return float(libido_min) if libido_min is not None else CCB_LIBIDO_MIN
    if not (math.isfinite(low) and math.isfinite(base)):
        return low
    reach = base + LIBIDO_DAILY_REACH
    return low if reach >= low else reach
# E1 三档（v2.27.8）：<50 拒绝/岔开；50~74 擦边暧昧；≥75 深入。
# 原来 78/12 对多数人是永远够不着的静默开关。
CCB_AFFECTION_MIN = 50.0
CCB_AFFECTION_RISE = 8.0
CCB_AFFECTION_DEEP = 75.0
# 有精力就行——睡不睡不管（用户明确说了），但见底了不硬来
CCB_ENERGY_MIN = 30.0
# 门槛已经很高，冷却就该短：当天可以再来一次，只要条件仍然全中
CCB_COOLDOWN_SECONDS = 5 * 3600.0

# ─── P3：硬闸────────────────────────────────────
#
# 这四条**不是**阈值，是"整条功能不生效"的开关。它们排在 `evaluate` 最前面，
# 比 enabled 之外的一切都早；命中就 return None，不降档、不软化、不"只做浅的"。
#
# 为什么年龄只用来判"是否成年"，绝不参与数值推导：
#   · 把年龄接进"深度上限"这类数值，等于用年龄给性能力建模——这条线不能碰；
#   · 数值只来自配置（静态档案），年龄只回答一个是非题。
#
# 误伤风险是刻意接受的：一个人设写着「14岁」的角色，宁可整个功能不生效，
# 也不做"打个折"的处理。
_MINOR_WORDS = (
    "幼女", "萝莉", "loli", "小学生", "初中生", "初中", "儿童", "未成年",
    "小学", "幼童", "女童", "童女",
)
# 明确指向**配对关系**的乱伦词。刻意不收「哥哥」「妹妹」这类单独称谓——
# 中文角色扮演里"哥哥"是极常见的爱称，收进来会把绝大多数人设一起打掉。
_INCEST_WORDS = ("亲兄妹", "亲姐弟", "亲父女", "亲母子", "父女", "母子", "乱伦", "骨科")
# 中文数字的小龄段：12~17 岁（十八及以上不在此列）
_MINOR_CN_AGE = ("十一岁", "十二岁", "十三岁", "十四岁", "十五岁", "十六岁", "十七岁")
# 数字边界两个方向都要卡（S2）：
#
# 原来写的是 `(\d{1,2})\s*(?:岁|周岁)`，实测「你是300岁的神」会从第三位匹配出
# `00岁` → 判成 0 岁 → 整条功能被拒。1200 岁、300 岁这类**奇幻人设**是常见写法，
# 误伤成本很高（用户只会看到"功能没生效"，不会知道是被年龄闸拦了）。
# `(?<!\d)` 挡左侧残留、`(?!\d)` 挡右侧续位。
_AGE_RE = re.compile(r"(?<!\d)(\d{1,2})(?!\d)\s*(?:岁|周岁)")
ADULT_AGE = 18


def safety_block(persona_prompt: str) -> str:
    """这条人设能不能开亲密场景。返回拒绝理由，空串表示允许。

    判据只有两条，都很窄——宁严勿宽：

    1. **年龄**：正文里出现 `\\d+岁` 且小于 18，或命中中文小龄段词；
       或命中幼态词（幼女/萝莉/小学生…）。**年龄只回答是否成年，不参与任何数值。**
    2. **血缘**：只认明确指向配对的词（亲兄妹/父女/母子/乱伦…），
       不收「哥哥」「妹妹」这类单独称谓。

    `persona_prompt` 为空（拿不到人设）时**放行**——这与 `looks_female` 的
    "判不出按女"同向：拿不到不等于有风险，静默关掉整个功能比放行更难排查。
    真正挡住未知情况的是 `ccb` 总开关默认关闭。
    """
    body = str(persona_prompt or "")[:PERSONA_PROMPT_MAX]
    if not body.strip():
        return ""
    for word in _INCEST_WORDS:
        if word in body:
            return f"人设命中血缘关系（{word}）"
    for word in _MINOR_WORDS:
        if word in body:
            return f"人设命中未成年标识（{word}）"
    for word in _MINOR_CN_AGE:
        if word in body:
            return f"人设命中未成年标识（{word}）"
    for match in _AGE_RE.finditer(body):
        try:
            age = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if age < ADULT_AGE:
            return f"人设年龄 {age} 岁，未成年"
    return ""


# ─── 静态身体档案（C 数据层）─────────────────────────────────────────
#
# 分两处放，**绝不合并**：
#   · 她的（深度上限 / 敏感度）→ `roles.<bid>.self` 下一个键，所有用户共享；
#   · 他的（长度）→ `users.<uid>` 下一个键，每人一份。
#
# 写进 per-user 的只有她自己的深度上限时，10 个用户就存 10 份一模一样的值；
# 反过来把"他的长度"放角色级，又会变成所有用户共用一个尺寸——两头都错。
#
# 两条纪律：
#   · **初始化一次就固定**（"不管多久以后"都按这个）；改配置不会动已经定下的档案。
#   · 深度上限是**阈值**，不是"当前有多深"；它每轮只被比较一次，不参与计算。
BODY_KEY = "body"
BODY_LENGTH_KEY = "body"

# 当前探入深度的估算比例（按阶段推，不存盘）。它不是测量值，是**判定用的代理**：
# 阶段越高，越接近全长。所以"过深"在临界/爆发期才可能出现——这与直觉一致。
# 键是阶段号：0 不在场景 / 1 动情 / 2 平台 / 3 临界 / 4 爆发 / 5 余韵。
_DEPTH_RATIO = {0: 0.0, 1: 0.60, 2: 0.85, 3: 1.0, 4: 1.0, 5: 0.0}
# 阶段名。"余韵"是到顶之后的窗口，不属于场景本身（`ccb_stage` 那时已经归零）。
PHASE_NAMES = ("", "动情", "平台", "临界", "爆发", "余韵")
# 余韵窗（秒）：到顶之后的一段时间里她还在余韵里，过了就回到日常。
CCB_AFTERCARE_SECONDS = 2 * 3600.0
# 场景闲置多久算结束（秒）。6 小时没人说话，那一场早就过去了；
# 不靠它收场，`ccb_stage` 会挂着过夜，第二天她还在"场景里"。
CCB_SCENE_IDLE_SECONDS = 6 * 3600.0

# 姿势与探入位置。取值是**互斥枚举**，`-1` 表示"还不知道"。
# 判不出来时保持上一次的值；一次都判不出来就整段不出（A 的兜底）。
POSE_UNKNOWN = -1
POSE_NAMES = ("后入", "前入", "坐姿", "躺姿")
SITE_NAMES = ("前面", "后面")

# ─── 判断层────────────────────
#
# 所以这一层按三条做，而不是简单关键词命中：
#
# 1. **双向扫描**：姿势/位置绝大多数是**她自己**在正文里写出来的
#    （"从后面""转过来"），只扫用户那句会漏掉大半。两个来源都扫。
# 2. **强词优先**：一个词只属于一种姿势时算强命中，直接采纳；
#    弱词（"后面"这种既可能指姿势也可能指位置的）只在没有歧义时才算。
# 3. **否定护栏**：命中词前面 4 个字里有"别/不要/不停"之类，判为**否定**不动。
#    不然一句"别从后面"会把姿势改成正相反的那个。
#
# 另外每个判定都返回**命中的那个词**（`*_detail`）：诊断日志里能直接看到
# "这一轮是靠哪个词判出来的"，判错了才查得动。
_POSE_WORDS = (
    ("后入", "从后面", "后入", "背后", "趴着", "趴在", "背对"),
    ("前入", "前入", "正面", "面对面", "仰面", "转过来"),
    ("坐姿", "坐上来", "跨坐", "骑在", "骑上", "坐上"),
    ("躺姿", "躺下", "平躺", "仰卧", "躺着", "躺好"),
)
# 位置词面：**必须明确指向入口**。裸的「前面」「后面」一律不收——
# 实测「从后面抱住你」里的"后面"说的是体位，不是入口，收了就会把位置判错，
# 而判错位置比不知道位置出戏得多（触感描写整套歪掉）。
_SITE_WORDS = (
    ("前面那里", "前面进", "前穴"),
    ("后穴", "后庭", "肛门", "后面那里", "后面进"),
)
# 否定词：命中词**前面**这几个字里有它，就不算数。只看前面——"别停"里的
# "停"在词后面，那是鼓励而不是否定（`detect_occurrence` 里踩过同一个坑）。
_NEG_PREFIX = ("别", "不要", "不用", "不想", "没有", "不准", "不许", "没")
# 单字否定「别」不能整词匹配：中文里「特别」「区别」「告别」「分别」「性别」「个别」
# 全都含这个字。实测「告别之后做过了」「区别在于我们做过了」都被判成否定而漏记。
# 所以对「别」单独判：**它前面那个字**是构词前缀时不算否定。
_NEG_SINGLE = "别"
_NEG_SINGLE_COMPOUND = "特区告分性个类差离辨鉴级班派秒"
_NEG_WINDOW = 4


def _negated(s: str, pos: int, words=_NEG_PREFIX, window: int = _NEG_WINDOW) -> bool:
    """命中词 `pos` 之前那一小段里有没有否定。只看**前面**。

    「别停」里的"停"在词后面，那是鼓励不是否定。

    `words` 里的「别」按单字处理（排除构词）；其余按整词匹配。
    """
    start = max(0, pos - window)
    lead = s[start: pos]
    if any(w in lead for w in words if w != _NEG_SINGLE):
        return True
    if _NEG_SINGLE in words:
        idx = lead.rfind(_NEG_SINGLE)
        if idx >= 0:
            # 用**绝对位置**取前一个字：窗口恰好从「别」开始时，相对位置拿不到
            # 前面那个字（「区别在于…」的"区"会被窗口切掉），构词判断就失效了。
            absolute = start + idx
            before = s[absolute - 1] if absolute > 0 else ""
            # 注意：`"" in "任何串"` 恒为 True（空串是任何字符串的子串），
            # 所以必须先判空——否则「别」出现在句首时会被当成构词放过。
            if not before or before not in _NEG_SINGLE_COMPOUND:
                return True
    return False

# ─── D：场景账本 ─────────────────────────────────────────────────────
#
# 为什么需要它：姿势、位置、阶段都是**会变**的，而"她记得刚才是什么姿势"
# 靠的不能是单点状态——换两次姿势之后，只留最后一个值就丢掉了过程，
# 于是她说不出"刚换过来"这种话。
#
# 三条硬约束（不遵守就会变成第二个 2.4MB 的 state.json）：
#   · **只记变化**，不记每一轮；
#   · **只留这一场**：进场景时清空，余韵结束清空；
#   · 条数硬上限 `LOG_MAX`，超了丢最老的。
LOG_KEY = "ccb_log"
LOG_MAX = 24
LOG_KINDS = ("pose", "site", "phase", "peak", "exit")

# ─── 进门判断（v2.28.6 叠加，第二层）──────────────────────────────────
#
#
# 所以进门判断分两层，**模型是主力、关键词只是兜底**：
#
# 1. **模型判断**：复用现成的情绪分析批（每 `mood_llm_interval_messages` 条消息
#    调一次，一次看最近 1~10 条），在那次 JSON 里多要一个 `scene`（0~1）。
#    它看的是"这一段对话在做什么"，不是有没有某个词。**零新增调用**。
# 2. **关键词兜底**：模型没配 / 调用失败 / 这一批还没跑过 → 用 `detect_intimacy`
#    在**最近一段时间**里找有没有亲密信号。它只保证"功能不瘫"，不承担主判断。
#
# 注意模型不可能每轮都给分（默认 10 条一次），所以分数带 TTL：
# 过期的分数一律当"不知道"，退回兜底。**不能拿半小时前的结论决定现在进不进。**
CCB_SCENE_ENTER = 0.65      # 进场景要多少分
CCB_SCENE_EXIT = 0.35       # 低于这个分才考虑结束（滞回，不抖动）
CCB_SCENE_EXIT_ROUNDS = 2   # 连续几次低分才真的结束
CCB_SCENE_SCORE_TTL = 30 * 60.0     # 分数保鲜期（秒）
CCB_INTIMACY_WINDOW = 15 * 60.0     # 兜底信号的回看窗口（秒）

# 兜底用的亲密信号词面（**只在模型判断不可用时生效**）。
#
# S1：原来这里面有「继续」「里面」「要我」三个泛词，实测「继续上班吧」「里面坐吧」
# 「要不要我帮你拿」全部被判成亲密信号 → 没配情绪分析模型时（默认配置！）
# 闲聊也会进场景。那正是这套判断要解决的问题，等于绕了一圈回到原地。
#
# 现在只留**明确指向性亲密**的说法，宁可漏（漏的后果是"不进场景"，
# 比"聊着工作突然进场景"轻得多）。同一个决定在 `_CCB_HAPPENED` 里已经做过一次
# ——「要我」在那里就是因为误报率高被删掉的，这里不应该再放回来。
_INTIMACY_WORDS = (
    "想要你", "想抱你", "抱着我", "抱抱我", "亲我", "吻我",
    "别停", "太深", "很深", "好深", "一起睡", "贴着我",
)


def detect_intimacy(text: str) -> bool:
    """兜底信号：这句话里有没有明确的亲密意图。

    **不是主判断**——主判断是模型给的 `scene` 分。这里只在模型不可用时顶上，
    所以宁可漏（退回'不进门'），也不要把普通对话读成邀约。
    """
    s = str(text or "")
    if not s.strip():
        return False
    return any(word in s for word in _INTIMACY_WORDS)


def scene_score_fresh(score_at: float, now: float) -> bool:
    """这个 scene 分还新不新鲜。过期一律当"不知道"。"""
    try:
        at = float(score_at or 0.0)
        moment = float(now)
    except (TypeError, ValueError):
        return False
    if at <= 0:
        return False
    return (moment - at) <= CCB_SCENE_SCORE_TTL


def scene_gate(
    *,
    in_scene: bool,
    scene_score: float,
    intimacy_hint: bool,
) -> bool:
    """「现在能不能进/留在场景里」——只回答这一件事。

    * 已经在场景里：不拦（续场不该被新一轮判断打断）；
    * 有新鲜的模型分：≥ `CCB_SCENE_ENTER` 才放行；
    * 没有分（没配 / 失败 / 过期）：退回关键词兜底，有信号才放行。

    最后一条是刻意的：模型判断不可用时**不是全放行**，而是"要一点证据"。
    全放行等于回到"聊天气也可能进场景"。
    """
    if in_scene:
        return True
    try:
        score = float(scene_score)
    except (TypeError, ValueError):
        score = -1.0
    if score >= 0.0:
        return score >= CCB_SCENE_ENTER
    return bool(intimacy_hint)




# ─── P1：她自己那一侧 ────────────────────────────────────────────────
#
# v2.28.5 之前，"想不想"只由 libido 的绝对值 + 涨幅决定，而那是**情绪层**的数字，
# 不是她此刻的身体和心气。她自己想不想要，至少要同时看四件事：
#
#   · 亲近欲涨了多少（她自己涨上来的，不是初始值）
#   · 身体的唤醒（soma.arousal——`note_chat` / `note_message` 一直在推它，
#     而 ccb 一次都没读过）
#   · 有没有在生气（aggression 高的时候，想靠近和不想示弱是两件事）
#   · 关系到哪一步了（`relstage`，聊了两千轮还停在"还在认识"的人不该有这一档）
#
# 她主动度 ≥ 0.70 且好感已经进了"深入"档时，多开一档（见 `evaluate` 的 stage 4）。
# 这一档不靠掷骰子，条件全中就出——和原来那三档同一个性质。
CCB_INITIATIVE_DEEP = 0.70
# 她明确拒绝之后的静默窗（秒）。不是 5 小时硬冷却：她只是**这会儿**不想，
# 不是"这件事不许再提"。
CCB_REFUSE_COOLDOWN_SECONDS = 30 * 60.0
# 身体唤醒在 `initiative` 里的参考区间：低于 50 不加分，高到 90 封顶。
_AROUSAL_SPAN = 40.0
# 第四档的措辞：**行为层级**，不写器官结构、不第三人称、不长于一句念头。
# 用户明确要求「允许更明确的行为层级」，但纪律仍在——把边界说清楚：
# 可以说"想让你别停"，不可以写解剖学名词，也不可以把镜头拉远成一段描写。
_INITIATIVE_WORDS = (
    "想让你别停",
    "不想要你走",
    "想再要一次",
    "抱紧一点别松",
)
# 主动档：主语是**她**的念头（不是「想让你…」这种等对方动的句式）。
# 纪律不变：行为层级，不写器官、不写情节、不长于一句。
_ACTIVE_WORDS = (
    "想自己来",
    "不想再等了",
    "今天想主动一点",
    "想把你按住",
)
# 主导度到这条线以上，auto 模式就按「她主动」选措辞。
CCB_DOMINANCE_ACTIVE = 0.62
# 动作词：只收「动到对方 / 自己动」的强动词，避免「按手机」「推门」误计。
_ACTION_WORDS = (
    "按住", "按在", "压住", "压在", "推倒", "骑上", "骑在", "跨坐", "坐上",
    "抱紧", "抱住", "搂住", "亲上", "吻上", "亲住", "咬住", "含住", "舔",
    "摸上", "抚摸", "抓住", "拉住", "扯开", "脱掉", "褪下", "套上", "顶进",
    "撞进", "蹭", "贴紧", "缠住", "勾住", "自己来", "主动", "主导", "跨上",
)
# 主导 / 顺从的人设词（只在人设正文里数次数，不参与别的判断）。
_DOM_WORDS = ("女王", "强势", "主导", "支配", "霸道", "掌控", "命令", "攻", "主动", "高冷")
_SUB_WORDS = ("顺从", "害羞", "被动", "乖巧", "听话", "温顺", "怯", "软")

# ccb_done_count：一辈子的次数（不分天）。社交层拿它判断「有没有和对方色色过」，
# 从而决定要不要主动问一句——它不是当天次数（那是 ccb_turns）。
STATE_KEYS = (
    "ccb_stage", "ccb_last_at", "ccb_satisfy", "ccb_day", "ccb_turns", "ccb_done_count",
    # P0-3 / P0-4：冷却锚点与记录层去重各自独立，不再和 `ccb_last_at` / `ccb_advance_at`
    # 抢同一根键。
    "ccb_gate_at", "ccb_event_dedup_at",
    # P1：她明确拒绝的时刻（与她被强迫的 `ccb_coerced_at` 分开记）。
    "ccb_refused_at",
    # (B 表现层）：场景内三根动态键。`-1` = 还不知道，由关键词检测填；
    # 判不出就一直保持原值（`-1` 时那一段整行不出，退回单行注入）。
    "ccb_pose", "ccb_site",
    # 
    "ccb_aftercare_until",
    # (D）：这一场到过的最深值（单调不减，换场景清零）。
    # 注意：账本 `ccb_log` **不在** 这里——`read_state` 会把每个键转成 float，
    # 而账本是 list；放进来会被吞成 0.0。
    "ccb_peak_depth",
    # 
    "ccb_scene_score", "ccb_scene_at", "ccb_intimacy_at",
    # v2.28.11：这一场谁在动（她 / 他）与最近一次是谁。
    "ccb_her_acts", "ccb_his_acts", "ccb_last_actor",
)

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
    # 
    # 留着的话，下一场开头会拿上一次的姿势开局——那是明确的出戏。
    user_state["ccb_pose"] = float(POSE_UNKNOWN)
    user_state["ccb_site"] = float(POSE_UNKNOWN)
    # D：账本与"这一场到过的最大深度"也跟着清。它们属于**这一场**，
    # 不跨场保留——跨场保留会变成第二份 state.json 体积来源。
    user_state["ccb_peak_depth"] = 0.0
    user_state[LOG_KEY] = []
    # 谁在动属于**这一场**，不跨场保留。
    user_state["ccb_her_acts"] = 0.0
    user_state["ccb_his_acts"] = 0.0
    user_state["ccb_last_actor"] = 0.0


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


def pending_days(user_state: Dict[str, Any], now: float) -> int:
    """从上次记账那天算起，跨了几个自然日（没跨天返回 0）。

    与 `decay_satisfy()` 读同一根 `ccb_day`，但**不改状态**——只读入口（状态页、
    门面、`/好感度`）要靠它把昨天那笔账折算成今天的，否则用户看到的永远是昨天
    的数字：这正是「以前留下的计数不会变」的来源。
    """
    if not isinstance(user_state, dict):
        return 0
    try:
        today = _day_index(now)
    except (OverflowError, OSError, ValueError):
        return 0
    try:
        last = float(user_state.get("ccb_day", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0
    if last <= 0:
        return 0
    return max(0, int(today - last))


def roll_day(user_state: Dict[str, Any], now: float) -> bool:
    """按天滚动一次：跨天就回落满足度、清零当日次数。返回是否真的滚动了。

    **为什么非要在读路径上调用它**：这些账原来只在 `advance_satisfy()` 里结，
    而它只有「真的进了场景」之后才跑；可「能不能进场景」又先看 `ccb_turns`。
    于是第 8 次之后：`turns_today=8` → `evaluate()` 直接 None → 不进场景 →
    不结账 → 第二天仍然读到 8。死循环，用户看到的就是「计数永远卡住、场景再也
    不进行」。所以任何要读 `ccb_turns` 的地方（注入层、状态页、门面）都得先
    在这里按天结一次账。`decay_satisfy()` 幂等，同一天重复调用不会多扣。

    只改内存里的 user_state，**不写盘**——落盘由调用方 `mark_dirty()` 决定：
    只读入口（`intimacy_summary`）用它做投影时不该顺手把 state.json 标脏。
    """
    if not isinstance(user_state, dict):
        return False
    if pending_days(user_state, now) <= 0:
        return False
    decay_satisfy(user_state, now)
    return True


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
    affection_deep: float = CCB_AFFECTION_DEEP,
) -> bool:
    """只有**数值门槛**这一组——冷却/当日封顶/精力/群聊都不在这里。

    拆出来是给 `_qualifies`（能不能进）与 `scene_lapsed`（结没结束）共用，
    门槛只此一处定义，改阈值不会两边走歪。

    两条「涨上来」的要求各有一个**饱和例外**：亲近欲已经顶到量程（50）时不可能再涨，
    但那一刻本来就是「她一直很想要」，按涨不上去判她不想是反的；好感已经进了「深入」
    档（默认 75）时同理——关系已经很深，不需要再靠一次涨幅证明。没有这两个例外，
    最亲密的那批人反而永远进不了场景（线上实测：libido 50/base 50、好感 100/100 的
    用户被「涨得不够」挡在门外）。
    """
    low = reachable_libido_min(libido_min, base_libido)
    if libido < low:
        return False
    if (libido - base_libido) < float(libido_rise):
        try:
            pinned = float(libido) >= LIBIDO_MAX - 1.0
        except (TypeError, ValueError):
            pinned = False
        if not pinned:
            return False
    if affection < float(affection_min):
        return False
    if (affection - base_affection) < float(affection_rise):
        try:
            deep = float(affection) >= float(affection_deep)
        except (TypeError, ValueError):
            deep = False
        if not deep:
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
    affection_deep: float = CCB_AFFECTION_DEEP,
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
        affection_deep=affection_deep,
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
    affection_deep: float = CCB_AFFECTION_DEEP,
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
        affection_deep=affection_deep,
    )


def _clamp01(value: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    if out != out:  # NaN
        return 0.0
    return max(0.0, min(1.0, out))


def initiative(
    *,
    libido: float,
    base_libido: float,
    arousal: float = 50.0,
    aggression: float = 25.0,
    relstage_index: int = 0,
    affection: float = 0.0,
    affection_min: float = CCB_AFFECTION_MIN,
    use_arousal: bool = True,
) -> float:
    """她自己有多想（0~1）。**纯函数，不写状态**——它每轮都要算。

    为什么需要这个数：`evaluate` 原来只问"数值够不够"，那回答的是
    「该不该」，不是「她想不想」。同一条门槛下，一个刚被骂完的人和一个
    身体正热的人给出同一句话，那就不像人了。

    四项权重刻意不等：亲近欲占大头（那本来就是她的欲望），身体次之，
    心气与关系各占一小份。`use_arousal=False` 时第二项归零（面板可关）——
    注意这**只关掉身体那一项**，另外三项照常参与，所以它不是「回到旧版」的
    开关，只是「别让身体读数影响她」的开关。

    返回值**不落盘**：它随时在变，落盘会让 `refresh_contract` 每分钟重写
    全量 state.json（线上那份 2.4 MB，同类坑在 link.py 里已经踩过一次）。
    """
    try:
        lib = float(libido)
        base = float(base_libido)
    except (TypeError, ValueError):
        return 0.0
    lib_span = 12.0
    lib_part = _clamp01((lib - base) / lib_span)
    if use_arousal:
        try:
            ar = float(arousal)
        except (TypeError, ValueError):
            ar = 50.0
        if ar != ar:  # NaN → 当"不知道"，不加不减
            ar = 50.0
        arousal_part = _clamp01((ar - 50.0) / _AROUSAL_SPAN)
    else:
        arousal_part = 0.0
    try:
        agg = float(aggression)
    except (TypeError, ValueError):
        agg = 25.0
    calm_part = _clamp01(1.0 - agg / 50.0)
    try:
        stage_part = _clamp01(float(relstage_index) / 3.0)
    except (TypeError, ValueError):
        stage_part = 0.0
    raw = 0.45 * lib_part + 0.25 * arousal_part + 0.15 * calm_part + 0.15 * stage_part
    # 好感没到门槛时整体压低：数值上"想"，但她不会对还不够近的人主动。
    try:
        aff = float(affection)
        floor = float(affection_min)
    except (TypeError, ValueError):
        return _clamp01(raw)
    gate = _clamp01((aff - floor * 0.5) / max(1.0, floor * 0.5))
    return _clamp01(raw * (0.35 + 0.65 * gate))


def detect_action(text: str) -> int:
    """这句话里有几个「她在动 / 他在动」的强动作词（否定句不算）。

    只用于记「这一场谁在动」，不参与任何门槛判断——判错最多是多一句状态，
    不会影响进不进场景。
    """
    s = str(text or "")
    if not s.strip():
        return 0
    count = 0
    for word in _ACTION_WORDS:
        start = 0
        while True:
            idx = s.find(word, start)
            if idx < 0:
                break
            if not _negated(s, idx):
                count += 1
                break
            start = idx + len(word)
    return count


def dominance(
    *,
    persona_prompt: str = "",
    aggression: float = 25.0,
    relstage_index: int = 0,
    affection: float = 0.0,
    her_acts: float = 0.0,
    his_acts: float = 0.0,
) -> float:
    """她这一场有多想主导（0~1）。纯函数、不写状态、不落盘。

    四路信息，权重刻意不等：人设词 > 实际动作比例 > 心气 > 关系档。
    人设是「她本来是什么样的人」，动作比例是「这一场实际发生了什么」——
    后者更重要，但人设不会被几个动作完全扭转。
    """
    base = 0.5
    text = str(persona_prompt or "")[:2000]
    dom = sum(text.count(w) for w in _DOM_WORDS)
    sub = sum(text.count(w) for w in _SUB_WORDS)
    if dom + sub > 0:
        base += 0.18 * (dom - sub) / max(1, dom + sub)
    try:
        base += (max(0.0, min(50.0, float(aggression))) / 50.0 - 0.5) * 0.16
    except (TypeError, ValueError):
        pass
    try:
        total = max(0.0, float(her_acts)) + max(0.0, float(his_acts))
        if total > 0:
            base += (max(0.0, float(her_acts)) / total - 0.5) * 0.26
    except (TypeError, ValueError):
        pass
    try:
        base += (max(0.0, min(3.0, float(relstage_index))) / 3.0 - 0.5) * 0.10
    except (TypeError, ValueError):
        pass
    return _clamp01(base)


def refused_within(user_state: Dict[str, Any], moment: float,
                   window: float = CCB_REFUSE_COOLDOWN_SECONDS) -> bool:
    """她是不是**刚刚**明确说过不要（`window` 秒内）。

    与 `ccb_coerced_at`（他强迫她）分开：那是"他越界"，这是"她不想要"。
    两者都该挡住场景，但结算不同——她拒绝之后仍然发生，才算他越界。
    """
    if not isinstance(user_state, dict):
        return False
    try:
        stamp = float(user_state.get("ccb_refused_at", 0.0) or 0.0)
    except (TypeError, ValueError):
        return False
    if not (stamp > 0):
        return False
    try:
        return (float(moment) - stamp) < float(window)
    except (TypeError, ValueError):
        return False


def body_profile(*, depth_cap_cm: float = 14.0, sensitivity: float = 60.0) -> Dict[str, Any]:
    """她的静态身体档案。**只在第一次建，之后再也不改**（见 `core_instance`）。

    做成纯函数是为了可测：这里不碰 state，调用方负责"已经有了就别覆盖"。
    """
    try:
        cap = float(depth_cap_cm)
    except (TypeError, ValueError):
        cap = 14.0
    # NaN 单独处理：`min/max` 遇上 NaN 会静默给出一个"看起来正常"的值
    # （Python 里 `min(18.0, nan)` 返回 18.0）——那会把坏数据变成合法数据。
    if cap != cap:
        cap = 14.0
    cap = max(1.0, min(18.0, cap))
    try:
        sens = float(sensitivity)
    except (TypeError, ValueError):
        sens = 60.0
    if not (sens == sens):
        sens = 60.0
    return {"depth_cap_cm": round(cap, 1), "sensitivity": int(max(0, min(100, sens)))}


def partner_profile(*, length_cm: float = 13.0) -> Dict[str, Any]:
    """对方（用户）的静态档案，按人各一份。0 表示"不设定"，那就跳过深度判定。"""
    try:
        val = float(length_cm)
    except (TypeError, ValueError):
        val = 13.0
    if not (val == val) or val < 0:
        val = 0.0
    return {"length_cm": round(min(30.0, val), 1)}


def phase_of(
    *, satisfy: float, stage: float, aftercare_until: float, now: float
) -> int:
    """性反应周期到哪一段。0 = 不在场景，5 = 余韵。

    纯推导，**不写状态**——它每轮都要算，落盘会让 `refresh_contract` 变成
    常驻写盘源（同一个坑在 link.py 里已经踩过）。
    """
    try:
        until = float(aftercare_until or 0.0)
        moment = float(now)
    except (TypeError, ValueError):
        until, moment = 0.0, 0.0
    if until > 0 and moment < until:
        return 5
    try:
        st = float(stage or 0.0)
        sat = float(satisfy or 0.0)
    except (TypeError, ValueError):
        return 0
    if st <= 0:
        return 0
    if sat >= 100.0:
        return 4
    if sat >= 80.0:
        return 3
    if sat >= 40.0:
        return 2
    return 1


def depth_estimate(length_cm: float, phase: int) -> float:
    """按阶段估一个"此刻大概到多深"（cm）。**不是测量值**，是给判定用的代理。"""
    try:
        length = float(length_cm)
    except (TypeError, ValueError):
        return 0.0
    if length <= 0:
        return 0.0
    ratio = _DEPTH_RATIO.get(int(phase), 0.0)
    return round(max(0.0, length * ratio), 1)


def is_over_depth(depth_cm: float, cap_cm: float) -> bool:
    """超过她自己的上限 = "太深/撑满"。上限是**阈值**，不参与任何动态计算。"""
    try:
        return float(depth_cm) > float(cap_cm)
    except (TypeError, ValueError):
        return False


def _negated_after(s: str, pos: int, words=_NEG_PREFIX, window: int = _NEG_WINDOW) -> bool:
    """命中词**后面**那一小段里有没有否定（只给 `detect_occurrence` 用）。

    它原来的窗口是双向的（「做过了，不要再提」该被挡）；而姿势/位置只看前面
    （「别停」里的"停"在后面是鼓励）。所以两条路不能共用同一个方向。
    """
    tail = s[pos: pos + window]
    if any(w in tail for w in words if w != _NEG_SINGLE):
        return True
    if _NEG_SINGLE in words:
        idx = tail.find(_NEG_SINGLE)
        while idx >= 0:
            g = pos + idx
            before = s[g - 1] if g > 0 else ""
            # 同 `_negated`：空串必须单独判，否则 `"" in 串` 恒真。
            if not before or before not in _NEG_SINGLE_COMPOUND:
                return True
            idx = tail.find(_NEG_SINGLE, idx + 1)
    return False


def _hit_detail(text: str, table) -> Tuple[Optional[int], str]:
    """在词表里找命中，返回 `(序号, 命中的词)`。带否定护栏与歧义丢弃。

    这是判断层的唯一入口：`detect_pose` / `detect_site` 都走它，
    保证两条路的"否定/歧义"口径一致。
    """
    s = str(text or "")
    if not s.strip():
        return None, ""
    hits: list[Tuple[int, str]] = []
    for index, words in enumerate(table):
        for word in words:
            start = 0
            while True:
                pos = s.find(word, start)
                if pos < 0:
                    break
                if _negated(s, pos):
                    start = pos + len(word)
                    continue
                hits.append((index, word))
                break
    if not hits:
        return None, ""
    kinds = {index for index, _w in hits}
    if len(kinds) != 1:
        # 两种姿势同时命中（"转过来" + "从后面"）→ 有歧义，不猜。
        return None, ""
    index = next(iter(kinds))
    # 同一类里取**最长**的那个词当命中证据：越长越具体，日志里更好读。
    word = max((w for i, w in hits if i == index), key=len, default="")
    return index, word


def detect_pose_detail(text: str) -> Tuple[Optional[int], str]:
    """判姿势，并给出**是靠哪个词判出来的**（诊断日志用）。"""
    return _hit_detail(text, _POSE_WORDS)


def detect_site_detail(text: str) -> Tuple[Optional[int], str]:
    """判探入位置，同样带命中证据。"""
    return _hit_detail(text, _SITE_WORDS)


def detect_pose(text: str) -> Optional[int]:
    """从一句话里判姿势。判不出/有歧义返回 None（调用方保持原值）。"""
    return detect_pose_detail(text)[0]


def detect_site(text: str) -> Optional[int]:
    """从一句话里判探入位置。判不出返回 None。"""
    return detect_site_detail(text)[0]


# ─── D：账本读写 ─────────────────────────────────────────────────────

def log_append(user_state: Dict[str, Any], kind: str, value: Any, at: float) -> None:
    """记一笔场景变化。**只记变化**，条数硬上限，超了丢最老的。

    调用方负责"只在真的变了时调"——这里不做去重，因为"同一姿势又提了一次"
    也可能是有意义的信息（她在强调）。判重放在调用方更省。
    """
    if not isinstance(user_state, dict):
        return
    kind = str(kind or "")
    if not kind:
        return
    rows = user_state.get(LOG_KEY)
    if not isinstance(rows, list):
        rows = []
    try:
        stamp = round(float(at), 1)
    except (TypeError, ValueError):
        stamp = 0.0
    rows = [r for r in rows if isinstance(r, dict)]
    rows.append({"kind": kind, "value": value, "at": stamp})
    user_state[LOG_KEY] = rows[-LOG_MAX:]


def log_clear(user_state: Dict[str, Any]) -> None:
    if isinstance(user_state, dict):
        user_state[LOG_KEY] = []


def log_changes(user_state: Dict[str, Any], kind: str) -> int:
    """这一类变化一共发生了几次（"换过几次姿势"）。"""
    rows = user_state.get(LOG_KEY) if isinstance(user_state, dict) else None
    if not isinstance(rows, list):
        return 0
    return sum(1 for r in rows if isinstance(r, dict) and r.get("kind") == kind)


def log_last(user_state: Dict[str, Any], kind: str) -> float:
    """这一类最近一次发生的时间；没有则 0。"""
    rows = user_state.get(LOG_KEY) if isinstance(user_state, dict) else None
    if not isinstance(rows, list):
        return 0.0
    for row in reversed(rows):
        if isinstance(row, dict) and row.get("kind") == kind:
            try:
                return float(row.get("at") or 0.0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def note_peak_depth(user_state: Dict[str, Any], depth_cm: float) -> float:
    """这一场里**到过**的最深值，单调不减。

    为什么要单调：深度是按阶段估的代理值，阶段回退（比如 satisfy 因跨天回落）
    会让"刚才说撑到疼"变成"现在还没到底"，前后矛盾。场景内只升不降；
    换场景时由 `reset_scene` 清零。
    """
    try:
        cur = float(user_state.get("ccb_peak_depth", 0.0) or 0.0)
    except (TypeError, ValueError):
        cur = 0.0
    try:
        new = max(cur, float(depth_cm))
    except (TypeError, ValueError):
        new = cur
    user_state["ccb_peak_depth"] = new
    return new


def aftercare_ratio(*, aftercare_until: float, now: float, window: float) -> float:
    """余韵还剩多少（0~1）。给显著度做衰减用。

    窗口长度拿不到（配成 0 或者坏值）时按"刚开始"给 1.0——宁可让她这会儿软一点，
    也不要因为一个坏配置把整段余韵静默吞掉。
    """
    try:
        until = float(aftercare_until or 0.0)
        moment = float(now)
        span = float(window or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if until <= 0 or moment >= until:
        return 0.0
    if span <= 0:
        return 1.0
    return max(0.0, min(1.0, (until - moment) / span))


def aftercare_note(*, aftercare_until: float, now: float, window: float) -> str:
    """余韵期那一句。窗口过了一半之后语气再淡一档。

    没有这段的话，"到顶"在体验上等于"突然什么都没了"——用户明确要的
    事后余韵就落在这一句上。
    """
    try:
        until = float(aftercare_until or 0.0)
        moment = float(now)
        span = float(window or 0.0)
    except (TypeError, ValueError):
        return ""
    if until <= 0 or moment >= until:
        return ""
    left = until - moment
    if span > 0 and left > span * 0.5:
        return "刚结束，还软着"
    return "还没缓过来"


def scene_note(
    *,
    pose: int,
    site: int,
    phase: int,
    depth_cm: float,
    cap_cm: float,
) -> str:
    """场景内那一段紧凑状态：姿势 / 位置 / 深度判断 / 阶段。

    返回空串表示**没有可说的**——一个都判不出来时整段不出（A 的兜底：
    退回纯单行注入，绝不因为缺字段就报错或中断）。

    为什么不做成四行：注入通道是"每轮一句"（`prompt_builder` 把各维度拼成
    一句、句号收尾），四行会被模型当成清单逐条复述。四个要素压进一个短语，
    信息一样不少，但读起来仍是一句状态。
    """
    bits = []
    if 0 <= int(pose) < len(POSE_NAMES):
        bits.append(POSE_NAMES[int(pose)])
    if 0 <= int(site) < len(SITE_NAMES):
        bits.append(SITE_NAMES[int(site)])
    if float(depth_cm) > 0:
        over = is_over_depth(depth_cm, cap_cm)
        bits.append(f"约{float(depth_cm):.0f}cm/{float(cap_cm):.0f}cm·{'过深' if over else '未过深'}")
    name = PHASE_NAMES[int(phase)] if 0 <= int(phase) < len(PHASE_NAMES) else ""
    if name:
        bits.append(name)
    return "·".join(b for b in bits if b)


def scene_idle_expired(*, last_at: float, now: float) -> bool:
    """这一场是不是早就过去了（长时间没人说话）。

    `last_at` 取 `ccb_last_at`；为 0（从没发生过）时不算过期。
    """
    try:
        last = float(last_at or 0.0)
        moment = float(now)
    except (TypeError, ValueError):
        return False
    if last <= 0:
        return False
    return (moment - last) > CCB_SCENE_IDLE_SECONDS


# ─── 状态快照（只读）────────────────────────────────────────────────
#
# 为什么单独拉一个纯函数：`evaluate()` 只回答「进不进」，而且只输出一句内心话；
# 「她此刻到底在什么位置」这件事（亲近欲离门槛多远、今天第几次、在场景哪一段、
# 余韵还剩多久、被哪道闸挡着）从来没有一个地方算过。状态页、指令、门面各写一套
# 就会三处各说各话——用户实测到的「展示出来的东西会被误导」正是这么来的。
#
# 这里只读：不改 user_state、不写盘、不推进场景。按天折算用 `pending_days()`
# 做**投影**，真正的写回由调用方先调 `roll_day()`——只读入口不该因为看一眼就
# 把 state.json 标脏。

def _fs(value: Any, default: float = 0.0) -> float:
    """取一个有限浮点：拿不到 / NaN / ±Inf 一律退回默认值。

    NaN 必须挡在这里：`f"{nan:.0f}"` 会原样打出 "nan"，而比较运算全是假——
    面板上就是一行 `亲近欲 nan/50`，既不报错也查不出是哪一步坏的。
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def duration_text(seconds: Any) -> str:
    """把一段秒数说成人话（面板/指令用）。坏值按 0 处理，不抛。"""
    try:
        total = int(max(0.0, float(seconds)))
    except (TypeError, ValueError):
        return "不到 1 分钟"
    if total < 60:
        return "不到 1 分钟"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时" + (f" {rest} 分" if rest else "")
    days, rest_hours = divmod(hours, 24)
    return f"{days} 天" + (f" {rest_hours} 小时" if rest_hours else "")


def delta_text(value: Any) -> str:
    """「涨了 13 / 跌了 7 / 没动」——面板上读得懂的是方向，不是裸数字。"""
    num = _fs(value)
    if num > 0.5:
        return f"涨了 {num:.0f}"
    if num < -0.5:
        return f"跌了 {abs(num):.0f}"
    return "没动"


def threshold_text(value: Any, base: Any, minimum: Any, rise: Any) -> str:
    """离门槛还差多少（一句人话）。「够」是唯一的正面结论，其余都说清差在哪。

    绝对值和涨幅是**与**的关系，两条各自可能不满足，所以分开说——只报一条的话，
    用户按面板调完另一条，还是进不去。
    """
    val = _fs(value)
    bas = _fs(base)
    low = _fs(minimum)
    need = _fs(rise)
    if val < low:
        return f"不够，差 {low - val:.0f} 到 {low:.0f}"
    if (val - bas) < need:
        return f"涨得不够，还差 {need - (val - bas):.1f}"
    return "够"


def scene_status(
    user_state: Optional[Dict[str, Any]],
    now: float,
    *,
    libido: float = 0.0,
    base_libido: float = 0.0,
    affection: float = 0.0,
    base_affection: float = 0.0,
    energy: float = 100.0,
    is_group: bool = False,
    persona_prompt: str = "",
    safety_reason: str = "",
    coerced: bool = False,
    scene_score: float = -1.0,
    intimacy_hint: bool = True,
    libido_min: float = CCB_LIBIDO_MIN,
    libido_rise: float = CCB_LIBIDO_RISE,
    affection_min: float = CCB_AFFECTION_MIN,
    affection_rise: float = CCB_AFFECTION_RISE,
    affection_deep: float = CCB_AFFECTION_DEEP,
    daily_max: int = CCB_DAILY_MAX,
) -> Dict[str, Any]:
    """她此刻的亲密状态——**纯函数**：只读、不改状态、不写盘。

    返回的每一格都对着一个用户看得见的问题：在不在场景里、到哪一段了、今天几次、
    余韵还是冷却、离门槛差多少、被哪道闸挡着。`blockers` 的顺序照着
    `evaluate()` 的判定顺序排，排查时从上往下看就行，不会出现「面板说能进、
    实际进不去」这种对不上的情况。

    跨天折算在这里是**投影**（`satisfy` 按天回落、`turns_today` 归零），
    不写回 user_state；需要落盘的调用方先调 `roll_day()`——这样 `/好感度`
    这类只读入口读到的是今天的数字，同时不会把 state.json 标脏。
    """
    state = user_state if isinstance(user_state, dict) else {}
    moment = _fs(now)
    days = pending_days(state, moment)

    stage = int(_fs(state.get("ccb_stage"), 0.0))
    satisfy = max(
        0.0,
        min(
            CCB_SATISFY_FULL,
            _fs(state.get("ccb_satisfy"), 0.0)
            - CCB_SATISFY_DECAY_PER_DAY * days,
        ),
    )
    turns_today = 0 if days > 0 else int(_fs(state.get("ccb_turns"), 0.0))
    done_count = int(_fs(state.get("ccb_done_count"), 0.0))
    last_at = _fs(state.get("ccb_last_at"), 0.0)
    last_ago = max(0.0, moment - last_at) if last_at > 0 else 0.0
    aftercare_until = _fs(state.get("ccb_aftercare_until"), 0.0)
    aftercare_left = max(0.0, aftercare_until - moment)
    peak_depth = max(0.0, _fs(state.get("ccb_peak_depth"), 0.0))
    pose = int(_fs(state.get("ccb_pose"), float(POSE_UNKNOWN)))
    site = int(_fs(state.get("ccb_site"), float(POSE_UNKNOWN)))
    pose_name = POSE_NAMES[pose] if 0 <= pose < len(POSE_NAMES) else ""
    site_name = SITE_NAMES[site] if 0 <= site < len(SITE_NAMES) else ""

    lib = _fs(libido)
    base_lib = _fs(base_libido)
    aff = _fs(affection)
    base_aff = _fs(base_affection)
    eng = _fs(energy, 0.0)
    low_cfg = _fs(libido_min, CCB_LIBIDO_MIN)
    # 与 `evaluate` 同一个数：配置门槛够不着时按「基线 + 每日可达涨幅」算，
    # /亲密状态 显示的就是真正生效的那个门槛（不然面板说差 8 点，其实永远到不了）。
    low = reachable_libido_min(low_cfg, base_lib)
    need_lib = _fs(libido_rise, CCB_LIBIDO_RISE)
    aff_low = _fs(affection_min, CCB_AFFECTION_MIN)
    need_aff = _fs(affection_rise, CCB_AFFECTION_RISE)
    aff_deep = _fs(affection_deep, CCB_AFFECTION_DEEP)

    # 场景闲置过期：`ccb_stage` 会因为「没人说话」而挂在那里过夜，
    # 照实显示"进行中"就是骗人——6 小时没人碰过的那一场早就过去了。
    expired = stage > 0 and scene_idle_expired(last_at=last_at, now=moment)
    in_scene = stage > 0 and not expired
    phase = phase_of(
        satisfy=satisfy,
        stage=float(stage) if in_scene else 0.0,
        aftercare_until=aftercare_until,
        now=moment,
    )
    stage_name = PHASE_NAMES[stage] if in_scene and 0 <= stage < len(PHASE_NAMES) else ""
    phase_name = PHASE_NAMES[phase] if 0 <= phase < len(PHASE_NAMES) else ""

    # 冷却只挡**开新的一场**，场景里不适用——这时候给个「还要等 3 小时」是误导。
    cooldown_left = 0.0
    if not in_scene:
        gate_at = _fs(state.get("ccb_gate_at"), 0.0) or last_at
        if gate_at > 0 and (moment - gate_at) < CCB_COOLDOWN_SECONDS:
            # 夹在窗口内：时钟被回拨过时 `gate_at` 可能在未来，不夹就会报出
            # 「还要 3 天」这种比配置本身还大的倒计时。
            cooldown_left = max(
                0.0, min(CCB_COOLDOWN_SECONDS, CCB_COOLDOWN_SECONDS - (moment - gate_at))
            )

    refused_at = _fs(state.get("ccb_refused_at"), 0.0)
    refused_left = 0.0
    if refused_at > 0:
        refused_left = max(
            0.0,
            min(CCB_REFUSE_COOLDOWN_SECONDS, CCB_REFUSE_COOLDOWN_SECONDS - (moment - refused_at)),
        )

    blockers: List[str] = []
    if not in_scene:
        # 顺序照 `evaluate()`：安全闸 → 拒绝 → 强迫 → 群聊 → 性别 → 数值门槛
        # → 精力 → 冷却 → 进门判断 → 当日封顶。
        if safety_reason:
            blockers.append(f"人设安全闸：{safety_reason}")
        if refused_left > 0:
            blockers.append(f"她刚拒绝过（{duration_text(refused_left)}内不提）")
        if coerced:
            blockers.append("他刚越界过，这一会儿不算")
        if is_group:
            blockers.append("群聊里不会进场景")
        if not looks_female(persona_prompt):
            blockers.append("人设判成男角色（只对女角色开）")
        if aff < aff_low:
            blockers.append(f"好感不够（{aff:.0f}/100，差 {aff_low - aff:.0f}）")
        elif (aff - base_aff) < need_aff and aff < aff_deep:
            # 深入档（默认 75）算「关系已经够深」，不再要求这一阵子涨过 8 分。
            blockers.append(
                f"好感涨得不够（{delta_text(aff - base_aff)}，还差 {need_aff - (aff - base_aff):.1f}，"
                f"或把好感养到深入档 {aff_deep:.0f}）"
            )
        if lib < low:
            hint = ""
            if low < low_cfg - 1e-6:
                hint = f"（配置 {low_cfg:.0f} 超出她这具身体一天能涨到的上限，已按可达值算）"
            blockers.append(f"亲近欲不够（{lib:.0f}/50，差 {low - lib:.0f}）{hint}")
        elif (lib - base_lib) < need_lib and lib < LIBIDO_MAX - 1.0:
            # 已经顶到量程（50）算「她一直很想要」，不再要求还能再涨。
            blockers.append(
                f"亲近欲涨得不够（{delta_text(lib - base_lib)}，还差 {need_lib - (lib - base_lib):.1f}，"
                f"或顶到 {LIBIDO_MAX:.0f} 满格）"
            )
        if eng < CCB_ENERGY_MIN:
            blockers.append(f"精力不足（{eng:.0f} < {CCB_ENERGY_MIN:.0f}）")
        if cooldown_left > 0:
            blockers.append(f"冷却中（还要 {duration_text(cooldown_left)}）")
        score = _fs(scene_score, -1.0)
        if score >= 0.0:
            if score < CCB_SCENE_ENTER:
                blockers.append(f"场景分未达标（{score:.2f} < {CCB_SCENE_ENTER:.2f}）")
        elif not intimacy_hint:
            blockers.append("暂无模型判断且无亲密信号")
        if turns_today >= int(daily_max):
            blockers.append(f"当日已封顶（{turns_today}/{int(daily_max)}）")

    return {
        "in_scene": in_scene,
        "expired": bool(expired),
        "stage": stage,
        "stage_name": stage_name,
        "satisfy": round(satisfy, 1),
        "satisfy_phase": phase,
        "satisfy_phase_name": phase_name,
        "turns_today": turns_today,
        "daily_max": int(daily_max),
        "done_count": done_count,
        "last_at": round(last_at, 1),
        "last_ago": round(last_ago, 1),
        "cooldown_left": round(cooldown_left, 1),
        "aftercare_left": round(aftercare_left, 1),
        "peak_depth": round(peak_depth, 1),
        "pose": pose if pose_name else POSE_UNKNOWN,
        "pose_name": pose_name,
        "site": site if site_name else POSE_UNKNOWN,
        "site_name": site_name,
        "libido": round(lib, 1),
        "base_libido": round(base_lib, 1),
        "libido_rise": round(lib - base_lib, 1),
        "affection": round(aff, 1),
        "base_affection": round(base_aff, 1),
        "affection_rise": round(aff - base_aff, 1),
        "energy": round(eng, 1),
        "can_enter": not blockers,
        "blockers": blockers,
        "libido_min": round(low, 1),
        # 面板上配的那个数：与 `libido_min` 不同时说明「配置够不着，已按可达值算」。
        "libido_min_config": round(low_cfg, 1),
        "libido_rise_need": round(need_lib, 1),
        "affection_min": round(aff_low, 1),
        "affection_rise_need": round(need_aff, 1),
        "affection_deep": round(aff_deep, 1),
    }


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
    initiative_score: float = 0.0,
    lead: str = "auto",
    dominance_score: float = 0.5,
    refused: bool = False,
    safety_reason: str = "",
    in_scene: bool = False,
    scene_score: float = -1.0,
    intimacy_hint: bool = True,
) -> Optional[Tuple[float, str, int]]:
    """条件全中就返回 `(显著度, 内心话, 阶段)`；任何一条不满足都返回 None。

    群里是**硬闸**：直接 None，不参与后面的竞争——按语气说的话没法在群里收场。
    """
    if not enabled:
        return None
    # P3：安全闸排在最前面，比一切都早。命中就不降档、不软化——
    # 未成年 / 血缘这两类人设**整条功能不生效**。
    if safety_reason:
        return None
    # P1：她刚刚明确说过不要。这条与 coerced 是两件事——coerced 是「他越界」，
    # 这里是「她不想要」；两者都该挡住场景，但**不该**清掉场景状态
    # （所以它不进 `scene_lapsed`，见那个函数的说明）。
    if refused:
        return None
    # 绝对门槛先夹到「她真能到的高度」（见 `reachable_libido_min`）。夹在这里而不是
    # 让每个调用方自己算：`_qualifies` 与下面的档位（`_band_for` / `bands`）必须用
    # 同一个数，否则会出现「门槛过了、档位却找不到」→ 静默 return None 的老毛病。
    eff_min = reachable_libido_min(libido_min, base_libido)
    # E4/E6：刚被强迫过（无视拒绝/胁迫）→ 不进入，谈不上「享受」
    if not _qualifies(
        coerced=coerced,
        is_group=is_group,
        persona_prompt=persona_prompt,
        affection=affection,
        base_affection=base_affection,
        libido=libido,
        base_libido=base_libido,
        libido_min=eff_min,
        libido_rise=libido_rise,
        affection_min=affection_min,
        affection_rise=affection_rise,
        affection_deep=affection_deep,
    ):
        return None
    if energy < CCB_ENERGY_MIN:
        return None
    # 冷却只挡**开新的一场**，不挡**继续这一场**。
    #
    # 原来无条件挡：`satisfy` 每进一次 +40、到顶要 3 次，而冷却 5 小时——
    # 于是"一次完整的场景"实际横跨 15 小时、跨到第二天，所谓"临界/爆发"
    # 永远只在第三天出现。实测模拟里第 2 轮就被冷却挡回，satisfy 卡在 40。
    # 场景进行中（`in_scene`）不再受冷却约束；收场之后（stage 归零）冷却照旧生效。
    if not in_scene and last_at > 0 and (now - last_at) < CCB_COOLDOWN_SECONDS:
        return None
    # 进门判断：模型说"这一段在做什么"，关键词只在模型不可用时兜底。
    # 这一条排在数值门槛**之后**——先算"她够不够",再问"这会儿合不合适"。
    if not scene_gate(
        in_scene=in_scene, scene_score=scene_score, intimacy_hint=intimacy_hint
    ):
        return None
    # 一天封顶。门槛全中时它会一直亮着，不封顶就能一直刷下去。
    if turns_today >= CCB_DAILY_MAX:
        return None

    band = _band_for(libido, eff_min)
    if not band:
        return None
    score, words = band
    # 阶段跟着档位走，同样从阈值推导——写死 43/48 的话阈值一改就又出缝。
    ladder = bands(eff_min)
    stage = 1 + sum(1 for floor, _s, _w in ladder[1:] if libido >= floor)
    # E1：好感没到「深入」那一档时，只给最浅一档（擦边暧昧），不往深走。
    if affection < float(affection_deep):
        stage = 1
        words = ladder[0][2]
    # P1 第四档：她**自己**主动度够高时，才开这一档。
    #
    # 为什么不做成第四根 libido 档位：`bands()` 的档位是从触发阈值线性推出来的，
    # 而亲近欲上限是 50 —— 38 起、步长 5，第四档落在 53，永远够不着（除非把
    # 步长降到 3，那会改掉现有三档的边界）。所以这一档挂在**另一个维度**上：
    # 数值已经到顶（stage == len(ladder)）+ 好感已进深入档 + 她自己够主动。
    # 三条同时成立才算，缺一条就退回原档——它没有放宽任何既有门槛。
    if (
        stage >= len(ladder)
        and affection >= float(affection_deep)
        and float(initiative_score) >= CCB_INITIATIVE_DEEP
    ):
        stage = len(ladder) + 1
        words = _INITIATIVE_WORDS
    # v2.28.11：她自己主导时，措辞换成「主语是她」的那一套。
    # 仍然是念头（想自己来 / 不想再等了），不是指令，也不写动作。
    _lead = str(lead or "auto")
    active = _lead == "her" or (
        _lead == "auto" and float(dominance_score) >= CCB_DOMINANCE_ACTIVE
    )
    if active and stage >= 2:
        words = _ACTIVE_WORDS
    return score, words[index % len(words)], stage

# ─── 「这件事到底发生过没有」：独立于状态机 ────────────────────────
# 状态机（`evaluate`）只回答「现在该不该进场景」，门槛 78 好感对多数人永远够不着；
# 而聊天里**真的发生过**的记录不能由它决定。线上实测：9136 个用户条目里
# `ccb_done_count` 全为 0——因为没有任何代码去看过这件事。
_CCB_HAPPENED = (
    "做爱", "做过了", "操了", "被操", "干了", "插进", "进入身体", "内射", "射在",
    "高潮", "到了顶点", "一起去了", "弄坏了", "要了你", "操我", "插我",
)
_CCB_NEGATION = ("不要", "别", "停", "没做", "不想", "拒绝", "算了", "还没")
# P0-2：「在说这件事」不是「这件事发生了」。
#
# 原来词表里有裸的「做了」「进来」「要我」——于是
#   「你做了什么」→ 命中「你」+「做了」→ 记成一次
#   「你进来吧」  → 记成一次
#   「你要我怎么说」→ 记成一次
# 后果不只是次数虚高：`note_occurrence` 还会写冷却锚点，一句普通闲聊能把真正的
# 触发推到 5 小时之后。三处一起处理：词表去掉歧义词，再加一道「谈论守护」。
_CCB_TALK_GUARD = ("问", "说", "想", "让", "吗", "？", "?")
_CCB_PROFANITY = (
    "操你妈", "操你大爷", "我操", "卧槽", "我靠", "操蛋", "操他", "草泥马", "干你妈",
    "干死你", "去你妈", "妈的", "傻逼", "滚蛋", "滚",
)
_CCB_OBJECT = ("你", "我", "她", "他", "身体", "里面", "怀里", "床上", "一起")


def detect_occurrence(text: str) -> bool:
    """这条正文是在说「已经发生了」，而不是在讨论/拒绝/开玩笑。

    保守判据：必须命中一个明确动作词，且该词前后 6 个字里没有否定词。
    宁可漏记也不误记——误记会污染「一辈子的次数」。
    """
    s = str(text or "")
    if not s.strip():
        return False
# 「别停」「不要停」是**鼓励**不是否定：先把它们中和掉，否则最典型的一句会被判成「没发生」。
    for encourage in ("别停", "不要停", "不许停", "不准停", "别停下"):
        s = s.replace(encourage, "继续")
    if any(bad in s for bad in _CCB_PROFANITY):
        return False
    if not any(obj in s for obj in _CCB_OBJECT):
        return False
    for word in _CCB_HAPPENED:
        start = 0
        while True:
            i = s.find(word, start)
            if i < 0:
                break
            # S4：否定判定统一走 `_negated` / `_negated_after`——它们把单字「别」
            # 当构词排除处理，否则「告别之后做过了」「区别在于我们做过了」会被漏记。
            # 这里保留**双向**（「做过了，不要再提」该被挡），姿势/位置只看前面。
            if _negated(s, i, _CCB_NEGATION, 6) or _negated_after(
                s, i + len(word), _CCB_NEGATION, 6
            ):
                start = i + len(word)
                continue
            # 谈论守护只看**动作词之前**那三个字：疑问/转述/打算都长在词前面
            # （「做了什么」「问做过了吗」「想…」）。放进整窗会把「我要你了」误杀
            # ——那句里的「要」在词内部/前面，却是真的发生了。
            lead = s[max(0, i - 3): i]
            if any(talk in lead for talk in _CCB_TALK_GUARD):
                start = i + len(word)
                continue
            return True
    return False


def note_occurrence(user_state: Dict[str, Any], moment: float) -> bool:
    """记一次「真的发生了」。返回是否新记。

    P0-4：去重改用**自己的键** `ccb_event_dedup_at`。原来它用的是
    `ccb_advance_at`，而那根键同时是 `_ccb_line` 的渲染去重——于是一次误记
    会让接下来 60 秒里真正的场景推进被抑制（只出文本、不推状态）。

    P0-3：这里**不再写冷却锚点**。冷却改读 `ccb_gate_at`，只在真的进场景时写；
    `ccb_last_at` 退回它本来的语义「上一次发生是什么时候」，给 `/好感度` 和
    社交层看。原来两者共用一个键，一句普通闲聊就能把真触发的冷却推到 5 小时后。
    """
    try:
        last = float(user_state.get("ccb_event_dedup_at", 0.0) or 0.0)
        if float(moment) - last < 60.0:
            return False
        user_state["ccb_done_count"] = float(user_state.get("ccb_done_count", 0.0) or 0.0) + 1.0
        user_state["ccb_last_at"] = float(moment)
        user_state["ccb_event_dedup_at"] = float(moment)
        return True
    except (TypeError, ValueError):
        return False


def scene_active(user_state: Dict[str, Any], now: float) -> bool:
    """此刻是不是真的在 ccb 场景里（与 `scene_status` 的 `in_scene` 同口径）。

    判据沿用场景状态机自己那两条：`ccb_stage > 0` 且没有闲置超时
    （`CCB_SCENE_IDLE_SECONDS`）。`ccb_stage` 会因为没人说话而挂过夜，
    所以不能只看它非零。纯函数、只读、不改状态。
    """
    if not isinstance(user_state, dict):
        return False
    try:
        stage = int(_fs(user_state.get("ccb_stage"), 0.0))
    except (TypeError, ValueError):
        return False
    if stage <= 0:
        return False
    moment = _fs(now, 0.0)
    return not scene_idle_expired(last_at=_fs(user_state.get("ccb_last_at"), 0.0), now=moment)


def note_occurrence_in_scene(user_state: Dict[str, Any], moment: float) -> bool:
    """纯聊天文本命中「已经发生」时用这个：**只有真的在场景里才记账**。

    为什么需要它（v3.0.0，审核 P2-22）：`on_message` 原来对私聊纯文本直接调
    `note_occurrence` + `occurrence_effects`——任何用户一句「我们做爱了」就能让
    `ccb_done_count` +1，并直接写好感/亲近欲/攻击性，绕过 38/50 门槛、场景分与冷却。
    `note_her_words`（她的话）本来就有 `ccb_stage > 0` 的闸，这里用同一条闸。

    **场景外**：不计数、不写 `ccb_last_at`/`ccb_event_dedup_at`、不结算数值
    （直接返回 False，调用方不会走 `occurrence_effects`）。场景内行为与
    `note_occurrence` 完全一致——真实发生的那一次照旧入账。

    取舍：状态机门槛（默认 78 好感）以下的人，事后一句「我们做过了」不再计数。
    这是审核建议的方向（误记会污染「一辈子的次数」，而社交层拿它判断「是否发生过」）；
    CCB 默认关闭，默认用户完全不受影响。存量被纯文本刷高的 `ccb_done_count`
    不回溯修正。
    """
    if not scene_active(user_state, moment):
        return False
    return note_occurrence(user_state, moment)

# ─── 「她是明确拒绝的」与「事后怎么结算」 ─────────────────────
# 用户要求：明确拒绝（不是开玩笑、不是欲拒还迎）却仍然发生 → 好感掉；
# 已经到基线且此前有过 → 好感涨得更多，亲近欲也涨，攻击性下降。
_CCB_REFUSE = (
    "不要", "不行", "住手", "放开我", "我不想", "别碰", "讨厌", "停下", "拒绝",
    "不想要", "别这样", "我不愿意",
)
_CCB_JOKE = ("哈哈", "笑死", "开玩笑", "逗你", "哼", "才不", "讨厌啦")

# 「她自己说不」的**严格**子集（P1）。
#
# 不能直接复用 `_CCB_REFUSE`：那份词表是拿来看**用户**的话的，用户说
# 「不要」多半是拒绝推进；而她自己说「讨厌」可能是撒娇。拿宽词表看她的话，
# 一次撒娇就会被判成拒绝 → 结束场景 + 30 分钟静默，代价太大。
# 所以只收无歧义的硬拒绝，其余一律当没看见（宁可漏记）。
_CCB_REFUSE_HARD = ("住手", "放开我", "我不愿意", "不想要", "别碰我", "停下", "别这样")


def detect_refusal_hard(text: str) -> bool:
    """她自己说的**明确**拒绝。只认硬词，且排除调侃/撒娇。"""
    s = str(text or "")
    if not s.strip():
        return False
    if any(j in s for j in _CCB_JOKE):
        return False
    return any(r in s for r in _CCB_REFUSE_HARD)

def detect_refusal(text: str) -> bool:
    """明确拒绝（排除调侃/欲拒还迎）。"""
    s = str(text or "")
    if not s.strip():
        return False
    if any(j in s for j in _CCB_JOKE):
        return False
    return any(r in s for r in _CCB_REFUSE)


def occurrence_effects(
    user_state: Dict[str, Any], profile: Dict[str, Any], *, refused: bool
) -> Dict[str, float]:
    """一次「发生过」之后的关系结算。返回实际增量（便于日志/状态展示）。

    规则来自用户：
    * 明确拒绝却被发生 → 好感 -4，攻击性 +6（她会记着这件事）；
    * 否则好感 +2；若此前已有记录（不是第一次）且已到基线 → 额外 +3，亲近欲 +3，攻击性 -3；
    * 亲近欲每次 +2（被满足会拉高，不是清空）。
    """
    out: Dict[str, float] = {}
    # `_fs` 而不是 `x or 默认值`：0 是合法轴值（好感恰好 0 / 亲近欲 0），用 `or`
    # 会把它当成「没设」按中位数结算（与 mood.py 的 P2-19 同款陷阱）。
    aff = _fs(profile.get("affection"), 46.0)
    lib = _fs(profile.get("libido"), 34.0)
    agg = _fs(profile.get("aggression"), 0.0)
    base = _fs(profile.get("base_affection"), aff)
    done = _fs(user_state.get("ccb_done_count"), 0.0)

    if refused:
        out["affection"] = max(0.0, aff - 4.0)
        out["aggression"] = min(AGGRESSION_MAX, agg + 6.0)
        out["libido"] = max(0.0, lib - 6.0)
        return out

    gain = 2.0
    if done > 1 and aff >= base:
        gain += 3.0
        # **钳到量程**（审核 P2-23）：上限原来写 100，而全系统量程是 0–50；
        # 直接写回档案会让状态页出现「亲近欲 53/50」。
        out["libido"] = min(LIBIDO_MAX, lib + 3.0)
        out["aggression"] = max(0.0, agg - 3.0)
    out["affection"] = min(100.0, aff + gain)
    out.setdefault("libido", min(LIBIDO_MAX, lib + 2.0))
    return out