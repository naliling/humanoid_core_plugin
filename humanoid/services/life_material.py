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

import hashlib
from typing import Any, Iterable, Mapping, Sequence

from ..opportunity import MATERIAL_TTL_SECONDS, anchor_key_for, make_card
from ..wording import pick

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
    sensory: str = "",
) -> dict[str, Any]:
    """一条素材。topic 是去重用的题眼：今天/昨天做过含这个词的事，就不再发它。

    `sensory`（v3.1.9）是这一条的**画面细节**，六到十四个字，只能影响语气、不能直接
    念出来：「水汽还没散」「手上有股洗衣液味」「窗外有车过去」。以前卡片只有
    「地点 + 大概 N 分钟」，模型拿到的是一张工时表，写出来的话也像在报进度。给一句
    手上/身上/窗外的感觉，它才有东西可依着说。没写的条目按 `band` 用模板池兜底。
    """
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
        "sensory": sensory,
    }


# ----------------------------------------------------------------------
# 素材池
# ----------------------------------------------------------------------
# 分档只为抽样时均衡：每个时段都该有「该干的活」「自己的事」「歇着」三种可能，
# 不能一整批全是家务（那样她的一天就只剩家务），也不能全是躺平。
MATERIALS: tuple[dict[str, Any], ...] = (
    # ---- 生活：谁过日子都躲不开的那些 ----
    _m("把攒了两天的衣服洗了，晾到阳台", "家里", 25, 45, _WAKING, "洗衣", "life", sensory="手上有股洗衣液味"),
    _m("收拾厨房，把台面和水槽刷了一遍", "厨房", 20, 35, _WAKING, "厨房", "life", sensory="水槽边还沾着水"),
    _m("去楼下超市把缺的东西补上", "超市", 35, 60, _DAY, "买菜", "life", sensory="袋子提手勒得手疼"),
    _m("给自己弄点吃的，吃完把锅洗了", "厨房", 25, 50, ("早上", "中午", "傍晚"), "做饭", "life", sensory="围裙上溅了点油"),
    _m("把冰箱里过期的东西清出来", "厨房", 15, 25, _WAKING, "冰箱", "life", sensory="手冻得有点发麻"),
    _m("扫地拖地，顺手把桌子擦了", "家里", 20, 35, _WAKING, "打扫", "life", sensory="膝盖跪得有点酸"),
    _m("洗了个澡，把头发吹干，换了身衣服", "浴室", 25, 40, _WAKING, "洗澡", "life", sensory="头发还湿着，贴在脖子上"),
    _m("整理衣柜，把不穿的收起来", "卧室", 30, 50, _DAY, "衣柜", "life", sensory="衣服上有股樟脑味"),
    _m("下楼把快递取上来，纸箱折好", "楼下", 10, 20, _DAY, "快递", "life", sensory="纸箱边角扎手"),
    _m("把手机里没用的截图和照片清了一遍", "家里", 15, 30, _WAKING, "手机", "life", sensory="手指划得有点发麻"),
    _m("把床单被套换下来洗了", "卧室", 30, 50, ("早上", "上午", "下午"), "床单", "life", sensory="床单刚套上，还带折痕"),
    _m("烧了壶水，把杯子洗了，坐下来慢慢喝完", "厨房", 10, 20, _WAKING, "喝水", "life", sensory="杯子还是温的"),
    _m("把阳台的花草搬出去晒了太阳，顺手浇了水", "阳台", 20, 40, _DAY, "花草", "life", sensory="手上有股泥土味"),
    _m("把换季的衣服翻出来，挑了几件能穿的", "卧室", 40, 70, _DAY, "换季", "life", sensory="袖口蹭到胳膊，有点痒"),
    _m("照着说明书把新买的小家电装好，试了试能用", "家里", 30, 60, _WAKING, "小家电", "life", sensory="手上还留着螺丝刀的凉"),
    _m("把书桌收拾出来，扔掉一袋没用的纸", "书桌前", 25, 45, _WAKING, "书桌", "life", sensory="桌上落了一层灰"),
    _m("去菜市场转了一圈，挑了点当季的菜", "菜市场", 35, 60, ("早上", "上午"), "菜市场", "life", sensory="鞋底沾了点泥"),
    _m("把家里的药箱翻了一遍，过期的挑出来", "家里", 20, 40, _DAY, "药箱", "life", sensory="药片碰在纸上响了一下"),
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
    _m("弹了会儿琴，把总弹错的那段练顺了", "客厅", 40, 80, _WAKING, "弹琴", "interest", interest="music"),
    _m("临摹了一张画，画完贴在了墙上", "书桌前", 60, 120, _DAY, "临摹", "interest", interest="draw"),
    _m("把攒着的剧看了两集，边看边记了两句台词", "客厅", 60, 120, ("下午", "傍晚", "夜里"), "看剧", "interest", interest="film"),
    _m("照着教程做了个手工，做得歪歪扭扭", "书桌前", 60, 120, _DAY, "手作", "interest", interest="craft"),
    _m("把旧照片扫进电脑，按年份分了文件夹", "书桌前", 45, 90, _DAY, "扫照片", "interest", interest="photo"),
    _m("读完一本书的最后一章，合上书坐了会儿", "客厅", 40, 90, _WAKING, "读完书", "interest", interest="read"),
    # ---- 责任：工作 / 照护 / 宠物，同样只在人设有依据时发 ----
    _m("处理工作上的事，回了几封邮件", "书桌前", 40, 90, _WAKING, "工作", "duty", needs=("job",)),
    _m("把手上的活往前推了一段，中间歇了一次", "书桌前", 60, 120, _DAY, "干活", "duty", needs=("job",)),
    _m("开了个会，会后又对了一遍细节", "书桌前", 45, 90, ("上午", "下午"), "开会", "duty", needs=("job",)),
    _m("给家里打了个电话，说了会儿近况", "家里", 15, 30, _WAKING, "电话", "duty", needs=("care",)),
    _m("家里要照看的事忙了一阵才坐下", "家里", 60, 120, _DAY, "照看", "duty", needs=("care",)),
    _m("给猫添了粮，把水碗洗了", "厨房", 10, 20, _WAKING, "喂猫", "duty", needs=("pet",)),
    _m("带狗下楼转了一圈，回来给它擦了脚", "楼下", 25, 45, _WAKING, "遛狗", "duty", needs=("pet",)),
    _m("把明天要用的材料整理好了，列了张单子", "书桌前", 30, 60, ("下午", "傍晚", "夜里"), "整理材料", "duty", needs=("job",)),
    _m("回了客户的消息，把报价改了一版", "书桌前", 30, 60, ("上午", "下午"), "客户", "duty", needs=("job",)),
    _m("去公司开了一下午的会，回来嗓子都哑了", "公司", 90, 180, ("上午", "下午"), "去公司", "duty", needs=("job",)),
    _m("给家里老人打了电话，问了问身体", "家里", 15, 30, _WAKING, "问身体", "duty", needs=("care",)),
    _m("带猫去宠物店剪了指甲，回来它躲了半天", "宠物店", 60, 120, _DAY, "剪指甲", "duty", needs=("pet",)),
    # ---- 社交：只在人设里提到有朋友/同学/室友/邻居时发 ----
    _m("跟朋友在楼下坐了会儿，聊到忘了时间", "楼下", 60, 110, ("下午", "傍晚", "夜里"), "朋友", "social", needs=("social",)),
    _m("回朋友的消息，聊着聊着聊久了", "家里", 20, 45, _WAKING, "回消息", "social", needs=("social",)),
    _m("朋友顺路过来，在门口说了会儿话", "门口", 15, 30, _WAKING, "串门", "social", needs=("social",)),
    _m("约了人一起吃饭，回来晚了一点", "外面", 90, 150, ("傍晚", "夜里"), "约饭", "social", needs=("social",)),
    _m("和朋友约在咖啡馆坐了一下午，聊了很多", "咖啡馆", 90, 150, ("下午", "傍晚"), "咖啡馆", "social", needs=("social",)),
    _m("室友做了饭，一起吃的时候聊了很久", "家里", 40, 80, ("中午", "傍晚", "夜里"), "一起吃", "social", needs=("social",)),
    _m("在群里聊起来，最后约了周末见面", "家里", 20, 45, _WAKING, "群里", "social", needs=("social",)),
    _m("帮邻居收了个快递，在门口说了会儿话", "门口", 10, 25, _WAKING, "帮邻居", "social", needs=("social",)),
    # ---- 歇着：什么都不干也是一种状态（注意：这里没有「睡一觉」）----
    _m("在沙发上躺着刷手机，什么正事也没做", "客厅", 20, 50, _WAKING, "刷手机", "rest", wants=True, sensory="屏幕的光照在脸上"),
    _m("什么也不干，坐着发了会儿呆", "客厅", 15, 40, _WAKING, "发呆", "rest", wants=True, sensory="盯着墙上一块影子"),
    _m("靠在窗边晒太阳，眼睛都眯起来了", "窗边", 20, 40, ("早上", "上午", "下午"), "晒太阳", "rest", wants=True, sensory="背上晒得发烫"),
    _m("泡了杯东西，坐着慢慢喝完", "客厅", 20, 35, _WAKING, "泡茶", "rest", wants=True, sensory="水汽还没散"),
    _m("把窗户打开，站在阳台上吹了会儿风", "阳台", 10, 20, _WAKING, "吹风", "rest", wants=True, sensory="窗帘被风掀起一角"),
    _m("把要做的事往后推了推，先缓了一会儿", "客厅", 20, 40, _WAKING, "拖延", "rest", wants=True, sensory="脚边的拖鞋歪着"),
    _m("靠在沙发上看窗外，一下午没挪地方", "客厅", 40, 90, ("下午", "傍晚"), "看窗外", "rest", wants=True, sensory="玻璃上有一点手印"),
    _m("给自己泡了壶茶，慢慢喝完才起身", "客厅", 30, 50, _WAKING, "泡壶茶", "rest", wants=True, sensory="杯壁烫得换了个手"),
    _m("躺在地毯上听了会儿歌，什么也没想", "家里", 30, 60, ("下午", "夜里"), "躺地毯", "rest", wants=True, sensory="地毯有点扎后背"),
    # ---- 偶发：不常发生，但发生了就是一天里的一段 ----
    _m("在小区里走了一圈，看了看楼下那家店", "小区", 15, 30, _DAY, "散步", "rare", sensory="鞋跟敲在地上的声音"),
    _m("家里有个东西坏了，找工具弄了半天", "家里", 30, 60, _WAKING, "修理", "rare", sensory="手上沾了点机油"),
    _m("下雨了，在门口等了一会儿才回来", "门口", 10, 25, _DAY, "下雨", "rare", sensory="袖口湿了半截"),
    _m("邻居敲门说了点事，站在门口聊了几句", "门口", 10, 20, _WAKING, "邻居", "rare", needs=("social",)),
    _m("翻出以前的照片看了一会儿", "家里", 20, 40, _WAKING, "旧照片", "rare", wants=True, sensory="照片边角卷起来了"),
    _m("临时想起一件事，翻箱倒柜找了半天", "家里", 20, 45, _WAKING, "找东西", "rare", sensory="翻得满手是灰"),
    _m("楼上装修吵得不行，干脆出门躲了半天", "外面", 60, 120, _DAY, "装修", "rare", sensory="耳朵里还嗡嗡的"),
    _m("在楼下碰到一只流浪猫，蹲下来喂了点东西", "楼下", 10, 25, _DAY, "流浪猫", "rare", sensory="手背被蹭了一下"),
    _m("整理旧物翻到一封旧信，看了两遍", "家里", 20, 40, _WAKING, "旧信", "rare", wants=True, sensory="纸有点发脆"),
    _m("小区突然停水，拎着桶下楼接了一趟", "楼下", 20, 40, _DAY, "停水", "rare", sensory="水桶提着勒手"),
    # ---- 夜里醒着（凌晨）：睡了半截又醒、或者干脆还没睡的时候 ----
    # 这一档原来没有：凌晨整池是空的，于是那个钟点一段素材都发不出来，
    # 模型又只剩「接着睡」一个选项。
    _m("起来倒了杯水，在窗边站了会儿", "窗边", 10, 25, ("凌晨", "夜里"), "起夜", "rest", wants=True, sensory="脚踩在地板上有点凉"),
    _m("在客厅坐着，翻了会儿手机", "客厅", 15, 40, ("凌晨", "夜里"), "夜里刷手机", "life", sensory="屋里只有屏幕亮着"),
    _m("半夜饿醒，起来煮了碗面吃完才回去", "厨房", 20, 35, ("凌晨", "夜里"), "半夜煮面", "life", sensory="锅里的热气扑到脸上"),
    _m("凌晨还醒着，在阳台站了会儿看天", "阳台", 10, 25, ("凌晨", "夜里"), "看天", "rest", wants=True, sensory="夜里风有点凉"),
    # ---- 通用（v3.1.9）：不挑人设、不挑身份，谁过日子都可能碰上 ----
    # 为什么加：默认池原来四十条，同一个人连着看两周就会开始重复；重复的由头会被
    # 模型说成「又是这件」，反而更假。这批补的是**时段覆盖**——凌晨到夜里每一档都
    # 有几条能落到手上的小事，且都带一句感官细节。
    _m("把门口的鞋摆整齐，顺手擦了鞋柜", "门口", 10, 20, _WAKING, "鞋柜", "life", sensory="鞋油味有点冲"),
    _m("把垃圾拎下楼，顺便看了眼信箱", "楼下", 5, 15, _WAKING, "倒垃圾", "life", sensory="手上还留着塑料袋的滑"),
    _m("把晾干的衣服收进来叠好", "阳台", 15, 25, _DAY, "收衣服", "life", sensory="衣服上还有太阳味"),
    _m("把地垫拿出去抖了抖", "门口", 5, 10, _DAY, "抖地垫", "life", sensory="灰尘在光里飘"),
    _m("切了点水果，装在盘子里慢慢吃", "厨房", 10, 20, _DAY, "吃水果", "life", sensory="手指上沾了点汁"),
    _m("煮了碗面，站着就吃完了", "厨房", 15, 25, ("中午", "夜里", "凌晨"), "吃面", "life", sensory="嘴里还烫着"),
    _m("倒了杯热水，捧在手里暖着", "家里", 5, 10, ("早上", "夜里", "凌晨"), "喝热水", "life", sensory="杯壁烫得手心发红"),
    _m("翻冰箱找吃的，最后拿了盒酸奶", "厨房", 5, 10, ("下午", "夜里"), "找吃的", "life", sensory="冰箱的凉气扑到脸上"),
    _m("把抽屉里的东西倒出来重新归位", "书桌前", 20, 40, _WAKING, "抽屉", "life", sensory="抽屉滑轨有点涩"),
    _m("把充电线、耳机线缠好收进盒子", "书桌前", 10, 20, _WAKING, "收线", "life", sensory="线缠得手指发紧"),
    _m("把手机充上电，把线理顺", "家里", 5, 10, _WAKING, "充电", "life", sensory="线头有点发烫"),
    _m("把没回的消息翻出来，一条条回", "家里", 15, 35, _WAKING, "清消息", "life", sensory="拇指按得有点酸"),
    _m("把相册往前翻，翻到很久以前", "家里", 15, 30, _WAKING, "翻相册", "rare", wants=True, sensory="屏幕看久了眼睛发涩"),
    _m("把明天要穿的衣服搭在椅背上", "卧室", 5, 10, ("夜里", "凌晨"), "准备衣服", "life", sensory="衣架碰在一起响了一声"),
    _m("把灯打开，屋里一下亮了", "家里", 5, 10, ("傍晚", "夜里"), "开灯", "life", sensory="灯亮起来那下有点晃眼"),
    _m("把肩膀转了转，脖子咔了一声", "家里", 5, 15, _WAKING, "拉伸", "rest", wants=True, sensory="脖子有点发僵"),
    _m("站在窗边伸了个懒腰，眼睛闭了会儿", "窗边", 5, 10, ("早上", "上午", "下午"), "伸懒腰", "rest", wants=True, sensory="胳膊举过头顶有点酸"),
    _m("把窗开了条缝，听了会儿楼下的动静", "窗边", 10, 25, _WAKING, "听动静", "rest", wants=True, sensory="风把窗帘吹起来一下"),
    _m("看了会儿楼下路过的人", "窗边", 10, 20, _DAY, "看路人", "rest", wants=True, sensory="玻璃有点凉"),
    _m("靠在椅背上闭眼歇了会儿", "书桌前", 10, 25, ("中午", "下午"), "歇一会儿", "rest", wants=True, sensory="后背贴着椅背有点热"),
    _m("刷牙的时候对着镜子发了会儿呆", "浴室", 5, 10, ("早上", "凌晨"), "刷牙", "rest", wants=True, sensory="牙膏味有点冲"),
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

# ----------------------------------------------------------------------
# 确定性轮换：同一天同一时段稳定，换一天换一批
# ----------------------------------------------------------------------
# 原来是 random.Random(seed) 洗牌：同一天稳定没问题，但「换一天换一批」靠的是洗牌
# 结果碰巧不同，而且时段不进 seed，上午和下午拿到的是同一批的相对顺序。改成按
# sha256 排序：排序键只由「角色 + 日期 + 时段 + 条目文本」决定，可复算、可测试，
# 没有随机数状态，进程重启也不会漂。
MATERIALS_USED_KEY = "materials_used"
MATERIALS_USED_LIMIT = 128
MATERIALS_USED_WINDOW_SECONDS = 7 * 24 * 3600.0


def _seed_text(seed: Sequence[Any] | None, slot: str) -> str:
    base = "|".join(str(x) for x in (seed or []))
    return base + "|materials|" + str(slot or "")


def _rank(seed_text: str, text: Any) -> str:
    body = seed_text + "|" + str(text or "")
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _ordered(
    items: Iterable[Mapping[str, Any]],
    seed_text: str,
    salt: str = "",
) -> list[dict[str, Any]]:
    """按 sha256 排一批素材：同 seed 同顺序，换日期/时段换顺序。"""
    rows = [dict(item) for item in items if isinstance(item, Mapping)]
    return sorted(rows, key=lambda item: _rank(seed_text + "|" + salt, item.get("event")))


def _used_at(row: Mapping[str, Any]) -> float:
    try:
        return float(row.get("used_at") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def used_keys(
    records: Any,
    now_ts: Any,
    *,
    window: float = MATERIALS_USED_WINDOW_SECONDS,
) -> set[str]:
    """materials_used → 窗口期内用过的键（card_id 与 anchor_key）。

    读侧必须 fail-open：没有记录、结构脏、时间戳不可解析，都当「没用过」——宁可
    再提一次，也不要因为一条烂记录把整批素材筛空。
    """
    keys: set[str] = set()
    if not isinstance(records, (list, tuple)):
        return keys
    try:
        now = float(now_ts)
    except (TypeError, ValueError):
        return keys
    for record in records:
        if not isinstance(record, Mapping):
            continue
        used_at = _used_at(record)
        if used_at <= 0.0:
            continue
        if now - used_at > float(window):
            continue
        for name in ("card_id", "anchor_key"):
            text = str(record.get(name) or "").strip()
            if text:
                keys.add(text)
    return keys


def remember_used(
    records: Any,
    card: Mapping[str, Any],
    now_ts: Any,
    *,
    limit: int = MATERIALS_USED_LIMIT,
) -> list[dict[str, Any]]:
    """把「这张卡用过了」记进 materials_used：同 card_id 覆盖，超上限丢最旧的。"""
    rows: list[dict[str, Any]] = []
    if isinstance(records, (list, tuple)):
        for record in records:
            if isinstance(record, Mapping):
                rows.append(dict(record))
    card_id = str(card.get("id") or "").strip()
    anchor_key = str(card.get("anchor_key") or "").strip()
    if not card_id and not anchor_key:
        return rows
    try:
        used_at = float(now_ts)
    except (TypeError, ValueError):
        used_at = 0.0
    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("card_id") or "").strip() or str(row.get("anchor_key") or "").strip()
        if not key:
            continue
        deduped[key] = row
    deduped[card_id or anchor_key] = {
        "card_id": card_id,
        "anchor_key": anchor_key,
        "used_at": used_at,
    }
    ordered = sorted(deduped.values(), key=_used_at)
    if int(limit) <= 0:
        return []
    return ordered[-int(limit):]


# ----------------------------------------------------------------------
# 绑日程时段：这一段是上班 / 午休 / 睡觉时，别递冲突的素材
# ----------------------------------------------------------------------
SEGMENT_SLEEP_WORDS = ("睡", "小憩", "打盹", "午觉", "补觉", "赖床")
SEGMENT_WORK_WORDS = (
    "上班", "工作", "加班", "开会", "会议", "值班", "上课", "出差", "客户",
    "项目", "赶稿", "写稿", "汇报", "店里",
)
SEGMENT_BREAK_WORDS = ("午休", "午间", "午饭", "吃饭", "用餐", "歇晌")

_SEGMENT_BANDS: dict[str, tuple[str, ...]] = {
    "sleep": ("rest",),
    "work": ("duty", "interest"),
}
_SEGMENT_NIGHT_SLOTS = ("凌晨", "夜里")


def segment_kind(text: Any, *, asleep: bool = False) -> str:
    """当前日程段属于哪一类：sleep / work / break / ""（认不出就不筛）。"""
    if asleep:
        return "sleep"
    body = str(text or "")
    if any(word in body for word in SEGMENT_SLEEP_WORDS):
        return "sleep"
    if any(word in body for word in SEGMENT_WORK_WORDS):
        return "work"
    if any(word in body for word in SEGMENT_BREAK_WORDS):
        return "break"
    return ""


def _segment_allows(item: Mapping[str, Any], kind: str) -> bool:
    """日程段和素材档冲突时丢掉：睡着时别递「去菜市场」，上班时别递「收拾衣柜」。"""
    if not kind:
        return True
    band = str(item.get("band") or "")
    if kind == "break":
        return band != "duty"
    allowed = _SEGMENT_BANDS.get(kind)
    if allowed is None:
        return True
    if band not in allowed:
        return False
    if kind == "sleep":
        return bool(set(_SEGMENT_NIGHT_SLOTS) & set(item.get("slots") or ()))
    return True


def _used_item(item: Mapping[str, Any], used: set[str]) -> bool:
    """这条素材的题眼是不是近 7 天已经用过（按 anchor_key 比）。"""
    if not used:
        return False
    topic = str(item.get("topic") or "").strip() or str(item.get("event") or "")
    return anchor_key_for("life", topic) in used


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
    *,
    used: Iterable[str] = (),
    segment: str = "",
    asleep: bool = False,
) -> list[dict[str, Any]]:
    """抽一批「这个时候她可能做的事」：同一天同一时段稳定，换一天换一批。

    recent_events 是今天已经过完的段 + 昨天的收尾：题眼出现过的事不再发——否则
    模型每天看到的都是同一批素材，「她的一天高度雷同」这条老毛病就换了个来源。

    extra 是这个角色专属的素材（role_materials 按人设生成的）：它们已经按人设筛过
    身份条件，进池后优先于内置条目——不然「她是谁」写进去的那些具体的事永远排在
    通用条目后面，等于没加。

    used 是近 7 天已经用过的键（card_id / anchor_key，见 used_keys）：命中就跳过，
    别每天把同一件事再推一遍。拿不到记录就传空——空集合等于不过滤（fail-open）。

    segment 是当前日程段的文本、asleep 是契约里的深睡标记：睡着时只留「夜里醒着」
    这类素材，上班时只留工作与自己的事，午休时把工作档拿掉。
    """
    caps = capabilities(persona_text)
    interests = interests_in(persona_text)
    slot = slot_name(now_minute)
    recent = [str(text or "") for text in recent_events or () if str(text or "").strip()]
    seed_text = _seed_text(seed, slot)
    kind = segment_kind(segment, asleep=asleep)
    used_set = {str(key).strip() for key in (used or ()) if str(key or "").strip()}

    def usable(item: Mapping[str, Any]) -> bool:
        return (
            slot in tuple(item.get("slots") or ())
            and not _topic_used(str(item.get("topic") or ""), recent)
            and _segment_allows(item, kind)
            and not _used_item(item, used_set)
        )

    role_pool = _ordered(
        (item for item in extra if isinstance(item, Mapping) and usable(item)),
        seed_text,
        "role",
    )
    builtin_pool = _ordered(
        (item for item in MATERIALS if usable(item) and _allowed(item, caps, interests)),
        seed_text,
        "builtin",
    )
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
        for band, items in buckets.items():
            buckets[band] = _ordered(items, seed_text, "back:" + band)
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
    picked = _ordered(picked, seed_text, "out")
    if not picked and kind == "work":
        # 上班段可能一条都不剩：人设里没有工作/兴趣条件时，duty 与 interest 两档
        # 整个被 _allowed 筛掉，时段再筛一次就是空批。这时宁可递一批不挑时段的
        # 素材，也别让她上班那几小时一张证据卡都没有。睡觉段不走这条兜底——
        # 睡着时递「去菜市场」比空着更糟。
        return materials_for(
            seed,
            persona_text,
            now_minute,
            recent_events,
            count,
            extra,
            used=used,
            segment="",
            asleep=False,
        )
    return picked[: max(1, int(count))]


def material_minutes(item: dict[str, Any], seed: list[Any] | None = None) -> int:
    """这一条今天按多少分钟给（区间内取一个，同一天稳定）。"""
    lo, hi = item.get("minutes") or (15, 30)
    lo, hi = int(lo), int(hi)
    if hi <= lo:
        return lo
    digest = hashlib.sha256(
        ("|".join(str(x) for x in (seed or [])) + "|" + str(item.get("event") or "")).encode("utf-8")
    ).hexdigest()
    return lo + int(digest[:16], 16) % (hi - lo + 1)


def render_materials(items: Sequence[dict[str, Any]], seed: list[Any] | None = None) -> str:
    """把素材渲染成 prompt 里那一段（只给事件、地点、大致多久）。"""
    rows = []
    for item in items:
        minutes = material_minutes(item, seed)
        rows.append("  - " + str(item["event"]) + "（" + str(item["location"]) + "，约 " + str(minutes) + " 分钟）")
    return "\n".join(rows)


# ----------------------------------------------------------------------
# 证据卡：把素材翻成「社交层能直接用的卡」
# ----------------------------------------------------------------------
# 素材原本只给日程生成用（event/location/minutes/slots）。社交层要拿它当由头
# 主动开口，还需要三样东西：这条是**哪一类**素材（中文出处，念得出来）、
# 什么时候采集的、到什么时候就不该再说了。这三样统一由 humanoid.opportunity
# 的卡片形状承载，这里只负责把一条素材翻过去。
BAND_SOURCES: dict[str, str] = {
    "life": "生活素材",
    "interest": "兴趣",
    "duty": "责任",
    "social": "社交",
    "rest": "歇着",
    "rare": "偶发",
}


def material_source(item: Mapping[str, Any]) -> str:
    """这条素材的出处标签（未知档按「生活素材」）。"""
    return BAND_SOURCES.get(str(item.get("band") or ""), BAND_SOURCES["life"])


# ----------------------------------------------------------------------
# 感官细节（v3.1.9）：卡片上那半行「手上的感觉」
# ----------------------------------------------------------------------
# 为什么要有：v3.1.8 的卡片 detail 是 `地点，大概 N 分钟`——那是一张工时表。模型
# 读到「超市，大概 55 分钟」，写出来的话就在报进度（「我刚去超市买了点东西，大概
# 花了快一个小时」）。真人不会这么说，她只会说「提回来手都勒红了」。
#
# 所以 detail 改成一句**只能影响语气、不能直接复述**的画面细节。条目自己写了
# `sensory` 就用它；没写的（兴趣/责任/社交这类只在人设有依据时才发的）按档位从
# 模板池里抽一句——宁可句子泛一点，也不要再退回「大概 N 分钟」。
SENSORY_TEMPLATES: dict[str, tuple[str, ...]] = {
    "life": ("手上还有点湿", "身上出了点薄汗", "手边的东西挪了个位置", "衣服上沾了点灰"),
    "rest": ("手边的东西放着没动", "屋里静得能听见自己呼吸", "坐的这块有点陷下去"),
    "duty": ("肩上有点发紧", "眼睛看屏幕看得发干", "手边堆着几张纸"),
    "social": ("嘴角还带着点笑", "说话说得嗓子有点干", "耳边还留着刚才的动静"),
    "rare": ("手上还沾着点灰", "心里还惦记着刚才那下", "屋里比平时乱一点"),
    "interest": ("手上还留着刚才的劲", "眼睛有点发酸", "肩颈僵着没松开"),
}


def sensory_line(item: Mapping[str, Any], seed: Sequence[Any] | None = None) -> str:
    """这一条的感官细节：条目自带的优先，否则按档位模板池抽一句（同日稳定）。"""
    own = str(item.get("sensory") or "").strip()
    if own:
        return own
    band = str(item.get("band") or "").strip()
    options = SENSORY_TEMPLATES.get(band) or SENSORY_TEMPLATES["life"]
    return pick(
        "material_sensory_" + (band or "life"),
        [*(seed or []), str(item.get("event") or "")],
        options,
    )


def material_card(
    item: Mapping[str, Any],
    *,
    captured_at: Any,
    seed: Sequence[Any] | None = None,
    ttl: Any = None,
) -> dict[str, Any]:
    """把一条素材翻成证据卡（形状见 humanoid.opportunity）。

    `captured_at` 由调用方给（通常是契约生成时刻，按小时取整）：素材是「她这样的人
    今天还可能做」的事，采集时间说的是「什么时候想到的」，不是「什么时候发生的」。
    `anchor_key` 用题眼（topic）：今天做过「洗衣」就不再发「洗衣」，两处指的是同一件事。
    """
    event = str(item.get("event") or "").strip()
    # v3.1.9：detail 从「地点，大概 N 分钟」改成一句感官细节。为什么不留时长：
    # 模型会把「大概 55 分钟」当成一句可以直接念的进度汇报。时长仍然由
    # `material_minutes` 给日程生成用，只是不再进模型上下文。
    detail = sensory_line(item, seed)
    topic = str(item.get("topic") or "").strip() or event
    return make_card(
        kind="life",
        title=event,
        detail=detail,
        source=material_source(item),
        captured_at=captured_at,
        ttl=MATERIAL_TTL_SECONDS if ttl is None else ttl,
        anchor_key=anchor_key_for("life", topic),
    )


def materials_as_cards(
    items: Iterable[Mapping[str, Any]],
    *,
    captured_at: Any,
    seed: Sequence[Any] | None = None,
    skip: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """一批素材 → 一批卡。坏条目单条丢弃，不因为一条坏的让整批作废。

    skip 是近 7 天已经用过的键（card_id / anchor_key），命中就丢：只按题眼过滤不
    够——旧记录里可能只有 card_id，所以这里拿卡自己的 id 再兜一次。
    """
    skip_set = {str(key).strip() for key in (skip or ()) if str(key or "").strip()}
    cards: list[dict[str, Any]] = []
    for item in items or ():
        if not isinstance(item, Mapping):
            continue
        try:
            card = material_card(item, captured_at=captured_at, seed=seed)
        except (TypeError, ValueError):
            continue
        if skip_set and (
            str(card.get("id") or "") in skip_set
            or str(card.get("anchor_key") or "") in skip_set
        ):
            continue
        cards.append(card)
    return cards


# ----------------------------------------------------------------------
# 从「可能想做」到「已经做了」（v3.1.9）
# ----------------------------------------------------------------------
# 为什么要有这一步：卡片上写「去楼下超市把缺的东西补上」，模型只能把它当**可能的
# 由头**说（「我可能等会儿去趟超市」）；可她下一句又说「我刚从超市回来」，两轮之后
# 自己都记不清到底去没去。人不是这样过日子的：做过就是做过。
#
# 所以：她真的说出口、且命中了某条素材的关键词，就把这条记成 done，随后几轮以
# **已经发生**的样子再递一次（`今天下午去楼下超市把缺的东西补上了`），并停止再以
# 「可能想做」发它。判定放在发送侧（main.py 的两个出站钩子），因为 Core 看不到正文。
MATERIALS_DONE_KEY = "materials_done"
MATERIALS_DONE_LIMIT = 64
#: 命中后还能以「已经做了」的形式出现几轮。三轮足够她把这件事讲完，又不会一直挂在
#: 上下文里变成新的复读。
MATERIALS_DONE_TURNS = 3
MATERIALS_DONE_WINDOW_SECONDS = 2 * 24 * 3600.0

#: 判断「她这句话是不是在说这条素材」时不算数的双字组合：都是虚词/泛用词，命中它们
#: 说明不了任何事（「有点」「东西」「什么」几乎每句都有）。
_STOP_BIGRAMS = frozenset(
    """
    什么 一个 一下 一会 有点 东西 时候 自己 已经 还是 就是 不是 没有 可以 我们
    你们 他们 这个 那个 现在 今天 昨天 明天 起来 出来 上去 下来 过去 回来 的话
    得很 会儿 一直 其实 然后 所以 但是 因为 一点 两个 几个 半天 一阵 这件 那件
    这样 那样 怎么 知道 觉得 应该 真的 好像 可能 一定 也是 都是 还有 一边 里面
    外面 这里 那里 之前 之后 差不多 大概 左右
    """.split()
)


def material_keywords(card: Mapping[str, Any]) -> set[str]:
    """一张卡的关键词：题眼 + 标题/细节里的双字组合（去掉虚词组合）。

    为什么用双字组合而不是分词：这里没有分词器，而中文里「超市」「快递」「阳台」
    这类实词恰好都是双字；拿双字窗口去撞她的原话，误判率比整句包含低得多，
    也不需要额外依赖。
    """
    title = str(card.get("title") or "")
    detail = str(card.get("detail") or "")
    words: set[str] = set()
    anchor = str(card.get("anchor_key") or "")
    topic = anchor.split(":", 1)[1].strip() if ":" in anchor else ""
    if len(topic) >= 2:
        words.add(topic)
    clean = "".join(ch for ch in title + detail if "\u4e00" <= ch <= "\u9fff")
    for index in range(len(clean) - 1):
        gram = clean[index:index + 2]
        if gram not in _STOP_BIGRAMS:
            words.add(gram)
    return words


def material_mentioned(
    text: str, cards: Iterable[Mapping[str, Any]], *, threshold: int = 2
) -> list[dict[str, Any]]:
    """她这句话里提到了哪几条素材（≥ threshold 个关键词命中才算）。

    阈值是 2：只命中一个词太容易误判（「手机」既能是她刚清过截图，也可能只是顺口
    一提）；两个词同时出现，基本就是在说同一件事。
    """
    body = str(text or "")
    if not body:
        return []
    hits: list[dict[str, Any]] = []
    for card in cards or ():
        if not isinstance(card, Mapping):
            continue
        matched = sum(1 for word in material_keywords(card) if word and word in body)
        if matched >= int(threshold):
            hits.append(dict(card))
    return hits


def note_done(
    records: Iterable[Mapping[str, Any]] | None,
    card: Mapping[str, Any],
    now_ts: Any,
    *,
    slot: str = "",
    limit: int = MATERIALS_DONE_LIMIT,
    turns: int = MATERIALS_DONE_TURNS,
) -> list[dict[str, Any]]:
    """把一条素材记成「已经做了」。同一题眼只留一条，后来的覆盖时间与轮数。"""
    rows = [dict(row) for row in list(records or []) if isinstance(row, Mapping)]
    anchor = str(card.get("anchor_key") or "").strip()
    title = str(card.get("title") or "").strip()
    if not anchor or not title:
        return rows
    try:
        moment = float(now_ts)
    except (TypeError, ValueError):
        return rows
    entry = {
        "anchor_key": anchor,
        "title": title,
        "detail": str(card.get("detail") or "").strip(),
        "source": str(card.get("source") or "").strip(),
        "slot": str(slot or "").strip(),
        "done_at": moment,
        "turns": max(0, int(turns)),
    }
    for index, row in enumerate(rows):
        if str(row.get("anchor_key") or "") == anchor:
            rows[index] = entry
            break
    else:
        rows.append(entry)
    rows.sort(key=lambda row: float(row.get("done_at") or 0.0))
    return rows[-int(limit):]


def past_title(event: str, slot: str = "") -> str:
    """把「可能做」的标题改成「已经做了」的说法。

    中文没有时态标记，只能靠「了/过/着/完」这类字。已经有这些字的原样保留
    （「坐着发了会儿呆」本身就成立）；没有的补一个「了」在末尾（「去楼下超市把缺的
    东西补上」→「…补上了」）。宁可句子糙一点，也不要让模型看不出这是既成事实。
    """
    text = str(event or "").strip()
    if not text:
        return ""
    prefix = f"今天{slot}" if slot else "今天"
    if any(mark in text for mark in ("了", "过", "着", "完")):
        return prefix + text
    return prefix + text + "了"


def done_cards(
    records: Iterable[Mapping[str, Any]] | None,
    now_ts: Any,
    *,
    window: float = MATERIALS_DONE_WINDOW_SECONDS,
    limit: int = MATERIALS_DONE_LIMIT,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    """把台账翻成这一轮要递的「已经做了」卡片。

    返回 `(cards, updated, keys)`：`cards` 是本轮还能出现（`turns > 0`）的卡片，
    `updated` 是扣掉一轮之后的台账（调用方写回），`keys` 是仍然生效的题眼集合——
    调用方用它把同名素材从「可能想做」那一批里剔掉。
    """
    try:
        moment = float(now_ts)
    except (TypeError, ValueError):
        moment = 0.0
    live: list[dict[str, Any]] = []
    for row in list(records or []):
        if not isinstance(row, Mapping):
            continue
        try:
            done_at = float(row.get("done_at") or 0.0)
        except (TypeError, ValueError):
            continue
        if done_at <= 0:
            continue
        if moment and done_at - moment > 3600.0:
            # 未来时间戳（时钟回拨/坏数据）当现在处理，不当成永久有效。
            done_at = moment
        if moment and moment - done_at > float(window):
            continue
        live.append(dict(row))
    live = live[-int(limit):]
    cards: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    for row in live:
        try:
            turns = int(row.get("turns") or 0)
        except (TypeError, ValueError):
            turns = 0
        if turns > 0:
            try:
                card = done_card(row)
            except (TypeError, ValueError):
                card = None
            if card:
                cards.append(card)
                row["turns"] = turns - 1
        updated.append(row)
    keys = {
        str(row.get("anchor_key") or "").strip()
        for row in updated
        if str(row.get("anchor_key") or "").strip()
    }
    return cards, updated, keys


def done_card(row: Mapping[str, Any]) -> dict[str, Any]:
    """一条 done 台账 → 一张证据卡（kind 沿用现有卡类型，purpose 与其他卡一致）。"""
    title = str(row.get("title") or "").strip()
    if not title:
        raise ValueError("empty title")
    try:
        done_at = float(row.get("done_at") or 0.0)
    except (TypeError, ValueError):
        done_at = 0.0
    anchor = str(row.get("anchor_key") or "").strip() or anchor_key_for("life", title)
    return make_card(
        kind="life",
        title=past_title(title, str(row.get("slot") or "").strip()),
        detail=str(row.get("detail") or "").strip(),
        source=str(row.get("source") or "").strip() or BAND_SOURCES["life"],
        captured_at=done_at,
        ttl=MATERIAL_TTL_SECONDS,
        anchor_key=anchor,
    )
