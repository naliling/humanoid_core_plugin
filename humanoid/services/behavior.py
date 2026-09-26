"""行为与注意力服务。

只保存短期、运行时事件；持久状态仍由 RoleScope 管理。
数据流：事件 -> 注意力 -> 行为倾向 -> PromptBuilder。

注意力是三条独立的轴（v2.16.4）：

* `care` —— 在意度：对**这个人**。慢变量，每用户一个自己的基线（由好感度、脾气、
  以及一个按用户 ID 稳锚的错位推出来），往目标值指数逼近。以前所有人第一面都是
  同一个 50 分，那正是「数值全是单一的」的根源。
* `focus` —— 上心程度：对**TA正在讲的这件事**。不算不写盘，每条消息现推：问句、
  长度、是否推到她今天在做的事、是否推到她自己记着的事、心情好不好。
* `spare` —— 注意力余量：**她自己手里还剩多少**。从身体的困/难受/唤醒与手上在做的
  事推。这一轴跟 TA 无关，只跟她自己有关——真人不是随时都能接住话的。

三轴只进 `interest_state()`，写进上下文的是措辞（见 `wording.py`），数值只进
`/她的状态` 与 `/拟人诊断`。
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Dict, List, Optional

from ..config import HumanoidConfig
from ..slots import is_meal_event, is_sleep_event


# 在意度往目标值逼近的半衰期（小时）。太快的话一句好话就能把关系拉满，不像人。
CARE_HALF_LIFE_HOURS = 72.0
# 每用户基线错位幅度（±）：同一份好感度下，她对不同人的默认上心度本来就不一样。
CARE_SEED_SPAN = 0.22
CARE_EMA_MIN_GAP_SECONDS = 600.0


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
        events: List[Dict[str, Any]],
        social_energy: float,
        energy: float,
    ) -> Dict[str, float]:
        """她此刻的行为倾向：想不想开话头、还想不想接着聊。

        返回的就是这两轴，没有别的。原来还算了 curiosity / care / social_willingness
        三轴并按好感度与事件逐条调整，可 prompt_builder 一眼都不读——三份算完就扔的死功。
        """
        agency = {
            # 基线定得比中档门槛（0.45）高一点是有意的：原来基线 0.42 卡在门槛下方，
            # 于是**每一条消息**的注入开头都是「她不太想开话头／这会儿没什么特别想说的」——
            # 一句话就把模型往冷淡上推。0.52 让「她有点想说话」成为常态，
            # 「话匣子是开着的」那档也终于够得着（0.52+0.06+0.04+事件增量）。
            "initiative": 0.52,
            "continuation": 0.52,
        }

        social_energy = float(social_energy)
        energy = float(energy)

        if social_energy < 30:
            agency["initiative"] *= 0.70
        elif social_energy < 60:
            agency["initiative"] *= 0.88
        elif social_energy > 85:
            agency["initiative"] += 0.06

        if energy < 20:
            agency["initiative"] *= 0.55
            agency["continuation"] *= 0.75
        elif energy < 50:
            agency["initiative"] *= 0.78
            agency["continuation"] *= 0.90
        elif energy > 80:
            agency["initiative"] += 0.04

        for event in events:
            typ = event.get("type")
            attention = max(0.0, min(1.0, float(event.get("attention", 0.0))))
            if attention <= 0:
                continue
            if typ == "conversation_resumed":
                agency["continuation"] += 0.10 * attention
            elif typ in ("user_returned", "long_gap"):
                agency["initiative"] += 0.08 * attention

        for key, value in agency.items():
            agency[key] = max(0.0, min(1.0, float(value)))
        return agency

    def clear_user_events(self, user_id: str) -> None:
        key = self._key(user_id)
        self._events.pop(key, None)
        self._active_event.pop(key, None)

    # ------------------------------------------------------------------
    # 注意力三轴（v2.16.4）
    # ------------------------------------------------------------------

    def _seed_offset(self, user_id: str) -> float:
        """按（角色, 用户）稳锚的基线错位：同一份好感度，她对不同人的默认上心度不同。"""
        raw = f"{self._core.role_id}:{user_id}"
        digest = hashlib.blake2b(raw.encode("utf-8"), digest_size=4).digest()
        return (int.from_bytes(digest, "big") / 4294967295.0 - 0.5) * 2.0 * CARE_SEED_SPAN

    def _care_target(self, user_id: str, profile: Dict[str, Any]) -> float:
        """目标在意度：好感度为主，脾气与“她本来对这个人是什么缘分”为辅。"""
        try:
            affection = float(profile.get("affection", 50.0))
            aggression = float(profile.get("aggression", 15.0))
        except (TypeError, ValueError):
            affection, aggression = 50.0, 15.0
        value = affection / 100.0 * 0.78 + 0.11
        value -= min(0.22, max(0.0, aggression - 25.0) / 100.0 * 0.6)
        value += self._seed_offset(user_id) * 0.5
        return max(0.0, min(1.0, value))

    def care(self, user_id: str, now: float, persist: bool = True) -> float:
        """在意度：惰性积分，不每条消息写盘。

        `persist=False` 用于群聊里没开成员情绪档案的场合：那种模式下连 mood 档案都不该
        建（旧版有一条测试专门卡这个），在意度更不能顺手把用户条目写出来。
        """
        scope = self._core.scope
        record = scope.get_user(user_id, "attention") or {}
        try:
            current = float(record.get("care", 0.0))
            at = float(record.get("care_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            current, at = 0.0, 0.0
        try:
            profile = self._core.mood.profile(user_id)
        except Exception:
            profile = {}
        target = self._care_target(user_id, profile)
        if current <= 0.0 or at <= 0.0:
            # 第一面直接落在目标上（含基线错位）：这一轴不该从 0 慢慢爬。
            value = target
        else:
            hours = max(0.0, (now - at) / 3600.0)
            decay = math.pow(0.5, hours / CARE_HALF_LIFE_HOURS) if hours > 0 else 1.0
            value = target + (current - target) * decay
        value = round(max(0.0, min(1.0, value)), 4)
        if persist and (abs(value - current) >= 0.01 or (now - at) >= CARE_EMA_MIN_GAP_SECONDS):
            scope.set_user(user_id, "attention", {"care": value, "care_at": now})
        return value

    def _focus(self, user_id: str, text: str, profile: Dict[str, Any]) -> float:
        """上心程度：对TA这句活本身。每条现算，不存。"""
        body = (text or "").strip()
        if not body:
            return 0.20
        # 同上：0.34 卡在 FOCUS_WORDS 的中档门槛（0.45）下方，于是「你好」这种短消息
        # 也会被描述成「她心思飘在别处／注意力没放在对话上」——而它和同一句里的
        # 「她现在很闲」直接打架（有空却没在听）。0.46 让默认落在「心思比较齐」。
        value = 0.46
        # 很多人打字不带问号，但「…吗」「…呢」就是在问她。
        if any(ch in body for ch in "?？") or body[-1] in ("吗", "呢", "吧", "啊", "呀"):
            value += 0.18
        if len(body) >= 24:
            value += 0.12
        elif len(body) >= 10:
            value += 0.06
        if any(ch in body for ch in "!！😭😡❤️"):
            value += 0.06
        # 推到她今天正在做的事上：这是最强的上心信号。
        if self._touches_her_day(body):
            value += 0.20
        # 推到 TA 自己之前说过、她记着的事。
        if self._touches_recalled(user_id, body):
            value += 0.16
        try:
            aggression = float(profile.get("aggression", 15.0))
            libido = float(profile.get("libido", 25.0))
        except (TypeError, ValueError):
            aggression, libido = 15.0, 25.0
        value -= min(0.20, max(0.0, aggression - 40.0) / 100.0 * 0.7)
        value += min(0.10, max(0.0, libido - 42.0) / 100.0 * 0.4)
        return round(max(0.0, min(1.0, value)), 3)

    def _her_today_keywords(self) -> List[str]:
        """她今天日程里的事件名（呷掉“睡眠/吃饭”这类不算话题的）。"""
        words: List[str] = []
        try:
            slots = self._core.schedule.current_slots()
        except Exception:
            return words
        for slot in slots or []:
            event = str(slot.get("event") or "").strip()
            if not event or is_sleep_event(event) or is_meal_event(event):
                continue
            words.extend(_chunks(event))
        return words[:60]

    def _touches_her_day(self, body: str) -> bool:
        return any(word and word in body for word in self._her_today_keywords())

    def _touches_recalled(self, user_id: str, body: str) -> bool:
        try:
            lines = self._core.mood.recall_lines(user_id, 6)
        except Exception:
            return False
        for line in lines:
            text = str(line)
            for word in _chunks(text):
                if word and word in body:
                    return True
        return False

    def _spare(self, now: float) -> float:
        """注意力余量：身体与手上在做的事给她剩多少。"""
        core = self._core
        value = 0.85
        try:
            body = core.soma.snapshot()
        except Exception:
            body = {}
        if body:
            value -= max(0.0, float(body.get("sleep_pressure", 0.0)) - 45.0) / 100.0 * 0.9
            value -= max(0.0, float(body.get("discomfort", 0.0)) - 40.0) / 100.0 * 0.6
            value -= max(0.0, float(body.get("hunger", 0.0)) - 65.0) / 100.0 * 0.3
            if float(body.get("asleep", 0.0)) >= 1.0:
                value -= 0.45
        try:
            proc = core.process.current()
        except Exception:
            proc = {}
        name = str(proc.get("name") or "").strip()
        phase = str(proc.get("phase") or "").strip()
        busy = any(word in f"{name}{phase}" for word in ("会", "通勤", "开车", "上课", "上班", "做饭", "排队", "加班", "考试", "开会"))
        if busy:
            value -= 0.30
        try:
            value -= max(0.0, 40.0 - float(core.energy.energy)) / 100.0 * 0.5
        except Exception:
            pass
        return round(max(0.0, min(1.0, value)), 3)

    def interest_state(self, user_id: str, now: float, text: str = "", is_group: bool = False) -> Dict[str, float]:
        """三轴汇总。拿不到子服务时全部给中性值，不抛错。

        群聊里没开成员情绪档案时返回空：那条路上连关系都不记，注意力更不该去建用户条目。
        """
        cfg = self.config
        if cfg.mood_enabled and is_group and not cfg.mood_enabled_in_group:
            return {}
        if not cfg.mood_enabled:
            return {"care": 0.5, "focus": self._focus(user_id, text, {}), "spare": self._spare(now)}
        try:
            profile = self._core.mood.profile(user_id)
        except Exception:
            profile = {}
        try:
            care_value = self.care(user_id, now)
        except Exception:
            care_value = 0.5
        try:
            focus_value = self._focus(user_id, text, profile)
        except Exception:
            focus_value = 0.4
        try:
            spare_value = self._spare(now)
        except Exception:
            spare_value = 0.7
        return {"care": care_value, "focus": focus_value, "spare": spare_value}


# 中文没分词：从一段话里抽 2~4 字的滑窗当作关键词，只用于“这句话里有没有提到它”。
_CHUNK_SKIP = set("，。、；！？…~（）()【】“”" + '"\' ')


def _chunks(text: str) -> List[str]:
    cleaned = "".join(ch for ch in text if ch not in _CHUNK_SKIP)[:40]
    if len(cleaned) < 2:
        return []
    out: List[str] = []
    for size in (4, 3, 2):
        for start in range(0, max(0, len(cleaned) - size + 1)):
            out.append(cleaned[start:start + size])
    return out

