"""v3.1.32：预算被别的用途吃光时，日程生成还有自己那份保留额。

线上形态（2026-11-10 23:28）：情绪分析把 200 次/天的额度用到 200/200，之后每次日程
生成都被「今日预算已用完」挡回，六个角色一整天只剩本地兜底段（「睡一觉」「把床单被套
换下来洗了」）。保留额只在总预算用完那一刻才起作用，平时不设限。
"""
from __future__ import annotations

import unittest

from humanoid.llm import (
    PURPOSE_MOOD,
    PURPOSE_SCHEDULE,
    SCHEDULE_RESERVE_MIN,
    SCHEDULE_RESERVE_SHARE,
    CallGate,
)

from .fakes import FakeClock
from .test_llm import cfg

BUDGET = 40


class BudgetReserveTest(unittest.TestCase):
    def build(self):
        conf = cfg(
            llm_daily_call_budget=BUDGET,
            # 只测预算这一条：空闲静默与最小间隔都关掉，别让它们先挡。
            llm_idle_silence_minutes=0,
            schedule_min_interval_minutes=0,
            schedule_max_interval_minutes=0,
        )
        wall = FakeClock(0.0)
        mono = FakeClock(0.0)
        return CallGate(lambda: conf, wall_clock=wall, monotonic=mono)

    def _spend(self, gate, purpose, times):
        for _ in range(times):
            gate.consume(purpose)

    def test_schedule_still_runs_after_the_shared_budget_is_gone(self):
        gate = self.build()
        self._spend(gate, PURPOSE_MOOD, BUDGET)
        verdict = gate.check(PURPOSE_SCHEDULE)
        self.assertTrue(verdict.allowed, verdict.describe())

    def test_the_reserve_is_finite(self):
        gate = self.build()
        self._spend(gate, PURPOSE_MOOD, BUDGET)
        self._spend(gate, PURPOSE_SCHEDULE, SCHEDULE_RESERVE_MIN)
        verdict = gate.check(PURPOSE_SCHEDULE)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "budget")

    def test_mood_does_not_eat_the_reserve(self):
        gate = self.build()
        self._spend(gate, PURPOSE_MOOD, BUDGET)
        verdict = gate.check(PURPOSE_MOOD)
        self.assertFalse(verdict.allowed, "情绪分析不许用日程的保留额")

    def test_under_budget_nothing_changes(self):
        gate = self.build()
        self.assertTrue(gate.check(PURPOSE_SCHEDULE).allowed)
        self.assertTrue(gate.check(PURPOSE_MOOD).allowed)

    def test_reserve_math(self):
        gate = self.build()
        self.assertEqual(
            gate._schedule_reserve(BUDGET),
            min(BUDGET, max(SCHEDULE_RESERVE_MIN, int(BUDGET * SCHEDULE_RESERVE_SHARE))),
        )
        self.assertEqual(gate._schedule_reserve(0), 0, "不限量时没有保留额这回事")

    def test_snapshot_exposes_the_reserve(self):
        gate = self.build()
        snap = gate.snapshot()
        self.assertGreater(snap["schedule_reserve"], 0)
        self.assertEqual(snap["schedule_reserve"], gate._schedule_reserve(snap["budget"]))
