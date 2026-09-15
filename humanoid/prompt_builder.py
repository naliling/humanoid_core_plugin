"""把身体与处境编译成上下文——**只给事实，不给台词、不给禁令、不给读数**。

分工（v2.16.4 起）：

* 这个文件只产出**事实块**：此刻几点/在哪/跟TA是群聊还是私聊、她的身体现在是什么状态、
  今天到这会她过了什么、TA隔了多久没说话、这三样在她心里各占多重。
* 「这些事实该怎么被理解」不写在事实里，由 `FRAMING_TEXT` 一次性放进 system_prompt
  （见 `main.py`）。事实每条消息都在变，读它的方式不该变。
* 数值（百分比、好感度、UTC 偏移）一律不进上下文：那是体检单，人聊天时不念体检单。
  要看数值去 `/她的状态`、`/好感度`、`/时间`、`/拟人诊断`。

不做什么：
* 不写台词——「我现在需要休息，明天再聊吧」这种句子等于插件替她把话说完。
* 不下禁令——「不要接着聊」「控制在20字」「这一轮不追问」插件根本执行不了，只是让模型猜规矩。
* 不编细节——「手机震醒的」「眼睛睁不开」不是身体算出来的东西，是插件替她写的人设。
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .core_instance import HumanoidCoreInstance

from .config import HumanoidConfig
from .data.mood_map import get_mood_label
from .services.schedule import day_lines, day_phrases, done_between
from .wording import CARE_WORDS, FOCUS_WORDS, SPARE_WORDS, SINCE_WORDS, pick, scale_word

_CJK_RANGES = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")

# 事实块的开头标记。注入走的是用户消息，而 AstrBot 会把拼装后的用户消息存进会话历史，
# 所以每次注入前要把历史里旧的块抹掉（`main.py` 负责），标记就是给这一步认路用的。
# 换文案时把 v 后面的数字加一，老会话里的旧块会被当成不认识的内容直接清掉。
MARK_PREFIX = "〔她的身体与生活"
MARK_VERSION = "v4"

# 给 system_prompt 的那段话：说明下面这些是什么、按什么方式读它。稳定文本，不含事实。
FRAMING_TEXT = (
    "\n\n【她的身体与生活】\n"
    "接下来标着「" + MARK_PREFIX + "」的那几小块，是这个人的真实处境：一副会困会饿的身体、"
    "一份今天已经在过的日子、以及你面前这个人在她心里的位置。\n"
    "人说话的时候这些一直在起作用，但没有人会把它们念出来——它们是她说话时的处境，"
    "不是要逐条回应的任务，也不是照着发的稿子。上面写她困、写她手上正忙着，说的是身体，"
    "不是规定她该回几个字、该不该回。说什么、说多长、接不接得下去，由她自己判断。"
)

# 注入块的硬长度上限（字符）。每条聊天请求都追加一次，所以它必须是个定值而不是
# 「看拼出来多少」；超限时按块边界丢，绝不从句子中间硬截（截在半句上模型会自己补下去）。
INJECT_MAX_CHARS = {"low": 520, "full": 860, "mood_only": 260}


def estimate_tokens(text: str) -> int:
    """估算 token 数：中日韩一字算一 token，其余按 3.5 字符一 token。

    与自主拟人社交侧同口径；没装分词器时宁可高估也不能低估预算。
    """
    if not text:
        return 0
    cjk = len(_CJK_RANGES.findall(text))
    return int(math.ceil(cjk + (len(text) - cjk) / 3.5))


# 情绪标签 → 她此刻对TA的心气。写的是心里怎么样，不是「语气要怎样」：
# 怎么说由她自己定。每个标签备几条等价说法，按天抽一条（见 wording.pick）。
MOOD_TONE_HINTS = {
    "亲密": ("跟TA没什么防备", "在TA面前不端着"),
    "依恋": ("想黏着TA一点", "想多跟TA说两句"),
    "信赖": ("TA说的话你愿意当真", "TA讲的事你信"),
    "热情": ("对TA讲的事有兴趣", "TA说什么你都想接"),
    "友好": ("愿意搭理TA", "对TA挺和气的"),
    "平常": ("",),
    "疏远": ("不想跟TA多说", "跟TA有点距离"),
    "冷淡": ("没什么想说的", "话到嘴边也懒得多说"),
    "敌视": ("看TA有点不顺眼", "对TA带着点火"),
    "警惕": ("先看看TA怎么讲", "对TA留着一手"),
}

# 低注入档下最多给几条体感：真人多数时候不觉得自己在报备身体。
MAX_FEELINGS_LOW = 2
MAX_FEELINGS_FULL = 5
FEELING_THRESHOLD_LOW = 0.55

# 注入块里「今天干了什么」与「记得的事」各给多少。这两块是生活细节，
# 但注入每条聊天请求都追加一次，必须控住体积。
DAY_ITEMS_LOW = 2
DAY_ITEMS_FULL = 3
TOPICS_LOW = 2
TOPICS_FULL = 4

# 情绪偏到一定程度就直接说心里怎么样，而不是只贴一个标签词。
AGGRAVATED_AGGRESSION = 28.0
EAGER_LIBIDO = 32.0
COLD_AFFECTION = 32.0
WARM_AFFECTION = 72.0

EVENT_TEXT = {
    "conversation_started": "这是你和TA的第一次对话",
    "conversation_resumed": "TA刚重新接上话",
    "user_returned": "TA重新出现了",
    "long_gap": "TA隔了很久才回来",
}


def humanize_gap(seconds: float) -> str:
    """把秒数说成一句时长：12 分钟、3 小时 12 分、2 天 4 小时。

    既然要报就报准：旧版这里给的是「约2~6小时重新出现」这种档位话术，把一件客观事
    描述成一种情境。拿不到准确隔时时才退回 EVENT_TEXT 里那句。"""
    total = max(0, int(round(float(seconds))))
    minutes, hours, days = total // 60, total // 3600, total // 86400
    if hours >= 24:
        rest_h = (minutes - days * 1440) // 60
        return f"{days} 天{f' {rest_h} 小时' if rest_h else ''}"
    if hours:
        return f"{hours} 小时 {minutes % 60} 分" if minutes % 60 else f"{hours} 小时"
    if minutes:
        return f"{minutes} 分钟"
    return "不到一分钟"


AGENCY_LABELS = {
    "initiative": "主动",
    "curiosity": "好奇",
    "care": "关心",
    "social_willingness": "社交意愿",
    "continuation": "延续话题",
}


class PromptBuilder:
    def __init__(self, core_instance: "HumanoidCoreInstance") -> None:
        self._core = core_instance

    @property
    def config(self) -> HumanoidConfig:
        return self._core.config

    # ------------------------------------------------------------------

    def build(
        self,
        user_id: str,
        is_group: bool = False,
        events: Optional[List[Dict[str, Any]]] = None,
        agency: Optional[Dict[str, float]] = None,
        text: str = "",
        interest: Optional[Dict[str, float]] = None,
    ) -> str:
        cfg = self.config
        events = events or []
        agency = agency or {}
        if interest is None:
            interest = {}
        mode = cfg.inject_activity_context

        if mode == "mood_only":
            # 最轻的一档也带着三样必需品：时间、场景、称呼。省掉的只能是生活细节。
            relation = self._relation_lines(user_id, is_group, detailed=False, interest=interest)
            nickname = self._nickname_line(user_id, is_group)
            if nickname:
                relation = relation + [nickname]
            parts = [
                self._block("此刻", self._scene_lines(is_group, detailed=False)),
                self._block("对TA", relation),
                self._block("感觉", self._feelings_lines(max_items=1, threshold=0.7)),
                self._block("记得", self._memory_lines(user_id, detailed=False)),
            ]
            return self._finish("\n".join(parts), cfg)

        if mode == "full":
            return self._finish(self._build_full(user_id, is_group, events, agency, text, interest), cfg)

        return self._finish(self._build_low(user_id, is_group, events, agency, text, interest), cfg)

    # ------------------------------------------------------------------
    # 分块
    # ------------------------------------------------------------------

    def _seed(self, user_id: str) -> List[Any]:
        """措辞抽签的种子：角色 + 今天 + 用户。同一天里同一句话不会换两种说法。"""
        try:
            today = self._core.clock.today_str()
        except Exception:
            today = ""
        return [self._core.role_id, today, user_id]

    def _block(self, title: str, lines: List[str]) -> str:
        lines = [line for line in lines if line]
        if not lines:
            return ""
        return f"【{title}】" + "；".join(lines) + "。"

    def _feelings_lines(self, max_items: int, threshold: float) -> List[str]:
        """按显著度挑体感。低于门槛的一律不注入。"""
        core = self._core
        if not self.config.soma_enabled:
            return []
        try:
            feelings = core.soma.feelings(float(core.energy.energy))
        except Exception:
            return []
        picked = [item for item in feelings if item[0] >= threshold]
        picked.sort(key=lambda item: item[0], reverse=True)
        return [text for _, text in picked[:max_items]]

    def _night_lines(self) -> List[str]:
        """夜间/午睡：只报身体与日程的事实，不编细节、不管她怎么回。

        v2.16.3 这一档写的是「你还在睡，是手机震醒的那种：眼睛睁不开，脑子没转，话到
        嘴边只剩一两个字」——手机、眼睛、脑子都不是算出来的，是插件替她写的人设。现在
        只留两件事：按她自己的日程这会儿算不算睡，以及她的身体有多困。"""
        cfg = self.config
        core = self._core
        if not cfg.night_mode_enabled or not cfg.show_sleep_window or not core.clock.is_night():
            return []
        lines: List[str] = []
        if cfg.soma_enabled:
            try:
                snap = core.soma.snapshot()
            except Exception:
                snap = {}
            if float(snap.get("asleep", 0.0)) >= 1.0:
                # 体感那块已经说了「这段时间她自己排的是觉」，同一件事不必在两个块里各说一遍。
                return []
            lines.append("按她自己的节律，现在是这一天里最该睡的时候")
        else:
            lines.append("按她自己的作息，这会儿算夜里")
        return lines

    def _relation_lines(
        self, user_id: str, is_group: bool, detailed: bool, interest: Dict[str, float]
    ) -> List[str]:
        """她对TA、以及对TA正在讲的这件事，各是什么位置。三轴措辞 + 心气，没有数值。"""
        cfg = self.config
        core = self._core
        if not (cfg.mood_enabled and (not is_group or cfg.mood_enabled_in_group)):
            return []
        seed = self._seed(user_id)
        lines: List[str] = []
        try:
            data = core.mood.profile(user_id)
            label = get_mood_label(data["affection"], data["libido"], data["aggression"])
        except Exception:
            data, label = {}, ""
        care = float(interest.get("care", 0.5))
        focus = float(interest.get("focus", 0.4))
        spare = float(interest.get("spare", 0.7))
        care_word = pick("care", seed, scale_word(care, CARE_WORDS) or "")
        focus_word = pick("focus", seed, scale_word(focus, FOCUS_WORDS) or "")
        # 心气括号与「在意度」说的是同一件事，两个都上就变成报两遍。三轴在的时候只留标签。
        if label and not care_word:
            hint = pick(f"tone:{label}", seed, MOOD_TONE_HINTS.get(label) or ("",))
            lines.append(f"对TA的感觉：{label}" + (f"（{hint}）" if hint else ""))
        elif label:
            lines.append(f"对TA的感觉：{label}")
        if care_word:
            lines.append(care_word)
        if focus_word:
            lines.append(focus_word)
        if spare < 0.45:
            lines.append(pick("spare", seed, scale_word(spare, SPARE_WORDS) or ""))
        lines.extend(self._emotion_lines(data, seed))
        tag = ""
        if cfg.mood_tag_enabled:
            try:
                tag = core.mood.tag(user_id)
            except Exception:
                tag = ""
        if tag:
            lines.append(f"她今天的心情：{tag}")
        return [line for line in lines if line]

    @staticmethod
    def _emotion_lines(data: Dict[str, Any], seed: List[Any]) -> List[str]:
        """把偏出常态的情绪轴说成第一人称的态度句。

        只说心里怎么样，不说「所以要短看、要顶回去」——后者是插件替她决定怎么开口。
        措辞按天抽，同一状态一天里不换说法，隔天换一套。"""
        try:
            affection = float(data.get("affection", 50.0))
            libido = float(data.get("libido", 25.0))
            aggression = float(data.get("aggression", 15.0))
            base_affection = float(data.get("base_affection", affection))
            base_aggression = float(data.get("base_aggression", aggression))
        except (TypeError, ValueError):
            return []
        lines: List[str] = []
        if aggression >= AGGRAVATED_AGGRESSION:
            if aggression >= base_aggression + 8:
                lines.append(pick("angry_recent", seed, (
                    "你这两天积了点火，现在对TA有点意见",
                    "攒着点事，这会儿说话不太耐烦",
                    "火压在底下，对TA没那么耐听",
                )))
            else:
                lines.append(pick("angry_always", seed, (
                    "你对TA本来就没多少耐心",
                    "跟TA说话你一直懒得绕弯",
                )))
        elif libido >= EAGER_LIBIDO and aggression < 15:
            lines.append(pick("eager", seed, (
                "你今天挺想跟TA多聊两句的",
                "今天话头顺，跟TA聊得动",
            )))
        if affection <= COLD_AFFECTION:
            lines.append(pick("cold", seed, ("还没熟到什么都想说", "跟TA还隔着层东西")))
        elif affection >= WARM_AFFECTION and aggression < 12:
            lines.append(pick("warm", seed, ("TA说的话你愿意接", "TA的事你肯上心")))
        if base_affection - affection >= 6:
            lines.append(pick("cooled", seed, (
                "你对TA比前阵子淡了，之前不是这个态度",
                "前阵子不是这个态度的，这段时间淡下来了",
            )))
        return lines[:2]

    def _day_lines(
        self, detailed: bool, include_doing: bool = True, exclude: Optional[List[str]] = None
    ) -> List[str]:
        """今天到这会她干了什么。只从日程算，不额外调任何东西。

        `exclude` 是【刚刚】那块已经说过的「这期间她做了什么」——同一份日程的两个切法，
        两处各说一遍模型会当成两件事。"""
        core = self._core
        try:
            now = core.clock.now()
            phrases = day_phrases(
                core.schedule.current_slots(),
                now.hour * 60 + now.minute,
                past_limit=DAY_ITEMS_FULL if detailed else DAY_ITEMS_LOW,
            )
        except Exception:
            return []
        lines = day_lines(
            phrases,
            DAY_ITEMS_FULL if detailed else DAY_ITEMS_LOW,
            include_doing=include_doing,
        )
        if exclude and lines:
            # day_lines 拼出来是「今天到这会做过：A；B」，先按「：」把前缀切掉再逐条比，
            # 不然第一条永远留在【今天】里，【刚刚】和它就重复了。
            prefix, sep, tail = lines[0].partition("：")
            items = [part for part in tail.split("；") if part]
            kept = [part for part in items if not any(part == x or part.endswith(x) for x in exclude)]
            lines = [prefix + sep + "；".join(kept)] if kept else []
        return lines

    def _memory_lines(self, user_id: str, detailed: bool) -> List[str]:
        """TA 之前说过什么。没记过就不注入，而不是编一个。"""
        try:
            lines = self._core.mood.recall_lines(user_id, TOPICS_FULL if detailed else TOPICS_LOW)
        except Exception:
            return []
        return lines

    def _nickname_line(self, user_id: str, is_group: bool) -> str:
        """称呼：必需品。没记下过就说没记下，而不是不提——真人会想「我还不知道叫你什么」。"""
        cfg = self.config
        if not (cfg.mood_enabled and (not is_group or cfg.mood_enabled_in_group)):
            return ""
        try:
            nickname = self._core.mood.nickname(user_id)
        except Exception:
            return ""
        return f"你管TA叫{nickname}" if nickname else "还没问过TA叫什么"

    def _scene_lines(self, is_group: bool, detailed: bool = True) -> List[str]:
        """场景必需品：几点、在她的哪个城市、这是群聊还是私聊。三样永远在。"""
        cfg = self.config
        lines: List[str] = [
            "群聊，周围还有人看着" if is_group else "私聊，只有你和TA"
        ]
        now = self._core.clock.now()
        line = f"{now.strftime('%H:%M')} {self._time_of_day(now.hour)}"
        try:
            today = self._core.snapshot(refresh=False).get("today") or ""
        except Exception:
            today = ""
        if today:
            line = f"{today} {line}"
        try:
            holiday = self._core.clock.holiday(now)
        except Exception:
            holiday = ""
        if holiday:
            line += f"，{holiday}"
        lines.append(line)
        if cfg.show_city_time_in_low_intrusion:
            try:
                city = (self._core.snapshot(refresh=False).get("city") or "").strip()
            except Exception:
                city = ""
            if city:
                lines.append(f"你在{city}")
        weather = {}
        try:
            weather = self._core.snapshot(refresh=False).get("weather") or {}
        except Exception:
            weather = {}
        temp = _short_weather(weather)
        if temp:
            lines.append(temp)
        return lines

    def _state_lines(self, snap: Dict[str, Any], detailed: bool) -> List[str]:
        """精力与手上在做的事。

        开了生理层之后，low 档不再注入「精力状态良好」这类标签：体感已经把它说得更准，
        同时出现两份只会让模型去调和问题。关掉 soma 时仍需要它兜底。
        """
        lines: List[str] = []
        if detailed or not self.config.soma_enabled:
            lines.append(f"精力{snap['energy']['text']}")
        cycle = str(snap.get("cycle") or "").strip()
        if cycle and detailed:
            lines.append(cycle)
        proc = self._core.process.current()
        name = str(proc.get("name", "")).strip()
        phase = str(proc.get("phase", "")).strip()
        if name and name not in {"休息", "自由活动"}:
            lines.append(f"手上在做的：{name}/{phase}" if phase and phase != name else f"手上在做的：{name}")
        return lines

    def _behavior_lines(
        self,
        user_id: str,
        events: List[Dict[str, Any]],
        agency: Dict[str, float],
        with_previous: bool,
    ) -> List[str]:
        """隔了多久 + TA离开前那句原话 + 她这期间自己干了什么。

        这一块的目的是让她**感觉到那段间隔**：真人看到「三小时十二分」不会有反应，
        看到「TA有 3 小时 12 分没吭声了，离开前说的是『我先去开会』，这期间我把方案
        改完了一版」才会自己想「这人是真忙还是不想理我」。所以三段都是事实，
        一句指导话都不写（旧版那句「这是一次性的背景，不是每天要重新问候的事」删了）。
        """
        if not events:
            return []
        top = events[0]
        event_type = str(top.get("type", ""))
        data = top.get("data") or {}
        seconds = data.get("gap_seconds")
        seed = self._seed(user_id)
        lines: List[str] = []
        if seconds is not None:
            gap = humanize_gap(float(seconds))
            lines.append(pick("since", seed, SINCE_WORDS).format(gap=gap))
        else:
            lines.append(EVENT_TEXT.get(event_type) or "最近出现了交流变化")
        previous = data.get("previous_message") if with_previous else None
        if previous:
            previous = str(previous).replace("\n", " ").strip()[:60]
            if previous:
                lines.append(f"TA离开前说的是「{previous}」")
        during = self._during_gap_lines(seconds)
        if during:
            lines.append("这期间她" + "；她".join(during))
        if agency:
            strong = [
                AGENCY_LABELS[key]
                for key, value in sorted(agency.items(), key=lambda item: float(item[1]), reverse=True)
                if key in AGENCY_LABELS and float(value) >= 0.62
            ]
            if strong:
                lines.append("你现在更" + "、更".join(strong[:2]))
        return lines

    def _during_gap_lines(self, seconds: Any) -> List[str]:
        """她这期间在自己过日子：TA上一条消息到现在，她做完的那几件事。

        只说事实，不猜TA去干嘛了——「TA是不是去忙了」这种念头该是她自己从这些事实里
        生出来的；插件替她想好就等于替她说话。"""
        try:
            gap_seconds = float(seconds)
        except (TypeError, ValueError):
            return []
        if gap_seconds < 1800:
            return []
        core = self._core
        try:
            now = core.clock.now()
            minutes_now = now.hour * 60 + now.minute
            started = max(0, minutes_now - int(gap_seconds // 60))
            return done_between(
                core.schedule.current_slots(), started, minutes_now, DAY_ITEMS_FULL
            )
        except Exception:
            return []


    # ------------------------------------------------------------------
    # 三档组装
    # ------------------------------------------------------------------

    def _build_low(
        self, user_id: str, is_group: bool, events, agency, text: str, interest: Dict[str, float]
    ) -> str:
        snap = self._core.snapshot(refresh=False)
        parts: List[str] = [self._block("此刻", self._scene_lines(is_group, detailed=False))]

        body = self._feelings_lines(MAX_FEELINGS_LOW, FEELING_THRESHOLD_LOW) + self._night_lines()
        if body:
            parts.append(self._block("感觉", body))

        relation = self._relation_lines(user_id, is_group, detailed=False, interest=interest)
        nickname = self._nickname_line(user_id, is_group)
        if nickname:
            relation = relation + [nickname]
        if relation:
            parts.append(self._block("对TA", relation))

        hands = self._state_lines(snap, detailed=False)
        during = self._during_gap_lines((events[0].get("data") or {}).get("gap_seconds") if events else None)
        # 「手上在做的」已经说了此刻，今天这块就别再说一遍「现在在…」。
        day = self._block(
            "今天", self._day_lines(detailed=False, include_doing=not hands, exclude=during)
        )

        situ = self._behavior_lines(
            user_id, events, agency,
            with_previous=self.config.last_interaction_mode == "with_last_msg",
        )
        if situ:
            parts.append(self._block("刚刚", situ))

        if hands:
            parts.append(self._block("身边", hands))
        if day:
            parts.append(day)
        memory = self._block("记得", self._memory_lines(user_id, detailed=False))
        if memory:
            parts.append(memory)

        return "\n".join(part for part in parts if part)

    def _build_full(
        self, user_id: str, is_group: bool, events, agency, text: str, interest: Dict[str, float]
    ) -> str:
        snap = self._core.snapshot(refresh=False)
        parts: List[str] = [self._block("此刻", self._scene_lines(is_group, detailed=True))]
        state = self._state_lines(snap, detailed=True)
        if snap.get("social_energy"):
            state.append(f"社交能量{snap['social_energy']['text']}")
        if state:
            parts.append(self._block("身边", state))
        body = self._feelings_lines(MAX_FEELINGS_FULL, 0.0) + self._night_lines()
        if body:
            parts.append(self._block("感觉", body))
        relation = self._relation_lines(user_id, is_group, detailed=True, interest=interest)
        nickname = self._nickname_line(user_id, is_group)
        if nickname:
            relation = relation + [nickname]
        if relation:
            parts.append(self._block("对TA", relation))
        during = self._during_gap_lines((events[0].get("data") or {}).get("gap_seconds") if events else None)
        day = self._block("今天", self._day_lines(detailed=True, include_doing=False, exclude=during))
        situ = self._behavior_lines(
            user_id, events, agency,
            with_previous=self.config.last_interaction_mode == "with_last_msg",
        )
        if situ:
            parts.append(self._block("刚刚", situ))
        if day:
            parts.append(day)
        memory = self._block("记得", self._memory_lines(user_id, detailed=True))
        if memory:
            parts.append(memory)
        try:
            recent = self._core.process.recent()
        except Exception:
            recent = []
        if recent:
            parts.append(self._block("最近", ["最近做过：" + "、".join(recent)]))
        return "\n".join(part for part in parts if part)

    def _finish(self, text: str, cfg: HumanoidConfig) -> str:
        if not text:
            return ""
        header = f"{MARK_PREFIX} {MARK_VERSION} uid={_uid_tag(self._core.role_id)}〕"
        full = header + "\n" + text
        limit = INJECT_MAX_CHARS.get(cfg.inject_activity_context, 520)
        if len(full) > limit:
            # 按块切而不是按字硬截：截到半句上模型会自己补下去。
            kept: List[str] = []
            used = 0
            for line in full.split("\n"):
                cost = len(line) + 1
                if used + cost > limit:
                    break
                kept.append(line)
                used += cost
            full = "\n".join(kept)
        return full

    @staticmethod
    def _time_of_day(hour: int) -> str:
        if 5 <= hour < 8:
            return "清晨"
        if 8 <= hour < 12:
            return "上午"
        if 12 <= hour < 14:
            return "中午"
        if 14 <= hour < 18:
            return "下午"
        if 18 <= hour < 21:
            return "傍晚"
        if 21 <= hour < 24:
            return "晚上"
        return "深夜"


def _uid_tag(role_id: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", str(role_id))[-8:] or "self"


def _short_weather(weather: Dict[str, Any]) -> str:
    """天气只留一句能用的；没配好时直接不注入，而不是把配置说明书念给模型听。"""
    env = str(weather.get("env", "")).strip()
    if not env:
        return ""
    if any(word in env for word in ("未填", "未开启", "获取中", "没配天气")):
        return ""
    return env.replace("当前城市", "这边")[:26]
