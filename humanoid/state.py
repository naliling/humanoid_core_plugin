"""状态持久化：单一 state.json + 异步锁 + 去抖原子写。"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

STATE_VERSION = 3

_CONTAINER_FIELDS: dict[str, type] = {
    "roles": dict,
    "daily_schedule": list,
    "nicknames": dict,
    "moods": dict,
    "mood_logs": dict,
    "mood_tags": dict,
    "user_last_seen": dict,
    "last_message": dict,
}

_DROPPED_FIELDS = ("_energy_noise_date",)


def default_state(today: str = "", cycle_day: int = 1) -> dict[str, Any]:
    return {
        "_state_version": STATE_VERSION,
        "roles": {},
        "energy": 80.0,
        "social_energy": 100.0,
        "current_cycle_day": cycle_day,
        "last_cycle_update": today,
        "last_update": "",
        "today_date": "",
        "daily_schedule": [],
        "schedule_source": "",
        "schedule_generated_at": "",
        "_cached_weather_obj": None,
        "_last_weather_fetch": "",
        "_cached_location": "",
        "nicknames": {},
        "moods": {},
        "mood_logs": {},
        "mood_tags": {},
        "user_last_seen": {},
        "last_message": {},
        "_mood_decay_last_run": 0.0,
        "_last_social_energy_reset_date": "",
        "_schema_migrated_to": 0,
    }


def seed_cycle_day(today: str, cycle_length: int = 28) -> int:
    digest = hashlib.md5(today.replace("-", "").encode("utf-8")).hexdigest()[:8]
    return int(digest, 16) % max(1, cycle_length) + 1


class StateStore:
    __slots__ = (
        "_dirty",
        "_dirty_event",
        "_flush_task",
        "_interval",
        "_log",
        "_path",
        "_state",
        "_write_lock",
        "lock",
        "writes",
    )

    def __init__(
        self,
        path: str | Path,
        flush_interval_provider: Callable[[], float] | None = None,
        logger: Any = None,
    ) -> None:
        self._path = Path(path)
        self._interval = flush_interval_provider or (lambda: 5.0)
        self._log = logger
        self._state: dict[str, Any] = default_state()
        self._dirty = False
        self._dirty_event = asyncio.Event()
        self._flush_task: asyncio.Task[None] | None = None
        self.lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self.writes = 0

    @property
    def data(self) -> dict[str, Any]:
        return self._state

    @property
    def path(self) -> Path:
        return self._path

    def get(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._state[key] = value
        self.mark_dirty()

    def mark_dirty(self) -> None:
        self._dirty = True
        if not self._dirty_event.is_set():
            self._dirty_event.set()

    @property
    def dirty(self) -> bool:
        return self._dirty

    def load(self, today: str = "", cycle_length: int = 28) -> None:
        fresh = default_state(today, seed_cycle_day(today or "19700101", cycle_length))
        if not self._path.exists():
            self._state = fresh
            self.mark_dirty()
            return

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("state.json 顶层不是对象")
        except Exception as exc:
            self._backup_corrupt(exc)
            self._state = fresh
            self.mark_dirty()
            return

        self._state = self._migrate(raw, fresh, cycle_length)
        self._info("状态加载成功")

    def _backup_corrupt(self, exc: Exception) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = self._path.with_name(f"{self._path.stem}.corrupt-{stamp}.json")
        try:
            os.replace(self._path, backup)
            self._warn(f"状态文件损坏，已备份为 {backup.name} 并重建默认状态: {exc}")
        except OSError:
            self._warn(f"状态文件损坏且备份失败，使用默认状态: {exc}")

    def _migrate(self, raw: dict[str, Any], fresh: dict[str, Any], cycle_length: int) -> dict[str, Any]:
        state = dict(raw)

        for key in _DROPPED_FIELDS:
            state.pop(key, None)

        for key, value in fresh.items():
            state.setdefault(key, value)

        for key, expected in _CONTAINER_FIELDS.items():
            if not isinstance(state.get(key), expected):
                state[key] = expected()

        state["energy"] = _clamp_float(state.get("energy"), 80.0, 0.0, 1e6)
        state["social_energy"] = _clamp_float(state.get("social_energy"), 100.0, 0.0, 100.0)
        state["_mood_decay_last_run"] = _clamp_float(state.get("_mood_decay_last_run"), 0.0, 0.0, 1e18)

        length = max(1, int(cycle_length))
        try:
            day = int(state.get("current_cycle_day", 1))
        except (TypeError, ValueError):
            day = 1
        state["current_cycle_day"] = (day - 1) % length + 1 if day >= 1 else 1

        try:
            state["_schema_migrated_to"] = int(state.get("_schema_migrated_to", 0))
        except (TypeError, ValueError):
            state["_schema_migrated_to"] = 0

        if state.get("_state_version") != STATE_VERSION:
            state["_state_version"] = STATE_VERSION
            self.mark_dirty()

        state.setdefault("roles", {})
        return state

    async def start(self) -> None:
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop(), name="humanoid-state-flush")

    async def stop(self) -> None:
        task, self._flush_task = self._flush_task, None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.flush()

    async def _flush_loop(self) -> None:
        while True:
            await self._dirty_event.wait()
            try:
                await asyncio.sleep(max(0.0, float(self._interval())))
            except asyncio.CancelledError:
                raise
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._warn(f"状态落盘失败: {exc}")
                await asyncio.sleep(1.0)

    async def flush(self) -> bool:
        async with self._write_lock:
            if not self._dirty:
                return False
            payload = json.dumps(self._state, ensure_ascii=False, indent=2)
            self._dirty = False
            self._dirty_event.clear()
            try:
                await asyncio.to_thread(_atomic_write, self._path, payload)
            except Exception:
                self._dirty = True
                self._dirty_event.set()
                raise
            self.writes += 1
            return True

    def flush_sync(self) -> bool:
        if not self._dirty:
            return False
        payload = json.dumps(self._state, ensure_ascii=False, indent=2)
        _atomic_write(self._path, payload)
        self._dirty = False
        self.writes += 1
        return True

    def _info(self, message: str) -> None:
        if self._log is not None:
            self._log.info(f"[humanoid_core] {message}")

    def _warn(self, message: str) -> None:
        if self._log is not None:
            self._log.warning(f"[humanoid_core] {message}")


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _clamp_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out:
        return default
    return max(low, min(high, out))