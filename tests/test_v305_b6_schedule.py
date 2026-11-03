"""B6 批次（t3b-schedule 范围）：日程/身体/过程五条「中」的回归。

对应审核条目：#60 身体F7、#61 身体F8、#64 身体F11、#52 状态10、#58 状态21。
每条都写成「用户能看见的现象」而不是「函数返回了什么」：
* 她睡着的时候不该攒久坐的僵硬（#64）；
* 「整理工作资料」是工作，不是家务（#61）；
* 「失眠，翻来覆去刷手机」不是睡着（#58）；
* 23:00→07:00 的一觉在夹具里不该凭空消失，sleep_window_minutes 要能认出它（#52）；
* note_message 的注释要与它真正做的事一致（#60）。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.clock import Clock
from humanoid.config import HumanoidConfig
from humanoid.role_scope import RoleScope
from humanoid.services.process import ProcessService
from humanoid.services.soma import SomaService
from humanoid.slots import is_sleep_event, sleep_window_minutes, slot_sleeps

from .fakes import FrozenClock, ScopeStore
from .slot_fixtures import normalize_slots

TZ = ZoneInfo("Asia/Shanghai")


def cfg(**overrides) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", **overrides})


class _FakeSchedule:
    def __init__(self, event: str = "工作", start: str = "09:00", end: str = "18:00"):
        self._slot = {"event": event, "start": start, "end": end}

    def current_slot(self, minutes=None):
        return dict(self._slot)


def _process_service() -> ProcessService:
    conf = cfg()
    scope = RoleScope({"_state_version": 3, "roles": {}}, "bot1", lambda: None)
    return ProcessService(scope, lambda: conf, Clock(lambda: conf), _FakeSchedule())


class SittingWhileAsleepTest(unittest.TestCase):
    """#64：睡着不是久坐。整夜累加的话，早上醒来第一句就是「身体不舒服」。"""

    def build(self, moment, slots):
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load()
        clock = FrozenClock(moment)
        soma = SomaService(
            store.scope,
            lambda: cfg(),
            clock,
            schedule_provider=lambda: slots,
            weather_provider=None,
            time_source=lambda: clock.now().timestamp(),
        )
        return soma, clock

    def test_a_night_of_sleep_does_not_accumulate_stiffness(self):
        night = [
            {"event": "睡觉", "location": "卧室", "energy_rate": 0.1, "start": "22:00", "end": "24:00"},
            {"event": "睡觉", "location": "卧室", "energy_rate": 0.1, "start": "00:00", "end": "08:00"},
        ]
        soma, clock = self.build(datetime(2026, 8, 22, 22, 0, tzinfo=TZ), night)
        for _ in range(48):  # 22:00 → 06:00：一整夜
            clock.advance(minutes=10)
            soma.advance()
        now_ts = clock.now().timestamp()
        sitting_since = float(soma.data.get("sitting_since") or 0.0)
        self.assertGreaterEqual(
            sitting_since,
            now_ts - 15 * 60,
            "睡着期间久坐计时还在走：整夜都在累加僵硬感",
        )
        # 修前：stiffness 顶到 45 → 45*0.45=20.25 叠在周期项 24 上，醒来就是 44。
        # 修后只剩周期项（60*0.4=24）。阈值 30 正好把两者分开。
        self.assertLess(
            float(soma.data.get("discomfort") or 0.0),
            30.0,
            f"睡了一整夜还带着躯体不适：{soma.data.get('discomfort')}",
        )


class CategoryOrderTest(unittest.TestCase):
    """#61：裸「整理」抢在家务前面，于是「整理工作资料」被判成家务。"""

    def test_organising_work_materials_is_work(self):
        self.assertEqual(_process_service()._infer_category({"event": "整理工作资料"}), "工作")

    def test_organising_a_room_is_chores(self):
        self.assertEqual(_process_service()._infer_category({"event": "整理房间"}), "家务")

    def test_cooking_reaches_meal_not_chores(self):
        self.assertEqual(_process_service()._infer_category({"event": "做饭"}), "用餐")


class SleepWordOrderTest(unittest.TestCase):
    """#58 / 状态9：「失眠」「睡不着」含睡字，却正相反——她在床上醒着。"""

    def test_insomnia_is_not_sleeping(self):
        self.assertFalse(is_sleep_event("失眠，翻来覆去刷手机"))
        self.assertFalse(
            slot_sleeps(
                {
                    "event": "失眠，翻来覆去刷手机",
                    "location": "卧室",
                    "energy_rate": 0.1,
                    "start": "23:00",
                    "end": "07:00",
                }
            )
        )

    def test_exclusion_words_block_the_compound_rule(self):
        """#58 残留：排除表也要挡住复合判据——这三条事件名里没有别的醒词可依靠。

        修前 slot_sleeps 只查 is_sleep_event 里的排除表，复合判据（卧室 + 0.1 + 8 小时）
        会把它们判成睡着；删掉 slots.py 里那一行检查，这里立刻变红。
        """
        for event in ("失眠", "睡眠不足", "睡眠浅"):
            with self.subTest(event=event):
                self.assertFalse(slot_sleeps(self._night(event)))

    def test_a_real_sleep_with_washing_still_counts(self):
        """「洗漱后上床睡觉」是真睡：别把「洗」当成醒着的证据（见 #9 的反例）。"""
        self.assertTrue(is_sleep_event("洗漱后上床睡觉"))

    @staticmethod
    def _night(event, location="卧室", rate=0.1):
        return {
            "event": event,
            "location": location,
            "energy_rate": rate,
            "start": "23:00",
            "end": "07:00",
        }

    def test_washing_before_sleep_is_sleeping(self):
        """#58 验收：睡前洗漱、洗完准备睡，都是睡着。"""
        for event in ("洗漱后上床睡觉", "洗完澡准备睡了"):
            with self.subTest(event=event):
                self.assertTrue(is_sleep_event(event))
                self.assertTrue(slot_sleeps(self._night(event)))

    def test_washing_then_waking_up_is_awake(self):
        """#58 验收：洗完精神了、刚起床去洗漱，都是醒着——不能被「卧室 + 低消耗」判成睡。"""
        for event in ("洗了个澡，精神了", "刚起床，去洗漱"):
            with self.subTest(event=event):
                self.assertFalse(is_sleep_event(event))
                self.assertFalse(slot_sleeps(self._night(event)))

    def test_strong_awake_word_beats_strong_sleep_word(self):
        """强醒 > 强睡：一个刚起床、正打算睡午觉的人，现在不是睡着的。"""
        for event in ("起床后洗了个澡，准备睡个午觉", "刚醒来，打算补个午觉"):
            with self.subTest(event=event):
                self.assertFalse(is_sleep_event(event))
                self.assertFalse(slot_sleeps(self._night(event)))

    def test_lie_down_after_washing_is_sleeping(self):
        """「躺下」「上床」是睡的动作：没有睡字的真实睡段也要判得出来（弱醒词不再挡）。"""
        for event in ("洗漱后回卧室躺下", "洗完澡，上床"):
            with self.subTest(event=event):
                self.assertTrue(is_sleep_event(event))
                self.assertTrue(slot_sleeps(self._night(event)))

    def test_zero_length_poetic_segment_is_not_a_night(self):
        """没有睡字的卧床段靠「够长」成立；00 分钟不算一段（span<=0 那支已被删）。"""
        self.assertFalse(
            slot_sleeps(
                {
                    "event": "阖眼，呼吸渐沉",
                    "location": "卧室",
                    "energy_rate": 0.1,
                    "start": "23:00",
                    "end": "23:00",
                }
            )
        )


class CrossingSlotFixtureTest(unittest.TestCase):
    """#52：夹具的规范化曾把 23:00→07:00 整段丢掉，跨午夜分支永远测不到。"""

    def test_a_crossing_night_survives_normalisation(self):
        slots = normalize_slots(
            [
                {"event": "睡觉", "location": "卧室", "energy_rate": 0.1, "start": "23:00", "end": "07:00"},
                {"event": "上班", "start": "09:00", "end": "18:00"},
            ]
        )
        self.assertTrue(slots)
        window = sleep_window_minutes(slots)
        self.assertEqual(window, (23 * 60, 7 * 60))
        covered = [s for s in slots if slot_sleeps(s)]
        self.assertTrue(covered, "跨午夜的一觉在夹具里消失了")
        self.assertTrue(any(s["start"] == "00:00" for s in covered), covered)


class NoteMessageDocTest(unittest.TestCase):
    """#60：注释说「磨掉精神」，代码是清醒度 +1.5。改的是注释。"""

    def test_docstring_matches_the_body(self):
        doc = (SomaService.note_message.__doc__ or "").strip()
        self.assertIn("唤醒", doc)
        self.assertNotIn("磨掉", doc)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
