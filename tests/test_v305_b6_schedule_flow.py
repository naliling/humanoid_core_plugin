"""B6 第二批（t3b-schedule 范围）：日程五条「中」的回归。

对应审核条目：#65 起点向下取整+盲截断、#67 夜里睡够不醒、#70 resume 段能量速率恒 0、
#73 day_phrases 的 future 分支不可达、#75 生成失败不回滚事件/来访。

写法与第一批一致：断言「用户能看见的现象」。
* 09:22 开始、10:07 做完的事，不该在 10:10 变成「被打断，还剩 5 分钟」；
* 默认 8 小时睡眠遇上 23:00–06:00 的窗口，06:00 该补完剩下的 60 分钟，而不是写死 90；
* 回去接着做同一件事，体力消耗要沿用被打断那段的速率；
* 没有预排的未来，day_phrases 的 next 只能是空列表；
* 模型超时那次，「有人来找过她」和「刚刚发生的事」都还在。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.llm import LLMResult, OUTCOME_TIMEOUT
from humanoid.services.schedule import (
    ScheduleService,
    day_phrases,
    dynamic_segment,
    parse_segment,
    slot_sleeps,
)

from .fakes import FakeContext, FrozenClock, RecordingLogger, ScopeStore
from .test_schedule import cfg

TZ = ZoneInfo("Asia/Shanghai")


def _service(moment, **overrides):
    conf = cfg(**overrides)
    store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
    store.load(moment.strftime("%Y-%m-%d"), conf.cycle_length)
    service = ScheduleService(
        store.scope,
        lambda: conf,
        FrozenClock(moment),
        logger=RecordingLogger(),
        monotonic=lambda: 10_000.0,
    )
    return service, conf


class SegmentStartAfterPreviousTest(unittest.TestCase):
    """#65：segment_start 把起点向下对齐到刻度，会把「已经做完」读成「还没做完」。"""

    def test_start_never_falls_back_before_the_previous_end(self):
        slot = parse_segment(
            {"event": "做海报", "minutes": 30},
            now_minute=10 * 60 + 10,
            step=15,
            prev={"start": "09:22", "end": "10:07", "event": "写方案"},
        )
        self.assertIsNotNone(slot)
        self.assertEqual(slot["start"], "10:07", "起点退回了 10:00，与上一段的结束时刻重叠")

    def test_a_finished_segment_is_not_suspended_as_interrupted(self):
        service, _ = _service(datetime(2026, 8, 22, 10, 10, tzinfo=TZ))
        service._scope.update_self(
            daily_schedule=[
                {
                    "start": "09:00",
                    "end": "10:05",
                    "event": "写方案",
                    "location": "书房",
                    "emotion": "平常",
                    "energy_rate": 0.2,
                }
            ],
            today_date="2026-08-22",
        )
        service._append_segment(
            {
                "start": "10:07",
                "end": "10:37",
                "event": "做海报",
                "location": "客厅",
                "emotion": "平常",
                "energy_rate": 0.15,
            },
            "2026-08-22",
        )
        self.assertEqual(
            service._suspended_slot(),
            {},
            "10:05 就做完的事被当成「被打断，还剩几分钟」挂了起来",
        )


class NightOversleepTest(unittest.TestCase):
    """#67：夜里的 +180 分钟豁免让「睡够了就该醒」在默认窗口里永远不成立。"""

    def build(self, now_minute, asleep_minutes, **overrides):
        return dynamic_segment(
            cfg(night_start_hour=23, night_end_hour=6, **overrides),
            {"asleep": 1.0, "sleep_pressure": 80.0},
            now_minute=now_minute,
            step=15,
            seed=["bot", "2026-08-22"],
            asleep_minutes=asleep_minutes,
        )

    def test_default_need_tops_up_the_rest_of_the_night(self):
        """23:00–06:00 只有 420 分钟，8 小时的缺口是 60——补到 07:00，不是写死 90 到 07:30。"""
        slot = self.build(6 * 60, 420.0)
        self.assertTrue(slot_sleeps(slot))
        self.assertEqual(slot["end"], "07:00")

    def test_a_shorter_need_ends_the_night_on_time(self):
        slot = self.build(6 * 60, 420.0, sleep_need_hours=7.0)
        self.assertFalse(slot_sleeps(slot), f"已经睡了 7 小时，06:00 还没醒：{slot}")

    def test_sleeping_through_the_night_still_runs_to_the_window_end(self):
        slot = self.build(5 * 60, 400.0)
        self.assertTrue(slot_sleeps(slot))
        self.assertEqual(slot["end"], "06:00")


class ResumeEnergyRateTest(unittest.TestCase):
    """#70：resume 段按 0.0 算体力，等于「回去接着做」白做不耗体力。"""

    def test_the_rate_survives_the_suspended_snapshot(self):
        service, _ = _service(datetime(2026, 8, 22, 10, 10, tzinfo=TZ))
        service._scope.update_self(
            daily_schedule=[
                {
                    "start": "09:00",
                    "end": "12:00",
                    "event": "做海报",
                    "location": "客厅",
                    "emotion": "平常",
                    "energy_rate": 0.2,
                }
            ],
            today_date="2026-08-22",
        )
        service._append_segment(
            {
                "start": "10:10",
                "end": "10:40",
                "event": "去厨房",
                "location": "厨房",
                "emotion": "平常",
                "energy_rate": 0.3,
            },
            "2026-08-22",
        )
        suspended = service._suspended_slot()
        self.assertEqual(suspended.get("event"), "做海报")
        self.assertEqual(suspended.get("energy_rate"), 0.2, "挂起快照丢了能量速率")
        resumed = parse_segment(
            {"resume": True}, now_minute=10 * 60 + 15, step=15, suspended=suspended
        )
        self.assertIsNotNone(resumed)
        self.assertEqual(resumed["energy_rate"], 0.2)


class NoFutureInDayPhrasesTest(unittest.TestCase):
    """#73：segments() 只返回 start <= now，future 分支与 future_limit 都是死的。"""

    def test_next_is_always_empty(self):
        slots = [
            {"start": "13:00", "end": "14:00", "event": "有点饿了", "location": "客厅", "status": True}
        ]
        out = day_phrases(slots, 12 * 60)
        self.assertEqual(out["next"], [], "滚动分段没有预排的未来")
        self.assertIn("next", out, "契约里的键不能因为恒空就删掉")


class FailedGenerationKeepsTheEventTest(unittest.TestCase):
    """#75：生成真的失败了，刚取走的「来访」和「事件」必须还回去。"""

    class _FailingGateway:
        async def generate(self, **kwargs):
            return LLMResult(ok=False, outcome=OUTCOME_TIMEOUT, detail="boom")

    def test_a_timeout_does_not_eat_the_interruption_or_the_event(self):
        service, _ = _service(
            datetime(2026, 8, 22, 10, 10, tzinfo=TZ), use_llm_schedule=True
        )
        service.set_resolver_gateway(FakeContext(), self._FailingGateway())
        service.note_interruption(now=9_000.0)
        self.assertTrue(service.note_event(kind="很上心", now=9_500.0))
        phrase = service._event_phrase
        self.assertTrue(phrase)

        asyncio.run(service.ensure_fresh(force=True))

        self.assertEqual(service._last_outcome, OUTCOME_TIMEOUT)
        self.assertEqual(service._interrupt_at, 9_000.0, "「有人来过」被这次超时吃掉了")
        self.assertEqual(service._event_phrase, phrase, "「刚刚发生的事」被这次超时吃掉了")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
