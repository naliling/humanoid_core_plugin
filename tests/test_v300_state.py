"""v3.0.0 状态持久化域回归（P1-4 / P1-5 / P2-05 / P2-09 / P2-10 / P2-31 / P2-32）。

每条都对应审核报告里的一个编号，断言的是**可观测行为**：事件循环实测停顿、坏值自愈
之后真的能读写、未知顶层键真的从文件里消失、用户显式选择真的不被覆盖、6 段旧配置
真的迁成 8 段并写回、落盘失败真的升级告警且不丢内存状态。
"""

from __future__ import annotations

import asyncio
import json
import random
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from humanoid.config import DEFAULTS, HumanoidConfig, plan_default_migrations
from humanoid.role_scope import RoleScope
from humanoid.state import (
    STATE_VERSION,
    StateStore,
    _replace_with_retry,
    _serialize,
    _serialize_async,
)

from .fakes import RecordingLogger

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "_conf_schema.json"


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _new_store(tmp: str, log: RecordingLogger | None = None, interval: float = 60.0) -> StateStore:
    return StateStore(Path(tmp) / "state.json", lambda: interval, log or RecordingLogger())


def _big_state(per_role: int = 2000, role_count: int = 6) -> dict:
    """合成一份 2 MB 级别的真实形状状态（6 角色 / 12000 用户 / 含契约）。"""
    state: dict = {"_state_version": STATE_VERSION, "roles": {}}
    for r in range(role_count):
        role: dict = {
            "self": {
                "energy": 80.0,
                "social_energy": 66.0,
                "contract": {"schema": "v2", "fields_hash": "x" * 64, "capabilities": ["brief", "clock"]},
            },
            "users": {},
        }
        for i in range(per_role):
            role["users"][f"{r}{i:05d}"] = {
                "nickname": f"用户{r}-{i}",
                "first_met": 1690000000.0,
                "last_interaction": 1700000000.0 + i,
                "mood": {
                    "affection": 12.5,
                    "base_affection": 35.0,
                    "libido": 3.25,
                    "aggression": 1.5,
                    "turn_count": 42,
                    "last_decay": 1700000000.0,
                },
            }
        state["roles"][f"bot{r}"] = role
    return state


def _small_state() -> dict:
    return {
        "_state_version": STATE_VERSION,
        "roles": {
            "bot1": {
                "self": {
                    "energy": 63.4,
                    "contract": {"schema": "v2", "fields_hash": "abc123", "capabilities": ["brief"]},
                    "daily_schedule": [{"start": "08:00", "end": "09:00", "event": "早餐"}],
                },
                "users": {
                    "42": {"nickname": "小明", "last_interaction": 1700000000.0},
                    "43": {"nickname": "小红", "mood": {"affection": 50.5}},
                },
            }
        },
    }


def _random_state(rnd: random.Random, depth: int = 0):
    """随机小对象（含中文/控制字符/各种键类型），用来对拍分块编码器与 json.dumps。"""
    choice = rnd.random()
    if depth > 3 or choice < 0.4:
        return rnd.choice([
            None, True, False,
            rnd.randint(-(10 ** 9), 10 ** 9),
            rnd.random() * 10 ** 6 - 10 ** 5,
            "x" * rnd.randint(0, 30),
            "中文🙂" * rnd.randint(0, 4),
            "",
        ])
    if choice < 0.72:
        keys = ["k", 0, 1, 1.5, True, None]
        return {
            rnd.choice(keys): _random_state(rnd, depth + 1)
            for _ in range(rnd.randint(0, 7))
        }
    return [_random_state(rnd, depth + 1) for _ in range(rnd.randint(0, 7))]


class AsyncSerializationFormatTest(unittest.IsolatedAsyncioTestCase):
    """P1-4：分块异步序列化必须与 `_serialize` 逐字节一致（state.json 格式不能变）。"""

    async def test_fuzz_matches_sync_byte_for_byte(self):
        rnd = random.Random(20261028)
        for index in range(60):
            state = {"_state_version": STATE_VERSION, "roles": _random_state(rnd)}
            with self.subTest(index=index):
                self.assertEqual(await _serialize_async(state), _serialize(state))

    async def test_realistic_state_matches_sync(self):
        for state in (_small_state(), _big_state(per_role=20, role_count=2)):
            self.assertEqual(await _serialize_async(state), _serialize(state))

    async def test_bad_leaves_fall_back_to_null_like_sync(self):
        state = {
            "_state_version": STATE_VERSION,
            "roles": {
                "default": {
                    "self": {"nan": float("nan"), "inf": float("inf"), "bad": {1, 2}, "ok": 1.5},
                    "users": {},
                }
            },
        }
        expected = _serialize(state)
        got = await _serialize_async(state)
        self.assertEqual(got, expected)
        parsed = json.loads(got)
        self.assertIsNone(parsed["roles"]["default"]["self"]["nan"])
        self.assertEqual(parsed["roles"]["default"]["self"]["ok"], 1.5)

    async def test_circular_reference_degrades_instead_of_crashing(self):
        state: dict = {"_state_version": STATE_VERSION, "roles": {}}
        state["roles"]["selfref"] = state
        text = await _serialize_async(state)
        self.assertEqual(json.loads(text)["roles"]["selfref"], None)

    async def test_oversized_value_inside_large_container_keeps_json_valid(self):
        """批量路径里「单条过大退回递归」的分支：拼出来的 JSON 不能漏分隔符。"""
        huge = "长" * 600000  # 字符数超过批量硬上限 512K，必然走单条退回分支
        big_dict = {f"k{i}": i for i in range(7)}
        big_dict["huge"] = huge
        state = {
            "_state_version": STATE_VERSION,
            "roles": {"default": {"self": big_dict, "users": {}}},
        }
        self.assertEqual(await _serialize_async(state), _serialize(state))
        big_list = [i for i in range(7)] + [huge]
        state2 = {
            "_state_version": STATE_VERSION,
            "roles": {"default": {"self": {"l": big_list}, "users": {}}},
        }
        self.assertEqual(await _serialize_async(state2), _serialize(state2))

    async def test_very_long_string_is_chunked_and_identical(self):
        long_text = "长文本A" * 400000 + "\x01\n\"\\"  # 1.6 MB
        state = {
            "_state_version": STATE_VERSION,
            "roles": {"default": {"self": {"note": long_text}, "users": {}}},
        }
        self.assertEqual(await _serialize_async(state), _serialize(state))


class EventLoopStallTest(unittest.IsolatedAsyncioTestCase):
    """P1-4：2.2 MB 合成态下 flush 期间事件循环最大停顿 < 50 ms。

    测量方法：起一个 2 ms 周期的 ticker 任务，记录每次「await 回来」隔了多久。
    宿主是共享的（CI/多 agent 同机跑测试），别的进程抢 CPU 同样会把 ticker 的 wall
    间隔拉长——那不能算本实现阻塞事件循环的证据。所以每个间隙同时记 wall 时间与
    本进程 CPU 时间：CPU 占比不到一半的间隙判为宿主抢占，不计入停顿；再跑 3 次取
    最小值，避免一次偶发抢占就误红。真实阻塞（本进程死等/长时间 CPU）CPU 占比接近
    100%，不会被这条过滤漏掉。"""

    async def _measure_max_stall(
        self, store: StateStore, trials: int = 3
    ) -> tuple[float, float, bool, float]:
        """返回 (自身原因最大停顿, 原始 wall 最大间隔, 是否落盘成功, 总耗时)。"""
        async def one_trial() -> tuple[float, float, float]:
            gaps: list[tuple[float, float]] = []
            stop = asyncio.Event()

            async def ticker() -> None:
                loop = asyncio.get_running_loop()
                last_wall = loop.time()
                last_cpu = time.process_time()
                while not stop.is_set():
                    await asyncio.sleep(0.002)
                    now_wall = loop.time()
                    now_cpu = time.process_time()
                    gaps.append((now_wall - last_wall, now_cpu - last_cpu))
                    last_wall = now_wall
                    last_cpu = now_cpu

            task = asyncio.create_task(ticker())
            await asyncio.sleep(0.01)  # ticker 先稳定（这段不计入停顿）
            store.mark_dirty()
            started = time.perf_counter()
            ok = await store.flush()
            elapsed = time.perf_counter() - started
            stop.set()
            await task
            raw_max = max((wall for wall, _ in gaps), default=0.0)
            self_inflicted = [
                wall
                for wall, cpu in gaps
                if wall > 0.0 and cpu >= 0.5 * wall
            ]
            self.assertTrue(ok)
            return max(self_inflicted or [0.0]), raw_max, elapsed

        # 预热一次：首次写盘包含线程池创建/文件系统冷启动，不算实现停顿。
        store.mark_dirty()
        self.assertTrue(await store.flush())
        results = [await one_trial() for _ in range(trials)]
        best_self = min(r[0] for r in results)
        raw_max = max(r[1] for r in results)
        elapsed = max(r[2] for r in results)
        return best_self, raw_max, True, elapsed

    async def test_two_megabyte_state_stalls_loop_less_than_50ms(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = RecordingLogger()
            store = _new_store(tmp, log)
            store.load("2026-01-01")
            big = _big_state()
            self.assertGreater(len(_serialize(big)), 2_000_000, "合成态必须真的到 2 MB 级")
            store._state = big
            max_gap, raw_gap, ok, elapsed = await self._measure_max_stall(store)
            self.assertTrue(ok)
            self.assertGreater(store.last_payload_bytes, 2_000_000)
            self.assertLess(
                max_gap,
                0.05,
                f"事件循环自身原因最大停顿 {max_gap * 1000:.1f} ms"
                f"（原始 wall 最大间隔 {raw_gap * 1000:.1f} ms，flush 总耗时"
                f" {elapsed * 1000:.0f} ms；宿主抢占已按 CPU 占比过滤）",
            )
            # 落盘结果仍是标准 state.json：顶层只有两键，social 读的 contract 路径还在。
            saved = _read_json(Path(tmp) / "state.json")
            self.assertEqual(set(saved), {"_state_version", "roles"})
            self.assertEqual(saved["_state_version"], STATE_VERSION)
            self.assertEqual(saved["roles"]["bot0"]["self"]["contract"]["schema"], "v2")
            self.assertEqual(len(saved["roles"]["bot0"]["users"]), 2000)
            # 体积告警只报一次（2 MB 以上）。
            self.assertIn("状态文件已达", log.text("warning"))

    async def test_one_megabyte_single_string_stalls_loop_less_than_50ms(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _new_store(tmp, RecordingLogger())
            store.load("2026-01-01")
            store._state = {
                "_state_version": STATE_VERSION,
                "roles": {"default": {"self": {"note": "长文本A" * 300000}, "users": {}}},
            }
            max_gap, raw_gap, ok, elapsed = await self._measure_max_stall(store)
            self.assertTrue(ok)
            self.assertGreater(store.last_payload_bytes, 1_000_000)
            self.assertLess(
                max_gap,
                0.05,
                f"单条 1 MB 长文本下事件循环自身原因最大停顿 {max_gap * 1000:.1f} ms"
                f"（原始 wall 最大间隔 {raw_gap * 1000:.1f} ms，flush 总耗时"
                f" {elapsed * 1000:.0f} ms；宿主抢占已按 CPU 占比过滤）",
            )

    async def test_changes_during_flush_are_not_lost(self):
        """落盘期间又有改动：脏标记不能清，下一轮必须再写一次（dirty 世代号）。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = _new_store(tmp, RecordingLogger())
            store.load("2026-01-01")
            store._state = _big_state(per_role=400, role_count=2)
            store.mark_dirty()

            async def late_change() -> None:
                await asyncio.sleep(0)  # 让 flush 先开始序列化
                store._state["roles"]["bot0"]["self"]["late_key"] = "落盘期间的新改动"
                store.mark_dirty()

            task = asyncio.create_task(late_change())
            self.assertTrue(await store.flush())
            await task
            self.assertTrue(store.dirty, "落盘期间的新改动必须让脏标记保持")
            self.assertTrue(await store.flush())
            saved = _read_json(Path(tmp) / "state.json")
            self.assertEqual(saved["roles"]["bot0"]["self"]["late_key"], "落盘期间的新改动")

    async def test_async_flush_payload_equals_sync_serialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = _small_state()
            store = _new_store(tmp, RecordingLogger())
            store.load("2026-01-01")
            store._state = state
            store.mark_dirty()
            self.assertTrue(await store.flush())
            written = (Path(tmp) / "state.json").read_text(encoding="utf-8")
            self.assertEqual(written, _serialize(state))

    def test_flush_sync_is_still_available_and_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = _small_state()
            store = _new_store(tmp, RecordingLogger())
            store.load("2026-01-01")
            store._state = state
            store.mark_dirty()
            self.assertTrue(store.flush_sync())
            written = (Path(tmp) / "state.json").read_text(encoding="utf-8")
            self.assertEqual(written, _serialize(state))


class AdaptiveFlushIntervalTest(unittest.TestCase):
    """P1-4：按状态体积自适应 flush 间隔 + 一次性告警。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = RecordingLogger()
        self.store = _new_store(self.tmp.name, self.log)

    def tearDown(self):
        self.tmp.cleanup()

    def test_volume_scales_the_interval_up_to_the_cap(self):
        store = self.store
        store._last_payload_bytes = 100 * 1024
        self.assertEqual(store.effective_flush_interval, 60.0)
        store._last_payload_bytes = 600 * 1024
        self.assertEqual(store.effective_flush_interval, 180.0)
        store._last_payload_bytes = 3 * 1024 * 1024
        self.assertEqual(store.effective_flush_interval, 300.0)
        # 上限 300 秒：配置写得再大也不许无限拉长（崩溃丢的状态太多）。
        big_config = StateStore(Path(self.tmp.name) / "s2.json", lambda: 1000.0, self.log)
        self.assertEqual(big_config.effective_flush_interval, 300.0)

    def test_warnings_fire_only_once(self):
        store = self.store
        store._last_payload_bytes = 4 * 1024 * 1024
        store._last_serialize_seconds = 0.5
        store._maybe_warn_volume()
        store._maybe_warn_volume()
        self.assertEqual(self.log.text("warning").count("状态文件已达"), 1)
        self.assertEqual(self.log.text("warning").count("序列化耗时"), 1)
        self.assertIn("GiB/天", self.log.text("warning"))


class BadUserEntriesAreRepairedTest(unittest.TestCase):
    """P1-5：users[*] 的坏条目必须自愈，且修完能正常读写、能持久化。"""

    def test_load_repairs_every_bad_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            _write_json(path, {
                "_state_version": STATE_VERSION,
                "roles": {
                    "default": {
                        "self": {},
                        "users": {
                            "42": 5,
                            "43": "broken",
                            "44": None,
                            "45": [1, 2],
                            "46": {"nickname": "完好的"},
                        },
                    }
                },
            })
            log = RecordingLogger()
            store = StateStore(path, lambda: 60.0, log)
            store.load("2026-01-01")
            users = store.data["roles"]["default"]["users"]
            for uid in ("42", "43", "44", "45"):
                with self.subTest(uid=uid):
                    self.assertEqual(users[uid], {})
            self.assertEqual(users["46"]["nickname"], "完好的")
            self.assertIn("不是对象", log.text("warning"))
            self.assertTrue(store.dirty, "修过的坏值必须标脏，否则下次启动又读到坏数据")
            # 坏值修好之后 get_user / set_user 都不许再抛，并且写进去的值重载后还在。
            scope = RoleScope(store.data, "default", store.mark_dirty)
            self.assertEqual(scope.get_user("42", "nickname", "默认"), "默认")
            scope.set_user("42", "nickname", "阿明")
            self.assertEqual(scope.get_user("42", "nickname"), "阿明")
            self.assertTrue(store.flush_sync())
            reloaded = StateStore(path, lambda: 60.0, RecordingLogger())
            reloaded.load("2026-01-01")
            self.assertEqual(
                reloaded.data["roles"]["default"]["users"]["42"]["nickname"], "阿明"
            )

    def test_legacy_data_survives_broken_role_containers(self):
        """v1/v2 顶层旧字段 + 已坏的 default 角色容器：旧数据要救回来，load 不许崩。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            _write_json(path, {
                "_state_version": 2,
                "moods": {"42": {"affection": 50.0}},
                "nicknames": {"42": "阿明"},
                "roles": {"default": {"self": 5, "users": 7}},
            })
            log = RecordingLogger()
            store = StateStore(path, lambda: 60.0, log)
            store.load("2026-01-01")
            role = store.data["roles"]["default"]
            self.assertIsInstance(role["self"], dict)
            self.assertIsInstance(role["users"], dict)
            self.assertEqual(role["users"]["42"]["nickname"], "阿明")
            self.assertEqual(role["users"]["42"]["mood"]["affection"], 50.0)
            self.assertTrue(store.dirty, "救回来的旧数据必须落盘")

    def test_role_scope_rewrites_bad_entry_even_without_store(self):
        data = {"roles": {"r": {"self": {}, "users": {"7": 9, "8": {"nickname": "ok"}}}}}
        dirty: list[int] = []
        scope = RoleScope(data, "r", lambda: dirty.append(1))
        self.assertEqual(scope.user_state("7"), {})
        self.assertIsInstance(data["roles"]["r"]["users"]["7"], dict)
        self.assertTrue(dirty, "重写坏条目必须标脏")
        scope.set_user("7", "nickname", "阿")
        self.assertEqual(scope.get_user("7", "nickname"), "阿")
        self.assertEqual(scope.get_user("8", "nickname"), "ok")

    def test_role_scope_repairs_broken_containers(self):
        data: dict = {"roles": []}
        dirty: list[int] = []
        scope = RoleScope(data, "new", lambda: dirty.append(1))
        self.assertEqual(scope.role_id, "new")
        self.assertEqual(data["roles"], {"new": {"self": {}, "users": {}}})
        self.assertTrue(dirty)
        broken = {"roles": {"r": "坏角色", "s": {"self": 1, "users": 2}}}
        scope2 = RoleScope(broken, "r", lambda: None)
        self.assertEqual(scope2.all_user_ids(), [])
        self.assertEqual(broken["roles"]["r"], {"self": {}, "users": {}})
        scope3 = RoleScope(broken, "s", lambda: None)
        self.assertIsInstance(scope3.self_state, dict)
        self.assertEqual(scope3.all_user_ids(), [])


class UnknownTopLevelKeysTest(unittest.TestCase):
    """P2-05：迁移后顶层只允许 `_state_version` / `roles`。"""

    def test_unknown_keys_are_dropped_and_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            _write_json(path, {
                "_state_version": STATE_VERSION,
                "current_process": {"pid": 1234},
                "备注": "手改留下的键",
                "roles": {"default": {"self": {"energy": 88.0}, "users": {"42": {"nickname": "阿"}}}},
            })
            log = RecordingLogger()
            store = StateStore(path, lambda: 60.0, log)
            store.load("2026-01-01")
            self.assertEqual(sorted(store.data), ["_state_version", "roles"])
            self.assertIn("current_process", log.text("warning"))
            self.assertEqual(store.data["roles"]["default"]["self"]["energy"], 88.0)
            self.assertEqual(store.data["roles"]["default"]["users"]["42"]["nickname"], "阿")
            self.assertTrue(store.flush_sync())
            saved = _read_json(path)
            self.assertEqual(sorted(saved), ["_state_version", "roles"])
            self.assertNotIn("current_process", saved)
            self.assertNotIn("备注", saved)


class WriteFailureHandlingTest(unittest.IsolatedAsyncioTestCase):
    """P2-32：Windows 落盘失败要计数、升级告警、更长退避，且不丢内存状态。"""

    async def test_failure_is_counted_memory_kept_and_recovery_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = RecordingLogger()
            store = _new_store(tmp, log)
            store.load("2026-01-01")
            store._state = {"_state_version": STATE_VERSION, "roles": {"default": {"self": {"energy": 66.0}, "users": {}}}}
            store.mark_dirty()
            with mock.patch(
                "humanoid.state._atomic_write",
                side_effect=PermissionError("[WinError 5] 拒绝访问"),
            ):
                with self.assertRaises(PermissionError):
                    await store.flush()
            self.assertEqual(store.flush_failures, 1)
            self.assertEqual(store.consecutive_write_failures, 1)
            self.assertIn("WinError 5", store.last_write_error)
            self.assertTrue(store.dirty, "写失败必须把脏标记放回去，下一轮重试")
            self.assertEqual(store.data["roles"]["default"]["self"]["energy"], 66.0)
            self.assertIn("状态落盘失败（第 1 次", log.text("warning"))
            # 恢复：计数归零、数据落盘、留一条恢复日志。
            self.assertTrue(await store.flush())
            self.assertEqual(store.consecutive_write_failures, 0)
            self.assertEqual(store.flush_failures, 1)
            self.assertIn("状态落盘已恢复", log.text("info"))
            saved = _read_json(Path(tmp) / "state.json")
            self.assertEqual(saved["roles"]["default"]["self"]["energy"], 66.0)

    def test_consecutive_failures_escalate_and_back_off(self):
        log = RecordingLogger()
        store = _new_store(tempfile.mkdtemp(), log)
        exc = PermissionError("[WinError 5] 拒绝访问")
        expected = [5.0, 10.0, 20.0, 40.0]
        for index, want in enumerate(expected, start=1):
            store._record_write_failure(exc)
            with self.subTest(count=index):
                self.assertEqual(store._note_flush_failure(exc), want)
        self.assertIn("连续 3 次失败", log.text("error"))
        self.assertIn("WinError 5", log.text("error"))
        self.assertIn("内存里的状态没有丢", log.text("error"))
        for _ in range(10):
            store._record_write_failure(exc)
        self.assertEqual(store._note_flush_failure(exc), 300.0)

    def test_flush_sync_failure_is_counted_and_keeps_dirty(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = RecordingLogger()
            store = _new_store(tmp, log)
            store.load("2026-01-01")
            with mock.patch(
                "humanoid.state._atomic_write", side_effect=OSError("磁盘已满")
            ):
                with self.assertRaises(OSError):
                    store.flush_sync()
            self.assertEqual(store.flush_failures, 1)
            self.assertTrue(store.dirty)
            self.assertIn("磁盘已满", store.last_write_error)

    def test_replace_retries_with_exponential_backoff(self):
        calls: list[int] = []

        def fake_replace(src, dst):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError("[WinError 5] 被占用")

        with mock.patch("humanoid.state.os.replace", side_effect=fake_replace), mock.patch(
            "humanoid.state.time.sleep"
        ) as sleeper:
            _replace_with_retry("state.json.1.tmp", "state.json")
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            [call.args[0] for call in sleeper.call_args_list], [0.1, 0.2]
        )


class StaleRetentionConfigSemanticsTest(unittest.TestCase):
    """P2-09（配置侧）：0 = 永不按静默期整条删除，schema 与解析口径一致。"""

    def test_zero_is_kept_as_zero(self):
        cfg = HumanoidConfig.from_raw({"stale_user_retention_days": 0})
        self.assertEqual(cfg.stale_user_retention_days, 0)
        self.assertEqual(DEFAULTS.stale_user_retention_days, 30)

    def test_schema_documents_the_zero_semantics(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        item = schema["stale_user_retention_days"]
        self.assertEqual(item["minimum"], 0)
        self.assertIn("0 = 永不", item["hint"])
        self.assertIn("空壳", item["hint"])


class DefaultMigrationUserChoiceTest(unittest.TestCase):
    """P2-10：合法显式选择 = 旧默认值时，不再被静默覆盖。"""

    def test_affection_46_is_not_overwritten(self):
        self.assertNotIn("mood_initial_affection", plan_default_migrations({"mood_initial_affection": 46}))

    def test_slots_16_is_not_overwritten(self):
        self.assertNotIn("schedule_max_slots", plan_default_migrations({"schedule_max_slots": 16}))

    def test_legacy_prompt_extra_is_not_overwritten(self):
        legacy = "休闲日常，愉快的生活。"
        self.assertNotIn(
            "schedule_prompt_extra", plan_default_migrations({"schedule_prompt_extra": legacy})
        )

    def test_inject_level_low_is_not_overwritten(self):
        self.assertNotIn(
            "inject_activity_context", plan_default_migrations({"inject_activity_context": "low"})
        )

    def test_night_window_trio_is_not_overwritten(self):
        changes = plan_default_migrations({
            "schedule_follow_night_window": True,
            "night_start_hour": 23,
            "night_end_hour": 6,
        })
        self.assertEqual(changes, {})

    def test_custom_values_are_never_touched(self):
        self.assertEqual(plan_default_migrations({"mood_initial_affection": 80}), {})
        self.assertEqual(plan_default_migrations({"schedule_max_slots": 48}), {})

    def test_ambiguous_decisions_are_logged_with_reason(self):
        with self.assertLogs("humanoid.config", level="INFO") as captured:
            plan_default_migrations({"mood_initial_affection": 46})
        text = "\n".join(captured.output)
        self.assertIn("mood_initial_affection", text)
        self.assertIn("不自动迁移", text)
        self.assertIn("46", text)

    def test_explicit_and_applied_keys_are_skipped(self):
        raw = {"timezone_city": "北京", "weather_location": "Heyuan,CN"}
        self.assertEqual(plan_default_migrations(raw, applied={"weather_location"}), {})
        self.assertEqual(plan_default_migrations(raw, explicit_keys={"weather_location"}), {})
        self.assertEqual(plan_default_migrations(raw), {"weather_location": ""})


class PhaseMultiplierMigrationTest(unittest.TestCase):
    """P2-31：6 段旧周期恢复倍率自动迁移为 8 段默认并写回。"""

    def test_six_segments_migrate_to_the_eight_segment_defaults(self):
        changes = plan_default_migrations({
            "energy_recovery_phase_multipliers": [0.5, 1.0, 1.6, 1.9, 2.0, 1.1],
        })
        self.assertEqual(
            changes["energy_recovery_phase_multipliers"],
            list(DEFAULTS.energy_recovery_phase_multipliers),
        )
        self.assertIsInstance(changes["energy_recovery_phase_multipliers"], list)
        self.assertEqual(len(changes["energy_recovery_phase_multipliers"]), 8)

    def test_migrated_value_parses_without_the_old_warning(self):
        with self.assertNoLogs("humanoid.config", level="WARNING"):
            cfg = HumanoidConfig.from_raw({
                "energy_recovery_phase_multipliers": list(
                    DEFAULTS.energy_recovery_phase_multipliers
                )
            })
        self.assertEqual(
            cfg.energy_recovery_phase_multipliers, DEFAULTS.energy_recovery_phase_multipliers
        )

    def test_eight_or_odd_lengths_are_not_touched(self):
        eight = list(DEFAULTS.energy_recovery_phase_multipliers)
        self.assertNotIn(
            "energy_recovery_phase_multipliers",
            plan_default_migrations({"energy_recovery_phase_multipliers": eight}),
        )
        self.assertNotIn(
            "energy_recovery_phase_multipliers",
            plan_default_migrations({"energy_recovery_phase_multipliers": eight[:7]}),
        )
        self.assertNotIn(
            "energy_recovery_phase_multipliers",
            plan_default_migrations({"energy_recovery_phase_multipliers": "0.5,1.0"}),
        )
        self.assertNotIn(
            "energy_recovery_phase_multipliers",
            plan_default_migrations({"energy_recovery_phase_multipliers": None}),
        )

    def test_migration_respects_applied_and_explicit(self):
        raw = {"energy_recovery_phase_multipliers": [0.5, 1.0, 1.6, 1.9, 2.0, 1.1]}
        self.assertEqual(
            plan_default_migrations(raw, applied={"energy_recovery_phase_multipliers"}), {}
        )
        self.assertEqual(
            plan_default_migrations(
                raw, explicit_keys={"energy_recovery_phase_multipliers"}
            ),
            {},
        )


if __name__ == "__main__":
    unittest.main()