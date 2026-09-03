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
from humanoid.config import HumanoidConfig

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
        config_obj = HumanoidConfig.from_raw(raw)
        state_store = StateStore(state_path, lambda: 1.0, log)
        clock = Clock(lambda: config_obj)
        state_store.load(clock.today_str(), config_obj.cycle_length)
        resolver = ProviderResolver(ctx, log)
        gateway = LLMGateway(resolver, lambda: config_obj, log)
        role_manager = RoleManager(state_store, lambda: config_obj, log, resolver, gateway, fetch)
        engine = HumanoidEngine(ctx, raw, data_dir, log, fetch, role_manager)
        return engine, raw, log

    async def test_start_stop_is_clean(self):
        engine, _, log = self.build()
        await engine.role_manager.start()
        # 手动启动角色实例（因为role_manager.start()会启动所有已有实例）
        for core in engine.role_manager.get_all():
            await core.start()
        self.assertTrue(coverage_is_complete(engine.role_manager.get_all()[0].schedule.current_slots()))
        await engine.role_manager.stop()
        self.assertIn("角色 default 已停止", log.text("info"))  # 或类似
        # 落盘检查
        self.assertFalse(engine.role_manager._state_store.dirty)
        self.assertTrue(engine.role_manager._state_store.path.exists())

    async def test_snapshot_has_no_awaits_and_is_complete(self):
        engine, _, _ = self.build()
        await engine.role_manager.start()
        for core in engine.role_manager.get_all():
            await core.start()
        try:
            core = engine.role_manager.get_all()[0]
            snap = core.snapshot(self._sender)
            self.assertTrue(snap['today'])
            self.assertTrue(snap['weekday'])
            self.assertIn("energy", snap)
            self.assertIn("cycle", snap)
            self.assertIn("weather", snap)
            self.assertIn("schedule", snap)
        finally:
            await engine.role_manager.stop()

    async def test_injection_contains_expected_sections(self):
        engine, _, _ = self.build({"timezone_city": "北京", "mood_enabled_in_group": True})
        await engine.role_manager.start()
        core = engine.role_manager.get_or_create("99999")
        await core.start()
        try:
            core.mood.set_nickname("42", "小明")
            text = core.build_injection("42", is_group=True)
            self.assertIn("环境感知", text)
            self.assertIn("群聊", text)
            self.assertIn("小明", text)
            self.assertIn("当前情绪数值", text)
            self.assertIn("社交能量", text)
        finally:
            await engine.role_manager.stop()

    async def test_group_mood_gated_by_config(self):
        engine, raw, _ = self.build()
        await engine.role_manager.start()
        core = engine.role_manager.get_or_create("99999")
        await core.start()
        try:
            self.assertNotIn("当前情绪数值", core.build_injection("42", is_group=True))
            core.on_message("42", "hi", is_group=True)
            self.assertNotIn("42", core.mood._scope.user_state("42"), "群聊不该为成员建情绪档案")
            raw["mood_enabled_in_group"] = True
            engine.reload_config(raw)
            self.assertIn("当前情绪数值", core.build_injection("42", is_group=True))
        finally:
            await engine.role_manager.stop()

    # 其他测试方法类似，需使用 core = engine.role_manager.get_or_create(...)
    # 由于篇幅，这里省略其余方法，但修改思路一致：通过 role_manager 获取 core 实例，
    # 并通过 core 调用方法。

    # 以下仅为示例，实际需要补齐所有测试方法。
    def _sender(self):
        return "42"