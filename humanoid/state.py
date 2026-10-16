"""状态持久化：单一 state.json + 异步锁 + 去抖原子写 + 统一迁移。"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import threading
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
        "_dirty",
        "_dirty_event",
        "_disk_version",
        "_flush_task",
        "_interval",
        "_load_failed",
        "_log",
        "_path",
        "_state",
        "_surrogate_warned",
        "_version_blocked",
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

        moved = self._absorb_legacy(state, roles)
        if moved:
            self._info(f"旧版顶层字段已并入 default 角色（{moved} 项）")
            self.mark_dirty()

        # 无论上面搬没搬成功，旧顶层键都不许留在文件里：docstring 的「顶层只留版本号
        # 与角色表」必须是**无条件**的，否则 roles 里已有一份同名数据时顶层那份拷贝
        # 会永远留着（读不到、白占体积）。
        for key in _LEGACY_TOP_LEVEL:
            state.pop(key, None)

        for key, expected in _CONTAINER_FIELDS.items():
            if not isinstance(state.get(key), expected):
                state[key] = expected()

        for role_id, role_data in list(roles.items()):
            if not isinstance(role_data, dict):
                # 静默重置会把「谁的档案被吞了」这件事藏起来：带上原类型记一条。
                self._warn(
                    f"角色条目 {role_id!r} 不是对象（{type(role_data).__name__}），"
                    "已重置为空角色（self/users 都清空）"
                )
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
            # 求值放在 try 里：`_interval()` 由外部配置提供，抛一次就顺着任务冒出去，
            # 整个落盘循环会**静默死掉**（此后再也不写盘，且没人知道）。
            try:
                interval = max(0.0, float(self._interval()))
            except Exception as exc:
                self._warn(f"读取落盘间隔失败，本轮退回默认 60 秒: {exc}")
                interval = 60.0
            try:
                await asyncio.sleep(interval)
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
            blocked = self._write_block_reason()
            if blocked:
                self._refuse_write(blocked)
                return False
            payload = _serialize(self._state)
            self._dirty = False
            self._dirty_event.clear()
            try:
                scrubbed = await asyncio.to_thread(_atomic_write, self._path, payload)
            except Exception:
                self._dirty = True
                self._dirty_event.set()
                raise
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
        scrubbed = _atomic_write(self._path, _serialize(self._state))
        if scrubbed:
            self._warn_surrogate_once()
        self._dirty = False
        # 不清事件的话，异步 `_flush_loop` 醒来后会立刻再跑一轮空 flush。
        self._dirty_event.clear()
        self.writes += 1
        return True

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

    用紧凑分隔符只为控制体积（`indent=2` 会把文件撑大）。必须留在事件循环上同步
    执行：放到线程里就会与并发的 dict 写入赛跑（`dictionary changed size during
    iteration`），而当前写法保证每次落盘都是一个一致的时间点快照。

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
        os.replace(tmp, path)
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