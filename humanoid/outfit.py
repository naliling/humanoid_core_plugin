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

from typing import Optional

# ── 种类：粗分，够挑衣服就行 ──
KIND_HUMAN = "human"          # 人类
KIND_BEAST_EAR = "beast_ear"  # 兽耳 / 半兽：人形 + 兽征，照常穿衣服
KIND_BEAST = "beast"          # 兽 / 拟人动物：多为兽形，通常不穿衣服
KIND_SPIRIT = "spirit"        # 精灵 / 妖 / 鬼 / 神魔 / 天使 / 吸血鬼
KIND_MACHINE = "machine"      # 机械 / 仿生 / AI / 人偶

# 识别的关键词表。顺序 = 优先级：先判「兽形」（不穿衣），再判人形非人（穿衣）。
# 只收**强信号**——拿不准的一律走 human（人设非空）或空（人设空），宁可少判不可错判。
_KIND_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (KIND_MACHINE, (
        "机器人", "机械", "仿生", "人工智能", "人偶", "傀儡", "玩偶", "机甲",
        "android", "robot", "cyborg",
    )),
    (KIND_SPIRIT, (
        "精灵", "妖精", "恶魔", "天使", "幽灵", "鬼魂", "吸血鬼", "神仙", "仙人",
        "魔女", "女巫", "巫女", "神女", "龙女", "狐仙", "妖怪", "仙灵", "梦魔", "魅魔",
    )),
    (KIND_BEAST, (
        "小马", "马娘", "独角兽", "兽人", "狼人", "狐狸精", "九尾", "人鱼",
        "龙", "凤凰", "麒麟", "神兽", "猫妖", "犬妖", "鹰身", "狮鹫",
    )),
    (KIND_BEAST_EAR, (
        "兽耳", "猫耳", "狐耳", "狼耳", "兔耳", "犬耳", "熊耳", "耳朵", "兽尾",
        "尾巴", "毛茸茸", "猫娘", "狐娘", "狼女", "半兽", "兽娘", "兽人娘",
    )),
)

# ── 内置基础穿着（按种类）──
#
# 只给**一句够用的默认**，不写细（细节交给人设和模型）。手写预设（配置）会盖过它。
# 注意 beast 类是「不穿衣服」——那也是一种「符合」，不是「没有」。
_BASE: dict[str, str] = {
    KIND_HUMAN: "日常便服",
    KIND_BEAST_EAR: "日常便服",
    KIND_SPIRIT: "一身轻盈的长袍",
    KIND_MACHINE: "简洁的机能装",
    KIND_BEAST: "没穿衣服",
}

# 各场景下基础穿着怎么变（用「加在基础后面」的短语，避免和基础拼重）。
# 空串 = 不调整。beast 类本来就不穿，任何场景都不加。
_SCENE_SUFFIX: dict[str, str] = {
    "sleep": "（换成了睡衣）",
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


def detect_kind(name: str, persona: str) -> str:
    """从名字 + 人设正文里判她是什么种类。

    返回 KIND_*；**人设空时返回空串**（没有任何依据 → 不注入）。
    人设非空但没有任何强信号 → human（绝大多数角色是人）。
    """
    body = str(persona or "").strip()
    if not body:
        return ""
    text = f"{str(name or '')}\n{body}"[:4000].lower()
    for kind, words in _KIND_WORDS:
        for w in words:
            if w in text:
                return kind
    return KIND_HUMAN


def base_outfit(kind: str) -> str:
    """内置基础穿着；不认识的种类返回空串。"""
    return _BASE.get(str(kind or ""), "")


# 日程事件里出现这些词 = 在睡 / 在洗 / 出门。
_SLEEP_WORDS = ("睡", "入睡", "午睡", "小憩", "赖床", "打盹", "梦")
_BATH_WORDS = ("洗澡", "沐浴", "泡澡", "淋浴", "泡温泉", "冲凉", "沐浴")
_OUT_WORDS = ("出门", "外出", "上班", "上学", "通勤", "逛街", "买菜", "赴约",
              "旅行", "旅游", "出差", "跑步", "散步", "运动")


def scene_of(
    *,
    event: str = "",
    asleep: bool = False,
    temp_c: Optional[float] = None,
    discomfort: float = 0.0,
    cycle_phase: int = -1,
) -> str:
    """从当下的状态里推「场景」。返回 sleep / bath / out / cold / unwell / home / ""。"""
    ev = str(event or "")
    if asleep or any(w in ev for w in _SLEEP_WORDS):
        return "sleep"
    if any(w in ev for w in _BATH_WORDS):
        return "bath"
    if cycle_phase == 0 or float(discomfort or 0.0) >= 62.0:
        return "unwell"
    if any(w in ev for w in _OUT_WORDS):
        # 出门 + 冷 → 加外套；否则就是基础穿着出门
        if temp_c is not None and temp_c <= 10.0:
            return "cold"
        return "out"
    if temp_c is not None and temp_c <= 5.0:
        return "cold"
    return "home"


def resolve(
    *,
    preset: str = "",
    name: str = "",
    persona: str = "",
    scene: str = "",
    scene_variation: bool = True,
) -> str:
    """算出此刻的穿着。

    手写预设优先；没有就按识别到的种类取内置默认。两者都没有 → 返回空串（不注入）。
    返回空串还表示「不要给这一条」，调用方据此决定注入不注入。
    """
    text = str(preset or "").strip()
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
    if scene in _SCENE_BARE:
        bare = _SCENE_BARE[scene]
        # 手写预设/基础里已经写了这个状态的，不重复
        if bare and bare not in text:
            return bare
        return text
    suffix = _SCENE_SUFFIX.get(scene, "")
    if suffix and suffix not in text:
        return text + suffix
    return text


def line(text: str) -> str:
    """注入用的一句话。空串 → 空串（调用方据此不注入）。"""
    body = str(text or "").strip()
    if not body:
        return ""
    return f"她穿着：{body}"
