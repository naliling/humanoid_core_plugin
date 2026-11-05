"""习惯层：从过去几周的日程里长出「她平时怎么过」。

用户原话（m07427）：「没有从她过去几周日程长出来的习惯。这个我觉得需要」。

守四件事：

1. 跨天必须归档，否则只有今天 + 昨天，习惯层永远是空的；
2. 归档只留 28 天，state 不能无限长；
3. 样本不足、或只是巧合时不说话——宁可没有，也不要拿三天冒充习惯；
4. 习惯只进 prompt 当背景，标题里明写「不是今天必须照做」。
"""

from __future__ import annotations

import unittest

from humanoid.services import habit

from .test_v306_injection_golden import _core


def _day(rows):
    return rows


class CompactTest(unittest.TestCase):
    def test_keeps_four_columns(self):
        rows = habit.compact_slots(
            [{"start": "10:00", "end": "11:30", "event": "整理笔记", "location": "书桌"}]
        )
        self.assertEqual(rows, [[600, 690, "整理笔记", "书桌"]])

    def test_drops_broken_rows(self):
        rows = habit.compact_slots(
            [
                {"start": "bad", "end": "11:00"},
                {"start": "12:00", "end": "11:00"},
                "not a slot",
                {"start": "13:00", "end": "13:30", "event": "喝水", "location": ""},
            ]
        )
        self.assertEqual(rows, [[780, 810, "喝水", ""]])

    def test_caps_long_text(self):
        rows = habit.compact_slots(
            [{"start": "10:00", "end": "11:00", "event": "很" * 60, "location": "地" * 40}]
        )
        self.assertEqual(len(rows[0][2]), 24)
        self.assertEqual(len(rows[0][3]), 16)


class _Scope:
    """最小 scope：只要 get_self / update_self 两个方法。"""

    def __init__(self, data=None):
        self.data = dict(data or {})

    def get_self(self, key, default=None):
        return self.data.get(key, default)

    def update_self(self, **kwargs):
        self.data.update(kwargs)


class _BrokenScope:
    def get_self(self, key, default=None):
        raise RuntimeError("state 读不了")

    def update_self(self, **kwargs):
        raise RuntimeError("state 写不了")


class ArchiveTest(unittest.TestCase):
    def test_it_keeps_the_last_28_days(self):
        scope = _Scope()
        for day in range(1, 41):
            habit.archive_day(
                scope, "2026-01-%02d" % day if day <= 31 else "2026-02-%02d" % (day - 31),
                [{"start": "10:00", "end": "11:00", "event": "整理笔记", "location": "书桌"}],
            )
        history = habit.history_of(scope)
        self.assertEqual(len(history), habit.HISTORY_KEEP_DAYS)
        self.assertNotIn("2026-01-01", history)

    def test_same_day_written_twice_is_one_entry(self):
        scope = _Scope()
        for _ in range(3):
            habit.archive_day(
                scope, "2026-01-05",
                [{"start": "10:00", "end": "11:00", "event": "整理笔记", "location": "书桌"}],
            )
        self.assertEqual(len(habit.history_of(scope)), 1)

    def test_a_broken_scope_does_not_raise(self):
        habit.archive_day(_BrokenScope(), "2026-01-05", [{"start": "10:00", "end": "11:00"}])

    def test_empty_day_is_not_archived(self):
        scope = _Scope()
        habit.archive_day(scope, "2026-01-05", [])
        self.assertEqual(habit.history_of(scope), {})


def _history(days, rows):
    return {"2026-01-%02d" % (index + 1): rows for index in range(days)}


class BucketLineTest(unittest.TestCase):
    def test_not_enough_days_says_nothing(self):
        history = _history(4, [[600, 660, "整理笔记", "书桌"]])
        self.assertEqual(habit.habit_lines(history, 630), [])

    def test_a_repeated_event_at_this_hour_is_said(self):
        history = _history(10, [[600, 660, "整理笔记", "书桌"]])
        lines = habit.habit_lines(history, 630)
        self.assertTrue(any("整理笔记" in line for line in lines), lines)

    def test_an_occasional_event_is_not_a_habit(self):
        history = _history(10, [[600, 660, "整理笔记", "书桌"]])
        history["2026-01-02"] = [[600, 660, "去看展", "美术馆"]]
        lines = habit.habit_lines(history, 630)
        self.assertFalse(any("美术馆" in line for line in lines), lines)

    def test_sleep_is_never_a_habit_line(self):
        history = _history(10, [[1380, 1440, "睡眠", "卧室"]])
        lines = habit.habit_lines(history, 1400)
        self.assertFalse(any("睡眠" in line for line in lines), lines)

    def test_another_hour_is_not_this_hour(self):
        history = _history(10, [[600, 660, "整理笔记", "书桌"]])
        lines = habit.habit_lines(history, 1200)
        self.assertFalse(any("整理笔记" in line for line in lines), lines)


class RoutineTest(unittest.TestCase):
    def test_a_stable_wake_time_is_said(self):
        rows = [[430, 480, "睡眠", "卧室"], [480, 540, "洗漱", "卫生间"]]
        lines = habit.habit_lines(_history(10, rows), 1200)
        self.assertTrue(any("起" in line for line in lines), lines)

    def test_a_scattered_wake_time_is_not_said(self):
        history = {}
        for index in range(10):
            history["2026-01-%02d" % (index + 1)] = [
                [300 + index * 50, 400 + index * 50, "醒着", "客厅"]
            ]
        lines = habit.habit_lines(history, 1200)
        self.assertFalse(any("起" in line for line in lines), lines)


class PromptBlockTest(unittest.TestCase):
    def _prompt(self, habits):
        from humanoid.services.schedule import segment_prompt
        from humanoid.config import HumanoidConfig
        return segment_prompt(
            HumanoidConfig.from_raw({}),
            now_text="10:30",
            weekday="一",
            habits=habits,
        )

    def test_habits_go_into_their_own_block(self):
        prompt = self._prompt("  - 她常在书桌整理笔记。")
        self.assertIn("【她这段时间的习惯】", prompt)
        self.assertIn("不是今天必须照做", prompt)

    def test_no_habits_means_no_block(self):
        self.assertNotIn("【她这段时间的习惯】", self._prompt(""))


class RolloverArchiveTest(unittest.TestCase):
    """真的跨天：昨天那份必须进归档，否则习惯层永远是空的。"""

    def test_yesterday_is_archived_when_the_day_rolls(self):
        core = _core()
        core.scope.update_self(
            today_date="2026-09-26",
            daily_schedule=[
                {"start": "10:00", "end": "11:00", "event": "整理笔记", "location": "书桌"}
            ],
        )
        core.schedule._archive_previous_day("2026-09-27")
        history = habit.history_of(core.scope)
        self.assertIn("2026-09-26", history)
        self.assertEqual(history["2026-09-26"], [[600, 660, "整理笔记", "书桌"]])

    def test_archiving_the_same_day_twice_keeps_one_copy(self):
        core = _core()
        core.scope.update_self(
            today_date="2026-09-26",
            daily_schedule=[
                {"start": "10:00", "end": "11:00", "event": "整理笔记", "location": "书桌"}
            ],
        )
        core.schedule._archive_previous_day("2026-09-27")
        core.schedule._archive_previous_day("2026-09-27")
        self.assertEqual(len(habit.history_of(core.scope)), 1)


if __name__ == "__main__":
    unittest.main()
