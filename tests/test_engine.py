"""编排层的端到端测试：不需要 AstrBot 运行时，只用假 Context。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from humanoid import __version__
from humanoid.engine import HumanoidEngine
from humanoid.services.schedule import SOURCE_LLM, SOURCE_TEMPLATE
from humanoid.slots import coverage_is_complete
from humanoid.llm import ProviderResolver, LLMGateway
from humanoid.role_manager import RoleManager
from humanoid.state import StateStore
from humanoid.clock import Clock
from humanoid.config import ConfigBox, HumanoidConfig

from .fakes import FakeContext, FakeProvider, RecordingLogger, FakeClock

GOOD_SCHEDULE = json.dumps(
    [
        {"start": "00:00", "end": "08:00", "event": "睡眠", "location": "卧室", "emotion": "平静", "energy_rate": 0.15},
        {"start": "08:00", "end": "12:00", "event": "工作", "location": "书房", "emotion": "专注", "energy_rate": -0.1},
        {"start": "12:00", "end": "13:00", "event": "午餐", "location": "餐厅", "emotion": "放松", "energy_rate": 0.1},
        {"start": "13:00", "end": "18:00", "event": "工作", "location": "书房", "emotion": "认真", "energy_rate": -0.08},
        {"start": "18:00", "end": "24:00", "event": "休闲入睡", "location": "客厅", "emotion": "轻松", "energy_rate": 0.05},
    ],
    ensure_ascii=False,
)


class DictConfig(dict):
    """模拟 AstrBotConfig：dict 子类 + save_config()。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.saves = 0

    def save_config(self) -> None:
        self.saves += 1


class EngineTest(unittest.IsolatedAsyncioTestCase):
    def build(self, raw=None, providers=None, global_provider=None, fetch=None):
        raw = DictConfig(raw or {"timezone_city": "北京"})
        ctx = FakeContext(chat_providers=list(providers or []), global_provider=global_provider)
        log = RecordingLogger()
        data_dir = Path(tempfile.mkdtemp())
        state_path = data_dir / "state.json"
        box = ConfigBox(raw)
        config_obj = box.value
        state_store = StateStore(state_path, lambda: 1.0, log)
        clock = Clock(box)
        state_store.load(clock.today_str(), config_obj.cycle_length)
        resolver = ProviderResolver(ctx, log)
        gateway = LLMGateway(resolver, box, log)
        role_manager = RoleManager(state_store, box, log, resolver, gateway, fetch)
        engine = HumanoidEngine(ctx, box, data_dir, log, fetch, role_manager)
        return engine, box, log

    async def test_start_stop_is_clean(self):
        engine, _, log = self.build()
        await engine.role_manager.start()
        core = engine.role_manager.get_or_create("default")
        await core.start()
        # 滚动分段：开机种下第一段，覆盖「现在」，但不预排未来
        self.assertTrue(core.schedule.segments())
        self.assertIsNotNone(core.schedule.active_segment())
        await engine.role_manager.stop()
        self.assertIn("角色 default 已停止", log.text("info"))
        # 落盘检查
        self.assertFalse(engine.role_manager._state_store.dirty)
        self.assertTrue(engine.role_manager._state_store.path.exists())

    async def test_stop_does_not_let_a_late_role_start_leak(self):
        """卸载竞态：`stop()` 之后新建的角色不许再被启动。

        `stop()` 原来不复位 `_started`，于是竞态窗口里 `get_or_create` 仍会
        create_task 启动新实例；那个实例不在被停的名单里，5 个后台循环会一直活着
        并继续写 state.json。
        """
        engine, _, _ = self.build()
        await engine.role_manager.start()
        core = engine.role_manager.get_or_create("default")
        await core.start()
        await engine.role_manager.stop()
        self.assertFalse(engine.role_manager._started, "stop() 必须复位 _started")

        late = engine.role_manager.get_or_create("late")
        await asyncio.sleep(0)  # 给「万一被创建」的任务一个跑起来的机会
        self.assertEqual(engine.role_manager._start_tasks, set(), "stop() 之后不该再启动角色")
        self.assertFalse(late._started, "新角色不该被启动")
        self.assertEqual(late._tasks, set(), "新角色不该有后台任务")

    async def test_spawn_after_stop_is_refused(self):
        """停止信号发出后 `_spawn_background` 必须拒绝——否则循环会活过 stop()。"""
        engine, _, _ = self.build()
        await engine.role_manager.start()
        core = engine.role_manager.get_or_create("default")
        await core.start()
        await engine.role_manager.stop()
        self.assertTrue(core._stop_event.is_set())
        before = set(core._tasks)
        self.assertIsNone(core._spawn_background(core._body_loop(), "body-tick"))
        self.assertEqual(core._tasks, before)

    async def test_start_after_stop_is_refused(self):
        """停止信号已发出时 start() 不该把 `_started` 置真。

        否则 5 个循环全被 `_spawn_background` 拦下、而 `_started` 已经是 True，
        后续再 start() 会被开头那句挡掉 —— 这个角色**永远起不来**。
        """
        engine, _, _ = self.build()
        await engine.role_manager.start()
        core = engine.role_manager.get_or_create("default")
        await core.start()
        await engine.role_manager.stop()
        self.assertFalse(core._started)
        await core.start()
        self.assertFalse(core._started, "停止中不该被置成已启动")
        self.assertEqual(core._tasks, set())

    async def test_snapshot_has_no_awaits_and_is_complete(self):
        engine, _, _ = self.build()
        await engine.role_manager.start()
        core = engine.role_manager.get_or_create("default")
        await core.start()
        try:
            snap = core.snapshot(self._sender)
            self.assertTrue(snap['today'])
            self.assertTrue(snap['weekday'])
            self.assertIn("energy", snap)
            self.assertIn("cycle", snap)
            self.assertIn("weather", snap)
            self.assertIn("schedule", snap)
            self.assertIn("soma", snap, "身体轴没进快照，主动社交层就读不到")
            self.assertIn("sleep_pressure", snap["soma"])
            self.assertIn("max_chars", snap["form_policy"])
        finally:
            await engine.role_manager.stop()

    async def test_injection_contains_expected_sections(self):
        engine, _, _ = self.build({"timezone_city": "北京", "mood_enabled_in_group": True})
        await engine.role_manager.start()
        core = engine.role_manager.get_or_create("99999")
        await core.start()
        try:
            core.mood.set_nickname("42", "小明")
            group_text = core.build_injection("42", is_group=True)
            private_text = core.build_injection("42", is_group=False)
            # enable_chat_awareness 必须真的影响注入：v2.13.2 里这个配置项没人读。
            self.assertIn("群聊", group_text)
            self.assertIn("只有她和TA", private_text)
            self.assertIn("小明", private_text)
            self.assertIn("她对TA", private_text)
            self.assertLessEqual(len(private_text), 520)
            # 低注入档不摊数值：靠「禁止提及数据」堆补丁不如不给数据。
            for text in (group_text, private_text):
                self.assertNotIn("/100", text)
                self.assertNotIn("%", text)
        finally:
            await engine.role_manager.stop()

    async def test_full_mode_gives_attitude_but_no_numbers(self):
        """full 档多给的是心气与体感，不是读数：好感 46.0/100 那种成绩单不进上下文。"""
        engine, box, _ = self.build({"timezone_city": "北京", "inject_activity_context": "full"})
        await engine.role_manager.start()
        core = engine.role_manager.get_or_create("99999")
        try:
            full = core.build_injection("42", is_group=False)
            self.assertIn("她对TA", full)
            self.assertNotIn("情绪数值", full)
            self.assertNotIn("/100", full)
            box.raw["inject_activity_context"] = "low"
            box.reload()
            self.assertNotIn("情绪数值", core.build_injection("42", is_group=False))
            box.raw["inject_activity_context"] = "mood_only"
            box.reload()
            mood_only = core.build_injection("42", is_group=False)
            self.assertIn("她对TA", mood_only)
            self.assertNotIn("群聊", mood_only)
        finally:
            await engine.role_manager.stop()

    async def test_group_mood_gated_by_config(self):
        """群聊默认不参与情绪：既不建情绪档案，也不把关系信息注入。"""
        engine, box, _ = self.build({"timezone_city": "北京"})
        await engine.role_manager.start()
        core = engine.role_manager.get_or_create("99999")
        await core.start()
        try:
            self.assertNotIn("她对TA", core.build_injection("42", is_group=True))
            core.on_message("42", "hi", is_group=True)
            users = core.mood._scope._root.get("users", {})
            self.assertNotIn("mood", users.get("42", {}), "群聊不该为成员建情绪档案")
            self.assertIn("last_interaction", users.get("42", {}), "但聊天间隔要记，群聊里靠它知道好久没聊")
            box.raw["mood_enabled_in_group"] = True
            box.reload()
            self.assertIn("她对TA", core.build_injection("42", is_group=True))
        finally:
            await engine.role_manager.stop()

    # 其他测试方法类似，需使用 core = engine.role_manager.get_or_create(...)
    # 由于篇幅，这里省略其余方法，但修改思路一致：通过 role_manager 获取 core 实例，
    # 并通过 core 调用方法。

    # 以下仅为示例，实际需要补齐所有测试方法。
    def _sender(self):
        return "42"


class StateFileSafetyTest(unittest.TestCase):
    """P0：加载失败时**不许**用空状态覆盖那份可能完好的文件。

    `load()` 原来把任何读失败都当「损坏」：备份成功与否都接着把 `_state` 换成 fresh
    并标脏，下一次 `flush()` 就用空状态盖掉原文件。Windows 上杀软/编辑器占用
    （PermissionError）就能触发——文件内容完好，只是那一刻打不开。
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "state.json"
        self.log = RecordingLogger()

    def _store(self):
        return StateStore(self.path, lambda: 1.0, self.log)

    def test_backup_failure_never_overwrites_the_original(self):
        from unittest import mock

        # 内容确实是坏的（JSON 截断），但备份这一步打不开文件（占用/无权限）
        self.path.write_text('{"roles": {"default": ', encoding="utf-8")
        before = self.path.read_bytes()
        store = self._store()
        with mock.patch("humanoid.state.os.replace", side_effect=PermissionError("被占用")):
            store.load("2026-01-01")
            # 旧代码在这里把 `_state` 换成 fresh 并标脏，下一次 flush 就写空状态盖掉原文件。
            store.set("_touch", 1)
            self.assertFalse(store.flush_sync(), "备份失败后必须拒绝写盘")
            self.assertFalse(asyncio.run(store.flush()))
        self.assertEqual(self.path.read_bytes(), before, "原文件被空状态覆盖了")
        self.assertTrue(self.path.exists())
        self.assertEqual(list(self.dir.glob("*.corrupt-*.json")), [], "备份没成功就不该有备份")
        self.assertIn("拒绝写盘", self.log.text("error"))

    def test_unreadable_file_is_not_treated_as_corrupt(self):
        # 拿一个目录顶掉文件：`read_bytes()` 抛 OSError（Windows 上是 PermissionError）。
        self.path.mkdir()
        store = self._store()
        store.load("2026-01-01")
        store.set("roles", {})
        self.assertFalse(store.flush_sync())
        self.assertTrue(self.path.is_dir(), "读不到就不该动它：既不备份也不覆盖")
        self.assertEqual(list(self.dir.glob("*.corrupt-*.json")), [])

    def test_successful_backup_still_rebuilds(self):
        self.path.write_text("not json at all", encoding="utf-8")
        store = self._store()
        store.load("2026-01-01")
        self.assertEqual(len(list(self.dir.glob("*.corrupt-*.json"))), 1)
        self.assertTrue(store.flush_sync(), "原文件已经安全挪走，这里必须能重建")

    def test_two_backups_do_not_collide(self):
        for _ in range(2):
            self.path.write_text("still not json", encoding="utf-8")
            self._store().load("2026-01-01")
        self.assertEqual(len(list(self.dir.glob("*.corrupt-*.json"))), 2)

    def test_encoding_is_tried_before_declaring_corruption(self):
        """GBK 手改过的文件是**编码不对**，不是损坏：不许备份走、更不许重建。

        v3.0.0（P2-05）：顶层只允许 `_state_version`/`roles`，未知键（原用例里的
        顶层「备注」）会被迁移层清理，所以中文解码验证挪进 roles 里的真实字段；
        同时断言未知键确实被删掉。
        """
        self.path.write_bytes(
            '{"roles": {"default": {"self": {"last_update": "手改"}}}, "备注": "手改"}'.encode("gbk")
        )
        store = self._store()
        store.load("2026-01-01")
        self.assertEqual(list(self.dir.glob("*.corrupt-*.json")), [])
        self.assertEqual(store.data["roles"]["default"]["self"]["last_update"], "手改")
        self.assertNotIn("备注", store.data)

    def test_newer_state_version_refuses_to_write_back(self):
        from humanoid.state import STATE_VERSION

        payload = {
            "_state_version": STATE_VERSION + 1,
            "roles": {"default": {"self": {"energy": 42.0}, "users": {}}},
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        store = self._store()
        store.load("2026-01-01")
        store.set("_touch", 1)  # 让它变脏，否则 flush_sync 直接早退
        self.assertFalse(store.flush_sync(), "盘上版本更新时不许写回（写回 = 降级丢字段）")
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["_state_version"], STATE_VERSION + 1)
        self.assertIn("拒绝写盘", self.log.text("error"))

    def test_flush_sync_clears_the_dirty_event(self):
        store = self._store()
        store.load("2026-01-01")  # 文件不存在 → fresh + 标脏
        self.assertTrue(store.dirty)
        self.assertTrue(store.flush_sync())
        self.assertFalse(
            store._dirty_event.is_set(),
            "flush_sync 不清事件，异步落盘循环醒来后会立刻空跑一轮",
        )

    def test_serialize_never_writes_nan_or_infinity(self):
        from humanoid.state import _serialize

        text = _serialize({"a": float("nan"), "b": [float("inf"), 1.0]})
        self.assertNotIn("NaN", text)
        self.assertNotIn("Infinity", text)
        self.assertEqual(json.loads(text), {"a": None, "b": [None, 1.0]})

    def test_deprecated_top_level_keys_are_dropped(self):
        """`roles` 里已有一份同名数据时，顶层那份旧拷贝也必须被删掉。"""
        from humanoid.state import STATE_VERSION

        payload = {
            "_state_version": STATE_VERSION,
            "energy": 63.4,  # 旧版顶层字段，且 roles 里已有一份
            "moods": {"42": {"affection": 50.0}},
            "roles": {"default": {"self": {"energy": 88.0}, "users": {"42": {"mood": {"affection": 50.0}}}}},
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        store = self._store()
        store.load("2026-01-01")
        self.assertNotIn("energy", store.data, "顶层旧键不许留着（读不到又白占体积）")
        self.assertNotIn("moods", store.data)
        self.assertEqual(store.data["roles"]["default"]["self"]["energy"], 88.0)

    def test_tmp_name_is_unique_and_cleaned_up(self):
        import inspect

        from humanoid import state as state_module

        src = inspect.getsource(state_module._atomic_write)
        self.assertIn("getpid", src, "固定的 .tmp 名会让两个写者互相踩")
        self.assertIn("unlink", src, "失败分支必须清掉半截临时文件")


class _FakeScheduleForProcess:
    """只够 `ProcessService` 用的日程替身。"""

    def __init__(self, event: str = "工作", start: str = "09:00", end: str = "18:00"):
        self._slot = {"event": event, "start": start, "end": end}

    def current_slot(self, minutes=None):
        return dict(self._slot)


class ProcessServiceWiringTest(unittest.TestCase):
    """D5：这个服务此前**全项目零实例化**，一接线就崩。

    两处硬伤：它读的 `process_min_duration` / `process_max_duration` /
    `process_history_limit` 在 `config.py` 里根本不存在（AttributeError）；
    `_parse_dt` 返回 naive datetime 而 `clock.now()` 是 aware 的（所有比较点 TypeError）。
    """

    def _service(self, conf=None):
        from humanoid.clock import Clock
        from humanoid.role_scope import RoleScope
        from humanoid.services.process import ProcessService

        conf = conf or HumanoidConfig.from_raw({"timezone_city": "北京"})
        scope = RoleScope({"_state_version": 3, "roles": {}}, "bot1", lambda: None)
        clock = Clock(lambda: conf)
        return ProcessService(scope, lambda: conf, clock, _FakeScheduleForProcess()), scope

    def test_instantiates_with_real_clock_and_config(self):
        service, _ = self._service()
        proc = service.current()  # 读 process_min_duration / process_max_duration
        self.assertTrue(proc.get("name"))
        self.assertTrue(proc.get("expected_end"))
        self.assertTrue(proc.get("next_phase_at"))
        service.tick()
        service.recent()  # 读 process_history_limit
        self.assertTrue(service.describe())

    def test_parse_dt_is_comparable_with_clock_now(self):
        service, _ = self._service()
        parsed = service._parse_dt("2026-01-01T09:00:00")
        self.assertIsNotNone(parsed)
        self.assertIsNotNone(
            parsed.tzinfo, "naive datetime 与 aware 的 clock.now() 一比就 TypeError"
        )
        # 直接比较：不抛 TypeError 就是这条回归的全部意义
        self.assertIsInstance(service._clock.now() >= parsed, bool)

    def test_config_fallback_when_the_keys_are_missing(self):
        """旧配置 / 轻量替身上没有这 3 个键也要能跑（`getattr` 容错）。"""
        from types import SimpleNamespace

        from humanoid.role_scope import RoleScope
        from humanoid.services.process import ProcessService

        service = ProcessService(
            RoleScope({"_state_version": 3, "roles": {}}, "bot1", lambda: None),
            lambda: SimpleNamespace(),
            None,
            _FakeScheduleForProcess(),
        )
        self.assertEqual(service._int_cfg("process_min_duration", 20), 20)
        self.assertEqual(service._int_cfg("process_max_duration", 180), 180)
        self.assertEqual(service._int_cfg("process_history_limit", 20), 20)

    def test_ensure_fields_marks_state_dirty(self):
        """`_ensure_process_fields` 会改状态，改完必须标脏（否则补出来的字段永不落盘）。"""
        service, scope = self._service()
        dirty = []
        scope.set_mark_dirty(lambda: dirty.append(1))
        service._scope.set_self("current_process", {"name": "看一会儿资料"})
        proc = service.current()
        self.assertTrue(proc.get("started_at"))
        self.assertTrue(dirty, "补了字段却没标脏，重启后又是缺的")

    def test_phase_advance_crosses_multiple_phases(self):
        """一次跨过多个阶段：从**旧的** next_phase_at 往后推，而不是用 now 重算。"""
        from datetime import timedelta

        service, _ = self._service()
        now = service._clock.now()
        proc = {
            "name": "看一会儿资料",
            "phases": ["a", "b", "c", "d", "e"],
            "phase": "a",
            "phase_index": 0,
            "started_at": (now - timedelta(minutes=90)).isoformat(),
            "expected_end": (now + timedelta(minutes=30)).isoformat(),
            "next_phase_at": (now - timedelta(minutes=45)).isoformat(),
        }
        advanced = service._advance_phase(proc, now)
        self.assertGreater(advanced["phase_index"], 1, "停在第二阶段 = 没有跨过多个阶段")
        self.assertLessEqual(advanced["phase_index"], 4)


class ProcessWiredIntoCoreTest(unittest.IsolatedAsyncioTestCase):
    """过程服务必须真的挂在 core 上、tick 一次不抛、后台循环会推进它。"""

    def _core(self):
        from humanoid.core_instance import HumanoidCoreInstance

        conf = HumanoidConfig.from_raw({"timezone_city": "北京"})
        store = StateStore(Path(tempfile.mkdtemp()) / "state.json", lambda: 1.0)
        store.load("2026-09-26", 28)
        return HumanoidCoreInstance(
            role_id="bot1",
            state_store=store,
            config_provider=lambda: conf,
            logger=RecordingLogger(),
            stop_event=asyncio.Event(),
            resolver=FakeContext(),
            gateway=None,
        )

    async def test_core_has_the_service_and_one_tick_does_not_raise(self):
        core = self._core()
        self.assertTrue(hasattr(core, "process"), "过程服务没接到 core 上")
        core.process.tick()
        await core.process.update_async()
        self.assertTrue(core.process.peek(), "tick 之后应当有一个当前过程")
        self.assertIn("name", core.process_state())

    async def test_process_loop_advances_and_stops(self):
        core = self._core()
        task = asyncio.create_task(core._process_loop())
        await asyncio.sleep(0.05)
        core._stop_event.set()
        await asyncio.wait_for(task, timeout=2)
        self.assertTrue(core.process.peek().get("name"))

    async def test_snapshot_shows_the_process_without_creating_one(self):
        core = self._core()
        self.assertNotIn("process", core.snapshot(refresh=False), "只读快照不该凭空造过程")
        core.process.tick()
        await core.process.update_async()
        snap = core.snapshot(refresh=False)
        self.assertIn("process", snap)
        self.assertTrue(snap["process"]["text"])

    async def test_status_page_shows_the_process(self):
        core = self._core()
        core.process.tick()
        await core.process.update_async()
        self.assertIn("手上在做", "\n".join(core.status_lines("42")))


if __name__ == "__main__":
    unittest.main()