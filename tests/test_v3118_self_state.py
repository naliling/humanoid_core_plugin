"""v3.1.18：她自己的状态——知道，但不必每轮都说出口。

三件事：

* **整体心境**（`schedule_content.overall_mood`）：只看她自己的身体，给一个标签 +
  一句依据，不看任何一个人的好感；没到阈值就什么都不给。
* **私事**（`schedule_content.PRIVATE_TERMS` / `ScheduleService.note_intimate`）：
  亲密场景真的发生过才记一条，日程里不排、不写、不当素材；只在她自己的状态里留
  一句「身子还软着」。
* **感知门**（`main._asks_about_self`）：她一直知道自己的日子，但每轮都报备就成了
  表演——只有对方问到她本人时才把「她自己的情况」递进去。
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from humanoid.emotion import EmotionLayer
from humanoid.llm import LLMGateway, ProviderResolver
from humanoid.services.schedule import (
    INTIMATE_MAX_PER_DAY,
    INTIMATE_REST_MINUTES,
    ScheduleService,
    _step_for,
    segment_prompt,
    segment_start,
)
from humanoid.services.schedule_content import (
    build_slate,
    content_issues,
    fix_hint,
    overall_mood,
)

from .fakes import FakeContext, FakeProvider, FrozenClock, RecordingLogger, ScopeStore
from .test_main_integration import main_module as plugin_main
from .test_schedule import cfg, seg_reply

if plugin_main is None:  # pragma: no cover - 取决于运行环境
    raise unittest.SkipTest("main.py 加载失败，跳过自身状态用例")

TZ = ZoneInfo("Asia/Shanghai")
FIXED_TODAY = "2026-08-22"
FIXED_MOMENT = datetime(2026, 8, 22, 9, 0, tzinfo=TZ)


class PrivateTermsTest(unittest.TestCase):
    """私事不进日程：判得出来、候选里剔得掉、重写提示说得清。"""

    def test_private_terms_are_reported(self):
        self.assertIn("私事:自慰", content_issues("半夜忍不住自慰了一回"))
        self.assertIn("私事:做爱", content_issues("两个人在床上做爱"))
        self.assertIn("私事:高潮", content_issues("高潮过后躺着缓了半天"))

    def test_daily_intimacy_words_are_not_private(self):
        """「抱」「亲」这种日常词不禁——禁的是明确的那类事。"""
        self.assertEqual(content_issues("抱了一会儿，又亲了一下额头"), [])
        self.assertEqual(content_issues("牵着她的手在院子里站了会儿"), [])

    def test_fix_hint_says_it_is_private(self):
        hint = fix_hint(content_issues("自慰了一回"))
        self.assertIn("私事", hint)
        self.assertIn("换一件别的事", hint)

    def test_slate_drops_private_candidates(self):
        items = build_slate(
            [
                {"event": "半夜忍不住自慰了一回", "location": "卧室", "minutes": 30},
                {"event": "把被子抱出去晒", "location": "阳台", "minutes": 20},
            ]
        )
        self.assertEqual([item["event"] for item in items], ["把被子抱出去晒"])


class OverallMoodTest(unittest.TestCase):
    """整体心境：只从身体推，不看好感。"""

    def test_quiet_body_gives_nothing(self):
        label, reason = overall_mood(
            {
                "sleep_debt": 0.0,
                "last_sleep_hours": 7.5,
                "sleep_pressure": 20.0,
                "hunger": 10.0,
                "discomfort": 0.0,
                "arousal": 30.0,
            }
        )
        self.assertEqual((label, reason), ("", ""))

    def test_empty_body_gives_nothing(self):
        self.assertEqual(overall_mood({}), ("", ""))
        self.assertEqual(overall_mood(None), ("", ""))

    def test_sleep_debt(self):
        label, reason = overall_mood({"sleep_debt": 2.5, "last_sleep_hours": 7.5})
        self.assertIn("没睡够", label)
        self.assertIn("2.5", reason)

    def test_short_last_night(self):
        label, reason = overall_mood({"last_sleep_hours": 4.0})
        self.assertIn("没睡够", label)
        self.assertIn("4", reason)

    def test_sleep_pressure(self):
        label, _ = overall_mood({"last_sleep_hours": 7.5, "sleep_pressure": 85.0})
        self.assertIn("撑不住", label)

    def test_hunger(self):
        label, _ = overall_mood({"last_sleep_hours": 7.5, "hunger": 75.0})
        self.assertIn("发空", label)

    def test_discomfort_and_arousal(self):
        self.assertIn("不得劲", overall_mood({"last_sleep_hours": 7.5, "discomfort": 60.0})[0])
        self.assertIn("燥", overall_mood({"last_sleep_hours": 7.5, "arousal": 80.0})[0])

    def test_affection_does_not_change_it(self):
        """日程是她自己的日子：别人的好感不该把这一天变成「心情很好的一天」。"""
        label, _ = overall_mood(
            {"last_sleep_hours": 7.5, "affection": 100.0, "libido": 100.0}
        )
        self.assertEqual(label, "")


class FlexibleGranularityTest(unittest.TestCase):
    """默认粒度改成 flexible：不再把起点对齐到 15 分钟。"""

    def test_default_is_flexible(self):
        conf = cfg()
        self.assertEqual(conf.schedule_time_granularity, "flexible")
        self.assertEqual(conf.granularity_minutes, 0)

    def test_segment_start_keeps_the_real_minute(self):
        self.assertEqual(segment_start(487, 0), 487)  # 08:07 不被推到 08:00
        self.assertEqual(segment_start(487, 15), 480)  # 显式设 15 分钟仍对齐

    def test_step_for_handles_zero(self):
        # flexible 下 step 退化成 1：起点就是此刻，不再向下对齐。
        self.assertEqual(_step_for(60, 0), 1)
        self.assertEqual(_step_for(61, 15), 15)
        # 段比刻度短时也不用粗刻度，否则起点会退回到上一段里。
        self.assertEqual(_step_for(10, 15), 1)


class IntimateLogTest(unittest.IsolatedAsyncioTestCase):
    """私事只记事实：一天最多几条、只留最近三小时的余韵。"""

    def build(self, moment=FIXED_MOMENT):
        conf = cfg(
            schedule_provider_name="p",
            schedule_min_interval_minutes=0,
            schedule_max_interval_minutes=0,
            llm_idle_silence_minutes=0,
            llm_daily_call_budget=100,
        )
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load(FIXED_TODAY, conf.cycle_length)
        log = RecordingLogger()
        provider = FakeProvider("p", reply=seg_reply(minutes=40))
        resolver = ProviderResolver(FakeContext(chat_providers=[provider]), log)
        gateway = LLMGateway(resolver, lambda: conf, log, time.monotonic)
        service = ScheduleService(
            store.scope,
            lambda: conf,
            FrozenClock(moment),
            logger=log,
            monotonic=time.monotonic,
        )
        service.set_resolver_gateway(resolver, gateway)
        return service

    def test_records_and_counts(self):
        service = self.build()
        self.assertTrue(service.note_intimate(FIXED_MOMENT.timestamp()))
        self.assertEqual(service.intimate_today(), 1)
        self.assertEqual(service.status()["intimate_today"], 1)

    def test_bad_moment_is_ignored(self):
        service = self.build()
        self.assertFalse(service.note_intimate(0))
        self.assertFalse(service.note_intimate(float("nan")))

    def test_caps_per_day(self):
        service = self.build()
        base = FIXED_MOMENT.timestamp()
        for index in range(INTIMATE_MAX_PER_DAY + 3):
            service.note_intimate(base + index * 60)
        self.assertEqual(service.intimate_today(), INTIMATE_MAX_PER_DAY)

    def test_rest_line_inside_the_window(self):
        service = self.build()
        service.note_intimate(FIXED_MOMENT.timestamp())
        line = service._intimate_rest_line(FIXED_MOMENT)
        self.assertIn("身子还软着", line)
        self.assertEqual(content_issues(line), [], "余韵那句本身也不许带脏词")

    def test_rest_line_expires(self):
        service = self.build()
        service.note_intimate(FIXED_MOMENT.timestamp())
        later = FIXED_MOMENT + timedelta(minutes=INTIMATE_REST_MINUTES + 1)
        self.assertEqual(service._intimate_rest_line(later), "")

    def test_rest_line_without_any_record(self):
        service = self.build()
        self.assertEqual(service._intimate_rest_line(FIXED_MOMENT), "")

    def test_rest_line_never_names_the_act(self):
        """余韵只说身体，不复述发生了什么——日程里不写那件事。"""
        service = self.build()
        service.note_intimate(FIXED_MOMENT.timestamp())
        line = service._intimate_rest_line(FIXED_MOMENT)
        for word in ("自慰", "做爱", "高潮"):
            self.assertNotIn(word, line)


class PromptBlocksTest(unittest.TestCase):
    """两块文本进 prompt：有才出现，空就连标题都没有。"""

    def render(self, **kwargs):
        conf = cfg()
        return segment_prompt(
            conf,
            now_text="09:00",
            weekday="六",
            body={"energy": 60.0, "hunger": 10.0, "sleep_pressure": 20.0},
            **kwargs,
        )

    def test_absent_when_empty(self):
        prompt = self.render()
        self.assertNotIn("【她今天整体】", prompt)
        self.assertNotIn("【她身上的状态】", prompt)

    def test_mood_block(self):
        prompt = self.render(mood="心里发空（饿了大半天还没正经吃东西）")
        self.assertIn("【她今天整体】", prompt)
        self.assertIn("心里发空", prompt)

    def test_intimate_block(self):
        prompt = self.render(intimate="身子还软着，做什么都慢半拍，也不太想动")
        self.assertIn("【她身上的状态】", prompt)

    def test_private_things_stay_out_of_the_schedule_rule(self):
        prompt = self.render()
        self.assertIn("日程里不写亲密/性这类事", prompt)


class SceneBackgroundTest(unittest.TestCase):
    """亲密场景开头接得上她刚才在做的事（读侧）。"""

    def make(self, slot):
        layer = object.__new__(EmotionLayer)
        layer._core = SimpleNamespace(schedule=SimpleNamespace(current_slot=lambda: slot))
        return layer

    def test_reads_the_current_slot(self):
        line = self.make({"event": "把晾干的衣服收进来叠好"})._schedule_bg_line(0.0)
        self.assertEqual(line, "刚才在把晾干的衣服收进来叠好")

    def test_does_not_double_the_prefix(self):
        line = self.make({"event": "在厨房弄点吃的"})._schedule_bg_line(0.0)
        self.assertEqual(line, "刚才在厨房弄点吃的")

    def test_neutral_or_missing_slot_gives_nothing(self):
        self.assertEqual(self.make({"event": "自由活动"})._schedule_bg_line(0.0), "")
        self.assertEqual(self.make(None)._schedule_bg_line(0.0), "")
        self.assertEqual(self.make({"event": "   "})._schedule_bg_line(0.0), "")


class IntimateWriteBackTest(unittest.TestCase):
    """写回只发生在「真的记了账」之后，且失败不影响结算。"""

    def test_writes_through_to_schedule(self):
        from humanoid.core_instance import HumanoidCoreInstance

        seen = []
        fake = SimpleNamespace(schedule=SimpleNamespace(note_intimate=lambda at: seen.append(at)))
        HumanoidCoreInstance._note_intimate_in_schedule(fake, 1234.5)
        self.assertEqual(seen, [1234.5])

    def test_missing_schedule_is_harmless(self):
        from humanoid.core_instance import HumanoidCoreInstance

        HumanoidCoreInstance._note_intimate_in_schedule(SimpleNamespace(), 1234.5)

    def test_failure_is_swallowed(self):
        from humanoid.core_instance import HumanoidCoreInstance

        def boom(_at):
            raise RuntimeError("state broken")

        fake = SimpleNamespace(schedule=SimpleNamespace(note_intimate=boom))
        HumanoidCoreInstance._note_intimate_in_schedule(fake, 1234.5)


class _StubCore:
    """`_state_note` 要的那几样，只给最小可用的。"""

    def __init__(self, segments=(), snapshot=None, slot=None):
        self.prompt_builder = SimpleNamespace(
            config=SimpleNamespace(
                inject_token_budget=3500,
                mood_enabled=False,
                mood_enabled_in_group=False,
            ),
            background_note=lambda key="": "",
        )
        self.mood = SimpleNamespace(readable=self._not_readable)
        self.schedule = SimpleNamespace(
            current_slot=lambda: slot or {},
            ongoing_tasks=lambda: [],
            display_segments=lambda: list(segments),
        )
        self.soma = SimpleNamespace(snapshot=lambda: dict(snapshot or {}))
        self._snapshot = snapshot or {}

    @staticmethod
    def _not_readable(*_args, **_kwargs):
        return False

    def now_epoch(self):
        return FIXED_MOMENT.timestamp()

    def outfit_line(self):
        return ""


class SelfStateGateTest(unittest.TestCase):
    """「她自己的情况」只在对方问到她本人时才给。"""

    def note(self, said):
        core = _StubCore(
            segments=[
                {"event": "把晾干的衣服收进来叠好", "start": "07:00", "end": "07:40"},
                {"event": "在厨房弄点吃的", "start": "07:40", "end": "08:20"},
            ],
            snapshot={"last_sleep_hours": 5.5, "sleep_debt": 1.0},
        )
        return plugin_main._state_note(core, "u1", "她", "他", False, said)

    def test_asked_about_herself(self):
        note = self.note("你今天累不累")
        # 状态段在 `build_state_note` 里换成第一人称，所以只认「自己的情况」这个说法。
        self.assertIn("自己的情况", note)
        self.assertIn("把晾干的衣服收进来叠好", note)

    def test_not_asked_no_self_block(self):
        note = self.note("今天天气不错")
        self.assertNotIn("自己的情况", note)

    def test_asking_what_she_is_doing_does_not_duplicate(self):
        core = _StubCore(slot={"event": "把晾干的衣服收进来叠好", "location": "阳台"})
        note = plugin_main._state_note(core, "u1", "她", "他", False, "你在干嘛呢")
        self.assertIn("这会儿", note)
        self.assertNotIn("自己的情况", note, "问「在做什么」时不该再叠一遍今天做过的事")

    def test_gate_words(self):
        for said in ("你今天过得怎么样", "睡得好吗", "身体还好吗", "心情怎么样"):
            self.assertTrue(plugin_main._asks_about_self(said), said)
        for said in ("今天天气怎么样", "在吗", "帮我查个东西"):
            self.assertFalse(plugin_main._asks_about_self(said), said)

    def test_self_line_without_data_is_empty(self):
        self.assertEqual(plugin_main._self_state_line(_StubCore()), "")


class EmptyScheduleDoesNotCrashTest(unittest.IsolatedAsyncioTestCase):
    """没有日程数据时那几条新链路都要安静地返回空，不抛。"""

    def test_self_line_on_empty_schedule(self):
        self.assertEqual(plugin_main._self_state_line(_StubCore()), "")

    def test_bg_line_on_broken_schedule(self):
        layer = object.__new__(EmotionLayer)

        def boom():
            raise RuntimeError("no schedule")

        layer._core = SimpleNamespace(schedule=SimpleNamespace(current_slot=boom))
        self.assertEqual(layer._schedule_bg_line(0.0), "")

    def test_note_intimate_on_missing_scope(self):
        from humanoid.core_instance import HumanoidCoreInstance

        HumanoidCoreInstance._note_intimate_in_schedule(SimpleNamespace(schedule=None), 1.0)


if __name__ == "__main__":
    unittest.main()
