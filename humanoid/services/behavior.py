"""行为与注意力服务。

只保存短期、运行时事件；持久状态仍由 RoleScope 管理。
数据流：事件 -> 注意力 -> 行为倾向 -> PromptBuilder。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..config import HumanoidConfig


# (下界, 上界, 事件类型, 间隔档位, 基础重要性)
GAP_SEGMENTS = (
    (300.0, 1800.0, "conversation_resumed", "short_return", 0.30),
    (1800.0, 7200.0, "user_returned", "medium_return", 0.55),
    (7200.0, 21600.0, "user_returned", "long_return", 0.72),
    (21600.0, float("inf"), "long_gap", "very_long_return", 0.86),
)

EVENT_TTL = 3600.0
MAX_EVENTS_PER_USER = 12
MAX_RELEVANT_EVENTS = 1


class BehaviorService:
    """短期事件、注意力和行为倾向的运行时协调器。"""

    def __init__(self, core):
        self._core = core
        self._events: Dict[str, List[Dict[str, Any]]] = {}
        # 当前“回来这一轮”的事件不能在第一次 LLM 请求后立即消失。
        # 同一条用户消息可能触发多个 LLM 请求，因此事件绑定到当前回合，
        # 下一次用户发言时才清掉。
        self._active_event: Dict[str, Dict[str, Any]] = {}

    @property
    def config(self) -> HumanoidConfig:
        return self._core.config

    def _key(self, user_id: str) -> str:
        return f"{self._core.role_id}:{str(user_id)}"

    def _make_event(
        self,
        event_type: str,
        now: float,
        importance: float,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return {
            "type": event_type,
            "timestamp": float(now),
            "importance": max(0.0, min(1.0, float(importance))),
            "data": dict(data or {}),
        }

    def process_interval(
        self,
        user_id: str,
        now: float,
        last_ts: Optional[float],
        last_message: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """根据旧的 last_interaction 生成一次性事件。

        注意：本方法只计算，不写 RoleScope。调用方必须先调用本方法，再更新
        last_interaction，避免把间隔计算成 0。
        """
        if last_ts is None:
            return self._make_event(
                "conversation_started",
                now,
                0.08,
                {"gap_seconds": None},
            )

        try:
            elapsed = max(0.0, float(now) - float(last_ts))
        except (TypeError, ValueError):
            return self._make_event("conversation_started", now, 0.08, {})

        threshold = max(0.0, float(self.config.last_interaction_threshold_minutes) * 60.0)
        if elapsed < threshold or elapsed < 300.0:
            return None

        for low, high, event_type, bucket, importance in GAP_SEGMENTS:
            if low < elapsed <= high or (elapsed == low and low == 300.0):
                affection = 50.0
                try:
                    affection = float(self._core.mood.profile(user_id).get("affection", 50.0))
                except Exception:
                    pass

                # 关系状态只做轻微调制，避免“好感度=固定台词”。
                if affection >= 70.0:
                    importance *= 1.08
                elif affection <= 30.0:
                    importance *= 0.92

                data: Dict[str, Any] = {
                    "gap_seconds": elapsed,
                    "gap_bucket": bucket,
                }
                if last_message:
                    text = str(last_message.get("text", "")).strip()
                    if text:
                        data["previous_message"] = text[:120]

                return self._make_event(event_type, now, importance, data)

        return None

    def add_event(self, user_id: str, event: Dict[str, Any]) -> None:
        key = self._key(user_id)
        queue = self._events.setdefault(key, [])
        event_copy = dict(event)
        queue.append(event_copy)
        if len(queue) > MAX_EVENTS_PER_USER:
            del queue[:-MAX_EVENTS_PER_USER]
        self._active_event[key] = event_copy

    def _clean(self, user_id: str, now: float) -> List[Dict[str, Any]]:
        key = self._key(user_id)
        queue = self._events.get(key, [])
        fresh = []
        for event in queue:
            try:
                age = max(0.0, float(now) - float(event.get("timestamp", now)))
            except (TypeError, ValueError):
                continue
            if age <= EVENT_TTL:
                fresh.append(event)
        self._events[key] = fresh
        return fresh

    def compute_attention(self, user_id: str, now: float) -> List[Dict[str, Any]]:
        """计算短期注意力，不写持久状态。"""
        scored: List[Dict[str, Any]] = []
        for event in self._clean(user_id, now):
            try:
                age = max(0.0, float(now) - float(event.get("timestamp", now)))
            except (TypeError, ValueError):
                age = 0.0
            # 前几分钟保持较高新鲜度，1 小时后仍保留少量影响。
            freshness = max(0.35, 1.0 - age / EVENT_TTL)
            importance = max(0.0, min(1.0, float(event.get("importance", 0.5))))
            score = max(0.0, min(1.0, importance * freshness))
            item = dict(event)
            item["attention"] = score
            scored.append(item)

        scored.sort(key=lambda item: item.get("attention", 0.0), reverse=True)
        return scored[:MAX_RELEVANT_EVENTS]

    def get_relevant_events(self, user_id: str, now: float) -> List[Dict[str, Any]]:
        """读取短期事件，不消费，兼容旧调用。"""
        return self.compute_attention(user_id, now)

    def consume_relevant_events(self, user_id: str, now: float) -> List[Dict[str, Any]]:
        """返回当前“重新出现”事件，直到用户下一次发言。

        这样 AstrBot 对同一条用户消息进行多次 LLM 请求时，上下文不会第一次
        请求后突然消失；而下一条用户消息开始时，Core 会主动清除旧事件。
        """
        key = self._key(user_id)
        event = self._active_event.get(key)
        if not event:
            return []

        try:
            age = max(0.0, float(now) - float(event.get("timestamp", now)))
        except (TypeError, ValueError):
            self._active_event.pop(key, None)
            return []

        if age > EVENT_TTL:
            self._active_event.pop(key, None)
            return []

        item = dict(event)
        importance = max(0.0, min(1.0, float(item.get("importance", 0.5))))
        item["attention"] = max(
            0.0,
            min(1.0, importance * max(0.35, 1.0 - age / EVENT_TTL)),
        )
        return [item]

    def compute_agency(
        self,
        user_id: str,
        events: List[Dict[str, Any]],
        social_energy: float,
        mood_profile: Dict[str, Any],
        energy: float,
    ) -> Dict[str, float]:
        """根据长期状态 + 短期注意力形成行为倾向。

        这些值不是回复命令，只是 LLM 的决策背景。
        """
        agency = {
            "initiative": 0.42,
            "curiosity": 0.32,
            "care": 0.48,
            "social_willingness": 0.60,
            "continuation": 0.52,
        }

        social_energy = float(social_energy)
        energy = float(energy)
        affection = float(mood_profile.get("affection", 50.0))

        if social_energy < 30:
            agency["social_willingness"] *= 0.45
            agency["initiative"] *= 0.70
        elif social_energy < 60:
            agency["social_willingness"] *= 0.78
            agency["initiative"] *= 0.88
        elif social_energy > 85:
            agency["social_willingness"] += 0.06

        if energy < 20:
            agency["initiative"] *= 0.55
            agency["continuation"] *= 0.75
        elif energy < 50:
            agency["initiative"] *= 0.78
            agency["continuation"] *= 0.90
        elif energy > 80:
            agency["initiative"] += 0.04

        if affection >= 70:
            agency["care"] += 0.12
            agency["curiosity"] += 0.08
        elif affection <= 30:
            agency["care"] -= 0.10
            agency["social_willingness"] -= 0.08

        for event in events:
            typ = event.get("type")
            bucket = event.get("data", {}).get("gap_bucket")
            attention = max(0.0, min(1.0, float(event.get("attention", 0.0))))
            if attention <= 0:
                continue

            if typ == "conversation_started":
                agency["curiosity"] += 0.08 * attention
                agency["care"] += 0.04 * attention
            elif typ == "conversation_resumed":
                agency["curiosity"] += 0.12 * attention
                agency["continuation"] += 0.10 * attention
            elif typ == "user_returned":
                agency["curiosity"] += 0.18 * attention
                agency["care"] += 0.12 * attention
                agency["initiative"] += 0.08 * attention
                if bucket == "long_return":
                    agency["curiosity"] += 0.05 * attention
            elif typ == "long_gap":
                agency["curiosity"] += 0.22 * attention
                agency["care"] += 0.15 * attention
                agency["initiative"] += 0.10 * attention

        for key, value in agency.items():
            agency[key] = max(0.0, min(1.0, float(value)))
        return agency

    def clear_user_events(self, user_id: str) -> None:
        key = self._key(user_id)
        self._events.pop(key, None)
        self._active_event.pop(key, None)
