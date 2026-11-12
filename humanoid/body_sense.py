"""把亲密的内部参数翻译成**体感**（P1）。

为什么需要这一层
----------------
`ccb` 里存的是参数：阶段号、满足度、姿势枚举、探入位置、估算深度、深度上限、
敏感度、体力、余韵窗。它们回答的是「到哪一步了」「能不能进」，不回答
「现在身上是什么感觉」。

注入里原来只有三处参数被翻译过：满足度三档、`emotion._SCENE_FEELINGS` 六档、
余韵一句。剩下的（姿势、位置、深度、节奏、敏感、体力、依恋）全部躺在状态里
没进上下文——模型拿不到就只能自己编，编出来的和状态机对不上，用户看到的就是
「场景是个摆件」。这一层补的就是这段翻译。

三条纪律（PLAN-embodied-v2 §4.4）
------------------------------
1. 只说感觉与四足也有的部位（腰、腿、小腹、胸口、后背、喉咙、膝、蹄），
   不写手指、手掌、手腕、小臂、领口——那些她身上没有。
2. 不给动作指令、不写情节、不示范排版——它是一句状态，怎么回应由模型判断。
3. 不给读数、不给档位名（不出现「满足 85/100」「沉醉」「12cm」）。

范围
----
`feelings()/aftercare()/cycle_line()` 是**稳态**：同一时刻身上是什么样。
`delta()` 是**变化**：比上一轮更胀、更难忍、更没力气。变化句比状态句更容易被
模型当成「正在发生的事」，所以两者分开：稳态由词表给，变化由上一轮快照对比给。
上一轮快照存在用户档案的 `HISTORY_KEY` 下（`remember()` 写、`previous()` 只读），
值没变就不写、不标脏——同一状态连着两轮不会冒出「比刚才更…」。

词表主语
--------
词表里一律写「她」：渲染出口 `prompt_builder.to_first_person` 统一换「我」，
和别的行级函数同一约定（行级「她」、出口「我」）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

from . import ccb as _ccb
from . import state_snapshot as _snapshot

# 上一轮快照落在用户档案里的键。**不复用 ccb 的状态键**：那些是状态机的输入，
# 这个只是「上次渲染时我看到的样子」，删掉它不影响任何判定。
HISTORY_KEY = "body_sense"

# 变化阈值：低于这些就当没变。同一次场景里参数抖动一点就报「比刚才更…」，
# 读起来像卡顿的仪表盘，而不是身体在变。
_DEPTH_EPS = 0.6      # 厘米
_SATISFY_EPS = 12.0   # 满足度
_ENERGY_EPS = 8.0     # 体力

# 场景里最多给几句。三句是上限：再多就盖过「关系/情绪」那些更该说的信息，
# 而且每句都要占 token 预算。
MAX_SCENE_PHRASES = 3

# 姿势 → 角度上的感觉。索引对齐 `ccb.POSE_NAMES`（0 后入 / 1 前入 / 2 坐姿 / 3 躺姿）。
# 都只说身体的受力与松紧，不说体位名——体位名是分类，不是感觉。
_POSE_FEELINGS = {
    0: "背后贴着，腿有点合不拢",
    1: "被压着，腰有点悬",
    2: "坐着，膝盖有点发软，重心不太稳",
    3: "躺着，腰底下垫着，使不上劲",
}

# 探入位置（`ccb.SITE_NAMES`：0 前面 / 1 后面）。只给一句胀感差异，不写入口。
_SITE_FEELINGS = {1: "后面胀得有点慌"}

# 阶段 → 呼吸（索引对齐 `ccb.PHASE_NAMES` 的 1..4）。
_BREATH_FEELINGS = {
    1: "呼吸有点乱",
    2: "喘得有点急",
    3: "喘得说不出整句",
    4: "喘不上来，脑子一片白",
}

# 生理周期 8 个阶段 → 身体/情绪的感觉（索引对齐 `config.cycle_phase_index`）。
# 只给感觉，不改任何数值（用户拍板）。阶段名（月经期/排卵期…）一个都不出现：
# 那是分类，模型读到会去解释分期，而不是体会今天的身体。
# 每条都自带主语「我」与时间词：这几句不带「她」，`to_first_person` 换不到它们，
# 少了主语就成了「人挺有精神，皮肤也顺眼」这种无主句，模型读不出这是自己的身体。
#
# 每个阶段三句、按天轮换（v3.0.10）：原来一个阶段只有一句，而月经期横跨五天——
# 同一句话连着五天、每轮都递过去，模型就把它当台词念（现场：「小腹坠着，腰也酸」
# 被逐字复述，一天出现几十次）。同一天之内必须稳定，换天才换措辞：
# 句子的作用是她今天的身体，不是一句可以照抄的旁白。
_CYCLE_FEELING_SETS = (
    (
        "我这两天小腹坠着，腰也酸，人有点发懒",
        "我这会儿小腹一阵阵地坠，腰发沉，坐着都不太想动",
        "我身上没什么力气，小腹隐隐地抽着，腰也直不太起来",
    ),
    (
        "我这两天身子轻快起来了，心情也比前几天松",
        "我这两天缓过来了，身上不那么沉，心里也松快",
        "我这几天人渐渐回血，走路都比前几天轻",
    ),
    (
        "我这几天人挺有精神，皮肤也顺眼",
        "我这两天精神头不错，皮肤也透着点亮",
        "我这几天脑子转得快，人也显得好看些",
    ),
    (
        "我这几天有点想要人陪，也更容易被撩动",
        "我这两天心里有点发软，容易被人一句好话勾住",
        "我这几天特别想有人挨着，心思也比平时活",
    ),
    (
        "我这两天身上最热，靠得近了会心跳快一点",
        "我这两天体温高一点，一侧小腹隐隐发酸",
        "我这两天身上燥，靠太近会有点心慌",
    ),
    (
        "我这两天胸口有点胀，脾气比平时短",
        "我这几天胸口发胀，一点小事就容易烦",
        "我这两天身上发紧，说话比平时冲一点",
    ),
    (
        "我这几天人有点沉，总想吃东西，也容易累",
        "我这两天总犯馋，走两步就觉得累",
        "我这几天身上沉，胃口比平时大，睡也不太够",
    ),
    (
        "我这两天情绪起落大，小腹又开始隐隐地坠",
        "我这几天心里忽上忽下，小腹又开始坠了",
        "我这两天容易烦躁，身上也提前有了那种坠感",
    ),
)

# 每个阶段的第一句。面板/契约/旧断言仍按这一份读，措辞不变。
_CYCLE_FEELINGS = tuple(choices[0] for choices in _CYCLE_FEELING_SETS)

# 「比刚才…」的六个方向（P3）。集中在这里，是为了让边界守卫能一次遍历全部，
# 而不是去 grep 函数体里的字符串字面量。
_DELTA_HARDER = "比刚才更难忍"
_DELTA_SURGE = "刚才那阵又往上涌了一点"
_DELTA_DEEPER = "比刚才更胀，进去得更深了一点"
_DELTA_TIRED = "比刚才更没力气"
_DELTA_EASING = "缓下来一点"
_DELTA_WITHDRAW = "退出去一点，空了一下"
_DELTA_PHRASES = (
    _DELTA_HARDER, _DELTA_SURGE, _DELTA_DEEPER,
    _DELTA_TIRED, _DELTA_EASING, _DELTA_WITHDRAW,
)

# 余韵窗的三档（按还剩多久）。窗口长度来自配置，这里只按比例分档。
_AFTERCARE_EARLY = "身上还热着，腰有点酸，不太想动"
_AFTERCARE_MID = "缓过来一些了，腿还有点软"
_AFTERCARE_LATE = "差不多缓过来了，就是有点困"


def _num(value: Any, default: float = 0.0) -> float:
    """把可能来自存档/配置的值读成 float；坏值给默认。"""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out or out in (float("inf"), float("-inf")):  # NaN / inf
        return default
    return out


@dataclass(frozen=True)
class Sense:
    """某一刻的体感来源：全是**已经读出来**的标量，不再回头访问 core。

    `collect()` 负责读、`feelings()` 负责翻词——这样翻词部分可以在测试里
    直接构造 Sense，不用搭一整套 core。
    """

    now: float
    phase: int
    stage: float
    satisfy: float
    pose: int
    site: int
    depth_cm: float
    cap_cm: float
    sensitivity: float
    energy: float
    rel_stage: int
    activity_at: float
    aftercare_until: float


def collect(core: Any, user_id: str, now: Optional[float] = None) -> Optional[Sense]:
    """读一份体感来源。**纯读**：不建档、不落盘、不推进任何状态。

    拿不到就返回 None（没有情绪档案 / 没有 scope / 坏值），调用方按「这一层不
    说话」处理——少一句体感远好过整块注入消失。

    注意这里**不调** `core.ccb_body()` / `core.ccb_partner()`：那两个在缺档案时
    会建档写盘，而这是每轮都会跑的读路径。取不到就退回配置值。
    """
    if not user_id:
        return None
    scope = getattr(core, "_scope", None)
    if scope is None:
        return None
    try:
        profile = scope.peek_user(user_id, "mood")
    except Exception:
        return None
    if not isinstance(profile, dict) or not profile:
        return None
    try:
        moment = _num(core.now_epoch()) if now is None else _num(now)
    except Exception:
        return None
    if moment <= 0:
        return None

    stage = _num(scope.peek_user(user_id, "ccb_stage", 0.0))
    satisfy = _num(scope.peek_user(user_id, "ccb_satisfy", 0.0))
    aftercare_until = _num(scope.peek_user(user_id, "ccb_aftercare_until", 0.0))
    phase = int(_ccb.phase_of(
        satisfy=satisfy, stage=stage, aftercare_until=aftercare_until, now=moment,
    ))
    pose = int(_num(scope.peek_user(user_id, "ccb_pose", float(_ccb.POSE_UNKNOWN))))
    site = int(_num(scope.peek_user(user_id, "ccb_site", float(_ccb.POSE_UNKNOWN))))
    activity_at = _num(scope.peek_user(user_id, "ccb_activity_at", 0.0))

    # 长度 / 上限 / 敏感度：先读**已经建档**的那份，读不到退回配置。
    cfg = getattr(core, "config", None)
    length_cm = _num(getattr(cfg, "ccb_partner_length_cm", 15.0), 15.0)
    try:
        partner = scope.peek_user(user_id, _ccb.BODY_LENGTH_KEY)
        if isinstance(partner, dict) and partner.get("length_cm") is not None:
            length_cm = _num(partner.get("length_cm"), length_cm)
    except Exception:
        pass
    cap_cm = _num(getattr(cfg, "ccb_body_depth_cap_cm", 14.0), 14.0)
    sensitivity = _num(getattr(cfg, "ccb_body_sensitivity", 60.0), 60.0)
    try:
        body = getattr(scope, "self_state", {})
        body = body.get(_ccb.BODY_KEY) if isinstance(body, dict) else None
        if isinstance(body, dict):
            if body.get("depth_cap_cm") is not None:
                cap_cm = _num(body.get("depth_cap_cm"), cap_cm)
            if body.get("sensitivity") is not None:
                sensitivity = _num(body.get("sensitivity"), sensitivity)
    except Exception:
        pass
    depth_cm = _ccb.depth_estimate(length_cm, phase) if length_cm > 0 else 0.0

    energy = 100.0
    try:
        energy = _num(getattr(core, "energy", None).energy, 100.0)
    except Exception:
        energy = 100.0

    rel_stage = 0
    try:
        relation = _snapshot.relation_of(profile, moment, tz=_snapshot.tz_of(core))
        rel_stage = int(relation.stage_id)
    except Exception:
        rel_stage = 0

    return Sense(
        now=moment,
        phase=phase,
        stage=stage,
        satisfy=satisfy,
        pose=pose,
        site=site,
        depth_cm=depth_cm,
        cap_cm=cap_cm,
        sensitivity=sensitivity,
        energy=energy,
        rel_stage=rel_stage,
        activity_at=activity_at,
        aftercare_until=aftercare_until,
    )


def _fullness(sense: Sense) -> str:
    """充盈：估深 / 上限的比例分档；过深单独一句（它同时是「边界」那条轴）。"""
    if sense.depth_cm <= 0:
        return ""
    if sense.cap_cm > 0 and sense.depth_cm > sense.cap_cm:
        return "被撑得发胀，深处有点酸"
    ratio = sense.depth_cm / sense.cap_cm if sense.cap_cm > 0 else 0.0
    if ratio >= 1.0:
        return "填得很满，胀得发酸"
    if ratio >= 0.85:
        return "顶得有点深，腰有点软"
    if ratio >= 0.6:
        return "填得满满的，有点胀"
    return "有点胀，还没完全适应"


def _angle(sense: Sense) -> str:
    """角度与位置：姿势决定身体怎么受力，位置决定胀在哪儿。"""
    bits: List[str] = []
    phrase = _POSE_FEELINGS.get(sense.pose, "")
    if phrase:
        bits.append(phrase)
    phrase = _SITE_FEELINGS.get(sense.site, "")
    if phrase:
        bits.append(phrase)
    return "，".join(bits)


def _pace(sense: Sense) -> str:
    """节奏：这一场最后一次有动静离现在多久。不是读数，是「还急不急」。"""
    if sense.activity_at <= 0 or sense.stage <= 0:
        return ""
    gap = sense.now - sense.activity_at
    if gap < 0:
        return ""
    if gap < 60:
        return "动静还没停，心跳得厉害"
    if gap < 300:
        return "刚缓下来一点，还在发颤"
    return "缓下来了，身上还热着"


def _sensitive(sense: Sense) -> str:
    """敏感：面板上那一档 0..100 翻成「碰一下会怎样」。"""
    if sense.sensitivity <= 0:
        return "身上钝钝的，不太有感觉"
    if sense.sensitivity >= 80:
        return "一碰就发麻"
    if sense.sensitivity >= 50:
        return "碰一下就缩"
    if sense.sensitivity >= 25:
        return "比平时敏感"
    return ""


def _stamina(sense: Sense) -> str:
    """肌肉与疲惫：体力见底时腿撑不住，这一条比读数有用。"""
    if sense.energy < 25:
        return "腿在抖，有点撑不住"
    if sense.energy < 45:
        return "腰背有点酸，力气在往下掉"
    return ""


def _shame(sense: Sense) -> str:
    """羞耻：关系还很浅又已经在场景里，身体会绷着。关系深了就不写这一句。"""
    if sense.rel_stage >= 2:
        return ""
    if sense.pose < 0 and sense.site < 0:
        return ""
    return "脸有点烧，不太敢看"


def _attachment(sense: Sense) -> str:
    """依恋：关系够深时，场景里想要的是贴着而不是「做完」。"""
    if sense.rel_stage >= 2 and sense.phase >= 3:
        return "想被抱紧一点，不想松开"
    return ""


def feelings(sense: Optional[Sense], limit: int = MAX_SCENE_PHRASES) -> List[str]:
    """场景内的体感短句，按信息量排序，最多 `limit` 句。

    排序理由：充盈最先（它由深度/上限/阶段共同决定，是最有信息量的一条），
    其次角度与位置（决定身体怎么受力），然后节奏、呼吸、敏感、体力、羞耻、依恋。
    超出 `limit` 的丢掉——预算要留给关系与情绪。
    """
    if sense is None or sense.stage <= 0:
        return []
    out: List[str] = []
    for phrase in (
        _fullness(sense),
        _angle(sense),
        _pace(sense),
        _BREATH_FEELINGS.get(sense.phase, ""),
        _sensitive(sense),
        _stamina(sense),
        _shame(sense),
        _attachment(sense),
    ):
        if phrase and phrase not in out:
            out.append(phrase)
    if limit > 0:
        return out[:limit]
    return out


def aftercare(sense: Optional[Sense]) -> str:
    """余韵窗里的体感：按还剩多久分三档，不给「还剩 N 分钟」。"""
    if sense is None:
        return ""
    left = sense.aftercare_until - sense.now
    if left <= 0:
        return ""
    if left > 3600:
        return _AFTERCARE_EARLY
    if left > 900:
        return _AFTERCARE_MID
    return _AFTERCARE_LATE


def cycle_line(core: Any) -> str:
    """生理周期当前阶段的一句感觉。关掉周期功能 / 读不到就返回空串。

    只翻词，不改任何数值：energy、libido 的调制仍在各自原来的地方算，
    这里只负责让模型知道「今天身上是什么感觉」。
    """
    cfg = getattr(core, "config", None)
    if not bool(getattr(cfg, "enable_cycle", False)):
        return ""
    scope = getattr(core, "_scope", None)
    if scope is None:
        return ""
    try:
        day = int(scope.get_self("current_cycle_day", 1) or 1)
    except Exception:
        return ""
    try:
        index = int(cfg.cycle_phase_index(day))
    except Exception:
        return ""
    if not (0 <= index < len(_CYCLE_FEELING_SETS)):
        return ""
    choices = _CYCLE_FEELING_SETS[index]
    # 按天轮换：同一天里所有请求拿到同一句（否则一轮一个说法，比重复更糟），
    # 换天换一句。阶段横跨好几天时，句子才不会连着五天一字不变。
    # 用 (day - 1) 而不是 day：周期第 10 天必须仍落在各阶段的第一句上——
    # 那是这套句子的原始措辞，注入快照与验收用例都以它为基准。
    return choices[(int(day) - 1) % len(choices)]


def _snapshot_of(sense: Sense) -> dict:
    """要记住的最小字段集：只留会用来算变化的四个量加阶段。

    存 float/int 而不是整个 `Sense`：存档是 JSON，多一个字段就多一份要维护的
    形状，而这几个数字是唯一被 `delta()` 读的。
    """
    return {
        "stage": round(float(sense.stage), 3),
        "depth_cm": round(float(sense.depth_cm), 2),
        "phase": int(sense.phase),
        "satisfy": round(float(sense.satisfy), 2),
        "energy": round(float(sense.energy), 2),
    }


def previous(core: Any, user_id: str) -> Optional[dict]:
    """**只读**：上一轮渲染时的快照；没有就 None（不因为读而建档）。"""
    scope = getattr(core, "_scope", None)
    if scope is None:
        return None
    try:
        raw = scope.peek_user(user_id, HISTORY_KEY, None)
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def remember(core: Any, user_id: str, sense: Optional[Sense]) -> None:
    """把这一轮的快照写进用户档案。**值没变就不写、不标脏**。

    写盘时机：只在真的变了的时候标脏。注入是每轮都跑的，如果每轮都标脏，
    落盘队列会被一次聊天刷满，而内容一个字都没变。
    """
    if sense is None:
        return
    scope = getattr(core, "_scope", None)
    if scope is None:
        return
    snap = _snapshot_of(sense)
    if previous(core, user_id) == snap:
        return
    try:
        scope.user_state(user_id)[HISTORY_KEY] = snap
        scope.mark_dirty()
    except Exception:
        return


def delta(sense: Optional[Sense], prev: Optional[dict]) -> str:
    """「比刚才…」一句，最多一句；没有值得说的变化就返回空串。

    只报一个方向（最明显的那个），因为变化句是补在状态句后面的，两句并列会变成
    体检报告。顺序按「对当下的影响」排：更难忍 → 又涌上来 → 更胀 → 更没力气 →
    缓下来 → 退出去一点。
    """
    if sense is None or not isinstance(prev, dict):
        return ""
    # 不在场景里不报变化：那时候「更胀」没有意义，余韵句已经说了身体的状态。
    if sense.stage <= 0:
        return ""
    # 刚进场景：状态句已经说了「正在亲密里（…）」，再补「比刚才更…」是在跟谁比？
    if _num(prev.get("stage"), 0.0) <= 0:
        return ""
    try:
        prev_phase = int(prev.get("phase") or 0)
    except (TypeError, ValueError):
        prev_phase = 0
    prev_depth = _num(prev.get("depth_cm"), 0.0)
    prev_satisfy = _num(prev.get("satisfy"), 0.0)
    prev_energy = _num(prev.get("energy"), 0.0)
    if sense.phase > prev_phase and sense.phase >= 2:
        return _DELTA_HARDER
    if sense.satisfy - prev_satisfy >= _SATISFY_EPS:
        return _DELTA_SURGE
    if sense.depth_cm - prev_depth >= _DEPTH_EPS:
        return _DELTA_DEEPER
    if prev_energy - sense.energy >= _ENERGY_EPS:
        return _DELTA_TIRED
    if prev_satisfy - sense.satisfy >= _SATISFY_EPS:
        return _DELTA_EASING
    if prev_depth - sense.depth_cm >= _DEPTH_EPS:
        return _DELTA_WITHDRAW
    return ""
