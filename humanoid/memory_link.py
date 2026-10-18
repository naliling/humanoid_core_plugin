"""与 `astrbot_plugin_memory_companion`（我会牢牢记住你）的联动。

核心插件管的是「她此刻的身体与生活」。长期事实那条线原本是空的：她能拿到
「手上还开着的事」（`life_log`），但拿不到「这个人以前说过什么」的长期沉淀。
这里补上这一环，两个方向各做一件事：

- **生成前**：按 TA 这条消息去记忆库要几条相关长期记忆，并进她的事实块；
- **事后**：她自己的生活过程真正结束时写一条回去——那是聊天里看不见的事，
  记忆插件自己读不到，只有她知道（过程一结束，`ongoing_tasks` 也再报不出它）。

**拿桥接对象必须查宿主注册表，不能 importlib。** AstrBot 不保证插件模块以某个别名
留在 `sys.modules` 里，而 `import_module` 在没装时会凭空造一个空模块（「没装却说装了」）。
注册表可用却没找到 = 真的没装，到此为止，不再翻 `sys.modules`。写法与 memory_companion
自己的 `core/capability_probe.py`、社交插件的 `social/memory_bridge.py` 一致。

没装 memory_companion 时全部返回空 / 不写，注入与生活照常，只是没有记忆可用。

**读结果做短缓存**：注入在每条消息的热路径上，同一个人的记忆不会因为对方连着发三条
就变得不一样，所以同一个会话 60 秒内只查一次——省的是记忆插件那边的检索开销，
不是省这里的一行代码。
"""

from __future__ import annotations

import hashlib
import sys
import time
from typing import Any, Dict, Optional

from astrbot.api import logger

MEMORY_PLUGIN_ID = "astrbot_plugin_memory_companion"
# 宿主注册表里可能出现这些身份（name / display_name / 目录名）。
MEMORY_PLUGIN_NAMES = {
    MEMORY_PLUGIN_ID,
    "MemoryCompanion",
    "我会牢牢记住你",
}

#: 写回时声明自己是谁——记忆库里要能分清哪条是社交层写的、哪条是核心层写的。
SOURCE_PLUGIN = "astrbot_plugin_humanoid_core"

#: 读回来的文本上限。核心的事实块预算比主动消息紧得多（medium 档整块才 2000 字符），
#: 记忆是「补充」，不能把身体与生活那几句挤掉。
READ_MAX_CHARS = 240
READ_TOP_K = 3
#: 同一个会话多久内不重复查记忆库。
READ_CACHE_TTL_SECONDS = 60.0
#: 缓存条目上限：超过就整体清空，比按 LRU 淘汰简单，且这里的数据全部可重建。
READ_CACHE_MAX_ENTRIES = 256

# 三类失败原因分开记：区分「对方插件没装」与「装了但坏了」，排查时一眼看得出方向。
FAIL_MISSING = "missing"      # 注册表里没有它（没装 / 未激活 / 宿主没有注册表 API）
FAIL_SIGNATURE = "signature"  # 找到了它，但桥接工厂或所需方法不可调用（接口对不上）
FAIL_EXCEPTION = "exception"  # 注册表、工厂或桥接方法调用时抛了异常
# 对面装了、也激活了，但把桥接开关关了（`private_companion_bridge.enabled`）。
# 这是**用户在配置页上做的一个选择**，不是故障——降级到「没有记忆」是预期行为。
FAIL_DISABLED = "disabled"

_THROTTLE_WINDOW_SECONDS = 600.0
#: 最近一次失败分类 + 原因；空串表示这轮没出过问题。
_LAST_ERROR = ""


class _Throttle:
    """同类失败一个窗口内只报一条日志，末尾附上被省略的条数。

    没有复用社交插件那套 `LogThrottle`：两个插件各自独立发布，跨包 import 会在
    「只装了一个」的部署里当场炸掉。这里只需要「限流 + 汇总」两件事。
    """

    def __init__(self, window: float = _THROTTLE_WINDOW_SECONDS) -> None:
        self._window = float(window)
        self._last: Dict[str, float] = {}
        self._skipped: Dict[str, int] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        last = self._last.get(key)
        if last is not None and now - last < self._window:
            self._skipped[key] = self._skipped.get(key, 0) + 1
            return False
        self._last[key] = now
        return True

    def summary(self, key: str) -> str:
        count = self._skipped.pop(key, 0)
        return f"（同类失败另有 {count} 次未记录）" if count else ""


_THROTTLE = _Throttle()
#: (umo, uid) -> (文本, 写入时刻)。文本为空串也缓存：没记忆的人不必每轮再问一次。
_READ_CACHE: Dict[tuple, tuple] = {}


def last_error() -> str:
    """最近一次记忆桥失败的原因（含类别）；空串表示没有失败记录。"""
    return _LAST_ERROR


def _note_failure(reason: str, detail: str) -> None:
    """记一次失败：留痕（`last_error`）总是更新，日志按同类窗口限流。

    「没装」和「对面关了桥接」都是正常降级，走 debug；
    「签名不符」「调用抛异常」是真故障，走 warning。
    """
    global _LAST_ERROR
    _LAST_ERROR = f"{reason}: {detail}"
    if not _THROTTLE.allow(reason):
        return
    message = (
        f"[humanoid_core] memory_companion 桥接不可用（{reason}）：{detail}"
        + _THROTTLE.summary(reason)
    )
    try:
        if reason in (FAIL_MISSING, FAIL_DISABLED):
            logger.debug(message)
        else:
            logger.warning(message)
    except Exception:  # 日志本身失败绝不能反过来影响注入链路
        pass


def _brief(exc: BaseException, limit: int = 160) -> str:
    """异常一句话（带类型、截断）：够定位问题，又不把整段内容抄进日志。"""
    try:
        text = f"{type(exc).__name__}: {exc}"
    except Exception:  # 连 __str__ 都坏了的异常
        text = type(exc).__name__
    return text if len(text) <= limit else text[:limit] + "…"


def _text_of(value: Any) -> str:
    try:
        return str(value or "").strip()
    except Exception:
        return ""


def _identity_matches(meta: Any) -> bool:
    try:
        names = {
            _text_of(getattr(meta, attr, ""))
            for attr in ("name", "display_name", "root_dir_name", "module_path")
        }
    except Exception:
        return False
    return bool(names & MEMORY_PLUGIN_NAMES)


def _module_of(meta: Any) -> Any:
    module = getattr(meta, "module", None)
    if module is not None:
        return module
    star_cls = getattr(meta, "star_cls", None)
    mod_name = getattr(star_cls, "__module__", "") if star_cls is not None else ""
    return sys.modules.get(mod_name) if mod_name else None


def _bridge_from(meta: Any) -> Optional[Any]:
    factory = getattr(_module_of(meta), "get_memory_companion_bridge", None)
    if not callable(factory):
        _note_failure(
            FAIL_SIGNATURE,
            "已找到 memory_companion，但没有可调用的 get_memory_companion_bridge 工厂",
        )
        return None
    try:
        bridge = factory()
    except Exception as exc:
        _note_failure(FAIL_EXCEPTION, f"get_memory_companion_bridge() 抛异常：{_brief(exc)}")
        return None
    if bridge is None:
        # 工厂返回 None 只说明 `_ACTIVE_BRIDGE` 是空的，而那个变量由**对面的配置开关**
        # 决定：`private_companion_bridge.enabled` 关掉时它被设成 None（见它 main.py
        # 里那行赋值）。所以这里不是「接口签名不符」，而是「对面没开桥接」——用户自己
        # 的选择。按 WARNING 报会在日志里刷成故障，按 debug 记并把开关名写出来。
        _note_failure(
            FAIL_DISABLED,
            "memory_companion 的桥接开关是关的（配置页「记忆插件协同 → 启用桥接 API」"
            "= private_companion_bridge.enabled）；要用记忆就把那个开关打开",
        )
    return bridge if bridge is not None else None


def find_memory_bridge(context: Any) -> Optional[Any]:
    """返回 memory_companion 的桥接对象；没装 / 没就绪返回 None。"""
    if context is None:
        return None
    get_all_stars = getattr(context, "get_all_stars", None)
    get_registered_star = getattr(context, "get_registered_star", None)
    if not (callable(get_all_stars) or callable(get_registered_star)):
        # 老 AstrBot / 单测：没有注册表 API 就当作没装，绝不 importlib 造模块。
        _note_failure(
            FAIL_MISSING, "宿主没有 get_all_stars/get_registered_star，无法确认对面是否安装"
        )
        return None

    seen: set = set()
    saw_identity = False
    lookup_failed = False

    def inspect(meta: Any) -> Optional[Any]:
        nonlocal saw_identity
        if meta is None or id(meta) in seen:
            return None
        seen.add(id(meta))
        if not _identity_matches(meta):
            return None
        saw_identity = True
        if not bool(getattr(meta, "activated", True)):
            _note_failure(FAIL_MISSING, "memory_companion 已安装但未激活")
            return None
        return _bridge_from(meta)

    if callable(get_all_stars):
        try:
            stars = list(get_all_stars() or [])
        except Exception as exc:
            lookup_failed = True
            _note_failure(FAIL_EXCEPTION, f"get_all_stars() 抛异常：{_brief(exc)}")
            stars = []
        for meta in stars:
            bridge = inspect(meta)
            if bridge is not None:
                return bridge
    if callable(get_registered_star):
        try:
            meta = get_registered_star(MEMORY_PLUGIN_ID)
        except Exception as exc:
            lookup_failed = True
            _note_failure(FAIL_EXCEPTION, f"get_registered_star() 抛异常：{_brief(exc)}")
            meta = None
        bridge = inspect(meta)
        if bridge is not None:
            return bridge
    if not saw_identity and not lookup_failed:
        # 注册表可用却查无此人 = 真的没装。查不动时不再补一条误导性的「没装」。
        _note_failure(FAIL_MISSING, "宿主注册表里没有 memory_companion（没装）")
    return None


def _session_context(umo: str, uid: str, is_group: bool = False) -> Dict[str, Any]:
    """拼一个 memory_companion 认的会话上下文（它同时接受 dict）。"""
    return {
        "session_id": str(umo or ""),
        "scope": "group" if is_group else "private",
        "user_id": str(uid or ""),
        "platform": str(umo or "").split(":", 1)[0],
    }


def clear_cache() -> None:
    """清空读缓存。测试与「换人/换会话」的场景用得上。"""
    _READ_CACHE.clear()


def _cached(key: tuple, now: float) -> Optional[str]:
    hit = _READ_CACHE.get(key)
    if hit is None:
        return None
    text, at = hit
    if now - float(at) > READ_CACHE_TTL_SECONDS:
        _READ_CACHE.pop(key, None)
        return None
    return text


def _remember(key: tuple, text: str, now: float) -> None:
    if len(_READ_CACHE) >= READ_CACHE_MAX_ENTRIES:
        # 全部可重建，整体清空比淘汰算法便宜，也不会留下过期条目。
        _READ_CACHE.clear()
    _READ_CACHE[key] = (text, now)


async def fetch_memory_text(
    bridge: Any,
    umo: str,
    uid: str,
    *,
    query: str = "",
    is_group: bool = False,
    max_chars: int = READ_MAX_CHARS,
    top_k: int = READ_TOP_K,
) -> str:
    """拉这个人相关的长期记忆（拼好的注入文本）；拿不到返回空串。

    带 60 秒缓存：同一个会话连着几条消息只查一次记忆库。
    """
    if bridge is None or not str(uid or "").strip():
        return ""
    # 缓存键必须带上 query：检索是**按 TA 这句话**做的（方案①），只按会话缓存会让
    # 一分钟内的第二条消息拿到第一条消息的检索结果——「按当前消息检索」当场失效。
    # 摘要进键是为了让键长有界：消息本身可以是几千字。
    key = (str(umo or ""), str(uid or ""), _content_digest(str(query or "")))
    now = time.monotonic()
    hit = _cached(key, now)
    if hit is not None:
        return hit

    fn = getattr(bridge, "compose_context", None)
    if not callable(fn):
        _note_failure(FAIL_SIGNATURE, "桥接对象没有可调用的 compose_context（接口签名不符）")
        return ""
    try:
        text = await fn(
            query=str(query or ""),
            session_context=_session_context(umo, uid, is_group),
            top_k=int(top_k),
            max_chars=int(max_chars),
        )
    except Exception as exc:
        _note_failure(FAIL_EXCEPTION, f"compose_context() 调用失败：{_brief(exc)}")
        return ""
    out = _text_of(text)
    _remember(key, out, now)
    return out


def _content_digest(text: str) -> str:
    """内容摘要（跨进程稳定：内置 hash() 按进程加盐，不能用来做幂等键）。"""
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def _idempotency_key(umo: str, payload: str, key: str) -> str:
    """幂等键：调用方给了稳定键就用它，否则退回内容摘要。

    稳定键必须由调用方按「这件事是什么 + 它什么时候发生的」算出来，不能用 `now()`——
    用当下时刻的话，同一条记录重放一次就是一条新记忆。
    """
    stable = _text_of(key)
    if stable:
        return f"life:{umo}:{stable}"
    return f"life:{umo}:c:{_content_digest(payload)}"


async def write_life_event(
    bridge: Any,
    uid: str,
    content: str,
    *,
    umo: str = "",
    key: str = "",
    importance: float = 0.5,
) -> None:
    """把一件「她自己的、聊天里看不见的事」写进长期记忆；失败只记日志。

    写的是**事件**，不是她的状态数值：数值下一轮就变了，写进去只是垃圾。
    """
    text = _text_of(content)
    if bridge is None or not text:
        return
    fn = getattr(bridge, "record_external_memory", None)
    if not callable(fn):
        _note_failure(
            FAIL_SIGNATURE, "桥接对象没有可调用的 record_external_memory（接口签名不符）"
        )
        return
    payload = f"（她自己生活里的事）{text}"
    try:
        await fn(
            user_id=str(uid or ""),
            content=payload,
            source_plugin=SOURCE_PLUGIN,
            idempotency_key=_idempotency_key(umo, payload, key),
            importance=float(importance),
            tags=["life"],
        )
    except Exception as exc:
        _note_failure(FAIL_EXCEPTION, f"record_external_memory() 调用失败：{_brief(exc)}")
