"""把身体状态编译成模型能「感觉到」的上下文。

v2.13.x 的做法是把状态拼成一行标签卡（`精力状态一般；情绪调皮；社交能量低`）塞进
用户消息，模型看到的是一堆**数据**，于是只能再补一句「禁止提及任何具体数据」去堵它
的复述冲动。这一版改成三段：

1. **体感**：第一人称的生理感受短句，由 soma 的轴 + 显著度门控产生。多数轴多数时候
   不出现——真人也不会每条消息都报告自己的状态。
2. **话的份量**：身体这一轮够得着什么（大概多长、想不想问、适不适合长篇）。它改变的是
   形式而不是措辞风格，所以比「语气慵懒」有效；但它是**处境描述**，不是规矩。
3. **场景与关系**：群聊还是私聊、时间地点天气、怎么称呼对方、对他的情绪标签。

v2.15.1 把这一层里所有「替她说好的台词」和「不许怎么样」的禁令都改成了状态陈述：
这个插件的职责是给 AI 一具身体，不是替她开口，也不是给她立规矩。台词（「你现在需要
休息，明天再聊吧」）会让她的话变成插件写的句子，禁令则根本执行不了——插件拦不住回复。

三档注入 `full / low / mood_only` 都真正区分开；v2.13.2 里 `mood_only` 与 `low` 走
的是同一分支，`enable_chat_awareness`、`show_city_time_in_low_intrusion`、
`night_mode_force_sleep`、`last_interaction_mode` 四个配置项则完全没有代码读。
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .core_instance import HumanoidCoreInstance

from .config import HumanoidConfig
from .data.mood_map import get_mood_label
from .services.schedule import day_lines, day_phrases

_CJK_RANGES = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")

# 注入块的硬长度上限（字符）。每次聊天请求都追加这一条，所以它必须是个定值而不是
# 「看拼出来多少」。v2.15 多给了两块生活细节（今天干了什么 / 你们聊到过什么），
# 上限相应抬高了一点，仍然卡在一千 token 以内。
INJECT_MAX_CHARS = {"low": 640, "full": 900, "mood_only": 330}


def estimate_tokens(text: str) -> int:
    """估算 token 数：中日韩一字算一 token，其余按 3.5 字符一 token。

    与自主拟人社交侧同口径；没装分词器时宁可高估也不能低估预算。
    """
    if not text:
        return 0
    cjk = len(_CJK_RANGES.findall(text))
    return int(math.ceil(cjk + (len(text) - cjk) / 3.5))

# 情绪标签 → 她此刻对TA的心气。写的是状态，不是「语气要怎样」的指令：
# 怎么说由她自己定，插件只负责把这份心思放在那儿。
MOOD_TONE_HINTS = {
    "亲密": "跟TA没什么防备",
    "依恋": "想黏着TA一点",
    "信赖": "TA说的话你愿意当真",
    "热情": "对TA讲的事有兴趣",
    "友好": "愿意搭理TA",
    "平常": "",
    "疏远": "不想跟TA多说",
    "冷淡": "没什么想说的",
    "敌视": "看TA有点不顺眼",
    "警惕": "先看看TA怎么讲",
}

# 注入块收尾的两句：一句说明上面那些是什么，一句给出许可。都不带禁令。
BOUNDARY_LINE = (
    "上面这些是你的身体、今天已经过掉的那段日子、你对TA此刻的心气。"
    "它们是你说这些话时的处境，不是要你汇报的表格，也不是要你照着念的台词"
    "——人聊天时不会念自己的体检单。"
)
# 上一句只挡住「报数据」，没告诉她感觉可以就是她说话的样子，模型于是永远中性。
PERMISSION_LINE = (
    "困了、烦了、饿了、想跟TA多聊两句、心里对TA有点意见，都可以直接就是你说话的样子。"
    "说多长、想不想问、怎么接下去，你自己定：不用永远热情得体，也不必逐条回应上面这些。"
)

# 低注入档下最多给几条体感：真人多数时候不觉得自己在报备身体。
MAX_FEELINGS_LOW = 2
MAX_FEELINGS_FULL = 5
FEELING_THRESHOLD_LOW = 0.55

# 注入块里「今天干了什么」与「记得的事」各给多少。这两块是新增的生活细节，
# 但注入每条聊天请求都追加一次，必须控住体积。
DAY_ITEMS_LOW = 2
DAY_ITEMS_FULL = 3
TOPICS_LOW = 2
TOPICS_FULL = 4

# 情绪偏到一定程度就直接用第一人称告诉她，而不是只贴一个标签词。
AGGRAVATED_AGGRESSION = 28.0
EAGER_LIBIDO = 32.0
COLD_AFFECTION = 32.0
WARM_AFFECTION = 72.0

EVENT_TEXT = {
    "conversation_started": "刚开始交流",
    "conversation_resumed": "对方刚重新接上对话",
    "user_returned": {
        "medium_return": "对方隔了一阵重新出现",
        "long_return": "对方隔了挺久重新出现",
        "short_return": "对方刚回来",
    },
    "long_gap": "对方隔了很久重新出现",
}

FIRST_CONTACT_LINE = "这是你和TA的第一次对话"


def humanize_gap(seconds: float) -> str:
    """把秒数说成一句时长：12 分钟、3 小时 12 分、2 天 4 小时。

    旧版这里用的是「对方隔了约2~6小时重新出现」这类档位话术，把一件客观事描述成
    一种情境；既然要报就报准。拿不到准确隔时才退回上面的档位词。"""
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
    ) -> str:
        cfg = self.config
        events = events or []
        agency = agency or {}
        mode = cfg.inject_activity_context

        if mode == "mood_only":
            parts = []
            relation = self._block("关系", self._relation_lines(user_id, is_group, detailed=False))
            if relation:
                parts.append(relation)
            feelings = self._block("感觉", self._feelings_lines(max_items=1, threshold=0.7))
            if feelings:
                parts.append(feelings)
            memory = self._block("记得", self._memory_lines(user_id, detailed=False))
            if memory:
                parts.append(memory)
            return self._finish("\n".join(parts), cfg)

        if mode == "full":
            return self._finish(self._build_full(user_id, is_group, events, agency), cfg)

        return self._finish(self._build_low(user_id, is_group, events, agency), cfg)

    # ------------------------------------------------------------------
    # 分块
    # ------------------------------------------------------------------

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

    def _form_lines(self) -> List[str]:
        """身体这一轮够得着什么：说处境，不下规矩。"""
        core = self._core
        cfg = self.config
        if not cfg.soma_enabled:
            return []
        try:
            policy = core.soma.form_policy(float(core.energy.energy), float(core.social.value))
        except Exception:
            return []
        lines: List[str] = []
        max_chars = int(policy.get("max_chars", 120))
        if max_chars <= 24:
            lines.append(f"这副身体这会儿只够说一两句，{max_chars}字上下的量")
        elif max_chars <= 60:
            lines.append(f"话到嘴边也就{max_chars}字上下，再多就散了")
        elif not policy.get("long_reply_ok", True):
            lines.append("现在这状态展开不了一段长篇")
        question_bias = float(policy.get("question_bias", 0.35))
        if question_bias <= 0.1:
            lines.append("这会儿没心思追问什么，TA说的先接住")
        elif question_bias >= 0.5:
            lines.append("对TA那件事有点想知道下文")
        if policy.get("burst_ok"):
            lines.append("想说的东西多，分成几条短的发也行")
        return lines

    def _night_lines(self) -> List[str]:
        """夜间/睡眠：真正按 night_mode_force_sleep 分强弱。"""
        cfg = self.config
        core = self._core
        if not cfg.night_mode_enabled or not core.clock.is_night():
            return []
        asleep = False
        if cfg.soma_enabled:
            try:
                asleep = core.soma.snapshot().get("asleep", 0.0) >= 1.0
            except Exception:
                asleep = False
        return build_night_lines(cfg, core.clock.is_deep_sleep(), asleep)

    def _relation_lines(self, user_id: str, is_group: bool, detailed: bool) -> List[str]:
        cfg = self.config
        core = self._core
        if not (cfg.mood_enabled and (not is_group or cfg.mood_enabled_in_group)):
            return []
        lines: List[str] = []
        try:
            data = core.mood.profile(user_id)
            label = get_mood_label(data["affection"], data["libido"], data["aggression"])
        except Exception:
            return []
        hint = MOOD_TONE_HINTS.get(label)
        lines.append(f"对TA的感觉：{label}" + (f"（{hint}）" if hint else ""))
        lines.extend(self._emotion_lines(data))
        tag = core.mood.tag(user_id)
        if tag and cfg.mood_tag_enabled:
            lines.append(f"心情标签：{tag}")
        return lines

    @staticmethod
    def _emotion_lines(data: Dict[str, Any]) -> List[str]:
        """把三个情绪轴翻成第一人称的态度句。

        光贴一个「敌视/小脾气」的标签词，模型多半看一眼就过；换成「你现在对她有点意见」
        她才会真的带着这份心气说话。只说心里怎么样，不说「所以你要短看、要顶回去」——
        后者是插件替她决定怎么开口。只挑最突出的两条，不拼成情绪清单。
        """
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
                lines.append("你这两天积了点火，现在对TA有点意见")
            else:
                lines.append("你对TA本来就没多少耐心")
        elif libido >= EAGER_LIBIDO and aggression < 15:
            lines.append("你今天挺想跟TA多聊两句的")
        if affection <= COLD_AFFECTION:
            lines.append("还没熟到什么都想说")
        elif affection >= WARM_AFFECTION and aggression < 12:
            lines.append("TA说的话你愿意接")
        if base_affection - affection >= 6:
            lines.append("你对TA比前阵子淡了，之前不是这个态度")
        return lines[:2]

    def _day_lines(self, detailed: bool, include_doing: bool = True) -> List[str]:
        """今天到这会她干了什么。只从日程算，不额外调任何东西。"""
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
        return day_lines(
            phrases,
            DAY_ITEMS_FULL if detailed else DAY_ITEMS_LOW,
            include_doing=include_doing,
        )

    def _memory_lines(self, user_id: str, detailed: bool) -> List[str]:
        """TA 之前说过什么。没记过就不注入，而不是编一个。"""
        try:
            lines = self._core.mood.recall_lines(user_id, TOPICS_FULL if detailed else TOPICS_LOW)
        except Exception:
            return []
        return lines

    def _nickname_line(self, user_id: str, is_group: bool) -> str:
        cfg = self.config
        if not (cfg.mood_enabled and (not is_group or cfg.mood_enabled_in_group)):
            return ""
        nickname = self._core.mood.nickname(user_id)
        return f"你管TA叫{nickname}" if nickname else ""

    def _scene_lines(self, snap: Dict[str, Any], is_group: bool) -> List[str]:
        cfg = self.config
        lines: List[str] = []
        if cfg.enable_chat_awareness:
            lines.append("群聊里，周围还有人看着" if is_group else "只有你和TA两个人在聊")
        now = self._core.clock.now()
        line = f"{snap['today']} {now.strftime('%H:%M')} {self._time_of_day(now.hour)}"
        try:
            holiday = self._core.clock.holiday(now)
        except Exception:
            holiday = ""
        if holiday:
            line += f"，{holiday}"
        lines.append(line)
        if cfg.show_city_time_in_low_intrusion:
            lines.append(f"你在{snap['city']}")
        weather = snap.get("weather") or {}
        temp = _short_weather(weather)
        if temp:
            lines.append(temp)
        return lines

    def _state_lines(self, snap: Dict[str, Any], detailed: bool) -> List[str]:
        """传统状态行。

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
        self, events: List[Dict[str, Any]], agency: Dict[str, float], with_previous: bool
    ) -> List[str]:
        if not events:
            return []
        top = events[0]
        event_type = str(top.get("type", ""))
        data = top.get("data") or {}
        mapped = EVENT_TEXT.get(event_type, "")
        seconds = data.get("gap_seconds")
        if seconds is None and event_type not in EVENT_TEXT:
            event_text = FIRST_CONTACT_LINE
        elif seconds is not None:
            event_text = f"距上次说话 {humanize_gap(float(seconds))}"
        elif isinstance(mapped, dict):
            event_text = mapped.get(str(data.get("gap_bucket", "")), "对方隔了一段时间重新出现")
        else:
            event_text = mapped or "最近出现了交流变化"
        lines = [event_text]
        previous = data.get("previous_message") if with_previous else None
        if previous:
            previous = str(previous).replace("\n", " ").strip()[:60]
            if previous:
                lines.append(f"TA离开前说的是「{previous}」")
        if agency:
            strong = [
                AGENCY_LABELS[key]
                for key, value in sorted(agency.items(), key=lambda item: float(item[1]), reverse=True)
                if key in AGENCY_LABELS and float(value) >= 0.62
            ]
            if strong:
                lines.append("你现在更" + "、更".join(strong[:2]))
        return lines

    # ------------------------------------------------------------------
    # 两档组装
    # ------------------------------------------------------------------

    def _build_low(self, user_id: str, is_group: bool, events, agency) -> str:
        snap = self._core.snapshot(refresh=False)
        parts: List[str] = [self._block("此刻", self._scene_lines(snap, is_group))]

        feelings = self._feelings_lines(MAX_FEELINGS_LOW, FEELING_THRESHOLD_LOW)
        night = self._night_lines()
        body = feelings + night
        if body:
            parts.append(self._block("我的感觉", body))

        form = self._form_lines()
        if form:
            parts.append(self._block("话的份量", form))

        relation = self._relation_lines(user_id, is_group, detailed=False)
        nickname = self._nickname_line(user_id, is_group)
        if nickname:
            relation = relation + [nickname]
        if relation:
            parts.append(self._block("对TA", relation))

        situ = self._behavior_lines(
            events, agency, with_previous=self.config.last_interaction_mode == "with_last_msg"
        )
        if situ:
            parts.append(self._block("刚刚", situ + ["这是一次性的背景，不是每天要重新问候的事"]))

        hands = self._state_lines(snap, detailed=False)
        if hands:
            parts.append(self._block("身边", hands))

        # 「手上在做的」已经说了此刻，今天这条就别再说一遍，只补前面和后面。
        day = self._block("今天", self._day_lines(detailed=False, include_doing=not hands))
        if day:
            parts.append(day)
        memory = self._block("记得", self._memory_lines(user_id, detailed=False))
        if memory:
            parts.append(memory)

        return "\n".join(part for part in parts if part)

    def _build_full(self, user_id: str, is_group: bool, events, agency) -> str:
        snap = self._core.snapshot(refresh=False)
        lines: List[str] = []
        lines += self._scene_lines(snap, is_group)
        lines += self._state_lines(snap, detailed=True)
        lines += [f"社交能量{snap['social_energy']['text']}" if snap.get("social_energy") else ""]
        lines += self._feelings_lines(MAX_FEELINGS_FULL, 0.0)
        lines += self._night_lines()
        lines += self._form_lines()
        lines += self._relation_lines(user_id, is_group, detailed=True)
        nickname = self._nickname_line(user_id, is_group)
        if nickname:
            lines.append(nickname)
        lines += self._behavior_lines(
            events, agency, with_previous=self.config.last_interaction_mode == "with_last_msg"
        )
        lines += self._day_lines(detailed=True, include_doing=not any("手上在做的" in x for x in lines))
        lines += self._memory_lines(user_id, detailed=True)
        recent = self._core.process.recent()
        if recent:
            lines.append("最近做过：" + "、".join(recent))
        return ("【拟人状态】" + "；".join(line for line in lines if line) + "。")

    def _finish(self, text: str, cfg: HumanoidConfig) -> str:
        if not text:
            return ""
        full = text + "\n" + BOUNDARY_LINE + "\n" + PERMISSION_LINE
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
            kept.append("（以上只是背景，不必逐条回应，也不用原样说给对方听。）")
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


def build_night_lines(cfg: HumanoidConfig, is_deep: bool, asleep: bool) -> List[str]:
    """夜间：把身体的事实说清楚。纯函数，调用方负责保证现在确实落在夜间窗口里。

    v2.15.1 起这一档不再写台词：旧版让插件直接给出「我现在需要休息，明天再聊吧」，
    那等于插件替她把话说完。`night_mode_force_sleep` 现在决定的是**她说得多硬**——
    开着时直接讲她还在睡、是被震醒的；关着时只讲夜已深。回不回、怎么回是她自己的事
    （插件本来就拦不住回复，写禁令也只是让模型猜）。"""
    if cfg.night_mode_force_sleep:
        if is_deep or asleep:
            return ["你还在睡，是手机震醒的那种：眼睛睁不开，脑子没转，话到嘴边只剩一两个字"]
        return ["夜已经很深，你其实困得不太想开口了"]
    if is_deep or asleep:
        return ["刚被吵醒，迷迷糊糊，句子是断的"]
    return ["夜里慵懒，话很轻很短"]


def _short_weather(weather: Dict[str, Any]) -> str:
    """天气只留一句能用的；没配好时直接不注入，而不是把配置说明书念给模型听。"""
    env = str(weather.get("env", "")).strip()
    if not env:
        return ""
    if any(word in env for word in ("未填", "未开启", "获取中", "没配天气")):
        return ""
    return env.replace("当前城市", "这边")[:26]
