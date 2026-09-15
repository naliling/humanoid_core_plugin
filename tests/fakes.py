"""测试替身：模拟 AstrBot 4.x 的 Context / Provider 表面。

刻意只暴露 AstrBot 4.26.3 真实存在的方法（`get_provider_by_id`、`get_all_providers`、
`get_using_provider`）。任何依赖 `get_provider` / `providers` / `get_providers`
的解析代码在这里都会落空，和真实框架下的表现一致。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from humanoid.role_scope import RoleScope
from humanoid.state import StateStore


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
    persona_manager: Any = None
    conversation_manager: Any = None

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


class FakePersonaManager:
    """只实现日程真正会调到的那几个方法，并记录被调了几次。

    resolve_selected_persona 的返回形状与 AstrBot 一致：
    (persona_id, persona, force_applied_id, use_webchat_default)。
    """

    def __init__(
        self,
        personas: dict[str, dict[str, Any]] | None = None,
        default: dict[str, Any] | None = None,
        marker_for: str = "",
    ) -> None:
        self.personas = personas or {}
        self.default = default
        self.marker_for = marker_for
        self.calls = 0
        self.umos: list[str] = []

    async def resolve_selected_persona(
        self,
        *,
        umo: str,
        conversation_persona_id: str | None,
        platform_name: str,
        provider_settings: dict | None = None,
    ) -> tuple[Any, ...]:
        self.calls += 1
        self.umos.append(umo)
        if self.marker_for and umo == self.marker_for:
            return ("[%None]", None, None, False)
        pid = conversation_persona_id or ""
        if pid and pid in self.personas:
            return (pid, self.personas[pid], pid, False)
        return ("default", self.default, None, False)

    async def get_default_persona_v3(self, umo=None) -> Any:
        self.calls += 1
        return self.default


class FakeConversationManager:
    """按 umo 返回预先绑定的 persona_id。"""

    def __init__(self, binding: dict[str, str] | None = None) -> None:
        self.binding = binding or {}

    async def get_curr_conversation_id(self, umo: str) -> str:
        return f"conv:{umo}" if umo else ""

    async def get_conversation(self, umo: str, cid: str) -> Any:
        pid = self.binding.get(umo, "")
        if not pid:
            return None
        return type("Conv", (), {"persona_id": pid})()


def fake_persona(name: str, prompt: str) -> dict[str, Any]:
    return {"name": name, "prompt": prompt, "begin_dialogs": []}


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


class FrozenClock:
    """固定「现在」的 Clock 替身。

    必须跟状态文件里的日期一起固定：否则 today_str() 返回真实日期，而 state 是在
    某个假日期下 load 的，服务会把每一次调用都当成「跨天」而强制重新生成。
    表面与真实 Clock 对齐（display_city / is_night 等），否则测试里走不到完整注入。
    """

    def __init__(self, moment, city: str = "北京") -> None:
        self.moment = moment
        self.city = city

    def now(self):
        return self.moment

    def today_str(self) -> str:
        return self.moment.strftime("%Y-%m-%d")

    def weekday(self) -> str:
        return "一二三四五六日"[self.moment.weekday()]

    def holiday(self) -> str:
        return ""

    @property
    def display_city(self) -> str:
        return self.city

    def is_night(self, moment=None) -> bool:
        return False

    def zone_state(self):
        """与真实 Clock 对齐：固定时刻带 tzinfo 时报告它的偏移，否则当成机器时间。"""
        from humanoid.clock import ZoneState, resolve_zone_name

        offset = self.moment.utcoffset()
        name = getattr(self.moment.tzinfo, "key", "") or resolve_zone_name(self.city) or ""
        return ZoneState(
            city=self.city,
            zone_name=name if offset is not None else "",
            offset_minutes=int(offset.total_seconds() // 60) if offset else 0,
            note="",
        )

    def is_deep_sleep(self, moment=None) -> bool:
        return False

    def advance(self, **kwargs) -> None:
        self.moment = self.moment + timedelta(**kwargs)


class ScopeStore:
    """StateStore + 一个角色的 RoleScope。

    服务层重构后一律读写 `roles[bid].self` / `roles[bid].users`，测试里仍希望
    `store.data["energy"]` 直接命中当前角色的 self 字典，所以 `.data` 指向那份
    子字典而不是整个 state。`load()` 会整体替换底层 state，因此每次载入后重建
    RoleScope，避免拿到已经脱离字典树的旧引用。
    """

    def __init__(
        self,
        path: str | Path,
        flush_interval: float = 0.01,
        role_id: str = "default",
    ) -> None:
        self.store = StateStore(path, lambda: flush_interval)
        self.role_id = role_id
        self._scope: RoleScope | None = None

    def load(self, today: str = "", cycle_length: int = 28) -> None:
        self.store.load(today, cycle_length)
        self._scope = None

    @property
    def scope(self) -> RoleScope:
        if self._scope is None:
            self._scope = RoleScope(self.store.data, self.role_id)
            self._scope.set_mark_dirty(self.store.mark_dirty)
        return self._scope

    @property
    def data(self) -> dict[str, Any]:
        """当前角色的 self 字典。"""
        return self.scope.self_state

    def user(self, user_id: str) -> dict[str, Any]:
        return self.scope.user_state(user_id)

    def get(self, key: str, default: Any = None) -> Any:
        return self.scope.get_self(key, default)

    def set(self, key: str, value: Any) -> None:
        self.scope.set_self(key, value)

    @property
    def path(self) -> Path:
        return self.store.path

    @property
    def dirty(self) -> bool:
        return self.store.dirty

    @property
    def writes(self) -> int:
        return self.store.writes

    def mark_dirty(self) -> None:
        self.store.mark_dirty()

    def flush_sync(self) -> bool:
        return self.store.flush_sync()

    async def flush(self) -> bool:
        return await self.store.flush()


def freeze(core, moment):
    """把角色的所有服务换成同一个假时钟。

    各服务在构造时就捕获了 clock 对象，只换 `core.clock` 会让它们各看各的时间：
    身体会把她的钟点按宿主机小时算，「她在睡觉」「该不该说话」全部错一个偏移量。
    城市沿用真 Clock 已经算好的显示名（IANA 名会翻成中文），否则测试里看到的
    「你在北京」其实与配置无关。
    """
    clock = FrozenClock(moment, city=getattr(core.clock, "display_city", "北京"))
    core.clock = clock
    for service in (core.schedule, core.soma, core.energy, core.process, core.mood,
                    core.social, core.weather):
        service._clock = clock
    return core
