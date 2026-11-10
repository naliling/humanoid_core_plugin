"""v3.1.30：每个角色都带一张质量计数表，全 0 也算有。

以前只在命中闸门时才写 `schedule_quality_stats`，于是从没触发过闸的角色永远是
「没有计数」，面板和巡检都读不到东西——看起来像没在统计，其实是一次都没发生。
现在角色一启动就把九项计数写成 0，命中时照常累加。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.services.schedule import (
    QUALITY_KEYS,
    QUALITY_STATS_KEY,
    ScheduleService,
)

from .fakes import FrozenClock, RecordingLogger, ScopeStore
from .test_schedule import cfg

TZ = ZoneInfo("Asia/Shanghai")
FIXED_TODAY = "2026-08-22"


def slot(start: str, end: str, event: str, **extra) -> dict:
    body = {
        "start": start,
        "end": end,
        "event": event,
        "location": "家里",
        "energy_rate": 0.0,
    }
    body.update(extra)
    return body


class QualityStatsTableTest(unittest.TestCase):
    moment = datetime(2026, 8, 22, 13, 10, tzinfo=TZ)

    def build(self):
        conf = cfg(
            schedule_min_interval_minutes=0,
            schedule_max_interval_minutes=0,
            llm_idle_silence_minutes=0,
            llm_daily_call_budget=100,
        )
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load(FIXED_TODAY, conf.cycle_length)
        service = ScheduleService(
            store.scope,
            lambda: conf,
            FrozenClock(self.moment),
            logger=RecordingLogger(),
            monotonic=lambda: 10_000.0,
        )
        return service, conf

    def test_a_fresh_role_gets_a_full_zero_table(self):
        service, _conf = self.build()
        self.assertIsNone(service._scope.get_self(QUALITY_STATS_KEY))
        service._ensure_quality_stats()
        stats = service._scope.get_self(QUALITY_STATS_KEY)
        self.assertEqual(stats, {key: 0 for key in QUALITY_KEYS})
        self.assertIn("low_info", stats, "空话也要有计数位")
        self.assertIn("content_final", stats, "最终硬闸也要有计数位")

    def test_existing_counts_are_kept_and_missing_keys_are_filled(self):
        service, _conf = self.build()
        service._scope.set_self(QUALITY_STATS_KEY, {"repeat": 3, "stall": 1})
        service._ensure_quality_stats()
        stats = service._scope.get_self(QUALITY_STATS_KEY)
        self.assertEqual(stats["repeat"], 3, "已有计数不能被清零")
        self.assertEqual(stats["stall"], 1)
        self.assertEqual(stats["invented"], 0, "缺的键补 0")
        self.assertEqual(set(QUALITY_KEYS) - set(stats), set())

    def test_heal_gap_writes_the_table_on_the_write_path(self):
        service, _conf = self.build()
        # 有段罩住此刻：heal_gap 不会补段，但它在补段判断之前就写计数表
        service._scope.update_self(
            today_date=FIXED_TODAY,
            daily_schedule=[slot("12:30", "14:00", "把碗洗了，灶台擦了一遍")],
        )
        self.assertFalse(service.heal_gap())
        stats = service._scope.get_self(QUALITY_STATS_KEY)
        self.assertEqual(stats, {key: 0 for key in QUALITY_KEYS})

    def test_status_exposes_the_zero_table(self):
        service, _conf = self.build()
        service._ensure_quality_stats()
        total = service.status()["quality_total"]
        self.assertEqual(total, {key: 0 for key in QUALITY_KEYS})

    def test_a_bump_still_accumulates(self):
        service, _conf = self.build()
        service._ensure_quality_stats()
        service._bump_quality("rewrite")
        service._bump_quality("rewrite")
        service._ensure_quality_stats()  # 再补一次不能把计数抹平
        stats = service._scope.get_self(QUALITY_STATS_KEY)
        self.assertEqual(stats["rewrite"], 2)
        self.assertEqual(stats["repeat"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
