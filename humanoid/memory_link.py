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

**读结果做短缓存**：注入在每条消息的热路径上，缓存键是「会话 + 人 + 这句话的内容摘要」，
所以同一个人的**同一句话** 60 秒内只查一次；换一句话就重新检索（记忆要随这句话变，
只按会话缓存会让一分钟内的第二条消息拿到第一条的检索结果）。省的是记忆插件那边的
检索开销，不是省这里的一行代码。
"""

from __future__ import annotations

import asyncio
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

# 跨插件调用必须有上限（P1-6）：注入挂在 @filter.on_llm_request 上，对**每条消息**执行。
# 对面检索一次卡住（死锁/在等网络），没有超时的 await 会把本插件所有回复一起挂住——
# 表现是「装死」而不是报错，用户完全看不出方向。
# 默认值取 0.5s：长期记忆是加分项，不值得为它让用户多等；写回复走 1.0s（更宽，但同样有界）。
READ_TIMEOUT_SECONDS = 0.5
WRITE_TIMEOUT_SECONDS = 1.0
#: 上限：调用方即使传了更大的值也夹到这里，防止「可配」变成「无限等」。
MAX_TIMEOUT_SECONDS = 2.0

# 失败原因分开记：区分「对方插件没装」「装了但坏了」「卡住了」，排查时一眼看得出方向。
FAIL_MISSING = "missing"      # 注册表里没有它（没装 / 宿主没有注册表 API）
FAIL_SIGNATURE = "signature"  # 找到了它，但桥接工厂或所需方法不可调用（接口对不上）
FAIL_EXCEPTION = "exception"  # 注册表、工厂或桥接方法调用时抛了异常
FAIL_TIMEOUT = "timeout"      # 桥接方法在限定时间内没返回（对面卡住/死锁）
# 对面装了、也激活了，但把桥接开关关了（`private_companion_bridge.enabled`）。
# 这是**用户在配置页上做的一个选择**，不是故障——降级到「没有记忆」是预期行为。
FAIL_DISABLED = "disabled"
# 对面**还没就绪**：装好了但尚未激活完成 / 桥接对象还没建出来。
# 这是启动时序，不是用户的选择，也不是没装——下一轮可能就好了，所以单列一类。
FAIL_NOT_READY = "not_ready"

# 连续读超时到第 N 次就熔断一个窗口：对面卡死时，每条消息都白等满 0.5 秒，
# 而拿到的仍然是空串。熔断期内直接返回空串，不再进 await。
TIMEOUT_STREAK_LIMIT = 3
TIMEOUT_COOLDOWN_SECONDS = 60.0
_TIMEOUT_STREAK = 0
_SKIP_UNTIL = 0.0

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
    """**当前**记忆桥的故障原因（含类别）；空串表示此刻没有故障。

    语义是「现在是否还坏着」，不是「历史上最近一次失败」：以前只写不清，一次超时
    就会在 /拟人 诊断 里挂到天荒地老，用户看到的是陈年故障、而实际早就恢复了。
    历史故障仍可以从日志里找（失败日志按类别限流后打在那里）。
    """
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
        if reason in (FAIL_MISSING, FAIL_DISABLED, FAIL_NOT_READY):
            logger.debug(message)
        else:
            logger.warning(message)
    except Exception:  # 日志本身失败绝不能反过来影响注入链路
        pass


def _note_success() -> None:
    """一次成功的桥接调用：把「当前故障」清掉。

    与 `_note_bridge_ok` 分工不同：那个只清**连续超时计数**（熔断用），这个清
    `last_error`（诊断用）。成功一次就证明对面此刻是通的，陈旧故障不该继续挂在诊断里。
    """
    global _LAST_ERROR
    _LAST_ERROR = ""


def _brief(exc: BaseException, limit: int = 160) -> str:
    """异常一句话（带类型、截断）：够定位问题，又不把整段内容抄进日志。"""
    try:
        text = f"{type(exc).__name__}: {exc}"
    except Exception:  # 连 __str__ 都坏了的异常
        text = type(exc).__name__
    return text if len(text) <= limit else text[:limit] + "…"


def _clamp_timeout(timeout: Any, default: float) -> float:
    """把调用方给的超时夹进 (0, MAX_TIMEOUT_SECONDS]；坏值退回默认。

    跨插件等待不允许「配成无限」：桥接是注入链路的一部分，等太久等于把所有回复一起挂住。
    """
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        value = float(default)
    if not (value > 0):
        value = float(default)
    return min(value, MAX_TIMEOUT_SECONDS)


def _text_of(value: Any) -> str:
    try:
        return str(value or "").strip()
    except Exception:
        return ""


def _identity_matches(meta: Any) -> bool:
    try:
        # 只比三个**短名**域：name / display_name / root_dir_name。
        # `module_path` 是文件系统路径（如 .../data/plugins/astrbot_plugin_x），
        # 取值域与这三个短名不相交，放进集合里永远为假——留着只会让人以为它能匹配。
        names = {
            _text_of(getattr(meta, attr, ""))
            for attr in ("name", "display_name", "root_dir_name")
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
        # 工厂返回 None 只说明 `_ACTIVE_BRIDGE` 是空的：既可能是**对面的配置开关**
        # 关掉了（`private_companion_bridge.enabled`），也可能是**对面还没就绪**
        # （装好了但没激活完 / initialize 还没跑到建桥接那一步）。
        # 两者对用户的意义完全相反：前者要去配置页打开，后者什么都别做、下一轮会自愈。
        # 判据与 find_memory_bridge 的中间态一致：看 star 元信息里的 activated。
        if not bool(getattr(meta, "activated", True)):
            _note_failure(
                FAIL_NOT_READY,
                "memory_companion 已安装但尚未激活完成，桥接对象还没建出来"
                "（启动时序问题，不是配置问题；下一轮会再试）",
            )
        else:
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
            # 装好了但还没激活完：这是时序，不是「没装」，也不该让人去翻安装步骤。
            _note_failure(
                FAIL_NOT_READY,
                "memory_companion 已安装但尚未激活完成（启动时序问题，下一轮会再试）",
            )
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
    """清空读缓存（连带超时熔断状态）。测试与「换人/换会话」的场景用得上。"""
    _READ_CACHE.clear()
    _reset_timeout_breaker()


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


def _reset_timeout_breaker() -> None:
    """清零熔断状态（清缓存/测试隔离时用）。"""
    global _TIMEOUT_STREAK, _SKIP_UNTIL
    _TIMEOUT_STREAK = 0
    _SKIP_UNTIL = 0.0


def _trip_after_timeout(now: float) -> bool:
    """记一次读超时；连续到上限就打开熔断窗口，返回是否刚打开。"""
    global _TIMEOUT_STREAK, _SKIP_UNTIL
    _TIMEOUT_STREAK += 1
    if _TIMEOUT_STREAK < TIMEOUT_STREAK_LIMIT:
        return False
    _TIMEOUT_STREAK = 0
    _SKIP_UNTIL = now + TIMEOUT_COOLDOWN_SECONDS
    return True


def _note_bridge_ok() -> None:
    """一次成功调用就清零超时计数：连续超时才熔断，偶发一次不算。"""
    global _TIMEOUT_STREAK
    _TIMEOUT_STREAK = 0


async def fetch_memory_text(
    bridge: Any,
    umo: str,
    uid: str,
    *,
    query: str = "",
    is_group: bool = False,
    max_chars: int = READ_MAX_CHARS,
    top_k: int = READ_TOP_K,
    timeout: float = READ_TIMEOUT_SECONDS,
) -> str:
    """拉这个人相关的长期记忆（拼好的注入文本）；拿不到返回空串。

    带 60 秒缓存：同一个会话连着几条消息只查一次记忆库。

    **等待有上限**（P1-6）：调用点在 @filter.on_llm_request 里、对每条消息执行，对面
    检索卡住时这里必须放弃而不是一直等。超时按「这一轮没有记忆」降级，下一轮重试。
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
    if now < _SKIP_UNTIL:
        # 熔断中：对面连续超时，这一轮连 await 都不进。
        # 用户感知是「记忆暂时缺席，但回复不再陪等 0.5 秒」，窗口过后自动重试。
        return ""

    fn = getattr(bridge, "compose_context", None)
    if not callable(fn):
        _note_failure(FAIL_SIGNATURE, "桥接对象没有可调用的 compose_context（接口签名不符）")
        return ""
    limit = _clamp_timeout(timeout, READ_TIMEOUT_SECONDS)
    try:
        text = await asyncio.wait_for(
            fn(
                query=str(query or ""),
                session_context=_session_context(umo, uid, is_group),
                top_k=int(top_k),
                max_chars=int(max_chars),
            ),
            timeout=limit,
        )
    except asyncio.TimeoutError:
        # 超时不进缓存：对面下一轮可能就恢复了，负缓存会把记忆永久判死。
        _note_failure(
            FAIL_TIMEOUT,
            f"compose_context() 超过 {limit:g}s 未返回，已放弃本轮记忆（对面可能卡住了）",
        )
        _trip_after_timeout(now)
        return ""
    except Exception as exc:
        _note_failure(FAIL_EXCEPTION, f"compose_context() 调用失败：{_brief(exc)}")
        return ""
    _note_bridge_ok()
    _note_success()
    out = _text_of(text)
    _remember(key, out, now)
    return out


def _content_digest(text: str) -> str:
    """内容摘要（跨进程稳定：内置 hash() 按进程加盐，不能用来做幂等键）。"""
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def _idempotency_key(
    umo: str, payload: str, key: str, occurred_at: float = 0.0
) -> str:
    """幂等键：调用方给了稳定键就用它，否则退回内容摘要。

    稳定键必须由调用方按「这件事是什么 + 它什么时候发生的」算出来，不能用 `now()`——
    用当下时刻的话，同一条记录重放一次就是一条新记忆。

    `occurred_at` 是「这一次发生」的时刻（秒级，可选）：光有稳定键不够——同一件事
    第二次发生时稳定键一模一样，会被对面当成重放丢掉，于是「这件事再发生一次」
    永远写不进记忆库。带上发生时刻，同一次重放（时刻不变）仍然幂等，不同次则分开。
    没给时刻就只用稳定键（旧行为，兼容旧调用方）。
    """
    stable = _text_of(key)
    stamp = ""
    try:
        value = float(occurred_at or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    if value > 0:
        stamp = f":{int(value)}"
    if stable:
        return f"life:{umo}:{stable}{stamp}"
    return f"life:{umo}:c:{_content_digest(payload)}{stamp}"


async def write_life_event(
    bridge: Any,
    uid: str,
    content: str,
    *,
    umo: str = "",
    key: str = "",
    occurred_at: float = 0.0,
    importance: float = 0.5,
    timeout: float = WRITE_TIMEOUT_SECONDS,
) -> bool:
    """把一件「她自己的、聊天里看不见的事」写进长期记忆；失败只记日志。

    返回**是否真的写成功**：这个函数内部吞掉异常（写记忆失败不该影响聊天），所以
    调用方拿不到异常，只能靠返回值判断。以前它返回 None，调用方无从区分「写进去了」
    与「悄悄失败了」，于是「失败不记账、稍后重试」的逻辑永远走不到。

    写的是**事件**，不是她的状态数值：数值下一轮就变了，写进去只是垃圾。

    `occurred_at`：这一次发生的时刻（可选）。同一件事发生第二次时内容与稳定键都一样，
    不给时刻就会被对面当成重放丢掉——那正是「第二次发生写不进去」的成因。

    写回同样有上限（P1-6）：它被注入链路 await，对面写库卡住不能连带把回复挂住。
    """
    text = _text_of(content)
    if bridge is None or not text:
        return False
    fn = getattr(bridge, "record_external_memory", None)
    if not callable(fn):
        _note_failure(
            FAIL_SIGNATURE, "桥接对象没有可调用的 record_external_memory（接口签名不符）"
        )
        return False
    payload = f"（她自己生活里的事）{text}"
    limit = _clamp_timeout(timeout, WRITE_TIMEOUT_SECONDS)
    try:
        await asyncio.wait_for(
            fn(
                user_id=str(uid or ""),
                content=payload,
                source_plugin=SOURCE_PLUGIN,
                idempotency_key=_idempotency_key(umo, payload, key, occurred_at),
                importance=float(importance),
                tags=["life"],
            ),
            timeout=limit,
        )
    except asyncio.TimeoutError:
        _note_failure(
            FAIL_TIMEOUT,
            f"record_external_memory() 超过 {limit:g}s 未返回，已放弃本次写回",
        )
        return False
    except Exception as exc:
        _note_failure(FAIL_EXCEPTION, f"record_external_memory() 调用失败：{_brief(exc)}")
        return False
    # 写成功同样证明对面此刻是通的：清掉之前读阶段留下的「当前故障」。
    _note_success()
    return True
