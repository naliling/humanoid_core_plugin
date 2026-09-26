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
* **不搬人设**——v2.16.x 曾经从人设里摘「说话方式」的原句进上下文。它是按「风格/语气/口头禅」
  这类关键词从句子里挑，挑中的是人设里写给作者看的**示例对话**，于是情色内容、别人的 @
  都原样进了每一轮聊天；而且它走的是用户消息（不是 system_prompt），模型会把示例
  当成「她刚说过的话」去接。AstrBot 自己已经把人设放进 system_prompt，插件再搬一遍
  只有副作用。
"""

from __future__ import annotations

import math
import re
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .core_instance import HumanoidCoreInstance

from .config import HumanoidConfig
from .data.mood_map import get_mood_label
from .services.schedule import day_lines, day_phrases, done_between, just_done
from .wording import (
    AGENCY_CONTINUATION,
    AGENCY_INITIATIVE,
    CARE_WORDS,
    FOCUS_WORDS,
    SINCE_WORDS,
    SPARE_WORDS,
    pick,
    scale_word,
)

_CJK_RANGES = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")

# 事实块的开头标记。注入走的是用户消息，而 AstrBot 会把拼装后的用户消息存进会话历史，
# 所以每次注入前要把历史里旧的块抹掉（`main.py` 负责），标记就是给这一步认路用的。
# 换文案时把 v 后面的数字加一，老会话里的旧块会被当成不认识的内容直接清掉。
MARK_PREFIX = "〔她的身体与生活"
MARK_VERSION = "v10"

# 给 system_prompt 的那段话：说明下面这些是什么、按什么方式读它。稳定文本，不含事实。
# 只划参考边界，不下指令：说什么、怎么说、说多长，都由她自己判断。
FRAMING_TEXT = (
    "\n\n【她的身体与生活】\n"
    "以下是她当前的真实处境与身体状态，作为背景参考。"
    "自然融入即可，无需刻意提及或逐条回应；如何回应、说什么、说多长，由她自己判断。"
)

# 注入块的尺寸红线（字符）：防止哪天拼装逻辑把注入胀到几千字。
# 实际卡住体积的是 cfg.inject_token_budget（超了按显著度整句丢）。
INJECT_MAX_CHARS = {"medium": 2000, "full": 3000, "mood_only": 500}


def estimate_tokens(text: str) -> int:
    """估算 token 数：中日韩一字算一 token，其余按 3.5 字符一 token。

    与自主拟人社交侧同口径；没装分词器时宁可高估也不能低估预算。
    """
    if not text:
        return 0
    cjk = len(_CJK_RANGES.findall(text))
    return int(math.ceil(cjk + (len(text) - cjk) / 3.5))


# 情绪标签逆映射已删（v2.16.7）：「态度亲昵」「带有敌意」这类括号注释是在教她用什么
# 语气开口，属于限制发言。现在只给标签本身（如「亲密」），怎么说出来由她自己定。

# 低注入档下最多给几条体感：真人多数时候不觉得自己在报备身体。
MAX_FEELINGS_LOW = 2
MAX_FEELINGS_FULL = 5
FEELING_THRESHOLD_LOW = 0.55

# 「记得的事」给几句原话。一条就够：给多了模型会当成一份话题清单逐条追着问，
# 而真人只记得一两件事，剩下的过半天就忘了。
TOPICS_LOW = 1
TOPICS_FULL = 1

# 心气词（压着火 / 惦记TA / 拧巴）的触发门槛。刻意高于配置里的初始值
# （aggression 28、libido 34）——门槛压着初始值时，这几句几乎每条消息都在，
# 于是「一股气没处发，有点惦记TA，她有点拧巴，矛盾」成了常驻背景。
EMOTION_AGGRESSION_ONSET = 40.0
EMOTION_LIBIDO_ONSET = 42.0

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


def _core_epoch(core) -> float:
    """从 core 的钟上取 epoch：注入里所有「过了多久」都得跟她的钟同源，
    否则冻结/换城市的场合会出现身体按一台钟走、措辞按另一台钟算的分叉。"""
    try:
        return float(core.now_epoch())
    except Exception:
        return time.time()


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
            # 最轻的一档也带着四样必需品：时间、场景、称呼、今天。省掉的只能是生活细节。
            relation = self._relation_lines(user_id, is_group, detailed=False, interest=interest)
            nickname = self._nickname_line(user_id, is_group)
            if nickname:
                relation = relation + [nickname]
            tendency = self._tendency_lines(agency, interest, self._seed(user_id))
            if tendency:
                # 倾向这一层也跟着进最轻的一档：它不是「生活细节」，
                # 而是「她现在想不想说话」——缺了它，模型只能拿身体读数自己猜
                relation = relation + tendency
            sents = [
                (self._sentence(self._scene_lines(is_group)), 10.0),
                (self._sentence(relation), 8.0),
                (self._sentence(self._feelings_lines(max_items=1, threshold=0.7)), 7.0),
                (self._sentence(self._day_lines(detailed=False)), 5.0),
                (self._sentence(self._memory_lines(user_id, detailed=False)), 6.0),
            ]
            return self._finish(sents, cfg)

        if mode == "full":
            return self._finish(self._build_full(user_id, is_group, events, agency, text, interest), cfg)

        return self._finish(self._build_medium(user_id, is_group, events, agency, text, interest), cfg)

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

    def _sentence(self, lines: List[str]) -> str:
        """把一组事实拼成一句自然的话。没有标题、没有标签——标签是束缚词，
        模型看到【感觉】就会以为该谈感觉。事实自己会说话。"""
        lines = [line for line in lines if line]
        if not lines:
            return ""
        return "，".join(lines)

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
        if not self.config.show_sleep_window:
            # 「她这会儿在睡」也是睡眠信息：关掉不提睡眠时段，就不能换句话又说她在睡。
            picked = [item for item in picked if "在睡" not in item[1]]
        picked.sort(key=lambda item: item[0], reverse=True)
        # 体感句一律披上主语，但 soma 自己带主语的整句不能再披一遍。
        return [
            text if text.startswith("她") else f"她{text}" for _, text in picked[:max_items]
        ]

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
                return []
            # 到点了但还醒着：说「她在睡」是假话，熬夜的人这个点正精神。
            lines.append("这会儿是她的睡眠时段")
        else:
            lines.append("这会儿是她的夜间作息")
        return lines

    def _tendency_lines(
        self,
        agency: Dict[str, float],
        interest: Dict[str, float],
        seed: List[Any],
    ) -> List[str]:
        """她此刻的倾向：想不想说、想不想接着、注意力在哪、有多少闲。

        这七个数（agency 五轴 + focus/spare）以前算了、传了七层，_behavior_lines 里
        一次都没读过——模型只拿到「她很困」这类身体状态，得自己推「那她大概不想说话」，
        而这恰恰是该给它的参考。给不出倾向，就只能给身体读数；只给读数，模型只能猜。

        措辞上有一条硬要求：**只描述她此刻的状态，不写对模型的要求**。
        「她这会儿挺想说点什么」是状态；「所以你应该主动找TA」是越界。
        """
        def word(key: str, value: Any, ladder) -> str:
            try:
                v = max(0.0, min(1.0, float(value)))
            except (TypeError, ValueError):
                return ""
            # scale_word 返回的是「该档位的多个候选词」组成的元组（取不到时是空串），
            # 必须原样交给 pick 让它抽一个出来，不能再包一层或 or ""——
            # 那会让这里返回 tuple，后面 join 句子时直接报类型错
            return pick(key, seed, scale_word(v, ladder))

        lines: List[str] = []
        for key, source, ladder in (
            ("ag_initiative", agency.get("initiative"), AGENCY_INITIATIVE),
            ("ag_continuation", agency.get("continuation"), AGENCY_CONTINUATION),
            ("focus", interest.get("focus"), FOCUS_WORDS),
            ("spare", interest.get("spare"), SPARE_WORDS),
        ):
            got = word(key, source, ladder)
            if got:
                lines.append(got)
        return lines

    def _relation_lines(
        self, user_id: str, is_group: bool, detailed: bool, interest: Dict[str, float]
    ) -> List[str]:
        """她对TA是什么位置、认识多久、最近一次情绪事件。

        关系只说一句。原先 label、care、affection 偏离三处各说一句，拼出来是
        「她对TA热烈，她和TA十分在意，最近对TA更上心」——同一件事说三遍，
        模型会当成三件事。有明确升降时用升降句（它带方向），否则退到位置词。
        """
        cfg = self.config
        core = self._core
        if not (cfg.mood_enabled and (not is_group or cfg.mood_enabled_in_group)):
            return []
        seed = self._seed(user_id)
        try:
            data = core.mood.profile(user_id)
        except Exception:
            data = {}

        lines: List[str] = []
        position = self._position_line(data, interest, seed)
        if position:
            lines.append(position)
        lines.extend(self._emotion_lines(data, seed))

        # **认识多久不进上下文。** 关系深浅已经由上面那句位置词承载（「她对TA关系亲密」比
        # 「你们认识 400 天」更能指导她怎么说话），而把天数递给模型，它就会拿去念——
        # 「我们认识这么久了」是聊天里最没劲的一句话。想知道天数用 `/你的状态`，那里给你看。
        # 很久没见这件事也不需要专门说一句：间隔那块会报「TA有 7 天没吭声了」。

        # 情绪事件：她记得今天发生过什么（被骂了/被逗开心了），不记得才出戏。
        emo_ev = core.mood.last_emotional_event(user_id)
        if emo_ev:
            lines.append(emo_ev[0])
        return [line for line in lines if line]

    def _position_line(
        self, data: Dict[str, Any], interest: Dict[str, float], seed: List[Any]
    ) -> str:
        """她对TA的位置：升降 / 在意度 / 关系标签，三者只出一句。

        三种说法统一用「她对TA…」开头，拼进句子里读起来是一件事而不是三件。
        顺序有讲究：好感度相对基线有明确升降时，升降句最有用（它说了往哪边），
        「热烈」「十分在意」这种静态词反而不如它。
        """
        if not data:
            return ""
        try:
            affection = float(data.get("affection", 50.0))
            base = float(data.get("base_affection", affection))
        except (TypeError, ValueError):
            return ""
        if affection - base >= 8.0:
            return f"她对TA{pick('rel_shifted_up', seed, ('最近更上心', '心里离TA近了些'))}"
        if base - affection >= 8.0:
            return f"她对TA{pick('rel_shifted_down', seed, ('最近淡了些', '心里离TA远了些'))}"
        try:
            care = max(0.0, min(1.0, float(interest.get("care", 0.5))))
        except (TypeError, ValueError):
            care = 0.5
        try:
            label = core.mood.stable_label(user_id)
        except Exception:
            label = get_mood_label(
                float(data.get("affection", 50.0)),
                float(data.get("libido", 25.0)),
                float(data.get("aggression", 15.0)),
            )
        # 在意度高的时候交给关系标签（词表细，热烈/眷恋/吃醋都从这里来）；在意度低的时候
        # 标签往往还在说「调皮」这种好感度词，而真正该说的是「关系疏远、不常联系」——
        # 那才是她对这段关系的定位。两边只出一句，不并列。
        if care >= 0.62 and label:
            return f"她对TA{label}"
        care_word = pick("care", seed, scale_word(care, CARE_WORDS))
        if care_word:
            return f"她对TA{care_word}"
        return f"她对TA{label}" if label else ""

    @staticmethod
    def _days_line(elapsed: float) -> str:
        """认识多久——**只给 `/你的状态` 面板看，不进上下文**。

        「你们认识几天了」这种档位话回答不了「几天」，而具体天数递进模型会被拿去念。
        面板里给准数，是为了你自己能核对；模型那边靠关系位置词（「关系亲密／普通」）就够。
        """
        seconds = max(0.0, float(elapsed))
        days = seconds / 86400.0
        if days < 1.0:
            hours = int(seconds // 3600)
            return "刚认识上" if hours < 1 else f"认识 {hours} 个小时"
        whole = int(days)
        if whole < 30:
            return f"认识 {whole} 天"
        if days < 365:
            return f"认识 {int(days // 30)} 个月"
        return f"认识 {int(days // 365)} 年"

    @staticmethod
    def _emotion_lines(data: Dict[str, Any], seed: List[Any]) -> List[str]:
        """她此刻的心气：最多一句。

        原先压着火、惦记TA、拧巴三处各自判阈值，谁也不看谁，于是气一上来就拼成
        「一股气没处发，有点惦记TA，她有点拧巴」——既矛盾又重复。这里按谁压过谁
        排一次，只出最重的那一句。
        """
        if not data:
            return []
        try:
            libido = float(data.get("libido", 25.0))
            aggression = float(data.get("aggression", 15.0))
        except (TypeError, ValueError):
            return []
        angry = aggression >= EMOTION_AGGRESSION_ONSET
        eager = libido >= EMOTION_LIBIDO_ONSET
        if angry and eager:
            return [pick("emo_tense", seed, ("她心里两个念头抣着", "她有点拧巴", "又气又放不下"))]
        if angry:
            return [pick("emo_angry", seed, ("心里压着火", "一股气没处发"))]
        if eager:
            # 原来的备选是「心思飘过去了」——那和「有点惦记TA」意思正相反，而且和
            # FOCUS_WORDS 的「她心思飘在别处」撞车，同一句里能出现两句互相打架的。
            # 「惦记」这一维的说法要同向。
            return [pick("emo_miss", seed, ("有点惦记TA", "心思在TA那边"))]
        return []

    def _echo_lines(self) -> List[str]:
        """她刚：带着上一件事的余韵进来。全是刚发生的时间性事实。

        刚睡醒和睡了两小时后是两回事，刚在运动和上午在运动也是两回事——
        余韵只在刚发生的那段时间里存在，过了就不提。
        """
        core = self._core
        lines: List[str] = []
        try:
            snap = core.soma.snapshot()
        except Exception:
            snap = {}
        if snap.get("asleep", 0.0) < 1.0:
            wake = float(core.soma.data.get("last_wake_at", 0.0) or 0.0)
            slept = float(snap.get("last_sleep_hours", 0.0) or 0.0)
            if wake > 0 and slept >= 0.5:
                mins = (_core_epoch(self._core) - wake) / 60.0
                if 0 < mins <= 90:
                    lines.append(
                        f"她刚睡醒，睡了{slept:.0f}小时" if slept >= 1.0 else "她刚睡醒"
                    )
        try:
            slots = core.schedule.current_slots()
        except Exception:
            slots = []
        if slots:
            now = core.clock.now()
            just = just_done(slots, now.hour * 60 + now.minute)
            if just:
                lines.append(f"她刚在{just}")
        # 做事进度：刚开始和做了一阵了不是一回事，时长从 process 现算。
        try:
            dur = float((core.process.current() or {}).get("duration_minutes", 0) or 0)
        except Exception:
            dur = 0.0
        if 0 < dur < 15:
            lines.append("手上这事刚开始")
        elif dur > 60:
            lines.append("手上这事做了一阵了")
        return lines

    @staticmethod
    def _drop_echoed_in_day(echo: List[str], day_lines: List[str]) -> List[str]:
        """「她刚在X」与「今天上午X」说的是同一件事时，只留后者的那一份。

        日程里刚结束的那段会同时落进这两处：「她刚在起身去厨房准备水果」+
        「她今天上午起身去厨房准备水果」。不滤就是同一件事在一句话里说两遍。
        """
        day_text = "".join(day_lines or [])
        if not day_text:
            return echo
        out: List[str] = []
        for line in echo:
            if line.startswith("她刚在") and line[len("她刚在"):] in day_text:
                continue
            out.append(line)
        return out

    def _day_lines(
        self, detailed: bool, include_doing: bool = True, exclude: Optional[List[str]] = None
    ) -> List[str]:
        """今天到这会过了什么：刚做完的事、手上正在做的事。全是日程事实。

        只报她这一天实际经过的事，不写「接着聊这个话题」之类的过程要求。
        """
        try:
            slots = self._core.schedule.current_slots()
        except Exception:
            return []
        if not slots:
            return []
        now = self._core.clock.now()
        now_minutes = now.hour * 60 + now.minute
        try:
            phrases = day_phrases(
                slots, now_minutes,
                past_limit=3 if detailed else 2,
                future_limit=1,
            )
            return self._day_prose(
                day_lines(phrases, max_items=2 if detailed else 1, include_doing=include_doing)
            )
        except Exception:
            return []

    @staticmethod
    def _day_prose(lines: List[str]) -> List[str]:
        """把 day_lines 的清单头削成自然句：她今天上午在改海报，现在在跟客户过方案。

        不写「今天到这会」：时段词本身就在每一项里（「下午在吃午饭」），加上去反而成了
        「今天到这会下午在吃午饭」这种不像人话的句子，而模型会照抄。"""
        out: List[str] = []
        for line in lines:
            body = str(line).split("：", 1)[-1].replace("；", "，")
            # 「现在空着」不值得占一条消息的形状：拼出来是「她今天上午在改海报，现在空着」。
            # 原来只有 medium 档在外层拦，full 档没拦，会漏出这种不像人话的句子
            if not body or "现在空着" in body:
                continue
            out.append(f"她今天{body}")
        return out

    def _memory_lines(self, user_id: str, detailed: bool) -> List[str]:
        """TA 之前说过什么。没记过就不注入，而不是编一个。"""
        try:
            lines = self._core.mood.recall_lines(user_id, TOPICS_FULL if detailed else TOPICS_LOW)
        except Exception:
            return []
        return lines

    def _nickname_line(self, user_id: str, is_group: bool) -> str:
        """称呼：必需品，不受情绪开关影响——用户自己设的称呼，任何档位都不能丢。

        没记下称呼就整行不提：旧版写「还没问过TA叫什么」，模型把它当成待办，
        逢人就追问名字。现在没名字就只叫「TA」，要名字交给自动认定去拿。"""
        try:
            nickname = self._core.mood.nickname(user_id)
        except Exception:
            return ""
        return f"你管TA叫{nickname}" if nickname else ""

    def _scene_lines(self, is_group: bool) -> List[str]:
        """场景必需品：几点、在她的哪个城市、这是群聊还是私聊。三样永远在。"""
        cfg = self.config
        if is_group:
            # 「这是群聊」永远给；这一层「周围还有人看着」由 enable_chat_awareness 定。
            lines: List[str] = ["群聊，周围还有人看着" if cfg.enable_chat_awareness else "群聊"]
        else:
            lines = ["私聊，只有你和TA"]
        now = self._core.clock.now()
        # 只报钟点，不报「上午/下午/晚上」这类时段词：时段词会被模型当成打招呼的
        # 由头（无论几点都回一句「下午好呀」），钟点是一张随手可看的表，用不用由她。
        line = f"现在是{now.strftime('%H:%M')}"
        try:
            today = self._core.snapshot(refresh=False).get("today") or ""
        except Exception:
            today = ""
        if today:
            line = f"今天是{today}，{line}"
        try:
            holiday = self._core.clock.holiday(now)
        except Exception:
            holiday = ""
        if holiday:
            line += f"，{holiday}"
        lines.append(line)
        # 今天周几：周五和周一的感觉不一样。
        try:
            lines.append(f"今天周{self._core.clock.weekday()}")
        except Exception:
            pass
        # 季节：真人知道现在入秋了、快过年了。
        # 按所在城市的时区判南北半球——只看月份的话，9 月的悉尼角色会被告知「入秋了」。
        # 没有权威的纬度表，与其造一张不如按时区名判：下面这些前缀本身就只在南半球。
        # Australia/Darwin 在北纬、南回归线上，刻意不列进去。
        month = now.month
        season = ""
        try:
            from .data.cities import resolve_zone_name

            # 城市口径必须和下面「你在XX」那句同一个源：那里读的是 clock（带角色级
            # 覆盖），这里原来读 config.timezone_city，于是多 bot 各自设城市时
            # 季节按全局城市判，9 月在悉尼的 bot 会被告知「入秋了」。
            # 解析不出时区名就不判季节——拿不准时说季节，比说错一句更糟。
            zone = str(resolve_zone_name(str(self._core.clock.city or "")) or "").upper()
        except Exception:
            zone = ""
        if zone:
            southern = zone.startswith(
                ("AUSTRALIA/SYDNEY", "AUSTRALIA/MELBOURNE", "AUSTRALIA/HOBART",
                 "AUSTRALIA/ADELAIDE", "AUSTRALIA/BRISBANE", "AUSTRALIA/LORD_HOWE",
                 "PACIFIC/AUCKLAND", "ANTARCTICA/",
                 # 南美洲：阿根廷、智利、乌拉圭、玻利维亚。美洲这一区里南半球城市和大把
                 # 北半球城市混在一起，所以只列确实在南半球的那几个前缀。
                 "AMERICA/ARGENTINA/", "AMERICA/SANTIAGO/", "AMERICA/MONTEVIDEO/",
                 "AMERICA/LA_PAZ/", "PACIFIC/CHATHAM")
            )
            # 北半球 3-5 春 / 6-8 夏 / 9-11 秋 / 12-2 冬；南半球反过来
            table = (
                ((9, 11), (12, 2), (3, 5), (6, 8))
                if southern else
                ((3, 5), (6, 8), (9, 11), (12, 2))
            )
            for idx, (lo, hi) in enumerate(table):
                in_span = (lo <= month <= hi) if lo <= hi else (month >= lo or month <= hi)
                if in_span:
                    season = ("开春了", "入夏了", "入秋了", "入冬了")[idx]
                    break
        if season:
            lines.append(season)
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
            lines.append(f"外头{temp}")
        return lines

    def _sleep_cause_line(self) -> str:
        """熬夜因果：昨晚只睡了几小时，今天整个人状态就不对。

        睡眠债的体感措辞（「欠着觉」）说的是结果，这句给它一个原因，因果链闭合。
        """
        try:
            snap = self._core.soma.snapshot()
        except Exception:
            return ""
        if snap.get("asleep", 0.0) >= 1.0:
            return ""
        slept = float(snap.get("last_sleep_hours", 0.0) or 0.0)
        if 0 < slept < 6.0:
            return f"昨晚只睡了{slept:.0f}小时"
        return ""

    def _state_lines(self, snap: Dict[str, Any], detailed: bool) -> List[str]:
        """精力读数（只在低时兜底）与生理周期。

        精力这一层原来总是说一遍「精力充沛/状态挺好」：体感的 arousal 已经在说同一件事，
        两份同时在，读出来就是「她人挺精神的，她精力充沛」。所以这里只在低档位兜底——
        体感在中间区间（energy 40~80）什么都不说，而那正是该给一句的时候。
        关掉 soma 时没有体感，仍要靠它。
        """
        lines: List[str] = []
        try:
            energy = snap.get("energy") or {}
            energy_value = float(energy.get("value", 80.0))
        except (TypeError, ValueError, AttributeError):
            energy_value = 80.0
        if energy_value < 45.0 or not self.config.soma_enabled:
            # 取值也走安全路径：上面读 value 时防了一层，这里再防一层不是多余的——
            # 一边防读取、一边硬下标，防御是不对称的，snapshot 一旦换了实现就会在这里炸。
            text = str(energy.get("text", "") or "") if isinstance(energy, dict) else ""
            if text:
                lines.append(f"她{text}")
        cycle = str(snap.get("cycle") or "").strip()
        if cycle and detailed:
            lines.append(cycle)
        return lines

    def _behavior_lines(
        self,
        user_id: str,
        events: List[Dict[str, Any]],
        agency: Dict[str, float],
        with_previous: bool,
        is_group: bool = False,
    ) -> List[str]:
        """隔了多久 + TA离开前那句原话。

        这一块的目的是让她**感觉到那段间隔**：真人看到「三小时十二分」不会有反应，
        看到「TA有 3 小时 12 分没吭声了，离开前说的是『我先去开会』」才会自己想
        「这人是真忙还是不想理我」。所以只给事实，一句指导话都不写（旧版那句
        「这是一次性的背景，不是每天要重新问候的事」删了）。
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
        # 离开前那句原话是实用性的一半：没有它，「隔了三小时」只是一个秒表读数；
        # 有了它，模型自己就能接上「会开完了吗」。只给事实，不写「要主动问起」。
        if with_previous:
            previous = str(data.get("previous_message") or "").strip()
            if previous:
                lines.append(f"TA离开前说的最后一句：「{previous[:60]}」")
        lines.extend(self._during_gap_lines(seconds))
        # 聊天频率：TA今天话不少/很少，中间档不给。
        # 只在私聊给：这个计数挂在角色上（`daily_msg_count` 是 self 级），群聊里 A 会看到
        # 「TA今天话不少」，而那句话可能是 B、C 在私聊里刷出来的——同一条注入发给不同的人，
        # 说的却是同一件事。
        if not is_group:
            try:
                count = int(self._core.scope.get_self("daily_msg_count", 0) or 0)
            except Exception:
                count = 0
            if count >= 20:
                lines.append("TA今天话不少")
            elif 0 < count <= 3:
                lines.append("TA今天话很少")
        # 几天没说话：隔 3 小时和隔 5 天是两回事，关系层面的分量。
        if seconds is not None:
            try:
                gap_seconds = float(seconds)
            except (TypeError, ValueError):
                gap_seconds = 0.0
            if gap_seconds > 48 * 3600:
                lines.append(f"你们有{int(gap_seconds // 86400)}天没说话了")
        return lines

    def _during_gap_lines(self, seconds: Any) -> List[str]:
        """间隔期间她在过自己的日子：按日程报这期间做过了什么，纯事实。

        只说「这期间她在上课」，不说「所以别提这个间隔」——怎么接由她自己定。
        """
        try:
            gap = float(seconds)
        except (TypeError, ValueError):
            return []
        if gap < 1800.0:
            return []
        try:
            slots = self._core.schedule.current_slots()
        except Exception:
            return []
        if not slots:
            return []
        now = self._core.clock.now()
        now_minutes = now.hour * 60 + now.minute
        start_minutes = now_minutes - gap / 60.0
        try:
            done = done_between(slots, start_minutes, now_minutes, limit=1)
        except Exception:
            return []
        if not done:
            return []
        return [f"这期间她{done[0]}"]
    # ------------------------------------------------------------------
    # 三档组装
    # ------------------------------------------------------------------

    def _append_sleep_essentials(
        self,
        sents: List[tuple[str, float]],
        user_id: str,
        is_group: bool,
        events: List[Dict[str, Any]],
        agency: Dict[str, float],
        interest: Dict[str, float],
        detailed: bool,
    ) -> None:
        """睡着时也不能丢的必需品：她对TA、称呼、间隔、TA说过的话。

        夜间精简省掉的只能是生活细节（今天做了什么、余韵、精力读数），不能是关系
        本身——半夜被消息吵醒的真人照样知道这是谁、隔了多久没见、TA之前提过什么。
        睡着 ≠ 失忆：把她变成一个不认识TA的梦游者，比多说两句生活细节更出戏。
        """
        relation = self._relation_lines(user_id, is_group, detailed=detailed, interest=interest)
        nickname = self._nickname_line(user_id, is_group)
        if nickname:
            relation = relation + [nickname]
        if relation:
            sents.append((self._sentence(relation), 8.0))
        situ = self._behavior_lines(
            user_id, events, agency,
            with_previous=self.config.last_interaction_mode == "with_last_msg",
            is_group=is_group,
        )
        if situ:
            sents.append((self._sentence(situ), 7.0))
        tendency = self._sentence(
            self._tendency_lines(agency, interest, self._seed(user_id))
        )
        if tendency:
            sents.append((tendency, 6.5))
        memory = self._sentence(self._memory_lines(user_id, detailed=detailed))
        if memory:
            sents.append((memory, 6.0))

    def _build_medium(
        self, user_id: str, is_group: bool, events, agency, text: str,
        interest: Dict[str, float],
    ) -> str:
        snap = self._core.snapshot(refresh=False)
        # 夜间精简：睡着时省掉生活细节，但必需品（关系/称呼/间隔/记忆）由
        # `_append_sleep_essentials` 补上——睡着不是失忆。
        try:
            asleep = float((snap.get("soma") or {}).get("asleep", 0.0)) >= 1.0
        except (TypeError, ValueError):
            asleep = False
        sents: List[tuple[str, float]] = [
            (self._sentence(self._scene_lines(is_group)), 10.0)
        ]
        if asleep:
            body = self._feelings_lines(MAX_FEELINGS_LOW, 0.0)
            if body:
                sents.append((self._sentence(body), 9.0))
            self._append_sleep_essentials(
                sents, user_id, is_group, events, agency, interest,
                detailed=False,
            )
            return sents

        body = self._feelings_lines(MAX_FEELINGS_LOW, FEELING_THRESHOLD_LOW) + self._night_lines()
        sleep_cause = self._sleep_cause_line()
        if sleep_cause:
            body = body + [sleep_cause]
        if body:
            sents.append((self._sentence(body), 8.0))

        day_list = self._day_lines(detailed=False)
        echo = self._sentence(self._drop_echoed_in_day(self._echo_lines(), day_list))
        if echo:
            sents.append((echo, 6.0))

        # 显著度浮动：今天只在手上真有事时给；「现在空着」不值得占一条消息的形状。
        if day_list and not any("现在空着" in line for line in day_list):
            sents.append((self._sentence(day_list), 5.0))

        relation = self._relation_lines(user_id, is_group, detailed=False, interest=interest)
        nickname = self._nickname_line(user_id, is_group)
        if nickname:
            relation = relation + [nickname]
        if relation:
            sents.append((self._sentence(relation), 8.0))

        situ = self._behavior_lines(
            user_id, events, agency,
            with_previous=self.config.last_interaction_mode == "with_last_msg",
            is_group=is_group,
        )
        if situ:
            sents.append((self._sentence(situ), 7.0))
        tendency = self._sentence(
            self._tendency_lines(agency, interest, self._seed(user_id))
        )
        if tendency:
            sents.append((tendency, 6.5))

        # medium 档：精力够高时身边句整句消失，形状每条消息都在变。
        hands = self._state_lines(snap, detailed=False)
        if hands:
            sents.append((self._sentence(hands), 4.0))
        memory = self._sentence(self._memory_lines(user_id, detailed=False))
        if memory:
            sents.append((memory, 6.0))

        return sents

    def _build_full(
        self, user_id: str, is_group: bool, events, agency, text: str,
        interest: Dict[str, float],
    ) -> str:
        snap = self._core.snapshot(refresh=False)
        try:
            asleep = float((snap.get("soma") or {}).get("asleep", 0.0)) >= 1.0
        except (TypeError, ValueError):
            asleep = False
        sents: List[tuple[str, float]] = [
            (self._sentence(self._scene_lines(is_group)), 10.0)
        ]
        if asleep:
            body = self._feelings_lines(MAX_FEELINGS_FULL, 0.0)
            if body:
                sents.append((self._sentence(body), 9.0))
            self._append_sleep_essentials(
                sents, user_id, is_group, events, agency, interest, detailed=True
            )
            return sents
        state = self._state_lines(snap, detailed=True)
        if state:
            sents.append((self._sentence(state), 4.0))
        body = self._feelings_lines(MAX_FEELINGS_FULL, 0.0) + self._night_lines()
        sleep_cause = self._sleep_cause_line()
        if sleep_cause:
            body = body + [sleep_cause]
        if body:
            sents.append((self._sentence(body), 8.0))
        day_list = self._day_lines(detailed=True)
        echo = self._sentence(self._drop_echoed_in_day(self._echo_lines(), day_list))
        if echo:
            sents.append((echo, 6.0))
        day = self._sentence(day_list)
        if day:
            sents.append((day, 5.0))
        relation = self._relation_lines(user_id, is_group, detailed=True, interest=interest)
        nickname = self._nickname_line(user_id, is_group)
        if nickname:
            relation = relation + [nickname]
        if relation:
            sents.append((self._sentence(relation), 8.0))
        situ = self._behavior_lines(
            user_id, events, agency,
            with_previous=self.config.last_interaction_mode == "with_last_msg",
            is_group=is_group,
        )
        if situ:
            sents.append((self._sentence(situ), 7.0))
        tendency = self._sentence(
            self._tendency_lines(agency, interest, self._seed(user_id))
        )
        if tendency:
            sents.append((tendency, 6.5))
        memory = self._sentence(self._memory_lines(user_id, detailed=True))
        if memory:
            sents.append((memory, 6.0))
        return sents

    def _finish(self, sents, cfg: HumanoidConfig) -> str:
        # 兼容两种入参：List[tuple[str, float]]（带显著度）或纯字符串。
        if isinstance(sents, str):
            sents = [(sents, 10.0)]
        normalized: List[tuple[str, float]] = []
        for item in sents:
            if isinstance(item, tuple):
                normalized.append((str(item[0]), float(item[1])))
            else:
                normalized.append((str(item), 10.0))
        if not normalized:
            return ""
        header = f"{MARK_PREFIX} {MARK_VERSION} uid={_uid_tag(self._core.role_id)}〕"
        budget = max(200, int(cfg.inject_token_budget))
        # Token 硬预算：按显著度从高到低收，超预算丢低显著度句；原顺序拼装。
        # 场景/称呼（显著度 10）永远排最前，永不丢。
        used = estimate_tokens(header) + 1
        kept_idx: List[int] = []
        for i in sorted(range(len(normalized)), key=lambda j: -normalized[j][1]):
            text, _salience = normalized[i]
            if not text:
                continue
            cost = estimate_tokens(text) + 1
            if used + cost > budget:
                continue
            kept_idx.append(i)
            used += cost
        kept_idx.sort()
        body = "。".join(sents[i][0] for i in kept_idx)
        if not body:
            return ""
        return header + "\n" + body + "。"


# ── 模块末尾的辅助函数区 ──
def is_notice(text: str) -> bool:
    """这句天气是配置说明书还是真天气。

    保留下来是为了认**旧状态文件里已经写进去的** notice 文本，以及给面板/诊断显示用；
    注入路径已经改看 `snapshot()` 的 `notice` 字段了。
    """
    from .services.weather import is_notice as _is_notice

    return _is_notice(text)


def _uid_tag(role_id: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", str(role_id))[-8:] or "self"


def _short_weather(weather: Dict[str, Any]) -> str:
    """天气只留一句能用的；没配好时直接不注入，而不是把配置说明书念给模型听。

    「这是说明书」由 `WeatherService.snapshot()` 写在 `notice` 字段上（而不是靠猜字符串）
    ——之前靠关键词黑名单，任何人新加一句不含那些词的提示就会原样进上下文。

    以前这里还会按字数硬截一次，于是「气温 12℃」被截掉只剩「…天气：多云」——
    天气没有温度等于没说。句子长度改由天气服务自己控制（见 parse_payload）。
    """

    if weather.get("notice"):
        return ""
    env = str(weather.get("env", "")).strip()
    if not env or is_notice(env):
        return ""
    return env.replace("天气：", "", 1)
