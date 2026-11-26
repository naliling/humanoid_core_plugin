"""日程接续（v3.0.11）：空档保持覆盖，普通补排仍遵守模型调用间隔。"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.config import HumanoidConfig
from humanoid.llm import CallGate, LLMGateway, ProviderResolver
from humanoid.services.schedule import (
    SOURCE_LLM,
    ScheduleService,
    parse_segment,
)
from humanoid.state import StateStore

from .fakes import FakeContext, FakeProvider, FrozenClock, RecordingLogger, ScopeStore

TZ = ZoneInfo("Asia/Shanghai")
FIXED_TODAY = "2026-08-22"
FIXED_MOMENT = datetime(2026, 8, 22, 9, 0, tzinfo=TZ)


def cfg(**overrides) -> HumanoidConfig:
    # v3.1.28 起模型自审层已删；本文件只留闸门间隔、空档接续与固定段解析。
    return HumanoidConfig.from_raw(
        {"timezone_city": "北京", "schedule_change_chance": 0,
         **overrides}
    )


def seg_reply(event="去超市买菜", minutes=85, **extra) -> str:
    payload = {
        "continue": False,
        "event": event,
        "location": "超市",
        "emotion": "随性",
        "energy_rate": -0.05,
        "minutes": minutes,
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def continue_reply(minutes=60) -> str:
    return json.dumps({"continue": True, "minutes": minutes}, ensure_ascii=False)


def review_reply(**payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


class GateIntervalTest(unittest.TestCase):
    """闸门：ignore_interval 只越过「还没到间隔」，别的照旧。"""

    def build(self, **overrides):
        raw = {
            "schedule_min_interval_minutes": 30,
            "schedule_max_interval_minutes": 30,
            "llm_idle_silence_minutes": 0,
            "llm_daily_call_budget": 10,
        }
        raw.update(overrides)
        conf = cfg(**raw)
        gate = CallGate(lambda: conf, None)
        return conf, gate

    def test_interval_blocks_the_next_call(self):
        _conf, gate = self.build()
        gate.consume("schedule")
        self.assertFalse(gate.check("schedule").allowed)

    def test_ignore_interval_lets_it_through(self):
        _conf, gate = self.build()
        gate.consume("schedule")
        self.assertTrue(gate.check("schedule", ignore_interval=True).allowed)

    def test_ignore_interval_still_counts_budget(self):
        """预算不跟着一起绕：绕开预算等于开一个不限量的口子。"""
        _conf, gate = self.build(llm_daily_call_budget=1)
        gate.consume("schedule")
        verdict = gate.check("schedule", ignore_interval=True)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "budget")

    def test_ignore_interval_still_respects_idle_silence(self):
        """空闲静默照旧：没人说话的时候不该因为「空档」就偷偷排日程。"""
        _conf, gate = self.build(llm_idle_silence_minutes=60)
        gate.note_interaction(now=0.0, role_id="r1")
        verdict = gate.check("schedule", now=3 * 3600.0, role_id="r1", ignore_interval=True)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "idle")


class GapContinuityTest(unittest.IsolatedAsyncioTestCase):
    """空档接续：间隔内不重复问模型，用本地动态段覆盖时间线。"""

    def build(self, **overrides):
        conf = cfg(
            schedule_provider_name="p",
            schedule_min_interval_minutes=30,
            schedule_max_interval_minutes=30,
            llm_idle_silence_minutes=0,
            **overrides,
        )
        self.conf = conf
        tmp = Path(tempfile.mkdtemp()) / "state.json"
        store = ScopeStore(tmp)
        store.load(FIXED_TODAY, conf.cycle_length)
        log = RecordingLogger()
        provider = FakeProvider("p", reply=seg_reply(event="倒杯水", minutes=5))
        ctx = FakeContext(chat_providers=[provider])
        resolver = ProviderResolver(ctx, log)
        clock = FrozenClock(FIXED_MOMENT)
        monotonic = clock.timestamp
        gate = CallGate(lambda: self.conf, log, wall_clock=clock.timestamp, monotonic=monotonic)
        gate.note_interaction(now=clock.timestamp(), role_id=str(store.scope.role_id or ""))
        gateway = LLMGateway(resolver, lambda: self.conf, log, monotonic, gate=gate)

        service = ScheduleService(
            store.scope, lambda: self.conf, clock, logger=log, monotonic=monotonic
        )
        service.set_resolver_gateway(resolver, gateway)
        return service, provider, gateway

    async def test_gap_stays_covered_without_repeating_model_within_interval(self):
        service, provider, _gateway = self.build(schedule_time_granularity="15min")
        await service.ensure_fresh()  # 09:00-09:05 倒杯水（保留真实短时长）
        self.assertEqual(
            provider.calls, 1,
            f"首段未到 provider；result={_gateway.last_result('日程生成')!r}; status={service.status()!r}",
        )
        service._clock.advance(minutes=6)  # 09:06，距离模型间隔下限仍不足 30 分钟
        self.assertTrue(service.refresh_due())
        self.assertTrue(await service.ensure_fresh())
        self.assertEqual(provider.calls, 1, "普通空档不能绕过 30 分钟模型调用间隔")
        active, _prev = service._active_and_prev(9 * 60 + 6)
        self.assertIsNotNone(active, "模型调用被限流后仍要用本地动态片段覆盖空档")

    async def test_idle_silence_still_blocks_even_in_a_gap(self):
        """空档不是免死金牌：没人跟她说话时，照样一次都不许调。"""
        conf = cfg(
            schedule_provider_name="p",
            schedule_min_interval_minutes=0,
            schedule_max_interval_minutes=0,
            llm_idle_silence_minutes=60,
        )
        tmp = Path(tempfile.mkdtemp()) / "state.json"
        store = ScopeStore(tmp)
        store.load(FIXED_TODAY, conf.cycle_length)
        log = RecordingLogger()
        provider = FakeProvider("p", reply=seg_reply(event="倒杯水", minutes=5))
        resolver = ProviderResolver(FakeContext(chat_providers=[provider]), log)
        gate = CallGate(lambda: conf, log)
        gateway = LLMGateway(resolver, lambda: conf, log, time.monotonic, gate=gate)
        service = ScheduleService(
            store.scope, lambda: conf, FrozenClock(FIXED_MOMENT), logger=log,
            monotonic=time.monotonic,
        )
        service.set_resolver_gateway(resolver, gateway)
        await service.ensure_fresh()
        self.assertEqual(provider.calls, 0, "没人说话时不该开始排日程")
        service._clock.advance(minutes=6)
        await service.ensure_fresh()
        self.assertEqual(provider.calls, 0, "空档也不能绕开空闲静默")


class FixedSegmentParseTest(unittest.TestCase):
    def test_minimal_fixed_payload_is_usable(self):
        slot = parse_segment(
            {"event": "把杯子洗了", "location": "厨房", "minutes": 6},
            now_minute=9 * 60,
            step=5,
            prev=None,
        )
        self.assertIsNotNone(slot)
        self.assertEqual(slot["start"], "09:00")
        self.assertEqual(slot["end"], "09:06", "6 分钟活动保留真实时长（#311）")


if __name__ == "__main__":
    unittest.main()
