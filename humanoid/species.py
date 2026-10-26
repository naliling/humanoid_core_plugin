"""物种与身体结构事实（只描述结构，不写器官、不下指令）。

用途：CCB 场景里给模型一句「她的身体是什么结构」的事实——小马/半人马/人鱼/龙娘/
触手系不再只能按人类写；契约/门面导出给社交层，主动消息也能提一句。

为什么单独一层：outfit.py 已有成熟的「物种→穿不穿衣服」判定，但穿搭只关心
「是不是兽形」，不关心「四蹄/鬃毛/可爱标志/触手/翅膀」这些场景里真正要用的结构。
这里复用它的词表与防误判思路，只补结构层；**mask 与分类顺序自己控**，因为
outfit 的 mask 会把「半人马」里的「人马」吃掉，分类也会把「有尾巴的魅魔」判成半兽。

纪律：判不出就不给（置信度低于 INJECT_MIN 返回空）；只写结构，不写器官名词、
不写动作；先 mask 语素复合词，再要求身份锚点或身体信号，宁可少说不可误判。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from . import outfit

KIND_HUMAN = "human"
KIND_PONY = "pony"
KIND_BEAST_EAR = "beast_ear"
KIND_BEAST = "beast"
KIND_SPIRIT = "spirit"
KIND_MACHINE = "machine"

#: 低于这个置信度就不注入（判不出当人类处理，宁缺勿错）。
INJECT_MIN = 0.65

#: mask 表 = outfit 那份（**去掉「人马」**，否则「半人马」会被挖掉）+ 额外复合词。
_EXTRA_MASK: Tuple[str, ...] = (
    "小马哥", "小马甲", "小马扎", "小马仔",
    "独角兽公司", "独角兽企业", "独角戏",
    "触手可及", "触手可得", "触手生春", "章鱼烧", "章鱼小丸子", "八爪鱼",
    "翅膀硬了", "插翅难飞", "如虎添翼", "翼装飞行",
    "尾巴骨", "夹着尾巴", "狐狸尾巴", "露马脚",
    "鳞次栉比", "鱼鳞病", "鳞状",
    "角落", "角色", "角度", "三角形", "口角", "号角", "牛角尖", "勾心斗角",
    "马赛克", "马路", "马上", "骑马", "木马", "码头", "马虎", "马屁", "马甲",
    "马后炮", "马大哈", "马不停蹄", "万马奔腾", "马到成功", "单枪匹马",
    "蛛丝马迹", "招兵买马", "马马虎虎", "人马座",
    "龙卷风", "龙飞凤舞", "恐龙园", "恐龙蛋", "恐龙头骨", "恐龙化石",
    "人鱼线", "鱼尾纹", "鱼龙混杂",
)
_MASK_WORDS: Tuple[str, ...] = tuple(
    word for word in outfit._MASK_WORDS if word != "人马"
) + _EXTRA_MASK

#: 强信号：出现即可判定「小马系」。
_PONY_STRONG: Tuple[str, ...] = (
    "小马宝莉", "彩虹小马", "小马国", "my little pony", "mlp",
)

#: 小马词：需要身份锚点、或与身体信号一起出现才算。
_PONY_WORDS: Tuple[str, ...] = (
    "小马", "马娘", "天马", "飞马", "陆马", "独角兽", "天角兽", "夜骐",
    "幻形灵", "麒麟", "斑马", "马驹", "pony",
)

#: 身体信号：只属于小马系（或极强地指向它）。
_PONY_BODY: Tuple[str, ...] = (
    "四蹄", "鬃毛", "可爱标志", "cutie mark", "马蹄", "马耳", "马尾巴",
    "马身", "马背", "蹄子",
)

#: 身份锚点。
_ANCHOR_RE = re.compile(
    r"(?:是|为|乃|变成|化身|身为|属于|作为一个|种族|物种|原型|本体|化形|设定|"
    r"一只|一头|一条|一匹|一尾|一个)"
    r"[^，。！？；\n]{0,10}$"
)

#: 身体结构（可叠加在任意物种上）。顺序 = 输出顺序。
_TRAITS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("角", ("独角", "龙角", "兽角", "犄角", "尖角")),
    ("翅膀", ("翅膀", "羽翼", "蝠翼", "翼膜", "双翼")),
    ("触手", ("触手", "触须", "腕足", "触肢", "藤蔓", "藤条")),
    ("多臂", ("多臂", "四臂", "六臂", "四条手臂", "六条手臂", "四只手")),
    ("尾巴", ("尾巴", "兽尾", "尾羽", "鱼尾")),
    ("兽耳", ("兽耳", "猫耳", "狐耳", "狼耳", "兔耳", "犬耳", "熊耳", "马耳")),
    ("鳞片", ("鳞片", "鳞甲", "鱼鳞")),
)

#: 明确物种（在身份区里出现就判，优先于粗分类）。
_SPECIFIC: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("半人马", "半人马", ("四蹄",)),
    ("人鱼", "人鱼", ("尾巴",)),
    ("鲛人", "鲛人", ("尾巴",)),
    ("魅魔", "魅魔", ("尾巴",)),
    ("梦魔", "梦魔", ()),
    ("天使", "天使", ("翅膀",)),
    ("恶魔", "恶魔", ("角", "尾巴")),
    ("吸血鬼", "吸血鬼", ()),
    ("精灵", "精灵", ("兽耳",)),
    ("妖精", "妖精", ("翅膀",)),
    ("龙", "龙", ("角", "鳞片")),
    ("麒麟", "麒麟", ("角", "鳞片")),
)

#: 兽耳类（明确词，不用裸「尾巴」——否则魅魔/龙都会被抢先判成半兽）。
_BEAST_EAR_WORDS = (
    "兽耳", "猫耳", "狐耳", "狼耳", "兔耳", "犬耳", "熊耳", "马耳",
    "猫娘", "狐娘", "狼女", "兽娘", "兽人娘", "兽尾", "毛茸茸",
)

#: 兽形动物词（身份区）。
_ANIMALS: Tuple[str, ...] = (
    "马", "狼", "狐", "猫", "犬", "兔", "龙", "蛇", "鹿", "虎", "熊", "豹",
    "鹰", "狮", "象", "鲸",
)

_MACHINE_WORDS = ("机械", "仿生", "机娘", "机器人", "人偶", "机甲")


def _mask(text: str) -> str:
    """在匹配副本上把复合词挖成等长方块（原文不动）。"""
    out = text
    for word in _MASK_WORDS:
        if word in out:
            out = out.replace(word, "\u25a1" * len(word))
    return out


def _zone(masked: str) -> str:
    """身份区：前 600 字 + 含身份关键词的行（与 outfit 同口径）。"""
    return outfit._identity_zone(masked)


def _anchored(text: str, word: str) -> bool:
    start = 0
    while True:
        idx = text.find(word, start)
        if idx < 0:
            return False
        if _ANCHOR_RE.search(text[max(0, idx - 14):idx]):
            return True
        start = idx + len(word)


def _pony_conf(masked: str) -> Tuple[float, List[str]]:
    low = masked.lower()
    hits: List[str] = []
    conf = 0.0
    for word in _PONY_STRONG:
        if word in low:
            conf = max(conf, 0.95)
            hits.append(word)
    #: 「物种名」本身就是身份声明（独角兽/天角兽/陆马/夜骐…），不需要系动词锚点；
    #: 「小马 / 马娘 / pony」这类才需要锚点或身体信号，否则「喜欢小马」也会命中。
    species_names = ("独角兽", "天角兽", "陆马", "夜骐", "幻形灵", "麒麟", "斑马", "马驹")
    for word in _PONY_WORDS:
        if word in masked:
            hits.append(word)
            if word in species_names:
                conf = max(conf, 0.82)
            else:
                conf = max(conf, 0.88 if _anchored(masked, word) else 0.55)
    body_hits = [w for w in _PONY_BODY if _hit(masked, w)]
    if body_hits:
        hits.extend(body_hits)
        if "可爱标志" in body_hits or "cutie mark" in low:
            conf = max(conf, 0.85)
        conf = max(conf, 0.82 if len(body_hits) >= 2 else 0.62)
    if any(w in masked for w in _PONY_WORDS) and body_hits:
        conf = max(conf, 0.92)
    return conf, hits


#: 否定词：命中词**前面**几个字里有它就不算数（「没有翅膀」的陆马不能判成天马）。
_NEG_WORDS = ("没有", "没", "无", "不带", "不长", "不算")
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


#: 裸「角」只有在「有/长着/带着/一对」这类领属语境里才算，避免「角度/角色」误判。
_POSSESS_HORN_RE = re.compile(
    r"(?:有|长着|带着|一对|两只|头上|额前)[^，。！？；\n]{0,4}(?:角|犄角)"
)


def _traits(masked: str) -> List[str]:
    found: List[str] = []
    for label, words in _TRAITS:
        for word in words:
            if _hit(masked, word):
                found.append(label)
                break
    if "角" not in found and _POSSESS_HORN_RE.search(masked):
        found.append("角")
    return found


def _specific(masked: str, zone: str) -> Tuple[str, List[str]]:
    """明确物种：label + 附加结构。"""
    for word, label, extra in _SPECIFIC:
        if word in zone:
            return label, list(extra)
    return "", []


#: 结构声明：人设里常写「结构：有翅膀，绝对无独角」——这是**最可信的一句**，
#: 优先于正文里其他地方提到的结构（那些可能是在描述别人或规则示例）。
_STRUCT_RE = re.compile(r"(?:结构|构造|身体构造)[^。\n]{0,140}")


def _structure_segment(masked: str) -> str:
    try:
        return "".join(_STRUCT_RE.findall(masked))
    except Exception:
        return ""


def _pony_label(traits: List[str], masked: str = "") -> str:
    # ① 种族声明最可信：「种族：独角兽」不能被正文里别处的「天角兽」盖过去。
    declared = re.search(r"(?:种族|物种)\s*[:：]\s*([\u4e00-\u9fa5]{1,6})", masked)
    if declared:
        name = declared.group(1)
        for word in ("天角兽", "独角兽", "夜骐", "幻形灵", "陆马", "天马", "飞马", "麒麟", "小马"):
            if name.startswith(word):
                return word
    # ② 明确物种词，其次按结构推
    for word, label in (
        ("天角兽", "天角兽"), ("独角兽", "独角兽"), ("夜骐", "夜骐"),
        ("幻形灵", "幻形灵"), ("陆马", "陆马"), ("麒麟", "麒麟"),
        ("天马", "天马"), ("飞马", "天马"),
    ):
        if word in masked:
            return label
    if "角" in traits and "翅膀" in traits:
        return "天角兽"
    if "角" in traits:
        return "独角兽"
    if "翅膀" in traits:
        return "天马"
    return "小马"


def _animal_label(zone: str) -> str:
    for animal in _ANIMALS:
        if animal in zone:
            return animal + "（兽形）"
    return "兽形"


def detect(persona: str, *, override: str = "") -> Dict[str, Any]:
    """从人设正文（或面板覆盖值）判物种与身体结构。

    返回 {kind, label, traits, confidence, terms, source}；
    判不出时 kind=human, confidence=0.0, traits=[]（调用方按「不注入」处理）。
    """
    body = str(override or "").strip()
    source = "override" if body else "persona"
    if not body:
        body = str(persona or "").strip()
    if not body:
        return {"kind": KIND_HUMAN, "label": "", "traits": [], "confidence": 0.0,
                "terms": [], "source": ""}

    masked = _mask(body if source == "override" else body.lower())
    zone = _zone(masked)
    traits = _traits(masked)

    pony_conf, pony_terms = _pony_conf(masked)
    if source == "override" and pony_conf > 0:
        # 面板覆盖值只在她明确写了小马系词时按 1.0 算；
        # 写「半人马+触手」不能被强行算成小马。
        pony_conf = 1.0

    # 明确物种（半人马/人鱼/魅魔/龙…）优先
    spec_label, spec_traits = _specific(masked, zone)
    for item in spec_traits:
        if item not in traits:
            traits.append(item)

    if pony_conf >= 0.65:
        kind = KIND_PONY
        confidence = pony_conf
        terms = pony_terms
        pony_traits: List[str] = []
        # 物种词本身隐含的结构：天角兽=角+翅膀，独角兽=角，天马/飞马/夜骐=翅膀
        implied: List[str] = []
        if "天角兽" in masked:
            implied = ["角", "翅膀"]
        elif "独角兽" in masked:
            implied = ["角"]
        elif any(w in masked for w in ("天马", "飞马", "夜骐")):
            implied = ["翅膀"]
        elif "幻形灵" in masked:
            implied = ["翅膀"]
        elif "陆马" in masked:
            implied = ["四蹄"]
        elif "麒麟" in masked:
            implied = ["角"]
        for key in ("角", "翅膀"):
            if key in traits or key in implied:
                pony_traits.append(key)
        if any(_hit(masked, w) for w in ("四蹄", "蹄子", "马蹄")) or "四蹄" in implied:
            pony_traits.append("四蹄")
        if _hit(masked, "鬃毛"):
            pony_traits.append("鬃毛")
        if "可爱标志" in masked or "cutie mark" in masked.lower():
            pony_traits.append("可爱标志")
        for key in ("触手", "尾巴"):
            if key in traits:
                pony_traits.append(key)
        # 结构声明优先：「绝对无独角」不能被别处的「双龙角」盖过去。
        seg = _structure_segment(masked)
        if seg:
            if re.search(r"无独角|没有独角|无角", seg):
                pony_traits = [t for t in pony_traits if t != "角"]
            elif re.search(r"独角|龙角|犄角", seg) and "角" not in pony_traits:
                pony_traits.append("角")
            if re.search(r"无翅膀|没有翅膀|无翼", seg):
                pony_traits = [t for t in pony_traits if t != "翅膀"]
            elif re.search(r"翅膀|双翼|羽翼", seg) and "翅膀" not in pony_traits:
                pony_traits.append("翅膀")
        traits = pony_traits[:4]
        label = _pony_label(traits, masked)
    elif spec_label:
        kind = KIND_SPIRIT if spec_label in (
            "魅魔", "梦魔", "天使", "恶魔", "吸血鬼", "精灵", "妖精"
        ) else KIND_BEAST
        confidence = 0.85
        terms = [spec_label]
        label = spec_label
    elif any(w in zone for w in _MACHINE_WORDS):
        kind = KIND_MACHINE
        confidence = 0.75
        terms = ["machine"]
        label = "机械身体"
    elif any(w in zone for w in _BEAST_EAR_WORDS):
        kind = KIND_BEAST_EAR
        confidence = 0.75
        terms = ["beast_ear"]
        label = "半兽人形"
    elif outfit._IDENTITY_RE.search(masked) or any(w in zone for w in outfit._BEAST_STRONG):
        kind = KIND_BEAST
        confidence = 0.75
        terms = ["beast"]
        label = _animal_label(zone)
    else:
        kind = KIND_HUMAN
        confidence = 0.0
        terms = []
        if any(t in traits for t in ("触手", "多臂", "翅膀", "尾巴", "鳞片", "兽耳")):
            confidence = 0.7
            terms = traits[:]
        label = "人类"

    if source == "override" and confidence > 0:
        # 面板覆盖 = 用户明确指定，置信度拉满（但判不出仍然不注入）。
        confidence = 1.0

    return {
        "kind": kind,
        "label": label,
        "traits": traits,
        "confidence": round(confidence, 2),
        "terms": terms[:6],
        "source": source,
    }


def body_line(profile: Dict[str, Any], *, max_chars: int = 34) -> str:
    """把档案压成一句事实：小马（四蹄、鬃毛、可爱标志）。空串 = 不注入。"""
    if not isinstance(profile, dict):
        return ""
    if float(profile.get("confidence") or 0.0) < INJECT_MIN:
        return ""
    kind = str(profile.get("kind") or "")
    traits = [str(t) for t in (profile.get("traits") or []) if str(t).strip()]
    label = str(profile.get("label") or "").strip()
    if kind == KIND_HUMAN and not traits:
        return ""
    if not label:
        return ""
    text = label + ("（" + "、".join(traits[:3]) + "）" if traits else "")
    return text[:max_chars]
