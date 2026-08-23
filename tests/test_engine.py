"""编排层的端到端测试：不需要 AstrBot 运行时，只用假 Context。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from humanoid import __version__
from humanoid.engine import HumanoidEngine
from humanoid.services.schedule import SOURCE_LLM, SOURCE_TEMPLATE
from humanoid.slots import coverage_is_complete

from .fakes import FakeContext, FakeProvider, RecordingLogger

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
        engine = HumanoidEngine(ctx, raw, Path(tempfile.mkdtemp()), log, fetch)
        return engine, raw, log

    async def test_start_stop_is_clean(self):
        engine, _, log = self.build()
        await engine.start()
        self.assertTrue(coverage_is_complete(engine.schedule.current_slots()))
        await engine.stop()
        self.assertIn("已清理资源", log.text("info"))
        self.assertFalse(engine.state.dirty, "stop() 必须把状态落盘")
        self.assertTrue(engine.state.path.exists())

    async def test_snapshot_has_no_awaits_and_is_complete(self):
        engine, _, _ = self.build()
        await engine.start()
        try:
            snap = engine.snapshot(advance_energy=True)
            self.assertTrue(snap.today)
            self.assertTrue(snap.weekday)
            self.assertTrue(snap.energy_text)
            self.assertTrue(snap.cycle_text)
            self.assertIn("weather", snap.weather)
            self.assertTrue(snap.event)
            self.assertEqual(snap.schedule_source_text, "内置模板")
        finally:
            await engine.stop()

    async def test_injection_contains_expected_sections(self):
        engine, _, _ = self.build()
        await engine.start()
        try:
            engine.set_nickname("42", "小明")
            text = engine.build_injection("42", is_group=True)
            self.assertIn("环境感知", text)
            self.assertIn("群聊", text)
            self.assertIn("小明", text)
            self.assertIn("当前情绪数值", text)
            self.assertIn("社交能量", text)
        finally:
            await engine.stop()

    async def test_injection_modes(self):
        engine, raw, _ = self.build({"timezone_city": "北京", "inject_activity_context": "full"})
        await engine.start()
        try:
            self.assertIn("当前日程计划", engine.build_injection("1"))
            raw["inject_activity_context"] = "mood_only"
            engine.reload_config()
            text = engine.build_injection("1")
            self.assertNotIn("当前日程计划", text)
            self.assertIn("情绪倾向", text)
        finally:
            await engine.stop()

    async def test_background_generation_replaces_template(self):
        provider = FakeProvider("p1", reply=GOOD_SCHEDULE)
        engine, _, _ = self.build(
            {"timezone_city": "北京", "schedule_provider_name": "p1"}, [provider]
        )
        await engine.start()
        try:
            self.assertEqual(engine.schedule.source, SOURCE_TEMPLATE)
            for _ in range(50):
                if engine.schedule.source == SOURCE_LLM:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(engine.schedule.source, SOURCE_LLM)
            self.assertEqual(provider.calls, 1)
        finally:
            await engine.stop()

    async def test_slow_provider_never_blocks_injection(self):
        """端到端护栏：模型再慢也不该拖慢注入。"""
        slow = FakeProvider("p1", reply=GOOD_SCHEDULE, delay=5.0)
        engine, _, _ = self.build(
            {"timezone_city": "北京", "schedule_provider_name": "p1"}, [slow]
        )
        await engine.start()
        try:
            loop = asyncio.get_running_loop()
            started = loop.time()
            for _ in range(20):
                engine.build_injection("1", is_group=False)
                engine.on_message("1")
            elapsed = loop.time() - started
            self.assertLess(elapsed, 1.0, f"20 次注入耗时 {elapsed:.2f}s，说明有阻塞调用")
            self.assertEqual(engine.schedule.source, SOURCE_TEMPLATE)
        finally:
            await engine.stop()

    async def test_environment_mode_filter_actually_works(self):
        """environment_mode 必须真的过滤掉不匹配的会话类型。"""
        engine, raw, _ = self.build({"timezone_city": "北京", "environment_mode": "private"})
        self.assertTrue(engine.environment_allows(is_private=True))
        self.assertFalse(engine.environment_allows(is_private=False))
        raw["environment_mode"] = "group"
        engine.reload_config()
        self.assertFalse(engine.environment_allows(is_private=True))
        self.assertTrue(engine.environment_allows(is_private=False))
        raw["environment_mode"] = "both"
        engine.reload_config()
        self.assertTrue(engine.environment_allows(is_private=True))
        self.assertTrue(engine.environment_allows(is_private=False))

    async def test_admin_accepts_astrbot_admin(self):
        engine, _, _ = self.build({"timezone_city": "北京", "admin_qq": ["111"]})
        self.assertTrue(engine.is_admin("111"))
        self.assertFalse(engine.is_admin("222"))
        self.assertTrue(engine.is_admin("222", astrbot_admin=True))

    async def test_config_migration_flips_global_fallback_once(self):
        raw = {"timezone_city": "北京", "schedule_allow_global_fallback": False}
        engine, cfg_dict, log = self.build(raw)
        await engine.start()
        try:
            self.assertTrue(cfg_dict["schedule_allow_global_fallback"])
            self.assertTrue(engine.config.schedule_allow_global_fallback)
            self.assertEqual(cfg_dict.saves, 1)
            self.assertIn("一次性迁移", log.text("info"))
        finally:
            await engine.stop()

        # 第二次启动（同一 state 目录）不应再改
        engine2 = HumanoidEngine(
            FakeContext(), cfg_dict, engine.state.path.parent, RecordingLogger()
        )
        cfg_dict["schedule_allow_global_fallback"] = False
        await engine2.start()
        try:
            self.assertFalse(
                cfg_dict["schedule_allow_global_fallback"], "迁移只应执行一次"
            )
        finally:
            await engine2.stop()

    async def test_command_texts(self):
        engine, _, _ = self.build()
        await engine.start()
        try:
            self.assertIn("当前状态", "\n".join(engine.status_lines("1")))
            self.assertIn("情绪档案", engine.mood_profile_text("1"))
            self.assertIn("情绪详细档案", engine.mood_profile_text("1", detailed=True))
            self.assertIn("暂无情绪波动", engine.mood_log_text("1"))
            self.assertIn("日程表", engine.schedule_text())
            self.assertIn("北京", engine.city_time_text("北京") or "")
            self.assertIsNone(engine.city_time_text("不存在的城市"))
            report = engine.diagnostics_text()
            self.assertIn(__version__, report)
            self.assertIn("可用对话模型 id", report)
        finally:
            await engine.stop()

    async def test_diagnostics_explains_typo(self):
        engine, _, _ = self.build(
            {"timezone_city": "北京", "schedule_provider_name": "DeepSeek_V3"},
            [FakeProvider("deepseek_v3")],
        )
        report = engine.diagnostics_text()
        self.assertIn("deepseek_v3", report)
        engine2, _, _ = self.build(
            {"timezone_city": "北京", "schedule_provider_name": "totally-wrong"},
            [FakeProvider("deepseek_v3")],
        )
        report2 = engine2.diagnostics_text()
        self.assertIn("未找到", report2)
        self.assertIn("可用列表里没有它", report2)

    async def test_nickname_roundtrip_does_not_lose_state(self):
        """设置昵称不应把内存状态从磁盘覆盖回来。"""
        engine, _, _ = self.build()
        await engine.start()
        try:
            engine.state.data["energy"] = 12.5
            engine.set_nickname("1", "阿猫")
            engine.set_nickname("2", "阿狗")
            self.assertEqual(engine.state.data["energy"], 12.5, "内存中的精力被覆盖了")
            self.assertEqual(engine.all_nicknames(), {"1": "阿猫", "2": "阿狗"})
            self.assertEqual(engine.nickname("1"), "阿猫")
        finally:
            await engine.stop()

    async def test_reset_state_and_schedule(self):
        provider = FakeProvider("p1", reply=GOOD_SCHEDULE)
        engine, _, _ = self.build(
            {"timezone_city": "北京", "schedule_provider_name": "p1"}, [provider]
        )
        await engine.start()
        try:
            energy, social, cycle_day = engine.reset_state()
            self.assertEqual(energy, 80.0)
            self.assertEqual(social, 100.0)
            self.assertGreaterEqual(cycle_day, 1)
            self.assertTrue(await engine.reset_schedule())
            self.assertEqual(engine.schedule.source, SOURCE_LLM)
        finally:
            await engine.stop()

    async def test_reset_schedule_bypasses_cooldown(self):
        broken = FakeProvider("p1", error=RuntimeError("down"))
        engine, _, _ = self.build(
            {
                "timezone_city": "北京",
                "schedule_provider_name": "p1",
                "schedule_allow_global_fallback": False,
                "schedule_provider_cooldown_minutes": 30,
            },
            [broken],
        )
        await engine.start()
        try:
            await engine.reset_schedule()
            first = broken.calls
            await engine.reset_schedule()
            self.assertGreater(broken.calls, first, "管理员显式重置应绕过冷却")
        finally:
            await engine.stop()

    async def test_parse_affection_batch(self):
        parse = HumanoidEngine.parse_affection_batch
        self.assertEqual(parse("111:50, 222:60"), [("111", 50.0), ("222", 60.0)])
        self.assertEqual(parse("111：50"), [("111", 50.0)])
        self.assertEqual(
            parse('[{"qq": "333", "value": 70}]'), [("333", 70.0)]
        )
        self.assertEqual(parse(""), [])
        self.assertEqual(parse("garbage"), [])

    async def test_mood_update_spawned_and_awaited_on_stop(self):
        engine, _, _ = self.build({"timezone_city": "北京", "mood_sensitivity": 100})
        await engine.start()
        try:
            engine.mood.profile("1")["last_interaction"] = 1.0
            engine.spawn_mood_update("1", "你好棒")
            await asyncio.sleep(0.05)
            self.assertGreater(engine.mood.profile("1")["turn_count"], 1)
        finally:
            await engine.stop()

    async def test_weather_flows_into_snapshot(self):
        async def fetch(url: str, timeout: float) -> dict:
            return {"weather": [{"description": "小雨"}], "main": {"temp": 22, "humidity": 88}}

        engine, _, _ = self.build(
            {
                "timezone_city": "北京",
                "weather_api_key": "0123456789abcdef",
                "weather_location": "Beijing,CN",
            },
            fetch=fetch,
        )
        await engine.start()
        try:
            await engine.weather.refresh(force=True)
            snap = engine.snapshot()
            self.assertIn("小雨", snap.weather_text)
            self.assertIn("湿度 88%", snap.weather_env)
        finally:
            await engine.stop()


if __name__ == "__main__":
    unittest.main()
