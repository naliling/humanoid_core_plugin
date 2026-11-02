"""v3.0.2 日程与身体审计回归：睡眠认不出来、睡够了不醒、债还不完、失败冷却太长。

用户看到的现象是「她一直在睡觉，除了睡觉几乎不干别的」。查下来是四件事叠在一起：

1. 睡眠只按关键词判（`slots.is_sleep_event`）。模型写的睡段常是「阖眼，呼吸渐沉」
   「将被窝拉到肩上」这种安静的卧床描写，一个睡字都没有，于是一整夜都不算睡：
   睡眠压力一路涨、精力按醒着掉、提示词里再写一句「她醒着」，和日程互相打脸。
   `slots.slot_sleeps` 补了第二条判据（卧床 + 正的回能速率 + 够长的跨度，且没有
   清醒活动词），soma / schedule / process / core_instance 统一走它。
2. 模型不可用时走本地兜底分段，而兜底每被问一次就再排一段睡：睡着了 → 再排 90
   分钟睡 → 仍然睡着 → 再排……`dynamic_segment` 现在接 `asleep_minutes`，睡过该睡
   的量就必须给一段醒着的时段；读数来自 `ScheduleService.sleeping_minutes()`。
3. 睡眠债按写死的半速（0.5/小时）回收，却按 `sleep_need_hours` 攒（约 1.14/小时）：
   一晚睡 8 小时只还 4 小时，债只增不减，顶到上限之后「睡一觉」再无意义（表现就是
   一直补觉）。回收速率现在不低于攒债速率。
4. 一次永久性失败（4xx / 参数被提供方拒绝 / 内容拒绝）就把 provider 冷却满 30 分钟，
   首选与备用一起冷却 → 日程退回本地模板半小时。永久性失败改用短冷却。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from humanoid.config import HumanoidConfig
from humanoid.llm import PERMANENT_FAILURE_COOLDOWN_SECONDS, PURPOSE_SCHEDULE, LLMGateway
from humanoid.services.schedule import ScheduleService, dynamic_segment
from humanoid.services.soma import SomaService
from humanoid.slots import slot_sleeps, sleep_window_minutes

from .fakes import FrozenClock, RecordingLogger, ScopeStore

TZ = ZoneInfo("Asia/Shanghai")


def cfg(**overrides) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", **overrides})


# 生产里真实出现过的睡段：安静、卧床、回能，没有一个睡字。
POETIC_SLEEP = {
    "event": "回到卧室，将浅蓝瞳孔轻阖，呼吸渐沉",
    "location": "卧室",
    "energy_rate": 0.12,
    "start": "16:00",
    "end": "17:30",
}


class SleepRecognitionTest(unittest.TestCase):
    """睡不睡看的是这一段在干什么，不是事件名里有没有「睡」。"""

    def test_a_poetic_bed_scene_counts_as_sleep(self):
        self.assertTrue(slot_sleeps(POETIC_SLEEP))

    def test_a_twenty_minute_lie_down_is_not_a_night(self):
        self.assertFalse(slot_sleeps({**POETIC_SLEEP, "end": "16:20"}))

    def test_awake_activity_in_bed_is_not_sleep(self):
        for event in ("躺在床上刷手机", "窝在被窝里看剧", "靠着枕头回消息"):
            with self.subTest(event=event):
                self.assertFalse(slot_sleeps({**POETIC_SLEEP, "event": event}))

    def test_meal_still_wins_over_the_nap_word(self):
        slot = {
            "event": "午餐与午后小憩",
            "location": "餐桌",
            "energy_rate": 0.15,
            "start": "12:00",
            "end": "13:30",
        }
        self.assertFalse(slot_sleeps(slot))

    def test_the_filler_segment_is_not_a_sleep_slot(self):
        filler = {"event": "夜间休息", "start": "23:00", "end": "07:00"}
        self.assertFalse(slot_sleeps(filler), "兜底填充段不等于她真的在睡")
        self.assertIsNone(sleep_window_minutes([filler]))

    def test_a_flag_or_a_dream_from_the_model_counts(self):
        self.assertTrue(slot_sleeps({"event": "安静待着", "sleep": True}))
        self.assertTrue(slot_sleeps({"event": "闭目", "dreams": ["梦见海"]}))


class DynamicSegmentWakeUpTest(unittest.TestCase):
    """兜底分段：睡够了必须给醒着的时段，否则「睡着」会自我延续。"""

    def build(self, now_minute, body, asleep_minutes=0.0):
        return dynamic_segment(
            cfg(night_start_hour=23, night_end_hour=7),
            body,
            now_minute=now_minute,
            step=15,
            seed=["bot", "2026-08-22"],
            asleep_minutes=asleep_minutes,
        )

    def test_a_fresh_sleeper_keeps_sleeping(self):
        slot = self.build(3 * 60, {"asleep": 1.0, "sleep_pressure": 80.0})
        self.assertTrue(slot_sleeps(slot), "刚开始睡的一段不该被叫醒")

    def test_a_two_hour_nap_is_still_a_sleep(self):
        slot = self.build(9 * 60, {"asleep": 1.0}, asleep_minutes=120.0)
        self.assertTrue(slot_sleeps(slot))

    def test_a_full_nights_sleep_ends_in_daylight(self):
        slot = self.build(9 * 60, {"asleep": 1.0, "sleep_pressure": 60.0}, asleep_minutes=600.0)
        self.assertFalse(slot_sleeps(slot), f"睡满 10 小时还在卧室里睡：{slot}")
        self.assertNotIn("睡", str(slot.get("event", "")))

    def test_the_night_allows_a_little_oversleep(self):
        slot = self.build(4 * 60, {"asleep": 1.0, "sleep_pressure": 60.0}, asleep_minutes=600.0)
        self.assertTrue(slot_sleeps(slot), "凌晨还早，睡过头一点是正常的")

    def test_oversleeping_deep_into_the_night_ends(self):
        slot = self.build(4 * 60, {"asleep": 1.0, "sleep_pressure": 40.0}, asleep_minutes=700.0)
        self.assertFalse(slot_sleeps(slot), f"夜里睡过 11 小时还不醒：{slot}")


class SleepingMinutesTest(unittest.TestCase):
    """`sleeping_minutes()` 是兜底判「睡够了没有」的读数。"""

    MOMENT = datetime(2026, 8, 22, 4, 0, tzinfo=TZ)

    def build(self):
        conf = cfg()
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load(self.MOMENT.strftime("%Y-%m-%d"), conf.cycle_length)
        clock = FrozenClock(self.MOMENT)
        service = ScheduleService(
            store.scope,
            lambda: conf,
            clock,
            logger=RecordingLogger(),
            monotonic=lambda: 0.0,
        )
        return service, store

    def _install(self, store, slots):
        store.set("today_date", "2026-08-22")
        store.set("daily_schedule", slots)

    def test_consecutive_sleep_slots_add_up(self):
        service, store = self.build()
        self._install(store, [
            {"event": "阖眼，呼吸渐沉", "location": "卧室", "energy_rate": 0.12,
             "start": "00:00", "end": "03:00"},
            {"event": "将被窝拉到肩上", "location": "卧室", "energy_rate": 0.12,
             "start": "03:00", "end": "08:00"},
        ])
        self.assertEqual(service.sleeping_minutes(), 240.0)

    def test_a_waking_segment_resets_the_count(self):
        service, store = self.build()
        self._install(store, [
            {"event": "阖眼，呼吸渐沉", "location": "卧室", "energy_rate": 0.12,
             "start": "00:00", "end": "03:00"},
            {"event": "醒神", "location": "家里", "energy_rate": -0.05,
             "start": "03:00", "end": "08:00"},
        ])
        self.assertEqual(service.sleeping_minutes(), 0.0)


class SleepDebtTest(unittest.TestCase):
    """欠的债要能睡回来：回收速率不能低于攒债速率。"""

    def build(self, moment, slots):
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load()
        clock = FrozenClock(moment)
        soma = SomaService(
            store.scope,
            lambda: cfg(),
            clock,
            schedule_provider=lambda: slots,
            weather_provider=None,
            # advance() 的积分步进走的是 `_time()`，默认是 time.time（真实墙钟）。
            # 不接冻结时钟的话，每次 advance 的 elapsed 都是 0，睡眠债永远不动。
            time_source=lambda: clock.now().timestamp(),
        )
        return soma, clock

    def test_one_good_night_pays_a_night_of_debt_back(self):
        night = [
            {"event": "睡觉", "start": "23:00", "end": "24:00"},
            {"event": "睡觉", "start": "00:00", "end": "08:00"},
        ]
        soma, clock = self.build(datetime(2026, 8, 22, 0, 0, tzinfo=TZ), night)
        soma._set("sleep_debt", 8.0)
        for _ in range(48):  # 00:00 → 08:00：整整一夜
            clock.advance(minutes=10)
            soma.advance()
        debt = soma.snapshot()["sleep_debt"]
        self.assertLess(debt, 1.0, f"睡了一整夜还欠 {debt} 小时：回收速率低于攒债速率")


class PermanentFailureCooldownTest(unittest.TestCase):
    """永久性失败冷却要短于完整窗口，否则一次坏请求停掉整条日程链。"""

    def test_a_permanent_failure_does_not_park_the_provider(self):
        fake = SimpleNamespace(_cooldown_seconds=lambda purpose: 30 * 60.0)
        seconds = LLMGateway._failure_cooldown(fake, PURPOSE_SCHEDULE, True)
        self.assertEqual(seconds, PERMANENT_FAILURE_COOLDOWN_SECONDS)
        self.assertLess(seconds, 30 * 60.0)

    def test_a_transient_failure_keeps_the_configured_window(self):
        fake = SimpleNamespace(_cooldown_seconds=lambda purpose: 30 * 60.0)
        self.assertEqual(LLMGateway._failure_cooldown(fake, PURPOSE_SCHEDULE, False), 30 * 60.0)

    def test_a_zero_cooldown_stays_zero(self):
        fake = SimpleNamespace(_cooldown_seconds=lambda purpose: 0.0)
        self.assertEqual(LLMGateway._failure_cooldown(fake, PURPOSE_SCHEDULE, True), 0.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
