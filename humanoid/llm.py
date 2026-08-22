"""模型解析与调用网关。

## AstrBot 4.x 的 Provider API

只有这三个入口存在（`astrbot/core/star/context.py`）：

- `context.get_provider_by_id(provider_id)` —— 查 `provider_manager.inst_map`；
  配置项里 `_special: select_provider` 的下拉框存的正是这个 id
- `context.get_all_providers()` —— 已过滤为 chat_completion 类型的列表
- `context.get_using_provider(umo)` —— 当前会话使用的对话模型

`context.get_provider()`、`context.providers`、`context.get_providers()`
**都不存在**。用它们探测会静默失败（`getattr` 拿不到就当没有），表现为「下拉框里选了
模型却始终不生效，不选反而正常」—— 因为不选时走的是上面第三个真实 API。
`tests/test_llm.py::RootCauseWitnessTest` 会对真实 `Context` 类断言这一点。

另外注意 `get_provider_by_id` 的返回类型是
`Provider | TTSProvider | STTProvider | EmbeddingProvider | RerankProvider | None`，
拿到对象后必须确认它真的能对话（有 `text_chat`）。

本模块不 import astrbot，Context / Provider 都按鸭子类型处理。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

# 失败分类。诊断报告与日志都用这套词汇。
OUTCOME_OK = "ok"
OUTCOME_NOT_FOUND = "not_found"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_ERROR = "error"
OUTCOME_EMPTY = "empty"
OUTCOME_COOLDOWN = "cooldown"
OUTCOME_NO_CANDIDATE = "no_candidate"

_OUTCOME_TEXT = {
    OUTCOME_OK: "成功",
    OUTCOME_NOT_FOUND: "未找到",
    OUTCOME_TIMEOUT: "超时",
    OUTCOME_ERROR: "调用报错",
    OUTCOME_EMPTY: "返回空内容",
    OUTCOME_COOLDOWN: "冷却中已跳过",
    OUTCOME_NO_CANDIDATE: "没有可用候选",
}


def describe_outcome(outcome: str) -> str:
    return _OUTCOME_TEXT.get(outcome, outcome)


@dataclass(frozen=True, slots=True)
class Attempt:
    """一次候选模型的尝试记录。"""

    label: str
    provider_id: str
    outcome: str
    detail: str = ""
    elapsed: float = 0.0

    def describe(self) -> str:
        who = f"{self.label}({self.provider_id})" if self.provider_id else self.label
        text = f"{who} {describe_outcome(self.outcome)}"
        if self.elapsed:
            text += f" 用时 {self.elapsed:.1f}s"
        if self.detail:
            text += f"：{self.detail}"
        return text


@dataclass(frozen=True, slots=True)
class LLMResult:
    ok: bool
    text: str = ""
    label: str = ""
    provider_id: str = ""
    outcome: str = OUTCOME_NO_CANDIDATE
    detail: str = ""
    elapsed: float = 0.0
    attempts: tuple[Attempt, ...] = field(default_factory=tuple)

    def summary(self) -> str:
        if self.ok:
            return f"成功（{self.label}:{self.provider_id}，用时 {self.elapsed:.1f}s）"
        if not self.attempts:
            return describe_outcome(self.outcome)
        return " → ".join(a.describe() for a in self.attempts)


def extract_text(response: Any) -> str:
    """从 LLMResponse 取纯文本。先看 completion_text，再退到 result_chain。"""
    if response is None:
        return ""
    text = getattr(response, "completion_text", None)
    if isinstance(text, str) and text.strip():
        return text
    chain = getattr(response, "result_chain", None)
    getter = getattr(chain, "get_plain_text", None)
    if callable(getter):
        try:
            plain = getter()
            if isinstance(plain, str) and plain.strip():
                return plain
        except Exception:
            pass
    if isinstance(text, str):
        return text
    return str(response)


def _is_chat_provider(candidate: Any) -> bool:
    """能对话才算数：get_provider_by_id 也可能返回 TTS / STT / Embedding provider。"""
    return candidate is not None and callable(getattr(candidate, "text_chat", None))


class ProviderResolver:
    """按 id 解析对话 provider。"""

    __slots__ = ("_context", "_log")

    def __init__(self, context: Any, logger: Any = None) -> None:
        self._context = context
        self._log = logger

    # ---------- 查询 ----------

    def available_ids(self) -> list[str]:
        """所有可用于对话的 provider id。诊断与失败日志都靠它。"""
        ids: list[str] = []
        for provider in self._all_providers():
            pid = self.id_of(provider)
            if pid and pid not in ids:
                ids.append(pid)
        return ids

    def id_of(self, provider: Any) -> str:
        meta = getattr(provider, "meta", None)
        if callable(meta):
            try:
                pid = getattr(meta(), "id", None)
                if pid:
                    return str(pid)
            except Exception:
                pass
        config = getattr(provider, "provider_config", None)
        if isinstance(config, dict) and config.get("id"):
            return str(config["id"])
        return type(provider).__name__

    def _all_providers(self) -> list[Any]:
        getter = getattr(self._context, "get_all_providers", None)
        if not callable(getter):
            return []
        try:
            result = getter()
        except Exception as exc:
            self._debug(f"get_all_providers() 失败: {exc}")
            return []
        if isinstance(result, Sequence) and not isinstance(result, (str, bytes)):
            return [p for p in result if p is not None]
        return []

    # ---------- 解析 ----------

    def resolve(self, provider_id: str) -> Any | None:
        """按 id 解析对话 provider。找不到或类型不对返回 None。"""
        target = str(provider_id or "").strip()
        if not target:
            return None

        getter = getattr(self._context, "get_provider_by_id", None)
        if callable(getter):
            try:
                candidate = getter(target)
            except Exception as exc:
                self._debug(f"get_provider_by_id('{target}') 失败: {exc}")
                candidate = None
            if _is_chat_provider(candidate):
                return candidate
            if candidate is not None:
                self._debug(f"'{target}' 命中的 provider 不支持对话（{type(candidate).__name__}）")

        # 兜底：遍历 chat_completion 列表。先精确比对，再忽略大小写/空白。
        providers = self._all_providers()
        for provider in providers:
            if _is_chat_provider(provider) and self.id_of(provider) == target:
                return provider
        folded = target.casefold()
        for provider in providers:
            if _is_chat_provider(provider) and self.id_of(provider).strip().casefold() == folded:
                self._debug(f"'{target}' 通过忽略大小写匹配到 {self.id_of(provider)}")
                return provider
        return None

    def resolve_global(self, umo: str | None = None) -> Any | None:
        """AstrBot 全局/会话默认对话模型。"""
        getter = getattr(self._context, "get_using_provider", None)
        if not callable(getter):
            return None
        for args in ((umo,), ()) if umo else ((),):
            try:
                candidate = getter(*args)
            except Exception as exc:
                self._debug(f"get_using_provider({args}) 失败: {exc}")
                continue
            if _is_chat_provider(candidate):
                return candidate
        return None

    def _debug(self, message: str) -> None:
        if self._log is not None:
            self._log.debug(f"[humanoid_core] {message}")


GLOBAL_LABEL = "全局默认"


class LLMGateway:
    """按候选链调用模型：未找到 / 超时 / 报错三种失败等价，都往下一个候选走。

    失败的 provider 进入冷却期，冷却期内直接跳过 —— 这样「配错一个 id」不会让
    之后每一次生成都白等一轮超时。成功一次即解除该 id 的冷却。
    """

    __slots__ = ("_config", "_cooldown", "_last_results", "_log", "_monotonic", "_resolver")

    def __init__(
        self,
        resolver: ProviderResolver,
        config_provider: Callable[[], Any],
        logger: Any = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._resolver = resolver
        self._config = config_provider
        self._log = logger
        self._monotonic = monotonic
        self._cooldown: dict[str, float] = {}
        self._last_results: dict[str, LLMResult] = {}

    # ---------- 冷却 ----------

    def _cooldown_seconds(self) -> float:
        return max(0.0, float(getattr(self._config(), "schedule_provider_cooldown_minutes", 0)) * 60.0)

    def cooldown_remaining(self, provider_id: str) -> float:
        until = self._cooldown.get(provider_id)
        if until is None:
            return 0.0
        remaining = until - self._monotonic()
        if remaining <= 0:
            self._cooldown.pop(provider_id, None)
            return 0.0
        return remaining

    def cooldowns(self) -> dict[str, float]:
        """当前处于冷却中的 provider → 剩余秒数。"""
        return {pid: rem for pid in list(self._cooldown) if (rem := self.cooldown_remaining(pid)) > 0}

    def clear_cooldown(self, provider_id: str | None = None) -> None:
        if provider_id is None:
            self._cooldown.clear()
        else:
            self._cooldown.pop(provider_id, None)

    def _enter_cooldown(self, provider_id: str) -> None:
        seconds = self._cooldown_seconds()
        if provider_id and seconds > 0:
            self._cooldown[provider_id] = self._monotonic() + seconds

    # ---------- 诊断 ----------

    def last_result(self, purpose: str) -> LLMResult | None:
        return self._last_results.get(purpose)

    # ---------- 调用 ----------

    async def generate(
        self,
        *,
        prompt: str,
        chain: Sequence[tuple[str, str]],
        allow_global: bool,
        timeout: float,
        attempts_per_provider: int = 1,
        retry_interval: float = 0.0,
        umo: str | None = None,
        purpose: str = "llm",
        ignore_cooldown: bool = False,
        **call_kwargs: Any,
    ) -> LLMResult:
        candidates: list[tuple[str, str]] = [(label, pid) for label, pid in chain if pid]
        if allow_global:
            candidates.append((GLOBAL_LABEL, ""))

        attempts: list[Attempt] = []
        if not candidates:
            result = LLMResult(
                ok=False,
                outcome=OUTCOME_NO_CANDIDATE,
                detail="未配置专用模型，且已禁止回退全局默认模型",
                attempts=(),
            )
            self._last_results[purpose] = result
            return result

        for label, configured_id in candidates:
            is_global = label == GLOBAL_LABEL and not configured_id

            if not is_global and not ignore_cooldown:
                remaining = self.cooldown_remaining(configured_id)
                if remaining > 0:
                    attempts.append(
                        Attempt(label, configured_id, OUTCOME_COOLDOWN, f"剩余约 {remaining / 60:.0f} 分钟")
                    )
                    continue

            provider = self._resolver.resolve_global(umo) if is_global else self._resolver.resolve(configured_id)
            if provider is None:
                attempts.append(Attempt(label, configured_id, OUTCOME_NOT_FOUND, self._not_found_detail(is_global)))
                self._warn_not_found(purpose, label, configured_id, is_global)
                if not is_global:
                    self._enter_cooldown(configured_id)
                continue

            actual_id = self._resolver.id_of(provider) or configured_id
            outcome, detail, elapsed, text = await self._call_with_retries(
                provider=provider,
                prompt=prompt,
                timeout=timeout,
                attempts_per_provider=max(1, int(attempts_per_provider)),
                retry_interval=max(0.0, float(retry_interval)),
                label=label,
                actual_id=actual_id,
                attempts=attempts,
                call_kwargs=call_kwargs,
            )
            if outcome == OUTCOME_OK:
                self.clear_cooldown(actual_id)
                self.clear_cooldown(configured_id)
                result = LLMResult(
                    ok=True,
                    text=text,
                    label=label,
                    provider_id=actual_id,
                    outcome=OUTCOME_OK,
                    elapsed=elapsed,
                    attempts=tuple(attempts),
                )
                self._last_results[purpose] = result
                self._info(f"{purpose} 使用 {label}({actual_id}) 成功，用时 {elapsed:.1f}s")
                return result

            if not is_global:
                self._enter_cooldown(actual_id or configured_id)
            self._warn(f"{purpose} {label}({actual_id}) {describe_outcome(outcome)}：{detail}")

        last = attempts[-1] if attempts else None
        result = LLMResult(
            ok=False,
            label=last.label if last else "",
            provider_id=last.provider_id if last else "",
            outcome=last.outcome if last else OUTCOME_NO_CANDIDATE,
            detail=last.detail if last else "",
            attempts=tuple(attempts),
        )
        self._last_results[purpose] = result
        return result

    async def _call_with_retries(
        self,
        *,
        provider: Any,
        prompt: str,
        timeout: float,
        attempts_per_provider: int,
        retry_interval: float,
        label: str,
        actual_id: str,
        attempts: list[Attempt],
        call_kwargs: dict[str, Any],
    ) -> tuple[str, str, float, str]:
        outcome, detail, elapsed = OUTCOME_ERROR, "", 0.0
        for attempt_no in range(1, attempts_per_provider + 1):
            started = self._monotonic()
            try:
                response = await asyncio.wait_for(
                    provider.text_chat(prompt=prompt, **call_kwargs), timeout=timeout
                )
            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, TimeoutError):
                elapsed = self._monotonic() - started
                outcome, detail = OUTCOME_TIMEOUT, f"超过 {timeout:.0f}s（第 {attempt_no} 次）"
            except Exception as exc:
                elapsed = self._monotonic() - started
                outcome, detail = OUTCOME_ERROR, f"{type(exc).__name__}: {exc}"
            else:
                elapsed = self._monotonic() - started
                text = extract_text(response)
                if text.strip():
                    attempts.append(Attempt(label, actual_id, OUTCOME_OK, "", elapsed))
                    return OUTCOME_OK, "", elapsed, text
                outcome, detail = OUTCOME_EMPTY, f"第 {attempt_no} 次返回空内容"

            attempts.append(Attempt(label, actual_id, outcome, detail, elapsed))
            if attempt_no < attempts_per_provider and retry_interval > 0:
                await asyncio.sleep(retry_interval)
        return outcome, detail, elapsed, ""

    # ---------- 日志 ----------

    def _not_found_detail(self, is_global: bool) -> str:
        if is_global:
            return "AstrBot 未设置全局默认对话模型"
        ids = self._resolver.available_ids()
        return f"当前可用 id: {ids}" if ids else "AstrBot 当前没有任何可用的对话模型"

    def _warn_not_found(self, purpose: str, label: str, provider_id: str, is_global: bool) -> None:
        if is_global:
            self._warn(f"{purpose} 无法获取全局默认对话模型，请检查 AstrBot 的「默认对话模型」设置")
            return
        ids = self._resolver.available_ids()
        self._warn(
            f"{purpose} {label} 配置的 id '{provider_id}' 不存在。"
            f"当前可用 id: {ids or '（无）'}。请在插件配置里重新选择，或用 /拟人诊断 查看详情"
        )

    def _info(self, message: str) -> None:
        if self._log is not None:
            self._log.info(f"[humanoid_core] {message}")

    def _warn(self, message: str) -> None:
        if self._log is not None:
            self._log.warning(f"[humanoid_core] {message}")



