"""测试替身：模拟 AstrBot 4.x 的 Context / Provider 表面。

刻意只暴露 AstrBot 4.26.3 真实存在的方法（`get_provider_by_id`、`get_all_providers`、
`get_using_provider`）。v2.10.2 的解析代码探测的是 `get_provider`/`providers`/`get_providers`，
在这里同样会全部落空 —— 这正是根因 1 的回归护栏。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class FakeMeta:
    id: str


class FakeResponse:
    """模拟 LLMResponse：completion_text 属性。"""

    def __init__(self, text: str) -> None:
        self.completion_text = text

    def __str__(self) -> str:  # pragma: no cover
        return f"FakeResponse({self.completion_text!r})"


class FakeChainResponse:
    """模拟只有 result_chain 的响应，用于验证文本提取兜底。"""

    def __init__(self, text: str) -> None:
        self._text = text
        self.result_chain = self

    def get_plain_text(self) -> str:
        return self._text


class FakeProvider:
    """可控的对话 provider：延迟、异常、返回内容、调用计数。"""

    def __init__(
        self,
        provider_id: str,
        reply: str = "[]",
        delay: float = 0.0,
        error: BaseException | None = None,
        response_factory: Any = None,
    ) -> None:
        self._id = provider_id
        self.reply = reply
        self.delay = delay
        self.error = error
        self.response_factory = response_factory
        self.calls = 0
        self.last_kwargs: dict[str, Any] = {}

    def meta(self) -> FakeMeta:
        return FakeMeta(self._id)

    async def text_chat(self, prompt: str | None = None, **kwargs: Any) -> Any:
        self.calls += 1
        self.last_kwargs = {"prompt": prompt, **kwargs}
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if self.response_factory is not None:
            return self.response_factory(self.reply)
        return FakeResponse(self.reply)


class FakeNonChatProvider:
    """有 meta() 但没有 text_chat —— 模拟 TTS/STT/Embedding provider。"""

    def __init__(self, provider_id: str) -> None:
        self._id = provider_id

    def meta(self) -> FakeMeta:
        return FakeMeta(self._id)


@dataclass
class FakeContext:
    """只暴露 AstrBot 4.x 真实 API 的假 Context。"""

    chat_providers: list[Any] = field(default_factory=list)
    other_providers: list[Any] = field(default_factory=list)
    global_provider: Any = None
    raise_on_get_by_id: bool = False
    raise_on_get_all: bool = False
    raise_on_get_using: bool = False
    using_calls: list[tuple[Any, ...]] = field(default_factory=list)

    def _inst_map(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for provider in [*self.chat_providers, *self.other_providers]:
            out[provider.meta().id] = provider
        return out

    def get_provider_by_id(self, provider_id: str) -> Any:
        if self.raise_on_get_by_id:
            raise RuntimeError("boom: get_provider_by_id")
        return self._inst_map().get(provider_id)

    def get_all_providers(self) -> list[Any]:
        if self.raise_on_get_all:
            raise RuntimeError("boom: get_all_providers")
        return list(self.chat_providers)

    def get_using_provider(self, umo: str | None = None) -> Any:
        self.using_calls.append((umo,))
        if self.raise_on_get_using:
            raise RuntimeError("boom: get_using_provider")
        return self.global_provider


class FakeLegacyContext:
    """完全没有 provider 相关 API 的 Context，验证解析器不会崩。"""

    def get_config(self) -> dict[str, Any]:
        return {}


class RecordingLogger:
    """收集日志文本，供断言「失败时是否打印了可用 id」。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def _add(self, level: str, message: str) -> None:
        self.records.append((level, str(message)))

    def debug(self, message: str) -> None:
        self._add("debug", message)

    def info(self, message: str) -> None:
        self._add("info", message)

    def warning(self, message: str) -> None:
        self._add("warning", message)

    def error(self, message: str) -> None:
        self._add("error", message)

    def text(self, level: str | None = None) -> str:
        return "\n".join(m for lvl, m in self.records if level is None or lvl == level)


class FakeClock:
    """可手动推进的单调时钟，用于测试冷却期而不真的等待。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds
