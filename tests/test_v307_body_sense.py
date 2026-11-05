"""P1 体感层：把身体参数翻成感觉，并守住「不写器官名 / 不写读数 / 不写档位名」。

用户原话（m03902）：「亲密体感要写，但不要说得太直接」。所以这一层只做两件事：

* 把 ccb 里已经存着的标量（阶段 / 满足度 / 姿势 / 位置 / 估算深度 / 上限 /
  敏感度 / 体力 / 关系阶段）翻成身体能感觉到的话；
* 一条读数、一个档位名、一个器官名都不许出现——那是面板与门面的语言。

这里的用例分两类。**翻词表**：直接构造 `Sense` 打边界，不搭 core；**读路径**：
用真的 core 确认 `collect()` 纯读（不建档、不落盘），以及「拿不到就不说话」。
"""

from __future__ import annotations

import json
import re
import unittest

from humanoid import body_sense, ccb
from humanoid.config import HumanoidConfig

from .test_v2287_ccb_presence import _core, _in_scene, _mood, _state

#: 器官名一个都不许出现（用户明确要求「不要太直接」，本层只到非解剖部位）。
BANNED_ORGANS = (
    "肉穴", "后庭", "肉棒", "阴茎", "阴道", "子宫", "龟头", "乳头", "阴蒂",
    "前列腺", "乳房",
)
#: 读数、百分比、cm 都不进注入：模型会把它们当绩效指标去解释。
BANNED_NUMBERS = re.compile(r"[0-9０-９]|cm|厘米")
#: 状态机的档位名（面板用）不许漏进感觉层。
BANNED_STAGES = tuple(name for name in ccb.PHASE_NAMES[1:] if name)


def _sense(**kw) -> body_sense.Sense:
    """一份默认「在场景里、关系已深、体力正常」的体感来源，按需覆盖。"""
    base = dict(
        now=1000.0, phase=2, stage=2.0, satisfy=50.0, pose=0, site=1,
        depth_cm=10.0, cap_cm=14.0, sensitivity=60.0, energy=70.0,
        rel_stage=2, activity_at=940.0, aftercare_until=0.0,
    )
    base.update(kw)
    return body_sense.Sense(**base)


class FullnessTest(unittest.TestCase):
    """充盈：估深 / 上限的比例分档；过深单独一句。"""

    def test_no_depth_says_nothing(self):
        self.assertEqual(body_sense._fullness(_sense(depth_cm=0.0)), "")

    def test_over_the_cap_is_its_own_phrase(self):
        self.assertEqual(
            body_sense._fullness(_sense(depth_cm=15.0, cap_cm=14.0)),
            "被撑得发胀，深处有点酸",
        )

    def test_exactly_at_the_cap_is_full(self):
        self.assertEqual(
            body_sense._fullness(_sense(depth_cm=14.0, cap_cm=14.0)),
            "填得很满，胀得发酸",
        )

    def test_each_ratio_band_is_reachable(self):
        cases = (
            (12.0, "顶得有点深，腰有点软"),
            (9.0, "填得满满的，有点胀"),
            (5.0, "有点胀，还没完全适应"),
        )
        for depth, expected in cases:
            self.assertEqual(
                body_sense._fullness(_sense(depth_cm=depth, cap_cm=14.0)),
                expected,
                f"depth={depth}",
            )

    def test_zero_cap_does_not_divide_by_zero(self):
        # 上限读成 0（坏配置/坏存档）时不能抛，也不能说「过深」。
        self.assertEqual(
            body_sense._fullness(_sense(depth_cm=9.0, cap_cm=0.0)),
            "有点胀，还没完全适应",
        )


class AngleTest(unittest.TestCase):
    def test_pose_and_site_both_speak(self):
        self.assertEqual(
            body_sense._angle(_sense(pose=0, site=1)),
            "背后贴着，腿有点合不拢，后面胀得有点慌",
        )

    def test_unknown_pose_and_site_say_nothing(self):
        self.assertEqual(body_sense._angle(_sense(pose=-1, site=-1)), "")

    def test_every_known_pose_has_a_phrase(self):
        for pose in range(len(ccb.POSE_NAMES)):
            self.assertTrue(
                body_sense._angle(_sense(pose=pose, site=-1)),
                f"姿势 {ccb.POSE_NAMES[pose]} 没有对应的感觉",
            )


class PaceTest(unittest.TestCase):
    def test_three_bands(self):
        cases = (
            (30.0, "动静还没停，心跳得厉害"),
            (120.0, "刚缓下来一点，还在发颤"),
            (600.0, "缓下来了，身上还热着"),
        )
        for gap, expected in cases:
            self.assertEqual(
                body_sense._pace(_sense(now=1000.0, activity_at=1000.0 - gap)),
                expected,
                f"gap={gap}",
            )

    def test_no_activity_or_no_scene_says_nothing(self):
        self.assertEqual(body_sense._pace(_sense(activity_at=0.0)), "")
        self.assertEqual(body_sense._pace(_sense(stage=0.0)), "")

    def test_clock_rolled_back_says_nothing(self):
        # 存档里的 activity_at 在未来（时钟回拨）时不许算出负的间隔。
        self.assertEqual(body_sense._pace(_sense(now=1000.0, activity_at=1200.0)), "")


class SensitivityTest(unittest.TestCase):
    def test_bands(self):
        cases = (
            (0.0, "身上钝钝的，不太有感觉"),
            (85.0, "一碰就发麻"),
            (60.0, "碰一下就缩"),
            (30.0, "比平时敏感"),
            (20.0, ""),
        )
        for value, expected in cases:
            self.assertEqual(
                body_sense._sensitive(_sense(sensitivity=value)), expected, f"v={value}"
            )


class StaminaTest(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(body_sense._stamina(_sense(energy=20.0)), "腿在抖，有点撑不住")
        self.assertEqual(
            body_sense._stamina(_sense(energy=40.0)), "腰背有点酸，力气在往下掉"
        )
        self.assertEqual(body_sense._stamina(_sense(energy=70.0)), "")


class ShameAndAttachmentTest(unittest.TestCase):
    def test_shame_only_while_the_relation_is_shallow(self):
        self.assertEqual(body_sense._shame(_sense(rel_stage=0)), "脸有点烧，不太敢看")
        self.assertEqual(body_sense._shame(_sense(rel_stage=2)), "")
        self.assertEqual(body_sense._shame(_sense(rel_stage=0, pose=-1, site=-1)), "")

    def test_attachment_only_when_deep_and_late(self):
        self.assertEqual(
            body_sense._attachment(_sense(rel_stage=2, phase=3)),
            "想被抱紧一点，不想松开",
        )
        self.assertEqual(body_sense._attachment(_sense(rel_stage=1, phase=3)), "")
        self.assertEqual(body_sense._attachment(_sense(rel_stage=2, phase=2)), "")


class FeelingsTest(unittest.TestCase):
    def test_limit_is_respected(self):
        sense = _sense(sensitivity=90.0, energy=20.0)
        self.assertEqual(len(body_sense.feelings(sense, limit=2)), 2)
        self.assertEqual(
            len(body_sense.feelings(sense, limit=99)),
            len(body_sense.feelings(sense, limit=0)),
        )

    def test_order_starts_with_fullness(self):
        out = body_sense.feelings(_sense(sensitivity=90.0, energy=20.0))
        self.assertTrue(out[0].startswith(("被撑得", "填得", "顶得", "有点胀")), out)

    def test_not_in_scene_or_no_sense_says_nothing(self):
        self.assertEqual(body_sense.feelings(_sense(stage=0.0)), [])
        self.assertEqual(body_sense.feelings(None), [])

    def test_phrases_are_deduplicated(self):
        out = body_sense.feelings(_sense(sensitivity=90.0, energy=20.0), limit=0)
        self.assertEqual(len(out), len(set(out)))


class AftercareTest(unittest.TestCase):
    def test_three_bands_and_boundaries(self):
        cases = (
            (7000.0, body_sense._AFTERCARE_EARLY),
            (3601.0, body_sense._AFTERCARE_EARLY),
            (3600.0, body_sense._AFTERCARE_MID),
            (901.0, body_sense._AFTERCARE_MID),
            (900.0, body_sense._AFTERCARE_LATE),
            (1.0, body_sense._AFTERCARE_LATE),
        )
        for left, expected in cases:
            sense = _sense(now=1000.0, aftercare_until=1000.0 + left)
            self.assertEqual(body_sense.aftercare(sense), expected, f"left={left}")

    def test_window_over_or_missing_says_nothing(self):
        self.assertEqual(body_sense.aftercare(_sense(now=1000.0, aftercare_until=0.0)), "")
        self.assertEqual(body_sense.aftercare(_sense(now=1000.0, aftercare_until=999.0)), "")
        self.assertEqual(body_sense.aftercare(None), "")


class CycleLineTest(unittest.TestCase):
    def test_every_phase_is_reachable(self):
        core = _core()
        cfg = HumanoidConfig.from_raw({})
        by_phase = {}
        seen = {}
        for day in range(1, 29):
            core._scope.update_self(current_cycle_day=day)
            line = body_sense.cycle_line(core)
            self.assertTrue(line, f"第 {day} 天没有周期感觉")
            self.assertTrue(line.startswith("我"), "周期句必须自带主语「我」")
            seen.setdefault(line, []).append(day)
            by_phase.setdefault(cfg.cycle_phase_index(day), set()).add(line)
        # 8 个阶段各至少一句；跨多天的阶段按天轮换，所以候选句总数可以多于 8。
        self.assertEqual(len(by_phase), 8, f"8 个阶段应各有一句，实得 {sorted(by_phase)}")
        self.assertGreaterEqual(len(seen), 8)
        rotated = [p for p, lines in by_phase.items() if len(lines) > 1]
        self.assertTrue(rotated, "没有任何一个阶段按天换措辞：月经期会五天一字不变")

    def test_the_same_day_keeps_one_wording(self):
        """同一天里所有请求必须拿到同一句：一轮一个说法比重复更糟。"""
        core = _core()
        core._scope.update_self(current_cycle_day=10)
        self.assertEqual(
            body_sense.cycle_line(core), body_sense.cycle_line(core)
        )

    def test_disabled_cycle_says_nothing(self):
        core = _core(enable_cycle=False)
        core._scope.update_self(current_cycle_day=3)
        self.assertEqual(body_sense.cycle_line(core), "")

    def test_bad_cycle_day_says_nothing_instead_of_raising(self):
        core = _core()
        core._scope.update_self(current_cycle_day="不是数字")
        self.assertEqual(body_sense.cycle_line(core), "")

    def test_cycle_lines_never_use_the_third_person(self):
        core = _core()
        for day in range(1, 29):
            core._scope.update_self(current_cycle_day=day)
            self.assertNotIn("她", body_sense.cycle_line(core),
                             "周期句自带主语，不该等 to_first_person 去换")


class NoLeakGuardTest(unittest.TestCase):
    """三条边界：器官名、读数、档位名，一个都不许出现在任何一句里。"""

    def _all_phrases(self):
        out = []
        for pose in list(range(len(ccb.POSE_NAMES))) + [-1]:
            for site in (0, 1, -1):
                for phase in range(len(ccb.PHASE_NAMES)):
                    for sensitivity in (0.0, 30.0, 60.0, 90.0):
                        for energy in (10.0, 40.0, 80.0):
                            for rel_stage in (0, 2):
                                sense = _sense(
                                    pose=pose, site=site, phase=phase,
                                    sensitivity=sensitivity, energy=energy,
                                    rel_stage=rel_stage, depth_cm=15.0,
                                )
                                out.extend(body_sense.feelings(sense, limit=0))
        out.extend((
            body_sense._AFTERCARE_EARLY,
            body_sense._AFTERCARE_MID,
            body_sense._AFTERCARE_LATE,
        ))
        out.extend(body_sense._DELTA_PHRASES)
        core = _core()
        for day in range(1, 29):
            core._scope.update_self(current_cycle_day=day)
            out.append(body_sense.cycle_line(core))
        return [text for text in out if text]

    def test_no_organ_words(self):
        bad = [
            text for text in self._all_phrases()
            if any(word in text for word in BANNED_ORGANS)
        ]
        self.assertEqual(bad, [])

    def test_no_numbers_or_units(self):
        bad = [text for text in self._all_phrases() if BANNED_NUMBERS.search(text)]
        self.assertEqual(bad, [])

    def test_no_stage_names(self):
        bad = [
            text for text in self._all_phrases()
            if any(name in text for name in BANNED_STAGES)
        ]
        self.assertEqual(bad, [])

    def test_no_imperatives(self):
        # 不写动作指令：这一层只说「身上什么样」，不说「该做什么」。
        bad = [
            text for text in self._all_phrases()
            if any(word in text for word in ("要她", "让她", "该", "必须", "请"))
        ]
        self.assertEqual(bad, [])


class CollectIsReadOnlyTest(unittest.TestCase):
    """`collect()` 每轮都跑，绝不能在读路径上建档或落盘。"""

    def test_it_does_not_create_a_profile_or_a_body_record(self):
        core = _core()
        _mood(core)
        state = _state(core)
        before = json.dumps(state, ensure_ascii=False, sort_keys=True)
        sense = body_sense.collect(core, "u1")
        self.assertIsNotNone(sense)
        after = json.dumps(_state(core), ensure_ascii=False, sort_keys=True)
        self.assertEqual(before, after, "collect() 改了用户状态")
        self.assertNotIn(ccb.BODY_LENGTH_KEY, state, "collect() 建了伴侣档案")
        self.assertNotIn(
            ccb.BODY_KEY, getattr(core._scope, "self_state", {}),
            "collect() 建了身体档案",
        )

    def test_unknown_user_says_nothing_and_creates_nothing(self):
        core = _core()
        self.assertIsNone(body_sense.collect(core, "nobody"))
        self.assertIsNone(
            core._scope.peek_user("nobody", "mood"),
            "读体感给陌生人建了情绪档案",
        )

    def test_it_reads_the_scene_values_it_was_given(self):
        core = _core()
        _mood(core)
        _in_scene(core, stage=2.0, satisfy=45.0)
        state = _state(core)
        state["ccb_activity_at"] = core.now_epoch() - 30.0
        sense = body_sense.collect(core, "u1")
        self.assertEqual(sense.stage, 2.0)
        self.assertEqual(sense.pose, 0)
        self.assertEqual(sense.site, 1)
        self.assertGreater(sense.depth_cm, 0.0)
        self.assertGreater(sense.cap_cm, 0.0)
        self.assertEqual(body_sense._pace(sense), "动静还没停，心跳得厉害")


class DeltaTest(unittest.TestCase):
    """「比刚才…」只在真的变了的时候说一句，方向只报最明显的那个。"""

    def test_first_turn_has_nothing_to_compare(self):
        self.assertEqual(body_sense.delta(_sense(), None), "")

    def test_entering_the_scene_is_not_a_change(self):
        # 上一轮不在场景里：状态句刚说完「正在亲密里」，再比「比刚才更…」没有对象。
        self.assertEqual(body_sense.delta(_sense(), {"stage": 0.0, "phase": 0}), "")

    def test_leaving_the_scene_says_nothing(self):
        self.assertEqual(
            body_sense.delta(_sense(stage=0.0), {"stage": 2.0, "phase": 2}),
            "",
        )

    def test_broken_history_is_not_a_change(self):
        for bad in ("x", [], 3, {"phase": "??"}):
            self.assertEqual(body_sense.delta(_sense(), bad), "")

    def test_a_higher_phase_is_reported_first(self):
        prev = {"stage": 2.0, "phase": 1, "depth_cm": 10.0, "satisfy": 40.0, "energy": 90.0}
        self.assertEqual(body_sense.delta(_sense(phase=2), prev), body_sense._DELTA_HARDER)

    def test_a_surge_in_satisfaction(self):
        prev = {"stage": 2.0, "phase": 2, "depth_cm": 10.0, "satisfy": 40.0, "energy": 70.0}
        self.assertEqual(body_sense.delta(_sense(satisfy=52.0), prev), body_sense._DELTA_SURGE)

    def test_a_deeper_push(self):
        prev = {"stage": 2.0, "phase": 2, "depth_cm": 10.0, "satisfy": 50.0, "energy": 70.0}
        self.assertEqual(
            body_sense.delta(_sense(depth_cm=10.7), prev), body_sense._DELTA_DEEPER
        )

    def test_losing_strength(self):
        prev = {"stage": 2.0, "phase": 2, "depth_cm": 10.0, "satisfy": 50.0, "energy": 70.0}
        self.assertEqual(
            body_sense.delta(_sense(energy=60.0), prev), body_sense._DELTA_TIRED
        )

    def test_easing_off_and_withdrawing(self):
        prev = {"stage": 2.0, "phase": 2, "depth_cm": 10.0, "satisfy": 80.0, "energy": 70.0}
        self.assertEqual(
            body_sense.delta(_sense(satisfy=60.0), prev), body_sense._DELTA_EASING
        )
        prev = {"stage": 2.0, "phase": 2, "depth_cm": 12.0, "satisfy": 50.0, "energy": 70.0}
        self.assertEqual(
            body_sense.delta(_sense(depth_cm=10.0), prev), body_sense._DELTA_WITHDRAW
        )

    def test_small_wobbles_are_not_reported(self):
        # 阈值以内一律不说话：参数抖一点就报「比刚才更…」，读起来像卡顿的仪表盘。
        prev = {"stage": 2.0, "phase": 2, "depth_cm": 10.0, "satisfy": 50.0, "energy": 70.0}
        sense = _sense(depth_cm=10.4, satisfy=55.0, energy=66.0)
        self.assertEqual(body_sense.delta(sense, prev), "")


class HistoryTest(unittest.TestCase):
    """上一轮快照：写进去、值没变不写、读不建档。"""

    def test_remember_writes_only_the_five_numbers(self):
        core = _core()
        _mood(core)
        state = _state(core)
        body_sense.remember(core, "u1", _sense())
        self.assertEqual(
            sorted(state[body_sense.HISTORY_KEY]),
            ["depth_cm", "energy", "phase", "satisfy", "stage"],
        )

    def test_remember_does_not_touch_disk_when_nothing_changed(self):
        from unittest import mock

        core = _core()
        _mood(core)
        _state(core)
        body_sense.remember(core, "u1", _sense())
        with mock.patch.object(core._scope, "mark_dirty") as dirty:
            body_sense.remember(core, "u1", _sense())
            dirty.assert_not_called()
            body_sense.remember(core, "u1", _sense(satisfy=90.0))
            dirty.assert_called_once()

    def test_previous_is_read_only(self):
        core = _core()
        self.assertIsNone(body_sense.previous(core, "nobody"))
        self.assertIsNone(
            core._scope.peek_user("nobody", "mood"),
            "读上一轮快照给陌生人建了档案",
        )

    def test_broken_history_reads_as_none(self):
        core = _core()
        _mood(core)
        _state(core)["body_sense"] = "not a dict"
        self.assertIsNone(body_sense.previous(core, "u1"))


class PresenceWiringTest(unittest.TestCase):
    """接进场景句之后的行为：同状态两轮逐字相同，变了才多一句。"""

    def _line(self, core):
        return core.prompt_builder._ccb_presence_line("u1")

    def test_the_same_state_twice_renders_identically(self):
        core = _core()
        _mood(core)
        state = _in_scene(core, stage=2.0, satisfy=45.0)
        state["ccb_activity_at"] = core.now_epoch() - 600.0
        first = self._line(core)
        self.assertEqual(self._line(core), first)
        self.assertNotIn("比刚才", first, "第一轮没有可比的对象")

    def test_a_change_shows_up_as_one_short_clause(self):
        core = _core()
        _mood(core)
        state = _in_scene(core, stage=2.0, satisfy=45.0)
        state["ccb_activity_at"] = core.now_epoch() - 600.0
        self._line(core)
        state["ccb_satisfy"] = 90.0
        line = self._line(core)
        self.assertIn("；", line)
        self.assertIn(body_sense._DELTA_HARDER, line)
        self.assertEqual(line.count("比刚才"), 1, "变化句只报一个方向")

    def test_leaving_the_scene_stops_reporting_changes(self):
        core = _core()
        _mood(core)
        state = _in_scene(core, stage=2.0, satisfy=45.0)
        self._line(core)
        state["ccb_stage"] = 0.0
        state["ccb_aftercare_until"] = core.now_epoch() + 7000.0
        line = self._line(core)
        self.assertIn("身上还热着", line)
        self.assertNotIn("比刚才", line)
        self.assertEqual(
            state[body_sense.HISTORY_KEY]["stage"], 0.0,
            "退出场景也要记一笔，否则下一场会拿上一场的数据去比",
        )


if __name__ == "__main__":
    unittest.main()
