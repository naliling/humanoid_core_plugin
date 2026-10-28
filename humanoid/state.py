"""状态持久化：单一 state.json + 异步锁 + 去抖原子写 + 统一迁移。"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

STATE_VERSION = 3

# ── 落盘体积 / 节奏（P1-4）──────────────────────────────────────────────
# 状态是**全量重写**的：体积越大，每次序列化 + 写盘越贵。默认 60 秒一次时，
# 2.2 MB / 7,800 用户的生产快照实测每次 231 ms、约 3.2 GB/天写盘。体积上去以后
# 自动拉长落盘间隔（不改 state.json 的任何格式/结构，只改「多久写一次」）。
# 上限 5 分钟：再长的话崩溃丢的状态就太多了。
_FLUSH_MEDIUM_BYTES = 512 * 1024
_FLUSH_MEDIUM_INTERVAL = 180.0
_FLUSH_LARGE_BYTES = 2 * 1024 * 1024
_FLUSH_LARGE_INTERVAL = 300.0
MAX_FLUSH_INTERVAL = 300.0

# 分块序列化：每积累这么多字符就让出一次事件循环（24 KB ≈ 0.1~1 ms CPU）。
_SERIALIZE_CHUNK_CHARS = 24 * 1024
# 单个超长字符串（如 1 MB 的长文本）先按这个长度切成片再编码，避免一次几毫秒以上的
# 单次 JSON 编码——片与片之间同样让出事件循环。
_LARGE_STRING_CHARS = 64 * 1024
# 批量编码：`users` 这类「很多小条目」的字典，一次 `json.dumps` 编一组（走 C 实现），
# 而不是逐条 Python 递归——后者 2.5 MB 状态要 240k 次 dumps、耗时 3~4 秒。每批目标
# 64 KB、硬上限 512 KB（单次 C 编码约 0.1~1.5 ms），批与批之间让出事件循环。
_BATCH_TARGET_CHARS = 64 * 1024
_BATCH_MAX_CHARS = 512 * 1024
_DICT_BATCH_MIN = 8
_LIST_BATCH_MIN = 8
# 一次性告警阈值。
_SLOW_SERIALIZE_SECONDS = 0.05
_WRITE_VOLUME_WARN_BYTES_PER_DAY = 1024 ** 3  # 1 GiB/天
# 连续落盘失败时的退避上限（秒）。
MAX_FAILURE_BACKOFF = 300.0

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

# 顶层**只允许**有 `_state_version` 与 `roles`。这份集合把所有历史顶层键集中起来，
# 迁移时无条件 pop：以前只有 `_DROPPED_FIELDS` 是无条件删的，其余旧键仅在
# `_absorb_legacy` 真的搬成功时才被删——roles 里已有一份同名数据时，顶层那份
# 拷贝就永远留在文件里，既读不到又一直占体积。
_LEGACY_TOP_LEVEL = (
    frozenset(_LEGACY_SELF_FIELDS) | frozenset(_LEGACY_USER_FIELDS) | frozenset(_DROPPED_FIELDS)
)


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
        "_consecutive_write_failures",
        "_dirty",
        "_dirty_event",
        "_dirty_generation",
        "_disk_version",
        "_flush_failures",
        "_flush_task",
        "_interval",
        "_last_payload_bytes",
        "_last_serialize_seconds",
        "_last_write_error",
        "_load_failed",
        "_log",
        "_path",
        "_state",
        "_surrogate_warned",
        "_version_blocked",
        "_warned_once",
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
        # 加载失败 / 盘上版本更新 → 一律拒绝写盘（见 `_write_block_reason`）。
        self._load_failed = False
        self._version_blocked = False
        # 坏字符的那条 warning 只报一次：写不出去的那个字符会一直留在内存里，
        # 每次落盘都会重走一次替换，每 5 秒往日志里刷一行没有任何新信息。
        self._surrogate_warned = False
        self._disk_version = STATE_VERSION
        self._dirty_event = asyncio.Event()
        self._flush_task: asyncio.Task[None] | None = None
        self.lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self.writes = 0
        # 脏世代号：落盘期间如果又有改动，世代号会变，这一轮就不能清脏标记——
        # 否则「序列化那 200 ms 里新写进来的数据」会等到下一轮才落盘（甚至丢失）。
        self._dirty_generation = 0
        # 落盘健康度（P2-32）：连续失败次数用于升级告警与指数退避；累计失败次数
        # 与最后一次错误文本暴露给诊断（见 flush_failures / last_write_error 等属性）。
        self._flush_failures = 0
        self._consecutive_write_failures = 0
        self._last_write_error = ""
        # 上一次序列化/写盘的体积与耗时：自适应间隔与一次性告警都看它。
        self._last_payload_bytes = 0
        self._last_serialize_seconds = 0.0
        # 只报一次的信息（体积、耗时、写盘量、分块序列化退回等），避免每轮刷日志。
        self._warned_once: set[str] = set()

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
        self._dirty_generation += 1
        if not self._dirty_event.is_set():
            self._dirty_event.set()

    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def dirty_generation(self) -> int:
        """脏世代号：每次 mark_dirty 自增，用来判断落盘期间是否有新改动。"""
        return self._dirty_generation

    # ── 落盘健康度（诊断用，P2-32）─────────────────────────────────
    @property
    def flush_failures(self) -> int:
        """累计落盘失败次数（含重试期间每一轮的失败）。"""
        return self._flush_failures

    @property
    def consecutive_write_failures(self) -> int:
        """当前连续落盘失败次数；成功一次即归零。"""
        return self._consecutive_write_failures

    @property
    def last_write_error(self) -> str:
        """最近一次落盘失败的简短原因（空串 = 上次是成功的）。"""
        return self._last_write_error

    @property
    def last_payload_bytes(self) -> int:
        """上一次序列化出来的 JSON 字节数（UTF-8 近似：字符数）。"""
        return self._last_payload_bytes

    @property
    def last_serialize_ms(self) -> float:
        """上一次序列化耗时（毫秒）。分块后它只代表总 CPU 时间，不代表主循环停顿。"""
        return self._last_serialize_seconds * 1000.0

    @property
    def effective_flush_interval(self) -> float:
        """当前生效的落盘间隔（秒）：配置值按体积自适应放大后的结果。"""
        return self._effective_flush_interval()

    def load(self, today: str = "", cycle_length: int = 28) -> None:
        fresh = default_state(today, seed_cycle_day(today or "19700101", cycle_length))
        if not self._path.exists():
            self._state = fresh
            self.mark_dirty()
            return

        # 先读字节、再按多种编码试解码：**编码不对不是「文件损坏」**。以前直接
        # `read_text(encoding="utf-8")`，一份 GBK 手改的文件或带 BOM 的文件都会被判成
        # 损坏备份走，然后重建成空状态——数据其实完好。
        try:
            data = self._path.read_bytes()
        except OSError as exc:
            # 读都读不到（Windows 上杀软/编辑器占用 → PermissionError）时绝不能把
            # `_state` 换成 fresh：文件内容可能完好，只是此刻打不开，下一次 flush 就会
            # 用空状态把它覆盖掉。保留原文件，改为拒绝写盘。
            self._load_failed = True
            self._error(f"状态文件读取失败，已停止写盘以免覆盖原文件: {exc}")
            return

        try:
            raw = json.loads(_decode_bytes(data))
            if not isinstance(raw, dict):
                raise ValueError("state.json 顶层不是对象")
        except Exception as exc:
            if self._backup_corrupt(exc):
                self._state = fresh
                self.mark_dirty()
            return

        # JSON 里没有 NaN/Infinity，可 `json.loads` **默认接受**这三个字面量（写盘侧
        # `allow_nan=False` 拦得住自己，拦不住手改的文件和别的版本写下的文件）。不清的话
        # 同一份文件「读进来是 NaN、写回去是 null」，而中间这段时间里所有
        # `x < 阈值` 对 NaN 都判假：`cool_until=NaN` → `now >= until` 恒假 → 她**永远**
        # 出不了冷静期。在入口一次清掉，下游每个模块就不必各自再防一遍 NaN。
        scrubbed = _scrub_nonfinite(raw)
        if scrubbed:
            self._warn(f"状态里有 {scrubbed} 处非有限数值（NaN/Infinity），已置空")

        self._state = self._migrate(raw, fresh, cycle_length)
        disk_version = _state_version_of(raw)
        if disk_version > STATE_VERSION:
            # 旧版插件读到新版写的文件：照旧读，但**不许**再写回去（写回等于把它降级，
            # 新版加的字段会被整段抹掉）。
            self._version_blocked = True
            self._disk_version = disk_version
            self._error(
                f"状态文件版本 {disk_version} 高于本插件支持的 {STATE_VERSION}："
                "已停止写盘（请升级插件），读取仍按当前版本尽力解析"
            )
        self._info("状态加载成功")

    def _backup_corrupt(self, exc: Exception) -> bool:
        """把损坏的状态文件挪去备份名；返回是否备份成功。

        备份**失败**时不能当作「已处理」：原文件此刻打不开（被占用/无权限）不代表它
        坏了，紧接着拿一份空状态去覆盖它才是真的丢数据。所以失败时置 `_load_failed`
        并让 `flush()`/`flush_sync()` 拒绝写盘。
        """
        # 时间戳精确到微秒也**不够**：Windows 上 `datetime.now()` 的粒度约 15.6 毫秒，
        # 同一刻的两次损坏备份会拿到同一个名字，`os.replace` 直接覆盖前一份（等于把
        # 上一份待查的坏数据销毁）。所以在时间戳之外再探一个没被占用的序号。
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        base = f"{self._path.stem}.corrupt-{stamp}-{os.getpid()}"
        backup = self._path.with_name(f"{base}.json")
        seq = 0
        while backup.exists():
            seq += 1
            backup = self._path.with_name(f"{base}-{seq}.json")
        try:
            os.replace(self._path, backup)
        except OSError as backup_exc:
            self._load_failed = True
            self._error(
                f"状态文件损坏（{exc}）且备份失败（{backup_exc}）：已保留原文件并停止写盘，"
                "确认无误后手动删除该文件再重启即可恢复"
            )
            return False
        self._warn(f"状态文件损坏，已备份为 {backup.name} 并重建默认状态: {exc}")
        return True

    def _write_block_reason(self) -> str:
        """非空 = 此刻禁止写盘（盘上那份内容比内存里的更值得保留）。"""
        if self._load_failed:
            return "上次加载未能安全读取/备份原文件，写盘会覆盖它"
        if self._version_blocked:
            return (
                f"盘上状态版本 {self._disk_version} 高于本插件支持的 {STATE_VERSION}，"
                "写回会把它降级丢字段"
            )
        return ""

    def _refuse_write(self, reason: str) -> None:
        self._error(f"拒绝写盘：{reason}")
        # 脏标记与事件一起清掉，否则 `_flush_loop` 会每个间隔重试一次并把日志刷满；
        # 下一次真的改动会重新置位。
        self._dirty = False
        self._dirty_event.clear()

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

        moved, legacy_repaired = self._absorb_legacy(state, roles)
        if moved:
            self._info(f"旧版顶层字段已并入 default 角色（{moved} 项）")

        # 无论上面搬没搬成功，旧顶层键都不许留在文件里：docstring 的「顶层只留版本号
        # 与角色表」必须是**无条件**的，否则 roles 里已有一份同名数据时顶层那份拷贝
        # 会永远留着（读不到、白占体积）。
        for key in _LEGACY_TOP_LEVEL:
            state.pop(key, None)

        for key, expected in _CONTAINER_FIELDS.items():
            if not isinstance(state.get(key), expected):
                state[key] = expected()

        repaired = legacy_repaired
        for role_id, role_data in list(roles.items()):
            if not isinstance(role_data, dict):
                # 静默重置会把「谁的档案被吞了」这件事藏起来：带上原类型记一条。
                self._warn(
                    f"角色条目 {role_id!r} 不是对象（{type(role_data).__name__}），"
                    "已重置为空角色（self/users 都清空）"
                )
                roles[role_id] = {"self": {}, "users": {}}
                repaired = True
                continue
            if not isinstance(role_data.get("self"), dict):
                role_data["self"] = {}
                repaired = True
            if not isinstance(role_data.get("users"), dict):
                role_data["users"] = {}
                repaired = True
            self._normalize_self(role_data["self"], cycle_length)
            # P1-5：users 的**每一个 value** 也必须是对象。以前只修到 role 级：
            # 一个 int/字符串混进 users 就让那个用户永久失效（get_user 抛
            # AttributeError、set_user 抛 TypeError，重新 load 也修不好）。
            if self._repair_bad_users(role_id, role_data["users"]):
                repaired = True
        if repaired:
            # 修过的坏值必须落盘，否则下次启动又读到同一份坏数据。
            self.mark_dirty()

        # P2-05：顶层只允许 `_state_version` 与 `roles`。历史遗留键（生产快照里实测
        # 残留 `current_process`）既没有读取路径、也不会被后续版本认领，留着只会让每次
        # 全量重写都继续携带。清理动作要留日志：删的可能是某个旧版本的数据。
        allowed_top = {"_state_version", "roles"}
        unknown = [key for key in state if key not in allowed_top]
        for key in unknown:
            state.pop(key, None)
        if unknown:
            self._warn(
                "状态文件顶层有未知键，已删除（顶层只允许 _state_version/roles）："
                + "、".join(repr(key) for key in unknown)
            )
            self.mark_dirty()

        state["_state_version"] = STATE_VERSION
        return state

    def _repair_bad_users(self, role_id: Any, users: dict[str, Any]) -> bool:
        """把 users[*] 里不是对象的条目重置为 {}，返回是否修过（P1-5）。

        只修 role 级（以前的做法）不够：`{"users": {"42": 5}}` 这种文件 load 完
        仍然坏，`RoleScope.user_state` 的 `setdefault` 把 5 原样返回，于是 get_user
        抛 AttributeError、set_user 抛 TypeError——这个用户此后不涨好感、不记精力，
        重新 load 也永远修不好，只剩一条 warning。坏值本来就是**可以丢**的：它已经
        无法被任何代码读懂，重置成空档案至少能让这个用户重新开始。
        """
        repaired = False
        for user_id, entry in list(users.items()):
            if isinstance(entry, dict):
                continue
            self._warn(
                f"角色 {role_id!r} 的用户条目 {user_id!r} 不是对象"
                f"（{type(entry).__name__}）：已重置为空档案"
            )
            users[user_id] = {}
            repaired = True
        return repaired

    def _absorb_legacy(
        self, state: dict[str, Any], roles: dict[str, Any]
    ) -> tuple[int, bool]:
        """把 v1/v2 的顶层字段并入 roles["default"]，并从顶层删掉它们。

        返回 `(并入的项数, 是否顺手修过坏容器)`。坏容器必须在**这里**就兜住：
        `roles["default"]` 的 self/users 被打成非 dict 时，下面 `setdefault` 之后的
        `key not in self_state` / `users_state.setdefault` 会直接抛 TypeError，
        整个 load 变成损坏备份——本来是能救回来的老数据。
        """
        legacy_self = {key: state[key] for key in _LEGACY_SELF_FIELDS if key in state}
        legacy_users = {key: state[key] for key in _LEGACY_USER_FIELDS if key in state}
        if not legacy_self and not legacy_users:
            return 0, False

        repaired = False
        target = roles.setdefault("default", {"self": {}, "users": {}})
        if not isinstance(target, dict):
            target = {"self": {}, "users": {}}
            roles["default"] = target
            repaired = True
        self_state = target.get("self")
        if not isinstance(self_state, dict):
            self_state = {}
            target["self"] = self_state
            repaired = True
        users_state = target.get("users")
        if not isinstance(users_state, dict):
            users_state = {}
            target["users"] = users_state
            repaired = True

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
                key = str(uid)
                entry = users_state.get(key)
                if not isinstance(entry, dict):
                    # 用户条目本身是坏值：迁移层的旧数据比那坨坏值更值得留。
                    entry = {}
                    users_state[key] = entry
                    repaired = True
                if new_key not in entry:
                    entry[new_key] = value
                    moved += 1
        return moved, repaired

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
        """按自适应间隔把脏状态落盘；失败时指数退避并升级告警（P2-32）。

        - 间隔不是固定的：配置值之上按状态体积放大（见 `_effective_flush_interval`）。
        - 失败不是只 WARN 一句就完：连续失败计数、指数退避、连续 ≥3 次升级为 error，
          且**不丢内存状态**（`flush()` 失败时把脏标记重新置位）。
        """
        backoff = 0.0
        while True:
            await self._dirty_event.wait()
            if backoff > 0.0:
                # 上一轮失败：直接按退避时间重试，不再等一整个正常间隔。
                delay = backoff
            else:
                delay = self._effective_flush_interval()
            try:
                await asyncio.sleep(max(0.0, delay))
            except asyncio.CancelledError:
                raise
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                backoff = self._note_flush_failure(exc)
            else:
                backoff = 0.0

    def _effective_flush_interval(self) -> float:
        """配置间隔按状态体积放大：>512 KB 至少 180s、>2 MB 至少 300s，上限 300s。

        `_interval()` 由外部配置提供，抛一次就顺着任务冒出去，整个落盘循环会**静默
        死掉**（此后再也不写盘，且没人知道）——所以求值必须包在 try 里。
        """
        try:
            configured = max(0.0, float(self._interval()))
        except Exception as exc:
            self._warn(f"读取落盘间隔失败，本轮退回默认 60 秒: {exc}")
            configured = 60.0
        target = configured
        if self._last_payload_bytes >= _FLUSH_LARGE_BYTES:
            target = max(target, _FLUSH_LARGE_INTERVAL)
        elif self._last_payload_bytes >= _FLUSH_MEDIUM_BYTES:
            target = max(target, _FLUSH_MEDIUM_INTERVAL)
        return min(MAX_FLUSH_INTERVAL, target)

    async def flush(self) -> bool:
        async with self._write_lock:
            if not self._dirty:
                return False
            blocked = self._write_block_reason()
            if blocked:
                self._refuse_write(blocked)
                return False
            generation = self._dirty_generation
            started = time.perf_counter()
            try:
                # P1-4：分块异步序列化。2.2 MB 的生产快照一次性 dumps 要 231 ms，
                # 那 231 ms 里事件循环完全停摆（消息/流式/TTS/HTTP 回调全排队）。
                payload = await _serialize_async(self._state)
            except Exception as exc:  # 分块编码器自身出 bug 不该让状态写不出去
                self._warn_once(
                    "serialize_async_fallback",
                    f"分块序列化失败，已退回同步序列化（会短暂占用事件循环）: {exc}",
                )
                payload = _serialize(self._state)
            self._last_serialize_seconds = max(0.0, time.perf_counter() - started)
            self._last_payload_bytes = len(payload)
            self._maybe_warn_volume()
            if self._dirty_generation == generation:
                # 序列化期间没有新改动：这份 payload 就是当前状态，可以清脏。
                self._dirty = False
                self._dirty_event.clear()
            # 否则保持脏标记：快照之后写进来的改动不在 payload 里，下一轮必须再写一次。
            try:
                scrubbed = await asyncio.to_thread(_atomic_write, self._path, payload)
            except Exception as exc:
                # 写失败：内存状态原样保留，并把脏标记放回去，下一轮继续重试。
                self._dirty = True
                self._dirty_event.set()
                self._record_write_failure(exc)
                raise
            if self._consecutive_write_failures:
                self._info(
                    f"状态落盘已恢复（此前连续失败 {self._consecutive_write_failures} 次）"
                )
                self._consecutive_write_failures = 0
                self._last_write_error = ""
            if scrubbed:
                self._warn_surrogate_once()
            self.writes += 1
            return True

    def _warn_surrogate_once(self) -> None:
        if self._surrogate_warned:
            return
        self._surrogate_warned = True
        self._warn("状态里有无法用 UTF-8 编码的字符（孤立代理项），已替换后写入")

    def flush_sync(self) -> bool:
        if not self._dirty:
            return False
        blocked = self._write_block_reason()
        if blocked:
            self._refuse_write(blocked)
            return False
        started = time.perf_counter()
        payload = _serialize(self._state)
        self._last_serialize_seconds = max(0.0, time.perf_counter() - started)
        self._last_payload_bytes = len(payload)
        self._maybe_warn_volume()
        try:
            scrubbed = _atomic_write(self._path, payload)
        except Exception as exc:
            self._record_write_failure(exc)
            raise
        if self._consecutive_write_failures:
            self._info(
                f"状态落盘已恢复（此前连续失败 {self._consecutive_write_failures} 次）"
            )
            self._consecutive_write_failures = 0
            self._last_write_error = ""
        if scrubbed:
            self._warn_surrogate_once()
        self._dirty = False
        # 不清事件的话，异步 `_flush_loop` 醒来后会立刻再跑一轮空 flush。
        self._dirty_event.clear()
        self.writes += 1
        return True

    # ── 落盘失败处理 / 一次性告警（P2-32、P1-4 可观测性）──────────────
    def _record_write_failure(self, exc: BaseException) -> None:
        """记一次落盘失败：累计/连续计数、最后错误文本，并按连续次数升级日志。"""
        self._flush_failures += 1
        self._consecutive_write_failures += 1
        self._last_write_error = f"{type(exc).__name__}: {exc}"[:300]
        count = self._consecutive_write_failures
        hint = ""
        if isinstance(exc, PermissionError) or "WinError 5" in str(exc):
            # 现网实测：Windows 上杀软/同步盘会短暂占用 state.json，重试基本能过去。
            hint = "（Windows 上常见于杀软/同步盘占用 state.json，稍后重试）"
        if count >= 3:
            self._error(
                f"状态落盘已连续 {count} 次失败{hint}：会继续按指数退避重试，"
                f"内存里的状态没有丢；如果一直失败请检查数据目录权限/磁盘空间。原因: {exc}"
            )
        else:
            self._warn(f"状态落盘失败（第 {count} 次，稍后重试）{hint}: {exc}")

    def _note_flush_failure(self, exc: BaseException) -> float:
        """返回下一轮落盘失败要退避的秒数（P2-32：5s 起指数增长，上限 300s）。

        正常路径下失败已经由 `flush()` 调 `_record_write_failure()` 记过数；这里只在
        「不是写盘阶段抛的」兜底异常时补记一次。日志只在这里之外记一次，避免重复。
        """
        if self._consecutive_write_failures == 0:
            # 兜底：异常不是写盘阶段抛的（比如兜底的同步序列化也失败了），这里补记。
            self._record_write_failure(exc)
        exponent = min(self._consecutive_write_failures - 1, 6)
        return min(MAX_FAILURE_BACKOFF, 5.0 * (2 ** exponent))

    def _warn_once(self, key: str, message: str) -> None:
        if key in self._warned_once:
            return
        self._warned_once.add(key)
        self._warn(message)

    def _maybe_warn_volume(self) -> None:
        """体积/耗时/写盘量的一次性告警（P1-4 可观测性）。"""
        size = self._last_payload_bytes
        if size >= _FLUSH_LARGE_BYTES:
            self._warn_once(
                "volume_large",
                f"状态文件已达 {size / (1024 * 1024):.1f} MB（>2 MB）：落盘间隔自适应为"
                f" {_FLUSH_LARGE_INTERVAL:.0f} 秒一次，避免频繁全量重写；体积主要来自"
                "用户档案，可调小 stale_user_retention_days / mood_data_retention_days",
            )
        elif size >= _FLUSH_MEDIUM_BYTES:
            self._warn_once(
                "volume_medium",
                f"状态文件已达 {size / 1024:.0f} KB（>512 KB）：落盘间隔自适应为"
                f" {_FLUSH_MEDIUM_INTERVAL:.0f} 秒一次，减少全量重写开销",
            )
        if self._last_serialize_seconds >= _SLOW_SERIALIZE_SECONDS:
            self._warn_once(
                "serialize_slow",
                f"状态序列化耗时 {self._last_serialize_seconds * 1000:.0f} ms"
                f"（体积 {size / 1024:.0f} KB；已分块让出事件循环，主循环不会被整段卡住），"
                "如持续偏高可清理不活跃用户",
            )
        interval = self._effective_flush_interval()
        if interval > 0:
            per_day = size / interval * 86400.0
            if per_day >= _WRITE_VOLUME_WARN_BYTES_PER_DAY:
                self._warn_once(
                    "write_volume",
                    f"状态全量写盘约 {per_day / (1024 ** 3):.1f} GiB/天"
                    f"（{size / 1024:.0f} KB 每 {interval:.0f} 秒一次）：长期运行会持续写盘，"
                    "建议减小状态体积或调大 state_flush_interval_seconds",
                )

    def _info(self, message: str) -> None:
        if self._log is not None:
            self._log.info(f"[humanoid_core] {message}")

    def _warn(self, message: str) -> None:
        if self._log is not None:
            self._log.warning(f"[humanoid_core] {message}")

    def _error(self, message: str) -> None:
        if self._log is not None:
            self._log.error(f"[humanoid_core] {message}")


def _serialize(state: dict[str, Any]) -> str:
    """序列化整个状态。

    用紧凑分隔符只为控制体积（`indent=2` 会把文件撑大）。它是**同步**版本：`flush_sync()`
    和分块编码器出 bug 时的兜底路径用它。常规异步落盘走 `_serialize_async()`（分块 +
    让出事件循环，P1-4）——把整份 `json.dumps` 放进线程反而更糟：会在序列化的同时与
    事件循环里的 dict 写入赛跑（`dictionary changed size during iteration` 或撕裂快照）。

    `allow_nan=False`：Python 默认会把 `NaN`/`Infinity` 写成非标准的 JSON 字面量，
    别的解析器（含下次启动的自己以外的工具）读不了；状态里只要混进一个 NaN
    （坏输入算出来的浮点），整份文件就变成别人读不懂的东西。发现非有限数就把它
    降级成 null 再写——丢一个坏数值，好过写出一份坏文件。

    第二次尝试同样要接住 **TypeError**：`set`/自定义对象这类值根本没有 JSON 表示，
    而原来的写法只接 ValueError，于是它会一路冒到 `flush()`，变成「从此再也写不出
    状态」（每次落盘都在同一个值上失败）。清洗比丢弃整棵树便宜。
    """
    try:
        return json.dumps(
            state, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError):
        sanitized = _finite_safe(state)
        try:
            return json.dumps(
                sanitized, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            )
        except (TypeError, ValueError):
            # 连清洗后都写不出来（循环引用这类）：宁可退回一份最小的合法状态，
            # 也不写出坏文件。
            return json.dumps(
                default_state(), ensure_ascii=False, separators=(",", ":")
            )


class _ChunkedJSONEncoder:
    """把状态编码成 JSON，并在分块边界让出事件循环（P1-4）。

    为什么不让 `json.dumps` 留在事件循环上：2.2 MB / 7,800 用户的生产快照实测
    231 ms/次，那 231 ms 里整个事件循环停摆（消息、流式、TTS、HTTP 回调全部排队）。
    放进线程又不行——`dict` 在序列化的同时被事件循环里的代码修改，会变成
    `RuntimeError: dictionary changed size during iteration` 或者一份撕裂的快照。

    所以：**同一个线程，分块编码，块与块之间 `await asyncio.sleep(0)`**。单块 CPU
    控制在亚毫秒~几毫秒量级，主循环最多等这一块的时长。输出与 `_serialize`
    （紧凑分隔符、`allow_nan=False`）**逐字节一致**；NaN/不可序列化叶子按
    `_finite_safe` 的旧口径降级成 null，不会让整份状态写不出去。
    """

    __slots__ = ("_active", "_done", "parts", "pending")

    def __init__(self) -> None:
        # 两块缓冲区：`_done` 是已经合并好的完成块（每块约 24 KB），`parts` 是当前
        # 待合并的碎片。每个 yield 只把「这一块的碎片」join 一次——如果每次把
        # `parts` 整体（含此前所有内容）重新 join，2 MB 状态累计会复制上百 MB。
        self._done: list[str] = []
        self.parts: list[str] = []
        self.pending = 0
        # 当前递归路径上的容器 id：环引用会让编码无限递归（旧实现抛 RecursionError
        # 让整次落盘失败），这里按 null 降级。
        self._active: set[int] = set()

    async def encode(self, state: Any) -> str:
        await self._encode(state)
        if self.parts:
            self._done.append("".join(self.parts))
            self.parts = []
        return "".join(self._done)

    def _write(self, piece: str) -> None:
        self.parts.append(piece)
        self.pending += len(piece)

    async def _maybe_yield(self) -> None:
        if self.pending < _SERIALIZE_CHUNK_CHARS:
            return
        # 只合并当前块（约 24 KB），保证整体线性；不会重复复制已经产出的内容。
        self._done.append("".join(self.parts))
        self.parts = []
        self.pending = 0
        await asyncio.sleep(0)

    async def _encode(self, value: Any) -> None:
        if isinstance(value, dict):
            await self._encode_dict(value)
        elif isinstance(value, (list, tuple)):
            await self._encode_list(value)
        else:
            await self._encode_leaf(value)

    async def _encode_leaf(self, value: Any) -> None:
        """编码一个标量/小值；超长字符串分片编码，避免单次 CPU 过长。"""
        if isinstance(value, str) and len(value) >= _LARGE_STRING_CHARS:
            # 单个超长字符串（例如一条 1 MB 的长文本）：字符级转义是逐字符的，所以
            # 可以**分片编码再拼**——输出与整体 `json.dumps` 逐字节一致，但每片的
            # 编码 CPU 只有几十微秒~几毫秒，片间能让出事件循环。
            for piece in _dump_string_pieces(value):
                self._write(piece)
                await self._maybe_yield()
            return
        text = _dump_value(value)
        if len(text) < _SERIALIZE_CHUNK_CHARS:
            self._write(text)
            return
        for start in range(0, len(text), _SERIALIZE_CHUNK_CHARS):
            self._write(text[start:start + _SERIALIZE_CHUNK_CHARS])
            await self._maybe_yield()

    async def _encode_value(self, item: Any) -> None:
        """一个容器键/元素：容器递归，标量走 `_encode_leaf`。"""
        if isinstance(item, dict):
            await self._encode_dict(item)
        elif isinstance(item, (list, tuple)):
            await self._encode_list(item)
        else:
            await self._encode_leaf(item)

    async def _encode_dict(self, value: dict[Any, Any]) -> None:
        marker = id(value)
        if marker in self._active:
            self._write("null")
            return
        self._active.add(marker)
        try:
            self._write("{")
            items = list(value.items())
            if len(items) >= _DICT_BATCH_MIN:
                # 大量小条目（users 这类）：整批交给 C 编码，避免逐条 Python 递归。
                await self._encode_batched(items, list_mode=False)
            else:
                for index, (key, item) in enumerate(items):
                    if index:
                        self._write(",")
                    self._write(_dump_key(key))
                    self._write(":")
                    await self._encode_value(item)
                    await self._maybe_yield()
            self._write("}")
        finally:
            self._active.discard(marker)

    async def _encode_list(self, value: Any) -> None:
        marker = id(value)
        if marker in self._active:
            self._write("null")
            return
        self._active.add(marker)
        try:
            self._write("[")
            items = list(value)
            if len(items) >= _LIST_BATCH_MIN:
                await self._encode_batched(items, list_mode=True)
            else:
                for index, item in enumerate(items):
                    if index:
                        self._write(",")
                    await self._encode_value(item)
                    await self._maybe_yield()
            self._write("]")
        finally:
            self._active.discard(marker)

    async def _encode_batched(self, items: list[Any], *, list_mode: bool) -> None:
        """把「很多小条目」分组整批编码：每批一次 `json.dumps`（C 实现），批间让出。

        输出与逐条递归完全一致（`json.dumps` 对 dict/list 的紧凑编码 + 去掉外层括号）。
        批大小按上一批的平均体积自适应，目标 `_BATCH_TARGET_CHARS`；单批产物超过
        `_BATCH_MAX_CHARS` 就拆半重试，拆到单条还超标则退回递归编码那个值。
        """
        total = len(items)
        index = 0
        first = True
        average = 0.0
        while index < total:
            if average <= 0.0:
                size = min(32, total - index)
            else:
                size = max(4, min(1024, int(_BATCH_TARGET_CHARS / average)))
            group = items[index:index + size]
            while True:
                text = _dump_list_items(group) if list_mode else _dump_items(group)
                if len(text) <= _BATCH_MAX_CHARS or len(group) <= 1:
                    break
                group = group[: max(1, len(group) // 2)]
            if len(group) == 1 and len(text) > _BATCH_MAX_CHARS:
                # 单个值本身就过大：退回递归（容器）或分片（长字符串）编码。
                # 注意这里绕过了整批写入，分隔逗号要自己补，否则拼出来的不是合法 JSON。
                if not first:
                    self._write(",")
                first = False
                if list_mode:
                    await self._encode_value(items[index])
                else:
                    key, item = items[index]
                    self._write(_dump_key(key))
                    self._write(":")
                    await self._encode_value(item)
            else:
                self._write(("" if first else ",") + text)
                first = False
            index += len(group)
            average = len(text) / max(1, len(group))
            await self._maybe_yield()


async def _serialize_async(state: dict[str, Any]) -> str:
    """分块异步序列化（P1-4）：输出与 `_serialize` 逐字节一致，但不长时间占用事件循环。"""
    if not isinstance(state, dict):
        return _serialize(state)
    return await _ChunkedJSONEncoder().encode(state)


def _dump_value(value: Any) -> str:
    """编码一个非容器值（或兜底时的整棵子树），失败按 `_finite_safe` 降级。"""
    try:
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError):
        try:
            return json.dumps(
                _finite_safe(value),
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError, RecursionError):
            # 环引用之类：丢一个坏叶子，好过整份状态写不出去。
            return "null"


def _dump_items(items: list[tuple[Any, Any]]) -> str:
    """把若干 (key, value) 编成一个 JSON 对象的**内文**（不含两侧花括号）。

    整批走 C 实现（2.5 MB 状态逐条 Python 递归要 240k 次 dumps，3~4 秒；整批只要
    几十次）。坏值（NaN/不可序列化）时退回逐条编码，语义与 `_dump_value` 一致。
    """
    try:
        encoded = json.dumps(
            dict(items), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        return encoded[1:-1]
    except (TypeError, ValueError, RecursionError):
        return ",".join(
            f"{_dump_key(key)}:{_dump_value(value)}" for key, value in items
        )


def _dump_list_items(items: list[Any]) -> str:
    """把若干元素编成一个 JSON 数组的**内文**（不含两侧方括号）；兜底口径同上。"""
    try:
        encoded = json.dumps(
            items, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        return encoded[1:-1]
    except (TypeError, ValueError, RecursionError):
        return ",".join(_dump_value(item) for item in items)


def _dump_string_pieces(text: str) -> list[str]:
    """把超长字符串**分片**编码成 JSON 字符串（含两侧引号），输出与整体 dumps 一致。

    JSON 的字符串转义是逐字符的（控制字符/引号/反斜杠各转各的），字符之间没有
    跨片状态，所以逐片 `json.dumps` 去引号再拼接 = 整体 `json.dumps`。每个分片内部
    用 `ensure_ascii=False`，与 `_serialize`/`_dump_value` 的口径相同。
    """
    pieces = ["\""]
    for start in range(0, len(text), _LARGE_STRING_CHARS):
        encoded = json.dumps(text[start:start + _LARGE_STRING_CHARS], ensure_ascii=False)
        pieces.append(encoded[1:-1])
    pieces.append("\"")
    return pieces


def _dump_key(key: Any) -> str:
    """按 `json.dumps` 对**字典键**的规则编码（键在 JSON 里一律是字符串）。

    正常状态里键全是 str；int/float/bool/None 键按 json 模块的规则转成字符串，
    其它（tuple 等）按 `_finite_safe` 的旧口径 `str(key)`——与老实现清洗后的输出一致。
    """
    if isinstance(key, str):
        return json.dumps(key, ensure_ascii=False)
    if key is None:
        return '"null"'
    if key is True:
        return '"true"'
    if key is False:
        return '"false"'
    return json.dumps(str(key), ensure_ascii=False)


def _finite_safe(value: Any) -> Any:
    """把写不进 JSON 的东西换成 None（递归处理容器），其余原样。

    两类值会让 `json.dumps(allow_nan=False)` 失败：NaN/±Infinity（ValueError）与
    没有 JSON 表示的对象，如 `set`（TypeError）。原来它只处理第一类，第二类直接
    冒出去——**一个坏叶子就能让整份状态再也写不出去**，而下面的兜底又会把整棵树
    换成空骨架。清洗不该以整棵树为代价。
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            # JSON 的键只能是字符串/数字/布尔/null：tuple 之类的键会让 dumps 抛
            # TypeError，NaN 键在 allow_nan=False 下同样抛。
            if key is None or isinstance(key, (str, int, bool)):
                safe_key = key
            elif isinstance(key, float) and math.isfinite(key):
                safe_key = key
            else:
                safe_key = str(key)
            out[safe_key] = _finite_safe(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_finite_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return None


def _scrub_nonfinite(value: Any) -> int:
    """**就地**把 NaN/±Infinity 换成 None，返回改掉几处。

    与 `_finite_safe` 的分工：那个是「写盘前重新造一棵干净的树」，这个是「读盘后
    只修坏叶子」，因为读进来的树接下来还要按角色/用户就地迁移，不能换对象。
    """
    changed = 0
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, float) and not math.isfinite(item):
                value[key] = None
                changed += 1
            elif isinstance(item, (dict, list)):
                changed += _scrub_nonfinite(item)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, float) and not math.isfinite(item):
                value[index] = None
                changed += 1
            elif isinstance(item, (dict, list)):
                changed += _scrub_nonfinite(item)
    return changed


def _atomic_write(path: Path, payload: str) -> bool:
    """原子写。返回值 = 是否为了写成功而替换过 payload 里的坏字符。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    # `.tmp` 固定名会让两个进程/线程互相踩（A 写完 B 又写，A 的 replace 搬走 B 的内容）。
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    scrubbed = False

    def _write(text: str) -> None:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

    try:
        try:
            _write(payload)
        except UnicodeEncodeError:
            # 孤立代理项（`\ud800` 这类，平台昵称/消息里带进来过）用 UTF-8 编码不出来。
            # 原来它会一路抛到 `flush()`：**此后每次落盘都在同一个字符上失败**，插件
            # 表面照常跑，实则再也不落盘，重启就丢掉期间所有状态。换成 `?` 写下去是
            # 有损的，但比「永远写不出去」好得多（也不能写 WTF-8：那份文件下次自己都
            # 解不开，会被当成损坏备份走）。
            scrubbed = True
            _write(payload.encode("utf-8", "replace").decode("utf-8"))
        _replace_with_retry(tmp, path)
    except BaseException:
        # 失败分支别留下半截临时文件（下次启动的目录扫描会被它误导）。
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise
    # 目录项本身也要落盘，否则断电后可能只有旧文件留下。Windows 不支持对目录
    # fsync（会抛 OSError），失败就算了——那里本来也不保证这一点。
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError:
        return scrubbed
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)
    return scrubbed


def _decode_bytes(data: bytes) -> str:
    """按 utf-8 → utf-8-sig → gbk 试解码；三种都认不出来才算损坏（抛 ValueError）。"""
    last: Exception | None = None
    for encoding in ("utf-8", "utf-8-sig", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError as exc:
            last = exc
    raise ValueError(f"状态文件编码无法识别（已试 utf-8/utf-8-sig/gbk）: {last}")


def _state_version_of(raw: dict[str, Any]) -> int:
    try:
        return int(raw.get("_state_version", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _clamp_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out:
        return default
    return max(low, min(high, out))

def _replace_with_retry(tmp, path, attempts: int = 6) -> None:
    """`os.replace` 带指数退避重试（P2-32）。

    线上出现过 `[WinError 5] 拒绝访问`（杀软/索引/同步工具短暂持有目标文件）：
    一次失败就等于这一轮状态没落盘，紧接着的命令读到旧数据——用户看到的就是
    「问好感度没反应/数字不对」。现网 27 次失败全是这种瞬时占用，原来的 4 次
    × 线性 0.05~0.2 秒（合计约 0.5 秒）在同步盘/杀软扫描时不够用；改成 6 次
    × 指数退避 0.1→1.6 秒（合计约 3.1 秒，跑在写盘线程里不卡事件循环），
    仍失败才让它抛——失败的计数/升级告警在调用方（`_flush_loop`）。
    """
    last: Exception | None = None
    for i in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:      # WinError 5 / EACCES
            last = exc
            time.sleep(min(2.0, 0.1 * (2 ** i)))
        except OSError as exc:
            # 目标被占用也会走这里；同样退避重试
            last = exc
            time.sleep(min(2.0, 0.1 * (2 ** i)))
    if last is not None:
        raise last