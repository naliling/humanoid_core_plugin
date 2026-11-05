"""日程素材 - 把「她今天可能做的事」摆到模型面前。

为什么要有这个模块：日程是滚动生成的，一次只决定「从现在起的这一段」。模型每次
手里只有：人设、上一段、今天已经过完的几段、身体读数、天气节日、四条范例。人设
通常只有一段话，范例又是刻意写成中性、不带身份的——于是模型能选的事其实很少，
只能反复挑那几件（睡、吃、收拾、看手机），或者把「中午」当成默认的睡觉点。

这不是模型笨，是素材不够：真人过日子有一整柜子「今天可能做的事」，模型手里只有
四条例子。这个模块补的就是那一柜子——按当下钟点、按人设里确实有的生活条件
（工作/宠物/照护/社交/兴趣）、按今天和昨天已经做过的事去重之后，抽一批递进去。

两条硬规矩：

1. **素材不是清单**。递进去的每一批都明写着「可以挑一件、可以改成更贴她的、
   也可以一件都不做」。素材只负责把「有这些可能」告诉它，选哪件、做多久，还是
   她自己（人设 + 身体 + 处境）的事。
2. **素材不能替她编身份**。带 needs 的素材只在人设里确实提到那种生活条件时才发，
   带 interest 的只在人设里确实提到那项兴趣时才发——递一条「给猫添粮」给没有猫
   的角色，就是在替她编一只猫（这条教训在范例池里已经踩过一次）。
"""

from __future__ import annotations

import random
from typing import Any, Iterable, Mapping, Sequence

# ----------------------------------------------------------------------
# 生活条件：人设正文里出现这些词，才认为她确实有工作/宠物/要照护的人/社交
# ----------------------------------------------------------------------
# 这张表原来在 schedule.py 里（只用来过滤范例）。素材也要用同一套判据，所以挪到
# 这里，schedule.py 反过来 import 它——两处各写一份迟早会走偏（这个项目的历史就是
# 「同一个毛病换个地方回来」）。
CAPABILITY_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "job",
        (
            "工作", "上班", "公司", "职业", "同事", "老板", "领导", "客户", "开会",
            "项目", "店里", "开店", "老师", "医生", "护士", "程序", "工程师", "学生",
            "上学", "学校", "学业", "自由职业", "画师", "写手", "店员", "服务", "创业",
            "兼职", "实习", "打工",
        ),
    ),
    ("pet", ("猫", "狗", "宠物", "仓鼠", "鹦鹉", "兔子", "养了", "养着")),
    (
        "care",
        (
            "孩子", "女儿", "儿子", "家人", "妈妈", "爸爸", "爸妈", "老公", "老婆",
            "丈夫", "妻子", "老人", "妹妹", "弟弟", "姐姐", "哥哥", "家庭", "照护",
        ),
    ),
    ("social", ("朋友", "同学", "闺蜜", "室友", "邻居", "同事", "社交", "认识的人")),
)

# 兴趣：人设里提到哪一类，才发哪一类的素材。没有依据就不发——兴趣比家务更容易
# 变成「替她编一个人」。
INTEREST_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("draw", ("画", "美术", "涂鸦", "素描", "手绘", "设计")),
    ("music", ("音乐", "歌", "吉他", "钢琴", "乐器", "唱", "乐队")),
    ("read", ("书", "阅读", "小说", "看文", "读书")),
    ("game", ("游戏", "打本", "电竞", "主机", "手游")),
    ("sport", ("跑", "健身", "游泳", "瑜伽", "爬山", "球", "运动", "骑行")),
    ("craft", ("手工", "编织", "缝", "手作", "拼装", "木工")),
    ("cook", ("做饭", "烘焙", "下厨", "甜点", "料理")),
    ("plant", ("花", "植物", "多肉", "盆栽", "绿植", "种")),
    ("film", ("电影", "剧", "动漫", "番", "纪录片")),
    ("write", ("写作", "写文", "码字", "日记", "写东西")),
    ("photo", ("摄影", "拍照", "相机", "镜头")),
)

# 时段：按小时切。素材只在「这个时候真做得出这件事」的时段里发——半夜递一条
# 「去楼下超市买菜」等于告诉模型日程可以随便写。
SLOT_NAMES = ("凌晨", "早上", "上午", "中午", "下午", "傍晚", "夜里")
_SLOT_RANGES = (
    (0, 5, "凌晨"),
    (5, 9, "早上"),
    (9, 12, "上午"),
    (12, 14, "中午"),
    (14, 18, "下午"),
    (18, 21, "傍晚"),
    (21, 24, "夜里"),
)
_DAY = ("早上", "上午", "中午", "下午", "傍晚")
_WAKING = ("早上", "上午", "中午", "下午", "傍晚", "夜里")


def slot_name(minute: int) -> str:
    """这个钟点属于哪个时段（坏值当凌晨）。"""
    try:
        hour = int(minute) // 60 % 24
    except (TypeError, ValueError):
        return "凌晨"
    for start, end, name in _SLOT_RANGES:
        if start <= hour < end:
            return name
    return "凌晨"


def capabilities(text: str) -> set[str]:
    """从「她是谁」的原文里认出她确实有的生活条件（工作/宠物/照护/社交）。"""
    body = str(text or "")
    return {name for name, words in CAPABILITY_WORDS if any(word in body for word in words)}


def interests_in(text: str) -> set[str]:
    """从人设原文里认出她确实有的兴趣。"""
    body = str(text or "")
    return {name for name, words in INTEREST_WORDS if any(word in body for word in words)}


def _m(
    event: str,
    location: str,
    lo: int,
    hi: int,
    slots: Sequence[str],
    topic: str,
    band: str,
    needs: Sequence[str] = (),
    interest: str = "",
    wants: bool = False,
) -> dict[str, Any]:
    """一条素材。topic 是去重用的题眼：今天/昨天做过含这个词的事，就不再发它。"""
    return {
        "event": event,
        "location": location,
        "minutes": (int(lo), int(hi)),
        "slots": tuple(slots),
        "topic": topic,
        "band": band,
        "needs": tuple(needs),
        "interest": interest,
        "wants": bool(wants),
    }


# ----------------------------------------------------------------------
# 素材池
# ----------------------------------------------------------------------
# 分档只为抽样时均衡：每个时段都该有「该干的活」「自己的事」「歇着」三种可能，
# 不能一整批全是家务（那样她的一天就只剩家务），也不能全是躺平。
MATERIALS: tuple[dict[str, Any], ...] = (
    # ---- 生活：谁过日子都躲不开的那些 ----
    _m("把攒了两天的衣服洗了，晾到阳台", "家里", 25, 45, _WAKING, "洗衣", "life"),
    _m("收拾厨房，把台面和水槽刷了一遍", "厨房", 20, 35, _WAKING, "厨房", "life"),
    _m("去楼下超市把缺的东西补上", "超市", 35, 60, _DAY, "买菜", "life"),
    _m("给自己弄点吃的，吃完把锅洗了", "厨房", 25, 50, ("早上", "中午", "傍晚"), "做饭", "life"),
    _m("把冰箱里过期的东西清出来", "厨房", 15, 25, _WAKING, "冰箱", "life"),
    _m("扫地拖地，顺手把桌子擦了", "家里", 20, 35, _WAKING, "打扫", "life"),
    _m("洗了个澡，把头发吹干，换了身衣服", "浴室", 25, 40, _WAKING, "洗澡", "life"),
    _m("整理衣柜，把不穿的收起来", "卧室", 30, 50, _DAY, "衣柜", "life"),
    _m("下楼把快递取上来，纸箱折好", "楼下", 10, 20, _DAY, "快递", "life"),
    _m("把手机里没用的截图和照片清了一遍", "家里", 15, 30, _WAKING, "手机", "life"),
    _m("把床单被套换下来洗了", "卧室", 30, 50, ("早上", "上午", "下午"), "床单", "life"),
    _m("烧了壶水，把杯子洗了，坐下来慢慢喝完", "厨房", 10, 20, _WAKING, "喝水", "life"),
    # ---- 兴趣：只在人设里确实提到时发 ----
    _m("摊开本子画了一会儿，画到一半卡住了", "书桌前", 40, 90, _WAKING, "画画", "interest", interest="draw"),
    _m("戴上耳机听歌，跟着哼了几句", "家里", 20, 40, _WAKING, "听歌", "interest", interest="music"),
    _m("窝在沙发上看书，看到一段停下来想了会儿", "客厅", 40, 90, _WAKING, "看书", "interest", interest="read"),
    _m("开了一局游戏，输了又开一局", "书桌前", 30, 90, _WAKING, "游戏", "interest", interest="game"),
    _m("出去跑了一圈，回来出了一身汗", "外面", 40, 80, ("早上", "傍晚", "夜里"), "跑步", "interest", interest="sport"),
    _m("照着食谱试了一个新做法，厨房弄乱了", "厨房", 50, 90, ("中午", "傍晚"), "下厨", "interest", interest="cook"),
    _m("给阳台的花浇水，把枯叶剪掉", "阳台", 15, 30, _DAY, "浇花", "interest", interest="plant"),
    _m("找了一部片子看，看完还坐着没动", "客厅", 90, 150, ("下午", "傍晚", "夜里"), "看片", "interest", interest="film"),
    _m("写了一会儿东西，改了又删，最后留下两行", "书桌前", 40, 90, _WAKING, "写东西", "interest", interest="write"),
    _m("带着相机出门转了一圈，拍了几张", "外面", 60, 120, ("上午", "下午", "傍晚"), "拍照", "interest", interest="photo"),
    _m("把手边的东西拆了又装回去", "书桌前", 40, 80, _WAKING, "手工", "interest", interest="craft"),
    # ---- 责任：工作 / 照护 / 宠物，同样只在人设有依据时发 ----
    _m("处理工作上的事，回了几封邮件", "书桌前", 40, 90, _WAKING, "工作", "duty", needs=("job",)),
    _m("把手上的活往前推了一段，中间歇了一次", "书桌前", 60, 120, _DAY, "干活", "duty", needs=("job",)),
    _m("开了个会，会后又对了一遍细节", "书桌前", 45, 90, ("上午", "下午"), "开会", "duty", needs=("job",)),
    _m("给家里打了个电话，说了会儿近况", "家里", 15, 30, _WAKING, "电话", "duty", needs=("care",)),
    _m("家里要照看的事忙了一阵才坐下", "家里", 60, 120, _DAY, "照看", "duty", needs=("care",)),
    _m("给猫添了粮，把水碗洗了", "厨房", 10, 20, _WAKING, "喂猫", "duty", needs=("pet",)),
    _m("带狗下楼转了一圈，回来给它擦了脚", "楼下", 25, 45, _WAKING, "遛狗", "duty", needs=("pet",)),
    # ---- 社交：只在人设里提到有朋友/同学/室友/邻居时发 ----
    _m("跟朋友在楼下坐了会儿，聊到忘了时间", "楼下", 60, 110, ("下午", "傍晚", "夜里"), "朋友", "social", needs=("social",)),
    _m("回朋友的消息，聊着聊着聊久了", "家里", 20, 45, _WAKING, "回消息", "social", needs=("social",)),
    _m("朋友顺路过来，在门口说了会儿话", "门口", 15, 30, _WAKING, "串门", "social", needs=("social",)),
    _m("约了人一起吃饭，回来晚了一点", "外面", 90, 150, ("傍晚", "夜里"), "约饭", "social", needs=("social",)),
    # ---- 歇着：什么都不干也是一种状态（注意：这里没有「睡一觉」）----
    _m("在沙发上躺着刷手机，什么正事也没做", "客厅", 20, 50, _WAKING, "刷手机", "rest", wants=True),
    _m("什么也不干，坐着发了会儿呆", "客厅", 15, 40, _WAKING, "发呆", "rest", wants=True),
    _m("靠在窗边晒太阳，眼睛都眯起来了", "窗边", 20, 40, ("早上", "上午", "下午"), "晒太阳", "rest", wants=True),
    _m("泡了杯东西，坐着慢慢喝完", "客厅", 20, 35, _WAKING, "泡茶", "rest", wants=True),
    _m("把窗户打开，站在阳台上吹了会儿风", "阳台", 10, 20, _WAKING, "吹风", "rest", wants=True),
    _m("把要做的事往后推了推，先缓了一会儿", "客厅", 20, 40, _WAKING, "拖延", "rest", wants=True),
    # ---- 偶发：不常发生，但发生了就是一天里的一段 ----
    _m("在小区里走了一圈，看了看楼下那家店", "小区", 15, 30, _DAY, "散步", "rare"),
    _m("家里有个东西坏了，找工具弄了半天", "家里", 30, 60, _WAKING, "修理", "rare"),
    _m("下雨了，在门口等了一会儿才回来", "门口", 10, 25, _DAY, "下雨", "rare"),
    _m("邻居敲门说了点事，站在门口聊了几句", "门口", 10, 20, _WAKING, "邻居", "rare", needs=("social",)),
    _m("翻出以前的照片看了一会儿", "家里", 20, 40, _WAKING, "旧照片", "rare", wants=True),
    _m("临时想起一件事，翻箱倒柜找了半天", "家里", 20, 45, _WAKING, "找东西", "rare"),
    # ---- 夜里醒着（凌晨）：睡了半截又醒、或者干脆还没睡的时候 ----
    # 这一档原来没有：凌晨整池是空的，于是那个钟点一段素材都发不出来，
    # 模型又只剩「接着睡」一个选项。
    _m("起来倒了杯水，在窗边站了会儿", "窗边", 10, 25, ("凌晨", "夜里"), "起夜", "rest", wants=True),
    _m("在客厅坐着，翻了会儿手机", "客厅", 15, 40, ("凌晨", "夜里"), "夜里刷手机", "life"),
)

# 每个档抽几条：均衡比总量重要——一整批全是家务，她这一天就只剩家务。
BAND_QUOTA: tuple[tuple[str, int], ...] = (
    ("life", 3),
    ("interest", 2),
    ("duty", 1),
    ("social", 1),
    ("rest", 2),
    ("rare", 1),
)
DEFAULT_COUNT = 10


def _topic_used(topic: str, recent: Iterable[str]) -> bool:
    """今天/昨天是不是已经做过这件事（按题眼比，不看整句）。"""
    if not topic:
        return False
    return any(topic in str(text or "") for text in recent)


def _allowed(item: dict[str, Any], caps: set[str], interests: set[str]) -> bool:
    if any(need not in caps for need in item.get("needs") or ()):
        return False
    interest = str(item.get("interest") or "")
    if interest and interest not in interests:
        return False
    return True


def materials_for(
    seed: list[Any] | None,
    persona_text: str = "",
    now_minute: int = 720,
    recent_events: Iterable[str] = (),
    count: int = DEFAULT_COUNT,
    extra: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """抽一批「这个时候她可能做的事」：同一天稳定，不同天不同。

    recent_events 是今天已经过完的段 + 昨天的收尾：题眼出现过的事不再发——否则
    模型每天看到的都是同一批素材，「她的一天高度雷同」这条老毛病就换了个来源。

    extra 是这个角色专属的素材（role_materials 按人设生成的）：它们已经按人设筛过
    身份条件，进池后优先于内置条目——不然「她是谁」写进去的那些具体的事永远排在
    通用条目后面，等于没加。
    """
    caps = capabilities(persona_text)
    interests = interests_in(persona_text)
    slot = slot_name(now_minute)
    recent = [str(text or "") for text in recent_events or () if str(text or "").strip()]
    rnd = random.Random("|".join(str(x) for x in (seed or [])) + "|materials")

    def usable(item: Mapping[str, Any]) -> bool:
        return (
            slot in tuple(item.get("slots") or ())
            and not _topic_used(str(item.get("topic") or ""), recent)
        )

    role_pool = [dict(item) for item in extra if isinstance(item, Mapping) and usable(item)]
    builtin_pool = [
        item for item in MATERIALS
        if usable(item) and _allowed(item, caps, interests)
    ]
    rnd.shuffle(role_pool)
    rnd.shuffle(builtin_pool)
    pool = role_pool + builtin_pool
    by_band: dict[str, list[dict[str, Any]]] = {}
    for item in pool:
        by_band.setdefault(str(item["band"]), []).append(item)

    picked: list[dict[str, Any]] = []
    seen: set[int] = set()
    for band, quota in BAND_QUOTA:
        for item in by_band.get(band, [])[:quota]:
            picked.append(item)
            seen.add(id(item))
    if len(picked) < count:
        # 某一档被去重或人设条件筛空了：从剩下的里补，别让这批素材变单薄。
        # 补位也按档轮转（不能谁排在前面就补谁）：否则「歇着」这类条目多的档
        # 会一路补到占满一半，递出去的一批素材变成「她今天打算躺一天」。
        rest = [item for item in pool if id(item) not in seen]
        buckets: dict[str, list[dict[str, Any]]] = {}
        for item in rest:
            buckets.setdefault(str(item["band"]), []).append(item)
        for items in buckets.values():
            rnd.shuffle(items)
        order = [band for band, _ in BAND_QUOTA if band in buckets]
        while len(picked) < count and order:
            alive: list[str] = []
            for band in order:
                bucket = buckets[band]
                if not bucket:
                    continue
                picked.append(bucket.pop(0))
                alive.append(band)
                if len(picked) >= count:
                    break
            order = alive
    rnd.shuffle(picked)
    return picked[: max(1, int(count))]


def material_minutes(item: dict[str, Any], seed: list[Any] | None = None) -> int:
    """这一条今天按多少分钟给（区间内取一个，同一天稳定）。"""
    lo, hi = item.get("minutes") or (15, 30)
    if int(hi) <= int(lo):
        return int(lo)
    rnd = random.Random(
        "|".join(str(x) for x in (seed or [])) + "|" + str(item.get("event") or "")
    )
    return rnd.randint(int(lo), int(hi))


def render_materials(items: Sequence[dict[str, Any]], seed: list[Any] | None = None) -> str:
    """把素材渲染成 prompt 里那一段（只给事件、地点、大致多久）。"""
    rows = []
    for item in items:
        minutes = material_minutes(item, seed)
        rows.append("  - " + str(item["event"]) + "（" + str(item["location"]) + "，约 " + str(minutes) + " 分钟）")
    return "\n".join(rows)
