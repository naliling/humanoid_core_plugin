"""日程接续与模型审查（v3.0.11）：空档不被节流挡住、生成后有模型复审。"""

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
    _review_prompt,
    parse_segment,
)
from humanoid.state import StateStore

from .fakes import FakeContext, FakeProvider, FrozenClock, RecordingLogger, ScopeStore

TZ = ZoneInfo("Asia/Shanghai")
FIXED_TODAY = "2026-08-22"
FIXED_MOMENT = datetime(2026, 8, 22, 9, 0, tzinfo=TZ)


def cfg(**overrides) -> HumanoidConfig:
    # 3.1.0 起 schedule_quality_gate 默认关；本文件测的就是「生成后复审」这条链路，
    # 显式打开，否则审查根本不会被调用（要测「关掉」的用例自己再传 False）。
    return HumanoidConfig.from_raw(
        {"timezone_city": "北京", "schedule_change_chance": 0,
         "schedule_quality_gate": True, **overrides}
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


class ReviewPromptTest(unittest.TestCase):
    def test_prompt_carries_the_previous_segment(self):
        text = _review_prompt(
            now_text="09:00",
            prev={"start": "08:30", "end": "08:55", "event": "洗杯子", "location": "厨房"},
            slot={"start": "09:00", "end": "09:30", "event": "在沙发上喝东西", "location": "客厅"},
            persona_text="她是个安静的人",
        )
        self.assertIn("【审查这一段】", text)
        self.assertIn("洗杯子", text)
        self.assertIn("在沙发上喝东西", text)
        self.assertIn("30 分钟", text)

    def test_prompt_asks_for_a_reason_and_a_fixed_version(self):
        text = _review_prompt(
            now_text="09:00",
            prev=None,
            slot={"start": "09:00", "end": "09:30", "event": "喝东西", "location": "客厅"},
            persona_text="",
        )
        self.assertIn("ok", text)
        self.assertIn("reason", text)
        self.assertIn("fixed", text)
        # 判据必须在提示词里，否则「准一点」无从谈起。
        for word in ("时间", "地点", "时长", "重复"):
            self.assertIn(word, text)


class ReviewServiceTest(unittest.IsolatedAsyncioTestCase):
    def build(self, config=None, providers=None, gate=None):
        conf = config or cfg()
        self.conf = conf
        tmp = Path(tempfile.mkdtemp()) / "state.json"
        store = ScopeStore(tmp)
        store.load(FIXED_TODAY, conf.cycle_length)
        log = RecordingLogger()
        ctx = FakeContext(chat_providers=list(providers or []))
        resolver = ProviderResolver(ctx, log)
        gateway = LLMGateway(resolver, lambda: self.conf, log, time.monotonic, gate=gate)
        clock = FrozenClock(FIXED_MOMENT)
        service = ScheduleService(
            store.scope,
            lambda: self.conf,
            clock,
            logger=log,
            monotonic=time.monotonic,
        )
        service.set_resolver_gateway(resolver, gateway)
        return service, store, log

    async def test_ok_verdict_keeps_the_segment(self):
        provider = FakeProvider("p", reply=seg_reply(event="改海报", minutes=40))
        service, _store, _log = self.build(cfg(schedule_provider_name="p"), [provider])
        self.assertTrue(await service.ensure_fresh())
        self.assertEqual(provider.calls, 1)
        self.assertEqual(provider.review_calls, 1, "生成完必须审一遍")
        segs = service.segments()
        self.assertEqual(segs[0]["event"], "改海报")
        self.assertEqual(segs[0]["end"], "09:40")

    async def test_fixed_verdict_replaces_the_segment_without_rewriting(self):
        provider = FakeProvider("p", reply=seg_reply(event="在沙发上喝东西", minutes=30))
        provider.review_reply = review_reply(
            ok=False,
            reason="喝东西不该占 30 分钟",
            fixed={"event": "把杯子洗了", "location": "厨房", "minutes": 6},
        )
        service, _store, _log = self.build(cfg(schedule_provider_name="p"), [provider])
        await service.ensure_fresh()
        segs = service.segments()
        self.assertEqual(segs[0]["event"], "把杯子洗了", "用模型改好的那版")
        self.assertEqual(segs[0]["end"], "09:30", "改法也要过清醒下限（#311）")
        self.assertEqual(provider.calls, 1, "同一份回答里给了改法，不该再问一次")

    async def test_reason_verdict_sends_the_reason_into_the_rewrite(self):
        provider = FakeProvider("p", reply=seg_reply(event="在沙发上喝东西", minutes=30))
        provider.review_reply = review_reply(
            ok=False, reason="在沙发上喝东西不该占 30 分钟",
        )
        service, _store, _log = self.build(cfg(schedule_provider_name="p"), [provider])
        await service.ensure_fresh()
        self.assertEqual(provider.calls, 2, "生成一次 + 按原因重写一次")
        prompt = provider.last_kwargs.get("prompt") or ""
        self.assertIn("在沙发上喝东西不该占 30 分钟", prompt, "原因要原样带进重写提示词")

    async def test_review_answer_without_ok_is_ignored(self):
        """模型没给结论（或答了别的东西）时保持原样，别把它当不合格。"""
        provider = FakeProvider("p", reply=seg_reply(event="改海报", minutes=40))
        provider.review_reply = "这一段看起来还行。"
        service, _store, _log = self.build(cfg(schedule_provider_name="p"), [provider])
        await service.ensure_fresh()
        self.assertEqual(provider.calls, 1, "没结论就不该触发重写")
        self.assertEqual(provider.review_calls, 1)
        self.assertEqual(service.segments()[0]["event"], "改海报")

    async def test_review_is_skipped_when_she_keeps_doing_the_same_thing(self):
        provider = FakeProvider("p", reply=seg_reply(event="改海报", minutes=60))
        service, _store, _log = self.build(cfg(schedule_provider_name="p"), [provider])
        await service.ensure_fresh()
        self.assertEqual(provider.review_calls, 1)
        service._clock.advance(minutes=30)
        provider.reply = continue_reply(minutes=60)
        self.assertTrue(await service.ensure_fresh(force=True))
        self.assertEqual(provider.review_calls, 1, "接着做同一件事没有转场可审")

    async def test_review_is_skipped_when_quality_gate_is_off(self):
        provider = FakeProvider("p", reply=seg_reply(event="改海报", minutes=40))
        service, _store, _log = self.build(
            cfg(schedule_provider_name="p", schedule_quality_gate=False), [provider]
        )
        await service.ensure_fresh()
        self.assertEqual(provider.review_calls, 0)


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
        service, provider, _gateway = self.build()
        await service.ensure_fresh()  # 09:00-09:30 倒杯水（清醒下限 30）
        self.assertEqual(provider.calls, 1)
        service._clock.advance(minutes=31)  # 09:31，手上没有任何一段
        self.assertTrue(service.refresh_due())
        provider.reply = seg_reply(event="回消息", minutes=30)
        self.assertTrue(await service.ensure_fresh())
        self.assertEqual(provider.calls, 2, "空档接续不该被 30 分钟间隔挡住")
        segs = service.segments()
        self.assertEqual(segs[1]["start"], "09:30", "从上一段结束处接上，不留窟窿")
        self.assertEqual(segs[1]["event"], "回消息")

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
