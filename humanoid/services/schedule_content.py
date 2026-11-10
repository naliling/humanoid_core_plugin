"""日程内容的硬准入：什么词不能进日程，什么候选可以递给模型。

为什么单独一层（v3.1.16 重构）：

旧做法是「生成完再替换」——`schedule.py` 里的 `_DEVICE_SWAPS` 把「手机」换成
「手边的东西」。问题不在换得对不对，在于这个动作本身是永动机：

    模型写「夹着手机」→ 换成「手边的东西」→ 存进日程
      → 这句又被注入回提示词 → 模型看到含糊词，自己补回「手机」
      → 再被换成「手边的东西」……

禁词表越加越长（3.1.12 加手机/屏幕，3.1.14 又加打字/消息/耳机/充电线），
每一轮补丁都在给下一轮制造新词。而且替换出来的句子是病句：
「给姐姐发消息」→「给姐姐发话」。

这里改成两条，各管一头：

1. **生成前**（`build_slate` / `render_slate`）：把已经过筛的候选事件摆到
   模型面前，让它**选**一条或照着改，而不是从空白里自由写。候选里没有设备、
   没有含糊占位词，写出来自然干净。
2. **生成后**（`content_issues`）：只做**判定**，不做替换。判出问题就交给
   现有的质量闸重写一次；重写还脏就降级成处境词（`dynamic_segment` 那批
   干净的词），**绝不把脏文本写进状态**。

为什么不做「替换」：替换会改写用户/模型的原意，还会造病句；而判定+重写
把选择权留给模型，最差也只是退成一句中性但通顺的处境词。

词表口径：
- `DEVICE_TERMS`：她活在对话里，不活在设备里。手机/电脑/屏幕这类实物，
  以及只能在设备上做的事（打字、发消息、上网、打游戏）。
- `VAGUE_TERMS`：用含糊词顶替具体物的写法（「手边的东西」）。这类词本身
  没信息量，被模型读到还会自己补全回设备——正是旧替换器制造的那一类。
- 「消息」两个字**不单独禁**：它是普通中文（「他发来一条消息」），禁掉会
  误伤。只禁「发消息/回消息/看消息」这种设备动作短语。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

#: 设备实物与只能在设备上做的事。按子串匹配——中文没有词边界，子串最稳。
DEVICE_TERMS: tuple[str, ...] = (
    # 实物
    "手机", "屏幕", "电脑", "平板", "键盘", "鼠标", "耳机", "充电器", "充电线",
    "充电", "电视", "显示器", "摄像头", "音箱", "智能机", "路由器", "插座板",
    # 网络与账号
    "微信", "QQ", "qq", "短信", "上网", "网络", "wifi", "WiFi", "WIFI",
    # 屏幕上的娱乐
    "游戏", "手游", "电竞", "视频", "直播", "弹幕",
    # 设备动作（只禁动作短语，不禁「消息」这个词本身）
    "打字", "发消息", "回消息", "看消息", "刷消息", "消息提示",
    "电话", "视频通话", "语音通话", "刷手机", "看手机", "玩手机",
)

#: 含糊占位词：旧替换器（手机 → 手边的东西）制造出来的那一类。
#: 它们不是设备词，但同样不该进日程：读者不知道是什么，模型会自己补回设备。
VAGUE_TERMS: tuple[str, ...] = (
    "手边",
    "手头的东西",
    "手上的东西",
    "身边的东西",
    "某个东西",
    "一些东西",
    "那东西",
    "那个东西",
    "东西挪了个位置",
    "东西放着没动",
)

#: 低信息量占位说法（v3.1.17）：语法通顺、也不碰设备，但读者仍然不知道她在做什么。
#:
#: 它们本来是 `dynamic_segment` 的**处境词**（「忙自己的事」表示她这会儿没有具体的事），
#: 被模型当成事件写进日程就成了空话：日程列出来是「忙自己的事」，等于没排。
#: 只用于判定**模型自己写的事件**；处境兜底词本身带 `status`，不走这条。
LOW_INFO_TERMS: tuple[str, ...] = (
    "忙自己的事",
    "自己的事",
    "手头的事",
    "手头的活",
    "手上的活",
    "手头有活",
    "忙手里的活",
    "手头有东西要处理",
    "手上有东西",
    "有东西要处理",
    "手上有点事",
    "有点事要处理",
    "找点事做",
    "做点事",
    "干点事",
    "瞎忙",
    "忙活",
    "什么也不干",
    "什么也没干",
    "无所事事",
    "坐着发呆",
    "坐着发愣",
)

#: 私事（v3.1.18）：她和别人的身体关系不进日程。
#:
#: 日程是「她这一天在做什么」，写的是能说出口的日常；这类事是她自己的私事，
#: 写进日程之后会被下一轮生成当成素材，于是同一件事反复重演——用户原话是
#: 「难不成想要人设每晚都自慰吗」。判定只认明确说法，不认「抱」「亲」这种日常词。
PRIVATE_TERMS: tuple[str, ...] = (
    "做爱", "性爱", "性交", "性行为", "自慰", "手淫", "高潮", "射精",
    "插入", "抽插", "口交", "舔舐", "下体", "私处", "阴道", "龟头", "勃起",
)

#: 候选清单最多摆几条：太多模型会当成清单逐条回应，太少又回到「天天那几件」。
SLATE_MAX = 8
#: 少于这个数就不算「有得选」，prompt 里明说「自己写也要具体」。
SLATE_MIN = 2

_STRIP_CHARS = "，。、；：！？…·-—~～ \t\n（）()「」“”\"'’"


def content_issues(text: str) -> list[str]:
    """这段文本为什么不能进日程。返回 ["设备:手机", "含糊:手边"]，空列表 = 干净。

    判定只看事件文本（event），不看 location/emotion——地点叫「书桌前」没问题，
    情绪词里也不会冒出设备。
    """
    body = str(text or "")
    if not body.strip():
        return []
    issues: list[str] = []
    for term in DEVICE_TERMS:
        if term in body:
            issues.append("设备:" + term)
    for term in VAGUE_TERMS:
        if term in body:
            issues.append("含糊:" + term)
    for term in PRIVATE_TERMS:
        if term in body:
            issues.append("私事:" + term)
    return issues


def low_info_issues(text: str) -> list[str]:
    """这段事件是不是「说了一句空话」（返回 ["含糊:忙自己的事"]，空列表 = 有信息量）。

    与 `content_issues` 分开：设备/含糊词是**硬约束**（任何出口都不许有），
    低信息量只对「模型自己写的事件」成立——处境兜底词（带 status）本来就是这个形状。
    """
    body = str(text or "")
    if not body.strip():
        return []
    for term in LOW_INFO_TERMS:
        if term in body:
            return ["含糊:" + term]
    return []


def is_clean(text: str) -> bool:
    """这段文本能不能直接进日程。"""
    return not content_issues(text)


def describe_issues(issues: Sequence[str]) -> str:
    """把 issue 码翻成重写提示里能读的一句（给 `_quality_fix_block` 用）。"""
    if not issues:
        return ""
    hits: list[str] = []
    for code in issues:
        _, _, word = str(code).partition(":")
        if word and word not in hits:
            hits.append(word)
    return "、".join(f"「{word}」" for word in hits[:4])


def fix_hint(issues: Sequence[str]) -> str:
    """重写提示里的完整修正说明。"""
    words = describe_issues(issues)
    if not words:
        return ""
    if any(str(code).startswith("物种:") for code in issues):
        # 物种这一条必须单独说清楚：模型不是「用错了词」，是这件事她本身做不了。
        return (
            f"- 这一版里出现了 {words}，可她的身体没有这些。"
            "换成她的蹄子、嘴或魔法做得到的事（比如把布叼起来、用角顶开柜门）；"
            "写不出就用不涉及身体形态的事。"
        )
    if any(str(code).startswith("私事:") for code in issues):
        return (
            f"- 这一版写的是 {words}。日程里不写这类事：那是她自己的私事，"
            "不排进日常，也不许用替换词把它写进去。换一件别的事。"
        )
    low = [str(code).partition(":")[2] for code in issues
           if str(code).partition(":")[2] in LOW_INFO_TERMS]
    if low:
        return (
            f"- 这一版里写的是 {words}，那是一句处境，不是一件事。"
            "要写她**手上正在做**的具体动作：洗了哪只碗、翻到第几页、"
            "把什么收进了哪个抽屉；写不出具体动作就换一件别的。"
        )
    return (
        f"- 这一版里出现了 {words}。她的世界没有手机、电脑、屏幕这类东西，"
        "也不许用「手边的东西」这种含糊说法顶替具体物。\n"
        "  换成具体、说得出口的东西：书、本子、杯子、碗、毛线、纸、窗外的树；"
        "或者干脆换一件不涉及这些的事。"
    )


#: 「她手上还开着的事」（life_log）用的窄口径（v3.1.30）：只认设备**物件**。
#:
#: 日程那边用的是 `content_issues`，它连「回消息」「打字」这类动作一起禁——对日程
#: 是对的（她的日程里不该有手机），但 life_log 记的是「谁在等什么」，「等 TA 回消息」
#: 讲的是那个人，按动作词删就把真事丢了。所以这里只清点名设备的词。
LIFE_OBJECT_TERMS: tuple[str, ...] = (
    "手机",
    "屏幕",
    "电脑",
    "平板",
    "键盘",
    "鼠标",
    "耳机",
    "充电器",
    "充电线",
    "电视",
    "显示器",
    "摄像头",
    "音箱",
    "智能机",
    "路由器",
    "插座板",
    "微信",
    "短信",
)


def object_issues(text: str) -> list[str]:
    """life_log 里该删的条目：出现设备**物件**才算，动作词（回消息、打字）不算。"""
    body = str(text or "")
    return [f"设备:{term}" for term in LIFE_OBJECT_TERMS if term in body]


#: 人形的身体才有、非人形角色身上不该出现的东西（v3.1.33）。
#:
#: 用户原话：「我的角色都是小马 哪来的鞋子 连人设都识别得不对」。素材池是按「人」
#: 写的一批日常（鞋柜、袜子、裤腿、手指），物种档案认出了小马，日程链路却从来没
#: 用过它——于是她一早起来「把门口的鞋摆整齐」。这里按「这具身体有没有」判，不按
#: 「像不像人」判：四条腿走路的小马照样会洗碗、会出门，只是没有手指和鞋。
HUMAN_ONLY_TERMS: tuple[str, ...] = (
    "鞋柜",
    "鞋底",
    "鞋跟",
    "拖鞋",
    "鞋带",
    "鞋子",
    "袜子",
    "裤子",
    "袖子",
    "手套",
    "手指",
    "五指",
    "指尖",
    "手掌",
    "手背",
    "手心",
    "头发",
    "穿鞋",
    "系鞋带",
    "鞋",
)

#: 判「不该有人形部件」的物种。人类与机械不判（机械的手是零件，不在这条线上）。
NON_HUMAN_KINDS: tuple[str, ...] = ("pony", "beast", "beast_ear", "spirit")


def species_issues(text: str, kind: str) -> list[str]:
    """这段文本里有没有「这具身体不该有」的东西。返回 ["物种:鞋"]，空表 = 没问题。

    `kind` 来自 `humanoid.species` 判出来的档案（pony/beast/beast_ear/spirit/human/
    machine），空串表示还没认出来——那就什么都不判，别在信息不足时乱删。
    """
    if str(kind or "") not in NON_HUMAN_KINDS:
        return []
    body = str(text or "")
    for term in HUMAN_ONLY_TERMS:
        if term in body:
            return ["物种:" + term]
    return []


def species_note(kind: str, label: str = "", traits: Sequence[str] = ()) -> str:
    """一句「按这具身体写」的事实（给日程提示词用）。认不出 / 人类时返回空串。"""
    if str(kind or "") not in NON_HUMAN_KINDS:
        return ""
    name = str(label or "").strip() or "非人形"
    parts = [str(t).strip() for t in (traits or ()) if str(t).strip()]
    head = f"她是{name}（{'、'.join(parts[:3])}）。" if parts else f"她是{name}。"
    if "四蹄" in parts:
        limb = "蹄子、嘴或魔法"
    elif str(kind) in ("beast", "beast_ear"):
        limb = "爪子、嘴或尾巴"
    else:
        limb = "她自己的身体"
    return head + f"没有手指、没有鞋，做事用{limb}。"


def is_displayable(slot: Mapping[str, Any], *, kind: str = "") -> bool:
    """这一段能不能露出来（v3.1.26 / v3.1.29 / v3.1.33）。

    设备/含糊/私事一律不行；低信息量的空话也不行——**包括带 `status: true` 的**
    「忙手里的活」「忙自己的事」：它们读起来像活动，其实什么也没说，露出来就是
    让读者自己去猜她在做什么。真正的处境词（「歇着」「赖床」「有点饿了」「睡一觉」）
    不在 `LOW_INFO_TERMS` 里，照样能露。

    `kind` 是物种档案（v3.1.33）：小马身上不该出现「擦鞋柜」这种段落，读路径也
    跟着挡——但只在认出物种之后才判，认不出来时一个字都不筛。
    """
    event = str(slot.get("event") or "")
    if content_issues(event) or species_issues(event, kind):
        return False
    return not low_info_issues(event)


def clean_slots(
    slots: Iterable[Mapping[str, Any]], *, kind: str = ""
) -> list[Mapping[str, Any]]:
    """过滤掉不该露出来的时段（读路径用：旧状态里的脏文本不再喂回模型）。

    只过滤、不改写：历史文本本身不动，但也不让它继续污染提示词与去重。
    """
    return [slot for slot in slots if is_displayable(slot, kind=kind)]


def _key(text: str) -> str:
    """比对键：去标点空白后取前 12 字。"""
    cleaned = "".join(ch for ch in str(text or "") if ch not in _STRIP_CHARS)
    return cleaned[:12]


def _clash(candidate: str, recent: Sequence[str]) -> bool:
    """这条候选是不是跟今天已经做过的事撞了（撞了就别再摆出来）。"""
    key = _key(candidate)
    if len(key) < 4:
        return False
    for text in recent:
        other = _key(text)
        if not other:
            continue
        if key == other:
            return True
        # 「在厨房煮面」与「在厨房煮了碗面」：前 6 字相同就算撞，别让去重形同虚设。
        if len(key) >= 6 and len(other) >= 6 and key[:6] == other[:6]:
            return True
    return False


def build_slate(
    materials: Iterable[Mapping[str, Any]] = (),
    examples: Iterable[Mapping[str, Any]] = (),
    *,
    recent_events: Iterable[str] = (),
    suspended: Mapping[str, Any] | None = None,
    rejected: Iterable[str] = (),
    kind: str = "",
    limit: int = SLATE_MAX,
) -> list[dict[str, Any]]:
    """把素材/范例/挂起整理成「可以选的事」：已过筛、去重、带编号。

    每条是 {"id": 1, "event": ..., "location": ..., "minutes": int, "source": "material"}。
    `source` 只用于日志与测试，不进 prompt。

    排序：挂起的事（能回去接着做）排第一，然后是素材，最后是范例——范例是
    「形状示范」，不该压过这个角色今天真有的素材。
    """
    recent = [str(text or "") for text in recent_events or ()]
    # 被质量闸拒过的写法（v3.1.17）：不再摆到模型面前，免得它照着又写一遍。
    blocked = [str(text or "") for text in rejected or ()]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(event: Any, location: Any, minutes: Any, source: str) -> None:
        text = str(event or "").strip()
        if not text or not is_clean(text):
            return
        # 空话不进候选（v3.1.27）：素材池里「什么也不干，坐着发了会儿呆」这类
        # 句子以前能混进 slate，自愈挑中就直接落库——候选必须是能看见的动作。
        if low_info_issues(text):
            return
        # 物种不对的也不进候选（v3.1.33）：小马没有鞋，可素材池是按人写的。
        if species_issues(text, kind):
            return
        key = _key(text)
        if key in seen or _clash(text, recent) or _clash(text, blocked):
            return
        seen.add(key)
        try:
            span = int(float(minutes or 0))
        except (TypeError, ValueError):
            span = 0
        out.append(
            {
                "id": len(out) + 1,
                "event": text,
                "location": str(location or "").strip(),
                "minutes": span,
                "source": source,
            }
        )

    susp = suspended if isinstance(suspended, Mapping) else {}
    if str(susp.get("event") or "").strip():
        add(
            susp.get("event"),
            susp.get("location"),
            susp.get("remaining_minutes"),
            "resume",
        )
    for item in materials:
        if isinstance(item, Mapping):
            add(item.get("event"), item.get("location"), item.get("minutes"), "material")
    for item in examples:
        if isinstance(item, Mapping):
            add(item.get("event"), item.get("location"), item.get("minutes"), "example")
    return out[: max(0, int(limit))]


def render_slate(items: Sequence[Mapping[str, Any]]) -> str:
    """把候选清单渲染成 prompt 里那一段（编号从 1 起）。"""
    rows: list[str] = []
    for item in items:
        location = str(item.get("location") or "").strip()
        try:
            minutes = int(item.get("minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        tail = ""
        if location and minutes > 0:
            tail = f"（{location}，约 {minutes} 分钟）"
        elif location:
            tail = f"（{location}）"
        elif minutes > 0:
            tail = f"（约 {minutes} 分钟）"
        rows.append(f"  {item.get('id')}. {str(item.get('event') or '').strip()}{tail}")
    return "\n".join(rows)


def slate_block(items: Sequence[Mapping[str, Any]]) -> str:
    """候选清单在 prompt 里的完整一段（含用法说明）。没有候选时返回空串。"""
    if not items:
        return ""
    lines = [
        "【可以选的事】（这个钟点她这样的人可能做的；都过过筛：不碰手机电脑这类东西，"
        "也不含糊）",
        render_slate(items),
        "  想用其中一条，就在 JSON 里写 \"pick\": 编号（再补 emotion / energy_rate 就行；"
        "location 与 minutes 不写就按上面那条）。",
    ]
    if len(items) < SLATE_MIN:
        lines.append("  候选不多，自己写一件也行——但要具体。")
    lines.append(
        "  也可以自己写一件——但同样要具体、同样不碰那些东西，别用「手边的东西」顶替。"
    )
    return "\n".join(lines) + "\n\n"

#: 整体心境的阈值（v3.1.18）：与 soma 的档位同一套量级，不另立一套。
MOOD_SLEEP_DEBT_HOURS = 2.0
MOOD_LAST_SLEEP_HOURS = 5.0
MOOD_SLEEP_PRESSURE = 80.0
MOOD_HUNGER = 70.0
MOOD_DISCOMFORT = 50.0
MOOD_AROUSAL = 78.0


def _num(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def overall_mood(body: Mapping[str, Any] | None) -> tuple[str, str]:
    """她今天整体是什么心境，返回 (标签, 依据)；没到阈值返回 ("", "")。

    只看**她自己的身体**，不看任何一个人的好感：日程是她自己的日子，不能因为
    某个人今天说了句好话就变成「心情很好的一天」。

    返回值里只有一句状态和一句依据，没有「所以多写点开心的事」这种指令——
    要不要让这一段受它影响，由生成侧自己权衡。
    """
    data = body or {}
    debt = _num(data.get("sleep_debt"))
    last = _num(data.get("last_sleep_hours"))
    if debt >= MOOD_SLEEP_DEBT_HOURS or 0.0 < last < MOOD_LAST_SLEEP_HOURS:
        if 0.0 < last < MOOD_LAST_SLEEP_HOURS:
            reason = f"昨夜只睡了 {last:g} 小时"
        else:
            reason = f"这两天欠了 {debt:g} 小时觉"
        return "没睡够，人有点发飘", reason
    if _num(data.get("sleep_pressure")) >= MOOD_SLEEP_PRESSURE:
        return "困得有点撑不住", "困意已经顶到 80 以上"
    if _num(data.get("hunger")) >= MOOD_HUNGER:
        return "心里发空", "饿了大半天还没正经吃东西"
    if _num(data.get("discomfort")) >= MOOD_DISCOMFORT:
        return "身上不得劲", "躯体不适已经过半"
    if _num(data.get("arousal")) >= MOOD_AROUSAL:
        return "身上有点燥", "身子发软，静不下来"
    return "", ""
