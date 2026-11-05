"""身体上的事什么时候递给她：不是每轮都说，更不是同一句反复说。

现场（用户 m08201 / m08275，附聊天记录）：她**每一句**回复都在说小腹——「按着隐隐
作痛的小腹」「小腹坠着」「肚子难受」。注入里的那一句是：

    我这两天小腹坠着，腰也酸，人有点发懒

这句话本身没错，错在**递送频率**：月经期横跨五天，而每个阶段只有一句措辞，于是
同一句话一天几十轮、连着五天逐字进上下文。模型看到「她一直在说这件事」，最省力
的续写就是把它当台词复述一遍——每一轮都是。

真人不会每隔一句就告诉对方自己肚子疼。她身上难受，只会在**说得着的时候**提：
对方问起了，或者隔了很久（几天）才顺口带一句。这个模块就是那道闸：

1. `asked_about_body`：对方是不是在问她的身体（问到了就必须答，不能装没感觉）；
2. `mentions_body`：她**自己刚说过**身体上的事（说过就别再递，否则自我强化）；
3. `due`：距上次递过去够不够久。

三个判据都只看结构化输入（对方原话、她自己的原话、时间戳），不看注入正文里有没有
某个词——正文里出现「小腹」这件事本身不构成任何判断依据（P-03 的教训）。
"""

from __future__ import annotations

#: 她自己的话里出现这些词 = 她刚说过身体上的事。宁宽不窄：漏一个词，她就会连着
#: 几轮把同一件事换个说法重复，那正是要修的病。
BODY_WORDS = (
    "小腹", "肚子", "腰酸", "腰疼", "腰也酸", "腰发沉", "腰直不", "坠", "胀",
    "疼", "痛", "难受", "不舒服", "发冷", "着凉", "恶心", "头晕", "没力气",
    "经期", "月经", "姨妈", "生理期", "例假",
)

#: 对方问到这些 = 在问她的身体，必须答。
#:
#: **不收「肚子」「腰」这两个裸词**：家常话里「肚子饿了」「你腰不好吗」到处都是，
#: 收进来等于每两句就触发一次，又回到「每轮都在说小腹坠」。
#: 「怎么了」「还好吗」「没事吧」这类泛泛关心也不收——它们问的是"你现在怎么样"，
#: 不是"你身体怎么了"，由关系与情绪层回答更合适。
ASKED_TOPICS = (
    "姨妈", "月经", "经期", "生理期", "例假", "痛经", "小腹",
    "腰酸", "腰疼", "腰不舒服", "肚子疼", "肚子痛", "肚子不舒服",
    "疼吗", "痛吗", "还疼", "还痛", "难受吗", "不舒服吗",
    "身体", "气色", "脸色",
)

#: 周期感觉最短重复间隔（秒）。三天：一个经期（约五天）里最多提两次，
#: 对方问起时不看这个间隔。
CYCLE_QUIET_SECONDS = 3 * 24 * 3600.0

#: 「身体不适」这条体感的最短重复间隔（秒）。它和周期是两回事（久坐、闷热、
#: 经期都会推高 discomfort），但落到她嘴里都是「不舒服」，所以同样限流。
DISCOMFORT_QUIET_SECONDS = 8 * 3600.0

#: 她自己说过身体上的事之后，这段时间内不再递身体事实（秒）。
#: 这一条专治自我强化：她上一轮刚说完肚子难受，下一轮的注入又递一句「小腹坠着」，
#: 模型于是把这件事当成正在进行的对话主题，接着往下说。
ECHO_QUIET_SECONDS = 4 * 3600.0


def mentions_body(text: object) -> bool:
    """她这句话里有没有身体上的事。"""
    body = str(text or "")
    if not body:
        return False
    return any(word in body for word in BODY_WORDS)


def asked_about_body(text: object) -> bool:
    """对方这句话是不是在问她的身体。"""
    said = str(text or "")
    if not said:
        return False
    return any(word in said for word in ASKED_TOPICS)


def due(now: float, last_at: float, quiet_seconds: float) -> bool:
    """距上次够不够久。

    `last_at <= 0` 表示没记过（新角色、老存档、写不进去）——按「从没说过」处理，
    宁可多说一次，也不要因为存不下而整月不吭声。时间倒流（时钟回拨）同样按
    「没记过」处理：负的间隔既不该永远为真，也不该永远为假。
    """
    try:
        moment = float(now)
        last = float(last_at or 0.0)
    except (TypeError, ValueError):
        return True
    if last <= 0.0:
        return True
    elapsed = moment - last
    if elapsed < 0.0:
        return True
    return elapsed >= float(quiet_seconds)


def cycle_due(now: float, last_at: float, *, asked: bool = False) -> bool:
    """周期感觉这一轮要不要递：对方问了就递，否则隔够 CYCLE_QUIET_SECONDS 才递。"""
    if asked:
        return True
    return due(now, last_at, CYCLE_QUIET_SECONDS)
