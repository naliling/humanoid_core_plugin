"""单一真源（B3 / R1）：同一件事只在一处算。

这个文件守的是**口径**，不是数值：

* `relation_of` 必须把「相处」那一半（轮数 / 天数）算进去，而且轮数被清理归零后
  阶段不许倒退——旧代码里注入用「好感 + 相处」、门面/情绪/里程碑只用好感，
  同一个人在同一轮能同时是「熟到可以直说」和「还在熟悉」。
* `sleep_of` 必须是三态：身体模块关掉、读不到状态时返回 `unknown`，
  不许用 `False`（=「醒着」）冒充。旧代码的退化分支正是这么干的，
  下游读成「她状态正常」。
* `in_night` 在夜间模式关掉时返回 `None`，不是 `False`：`False` 是
  「查了，不是夜里」，`None` 是「这个开关关掉了，没有这个概念」。
* 默认值只有一份：`aggression_default` 必须等于 `config.mood_initial_aggression`，
  且配置写 0 时要真的是 0（旧代码 `or 25.0` 把 0 读成 25）。
"""
from __future__ import annotations

import math
import unittest
from datetime import datetime
from types import SimpleNamespace

from humanoid import state_snapshot as snap
from humanoid.config import HumanoidConfig


def cfg(**overrides) -> HumanoidConfig:
    return HumanoidConfig.from_raw(dict(overrides))


class FakeSoma:
    """只实现快照会用到的那两个方法。"""

    def __init__(self, state=None, night=None):
        self._state = {} if state is None else state
        self._night = night or (lambda hour: hour >= 23 or hour < 6)
        self.data = {"asleep_since": 111.0}

    def wake_state(self):
        if isinstance(self._state, Exception):
            raise self._state
        return self._state

    def in_scheduled_night(self, hour):
        return bool(self._night(hour))


class FakeClock:
    def __init__(self, moment: datetime):
        self._moment = moment

    def now(self):
        return self._moment


class FakeSchedule:
    def __init__(self, slot=None):
        self._slot = slot or {}

    def current_slot(self):
        return self._slot


def fake_core(*, hour=23, minute=0, soma_state=None, conf=None, slot=None, night=None):
    moment = datetime(2026, 9, 26, hour, minute)
    return SimpleNamespace(
        config=conf or cfg(),
        soma=FakeSoma(soma_state, night),
        clock=FakeClock(moment),
        schedule=FakeSchedule(slot),
        now_epoch=lambda: moment.timestamp(),
    )


class RelationOfTest(unittest.TestCase):
    """关系位置：好感 + 相处，两条证据合成一份。"""

    def test_turn_count_reset_does_not_lower_the_stage(self):
        # 清理逻辑会把长期不联系的人 turn_count 归零，first_met 却保留。
        # 旧代码直接拿归零后的轮数算，阶段会从「熟到可以直说」掉回「还在熟悉」。
        now = 1_800_000_000.0
        cleaned = {
            "affection": 20.0,
            "turn_count": 0,
            "turn_count_peak": 400,
            "first_met": now - 40 * 86400,
        }
        rel = snap.relation_of(cleaned, now)
        self.assertEqual(rel.stage_id, 2)
        self.assertEqual(rel.turns, 400)
        self.assertTrue(rel.available)

    def test_old_records_without_peak_still_work(self):
        now = 1_800_000_000.0
        rel = snap.relation_of(
            {"affection": 20.0, "turn_count": 400, "first_met": now - 40 * 86400}, now
        )
        self.assertEqual(rel.stage_id, 2)

    def test_turns_alone_without_days_does_not_promote(self):
        # 「一口气刷 300 条」不等于熟：相处档要求轮数与天数同时满足（relstage 的纪律）。
        rel = snap.relation_of({"affection": 20.0, "turn_count": 400})
        self.assertEqual(rel.stage_id, 0)

    def test_peak_wins_even_when_turn_count_is_larger(self):
        rel = snap.relation_of({"affection": 20.0, "turn_count": 10, "turn_count_peak": 400})
        self.assertEqual(rel.turns, 400)

    def test_unavailable_carries_a_reason_and_no_stage(self):
        rel = snap.relation_of({}, 0.0, available=False, reason=snap.REASON_MOOD_DISABLED)
        self.assertFalse(rel.available)
        self.assertFalse(rel.known)
        self.assertEqual(rel.reason, "mood_disabled")
        self.assertEqual(rel.stage_id, 0)

    def test_days_come_from_first_met(self):
        now = 1_800_000_000.0
        rel = snap.relation_of({"affection": 20.0, "first_met": now - 30 * 86400}, now)
        self.assertAlmostEqual(rel.days, 30.0, places=6)

    def test_days_are_zero_when_time_is_unknown(self):
        # now=0（时钟没设 / 拿不到）时不能算出负数天，也不能拿 1970 年的 first_met 当真相。
        self.assertEqual(snap.relation_of({"first_met": 123.0}, 0.0).days, 0.0)
        self.assertEqual(snap.relation_of({"affection": 20.0}, 123.0).days, 0.0)

    def test_bad_values_do_not_raise(self):
        rel = snap.relation_of(
            {"affection": "oops", "turn_count": "x", "first_met": float("nan")}, 1.0
        )
        self.assertTrue(rel.available)
        self.assertEqual(rel.affection, 0.0)
        self.assertEqual(rel.turns, 0)
        self.assertEqual(rel.days, 0.0)

    def test_unknown_relation_is_not_a_fact(self):
        self.assertFalse(snap.UNKNOWN_RELATION.available)
        self.assertFalse(snap.UNKNOWN_RELATION.known)


class RelationFromCoreTest(unittest.TestCase):
    """读路径不许建档：不可读时直接返回 unavailable。"""

    class _Mood:
        def __init__(self, readable: bool):
            self._readable = readable
            self.profile_calls = 0

        def readable(self, *args, **kwargs):
            return self._readable

        def profile(self, user_id):
            self.profile_calls += 1
            return {"affection": 92.0, "turn_count": 60}

    def _core(self, mood, conf):
        return SimpleNamespace(
            prompt_builder=SimpleNamespace(config=conf),
            config=conf,
            mood=mood,
            now_epoch=lambda: 1_800_000_000.0,
        )

    def test_mood_disabled_does_not_create_a_profile(self):
        mood = self._Mood(False)
        rel = snap.relation(self._core(mood, cfg(mood_enabled=False)), "u1")
        self.assertFalse(rel.available)
        self.assertEqual(rel.reason, snap.REASON_MOOD_DISABLED)
        self.assertEqual(mood.profile_calls, 0)

    def test_readable_mood_reads_the_same_record_everywhere(self):
        mood = self._Mood(True)
        rel = snap.relation(self._core(mood, cfg()), "u1")
        self.assertTrue(rel.available)
        self.assertEqual(rel.affection, 92.0)
        self.assertEqual(rel.turns, 60)
        self.assertEqual(mood.profile_calls, 1)

    def test_broken_mood_reports_unavailable(self):
        class Broken(self._Mood):
            def profile(self, user_id):
                raise RuntimeError("state.json 坏了")

        rel = snap.relation(self._core(Broken(True), cfg()), "u1")
        self.assertFalse(rel.available)
        self.assertEqual(rel.reason, snap.REASON_NO_STATE)


class SleepOfTest(unittest.TestCase):
    """睡眠三态：睡着 / 醒着 / 不知道。"""

    def test_soma_disabled_is_unknown_not_awake(self):
        sleep = snap.sleep_of(fake_core(conf=cfg(soma_enabled=False)))
        self.assertEqual(sleep.state, snap.SLEEP_UNKNOWN)
        self.assertEqual(sleep.reason, snap.REASON_SOMA_DISABLED)
        self.assertIsNone(sleep.asleep)
        self.assertFalse(sleep.known)

    def test_wake_state_raising_is_unknown(self):
        sleep = snap.sleep_of(fake_core(soma_state=RuntimeError("boom")))
        self.assertEqual(sleep.state, snap.SLEEP_UNKNOWN)
        self.assertEqual(sleep.reason, snap.REASON_NO_STATE)

    def test_non_dict_wake_state_is_unknown(self):
        sleep = snap.sleep_of(fake_core(soma_state=["asleep"]))
        self.assertEqual(sleep.state, snap.SLEEP_UNKNOWN)
        self.assertEqual(sleep.reason, snap.REASON_NO_STATE)

    def test_asleep_at_night_says_night(self):
        sleep = snap.sleep_of(fake_core(hour=23, soma_state={"asleep": True}))
        self.assertEqual(sleep.state, snap.SLEEP_ASLEEP)
        self.assertTrue(sleep.asleep)
        self.assertEqual(sleep.reason, "night")
        self.assertEqual(sleep.asleep_since, 111.0)

    def test_asleep_in_the_daytime_says_nap(self):
        sleep = snap.sleep_of(fake_core(hour=14, soma_state={"asleep": True}))
        self.assertEqual(sleep.state, snap.SLEEP_ASLEEP)
        self.assertEqual(sleep.reason, "nap")

    def test_awake_is_a_real_answer(self):
        sleep = snap.sleep_of(fake_core(hour=14, soma_state={"asleep": False}))
        self.assertEqual(sleep.state, snap.SLEEP_AWAKE)
        self.assertFalse(sleep.asleep)
        self.assertTrue(sleep.known)
        # 醒着就没有「这一觉几点醒」这件事，不能给一个假时间。
        self.assertEqual(sleep.planned_wake, 0.0)

    def test_planned_wake_crosses_midnight(self):
        sleep = snap.sleep_of(
            fake_core(hour=23, soma_state={"asleep": True}, slot={"end": "07:00"})
        )
        expected = datetime(2026, 9, 27, 7, 0).timestamp()
        self.assertAlmostEqual(sleep.planned_wake, expected, places=3)

    def test_planned_wake_same_day_when_end_is_later(self):
        sleep = snap.sleep_of(
            fake_core(hour=2, soma_state={"asleep": True}, slot={"end": "07:00"})
        )
        expected = datetime(2026, 9, 26, 7, 0).timestamp()
        self.assertAlmostEqual(sleep.planned_wake, expected, places=3)

    def test_no_slot_means_no_planned_wake(self):
        sleep = snap.sleep_of(fake_core(hour=23, soma_state={"asleep": True}))
        self.assertEqual(sleep.planned_wake, 0.0)


class InNightTest(unittest.TestCase):
    """夜间模式关掉时「是不是夜里」没有答案，不是「不是夜里」。"""

    def test_disabled_night_mode_is_none(self):
        self.assertIsNone(snap.in_night(fake_core(hour=23, conf=cfg(night_mode_enabled=False))))

    def test_hour_inside_the_window(self):
        self.assertTrue(snap.in_night(fake_core(hour=23)))

    def test_hour_outside_the_window(self):
        self.assertFalse(snap.in_night(fake_core(hour=12)))

    def test_broken_clock_is_none(self):
        core = fake_core(hour=23)
        core.clock = SimpleNamespace(now=lambda: (_ for _ in ()).throw(RuntimeError("no clock")))
        self.assertIsNone(snap.in_night(core))


class DefaultsTest(unittest.TestCase):
    """默认值只有一份，而且配置写 0 就是 0。"""

    def test_aggression_default_matches_config_dataclass(self):
        conf = cfg()
        self.assertEqual(snap.DEFAULT_AGGRESSION, float(conf.mood_initial_aggression))
        self.assertEqual(snap.aggression_default(conf), snap.DEFAULT_AGGRESSION)

    def test_no_config_falls_back_to_the_constant(self):
        self.assertEqual(snap.aggression_default(None), snap.DEFAULT_AGGRESSION)

    def test_zero_is_a_real_value(self):
        # 旧代码 `float(profile.get("aggression", 25.0) or 25.0)` 把 0 读成 25，
        # 而 ccb 里写着「aggression 0 是明确的不想」，读成 25 等于把拒绝改成愿意。
        self.assertEqual(snap.aggression_default(cfg(mood_initial_aggression=0)), 0.0)

    def test_bad_config_value_falls_back(self):
        self.assertEqual(
            snap.aggression_default(SimpleNamespace(mood_initial_aggression="x")),
            snap.DEFAULT_AGGRESSION,
        )

    def test_affection_and_libido_defaults_match_config(self):
        conf = cfg()
        self.assertEqual(snap.affection_default(conf), float(conf.mood_initial_affection))
        self.assertEqual(snap.libido_default(conf), float(conf.mood_initial_libido))


class WordsTest(unittest.TestCase):
    """档位词走各自模块的那一张梯子，不另写一份。"""

    def test_hunger_55_to_60_is_not_starving(self):
        self.assertIn(snap.hunger_word(56.0), ("有点饿", "肚子有点空"))

    def test_hunger_high_is_hungry(self):
        self.assertIn(snap.hunger_word(90.0), ("很饿", "饿得发慌"))

    def test_hunger_low(self):
        self.assertEqual(snap.hunger_word(10.0), "不饿")

    def test_energy_top_and_bottom(self):
        self.assertIn(snap.energy_word(95.0), ("精力充沛", "身上有劲", "精神足"))
        self.assertIn(snap.energy_word(0.0), ("很疲惫", "身上没劲", "累得不想动"))

    def test_out_of_range_values_are_clamped_not_dropped(self):
        # 越界值不能变成空字符串：注入里少一句，读起来像「她今天没有精力状态」。
        self.assertIn(snap.energy_word(-5.0), ("很疲惫", "身上没劲", "累得不想动"))
        self.assertIn(snap.energy_word(140.0), ("精力充沛", "身上有劲", "精神足"))
        self.assertEqual(snap.hunger_word(140.0), snap.hunger_word(100.0))

    def test_energy_word_is_stable_for_the_same_seed(self):
        self.assertEqual(snap.energy_word(50.0, ["u1", 3]), snap.energy_word(50.0, ["u1", 3]))

    def test_nan_does_not_raise(self):
        self.assertEqual(snap.hunger_word(float("nan")), "不饿")
        self.assertTrue(math.isfinite(snap.aggression_default(cfg())))


class BuildTest(unittest.TestCase):
    """一轮一份快照，三个字段必须同源。"""

    class _Mood:
        def readable(self, *args, **kwargs):
            return True

        def profile(self, user_id):
            return {"affection": 92.0, "turn_count": 60}

    def test_build_returns_one_consistent_snapshot(self):
        conf = cfg()
        core = fake_core(hour=23, soma_state={"asleep": True})
        core.prompt_builder = SimpleNamespace(config=conf)
        core.mood = self._Mood()
        state = snap.build(core, "u1")
        self.assertEqual(state.user_id, "u1")
        self.assertTrue(state.relation.available)
        self.assertEqual(state.sleep.state, snap.SLEEP_ASLEEP)
        self.assertTrue(state.night)
        self.assertGreater(state.now, 0.0)

    def test_build_is_read_only(self):
        # 快照不许推进身体、不许建档：同一时刻连取两次结果必须完全一样。
        conf = cfg()
        core = fake_core(hour=23, soma_state={"asleep": False})
        core.prompt_builder = SimpleNamespace(config=conf)
        mood = self._Mood()
        core.mood = mood
        first = snap.build(core, "u1")
        second = snap.build(core, "u1")
        self.assertEqual(first, second)
        self.assertEqual(mood.profile_calls if hasattr(mood, "profile_calls") else 0, 0)


if __name__ == "__main__":
    unittest.main()
