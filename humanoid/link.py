"""与「自主拟人社交」插件之间的契约。

v1.7.4 的社交层直接按结构读 Core 的 state.json（`roles[bid].self.energy` 等）。那是
无契约的结构耦合：Core 改个字段名，联动就静默断掉，而且 Core 当时根本没有「她现在多想
说话」这一层可以给它。

现在两边各写各的文件，永不并发写同一个：

* Core → `state.json` 里 `roles[bid].self.contract`，带 `v` 版本号，字段只加不改。
* Social → 自己目录下的 `humanoid_signals.json`，Core 只读，并按 mtime 忽略过期数据。

社交层拿不到契约时（Core 没装、或版本太老）会退回读旧字段，功能不中断。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .services.schedule import (
    day_phrases,
    schedule_wake_text,
    sleep_spans,
)

CONTRACT_VERSION = 1

# 社交层写回的信号文件：超过这个秒数就不采信（它自己也是每分钟才写一次）。
SIGNALS_TTL_SECONDS = 900.0
SOCIAL_PLUGIN_DIR_NAME = "astrbot_plugin_autonomous_social"
SIGNALS_FILE_NAME = "humanoid_signals.json"


def build_contract(core: Any) -> dict[str, Any]:
    """导出这份角色的身体快照。只读，不推进任何状态。"""
    cfg = core.config
    now = core.clock.now()
    snap = core.snapshot(refresh=False)
    soma = snap.get("soma") or {}
    form = snap.get("form_policy") or {}
    slot = core.schedule.current_slot() or {}
    slots = core.schedule.current_slots()
    now_minutes = now.hour * 60 + now.minute
    try:
        # 社交层拿这个去说「我刚从X回来」，不拿它就只能说「我在休息」。
        day = day_phrases(slots, now_minutes, past_limit=3, future_limit=2)
    except Exception:
        day = {}
    feelings: list[str] = []
    if cfg.soma_enabled:
        try:
            feelings = [text for _, text in core.soma.feelings(float(snap["energy"]["value"]))[:3]]
        except Exception:
            feelings = []
    offset = now.utcoffset()
    zone = getattr(now.tzinfo, "key", "") or ""
    return {
        "v": CONTRACT_VERSION,
        "core_version": getattr(core, "version", "") or "",
        "generated_at": round(now.timestamp(), 1),
        "time": {
            "local": now.strftime("%Y-%m-%d %H:%M"),
            "hour": now.hour,
            "weekday": snap.get("weekday", ""),
            "city": snap.get("city", ""),
            "is_night": bool(cfg.night_mode_enabled and core.clock.is_night(now)),
            "is_deep_sleep": bool(cfg.night_mode_enabled and core.clock.is_deep_sleep(now)),
            # 社交层跟 Core 跑在同一台机器上，但它自己的时段判断用的是本机时钟。
            # 给出偏移量比给出一个时刻更可靠：拿它把 epoch 换算成她的本地小时即可。
            # 算不出来时必须是 None 而不是 0：当成 0 等于把她的城市当 UTC，错得静悄悄。
            "utc_offset_minutes": int(offset.total_seconds() // 60) if offset is not None else None,
            # 生效的 IANA 名；空串意味着她其实没拿到真时区（缺 tzdata 或城市认不出）。
            "tz": zone,
        },
        "body": {
            "energy": round(float(snap["energy"]["value"]), 1),
            "energy_text": snap["energy"]["text"],
            "sleep_pressure": soma.get("sleep_pressure"),
            "sleep_debt": soma.get("sleep_debt"),
            "hunger": soma.get("hunger"),
            "discomfort": soma.get("discomfort"),
            "arousal": soma.get("arousal"),
            "asleep": bool(soma.get("asleep")),
            "last_sleep_hours": soma.get("last_sleep_hours"),
            "social_energy": round(float(snap["social_energy"]["value"]), 1),
            "social_desire": soma.get("social_desire"),
            "cycle_day": int(core.energy.cycle_day),
            "cycle_phase": (snap.get("cycle") or "").strip(),
        },
        "feelings": feelings,
        "form": {
            "max_chars": int(form.get("max_chars", 120)),
            "question_bias": float(form.get("question_bias", 0.35)),
            "long_reply_ok": bool(form.get("long_reply_ok", True)),
            "burst_ok": bool(form.get("burst_ok", False)),
        },
        "activity": {
            "name": str((snap.get("process") or {}).get("name", "")),
            "phase": str((snap.get("process") or {}).get("phase", "")),
            "schedule_event": str(slot.get("event", "")),
            "location": str(slot.get("location", "")),
        },
        # 今天这条时线：刚做过什么 / 正在做什么 / 接下来做什么。
        "day": {
            "doing": str(day.get("doing") or ""),
            "done": [str(x) for x in (day.get("done") or []) if str(x).strip()],
            "next": [str(x) for x in (day.get("next") or []) if str(x).strip()],
        },
        # 日程是按哪个 AstrBot 人设排的：社交层靠它确认两边用的是同一个人。
        "persona": str(core.schedule.status().get("persona", "") or ""),
        # 作息：社交层靠它知道她睡够没睡够，诊断靠它提示两套时间不一致。
        "routine": {
            "night_start_hour": cfg.night_start_hour,
            "night_end_hour": cfg.night_end_hour,
            "night_span_hours": round(cfg.night_span_hours, 2),
            "sleep_need_hours": cfg.sleep_need_hours,
            "wake_at": schedule_wake_text(slots),
            "sleep_spans": [
                f"{s.get('start')}-{s.get('end')} {s.get('event')}" for s in sleep_spans(slots)
            ],
        },
        "weather": str((snap.get("weather") or {}).get("env", "")),
        # 契约里没有按用户的数据（1000 人时会把文件撑大）；这些路径是稳定承诺。
        "paths": {
            "user_affection": "roles.<bid>.users.<uid>.mood.affection",
            "user_nickname": "roles.<bid>.users.<uid>.nickname",
            "user_last_interaction": "roles.<bid>.users.<uid>.last_interaction",
            "user_said": "roles.<bid>.users.<uid>.said",
        },
    }


def signals_path(data_root: str | os.PathLike[str] | None) -> Path | None:
    if not data_root:
        return None
    path = Path(data_root).joinpath("plugin_data", SOCIAL_PLUGIN_DIR_NAME, SIGNALS_FILE_NAME)
    return path if path.exists() else None


class SocialSignals:
    """只读社交层写回的信号。文件不存在或过期时一切返回空。"""

    def __init__(self, data_root_provider: Callable[[], Any], time_source: Callable[[], float] = time.time) -> None:
        self._root = data_root_provider
        self._time = time_source
        self._fingerprint: tuple[int, int] | None = None
        self._payload: dict[str, Any] = {}
        self._loaded_at = 0.0

    def read(self) -> dict[str, Any]:
        now = self._time()
        try:
            path = signals_path(self._root())
        except Exception:
            return {}
        if path is None:
            self._fingerprint = None
            self._payload = {}
            return {}
        try:
            stat = path.stat()
        except OSError:
            return {}
        if now - stat.st_mtime > SIGNALS_TTL_SECONDS:
            # 社交层停了：它写的那份「刚主动找过谁」不该继续影响身体。
            self._payload = {}
            return {}
        fingerprint = (stat.st_mtime_ns, stat.st_size)
        if fingerprint == self._fingerprint:
            return self._payload
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._payload or {}
        if not isinstance(payload, dict):
            return self._payload or {}
        self._fingerprint = fingerprint
        self._payload = payload
        self._loaded_at = now
        return payload

    def last_proactive(self) -> tuple[str, float]:
        """社交层最近一次主动开口：(对象 uid, epoch)。没有则 ("", 0.0)。"""
        payload = self.read()
        try:
            at = float(payload.get("last_proactive_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            at = 0.0
        target = str(payload.get("last_target_uid", "") or "")
        return target, at

    def ignored_streak(self) -> int:
        try:
            return max(0, int(self.read().get("ignored_streak", 0) or 0))
        except (TypeError, ValueError):
            return 0
