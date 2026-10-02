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

from .fakes import FakeContext, FakeProvider, RecordingLogger, ScopeStore

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


def build_store(conf: HumanoidConfig) -> ScopeStore:
    tmp = Path(tempfile.mkdtemp()) / "state.json"
    store = ScopeStore(tmp)
    store.load("2026-08-22", conf.cycle_length)
    return store


class EnergyTest(unittest.TestCase):
    def build(self, conf=None, moment=None, slots=None):
        conf = conf or cfg()
        store = build_store(conf)
        clock = FrozenClock(moment or datetime(2026, 8, 22, 9, 0, tzinfo=TZ))
        service = EnergyService(store.scope, lambda: conf, clock, lambda: slots or WORKDAY)
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
        """档位要对得上，但措辞按天抽签，所以不能钉死某一句。"""
        from humanoid.services.energy import ENERGY_WORDS

        def tier_words(value):
            floor, options = max(
                (entry for entry in ENERGY_WORDS if value >= entry[0]), key=lambda entry: entry[0]
            )
            return options

        high = describe_energy(95)
        low = describe_energy(5)
        self.assertIn(high, tier_words(95))
        self.assertIn(low, tier_words(5))
        self.assertNotEqual(high, low)
        # 「语气轻快/语气低落」那半句是在教她怎么说话，不许再出现在精力描述里。
        for text in (describe_energy(v) for v in (95, 75, 50, 25, 5)):
            self.assertNotIn("语气", text, text)


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
            store.scope,
            lambda: self.conf,
            clock,
            gateway=gateway,
            logger=log,
            time_source=lambda: self.time_value,
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
        self.assertEqual(service.profile("888")["affection"], 35.0)

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
        store.user("9")["mood"] = {
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
        self.assertLessEqual(len(store.user("1")["mood_logs"]), 3)
        self.assertEqual(len(service.logs("1", limit=2)), 2)

    async def test_mood_log_disabled(self):
        service, store, _ = self.build(cfg(mood_log_enabled=False, mood_sensitivity=100))
        service.profile("1")["last_interaction"] = self.time_value
        await service.update_from_message("1", "你好棒", 80.0, 8)
        self.assertEqual(store.user("1").get("mood_logs", []), [])

    async def test_tag_updates_with_energy(self):
        service, _, _ = self.build(cfg(mood_sensitivity=100))
        service.profile("1")["last_interaction"] = self.time_value
        await service.update_from_message("1", "你好棒", 90.0, 8)
        # 同一档多条等价说法随机抽，断言得认全该档的词。
        high_energy = ("精力充沛", "精神饱满", "状态在线", "劲头很足")
        tag = service.tag("1")
        self.assertTrue(any(w in tag for w in high_energy), tag)

    async def test_admin_operations(self):
        service, _, _ = self.build()
        self.assertEqual(service.set_affection("1", 77.0), 77.0)
        self.assertEqual(service.profile("1")["base_affection"], 77.0)
        self.assertEqual(service.set_affection_batch([("2", 10.0), ("3", 500.0)]), 1)
        reset = service.reset("1")
        self.assertEqual(reset["affection"], 35.0)
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

    async def test_local_negative_skips_model_without_changing_result(self):
        """本地判负明确时那次模型调用是白花的，跳过它结果一个数都不差。

        情绪是「本地词典 + 模型」融合，模型只占 0.3 权重（见 `_resolve_delta`）：负面词典
        命中时 `base.affection` 必然 ≤ -2，结果会走 `base.scaled(1.2)` 把模型那一票整段
        丢掉。所以调模型之前先判一次：省掉一次网络往返，而好感照样按本地词典跌。
        """
        conf = cfg(mood_use_llm_for_delta=True, mood_llm_interval_messages=3,
                   mood_provider_name="p")
        # 模型给一个大到无法忽视的正向结果：一旦它被采纳，好感必然明显偏高。
        reply = '{"affection_delta":9,"libido_delta":0,"aggression_delta":0}'

        hot_provider = FakeProvider("p", reply=reply)
        hot, _, _ = self.build(conf, [hot_provider])
        hot.profile("1")["last_interaction"] = self.time_value
        for _ in range(3):
            await hot.update_from_message("1", "你真蠢滚开")

        self.assertEqual(hot_provider.calls, 0, "本地已判负时不该调模型")
        self.assertLess(
            hot.profile("1")["affection"], conf.mood_initial_affection,
            "负面消息照样要掉好感（本地词典在起作用，不是没生效）",
        )

        mild_provider = FakeProvider("p", reply=reply)
        mild, _, _ = self.build(conf, [mild_provider])
        mild.profile("1")["last_interaction"] = self.time_value
        for _ in range(3):
            await mild.update_from_message("1", "hi")

        self.assertEqual(mild_provider.calls, 1, "中性消息该照常调模型")

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
        service = SocialEnergyService(store.scope, lambda: self.conf, clock, RecordingLogger())
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
        service = SocialEnergyService(store.scope, lambda: self.conf, clock)
        store.data["social_energy"] = 0.0
        stop = asyncio.Event()
        task = asyncio.create_task(service.run_recovery_loop(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)  # 关停不该等满 60 秒
        self.assertGreater(service.value, 0.0, "第一轮就应该恢复过一次")

    async def test_hint_bands(self):
        """意愿描述随能量单调变淡，而且不能出现数值。"""
        service, store, _ = self.build()
        store.data["social_energy"] = 95.0
        talkative = service.hint()
        store.data["social_energy"] = 10.0
        terse = service.hint()
        self.assertIn("意愿高", talkative)
        self.assertIn("独处", terse)
        self.assertNotIn("95", talkative)
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
        service = WeatherService(store.scope, lambda: self.conf, clock, fetch, RecordingLogger())
        return service, store, clock

    async def test_snapshot_never_awaits_and_reports_pending(self):
        service, _, _ = self.build()
        snap = service.snapshot()
        self.assertIn("获取中", snap["env"])

    async def test_disabled_snapshot(self):
        service, _, _ = self.build(cfg(weather_enabled=False))
        self.assertEqual(service.snapshot()["env"], "天气未开启")

    async def test_missing_key_snapshot(self):
        service, _, _ = self.build(cfg(weather_api_key="short", weather_location="Beijing,CN"))
        self.assertIn("未填 API Key", service.snapshot()["env"])
        self.assertFalse(await service.refresh_async())

    async def test_no_weather_city_configured_is_reported_not_guessed(self):
        """城市没配好时直接说没配，不能拿另一个城市的天气冒充。"""
        service, _, _ = self.build(cfg(weather_api_key="0123456789abcdef", weather_location=""))
        snap = service.snapshot()
        self.assertIn("没配天气城市", snap["env"])
        self.assertEqual(snap["weather"], "")
        self.assertFalse(service.is_stale(), "没有城市就不该发起请求")

    async def test_refresh_caches_result(self):
        calls: list[str] = []

        async def fetch(url: str, timeout: float) -> dict:
            calls.append(url)
            return self.PAYLOAD

        service, store, _ = self.build(fetch=fetch)
        self.assertTrue(await service.refresh_async())
        self.assertIn("多云", service.snapshot()["weather"])
        self.assertIn("湿度 70%", service.snapshot()["env"])
        self.assertEqual(store.get("_cached_location"), "Beijing,CN")
        self.assertFalse(await service.refresh_async(), "缓存未过期时不应再请求")
        self.assertEqual(len(calls), 1)

    async def test_refresh_after_interval(self):
        async def fetch(url: str, timeout: float) -> dict:
            return self.PAYLOAD

        service, store, clock = self.build(
            cfg(weather_api_key="0123456789abcdef", weather_location="Beijing,CN", weather_refresh_minutes=60), fetch
        )
        await service.refresh_async()
        clock.advance(minutes=61)
        self.assertTrue(service.is_stale())
        self.assertTrue(await service.refresh_async())

    async def test_location_change_invalidates_cache(self):
        async def fetch(url: str, timeout: float) -> dict:
            return self.PAYLOAD

        service, store, _ = self.build(fetch=fetch)
        await service.refresh_async()
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
        await service.refresh_async()
        state["fail"] = True
        clock.advance(minutes=120)
        self.assertFalse(await service.refresh_async())
        self.assertIn("多云", service.snapshot()["weather"], "失败时应保留上次结果")

    async def test_malformed_payload_rejected(self):
        async def fetch(url: str, timeout: float) -> dict:
            return {"unexpected": True}

        service, _, _ = self.build(fetch=fetch)
        self.assertFalse(await service.refresh_async())

    async def test_url_encodes_location(self):
        url = build_url("Nizhny Novgorod,RU", "key with space")
        self.assertNotIn(" ", url)
        self.assertIn("appid=key%20with%20space", url)

    async def test_parse_payload_without_humidity(self):
        parsed = parse_payload({"weather": [{"description": "晴"}], "main": {"temp": 30}})
        self.assertIsNotNone(parsed)
        self.assertNotIn("湿度", parsed["env"])


if __name__ == "__main__":
    unittest.main()



class AffectionIsALedgerNotAnAccumulator(unittest.TestCase):
    """好感必须**能跌**，而且她要**记得发生过什么**。

    原来 `_commit` 是纯累加器：`record[key] = clamp(before[key] + values[key])`。
    只能往上——因为没有任何机制把「他已经不来了」记成负的。衰减把它拉回 base 就
    算完事，于是表现成「涨得太快、只涨不跌、设定值也不动」。

    改成账本：每一笔记进「发生过的事」，好感 = base + **账本此刻的净值**（每笔
    按距今时衰）。于是三件事同时成立：
      · 她三个月前对你好、今天不理你了 → 旧的衰到 0，新的负的还在 → **会跌**
      · 设定的值是起点不是天花板
      · 她能说得出「你最近老是敷衍我」，因为她知道那件事
    """

    def build(self, **kw):
        t = MoodTest("test_profile_uses_configured_initials")
        t.setUp()
        svc, _store, _log = t.build(cfg(mood_initial_affection=50, mood_sensitivity=100, **kw))
        svc.profile("1")
        return t, svc

    def _run(self, svc, text, energy, n, step=3600.0):
        async def go():
            for _ in range(n):
                await svc.update_from_message("1", text, energy, 14)
                self.t.time_value += step
        asyncio.run(go())

    def test_affection_falls_when_they_go_cold(self):
        t, svc = self.build()
        self.t = t
        self._run(svc, "你好棒，我最喜欢你了", 95.0, 12)
        up = svc.profile("1")["affection"]
        self._run(svc, "你好烦 真讨厌", 40.0, 5)
        down = svc.profile("1")["affection"]
        self.assertLess(down, up, "敷衍之后好感必须跌——原来这里是纯累加，只会涨")
        self.assertGreater(down, 30.0, "也不能跌穿设定基线太多")

    def test_the_set_value_is_a_starting_point_not_a_ceiling(self):
        t, svc = self.build()
        self.t = t
        self.assertEqual(svc.profile("1")["base_affection"], 50.0)
        self._run(svc, "你好棒，我最喜欢你了", 95.0, 12)
        self.assertGreater(svc.profile("1")["affection"], 50.0,
                           "好感该能浮在设定值之上")
        # 「下的快上的也快」：负向账目更重，所以几笔冷淡就足以压回设定值附近
        self._run(svc, "你好烦 真讨厌", 40.0, 8)
        got = svc.profile("1")["affection"]
        self.assertLess(got, 56.0,
                        f"冷下来之后仍悬在 {got:.1f}，负向不够重（'下的快'没兑现）")
        self._run(svc, "你好烦 真讨厌", 40.0, 8)
        self.assertLess(svc.profile("1")["affection"], 50.0,
                        "继续冷淡就该跌回设定值以下")

    def test_she_remembers_what_happened(self):
        t, svc = self.build()
        self.t = t
        self._run(svc, "你好棒，我最喜欢你了", 95.0, 4)
        self._run(svc, "你好烦 真讨厌", 40.0, 3)
        kinds = [k for k, _ in svc.recent_events("1")]
        self.assertTrue(kinds, "账本是空的——那她就只知道分数、不知道原因")
        self.assertTrue(any(k in ("有点冷", "很敷衍") for k in kinds),
                        f"最近几件里有敷衍，账本却记成 {kinds}")

    def test_one_message_is_counted_once(self):
        """base 不再逐条漂移。

        原来每条消息除了进累加器、还按 delta 的一半推 base——同一件事算两遍，
        单次变化于是超过 delta 的 cap（实测 2.07 > 2.0）。
        """
        t, svc = self.build(mood_affection_delta_cap=2)
        self.t = t
        for _ in range(6):
            before = svc.profile("1")["affection"]
            self._run(svc, "你好棒，我最喜欢你了", 95.0, 1)
            after = svc.profile("1")["affection"]
            self.assertLessEqual(abs(after - before), 2.0 + 1e-6,
                                 f"单条把好感推动了 {abs(after - before):.2f}，超过 cap")


class ColdnessIsNotInsults(unittest.TestCase):
    """**能让她掉分的不该只有骂她的人。**

    原来 `NEGATIVE_PATTERN` 只认脏话，于是「嗯」「哦」「随你」「不想说」这类
    真正让人心里一沉的敷衍全都走中性分支（`uniform(-0.5, 0.5)`，均值 0）——
    攒一百次也攒不出变化。冷淡识别必须单独一份，而且要和脏话分开。

    更难的是**别冤枉人**：「我今天有点烦」「我好累」里有「烦」「累」，跟冷淡词
    长得像，但那是 TA 自己难受，不是 TA 对我们冷淡。自我状态**一票否决**在所有
    分支之前。
    """

    def _kind(self, text):
        from humanoid.services.mood import local_delta
        v = local_delta(text).affection
        if v <= -1.5:
            return "负"
        if v <= -0.55:
            return "冷"
        if v >= 0.8:
            return "正"
        return "中性"

    def test_dismissive_tone_counts_as_cold(self):
        for t in ("嗯", "哦", "随你", "算了吧", "我不想说", "关我什么事", "随便"):
            self.assertEqual(self._kind(t), "冷", f"「{t}」该算冷淡")

    def test_insults_still_land_harder(self):
        for t in ("你好烦 真讨厌", "你真烦", "滚"):
            self.assertEqual(self._kind(t), "负", f"「{t}」该算骂人，不是冷淡")

    def test_their_own_bad_day_is_not_coldness_toward_us(self):
        """**最容易做错的一条。**"""
        for t in ("我今天有点烦", "我好累啊", "我最近状态不好", "我有点难过"):
            self.assertEqual(self._kind(t), "中性",
                             f"「{t}」是 TA 自己难受，不该记成对方冷淡")

    def test_warmth_still_lands(self):
        for t in ("你真棒", "谢谢你", "我最喜欢你了"):
            self.assertEqual(self._kind(t), "正", f"「{t}」该算暖")

    def test_cold_and_silent_ranges_do_not_overlap(self):
        """冷淡 (-1.2,-0.6) 与中性 (-0.5,0.5) 之间**必须留缝**。

        原来冷淡写的是 (-1.2,-0.4)，和中性在 (-0.5,-0.4) 交叠——于是「敷衍」和
        「没说话」从数值上分不开，测试都没法判断一条属于哪边。
        """
        from humanoid.services import mood as M
        seen = {round(M.local_delta("嗯").affection, 2) for _ in range(300)}
        self.assertTrue(all(-1.5 < v <= -0.55 for v in seen),
                        f"冷淡里混进了中性值：{sorted(seen)[:5]}")

    def test_repeated_dismissal_eventually_shows(self):
        """单句几乎不推好感，靠账本累积。"""
        t = MoodTest("test_profile_uses_configured_initials")
        t.setUp()
        svc, _s, _l = t.build(cfg(mood_initial_affection=50, mood_sensitivity=100))
        svc.profile("1")

        async def spam(words, hours=6.0):
            for w in words:
                await svc.update_from_message("1", w, 60.0, 14)
                t.time_value += hours * 3600

        asyncio.run(spam(["嗯"] * 3))
        few = svc.profile("1")["affection"]
        asyncio.run(spam(["嗯", "哦", "随你"] * 10))
        many = svc.profile("1")["affection"]
        self.assertLess(few, 50.0)
        self.assertGreater(few, 40.0, f"三句就掉了 {50 - few:.1f}，太重了")
        self.assertLess(many, 25.0, "连着两周敷衍该把好感拉下来")
        kinds = [k for k, _ in svc.recent_events("1", 5)]
        self.assertTrue(all(k in ("有点冷", "很敷衍") for k in kinds),
                        f"账本记的是 {kinds}")


class ProactiveSilenceCostsAffection(unittest.TestCase):
    """她主动找了 TA、没被回 → **好感要降**。

    原来这条链路是断的：社交层把「被冷落」写进信号文件，Core 读到之后
    `soma.set_social_feedback(streak)` 接的是**社交能量**（她少想找人说话），
    不是**好感度**。于是「他对TA爱搭不理」会让她变得冷淡，
    但**不会让她对 TA 少一分喜欢**——那不对。
    """

    def _svc(self, base=70):
        t = MoodTest("test_profile_uses_configured_initials")
        t.setUp()
        svc, _s, _l = t.build(cfg(mood_initial_affection=base))
        svc.profile("1")
        return svc

    def test_being_ignored_lowers_affection(self):
        svc = self._svc(70)
        start = svc.profile("1")["affection"]
        for k in (1, 2, 3, 4, 5):
            svc.note_ignored_by_peer("1", k)
        self.assertLess(svc.profile("1")["affection"], start,
                        "连着五次主动发出去没被回，好感却一点没降")

    def test_the_same_streak_is_not_counted_twice(self):
        """**最容易出的错。** streak 是不回就累加的，看增量而不是看绝对值——
        每轮结算都调一次的话，一天下来同一个数会被扣十几次，好感直接打到 0。"""
        svc = self._svc(70)
        svc.note_ignored_by_peer("1", 3)
        once = svc.profile("1")["affection"]
        for _ in range(50):
            svc.note_ignored_by_peer("1", 3)
        self.assertAlmostEqual(svc.profile("1")["affection"], once, places=6,
                               msg="同一个 streak 反复上报被重复扣了")
        svc.note_ignored_by_peer("1", 5)
        self.assertLess(svc.profile("1")["affection"], once,
                        "streak 涨到 5 时该再扣一次")

    def test_it_can_really_go_down_but_stays_above_zero(self):
        """掉到 0 是可能的，但不该轻轻一下就归零——那不像记仇，像迁怒。"""
        svc = self._svc(50)
        for k in range(1, 40):
            svc.note_ignored_by_peer("1", k)
        got = svc.profile("1")["affection"]
        self.assertGreaterEqual(got, 0.0)
        self.assertLess(got, 25.0, f"连着 39 次没回只掉到 {got:.1f}，太温和了")

    def test_ignored_is_not_the_same_as_insulted(self):
        """被冷落记的是「有点冷」，不是「很敷衍」——它们轻重不同。"""
        svc = self._svc(70)
        svc.note_ignored_by_peer("1", 2)
        kinds = [k for k, _ in svc.recent_events("1", 3)]
        self.assertTrue(kinds, "账本里该留下这一笔")
        self.assertTrue(all(k in ("温和", "有点冷", "一直没被理") for k in kinds),
                        f"被不回不该记成 {kinds}——「很敷衍」是骂人的分量")
