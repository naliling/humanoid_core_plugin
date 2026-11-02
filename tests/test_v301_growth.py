"""v3.0.1：数值只跌不涨的根因与回归。

用户反馈「好感度不会提升，所有数值都不会往好的方向走」。查下来是三处叠在一起：

1. 正向日额度 3.1、负向 4.5×1.2=5.4：正常聊一天最多涨 3.1，被冷一句就掉 1.2——
   涨得比掉慢一倍，长期必然往下走。现在正向 6.0，与负向对齐。
2. 数据保留默认 7 天：离开一周回来，情绪档案连同 base_affection 一起被清，
   好感回到面板初始值——攒多久都白攒。现在清档会留下关系基线。
3. 当日额度的「日」键只有负向按时钟算，正向直接读 clock.today_str()；没有时钟时
   正向记下的日期是空串，而空串永远等于空串，额度攒满一次就再也不重置。
   现在两边共用 `_day_key()`，取不到时钟就按账本时间兜底。

这里盯住「会涨」这件事本身，以及上面三条不许回退。
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from humanoid.config import HumanoidConfig
from humanoid.services import mood as M
from humanoid.services.mood import MoodService

from .fakes import RecordingLogger, ScopeStore

TZ = timezone(timedelta(hours=8))
WARM = ("喜欢你", "谢谢", "你真棒", "抱抱", "想你了", "你好可爱", "辛苦了", "陪我一会儿")


class _Clock:
    def __init__(self, dt):
        self.dt = dt

    def now(self):
        return self.dt

    def advance(self, minutes):
        self.dt = self.dt + timedelta(minutes=minutes)

    def today_str(self):
        return self.dt.strftime("%Y-%m-%d")


def _svc(start=datetime(2026, 8, 22, 9, 0, tzinfo=TZ), **over):
    conf = HumanoidConfig.from_raw(
        {"timezone_city": "北京", "mood_use_llm_for_delta": False, **over}
    )
    store = ScopeStore(Path(tempfile.mkdtemp()) / "s.json")
    store.load("2026-08-22", conf.cycle_length)
    clock = _Clock(start)
    svc = MoodService(
        store.scope, lambda: conf, clock,
        time_source=lambda: clock.now().timestamp(),
        logger=RecordingLogger(),
    )
    return svc, clock


class CapSymmetry(unittest.TestCase):
    """A：涨的额度不能比掉的额度小。"""

    def test_growth_is_not_throttled_harder_than_decline(self):
        self.assertGreaterEqual(
            M.DAILY_NET_GAIN_CAP,
            M.DAILY_NET_LOSS_CAP * M.LEDGER_NEGATIVE_AMPLIFY,
            "正向日额度又比负向小了：长期只会往下走",
        )

    def test_aggression_can_fall_as_fast_as_it_rises(self):
        self.assertGreaterEqual(M.AGGRESSION_DOWN_CAP, 1.0, "脾气降得比升得慢太多")


class DailyBudget(unittest.TestCase):
    """B：当日额度按自然日翻篇，不是一次用光就没了。"""

    def test_the_budget_rolls_over_on_the_next_day(self):
        svc, clock = _svc()
        svc.profile("1")
        for i in range(40):
            asyncio.run(svc.update_from_message_async("1", WARM[i % len(WARM)]))
            clock.advance(1)
        day = svc.profile("1").get("warm_day") or {}
        self.assertGreaterEqual(
            float(day.get("gain", 0.0) or 0.0), M.DAILY_NET_GAIN_CAP - 0.01,
            "一天的暖话没把额度用满",
        )
        # 同一天再说一句：加不动
        self.assertLessEqual(
            asyncio.run(svc.update_from_message_async("1", "谢谢你")).affection, 1e-9
        )
        clock.advance(24 * 60)
        gained = asyncio.run(svc.update_from_message_async("1", "谢谢你")).affection
        self.assertGreater(gained, 0.0, "跨天了当日额度还没翻篇")

    def test_the_day_key_survives_a_missing_clock(self):
        """没有时钟时也必须给得出「今天」，否则额度会永久记在空日期上。"""
        conf = HumanoidConfig.from_raw(
            {"timezone_city": "北京", "mood_use_llm_for_delta": False}
        )
        store = ScopeStore(Path(tempfile.mkdtemp()) / "s.json")
        store.load("2026-08-22", conf.cycle_length)
        svc = MoodService(store.scope, lambda: conf, None, time_source=lambda: 1_000_000.0)
        self.assertTrue(svc._day_key(), "取不到日期：日额度会攒满一次就再也不重置")


class GrowthOverDays(unittest.TestCase):
    """C：连着聊几天，好感必须真的往上走。"""

    def test_a_week_of_warm_chat_moves_affection_up(self):
        svc, clock = _svc()
        start = svc.profile("1")["affection"]
        for i in range(7 * 10):
            svc.decay_user("1")
            asyncio.run(svc.update_from_message_async("1", WARM[i % len(WARM)]))
            clock.advance(30)
            if (i + 1) % 10 == 0:
                clock.advance(19 * 60)  # 隔一夜，进入下一个自然日
        end = svc.profile("1")["affection"]
        self.assertGreater(end, start + 10.0, f"七天暖话只从 {start} 走到 {end}")


class RelationshipFloorSurvivesPruning(unittest.TestCase):
    """D：清档只清明细，关系基线要留下。"""

    def _age(self, svc, uid, days):
        svc._scope.set_user(uid, "last_interaction", svc._time() - days * 86400.0)

    def test_pruning_keeps_the_earned_baseline(self):
        svc, _clock = _svc()
        record = svc.profile("7")
        record["base_affection"] = 72.0
        record["affection"] = 80.0
        self._age(svc, "7", 10)  # 超过默认 7 天的情绪档案保留窗口
        self.assertEqual(svc.prune_expired(), 1, "情绪档案该被清掉")
        self.assertNotIn("mood", svc._scope.user_state("7"))
        self.assertAlmostEqual(
            svc.profile("7")["affection"], 72.0, places=3,
            msg="清档把关系基线一起清了，回来又变回初始值",
        )

    def test_manual_set_clears_the_kept_baseline(self):
        svc, _clock = _svc()
        record = svc.profile("8")
        record["base_affection"] = 72.0
        self._age(svc, "8", 10)
        svc.prune_expired()
        self.assertIn("base_affection_kept", svc._scope.user_state("8"))
        svc.set_affection("8", 20.0)
        self.assertNotIn("base_affection_kept", svc._scope.user_state("8"))
        self.assertAlmostEqual(svc.profile("8")["affection"], 20.0, places=3)


if __name__ == "__main__":
    unittest.main()
