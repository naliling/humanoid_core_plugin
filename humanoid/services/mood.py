"""情绪：好感度 / 亲近欲 / 攻击性，含衰减、心情标签与情绪日志。

三条设计约定：

1. **`mood_affection_delta_cap` 的钳制放在所有系数之后。**
   精力系数（最多 ×1.3）和周期系数（最多 ×1.5）会放大变化量，先钳制再乘等于
   让上限失效，实际单次变化可达配置值的两倍。
2. **衰减按用户各自记账**（每份档案自带 `last_decay`）。
   消息路径只衰减当前用户，全量清扫交给后台低频任务，避免每条消息都遍历全表。
3. **`messages_since_llm` 从 0 起算。**
   每 `mood_llm_interval_messages` 条消息才调一次模型，新面孔的第一条消息只走本地
   词典规则 —— 不为建档花一次模型调用，但仍然产生正常的情绪波动。
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..config import HumanoidConfig
from ..data.mood_map import generate_mood_tag, get_mood_label
from ..jsonx import extract_json_object
from ..llm import PURPOSE_MOOD, LLMGateway
from ..state import StateStore

# 冷却记账要按用途分区，所以用途常量的唯一来源在 llm.py
PURPOSE = PURPOSE_MOOD

NEGATIVE_PATTERN = re.compile(
    r"(傻|蠢|笨|白痴|废物|垃圾|去死|死吧|滚蛋|操|妈|逼|贱|恶心|讨厌|恨|烦|骂|吵|滚|弱智|脑残|sb|煞笔)",
    re.IGNORECASE,
)
POSITIVE_PATTERN = re.compile(r"(爱|喜欢|好|棒|厉害|赞|开心|谢谢|感谢|乖|可爱|聪明)", re.IGNORECASE)

AFFECTION_RANGE = (0.0, 100.0)
LIBIDO_RANGE = (0.0, 50.0)
AGGRESSION_RANGE = (0.0, 50.0)

# 三个维度 → (当前值键, 基线值键, 取值范围)
DIMENSIONS: tuple[tuple[str, str, tuple[float, float]], ...] = (
    ("affection", "base_affection", AFFECTION_RANGE),
    ("libido", "base_libido", LIBIDO_RANGE),
    ("aggression", "base_aggression", AGGRESSION_RANGE),
)

LLM_WEIGHT = 0.3
STRONG_NEGATIVE_THRESHOLD = -1.5
STRONG_NEGATIVE_BOOST = 1.2
MIN_DECAY_HOURS = 0.1


@dataclass(frozen=True, slots=True)
class Delta:
    affection: float = 0.0
    libido: float = 0.0
    aggression: float = 0.0

    def scaled(self, factor: float) -> Delta:
        return Delta(self.affection * factor, self.libido * factor, self.aggression * factor)

    def blend(self, other: Delta, other_weight: float) -> Delta:
        mine = 1.0 - other_weight
        return Delta(
            self.affection * mine + other.affection * other_weight,
            self.libido * mine + other.libido * other_weight,
            self.aggression * mine + other.aggression * other_weight,
        )

    def capped(self, cap: float) -> Delta:
        cap = abs(cap)
        return Delta(
            max(-cap, min(cap, self.affection)),
            max(-cap, min(cap, self.libido)),
            max(-cap, min(cap, self.aggression)),
        )


def local_delta(text: str) -> Delta:
    """本地规则判定：命中负面词、正面词、或都不命中时的三档取值区间。"""
    if NEGATIVE_PATTERN.search(text):
        return Delta(random.uniform(-4, -2), random.uniform(-2, -1), random.uniform(2, 4))
    if POSITIVE_PATTERN.search(text):
        return Delta(random.uniform(1, 3), random.uniform(0.5, 2), random.uniform(-1, -0.5))
    return Delta(random.uniform(-0.5, 0.5), random.uniform(-0.3, 0.3), random.uniform(-0.3, 0.3))


def _clamp(value: float, bounds: tuple[float, float]) -> float:
    return max(bounds[0], min(bounds[1], value))


def _as_timestamp(value: Any) -> float:
    """把档案里的时间戳读成 float。不可解析、负数或 0（老版本的哨兵值）都返回 0.0。"""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    if out != out or out <= 0.0:  # NaN 或非正数
        return 0.0
    return out


class MoodService:
    __slots__ = ("_clock", "_config", "_gateway", "_lock", "_log", "_state", "_time")

    def __init__(
        self,
        state: StateStore,
        config_provider: Callable[[], HumanoidConfig],
        gateway: LLMGateway,
        clock: Any = None,
        logger: Any = None,
        time_source: Callable[[], float] = time.time,
    ) -> None:
        self._state = state
        self._config = config_provider
        self._gateway = gateway
        self._clock = clock
        self._log = logger
        self._time = time_source
        self._lock = asyncio.Lock()

    # ---------- 档案读写 ----------

    def profile(self, qq: str) -> dict[str, Any]:
        """取用户情绪档案，不存在则按配置初始化。"""
        qq = str(qq)
        moods = self._state.data.setdefault("moods", {})
        record = moods.get(qq)
        if isinstance(record, dict):
            return self._repair(record)

        cfg = self._config()
        affection = float(cfg.mood_initial_affection)
        override = cfg.affection_override_for(qq)
        if override is not None:
            affection = override
        libido = float(cfg.mood_initial_libido)
        aggression = float(cfg.mood_initial_aggression)
        now = self._time()
        record = {
            "affection": affection,
            "libido": libido,
            "aggression": aggression,
            "base_affection": affection,
            "base_libido": libido,
            "base_aggression": aggression,
            "last_interaction": now,
            "last_decay": now,
            "turn_count": 0,
            # 从 0 起算：新面孔的第一条消息只走本地词典规则，不为建档花一次模型调用
            "messages_since_llm": 0,
        }
        moods[qq] = record
        self._state.mark_dirty()
        return record

    def _repair(self, record: dict[str, Any]) -> dict[str, Any]:
        """补齐老档案缺失的字段（如早期版本没有的 last_decay）并把数值夹回合法区间。"""
        changed = False
        for key, base_key, bounds in DIMENSIONS:
            for target in (key, base_key):
                try:
                    value = float(record.get(target, bounds[0]))
                except (TypeError, ValueError):
                    value = bounds[0]
                clamped = _clamp(value, bounds)
                if record.get(target) != clamped:
                    record[target] = clamped
                    changed = True
        if "last_decay" not in record:
            record["last_decay"] = float(record.get("last_interaction") or self._time())
            changed = True
        if "turn_count" not in record:
            record["turn_count"] = 0
            changed = True
        if "messages_since_llm" not in record:
            record["messages_since_llm"] = 0
            changed = True
        # v2.11.0 及更早的建档把 last_interaction 存成 0.0（哨兵值）。键是存在的，所以不能只
        # 判断「缺失」—— 留着 0 会让 cleanup_expired_users 把这些老档案当成过期用户删掉。
        if not _as_timestamp(record.get("last_interaction")):
            record["last_interaction"] = self._time()
            changed = True
        if changed:
            self._state.mark_dirty()
        return record

    def label(self, qq: str) -> str:
        data = self.profile(qq)
        return get_mood_label(data["affection"], data["libido"], data["aggression"])

    def tag(self, qq: str) -> str:
        if not self._config().mood_tag_enabled:
            return ""
        return str(self._state.data.setdefault("mood_tags", {}).get(str(qq), ""))

    def update_tag(self, qq: str, energy: float) -> str:
        cfg = self._config()
        if not cfg.mood_tag_enabled:
            return ""
        data = self.profile(qq)
        text = generate_mood_tag(data["affection"], data["libido"], data["aggression"], energy)
        self._state.data.setdefault("mood_tags", {})[str(qq)] = text
        self._state.mark_dirty()
        return text

    def logs(self, qq: str, limit: int = 10) -> list[dict[str, Any]]:
        entries = self._state.data.setdefault("mood_logs", {}).get(str(qq), [])
        if not isinstance(entries, list):
            return []
        return entries[-limit:] if limit > 0 else list(entries)

    # ---------- 衰减 ----------

    def decay_user(self, qq: str) -> bool:
        """只衰减一个用户。消息路径走这条，避免遍历全表。"""
        cfg = self._config()
        if not cfg.mood_enabled:
            return False
        return self._decay_record(self.profile(qq), cfg, self._time())

    def decay_all(self) -> int:
        """全量清扫，供后台低频任务调用。返回被改动的用户数。"""
        cfg = self._config()
        if not cfg.mood_enabled:
            return 0
        now = self._time()
        moods = self._state.data.setdefault("moods", {})
        changed = 0
        for record in list(moods.values()):
            if isinstance(record, dict) and self._decay_record(self._repair(record), cfg, now):
                changed += 1
        self._state.data["_mood_decay_last_run"] = now
        if changed:
            self._state.mark_dirty()
        return changed

    def _decay_record(self, record: dict[str, Any], cfg: HumanoidConfig, now: float) -> bool:
        try:
            last = float(record.get("last_decay", now))
        except (TypeError, ValueError):
            last = now
        elapsed_hours = (now - last) / 3600.0
        if elapsed_hours < MIN_DECAY_HOURS:
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
            updated = _clamp(current - deviation * ratio, bounds)
            if abs(updated - current) > 1e-4:
                record[key] = updated
                changed = True
        record["last_decay"] = now
        self._state.mark_dirty()
        return changed

    # ---------- 单次对话的情绪更新 ----------

    async def update_from_message(
        self, qq: str, text: str, energy: float, cycle_day: int, umo: str | None = None
    ) -> Delta | None:
        cfg = self._config()
        if not cfg.mood_enabled:
            return None
        message = (text or "").strip()
        if not message:
            return None

        async with self._lock:
            record = self.profile(qq)
            record["last_interaction"] = self._time()

            try:
                messages_since = int(record.get("messages_since_llm", 0)) + 1
            except (TypeError, ValueError):
                messages_since = 1
            record["messages_since_llm"] = messages_since
            interval = max(1, cfg.mood_llm_interval_messages)
            should_call_llm = cfg.mood_use_llm_for_delta and messages_since >= interval
            if should_call_llm:
                record["messages_since_llm"] = 0
            self._state.mark_dirty()

            if cfg.mood_verbose_log:
                self._info(
                    f"用户 {qq} 消息计数：{messages_since}/{interval}，"
                    f"{'将调用LLM' if should_call_llm else '不调用'}"
                )

        base = local_delta(message)
        llm = None
        if should_call_llm:
            llm = await self._llm_delta(cfg, record, message, umo)

        async with self._lock:
            record = self.profile(qq)
            delta = self._resolve_delta(base, llm)
            delta = self._apply_modifiers(delta, cfg, energy, cycle_day, record)
            self._commit(qq, record, delta, cfg, energy)
            return delta

    def _resolve_delta(self, base: Delta, llm: Delta | None) -> Delta:
        if llm is None:
            return base
        if base.affection < STRONG_NEGATIVE_THRESHOLD:
            return base.scaled(STRONG_NEGATIVE_BOOST)
        return base.blend(llm, LLM_WEIGHT)

    def _apply_modifiers(
        self, delta: Delta, cfg: HumanoidConfig, energy: float, cycle_day: int, record: dict[str, Any]
    ) -> Delta:
        delta = delta.scaled(max(0.0, cfg.mood_sensitivity / 100.0))

        # 精力影响
        if energy > 70:
            delta = Delta(
                delta.affection * 1.3 if delta.affection > 0 else delta.affection,
                delta.libido * 1.3 if delta.libido > 0 else delta.libido,
                delta.aggression * 1.3 if delta.aggression > 0 else delta.aggression,
            )
        elif energy < 40:
            delta = delta.scaled(0.8)

        # 生理周期
        phase = cfg.cycle_phase_index(cycle_day)
        if phase == 0:
            delta = Delta(
                delta.affection * (0.5 if delta.affection > 0 else 1.5),
                delta.libido * (0.5 if delta.libido > 0 else 1.5),
                delta.aggression * (0.8 if delta.aggression > 0 else 1.5),
            )
        elif phase == 2:
            delta = Delta(
                delta.affection * 1.4 if delta.affection > 0 else delta.affection,
                delta.libido * 1.4 if delta.libido > 0 else delta.libido,
                delta.aggression * 1.2 if delta.aggression > 0 else delta.aggression,
            )

        # 好感度高时攻击性自然回落
        affection = float(record.get("affection", 50))
        if affection > 80:
            if delta.aggression > 0:
                delta = Delta(delta.affection, delta.libido, delta.aggression * 0.5)
            elif delta.aggression < 0:
                delta = Delta(delta.affection, delta.libido, delta.aggression * 1.2)

        return delta.capped(cfg.mood_affection_delta_cap)

    def _commit(
        self, qq: str, record: dict[str, Any], delta: Delta, cfg: HumanoidConfig, energy: float
    ) -> None:
        before = {key: float(record[key]) for key, _, _ in DIMENSIONS}
        amounts = {"affection": delta.affection, "libido": delta.libido, "aggression": delta.aggression}

        # turn 是「包含本次在内的第几轮」。`_ensure` 存的是 0，所以先自增再用，
        # 否则第一条消息会被记成第 2 轮，`/情绪详情` 的轮次会一直多一。
        try:
            turn = int(record.get("turn_count", 0) or 0) + 1
        except (TypeError, ValueError):
            turn = 1
        base_coef = 1.0 if turn <= 10 else 0.2
        for key, base_key, bounds in DIMENSIONS:
            amount = amounts[key]
            record[key] = _clamp(before[key] + amount, bounds)
            drift = amount * base_coef * 0.5
            if key == "affection" and amount < -1:
                drift += amount * 0.3
            record[base_key] = _clamp(float(record[base_key]) + drift, bounds)

        record["turn_count"] = turn
        record["last_interaction"] = self._time()
        self._log_event(qq, before, record, cfg)
        self.update_tag(qq, energy)
        self._state.mark_dirty()

    async def _llm_delta(
        self, cfg: HumanoidConfig, record: dict[str, Any], message: str, umo: str | None
    ) -> Delta | None:
        prompt = (
            f"用户说：{message}\n"
            f"当前情绪状态：好感度 {record['affection']:.1f}/100，"
            f"亲近欲 {record['libido']:.1f}/50，攻击性 {record['aggression']:.1f}/50\n"
            "请分析这句话会让 AI 对用户的情绪产生什么变化。只返回 JSON："
            '{"affection_delta": 数值(-5~5), "libido_delta": 数值(-5~5), "aggression_delta": 数值(-5~5)}'
        )
        result = await self._gateway.generate(
            prompt=prompt,
            chain=cfg.mood_provider_ids,
            allow_global=cfg.schedule_allow_global_fallback,
            timeout=float(cfg.mood_update_timeout),
            attempts_per_provider=1,
            purpose=PURPOSE,
            umo=umo,
        )
        if not result.ok:
            self._warn(f"情绪分析失败，本次仅用本地规则：{result.summary()}")
            return None
        payload = extract_json_object(result.text)
        if not payload:
            self._warn("情绪分析返回内容无法解析为 JSON，本次仅用本地规则")
            return None

        def pick(key: str) -> float:
            try:
                return max(-5.0, min(5.0, float(payload.get(key, 0.0))))
            except (TypeError, ValueError):
                return 0.0

        # 成功行保留耗时（用户靠它确认专用模型真的在工作），但不带用户原文 ——
        # 默认每 5 条消息就是一次，无条件打印等于把聊天内容持续写进日志。
        self._info(f"情绪分析 LLM 调用成功，耗时 {result.elapsed:.1f}s")
        if cfg.debug_mode:
            self._info(f"情绪分析消息片段：{message[:30]}")

        return Delta(pick("affection_delta"), pick("libido_delta"), pick("aggression_delta"))

    # ---------- 清理过期用户 ----------

    def cleanup_expired_users(self, days: int) -> int:
        """删掉超过 `days` 天没有交互的情绪档案、日志与心情标签。`days <= 0` 表示不清理。"""
        if days <= 0:
            return 0
        now = self._time()
        cutoff = now - days * 86400.0
        moods = self._state.data.get("moods", {})
        removed = 0
        for qq, record in list(moods.items()):
            if not isinstance(record, dict):
                continue
            # 读不出时间戳时按「刚刚交互过」处理：宁可留着，也不要因为一条写坏的记录删数据
            last = _as_timestamp(record.get("last_interaction")) or now
            if last < cutoff:
                del moods[qq]
                self._state.data.get("mood_logs", {}).pop(qq, None)
                self._state.data.get("mood_tags", {}).pop(qq, None)
                removed += 1
                self._state.mark_dirty()
        if removed:
            self._info(f"清理了 {removed} 个过期用户（超过 {days} 天未交互）")
        return removed

    # ---------- 情绪日志 ----------

    def _log_event(
        self, qq: str, before: dict[str, float], record: dict[str, Any], cfg: HumanoidConfig
    ) -> None:
        if not cfg.mood_log_enabled:
            return
        thresholds = {
            "affection": (cfg.mood_log_threshold_affection, "好感度"),
            "libido": (cfg.mood_log_threshold_libido, "亲近欲"),
            "aggression": (cfg.mood_log_threshold_aggression, "攻击性"),
        }
        events: list[str] = []
        for key, (threshold, name) in thresholds.items():
            change = float(record[key]) - before[key]
            if abs(change) >= max(0.0, float(threshold)):
                direction = "上升" if change > 0 else "下降"
                events.append(f"{name}{direction}至 {record[key]:.1f}")
        if not events:
            return

        logs = self._state.data.setdefault("mood_logs", {})
        entries = logs.setdefault(str(qq), [])
        if not isinstance(entries, list):
            entries = []
            logs[str(qq)] = entries
        entries.append(
            {
                "time": self._now_text(),
                "event": "，".join(events),
                "affection": round(float(record["affection"]), 1),
                "libido": round(float(record["libido"]), 1),
                "aggression": round(float(record["aggression"]), 1),
            }
        )
        limit = max(1, int(cfg.mood_log_max_entries))
        if len(entries) > limit:
            logs[str(qq)] = entries[-limit:]
        self._state.mark_dirty()

    def _now_text(self) -> str:
        if self._clock is not None:
            try:
                return self._clock.now().strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ---------- 管理操作 ----------

    def reset(self, qq: str) -> dict[str, Any]:
        cfg = self._config()
        record = self.profile(qq)
        values = {
            "affection": float(cfg.mood_initial_affection),
            "libido": float(cfg.mood_initial_libido),
            "aggression": float(cfg.mood_initial_aggression),
        }
        for key, base_key, _ in DIMENSIONS:
            record[key] = values[key]
            record[base_key] = values[key]
        record["turn_count"] = 0
        record["last_interaction"] = self._time()
        record["last_decay"] = self._time()
        record["messages_since_llm"] = 0
        self._state.mark_dirty()
        return record

    def set_affection(self, qq: str, value: float) -> float:
        record = self.profile(qq)
        clamped = _clamp(float(value), AFFECTION_RANGE)
        record["affection"] = clamped
        record["base_affection"] = clamped
        self._state.mark_dirty()
        return clamped

    def set_affection_batch(self, pairs: list[tuple[str, float]]) -> int:
        applied = 0
        for qq, value in pairs:
            if AFFECTION_RANGE[0] <= value <= AFFECTION_RANGE[1]:
                self.set_affection(qq, value)
                applied += 1
        return applied

    def _info(self, message: str) -> None:
        if self._log is not None:
            self._log.info(f"[humanoid_core] {message}")

    def _warn(self, message: str) -> None:
        if self._log is not None:
            self._log.warning(f"[humanoid_core] {message}")
