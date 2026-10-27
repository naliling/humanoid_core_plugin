"""兄弟 bot 互聊护栏：可以聊，但防互刷。

背景：多台同平台/同进程部署的 bot，本插件状态里见过的其它角色 id 会被认成「兄弟」
（`main.py::_known_role_ids`）。以前在回复钩子里对兄弟 bot 直接 `stop_event`，于是
同机另一台 bot 永远得不到回复——在同一个群里互相 @ 时，本台一句都不接，看起来像装死。

但完全放开也不行：两台 bot 你回我、我回你，一条人类消息能引出几十轮机器对机器的
空转。这里只做一件很小的事：

* 同一对 `(role_id, peer_id)` 在 `window_seconds`（默认 600 秒）内最多放行
  `max_turns`（默认 3）次；第 4 次拒绝；窗口滑过去后自动恢复；
* 真人一开口，`note_human(role_id)` 清掉**该角色名下所有 peer** 的计数——
  互聊链本来就该由人打断。

刻意保持**纯逻辑、无 IO、无 asyncio**：所有时间由调用方递进来（生产传
`time.time()`，测试直接喂假时间），坏参数/坏时间戳只影响这一次判断，绝不抛。

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
        # (role, peer) → 最近一次窗口内**放行**的时间戳（升序，最多 max_turns 个）。
        self._turns: dict[tuple[str, str], list[float]] = {}

    def allow(self, role_id: Any, peer_id: Any, now: Any = None) -> bool:
        """这条兄弟 bot 的发言放不放行；放行时记一笔。

        身份认不出来（空 id）→ 不放行、也不记账。防刷是这个类唯一的职责，认不出
        谁在说话时让互聊链开跑，比少回一句更糟。
        `now` 缺省或坏掉时按真实时钟算：一个坏参数不该把护栏关掉。
        """
        role = _as_id(role_id)
        peer = _as_id(peer_id)
        if not role or not peer:
            return False
        stamp = _finite_stamp(now)
        if stamp is None:
            stamp = time.time()
        key = (role, peer)
        recent = self._recent(key, stamp)
        if len(recent) >= self.max_turns:
            # 拒绝不记账：额度只按「真的放行了」消耗，否则被拒的消息会一直续窗口。
            self._store(key, recent)
            return False
        recent.append(stamp)
        self._store(key, recent)
        return True

    def note_human(self, role_id: Any) -> None:
        """真人开口：该角色名下所有 peer 的互聊计数清零。

        只清这个角色：多角色部署时，另一个 bot 的互聊链不该被这边连带清掉。
        """
        role = _as_id(role_id)
        if not role:
            return
        for key in [k for k in self._turns if k[0] == role]:
            del self._turns[key]

    # ------------------------------------------------------------------
    # 内部：窗口维护
    # ------------------------------------------------------------------

    def _recent(self, key: tuple[str, str], now: float) -> list[float]:
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

    def _store(self, key: tuple[str, str], recent: list[float]) -> None:
        if recent:
            self._turns[key] = recent
        else:
            self._turns.pop(key, None)
