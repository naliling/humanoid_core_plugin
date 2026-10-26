"""v2.28.9：日程可打断 / 可续做——可配保鲜期、打断换事、挂起与回来接着做。

用户原话：「日程生成还是有限制，所谓的保鲜期，就不是可以中途打断换一个，
或在当前日程接下去的。我需要加，加逻辑与功能」。

改动前：日程是滚动分段生成，`note_interruption`/`note_event` 只把标记立起来，
是否真的重排还要过 refresh_due/节流/骰子，标记过期就丢；中断之后原来在做的事
没有任何记录，回不去；也没有「明确要求换一件事/接着做」的即时通道；TTL 全写死。

本文件盯五件事：

1. 保鲜期 / 安静间隔 / 冷却 / 「叫住她就打断」全部可配，默认值与原常量一致；
2. `interrupt_now()` 立刻换一件（模型回 continue=true 也不许原地续）；
3. `continue_now()` 延长当前段（不越过后段、不跨午夜、无段返回 False）；
4. 被打断的事写进 `schedule_suspended`，下一次 prompt 带着它，resume 能重建并清空；
5. `status()` 的 `suspended` / `interrupt` 只读字段，以及旧状态（没有该键）兼容。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.config import DEFAULTS, HumanoidConfig
from humanoid.llm import LLMGateway, ProviderResolver
from humanoid.services.schedule import (
    EVENT_TTL_SECONDS,
    INTERRUPT_TTL_SECONDS,
    SEGMENT_MIN_MINUTES,
    SOURCE_LLM,
    SOURCE_TEMPLATE,
    ScheduleService,
    parse_segment,
    segment_prompt,
)
from humanoid.slots import parse_time

from .fakes import FakeContext, FakeProvider, FrozenClock, RecordingLogger, ScopeStore

TZ = ZoneInfo("Asia/Shanghai")
FIXED_MOMENT = datetime(2026, 8, 22, 9, 0, tzinfo=TZ)
FIXED_TODAY = "2026-08-22"
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "_conf_schema.json"

NEW_CONFIG_KEYS = {
    "schedule_interrupt_ttl_minutes": ("int", 60, 10, 720),
    "schedule_event_ttl_minutes": ("int", 30, 5, 360),
    "schedule_continue_minutes": ("int", 45, 10, 240),
    "schedule_pause_ttl_minutes": ("int", 180, 15, 720),
    "schedule_interrupt_quiet_minutes": ("int", 45, 0, 720),
    "schedule_interrupt_cooldown_minutes": ("int", 60, 0, 720),
}


def cfg(**overrides) -> HumanoidConfig:
    # chance 压到 0：概率重估单独测，不让它污染「不该生成」的断言。
    return HumanoidConfig.from_raw(
        {
            "timezone_city": "北京",
            "schedule_change_chance": 0,
            "schedule_provider_name": "p",
            **overrides,
        }
    )


def seg_reply(event: str = "改海报", minutes: int = 60, **extra) -> str:
    payload = {
        "continue": False,
        "event": event,
        "location": "书桌前",
        "emotion": "专注",
        "energy_rate": -0.06,
        "minutes": minutes,
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def continue_reply(minutes: int = 60) -> str:
    return json.dumps({"continue": True, "minutes": minutes}, ensure_ascii=False)


def resume_reply(**extra) -> str:
    payload = {"resume": True}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def build_service(
    config: HumanoidConfig | None = None,
    *,
    reply: str = "{}",
    moment: datetime = FIXED_MOMENT,
    body=None,
):
    """装配一个能真的调到模型（FakeProvider "p"）的 ScheduleService。

    单调时钟与墙钟共用 FrozenClock：保鲜期/冷却/挂起过期都只推进这一台钟，
    测试里不会出现「两个计时源各走各的」。
    """
    conf = config or cfg()
    tmp = Path(tempfile.mkdtemp()) / "state.json"
    store = ScopeStore(tmp)
    store.load(moment.strftime("%Y-%m-%d"), conf.cycle_length)
    log = RecordingLogger()
    provider = FakeProvider("p", reply=reply)
    ctx = FakeContext(chat_providers=[provider])
    resolver = ProviderResolver(ctx, log)
    clock = FrozenClock(moment)
    gateway = LLMGateway(resolver, lambda: conf, log, clock.timestamp)
    service = ScheduleService(
        store.scope,
        lambda: conf,
        clock,
        logger=log,
        monotonic=clock.timestamp,
        body_provider=(lambda: dict(body)) if body is not None else None,
    )
    service.set_resolver_gateway(resolver, gateway)
    return service, store, clock, provider, log


def seed_day(store, slots, **extra) -> None:
    """把「今天已经过出来的段」直接写进 state（等价于旧版本留下的日程）。"""
    store.scope.update_self(today_date=FIXED_TODAY, daily_schedule=[dict(s) for s in slots], **extra)


# ─────────────────────────────────────────────────────────────
# A. 可配保鲜期：配置键本身
# ─────────────────────────────────────────────────────────────


class ConfigKeysTest(unittest.TestCase):
    def test_defaults_keep_the_old_hardcoded_behaviour(self):
        """默认值必须等于原来写死的常量——升级当天行为不变。"""
        conf = DEFAULTS
        self.assertEqual(conf.schedule_interrupt_ttl_minutes * 60, INTERRUPT_TTL_SECONDS)
        self.assertEqual(conf.schedule_event_ttl_minutes * 60, EVENT_TTL_SECONDS)
        self.assertEqual(conf.schedule_continue_minutes, 45)
        self.assertEqual(conf.schedule_pause_ttl_minutes, 180)
        self.assertEqual(conf.schedule_interrupt_quiet_minutes, 45)
        self.assertEqual(conf.schedule_interrupt_cooldown_minutes, 60)
        self.assertTrue(conf.schedule_interrupt_on_call)

    def test_out_of_range_and_junk_values_are_clamped(self):
        conf = HumanoidConfig.from_raw(
            {
                "schedule_interrupt_ttl_minutes": 1,       # 下限 10
                "schedule_event_ttl_minutes": 99999,       # 上限 360
                "schedule_continue_minutes": "abc",        # 坏值 → 默认 45
                "schedule_pause_ttl_minutes": -5,          # 下限 15
                "schedule_interrupt_quiet_minutes": 0,     # 0 是合法值（不要求安静）
                "schedule_interrupt_cooldown_minutes": 999,  # 上限 720
                "schedule_interrupt_on_call": "false",
            }
        )
        self.assertEqual(conf.schedule_interrupt_ttl_minutes, 10)
        self.assertEqual(conf.schedule_event_ttl_minutes, 360)
        self.assertEqual(conf.schedule_continue_minutes, 45)
        self.assertEqual(conf.schedule_pause_ttl_minutes, 15)
        self.assertEqual(conf.schedule_interrupt_quiet_minutes, 0)
        self.assertEqual(conf.schedule_interrupt_cooldown_minutes, 720)
        self.assertFalse(conf.schedule_interrupt_on_call)

    def test_schema_exposes_every_new_key_with_matching_defaults(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        for key, (type_, default, minimum, maximum) in NEW_CONFIG_KEYS.items():
            with self.subTest(key=key):
                meta = schema[key]
                self.assertEqual(meta["type"], type_)
                self.assertEqual(meta["default"], default)
                self.assertEqual(meta["minimum"], minimum)
                self.assertEqual(meta["maximum"], maximum)
                self.assertIn("slider", meta)
                self.assertIn("hint", meta)
                self.assertTrue(meta["description"])
        on_call = schema["schedule_interrupt_on_call"]
        self.assertEqual(on_call["type"], "bool")
        self.assertIs(on_call["default"], True)


class InterruptWindowConfigTest(unittest.TestCase):
    """来访的三道比例尺读配置：保鲜期、安静间隔、冷却。"""

    def test_interrupt_ttl_is_configurable(self):
        service, store, clock, _, _ = build_service(
            cfg(schedule_interrupt_ttl_minutes=10)
        )
        service.note_interruption()
        self.assertTrue(service._fresh_interruption(clear=False))
        clock.advance(minutes=11)
        self.assertFalse(service._fresh_interruption(clear=False), "超过配置的保鲜期就不该还算数")
        self.assertFalse(service._fresh_interruption(), "过期要顺手清掉")
        self.assertEqual(service._interrupt_at, 0.0)

    def test_event_ttl_is_configurable(self):
        service, _, clock, _, _ = build_service(cfg(schedule_event_ttl_minutes=5))
        self.assertTrue(service.note_event(kind="很敷衍", seed=["r"]))
        self.assertTrue(service.pending_event())
        clock.advance(minutes=6)
        self.assertEqual(service.pending_event(), {})

    def test_quiet_zero_means_every_private_message_counts(self):
        service, _, clock, _, _ = build_service(
            cfg(schedule_interrupt_quiet_minutes=0, schedule_interrupt_cooldown_minutes=0)
        )
        service.note_interruption()
        self.assertTrue(service._consume_interruption())
        clock.advance(minutes=1)
        service.note_interruption()
        self.assertTrue(service._consume_interruption(), "quiet=0：任何一条私聊都算来访")

    def test_default_quiet_gap_still_blocks_a_burst(self):
        service, _, clock, _, _ = build_service()
        service.note_interruption()
        self.assertTrue(service._consume_interruption())
        clock.advance(minutes=10)
        service.note_interruption()
        self.assertFalse(service._consume_interruption(), "消息没断过，不该反复算来访")

    def test_on_call_breaks_the_quiet_gap(self):
        service, _, clock, _, _ = build_service(
            cfg(schedule_interrupt_quiet_minutes=45, schedule_interrupt_cooldown_minutes=0)
        )
        service.note_interruption(text="今天天气不错")
        self.assertTrue(service._consume_interruption())
        clock.advance(minutes=5)  # 安静间隔远没到
        service.note_interruption(text="在吗")
        self.assertTrue(service._consume_interruption(), "「在吗」是明确叫住她，破例算来访")

    def test_on_call_still_obeys_the_cooldown(self):
        service, _, clock, _, _ = build_service(
            cfg(schedule_interrupt_quiet_minutes=45, schedule_interrupt_cooldown_minutes=60)
        )
        service.note_interruption(text="随便聊聊")
        self.assertTrue(service._consume_interruption())
        clock.advance(minutes=5)
        service.note_interruption(text="你在干嘛")
        self.assertFalse(service._consume_interruption(), "叫住她也得过冷却")

    def test_on_call_switch_off_keeps_the_quiet_gap(self):
        service, _, clock, _, _ = build_service(
            cfg(
                schedule_interrupt_on_call=False,
                schedule_interrupt_quiet_minutes=45,
                schedule_interrupt_cooldown_minutes=0,
            )
        )
        service.note_interruption(text="今天天气不错")
        self.assertTrue(service._consume_interruption())
        clock.advance(minutes=5)
        service.note_interruption(text="在吗")
        self.assertFalse(service._consume_interruption(), "关掉开关后「在吗」不再破例")

    def test_group_messages_are_never_a_visit(self):
        service, _, _, _, _ = build_service(
            cfg(schedule_interrupt_quiet_minutes=0, schedule_interrupt_cooldown_minutes=0)
        )
        service.note_interruption(is_group=True, text="在吗")
        self.assertFalse(service._consume_interruption(), "群聊永远不算「有人来找她」")
        self.assertIsNone(service._last_message_at, "群消息不该刷新私聊的安静刻度")


# ─────────────────────────────────────────────────────────────
# B. interrupt_now：立刻换一件事
# ─────────────────────────────────────────────────────────────


class InterruptNowTest(unittest.IsolatedAsyncioTestCase):
    async def test_interrupt_switches_to_another_thing_even_if_model_says_continue(self):
        service, store, clock, provider, _ = build_service(reply=seg_reply("改海报", 90))
        await service.ensure_fresh()
        self.assertEqual(service.current_activity()["name"], "改海报")
        clock.advance(minutes=20)  # 09:20，这一段还没做完
        provider.reply = continue_reply(60)
        self.assertTrue(service.interrupt_now())
        await service._task
        self.assertEqual(provider.calls, 2)
        prompt = provider.last_kwargs.get("prompt") or ""
        self.assertIn("换一件事", prompt, "打断的提示词必须明确要换事")
        self.assertIn("不能 continue=true", prompt)
        self.assertNotEqual(
            service.current_activity()["name"], "改海报", "模型说 continue 也不许原地续"
        )
        self.assertEqual(service.source, SOURCE_TEMPLATE, "拿不到新事件就按身体现算一段，不原地续")
        self.assertIn("换一件事", service.last_error)
        # 被截断的那件事要挂起来，之后可以回去接着做。
        suspended = service.suspended_info()
        self.assertEqual(suspended["event"], "改海报")
        self.assertEqual(suspended["remaining_minutes"], 75)

    async def test_interrupt_accepts_the_model_s_new_event(self):
        service, _, clock, provider, _ = build_service(reply=seg_reply("改海报", 90))
        await service.ensure_fresh()
        clock.advance(minutes=20)
        provider.reply = seg_reply("下楼取快递", 25)
        self.assertTrue(service.interrupt_now())
        await service._task
        self.assertEqual(service.source, SOURCE_LLM)
        self.assertEqual(service.current_activity()["name"], "下楼取快递")
        segs = service.segments()
        self.assertEqual(segs[-1]["start"], "09:15", "新的一段从现在就接上")
        self.assertEqual(segs[0]["end"], "09:15", "原来那件事被截断到新段起点")
        self.assertEqual(service.suspended_info()["event"], "改海报")

    async def test_interrupt_returns_false_when_a_generation_is_already_running(self):
        service, _, _, provider, _ = build_service(reply=seg_reply("改海报"), )
        provider.delay = 0.15
        task = __import__("asyncio").create_task(service.ensure_fresh(force=True))
        await __import__("asyncio").sleep(0.03)
        self.assertFalse(service.interrupt_now(), "已经有生成在跑：不要排队重复")
        await task

    async def test_interrupt_returns_false_when_a_background_task_is_running(self):
        import asyncio

        service, _, _, _, _ = build_service(reply=seg_reply("改海报"))
        service._task = asyncio.create_task(asyncio.sleep(0.05))
        self.assertFalse(service.interrupt_now())
        await service._task

    async def test_interrupt_works_with_no_active_segment(self):
        service, _, _, provider, _ = build_service(reply=seg_reply("整理个案笔记", 40))
        self.assertEqual(service.segments(), [])
        self.assertTrue(service.interrupt_now(), "没有可打断的段时=直接决定下一段")
        await service._task
        self.assertEqual(provider.calls, 1)
        self.assertEqual(service.current_activity()["name"], "整理个案笔记")

    async def test_interrupt_rejects_a_same_event_reply(self):
        service, _, clock, provider, _ = build_service(reply=seg_reply("改海报", 90))
        await service.ensure_fresh()
        clock.advance(minutes=20)
        provider.reply = seg_reply("改海报", 30)  # continue=false，但排回了同一件事
        self.assertTrue(service.interrupt_now())
        await service._task
        self.assertNotEqual(service.current_activity()["name"], "改海报")
        self.assertIn("换一件事", service.last_error)


# ─────────────────────────────────────────────────────────────
# C. continue_now：接着做下去（不调模型）
# ─────────────────────────────────────────────────────────────


class ContinueNowTest(unittest.TestCase):
    def _service(self, slots, **overrides):
        moment = overrides.pop("moment", datetime(2026, 8, 22, 9, 30, tzinfo=TZ))
        service, store, clock, provider, _ = build_service(
            cfg(**overrides), moment=moment
        )
        seed_day(store, slots)
        return service, store, clock, provider

    def test_extends_the_current_segment_end(self):
        service, store, _, _ = self._service([{
            "start": "09:00", "end": "10:00", "event": "改海报",
            "location": "书桌", "emotion": "专注", "energy_rate": -0.06,
        }])
        self.assertTrue(service.continue_now())
        seg = service.segments()[0]
        self.assertEqual(seg["end"], "10:45", "默认延长 45 分钟")
        self.assertEqual(seg["energy_rate"], -0.06, "energy_rate 要沿用这一段")
        self.assertEqual(service.active_segment()["end"], "10:45")
        self.assertEqual(service.current_activity()["expected_end"], "10:45")
        self.assertEqual(service.current_activity()["remaining_minutes"], 75)
        self.assertIn("10:45", service.status()["active"], "status 要立刻反映新的 end")
        self.assertEqual(service.status()["activity"]["expected_end"], "10:45")

    def test_extension_amount_is_configurable(self):
        service, _, _, _ = self._service(
            [{"start": "09:00", "end": "10:00", "event": "改海报",
              "location": "书桌", "emotion": "专注", "energy_rate": -0.06}],
            schedule_continue_minutes=20,
        )
        self.assertTrue(service.continue_now())
        self.assertEqual(service.segments()[0]["end"], "10:20")

    def test_never_overlaps_the_next_segment(self):
        service, store, _, _ = self._service([
            {"start": "09:00", "end": "10:00", "event": "A", "location": "", "emotion": ""},
            {"start": "10:20", "end": "11:00", "event": "B", "location": "", "emotion": ""},
        ])
        self.assertTrue(service.continue_now())
        stored = store.get("daily_schedule")
        self.assertEqual(stored[0]["end"], "10:20", "有后续段时最多延长到它的 start")
        self.assertEqual(stored[1]["start"], "10:20", "后续段不被吞掉")

    def test_returns_false_when_the_next_segment_is_adjacent(self):
        service, _, _, _ = self._service([
            {"start": "09:00", "end": "10:00", "event": "A", "location": "", "emotion": ""},
            {"start": "10:00", "end": "11:00", "event": "B", "location": "", "emotion": ""},
        ])
        self.assertFalse(service.continue_now(), "后面紧接着一整段，没有可延长的时间")

    def test_returns_false_without_a_current_segment(self):
        service, _, _, _ = self._service([])
        self.assertFalse(service.continue_now())

    def test_refuses_to_cross_midnight(self):
        service, _, _, _ = self._service(
            [{"start": "23:00", "end": "23:50", "event": "看剧",
              "location": "客厅", "emotion": "放松"}],
            moment=datetime(2026, 8, 22, 23, 40, tzinfo=TZ),
        )
        self.assertFalse(service.continue_now(), "延长会超过 24:00：不延长")
        self.assertEqual(service.segments()[0]["end"], "23:50")

    def test_extension_that_lands_exactly_on_midnight_is_allowed(self):
        service, _, _, _ = self._service(
            [{"start": "22:50", "end": "23:15", "event": "看剧",
              "location": "客厅", "emotion": "放松"}],
            moment=datetime(2026, 8, 22, 23, 0, tzinfo=TZ),
            schedule_continue_minutes=45,
        )
        self.assertTrue(service.continue_now())
        self.assertEqual(service.segments()[0]["end"], "24:00")

    def test_does_not_call_the_model(self):
        service, _, _, provider = self._service([{
            "start": "09:00", "end": "10:00", "event": "改海报",
            "location": "书桌", "emotion": "专注"}])
        self.assertTrue(service.continue_now())
        self.assertEqual(provider.calls, 0, "继续是纯本地延长，不该调模型")


# ─────────────────────────────────────────────────────────────
# D. suspended：被打断之前她在做的那件事
# ─────────────────────────────────────────────────────────────


class SuspendedStateTest(unittest.TestCase):
    def _service(self, slots, **overrides):
        moment = overrides.pop("moment", datetime(2026, 8, 22, 9, 30, tzinfo=TZ))
        service, store, clock, provider, _ = build_service(cfg(**overrides), moment=moment)
        seed_day(store, slots)
        return service, store, clock

    def test_interruption_records_the_truncated_segment(self):
        service, store, clock = self._service([{
            "start": "09:00", "end": "10:30", "event": "改海报",
            "location": "书桌", "emotion": "专注", "energy_rate": -0.06,
            "ongoing": "还差 4 张",
        }])
        slot = parse_segment({"event": "下楼取快递", "minutes": 30}, now_minute=9 * 60 + 30, step=15)
        service._append_segment(slot, FIXED_TODAY)
        suspended = store.get("schedule_suspended")
        self.assertIsInstance(suspended, dict)
        self.assertEqual(suspended["event"], "改海报")
        self.assertEqual(suspended["location"], "书桌")
        self.assertEqual(suspended["emotion"], "专注")
        self.assertEqual(suspended["ongoing"], "还差 4 张")
        self.assertEqual(suspended["remaining_minutes"], 60)
        self.assertEqual(suspended["at"], clock.timestamp())
        self.assertEqual(suspended["ttl_minutes"], 180)
        self.assertEqual(service.segments()[0]["end"], "09:30", "被截断的段不能留重叠")

    def test_same_event_continuation_does_not_suspend_anything(self):
        service, store, _ = self._service([{
            "start": "09:00", "end": "10:30", "event": "改海报",
            "location": "书桌", "emotion": "专注", "energy_rate": -0.06,
        }])
        slot = parse_segment({"continue": True, "minutes": 45}, now_minute=9 * 60 + 30,
                             step=15, prev={"event": "改海报", "location": "书桌"})
        service._append_segment(slot, FIXED_TODAY)
        self.assertFalse(store.get("schedule_suspended"), "接着做同一件事不算被打断")

    def test_suspended_expires_after_pause_ttl(self):
        service, store, clock = self._service(
            [{"start": "09:00", "end": "10:30", "event": "改海报",
              "location": "书桌", "emotion": "专注"}],
            schedule_pause_ttl_minutes=15,
        )
        slot = parse_segment({"event": "下楼取快递", "minutes": 30}, now_minute=9 * 60 + 30, step=15)
        service._append_segment(slot, FIXED_TODAY)
        self.assertEqual(service.suspended_info()["event"], "改海报")
        clock.advance(minutes=16)
        self.assertEqual(service.suspended_info(), {}, "超过保留期就作废")
        self.assertIsNone(store.get("schedule_suspended"), "过期要顺手清掉")

    def test_old_state_without_the_key_is_fine(self):
        service, store, _ = self._service([{
            "start": "09:00", "end": "10:00", "event": "改海报",
            "location": "书桌", "emotion": "专注"}])
        self.assertEqual(service.suspended_info(), {})
        status = service.status()
        self.assertEqual(status["suspended"], {})
        self.assertFalse(status["interrupt"]["pending"])
        # 旧状态照常能追加新段（不因为缺这个键炸）。
        slot = parse_segment({"event": "回消息", "minutes": 20}, now_minute=9 * 60 + 30, step=15)
        service._append_segment(slot, FIXED_TODAY)
        self.assertEqual(service.suspended_info()["event"], "改海报")


class SuspendedPromptTest(unittest.TestCase):
    def test_prompt_carries_the_interrupted_thing(self):
        suspended = {
            "event": "改海报", "location": "书桌", "emotion": "专注",
            "ongoing": "还差 4 张", "wants": False, "remaining_minutes": 60,
            "at": 0.0, "ttl_minutes": 180,
        }
        prompt = segment_prompt(
            cfg(), now_text="09:30", weekday="六", suspended=suspended, force_switch=False
        )
        self.assertIn("被打断之前她在做", prompt)
        self.assertIn("「改海报」", prompt)
        self.assertIn("还剩 60 分钟", prompt)
        self.assertIn('"resume": true', prompt)

    def test_prompt_unchanged_without_suspended(self):
        prompt = segment_prompt(cfg(), now_text="09:30", weekday="六")
        self.assertNotIn("被打断之前", prompt)
        self.assertNotIn('"resume"', prompt)

    def test_force_switch_prompt_forbids_continue(self):
        prompt = segment_prompt(cfg(), now_text="09:30", weekday="六", force_switch=True)
        self.assertIn("换一件事", prompt)
        self.assertIn("不能 continue=true", prompt)
        plain = segment_prompt(cfg(), now_text="09:30", weekday="六")
        self.assertNotIn("不能 continue=true", plain, "普通生成不该禁止接着做")

    def test_prev_line_reports_expected_end_and_remaining(self):
        prev = {"start": "09:00", "end": "10:30", "event": "改海报", "location": "书桌"}
        prompt = segment_prompt(cfg(), now_text="09:30", weekday="六",
                                prev=prev, elapsed_minutes=30)
        self.assertIn("这一段预计到 10:30（还剩 60 分钟）", prompt)
        self.assertIn("已经 30 分钟", prompt)


class ResumeParseTest(unittest.TestCase):
    def _suspended(self, **extra):
        data = {
            "event": "改海报", "location": "书桌", "emotion": "专注",
            "ongoing": "还差 4 张", "wants": False, "remaining_minutes": 60,
            "at": 0.0, "ttl_minutes": 180,
        }
        data.update(extra)
        return data

    def test_resume_rebuilds_the_interrupted_thing(self):
        slot = parse_segment(
            {"resume": True}, now_minute=9 * 60 + 30, step=15, suspended=self._suspended()
        )
        self.assertEqual(slot["event"], "改海报")
        self.assertEqual(slot["start"], "09:30", "start=现在（向下对齐粒度）")
        self.assertEqual(slot["end"], "10:30", "minutes=剩下的 60 分钟")
        self.assertEqual(slot["location"], "书桌")
        self.assertEqual(slot["emotion"], "专注")
        self.assertEqual(slot["ongoing"], "还差 4 张")

    def test_resume_minutes_have_a_floor_and_a_cap(self):
        short = parse_segment({"resume": True}, now_minute=600, step=15,
                              suspended=self._suspended(remaining_minutes=3))
        self.assertGreaterEqual(parse_time(short["end"]) - parse_time(short["start"]),
                                SEGMENT_MIN_MINUTES)
        huge = parse_segment({"resume": True}, now_minute=13 * 60, step=15,
                             suspended=self._suspended(remaining_minutes=9999))
        self.assertLessEqual(parse_time(huge["end"]) - parse_time(huge["start"]), 180)

    def test_resume_wins_over_continue(self):
        slot = parse_segment(
            {"continue": True, "resume": True, "minutes": 10},
            now_minute=600,
            step=15,
            prev={"event": "别的事", "location": "客厅"},
            suspended=self._suspended(),
        )
        self.assertEqual(slot["event"], "改海报", "两个标记同时出现时以 resume 优先")

    def test_resume_without_suspended_is_not_a_segment(self):
        self.assertIsNone(parse_segment({"resume": True}, now_minute=600, step=15))


class ResumeFlowTest(unittest.IsolatedAsyncioTestCase):
    async def test_generation_resumes_and_clears_the_suspended_item(self):
        service, store, clock, provider, _ = build_service(
            reply=resume_reply(), moment=datetime(2026, 8, 22, 9, 20, tzinfo=TZ)
        )
        seed_day(store, [{
            "start": "09:00", "end": "10:30", "event": "改海报",
            "location": "书桌", "emotion": "专注", "energy_rate": -0.06,
        }])
        store.scope.update_self(schedule_suspended={
            "event": "整理个案笔记", "location": "书桌前", "emotion": "有点累",
            "ongoing": "还差 4 份", "wants": False, "remaining_minutes": 60,
            "at": clock.timestamp(), "ttl_minutes": 180,
        })
        self.assertTrue(await service.ensure_fresh(force=True))
        prompt = provider.last_kwargs.get("prompt") or ""
        self.assertIn("被打断之前她在做", prompt, "下一次生成要把挂起的事递给模型")
        segs = service.segments()
        self.assertEqual(segs[-1]["event"], "整理个案笔记", "resume=true 用被挂起的事重建")
        self.assertEqual(segs[-1]["start"], "09:15")
        self.assertEqual(segs[-1]["end"], "10:15")
        self.assertEqual(segs[-1]["ongoing"], "还差 4 份")
        self.assertEqual(service.suspended_info(), {}, "resume 成功后挂起要清空")
        self.assertIsNone(store.get("schedule_suspended"))

    async def test_model_scheduling_the_same_event_also_clears_it(self):
        service, store, clock, provider, _ = build_service(
            reply=seg_reply("整理个案笔记", 40),
            moment=datetime(2026, 8, 22, 9, 20, tzinfo=TZ),
        )
        seed_day(store, [{
            "start": "09:00", "end": "10:30", "event": "改海报",
            "location": "书桌", "emotion": "专注", "energy_rate": -0.06,
        }])
        store.scope.update_self(schedule_suspended={
            "event": "整理个案笔记", "location": "书桌前", "emotion": "有点累",
            "ongoing": "", "wants": False, "remaining_minutes": 30,
            "at": clock.timestamp(), "ttl_minutes": 180,
        })
        self.assertTrue(await service.ensure_fresh(force=True))
        self.assertEqual(service.current_activity()["name"], "整理个案笔记")
        self.assertEqual(service.suspended_info(), {}, "已经又排上同一件事：挂起结束")


# ─────────────────────────────────────────────────────────────
# E. status()：新字段与只读性
# ─────────────────────────────────────────────────────────────


class StatusTest(unittest.TestCase):
    def test_status_has_suspended_and_interrupt_snapshots(self):
        service, store, clock, _, _ = build_service(
            cfg(schedule_interrupt_quiet_minutes=0, schedule_interrupt_cooldown_minutes=0)
        )
        seed_day(store, [{
            "start": "09:00", "end": "10:00", "event": "改海报",
            "location": "书桌", "emotion": "专注"}])
        service.note_interruption()
        status = service.status()
        self.assertEqual(status["suspended"], {})
        interrupt = status["interrupt"]
        for key in ("pending", "ttl_left", "cooldown_left", "quiet_left"):
            self.assertIn(key, interrupt)
        self.assertTrue(interrupt["pending"])
        self.assertGreater(interrupt["ttl_left"], 0)
        # 只读：看 status 不能把标记消费掉。
        self.assertTrue(service._fresh_interruption(clear=False))

    def test_status_reports_suspended_item(self):
        service, store, clock, _, _ = build_service(
            cfg(), moment=datetime(2026, 8, 22, 9, 30, tzinfo=TZ)
        )
        seed_day(store, [{
            "start": "09:00", "end": "10:30", "event": "改海报",
            "location": "书桌", "emotion": "专注"}])
        slot = parse_segment({"event": "下楼取快递", "minutes": 30}, now_minute=9 * 60 + 30, step=15)
        service._append_segment(slot, FIXED_TODAY)
        suspended = service.status()["suspended"]
        self.assertEqual(suspended["event"], "改海报")
        self.assertEqual(suspended["remaining_minutes"], 60)
        self.assertIn("ttl_left_minutes", suspended)
        self.assertEqual(suspended["ttl_left_minutes"], 180)


if __name__ == "__main__":
    unittest.main()
