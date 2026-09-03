"""情绪服务 - 使用 RoleScope 版本。"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections.abc import Callable
from datetime import datetime
from dataclasses import dataclass
from typing import Any, Optional

from ..config import HumanoidConfig
from ..data.mood_map import generate_mood_tag, get_mood_label
from ..jsonx import extract_json_object
from ..llm import PURPOSE_MOOD, LLMGateway
from ..role_scope import RoleScope

NEGATIVE_PATTERN = re.compile(
    r"(傻|蠢|笨|白痴|废物|垃圾|去死|死吧|滚蛋|操|妈|逼|贱|恶心|讨厌|恨|烦|骂|吵|滚|弱智|脑残|sb|煞笔)",
    re.IGNORECASE,
)
POSITIVE_PATTERN = re.compile(r"(爱|喜欢|好|棒|厉害|赞|开心|谢谢|感谢|乖|可爱|聪明)", re.IGNORECASE)

AFFECTION_RANGE = (0.0, 100.0)
LIBIDO_RANGE = (0.0, 50.0)
AGGRESSION_RANGE = (0.0, 50.0)
DIMENSIONS = (
    ("affection", "base_affection", AFFECTION_RANGE),
    ("libido", "base_libido", LIBIDO_RANGE),
    ("aggression", "base_aggression", AGGRESSION_RANGE),
)

@dataclass(frozen=True, slots=True)
class Delta:
    affection: float
    libido: float
    aggression: float

    def scaled(self, factor: float) -> "Delta":
        return Delta(self.affection * factor, self.libido * factor, self.aggression * factor)

    def capped(self, cap: float) -> "Delta":
        return Delta(
            max(-cap, min(cap, self.affection)),
            max(-cap, min(cap, self.libido)),
            max(-cap, min(cap, self.aggression)),
        )

    def blend(self, other: "Delta", weight: float) -> "Delta":
        weight = max(0.0, min(1.0, weight))
        return Delta(
            self.affection * (1 - weight) + other.affection * weight,
            self.libido * (1 - weight) + other.libido * weight,
            self.aggression * (1 - weight) + other.aggression * weight,
        )

def local_delta(text: str) -> Delta:
    if NEGATIVE_PATTERN.search(text):
        return Delta(random.uniform(-4, -2), random.uniform(-2, -1), random.uniform(2, 4))
    if POSITIVE_PATTERN.search(text):
        return Delta(random.uniform(1, 3), random.uniform(0.5, 2), random.uniform(-1, -0.5))
    return Delta(random.uniform(-0.5, 0.5), random.uniform(-0.3, 0.3), random.uniform(-0.3, 0.3))


class MoodService:
    def __init__(
        self,
        scope: RoleScope,
        config_provider: Callable[[], HumanoidConfig],
        clock=None,
        spawn_fn=None,
        time_source: Callable[[], float] = time.time,
        gateway: LLMGateway | None = None,
        logger=None,
    ):
        self._scope = scope
        self._config = config_provider
        self._clock = clock
        self._spawn = spawn_fn
        self._time = time_source
        self._gateway: LLMGateway | None = None
        self._log = None
        self._lock = asyncio.Lock()
        self._gateway = gateway
        self._log = logger

    @property
    def config(self) -> HumanoidConfig:
        return self._config()

    def profile(self, user_id: str) -> dict[str, Any]:
        user_state = self._scope.user_state(user_id)
        record = user_state.get("mood")
        if isinstance(record, dict):
            return self._repair(record, user_state)

        cfg = self.config
        affection = float(cfg.mood_initial_affection)
        override = cfg.affection_override_for(user_id)
        if override is not None:
            affection = override
        record = {
            "affection": affection,
            "libido": float(cfg.mood_initial_libido),
            "aggression": float(cfg.mood_initial_aggression),
            "base_affection": affection,
            "base_libido": float(cfg.mood_initial_libido),
            "base_aggression": float(cfg.mood_initial_aggression),
            "last_interaction": self._time(),
            "last_decay": self._time(),
            "turn_count": 0,
            "messages_since_llm": 0,
        }
        user_state["mood"] = record
        self._scope.mark_dirty()
        return record

    def _repair(self, record: dict, user_state: dict) -> dict:
        changed = False
        for key, base_key, bounds in DIMENSIONS:
            for target in (key, base_key):
                try:
                    value = float(record.get(target, bounds[0]))
                except (TypeError, ValueError):
                    value = bounds[0]
                clamped = max(bounds[0], min(bounds[1], value))
                if record.get(target) != clamped:
                    record[target] = clamped
                    changed = True
        for field in ("last_decay", "messages_since_llm", "turn_count"):
            if field not in record:
                record[field] = 0 if field == "turn_count" else self._time()
                changed = True
        if changed:
            user_state["mood"] = record
            self._scope.mark_dirty()
        return record

    def label(self, user_id: str) -> str:
        data = self.profile(user_id)
        return get_mood_label(data["affection"], data["libido"], data["aggression"])

    def tag(self, user_id: str) -> str:
        return self._scope.get_user(user_id, "mood_tag", "")

    def nickname(self, user_id: str) -> str:
        return self._scope.get_user(user_id, "nickname", "")

    def set_nickname(self, user_id: str, nickname: str) -> str:
        self._scope.set_user(user_id, "nickname", nickname)
        return nickname

    def all_nicknames(self) -> dict[str, str]:
        result = {}
        for uid in self._scope.all_user_ids():
            name = self._scope.get_user(uid, "nickname")
            if name:
                result[uid] = name
        return result

    def decay_user(self, user_id: str) -> bool:
        cfg = self.config
        if not cfg.mood_enabled:
            return False
        user_state = self._scope.user_state(user_id)
        record = user_state.get("mood")
        if not record:
            return False
        return self._decay_record(record, user_state, cfg, self._time())

    def decay_all(self) -> int:
        cfg = self.config
        if not cfg.mood_enabled:
            return 0
        now = self._time()
        changed = 0
        for uid in self._scope.all_user_ids():
            user_state = self._scope.user_state(uid)
            record = user_state.get("mood")
            if record and self._decay_record(record, user_state, cfg, now):
                changed += 1
        return changed

    def _decay_record(self, record: dict, user_state: dict, cfg: HumanoidConfig, now: float) -> bool:
        try:
            last = float(record.get("last_decay", now))
        except (TypeError, ValueError):
            last = now
        elapsed_hours = (now - last) / 3600.0
        if elapsed_hours < 0.1:
            return False

        duration = max(0.5, float(cfg.mood_decay_hours))
        ratio = 1.0 if elapsed_hours >= duration else (elapsed_hours / duration) ** 2
        changed = False
        for key, base_key, bounds in DIMENSIONS:
            current = float(record.get(key, bounds[0]))
            base = float(record.get(base_key, bounds[0]))
            deviation = current - base
            if abs(deviation) < 1e-3:
                continue
            updated = max(bounds[0], min(bounds[1], current - deviation * ratio))
            if abs(updated - current) > 1e-4:
                record[key] = updated
                changed = True
        if changed:
            record["last_decay"] = now
            user_state["mood"] = record
            self._scope.mark_dirty()
        return changed

    async def update_from_message(self, user_id: str, text: str, *args, **kwargs) -> Optional[dict]:
        return await self.update_from_message_async(user_id, text)

    async def update_from_message_async(self, user_id: str, text: str) -> Optional[dict]:
        cfg = self.config
        if not cfg.mood_enabled or not text:
            return None

        base_delta = self._local_delta(text)

        async with self._lock:
            user_state = self._scope.user_state(user_id)
            record = user_state.get("mood")
            if not record:
                record = self.profile(user_id)
                user_state = self._scope.user_state(user_id)

            messages_since = int(record.get("messages_since_llm", 0)) + 1
            record["messages_since_llm"] = messages_since
            interval = max(1, cfg.mood_llm_interval_messages)
            should_call_llm = cfg.mood_use_llm_for_delta and messages_since >= interval
            if should_call_llm:
                record["messages_since_llm"] = 0
            self._scope.mark_dirty()

        llm_delta = None
        if should_call_llm:
            llm_delta = await self._llm_delta(user_id, text)

        async with self._lock:
            user_state = self._scope.user_state(user_id)
            record = user_state.get("mood")
            if not record:
                return None

            delta = self._resolve_delta(base_delta, llm_delta)
            delta = self._apply_modifiers(delta, user_id)
            self._commit(user_id, record, user_state, delta)
            return {"delta": delta, "record": record}

    def _local_delta(self, text: str) -> Delta:
        return local_delta(text)

    def _resolve_delta(self, base: Delta, llm: Delta | None) -> Delta:
        if llm is None:
            return base
        if base.affection < -1.5:
            return base.scaled(1.2)
        return base.blend(llm, 0.3)

    def _apply_modifiers(self, delta: Delta, user_id: str) -> Delta:
        cfg = self.config
        record = self.profile(user_id)
        factor = cfg.mood_sensitivity / 100.0
        delta = delta.scaled(factor)

        def adjust(value: float) -> float:
            if energy > 70 and value > 0:
                return value * 1.3
            if energy < 40:
                return value * 0.8
            return value

        energy = self._scope.get_self("energy", 80)
        delta = Delta(adjust(delta.affection), adjust(delta.libido), adjust(delta.aggression))

        cycle_day = self._scope.get_self("current_cycle_day", 1)
        phase = cfg.cycle_phase_index(cycle_day)
        if phase == 0:
            delta = Delta(
                delta.affection * (0.5 if delta.affection > 0 else 1.5),
                delta.libido * (0.5 if delta.libido > 0 else 1.5),
                delta.aggression * (0.5 if delta.aggression > 0 else 1.5),
            )
        elif phase == 2:
            delta = Delta(
                delta.affection * (1.4 if delta.affection > 0 else 1.0),
                delta.libido * (1.4 if delta.libido > 0 else 1.0),
                delta.aggression * (1.4 if delta.aggression > 0 else 1.0),
            )

        return delta.capped(float(cfg.mood_affection_delta_cap))

    def _commit(self, user_id: str, record: dict, user_state: dict, delta: Delta):
        before = {k: float(record[k]) for k, _, _ in DIMENSIONS}
        values = {
            "affection": delta.affection,
            "libido": delta.libido,
            "aggression": delta.aggression,
        }
        for key, _, bounds in DIMENSIONS:
            record[key] = max(bounds[0], min(bounds[1], before[key] + values.get(key, 0)))

        turn = int(record.get("turn_count", 0)) + 1
        base_coef = 1.0 if turn <= 10 else 0.2
        for key, base_key, bounds in DIMENSIONS:
            drift = values.get(key, 0) * base_coef * 0.5
            record[base_key] = max(bounds[0], min(bounds[1], float(record[base_key]) + drift))

        record["turn_count"] = turn
        record["last_interaction"] = self._time()
        user_state["mood"] = record
        self._scope.mark_dirty()
        self._log_event(user_id, before, record)

    async def _llm_delta(self, user_id: str, text: str) -> Delta | None:
        if self._gateway is None:
            return None
        cfg = self.config
        prompt = (
            "你是情绪变化分析器。只分析用户这条消息对角色的即时影响。\n"
            "返回严格 JSON，不要 Markdown："
            '{"affection_delta": 0, "libido_delta": 0, "aggression_delta": 0}.\n'
            "数值范围：affection -10~10，libido -5~5，aggression -5~5。\n"
            f"用户消息：{text[:500]}"
        )
        if cfg.debug_mode and self._log:
            self._log.debug(f"[humanoid_core] 情绪分析请求: {prompt}")

        result = await self._gateway.generate(
            prompt=prompt,
            chain=cfg.mood_provider_ids,
            allow_global=cfg.schedule_allow_global_fallback,
            timeout=float(cfg.mood_update_timeout),
            attempts_per_provider=1,
            purpose=PURPOSE_MOOD,
        )
        if not result.ok:
            if self._log is not None:
                self._log.warning(f"[humanoid_core] 情绪分析失败：{result.summary()}")
            return None
        data = extract_json_object(result.text)
        if not data:
            if self._log is not None:
                self._log.warning("[humanoid_core] 情绪分析失败：模型返回不是有效 JSON")
            return None
        try:
            delta = Delta(
                float(data.get("affection_delta", 0)),
                float(data.get("libido_delta", 0)),
                float(data.get("aggression_delta", 0)),
            ).capped(10.0)
            if cfg.debug_mode and self._log:
                self._log.debug(f"[humanoid_core] 情绪分析结果: {delta}")
            return delta
        except (TypeError, ValueError):
            if self._log is not None:
                self._log.warning("[humanoid_core] 情绪分析失败：JSON 数值无效")
            return None

    def _log_event(self, user_id: str, before: dict, record: dict):
        cfg = self.config
        if not cfg.mood_log_enabled:
            return
        events = []
        for key, (threshold, name) in {
            "affection": (cfg.mood_log_threshold_affection, "好感度"),
            "libido": (cfg.mood_log_threshold_libido, "亲近欲"),
            "aggression": (cfg.mood_log_threshold_aggression, "攻击性"),
        }.items():
            change = float(record[key]) - before.get(key, 0)
            if abs(change) >= max(0.0, float(threshold)):
                events.append(f"{name}{'上升' if change > 0 else '下降'}至 {record[key]:.1f}")
        if not events:
            return

        logs = self._scope.user_state(user_id).setdefault("mood_logs", [])
        logs.append({
            "time": self._clock.now().strftime("%Y-%m-%d %H:%M:%S") if self._clock else datetime.now().isoformat(),
            "event": "，".join(events),
            "affection": round(float(record["affection"]), 1),
            "libido": round(float(record["libido"]), 1),
            "aggression": round(float(record["aggression"]), 1),
        })
        limit = max(1, cfg.mood_log_max_entries)
        if len(logs) > limit:
            logs = logs[-limit:]
        self._scope.mark_dirty()

    def reset(self, user_id: str) -> dict:
        cfg = self.config
        record = self.profile(user_id)
        values = {
            "affection": float(cfg.mood_initial_affection),
            "libido": float(cfg.mood_initial_libido),
            "aggression": float(cfg.mood_initial_aggression),
        }
        for key in values:
            record[key] = values[key]
            record[f"base_{key}"] = values[key]
        record["turn_count"] = 0
        record["last_interaction"] = self._time()
        record["last_decay"] = self._time()
        record["messages_since_llm"] = 0
        self._scope.user_state(user_id)["mood"] = record
        self._scope.mark_dirty()
        return record

    def set_affection(self, user_id: str, value: float) -> float:
        clamped = max(0.0, min(100.0, value))
        record = self.profile(user_id)
        record["affection"] = clamped
        record["base_affection"] = clamped
        self._scope.user_state(user_id)["mood"] = record
        self._scope.mark_dirty()
        return clamped

    def set_affection_batch(self, pairs: list[tuple[str, float]]) -> int:
        applied = 0
        for uid, value in pairs:
            if 0 <= value <= 100:
                self.set_affection(uid, value)
                applied += 1
        return applied

    def logs_text(self, user_id: str, limit: int = 10) -> str:
        logs = self._scope.user_state(user_id).get("mood_logs", [])
        if not logs:
            return "📭 暂无情绪波动记录。"
        entries = logs[-limit:] if limit > 0 else logs
        lines = [f"📋 情绪波动记录（最近{len(entries)}条）：", "——————————————"]
        for entry in entries:
            lines.append(f"{entry.get('time', '')} | {entry.get('event', '')}")
        return "\n".join(lines)

    def profile_text(self, user_id: str, detailed: bool = False) -> str:
        data = self.profile(user_id)
        title = "〖情绪详细档案〗" if detailed else "〖情绪档案〗"
        lines = [title]
        if detailed:
            lines.append(f"好感度：{data['affection']:.1f}/100（基线 {data['base_affection']:.1f}）")
        else:
            lines.append(f"好感度：{data['affection']:.1f}/100")
        lines += [
            f"亲近欲：{data['libido']:.1f}/50（基线 {data['base_libido']:.1f}）",
            f"攻击性：{data['aggression']:.1f}/50（基线 {data['base_aggression']:.1f}）",
            f"当前标签：{self.label(user_id)}",
        ]
        if detailed:
            lines.append(f"交互轮次：{int(data.get('turn_count', 0))}")
        tag = self.tag(user_id)
        if tag:
            lines.append(f"心情标签：{tag}")
        return "\n".join(lines)
