"""B4 / R5：注入里的「昨晚」「认识 N 天」按角色时区的日历天算（P-02 / P-09）。

旧行为的两处时间口径错误：

* `_sleep_cause_line` 把「最近这一觉」一律说成「昨晚只睡了 N 小时」——下午的午睡、
  三天前的觉都成了「昨晚」。
* `_met_days_line` 用 `int((now - first_met) // 86400)` 按 24 小时整块截断：23:59
  认识、次日 00:01 显示「认识 0 天」（刚认识不久）。

这里固定「现在」，逐条钉住日历天口径，并验证时区取的是**角色时区**而不是宿主机。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.config import HumanoidConfig
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.state import StateStore

from .fakes import FakeContext, FrozenClock, RecordingLogger

SH = ZoneInfo("Asia/Shanghai")
MOMENT = datetime(2026, 9, 26, 13, 3, tzinfo=SH)
TODAY = MOMENT.strftime("%Y-%m-%d")


def cfg(**kw) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", **kw})


def make_core(moment: datetime = MOMENT) -> HumanoidCoreInstance:
    store = StateStore(Path(tempfile.mkdtemp()) / "s.json", lambda: 0.01)
    store.load(moment.strftime("%Y-%m-%d"), 28)
    config = cfg()
    core = HumanoidCoreInstance(
        role_id="bot1", state_store=store, config_provider=lambda: config,
        logger=RecordingLogger(), stop_event=asyncio.Event(),
        resolver=FakeContext(), gateway=None,
    )
    clock = FrozenClock(moment)
    core.clock = clock
    for name in ("schedule", "soma", "energy", "social", "process", "mood"):
        service = getattr(core, name, None)
        if service is not None and hasattr(service, "_clock"):
            service._clock = clock
    return core


def _ts(moment: datetime) -> float:
    return moment.timestamp()


class SleepCauseDateTest(unittest.TestCase):
    """「只睡了 N 小时」前面的日期限定词必须跟这一觉的真实时刻一致。"""

    def _line(self, core, *, wake_at: float, slept: float) -> str:
        core.soma.data["asleep_since"] = None
        core.soma.data["last_sleep_hours"] = slept
        core.soma.data["wake_at"] = wake_at
        core.soma.data["last_wake_at"] = wake_at
        return core.prompt_builder._sleep_cause_line()

    def test_nap_today_says_today(self):
        """今天凌晨睡、今天早上醒：是「今天」，不是「昨晚」。"""
        core = make_core()
        wake = datetime(2026, 9, 26, 8, 0, tzinfo=SH)
        line = self._line(core, wake_at=_ts(wake), slept=4.0)
        self.assertEqual(line, "今天只睡了4小时")

    def test_last_night_says_last_night(self):
        """昨天深夜睡、今天凌晨醒：跨了日历天，才叫「昨晚」。"""
        core = make_core()
        wake = datetime(2026, 9, 26, 0, 30, tzinfo=SH)
        line = self._line(core, wake_at=_ts(wake), slept=4.0)
        self.assertEqual(line, "昨晚只睡了4小时")

    def test_yesterday_says_yesterday(self):
        """昨天早上醒的（现在下午）：不是「昨晚」，是「昨天」。"""
        core = make_core()
        wake = datetime(2026, 9, 25, 8, 0, tzinfo=SH)
        line = self._line(core, wake_at=_ts(wake), slept=4.0)
        self.assertEqual(line, "昨天只睡了4小时")

    def test_days_ago_does_not_claim_last_night(self):
        """三天前的觉不能再说「昨晚」。"""
        core = make_core()
        wake = datetime(2026, 9, 23, 8, 0, tzinfo=SH)
        line = self._line(core, wake_at=_ts(wake), slept=4.0)
        self.assertEqual(line, "前几天只睡了4小时")

    def test_missing_wake_time_drops_the_date_claim(self):
        """不知道这一觉什么时候睡的，就不许替她指定「昨晚」。"""
        core = make_core()
        core.soma.data["asleep_since"] = None
        core.soma.data["last_sleep_hours"] = 4.0
        core.soma.data.pop("wake_at", None)
        core.soma.data.pop("last_wake_at", None)
        self.assertEqual(
            core.prompt_builder._sleep_cause_line(), "最近只睡了4小时"
        )

    def test_long_sleep_still_says_nothing(self):
        """睡够 6 小时以上本来就不出这句（原有门槛不变）。"""
        core = make_core()
        wake = datetime(2026, 9, 26, 8, 0, tzinfo=SH)
        self.assertEqual(self._line(core, wake_at=_ts(wake), slept=7.0), "")

    def test_asleep_says_nothing(self):
        core = make_core()
        core.soma.data["asleep_since"] = core.soma.now - 3600.0
        core.soma.data["last_sleep_hours"] = 4.0
        self.assertEqual(core.prompt_builder._sleep_cause_line(), "")


class MetDaysCalendarTest(unittest.TestCase):
    """「认识 N 天」按日历天，不按 24 小时整块。"""

    def _line(self, core, first_met: float) -> str:
        core.mood.profile("42")["first_met"] = first_met
        return core.prompt_builder._met_days_line("42")

    def test_two_minutes_past_midnight_is_day_one(self):
        """23:59 认识、次日 00:01：已经是第二天，不能再报「刚认识不久」。"""
        core = make_core()
        first_met = _ts(datetime(2026, 9, 25, 23, 59, tzinfo=SH))
        self.assertEqual(self._line(core, first_met), "你们已经认识 1 天了")

    def test_same_calendar_day_still_just_met(self):
        core = make_core()
        first_met = _ts(datetime(2026, 9, 26, 0, 30, tzinfo=SH))
        self.assertEqual(self._line(core, first_met), "你们刚认识不久")

    def test_calendar_day_not_24h_blocks(self):
        """昨天 20:00 认识（不到 24 小时）：日历天差 1，就该报 1 天。"""
        core = make_core()
        first_met = _ts(datetime(2026, 9, 25, 20, 0, tzinfo=SH))
        self.assertEqual(self._line(core, first_met), "你们已经认识 1 天了")

    def test_day_count_uses_the_role_timezone(self):
        """同一个 epoch 秒在洛杉矶是 25 号、在上海是 26 号：按角色时区算。"""
        la = ZoneInfo("America/Los_Angeles")
        # 角色在洛杉矶，现在当地 2026-09-26 02:00（上海 17:00）。
        moment = datetime(2026, 9, 26, 2, 0, tzinfo=la)
        core = make_core(moment)
        # 认识时刻 = 上海 2026-09-26 05:00 = 洛杉矶 2026-09-25 14:00。
        first_met = _ts(datetime(2026, 9, 26, 5, 0, tzinfo=SH))
        self.assertEqual(self._line(core, first_met), "你们已经认识 1 天了")


if __name__ == "__main__":
    unittest.main()
