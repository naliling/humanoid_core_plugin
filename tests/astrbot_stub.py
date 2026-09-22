"""测试用的 AstrBot / aiohttp 最小替身。

`main.py` 是插件与框架的接缝，它一旦写坏（装饰器名字改了、钩子签名变了）整份插件就加载不了，
所以这一层也必须被测到。真实环境里没有 astrbot 时，这里补上它真正被用到的那点表面：
装饰器只登记不执行、ProviderRequest 是个可写属性的空壳、logger 收集输出。
"""

from __future__ import annotations

import sys
import types
from typing import Any, Callable


class _Collector:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def _add(self, level: str, *args: Any) -> None:
        self.lines.append((level, " ".join(str(a) for a in args)))

    def debug(self, *a): self._add("debug", *a)
    def info(self, *a): self._add("info", *a)
    def warning(self, *a): self._add("warning", *a)
    def error(self, *a): self._add("error", *a)
    def critical(self, *a): self._add("error", *a)

    def text(self, level: str = "") -> str:
        return "\n".join(t for lv, t in self.lines if not level or lv == level)


class PermissionType:
    ADMIN = "admin"
    USER = "user"


class EventMessageType:
    ALL = "all"
    TEXT = "text"


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

    def permission_type(self, level: Any):
        return self._register("permission", level)

    def event_message_type(self, kind: Any):
        return self._register("event_type", kind)

    def on_llm_request(self):
        return self._register("on_llm_request", "")

    def on_waiting_llm_request(self):
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
    handles: dict[str, Any] = {"logger": _Collector(), "filter": _Filter()}

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
