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

from typing import Any, Dict, List, Optional, Tuple

# 门槛：情绪要**高于初始值**才出现。初始值是 affection 46 / libido 34 / aggression 28，
# 门槛压着初始值时，这些话就是常驻背景而不是异常信号。
SHIFT_HIGH = 9.0
SHIFT_LOW = -7.0
AGGRESSION_UP = 7.0
# 亲近欲的门槛必须**比基线高**多少，而不是绝对值：初始亲近欲就是 34，
# 拿绝对值 8 当门槛等于「永远满足」。
LIBIDO_UP = 8.0
CARE_CLOSE = 0.62

# 情绪记忆的门槛：单次不算（那叫记仇），重复且近期才算（那叫在意）。
GRUDGE_SAME_DAY = 2
GRUDGE_RECENT_SECONDS = 2 * 3600.0


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pick(texts: Tuple[str, ...], index: int) -> str:
    return texts[max(0, index) % len(texts)]


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
            if not str(entry.get("time", "")).startswith(today):
                continue
            event = str(entry.get("event", ""))
            if "下降" in event:
                downs += 1
            elif "上升" in event:
                ups += 1
        return ups, downs

    def _recent_negative(self, user_id: str) -> bool:
        """最近一次负向波动是不是就在刚才（两小时内）。"""
        try:
            from ..humanoid.clock import parse_state_timestamp  # type: ignore
        except Exception:
            return False
        try:
            from .clock import parse_state_timestamp
        except Exception:
            return False
        entries = self._core.mood.logs(user_id, limit=1)
        if not entries:
            return False
        if "下降" not in str(entries[0].get("event", "")):
            return False
        try:
            stamp = parse_state_timestamp(
                str(entries[0].get("time", "")), self._core.clock.now()
            )
        except Exception:
            return False
        if stamp is None:
            return False
        return (self._core.now_epoch() - stamp.timestamp()) <= GRUDGE_RECENT_SECONDS

    # ------------------------------------------------------------------
    # 五个维度
    # ------------------------------------------------------------------

    def _day_tone(self, user_id: str, index: int) -> Tuple[float, str]:
        """今天的底色。按当天的波动次数算，所以一天之内不翻来覆去。"""
        ups, downs = self._today_events(user_id)
        if downs >= 2 and ups == 0:
            return 0.86, _pick(("她今天一整天都不太顺", "她今天状态一直不好"), index)
        if downs == 1 and ups == 0:
            return 0.62, _pick(("她今天有点闷", "她今天心情低落的"), index)
        if downs >= 1 and ups >= 1:
            return 0.55, _pick(("她今天情绪起起伏伏的", "她今天一会儿好一会儿不好"), index)
        if ups >= 2:
            return 0.80, _pick(("她今天心情不错", "她今天挺来劲的"), index)
        if ups == 1:
            return 0.50, _pick(("她今天心情还行", "她今天挺轻松的"), index)
        return 0.0, ""

    def _now_mood(self, profile: Dict[str, Any], index: int) -> Tuple[float, str]:
        """此刻的心气。三轴相对基线动了多少。"""
        aff = _num(profile.get("affection"), 46.0)
        base_aff = _num(profile.get("base_affection"), aff)
        libido = _num(profile.get("libido"), 34.0)
        base_lib = _num(profile.get("base_libido"), libido)
        aggr = _num(profile.get("aggression"), 28.0)
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
        aff = _num(profile.get("affection"), 46.0)
        base_aff = _num(profile.get("base_affection"), aff)
        aggr = _num(profile.get("aggression"), 28.0)
        base_aggr = _num(profile.get("base_aggression"), aggr)

        if downs >= GRUDGE_SAME_DAY or self._recent_negative(user_id):
            return 0.92, _pick(("你上次的火她还没消", "你上次那事她记着呢"), index)
        if aggr - base_aggr >= AGGRESSION_UP:
            return 0.85, _pick(("她今天对你有点冷淡", "她不想主动理你"), index)
        if aff - base_aff >= SHIFT_HIGH * 1.5:
            return 0.78, _pick(("她今天对你特别有耐心", "她今天黏着你"), index)
        if base_aff - aff >= abs(SHIFT_LOW) * 1.5:   # 跌幅够大才算生疏
            return 0.76, _pick(("她今天对你有点客气", "她有点想躲着你"), index)
        if ups >= 2:
            return 0.62, _pick(("她今天心情好，看着你顺眼", "她今天挺愿意搭理你"), index)
        return 0.0, ""

    def _intimacy(self, interest: Dict[str, Any], profile: Dict[str, Any], index: int) -> Tuple[float, str]:
        """她愿意说多少。关系亲密度 × 当天基调。"""
        care = _num(interest.get("care"), 0.5)
        aff = _num(profile.get("affection"), 46.0)
        base_aff = _num(profile.get("base_affection"), aff)
        aggr = _num(profile.get("aggression"), 28.0)
        base_aggr = _num(profile.get("base_aggression"), aggr)
        upset = (aggr - base_aggr >= AGGRESSION_UP) or (aff - base_aff <= SHIFT_LOW)

        if care >= CARE_CLOSE and not upset:
            return 0.66, _pick(("她愿意多说两句", "她愿意说点私人的"), index)
        if care >= CARE_CLOSE and upset:
            return 0.68, _pick(("她不太想多说", "她今天不太想说真心话"), index)
        if care < 0.35:
            return 0.55, _pick(("她和你还有点距离", "她不太愿意提私事"), index)
        return 0.45, _pick(("她照常", "她跟你说话挺正常的"), index)

    def _willingness(self, agency: Dict[str, Any], index: int) -> Tuple[float, str]:
        """主动性：她想不想开话头。"""
        try:
            value = _num(agency.get("initiative"), 0.0)
        except Exception:
            return 0.0, ""
        if value >= 0.70:
            return 0.68, _pick(("她这会儿挺想说点什么", "话匣子是开着的"), index)
        if value >= 0.45:
            return 0.52, _pick(("她有点想说话", "接话接得住"), index)
        if value > 0.0:
            return 0.40, _pick(("她不太想开话头", "这会儿没什么特别想说的"), index)
        return 0.0, ""

    # ------------------------------------------------------------------

    def candidate(
        self,
        user_id: str,
        *,
        interest: Dict[str, Any],
        agency: Dict[str, Any],
    ) -> Tuple[float, str]:
        """五个维度竞争，返回显著度最高的那一条。`(0.0, "")` = 此刻没什么特别情绪。"""
        try:
            profile = self._core.mood.profile(user_id) or {}
        except Exception:
            return 0.0, ""
        if not profile:
            return 0.0, ""
        index = abs(hash(str(user_id))) % 3

        pools: List[Tuple[float, str]] = [
            self._day_tone(user_id, index),
            self._now_mood(profile, index),
            self._attitude(profile, user_id, index),
            self._intimacy(interest or {}, profile, index),
            self._willingness(agency or {}, index),
        ]
        best = (0.0, "")
        for score, text in pools:
            if text and score > best[0]:
                best = (score, text)
        return best


def bracket(text: str) -> str:
    """把一句话包成「隐藏的内心活动」。

    括号是关键：它告诉模型这是状态、不是要说出口的话。同时它天然地把这句和外面
    那些客观事实分开——模型读到括号会当内心独白，而不是当台词复述。
    """
    body = str(text or "").strip()
    if not body:
        return ""
    # 已经是完整句（「她今天对你有点冷淡」）就直接括起来，括号本身就是内心独白的标记；
    # 只给了内容的（「心里有点堵」）才补主语——补成「（她的内心：她心里有点堵。）」的话，
    # 同一个主语说两遍。
    # 结尾**不加句号**：外层 join 时会补，否则拼出「（…。）。」这种双句号。
    if body[0] in ("她", "你"):
        return f"（{body}）"
    return f"（她的内心：{body}）"
