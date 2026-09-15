"""v2.14.2 作息回归。

这一轮改的全是「她几点起、睡够没有、什么时候最困」这类问题，而它们以前要么算错、
要么配置项根本没进计算，所以断言都盯着数值本身，不盯字符串。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from humanoid.config import HumanoidConfig
from humanoid.data.schedule_templates import FALLBACK_TEMPLATES
from humanoid.services.schedule import (
    align_sleep_to_night,
    build_prompt,
    routine_prompt,
    schedule_wake_minute,
    schedule_wake_text,
)
from humanoid.services.soma import SomaService
from humanoid.slots import coverage_is_complete, find_slot, is_sleep_event, normalize_slots

from .fakes import FrozenClock, ScopeStore

TZ = timezone(timedelta(hours=8))


def cfg(**overrides) -> HumanoidConfig:
    base = {
        "timezone_city": "北京",
        "night_mode_enabled": True,
        "night_start_hour": 23,
        "night_end_hour": 5,
        "sleep_need_hours": 8.0,
        "schedule_follow_night_window": True,
        "weather_affects_body": False,
        "enable_cycle": False,
    }
    base.update(overrides)
    return HumanoidConfig.from_raw(base)


class BodyFixture:
    """只带身体服务的角色作用域，时间由测试推着走。"""

    def __init__(self, moment: datetime, config: HumanoidConfig, slots=None) -> None:
        self.store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        self.store.load(moment.strftime("%Y-%m-%d"), config.cycle_length)
        self.clock = FrozenClock(moment)
        self.time_value = moment.timestamp()
        self.slots = slots if slots is not None else []
        self.soma = SomaService(
            self.store.scope,
            lambda: config,
            self.clock,
            schedule_provider=lambda: self.slots,
            weather_provider=lambda: {},
            time_source=lambda: self.time_value,
        )

    def to(self, moment: datetime) -> dict:
        self.clock.moment = moment
        self.time_value = moment.timestamp()
        return self.soma.advance()


AWAKE_ALL_DAY = [
    {"start": "00:00", "end": "24:00", "event": "白天活动", "energy_rate": 0.0}
]
SLEEP_UNTIL_0800 = [
    {"start": "00:00", "end": "08:00", "event": "睡眠", "energy_rate": 0.18},
    {"start": "08:00", "end": "24:00", "event": "活动", "energy_rate": 0.0},
]
SLEEP_UNTIL_0400 = [
    {"start": "00:00", "end": "04:00", "event": "睡眠", "energy_rate": 0.18},
    {"start": "04:00", "end": "24:00", "event": "活动", "energy_rate": 0.0},
]


class NightWindowGeometryTest(unittest.TestCase):
    def test_cross_midnight_midpoint_is_the_real_one(self):
        """23→5 的中点是凌晨 2 点，不是算术平均出来的下午 2 点。"""
        self.assertAlmostEqual(cfg().night_mid_hour, 2.0)
        self.assertAlmostEqual(cfg(night_start_hour=23, night_end_hour=6).night_mid_hour, 2.5)
        self.assertAlmostEqual(cfg(night_start_hour=1, night_end_hour=9).night_mid_hour, 5.0)

    def test_dip_lands_in_the_night_not_the_afternoon(self):
        soma = BodyFixture(datetime(2026, 8, 22, 8, 0, tzinfo=TZ), cfg()).soma
        self.assertGreater(soma.circadian_dip(5.0), 0.9, "生物钟夜中点后两小时该是全天最困")
        self.assertLess(soma.circadian_dip(17.0), 0.1, "下午五点不该是昼夜低谷")

    def test_window_length_and_debt_gain(self):
        self.assertEqual(cfg().night_span_hours, 6.0)
        self.assertAlmostEqual(cfg().sleep_debt_gain_per_hour, 8.0 / 6.0, places=3)
        self.assertEqual(cfg(night_start_hour=22, night_end_hour=8).sleep_debt_gain_per_hour, 1.0)
        # 关掉夜间模式后窗口长度为 0，缩放退回 1 倍而不是除零
        self.assertEqual(cfg(night_mode_enabled=False).sleep_debt_gain_per_hour, 1.0)


class SleepNeedActuallyCountsTest(unittest.TestCase):
    def test_relief_rate_comes_from_sleep_need(self):
        self.assertAlmostEqual(cfg(sleep_need_hours=8.0).sleep_relief_per_hour, 12.5)
        self.assertAlmostEqual(cfg(sleep_need_hours=4.0).sleep_relief_per_hour, 25.0)

    def test_sleeping_need_hours_worth_clears_all_pressure(self):
        """睡够「一晚该睡多久」正好把满值的困意清干净，睡短了则留着。"""
        start = datetime(2026, 8, 22, 0, 0, tzinfo=TZ)
        full = BodyFixture(start, cfg(sleep_need_hours=8.0), SLEEP_UNTIL_0800)
        full.soma._set("sleep_pressure", 100.0)
        after = full.to(start + timedelta(hours=7))
        self.assertLess(after["sleep_pressure"], 15.0, "8 小时该清到接近 0，七小时就该只剩一点")

        short = BodyFixture(start, cfg(sleep_need_hours=8.0), SLEEP_UNTIL_0400)
        short.soma._set("sleep_pressure", 100.0)
        after = short.to(start + timedelta(hours=4))
        self.assertGreater(after["sleep_pressure"], 40.0, "只睡一半不该把困意清完")

    def test_wake_does_not_clear_pressure_a_second_time(self):
        """积分时已经按小时清过，醒来那一刻不能再减一笔。"""
        start = datetime(2026, 8, 22, 0, 0, tzinfo=TZ)
        # need=14 时一夜只清得掉一半，醒来时压力还在高位，重复扣减会看得出
        fixture = BodyFixture(start, cfg(sleep_need_hours=14.0), SLEEP_UNTIL_0800)
        fixture.soma._set("sleep_pressure", 100.0)
        before = fixture.to(start + timedelta(hours=7, minutes=50))
        self.assertGreater(before["sleep_pressure"], 30.0, "这一觉还没清完，醒来时压力仍在高位")
        after = fixture.to(start + timedelta(hours=8, minutes=10))
        self.assertGreater(
            after["sleep_pressure"], before["sleep_pressure"] - 1.0,
            "醒来的那一刻不该出现一次性下跌（旧实现会在这里再清 12 点）",
        )
        self.assertGreater(fixture.soma.data["last_sleep_hours"], 7.5)

    def test_short_window_makes_night_owes_more(self):
        """窗口 6 小时却需要睡 8 小时的人，同样熬一夜更补不回来。"""
        start = datetime(2026, 8, 22, 23, 0, tzinfo=TZ)
        tight = BodyFixture(start, cfg(night_start_hour=23, night_end_hour=5, sleep_need_hours=8.0), AWAKE_ALL_DAY)
        tight.to(start + timedelta(hours=1, minutes=30))
        wide = BodyFixture(start, cfg(night_start_hour=22, night_end_hour=8, sleep_need_hours=8.0), AWAKE_ALL_DAY)
        wide.to(start + timedelta(hours=1, minutes=30))
        self.assertGreater(tight.soma.snapshot()["sleep_debt"], wide.soma.snapshot()["sleep_debt"] + 0.4)

    def test_night_mode_off_stops_counting_debt(self):
        start = datetime(2026, 8, 22, 23, 0, tzinfo=TZ)
        fixture = BodyFixture(start, cfg(night_mode_enabled=False), AWAKE_ALL_DAY)
        fixture.to(start + timedelta(hours=3))
        self.assertEqual(fixture.soma.snapshot()["sleep_debt"], 0.0)


class SleepKeywordTest(unittest.TestCase):
    def test_lie_in_bed_counts_as_sleep(self):
        """「懒觉」以前认不出来：日程写着懒觉，身体却算她醒着。"""
        fixture = BodyFixture(
            datetime(2026, 8, 22, 6, 0, tzinfo=TZ),
            cfg(),
            [{"start": "00:00", "end": "09:00", "event": "懒觉", "energy_rate": 0.2}],
        )
        self.assertTrue(fixture.soma.is_sleep_time(datetime(2026, 8, 22, 6, 30, tzinfo=TZ)))

    def test_feeling_the_word_is_not_sleep(self):
        fixture = BodyFixture(
            datetime(2026, 8, 22, 6, 0, tzinfo=TZ),
            cfg(),
            [{"start": "00:00", "end": "24:00", "event": "觉得无聊刷手机", "energy_rate": 0.0}],
        )
        self.assertFalse(fixture.soma.is_sleep_time(datetime(2026, 8, 22, 6, 30, tzinfo=TZ)))

    def test_lunch_nap_word_does_not_put_her_to_bed(self):
        """「午餐与午休发呆」是在吃饭：把她判成睡着，中午就再也不会主动找人。"""
        fixture = BodyFixture(
            datetime(2026, 8, 22, 12, 30, tzinfo=TZ),
            cfg(),
            [
                {"start": "00:00", "end": "12:00", "event": "睡眠", "energy_rate": 0.18},
                {"start": "12:00", "end": "13:30", "event": "午餐与午休发呆", "energy_rate": 0.08},
                {"start": "13:30", "end": "24:00", "event": "下午工作", "energy_rate": -0.1},
            ],
        )
        self.assertFalse(fixture.soma.is_sleep_time(datetime(2026, 8, 22, 12, 40, tzinfo=TZ)))

    def test_a_plain_nap_still_counts_as_sleep(self):
        fixture = BodyFixture(
            datetime(2026, 8, 22, 13, 0, tzinfo=TZ),
            cfg(),
            [
                {"start": "00:00", "end": "12:30", "event": "睡眠", "energy_rate": 0.18},
                {"start": "12:30", "end": "13:30", "event": "午休", "energy_rate": 0.1},
                {"start": "13:30", "end": "24:00", "event": "下午工作", "energy_rate": -0.1},
            ],
        )
        self.assertTrue(fixture.soma.is_sleep_time(datetime(2026, 8, 22, 13, 0, tzinfo=TZ)))


class AlignSleepToNightWindowTest(unittest.TestCase):
    def test_template_sleep_moves_to_the_window(self):
        config = cfg()
        for template in FALLBACK_TEMPLATES:
            base = normalize_slots(template, max_slots=16)
            aligned = align_sleep_to_night(base, config)
            self.assertTrue(coverage_is_complete(aligned), f"对齐后不闭合：{aligned}")
            minutes = schedule_wake_minute(aligned)
            self.assertEqual(minutes, 5 * 60, f"起床点没贴到 05:00：{aligned}")
            slot = find_slot(aligned, 6 * 60)
            self.assertFalse(
                any(w in str(slot.get("event", "")) for w in ("睡", "眠", "懒觉", "小憩")),
                f"6 点还在睡：{slot}",
            )

    def test_model_schedule_that_ignores_the_constraint_is_fixed(self):
        config = cfg()
        late = normalize_slots(
            [
                {"start": "00:00", "end": "08:00", "event": "睡眠休息", "energy_rate": 0.18},
                {"start": "08:00", "end": "12:00", "event": "工作", "energy_rate": -0.1},
                {"start": "12:00", "end": "18:00", "event": "下午做事", "energy_rate": -0.05},
                {"start": "18:00", "end": "24:00", "event": "晚间休闲", "energy_rate": 0.0},
            ],
            max_slots=16,
        )
        aligned = align_sleep_to_night(late, config)
        self.assertEqual(schedule_wake_text(aligned), "05:00")
        self.assertTrue(coverage_is_complete(aligned))
        # 5-8 点这段从睡眠里切出来，不能再带「睡」字，否则身体以为她还在睡
        self.assertEqual(find_slot(aligned, 6 * 60)["event"], "赖床与洗漱")

    def test_alignment_is_idempotent(self):
        config = cfg()
        once = align_sleep_to_night(
            normalize_slots(FALLBACK_TEMPLATES[0], max_slots=16), config
        )
        twice = align_sleep_to_night(once, config)
        self.assertEqual(once, twice)

    def test_nap_outside_the_window_keeps_its_name(self):
        """午休在窗口外：它是午休，不该被改名成「赖床」。"""
        config = cfg()
        aligned = align_sleep_to_night(normalize_slots(FALLBACK_TEMPLATES[0], max_slots=16), config)
        events = " ".join(str(s["event"]) for s in aligned)
        self.assertIn("午餐与午休发呆", events)

    def test_off_switch_leaves_the_schedule_alone(self):
        config = cfg(schedule_follow_night_window=False)
        base = normalize_slots(FALLBACK_TEMPLATES[0], max_slots=16)
        self.assertEqual(align_sleep_to_night(base, config), base)

    def test_evening_routine_before_bedtime_is_named_after_itself(self):
        """睡前那一格叫「洗漱与护肤」就该保持这个名字：它不是觉，别被改名、更别被当成在睡。"""
        config = cfg()
        aligned = align_sleep_to_night(normalize_slots(FALLBACK_TEMPLATES[0], max_slots=16), config)
        slot = find_slot(aligned, 22 * 60 + 30)
        self.assertEqual(slot["event"], "夜间洗漱与护肤")
        self.assertFalse(is_sleep_event(slot["event"]), "把睡前流程算成睡眠，她就 22 点起再没醒过")


class RoutinePromptTest(unittest.TestCase):
    def test_prompt_carries_bedtime_and_wake_time(self):
        prompt = build_prompt(cfg(), "2026-08-22", "六")
        self.assertIn("23:00 上床", prompt)
        self.assertIn("05:00 结束", prompt)
        self.assertIn("睡眠", prompt)

    def test_prompt_warns_when_window_is_shorter_than_needed_sleep(self):
        self.assertIn("赖床", routine_prompt(cfg(sleep_need_hours=8.0)))
        self.assertNotIn("赖床", routine_prompt(cfg(night_start_hour=22, night_end_hour=8)))

    def test_prompt_lets_the_persona_choose_her_routine(self):
        """不锁窗口时不再写「必须 23:00 上床」，但也不能什么都不说——那会退回模型的直觉。"""
        free = routine_prompt(cfg(schedule_follow_night_window=False))
        self.assertNotIn("必须遵守", free)
        self.assertNotIn("23:00 上床", free)
        self.assertIn("由她是谁决定", free)
        self.assertIn("夜猫子", free)
        # 关掉夜间模式只是不算生物钟夜，「几点睡由她自己定」这句照样该给模型——
        # 不给的话模型会退回直觉，把所有人都排成 00:00→08:00。
        self.assertIn("由她是谁决定", routine_prompt(cfg(night_mode_enabled=False)))
        self.assertNotIn("锁定了", routine_prompt(cfg(night_mode_enabled=False)))

    def test_locked_window_is_an_explicit_override(self):
        locked = routine_prompt(cfg(schedule_follow_night_window=True))
        self.assertIn("用户把她的作息锁定了", locked)
        self.assertIn("23:00 上床", locked)


if __name__ == "__main__":
    unittest.main()
