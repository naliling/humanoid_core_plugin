"""v3.1.29：碎段、「忙」这类空话、以及今天日程里残留的旧空话段。

三件事都来自线上真实数据：

* 13:15–13:18 的「把窗户打开，站在阳台上吹了会儿风」——被下一段从中间截断后
  留下的一小截。3 分钟的段不是一段生活，是拼接留下的碎渣。
* `2625379556` 的「忙自己的事」「忙手里的活」、`3887128092` 的「手头有东西要处理」
  ——3.1.23 之前写进 state 的处境词，带 `status: true` 所以 purge v2 与读路径都放过了，
  今天日程里照旧露出来，读者还是不知道她在做什么。
* `dynamic_segment` 上午/下午那两支还在生产「忙自己的事 / 做事」这种空话，
  下次没人说话时又会补一条进去。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.services.schedule import (
    MIN_TRUNCATED_STUB_MINUTES,
    PURGE_KEY,
    PURGE_VERSION,
    ScheduleService,
    dynamic_segment,
)
from humanoid.services.schedule_content import content_issues, low_info_issues

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


class _Base(unittest.TestCase):
    moment = datetime(2026, 8, 22, 13, 10, tzinfo=TZ)

    def build(self, moment=None):
        conf = cfg(
            schedule_min_interval_minutes=0,
            schedule_max_interval_minutes=0,
            llm_idle_silence_minutes=0,
            llm_daily_call_budget=100,
        )
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load(FIXED_TODAY, conf.cycle_length)
        log = RecordingLogger()
        service = ScheduleService(
            store.scope,
            lambda: conf,
            FrozenClock(moment or self.moment),
            logger=log,
            monotonic=lambda: 10_000.0,
        )
        return service, conf

    def seed(self, service, segments):
        service._scope.update_self(today_date=FIXED_TODAY, daily_schedule=list(segments))


class ShortStubTest(_Base):
    """被截断后剩下的那一小截，短于阈值就整段撤掉。"""

    def test_a_five_minute_stub_is_dropped(self):
        service, _conf = self.build()
        self.seed(service, [slot("13:00", "14:00", "把碗洗了")])
        service._append_segment(slot("13:05", "14:05", "把地扫了"), FIXED_TODAY)
        segs = service.segments()
        self.assertEqual([item["event"] for item in segs], ["把地扫了"])

    def test_a_twenty_minute_stub_is_kept(self):
        service, _conf = self.build(datetime(2026, 8, 22, 13, 25, tzinfo=TZ))
        self.seed(service, [slot("13:00", "14:00", "把碗洗了")])
        service._append_segment(slot("13:20", "14:20", "把地扫了"), FIXED_TODAY)
        segs = service.segments()
        self.assertEqual([item["event"] for item in segs], ["把碗洗了", "把地扫了"])
        self.assertEqual(segs[0]["end"], "13:20")

    def test_a_sleep_stub_is_never_dropped(self):
        service, _conf = self.build()
        self.seed(service, [slot("13:00", "14:00", "睡一觉", status=True, sleep=True)])
        service._append_segment(slot("13:05", "14:05", "把地扫了"), FIXED_TODAY)
        segs = service.segments()
        self.assertEqual([item["event"] for item in segs], ["睡一觉", "把地扫了"])

    def test_the_threshold_is_used(self):
        service, _conf = self.build()
        self.seed(service, [slot("13:00", "14:00", "把碗洗了")])
        service._append_segment(
            slot("13:00", "14:00", "把地扫了"), FIXED_TODAY
        )
        # 起点相同 → 旧段整段被换掉，不留零长度记录
        self.assertEqual([item["event"] for item in service.segments()], ["把地扫了"])
        self.assertGreaterEqual(MIN_TRUNCATED_STUB_MINUTES, 10)


class PurgeVagueStatusTest(_Base):
    """带 status 的空话也是空话：purge 要删，真处境词和觉要留。"""

    def test_vague_status_segments_are_purged(self):
        service, _conf = self.build()
        self.seed(
            service,
            [
                slot("08:36", "10:06", "忙自己的事", status=True),
                slot("10:06", "11:12", "忙手里的活", status=True),
                slot("11:12", "12:24", "歇着", status=True),
                slot("03:00", "08:00", "睡一觉", status=True, sleep=True),
            ],
        )
        service._purge_dirty_history()
        # 断言 state 里的原文（`segments()` 会把还没到的段挡掉，这里要看全表）
        events = [
            item.get("event")
            for item in (service._scope.get_self("daily_schedule") or [])
        ]
        self.assertEqual(events, ["歇着", "睡一觉"])
        self.assertEqual(service._scope.get_self(PURGE_KEY), PURGE_VERSION)

    def test_a_purged_role_is_not_purged_again(self):
        service, _conf = self.build()
        self.seed(service, [slot("10:36", "12:06", "忙自己的事", status=True)])
        service._purge_dirty_history()
        self.seed(service, [slot("10:36", "12:06", "忙自己的事", status=True)])
        self.assertEqual(service._purge_dirty_history(), 0)
        self.assertEqual(len(service.segments()), 1)

    def test_short_stubs_are_purged_too(self):
        """已经落盘的碎段（3 分钟的窗户、11 分钟的夜里醒着）也一起删；觉不删。"""
        service, _conf = self.build()
        self.seed(
            service,
            [
                slot("13:15", "13:18", "把窗户打开，站在阳台上吹了会儿风"),
                slot("00:00", "00:11", "夜里醒着"),
                slot("03:00", "03:03", "睡一觉", status=True, sleep=True),
                slot("08:00", "09:30", "把碗洗了，灶台擦了一遍"),
            ],
        )
        service._purge_dirty_history()
        events = [
            item.get("event")
            for item in (service._scope.get_self("daily_schedule") or [])
        ]
        self.assertEqual(events, ["睡一觉", "把碗洗了，灶台擦了一遍"])


class DynamicSegmentIsConcreteTest(_Base):
    """没人说话时的兜底，上午/下午也要给具体的事，不能给「忙自己的事」。"""

    BODY = {
        "energy": 70.0,
        "hunger": 10.0,
        "sleep_pressure": 10.0,
        "sleep_debt": 0.0,
        "discomfort": 0.0,
        "chill": 0.0,
        "arousal": 20.0,
        "social_desire": 20.0,
        "asleep": 0.0,
    }

    def test_forenoon_and_afternoon_are_concrete(self):
        _service, conf = self.build()
        for now_minute in (600, 900):
            for seed in (("r", "d"), ("r", "x"), ("r", "y"), ("r", "z")):
                picked = dynamic_segment(
                    conf,
                    self.BODY,
                    now_minute=now_minute,
                    step=15,
                    prev=None,
                    seed=seed,
                    asleep_minutes=0.0,
                )
                event = str(picked.get("event") or "")
                self.assertEqual(content_issues(event), [])
                self.assertEqual(
                    low_info_issues(event), [], f"{now_minute} 点给了空话：{event}"
                )


if __name__ == "__main__":
    unittest.main()
