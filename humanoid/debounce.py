"""消息合并的状态机（防抖）。

参考 `astrbot_plugin_debounce` 的整体做法——攒进 buffer、判完整性、合并成一次请求——
但有两处刻意的不同，都是为了让这个方案"不会神经病"：

1. **判定不用模型**。它用 BERT 分类器（`onnxruntime + transformers + modelscope` 四个依赖，
   模型还得运行时从 ModelScope 拉）。这里用 `merge.judge()` 的纯规则：零依赖、零延迟，
   而且没有"模型拉不到 → catch 住异常 → 防抖静默失效"这个失败模式。

2. **不伪造事件、不丢弃在途响应**。它在 `on_llm_request` 里 stop 掉没说完的消息，等超时后
   用 `StarTools.create_event` **伪造一条新消息**重新投给事件总线；响应回来时还要判断该不该
   丢弃。本插件不这么做：本状态机只在 `on_waiting_llm_request`（**抢会话锁之前**）里工作，
   拿不准时就在这里等。被合并的旧消息走的是 `stop_event()`，**内容始终留在 buffer 里**，
   由胜出的那条带走；超时则由当前这条自己带出去。消息永远不会被"停在半路"。

代价：拿不准的那一类消息要等 `message_merge_timeout_seconds`（默认 3 秒）。
换来的是不会被切开，也不会丢。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .merge import judge, join, normalize

# 会话上限：防止 umo 异常时无限增长。
SESSION_CAP = 512
# 多久没动静就把一个会话从表里清掉（秒）。有 buffer 的会话不按这个清——那里面有等着发的内容。
SESSION_IDLE_SECONDS = 900.0
# 「只是攒够条数」时的确认窗口上限（秒）：再看一眼还有没有后续。用户逐字连发
# （「你」「今天」「我好累啊」）时，攒够两条就发等于把一句切成两半。
SETTLE_CAP_SECONDS = 0.6

# 挂到事件上的键：合并进来的更早几条原话。
MERGE_EXTRA_KEY = "humanoid_merge_earlier"
# 挂到事件上的键：合并后的完整原话（含胜出者自己那条）。注入层用它算注意力，
# 否则前几条说的问句/话题不参与判断，注意力会被算低。
MERGE_TEXT_EXTRA_KEY = "humanoid_merge_text"


@dataclass
class _Session:
    """一个会话的合并状态。`seq` 是单调递增的入场序号，用来判「我是不是最后一条」。"""

    seq: int = 0
    buffer: List[str] = field(default_factory=list)
    last_active: float = field(default_factory=time.monotonic)


class Debouncer:
    """把连续几条消息并成一次请求。只在 `on_waiting_llm_request` 里动手。"""

    def __init__(self, config_provider: Callable[[], Any], logger: Any = None) -> None:
        self._config = config_provider
        self._log = logger
        self._sessions: Dict[str, _Session] = {}

    # ------------------------------------------------------------------

    def reset(self) -> None:
        """清空全部缓冲（重置/重载配置时用）。"""
        self._sessions.clear()

    def _sweep(self) -> None:
        """丢掉长时间没动静的空会话，防 umo 异常把表撑爆。"""
        if len(self._sessions) <= SESSION_CAP:
            return
        now = time.monotonic()
        for key, sess in list(self._sessions.items()):
            if sess.buffer:
                continue
            if now - sess.last_active > SESSION_IDLE_SECONDS:
                self._sessions.pop(key, None)
        # 还是超量（说明有大量会话正在攒内容）：再按最旧的清，buffer 也一起丢。
        while len(self._sessions) > SESSION_CAP:
            oldest = min(self._sessions.items(), key=lambda kv: kv[1].last_active)[0]
            self._sessions.pop(oldest, None)

    def _debug(self, message: str) -> None:
        if self._log is not None:
            try:
                self._log.debug(f"[humanoid_core] 消息合并: {message}")
            except Exception:
                pass

    # ------------------------------------------------------------------

    async def hold(self, event: Any, *, key: str, text: str, stop_event: Callable[[], None]) -> str:
        """处理一条「即将调 LLM」的消息。

        返回值是**这条消息要不要继续往下走**：False 表示已经被合并掉了（调用方直接返回）。

        `key` 是会话标识（`unified_msg_origin`）。调用方要保证配置里 `message_merge_enabled`
        为真、消息有正文、且属于生效范围——这三条都判断过再进来。
        """
        cfg = self._config()
        timeout = max(0.0, float(getattr(cfg, "message_merge_timeout_seconds", 0.0) or 0.0))
        max_count = max(1, int(getattr(cfg, "message_merge_max_count", 6) or 6))
        body = normalize(text)
        if not body:
            return True

        sess = self._sessions.setdefault(key, _Session())
        sess.seq += 1
        my_seq = sess.seq
        sess.buffer.append(body)
        sess.last_active = time.monotonic()
        if len(sess.buffer) > max_count:
            del sess.buffer[:-max_count]
        self._sweep()

        verdict = judge(join(sess.buffer, limit=max_count), buffer_len=len(sess.buffer))
        if verdict.complete and verdict.sure:
            self._release(event, sess, max_count)
            self._debug(f"直接放行（{verdict.why}）：{body[:20]}")
            return True

        # 走到这里只有两种情况：没判定完（「肯定没说完」或「拿不准」），或者只是攒够了条数。
        # 等多久：攒够条数的只等一个短的确认窗口（用户可能还在一个字一个字发），
        # 其它等满 timeout。
        wait = min(SETTLE_CAP_SECONDS, timeout) if verdict.complete else timeout
        if wait <= 0:
            self._release(event, sess, max_count)
            self._debug(f"不等待直接放行（{verdict.why}）：{body[:20]}")
            return True

        self._debug(f"等一下再发（{verdict.why}，最多 {wait}s）：{body[:20]}")
        try:
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            raise
        if sess.seq != my_seq:
            # 等待期间又来了新消息：它会把我的内容一起带走，我不必再回。
            stop_event()
            self._debug("等待期间来了新消息，这条交给后来者合并")
            return False
        # 等到现在也没人来：内容都在我这条的 buffer 里，我自己带出去。
        self._release(event, sess, max_count)
        self._debug("等完没等到人，自己发出")
        return True

    def _release(self, event: Any, sess: _Session, max_count: int) -> None:
        """把 buffer 清空，并把合并结果挂到事件上供后面用。"""
        batch = sess.buffer
        sess.buffer = []
        sess.last_active = time.monotonic()
        if not batch:
            return
        earlier = batch[:-1]
        setter = getattr(event, "set_extra", None)
        if not callable(setter):
            return
        try:
            if earlier:
                setter(MERGE_EXTRA_KEY, list(earlier))
            setter(MERGE_TEXT_EXTRA_KEY, join(batch, limit=max_count))
        except Exception:
            pass

    def peek_text(self, event: Any, fallback: str) -> str:
        """取这条消息「实际说出口的全貌」：有被合并的前文就用合并后的完整文本。

        注入层算注意力（上心程度）要看用户到底说了什么——只看胜出那一条会低估。
        """
        getter = getattr(event, "get_extra", None)
        if callable(getter):
            try:
                merged = getter(MERGE_TEXT_EXTRA_KEY)
            except Exception:
                merged = None
            if isinstance(merged, str) and merged.strip():
                return merged
        return fallback
