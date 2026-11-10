"""情绪的表达层：把三轴数值翻译成她此刻的样子。

分工：`mood.py` 管数值与记忆，这里管**怎么说**。

两条硬规矩，都是踩过坑之后定下来的：

1. **可以说心里话，但要用日常词。** 「心里有点堵」是人话，「心里压着一股气没处发」
   是病历——后者会被模型原样复读，于是每句话都像在念诊断书。这是「不拟人」的主要
   来源，不是「写了心理」这件事本身。

2. **每轮最多出一条。** 候选做得大是为了选择余地大，不是为了让句子变多。同一时刻给
   三四条，模型会当成要复述的清单——那正是「一句话里塞了七件事」的老毛病。

输出格式是 `（她的内心：……）`：括号是「隐藏状态」的信号（不是要说出来的话），
`名字的内心` 把名字绑成第一人称的潜意识而不是旁白里的第三方。
"""

from __future__ import annotations
from . import ccb as _ccb
from . import state_snapshot as _snap
# 情绪记忆的有效期只有一份（审核 F10）：以前 7 天只写在 mood 的写入侧剪枝里，
# 读取侧（_memory_note）不判时间，「7 天有效期」实际变成「下次再记一笔时才生效」。
from .services.mood import EMOTION_MEMORY_DAYS
# 时间换算只许有一处实现（R5）：这里原来自己 `datetime.fromtimestamp(ts, tz)`，
# 与 clock.local 是同一件事的两份写法——换时区/换城市后各处各算一天。
from .clock import local as _clock_local
import hashlib
import math

from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger

# 门槛：情绪要**高于初始值**才出现。初始值读配置（默认 affection 35 / libido 34 /
# aggression 28），门槛压着初始值时，这些话就是常驻背景而不是异常信号。
# 这里以前把 46 / 34 / 28 写死成「初始值」，而面板默认是 35 / 34 / 28——注释和代码
# 说的不是同一个数（审核 注入生成 P-10）。默认值现在只有 state_snapshot 一份。
SHIFT_HIGH = 9.0
SHIFT_LOW = -7.0
AGGRESSION_UP = 7.0
# 亲近欲的门槛必须**比基线高**多少，而不是绝对值：初始亲近欲就是 34，
# 拿绝对值 8 当门槛等于「永远满足」。
LIBIDO_UP = 8.0
CARE_CLOSE = 0.62

# 情绪记忆的门槛：单次不算（那叫记仇），重复且近期才算（那叫在意）。
# v2.27.8（B4）：只按「同一天里 ≥2 次」判定——单次负向不再立刻亮「窝火」。
GRUDGE_SAME_DAY = 2

# ccb 推进状态的去重窗口（秒）：同一条用户消息可能触发多次 LLM 请求（工具循环/重试），
# 而 ccb 是在**读路径**（build_injection）里推进的，不去重的话一条消息会被算成几次
# （当日次数、一辈子的次数都重复计，ccb_satisfy 还会一步跨几格）。
CCB_ADVANCE_DEDUP_SECONDS = 60.0

# 注入里出现的「阶段」只给感受词，不给档位名（#62）。索引就是 `ccb.phase_of()` 的
# 返回值：0=不在场景、1=起步、2=中间、3=接近顶点、4=顶点、5=余韵。边界由
# `ccb.CCB_SATISFY_STEP` / `CCB_SATISFY_FULL` 决定，这里**只翻词、不写阈值**——
# 所以 #12 改 `PHASE_NAMES` 的字面量（面板用的内部档位名）不会影响注入。
# 长度必须与 `ccb.PHASE_NAMES` 对齐：`_scene_feeling` 用它做边界，
# `tests/test_v304_b5_body.py` 里有守卫用例。
_SCENE_FEELINGS: Tuple[str, ...] = (
    # 每一条都**不能**包含 `PHASE_NAMES` 里的词（「动情/沉醉/临界/爆发/余韵」）：
    # 守卫用例按子串查，写成「有点动情」会让「动情」照样漏进上下文。
    "", "心跳有点快", "正舒服", "快忍不住了", "到了", "还没缓过来",
)


def _scene_feeling(phase: int) -> str:
    """相位编号 → 她自己的感受。0 / 越界 / 非数 → 空串（宁可不说，也不说档位名）。"""
    try:
        idx = int(phase)
    except (TypeError, ValueError):
        return ""
    if idx <= 0 or idx >= len(_ccb.PHASE_NAMES) or idx >= len(_SCENE_FEELINGS):
        return ""
    return _SCENE_FEELINGS[idx]


# 注意：调用点一律写 `getattr(self, "_core", None)`，不要直接 `self._core`。
# 这几个方法只依赖 `core.config`，而测试会用 `EmotionLayer.__new__` 直接构造对象来
# 单独验纯函数（不跑 __init__，也就没有 _core）；直接取属性会 AttributeError。
def _seed_affection(core: Any) -> float:
    return _snap.affection_default(getattr(core, "config", None))


def _seed_libido(core: Any) -> float:
    return _snap.libido_default(getattr(core, "config", None))


def _seed_aggression(core: Any) -> float:
    return _snap.aggression_default(getattr(core, "config", None))


def _num(value: Any, default: float = 0.0) -> float:
    """取值：拿不到或不是有限数（NaN/±Inf）就退回默认值。

    NaN 必须挡在这里而不是放行：`nan >= 阈值` 全是假，于是「该亮的情绪不亮」；
    更糟的是**该过期的不过期**——`_cooling_line` 里 `now >= nan` 为假，冷静期
    会永远挂着，她的每一轮都只剩「还在为那件事烦着」。inf 同理。
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _pick(texts: Tuple[str, ...], index: int) -> str:
    return texts[max(0, index) % len(texts)]


def ccb_coerced_within(
    user_state: Any, moment: float, window: float = 3600.0
) -> bool:
    """这个人是不是**刚刚**被强迫过（`window` 秒内）。

    `ccb_coerced_at` 有两个可能的位置，必须都认：

    * `users.<uid>` 顶层——未来迁移后的位置，也是这里优先读的；
    * `users.<uid>.mood` 子字典——**当前写侧实际写的就是这里**
      （`services/mood.py::_apply_intimacy_request` 拿到的 `record` 就是那份 mood dict）。

    以前只读顶层，两侧永不相等，于是「刚被强迫 → 本轮不进场景」这道安全阀恒为 False。
    这是安全阀，不是功能开关：读错方向时它不会报错，只会静默失效。
    """
    if not isinstance(user_state, dict):
        return False
    raw = user_state.get("ccb_coerced_at")
    if raw is None:
        mood_state = user_state.get("mood")
        if isinstance(mood_state, dict):
            raw = mood_state.get("ccb_coerced_at")
    if raw is None:
        return False
    try:
        stamp = float(raw)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(stamp) or stamp <= 0:
        return False
    return (float(moment) - stamp) < float(window)


class EmotionLayer:
    """按维度产出候选，带显著度，最后由调用方取最高的那一条。"""

    def __init__(self, core: Any) -> None:
        self._core = core

    # ------------------------------------------------------------------

    def _today_events(self, user_id: str) -> Tuple[int, int]:
        """今天正/负向波动各几次。返回 (正, 负)。"""
        try:
            today = self._core.clock.today_str()
            entries = self._core.mood.logs(user_id, limit=60)
        except Exception:
            return 0, 0
        ups = downs = 0
        for entry in entries or []:
            # 损坏的 state 里可能混进非字典条目。没有这条守卫的话 `entry.get` 抛
            # AttributeError，冒到 `prompt_builder._emotion_line` 的宽 except 被吞，
            # 于是这个用户的整句「内心」**永久消失**——而且不会有人发现。
            if not isinstance(entry, dict):
                continue
            if not str(entry.get("time", "")).startswith(today):
                continue
            # **逐轴判正负**：攻击性这一轴的词面与情感是反的——「攻击性上升」是她更生气
            # （负向），「攻击性下降」是气消了（正向）。原来按「上升/下降」字符串猜：
            # 一条发怒被读成心情好；而甜话把攻击性压下来的那 0.3 分又被读成负向，
            # 于是 `_attitude` 会在**好好聊了一天**之后说「上回那句话到现在都还窝火」。
            # 日志喂饱之后（v2.28.9 修掉饿死后）这一点尤其致命。
            for segment in str(entry.get("event", "")).split("，"):
                rising = "上升" in segment
                falling = "下降" in segment
                if not rising and not falling:
                    continue
                good = falling if "攻击性" in segment else rising
                if good:
                    ups += 1
                else:
                    downs += 1
        return ups, downs

    # ------------------------------------------------------------------
    # 五个维度
    # ------------------------------------------------------------------

    def _day_tone(self, user_id: str, index: int) -> Tuple[float, str]:
        """今天的底色。按当天的波动次数算，所以一天之内不翻来覆去。

        「一会儿好一会儿不好」要求正负各 ≥2 次（v2.27.2）：原来各 1 次就亮，
        而日志阈值只有 2 分、单条消息都能触发——一句玩笑话加一句抱怨，
        当天底色就变成「说不上来」。正负各两次以上，才真是反复的一天。
        （v2.28.9 起日志按同一自然日的累计幅度触发，不再被单条阈值饿死；
        正负的判定见 `_today_events`——「攻击性上升」算负向。）
        """
        ups, downs = self._today_events(user_id)
        if downs >= 2 and ups == 0:
            return 0.86, _pick(("今天一整天都不太顺", "今天整个人蔫蔫的"), index)
        if downs == 1 and ups == 0:
            return 0.62, _pick(("今天有点闷", "今天心里堵得慌"), index)
        if downs >= 2 and ups >= 2:
            return 0.55, _pick(("今天一会儿好一会儿不好", "今天这心情说不上来"), index)
        # 单次互有：不出「说不上来」，用轻量版的当天基调。
        if downs >= 1 and ups >= 1:
            return 0.45, _pick(("今天有点起有落", "今天平平常常"), index)
        if ups >= 2:
            return 0.80, _pick(("今天心情不错", "今天挺来劲的"), index)
        if ups == 1:
            return 0.50, _pick(("今天心情还行", "今天挺轻松的"), index)
        return 0.0, ""

    def _now_mood(self, profile: Dict[str, Any], index: int) -> Tuple[float, str]:
        """此刻的心气。三轴相对基线动了多少。"""
        aff = _num(profile.get("affection"), _seed_affection(getattr(self, "_core", None)))
        base_aff = _num(profile.get("base_affection"), aff)
        libido = _num(profile.get("libido"), _seed_libido(getattr(self, "_core", None)))
        base_lib = _num(profile.get("base_libido"), libido)
        aggr = _num(profile.get("aggression"), _seed_aggression(getattr(self, "_core", None)))
        base_aggr = _num(profile.get("base_aggression"), aggr)
        eager = libido - base_lib >= LIBIDO_UP

        # 这一组**不带主语**：bracket 会补上「她的内心：」，带了主语就成了
        # 「（她的内心：她心里有点堵。）」——同一个主语说两遍。
        grudge = aggr - base_aggr >= AGGRESSION_UP
        if grudge and eager:
            return 0.90, _pick(("心里两个念头拧着", "又气又放不下"), index)
        if grudge:
            return 0.88, _pick(("心里有点堵", "有点不想搭理你"), index)
        if aff - base_aff >= SHIFT_HIGH:
            return 0.72, _pick(("心里有点软", "现在看什么都顺眼"), index)
        if aff - base_aff <= SHIFT_LOW:      # 跌了 SHIFT_LOW 分以上
            return 0.70, _pick(("心里空落落的", "有点提不起劲"), index)
        if libido - base_lib >= LIBIDO_UP * 2:
            return 0.60, _pick(("心里老想着你", "总在琢磨你的事"), index)
        return 0.0, ""

    def _attitude(self, profile: Dict[str, Any], user_id: str, index: int) -> Tuple[float, str]:
        """对你的态度。含「记着上次那件事」这一类。"""
        ups, downs = self._today_events(user_id)
        aff = _num(profile.get("affection"), _seed_affection(getattr(self, "_core", None)))
        base_aff = _num(profile.get("base_affection"), aff)
        aggr = _num(profile.get("aggression"), _seed_aggression(getattr(self, "_core", None)))
        base_aggr = _num(profile.get("base_aggression"), aggr)

        if downs >= GRUDGE_SAME_DAY:
            return 0.92, _pick(("上回那句话到现在都还窝火", "上次那事我还记着"), index)
        if aggr - base_aggr >= AGGRESSION_UP:
            return 0.85, _pick(("今天不太想搭理你", "今天说话不太想接"), index)
        if aff - base_aff >= SHIFT_HIGH * 1.5:
            return 0.78, _pick(("今天你说什么我都愿意听", "今天特别想跟你待着"), index)
        if base_aff - aff >= abs(SHIFT_LOW) * 1.5:   # 跌幅够大才算生疏
            return 0.76, _pick(("今天跟你说话有点生分", "今天有点想躲着你"), index)
        if ups >= 2:
            return 0.62, _pick(("今天你做什么我都觉得挺好", "今天挺想跟你聊两句"), index)
        return 0.0, ""

    def _intimacy(self, interest: Dict[str, Any], profile: Dict[str, Any], index: int) -> Tuple[float, str]:
        """她愿意说多少。关系亲密度 × 当天基调。

        ## 没有信号就**不出声**，不要拿填充句占位（v2.27.42）

        原来最后一条兜底返回「今天跟平时一样」/「今天跟你说话挺正常的」，分数 0.45。
        它的本意是「没什么特别」，但它在提示词里长得和别的情绪一模一样：

            她被心里的事缠着       ← 真信号
            今天跟你说话挺正常的   ← 填充句

        模型分不出这两种。它读到后者，就会把它当成「她此刻的内心」照演——于是每一轮
        都有一句毫无来由的情绪陈述，角色扮演时最容易演化成「毫无来由的亲密/关心」，
        用户的原话是「情绪参考过于虚假，导致角色扮演类模型会虚假互动」。

        更糟的是它对**从没说过话的人**也这么写：`interest` 缺 `care` 时按 0.5 兜底，
        一路掉进最后那条，于是 `turn_count=0` 的用户也会看到「今天跟你说话挺正常的」
        ——一句关于不存在对话的评价。

        现在只在**真有可读信号**时出这一维：care 明确高/低；读不到 care 就不猜。
        """
        try:
            care = float(interest.get("care"))
        except (TypeError, ValueError):
            return 0.0, ""
        aff = _num(profile.get("affection"), _seed_affection(getattr(self, "_core", None)))
        base_aff = _num(profile.get("base_affection"), aff)
        aggr = _num(profile.get("aggression"), _seed_aggression(getattr(self, "_core", None)))
        base_aggr = _num(profile.get("base_aggression"), aggr)
        upset = (aggr - base_aggr >= AGGRESSION_UP) or (aff - base_aff <= SHIFT_LOW)

        if care >= CARE_CLOSE and not upset:
            return 0.66, _pick(("愿意多说两句", "愿意说点私人的"), index)
        if care >= CARE_CLOSE and upset:
            return 0.68, _pick(("不太想多说", "今天不太想说真心话"), index)
        if care < 0.25:
            return 0.55, _pick(("跟你还有点距离", "不太愿意提私事"), index)
        return 0.0, ""

    def _willingness(self, agency: Dict[str, Any], index: int) -> Tuple[float, str]:
        """主动性：她想不想开话头。"""
        try:
            value = _num(agency.get("initiative"), 0.0)
        except Exception:
            return 0.0, ""
        if value >= 0.70:
            return 0.68, _pick(("这会儿挺想说点什么", "话匣子是开着的"), index)
        if value >= 0.45:
            return 0.52, _pick(("有点想说话", "这会儿有话想说"), index)
        if value > 0.0:
            return 0.40, _pick(("不太想开话头", "这会儿没什么特别想说的"), index)
        return 0.0, ""

    def _cycle_phase(self) -> int:
        """当前生理周期阶段（0 月经 / 4 排卵）。读不到按 -1（不调制）。"""
        try:
            day = int(self._core._scope.get_self("current_cycle_day", 1) or 1)
            return int(self._core.config.cycle_phase_index(day))
        except Exception:
            return -1

    def _extra_moods(self, profile: Dict[str, Any], index: int) -> List[Tuple[float, str]]:
        """C1 新增维度：委屈 / 害羞 / 吃醋 / 烦 / 开心；C4 生理周期参与。

        显著度刻意压在既有五维之下——它们是「没什么大事、但心里有点东西」的底色，
        不该盖过「被惹了」「很在意你」这些更重的信号。
        """
        aff = _num(profile.get("affection"), _seed_affection(getattr(self, "_core", None)))
        base_aff = _num(profile.get("base_affection"), aff)
        libido = _num(profile.get("libido"), _seed_libido(getattr(self, "_core", None)))
        base_lib = _num(profile.get("base_libido"), libido)
        aggr = _num(profile.get("aggression"), _seed_aggression(getattr(self, "_core", None)))
        base_aggr = _num(profile.get("base_aggression"), aggr)
        d_aff = aff - base_aff
        d_lib = libido - base_lib
        d_aggr = aggr - base_aggr
        phase = self._cycle_phase()
        try:
            energy_val = float(self._core.energy.energy)
        except Exception:
            energy_val = 60.0
        tired = energy_val < 35.0
        out: List[Tuple[float, str]] = []
        # 烦：生理期（阶段 0）或精力见底
        if phase == 0 or tired:
            out.append((0.60, _pick(("今天有点烦", "今天没什么耐心"), index)))
        # 委屈：被惹了、但好感也掉了——不是发火，是心里发酸
        if d_aggr >= AGGRESSION_UP and d_aff <= SHIFT_LOW:
            out.append((0.67, _pick(("心里有点委屈", "鼻子有点酸"), index)))
        # 害羞：亲近欲涨了、关系还没热到可以直说
        if d_lib >= LIBIDO_UP and d_aff < SHIFT_HIGH:
            out.append((0.59, _pick(("有点不好意思", "不太敢看你"), index)))
        # 吃醋：亲近欲高、对方却冷了——酸
        if d_lib >= LIBIDO_UP and d_aff <= SHIFT_LOW:
            out.append((0.62, _pick(("心里有点酸", "有点不是滋味"), index)))
        # 开心：好感明显高于基线 + 精力足（排卵期更亮一点）
        if d_aff >= SHIFT_HIGH and not tired:
            out.append((0.73 if phase == 4 else 0.71, _pick(("今天挺开心", "心里挺敞亮"), index)))
        # E1（v2.27.11）再加四维：疲倦 / 兴奋 / 心虚 / 期待。
        # 显著度压得低一些，只在上面几个更重的信号都不亮时才露头。
        if energy_val < 30.0:
            out.append((0.55, _pick(("有点乏，提不起劲", "整个人蔫蔫的"), index)))
        elif energy_val >= 85.0:
            out.append((0.54, _pick(("今天精神头很足", "这会儿浑身是劲"), index)))
        if d_aggr >= AGGRESSION_UP and d_aff >= 0:
            out.append((0.56, _pick(("心里有点虚", "有点不太自在"), index)))
        if d_lib >= LIBIDO_UP * 2:
            out.append((0.57, _pick(("心里有点期待", "有点按捺不住"), index)))
        # E4：关系里程碑——最近刚往上跨了一档，给一句淡淡的（不报数字、不像宣告）。
        try:
            rose = float(profile.get("stage_rose_at", 0.0) or 0.0)
            if rose > 0 and (self._core.now_epoch() - rose) < 6 * 3600.0:
                out.append((0.52, _pick(("今天好像跟TA更近了一点", "有点不一样的感觉"), index)))
        except Exception:
            pass
        # M2：认识 TA 的纪念日（周年 / 整百天）——同样只给一句淡淡的，不报天数。
        try:
            sig, line = self._anniversary_line(profile, index)
            if line:
                out.append((sig, line))
        except Exception:
            pass
        return out

    def _anniversary_line(self, profile: Dict[str, Any], index: int) -> Tuple[float, str]:
        """M2：认识 TA 的纪念日（周年 / 整百天）。

        天数**不进上下文**——把「认识 400 天」递进去，她会拿去念一句「我们认识这么久了」，
        那是聊天里最没劲的一句话（与「认识多久不进上下文」同一条规矩）。这里只说
        「今天这个日子她记着」，要不要开口、怎么开口由她自己定。
        """
        try:
            first = float(profile.get("first_met", 0.0) or 0.0)
            now_epoch = float(self._core.now_epoch())
            tz = self._core.clock.now().tzinfo
        except Exception:
            return 0.0, ""
        if first <= 0 or now_epoch <= first:
            return 0.0, ""
        try:
            met = _clock_local(first, tz)
            today = _clock_local(now_epoch, tz)
        except Exception:
            return 0.0, ""
        years = today.year - met.year
        if years >= 1 and (today.month, today.day) == (met.month, met.day):
            return 0.53, _pick(("今天这个日子她一直记着", "今天对她来说有点特别"), index)
        days = (today.date() - met.date()).days
        if days in (100, 200, 300, 500, 1000, 1500, 2000):
            return 0.52, _pick(("今天好像是个该记着的日子", "今天心里有点说不清的在意"), index)
        return 0.0, ""

    def _memory_note(self, user_id: str, index: int) -> Tuple[float, str]:
        """C3 长期情绪记忆（7 天）：记仇 + 记好。看**最近一笔**是什么。"""
        try:
            record = self._core.mood.profile(user_id) or {}
        except Exception:
            return 0.0, ""
        led = record.get("emotion_memory")
        if not isinstance(led, list) or not led:
            return 0.0, ""
        last = led[-1] if isinstance(led[-1], dict) else {}
        # 读取侧也要判有效期（审核 F10）：写入侧 `mood._note_emotion_memory` 只在
        # 「又记了一笔」时剪枝，而正常使用里 8–30 天没有一笔 ±1.5 的情绪事件很常见
        # ——那段时间 led[-1] 一直是旧的那笔，注入句照旧说「前几天那件事我还记着」，
        # 把一个月前的事说成前几天。TTL 只在一个方向生效，读到的就不是「还新鲜的」，
        # 而是「最后写进去的」。
        try:
            stamp = float(last.get("t", 0.0) or 0.0)
            now = float(self._core.now_epoch())
        except (TypeError, ValueError):
            return 0.0, ""
        if not math.isfinite(now) or stamp <= 0 or now - stamp > EMOTION_MEMORY_DAYS * 86400.0:
            return 0.0, ""
        kind = str(last.get("k") or "")
        level = str(last.get("lvl") or "light")
        # M3：分轻重——重的是「那件事还堵着」，轻的是「有点小别扭/记着你的好」。
        if kind == "grudge":
            if level == "heavy":
                return 0.63, _pick(("前几天那件事我还记着", "有些事没那么容易过去"), index)
            return 0.55, _pick(("心里还有点小别扭", "有点小疙瘩"), index)
        if kind == "kindness":
            if level == "heavy":
                return 0.56, _pick(("你对我的好我记着", "一直记着你的好"), index)
            return 0.50, _pick(("记着你的好", "这点好我记着"), index)
        return 0.0, ""

    def _cooling_line(self, user_id: str) -> Optional[Tuple[float, str]]:
        """D 冷静期：她带着情绪冷着，直接返回、不参与竞争。

        句子一律由 `mood.cooling_line(record, inner=True)` 渲染（审核 F1/F2）：模板只有
        一份，所以「还在为你刚才说的话的事」这种二次拼装不会再出现；inner 通道只给
        感受、不给祈使句，所以「先别理他」不会混进她的内心独白。
        """
        try:
            record = self._core.mood.profile(user_id) or {}
            # 非有限数当「没有冷静期」，不能当「还在冷静」：`now >= nan` 为假，
            # 一个坏掉的 cool_until 会让她**永远**冷着（这一条又是直接返回、
            # 盖掉所有别的情绪），比不显示冷静期糟得多。
            until = _num(record.get("cool_until"), 0.0)
            now = float(self._core.now_epoch())
        except Exception:
            return None
        if not math.isfinite(now):
            # 「此刻」不可信就不出这一条：它是直接返回、盖掉所有别的情绪的，
            # 拿不准的时候不该由它独占。
            return None
        if until <= 0 or now >= until:
            return None
        try:
            text = self._core.mood.cooling_line(record, inner=True)
        except Exception:
            return None
        if not text:
            return None
        return 0.95, text

    # ------------------------------------------------------------------

    def candidate(
        self,
        user_id: str,
        *,
        interest: Dict[str, Any],
        agency: Dict[str, Any],
        is_group: bool = False,
        persona_prompt: str = "",
    ) -> Tuple[float, str]:
        """五个维度竞争，返回显著度最高的那一条。`(0.0, "")` = 此刻没什么特别情绪。"""
        score, text, _kind = self.candidate_with_kind(
            user_id, interest=interest, agency=agency,
            is_group=is_group, persona_prompt=persona_prompt,
        )
        return score, text

    def candidate_with_kind(
        self,
        user_id: str,
        *,
        interest: Dict[str, Any],
        agency: Dict[str, Any],
        is_group: bool = False,
        persona_prompt: str = "",
        asleep: bool = False,
    ) -> Tuple[float, str, str]:
        """同 `candidate`，多返回一个来源标记：`"emotion"` / `"ccb"` / `"cooling"` / 空串。

        `prompt_builder._emotion_line` 要对「逐轮重复的同一句」去重（线上实测：
        同一句情绪话连续 8 轮被照抄成「心里窝火」模板）。但 ccb 那句不能去重——
        它本来就是场景里的实时状态（姿势/满足度还会拼在后面），隔一轮不给等于场景里
        突然没声音了。冷静期那句同理：`cool_until` 是一个会过期的状态，
        被 10 分钟去重压掉就会出现「她还在生气，但注入里一句都不提」的空窗
        （黄金快照实测：t0 有、t+2/t+9 没有、t+11 又有）。所以这两种来源都单独标记，
        只有普通情绪句参与逐字去重。

        `asleep=True` 时去掉「主动性」这一维：睡着的人不会「有点想说话」。
        同一轮里睡眠状态句已经写着「正睡着」，再来一句「她内心：有点想说话」，
        模型只能把两条互斥的事实二选一——实测它选了后者，照着「想说话」的口气回话。
        心情（心气/态度/记忆）照样保留：半夜被吵醒时她仍然可能是窝火或心软的。
        """
        try:
            profile = self._core.mood.profile(user_id) or {}
        except Exception:
            return 0.0, "", ""
        if not profile:
            return 0.0, "", ""
        # 抽签种子必须「**一天内稳定、跨天轮换**」——这正是 `wording.pick` 那条规矩。
        #
        # 两版都错过：
        #   · `abs(hash(str(user_id))) % 3`——Python 的字符串 hash 每个进程都加随机盐，
        #     同一个用户**每次重启**就换一个维度。（这也是最早暴露出随机测试失败的原因。）
        #   · 改成 blake2b 但种子只有 role+uid——跨进程稳了，却**一辈子只抽一种措辞**：
        #     实测同一个用户连跑 6 天，六天全是「有点想说话」。一个人不会永远心里只说一句。
        #
        # 加上今天：同一天内反复渲染不换（模型看到的是同一句感受，不是两件事），
        # 过了零点换一个（不会磨成口头禅）。`wording.pick` 早就这么干了。
        #
        # 另外 `role_id` 必须走 `self._core.role_id`：`candidate()` 的参数里**没有**
        # 它，直接引用会抛 NameError，被 `_emotion_line` 的 except 吞掉 → 整句情绪消失。踩过。
        try:
            role = str(getattr(self._core, "role_id", "") or "")
        except Exception:
            role = ""
        try:
            today = str(self._core.clock.today_str() or "")
        except Exception:
            today = ""
        raw = f"emotion:{role}:{user_id}:{today}".encode("utf-8")
        index = int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big") % 3

        pools: List[Tuple[float, str]] = [
            self._day_tone(user_id, index),
            self._now_mood(profile, index),
            self._attitude(profile, user_id, index),
            self._intimacy(interest or {}, profile, index),
        ]
        # 主动性只在醒着时成立：睡着的人不「想开话头」（见 docstring 的 asleep 段）。
        if not asleep:
            pools.append(self._willingness(agency or {}, index))
        pools.extend(self._extra_moods(profile, index))
        note = self._memory_note(user_id, index)
        if note[1]:
            pools.append(note)
        # (D）：余韵。它**不进 ccb 的直出通道**——余韵不是"这一刻必须
        # 说"的状态，只是她这两小时里说话会软一点。所以它作为一个普通候选
        # 参与竞争，显著度压得比常态低，别的情绪该赢还是赢。
        after = self._aftercare_line(user_id)
        if after[1]:
            pools.append(after)

        # 冷静期优先于一切：她正在气头上，别的情绪都让位。
        cool = self._cooling_line(user_id)
        if cool is not None:
            # 来源标 "cooling"：它会过期，不能被 _emotion_line 的逐字去重压掉。
            return cool[0], cool[1], "cooling"

        # ccb：一个默认关的独立开关。条件全中时它**直接返回**，不参与竞争——
        # 门槛已经很高（亲近欲 ≥ `ccb.CCB_LIBIDO_MIN`=38，且比基线涨了
        # `ccb.CCB_LIBIDO_RISE`=4 以上，见 ccb.py:36-45；这里以前写死「45/50」，
        # 是 #158 之前那套已作废的数字），到了就是这一刻，
        # 让它再和「有点想说话」比显著度就等于把它稀释掉了。
        got = self._ccb_line(
            user_id, profile, is_group=is_group, persona_prompt=persona_prompt, now=None
        )
        if got is not None:
            return got[0], got[1], "ccb"

        best = (0.0, "")
        for score, text in pools:
            if text and score > best[0]:
                best = (score, text)
        return best[0], best[1], "emotion"

    def _aftercare_line(self, user_id: str) -> Tuple[float, str]:
        """余韵期的一句。不在余韵期返回 `(0.0, "")`。

        显著度**随时间衰减**：刚收场那会儿 0.90（该它说话，她确实还没缓过来），
        到窗口末尾掉到 0.50（该让位给别的情绪了）。

        为什么不能固定一个值：
        * 固定 0.52 时实测它几乎总是输给态度/心境那几句——余韵等于不存在；
        * 固定 0.9 又会在两小时里把别的情绪全压死，她只剩一种语气。
        线性衰减两头都避开了。
        """
        try:
            cfg = getattr(self._core, "config", None)
            if not bool(getattr(cfg, "ccb", False)):
                return 0.0, ""
            # #82：这里以前还有一道 `if not ccb_scene_detail: return`——余韵句是
            # **独立的一条情绪候选**（在 pools 里按显著度竞争），不是「场景细节」，
            # 关掉细节把它一起关掉，等于 `ccb_scene_detail` 的文档承诺（只影响
            # 场景状态段）与实际行为不符：用户关掉细节后，连「她还没缓过来」也消失了。
            # 细节开关只该管 `_scene_text` 那道闸（:861），不该管这一条。
        except Exception:
            return 0.0, ""
        until_raw = self._core._scope.peek_user(user_id, "ccb_aftercare_until", 0.0)
        try:
            until = float(until_raw or 0.0)
        except (TypeError, ValueError):
            return 0.0, ""
        if until <= 0:
            return 0.0, ""
        window = _num(getattr(cfg, "ccb_aftercare_minutes", 120.0), 120.0) * 60.0
        now = self._core.now_epoch()
        text = _ccb.aftercare_note(aftercare_until=until, now=now, window=window)
        if not text:
            return 0.0, ""
        ratio = _ccb.aftercare_ratio(aftercare_until=until, now=now, window=window)
        return 0.50 + 0.40 * ratio, text

    def _tz(self):
        """她的时区（ccb 的按天账按她的日历算，不按宿主）。取不到就 None=旧行为。"""
        clock = getattr(self._core, "clock", None)
        return getattr(clock, "tz", None)

    def _roll_ccb_day(self, user_state: Dict[str, Any], moment: float) -> bool:
        """按天滚动 ccb 的满足度与当日次数，返回**确实改了状态**没有。

        为什么它必须在读路径上跑：`ccb.decay_satisfy` 平时只被 `advance_satisfy`
        调，而 `advance_satisfy` 只有在「真的进场景」之后才跑；可进场景的条件里又有
        「当日次数 < 8」。于是一旦当天打满 8 次，第二天 `evaluate` 读到的还是 8 →
        永远进不了场景 → `decay_satisfy` 永远不跑 → `ccb_turns` 永久卡在 8、
        ccb 再也不触发。用户看到的就是「计数不会变 + 场景再也没进行过」。

        幂等：`decay_satisfy` 在 `ccb_day` 没变时直接返回，所以每轮调一次只多一次
        整数比较；没有状态变化时不标脏，不制造额外的落盘。
        """
        keys = ("ccb_day", "ccb_satisfy", "ccb_turns")
        try:
            before = tuple(user_state.get(key) for key in keys)
            _ccb.decay_satisfy(user_state, float(moment), tz=self._tz())
            return tuple(user_state.get(key) for key in keys) != before
        except Exception:
            # 修复项本身失败不该连累这一轮的内心话（这个函数外面没有 try）。
            return False

    def _ccb_line(
        self,
        user_id: str,
        profile: Dict[str, Any],
        *,
        is_group: bool,
        persona_prompt: str,
        now: Optional[float],
    ) -> Optional[Tuple[float, str]]:
        """ccb 那一维。条件不满足就 None（连池子都不进）。"""
        cfg = getattr(self._core, "config", None)
        if not bool(getattr(cfg, "ccb", False)):
            return None
        if not _ccb.allowed_for(cfg, user_id):
            return None
        state = self._core.mood.profile(user_id) or profile or {}
        try:
            energy = float(self._core.energy.energy)
        except (TypeError, ValueError, AttributeError):
            energy = 100.0
        moment = now if now is not None else self._core.now_epoch()
        raw = f"ccb:{getattr(self._core, 'role_id', '')}:{user_id}".encode("utf-8")
        idx = int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big") % 2
        user_state = self._core._scope.user_state(user_id)
        _cfg = self._core.config
        # (P3）：安全闸。人设命中未成年 / 血缘 → 整条功能不生效，
        # 连"内心话"都不出。放在这里而不是只在 `evaluate` 里，是因为这条路径
        # 还要推进状态、扣精力——被挡的人不该留下任何账。
        safety = _ccb.safety_block(persona_prompt)
        if safety:
            # 命中原因写 debug：这是**静默关掉整条功能**的闸，不写日志时用户只看到
            # 「没反应」，分不清是门槛没到还是人设被安全词表拦了（B6 #81）。
            logger.debug("[humanoid] ccb 安全闸命中，整条功能不生效：%s", safety)
            return None
        # P0：进 evaluate 之前先把「按天回落 + 当日次数重算」做掉。
        # 不做的话第 8 次之后 `ccb_turns` 永久卡住、ccb 再也不触发（根因见
        # `_roll_ccb_day`）。它只在跨天时改状态，所以平时这一行不产生写入；
        # 放在安全闸之后，是为了被挡的人设连一根计数键都不留下。
        if self._roll_ccb_day(user_state, moment):
            self._core._scope.mark_dirty()
        coerced = ccb_coerced_within(user_state, moment)
        # 门槛从配置取。写死在这个模块里的话，用户看到「够不着」却无处可调——
        # 而这个开关默认的 affection 门槛对多数人是永远够不着的。
        libido_min = getattr(_cfg, "ccb_libido_min", _ccb.CCB_LIBIDO_MIN)
        libido_rise = getattr(_cfg, "ccb_libido_rise", _ccb.CCB_LIBIDO_RISE)
        affection_min = getattr(_cfg, "ccb_affection_min", _ccb.CCB_AFFECTION_MIN)
        affection_rise = getattr(_cfg, "ccb_affection_rise", _ccb.CCB_AFFECTION_RISE)
        affection_deep = getattr(_cfg, "ccb_affection_deep", _ccb.CCB_AFFECTION_DEEP)
        affection = _num(state.get("affection"), 46.0)
        base_affection = _num(state.get("base_affection"), 46.0)
        libido = _num(state.get("libido"), 34.0)
        base_libido = _num(state.get("base_libido"), 34.0)

        # ── P1：她自己那一侧 ────────────────────────────────────────
        # 拒绝窗与 coerced 分开：coerced 是「他越界」，refused 是「她不想要」。
        refused = _ccb.refused_within(user_state, moment)
        use_arousal = bool(getattr(_cfg, "ccb_use_arousal", True))
        # 身体的唤醒：soma 一直在推它（note_chat +6 / note_message +1.5），
        # 而 ccb 之前一次都没读过。面板关掉时按 50（=不加不减）处理。
        arousal = 50.0
        if use_arousal:
            try:
                arousal = float(self._core.soma.snapshot().get("arousal", 50.0) or 50.0)
            except Exception:
                arousal = 50.0
        # 关系到哪一步：走 state_snapshot 的唯一一份（好感 + 相处时长）。
        # 原来这里只传好感，于是聊过几百轮的人在注入里是「熟到可以直说」、
        # 在这里还算成「还在熟悉」——同一轮两套阶段（审核 状态与人设 1）。
        rel_index = 0
        try:
            rel_index = int(
                _snap.relation_of(state, moment, tz=_snap.tz_of(self._core)).stage_id
            )
        except Exception:
            rel_index = 0
        initiative_score = _ccb.initiative(
            libido=libido,
            base_libido=base_libido,
            arousal=arousal,
            aggression=_num(state.get("aggression"), _seed_aggression(getattr(self, "_core", None))),
            relstage_index=rel_index,
            affection=affection,
            affection_min=affection_min,
            use_arousal=use_arousal,
        )

        # ── 进门判断────────────────────────────────────
        # 模型分只在那次情绪批里更新（默认 10 条一次），所以必须判它新不新鲜：
        # 30 分钟前的结论不能拿来决定现在进不进。过期 → -1（不知道）→ 退回兜底。
        # F8：闲置超时（`CCB_SCENE_IDLE_SECONDS`，默认 6 小时）必须**真的结束这一场**。
        # 原来只有只读面板（`scene_status`）判「已过期」，状态机照旧拿
        # `ccb_stage > 0` 当「还在场景里」——于是隔夜再聊，她还在上一场的姿势、
        # 满足度与「临界」里续着。判据走 `scene_active()`，与面板同源，不另算。
        in_scene = _ccb.scene_active(user_state, moment)
        if not in_scene and float(user_state.get("ccb_stage", 0.0) or 0.0) > 0:
            _ccb.reset_scene(user_state)
            self._core._scope.mark_dirty()
        raw_score = _num(user_state.get("ccb_scene_score"), -1.0)
        score_at = _num(user_state.get("ccb_scene_at"), 0.0)
        scene_score = raw_score if _ccb.scene_score_fresh(score_at, moment) else -1.0
        # 兜底信号：最近 15 分钟内出现过亲密意图（用户或她自己说的）。
        intimacy_hint = (
            moment - _num(user_state.get("ccb_intimacy_at"), 0.0)
        ) <= _ccb.CCB_INTIMACY_WINDOW
        # 滞回的另一半：只有**这一场开始之后**给出的判断才有资格结束它。
        # 拿进场前的旧结论赶人，会让她刚进来就出去。
        if in_scene and scene_score >= 0 and scene_score <= _ccb.CCB_SCENE_EXIT:
            if score_at > _num(user_state.get("ccb_gate_at"), 0.0):
                _ccb.reset_scene(user_state)
                self._core._scope.mark_dirty()
                return None
        lead_mode = str(getattr(_cfg, "ccb_lead", "auto") or "auto")
        dominance_score = _ccb.dominance(
            persona_prompt=persona_prompt,
            aggression=_num(state.get("aggression"), _seed_aggression(getattr(self, "_core", None))),
            relstage_index=rel_index,
            affection=affection,
            her_acts=_num(user_state.get("ccb_her_acts"), 0.0),
            his_acts=_num(user_state.get("ccb_his_acts"), 0.0),
        )
        res = _ccb.evaluate(
            enabled=True,
            libido_min=libido_min,
            libido_rise=libido_rise,
            affection_min=affection_min,
            affection_rise=affection_rise,
            affection_deep=affection_deep,
            coerced=coerced,
            refused=refused,
            safety_reason=safety,
            in_scene=in_scene,
            scene_score=scene_score,
            intimacy_hint=intimacy_hint,
            initiative_score=initiative_score,
            lead=lead_mode,
            dominance_score=dominance_score,
            is_group=is_group,
            persona_prompt=persona_prompt,
            affection=affection,
            base_affection=base_affection,
            libido=libido,
            base_libido=base_libido,
            energy=energy,
            # P0-3：冷却读**自己的锚点**。原来读 `ccb_last_at`，而那个键会被
            # `note_occurrence`（「发生过」的记录层）写——一句普通闲聊命中误记词，
            # 就能把真正的触发推到 5 小时之后。旧数据没有 `ccb_gate_at`，
            # 退回 `ccb_last_at` 保证升级后行为不变（只是继续受旧账影响一轮）。
            last_at=float(
                user_state.get("ccb_gate_at", user_state.get("ccb_last_at", 0.0)) or 0.0
            ),
            now=float(moment),
            turns_today=float(user_state.get("ccb_turns", 0.0) or 0.0),
            index=idx,
        )
        if res is None:
            # 只有「该不该进入」的门槛**真的掉下去了**，才算场景结束、清掉场景状态。
            # 冷却期内 / 当日已达上限 / 群里 / 精力见底 都是**暂时的**——原来一律
            # reset_state() 会把 ccb_last_at / ccb_turns / ccb_done_count 一起清掉：
            # 冷却随之失效（下一轮又能触发），一辈子的次数也归零。
            if float(user_state.get("ccb_stage", 0.0) or 0.0) > 0 and _ccb.scene_lapsed(
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
                affection_deep=affection_deep,
                coerced=coerced,
            ):
                _ccb.reset_scene(user_state)
            return None
        score, text, stage = res
        # 同一条用户消息可能触发多次 LLM 请求，而这里是**读路径**：每次都推进的话，
        # ccb 的当日次数/一辈子次数会被重复计数（一条消息算成几次）。用短窗去重：
        # 窗口内的重复渲染只出文本、不再推进状态。
        try:
            last_advance = float(user_state.get("ccb_advance_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            last_advance = 0.0
        if float(moment) - last_advance < CCB_ADVANCE_DEDUP_SECONDS:
            # 状态没推进，但状态段要按**当前**状态给（此时状态已经是最新的）。
            return score, self._scene_text(text, user_state, moment, user_id, _cfg)
        user_state["ccb_advance_at"] = float(moment)
        # P0-3：冷却锚点只在这里写——只有**真的进了场景**才算一次冷却。
        user_state["ccb_gate_at"] = float(moment)
        # (D）：进**新的一场**时清账本。判据是"之前不在场景里"——
        # 场景中的推进不能清，否则换过的姿势全丢。
        try:
            was_in_scene = float(user_state.get("ccb_stage", 0.0) or 0.0) > 0
        except (TypeError, ValueError):
            was_in_scene = False
        if not was_in_scene:
            _ccb.log_clear(user_state)
            user_state["ccb_peak_depth"] = 0.0
            # 进场第一句要接得上她刚才在做的事（v3.1.18，B 的读侧）：这里只存一句，
            # 由 `_with_scene_note` 在开头 15 分钟内带上；过期就不再提。
            user_state["ccb_bg"] = self._schedule_bg_line(moment)
            user_state["ccb_bg_at"] = float(moment)
            # F5：当日次数 = 今天开了几场（进场时 +1），不是「推进了几格」。
            # 原来每推进一格 +1，而一场要 4 格——面板上的「今天 3 次」其实是
            # 「推进了 3 格」，也就是同一次事情还没做完就已经记了 3 次。
            user_state["ccb_turns"] = float(user_state.get("ccb_turns", 0.0) or 0.0) + 1.0
        user_state["ccb_stage"] = float(stage)
        # F5 / F6 / F7：这里只推进场景，**不记账**。
        # * `ccb_turns`（今天几场）在进场时 +1，见上面 `if not was_in_scene`。
        # * `ccb_done_count`（一辈子的次数）只有一个入口：`note_occurrence()`。
        #   这里再 +1 会让一次事情被记两遍（另一遍来自正文命中「已经发生」）。
        # * `ccb_last_at` 的语义是「上一次真的发生」，推进不是发生——它记在
        #   `ccb_activity_at`（只给闲置超时用），免得「上次发生」被推进刷成现在。
        user_state["ccb_activity_at"] = float(moment)
        # 推进场景。跨天的回落和次数重算都发生在这一步里，所以只在真进场景时
        # 才付这个代价——没进场景的日子不该把状态改掉。
        # F3：进场那一轮**不推进**。`advance_satisfy` 每次 +40，进场时 satisfy=0 →
        # 首轮立刻 40 → `phase_of()` 直接是 2「沉醉」，phase 1「动情」在注入路径上
        # 一次都到不了。改成从第二场内轮开始累积：0(动情) → 40(沉醉) → 80(临界)
        # → 100(到顶)；一场完整的场景因此是 4 轮，不是 3 轮。
        if was_in_scene and _ccb.advance_satisfy(
            user_state, float(moment), tz=self._tz()
        ):
            # 刚到顶：这一次是收场，扣一次体力。
            self._core._scope.set_self(
                "energy", max(0.0, float(self._core.energy.energy) - _ccb.CCB_ENERGY_COST)
            )
            # 
            # 记账路径还会 +2/+3 —— 等于自己给自己续火，下一次触发更近。
            try:
                self._core.ccb_settle(user_id, float(moment))
            except Exception:
                pass
            # 。
            # 场景态归零、姿势与位置作废；`ccb_satisfy` 保留——它是"上头"，
            # 由按天回落处理，不是场景状态。余韵窗单开一段。
            user_state["ccb_stage"] = 0.0
            user_state["ccb_pose"] = float(_ccb.POSE_UNKNOWN)
            user_state["ccb_site"] = float(_ccb.POSE_UNKNOWN)
            window = _num(
                getattr(getattr(self._core, "config", None), "ccb_aftercare_minutes", 120.0),
                120.0,
            ) * 60.0
            user_state["ccb_aftercare_until"] = float(moment) + max(0.0, window)
            _ccb.log_append(user_state, "peak", 0, moment)
        self._core._scope.mark_dirty()
        # 状态段放在**推进之后**算：原来放在 `evaluate` 之后、写 stage 之前，
        # 于是进场那一轮读到的还是 stage=0（整段不出），之后每轮读到的是**上一轮**
        # 的满足度（注入说"沉醉"、状态里已经是"临界"）。实测对不上，S3。
        return score, self._scene_text(text, user_state, moment, user_id, _cfg)

    def _scene_text(self, text: str, user_state: Dict[str, Any], moment: float,
                    user_id: str, cfg: Any) -> str:
        """状态段的唯一拼装口（两条 return 都走它，保证口径一致）。

        纯增强：开关关掉、或者任何一步出错，都原样返回前面那句内心话。
        """
        if not bool(getattr(cfg, "ccb_scene_detail", True)):
            return text
        try:
            return self._with_scene_note(text, user_state, moment, user_id)
        except Exception:
            return text

    def _schedule_bg_line(self, moment: float) -> str:
        """进场时她刚才在做什么（v3.1.18，B 的读侧）。

        只取一句、只在这一场开头用。`current_slot()` 已经过滤掉脏段，脏了会回
        中性「自由活动」——那种空话不递。拿不到就返回空串。
        """
        try:
            schedule = getattr(self._core, "schedule", None)
            slot = schedule.current_slot() if schedule is not None else None
        except Exception:  # noqa: BLE001 - 拿不到就不提
            return ""
        if not isinstance(slot, dict):
            return ""
        event = str(slot.get("event") or "").strip()
        if not event or event in ("自由活动", "歇着", "发呆", "随意"):
            return ""
        if event.startswith("在"):
            return f"刚才{event}"
        return f"刚才在{event}"

    def _with_scene_note(self, text: str, user_state: Dict[str, Any],
                         moment: float, user_id: str) -> str:
        """把「姿势 / 位置 / 深度判断 / 阶段」压成一个短语挂到内心话后面。

        A 的兜底：任何一项都拿不到、或整段压不出内容（`scene_note` 返回空串），
        就原样返回——退回单行注入，不报错、不中断。

        (D）：深度取**这一场到过的最深值**（单调不减），阶段回退时不会
        出现"刚才说撑到疼、现在又说还没到底"。
        """
        stage = _num(user_state.get("ccb_stage"), 0.0)
        if stage <= 0:
            # 到顶那一轮：`ccb_stage` 已经归零，但余韵窗**刚开**——这一轮正是爆发，
            # 什么都不给等于在最关键的一刻闭嘴。判据：余韵剩余量还接近整个窗口。
            window = _num(
                getattr(getattr(self._core, "config", None), "ccb_aftercare_minutes", 120.0),
                120.0,
            ) * 60.0
            until = _num(user_state.get("ccb_aftercare_until"), 0.0)
            if window > 0 and until > moment and (until - moment) >= window - 5.0:
                phase = 4
            else:
                # 不在场景里就只管内心话（余韵由 `_aftercare_line` 作为普通候选处理，
                # 不走这条直出通道）。
                return text
        else:
            phase = _ccb.phase_of(
                satisfy=_num(user_state.get("ccb_satisfy"), 0.0),
                stage=stage,
                aftercare_until=_num(user_state.get("ccb_aftercare_until"), 0.0),
                now=moment,
            )
        body = self._core.ccb_body()
        partner = self._core.ccb_partner(user_id)
        depth = _ccb.depth_estimate(_num(partner.get("length_cm"), 0.0), phase)
        # 单调化：这一场只升不降。
        depth = _ccb.note_peak_depth(user_state, depth)
        # cm 数字与"过深/未过深"的判定都留在 ccb 内部，注入只拿三态感受：
        # True=撑满、False=没撑满、None=这一场还没深度数据（不知道就不说）。
        cap = _num(body.get("depth_cap_cm"), 14.0)
        over = _ccb.is_over_depth(depth, cap) if depth > 0 else None
        # 姿势/位置是场景里的事实，照原样给；**阶段名不给**——「动情/沉醉/临界/爆发」
        # 是状态机的档位名（面板给运营看的内部刻度），递进上下文就会被当成事实复述
        # （「现在是临界」）。同一份 phase 走 `_scene_feeling` 翻成她自己的感受（#62）。
        scene = _ccb.scene_note(
            pose=int(_num(user_state.get("ccb_pose"), _ccb.POSE_UNKNOWN)),
            site=int(_num(user_state.get("ccb_site"), _ccb.POSE_UNKNOWN)),
            phase=0,
            over=over,
        )
        feeling = _scene_feeling(phase)
        # P4 验收：不用「·」拼，读感像读数，用「，」更自然
        # ccb.scene_note 内部还是用「·」拼的，我们把它替换掉
        scene = scene.replace("·", "，")
        # 进场那一句：她刚才在做什么（v3.1.18）。只在这一场开头 15 分钟内给——
        # 之后场景本身就是「正在发生的事」，再提就成了复述。
        bg = ""
        try:
            bg_at = _num(user_state.get("ccb_bg_at"), 0.0)
            if bg_at > 0 and 0.0 <= moment - bg_at <= 900.0:
                bg = str(user_state.get("ccb_bg") or "")
        except Exception:  # noqa: BLE001
            bg = ""
        note = "，".join(part for part in (bg, scene, feeling) if part)
        return f"{text}（{note}）" if note else text


def bracket(text: str) -> str:
    """把一句话标成「她私下的内心状态」。

    原来用括号 `（她的内心：…）`——括号确实能把「状态」和「事实」分开，但它同时
    给模型示范了一种排版：模型会照着在自己的回复里写「（动作）台词」。去掉括号，
    改成一句带标签的普通陈述「她内心：…」，既还是「她的内心」，又不再示范排版。
    """
    body = str(text or "").strip()
    if not body:
        return ""
    # 已经带过标签的不再补一层：套两次会变成「她内心：她内心：心里有点堵」。
    if body.startswith("她内心："):
        return body
    return f"她内心：{body}"
