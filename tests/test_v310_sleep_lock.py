"""v3.1.0 睡眠自锁回归：日程断档后 asleep_since 没人清，她会永久「在睡」。

现场（2026-11-06 线上 state.json）：
- 角色 webchat 的日程回落成模板，只剩一段 06:00–13:30 的睡；
- asleep_since = 11-05 22:00，到 11-06 23:21 已经 25.4 小时没醒；
- social 每轮都报「闸门挡下 … 她在睡 52」，主动消息一条也发不出去。

机制：asleep_since 只在 _integrate 判为「醒着」时由 _record_wake 清掉，
而出了那一段就是日程空窗 → sleep_state 返回 unknown → unknown 分支
既不记睡也不记醒（pass）→ asleep_since 永远不清。
wake_state() / snapshot() 又只看 asleep_since，于是对外一直报睡着。

这组测试锁死三件事：
1. 睡过硬上限（默认 max(10, 需求+3) 小时）必须强制结算成醒；
2. 日程明确写着醒着时，不必等到上限；
3. 相邻睡段有 1 分钟重叠时，sleeping_minutes() 仍要把它们算成一觉，
   否则兜底永远判不出「睡够了」，模板会一段接一段地排睡。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.config import HumanoidConfig
from humanoid.services.schedule import (
    SLEEP_CONTIGUITY_TOLERANCE_MINUTES,
    ScheduleService,
    dynamic_segment,
)
from humanoid.services.soma import SomaService
from humanoid.slots import parse_time

from .fakes import FrozenClock, RecordingLogger, ScopeStore

TZ = ZoneInfo("Asia/Shanghai")


def cfg(**overrides) -> HumanoidConfig:
    base = {
        "timezone_city": "北京",
        "sleep_need_hours": 8.0,
        "weather_affects_body": False,
        "enable_cycle": False,
    }
    base.update(overrides)
    return HumanoidConfig.from_raw(base)


def poetic(start: str, end: str) -> dict:
    """模型写的安静卧床段：没有「睡」字，靠 slot_sleeps 的第二条判据认出来。"""
    return {
        "event": "阖眼，呼吸渐沉",
        "location": "卧室",
        "energy_rate": 0.12,
        "start": start,
        "end": end,
    }


def awake_slot(start: str, end: str) -> dict:
    return {"event": "在家活动", "location": "家里", "energy_rate": -0.05, "start": start, "end": end}


class _SomaFixture:
    def __init__(self, moment: datetime, slots, config: HumanoidConfig) -> None:
        self.store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        self.store.load(moment.strftime("%Y-%m-%d"), config.cycle_length)
        self.clock = FrozenClock(moment)
        self.time_value = moment.timestamp()
        self.soma = SomaService(
            self.store.scope,
            lambda: config,
            self.clock,
            schedule_provider=lambda: slots,
            weather_provider=lambda: {},
            time_source=lambda: self.time_value,
        )

    def to(self, moment: datetime) -> dict:
        self.clock.moment = moment
        self.time_value = moment.timestamp()
        return self.soma.advance()


class SleepLockTest(unittest.TestCase):
    """线上那份形状：模板只剩一段睡，出了这段就是空窗。"""

    def setUp(self):
        # 夹具建在 14:00，再推进到 15:00：advance() 在时钟没前进时直接返回，
        # 同一时刻调用不会执行积分。
        self.moment = datetime(2026, 11, 6, 15, 0, tzinfo=TZ)
        self.fix = _SomaFixture(datetime(2026, 11, 6, 14, 0, tzinfo=TZ), [poetic("06:00", "13:30")], cfg())
        self.soma = self.fix.soma
        # 现场 asleep_since：11-05 22:00（前一晚）
        self.soma.data["asleep_since"] = datetime(2026, 11, 5, 22, 0, tzinfo=TZ).timestamp()

    def test_a_single_sleep_template_no_longer_locks_her(self):
        self.fix.to(self.moment)
        self.assertIsNone(self.soma.data.get("asleep_since"), "睡过上限必须结算成醒")
        self.assertEqual(self.soma.snapshot()["asleep"], 0.0)
        self.assertFalse(self.soma.wake_state()["asleep"])

    def test_the_cap_is_eleven_hours_by_default(self):
        self.assertEqual(self.soma._max_sleep_hours(), 11.0)

    def test_an_explicit_cap_is_respected(self):
        fix = _SomaFixture(datetime(2026, 11, 6, 15, 0, tzinfo=TZ), [poetic("06:00", "13:30")], cfg(max_sleep_hours=9.0))
        fix.soma.data["asleep_since"] = datetime(2026, 11, 6, 6, 0, tzinfo=TZ).timestamp()
        fix.to(datetime(2026, 11, 6, 16, 0, tzinfo=TZ))
        self.assertIsNone(fix.soma.data.get("asleep_since"), "配了 9 小时就该在 15:00 前醒")

    def test_a_fresh_nap_in_a_schedule_gap_is_not_touched(self):
        """空窗里刚睡下的那 30 分钟仍算睡着：日程重排的瞬时空窗不该被当成醒了。"""
        fix = _SomaFixture(datetime(2026, 11, 6, 14, 0, tzinfo=TZ), [poetic("06:00", "13:30")], cfg())
        fix.soma.data["asleep_since"] = datetime(2026, 11, 6, 14, 50, tzinfo=TZ).timestamp()
        fix.to(self.moment)
        self.assertIsNotNone(fix.soma.data.get("asleep_since"))
        self.assertTrue(fix.soma.sleeping_now(self.moment))
        self.assertEqual(fix.soma.snapshot()["asleep"], 1.0)

    def test_a_long_gap_is_no_longer_reported_as_sleeping(self):
        """断档超过宽限期就按醒着报（审核 A-4）：否则空窗能一路假睡到硬上限。"""
        fix = _SomaFixture(datetime(2026, 11, 6, 14, 0, tzinfo=TZ), [poetic("06:00", "13:30")], cfg())
        fix.soma.data["asleep_since"] = datetime(2026, 11, 6, 14, 0, tzinfo=TZ).timestamp()
        fix.to(self.moment)
        self.assertIsNotNone(fix.soma.data.get("asleep_since"), "还没睡过上限，记录本身不该被清")
        self.assertFalse(fix.soma.sleeping_now(self.moment))
        self.assertEqual(fix.soma.snapshot()["asleep"], 0.0)

    def test_a_schedule_that_says_awake_wakes_her_at_once(self):
        """日程明确写着这一段醒着：不用等 11 小时。"""
        fix = _SomaFixture(datetime(2026, 11, 6, 14, 0, tzinfo=TZ), [awake_slot("13:30", "24:00")], cfg())
        fix.soma.data["asleep_since"] = datetime(2026, 11, 6, 14, 0, tzinfo=TZ).timestamp()
        fix.to(self.moment)
        self.assertIsNone(fix.soma.data.get("asleep_since"))
        self.assertFalse(fix.soma.wake_state()["asleep"])

    def test_the_cap_also_fires_when_the_schedule_still_says_sleep(self):
        """日程整天都写着睡（模板排满一天）：上限仍要兜住。

        旧写法用 poetic("00:00", "24:00")：slot_sleeps 把 (1440-0)%1440 == 0 判成
        「不满 30 分钟」→ 不是睡段 → 走的是「日程说醒」分支，上限一次都没执行过
        （审核 A-1）。这里用 23:59 让它真的是睡段。
        """
        fix = _SomaFixture(datetime(2026, 11, 6, 14, 0, tzinfo=TZ), [poetic("00:00", "23:59")], cfg())
        fix.soma.data["asleep_since"] = datetime(2026, 11, 5, 20, 0, tzinfo=TZ).timestamp()
        fix.to(self.moment)
        self.assertIsNone(fix.soma.data.get("asleep_since"))
        self.assertEqual(fix.soma.snapshot()["asleep"], 0.0)
        self.assertFalse(fix.soma.sleeping_now(self.moment))
        self.assertGreater(
            float(fix.soma.data.get("last_wake_at") or 0.0), 0.0, "强制唤醒要记醒来的时间"
        )

    def test_the_forced_wake_is_not_undone_by_the_next_step(self):
        """审核 A-2：强制唤醒不能只顶一个积分步。

        日程还写着睡，下一步（10 分钟后）会把 asleep_since 又置回去——旧修复对外
        仍然是「在睡」，last_sleep_hours 还被写成 18.2 小时。
        """
        fix = _SomaFixture(datetime(2026, 11, 6, 14, 0, tzinfo=TZ), [poetic("00:00", "23:59")], cfg())
        fix.soma.data["asleep_since"] = datetime(2026, 11, 5, 20, 0, tzinfo=TZ).timestamp()
        fix.to(self.moment)
        fix.to(datetime(2026, 11, 6, 14, 10, tzinfo=TZ))
        self.assertIsNone(fix.soma.data.get("asleep_since"), "豁免期里不许重新判睡")
        self.assertFalse(fix.soma.sleeping_now(datetime(2026, 11, 6, 14, 10, tzinfo=TZ)))
        self.assertEqual(fix.soma.snapshot()["asleep"], 0.0)
        self.assertEqual(
            float(fix.soma.data.get("last_sleep_hours") or 0.0),
            0.0,
            "强制唤醒不是一觉睡完，不能污染 last_sleep_hours",
        )

    def test_a_broken_asleep_since_still_forces_a_wake(self):
        """审核 A-3：坏值（手改过 state.json）不能把上限变成永久失效。"""
        fix = _SomaFixture(datetime(2026, 11, 6, 14, 0, tzinfo=TZ), [poetic("00:00", "23:59")], cfg())
        fix.soma.data["asleep_since"] = "昨天半夜"
        fix.to(self.moment)
        self.assertIsNone(fix.soma.data.get("asleep_since"))
        self.assertEqual(fix.soma.snapshot()["asleep"], 0.0)

    def test_the_cap_has_a_floor(self):
        """审核 A-5：配置填 0.5 不能变成每 30 分钟强制唤醒一次。"""
        fix = _SomaFixture(
            datetime(2026, 11, 6, 14, 0, tzinfo=TZ), [poetic("06:00", "13:30")], cfg(max_sleep_hours=0.5)
        )
        self.assertEqual(fix.soma._max_sleep_hours(), 4.0)


class SleepContiguityTest(unittest.TestCase):
    """相邻睡段有 1 分钟重叠时也要算同一觉（现场：00:00–05:46 接 05:45–18:00）。"""

    MOMENT = datetime(2026, 11, 6, 8, 0, tzinfo=TZ)

    def build(self):
        conf = cfg()
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load(self.MOMENT.strftime("%Y-%m-%d"), conf.cycle_length)
        service = ScheduleService(
            store.scope,
            lambda: conf,
            FrozenClock(self.MOMENT),
            logger=RecordingLogger(),
            monotonic=lambda: 0.0,
        )
        return service, store

    def test_a_one_minute_overlap_does_not_reset_the_count(self):
        service, store = self.build()
        store.set("today_date", "2026-11-06")
        store.set("daily_schedule", [poetic("00:00", "05:46"), poetic("05:45", "18:00")])
        # 05:45→08:00 = 135，加上前一段 00:00→05:46 = 346，共 481
        self.assertEqual(service.sleeping_minutes(), 481.0)
        self.assertGreaterEqual(service.sleeping_minutes(), 480.0, "必须够到「睡够了」的阈值")

    def test_a_real_gap_still_resets_the_count(self):
        service, store = self.build()
        store.set("today_date", "2026-11-06")
        store.set("daily_schedule", [poetic("00:00", "03:00"), poetic("05:00", "18:00")])
        self.assertEqual(service.sleeping_minutes(), 180.0)

    def test_the_tolerance_is_small(self):
        self.assertLessEqual(SLEEP_CONTIGUITY_TOLERANCE_MINUTES, 5)


class TemplateSleepCapTest(unittest.TestCase):
    """兜底分段：夜间窗口再长，单段睡眠也不该超过「该睡的量 + 60 分钟」。"""

    def test_an_eighteen_hour_night_window_does_not_schedule_twelve_hours_of_sleep(self):
        conf = cfg(night_start_hour=0, night_end_hour=18)
        slot = dynamic_segment(conf, {"asleep": 1.0}, now_minute=60, asleep_minutes=0.0)
        lo = parse_time(slot.get("start"))
        hi = parse_time(slot.get("end"))
        self.assertIsNotNone(lo)
        self.assertIsNotNone(hi)
        self.assertLessEqual(hi - lo, 540, f"单段睡了 {hi - lo} 分钟：{slot}")

    def test_an_explicit_cap_shortens_the_fallback_sleep_segment(self):
        """审核 A-5：max_sleep_hours=5 时兜底单段不该还排 9 小时睡。"""
        conf = cfg(max_sleep_hours=5.0, night_start_hour=0, night_end_hour=18)
        slot = dynamic_segment(conf, {"asleep": 1.0}, now_minute=60, asleep_minutes=0.0)
        lo = parse_time(slot.get("start"))
        hi = parse_time(slot.get("end"))
        self.assertIsNotNone(lo)
        self.assertIsNotNone(hi)
        self.assertLessEqual(hi - lo, 300, f"单段睡了 {hi - lo} 分钟：{slot}")

    def test_enough_sleep_still_gives_an_awake_segment(self):
        conf = cfg(night_start_hour=0, night_end_hour=18)
        slot = dynamic_segment(conf, {"asleep": 1.0}, now_minute=60, asleep_minutes=600.0)
        self.assertFalse(
            slot.get("event") == "睡一觉" and slot.get("energy_rate") == 0.15,
            f"睡够了还在排睡：{slot}",
        )


if __name__ == "__main__":
    unittest.main()
