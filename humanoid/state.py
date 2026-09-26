"""状态持久化：单一 state.json + 异步锁 + 去抖原子写 + 统一迁移。"""

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

# 迁移前必须保证类型的容器字段（仅用于读入旧文件时的类型兼容）。
_CONTAINER_FIELDS: dict[str, type] = {
    "roles": dict,
}

# 旧版单角色结构里属于“角色自身”的字段。
_LEGACY_SELF_FIELDS = (
    "energy", "max_energy", "social_energy", "current_cycle_day", "last_cycle_update",
    "last_update", "today_date", "daily_schedule", "schedule_source", "schedule_generated_at",
    "_cached_weather_obj", "_last_weather_fetch", "_cached_location", "_mood_decay_last_run",
    "_last_social_energy_reset_date", "_schema_migrated_to", "soma",
)

# 旧版“按用户分字典”的字段 → v3 里用户条目下的字段名。
_LEGACY_USER_FIELDS = {
    "moods": "mood",
    "mood_logs": "mood_logs",
    "nicknames": "nickname",
    "mood_tags": "mood_tag",
    "user_last_seen": "last_interaction",
    "last_message": "last_message",
}

_DROPPED_FIELDS = ("_energy_noise_date",)


def default_state(today: str = "", cycle_day: int = 1) -> dict[str, Any]:
    """v3 只存两样东西：版本号与角色表。

    v2 时代把角色字段同时写一份到顶层“保兼容”，多角色后那份拷贝只属于恰好排在第一个的
    角色，既读不到也会把文件撑大，因此不再保留。
    """
    return {"_state_version": STATE_VERSION, "roles": {}}


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
        """把任意旧版结构收敛成 v3：顶层只留版本号与角色表。

        v2.12 之前的 `_migrate` 有两个坑：一是把角色字段再复制一份到顶层「保兼容」，
        多角色下那份拷贝只属于恰好排在第一个的角色；二是 `roles` 为空时按 set 顺序逐个
        `users = state.pop(key)`，把用户表整体覆盖掉，迁移结果取决于集合遍历顺序。
        现在两类字段都是「并入 default 角色的 self / users」，谁也不覆盖谁。
        """
        state = dict(raw)

        for key in _DROPPED_FIELDS:
            state.pop(key, None)

        if not isinstance(state.get("roles"), dict):
            state["roles"] = {}
        roles = state["roles"]

        moved = self._absorb_legacy(state, roles)
        if moved:
            self._info(f"旧版顶层字段已并入 default 角色（{moved} 项）")
            self.mark_dirty()

        for key, expected in _CONTAINER_FIELDS.items():
            if not isinstance(state.get(key), expected):
                state[key] = expected()

        for role_id, role_data in list(roles.items()):
            if not isinstance(role_data, dict):
                roles[role_id] = {"self": {}, "users": {}}
                continue
            if not isinstance(role_data.get("self"), dict):
                role_data["self"] = {}
            if not isinstance(role_data.get("users"), dict):
                role_data["users"] = {}
            self._normalize_self(role_data["self"], cycle_length)

        state["_state_version"] = STATE_VERSION
        return state

    def _absorb_legacy(self, state: dict[str, Any], roles: dict[str, Any]) -> int:
        """把 v1/v2 的顶层字段并入 roles["default"]，并从顶层删掉它们。"""
        legacy_self = {key: state[key] for key in _LEGACY_SELF_FIELDS if key in state}
        legacy_users = {key: state[key] for key in _LEGACY_USER_FIELDS if key in state}
        if not legacy_self and not legacy_users:
            return 0

        target = roles.setdefault("default", {"self": {}, "users": {}})
        if not isinstance(target, dict):
            target = {"self": {}, "users": {}}
            roles["default"] = target
        self_state = target.setdefault("self", {})
        users_state = target.setdefault("users", {})

        moved = 0
        for key, value in legacy_self.items():
            state.pop(key, None)
            if key not in self_state:
                self_state[key] = value
                moved += 1

        for old_key, new_key in _LEGACY_USER_FIELDS.items():
            if old_key not in legacy_users:
                continue
            payload = legacy_users[old_key]
            state.pop(old_key, None)
            if not isinstance(payload, dict):
                continue
            for uid, value in payload.items():
                entry = users_state.setdefault(str(uid), {})
                if isinstance(entry, dict) and new_key not in entry:
                    entry[new_key] = value
                    moved += 1
        return moved

    @staticmethod
    def _normalize_self(self_state: dict[str, Any], cycle_length: int) -> None:
        """钳制角色自身状态里可能被打坏的数值。"""
        self_state["energy"] = _clamp_float(self_state.get("energy"), 80.0, 0.0, 1e6)
        self_state["social_energy"] = _clamp_float(self_state.get("social_energy"), 100.0, 0.0, 100.0)
        self_state["_mood_decay_last_run"] = _clamp_float(
            self_state.get("_mood_decay_last_run"), 0.0, 0.0, 1e18
        )
        length = max(1, int(cycle_length))
        try:
            day = int(self_state.get("current_cycle_day", 1))
        except (TypeError, ValueError):
            day = 1
        self_state["current_cycle_day"] = (day - 1) % length + 1 if day >= 1 else 1

    async def start(self) -> None:
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop(), name="humanoid-state-flush")

    async def stop(self) -> None:
        task, self._flush_task = self._flush_task, None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # 卸载时的最后一次落盘失败（磁盘满、只读）**不能抛出去**：那会顺着
        # terminate() 冒到 AstrBot，把「插件卸载」变成一个报错事件。记一条日志就够了——
        # 状态文件本来就是缓存，丢一次不影响它下次从零开始。
        try:
            await self.flush()
        except Exception as exc:
            logger = getattr(self, "_log", None)
            if logger is not None:
                try:
                    logger.warning(f"[humanoid_core] 卸载时落盘失败（不影响使用）: {exc}")
                except Exception:
                    pass

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
            payload = _serialize(self._state)
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
        _atomic_write(self._path, _serialize(self._state))
        self._dirty = False
        self.writes += 1
        return True

    def _info(self, message: str) -> None:
        if self._log is not None:
            self._log.info(f"[humanoid_core] {message}")

    def _warn(self, message: str) -> None:
        if self._log is not None:
            self._log.warning(f"[humanoid_core] {message}")


def _serialize(state: dict[str, Any]) -> str:
    """序列化整个状态。

    紧凑格式（无缩进）：indent=2 会让文件体积翻倍、dumps 耗时约 4 倍，而这份文件
    每落盘一次就要在事件循环上完整序列化一遍，用户数上千时足以卡住 AstrBot。
    必须留在事件循环上执行：放到线程里会与并发的 dict 写入赛跑（dictionary changed
    size during iteration），而当前写法保证每次落盘都是一个一致的时间点快照。
    """
    return json.dumps(state, ensure_ascii=False, separators=(",", ":"))


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