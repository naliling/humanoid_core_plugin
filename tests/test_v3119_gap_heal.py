"""v3.1.19：时间线断了就自己接上，不等模型、不等闸门。

线上观察到的形态：某个角色的 `daily_schedule` 停在上一段结束处（例如 01:35），
后台循环还在跑、身体还在动，但之后几个小时没有任何一段罩住此刻。原因是「被闸门
挡回后用模板补位」那条路只有在**真的走了一次生成**时才会执行——一旦那次生成没
发生（旧进程、卡住的刷新任务、闸门长期关着），补位就永远不会发生。

`heal_gap()` 把补位从生成路径里拆出来：只看「此刻有没有段罩着」，没有就本地补
一段处境词。它不调模型、不过闸门、不受间隔与预算影响，因此不会因为外部条件而
长期失效。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.llm import LLMGateway, ProviderResolver
from humanoid.services.schedule import SOURCE_TEMPLATE, ScheduleService

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


class GapHealTest(unittest.TestCase):
    def build(self, moment=HOLE_MOMENT, reply=None):
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
        provider = FakeProvider("p", reply=reply or seg_reply(minutes=40))
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

    def seed(self, service, segments):
        service._scope.update_self(today_date=FIXED_TODAY, daily_schedule=list(segments))

    def test_a_hole_is_healed(self):
        service, _provider = self.build()
        self.seed(service, [slot("00:00", "01:35", "肚子空着")])
        self.assertTrue(service.heal_gap())
        segs = service.segments()
        self.assertEqual(len(segs), 2)
        last = segs[-1]
        self.assertTrue(str(last.get("event") or "").strip())
        self.assertIsNotNone(
            service._covering(segs, 9 * 60 + 30), "补完之后必须有一段罩住此刻"
        )

    def test_nothing_happens_when_covered(self):
        service, _provider = self.build()
        self.seed(service, [slot("08:00", "10:00")])
        self.assertFalse(service.heal_gap())
        self.assertEqual(len(service.segments()), 1)

    def test_nothing_happens_without_segments(self):
        """一天还没开始（没有任何段）：这是首段的活，不该由自愈代劳。"""
        service, _provider = self.build()
        self.seed(service, [])
        self.assertFalse(service.heal_gap())
        self.assertEqual(service.segments(), [])

    def test_future_segment_is_not_touched(self):
        """只有未来段（时钟回拨/乱序）：结束时间晚于此刻，不补。"""
        service, _provider = self.build()
        self.seed(service, [slot("10:00", "11:00")])
        before = len(service.segments())
        self.assertFalse(service.heal_gap())
        self.assertEqual(len(service.segments()), before)

    def test_heal_never_calls_the_model(self):
        service, provider = self.build()
        self.seed(service, [slot("00:00", "01:35")])
        self.assertTrue(service.heal_gap())
        self.assertEqual(provider.calls, 0)
        self.assertEqual(provider.review_calls, 0)

    def test_heal_marks_the_source_as_template(self):
        service, _provider = self.build()
        self.seed(service, [slot("00:00", "01:35")])
        service.heal_gap()
        self.assertEqual(service.source, SOURCE_TEMPLATE)

    def test_request_refresh_heals_before_asking_whether_to_generate(self):
        """自愈放在「该不该问模型」之前：闸门/间隔都挡不住它。"""
        service, provider = self.build()
        self.seed(service, [slot("00:00", "01:35")])
        service.refresh_due = lambda: False  # 模拟「还没到点」
        self.assertFalse(service.request_refresh())
        self.assertEqual(len(service.segments()), 2)
        self.assertEqual(provider.calls, 0)

    def test_heal_gives_a_concrete_thing_not_a_status_word(self):
        """v3.1.23：能挑到具体事就别用「忙自己的事」这类处境词糊过去。"""
        from humanoid.services.schedule_content import content_issues, low_info_issues

        service, _provider = self.build()
        self.seed(service, [slot("00:00", "01:35")])
        self.assertTrue(service.heal_gap())
        healed = service.segments()[-1]
        event = str(healed.get("event") or "")
        self.assertTrue(event)
        self.assertFalse(healed.get("status"), f"补出来的仍是处境词：{event}")
        self.assertEqual(content_issues(event), [])
        self.assertEqual(low_info_issues(event), [])
        self.assertEqual(healed.get("start"), "09:30")
        self.assertTrue(str(healed.get("location") or "").strip())

    def test_heal_avoids_repeating_the_previous_event(self):
        """补位不该把上一段刚做过的事再写一遍。"""
        service, _provider = self.build()
        self.seed(service, [slot("00:00", "01:35", "把桌上的小盒子拆了又装回去")])
        self.assertTrue(service.heal_gap())
        self.assertNotEqual(
            str(service.segments()[-1].get("event") or ""),
            "把桌上的小盒子拆了又装回去",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
