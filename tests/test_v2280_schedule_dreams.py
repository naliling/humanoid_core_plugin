"""v2.28.0：凌晨日程、梦、困意越线（C1）。

用户原话：
* 「日程不包括凌晨，夜猫子凌晨活动被砍」——凌晨不是天然的睡觉时段；
* 「白天/凌晨我觉得要加上梦境；凌晨的或许可以生成两个，白天睡觉的可以生成三个」；
* 「困意 86 还在厨房煮饭」——困意越线（80）时不该再排要精神的事，白天兜底要能小睡。

这里盯三件事：prompt 里有没有那几条规则、`dreams` 有没有全链路（parse → carry →
migrate → day_phrases/status → 契约），以及本地兜底在白天高困意时真的会安排小睡。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.config import HumanoidConfig
from humanoid.llm import LLMGateway, ProviderResolver
from humanoid.persona import Persona
from humanoid.services.schedule import (
    CARRY_KEY,
    CHANGE_TRIGGER_SLEEP_PRESSURE,
    ScheduleService,
    day_phrases,
    dynamic_segment,
    parse_segment,
    segment_prompt,
)
from humanoid.slots import is_sleep_event, parse_time

from .fakes import FakeContext, FakeProvider, FrozenClock, RecordingLogger, ScopeStore

TZ = ZoneInfo("Asia/Shanghai")
MOMENT = datetime(2026, 8, 22, 9, 0, tzinfo=TZ)
TODAY = "2026-08-22"
NIGHT = datetime(2026, 8, 22, 23, 0, tzinfo=TZ)


def cfg(**kw) -> HumanoidConfig:
    return HumanoidConfig.from_raw(
        {"timezone_city": "北京", "schedule_change_chance": 0, **kw}
    )


def build_service(
    config: HumanoidConfig | None = None,
    *,
    moment: datetime = MOMENT,
    body=None,
    reply: str = "{}",
):
    conf = config or cfg()
    tmp = Path(tempfile.mkdtemp()) / "state.json"
    store = ScopeStore(tmp)
    store.load(moment.strftime("%Y-%m-%d"), conf.cycle_length)
    log = RecordingLogger()
    provider = FakeProvider("p", reply=reply)
    ctx = FakeContext(chat_providers=[provider])
    resolver = ProviderResolver(ctx, log)
    gateway = LLMGateway(resolver, lambda: conf, log)
    clock = FrozenClock(moment)
    service = ScheduleService(
        store.scope, lambda: conf, clock, logger=log,
        body_provider=(lambda: body) if body is not None else None,
    )
    service.set_resolver_gateway(resolver, gateway)
    return service, store, clock, provider


def seg_reply(event: str = "工作", minutes: int = 60, **extra) -> str:
    payload = {"continue": False, "event": event, "location": "书桌前",
               "emotion": "平常", "energy_rate": -0.05, "minutes": minutes}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


class SegmentPromptRulesTest(unittest.TestCase):
    def build(self, **kw) -> str:
        return segment_prompt(cfg(), now_text="03:00", weekday="六", **kw)

    def test_midnight_is_a_full_day(self):
        prompt = self.build()
        self.assertIn("00:00-06:00 也是完整的一天", prompt)
        self.assertIn("夜猫子", prompt)
        self.assertIn("别因为过了零点就默认她在睡", prompt)
        self.assertIn("早起型的人凌晨在睡是正常的", prompt)

    def test_diligence_is_not_the_default(self):
        prompt = self.build()
        self.assertIn("勤快不是默认值", prompt)
        self.assertIn("别把一天排满工作、学习、家务", prompt)

    def test_lie_in_rule_only_after_sleep(self):
        after_sleep = self.build(prev_sleep=True)
        self.assertIn("上一段是睡眠", after_sleep)
        self.assertIn("赖床", after_sleep)
        self.assertNotIn("上一段是睡眠", self.build(prev_sleep=False))

    def test_pressure_rule_from_body(self):
        self.assertIn(
            "困意已经 80+/100",
            self.build(body={"sleep_pressure": 85.0}),
        )
        self.assertNotIn(
            "困意已经 80+/100",
            self.build(body={"sleep_pressure": 79.0}),
            "困意没越线却先劝她睡",
        )
        # 调用方显式给 flag 时（`_due` 因困意触发）也要生效。
        self.assertIn("困意已经 80+/100", self.build(body_pressure=True))
        self.assertNotIn("困意已经 80+/100", self.build(body_pressure=False))

    def test_dreams_output_rule(self):
        prompt = self.build()
        self.assertIn('"dreams"', prompt)
        self.assertIn("2 条梦", prompt)
        self.assertIn("3 条", prompt)
        self.assertIn("不是睡眠就不要写 dreams", prompt)

    def test_persona_traits_block(self):
        persona = Persona("小明", "她是个夜猫子，白天补觉，平时很懒散。", "会话")
        prompt = self.build(persona=persona)
        self.assertIn("【她这个人的作息与劲头】", prompt)
        self.assertIn("- 夜猫子：", prompt)
        self.assertIn("不算勤快", prompt)
        self.assertNotIn("勤快自律", prompt, "勤快与懒散同时进了线索块")

    def test_neutral_persona_has_no_traits_block(self):
        persona = Persona("小明", "她喜欢花，喜欢安静地听歌。", "会话")
        self.assertNotIn("【她这个人的作息与劲头】", self.build(persona=persona))
        self.assertNotIn("【她这个人的作息与劲头】", self.build())


class ParseSegmentDreamsTest(unittest.TestCase):
    def test_sleep_keeps_two_or_three_clean_dreams(self):
        slot = parse_segment(
            {
                "event": "睡眠",
                "minutes": 480,
                "dreams": ["梦到一片海", "", "一片海", "老朋友", 3, None, "x" * 40],
            },
            now_minute=23 * 60,
            step=15,
        )
        dreams = slot["dreams"]
        self.assertEqual(dreams[0], "一片海", "句首的「梦到」该被剥掉（社交层自己会加）")
        self.assertIn("老朋友", dreams)
        self.assertEqual(len(dreams), 3, f"最多 3 条：{dreams}")
        self.assertEqual(len(set(dreams)), len(dreams), "梦重复了")
        for item in dreams:
            self.assertLessEqual(len(item), 20, item)
        for item in dreams:
            self.assertIsInstance(item, str)

    def test_non_sleep_drops_dreams(self):
        slot = parse_segment(
            {"event": "写方案", "minutes": 60, "dreams": ["一片海"]},
            now_minute=10 * 60,
        )
        self.assertNotIn("dreams", slot, "清醒段不该带梦")

    def test_continue_sleep_inherits_previous_dreams(self):
        slot = parse_segment(
            {"continue": True, "minutes": 60},
            now_minute=23 * 60 + 60,
            step=15,
            prev={"event": "睡眠", "location": "卧室", "energy_rate": 0.15,
                  "dreams": ["一片海"]},
        )
        self.assertTrue(is_sleep_event(slot["event"]))
        self.assertEqual(slot["dreams"], ["一片海"], "接着睡把梦洗掉了")

    def test_dirty_dreams_do_not_raise(self):
        for raw in ({"x": 1}, 42, None, [{"text": "梦"}], "  "):
            with self.subTest(raw=raw):
                slot = parse_segment(
                    {"event": "睡眠", "minutes": 60, "dreams": raw},
                    now_minute=23 * 60,
                )
                self.assertIsInstance(slot["dreams"], list)

    def test_single_string_dream_is_tolerated(self):
        slot = parse_segment(
            {"event": "睡眠", "minutes": 60, "dreams": "梦到海"},
            now_minute=23 * 60,
        )
        self.assertEqual(slot["dreams"], ["海"], "单条字符串的「梦到」前缀也该剥掉")


class DreamPlumbingTest(unittest.TestCase):
    def test_carry_keeps_dreams_across_midnight(self):
        service, store, clock, _ = build_service(moment=NIGHT)
        slot = parse_segment(
            {"event": "睡眠", "minutes": 480, "dreams": ["一片海"]},
            now_minute=23 * 60,
            step=15,
        )
        service._append_segment(slot, TODAY)
        carry = store.get(CARRY_KEY)
        self.assertEqual(carry.get("dreams"), ["一片海"], "跨夜结转把梦丢了")

        clock.advance(minutes=90)  # 次日 00:30，触发 _migrate_new_day
        segs = service.segments()
        self.assertEqual(segs[0]["start"], "00:00")
        self.assertEqual(segs[0]["dreams"], ["一片海"], "跨天后的第一段丢了梦")

    def test_day_phrases_returns_latest_sleep_dreams(self):
        slots = [
            {"start": "00:00", "end": "07:00", "event": "睡眠", "dreams": ["一片海"]},
            {"start": "07:00", "end": "24:00", "event": "工作"},
        ]
        out = day_phrases(slots, 10 * 60)
        self.assertEqual(out["dreams"], ["一片海"])
        self.assertEqual(out["doing"], "工作")

    def test_latest_sleep_wins_even_without_dreams(self):
        slots = [
            {"start": "00:00", "end": "07:00", "event": "睡眠", "dreams": ["一片海"]},
            {"start": "13:00", "end": "14:00", "event": "午睡"},
            {"start": "14:00", "end": "24:00", "event": "工作"},
        ]
        self.assertEqual(day_phrases(slots, 15 * 60)["dreams"], [])

    def test_old_slots_without_dreams_are_safe(self):
        service, store, _, _ = build_service()
        store.set("today_date", TODAY)
        store.set("daily_schedule", [
            {"start": "00:00", "end": "07:00", "event": "睡眠",
             "location": "卧室", "emotion": "沉睡", "energy_rate": 0.15},
        ])
        self.assertEqual(service.last_dreams(), [])
        self.assertEqual(service.status()["dreams"], [])
        self.assertEqual(day_phrases(store.get("daily_schedule"), 8 * 60)["dreams"], [])

    def test_old_carry_without_dreams_is_safe(self):
        service, store, _, _ = build_service()
        store.set("today_date", TODAY)
        store.set(CARRY_KEY, {"end": "07:00", "event": "睡眠", "location": "卧室",
                              "emotion": "沉睡", "energy_rate": 0.15})
        carry = service._carry_slot()
        self.assertIsNotNone(carry)
        self.assertEqual(carry["dreams"], [])

    def test_status_reports_dreams(self):
        service, store, _, _ = build_service()
        store.set("today_date", TODAY)
        store.set("daily_schedule", [
            {"start": "00:00", "end": "07:00", "event": "睡眠", "dreams": ["一片海"]},
            {"start": "07:00", "end": "24:00", "event": "工作", "energy_rate": -0.05},
        ])
        self.assertEqual(service.status()["dreams"], ["一片海"])
        self.assertEqual(service.last_dreams(), ["一片海"])


class DaytimeNapFallbackTest(unittest.TestCase):
    """本地兜底：白天困意越线要有地方去，不能继续排煮饭。"""

    def test_daytime_high_pressure_naps(self):
        cfg_day = cfg(night_start_hour=23, night_end_hour=7)
        slot = dynamic_segment(
            cfg_day, {"sleep_pressure": 86.0}, now_minute=13 * 60, step=15,
            seed=["bot", "2026-08-22"],
        )
        self.assertTrue(is_sleep_event(slot["event"]), slot)
        minutes = parse_time(slot["end"]) - parse_time(slot["start"])
        self.assertGreaterEqual(minutes, 45)
        self.assertLessEqual(minutes, 90)

    def test_daytime_below_the_line_does_not_nap(self):
        cfg_day = cfg(night_start_hour=23, night_end_hour=7)
        slot = dynamic_segment(
            cfg_day, {"sleep_pressure": 81.0}, now_minute=13 * 60, step=15,
            seed=["bot", "2026-08-22"],
        )
        self.assertFalse(is_sleep_event(slot["event"]), slot)

    def test_night_pressure_still_sleeps_to_window_end(self):
        cfg_day = cfg(night_start_hour=23, night_end_hour=7)
        slot = dynamic_segment(
            cfg_day, {"sleep_pressure": 86.0}, now_minute=2 * 60, step=15,
            seed=["bot", "d"],
        )
        self.assertTrue(is_sleep_event(slot["event"]))
        self.assertEqual(slot["end"], "07:00")

    def test_already_asleep_by_day_is_capped_at_90(self):
        cfg_day = cfg(night_start_hour=23, night_end_hour=7)
        slot = dynamic_segment(
            cfg_day, {"asleep": 1.0}, now_minute=14 * 60, step=15, seed=["bot", "d"],
        )
        self.assertTrue(is_sleep_event(slot["event"]))
        self.assertEqual(slot["end"], "15:30", "白天已经睡着时兜底不该一次排两小时以上")

    def test_nap_duration_is_deterministic(self):
        cfg_day = cfg(night_start_hour=23, night_end_hour=7)
        first = dynamic_segment(
            cfg_day, {"sleep_pressure": 86.0}, now_minute=13 * 60, step=15,
            seed=["bot", "d"],
        )
        second = dynamic_segment(
            cfg_day, {"sleep_pressure": 86.0}, now_minute=13 * 60, step=15,
            seed=["bot", "d"],
        )
        self.assertEqual(first, second)


class PressureTriggerTest(unittest.IsolatedAsyncioTestCase):
    def test_threshold_is_80(self):
        self.assertEqual(CHANGE_TRIGGER_SLEEP_PRESSURE, 80.0)

    def test_due_fires_at_80(self):
        conf = cfg(use_llm_schedule=False)
        body = {"sleep_pressure": 79.0}
        service, store, _, _ = build_service(conf, body=body)
        service._append_segment(
            parse_segment({"event": "工作", "minutes": 120}, now_minute=9 * 60, step=15),
            TODAY,
        )
        self.assertFalse(service._due(roll=False), "79 不该触发")
        body["sleep_pressure"] = 80.0
        self.assertTrue(service._due(roll=False), "80 必须触发（旧阈值 88 会放过）")

    async def test_prompt_knows_about_pressure_and_previous_sleep(self):
        conf = cfg(schedule_provider_name="p")
        service, store, clock, provider = build_service(
            conf, moment=datetime(2026, 8, 22, 9, 10, tzinfo=TZ),
            body={"sleep_pressure": 85.0, "energy": 40.0},
            reply=seg_reply("工作", 60),
        )
        store.set("today_date", TODAY)
        store.set("daily_schedule", [
            {"start": "00:00", "end": "09:00", "event": "睡眠",
             "location": "卧室", "emotion": "沉睡", "energy_rate": 0.15},
        ])
        await service.ensure_fresh(force=True)
        prompt = provider.last_kwargs.get("prompt") or ""
        self.assertIn("困意已经 80+/100", prompt)
        self.assertIn("上一段是睡眠", prompt, "刚醒后的这一段该允许赖床")


if __name__ == "__main__":
    unittest.main()
