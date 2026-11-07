"""收尾守卫：别让「晚安 → 晚安 → 晚安」变成死循环。

humanoid 是**被动回复**：每条进来的消息都会触发一轮模型调用、然后发一条回复。
这对陪伴型角色基本正确，但有一个明显的坏情况——双方互道晚安时，她说一句「晚安」，
对方也回一句「晚安」，她又得回一句……真人不会这样，会自然停在那里。

这里只负责一件事：**判断这一轮是不是「多余的收尾」**。判断全部是纯函数
（不调模型、不发消息、不睡觉、不写盘），真正的「要不要跳过这一轮」由宿主
（main.py 的 on_llm_request）决定。

判定刻意保守，宁可多回一句也不要吞掉用户的话：

* 当前这句必须**整句只由收尾词构成**（晚安/道别/感谢/应声/早安/亲昵），
  出现问号、请求、新内容、附件就一律不算；
* 她上一句必须是**强收尾**（晚安/道别/感谢/早安/亲昵——光一个「好的」不算），
  且发生在窗口内；
* 时间戳缺失/看不懂/晚于当前输入时一律**放行**（fail-open）。

词表与扫描器照抄市场插件 astrbot_plugin_private_companion 的
companion_v2/conversation/private_closing.py（同一套判定在那边跑了很久），
不自己发明新词——收尾词表一旦发散，就会出现「她说晚安、对方说 nighty night，
她却还在追问」这种看起来像坏掉的行为。
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import re
import unicodedata
from typing import Any, Iterable

from . import clock as _clock

# 窗口：默认半小时。太短会让「说完晚安又刷了会儿手机再回来说晚安」漏判，
# 太长会把「早上起来又聊两句」误判成同一场收尾。
DEFAULT_WINDOW_SECONDS = 1800
MIN_WINDOW_SECONDS = 60
MAX_WINDOW_SECONDS = 7200

# 对方这句是「纯收尾」的上限。收尾话天然很短；给个上界既省分词，也挡住
# 「一长段话最后一个词是晚安」被当成收尾。
MAX_CLOSING_CHARS = 48
# 她自己的话可以更长、也可以带别的意思（「今天辛苦了，好好休息，晚安」），
# 但仍然有界，免得整篇小作文因为结尾两个字变成收尾。
MAX_BOT_CLOSING_CHARS = 120

# 对话窗口留多少条：判定只看「她最后一句 + 对方在那之前的一句」，
# 留 24 条足够覆盖一两轮来回，也不会让每个会话各攒一份长历史。
WINDOW_ENTRIES = 24
# 同一句她在 90 秒内被记两次（on_llm_response 与发送前兜底都会走到）时只留一条。
_DEDUPE_SECONDS = 90.0

GOODNIGHT_TOKENS = (
    "晚安", "安安", "好梦", "做个好梦", "做一个好梦", "做个梦", "梦里见", "梦里相见",
    "睡个好觉", "好好睡", "好好休息", "早点休息", "早点睡", "早睡", "快去睡", "去睡觉",
    "去睡了", "去睡啦", "睡觉去", "睡觉了", "睡觉啦", "该睡了", "该睡啦", "要睡了",
    "要睡啦", "我睡了", "我睡啦", "我先睡了", "我先睡啦", "那我先睡了", "那我睡了",
    "我困了", "困了", "休息了", "我先休息了", "我先休息啦", "先休息了", "先休息啦",
    "睡一下", "眯一会儿", "眯一会", "眯会儿", "躺一会儿", "躺一会", "躺了", "上床了",
    "关灯了", "这就睡", "这就去睡", "马上睡", "马上睡啦", "立刻睡", "先睡了", "先睡啦",
    "睡啦", "睡喽", "睡咯", "睡了", "睡吧", "睡觉", "我睡喽", "我睡吧", "该睡喽",
    "要睡喽", "去睡", "快睡", "还要早起", "要早起", "得早起", "安",
    "goodnight", "nightynight", "night", "gn", "sweetdreams", "sleepwell",
)
FAREWELL_TOKENS = (
    "拜拜", "拜", "白白", "再见", "再会", "回见", "回聊", "明天见", "明儿见",
    "早上见", "晚上见", "下次见", "改天见", "改天聊", "下次聊", "回头聊", "有空聊",
    "先这样", "先不聊了", "不聊了", "先撤了", "先撤", "撤了", "走了", "我走了",
    "下线了", "下了", "先下了", "我去忙了", "忙去了", "去忙了", "先忙了", "你忙吧",
    "去吃饭了", "吃饭去了", "出门了", "我出门了", "去洗澡了", "洗澡去了", "路上小心",
    "一路顺风", "goodbye", "bye", "seeyou", "seeya",
)
THANKS_TOKENS = (
    "谢谢", "多谢", "感谢", "辛苦", "麻烦", "感恩", "谢啦", "谢了",
    "thanks", "thankyou", "thx",
)
ACK_TOKENS = (
    "好的", "好", "好哒", "好嘞", "好呀", "好啊", "好吧", "好嘛", "好啦", "行", "行吧",
    "行呀", "行啦", "可以", "可以的", "可以啦", "收到", "知道了", "知道啦", "明白",
    "明白了", "明白啦", "了解", "了解啦", "没事", "没事的", "没关系", "那就好", "那好",
    "嗯", "嗯嗯", "嗯呢", "嗯哼", "哦", "噢", "喔", "安啦", "ok", "okay", "alright",
    "sure", "fine",
)
MORNING_TOKENS = (
    "早安", "早上好", "早", "早呀", "早啊", "晨安", "morning", "goodmorning",
)
AFFECTION_TOKENS = (
    "爱你", "么么哒", "么么", "亲亲", "抱抱", "啵啵", "mua",
)
TOKEN_KINDS = {
    "goodnight": GOODNIGHT_TOKENS,
    "farewell": FAREWELL_TOKENS,
    "thanks": THANKS_TOKENS,
    "ack": ACK_TOKENS,
    "morning": MORNING_TOKENS,
    "affection": AFFECTION_TOKENS,
}
# 只有强收尾才证明「她自己已经收过尾了」。光一句「好的/嗯」是应声，
# 不能把对方的第一句「晚安」直接吞掉。
STRONG_KINDS = frozenset(("goodnight", "farewell", "thanks", "morning", "affection"))
_TOKEN_KIND = {}
for _kind, _tokens in TOKEN_KINDS.items():
    for _token in _tokens:
        _TOKEN_KIND.setdefault(_token, _kind)
_TOKENS = tuple(sorted(_TOKEN_KIND, key=len, reverse=True))
# 语气词/称呼/时间词：只是让同一句收尾更自然（「晚安啦」「那我也睡了」）。
# 每个位置**先试词表再试它们**，所以 filler 抢不走已知收尾词的开头。
_FILLERS = tuple(sorted({
    "啦", "呀", "啊", "呢", "吧", "哈", "嘛", "咯", "喽", "哒", "滴", "嘞", "哟", "呦",
    "嘿", "诶", "欸", "了", "的", "我", "你", "您", "也", "都", "就", "先", "那", "那么",
    "再", "快", "乖", "是", "宝贝", "亲爱的", "宝宝", "小可爱", "大家", "今天", "今晚",
    "明天", "早上", "晚上", "现在", "已经", "一会儿", "一下",
} - set(_TOKEN_KIND), key=len, reverse=True))
_STRONG_TOKENS = tuple(sorted(
    (token for token, kind in _TOKEN_KIND.items() if kind in STRONG_KINDS),
    key=len, reverse=True))

# 附件占位符（AstrBot 把非文本消息塞进 message_str 时的几种写法）。
_MEDIA_PLACEHOLDER = re.compile(
    r"(?:\[(?:图片|语音|音频|视频|文件|合并转发|引用消息|分享卡片)[^\]]*\])"
    r"|(?:【(?:图片|语音|音频|视频|文件|合并转发|引用消息|分享卡片)[^】]*】)")
# 问句/请求：命中就一定放行——「晚安，你明天几点起？」不是收尾，是要回话的。
_QUESTION_OR_REQUEST = re.compile(
    r"[?？]|吗|几点|要不要|好不好|行不行|可以吗|好吗|你说|你觉得|想不想|去不去|"
    r"吃了吗|睡了吗|在吗|有没有|是不是|会不会|能不能|需要我|记得|提醒|叫我|告诉我|"
    r"发给我|帮我|陪我|听我说")
_ISO_TIME = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")


def normalize(text: Any) -> str:
    """去掉附件占位、标点、符号、空白并统一大小写。

    emoji 属于符号类（So），整条只有 emoji 时归一化后是空串——这种消息不是收尾，
    也就不可能被吞。数字保留，所以「23:00 晚安」不算纯收尾：时间点本身是内容。
    """
    compact = _MEDIA_PLACEHOLDER.sub("", str(text or ""))
    kept = []
    for char in compact:
        if char.isspace() or unicodedata.category(char)[0] in "PSCZ":
            continue
        kept.append(char)
    return "".join(kept).casefold()


def _scan(compact: str) -> tuple:
    """把整段文字按收尾词 + filler 吃干净。

    返回按首次出现顺序的类别元组；只要有**任何一个字符**不在这个小词表里，
    就返回空元组（= 这句不是纯收尾）。每个位置先试词表再试 filler，
    所以「晚安啦」读成「晚安 + 啦」，收尾词不会被语气词抢走。
    """
    index, kinds = 0, []
    while index < len(compact):
        for token in _TOKENS:
            if compact.startswith(token, index):
                index += len(token)
                kind = _TOKEN_KIND[token]
                if kind not in kinds:
                    kinds.append(kind)
                break
        else:
            for filler in _FILLERS:
                if compact.startswith(filler, index):
                    index += len(filler)
                    break
            else:
                return ()
    return tuple(kinds) if kinds else ()


def closing_categories(text: Any) -> tuple:
    """整句只是收尾时返回类别；否则空元组。"""
    raw = str(text or "")
    if "?" in raw or "？" in raw:
        # 带问号的一律当问句：再短的「晚安？」也是想让人回。
        return ()
    compact = normalize(raw)
    if not compact or len(compact) > MAX_CLOSING_CHARS:
        return ()
    return _scan(compact)


def is_closing(text: Any) -> bool:
    return bool(closing_categories(text))


def has_media_text(text: Any) -> bool:
    """文本里带附件占位符（[图片]/【语音】…）就算「有媒体」。

    给宿主做兜底判定用：消息链拿不到时只能看 `message_str` 里的占位符。
    带附件的一律不算收尾——发图配一句「晚安」是有内容的行为。
    """
    return bool(_MEDIA_PLACEHOLDER.search(str(text or "")))


def _ends_with_strong_closing(compact: str) -> bool:
    if not compact:
        return False
    # 「晚安啦」「晚安宝贝」：最多剥三次尾部语气词/称呼，每剥一次看一次强收尾后缀。
    for _ in range(3):
        if any(compact.endswith(token) for token in _STRONG_TOKENS):
            return True
        for filler in _FILLERS:
            if compact.endswith(filler) and len(compact) > len(filler):
                compact = compact[:-len(filler)]
                break
        else:
            return False
    return False


def role_closed(text: Any) -> bool:
    """她那句是不是「强收尾」（可以带别的内容，但必须以收尾结尾且不含问句/请求）。

    光一个「好的」不算：对方的第一句晚安值得一句真正的回应。
    """
    raw = str(text or "")
    if not raw.strip():
        return False
    if _QUESTION_OR_REQUEST.search(raw):
        return False
    compact = normalize(raw)
    if not compact or len(compact) > MAX_BOT_CLOSING_CHARS:
        return False
    kinds = _scan(compact)
    if kinds and any(kind in STRONG_KINDS for kind in kinds):
        return True
    return _ends_with_strong_closing(compact)


def _moment(value: Any):
    """把时间戳统一成 UTC datetime；看不懂就 None（调用方据此放行）。

    humanoid 自己的对话窗口用 epoch 秒（float），但也要能吃 datetime 与 ISO 串，
    这样测试与将来的历史来源（宿主会话记录）都能直接递进来。
    """
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, bool):
        return None
    elif isinstance(value, (int, float)):
        try:
            moment = _clock.local(float(value), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str) and _ISO_TIME.fullmatch(value.strip()):
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _speaker(item: dict) -> str:
    """对话条目的方向：humanoid 用 dir（in/out），也兼容 speaker（user/assistant）。"""
    raw = str(item.get("dir") or item.get("speaker") or "").strip().lower()
    if raw in ("out", "assistant", "bot", "self", "role"):
        return "out"
    if raw in ("in", "user", "peer", "them"):
        return "in"
    return ""


def _text_of(item: dict) -> str:
    return str(item.get("text") or item.get("content") or "")


def _last_exchange(dialogue) -> tuple:
    """从按时间排好的对话里取（她最后一句, 那句之前对方最近的一句）。"""
    items = tuple(item for item in (dialogue or ()) if isinstance(item, dict))
    assistant_index = None
    for index in range(len(items) - 1, -1, -1):
        if _speaker(items[index]) == "out":
            assistant_index = index
            break
    if assistant_index is None:
        return None, None
    prior_user = None
    for index in range(assistant_index - 1, -1, -1):
        if _speaker(items[index]) == "in":
            prior_user = items[index]
            break
    return items[assistant_index], prior_user


def clamp_window(value: Any, default: float = DEFAULT_WINDOW_SECONDS) -> float:
    """窗口配置夹到 [60, 7200]：配错（0、负数、字符串）时退回默认，别让守卫整条失效。"""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return float(default)
    if seconds != seconds:  # NaN
        return float(default)
    return float(min(MAX_WINDOW_SECONDS, max(MIN_WINDOW_SECONDS, seconds)))


def closing_decision(
    *,
    current_text: Any,
    dialogue=None,
    at: Any,
    has_media: bool = False,
    window_seconds: Any = DEFAULT_WINDOW_SECONDS,
    enabled: bool = True,
) -> dict:
    """这一轮是不是「多余的收尾」。

    dialogue 按时间正序，条目是 {"dir": "in"/"out", "text": ..., "ts": epoch 秒}；
    当前这句不必在里面（判定只看她最后一句 + 那句之前对方的一句）。

    返回 dict：suppress_reply / action / reason / last_reply_at / categories /
    last_assistant_closing / prior_user_closing / window_seconds。
    reason 的取值与参考实现一致，共 11 个。
    """
    window = clamp_window(window_seconds)
    base = {
        "suppress_reply": False,
        "action": "reply",
        "reason": "not_evaluated",
        "last_reply_at": None,
        "categories": (),
        "last_assistant_closing": False,
        "prior_user_closing": False,
        "window_seconds": window,
    }
    if not enabled:
        return {**base, "reason": "disabled"}
    categories = closing_categories(current_text)
    assistant, prior_user = _last_exchange(dialogue)
    evidence = dict(base)
    role_closed_flag, prior_closed, delta = False, False, None
    if assistant is not None:
        role_at = _moment(assistant.get("ts", assistant.get("at")))
        now = _moment(at)
        if role_at is not None and now is not None:
            delta = (now - role_at).total_seconds()
            if 0 <= delta <= window:
                role_closed_flag = role_closed(_text_of(assistant))
                prior_closed = bool(
                    prior_user is not None and is_closing(_text_of(prior_user))
                )
                evidence.update(
                    last_reply_at=assistant.get("ts", assistant.get("at")),
                    last_assistant_closing=role_closed_flag,
                    prior_user_closing=prior_closed,
                )
    if categories:
        evidence["categories"] = categories
    if has_media:
        # 发图/发语音/发文件都是有内容的行为，哪怕配文是「晚安」也不该被吞。
        return {**evidence, "reason": "media_input"}
    if not categories:
        # 这句不是收尾，永远不该被吞；但把「她上一句是收尾」的证据留着，
        # 好让提示词那一层提醒她别再追一句。
        return {**evidence, "reason": "not_a_closing"}
    if assistant is None:
        return {**base, "reason": "no_role_reply"}
    if delta is None:
        return {**base, "reason": "history_time_unknown"}
    if delta < 0:
        return {
            **base,
            "reason": "history_after_input",
            "last_reply_at": assistant.get("ts", assistant.get("at")),
        }
    if delta > window:
        return {
            **base,
            "reason": "outside_window",
            "last_reply_at": assistant.get("ts", assistant.get("at")),
        }
    if role_closed_flag:
        return {
            **evidence,
            "suppress_reply": True,
            "action": "silent",
            "reason": "role_already_closed",
        }
    if prior_closed:
        return {
            **evidence,
            "suppress_reply": True,
            "action": "silent",
            "reason": "repeated_user_closing",
        }
    return {**evidence, "reason": "role_did_not_close"}


def closing_instruction(decision: Any, *, prompt_enabled: bool = True) -> str:
    """不能硬跳过时，塞给模型的一句短提示（只在这一轮生效的临时块）。

    为什么还要提示：宿主未必允许跳过模型（不同 AstrBot 版本、第三方 runner），
    那就退一步——告诉她「已经收过尾了，别再追一句」，但**绝不吞消息**。
    """
    if not prompt_enabled:
        return ""
    if not isinstance(decision, dict):
        return ""
    if decision.get("reason") == "disabled":
        return ""
    if decision.get("suppress_reply"):
        return ""
    if decision.get("last_assistant_closing"):
        return (
            "【对话收尾】上一轮你已经用晚安、道别或感谢一类话收尾。对方这句如果仍然只是"
            "收尾、致谢或简短应声，就不要再补一条同义道别，也不要另起话题、追问或索要回应；"
            "让对话自然停在这里。出现新的问题、请求或需要接住的明确情绪时再正常回应。"
        )
    if decision.get("prior_user_closing"):
        return (
            "【对话收尾】对方上一句已经在道别或收尾。若本轮仍然只是收尾，不必为了礼貌再追一句；"
            "有新问题、请求或需要回应的事实再正常回应。"
        )
    return ""


class DialogueWindow:
    """每个会话最近若干条来去的轻量记录（humanoid 的历史里没有时间戳）。

    为什么要自己记：宿主的会话历史只有 role/content，没有时刻；而收尾判定必须
    知道「她上一句是多久以前说的」（窗口外就该正常回）。所以这里按会话记一份
    只进内存的小窗口——不落盘、不参与记忆、插件重启就清空（清空只会让判定退回
    放行，不会误吞消息）。
    """

    __slots__ = ("_items", "_maxlen")

    def __init__(self, maxlen: int = WINDOW_ENTRIES) -> None:
        self._maxlen = max(2, int(maxlen))
        self._items = deque(maxlen=self._maxlen)

    def record(self, direction: str, text: Any, at: Any) -> None:
        """记一条；空文本忽略，同方向同句在 90 秒内只留一条（两条记录路径会重叠）。"""
        body = str(text or "").strip()
        if not body:
            return
        side = "out" if str(direction).lower() in ("out", "assistant", "bot", "self") else "in"
        try:
            stamp = float(at)
        except (TypeError, ValueError):
            stamp = 0.0
        if self._items:
            last = self._items[-1]
            if (
                last.get("dir") == side
                and str(last.get("text") or "") == body
                and abs(stamp - float(last.get("ts") or 0.0)) <= _DEDUPE_SECONDS
            ):
                return
        self._items.append({"dir": side, "text": body, "ts": stamp})

    def entries(self) -> list:
        return [dict(item) for item in self._items]

    def __len__(self) -> int:
        return len(self._items)

    def clear(self) -> None:
        self._items.clear()
