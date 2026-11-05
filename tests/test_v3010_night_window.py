"""v3.0.10 生物钟夜：日程里那段觉得像一夜（审核：白天小睡冒充夜窗）。

现场：某个角色的当天日程里只有一段 11:15-12:45 的小睡，`sleep_window_minutes` 照样
把它返回成夜窗 11.25→12.75。于是上午十一点 `is_night=True`、`is_deep_sleep=True`，
低谷锚点被摆到中午十二点（下午三点最困、凌晨三点最清醒）。

这一组用例钉住三件事：
1. 跨度区间从 `sleep_need_hours` 推出来，不写死；
2. 太短（午睡）与太长（一整天都在睡）都不当夜，退回配置窗口；
3. 值夜班、白天补 7 小时的人照样算一夜（不能把「白天睡」一刀切掉）。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from humanoid.config import HumanoidConfig
from humanoid.services.soma import SomaService
from humanoid.slots import (
    night_span_bounds,
    night_span_hours,
    plausible_night,
    sleep_window_minutes,
)

from .fakes import FrozenClock, ScopeStore

TZ = timezone(timedelta(hours=8))


def cfg(**overrides) -> HumanoidConfig:
    base = {
        "timezone_city": "北京",
        "night_mode_enabled": True,
        "night_start_hour": 23,
        "night_end_hour": 6,
        "sleep_need_hours": 8.0,
        "weather_affects_body": False,
        "enable_cycle": False,
    }
    base.update(overrides)
    return HumanoidConfig.from_raw(base)


def full_day(sleep_start: str | None, sleep_end: str | None, event: str = "睡一觉") -> list[dict]:
    """一整天的日程：只在给定区间排一觉，其余时间醒着。"""
    if sleep_start is None:
        return [{"start": "00:00", "end": "24:00", "event": "在家活动", "energy_rate": 0.0}]
    return [
        {"start": "00:00", "end": sleep_start, "event": "在家活动", "energy_rate": 0.0},
        {
            "start": sleep_start,
            "end": sleep_end,
            "event": event,
            "location": "卧室",
            "energy_rate": 0.15,
        },
        {"start": sleep_end, "end": "24:00", "event": "在家活动", "energy_rate": 0.0},
    ]


class _Fixture:
    def __init__(self, moment: datetime, config: HumanoidConfig, slots) -> None:
        self.store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        self.store.load(moment.strftime("%Y-%m-%d"), config.cycle_length)
        self.clock = FrozenClock(moment)
        self.time_value = moment.timestamp()
        self.slots = slots
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


class SpanBoundsTest(unittest.TestCase):
    def test_bounds_follow_sleep_need(self):
        self.assertEqual(night_span_bounds(8.0), (4.8, 14.0))
        self.assertEqual(night_span_bounds(4.0), (2.4, 10.0))
        self.assertEqual(night_span_bounds(14.0), (6.0, 16.0))
        # 配得很小：下限有 2 小时的兜底，不会退化成「30 分钟也算一夜」
        self.assertEqual(night_span_bounds(3.0), (2.0, 9.0))

    def test_bad_need_falls_back_to_eight(self):
        for bad in (0, -3, float("nan"), None, "x"):
            self.assertEqual(night_span_bounds(bad), (4.8, 14.0), msg=repr(bad))

    def test_span_is_cross_midnight_aware(self):
        self.assertEqual(night_span_hours((23.0, 6.0)), 7.0)
        self.assertEqual(night_span_hours((2.0, 9.0)), 7.0)
        self.assertEqual(night_span_hours((11.25, 12.75)), 1.5)
        self.assertEqual(night_span_hours(None), 0.0)
        self.assertEqual(night_span_hours((5.0, 5.0)), 0.0)

    def test_nap_is_not_a_night(self):
        self.assertFalse(plausible_night((11.25, 12.75), 8.0))
        self.assertFalse(plausible_night((13.0, 17.5), 8.0))  # 4.5h 的下午觉，仍不够一夜

    def test_real_nights_are_accepted(self):
        self.assertTrue(plausible_night((23.0, 6.0), 8.0))
        self.assertTrue(plausible_night((2.0, 9.0), 8.0))    # 夜猫子
        self.assertTrue(plausible_night((9.0, 16.0), 8.0))   # 值夜班、白天补 7 小时

    def test_a_whole_day_of_sleep_is_not_a_night(self):
        self.assertFalse(plausible_night((0.0, 18.0), 8.0))


class NapOnlyDayTest(unittest.TestCase):
    """现场那一份日程：只有一段 11:15-12:45 的小睡。"""

    def setUp(self):
        self.moment = datetime(2026, 11, 4, 11, 30, tzinfo=TZ)
        self.fix = _Fixture(self.moment, cfg(), full_day("11:15", "12:45"))
        self.soma = self.fix.soma

    def test_the_window_is_still_the_nap(self):
        # 挑窗口的那一层没错：它就该返回那一段。错的是「拿它当一夜」。
        self.assertEqual(sleep_window_minutes(self.fix.slots), (675, 765))

    def test_biological_night_falls_back_to_config(self):
        self.assertEqual(self.soma.biological_night(), (23.0, 6.0))

    def test_scheduled_night_says_unknown(self):
        self.assertIsNone(self.soma.scheduled_night())

    def test_eleven_in_the_morning_is_not_night(self):
        self.assertFalse(self.soma._in_biological_night(11.5))
        self.assertFalse(self.soma.in_scheduled_night(11.5))

    def test_deep_sleep_is_not_at_noon(self):
        self.assertFalse(self.soma.in_scheduled_deep_sleep(11.5))

    def test_dip_anchor_is_the_real_midnight(self):
        # 23→6 的中点是凌晨 2:30；午睡会把锚点摆到中午 12:00（下午 3 点最困）
        self.assertAlmostEqual(self.soma._night_mid_hour(), 2.5)

    def test_no_sleep_debt_from_the_nap_window(self):
        """白天不该因为「夜窗 11:15-12:45」以外的 22 小时被算成夜里醒着而欠债。"""
        self.fix.to(datetime(2026, 11, 4, 15, 0, tzinfo=TZ))
        self.assertEqual(self.soma.data.get("sleep_debt", 0.0), 0.0)

    def test_the_warning_is_logged_once(self):
        with self.assertLogs("humanoid.services.soma", level="WARNING") as logs:
            for _ in range(3):
                self.soma.biological_night()
        self.assertEqual(len(logs.records), 1)
        text = logs.records[0].getMessage()
        self.assertIn("不把它当生物钟夜", text)
        self.assertIn("23:00-06:00", text)


class RealNightTest(unittest.TestCase):
    def test_a_real_night_is_used(self):
        moment = datetime(2026, 11, 4, 2, 0, tzinfo=TZ)
        fix = _Fixture(moment, cfg(), full_day("23:00", "07:00"))
        self.assertEqual(fix.soma.biological_night(), (23.0, 7.0))
        self.assertEqual(fix.soma.scheduled_night(), (23.0, 7.0))
        self.assertTrue(fix.soma._in_biological_night(2.0))

    def test_night_shift_sleeper_is_still_a_night(self):
        moment = datetime(2026, 11, 4, 12, 0, tzinfo=TZ)
        fix = _Fixture(moment, cfg(), full_day("09:00", "16:00"))
        self.assertEqual(fix.soma.biological_night(), (9.0, 16.0))

    def test_absurdly_long_sleep_falls_back(self):
        moment = datetime(2026, 11, 4, 12, 0, tzinfo=TZ)
        fix = _Fixture(moment, cfg(), full_day("00:00", "18:00"))
        self.assertEqual(fix.soma.biological_night(), (23.0, 6.0))


class NightModeOffTest(unittest.TestCase):
    def test_night_mode_off_is_still_none(self):
        moment = datetime(2026, 11, 4, 12, 0, tzinfo=TZ)
        fix = _Fixture(moment, cfg(night_mode_enabled=False), full_day("23:00", "07:00"))
        self.assertIsNone(fix.soma.biological_night())
        self.assertIsNone(fix.soma._night_mid_hour())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()