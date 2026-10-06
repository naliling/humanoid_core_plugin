"""v2.27.15：M2 认识纪念日（周年 / 整百天）。

只给一句淡淡的状态，**不报天数**——把「认识 400 天」递进上下文，她会拿去念一句
「我们认识这么久了」，那是聊天里最没劲的一句话（与既有的「认识多久不进上下文」
同一条规矩）。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from humanoid.emotion import EmotionLayer

TZ = timezone(timedelta(hours=8))


class _Clock:
    def __init__(self, dt):
        self.dt = dt

    def now(self):
        return self.dt

    def today_str(self):
        return self.dt.strftime("%Y-%m-%d")


class _Core:
    def __init__(self, moment):
        self.clock = _Clock(moment)
        self._epoch = moment.timestamp()

    def now_epoch(self):
        return self._epoch


def line_for(met_moment, now_moment):
    core = _Core(now_moment)
    profile = {"first_met": met_moment.timestamp()}
    return EmotionLayer(core)._anniversary_line(profile, 0)


class AnniversaryTest(unittest.TestCase):
    def test_one_year_anniversary(self):
        met = datetime(2025, 10, 6, 12, 0, tzinfo=TZ)
        now = datetime(2026, 10, 6, 9, 0, tzinfo=TZ)
        sig, line = line_for(met, now)
        self.assertTrue(line, "满一年的那天该有一句")
        self.assertGreater(sig, 0.0)

    def test_round_hundred_days(self):
        met = datetime(2026, 6, 28, 12, 0, tzinfo=TZ)
        now = datetime(2026, 10, 6, 9, 0, tzinfo=TZ)  # 正好 +100 天
        _sig, line = line_for(met, now)
        self.assertTrue(line, "满 100 天该有一句")

    def test_ordinary_day_is_silent(self):
        met = datetime(2026, 6, 1, 12, 0, tzinfo=TZ)
        now = datetime(2026, 10, 6, 9, 0, tzinfo=TZ)
        _sig, line = line_for(met, now)
        self.assertEqual(line, "", "普通日子不该硬塞一句纪念日")

    def test_no_number_leaks(self):
        met = datetime(2025, 10, 6, 12, 0, tzinfo=TZ)
        now = datetime(2026, 10, 6, 9, 0, tzinfo=TZ)
        _sig, line = line_for(met, now)
        for ch in "0123456789":
            self.assertNotIn(ch, line, f"纪念日句里漏出了天数：{line}")


class ContractHoliday(unittest.TestCase):
    """契约要导出「今天是节日」：社交层靠它派节日由头/破冰素材。"""

    def _contract(self, **conf):
        import asyncio

        from humanoid.link import build_contract
        from .test_link import Harness

        harness = Harness({"timezone_city": "北京", **conf})
        core = harness.roles.get_or_create("bot1")
        contract = build_contract(core)
        asyncio.run(harness.roles.stop())
        return contract

    def test_holiday_is_exported(self):
        from zoneinfo import ZoneInfo

        # 不能用 FrozenClock：它的 holiday() 恒为空。改用真实的钟 + 把节日设在「今天」。
        today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%m-%d")
        contract = self._contract(holidays=[{"date": today, "name": "测试节"}])
        self.assertEqual(contract.get("holiday"), "测试节")

    def test_no_holiday_is_empty(self):
        contract = self._contract()
        self.assertEqual(contract.get("holiday"), "")


if __name__ == "__main__":
    unittest.main()