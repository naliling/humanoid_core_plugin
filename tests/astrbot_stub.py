"""测试用的 AstrBot / aiohttp 最小替身。

`main.py` 是插件与框架的接缝，它一旦写坏（装饰器名字改了、钩子签名变了）整份插件就加载不了，
所以这一层也必须被测到。真实环境里没有 astrbot 时，这里补上它真正被用到的那点表面：
装饰器只登记不执行、ProviderRequest 是个可写属性的空壳、logger 收集输出。
"""

from __future__ import annotations

import logging
import sys
import types
from typing import Any, Callable


class _Collector:
    """`astrbot.api.logger` 的替身：只收集，不输出。

    真实 AstrBot 的 `astrbot.api.logger` 最终落到标准库 `logging.Logger`
    （`astrbot/core/log.py` 的 `LogManager.GetLogger(...) -> logging.Logger`），
    所以 `%s` / `%r` 惰性格式化、`exc_info=True`、`extra=` 这些关键字参数都得照单全收——
    业务模块把 `logger` 换成 astrbot 的那一个之后，调用写法原样不变才不会炸在这里。
    """

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def _add(self, level: str, *args: Any, **_kwargs: Any) -> None:
        self.lines.append((level, " ".join(str(a) for a in args)))

    def debug(self, *a, **k): self._add("debug", *a, **k)
    def info(self, *a, **k): self._add("info", *a, **k)
    def warning(self, *a, **k): self._add("warning", *a, **k)
    def error(self, *a, **k): self._add("error", *a, **k)
    def critical(self, *a, **k): self._add("error", *a, **k)
    def exception(self, *a, **k): self._add("error", *a, **k)

    def text(self, level: str = "") -> str:
        return "\n".join(t for lv, t in self.lines if not level or lv == level)


class _PluginLoggerProxy:
    """`astrbot.api.logger` 的替身。

    真实实现（`astrbot/api/__init__.py`）是个**按调用方模块名解析**的代理：
    `_PluginContextLogger.__getattr__` 用 `sys._getframe(1)` 取调用者的 `__name__`，
    再经 `LogManager.GetLogger` / `LogManager.get_plugin_logger` 落到一个标准库
    `logging.Logger`（`astrbot/core/log.py` 里两者的返回类型注解就是 `logging.Logger`）。

    这里照抄同一套机制，只把解析结果换成 `logging.getLogger(调用方模块名)`：
    业务模块 `logger.warning(...)` 的写法、`%s`/`%r` 惰性格式化、`exc_info=True`
    全都与线上一致，测试里的 `assertLogs("humanoid.config")` 也依然抓得到——
    这正是这些模块改用 `from astrbot.api import logger` 之前的行为。
    """

    def __getattr__(self, item: str):
        module_name = sys._getframe(1).f_globals.get("__name__", "") or "astrbot"
        return getattr(logging.getLogger(module_name), item)


PLUGIN_LOGGER = _PluginLoggerProxy()
"""`astrbot.api.logger` 的替身实例。与真实实现一样无状态，全进程共用一个即可。"""


class PermissionType:
    ADMIN = "admin"
    USER = "user"


class EventMessageType:
    ALL = "all"
    TEXT = "text"


class _GroupHandle:
    """`@filter.command_group` 装饰后的产物：仍是个可调用对象（组本身没行为），
    同时挂着 `.command()` / `.group()` 给子命令用。"""

    def __init__(self, func, factory: "_GroupFactory") -> None:
        self._func = func
        self._factory = factory

    def command(self, name: str = "", **kw):
        return self._factory.command(name, **kw)

    def group(self, name: str = "", **kw):
        return self._factory.group(name, **kw)

    def __call__(self, *args, **kwargs):
        return self._func(*args, **kwargs)

    def __getattr__(self, item):
        return getattr(self._func, item)


class _GroupFactory:
    """`@filter.command_group` / `@组名.group` 的产物：挂一个 `.command()` 出来。"""

    def __init__(self, owner: "_Filter", kind: str, name: str) -> None:
        self._owner = owner
        self._kind = kind
        self._name = name

    def command(self, name: str = "", **kw):
        return self._owner._register("command", f"{self._name} {name}".strip())

    def group(self, name: str = "", **kw):
        return _GroupFactory(self._owner, "group", f"{self._name} {name}".strip())


class _Filter:
    """装饰器只负责登记，真正驱动指令由测试直接调用被装饰的函数。"""

    PermissionType = PermissionType
    EventMessageType = EventMessageType

    def __init__(self) -> None:
        self.registered: list[tuple[str, str, Any]] = []

    def _register(self, kind: str, name: Any):
        def deco(func):
            self.registered.append((kind, str(name), func))
            return func
        return deco

    def command(self, name: str = "", **kw):
        return self._register("command", name)

    def command_group(self, name: str = "", **kw):
        """指令组：是**装饰器**，返回的句柄既能调用（组本身没行为），又有 `.command`。"""
        def deco(func):
            return _GroupHandle(func, _GroupFactory(self, "command_group", name))
        return deco

    def group(self, name: str = "", **kw):
        def deco(func):
            return _GroupHandle(func, _GroupFactory(self, "group", name))
        return deco

    def permission_type(self, level: Any):
        return self._register("permission", level)

    def event_message_type(self, kind: Any):
        return self._register("event_type", kind)

    def on_llm_request(self, **kw):
        return self._register("on_llm_request", "")

    def on_llm_response(self, **kw):
        return self._register("on_llm_response", "")

    def on_decorating_result(self, **kw):
        return self._register("on_decorating_result", "")

    def after_message_sent(self, **kw):
        """她自己发出去一句话之后触发。真实签名见 astrbot.api.event.filter。"""
        return self._register("after_message_sent", "")

    def on_waiting_llm_request(self, **kw):
        return self._register("on_waiting_llm_request", "")

    def at(self, *a, **k):
        return self._register("at", a)

    def prefix(self, *a, **k):
        return self._register("prefix", a)

    def regex(self, *a, **k):
        return self._register("regex", a)

    def func(self, name: str = "", **kw):
        return self._register("func", name)

    def plugins(self, *a, **k):
        return self._register("plugins", a)


class AstrMessageEvent:
    """类型占位：main.py 只用它做注解。"""


class ProviderRequest:
    def __init__(self, system_prompt: str = "", contexts: list | None = None) -> None:
        self.system_prompt = system_prompt
        self.contexts = contexts if contexts is not None else []
        self.extra_user_content_parts: list = []
        self.prompt: str = ""


class Context:
    """AstrBot 的 Context：测试里只暴露插件真正会调的那几个。"""

    def __init__(self, data_dir: str = "") -> None:
        self._data_dir = data_dir
        self.logger = _Collector()

    def get_config(self, name: str = ""):
        return None

    def get_status(self):
        return {}


class Star:
    def __init__(self, context: Context, config: Any = None) -> None:
        self.context = context
        self._conf = config

    async def initialize(self) -> None: ...
    async def terminate(self) -> None: ...
    async def cleanup(self) -> None: ...


class TextPart:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.type = "text"
        self._no_save = False

    def mark_as_temp(self):
        self._no_save = True
        return self

    def __repr__(self) -> str:
        return f"TextPart({self.text!r})"


class _Session:
    """aiohttp.ClientSession 占位：测试不发真请求。"""

    def __init__(self, *a, **k):
        self.closed = False

    async def __aenter__(self):
        raise AssertionError("测试里不该发真实网络请求")

    async def __aexit__(self, *a):
        return False

    async def close(self):
        self.closed = True


def install(data_dir: str = "/tmp/humanoid_test_data") -> dict[str, Any]:
    """把 astrbot / aiohttp 注册进 sys.modules，返回可断言的句柄。"""
    handles: dict[str, Any] = {"logger": PLUGIN_LOGGER, "filter": _Filter()}

    def mod(name: str) -> types.ModuleType:
        m = sys.modules.get(name)
        if m is None:
            m = types.ModuleType(name)
            sys.modules[name] = m
        return m

    astrbot = mod("astrbot")
    api = mod("astrbot.api")
    api.logger = handles["logger"]  # type: ignore[attr-defined]
    astrbot.api = api  # type: ignore[attr-defined]

    event = mod("astrbot.api.event")
    event.AstrMessageEvent = AstrMessageEvent  # type: ignore[attr-defined]
    event.filter = handles["filter"]  # type: ignore[attr-defined]
    api.event = event  # type: ignore[attr-defined]

    provider = mod("astrbot.api.provider")
    provider.ProviderRequest = ProviderRequest  # type: ignore[attr-defined]
    api.provider = provider  # type: ignore[attr-defined]

    star = mod("astrbot.api.star")
    star.Context = Context  # type: ignore[attr-defined]
    star.Star = Star  # type: ignore[attr-defined]
    api.star = star  # type: ignore[attr-defined]

    core = mod("astrbot.core")
    astrbot.core = core  # type: ignore[attr-defined]
    utils = mod("astrbot.core.utils")
    core.utils = utils  # type: ignore[attr-defined]
    path_mod = mod("astrbot.core.utils.astrbot_path")
    path_mod.get_astrbot_data_path = lambda: data_dir  # type: ignore[attr-defined]
    utils.astrbot_path = path_mod  # type: ignore[attr-defined]

    agent = mod("astrbot.core.agent")
    core.agent = agent  # type: ignore[attr-defined]
    msg = mod("astrbot.core.agent.message")
    msg.TextPart = TextPart  # type: ignore[attr-defined]
    agent.message = msg  # type: ignore[attr-defined]

    aiohttp = mod("aiohttp")
    aiohttp.ClientSession = _Session  # type: ignore[attr-defined]
    aiohttp.ClientTimeout = lambda **k: None  # type: ignore[attr-defined]

    handles["Context"] = Context
    handles["ProviderRequest"] = ProviderRequest
    handles["TextPart"] = TextPart
    return handles
