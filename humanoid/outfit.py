"""穿搭：她此刻穿着什么。

**「预设」不是固定模板**，只是给她的**一件衣服**（一个起点）。她知道自己穿着什么，
能穿就穿、能脱就脱、该光就光——插件只负责把这件衣服挑得贴合她（名字 / 人设 / 物种），
并在合适的场景（睡觉 / 洗澡 / 出门 / 冷天 / 生理期）做相应调整。

两条硬规矩：

1. **识别不出她是什么、又没有手写预设时，什么都不注入**（宁缺勿错——不给一件不合身的衣服）。
2. 穿着是**背景**，不是台词。她只是「知道」自己穿什么，要不要提、怎么提由她自己定。

这个模块是纯函数：给定（预设 / 名字 / 人设 / 场景），算出此刻穿什么。
"""

from __future__ import annotations

import math
import re
from typing import Any, Optional

from astrbot.api import logger as _LOG

# ── 种类：粗分，够挑衣服就行 ──
KIND_HUMAN = "human"          # 人类
KIND_BEAST_EAR = "beast_ear"  # 兽耳 / 半兽：人形 + 兽征，照常穿衣服
KIND_BEAST = "beast"          # 兽 / 拟人动物：多为兽形，通常不穿衣服
KIND_SPIRIT = "spirit"        # 精灵 / 妖 / 鬼 / 神魔 / 天使 / 吸血鬼
KIND_MACHINE = "machine"      # 机械 / 仿生 / AI / 人偶

# ── 物种判定：三层 ──
#
# 为什么不能裸扫子串：`喜欢恐龙` / `家里水龙头坏了` / `穿着尼龙外套` 里的「龙」不是物种，
# 而用户真正想用的「狼 / 马 / 龙 / 兽人」反倒因为词表里没有裸字而判不出来。所以：
#
#   层① **先 mask 语素复合词**（只在匹配副本上挖掉，原文一个字不动）；
#   层② 动物词必须落在**身份位置**（有系动词 / 物种声明的锚点）才算数；
#   层③ 人形非人（spirit / machine / beast_ear）只扫**身份区**（前 600 字 + 含身份词的行），
#        于是角色**名字**（以及正文里聊到的别的东西）不再能翻转物种。
#
# 层① 的 mask 表按「长词在前」排列：先挖掉「天使投资」，剩下的才不会误命中。
# **裸「天使」「凤凰」不 mask**：它们要留给层③ 在身份区里判 spirit（`你是一个天使`），
# 而误判源是「天使投资」「凤凰男」这种复合词——那些才在表里。这与 `_SPIRIT_WORDS` 里
# 其余词（精灵 / 恶魔 / 女巫 …）的口径一致。
# **不要** mask「恐龙」本身——真实恐龙人设要判 beast，要挡的是「恐龙园 / 恐龙蛋」这类。
_MASK_WORDS: tuple[str, ...] = (
    "尼龙", "水龙头", "龙头", "龙眼", "火龙果", "乌龙", "龙虾", "龙套", "龙舟", "龙井",
    "马虎", "马赛克", "人马", "马上", "马路", "木马", "码头", "小马哥",
    "狼人杀", "狼狗", "狼藉", "人鱼线", "仙人掌", "仙人球",
    "机械表", "机械化", "机械师", "人偶剧", "玩偶之家", "傀儡",
    "兽医学", "兽医", "天使投资", "神仙水", "凤凰男",
    "麒麟臂", "魔女宅急便", "独角兽公司", "猫耳朵", "尾巴骨", "掏耳朵",
    "恐龙园", "恐龙蛋", "恐龙头骨", "恐龙化石",
)

# 层②：身份位置里的动物词。锚点（是 / 为 / 种族 / 物种 / 设定 …）之后 0~6 个字内出现
# 一只 / 一头 / 一条 … 再跟动物词，才算「她在声明自己是什么」。
# 没有锚点的裸词一律不算：`喜欢猫` / `喜欢骑马` / `我家养了条狗` 都不是物种声明。
# 主语绑定：系动词的主语必须是**她/你/我**本身，中间只许隔几个副词。
# 「她的好朋友是一只猫」里系动词的主语是「好朋友」，不是她——以前裸扫「是…猫」
# 会把她判成兽形，于是注入「她穿着：没穿衣服」。
_SUBJECT = r"(?:她|你|我|他|它|本人|自己)"
_ADVERB = r"(?:现在|就|其实|也|都|真的|本来|确实|已经)?"
_COPULA = r"(?:是|为|乃|变成|化身|身为|属于)"
_ANIMAL = (
    r"(?:狼|马|龙|狐|虎|熊|兔|蛇|鹰|狮|象|鹿|鲸|猫|犬|豹|"
    r"兽人|半兽人|人鱼|独角兽|恐龙|龙娘|龙女)"
)
_IDENTITY_RE = re.compile(
    r"(?:"
    # ① 主语 + 系动词：「你是一只猫」「她其实是狼」
    + _SUBJECT + _ADVERB + _COPULA
    # ② 声明式锚点：本来就没有主语（「种族：狼」「作为一个兽人」）
    + r"|(?:作为一个|身为|种族|物种|原型|本体|化形|设定|身份)[:：]?"
    + r")"
    r"[^，。！？；\n]{0,6}"
    r"(?:一只|一头|一条|一匹|一尾|一个|只|头|条|匹|尾)?\s*"
    + _ANIMAL
)

# 层③：身份区 = 前 600 字 + 含下面这些词的行。人形非人只在身份区里判。
_IDENTITY_HINT_RE = re.compile(r"(你是|我叫|种族|物种|设定|外貌|身份|本体|原型)")
_IDENTITY_ZONE_CHARS = 600

# ── 人设里写了她穿什么：直接抄原文 ──
#
# 判不出物种时，原来会兜底 human、注入一句对所有人都一样的「日常便服」。
# 人设里如果写了「她总穿一条白色连衣裙」，那句原文才是她此刻的穿着——比内置默认贴她。
# 只认**带穿着动词**的短语（穿 / 身穿 / 身着 / 一身 / 打扮 …），截到标点为止。
# 裸「穿」要排除「穿越 / 穿梭 / 穿行」：人设里这些词远比「穿某件衣服」常见。
_CLOTHING_RE = re.compile(
    r"(?:总是|经常|习惯|平时|通常|大多)?"
    r"(?:穿着|身穿|身着|套着|一身|打扮成|打扮|穿(?!梭|越|插|透|过|行|戴))"
    r"([^，。；！？\n]{2,24})"
)
# 这些开头说明抓到的不是衣服（「穿得很随意」「穿着得体」），跳过继续找。
_CLOTHING_BAD_LEAD = ("得", "很", "非常", "太", "比较", "不", "起来", "着", "了")
# 这些字样说明抓到的多半是**别人**的穿着（「街上穿着西装的人」），不是她的。
_CLOTHING_BAD_TAIL = ("的人", "人们", "身影", "自己")


def persona_clothing(persona: str) -> str:
    """从人设里抽一句「她穿什么」的原文；抽不到返回空串（= 不注入）。"""
    body = str(persona or "")
    if not body:
        return ""
    for match in _CLOTHING_RE.finditer(body):
        text = match.group(1).strip().lstrip("着上了")
        if len(text) < 2 or text.startswith(_CLOTHING_BAD_LEAD):
            continue
        if any(bad in text for bad in _CLOTHING_BAD_TAIL):
            continue
        return text
    return ""

# **物理兽征**（耳朵 / 尾巴 / 毛茸茸）：人形 + 兽征，照常穿衣服，所以判定优先级最高。
# 「有兽耳的人形角色穿衣」是安全侧：判错成 beast 会让她「没穿衣服」，那才是事故。
_BEAST_EAR_FEATURES: tuple[str, ...] = (
    "兽耳", "猫耳", "狐耳", "狼耳", "兔耳", "犬耳", "熊耳", "耳朵",
    "兽尾", "尾巴", "毛茸茸",
)

# **人形兽词**：本身就是「人形 + 兽」的词，也走 beast_ear（穿衣）。放在层② 之后——
# `你是一只猫娘` 这种只有「猫」的句子该判 beast（用户明确要求），
# 只有真写了兽耳 / 尾巴（`你是一只猫娘，有猫耳和尾巴`）才升到 beast_ear。
_BEAST_EAR_WORDS: tuple[str, ...] = (
    "猫娘", "狐娘", "狼女", "兽娘", "兽人娘",
)

# **固有物种词**：它们不是别的词的构词成分（`小马哥` / `人鱼线` / `狼人杀` /
# `独角兽公司` 已在层① mask），所以不依赖系动词锚点——「你会说话的小马」是人设里
# 最常见的写法，没有「是」也必须判得出来。
_BEAST_STRONG: tuple[str, ...] = (
    "小马", "马娘", "独角兽", "兽人", "半兽人", "狼人", "狐狸精", "九尾", "人鱼",
    "神兽", "猫妖", "犬妖", "鹰身", "狮鹫",
)

# 精灵 / 妖 / 鬼 / 神魔：只在身份区里判（层③）。
_SPIRIT_WORDS: tuple[str, ...] = (
    "精灵", "妖精", "恶魔", "天使", "凤凰", "幽灵", "鬼魂", "吸血鬼", "神仙", "仙人",
    "魔女", "女巫", "巫女", "神女", "龙女", "狐仙", "妖怪", "仙灵", "梦魔", "魅魔",
)

# 机械 / 仿生 / 人偶：只在身份区里判（层③）。
_MACHINE_WORDS: tuple[str, ...] = (
    "机器人", "机械", "仿生", "人工智能", "人偶", "傀儡", "玩偶", "机甲",
    "android", "robot", "cyborg",
)

# ── 内置基础穿着（按种类）──
#
# 只给**一句够用的默认**，不写细（细节交给人设和模型）。手写预设（配置）会盖过它。
# 注意 beast 类是「不穿衣服」——那也是一种「符合」，不是「没有」。
# **人类不设默认**：KIND_HUMAN 已不由 detect_kind 返回（判不出来 = 不知道），
# 所以「日常便服」这句对所有人都一样的文本没有出处了——宁缺勿错，不注入。
_BASE: dict[str, str] = {
    KIND_BEAST_EAR: "日常便服",
    KIND_SPIRIT: "一身轻盈的长袍",
    KIND_MACHINE: "简洁的机能装",
    KIND_BEAST: "没穿衣服",
}

# 各场景下基础穿着怎么变（用「加在基础后面」的短语，避免和基础拼重）。
# 空串 = 不调整。beast 类本来就不穿，任何场景都不加。
_SCENE_SUFFIX: dict[str, str] = {
    "out": "",
    "cold": "（外面冷，多套了件外套）",
    "unwell": "（换成了宽松的家居服）",
    "home": "",
}

# 睡觉 / 洗澡这类场景里「不穿」怎么写。beast 类本来就写「没穿衣服」，不重复。
_SCENE_BARE = {
    "sleep": "睡衣",
    "bath": "没穿衣服",
}

# **手写预设**遇到 sleep / bath 时追加的短语。原来是整段替换成「睡衣」，
# 用户自己写的「白色蕾丝连衣裙」被丢掉了——那是用户的东西，只能追加不能扔。
_SCENE_PRESET_NOTE = {
    "sleep": "（睡前换上了睡衣）",
    "bath": "（刚洗过澡）",
}

# 冷阈：出门在外更敏感（≤10℃ 就加外套），居家只在真的冷时（≤5℃）才动。
_COLD_OUTDOOR_C = 10.0
_COLD_INDOOR_C = 5.0


def _debug(message: str, logger: Any = None) -> None:
    try:
        (logger or _LOG).debug(f"[humanoid_core] 穿搭: {message}")
    except Exception:
        pass


def _masked(body: str) -> str:
    """层①：在**匹配副本**上把语素复合词挖成等长的方块（原文不动）。"""
    text = body
    for word in _MASK_WORDS:
        if word in text:
            text = text.replace(word, "\u25a1" * len(word))
    return text


def _identity_zone(masked: str) -> str:
    """层③：身份区 = 前 600 字 + 含身份关键词的行。

    取「前 600 字」是因为人设开头的自我介绍才是身份声明；取整行是因为
    「种族：狼」这种声明可能写在很后面。角色**名字**不在这里——它由调用方传入、
    不参与判定（名字叫「小龙女」不该把角色判成 beast）。
    """
    head = masked[:_IDENTITY_ZONE_CHARS]
    lines = [line for line in masked.splitlines() if _IDENTITY_HINT_RE.search(line)]
    return head + "\n" + "\n".join(lines)


# 否定词：命中词**前面**几个字里有它就不算数（「她没有兽耳」「不是精灵」）。
_NEG_WORDS = ("没有", "没", "无", "不是", "并非", "而不是", "不带", "不长", "不算")
_NEG_WINDOW = 4


def _negated(text: str, pos: int) -> bool:
    return any(word in text[max(0, pos - _NEG_WINDOW):pos] for word in _NEG_WORDS)


def _hit(text: str, word: str) -> bool:
    """这个词有没有**非否定**的出现。"""
    start = 0
    while True:
        idx = text.find(word, start)
        if idx < 0:
            return False
        if not _negated(text, idx):
            return True
        start = idx + len(word)


def _first_hit(zone: str, words: tuple[str, ...]) -> bool:
    return any(_hit(zone, word) for word in words)


def detect_kind(name: str, persona: str) -> str:
    """从人设正文里判她是什么种类。

    返回 KIND_*；**判不出来就返回空串**（没有任何依据 → 不注入，调用方据此决定）。
    以前判不出来兜底 KIND_HUMAN，于是「不知道」和「确实是人」分不开：
    每个角色都被塞一句对所有人都一样的「日常便服」。

    `name` 只保留在签名里（调用方与面板都要用同一个入口），**不参与判定**：
    角色名是别人随手填的字段，不该决定她穿不穿衣服（原来它会被裸扫，
    名字里带个「龙」就把角色判成 beast）。
    """
    body = str(persona or "").strip()
    if not body:
        return ""
    masked = _masked(body.lower())
    zone = _identity_zone(masked)

    # ① 人形 + 兽征（耳朵 / 尾巴）：穿衣是安全侧，优先级最高。
    if _first_hit(zone, _BEAST_EAR_FEATURES):
        return KIND_BEAST_EAR
    # ② 身份位置上的动物词 → 兽形（只扫身份区，且要求主语是她自己）。
    if _IDENTITY_RE.search(zone):
        return KIND_BEAST
    # ③ 人形兽词（猫娘 / 兽娘 …）→ 还是人形，穿衣。
    if _first_hit(zone, _BEAST_EAR_WORDS):
        return KIND_BEAST_EAR
    # ④ 其余只在身份区里判：机械 → 精灵 → 固有物种词。
    if _first_hit(zone, _MACHINE_WORDS):
        return KIND_MACHINE
    if _first_hit(zone, _SPIRIT_WORDS):
        return KIND_SPIRIT
    if _first_hit(zone, _BEAST_STRONG):
        return KIND_BEAST
    # 没有物种证据 = 不知道，不是「人类」：不注入比注入一句无信息的话好。
    return ""


def base_outfit(kind: str) -> str:
    """内置基础穿着；不认识的种类返回空串。"""
    return _BASE.get(str(kind or ""), "")


# 日程事件里出现这些词 = 在睡 / 在洗 / 出门。
# 「梦」不在表里：`聊梦想` / `做梦想的事` 会被它判成 sleep，而 sleep 是 scene_of 的
# 第一个判据，会把 out / cold 整个盖掉。只留明确的「做梦 / 梦游 / 噩梦」。
_SLEEP_WORDS = ("睡", "入睡", "午睡", "小憩", "赖床", "打盹", "做梦", "梦游", "噩梦")
_BATH_WORDS = ("洗澡", "沐浴", "泡澡", "淋浴", "泡温泉", "冲凉", "沐浴")
_OUT_WORDS = ("出门", "外出", "上班", "上学", "通勤", "逛街", "买菜", "赴约",
              "旅行", "旅游", "出差", "跑步", "散步", "运动")

# 温度串里的数字：兼容旧调用方传「格式化好的天气展示串」。
# `(-?\d+(?:\.\d+)?)` + 单位（℃ / °C / ° / 度 / C）。「阴，3度」「12 C」都认。
_TEMP_IN_TEXT_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:℃|°c|°|度|c\b)", re.IGNORECASE)


def coerce_temp_c(value: Any, logger: Any = None) -> Optional[float]:
    """把温度参数收成数值：**优先接受数值**，字符串走兼容回退。

    旧路径是 `core_instance` 用正则从**格式化好的天气展示串**里第三次抠温度——
    文案一改就静默失配（「阴，3度」「12 C」都抠不出数字，cold 永不触发且没有任何日志）。
    现在数值直接过，字符串才解析；解析不出来记一条 debug，不再静默。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = str(value).strip()
    if not text:
        return None
    match = _TEMP_IN_TEXT_RE.search(text)
    if match:
        try:
            return float(match.group(1))
        except (TypeError, ValueError):
            return None
    _debug(f"温度解析不出数值，按「没有温度」处理：{text[:40]!r}", logger)
    return None


def scene_of(
    *,
    event: str = "",
    asleep: bool = False,
    temp_c: Optional[float] = None,
    discomfort: float = 0.0,
    cycle_phase: int = -1,
    logger: Any = None,
) -> str:
    """从当下的状态里推「场景」。返回 sleep / bath / out / cold / unwell / home / ""。

    `temp_c` 收数值（推荐）也收旧的天气展示串，见 `coerce_temp_c`。
    """
    ev = str(event or "")
    temp = coerce_temp_c(temp_c, logger)
    if asleep or any(w in ev for w in _SLEEP_WORDS):
        return "sleep"
    if any(w in ev for w in _BATH_WORDS):
        return "bath"
    # 出门在外优先于「不适」：人在外面，生理期 / 不舒服也不该被换成家居服
    # （原来 unwell 排在 out 前面，「出门买菜 + 2 度 + 不适 70」会返回 unwell）。
    if any(w in ev for w in _OUT_WORDS):
        # 出门 + 冷 → 加外套；否则就是基础穿着出门
        if temp is not None and temp <= _COLD_OUTDOOR_C:
            return "cold"
        return "out"
    if temp is not None and temp <= _COLD_INDOOR_C:
        return "cold"
    if cycle_phase == 0 or float(discomfort or 0.0) >= 62.0:
        return "unwell"
    return "home"


def resolve(
    *,
    preset: str = "",
    name: str = "",
    persona: str = "",
    scene: str = "",
    scene_variation: bool = True,
    temp_c: Optional[float] = None,
) -> str:
    """算出此刻的穿着。

    手写预设优先；其次是人设里写的穿着短语（原文照抄）；再其次是按识别到的种类取
    内置默认。三者都没有 → 返回空串（不注入）。
    返回空串还表示「不要给这一条」，调用方据此决定注入不注入。

    `temp_c` 是温度入口（数值优先，见 `coerce_temp_c`）：调用方可以只给温度不给场景，
    这时数值够冷就按冷天处理；给了场景时以场景为准（场景由 `scene_of` 算）。
    """
    text = str(preset or "").strip()
    if not text:
        # 人设里写了她穿什么，就用那句原文——比内置默认贴她。
        text = persona_clothing(persona)
    # 手写预设 / 人设原文都是「她自己的衣服」：睡觉 / 洗澡只能追加说明，不能整段替换。
    has_own_text = bool(text)
    kind = ""
    if not text:
        kind = detect_kind(name, persona)
        if not kind:
            return ""
        text = base_outfit(kind)
        if not text:
            return ""
    # 手写预设也判一次种类，只是用来决定场景调整要不要做（beast 不穿衣，别硬加睡衣）
    if not kind:
        kind = detect_kind(name, persona) or KIND_HUMAN
    if not scene_variation or kind == KIND_BEAST:
        return text
    scene = str(scene or "")
    if not scene and temp_c is not None:
        temp = coerce_temp_c(temp_c)
        if temp is not None and temp <= _COLD_INDOOR_C:
            scene = "cold"
    if scene in _SCENE_BARE:
        if has_own_text:
            # 用户写下的衣服只能**追加**，不能整段替换掉（那是用户写的东西）。
            note = _SCENE_PRESET_NOTE.get(scene, "")
            if note and note not in text:
                return text + note
            return text
        bare = _SCENE_BARE[scene]
        # 手写预设/基础里已经写了这个状态的，不重复
        if bare and bare not in text:
            return bare
        return text
    suffix = _SCENE_SUFFIX.get(scene, "")
    if suffix and suffix not in text:
        return text + suffix
    return text


# 这些是「状态」不是「衣服」：接在「她穿着：」后面会变成病句「她穿着：没穿衣服」。
_BARE_STATES = frozenset({"没穿衣服"})


def line(text: str) -> str:
    """注入用的一句话。空串 → 空串（调用方据此不注入）。

    「没穿衣服」这类状态型 body 走另一模板（「她没穿衣服」），其余仍是「她穿着：X」。
    """
    body = str(text or "").strip()
    if not body:
        return ""
    if body in _BARE_STATES:
        return f"她{body}"
    return f"她穿着：{body}"
