"""v3.1.20：自愈不能被「有一轮在跑」挡住，卡住的生成也不能把时间线永久锁死。

v3.1.19 的 `heal_gap()` 放在 `request_refresh` 里两个「忙」判断之后：

    if self._task and not self._task.done(): return False
    if self._generating: return False
    self.heal_gap()

线上就撞上了这个顺序的后果——某个角色的一轮生成卡住（协程不结束，`_generating`
或 `_task` 永远不复位），后台每 30 秒都在那两行返回，自愈一次都轮不到，日程继续
停在几小时前。这组测试盯住两件事：

1. 自愈必须在任何「忙」判断之前执行；
2. 卡过 `GENERATION_STALL_SECONDS` 的闸必须强制复位/取消，否则下一轮还是被挡。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.llm import LLMGateway, ProviderResolver
from humanoid.services.schedule import (
    GENERATION_STALL_SECONDS,
    SOURCE_TEMPLATE,
    ScheduleService,
)

from .fakes import FakeContext, FakeProvider, FrozenClock, RecordingLogger, ScopeStore
from .test_schedule import cfg, seg_reply

TZ = ZoneInfo("Asia/Shanghai")
FIXED_TODAY = "2026-08-22"
HOLE_MOMENT = datetime(2026, 8, 22, 9, 30, tzinfo=TZ)


def slot(start: str, end: str, event: str = "在窗边坐着", location: str = "家里") -> dict:
    return {
        "start": start,
        "end": end,
        "event": event,
        "location": location,
        "energy_rate": 0.0,
    }


class _PendingTask:
    """一个永远不结束的后台任务替身：用来模拟「协程卡住」。"""

    def __init__(self) -> None:
        self.cancelled = False

    def done(self) -> bool:
        return False

    def cancel(self) -> None:
        self.cancelled = True


class StallAndHealTest(unittest.TestCase):
    def build(self, moment=HOLE_MOMENT):
        conf = cfg(
            schedule_provider_name="p",
            schedule_min_interval_minutes=0,
            schedule_max_interval_minutes=0,
            llm_idle_silence_minutes=0,
            llm_daily_call_budget=100,
        )
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load(FIXED_TODAY, conf.cycle_length)
        log = RecordingLogger()
        provider = FakeProvider("p", reply=seg_reply(minutes=40))
        resolver = ProviderResolver(FakeContext(chat_providers=[provider]), log)
        gateway = LLMGateway(resolver, lambda: conf, log, time.monotonic)
        service = ScheduleService(
            store.scope,
            lambda: conf,
            FrozenClock(moment),
            logger=log,
            monotonic=time.monotonic,
        )
        service.set_resolver_gateway(resolver, gateway)
        return service, provider

    def seed_hole(self, service):
        service._scope.update_self(
            today_date=FIXED_TODAY,
            daily_schedule=[slot("00:00", "01:35", "肚子空着")],
        )

    # ------------------------------------------------------------------
    # 自愈必须在「忙」判断之前
    # ------------------------------------------------------------------

    def test_heal_runs_while_a_generation_is_in_flight(self):
        service, provider = self.build()
        self.seed_hole(service)
        service._generating = True
        service._generating_since = time.monotonic()
        self.assertFalse(service.request_refresh(), "在跑的时候不该再排一轮")
        segs = service.segments()
        self.assertEqual(len(segs), 2, "在跑也必须把空档接上")
        self.assertIsNotNone(service._covering(segs, 9 * 60 + 30))
        self.assertEqual(provider.calls, 0)

    def test_heal_runs_while_a_task_is_pending(self):
        service, provider = self.build()
        self.seed_hole(service)
        service._task = _PendingTask()
        service._task_since = time.monotonic()
        self.assertFalse(service.request_refresh())
        self.assertEqual(len(service.segments()), 2)
        self.assertEqual(provider.calls, 0)

    def test_heal_marks_source_even_when_busy(self):
        service, _provider = self.build()
        self.seed_hole(service)
        service._generating = True
        service._generating_since = time.monotonic()
        service.request_refresh()
        self.assertEqual(service.source, SOURCE_TEMPLATE)

    # ------------------------------------------------------------------
    # 卡住看门狗
    # ------------------------------------------------------------------

    def test_fresh_generation_blocks(self):
        service, _provider = self.build()
        service._generating = True
        service._generating_since = time.monotonic()
        self.assertTrue(service._busy())

    def test_stuck_generation_is_reset(self):
        service, _provider = self.build()
        service._generating = True
        service._generating_since = time.monotonic() - GENERATION_STALL_SECONDS - 1
        self.assertFalse(service._busy(), "卡过阈值就该放行")
        self.assertFalse(service._generating)
        self.assertEqual(service._generating_since, 0.0)
        self.assertEqual(service._quality.get("stall"), 1)

    def test_stuck_generation_reset_is_persisted(self):
        service, _provider = self.build()
        service._generating = True
        service._generating_since = time.monotonic() - GENERATION_STALL_SECONDS - 1
        service._busy()
        stats = service._scope.get_self("schedule_quality_stats", {}) or {}
        self.assertEqual(stats.get("stall"), 1, "卡住次数要能跨重启看见")

    def test_stuck_task_is_cancelled(self):
        service, _provider = self.build()
        task = _PendingTask()
        service._task = task
        service._task_since = time.monotonic() - GENERATION_STALL_SECONDS - 1
        self.assertFalse(service._busy())
        self.assertTrue(task.cancelled, "卡住的后台任务要被取消，否则永远占着闸")
        self.assertIsNone(service._task)
        self.assertEqual(service._task_since, 0.0)

    def test_fresh_task_blocks(self):
        service, _provider = self.build()
        service._task = _PendingTask()
        service._task_since = time.monotonic()
        self.assertTrue(service._busy())

    def test_a_stuck_task_is_healed_and_replaced(self):
        """卡住 → 取消 → 下一轮自愈照样补上（这条就是线上那次的实际路径）。"""
        service, provider = self.build()
        self.seed_hole(service)
        task = _PendingTask()
        service._task = task
        service._task_since = time.monotonic() - GENERATION_STALL_SECONDS - 1
        service.refresh_due = lambda: False
        self.assertFalse(service.request_refresh())
        self.assertTrue(task.cancelled)
        self.assertEqual(len(service.segments()), 2)
        self.assertEqual(provider.calls, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
