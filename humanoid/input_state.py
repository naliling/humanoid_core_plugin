"""输入状态（正在输入）上报。

做法照市场插件 astrbot_plugin_input_state_by_nc：在 LLM 请求开始后，每 interval 秒
向 NapCat 报一次 `set_input_status(user_id, event_type=1)`，请求结束就停。

两个必须照抄的细节：

1. **要持续重报**。QQ 的输入状态大约 8~10 秒没人刷新就自己消失，报一次是不够的。
2. **只在私聊**。这个接口的 user_id 是「私聊里的对方」，群聊里没有对应语义（参考插件
   也是直接把群聊跳过）。

这一层是纯装饰：任何一步出错（不是 NapCat、连接断了、对方离线）都只记一条 debug 日志，
既不影响正常回复，也不会让状态一直亮着——每个任务自己带 timeout 兜底。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Tuple

# 任务表里的每一条：任务、停止信号、上一次上报时间。
_Entry = Tuple[asyncio.Task, asyncio.Event]


class InputStateNotifier:
    """按会话维护「正在输入」的上报任务。"""

    def __init__(
        self,
        logger: Any,
        interval: float = 0.5,
        timeout: float = 120.0,
    ) -> None:
        self._log = logger
        self._tasks: Dict[str, _Entry] = {}
        self._last_active: Dict[str, float] = {}
        self.interval = max(0.05, float(interval))
        self.timeout = max(1.0, float(timeout))

    # ------------------------------------------------------------------

    def configure(self, interval: float, timeout: float) -> None:
        """配置热重载：改的是之后新起的任务，正在跑的按老节奏走完。"""
        self.interval = max(0.05, float(interval))
        self.timeout = max(1.0, float(timeout))

    def is_supported(self, event: Any) -> Tuple[bool, str]:
        """这个事件能不能上报：只有 NapCat 的私聊才做。

        返回 (能不能, 对方 ID)。判断刻意用「有没有 event.bot」+ 平台名，而不是
        isinstance：core 要跑在 aiocqhttp / qq_official 等各种部署上，拿不到就直接跳过。
        """
        try:
            if getattr(event, "get_platform_name", lambda: "")() != "aiocqhttp":
                return False, ""
            if not _is_private(event):
                return False, ""
            bot = getattr(event, "bot", None)
            api = getattr(bot, "api", None)
            if bot is None or api is None or not hasattr(api, "call_action"):
                return False, ""
            sender = str(event.get_sender_id() or "")
            if not sender:
                return False, ""
            return True, sender
        except Exception:
            return False, ""

    # ------------------------------------------------------------------

    async def start(self, key: str, event: Any) -> None:
        """开始给这条会话上报。已在跑就什么都不做（参考插件也是这个口径）。"""
        if not key:
            return
        existing = self._tasks.get(key)
        if existing is not None and not existing[0].done():
            return
        _, user_id = self.is_supported(event)
        if not user_id:
            return
        client = getattr(getattr(event, "bot", None), "api", None)
        if client is None:
            return
        stop_event = asyncio.Event()
        task = asyncio.create_task(
            self._run(key, client, user_id, stop_event),
            name=f"input-state:{key}",
        )
        self._tasks[key] = (task, stop_event)
        self._last_active[key] = time.monotonic()

    async def refresh(self, key: str, event: Any) -> None:
        """发送前补一帧，别让状态在「回复已生成、还没发出去」的空档里断掉。

        只在这一轮真的显示过（start 过且没过期）时才补——否则指令类回复、别的插件
        的结果也会平白闪一下「正在输入」。
        """
        if not key:
            return
        last = self._last_active.get(key)
        if last is None or (time.monotonic() - last) > 60.0:
            return
        ok, user_id = self.is_supported(event)
        if not ok:
            return
        client = getattr(getattr(event, "bot", None), "api", None)
        if client is None:
            return
        await self._report(client, user_id)

    async def stop(self, key: str) -> None:
        entry = self._tasks.pop(key, None)
        if entry is None:
            self._last_active.pop(key, None)
            return
        task, stop_event = entry
        stop_event.set()
        if not task.done():
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # pragma: no cover - 兜底
                self._log.debug(f"[humanoid_core] 输入状态任务收尾异常: {exc}")
        self._last_active[key] = time.monotonic()

    async def stop_all(self) -> None:
        for key in list(self._tasks.keys()):
            await self.stop(key)
        self._last_active.clear()

    # ------------------------------------------------------------------

    async def _run(self, key: str, client: Any, user_id: str, stop_event: asyncio.Event) -> None:
        deadline = time.monotonic() + self.timeout
        try:
            while not stop_event.is_set():
                self._last_active[key] = time.monotonic()
                await self._report(client, user_id)
                if time.monotonic() >= deadline:
                    self._log.debug(f"[humanoid_core] 输入状态上报超时（{self.timeout:.0f}s），停止")
                    return
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.interval)
                    return
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - 纯装饰，不往上抛
            self._log.debug(f"[humanoid_core] 输入状态上报失败: {exc}")
        finally:
            # 只清掉**还是自己**的那条记录：stop() 与 start() 可能交织，
            # 不加这个判断的话，旧任务的收尾会把刚起的新任务记录一起删掉，
            # 新任务就再也没人能停了（只能等 timeout）。
            current = asyncio.current_task()
            entry = self._tasks.get(key)
            if entry is not None and entry[0] is current:
                self._tasks.pop(key, None)
                self._last_active.pop(key, None)

    async def _report(self, client: Any, user_id: str) -> None:
        try:
            await client.call_action("set_input_status", user_id=str(user_id), event_type=1)
        except Exception as exc:
            self._log.debug(f"[humanoid_core] set_input_status 失败（可能不是 NapCat）: {exc}")
            raise


def _is_private(event: Any) -> bool:
    checker = getattr(event, "is_private_chat", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return True
    getter = getattr(event, "get_group_id", None)
    if callable(getter):
        try:
            return not bool(getter())
        except Exception:
            return True
    return True
