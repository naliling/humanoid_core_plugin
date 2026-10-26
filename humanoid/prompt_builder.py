"""把身体与处境编译成上下文——**只给事实，不给台词、不给禁令、不给读数**。

分工（v2.16.4 起）：

* 这个文件只产出**事实块**：此刻几点/在哪/跟TA是群聊还是私聊、她的身体现在是什么状态、
  TA隔了多久没说话、这几样在她心里各占多重。**她今天在做什么不进这里**——日程是插件的
  背景信息，不是要她汇报的内容；给什么日程都照着说，RP 角色会变成旁白。
* 「这些事实该怎么被理解」不写在事实里，由 `FRAMING_TEXT` 一次性放进 system_prompt
  （见 `main.py`）。事实每条消息都在变，读它的方式不该变。
* 数值（百分比、好感度、UTC 偏移）一律不进上下文：那是体检单，人聊天时不念体检单。
  要看数值去 `/你的状态`、`/好感度`、`/时间`、`/拟人 诊断`。

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

from . import ccb as _ccb
from . import species as _species
from . import life_log
from . import relstage
import math
import re
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from astrbot.api import logger

if TYPE_CHECKING:
    from .core_instance import HumanoidCoreInstance

from .config import HumanoidConfig
from .data.mood_map import get_mood_label
from .emotion import EmotionLayer, bracket
from .services.soma import WAKE_DISTURBED_WINDOW_SECONDS
from .wording import (
    AGENCY_CONTINUATION,
    CARE_WORDS,
    FOCUS_WORDS,
    SINCE_WORDS,
    SPARE_WORDS,
    pick,
    scale_word,
)

_CJK_RANGES = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")

# 事实块的标题标记。换文案时把 v 后面的数字加一，老会话里残留的旧块一眼能看出是哪一版。
MARK_PREFIX = "〔她的身体与生活"
MARK_VERSION = "v12"

# system_prompt 里那段稳定说明的标记，main.py 用它判断「已经放过了、别重复放」。
FRAMING_MARK = "【她的身体与生活】"

# 「此刻的她」那一段（I1）的标题：每轮变化，作为临时内容块发出（不落历史）。
STATE_MARK = "【此刻的她】"

# 刚醒 30 分钟内可以提一句梦：再晚她已经在过新的一段了，梦不该追着说。
# 与「她刚睡醒」那句 90 分钟的余韵窗口分开：梦比睡醒的余韵退得更快。
DREAM_WINDOW_SECONDS = 30 * 60

# 给 system_prompt 的那段话：说明下面这些是什么、按什么方式读它。稳定文本，不含事实。
# 只划参考边界，不下指令：说什么、怎么说、说多长，都由她自己判断。
# 名字会被包进「」拼进 system_prompt，而群名片是**对方能随手改**的，所以这里走白名单：
# 只留字母/数字/汉字/下划线（\w）、少量连接符与 emoji，其余一律丢弃。
# 剔除的重点是换行、引号、括号、控制字符、行/段分隔符与零宽字符——有它们才能闭合
# 外面那对「」把指令塞进 system_prompt，而 system_prompt 每轮重建，一旦成功就是
# **对该用户此后每一轮都持续生效**。
_NAME_KEEP = re.compile(
    r"[^\w\-.· \u2600-\u27bf\u2b00-\u2bff\ufe0f\u200d\U0001f000-\U0001faff]",
    re.UNICODE,
)
# 名字用不到这么长；封顶同时压缩"用一长串汉字拼出一句指令"的空间。
_NAME_MAX_CHARS = 16

# 名字里出现这些词就弃用、退回代词。名字会被**替换进句子**（「她今天有点累」→
# 「小雨今天有点累」），名字自己含代词就会替换出「小小雨今天…」这种句子。
# 原来只挡「她 / 你 / TA」，漏了「他 / 我 / 它 / 您」——同一条替换路径，同样会被替换坏。
_NAME_PRONOUNS = ("她", "他", "你", "我", "它", "您", "TA")

# 只认**独立成词**的「她」：`其他` / `他人` 里的那个字是构词成分，不是代词。
# 原来 `_personalize` 是 `text.replace("她", name, 1)`，任何含「她」的词都会被改坏
# （「其他人都说她温柔」→「其小雨人都说小雨温柔」）。
_SHE_STANDALONE_RE = re.compile(r"(?<!其)她")

# 同理，只认**独立成词**的「TA」。事实块里会原样带进玩家自己说的话
# （`recall.py` 的「TA之前说过「…」」、`_echo_lines` 的引用、社交层发布的 open item），
# 里面出现 DATA / TAOBAO / STAR 这类含 TA 的英文词时，盲替换会把玩家的原话改成
# 「D小明A」——模型看到的就是一句被改坏的话。前后都不挨着 ASCII 字母数字才算「TA」。
_TA_STANDALONE_RE = re.compile(r"(?<![0-9A-Za-z])TA(?![0-9A-Za-z])")


def sanitize_name(raw: str) -> str:
    """任何要拼进上下文的「名字」都从这里过一遍。

    名字里混进代词会替换出「小小雨今天…」这种句子，那种直接弃用、退回代词。
    群名片/角色名都会经过这里，而前者由对方控制，所以做白名单清洗而不是原样透传：
    换行、引号、括号、控制字符与零宽字符全部剔除，长度封顶。清洗或弃用时打一条
    debug 日志——原来是完全静默的，出问题只能靠猜。
    """
    original = str(raw or "")
    name = _NAME_KEEP.sub("", original).strip()
    if not name or any(pronoun in name for pronoun in _NAME_PRONOUNS):
        if original.strip():
            logger.debug("名字含代词或清洗后为空，已弃用、退回代词：%r", original)
        return ""
    if len(name) > _NAME_MAX_CHARS:
        logger.debug("名字过长（%d 字），已截断：%r", len(name), name)
        name = name[:_NAME_MAX_CHARS]
    if name != original.strip():
        logger.debug("名字含不安全字符，已清洗：%r -> %r", original, name)
    return name


def clean_label(raw: str) -> str:
    """把**只当标签用**的外部字符串（群名片、昵称）清成一行安全文本。

    与 `sanitize_name` 同一套字符白名单与长度上限，但**不做代词弃用**：代词黑名单是为
    「名字会被替换进句子」准备的，而标签只当 `f"{who}：{body}"` 的前缀、不参与替换，
    弃用只会让归属行整个消失（`TA：…` 退化成裸文本，模型又分不清在回谁）。
    """
    original = str(raw or "")
    name = _NAME_KEEP.sub("", original).strip()
    if len(name) > _NAME_MAX_CHARS:
        name = name[:_NAME_MAX_CHARS]
    return name


# 她开口喊这个人时最多叫几个字。超过就只叫前一段。
_CALL_NAME_MAX = 6
# 昵称里常见的「隔断符」：真人会把 `爱筝挽起·冲击波冠军` 只叫前半截。
_NAME_SPLIT = re.compile(r"[·•・|｜/\\．.\s]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def short_name(raw: str) -> str:
    """她在消息里**喊**这个人时用的名字：太长的中文昵称只叫前一段。

    线上真实消息：

        (休息) 忙着吃饭，爱筝挽起·冲击波冠军，想你❤️

    真人不会把群名片一个字不落地念出来。规则只处理中文昵称：

    * ≤6 字：原样（「小明」「阿澈」「游鱼」）；
    * 带隔断符：取第一段（至少 2 字）——`爱筝挽起·冲击波冠军` → `爱筝挽起`；
    * 其余：取前 4 个字；
    * **纯英文/数字不动**：`Regnator` / `hanhandouya` / `Luture` 本来就是全称，
      硬缩成 `Regn` 反而像系统截断。

    只影响「怎么称呼」，不动存下来的昵称本身（`/查看昵称` 还是全名）。
    """
    name = str(raw or "").strip()
    if not name or len(name) <= _CALL_NAME_MAX:
        return name
    if not _CJK_RE.search(name):
        return name
    parts = [p for p in _NAME_SPLIT.split(name) if p.strip()]
    if parts and 2 <= len(parts[0]) <= _CALL_NAME_MAX:
        return parts[0]
    return name[: _CALL_NAME_MAX - 2].strip() or name



# 会「换行 / 改方向 / 不可见」的字符：嵌进一行引号里之前必须清掉。
_QUOTE_STRIP = re.compile(r"[\r\n\u2028\u2029\u202a-\u202e\u200b-\u200f\ufeff]+")


def quote_line(raw: object, limit: int) -> str:
    """把一段**用户原文**压成能安全嵌进一行引号里的一小段。

    用户消息里可以有换行，也可以有「」这类引号。原样塞进 `「…」` 就能闭合引号，
    让后面的文字看起来像插件自己写的话（`TA离开前说的最后一句：「X」` 这种句式尤其明显）。
    这里把换行与双向控制字符压成空格、把引号剥掉、折叠空白，再截断。
    """
    text = _QUOTE_STRIP.sub(" ", str(raw or ""))
    for ch in ("「", "」", "『", "』", "【", "】"):
        text = text.replace(ch, "")
    return " ".join(text.split())[:limit]


def build_framing(char_name: str = "", user_name: str = "") -> str:
    """给 system_prompt 的那段稳定说明：这些是什么、怎么读、指的是谁。

    **指名道姓这一段是必须的。** 模型没有"自我"这个概念——它不扮演角色，只是按
    system_prompt 生成下一段文本。而 system_prompt 里人设写的是「你叫小雨」，
    注入里若写「她今天有点累」，模型读到的是"有个叫她的实体状态如何"，那是旁白，
    它会照着旁白转述（于是出现复读、或者冒出"好的我知道了"这种话）。
    写明「小雨指你自己」之后，同一句话才从旁白变成自述。

    另一半作用是把「不必使用」的授权收回来：原来那句「无需刻意提及」落到模型那儿
    等于"可以无视"——于是整个状态块被跳过，参考等于白给。改成「会影响你的语气和分寸」
    之后，它知道这些是要用的，只是别逐条念出来。

    末句「说什么、说多长由她自己判断」是插件的核心原则，不动。
    """
    who = sanitize_name(char_name)
    them = sanitize_name(user_name)
    role = f"「{who}」" if who else "「她」"
    peer = f"「{them}」" if them else "「TA」"
    return (
        f"\n\n{FRAMING_MARK}\n"
        f"下面提到的{role}指你自己，{peer}指正在和{role}说话的那个人。"
        "这些不是让你转述的资料，是你现在身上的状态："
        "累的时候话就短，心思在对方身上时才会主动往下接。"
        # 这些状态是**背景**，不是聊天内容。模型最容易犯的错就是把她几点醒的、
        # 困不困、外头什么天当成台词说出来——那样每一句都像在报备。
        # I6：描述式，不下禁令（`test_v2151_freedom` 盯着不许出现「不要/必须/别」）。
        "它们是背景，不是要拿出来讲的内容；对方没问起时，它们只影响你怎么说话。"
        "这些状态是感觉，不是数值。"
        # I4：长度以人设里的要求为准，插件不替她定。
        "如何回应、说什么由你自己判断；说多长以人设里的要求为准。"
    )


def build_state_note(char_name: str = "", user_name: str = "",
                     stage_line: str = "", tone_line: str = "",
                     day_line: str = "", style_line: str = "",
                     world_line: str = "") -> str:
    """一句「她此刻跟对方到什么程度、什么心气、在过什么日子、身处什么样的世界」（I1/I2）。

    与事实块里那句「她内心：…」分工不同：那句是**本轮**的隐藏状态（给这一轮参考），
    这句是让模型**始终**知道现在的关系位置、心气与「她在过自己的日子」，
    不用每轮重新从事实里推断。只说状态，不下指令。

    **日程与外界走这里、不走事实块**（I2）：单独成段地递出去，她只是「知道」，
    要不要提、怎么提由她自己定；混进事实块会被当成要回应的内容。
    """
    parts = [
        str(p).strip()
        for p in (stage_line, tone_line, day_line, style_line, world_line)
        if str(p or "").strip()
    ]
    if not parts:
        return ""
    return "\n\n" + STATE_MARK + "\n" + "。".join(parts) + "。"


# 模块级保留一份默认文案：诊断、测试这些不需要带名字的地方直接用它。
FRAMING_TEXT = build_framing()
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
# 体感门槛。**只在明显不适时才说**——「她有些困意」这种常态不影响这一轮怎么回，
# 写进去只会摊薄模型对那几条真正有用的事的注意（实测一句曾塞到七件事）。
FEELING_THRESHOLD_LOW = 0.75

# 「记得的事」给几句原话。一条就够：给多了模型会当成一份话题清单逐条追着问，
# 而真人只记得一两件事，剩下的过半天就忘了。
TOPICS_LOW = 1
TOPICS_FULL = 1

EVENT_TEXT = {
    "conversation_started": "这是你们头一次说话",
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


def _as_number(value: Any, default: float) -> float:
    """取一个**有限**的 float；坏值（None/字符串/NaN/±inf）一律退回默认值。

    注入层的取值原则是「读不到就少说一句」，但 NaN 不能放行：它不会抛错，
    只会把「亲近欲 nan/50」这种废字符写进上下文——模型会当成真实读数。
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


# 记忆插件交回来的是一段「给人看的注入文本」：可能带小标题、项目符号、换行。
# 事实块里只收一句和别的事实并列的话——照搬会把「标题 + 正文」的形状带进来，
# 那正是最容易让模型照着念的形状（`_sentence` 说的同一件事）。
_MEMORY_HEADING = re.compile(r"^(?:#{1,6}\s|[-=*_—~]{3,}$|[【\[［][^】\]］]*[】\]］]$)")
_MEMORY_BULLET = re.compile(r"^(?:[-*•·]|\d+[.、)]|[一二三四五六七八九十]+[、.)])\s*")
_MEMORY_WS = re.compile(r"\s+")
_MEMORY_RUN = re.compile(r"[，。；、]{2,}")
MEMORY_TEXT_MAX_CHARS = 220


def _flatten_memory(text: str, limit: int = MEMORY_TEXT_MAX_CHARS) -> str:
    """把记忆插件的一段注入文本压成一句能并进事实块的话。

    「- 【长期记忆】」这种行要整行丢掉，所以得先摘掉项目符号再判标题；但「---」这种
    分隔线摘掉一个连字符就只剩「--」，判不出来了——两个形状都判一次才不漏。剩下的一律
    保留原话：记忆的内容不该由这里改写，这里只负责形状。
    """
    lines: List[str] = []
    for raw in str(text or "").splitlines():
        stripped = raw.strip()
        line = _MEMORY_BULLET.sub("", stripped)
        if not line or _MEMORY_HEADING.match(line) or _MEMORY_HEADING.match(stripped):
            continue
        lines.append(line)
    body = _MEMORY_WS.sub(" ", "，".join(lines)).strip()
    body = _MEMORY_RUN.sub("，", body).strip("，。；、 ")
    cap = max(0, int(limit))
    if len(body) > cap:
        body = body[:cap].rstrip("，。；、 ") + "…"
    return body


# 事实块自己不用标题（`_sentence` 那条规矩），但正文里有两处内容是**别人给的**：
# 「TA之前说过『…』」引的是用户原话，`life_items` 里是她和对方还没聊完那件事的摘要
# ——谁都不保证里面不会夹一个【】。混进来模型就会把它当成小标题照着念。
_HEADING_SPAN = re.compile(r"[【\[［][^】\]］]*[】\]］]")
_LINE_BREAK = re.compile(r"[\r\n\u2028\u2029]+")
#: 正文自带的句尾标点：有它就不再补句号，免得写出「。。」「？！。」。
_SENTENCE_TAIL = ("。", "！", "？", "…", "」", "』", "”")


def _strip_heading_spans(text: str) -> str:
    """摘掉混进正文的【…】**整段**（含方括号里的字），并把换行压成空格。

    为什么连里面的字一起去掉：中文里【】几乎只用来做标签（「【长期记忆】」「【重要】」），
    而标签正是「开头一个标题然后是文本」那个毛病的引子——只摘括号留字，会得到
    「关于TA以前的事：长期记忆他上次说的那个面试」这种把标签读进句子中间的怪话。
    摘掉整段之后剩下的是正文本身，模型也就没有可照着念的标题了。

    只做形状，不改归属：这段文字来自用户原话与社交层摘要，摘掉的是它们自带的标记，
    不是这里的上下文改写的。事实块必须是**一行**，多一个换行就多一段，模型会当成
    要分点回答的东西。
    """
    body = _HEADING_SPAN.sub("", str(text or ""))
    return _LINE_BREAK.sub(" ", body).strip()


class PromptBuilder:
    # 天气重复注入的最短间隔（秒）。见 `_weather_worth_saying`。
    WEATHER_REPEAT_SECONDS = 4 * 3600.0
    # 同一句内心话的最短重复间隔（秒）。见 `_emotion_repeated`。
    EMOTION_REPEAT_SECONDS = 10 * 60.0
    # 内存里最多记多少人的「上次注入的内心话」——只用于去重，不落盘。
    EMOTION_SEEN_LIMIT = 512

    def __init__(self, core_instance: "HumanoidCoreInstance") -> None:
        self._core = core_instance
        self._emotion = EmotionLayer(core_instance)
        # 上次写进注入的天气：(文本, 时刻)。None = 还没写过。
        self._weather_seen: Optional[tuple] = None
        # 每个人上次注入的内心话：uid -> (文本, 时刻)。只放内存：
        # 重启后重新记一次没有代价，而落盘会让读路径变成写路径。
        self._emotion_seen: Dict[str, tuple] = {}

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
        char_name: str = "",
        user_name: str = "",
        memory_text: str = "",
    ) -> str:
        cfg = self.config
        events = events or []
        agency = agency or {}
        if interest is None:
            interest = {}
        mode = cfg.inject_activity_context

        if mode == "mood_only":
            # 最轻的一档也带着四样必需品：时间、场景、称呼、今天。省掉的只能是生活细节。
            relation = self._relation_lines(user_id, is_group, interest=interest)
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
                (self._emotion_line(user_id, interest, agency, is_group), 8.5),
                # ccb 常驻状态：不参与上一行的"每轮一条"竞争，显著度单独给高。
                (self._ccb_presence_line(user_id, is_group), 9.5),
                (self._sentence(self._feelings_lines(max_items=1, threshold=0.7)), 7.0),
                (self._sentence(self._memory_lines(user_id, detailed=False)), 6.0),
                (self._sentence(self._open_lines(user_id, is_group)), 6.0),
                (self._memory_link_line(memory_text), 6.0),
            ]
            return self._finish(sents, cfg, char_name, user_name)

        if mode == "full":
            return self._finish(
                self._build_full(
                    user_id, is_group, events, agency, text, interest,
                    memory_text=memory_text,
                ),
                cfg, char_name, user_name,
            )

        return self._finish(
            self._build_medium(
                user_id, is_group, events, agency, text, interest,
                memory_text=memory_text,
            ),
            cfg, char_name, user_name,
        )

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
        try:
            items = list(feelings)
        except TypeError:
            return []
        picked: List[tuple[float, str]] = []
        for item in items:
            # soma 返回的是 (显著度, 文本) 二元组。形状不对就跳过**这一条**，
            # 而不是让一个坏元素把整轮体感（乃至整条注入）带走。
            try:
                score, text = float(item[0]), str(item[1])
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            if score >= threshold:
                picked.append((score, text))
        if not self.config.show_sleep_window:
            # 「她这会儿在睡」也是睡眠信息：关掉不提睡眠时段，就不能换句话又说她在睡。
            picked = [(score, text) for score, text in picked if "在睡" not in text]
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
        if not cfg.night_mode_enabled or not cfg.show_sleep_window:
            return []
        # 「现在是不是她的夜里」按她**日程**推出来的夜判，不用配置里的固定窗口：
        # 夜猫子下午才起、早上才睡，23:00-06:00 不该被当成她的睡眠时段。
        try:
            now = core.clock.now()
            in_night = core.soma.in_scheduled_night(now.hour + now.minute / 60.0)
        except Exception:
            in_night = False
        if not in_night:
            return []
        lines: List[str] = []
        if cfg.soma_enabled:
            try:
                snap = core.soma.snapshot()
            except Exception:
                snap = {}
            try:
                asleep_now = float(snap.get("asleep", 0.0) or 0.0)
            except (TypeError, ValueError, AttributeError):
                # 快照里这一项坏了不该让整段背景消失（原来这个 float 在 try 之外，
                # 一抛异常就顺着调用栈把整轮注入带走）。
                asleep_now = 0.0
            if asleep_now >= 1.0:
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
        # **不含 initiative**：主动性归情绪层了（`EmotionLayer._willingness`）。
        # 两边都给就会出现「（她有点想说话）。她有点想说话」——同一件事在一句里两遍。
        for key, source, ladder in (
            ("ag_continuation", agency.get("continuation"), AGENCY_CONTINUATION),
            ("focus", interest.get("focus"), FOCUS_WORDS),
            ("spare", interest.get("spare"), SPARE_WORDS),
        ):
            got = word(key, source, ladder)
            if got:
                lines.append(got)
        return lines

    def _emotion_line(
        self,
        user_id: str,
        interest: Dict[str, float],
        agency: Dict[str, float],
        is_group: bool = False,
    ) -> str:
        """她此刻的心情，括号包起来：**每轮最多一条**。

        五个维度（当天基调 / 此刻心气 / 对你的态度 / 愿意说多少 / 主动性）各自给候选，
        带显著度一起竞争，最高者胜出。集合大是为了挑得准，不是为了句子多——
        一次给三四条，模型会当成要复述的清单。
        """
        cfg = self.config
        if not self._core.mood.readable(user_id, is_group, enabled=cfg.mood_enabled,
                                        in_group=cfg.mood_enabled_in_group):
            return ""
        # ccb 需要知道「这是不是群」和「她是谁」——两条都是它的硬闸，
        # 拿不到就走安全侧（不出）。
        try:
            _score, text, kind = self._emotion.candidate_with_kind(
                user_id, interest=interest or {}, agency=agency or {},
                is_group=bool(is_group), persona_prompt=self._persona_prompt_text(),
            )
        except Exception:
            return ""
        text = str(text or "").strip()
        if not text:
            return ""
        # ccb 那句是场景里的实时状态（还带姿势/满足度），不能按「重复」压掉；
        # 只有普通情绪句才做逐字去重。
        if kind != "ccb" and self._emotion_repeated(user_id, text):
            return ""
        return bracket(text)

    def _emotion_repeated(self, user_id: str, text: str) -> bool:
        """这一句内心话是不是刚注入过？是就这一轮不再注入。

        实测（2026-10-26，线上私聊 1420609667）：她当天被同一件事硌着，
        `_attitude` 每轮都以同样的显著度胜出，注入里逐轮出现一模一样的一句
        「她内心：上回那句话到现在都还窝火」。模型于是每一句回复都把
        「心里窝火」照抄进括号里——连续 8 条一字不差的模板。

        根因不在词，在**重复**：同一件事说一遍是状态，说到第八遍就是台词，
        模型会照着念。这里按「同一句 + 10 分钟窗口」去重：窗口内不再重复注入，
        模型从它自己刚说过的话里已经知道她这会儿什么心情。情绪换了（文本变了）
        立刻放行——去重只针对逐字重复，不做时间上的稀释。
        """
        now = _core_epoch(self._core)
        seen = self._emotion_seen.get(user_id)
        if isinstance(seen, tuple) and len(seen) == 2 and seen[0] == text:
            try:
                if 0.0 <= now - float(seen[1]) < self.EMOTION_REPEAT_SECONDS:
                    return True
            except (TypeError, ValueError):
                pass
        # 只记最近这些人：长跑之后内存不该被几万个 uid 慢慢吃掉。
        if len(self._emotion_seen) >= self.EMOTION_SEEN_LIMIT:
            self._emotion_seen.clear()
        self._emotion_seen[user_id] = (text, now)
        return False

    def _persona_prompt_text(self) -> str:
        """她的人设正文（缓存快照）。ccb 的两道硬闸（性别 / 安全）都看它。

        拿不到就返回空串：`ccb.safety_block("")` 与 `ccb.looks_female("")` 都按
        「不拦、当女角色」处理——这是既有的兜底口径，不在这里另立一套。
        """
        # v2.28.13：优先走 Core 的 `ccb_persona_text()`——它会沿用「上次取到的那份」
        # 人设。注入层与安全闸必须读同一份：人设源暂时取不到时，注入按缓存判物种、
        # 安全闸却按空串放行，就是两套结论。
        try:
            getter = getattr(self._core, "ccb_persona_text", None)
            if callable(getter):
                text = str(getter() or "")
                if text.strip():
                    return text
        except Exception:
            pass
        try:
            source = getattr(self._core, "persona_source", None)
            if source is None:
                return ""
            snap = source.cached_persona(self._core.role_id)
            return str(getattr(snap, "prompt", "") or "")
        except Exception:
            return ""

    # ── ccb 常驻事实句 ────────────────────────────────────────────────
    # 「刚换过姿势」的判定窗口（秒）。姿势是场景内的事件，十分钟内换的还叫"刚换过"，
    # 半小时前换的再写"刚"就是假话。
    CCB_POSE_RECENT_SECONDS = 600.0

    def _ccb_presence_line(self, user_id: str, is_group: bool = False) -> str:
        """她此刻的欲望在什么位置、在不在亲密场景里——每轮一句**常驻**事实。

        为什么需要它：`_emotion_line` 是多候选竞争、每轮只出一条，ccb 那句更只在
        条件全中、真的推进场景时才出。于是绝大多数轮里模型**完全看不到**她离
        「忍不住」还差多少，也不知道她此刻在不在场景里——用户原话是「在操对方时
        完全不知道 bot 有没有进入状态」。这一句不走竞争，作为高显著度事实直接拼进块。

        措辞纪律（与 `emotion.py` 同一套）：只说她此刻处在什么位置，不写器官、
        不写情节、不示范排版、不下指令——它是一句状态，怎么回应由模型自己判断。

        群里返回空串：群里整条 ccb 都是硬闸（收不了场），这里连状态都不给。
        ccb 关闭 / 开关关掉 / 人设安全闸 / 男角色也返回空串——前两种是"用户不要"，
        后两种是"这条功能本来就不生效"。

        任何一步拿不到或抛异常都返回空串：少一句事实远好过整块注入消失。
        """
        try:
            cfg = self.config
            if not bool(getattr(cfg, "ccb", False)):
                return ""
            if is_group:
                return ""
            if not bool(getattr(cfg, "ccb_presence", True)):
                return ""
            return self._ccb_presence_text(user_id, cfg)
        except Exception:
            return ""

    def _ccb_presence_text(self, user_id: str, cfg: HumanoidConfig) -> str:
        """`_ccb_presence_line` 的实现：先判硬闸与数据，再按三种状态分派。"""
        scope = getattr(self._core, "_scope", None)
        if scope is None:
            return ""
        # **只读**：不因为"看一眼欲望"就给这个人建情绪档案（那等于白涨 state）。
        profile = scope.peek_user(user_id, "mood")
        if not isinstance(profile, dict) or not profile:
            return ""
        persona_prompt = self._persona_prompt_text()
        # 安全闸（未成年 / 血缘）与性别闸都跟 `ccb.evaluate` 同一口径：
        # 功能不生效的人设，不该在上下文里出现「她的欲望」。
        if _ccb.safety_block(persona_prompt) or not _ccb.looks_female(persona_prompt):
            return ""
        now = _core_epoch(self._core)
        stage = _as_number(scope.peek_user(user_id, "ccb_stage", 0.0), 0.0)
        satisfy = _as_number(scope.peek_user(user_id, "ccb_satisfy", 0.0), 0.0)
        aftercare_until = _as_number(
            scope.peek_user(user_id, "ccb_aftercare_until", 0.0), 0.0
        )
        if stage > 0:
            return self._ccb_scene_presence(user_id, cfg, stage, satisfy, now)
        if aftercare_until > now:
            left = max(1, int(math.ceil((aftercare_until - now) / 60.0)))
            # 与 `_aftercare_line`（情绪候选）分工不同：那边给的是"还软着/还没缓过来"
            # 的感受，这边给的是"还剩多久"的事实。措辞上不再重复"刚结束"那三个字，
            # 免得同一块里出现两句几乎一样的话。
            return f"她还在余韵里（刚结束），还剩 {left} 分钟"
        libido = _as_number(profile.get("libido"), 34.0)
        base_libido = _as_number(profile.get("base_libido"), libido)
        return self._ccb_desire_presence(cfg, libido, base_libido)

    def _ccb_scene_presence(
        self,
        user_id: str,
        cfg: HumanoidConfig,
        stage: float,
        satisfy: float,
        now: float,
    ) -> str:
        """在场景里那半句：正在亲密里 + 满足度 + 今日第几次 + 刚换过姿势。

        阶段名不在这里重复说：`ccb_scene_detail` 开着时，`_ccb_line` 的 scene note
        已经在**同一块事实**里写了阶段（`_with_scene_note`），再说一遍就成了
        「两句同一状态」。scene note 关掉时才把阶段名补在这里，否则模型就彻底
        看不到阶段了。
        """
        phase = _ccb.phase_of(
            satisfy=satisfy, stage=stage, aftercare_until=0.0, now=now
        )
        name = (
            _ccb.PHASE_NAMES[phase]
            if 0 <= phase < len(_ccb.PHASE_NAMES)
            else ""
        )
        bits: List[str] = []
        if name and not bool(getattr(cfg, "ccb_scene_detail", True)):
            bits.append(f"{name}，满足 {satisfy:.0f}/100")
        else:
            bits.append(f"满足 {satisfy:.0f}/100")
        turns = int(_as_number(self._core._scope.peek_user(user_id, "ccb_turns", 0.0), 0.0))
        if turns > 0:
            bits.append(f"今日第 {turns} 次")
        if self._ccb_pose_just_changed(user_id, now):
            bits.append("刚换过姿势")
        text = "她正在亲密里（" + "，".join(bits) + "）"
        # 谁在动 / 谁来主导：一句短事实，和身体结构共用同一句，控制 token。
        try:
            lead = str(getattr(cfg, "ccb_lead", "auto") or "auto")
            her_acts = int(_as_number(
                self._core._scope.peek_user(user_id, "ccb_her_acts", 0.0), 0.0))
            his_acts = int(_as_number(
                self._core._scope.peek_user(user_id, "ccb_his_acts", 0.0), 0.0))
        except Exception:
            lead, her_acts, his_acts = "auto", 0, 0
        if lead == "her":
            text += "；这一场她想自己来"
        elif lead == "him":
            text += "；这一场她更想被带着走"
        elif her_acts > his_acts and her_acts >= 2:
            text += "；这一场她更主动"
        # 身体结构（物种）只在这一句里带，且只在场景内带：
        # 每轮都加会让 token 白涨；场景里不加，模型就只会按人类写。
        try:
            body = _species.body_line(self._core.species_profile())
        except Exception:
            body = ""
        if body:
            text += "；她的身体：" + body
        return text

    def _ccb_desire_presence(
        self, cfg: HumanoidConfig, libido: float, base_libido: float
    ) -> str:
        """不在场景里那半句：亲近欲在什么位置、离"忍不住"还差多少。

        用户要能判断"到没到"，所以必须给**数字**和**相对基线的方向**（只给一个
        41/50 的话，模型不知道 41 算高还是低）。阈值与涨幅门槛都从配置读，
        与 `ccb.evaluate` 同一套数——面板上调完这里跟着变，不会出现"注入说有戏、
        实际进不去"的分叉。
        """
        libido = max(0.0, min(50.0, libido))
        threshold = _as_number(
            getattr(cfg, "ccb_libido_min", _ccb.CCB_LIBIDO_MIN), _ccb.CCB_LIBIDO_MIN
        )
        # 注入里说的门槛必须与 `ccb.evaluate` 用的同一个数：配置门槛高于「基线 +
        # 每日可达涨幅」时按可达值算（否则会说「还差 8 才到忍不住」，而其实永远不到）。
        threshold = _ccb.reachable_libido_min(threshold, base_libido)
        rise = _as_number(
            getattr(cfg, "ccb_libido_rise", _ccb.CCB_LIBIDO_RISE), _ccb.CCB_LIBIDO_RISE
        )
        diff = libido - base_libido
        if diff >= 1.0:
            rel = f"比平常高 {diff:.0f}"
        elif diff <= -1.0:
            rel = f"比平常低 {abs(diff):.0f}"
        else:
            rel = "和平常差不多"
        if diff <= -1.0 and libido < threshold:
            # 低于基线、又没过门槛：她这会儿确实没那个心思。给一句正面结论，
            # 比让模型自己从数字里推更不容易读错。
            return f"她此刻没有那个心思（亲近欲 {libido:.0f}/50，{rel}）"
        gap = max(0.0, threshold - libido)
        rise_gap = max(0.0, rise - diff)
        if gap <= 0 and rise_gap <= 0:
            tail = "已经到了会忍不住的那条线"
        elif gap <= 0:
            tail = f"只差没比平常高出 {rise_gap:.0f}，还差一点才到忍不住"
        else:
            tail = f"还差 {gap:.0f} 才到忍不住"
        return f"她此刻的欲望：亲近欲 {libido:.0f}/50（{rel}），{tail}"

    def _ccb_pose_just_changed(self, user_id: str, now: float) -> bool:
        """这一场里刚换过姿势吗（看 `ccb_log` 的 pose 账本）。

        拿不到账本就当没换过——"刚"是会过期的事实，宁可少说一句也不要说错。
        """
        try:
            rows = self._core._scope.peek_user(user_id, "ccb_log", None)
            last = _ccb.log_last({"ccb_log": rows}, "pose")
        except Exception:
            return False
        return last > 0 and 0.0 <= now - last <= self.CCB_POSE_RECENT_SECONDS

    def _relation_lines(
        self,
        user_id: str,
        is_group: bool,
        interest: Dict[str, float],
    ) -> List[str]:
        """她对TA是什么位置、认识多久、最近一次情绪事件。

        关系只说一句。原先 label、care、affection 偏离三处各说一句，拼出来是
        「她对TA热烈，她和TA十分在意，最近对TA更上心」——同一件事说三遍，
        模型会当成三件事。有明确升降时用升降句（它带方向），否则退到位置词。
        """
        cfg = self.config
        core = self._core
        if not self._core.mood.readable(user_id, is_group, enabled=cfg.mood_enabled,
                                        in_group=cfg.mood_enabled_in_group):
            return []
        seed = self._seed(user_id)
        try:
            data = core.mood.profile(user_id)
        except Exception:
            data = {}

        lines: List[str] = []
        position = self._position_line(user_id, data, interest, seed)
        if position:
            lines.append(position)

        # 认识多久（C3，**有意反转旧规则**）：用户明确要求「把认识了几天加到提示词」。
        # 旧规则担心模型会拿去念「我们认识这么久了」——那个理由仍然成立，所以这里只给
        # 一句陈述事实的短句，不加任何「所以你们应该…」的引导；每轮最多一条。
        days_line = self._met_days_line(user_id)
        if days_line:
            lines.append(days_line)

        # 关系到了哪一步（「到什么程度可以说什么」）不再在这里说——【此刻的她】
        # 那一段已经有一句一样的关系阶段。两处都说，模型每轮都被同一件事推着，
        # 调子会卡在一个点上、看着重复（插件自己的规矩是「同一件事只说一遍」）。

        # 冷静期：好感跌破门槛后的一段时间里，她带着情绪冷着，想让他先别来吵（D3）。
        try:
            cool_line = core.mood.cooling_note(user_id)
        except Exception:
            cool_line = ""
        if cool_line:
            lines.append(cool_line)

        # 情绪事件：她记得今天发生过什么（被骂了/被逗开心了），不记得才出戏。
        emo_ev = core.mood.last_emotional_event(user_id)
        if emo_ev:
            lines.append(emo_ev[0])
        return [line for line in lines if line]

    def _position_line(
        self, user_id: str, data: Dict[str, Any], interest: Dict[str, float], seed: List[Any]
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
            label = self._core.mood.stable_label(user_id)
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

    def _met_days_line(self, user_id: str) -> str:
        """「你们已经认识 N 天了」——进上下文，每轮最多一条。

        **这是用户明确要求的有意反转。** 旧规则（旧版 `_days_line` 的注释）是
        「认识多久不进上下文」，理由是模型会把天数拿去念「我们认识这么久了」。
        那个风险还在，所以这里只给一句**陈述事实**的短句，不写任何引导复述的话；
        具体厚度仍由上面的关系位置词承载。想知道准数用 `/你的状态` 面板。
        """
        try:
            first_met = float(self._core.mood.first_met(user_id) or 0.0)
        except Exception:
            return ""
        if first_met <= 0:
            return ""
        try:
            now = float(self._core.now_epoch())
        except Exception:
            return ""
        days = int(max(0.0, now - first_met) // 86400.0)
        if days >= 1:
            return f"你们已经认识 {days} 天了"
        return "你们刚认识不久"

    @staticmethod
    def _days_line(elapsed: float) -> str:
        """认识多久——**面板与诊断用**，格式与注入里的那句事实不同。

        面板给人看，给的是准数与「个月/年」这种容易读的档位；注入里（`_met_days_line`）
        只说「认识 N 天/刚认识不久」，免得每轮都像在报纪念日。
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

    def _sleep_state_lines(self) -> List[str]:
        """她此刻的睡眠状态：正睡着 / 被消息吵到 / 刚醒还没缓过来。

        为什么要有这一块：用户原话「就算别人来吵或者给角色发消息，也不会显得明明在
        睡觉却生龙活虎」「刚醒还没缓过来」。以前睡着只说「她在睡」，醒来立刻就是完全
        清醒的人，中间那段「人还没醒透」根本不存在。

        只给事实，不写台词（和 `_night_lines` 同一条规矩）。
        `show_sleep_window=False` 时整块不出——用户关掉的就是「提她睡没睡」这类事实。
        """
        cfg = self.config
        if not cfg.show_sleep_window:
            return []
        core = self._core
        try:
            state = core.soma.wake_state()
        except Exception:
            return []
        if not isinstance(state, dict):
            return []
        now = _core_epoch(core)
        if state.get("asleep"):
            lines = ["她这会儿正睡着"]
            try:
                disturbed_at = float(core.soma.data.get("sleep_disturbed_at", 0.0) or 0.0)
            except (TypeError, ValueError, AttributeError):
                disturbed_at = 0.0
            # 只认最近 10 分钟内的那一次：更早被吵醒的人已经睡回去了，再说「刚被吵到」
            # 是假话。
            if disturbed_at > 0 and 0 <= now - disturbed_at <= WAKE_DISTURBED_WINDOW_SECONDS:
                lines.append("刚被这条消息吵到，还没醒透")
            return lines
        try:
            until = float(state.get("inertia_until", 0.0) or 0.0)
            wake_at = float(state.get("wake_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            return []
        if until <= 0 or now <= 0 or now >= until:
            return []
        minutes = int(max(0.0, now - wake_at) // 60.0) if wake_at > 0 else 0
        line = (
            f"她刚醒（{minutes} 分钟前），人还没完全缓过来"
            if minutes >= 1
            else "她刚醒，人还没完全缓过来"
        )
        if state.get("disturbed"):
            line += "，是被消息吵醒的"
        return [line]

    def _echo_lines(self) -> List[str]:
        """她刚：带着上一件事的余韵进来。剩的只有身体事实。

        刚睡醒和睡了两小时后是两回事——余韵只在刚发生的那段时间里存在，过了就不提。
        日程性的余韵（她刚在做完什么）不进这里：日程是背景，不往对话里报。
        """
        core = self._core
        lines: List[str] = []
        try:
            snap = core.soma.snapshot()
        except Exception:
            snap = {}
        try:
            asleep_now = float(snap.get("asleep", 0.0) or 0.0)
        except (TypeError, ValueError, AttributeError):
            asleep_now = 0.0
        if asleep_now < 1.0:
            try:
                wake = float(core.soma.data.get("last_wake_at", 0.0) or 0.0)
            except (TypeError, ValueError, AttributeError):
                wake = 0.0
            try:
                slept = float(snap.get("last_sleep_hours", 0.0) or 0.0)
            except (TypeError, ValueError, AttributeError):
                slept = 0.0
            if wake > 0 and slept >= 0.5:
                mins = (_core_epoch(self._core) - wake) / 60.0
                if 0 < mins <= 90:
                    lines.append(
                        f"她刚睡醒，睡了{slept:.0f}小时" if slept >= 1.0 else "她刚睡醒"
                    )
            # 刚醒 30 分钟内可以提一句梦：最多一条，不解释、不追问。
            # 窗口比上面那句余韵短——梦退得比「刚睡醒」快，过了半小就别再提。
            try:
                state = core.soma.wake_state()
                wake_at = float(state.get("wake_at", 0.0) or 0.0) if isinstance(state, dict) else 0.0
            except Exception:
                wake_at = 0.0
            if wake_at > 0 and 0 <= _core_epoch(self._core) - wake_at <= DREAM_WINDOW_SECONDS:
                try:
                    dreams = core.schedule.last_dreams()
                except Exception:
                    dreams = []
                if dreams:
                    lines.append(f"她刚从梦里醒过来（梦到{dreams[0]}）")
        return lines

    def _memory_lines(self, user_id: str, detailed: bool) -> List[str]:
        """TA 之前说过什么。没记过就不注入，而不是编一个。"""
        try:
            lines = self._core.mood.recall_lines(user_id, TOPICS_FULL if detailed else TOPICS_LOW)
        except Exception:
            return []
        return lines

    def _open_lines(self, user_id: str = "", is_group: bool = False) -> List[str]:
        """她手上还开着的事：跨轮、会自己结束的未完事项。

        **走事实块、不另起标题。** `_sentence` 那条规矩在这里同样成立——一个带标题的
        独立段落最像「要念出来的内容」，模型会照着报。这里它只是跟「TA之前说过…」
        并列的一句事实，怎么读由 `build_framing` 那段统一交代。

        为什么要它：长期记忆由记忆插件负责、短期记忆由 AstrBot 的会话历史负责（只留
        十几轮），中间那层「跨轮但会自己结束的事」两边都不管。事实块每轮重发、不落
        历史，所以会话历史被压缩掉之后它照样在——这正是它存在的理由。

        为什么条数这么少（`render_lines` 默认最多 3 条）：它是一份索引，不是记录。
        给多了模型会当成待办清单逐条追问，而且每一轮都要重新付一遍 token。

        **私聊只给这个人自己的事。** 这张账本是角色级的（社交层写进来的），里面有她
        跟**所有人**没结的事。原来不分人一律塞进每一轮，于是每个人都会看到
        「和星辰之间还有件事没完：讲述姐姐们的事情」——那是别人的事，模型拿它当话头
        就会对着一个陌生人问起另一个人的私事；就算不问，也是每轮都占着位置的一份
        噪音。群聊里不筛：一群人的事混着说是正常的，而且这里拿不到在场名单。
        """
        try:
            items = self._core.life_items()
        except Exception:
            return []
        if not items:
            return []
        if user_id and not is_group:
            mine = str(user_id)
            try:
                items = [
                    item for item in items
                    if not str((item or {}).get("uid") or "") or str(item.get("uid")) == mine
                ]
            except Exception:
                pass
            if not items:
                return []
        try:
            return life_log.render_lines(
                items, _core_epoch(self._core), name_of=self._core.mood.nickname
            )
        except Exception:
            return []

    def _memory_link_line(self, memory_text: str) -> str:
        """长期记忆：把记忆插件拼好的那段话压成一句事实。

        和上面那条「TA之前说过…」是一对——那条管这两天，这条管更早。条数由对面
        决定（这里只封长度），因为**检索该由记忆插件做主**：它才知道哪些事对这个
        人算重要。这里不解释、不改写，只压形状。
        """
        body = _flatten_memory(memory_text)
        if not body:
            return ""
        return f"关于TA以前的事：{body}"

    def _nickname_line(self, user_id: str, is_group: bool) -> str:
        """称呼：必需品，不受情绪开关影响——用户自己设的称呼，任何档位都不能丢。

        没记下称呼就整行不提：旧版写「还没问过TA叫什么」，模型把它当成待办，
        逢人就追问名字。现在没名字就只叫「TA」，要名字交给自动认定去拿。"""
        try:
            nickname = sanitize_name(self._core.mood.nickname(user_id))
        except Exception:
            return ""
        if not nickname:
            return ""
        # 太长只给前半截：这条是写给模型看的「她平时怎么叫」，模型会照抄；
        # 挂上 `爱筝挽起·冲击波冠军` 它就会一句一句把全名念出来（见 `short_name`）。
        return f"对TA的称呼是「{short_name(nickname)}」"

    def _scene_lines(self, is_group: bool) -> List[str]:
        """场景：这是群聊还是私聊。

        几点、几号、周几、季节、城市、天气这类**会被当成台词念出来**的参考不在这里——
        它们进【此刻的她】那一段（`background_note`）当背景，单独成段，不混进事实块。
        """
        cfg = self.config
        if is_group:
            # 「这是群聊」永远给；这一层「周围还有人看着」由 enable_chat_awareness 定。
            # 「有人看着」是监视语气，模型读到的是「所以不许聊」——于是群里她变成
            # 强硬拒绝。害羞要留着，但得是「这件事存在」而不是「这是禁令」，
            # 剩下的让她按自己人设去反应。
            # 用「会话」而不是「旁边」：群成员可能分布在不同城市，说「旁边」等于
            # 给了距离断言，模型会当成贴身盯着，害羞就变成戒备。
            return ["群聊，这个会话里还有别人" if cfg.enable_chat_awareness else "群聊"]
        return ["私聊，只有她和TA"]

    def _background_lines(self) -> List[str]:
        """她此刻身处的世界：几点、几号、周几、季节、她在哪个城市、外头什么天。

        这些是**背景**——模型看到它们就该「知道」，而不是拿去做开场白。所以它们单独成
        【此刻的她】一段递出去，不混进事实块里当要回应的内容。
        """
        cfg = self.config
        lines: List[str] = []
        now = self._core.clock.now()
        # 精确到秒（v2.28.1）。
        #
        # 为什么是秒：这段是**每轮重发的临时块**，两条消息之间常常只隔几十秒。
        # 只给「15:20」时，她分不清对方是 15:20:05 还是 15:20:55 说的——
        # 「刚说完」和「快半小时了」在她的时间里是同一时刻。
        #
        # 为什么不用担心她念出来：秒只在她**真的需要提时间**时才有分辨率，
        # 而「要不要提」早已由 system_prompt 的框架句管着（「它们是背景，
        # 不是要拿出来讲的内容；对方没问起时，它们只影响你怎么说话」）。
        # 钟点本身没有变，只是多了一根更细的刻度——就像给人一块表，
        # 不是命令她逢人就报时。
        #
        # 仍然不报「上午/下午/晚上」这类时段词：那才是真的会被当成打招呼由头的
        # （无论几点都回一句「下午好呀」）。
        line = f"现在是{now.strftime('%H:%M:%S')}"
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
        month = now.month
        season = ""
        try:
            from .data.cities import resolve_zone_name

            zone = str(resolve_zone_name(str(self._core.clock.city or "")) or "").upper()
        except Exception:
            zone = ""
        if zone:
            southern = zone.startswith(
                ("AUSTRALIA/SYDNEY", "AUSTRALIA/MELBOURNE", "AUSTRALIA/HOBART",
                 "AUSTRALIA/ADELAIDE", "AUSTRALIA/BRISBANE", "AUSTRALIA/LORD_HOWE",
                 "PACIFIC/AUCKLAND", "ANTARCTICA/",
                 "AMERICA/ARGENTINA/", "AMERICA/SANTIAGO/", "AMERICA/MONTEVIDEO/",
                 "AMERICA/LA_PAZ/", "PACIFIC/CHATHAM")
            )
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
        if temp and self._weather_worth_saying(temp):
            lines.append(f"外头{temp}")
        return lines

    def _weather_worth_saying(self, text: str) -> bool:
        """天气这一句，这一轮该不该再写一遍。

        「外头阴，多云，气温 8℃」是**永远有值、永远具体**的一条，每轮都摆在注入最
        前面就会被念成开场白——线上真实主动消息里 `(守汤) 多云，hanhandouya那边呢？`、
        `(放下餐具) 外头阴沉7度，有点冷呢` 都是这么来的。

        判据：**变了才说**；没变的话至少隔 4 小时才再提一次（真人不会每分钟看一次
        气温，但隔半天会觉得冷）。天气照样每轮进 Core 的 `snapshot()`，只是不再每轮
        进**模型**的上下文。
        """
        now = 0.0
        try:
            now = float(self._core.now_epoch())
        except Exception:
            now = 0.0
        prev = self._weather_seen
        if prev is not None:
            prev_text, prev_at = prev
            if prev_text == text and now > 0 and prev_at > 0:
                if (now - prev_at) < self.WEATHER_REPEAT_SECONDS:
                    return False
        self._weather_seen = (text, now)
        return True

    def background_note(self) -> str:
        """一段「她此刻的世界」，拼进【此刻的她】那一段；拿不到就返回空串。"""
        try:
            lines = [str(x).strip() for x in self._background_lines() if str(x or "").strip()]
        except Exception:
            return ""
        return "；".join(lines)

    def _sleep_cause_line(self) -> str:
        """熬夜因果：昨晚只睡了几小时，今天整个人状态就不对。

        睡眠债的体感措辞（「欠着觉」）说的是结果，这句给它一个原因，因果链闭合。
        """
        try:
            snap = self._core.soma.snapshot()
        except Exception:
            return ""
        try:
            asleep_now = float(snap.get("asleep", 0.0) or 0.0)
        except (TypeError, ValueError, AttributeError):
            asleep_now = 0.0
        if asleep_now >= 1.0:
            return ""
        try:
            slept = float(snap.get("last_sleep_hours", 0.0) or 0.0)
        except (TypeError, ValueError, AttributeError):
            slept = 0.0
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
        gap = ""
        if seconds is not None:
            # 与下面「几天没说话」那段同一个口径：这个值来自事件数据，坏掉时按
            # 「没有准确隔时」退回 EVENT_TEXT 那句，而不是让异常顺着调用栈
            # 把整轮注入带走（这一句就在注入热路径上，且不在任何 try 里）。
            try:
                gap = humanize_gap(float(seconds))
            except (TypeError, ValueError):
                gap = ""
        if gap:
            lines.append(pick("since", seed, SINCE_WORDS).format(gap=gap))
        else:
            lines.append(EVENT_TEXT.get(event_type) or "最近出现了交流变化")
        # 离开前那句原话是实用性的一半：没有它，「隔了三小时」只是一个秒表读数；
        # 有了它，模型自己就能接上「会开完了吗」。只给事实，不写「要主动问起」。
        if with_previous:
            # 这是**用户原文**，必须压成一行再嵌进引号：原文里的换行与「」能闭合
            # 外面这对引号，让后面的字看起来像插件自己写的话。
            previous = quote_line(data.get("previous_message"), 60)
            if previous:
                lines.append(f"TA离开前说的最后一句：「{previous}」")
        # 聊天频率：TA今天话不少/很少，中间档不给。
        # 只在私聊给：这个计数挂在角色上（`daily_msg_count` 是 self 级），群聊里 A 会看到
        # 「TA今天话不少」，而那句话可能是 B、C 在私聊里刷出来的——同一条注入发给不同的人，
        # 说的却是同一件事。
        #
        # v2.27.2 加「不早报」条件：每天头几条消息（≤3）时不报「话很少」——
        # 早上刚开口就被告知「TA今天话很少」，模型会演「你怎么这么冷淡」。
        # 等当天过了半天（≥12:00）还少，才是真的少。
        if not is_group:
            try:
                count = int(self._core.scope.get_self("daily_msg_count", 0) or 0)
            except Exception:
                count = 0
            if count >= 20:
                lines.append("TA今天话不少")
            elif 0 < count <= 3:
                try:
                    hour = self._core.clock.now().hour
                except Exception:
                    hour = 12
                if hour >= 12:
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
        memory_text: str = "",
    ) -> None:
        """睡着时也不能丢的必需品：她对TA、称呼、间隔、TA说过的话。

        夜间精简省掉的只能是生活细节（今天做了什么、余韵、精力读数），不能是关系
        本身——半夜被消息吵醒的真人照样知道这是谁、隔了多久没见、TA之前提过什么。
        睡着 ≠ 失忆：把她变成一个不认识TA的梦游者，比多说两句生活细节更出戏。
        """
        relation = self._relation_lines(user_id, is_group, interest=interest)
        nickname = self._nickname_line(user_id, is_group)
        if nickname:
            relation = relation + [nickname]
        if relation:
            sents.append((self._sentence(relation), 8.0))
        # 睡眠状态（正睡着/被吵到/没醒透）放在关系之后、其余必需品之前：
        # 它是这一轮最该被看见的处境，又必须保证一定进得去（不随预算被挤掉）。
        sleep_state = self._sleep_state_lines()
        if sleep_state:
            sents.append((self._sentence(sleep_state), 9.5))
        sents.append((self._emotion_line(user_id, interest, agency, is_group), 8.5))
        # 睡着时也要知道她的欲望/场景位置：ccb 的进门条件里没有"睡着"这一条，
        # 她可以人在睡而状态在推进，模型不知道就会把两种事实读成互斥。
        sents.append((self._ccb_presence_line(user_id, is_group), 9.5))
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
        opens = self._sentence(self._open_lines(user_id, is_group))
        if opens:
            sents.append((opens, 6.0))
        linked = self._memory_link_line(memory_text)
        if linked:
            sents.append((linked, 6.0))

    def _build_medium(
        self, user_id: str, is_group: bool, events, agency, text: str,
        interest: Dict[str, float], memory_text: str = "",
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
            # 「她在睡」这件事由更细的睡眠状态句（正睡着 / 被吵到 / 没醒透）承载，
            # 体感里那句「这会儿在睡」不与它重复（同一件事只说一遍）。
            if self._sleep_state_lines():
                body = [line for line in body if "在睡" not in line]
            if body:
                sents.append((self._sentence(body), 9.0))
            self._append_sleep_essentials(
                sents, user_id, is_group, events, agency, interest,
                detailed=False, memory_text=memory_text,
            )
            return sents

        body = self._feelings_lines(MAX_FEELINGS_LOW, FEELING_THRESHOLD_LOW) + self._night_lines()
        sleep_cause = self._sleep_cause_line()
        if sleep_cause:
            body = body + [sleep_cause]
        if body:
            sents.append((self._sentence(body), 8.0))

        # 刚醒的睡眠惯性（不是刚醒就是空）：只在醒来后一小段里出现。
        # 有它时不再重复「她刚睡醒」——两句说的是同一件事，模型会当成两件。
        sleep_state = self._sleep_state_lines()
        echo_lines = self._echo_lines()
        if sleep_state:
            echo_lines = [line for line in echo_lines if "刚睡醒" not in line]
        echo = self._sentence(echo_lines)
        if echo:
            sents.append((echo, 6.0))
        wake_state = self._sentence(sleep_state)
        if wake_state:
            sents.append((wake_state, 8.5))

        relation = self._relation_lines(user_id, is_group, interest=interest)
        nickname = self._nickname_line(user_id, is_group)
        if nickname:
            relation = relation + [nickname]
        if relation:
            sents.append((self._sentence(relation), 8.0))
        sents.append((self._emotion_line(user_id, interest, agency, is_group), 8.5))

        # 每轮都写：她欲望在什么位置、在不在场景里/余韵里（显著度高于别的句子，
        # 且与上一行的"一条"竞争无关——那条可能就是非 ccb 的情绪）。
        sents.append((self._ccb_presence_line(user_id, is_group), 9.5))

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
        opens = self._sentence(self._open_lines(user_id, is_group))
        if opens:
            sents.append((opens, 6.0))
        linked = self._memory_link_line(memory_text)
        if linked:
            sents.append((linked, 6.0))

        return sents

    def _build_full(
        self, user_id: str, is_group: bool, events, agency, text: str,
        interest: Dict[str, float], memory_text: str = "",
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
            if self._sleep_state_lines():
                body = [line for line in body if "在睡" not in line]
            if body:
                sents.append((self._sentence(body), 9.0))
            self._append_sleep_essentials(
                sents, user_id, is_group, events, agency, interest,
                detailed=True, memory_text=memory_text,
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
        # 与 medium 档同一口径：睡眠惯性句在时不再重复「她刚睡醒」。
        sleep_state = self._sleep_state_lines()
        echo_lines = self._echo_lines()
        if sleep_state:
            echo_lines = [line for line in echo_lines if "刚睡醒" not in line]
        echo = self._sentence(echo_lines)
        if echo:
            sents.append((echo, 6.0))
        wake_state = self._sentence(sleep_state)
        if wake_state:
            sents.append((wake_state, 8.5))
        relation = self._relation_lines(user_id, is_group, interest=interest)
        nickname = self._nickname_line(user_id, is_group)
        if nickname:
            relation = relation + [nickname]
        if relation:
            sents.append((self._sentence(relation), 8.0))
        sents.append((self._emotion_line(user_id, interest, agency, is_group), 8.5))
        # 与 medium 档同一句：每轮都让模型看见她欲望的位置/在不在场景里。
        sents.append((self._ccb_presence_line(user_id, is_group), 9.5))
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
        opens = self._sentence(self._open_lines(user_id, is_group))
        if opens:
            sents.append((opens, 6.0))
        linked = self._memory_link_line(memory_text)
        if linked:
            sents.append((linked, 6.0))
        return sents

    def _finish(
        self,
        sents,
        cfg: HumanoidConfig,
        char_name: str = "",
        user_name: str = "",
    ) -> str:
        # 兼容两种入参：List[tuple[str, float]]（带显著度）或纯字符串。
        if isinstance(sents, str):
            sents = [(sents, 10.0)]
        normalized: List[tuple[str, float]] = []
        for item in sents:
            if not isinstance(item, tuple):
                normalized.append((str(item), 10.0))
                continue
            try:
                salience = float(item[1])
            except (TypeError, ValueError, IndexError):
                # 显著度坏了不能连累整块注入：外面那层 except 会把整个事实块吞掉，
                # 一句话的排序问题不该让模型这一轮失忆。退回「没给显著度」那一档。
                salience = 10.0
            normalized.append((str(item[0]), salience))
        if not normalized:
            return ""
        # 抬头只留版本号，不再写 `uid=`。
        #
        # 那个 `uid={_uid_tag(role_id)}` 打的是**角色 id**（机器人自己），不是用户的，
        # 而且只取末 8 位——所有人看到的都是同一串 `uid=32823490`，模型则把它当成
        # 一个真实存在的编号。它不是任何东西的 id，只是给模型的一条假事实；
        # 真正需要它的地方（区分不同角色的块）由 `MARK_PREFIX` 本身就够了。
        header = f"{MARK_PREFIX} {MARK_VERSION}〕"
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
        # 用 normalized 而不是 sents：sents 里可能混着裸字符串（上面那个兼容分支），
        # `sents[i][0]` 取到的是**一个字符**。
        body = "。".join(normalized[i][0] for i in kept_idx)
        if not body:
            return ""
        # 最后一道形状闸：正文只允许是一行、且不含【…】。正文里那两处内容来自
        # 用户原话和社交插件发布的摘要，它们带什么进来不由这里决定。
        body = _strip_heading_spans(body)
        if not body:
            return ""
        # **只对正文做替换，标题不动。** 标题「〔她的身体与生活 v12〕」是
        # `MARK_PREFIX`，也是这块事实的抬头——一旦被 `_personalize` 换成
        # 「〔小雨的身体与生活…」，抬头就丢了。
        #
        # 句号只在正文没自带句尾标点时才补：正文里有引文与摘要，它们自带「。」「？」是
        # 常事，无脑再补一个就成了「……了。。」——注入的每一个字都在花预算，也在给
        # 模型做示范，这种双标点没必要。
        tail = "" if body.endswith(_SENTENCE_TAIL) else "。"
        return header + "\n" + self._personalize(body + tail, char_name, user_name)

    sanitize_name = staticmethod(sanitize_name)

    @classmethod
    def _personalize(cls, text: str, char_name: str = "", user_name: str = "") -> str:
        """把代词换成真名。

        词表里一律写「她」指角色、「TA」指玩家，**不写「你」**——system_prompt 里
        「你」已经是角色了，注入里再拿「你」指玩家，模型会把玩家当成自己。

        换成真名的原因：模型在 system_prompt 里认的是「你叫小雨」。于是
        「小雨今天有点堵」它当成自己说的话；「她今天有点堵」它当成在转述别人的事——
        这就是「模型在照着一份关于第三方的说明书说话」的由来。

        但**只替换第一次**：名字是拿来建立映射的，建立之后就该继续用「她」——
        每句都以「小雨」开头反而像流水账，句子之间的连贯感也没了。框架句里那句
        「「小雨」指你自己」已经把映射讲清了，后面跟着「她」不会歧义。

        名字取不到、或名字里混进代词时原样返回，退回「她 / TA」，功能不受影响。
        """
        if not text:
            return text
        who = cls.sanitize_name(user_name)
        if who:
            # 全部**独立成词**的「TA」都换（词表里玩家一律写 TA），但不碰
            # DATA / TAOBAO / STAR 这类词里的 TA——那些多半是玩家自己的原话。
            text = _TA_STANDALONE_RE.sub(lambda _m: who, text)
        name = cls.sanitize_name(char_name)
        if name:
            # 情绪句例外：它是独立的内心独白，每次都该叫得出名字（「小雨内心：心里有点堵」），
            # 否则句首又冒出一个「她」，读者得回头去猜这是谁。
            text = text.replace("她内心：", f"{name}内心：", 1)
            # 只替换**独立成词**的第一个「她」——盲替换单个汉字会把「其他」改成「其小雨」。
            match = _SHE_STANDALONE_RE.search(text)
            if match:
                text = text[: match.start()] + name + text[match.end() :]
        return text


# ── 模块末尾的辅助函数区 ──
def is_notice(text: str) -> bool:
    """这句天气是配置说明书还是真天气。

    保留下来是为了认**旧状态文件里已经写进去的** notice 文本，以及给面板/诊断显示用；
    注入路径已经改看 `snapshot()` 的 `notice` 字段了。
    """
    from .services.weather import is_notice as _is_notice

    return _is_notice(text)


def _uid_tag(role_id: str) -> str:
    """角色 id 的短标签。

    **已经不再拼进抬头**（v2.27.42）：它打的是机器人自己的 id、只取末 8 位，对模型
    来说是一串没有来源的编号，而且对每个用户都一样。留着这个函数是因为老的
    `state.json` 里可能还存着带 `uid=` 的旧块文本，将来要辨识时还用得上。
    """
    return re.sub(r"[^0-9A-Za-z]", "", str(role_id))[-8:] or "self"


def _short_weather(weather: Dict[str, Any]) -> str:
    """天气只留一句能用的；没配好时直接不注入，而不是把配置说明书念给模型听。

    「这是说明书」由 `WeatherService.snapshot()` 写在 `notice` 字段上（而不是靠猜字符串）
    ——之前靠关键词黑名单，任何人新加一句不含那些词的提示就会原样进上下文。

    以前这里还会按字数硬截一次，于是「气温 12℃」被截掉只剩「…天气：多云」——
    天气没有温度等于没说。句子长度改由天气服务自己控制（见 parse_payload）。
    """

    if not isinstance(weather, dict):
        # 快照里这一项坏了（None / 字符串）时返回空串，而不是让整段背景消失：
        # 这里原来是裸的 `weather.get(...)`，非 dict 直接 AttributeError。
        return ""
    if weather.get("notice"):
        return ""
    env = str(weather.get("env", "")).strip()
    if not env or is_notice(env):
        return ""
    return env.replace("天气：", "", 1)
