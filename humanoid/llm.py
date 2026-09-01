"""模型解析与调用网关。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

OUTCOME_OK = "ok"
OUTCOME_NOT_FOUND = "not_found"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_ERROR = "error"
OUTCOME_EMPTY = "empty"
OUTCOME_COOLDOWN = "cooldown"
OUTCOME_NO_CANDIDATE = "no_candidate"

PURPOSE_SCHEDULE = "日程生成"
PURPOSE_MOOD = "情绪分析"

_COOLDOWN_CONFIG_BY_PURPOSE = {
    PURPOSE_SCHEDULE: "schedule_provider_cooldown_minutes",
    PURPOSE_MOOD: "mood_provider_cooldown_minutes",
}
_DEFAULT_COOLDOWN_CONFIG = "schedule_provider_cooldown_minutes"

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
    return candidate is not None and callable(getattr(candidate, "text_chat", None))


class ProviderResolver:
    __slots__ = ("_context", "_log")

    def __init__(self, context: Any, logger: Any = None) -> None:
        self._context = context
        self._log = logger

    def available_ids(self) -> list[str]:
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

    def resolve(self, provider_id: str) -> Any | None:
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
        self._cooldown: dict[tuple[str, str], float] = {}
        self._last_results: dict[str, LLMResult] = {}

    def _cooldown_seconds(self, purpose: str) -> float:
        cfg = self._config()
        key = _COOLDOWN_CONFIG_BY_PURPOSE.get(purpose, _DEFAULT_COOLDOWN_CONFIG)
        return max(0.0, float(getattr(cfg, key, 0)) * 60.0)

    def cooldown_remaining(self, provider_id: str, purpose: str = PURPOSE_SCHEDULE) -> float:
        until = self._cooldown.get((purpose, provider_id))
        if until is None:
            return 0.0
        remaining = until - self._monotonic()
        if remaining <= 0:
            self._cooldown.pop((purpose, provider_id), None)
            return 0.0
        return remaining

    def cooldowns(self, purpose: str | None = None) -> dict[str, float]:
        out: dict[str, float] = {}
        for entry_purpose, pid in list(self._cooldown):
            if purpose is not None and entry_purpose != purpose:
                continue
            remaining = self.cooldown_remaining(pid, entry_purpose)
            if remaining > 0 and remaining > out.get(pid, 0.0):
                out[pid] = remaining
        return out

    def clear_cooldown(self, provider_id: str | None = None, purpose: str | None = None) -> None:
        for entry_purpose, pid in list(self._cooldown):
            if provider_id is not None and pid != provider_id:
                continue
            if purpose is not None and entry_purpose != purpose:
                continue
            self._cooldown.pop((entry_purpose, pid), None)

    def _enter_cooldown(self, provider_id: str, purpose: str) -> None:
        seconds = self._cooldown_seconds(purpose)
        if provider_id and seconds > 0:
            self._cooldown[(purpose, provider_id)] = self._monotonic() + seconds

    def last_result(self, purpose: str) -> LLMResult | None:
        return self._last_results.get(purpose)

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
        purpose: str = PURPOSE_SCHEDULE,
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
                remaining = self.cooldown_remaining(configured_id, purpose)
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
                    self._enter_cooldown(configured_id, purpose)
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
                self.clear_cooldown(actual_id, purpose)
                self.clear_cooldown(configured_id, purpose)
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
                self._enter_cooldown(actual_id or configured_id, purpose)
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