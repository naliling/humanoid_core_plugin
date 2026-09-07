"""精力 / 情绪 / 社交能量 / 天气服务的回归测试。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.config import HumanoidConfig
from humanoid.llm import LLMGateway, ProviderResolver
from humanoid.services.energy import EnergyService, describe_energy
from humanoid.services.mood import Delta, MoodService, local_delta
from humanoid.services.social import SocialEnergyService
from humanoid.services.weather import WeatherService, build_url, parse_payload
from humanoid.slots import normalize_slots
from humanoid.state import StateStore

from .fakes import FakeContext, FakeProvider, RecordingLogger

TZ = ZoneInfo("Asia/Shanghai")


def cfg(**overrides) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", **overrides})


WORKDAY = normalize_slots(
    [
        {"start": "00:00", "end": "08:00", "event": "睡眠", "energy_rate": 0.15},
        {"start": "08:00", "end": "12:00", "event": "工作", "energy_rate": -0.1},
        {"start": "12:00", "end": "13:00", "event": "午休", "energy_rate": 0.1},
        {"start": "13:00", "end": "18:00", "event": "工作", "energy_rate": -0.1},
        {"start": "18:00", "end": "24:00", "event": "休闲", "energy_rate": 0.0},
    ]
)


class FrozenClock:
    """可手动设定「现在」的 Clock 替身。"""

    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def now(self) -> datetime:
        return self.moment

    def today_str(self) -> str:
        return self.moment.strftime("%Y-%m-%d")

    def weekday(self) -> str:
        return "六"

    def advance(self, **kwargs) -> None:
        self.moment = self.moment + timedelta(**kwargs)


def build_store(conf: HumanoidConfig) -> StateStore:
    tmp = Path(tempfile.mkdtemp()) / "state.json"
    store = StateStore(tmp, lambda: 0.01)
    store.load("2026-08-22", conf.cycle_length)
    return store


class EnergyTest(unittest.TestCase):
    def build(self, conf=None, moment=None, slots=None):
        conf = conf or cfg()
        store = build_store(conf)
        clock = FrozenClock(moment or datetime(2026, 8, 22, 9, 0, tzinfo=TZ))
        service = EnergyService(store, lambda: conf, clock, lambda: slots or WORKDAY)
        return service, store, clock

    def test_new_day_reset_does_not_pin_energy_to_max(self):
        """跨天后凌晨三点的第一条消息不应把精力顶到上限。

        计费起点若被写成当天 00:00，自然恢复会按三小时计入，直接冲满。
        """
        service, store, clock = self.build(moment=datetime(2026, 8, 22, 3, 0, tzinfo=TZ))
        store.data["energy"] = 30.0
        store.data["last_update"] = "2026-08-21 22:10:00"
        energy = service.advance()
        self.assertLess(energy, 90.0, "跨天重置后不应接近上限")
        self.assertGreater(energy, 70.0)
        # 计费起点必须是「现在」，不是午夜
        self.assertEqual(store.get("last_update"), "2026-08-22 03:00:00")

    def test_work_slot_only_consumes(self):
        service, store, clock = self.build(moment=datetime(2026, 8, 22, 9, 0, tzinfo=TZ))
        store.data["energy"] = 90.0
        store.data["last_update"] = "2026-08-22 08:00:00"
        energy = service.advance()
        self.assertLess(energy, 90.0, "工作时段必须只减不加")

    def test_rest_slot_recovers(self):
        service, store, clock = self.build(moment=datetime(2026, 8, 22, 13, 0, tzinfo=TZ))
        store.data["energy"] = 50.0
        store.data["last_update"] = "2026-08-22 12:00:00"
        energy = service.advance()
        self.assertGreater(energy, 50.0)

    def test_natural_recovery_disabled(self):
        conf = cfg(enable_energy_natural_recovery=False)
        service, store, _ = self.build(conf, datetime(2026, 8, 22, 13, 0, tzinfo=TZ))
        store.data["energy"] = 50.0
        store.data["last_update"] = "2026-08-22 12:00:00"
        energy = service.advance()
        # 只剩日程本身的 0.1/分钟 × decay 0.5 × 60 = 3
        self.assertAlmostEqual(energy, 53.0, places=1)

    def test_recovery_interval_quantizes(self):
        conf = cfg(energy_natural_recovery_interval_minutes=30)
        service, store, _ = self.build(conf, datetime(2026, 8, 22, 12, 20, tzinfo=TZ))
        store.data["energy"] = 10.0
        store.data["last_update"] = "2026-08-22 12:00:00"
        energy_short = service.advance()

        service2, store2, _ = self.build(conf, datetime(2026, 8, 22, 12, 45, tzinfo=TZ))
        store2.data["energy"] = 10.0
        store2.data["last_update"] = "2026-08-22 12:00:00"
        energy_long = service2.advance()
        self.assertLess(energy_short, energy_long, "不满一个恢复间隔时不应计入自然恢复")

    def test_energy_clamped_to_max(self):
        conf = cfg(max_energy=60.0)
        service, store, _ = self.build(conf, datetime(2026, 8, 22, 8, 0, tzinfo=TZ))
        store.data["energy"] = 59.0
        store.data["last_update"] = "2026-08-22 00:00:00"
        self.assertLessEqual(service.advance(), 60.0)

    def test_clock_skew_backwards_is_safe(self):
        service, store, _ = self.build(moment=datetime(2026, 8, 22, 9, 0, tzinfo=TZ))
        store.data["energy"] = 42.0
        store.data["last_update"] = "2026-08-22 23:00:00"
        self.assertEqual(service.advance(), 42.0)
        self.assertEqual(store.get("last_update"), "2026-08-22 09:00:00")

    def test_consume_for_message(self):
        service, store, _ = self.build()
        store.data["energy"] = 50.0
        # energy_consumption_per_msg 默认 0.04
        self.assertAlmostEqual(service.consume_for_message(), 49.96, places=2)

    def test_cycle_advances_by_elapsed_days(self):
        service, store, clock = self.build()
        store.data["current_cycle_day"] = 27
        store.data["last_cycle_update"] = "2026-08-19"
        self.assertEqual(service.advance_cycle("2026-08-22"), 2)

    def test_cycle_respects_custom_length(self):
        conf = cfg(cycle_length=10)
        service, store, _ = self.build(conf)
        store.data["current_cycle_day"] = 10
        store.data["last_cycle_update"] = "2026-08-21"
        self.assertEqual(service.advance_cycle("2026-08-22"), 1)

    def test_cycle_description_styles(self):
        service, store, _ = self.build()
        store.data["current_cycle_day"] = 1
        self.assertIn("经期", service.cycle_description())
        conf = cfg(cycle_description_style="simple")
        service2, store2, _ = self.build(conf)
        store2.data["current_cycle_day"] = 8
        self.assertEqual(service2.cycle_description(), "卵泡期（第8天）")
        conf3 = cfg(enable_cycle=False)
        service3, _, _ = self.build(conf3)
        self.assertEqual(service3.cycle_description(), "")

    def test_describe_energy_bands(self):
        self.assertIn("充沛", describe_energy(95))
        self.assertIn("疲惫", describe_energy(5))


class MoodTest(unittest.IsolatedAsyncioTestCase):
    def build(self, conf=None, providers=None, now=1_000_000.0):
        conf = conf or cfg()
        self.conf = conf
        store = build_store(conf)
        log = RecordingLogger()
        ctx = FakeContext(chat_providers=list(providers or []))
        gateway = LLMGateway(ProviderResolver(ctx, log), lambda: self.conf, log)
        clock = FrozenClock(datetime(2026, 8, 22, 9, 0, tzinfo=TZ))
        self.time_value = now
        service = MoodService(
            store, lambda: self.conf, gateway, clock, log, time_source=lambda: self.time_value
        )
        return service, store, log

    async def test_profile_uses_configured_initials(self):
        service, _, _ = self.build(cfg(mood_initial_affection=60, mood_initial_libido=20))
        record = service.profile("1")
        self.assertEqual(record["affection"], 60.0)
        self.assertEqual(record["libido"], 20.0)
        self.assertEqual(record["base_affection"], 60.0)

    async def test_profile_honours_override(self):
        service, _, _ = self.build(cfg(mood_affection_override=["777:95"]))
        self.assertEqual(service.profile("777")["affection"], 95.0)
        self.assertEqual(service.profile("888")["affection"], 46.0)

    async def test_first_message_uses_local_rules_only(self):
        """新面孔的第一条消息不调模型，但走本地词典规则产生正常波动。"""
        provider = FakeProvider("p", reply='{"affection_delta":5,"libido_delta":0,"aggression_delta":0}')
        service, _, _ = self.build(
            cfg(mood_use_llm_for_delta=True, mood_provider_name="p", mood_sensitivity=100),
            [provider],
        )
        delta = await service.update_from_message("1", "你好棒", 80.0, 8)
        self.assertIsNotNone(delta, "第一条消息也应该产生情绪波动")
        self.assertGreater(delta.affection, 0)
        self.assertEqual(provider.calls, 0, "第一条消息不该为建档花一次模型调用")
        self.assertEqual(service.profile("1")["turn_count"], 1, "第一条消息是第 1 轮，不是第 2 轮")

    async def test_delta_cap_holds_after_all_modifiers(self):
        """精力 ×1.3 与排卵期 ×1.4 之后，单次变化仍不得超过 cap。"""
        service, _, _ = self.build(cfg(mood_affection_delta_cap=2, mood_sensitivity=100))
        service.profile("1")["last_interaction"] = self.time_value
        for _ in range(60):
            before = service.profile("1")["affection"]
            delta = await service.update_from_message("1", "你好棒，我最喜欢你了", 95.0, 14)
            after = service.profile("1")["affection"]
            self.assertLessEqual(abs(delta.affection), 2.0 + 1e-9, "delta 超过了 cap")
            self.assertLessEqual(abs(after - before), 2.0 + 1e-9)

    async def test_negative_message_lowers_affection(self):
        service, _, _ = self.build(cfg(mood_sensitivity=100))
        record = service.profile("1")
        record["last_interaction"] = self.time_value
        start = record["affection"]
        for _ in range(5):
            await service.update_from_message("1", "你真是个垃圾，滚", 80.0, 8)
        self.assertLess(service.profile("1")["affection"], start)

    async def test_local_delta_classification(self):
        self.assertLess(local_delta("你去死吧").affection, 0)
        self.assertGreater(local_delta("谢谢你，好棒").affection, 0)
        neutral = local_delta("今天几号")
        self.assertLessEqual(abs(neutral.affection), 0.5)

    async def test_decay_returns_to_baseline(self):
        service, _, _ = self.build(cfg(mood_decay_hours=6.0))
        record = service.profile("1")
        record["affection"] = 90.0
        record["base_affection"] = 46.0
        record["last_decay"] = self.time_value
        self.time_value += 6 * 3600 + 1
        self.assertTrue(service.decay_user("1"))
        self.assertAlmostEqual(service.profile("1")["affection"], 46.0, places=3)

    async def test_decay_is_gradual(self):
        service, _, _ = self.build(cfg(mood_decay_hours=6.0))
        record = service.profile("1")
        record["affection"] = 90.0
        record["base_affection"] = 46.0
        record["last_decay"] = self.time_value
        self.time_value += 3 * 3600
        service.decay_user("1")
        value = service.profile("1")["affection"]
        self.assertLess(value, 90.0)
        self.assertGreater(value, 46.0)

    async def test_decay_skips_when_too_recent(self):
        service, _, _ = self.build()
        record = service.profile("1")
        record["affection"] = 90.0
        record["base_affection"] = 46.0
        record["last_decay"] = self.time_value
        self.time_value += 60  # 1 分钟
        self.assertFalse(service.decay_user("1"))
        self.assertEqual(service.profile("1")["affection"], 90.0)

    async def test_decay_all_is_per_user(self):
        """每个用户各自记 last_decay，全量清扫与单用户衰减不会互相吃掉。"""
        service, store, _ = self.build(cfg(mood_decay_hours=6.0))
        for qq in ("1", "2"):
            record = service.profile(qq)
            record["affection"] = 90.0
            record["base_affection"] = 46.0
            record["last_decay"] = self.time_value
        self.time_value += 6 * 3600 + 1
        service.decay_user("1")
        self.assertEqual(service.decay_all(), 1, "用户 1 已衰减过，只剩用户 2 需要处理")
        self.assertAlmostEqual(service.profile("2")["affection"], 46.0, places=3)

    async def test_legacy_profile_without_last_decay(self):
        service, store, _ = self.build()
        store.data["moods"]["9"] = {
            "affection": 70.0,
            "libido": 30.0,
            "aggression": 10.0,
            "base_affection": 46.0,
            "base_libido": 34.0,
            "base_aggression": 28.0,
            "last_interaction": self.time_value - 3600,
            "turn_count": 5,
        }
        record = service.profile("9")
        self.assertIn("last_decay", record)
        self.assertEqual(record["affection"], 70.0)

    async def test_llm_delta_blended_when_enabled(self):
        provider = FakeProvider(
            "p", reply='{"affection_delta": 5, "libido_delta": 5, "aggression_delta": -5}'
        )
        service, _, _ = self.build(
            cfg(
                mood_use_llm_for_delta=True,
                mood_provider_name="p",
                mood_sensitivity=100,
                mood_llm_interval_messages=1,
            ),
            [provider],
        )
        service.profile("1")["last_interaction"] = self.time_value
        await service.update_from_message("1", "今天天气不错", 80.0, 8)
        self.assertEqual(provider.calls, 1)

    async def test_llm_failure_falls_back_to_local(self):
        broken = FakeProvider("p", error=RuntimeError("nope"))
        service, _, log = self.build(
            cfg(mood_use_llm_for_delta=True, mood_provider_name="p", mood_llm_interval_messages=1),
            [broken],
        )
        service.profile("1")["last_interaction"] = self.time_value
        delta = await service.update_from_message("1", "你好棒", 80.0, 8)
        self.assertIsNotNone(delta)
        self.assertIn("情绪分析失败", log.text("warning"))

    async def test_mood_log_respects_threshold_and_limit(self):
        service, store, _ = self.build(
            cfg(mood_log_max_entries=3, mood_log_threshold_affection=0, mood_sensitivity=100)
        )
        service.profile("1")["last_interaction"] = self.time_value
        for _ in range(6):
            await service.update_from_message("1", "你好棒", 80.0, 8)
        self.assertLessEqual(len(store.data["mood_logs"]["1"]), 3)
        self.assertEqual(len(service.logs("1", limit=2)), 2)

    async def test_mood_log_disabled(self):
        service, store, _ = self.build(cfg(mood_log_enabled=False, mood_sensitivity=100))
        service.profile("1")["last_interaction"] = self.time_value
        await service.update_from_message("1", "你好棒", 80.0, 8)
        self.assertEqual(store.data["mood_logs"], {})

    async def test_tag_updates_with_energy(self):
        service, _, _ = self.build(cfg(mood_sensitivity=100))
        service.profile("1")["last_interaction"] = self.time_value
        await service.update_from_message("1", "你好棒", 90.0, 8)
        self.assertIn("精力充沛", service.tag("1"))

    async def test_admin_operations(self):
        service, _, _ = self.build()
        self.assertEqual(service.set_affection("1", 77.0), 77.0)
        self.assertEqual(service.profile("1")["base_affection"], 77.0)
        self.assertEqual(service.set_affection_batch([("2", 10.0), ("3", 500.0)]), 1)
        reset = service.reset("1")
        self.assertEqual(reset["affection"], 46.0)
        self.assertEqual(reset["turn_count"], 0)

    async def test_disabled_mood_is_noop(self):
        service, _, _ = self.build(cfg(mood_enabled=False))
        self.assertIsNone(await service.update_from_message("1", "你好棒", 80.0, 8))
        self.assertFalse(service.decay_user("1"))
        self.assertEqual(service.decay_all(), 0)

    async def test_delta_helpers(self):
        d = Delta(2.0, 1.0, -1.0)
        self.assertEqual(d.scaled(2.0).affection, 4.0)
        self.assertEqual(d.capped(1.0).affection, 1.0)
        blended = Delta(0.0, 0.0, 0.0).blend(Delta(10.0, 10.0, 10.0), 0.3)
        self.assertAlmostEqual(blended.affection, 3.0)

    async def test_llm_call_interval(self):
        provider = FakeProvider("p", reply='{"affection_delta":1,"libido_delta":0,"aggression_delta":0}')
        service, _, _ = self.build(
            cfg(mood_use_llm_for_delta=True, mood_llm_interval_messages=3, mood_provider_name="p"),
            [provider]
        )
        service.profile("1")["last_interaction"] = self.time_value
        for _ in range(2):
            await service.update_from_message("1", "hi", 80, 8)
        self.assertEqual(provider.calls, 0)
        await service.update_from_message("1", "hi", 80, 8)
        self.assertEqual(provider.calls, 1)
        for _ in range(2):
            await service.update_from_message("1", "hi", 80, 8)
        self.assertEqual(provider.calls, 1)
        await service.update_from_message("1", "hi", 80, 8)
        self.assertEqual(provider.calls, 2)

    async def test_first_message_does_not_call_llm(self):
        """计数器从 0 起算，所以新面孔要攒满 interval 条消息才会触发第一次模型分析。"""
        provider = FakeProvider("p", reply='{"affection_delta":1,"libido_delta":0,"aggression_delta":0}')
        service, _, _ = self.build(
            cfg(mood_use_llm_for_delta=True, mood_llm_interval_messages=5, mood_provider_name="p"),
            [provider]
        )
        await service.update_from_message("1", "hello", 80, 8)
        self.assertEqual(provider.calls, 0)
        for _ in range(4):
            await service.update_from_message("1", "hello", 80, 8)
        self.assertEqual(provider.calls, 1, "第 5 条消息才该调模型")

    async def test_interval_rearms_after_failure(self):
        """失败也会重置计数器：再攒满一个间隔就重新尝试（此处关掉冷却单独验计数器）。"""
        broken = FakeProvider("p", error=RuntimeError("fail"))
        service, _, _ = self.build(
            cfg(
                mood_use_llm_for_delta=True,
                mood_llm_interval_messages=2,
                mood_provider_name="p",
                mood_provider_cooldown_minutes=0,
            ),
            [broken]
        )
        service.profile("1")["last_interaction"] = self.time_value
        await service.update_from_message("1", "hi", 80, 8)
        self.assertEqual(broken.calls, 0)
        await service.update_from_message("1", "hi", 80, 8)
        self.assertEqual(broken.calls, 1)
        await service.update_from_message("1", "hi", 80, 8)
        self.assertEqual(broken.calls, 1)
        await service.update_from_message("1", "hi", 80, 8)
        self.assertEqual(broken.calls, 2)

    async def test_cooldown_blocks_retry_even_when_interval_is_reached(self):
        """冷却期内即使凑够了间隔也不会真的发请求 —— 把这个组合行为显式钉下来。"""
        broken = FakeProvider("p", error=RuntimeError("fail"))
        service, _, _ = self.build(
            cfg(
                mood_use_llm_for_delta=True,
                mood_llm_interval_messages=2,
                mood_provider_name="p",
                mood_provider_cooldown_minutes=5,
                schedule_allow_global_fallback=False,
            ),
            [broken]
        )
        service.profile("1")["last_interaction"] = self.time_value
        for _ in range(6):
            await service.update_from_message("1", "hi", 80, 8)
        self.assertEqual(broken.calls, 1, "第一次失败后进入冷却，后续尝试都被网关短路")


class SocialEnergyTest(unittest.IsolatedAsyncioTestCase):
    def build(self, conf=None, moment=None):
        conf = conf or cfg()
        self.conf = conf
        store = build_store(conf)
        clock = FrozenClock(moment or datetime(2026, 8, 22, 9, 0, tzinfo=TZ))
        service = SocialEnergyService(store, lambda: self.conf, clock, RecordingLogger())
        return service, store, clock

    async def test_consume_and_recover(self):
        service, _, _ = self.build(cfg(social_energy_consumption_per_msg=1.0))
        self.assertEqual(service.value, 100.0)
        for _ in range(10):
            service.consume_for_message()
        self.assertAlmostEqual(service.value, 90.0)
        service.recover(400)  # 400 秒 ≈ 6.67 分钟 × 1.5/分钟 = 10
        self.assertAlmostEqual(service.value, 100.0)

    async def test_never_exceeds_bounds(self):
        service, store, _ = self.build(cfg(social_energy_consumption_per_msg=200.0))
        service.consume_for_message()
        self.assertEqual(service.value, 0.0)
        service.recover(10_000)
        self.assertEqual(service.value, 100.0)

    async def test_daily_reset_once(self):
        service, store, clock = self.build(
            cfg(social_energy_reset_hour=6), datetime(2026, 8, 22, 7, 0, tzinfo=TZ)
        )
        store.data["social_energy"] = 10.0
        self.assertTrue(service.maybe_daily_reset())
        self.assertEqual(service.value, 100.0)
        store.data["social_energy"] = 20.0
        self.assertFalse(service.maybe_daily_reset(), "同一天只重置一次")
        self.assertEqual(service.value, 20.0)

    async def test_daily_reset_waits_for_hour(self):
        service, store, _ = self.build(
            cfg(social_energy_reset_hour=6), datetime(2026, 8, 22, 3, 0, tzinfo=TZ)
        )
        store.data["social_energy"] = 10.0
        self.assertFalse(service.maybe_daily_reset())

    async def test_daily_reset_disabled(self):
        service, store, _ = self.build(cfg(social_energy_reset_hour=-1))
        store.data["social_energy"] = 10.0
        self.assertFalse(service.maybe_daily_reset())

    async def test_disabled_service_is_noop(self):
        service, store, _ = self.build(cfg(social_energy_enabled=False))
        store.data["social_energy"] = 50.0
        service.consume_for_message()
        service.recover(600)
        self.assertEqual(service.value, 50.0)

    async def test_recovery_loop_stops_immediately(self):
        conf = cfg()
        self.conf = conf
        store = build_store(conf)
        clock = FrozenClock(datetime(2026, 8, 22, 9, 0, tzinfo=TZ))
        service = SocialEnergyService(store, lambda: self.conf, clock)
        store.data["social_energy"] = 0.0
        stop = asyncio.Event()
        task = asyncio.create_task(service.run_recovery_loop(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)  # 关停不该等满 60 秒
        self.assertGreater(service.value, 0.0, "第一轮就应该恢复过一次")

    async def test_hint_bands(self):
        service, store, _ = self.build()
        self.assertIn("热情", service.hint())
        store.data["social_energy"] = 10.0
        self.assertIn("简短", service.hint())
        self.assertEqual(service.text, "较低")


class WeatherTest(unittest.IsolatedAsyncioTestCase):
    PAYLOAD = {
        "weather": [{"description": "多云"}],
        "main": {"temp": 26.5, "humidity": 70},
    }

    def build(self, conf=None, fetch=None, moment=None):
        conf = conf or cfg(weather_api_key="0123456789abcdef", weather_location="Beijing,CN")
        self.conf = conf
        store = build_store(conf)
        clock = FrozenClock(moment or datetime(2026, 8, 22, 9, 0, tzinfo=TZ))
        service = WeatherService(store, lambda: self.conf, clock, fetch, RecordingLogger())
        return service, store, clock

    async def test_snapshot_never_awaits_and_reports_pending(self):
        service, _, _ = self.build()
        snap = service.snapshot()
        self.assertIn("获取中", snap["env"])

    async def test_disabled_snapshot(self):
        service, _, _ = self.build(cfg(weather_enabled=False))
        self.assertEqual(service.snapshot()["env"], "天气未开启")

    async def test_missing_key_snapshot(self):
        service, _, _ = self.build(cfg(weather_api_key="short"))
        self.assertIn("未填 API Key", service.snapshot()["env"])
        self.assertFalse(await service.refresh())

    async def test_refresh_caches_result(self):
        calls: list[str] = []

        async def fetch(url: str, timeout: float) -> dict:
            calls.append(url)
            return self.PAYLOAD

        service, store, _ = self.build(fetch=fetch)
        self.assertTrue(await service.refresh())
        self.assertIn("多云", service.snapshot()["weather"])
        self.assertIn("湿度 70%", service.snapshot()["env"])
        self.assertEqual(store.get("_cached_location"), "Beijing,CN")
        self.assertFalse(await service.refresh(), "缓存未过期时不应再请求")
        self.assertEqual(len(calls), 1)

    async def test_refresh_after_interval(self):
        async def fetch(url: str, timeout: float) -> dict:
            return self.PAYLOAD

        service, store, clock = self.build(
            cfg(weather_api_key="0123456789abcdef", weather_refresh_minutes=60), fetch
        )
        await service.refresh()
        clock.advance(minutes=61)
        self.assertTrue(service.is_stale())
        self.assertTrue(await service.refresh())

    async def test_location_change_invalidates_cache(self):
        async def fetch(url: str, timeout: float) -> dict:
            return self.PAYLOAD

        service, store, _ = self.build(fetch=fetch)
        await service.refresh()
        self.conf = cfg(weather_api_key="0123456789abcdef", weather_location="Tokyo,JP")
        self.assertTrue(service.is_stale())
        self.assertIn("获取中", service.snapshot()["env"])

    async def test_fetch_failure_keeps_old_cache(self):
        state = {"fail": False}

        async def fetch(url: str, timeout: float) -> dict:
            if state["fail"]:
                raise RuntimeError("network down")
            return self.PAYLOAD

        service, _, clock = self.build(fetch=fetch)
        await service.refresh()
        state["fail"] = True
        clock.advance(minutes=120)
        self.assertFalse(await service.refresh())
        self.assertIn("多云", service.snapshot()["weather"], "失败时应保留上次结果")

    async def test_malformed_payload_rejected(self):
        async def fetch(url: str, timeout: float) -> dict:
            return {"unexpected": True}

        service, _, _ = self.build(fetch=fetch)
        self.assertFalse(await service.refresh())

    async def test_url_encodes_location(self):
        url = build_url("Nizhny Novgorod,RU", "key with space")
        self.assertNotIn(" ", url)
        self.assertIn("appid=key%20with%20space", url)

    async def test_parse_payload_without_humidity(self):
        parsed = parse_payload({"weather": [{"description": "晴"}], "main": {"temp": 30}}, "X")
        self.assertIsNotNone(parsed)
        self.assertNotIn("湿度", parsed["env"])


if __name__ == "__main__":
    unittest.main()
