"""兄弟 bot 互聊护栏：可以聊，但防互刷。

背景：多台同平台/同进程部署的 bot，本插件状态里见过的其它角色 id 会被认成「兄弟」
（`main.py::_known_role_ids`）。以前在回复钩子里对兄弟 bot 直接 `stop_event`，于是
同机另一台 bot 永远得不到回复——在同一个群里互相 @ 时，本台一句都不接，看起来像装死。

但完全放开也不行：两台 bot 你回我、我回你，一条人类消息能引出几十轮机器对机器的
空转。这里只做一件很小的事：

* 同一对 (role_id, peer_id) 在同一个会话（scope，生产传 umo）里、window_seconds
  （默认 600 秒，配置项 sibling_window_seconds）内最多放行 max_turns（默认 3，
  配置项 sibling_max_turns）次；超出拒绝；窗口滑过去后自动恢复；
* 真人一开口，note_human(role_id, peer_id, scope) 清掉**这一条链**的计数——互聊链
  本来就该由人打断；拿不到 peer / scope 时逐级退回（见方法注释）。

为什么额度要带会话：互刷是「两台 bot 在同一个会话里你一句我一句」。只按
(role, peer) 记，A 群聊满 3 句之后，B 群里那台 bot 的第一句也会被挡——她在一个
群里聊过天，就在另一个群里装死。加上会话维度之后，两个群各算各的。

刻意保持**纯逻辑、无 IO、无 asyncio**：所有时间由调用方递进来（生产传
`time.monotonic()`，测试直接喂假时间），坏参数/坏时间戳只影响这一次判断，绝不抛。
用单调时钟而不是墙钟：墙钟回拨会让「窗口内」恒真（护栏永久卡死），前拨会让
它恒假（护栏失效），而这两个数只用来算间隔，不需要日历含义。

边界（写清，免得当成 bug）：
* 这是**进程内**护栏。跨进程的两个 bot 互相认不出（`_known_role_ids` 只有本进程
  状态），护栏自然不会触发；
* 计数不落盘：插件重启 = 额度恢复。防的是短时间的机器对机器刷屏，不是长期配额。
"""

from __future__ import annotations

import time
from typing import Any

DEFAULT_WINDOW_SECONDS = 600.0
DEFAULT_MAX_TURNS = 3


def _finite_stamp(value: Any) -> float | None:
    """把一个「时间戳」归一化成有限 float；认不出来 / NaN / ±inf 返回 None。"""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _as_id(value: Any) -> str:
    """角色 / peer id 归一化成去空白的 str；None 或空 → 空串（认不出来）。"""
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:  # pragma: no cover - __str__ 抛异常的病态对象
        return ""


def _positive_number(value: Any, default: float) -> float:
    """正数参数校验：坏值 / 0 / 负数退回默认值。"""
    out = _finite_stamp(value)
    if out is None or out <= 0:
        return default
    return out


def _positive_int(value: Any, default: int) -> int:
    """正整数参数校验：坏值 / 小于 1 退回默认值。"""
    out = _finite_stamp(value)
    if out is None or out < 1:
        return default
    try:
        return int(out)
    except (TypeError, ValueError, OverflowError):  # pragma: no cover - 上面已挡住
        return default


class SiblingChatGuard:
    """同一 (role, peer) 的互聊限流器。语义见模块头。

    不是线程安全的：只允许在同一个事件循环（同一个进程的主线程）里用。
    """

    def __init__(
        self,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        max_turns: int = DEFAULT_MAX_TURNS,
    ) -> None:
        # 坏参数（字符串、0、负数、NaN）一律退回默认值——护栏宁可保守，也不能因为
        # 一个配置手滑变成「永不拦」或「全拦」。
        self.window_seconds = _positive_number(window_seconds, DEFAULT_WINDOW_SECONDS)
        self.max_turns = _positive_int(max_turns, DEFAULT_MAX_TURNS)
        # (role, peer, 会话) → 窗口内**放行**的时间戳（升序，最多 max_turns 个）。
        # 会话维度不能省：只按 (role, peer) 记，一个群聊满额度会让另一个群也静默。
        self._turns: dict[tuple[str, str, str], list[float]] = {}
        # 见过的 peer id。只用来回答「这个 uid 是不是兄弟 bot」——出站记账要用
        # 同一判据（见 is_known_peer）。id 集合很小（一个进程里几个 bot），不设上限。
        self._peers: set[str] = set()

    def allow(self, role_id: Any, peer_id: Any, now: Any = None, scope: Any = "") -> bool:
        """这条兄弟 bot 的发言放不放行；放行时记一笔。

        身份认不出来（空 id）→ 不放行、也不记账。防刷是这个类唯一的职责，认不出
        谁在说话时让互聊链开跑，比少回一句更糟。
        now 缺省或坏掉时按真实时钟算：一个坏参数不该把护栏关掉。
        scope 是会话标识（生产传 umo）：额度按 (role, peer, 会话) 各算各的，
        一个群聊满 3 句不该让另一个群也静默。空 scope = 认不出会话，退回全局一份
        （宁可多拦，也不给互刷留口子）。
        """
        role = _as_id(role_id)
        peer = _as_id(peer_id)
        if not role or not peer:
            return False
        # 认出来的 peer 一律记下（含被拒的）：它是不是兄弟 bot 与这次放不放行无关。
        self._peers.add(peer)
        stamp = _finite_stamp(now)
        if stamp is None:
            # 单调时钟：墙钟被回拨后 `now - stamp < window` 会恒真（护栏永久卡死），
            # 被前拨则恒假（护栏失效）。调用方也应按单调时钟传 now。
            stamp = time.monotonic()
        key = (role, peer, _as_id(scope))
        recent = self._recent(key, stamp)
        if len(recent) >= self.max_turns:
            # 拒绝不记账：额度只按「真的放行了」消耗，否则被拒的消息会一直续窗口。
            self._store(key, recent)
            return False
        recent.append(stamp)
        self._store(key, recent)
        return True

    def is_known_peer(self, peer_id: Any) -> bool:
        """这个 uid 是不是**已识别**的兄弟 bot。

        出站记账要用同一道判据：只在入站拦、出站照记，等于兄弟 bot 的每句话
        都在改她的情绪、好感与「她说过什么」（审核 跨插件契约 12）。只认 id：
        空 id / 认不出来的值永远不是 peer。
        """
        peer = _as_id(peer_id)
        return bool(peer) and peer in self._peers

    def note_human(self, role_id: Any, peer_id: Any = None, scope: Any = None) -> None:
        """真人开口：清掉这一条互聊链的计数。

        额度是按 (role, peer, 会话) 记的，人在这条链上说话只说明这条链该结束。
        以前无条件清该角色名下全部 peer，于是 A 群里有人说句话，B 群里那对 bot
        的额度也被顺手清零——一条人类消息就能让另一条互聊链重开一轮。

        逐级退回（信息越少清得越宽，但**不跨会话**）：
        * 给了 scope 和 peer → 只清那一条链；
        * 只给 scope → 清这个会话里该角色的所有 peer（人在这个会话里说话，这个
          会话里的互聊链都该结束）；
        * 没给 scope、只给 peer → 清这一对在所有会话里的额度；
        * 什么都没给 → 清该角色名下所有 peer 的所有会话。
        以前无条件走最后一档：A 群有人说话会把 B 群那对 bot 的额度一起放开，
        一条人类消息就能让另一条互刷链重开一轮。
        """
        role = _as_id(role_id)
        if not role:
            return
        peer = _as_id(peer_id)
        scope_id = _as_id(scope)
        has_scope = scope is not None
        doomed = [
            key
            for key in self._turns
            if key[0] == role
            and (not peer or key[1] == peer)
            and (not has_scope or key[2] == scope_id)
        ]
        for key in doomed:
            del self._turns[key]

    # ------------------------------------------------------------------
    # 内部：窗口维护
    # ------------------------------------------------------------------

    def _recent(self, key: tuple[str, str, str], now: float) -> list[float]:
        """窗口内仍有效的放行时间戳；历史里的坏值直接丢掉。"""
        out: list[float] = []
        for value in self._turns.get(key, ()):
            stamp = _finite_stamp(value)
            if stamp is None:
                continue
            # 时间戳比「现在」还新（时钟回拨）也算有效：宁可多算一次，也不给互刷留口子。
            if now - stamp < self.window_seconds:
                out.append(stamp)
        return out

    def _store(self, key: tuple[str, str, str], recent: list[float]) -> None:
        if recent:
            self._turns[key] = recent
        else:
            self._turns.pop(key, None)
