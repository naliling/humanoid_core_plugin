"""日程接续（v3.0.11）：空档不被节流挡住；模型自审层已在 v3.1.28 删除。"""

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
    """空档接续：她手上那段过完之后，立刻排下一段，不被上一段留下的间隔挡住。"""

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
        gateway = LLMGateway(resolver, lambda: self.conf, log, time.monotonic)
        clock = FrozenClock(FIXED_MOMENT)
        service = ScheduleService(
            store.scope, lambda: self.conf, clock, logger=log, monotonic=time.monotonic
        )
        service.set_resolver_gateway(resolver, gateway)
        return service, provider, gateway

    async def test_gap_is_filled_right_away_despite_the_interval(self):
        service, provider, _gateway = self.build(schedule_time_granularity="15min")
        await service.ensure_fresh()  # 09:00-09:30 倒杯水（清醒下限 30）
        self.assertEqual(provider.calls, 1)
        service._clock.advance(minutes=31)  # 09:31，手上没有任何一段
        self.assertTrue(service.refresh_due())
        provider.reply = seg_reply(event="整理书架", minutes=30)
        self.assertTrue(await service.ensure_fresh())
        self.assertEqual(provider.calls, 2, "空档接续不该被 30 分钟间隔挡住")
        segs = service.segments()
        self.assertEqual(segs[1]["start"], "09:30", "从上一段结束处接上，不留窟窿")
        self.assertEqual(segs[1]["event"], "整理书架")

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
        self.assertEqual(slot["end"], "09:30", "6 分钟被抬到清醒下限 30（#311）")


if __name__ == "__main__":
    unittest.main()
