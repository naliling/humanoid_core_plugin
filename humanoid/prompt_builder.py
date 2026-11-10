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
from . import clock as _clock
from . import body_sense as _body_sense
from . import body_voice as _body_voice
from . import injection as _injection
from . import species as _species
from . import life_log
from . import relstage
from . import state_snapshot
import math
import re
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

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
#
# v13 起抬头改**第一人称**（原「〔她的身体与生活 v12〕」）。第三人称抬头 + 第三人称
# 正文，模型读到的是「一个叫小雨的角色的状态报告」，它在思考里就写「我在扮演小雨」。
# 改成「〔我现在的身体〕」之后，正文里每一句都是它自己的，不需要再做「她=你自己」的
# 映射——映射本身就是在提醒它「这是别人」。
MARK_PREFIX = "〔我现在的身体"
MARK_VERSION = "v13"

# system_prompt 里那段稳定说明的标记，main.py 用它判断「已经放过了、别重复放」。
FRAMING_MARK = "【我现在的身体】"

# 「我现在的处境」那一段（I1）的标题：每轮变化，作为临时内容块发出（不落历史）。
STATE_MARK = "【我现在的情况】"

# 刚醒 30 分钟内可以提一句梦：再晚她已经在过新的一段了，梦不该追着说。
# 与「她刚睡醒」那句 90 分钟的余韵窗口分开：梦比睡醒的余韵退得更快。
DREAM_WINDOW_SECONDS = 30 * 60

# soma.feelings() 在她睡着时追加的那条体感的文本片段。soma 目前只返回
# (显著度, 文本)，没有域标记（P-15 的彻底修法是让 feelings() 带 domain）。
# 所以这里先用**结构化睡眠状态**门禁：她醒着时这条根本不存在，不必看文本；
# 只有睡着时才需要认出这一条——识别点全文件只此一处。
SLEEP_FEELING_MARK = "在睡"

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

    只影响「怎么称呼」，不动存下来的昵称本身（`/拟人 查看昵称` 还是全名）。
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


def to_first_person(text: str) -> str:
    """把注入正文里的「她」全部换成「我」（v13 归属改造的出口）。

    为什么在**渲染出口**做，而不是把上百处词表逐个改写：词表里「她」指角色是全文件
    一致的约定，逐条改容易漏，也会让以后新加的词条继续写「她」。出口统一转换，词表
    约定不变，产出永远是第一人称。

    安全性：注入正文里没有第二个人用「她」——玩家一律写「TA」，别人一律写名字或不
    出现。所以正文里的「她」100% 指角色自己，整字替换不会改错人。
    """
    if not text:
        return text
    return str(text).replace("她", "我")


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
    them = sanitize_name(user_name)
    peer = f"「{them}」" if them else "「TA」"
    return (
        f"\n\n{FRAMING_MARK}\n"
        # 第一人称（v13）：不再说「她指你自己」。那句话是在教模型做一次人称映射，
        # 而模型每做一次映射，就站到了角色外面——它在思考里会写「我在扮演她」。
        # 现在正文本身就是「我」，映射这一步整个消失了。
        "下面用「我」说的那些，就是你此刻的身体和处境；"
        f"{peer}指正在和你说话的那个人。"
        "这些不是对方递过来的资料，是你现在身上的状态："
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
    # 与事实块同一出口：这一段也一律第一人称（「我这会儿在忙手里的活」），
    # 否则两块一块说「我」一块说「她」，模型又要做一次映射。
    return "\n\n" + STATE_MARK + "\n" + to_first_person("。".join(parts)) + "。"


# 模块级保留一份默认文案：诊断、测试这些不需要带名字的地方直接用它。
FRAMING_TEXT = build_framing()
# 注入块的尺寸红线（字符）：唯一来源是 humanoid.injection.INJECT_MAX_CHARS。
# 以前这里是只给测试用的死常量（humanoid/ 下零引用），现在 `_finish` 按注入档位
# 真正执行它——与 token 预算是**两条都要过**的闸（P-04）。
INJECT_MAX_CHARS = _injection.INJECT_MAX_CHARS


def _meta(key: str, group: str = "", when: str = "") -> Dict[str, str]:
    """给一句话贴身份标签：key（同一件事）、group（互斥组）、when（生效条件）。

    生成处只负责说「这是什么事实」；说不说、跟谁说、排第几由 injection.select
    统一决定（R7）。文案里有没有某个词，不再参与判断（P-03 / P-07）。
    """
    return {"key": key, "group": group, "when": when}


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
#: 摘掉【…】之后可能只剩一个悬空的谓语（energy.py 的「处于【生理期】，…」→「处于，…」）。
#: 这是「标签与正文拼在一句里」的另一面：括号是别人给的，去掉括号要连它的谓语一起去。
_ORPHAN_VERB = re.compile(r"处于[，。]")
_LINE_BREAK = re.compile(r"[\r\n\u2028\u2029]+")
#: 正文自带的句尾标点：有它就不再补句号，免得写出「。。」「？！。」。
_SENTENCE_TAIL = ("。", "！", "？", "…", "」", "』", "”")


#: 对方的话里出现什么，才算在问她的身体——词表搬到 `humanoid/body_voice.py`。
#: 这里只留一个转发，免得两处词表各自演化（一处加了「腰」、另一处没加，就会
#: 出现"问了却答不上"与"没问却硬说"两种毛病各一半）。
_CYCLE_TOPICS = _body_voice.ASKED_TOPICS

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
    body = _ORPHAN_VERB.sub("", body)
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
        # 天气「变了才说」的门：**按会话/用户分桶**、桶有界（LRU）。None = 还没建。
        # 旧写法是一个 (文本, 时刻) 单值，整个角色共用一份（审核 P-08 #41）。
        self._weather_gate: Optional[Any] = None
        # 每个人上次注入的内心话：uid -> (文本, 时刻)。只放内存：
        # 重启后重新记一次没有代价，而落盘会让读路径变成写路径。
        #
        # v3.0.0：**OrderedDict 做 LRU，满了淘汰最旧的一个**。旧写法 `clear()` 整表清空，
        # 大部署（>512 个发过言的人）每次表满就忘掉所有人的去重记录，紧接着下一轮
        # 所有人都会再看到同一句内心话——v2.28.12 修的「同一句不复读」在大号上周期复发。
        self._emotion_seen: "OrderedDict[str, tuple]" = OrderedDict()

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
                (self._sentence(self._scene_lines(is_group)), 10.0, _meta("scene")),
                (self._sentence(relation), 8.0, _meta("relation")),
                (self._emotion_line(user_id, interest, agency, is_group), 8.5, _meta("emotion")),
                # ccb 常驻状态：不参与上一行的"每轮一条"竞争，显著度单独给高。
                (self._ccb_presence_line(user_id, is_group), 9.5, _meta("ccb_presence")),
                (
                    self._sentence(
                        self._feelings_lines(max_items=1, threshold=0.7, text=text)
                    ),
                    7.0,
                    _meta("feelings"),
                ),
                (self._sentence(self._memory_lines(user_id, detailed=False)), 6.0, _meta("memory")),
                (self._sentence(self._open_lines(user_id, is_group)), 6.0, _meta("opens")),
                (self._memory_link_line(memory_text), 6.0, _meta("memory_link")),
            ]
            return self._finish(sents, cfg, char_name, user_name, tier="mood_only")

        if mode == "full":
            return self._finish(
                self._build_full(
                    user_id, is_group, events, agency, text, interest,
                    memory_text=memory_text,
                ),
                cfg, char_name, user_name, tier="full",
            )

        return self._finish(
            self._build_medium(
                user_id, is_group, events, agency, text, interest,
                memory_text=memory_text,
            ),
            cfg, char_name, user_name, tier="medium",
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

    def _feelings_lines(
        self, max_items: int, threshold: float, *, include_sleep: bool = True,
        text: str = "",
    ) -> List[str]:
        """按显著度挑体感。低于门槛的一律不注入。

        `include_sleep=False` 表示这段睡眠已经由 `_sleep_state_lines()` 更细的句子
        承载了，体感里那条「她这会儿在睡」不该再说一遍（P-15：判据是调用方给的
        结构化事实，不是正文里有没有那三个字）。
        """
        core = self._core
        if not self.config.soma_enabled:
            return []
        try:
            # 「身体不适」这一条走限流（对方问起 / 八小时一次 / 她刚说过就不给）：
            # 它在 discomfort≥62 时每轮都会命中，而落到她嘴里就是「肚子难受」。
            feelings = core.soma.feelings(
                float(core.energy.energy),
                include_discomfort=self._discomfort_worth_saying(text),
            )
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
        if not include_sleep or not self.config.show_sleep_window:
            # 用户关掉了睡眠窗口，或这段睡眠已经由睡眠状态句说过：体感里那条
            # 「她这会儿在睡」不再进正文。用户开关优先——它要求的是「别出现」，
            # 不能因为结构化状态说她醒着就放行。
            picked = self._drop_sleep_feeling(
                picked, hide_sleep_anyway=not self.config.show_sleep_window
            )
        picked.sort(key=lambda item: item[0], reverse=True)
        # 体感句一律披上主语，但 soma 自己带主语的整句不能再披一遍。
        return [
            text if text.startswith("她") else f"她{text}" for _, text in picked[:max_items]
        ]

    def _drop_sleep_feeling(
        self, picked: List[tuple[float, str]], *, hide_sleep_anyway: bool = False
    ) -> List[tuple[float, str]]:
        """去掉 soma 在「她睡着」时追加的那条体感。

        先看结构化状态：她醒着时 soma 不会追加这一条，连文本都不用看；拿不到睡眠
        状态、或确实睡着时，才按 soma 的生产文本认出这一条（P-15）。
        `hide_sleep_anyway=True` 是用户把睡眠窗口关掉了：这时按「别出现」处理，
        不看结构化状态，文本里带睡眠字样的体感一律不进正文。
        """
        if not hide_sleep_anyway:
            try:
                asleep = (
                    float(self._core.soma.snapshot().get("asleep", 0.0) or 0.0) >= 1.0
                )
            except Exception:
                asleep = None
            if asleep is False:
                return picked
        return [(score, text) for score, text in picked if SLEEP_FEELING_MARK not in text]

    def _night_lines(self) -> List[str]:
        """夜间/午睡：只报身体与日程的事实，不编细节、不管她怎么回。

        v2.16.3 这一档写的是「你还在睡，是手机震醒的那种：眼睛睁不开，脑子没转，话到
        嘴边只剩一两个字」——手机、眼睛、脑子都不是算出来的，是插件替她写的人设。现在
        只留两件事：按她自己的日程这会儿算不算睡，以及她的身体有多困。"""
        cfg = self.config
        core = self._core
        if not cfg.night_mode_enabled or not cfg.show_sleep_window:
            return []
        # 「现在是不是她的夜里」只有一份推导：`state_snapshot.in_night()`（日程推出来的
        # 睡段 → 配置兜底）。这里以前单独问 `soma.in_scheduled_night`——它只认日程，
        # 还没排日程时恒为 False，于是注入里从来没有夜间那行，而同一时刻的契约
        # `time.is_night` 按配置窗口给出 true：两个答案（审核 跨插件契约 8）。
        try:
            in_night = bool(state_snapshot.in_night(core))
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

    def _body_lines(
        self,
        user_id: str,
        interest: Dict[str, float],
        agency: Dict[str, float],
        text: str = "",
    ) -> List[str]:
        """身体这一块的**判断句**：周期感觉 + 一条倾向，合成一句。

        为什么合并（v3.1.9）：v3.1.8 这里是四五句并列——「我这几天人挺有精神，
        皮肤也顺眼」「前面聊的还能接上」「我这会儿还挺专注」「我现在很闲」。每一句
        都是真的，但四句排在一起就是一张状态表：模型读完只知道「她状态良好、有空」，
        写出来的话也就只剩总结（「我今天还挺闲的」）。

        人不会同时报四条状态。所以这里只留两句：周期那句（一天之内不变），加一条
        最相关的倾向。剩下的交给 `_sensory_line`——判断少一点，细节多一点。

        倾向取**最后一条**：`_tendency_lines` 的顺序是「想不想接 → 注意力在哪 →
        有多少闲」，越靠后越贴近「她这会儿有没有空说话」，那正是模型最需要知道、
        又最容易读成任务指令之外的那一件事。
        """
        lines: List[str] = []
        cycle = self._cycle_line(text)
        if cycle:
            lines.append(cycle)
        tendency = self._tendency_lines(agency, interest, self._seed(user_id))
        if tendency:
            lines.append(tendency[-1])
        return lines

    def _sensory_line(
        self,
        now: Any,
        body: Dict[str, Any],
        style: str,
        seed: List[Any],
    ) -> str:
        """一句**只能影响语气**的感官细节，每轮最多一条。

        为什么要有（v3.1.9）：这个插件不生成台词，只给模型「她此刻是什么样」。可
        v3.1.8 给的每一样都是**判断**（有精神 / 专注 / 很闲），判断是可以直接念出来
        的——模型于是把注入原样翻译成台词，听起来像在念仪表盘。真人说「在吗」的时候
        不会先想「我现在很闲」，她只知道「手边的水已经温了」。这种细节进不了台词，
        但会让语气落地。

        组合：时段（必选）× 天气 × 身体档位 × 穿搭。**确定性抽签**：种子含角色、
        今天、用户、时段、天气档，所以同一天同一时段稳定换、跨天会变——她不会今天
        下午和明天下午是同一个下午。

        句式上有一条硬禁止：「我感到 / 我觉得 / 我心里」。那是内心独白，模型会直接
        复述成一句台词（「我觉得屋里有点闷」）；只写外部可感的细节，它才只能拿去
        调整语气。
        """
        try:
            hour = int(getattr(now, "hour"))
        except (AttributeError, TypeError, ValueError):
            return ""
        slot = _sensory_slot(hour)
        weather_key = _sensory_weather_key(self._weather_snapshot())
        options: List[str] = list(_SENSORY_SLOT_LINES.get(slot, ()))
        options.extend(_SENSORY_WEATHER_LINES.get(weather_key, ()))
        options.extend(_sensory_body_lines(body))
        options.extend(_sensory_style_lines(style))
        if not options:
            return ""
        line = pick(
            "sensory_line",
            [*(seed or []), slot, weather_key],
            options,
        )
        return str(line or "")

    def _ongoing_hint_line(self, busy: bool = False) -> str:
        """她手上有没有一件事还没放下——只说**有没有**，不说是什么。

        为什么要有（v3.1.9 / P2）：具体日程只在对方问「在干嘛」时才给（`_asks_about_now`
        那道门），于是绝大多数轮次里她只知道「很闲」。「手上有件事」这个信息量很小，
        但它决定了她说「等我一下」还是「现在就聊」——而具体是哪件事一旦写出来，
        模型就会当成话头去念。

        不写具体内容，也不和「她手上还有件事没完：<什么事>」抢：那句由 `_open_lines`
        出（life_log 的未完事项），带内容；这句只给状态，两者互补。

        「有没有事」看两个来源：日程当前时段有没有事件（她正处在某件事中间），
        以及 `ongoing_tasks()`（跨天跟着她的待办）。两处都不写进这句话——只说
        「有」还是「空」，具体内容一写出来就会被当成话头念。
        """
        if not busy:
            try:
                busy = bool(
                    [text for text in (self._core.schedule.ongoing_tasks() or [])
                     if str(text).strip()]
                )
            except Exception:
                busy = False
        if not busy:
            try:
                # 用 active_segment 而不是 current_slot：后者拿不到段时会返回一个
                # 「自由活动」的中性段（不编造她在做什么，但也不能用来判断「有没有事」），
                # 于是每一轮都会变成「手上有件事」。active_segment 只在真有覆盖当前
                # 时刻的段时返回，正是「她正处在某件事中间」。
                busy = self._core.schedule.active_segment() is not None
            except Exception:
                busy = False
        return "手上还有件事没放下，不是急事" if busy else "这会儿手上是空的"

    def _weather_snapshot(self) -> Dict[str, Any]:
        """当前天气快照；取不到就是空 dict（感官句会自动退化成只有时段那一档）。"""
        try:
            weather = self._core.snapshot(refresh=False).get("weather")
        except Exception:
            return {}
        return weather if isinstance(weather, dict) else {}

    def _outfit_text(self) -> str:
        """她这会儿穿什么（取不到就是空串）。感官句用它决定「腿上有点凉」这类细节。"""
        try:
            return str(self._core.outfit_text() or "")
        except Exception:
            return ""

    def _emotion_line(
        self,
        user_id: str,
        interest: Dict[str, float],
        agency: Dict[str, float],
        is_group: bool = False,
        asleep: bool = False,
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
                asleep=bool(asleep),
            )
        except Exception:
            return ""
        text = str(text or "").strip()
        if not text:
            return ""
        # ccb 那句是场景里的实时状态（还带姿势/满足度），冷静期那句是会过期的状态
        # （cool_until 到点就消失），两者都不能按「逐字重复」压掉——压掉的结果是
        # 场景里突然没声音 / 她还在生气但注入里一字不提。只有普通情绪句才做去重。
        if kind not in ("ccb", "cooling") and self._emotion_repeated(user_id, text):
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
                    self._emotion_seen.move_to_end(user_id)
                    return True
            except (TypeError, ValueError):
                pass
        # 只记最近这些人：长跑之后内存不该被几万个 uid 慢慢吃掉。
        #
        # 淘汰顺序：**先丢已过期的**（它们对去重已经没有意义），再从最旧的开始丢；
        # 绝不整表清空——那等于让所有人同时失去去重保护（审核 P2-15）。
        while self._emotion_seen:
            oldest_uid, oldest = next(iter(self._emotion_seen.items()))
            try:
                fresh = 0.0 <= now - float(oldest[1]) < self.EMOTION_REPEAT_SECONDS
            except (TypeError, ValueError):
                fresh = False
            if fresh:
                break
            self._emotion_seen.popitem(last=False)
        while len(self._emotion_seen) >= self.EMOTION_SEEN_LIMIT:
            self._emotion_seen.popitem(last=False)
        self._emotion_seen[user_id] = (text, now)
        self._emotion_seen.move_to_end(user_id)
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
            if not _ccb.allowed_for(cfg, user_id):
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
            # 账本不下发（v13）：原来写「还在余韵里（刚结束），还剩 N 分钟」——
            # 括号里的两个都是系统口径，模型读到只会切回「我在读一个系统的账本」。
            # 只给身体的感受，剩下的交给情绪层（那边给「还没缓过来」）。
            # P1：感受按余韵窗还剩多久分三档（刚结束最沉，快结束只剩困），
            # 词表在 body_sense；这里只负责兜底——取不到就还用原来那句。
            try:
                sense = _body_sense.collect(self._core, user_id, now)
                phrase = _body_sense.aftercare(sense)
                # P3：退出场景也记一笔（stage=0）。下一场开头的 delta 因此不会
                # 拿上一场的数据去比——隔了几小时说「比刚才更胀」是假话。
                _body_sense.remember(self._core, user_id, sense)
            except Exception:
                phrase = ""
            return phrase or "身上还热着，腰有点酸，不太想动"
        libido = _as_number(profile.get("libido"), 34.0)
        base_libido = _as_number(profile.get("base_libido"), libido)
        affection = _as_number(profile.get("affection"), 0.0)
        base_affection = _as_number(profile.get("base_affection"), affection)
        # 余下的门槛也一起取：她「能不能进那一步」不只看亲近欲。以前这一句只
        # 抄了亲近欲那一半，好感/精力/冷却全没算，于是模型会被告知「已经到了会
        # 忍不住的那条线」，而真正拦门的 `ccb.evaluate` 因为好感不够直接返回 None。
        try:
            energy = _as_number(self._core.energy.energy, 100.0)
        except Exception:
            energy = 100.0
        cooldown_left = 0.0
        try:
            gate_at = _as_number(scope.peek_user(user_id, "ccb_gate_at", 0.0), 0.0)
            if gate_at <= 0:
                gate_at = _as_number(scope.peek_user(user_id, "ccb_last_at", 0.0), 0.0)
            if gate_at > 0 and (now - gate_at) < _ccb.CCB_COOLDOWN_SECONDS:
                cooldown_left = max(
                    0.0,
                    min(
                        _ccb.CCB_COOLDOWN_SECONDS,
                        _ccb.CCB_COOLDOWN_SECONDS - (now - gate_at),
                    ),
                )
        except Exception:
            cooldown_left = 0.0
        return self._ccb_desire_presence(
            cfg,
            libido,
            base_libido,
            affection=affection,
            base_affection=base_affection,
            energy=energy,
            cooldown_left=cooldown_left,
        )

    def _ccb_scene_presence(
        self,
        user_id: str,
        cfg: HumanoidConfig,
        stage: float,
        satisfy: float,
        now: float,
    ) -> str:
        """在场景里那半句：正在亲密里 + 满足度 + 今日第几次 + 刚换过姿势。

        阶段名不在这句里说：它归 `ccb_scene_detail` 管的 scene note
        （`_with_scene_note`）。以前这里有一条补偿分支——细节关掉时把阶段名补回
        这一行，于是「关掉场景细节」只关掉了余韵句，阶段名照样每轮进注入，schema
        承诺的「关掉后退回纯单行」并不成立。开关只有一个意思：关掉就不说阶段。
        """
        # 满足度只说感受、不说读数：体检单不进上下文，模型会把「满足 85/100」
        # 当成绩效指标去解释。档位边界仍取自 ccb 的步长，这里只负责翻词。
        if satisfy >= _ccb.CCB_SATISFY_FULL:
            bits: List[str] = ["她这会儿很满足"]
        elif satisfy >= 2.0 * _ccb.CCB_SATISFY_STEP:
            bits = ["她这会儿挺满足"]
        else:
            bits = ["她还不太满足"]
        # 账本不下发（v13）：原来这里还拼「今日第 N 次」「刚换过姿势」。次数、姿势、
        # 还剩几分钟都是系统口径，对模型没有感觉含义，只提醒它「我在读一个系统的账本」。
        # 这些留给面板与门面；注入只留处境与感受。
        # P1：把参数翻成体感。ccb 里存的是阶段/满足度/姿势/位置/深度/上限/敏感/体力，
        # 这些以前一个都没进上下文，模型只能自己编。词表在 body_sense，这里只负责
        # 接进括号；取不到就退回「只有满足度」那句（少一句远好过整块消失）。
        sense = None
        try:
            sense = _body_sense.collect(self._core, user_id, now)
            bits.extend(_body_sense.feelings(sense, limit=_body_sense.MAX_SCENE_PHRASES))
        except Exception:
            sense = None
        text = "我正在亲密里（" + "，".join(bits) + "）"
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
        # P3：变化优先。状态句说「现在是」，变化句说「刚变成」——后者更容易被当成
        # 正在发生的事。同一状态连着两轮时 delta 返回空串，注入因此逐字不变。
        try:
            move = _body_sense.delta(sense, _body_sense.previous(self._core, user_id))
            if move:
                text += "；" + move
            _body_sense.remember(self._core, user_id, sense)
        except Exception:
            pass
        return text

    def _ccb_desire_presence(
        self,
        cfg: HumanoidConfig,
        libido: float,
        base_libido: float,
        *,
        affection: Optional[float] = None,
        base_affection: Optional[float] = None,
        energy: Optional[float] = None,
        cooldown_left: float = 0.0,
    ) -> str:
        """不在场景里那半句：亲近欲在什么位置、离「忍不住」还差多少。

        用户要能判断"到没到"，所以必须给**数字**和**相对基线的方向**（只给一个
        41/50 的话，模型不知道 41 算高还是低）。

        **门槛必须与 `ccb.evaluate` 同源**：能不能进那一步不只是亲近欲，还有好感、
        好感涨幅、精力、冷却。以前这里只抄了亲近欲那一半，于是会写出「已经到了会
        忍不住的那条线」，而同一时刻 `evaluate()` 因为好感不够直接返回 None——
        模型被告知有戏、实际永远进不去，用户看到的就是「场景是个摆件」。
        现在所有门槛走 `ccb.gate_gaps()`，说的和做的同一个数；有别的门槛没满足时
        只写「还没到那一步，因为…」，不再宣称她已经忍不住。
        """
        libido = max(0.0, min(50.0, libido))
        aff = _as_number(
            affection if affection is not None else _ccb.CCB_AFFECTION_DEEP,
            _ccb.CCB_AFFECTION_DEEP,
        )
        base_aff = _as_number(
            base_affection if base_affection is not None else aff, aff
        )
        eng = _as_number(energy if energy is not None else 100.0, 100.0)
        libido_min = _as_number(
            getattr(cfg, "ccb_libido_min", _ccb.CCB_LIBIDO_MIN), _ccb.CCB_LIBIDO_MIN
        )
        libido_rise = _as_number(
            getattr(cfg, "ccb_libido_rise", _ccb.CCB_LIBIDO_RISE), _ccb.CCB_LIBIDO_RISE
        )
        aff_min = _as_number(
            getattr(cfg, "ccb_affection_min", _ccb.CCB_AFFECTION_MIN),
            _ccb.CCB_AFFECTION_MIN,
        )
        aff_rise = _as_number(
            getattr(cfg, "ccb_affection_rise", _ccb.CCB_AFFECTION_RISE),
            _ccb.CCB_AFFECTION_RISE,
        )
        aff_deep = _as_number(
            getattr(cfg, "ccb_affection_deep", _ccb.CCB_AFFECTION_DEEP),
            _ccb.CCB_AFFECTION_DEEP,
        )
        gaps = _ccb.gate_gaps(
            affection=aff,
            base_affection=base_aff,
            libido=libido,
            base_libido=base_libido,
            energy=eng,
            cooldown_left=cooldown_left,
            libido_min=libido_min,
            libido_rise=libido_rise,
            affection_min=aff_min,
            affection_rise=aff_rise,
            affection_deep=aff_deep,
        )
        lib_gap = gaps["libido_gap"]
        lib_rise_gap = gaps["libido_rise_gap"]
        aff_gap = gaps["affection_gap"]
        aff_rise_gap = gaps["affection_rise_gap"]
        energy_gap = gaps["energy_gap"]
        cd_left = gaps["cooldown_left"]
        diff = libido - base_libido
        if diff <= -1.0 and lib_gap > 0.0:
            # 低于基线、又没过门槛：她这会儿确实没那个心思。给一句正面结论，
            # 比让模型自己从数字里推更不容易读错。
            return "她此刻没有那个心思，亲近的念头比平常还淡"
        tails: List[str] = []
        if lib_gap > 0.0:
            tails.append("还没到忍不住的那条线")
        elif lib_rise_gap > 0.0:
            tails.append("只差没比平常再高一点，还差一点才到忍不住")
        if aff_gap > 0.0:
            # 差多远按「离门槛的比例」分两档：数字不给，方向要给。
            far = aff_gap >= max(1.0, aff_min * 0.5)
            tails.append("关系还没到那一步，还差得远" if far else "关系还没到那一步，就差一点")
        elif aff_rise_gap > 0.0:
            tails.append("这份亲近还没攒够，这一阵涨得还不够")
        if energy_gap > 0.0:
            tails.append("她累坏了，这会儿撑不起一场")
        if cd_left > 0.0:
            tails.append(f"刚收场不久，还要等 {_ccb.duration_text(cd_left)}")
        if not tails:
            tails.append("已经到了会忍不住的那条线")
        if diff >= 1.0:
            lead = "她此刻的欲望：比平常想要"
        elif diff <= -1.0:
            lead = "她此刻的欲望：比平常淡"
        else:
            lead = "她此刻的欲望"
        return lead + "，" + "；".join(tails)

    @staticmethod
    def _safe(call: Callable[[], Any], default: Any = None) -> Any:
        """取一个「外部」值：任何异常都退成默认值。

        注入是每轮都要生成的东西，而它的输入来自五六个子系统。**单行取值抛出异常**
        在历史上至少两次把整段注入吞掉（一次是整块返回空串，一次是关系句整段消失），
        所以凡是不在自己 try 里的外部取值都从这里过——异常只该让那一句缺席，
        不该让整段消失。call 必须是纯读，不能带副作用（这里不负责回滚）。
        """
        try:
            return call()
        except Exception:
            return default

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
        if not self._safe(
            lambda: core.mood.readable(
                user_id, is_group,
                enabled=cfg.mood_enabled, in_group=cfg.mood_enabled_in_group,
            ),
            False,
        ):
            return []
        seed = self._seed(user_id)
        try:
            data = core.mood.profile(user_id)
        except Exception:
            data = {}

        lines: List[str] = []
        position = self._position_line(
            user_id, data, interest, seed, stage_id=self._stage_id(data)
        )
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

        # 冷静期不在这里说（#8）：它已经由情绪层「她内心：…还在为…的事烦着」那条
        # 通道完整表达（emotion._cooling_line 走 mood.cooling_line 的唯一模板）。
        # 两处都说 = 同一个状态两条措辞，正是「同一件事只说一遍」要挡的。

        # 情绪事件：她记得今天发生过什么（被骂了/被逗开心了），不记得才出戏。
        # 这一行以前是函数里唯一没有 try 的外部取值，而它就在每轮最热的路径上：
        # mood 那边任何一个坏值抛出来，整段关系句（位置 + 认识多久）一起消失
        # （审核 注入生成 P-11 / #44）。异常只该让**这一句**缺席。
        emo_ev = self._safe(lambda: core.mood.last_emotional_event(user_id))
        if emo_ev:
            lines.append(emo_ev[0])
        return [line for line in lines if line]

    def _position_line(
        self, user_id: str, data: Dict[str, Any], interest: Dict[str, float],
        seed: List[Any], *, stage_id: Optional[int] = None,
    ) -> str:
        """她对TA的位置：在意度 / 关系标签，只出一句。

        **涨落不在这里说**（P-01）：唯一判定是 `mood.rel_shift`，出句由【此刻的她】的
        tone 句负责。这里再按自己的 8.0 判一遍，同一轮就会说两遍，而且两处阈值不同
        （tone 用 7.0/9.0，这里用 8.0）——模型同时读到「最近更上心」和「感觉挺好」。
        """
        if not data:
            return ""
        cfg = self.config
        shift = getattr(self._core.mood, "rel_shift", None)
        try:
            if callable(shift) and shift(user_id) != "flat":
                return ""
        except Exception:
            pass
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
                state_snapshot.aggression_default(cfg),
            )
        # 在意度高的时候交给关系标签（词表细，热烈/眷恋/吃醋都从这里来）；在意度低的时候
        # 标签往往还在说「调皮」这种好感度词，而真正该说的是「关系疏远、不常联系」——
        # 那才是她对这段关系的定位。两边只出一句，不并列。关系标签只在真的熟了
        # （stage >= 2）时接管，否则会和「还在互相认识」打架（审核 状态与人设 19）。
        if stage_id is not None and stage_id >= 2 and care >= 0.62 and label:
            return f"她对TA{label}"
        care_word = pick("care", seed, scale_word(care, self._care_ladder(stage_id)))
        if care_word:
            return f"她对TA{care_word}"
        return f"她对TA{label}" if label else ""

    def _stage_id(self, data: Dict[str, Any]) -> Optional[int]:
        """关系阶段（唯一真源 `relstage`，经 `state_snapshot`）；算不出来返回 None。

        `_position_line` 的在意度词梯必须与「她和TA到哪一步了」同一事实，否则同一轮里
        会既说「还在互相认识」又说「关系亲密」（审核 状态与人设 19）。
        """
        try:
            return int(
                state_snapshot.relation_of(
                    data,
                    float(self._core.now_epoch()),
                    tz=state_snapshot.tz_of(self._core),
                ).stage_id
            )
        except Exception:
            return None

    @staticmethod
    def _care_ladder(stage_id: Optional[int]):
        """还在「互相认识」时，在意度词梯不许出现「关系亲密」那一档。"""
        if stage_id is not None and stage_id < 1:
            return tuple(band for band in CARE_WORDS if band[0] <= 0.30)
        return CARE_WORDS

    def _calendar_gap(self, then: float, now: float) -> Optional[int]:
        """`now` 比 `then` 晚几个**日历天**（角色时区）；实在拿不到时区返回 None。

        R5 唯一口径：一律走 `clock.day_gap`，不自己 `datetime.fromtimestamp`——
        那个按宿主机时区算，角色设了城市就会差出一整天。测试里的钟替身没有
        `day_gap` 时退回它的 `now().tzinfo`；连时区都没有（`clock.now()` 也不可用）
        才返回 None，让调用方少说一句日期，而不是在别人的时区上瞎猜。
        """
        clock = getattr(self._core, "clock", None)
        gap = getattr(clock, "day_gap", None)
        if callable(gap):
            try:
                return int(gap(then, now))
            except Exception:
                pass
        try:
            tz = getattr(clock, "tz", None)
            if tz is None:
                tz = getattr(clock.now(), "tzinfo", None)
            return int(_clock.day_gap(then, now, tz))
        except Exception:
            return None

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
        # 「认识 N 天」按**日历天**算，不按 24 小时整块截断（P-09）：23:59 认识、
        # 次日 00:01 再说「认识 0 天」是错的——那已经是第二天了。0 天仍走「刚认识
        # 不久」，与旧文案口径一致（认识当天不报天数）。
        days = self._calendar_gap(first_met, now)
        if days is None:
            return ""
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
        夜间模式关掉时同样整块不出（F2）：面板上的「生物钟夜」关着，却仍说她「正睡着」，
        是因为这句只看了 `show_sleep_window`——而那个开关当时被 `night_mode_enabled` 的
        schema condition 藏了起来，用户没有任何办法关掉它。
        """
        cfg = self.config
        if not cfg.show_sleep_window or not cfg.night_mode_enabled:
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

    def _echo_lines(self, *, suppress_wake_echo: bool = False) -> List[str]:
        """她刚：带着上一件事的余韵进来。剩的只有身体事实。

        刚睡醒和睡了两小时后是两回事——余韵只在刚发生的那段时间里存在，过了就不提。
        日程性的余韵（她刚在做完什么）不进这里：日程是背景，不往对话里报。

        `suppress_wake_echo=True` 表示这一轮已经有更细的睡眠惯性句（「她刚醒，人还没
        完全缓过来」），「她刚睡醒」不必再说一遍。由调用方按结构化事实传入，不靠事后
        比对文案（P-15）。
        """
        return [text for text, _key in self._echo_facts(suppress_wake_echo=suppress_wake_echo)]

    def _echo_facts(self, *, suppress_wake_echo: bool = False) -> List[Tuple[str, str]]:
        """(句子, 去重键)。键跟着**事实**走、不跟着文案走（P-07 / P-16）。

        「她刚睡醒」与「她刚醒（N 分钟前）」是同一件事的两种说法，靠字面子串过滤永远
        会漏（两句话里没有一个共同的连续子串）。这里给出同一个 key/group，
        由 `injection.select` 按结构互斥，谁都不用再写一遍 if。
        """
        core = self._core
        facts: List[Tuple[str, str]] = []
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
            if wake > 0 and slept >= 0.5 and not suppress_wake_echo:
                mins = (_core_epoch(self._core) - wake) / 60.0
                if 0 < mins <= 90:
                    facts.append((
                        f"她刚睡醒，睡了{slept:.0f}小时" if slept >= 1.0 else "她刚睡醒",
                        "echo_wake",
                    ))
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
                    facts.append((f"她刚从梦里醒过来（梦到{dreams[0]}）", "echo_dream"))
        return facts

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

        **只给这个人自己的事**（私聊与群聊同一口径）。这张账本是角色级的（社交层
        写进来的），里面有她跟**所有人**没结的事。原来私聊不分人一律塞进每一轮，于是
        每个人都会看到「和星辰之间还有件事没完：讲述姐姐们的事情」——那是别人的事，
        模型拿它当话头就会对着一个陌生人问起另一个人的私事；就算不问，也是每轮都
        占着位置的一份噪音。群聊原来不过滤，理由是「一群人的事混着说是正常的，而且
        这里拿不到在场名单」——前半句把**隐私边界**按会话类型划，同一条事实在私聊
        受保护、在群里公开；后半句不成立：过滤只需要当前说话人的 `user_id`，它就在
        参数里。现在群聊也按说话人筛，只留他自己的事和无归属的事（`uid` 为空的那类
        渲染成「她手上还有件事没完」，不含他人身份）。
        """
        try:
            items = self._core.life_items()
        except Exception:
            return []
        if not items:
            return []
        if user_id:
            mine = str(user_id)
            try:
                items = [
                    item for item in items
                    if not str((item or {}).get("uid") or "") or str(item.get("uid")) == mine
                ]
            except Exception:
                pass
            # TA 说过「这事到此为止」→ 那之前的未完事不再回灌（3.1.15）。
            # 只丢**属于 TA 的**旧条目；无归属的那类（她自己的事）不动。
            try:
                cutoff = self._core.mood.end_intent_at(mine)
                if cutoff > 0:
                    items = [
                        item for item in items
                        if str((item or {}).get("uid") or "") != mine
                        or float((item or {}).get("at", 0) or 0) >= cutoff
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
            nickname = ""
            if is_group:
                # 群里优先用名片名（3.1.15）：`nickname` 是「她认定的称呼」，可能
                # 是自动认定抓错的结果——实测群 1042481523 里她把对方叫成
                # 「一个人类」，因为她自己连着 17 轮回复都以「一个人类。❤️」结尾，
                # 自动认定把她的口头禅当成了对方的名字。名片名是消息自带的，
                # 每轮都在更新，不会被她的口癖带偏。
                # 用户 `/叫我` 显式设过的称呼仍然优先——那是本人的要求。
                if self._core.mood.nickname_source(user_id) != "user":
                    nickname = sanitize_name(self._core.mood.display_name(user_id))
            if not nickname:
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
        # v3.1.9：「私聊，只有她和TA」是**系统视角**的描述（谁在场、还剩谁），模型
        # 读到的是「这是一个封闭场景」的设定说明，于是开场白先交代环境。换成
        # 「在跟TA私聊」——同一件事，但它是她此刻的处境，不是一份场景清单。
        return ["在跟TA私聊"]

    def _background_lines(self, key: str = "") -> List[str]:
        """她此刻身处的世界：几点、几号、周几、季节、她在哪个城市、外头什么天。

        这些是**背景**——模型看到它们就该「知道」，而不是拿去做开场白。所以它们单独成
        【此刻的她】一段递出去，不混进事实块里当要回应的内容。

        `key`（会话/用户标识）只透传给天气那句的去重门（按会话分桶），别的行与它无关。
        """
        cfg = self.config
        lines: List[str] = []
        now = self._core.clock.now()
        # 精确到分钟（v3.1.9）。
        #
        # 为什么不再给秒：秒是**系统账本**的刻度，不是人嘴里的时间。模型读到
        # 「13:03:47」，写出来的就是「现在都 13 点 03 分了」这种报时句；而她真正
        # 需要知道的只是「这会儿是下午一点刚过」。分钟够她判断是「刚说完」还是
        # 「聊了半个钟头」，又不会让秒被念出来。
        #
        # 仍然不报「上午/下午/晚上」这类时段词：那才是真的会被当成打招呼由头的
        # （无论几点都回一句「下午好呀」）。
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
        # 城市先取出来：天气那句的指纹要带上它（换了城市就是天气变了，该说），
        # 「你在X」那行仍只受 show_city_time_in_low_intrusion 管。
        try:
            city_raw = self._core.snapshot(refresh=False).get("city")
        except Exception:
            city_raw = None
        # clock.display_city 在「说不出口」时返回 None（没定城市 / 时区认不出 /
        # 没有中文显示名）：不知道城市就整句省略，不能拼成「你在None」。
        city = city_raw.strip() if isinstance(city_raw, str) else ""
        if cfg.show_city_time_in_low_intrusion and city:
            lines.append(f"你在{city}")
        weather = {}
        try:
            weather = self._core.snapshot(refresh=False).get("weather") or {}
        except Exception:
            weather = {}
        temp = _short_weather(weather)
        if temp and self._weather_worth_saying(temp, key=key, city=city):
            lines.append(f"外头{temp}")
        return lines

    def _weather_worth_saying(self, text: str, key: str = "", city: str = "") -> bool:
        """天气这一句，这一轮该不该再写一遍。

        「外头阴，多云，气温 8℃」是**永远有值、永远具体**的一条，每轮都摆在注入最
        前面就会被念成开场白——线上真实主动消息里 `(守汤) 多云，hanhandouya那边呢？`、
        `(放下餐具) 外头阴沉7度，有点冷呢` 都是这么来的。

        判据：**变了才说**；没变的话至少隔 4 小时才再提一次（真人不会每分钟看一次
        气温，但隔半天会觉得冷）。天气照样每轮进 Core 的 `snapshot()`，只是不再每轮
        进**模型**的上下文。

        `key` 是会话/用户标识：去重状态按它分桶（审核 P-08 #41）。旧写法是本对象上的
        一个 (文本, 时刻) 单值，**整个角色共用一份**——一个会话把「阴，气温 8℃」讲过
        后，另一个会话 4 小时内再也拿不到天气句。`key` 为空时退化成同一个桶（= 旧
        行为），调用方把 key 接上就自动按会话分开。`city` 进指纹：换城市等于天气变了。
        """
        now = 0.0
        try:
            now = float(self._core.now_epoch())
        except Exception:
            now = 0.0
        gate = getattr(self, "_weather_gate", None)
        if gate is None:
            from .services.weather import WeatherSayGate

            gate = WeatherSayGate(repeat_seconds=self.WEATHER_REPEAT_SECONDS)
            self._weather_gate = gate
        return gate.worth_saying(text, key=key, now=now, extra=city)

    def background_note(self, *, key: str = "") -> str:
        """一段「她此刻的世界」，拼进【此刻的她】那一段；拿不到就返回空串。

        `key`（会话/用户标识）只影响天气那句的去重分桶（审核 P-08 #41）：不给就按
        同一个桶算，与旧行为一致。要按会话分开的调用点得把它传进来
        （`main._state_note` 里是 `background_note(key=user_id)`）。
        """
        try:
            lines = [str(x).strip() for x in self._background_lines(key=key) if str(x or "").strip()]
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
        if not (0 < slept < 6.0):
            return ""
        # 「昨晚」不能写死（P-02）：这一觉可能是今天下午的午睡、也可能是三天前的。
        # 用这一觉的**结束时刻** `wake_at` 与 `last_sleep_hours` 反推开始时刻，再按
        # 角色时区的日历天说「今天/昨晚/昨天/前几天」。拿不到时刻就不带日期限定词。
        try:
            wake = float(self._core.soma.wake_state().get("wake_at", 0.0) or 0.0)
        except Exception:
            wake = 0.0
        try:
            now = float(self._core.now_epoch())
        except Exception:
            now = 0.0
        if wake <= 0 or now <= 0:
            return f"最近只睡了{slept:.0f}小时"
        start = wake - slept * 3600.0
        end_gap = self._calendar_gap(wake, now)
        start_gap = self._calendar_gap(start, now)
        if end_gap is None or start_gap is None:
            return f"最近只睡了{slept:.0f}小时"
        end_gap = max(0, end_gap)
        if end_gap == 0 and start_gap <= 0:
            when = "今天"
        elif end_gap == 0:
            when = "昨晚"
        elif end_gap == 1:
            when = "昨天"
        else:
            when = "前几天"
        return f"{when}只睡了{slept:.0f}小时"

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
        # 生理周期不再从这里出（P2）：它由 `_cycle_line()` 统一翻成一句感觉。
        # 原来 full 档会把 `snap["cycle"]` 原样塞进来，而 `energy.cycle_description()`
        # 的措辞是**运营口径**（「激素冲到最高点又骤然回落」「内膜最厚」），
        # 与新的感觉句同时出现就是同一件事两个说法。面板与跨插件契约仍读那个字段。
        return lines

    def _body_echo_quiet(self, now: float) -> bool:
        """她**自己**刚说过身体上的事吗（说过就先别递身体事实）。

        现场（m08275）：她的回复每一句都在说小腹。注入只是一半原因——另一半是自我
        强化：她上一轮说完「肚子难受」，这一轮注入又递一句「小腹坠着」，模型于是把
        这件事当成正在进行的对话主题接着往下说，越说越像在播报病情。

        她刚说过 = 这件事已经在对话里了，不需要再提醒她一遍。
        """
        try:
            last = float(self._core.scope.get_self("body_echo_at", 0.0) or 0.0)
        except Exception:
            return False
        if last <= 0.0:
            return False
        elapsed = float(now) - last
        return 0.0 <= elapsed < _body_voice.ECHO_QUIET_SECONDS

    def _cycle_worth_saying(self, text: str) -> bool:
        """这一轮要不要把「经期身上什么样」递过去。

        现场：那句话一天之内一字不变、每轮都递，模型就把它当台词念——「我这两天小腹
        坠着，腰也酸」被逐字复述，一天几十次。身体事实不该每轮重复播报，真人也不会
        每隔一句就告诉对方自己肚子疼。

        判据三条（都在 `humanoid/body_voice.py`，这里只做取数与记账）：

        1. 对方问到了身体 → 给（问到了却不答，等于她没感觉）；
        2. 她自己刚说过身体上的事 → 不给（已经在对话里，再递就是催她重复）；
        3. 距上次递过去不足 `CYCLE_QUIET_SECONDS`（三天）→ 不给。

        上一版是「一天一次」，仍然等于**每天开场必提**，五天经期提五次；用户的原话是
        「他应该适当的说而不是句句都说」，所以间隔按天改成按三天，并且加了第 2 条。
        """
        scope = getattr(self._core, "scope", None)
        try:
            now = float(self._core.now_epoch())
        except Exception:
            now = time.time()
        asked = _body_voice.asked_about_body(text)
        if not asked and self._body_echo_quiet(now):
            return False
        try:
            last = float(scope.get_self("cycle_said_at", 0.0) or 0.0) if scope else 0.0
        except Exception:
            last = 0.0
        if not _body_voice.cycle_due(now, last, asked=asked):
            return False
        try:
            if scope is not None:
                scope.update_self(cycle_said_at=now)
        except Exception:
            pass
        return True

    def _discomfort_worth_saying(self, text: str) -> bool:
        """这一轮要不要说「身体不适」。

        久坐、闷热、经期都会推高 discomfort，而它一旦过 62 就每轮都出一条「身体不适」
        ——落到她嘴里就是「肚子难受」「身上不舒服」，和周期句叠在一起，等于每一句
        都在讲身体。同样三条判据：对方问了给，她刚说过不给，八小时内说过不给。
        """
        scope = getattr(self._core, "scope", None)
        try:
            now = float(self._core.now_epoch())
        except Exception:
            now = time.time()
        if _body_voice.asked_about_body(text):
            return True
        if self._body_echo_quiet(now):
            return False
        try:
            last = float(scope.get_self("discomfort_said_at", 0.0) or 0.0) if scope else 0.0
        except Exception:
            last = 0.0
        if not _body_voice.due(now, last, _body_voice.DISCOMFORT_QUIET_SECONDS):
            return False
        try:
            if scope is not None:
                scope.update_self(discomfort_said_at=now)
        except Exception:
            pass
        return True

    def _cycle_line(self, text: str = "") -> str:
        """生理周期当前阶段的一句感觉（P2）。

        周期数据早就算好了（`config.cycle_phase_index` 八阶段），但一直只被
        `outfit.scene_of` 和 full 档那一句「激素在往上爬」用过——身体与情绪上
        正在发生什么，模型完全看不到，只能自己编。这里只把阶段翻成感觉，
        **不改任何数值**（用户拍板）：energy / libido 的调制仍在各自原来的地方算。
        """
        try:
            if not self._cycle_worth_saying(text):
                return ""
            return _body_sense.cycle_line(self._core)
        except Exception:
            return ""

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
        sleep_state: Optional[List[str]] = None,
        text: str = "",
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
            sents.append((self._sentence(relation), 8.0, _meta("relation")))
        # 睡眠状态（正睡着/被吵到/没醒透）放在关系之后、其余必需品之前：
        # 它是这一轮最该被看见的处境，又必须保证一定进得去（不随预算被挤掉）。
        # 调用方已经算过就传进来，别为同一件事再问一遍身体（P-12）。
        if sleep_state is None:
            sleep_state = self._sleep_state_lines()
        if sleep_state:
            wake = self._sentence(sleep_state)
            # 与「她刚睡醒」同组：同一件事的两种说法，只留优先级高的那条（P-07）。
            sents.append((wake, 9.5, _meta("sleep_wake", "sleep_wake", "sleep_window")))
        # 睡着时也要知道她心里是什么滋味（半夜被吵醒照样会窝火/心软），但**不要**
        # 「主动性」那一维：睡着的人不会「有点想说话」，而同一轮里睡眠状态句正写着
        # 「正睡着」，两条互斥的事实摆在一起，模型会挑后者（黄金快照 D2）。
        sents.append((
            self._emotion_line(user_id, interest, agency, is_group, asleep=True),
            8.5, _meta("emotion"),
        ))
        # 睡着时也要知道她的欲望/场景位置：ccb 的进门条件里没有"睡着"这一条，
        # 她可以人在睡而状态在推进，模型不知道就会把两种事实读成互斥。
        sents.append((self._ccb_presence_line(user_id, is_group), 9.5, _meta("ccb_presence")))
        situ = self._behavior_lines(
            user_id, events, agency,
            with_previous=self.config.last_interaction_mode == "with_last_msg",
            is_group=is_group,
        )
        if situ:
            sents.append((self._sentence(situ), 7.0, _meta("behavior")))
        # 倾向（想不想说 / 注意力在哪 / 有多少闲）整块跳过：这三条都是**互动前置**
        # 的状态，睡着的人不成立。原来照发，于是同一段正文既写「她这会儿正睡着」
        # 又写「她这会儿还挺专注」「她现在很闲」——睡眠、兴趣、情绪三条线各算各的，
        # 取舍层按 key/group 也看不出语义冲突（黄金快照 D2）。
        # 周期也照给（P2）：睡着的人照样会痛经、会被胀醒。它不是「互动前置」，
        # 不该跟倾向一起被省掉；优先级压到 7.0，预算紧时先丢它。
        cycle = self._cycle_line(text)
        if cycle:
            sents.append((cycle, 7.5, _meta("cycle", "cycle")))
        memory = self._sentence(self._memory_lines(user_id, detailed=detailed))
        if memory:
            sents.append((memory, 6.0, _meta("memory")))
        opens = self._sentence(self._open_lines(user_id, is_group))
        if opens:
            sents.append((opens, 6.0, _meta("opens")))
        linked = self._memory_link_line(memory_text)
        if linked:
            sents.append((linked, 6.0, _meta("memory_link")))

    def _build_medium(
        self, user_id: str, is_group: bool, events, agency, text: str,
        interest: Dict[str, float], memory_text: str = "",
    ) -> List[Tuple[str, float]]:
        """返回 [(句子, 显著度), ...]；排序与预算裁剪在 `_finish` 里做。"""
        snap = self._core.snapshot(refresh=False)
        # 睡眠状态每轮只算一次（P-12）：下面判空、传给必需品、给 _echo_lines 做门禁都用它。
        sleep_state = self._sleep_state_lines()
        # 夜间精简：睡着时省掉生活细节，但必需品（关系/称呼/间隔/记忆）由
        # `_append_sleep_essentials` 补上——睡着不是失忆。
        try:
            asleep = float((snap.get("soma") or {}).get("asleep", 0.0)) >= 1.0
        except (TypeError, ValueError):
            asleep = False
        sents: List[tuple] = [
            (self._sentence(self._scene_lines(is_group)), 10.0, _meta("scene"))
        ]
        if asleep:
            # 「她在睡」这件事由更细的睡眠状态句（正睡着 / 被吵到 / 没醒透）承载；
            # 体感里那句「这会儿在睡」在取的时候就排除，不再事后按文案从正文里删。
            body = self._sentence(
                self._feelings_lines(
                    MAX_FEELINGS_LOW, 0.0, include_sleep=not sleep_state, text=text
                )
            )
            if body:
                sents.append((body, 9.0, _meta("feelings")))
            self._append_sleep_essentials(
                sents, user_id, is_group, events, agency, interest,
                detailed=False, memory_text=memory_text, sleep_state=sleep_state,
                text=text,
            )
            return sents

        # 体感 / 夜间 / 睡眠债是「身体这一块」：同组 → render 用「，」连成一句，
        # 与原来 _sentence 拼出来的形状一致。睡眠相关两句带 when 标签，
        # 关掉睡眠时段开关时由 select 统一丢，不再各处写守卫（P-03）。
        feelings = self._sentence(
            self._feelings_lines(MAX_FEELINGS_LOW, FEELING_THRESHOLD_LOW, text=text)
        )
        if feelings:
            sents.append((feelings, 8.0, _meta("feelings")))
        # 身体这一块的判断句：周期感觉 + 一条倾向，**合成一句**（v3.1.9）。
        # v3.1.8 这里是两句并列（周期一句、倾向一句），再往前是四五句——读起来是
        # 一张状态表，模型会把它翻译成总结（「我今天还挺闲的」）。
        body_judgement = self._body_lines(user_id, interest, agency, text)
        if body_judgement:
            sents.append((self._sentence(body_judgement), 7.5, _meta("body")))
        # 感官细节：只能影响语气、不能直接复述的一句，每轮最多一条（v3.1.9）。
        # 它排得很靠后（5.5），预算紧时先丢它——判断比细节重要，但细节才是「像人」。
        sensory = self._sensory_line(
            self._core.clock.now(),
            (snap.get("soma") or {}),
            self._outfit_text(),
            self._seed(user_id),
        )
        if sensory:
            sents.append((sensory, 5.5, _meta("sensory")))
        night = self._sentence(self._night_lines())
        if night:
            sents.append((night, 8.0, _meta("night", "", "sleep_window")))
        sleep_cause = self._sleep_cause_line()
        if sleep_cause:
            sents.append((sleep_cause, 8.0, _meta("sleep_cause", "", "sleep_window")))

        # 余韵逐条成事实：醒来那句与「她刚醒（N 分钟前）」同组互斥（P-07），
        # 两句都是睡眠信息，带 when 标签（P-03）。键跟着事实走，不按文案过滤。
        for line, key in self._echo_facts(suppress_wake_echo=bool(sleep_state)):
            group = "sleep_wake" if key == "echo_wake" else "sleep_dream"
            sents.append((line, 6.0, _meta(key, group, "sleep_window")))
        wake_state = self._sentence(sleep_state)
        if wake_state:
            sents.append((wake_state, 8.5, _meta("sleep_wake", "sleep_wake", "sleep_window")))

        relation = self._relation_lines(user_id, is_group, interest=interest)
        nickname = self._nickname_line(user_id, is_group)
        if nickname:
            relation = relation + [nickname]
        if relation:
            sents.append((self._sentence(relation), 8.0, _meta("relation")))
        sents.append((self._emotion_line(user_id, interest, agency, is_group), 8.5, _meta("emotion")))

        # 每轮都写：她欲望在什么位置、在不在场景里/余韵里（显著度高于别的句子，
        # 且与上一行的"一条"竞争无关——那条可能就是非 ccb 的情绪）。
        sents.append((self._ccb_presence_line(user_id, is_group), 9.5, _meta("ccb_presence")))

        situ = self._behavior_lines(
            user_id, events, agency,
            with_previous=self.config.last_interaction_mode == "with_last_msg",
            is_group=is_group,
        )
        if situ:
            sents.append((self._sentence(situ), 7.0, _meta("behavior")))

        # medium 档：精力够高时身边句整句消失，形状每条消息都在变。
        hands = self._state_lines(snap, detailed=False)
        if hands:
            sents.append((self._sentence(hands), 4.0, _meta("state")))
        memory = self._sentence(self._memory_lines(user_id, detailed=False))
        if memory:
            sents.append((memory, 6.0, _meta("memory")))
        opens_lines = self._open_lines(user_id, is_group)
        opens = self._sentence(opens_lines)
        if opens:
            sents.append((opens, 6.0, _meta("opens")))
        # P2：手上有没有一件事还没放下——只说有没有，不说是什么（v3.1.9）。
        hint = self._ongoing_hint_line(busy=bool(opens_lines))
        if hint:
            sents.append((hint, 5.0, _meta("ongoing_hint")))
        linked = self._memory_link_line(memory_text)
        if linked:
            sents.append((linked, 6.0, _meta("memory_link")))

        return sents

    def _build_full(
        self, user_id: str, is_group: bool, events, agency, text: str,
        interest: Dict[str, float], memory_text: str = "",
    ) -> List[Tuple[str, float]]:
        """返回 [(句子, 显著度), ...]；与 `_build_medium` 同一形状。"""
        snap = self._core.snapshot(refresh=False)
        # 与 medium 档同一口径：睡眠状态每轮只算一次（P-12）。
        sleep_state = self._sleep_state_lines()
        try:
            asleep = float((snap.get("soma") or {}).get("asleep", 0.0)) >= 1.0
        except (TypeError, ValueError):
            asleep = False
        sents: List[tuple] = [
            (self._sentence(self._scene_lines(is_group)), 10.0, _meta("scene"))
        ]
        if asleep:
            # 与 medium 档同一口径：睡眠状态句在时，体感里的睡眠条目不再重复。
            body = self._sentence(
                self._feelings_lines(
                    MAX_FEELINGS_FULL, 0.0, include_sleep=not sleep_state, text=text
                )
            )
            if body:
                sents.append((body, 9.0, _meta("feelings")))
            self._append_sleep_essentials(
                sents, user_id, is_group, events, agency, interest,
                detailed=True, memory_text=memory_text, sleep_state=sleep_state,
                text=text,
            )
            return sents
        state = self._state_lines(snap, detailed=True)
        if state:
            sents.append((self._sentence(state), 4.0, _meta("state")))
        # 与 medium 档同一结构：身体一块同组、睡眠两句带 when 标签（P-03）。
        feelings = self._sentence(self._feelings_lines(MAX_FEELINGS_FULL, 0.0, text=text))
        if feelings:
            sents.append((feelings, 8.0, _meta("feelings")))
        # 与 medium 档同一句（v3.1.9）：周期只给感觉，与一条倾向合成一句。
        body_judgement = self._body_lines(user_id, interest, agency, text)
        if body_judgement:
            sents.append((self._sentence(body_judgement), 7.5, _meta("body")))
        # 与 medium 档同一句：感官细节，只影响语气、不能直接复述。
        sensory = self._sensory_line(
            self._core.clock.now(),
            (snap.get("soma") or {}),
            self._outfit_text(),
            self._seed(user_id),
        )
        if sensory:
            sents.append((sensory, 5.5, _meta("sensory")))
        night = self._sentence(self._night_lines())
        if night:
            sents.append((night, 8.0, _meta("night", "", "sleep_window")))
        sleep_cause = self._sleep_cause_line()
        if sleep_cause:
            sents.append((sleep_cause, 8.0, _meta("sleep_cause", "", "sleep_window")))
        # 与 medium 档同一口径：醒来那句与「她刚醒」同组互斥（P-07）。
        for line, key in self._echo_facts(suppress_wake_echo=bool(sleep_state)):
            group = "sleep_wake" if key == "echo_wake" else "sleep_dream"
            sents.append((line, 6.0, _meta(key, group, "sleep_window")))
        wake_state = self._sentence(sleep_state)
        if wake_state:
            sents.append((wake_state, 8.5, _meta("sleep_wake", "sleep_wake", "sleep_window")))
        relation = self._relation_lines(user_id, is_group, interest=interest)
        nickname = self._nickname_line(user_id, is_group)
        if nickname:
            relation = relation + [nickname]
        if relation:
            sents.append((self._sentence(relation), 8.0, _meta("relation")))
        sents.append((self._emotion_line(user_id, interest, agency, is_group), 8.5, _meta("emotion")))
        # 与 medium 档同一句：每轮都让模型看见她欲望的位置/在不在场景里。
        sents.append((self._ccb_presence_line(user_id, is_group), 9.5, _meta("ccb_presence")))
        situ = self._behavior_lines(
            user_id, events, agency,
            with_previous=self.config.last_interaction_mode == "with_last_msg",
            is_group=is_group,
        )
        if situ:
            sents.append((self._sentence(situ), 7.0, _meta("behavior")))
        memory = self._sentence(self._memory_lines(user_id, detailed=True))
        if memory:
            sents.append((memory, 6.0, _meta("memory")))
        opens_lines = self._open_lines(user_id, is_group)
        opens = self._sentence(opens_lines)
        if opens:
            sents.append((opens, 6.0, _meta("opens")))
        # 与 medium 档同一句（v3.1.9 / P2）：手上有没有一件事还没放下。
        hint = self._ongoing_hint_line(busy=bool(opens_lines))
        if hint:
            sents.append((hint, 5.0, _meta("ongoing_hint")))
        linked = self._memory_link_line(memory_text)
        if linked:
            sents.append((linked, 6.0, _meta("memory_link")))
        return sents

    def _finish(
        self,
        sents,
        cfg: HumanoidConfig,
        char_name: str = "",
        user_name: str = "",
        tier: str = "medium",
    ) -> str:
        # 兼容三种入参：List[str] / List[(句子, 显著度)] / List[(句子, 显著度, 标签)]。
        # 标签（key/group/when）是这句话的身份；说不说、跟谁说、排第几全交给
        # injection.select（R7）。以前这里自己写了一遍取舍循环，于是「同一个事实
        # 两处各判一次」永远修不干净。
        if isinstance(sents, str):
            sents = [(sents, 10.0)]
        facts: List[_injection.Fact] = []
        for index, item in enumerate(sents):
            meta: Dict[str, str] = {}
            if isinstance(item, str):
                text, salience = item, 10.0
            elif isinstance(item, tuple):
                text = str(item[0])
                try:
                    salience = float(item[1])
                except (TypeError, ValueError, IndexError):
                    # 显著度坏了不能连累整块注入：外面那层 except 会把整个事实块吞掉，
                    # 一句话的排序问题不该让模型这一轮失忆。退回「没给显著度」那一档。
                    salience = 10.0
                if len(item) > 2 and isinstance(item[2], dict):
                    meta = item[2]
            else:
                text, salience = str(item), 10.0
            facts.append(_injection.Fact(
                key=str(meta.get("key") or f"s{index}"),
                text=text,
                group=str(meta.get("group") or ""),
                priority=salience,
                when=str(meta.get("when") or ""),
            ))
        if not facts:
            return ""
        # 抬头只留 `MARK_PREFIX`，**不带版本号**（v3.1.9）。
        #
        # v13 那版抬头是「〔我现在的身体 v13〕」。那个 `v13` 是**我们自己**的格式
        # 版本号：模型每轮读到 `v13`，就多了一个「这是一份被注入的资料、现在第 13
        # 版」的元视角，而它对「像不像人」没有任何帮助。版本号改成只活在常量里
        # （诊断页与测试仍能读），正文不再出现。
        #
        # 抬头本身必须留着：它是这块事实的识别符，旧块靠 `MARK_PREFIX` 前缀清掉，
        # 认不出来就会一轮一轮往上叠（test_v222_injection 有这条断言）。
        header = f"{MARK_PREFIX}〕"
        budget = max(200, int(cfg.inject_token_budget))
        # 取舍只有一个出口（P-03 / P-04 / P-07 / P-14）：条件过滤 → key 去重 →
        # 组内互斥 → 近似文本去重 → token 预算 + 档位字数红线。以前这里是手写的
        # 一遍循环：只看 token、不看字数，也不管「两句话说的是不是同一件事」。
        kept = _injection.select(
            facts,
            # 开关缺省按「开」：真实配置一定有这一项，缺它的是测试桩与旧存档。
            flags={"sleep_window": bool(getattr(cfg, "show_sleep_window", True))},
            budget_tokens=budget,
            max_chars=INJECT_MAX_CHARS.get(tier),
            estimate=estimate_tokens,
            header_tokens=estimate_tokens(header) + 1,
        )
        if not kept:
            # 一句都留不下时不能交白卷（P-14）：预算再紧，最该被看见的那句也要进去；
            # 它自己超了字数红线就截断它，而不是让整块事实变成空字符串。
            top = max(facts, key=lambda fact: fact.priority)
            text = top.text
            cap = INJECT_MAX_CHARS.get(tier)
            # 字数红线之外还有 token 预算：CJK 一字符≈一 token，取两者更紧的那个，
            # 否则「至少留一句」会把 200 token 的预算撑成两千字。
            cap = budget if cap is None else min(cap, budget)
            if len(text) > cap:
                text = text[: max(1, cap - 1)].rstrip("，。；、 ") + "…"
            kept = [_injection.Fact(
                key=top.key, text=text, group=top.group,
                priority=top.priority, when=top.when, source=top.source,
            )]
        body = _injection.render(kept)
        if not body:
            return ""
        # 最后一道形状闸：正文只允许是一行、且不含【…】。正文里那两处内容来自
        # 用户原话和社交插件发布的摘要，它们带什么进来不由这里决定。
        body = _strip_heading_spans(body)
        if not body:
            return ""
        # 归属（v13）：正文一律第一人称。放在 strip 之后、_personalize 之前——
        # _personalize 只负责把「TA」换成对方的名字，不再碰人称。
        body = to_first_person(body)
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

        v13 起**不再把「她」换成角色名**：正文已经统一成第一人称「我」（`to_first_person`），
        模型不需要再做「这个名字指我」的映射。以前那次替换的副作用正是「角色卡感」——
        每句都以「小雨」开头，读起来像在念一份人物档案。

        这里只剩一件事：把独立成词的「TA」换成对方的名字，让模型叫得出人。
        名字取不到时原样返回，退回「TA」，功能不受影响。
        """
        if not text:
            return text
        who = cls.sanitize_name(user_name)
        if who:
            # 全部**独立成词**的「TA」都换（词表里玩家一律写 TA），但不碰
            # DATA / TAOBAO / STAR 这类词里的 TA——那些多半是玩家自己的原话。
            text = _TA_STANDALONE_RE.sub(lambda _m: who, text)
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


# ----------------------------------------------------------------------
# 感官细节的措辞池（v3.1.9）
# ----------------------------------------------------------------------
# 每一条都必须是**外部可感**的：看得见、听得见、碰得到。不写「我感到」「我觉得」
# ——那是内心独白，模型会直接复述成一句台词。长度控制在 6~14 字，太长会被当成
# 一句要念出来的描述。
_SENSORY_SLOT_LINES: Dict[str, Tuple[str, ...]] = {
    "凌晨": ("屋里静得能听见冰箱响", "眼睛还发涩，灯显得刺眼", "桌上那杯水早凉透了"),
    "早上": ("窗帘缝里透进来的光有点白", "屋里还留着被窝的暖气味", "手指刚碰过凉水"),
    "上午": ("窗开着一条缝，风是温的", "屋里有点闷，水已经不烫了", "桌上的光晃了一下"),
    "中午": ("外头的光白得晃眼", "身上有点黏", "饭后的困劲还没散"),
    "下午": ("窗外有车过去", "屋里有点闷，桌上那杯水已经温了", "刚洗完手，指尖还凉"),
    "傍晚": ("天色开始发灰", "屋里没开灯，东西都看不太清", "窗外的人声多起来了"),
    "夜里": ("窗外路灯的光落在墙上", "屋里只剩台灯那一圈亮", "脚有点凉"),
}

_SENSORY_WEATHER_LINES: Dict[str, Tuple[str, ...]] = {
    "rain": ("雨点打在窗上，一阵一阵的", "外头湿气重，屋里也潮"),
    "snow": ("窗外白得发亮", "屋里烘得脸有点干"),
    "wind": ("风一阵一阵的，窗帘动了下", "窗户缝里有点凉风"),
    "hot": ("屋里闷着，风扇转也不顶用", "坐着也出一层薄汗"),
    "cold": ("手有点僵", "呵了口气，玻璃上起雾"),
    "clear": ("日头晒得地板发白", "光落在桌面上，很亮"),
    "cloud": ("天色灰着，看不出几点", "屋里一直是那个亮度"),
}

#: 身体档位对应的细节：低 → 高。取不到精力时这一档整段不出（宁可不给，也不编）。
_SENSORY_BODY_LOW: Tuple[str, ...] = ("后背有点僵", "眼皮发沉")
_SENSORY_BODY_MID: Tuple[str, ...] = ("肩膀有点紧", "坐久了腰有点酸")
_SENSORY_BODY_HIGH: Tuple[str, ...] = ("身上有点热", "手指敲着桌沿")


def _sensory_slot(hour: int) -> str:
    """钟点 → 时段名（与 life_material.SLOT_NAMES 同一套边界）。"""
    if hour < 5:
        return "凌晨"
    if hour < 9:
        return "早上"
    if hour < 12:
        return "上午"
    if hour < 14:
        return "中午"
    if hour < 18:
        return "下午"
    if hour < 21:
        return "傍晚"
    return "夜里"


def _sensory_weather_key(weather: Dict[str, Any]) -> str:
    """天气 → 一个粗档（雨/雪/风/热/冷/晴/阴）。认不出就不给这一档。"""
    text = _short_weather(weather) if isinstance(weather, dict) else ""
    if not text:
        return ""
    for word, key in (("雨", "rain"), ("雪", "snow"), ("风", "wind")):
        if word in text:
            return key
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if match:
        try:
            temp = float(match.group(0))
        except ValueError:
            temp = None
        if temp is not None:
            if temp >= 30.0:
                return "hot"
            if temp <= 8.0:
                return "cold"
    if "晴" in text:
        return "clear"
    if "云" in text or "阴" in text:
        return "cloud"
    return ""


def _sensory_body_lines(body: Dict[str, Any]) -> Tuple[str, ...]:
    """按精力档位给一条身体细节。取不到精力就不给（宁可少一句，不编一句）。"""
    if not isinstance(body, dict):
        return ()
    try:
        energy = body.get("energy")
        value = float(energy.get("value", 0.0) or 0.0) if isinstance(energy, dict) else float(energy or 0.0)
    except (TypeError, ValueError, AttributeError):
        return ()
    if value <= 0.0:
        return ()
    if value < 35.0:
        return _SENSORY_BODY_LOW
    if value > 78.0:
        return _SENSORY_BODY_HIGH
    return _SENSORY_BODY_MID


def _sensory_style_lines(style: str) -> Tuple[str, ...]:
    """按穿搭给一条细节。只认得出厚/薄两类，认不出就不给。"""
    text = str(style or "")
    if not text:
        return ()
    if any(word in text for word in ("外套", "大衣", "毛衣", "羽绒", "厚")):
        return ("领口有点闷", "袖子挽到了小臂")
    if any(word in text for word in ("裙", "短袖", "背心", "薄")):
        return ("腿上有点凉", "胳膊上起了层小疙瘩")
    return ()
