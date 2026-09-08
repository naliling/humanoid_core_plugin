from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone

from humanoid.clock import Clock
from humanoid.config import HumanoidConfig
from humanoid.role_scope import RoleScope
from humanoid.services.behavior import BehaviorService
from humanoid.services.schedule import ScheduleService


class FakeLog:
    def info(self, *args, **kwargs): pass
    def debug(self, *args, **kwargs): pass
    def warning(self, *args, **kwargs): pass


class FakeGateway:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0

    async def generate(self, **kwargs):
        self.calls += 1
        return type("Result", (), {
            "ok": True,
            "text": self.reply,
            "summary": lambda self: "ok",
        })()


class FrozenClock:
    def __init__(self, dt):
        self.dt = dt

    def now(self):
        return self.dt

    def today_str(self):
        return self.dt.strftime("%Y-%m-%d")

    def weekday(self):
        return "三"


GOOD = '[{"start":"00:00","end":"08:00","event":"睡眠","location":"卧室","emotion":"平静","energy_rate":0.1},{"start":"08:00","end":"18:00","event":"工作","location":"书房","emotion":"专注","energy_rate":-0.1},{"start":"18:00","end":"24:00","event":"休闲","location":"客厅","emotion":"轻松","energy_rate":0.05}]'


class ScheduleRegressionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = {
            "roles": {
                "bot": {
                    "self": {
                        "today_date": "2026-09-09",
                        "daily_schedule": [
                            {"start": "00:00", "end": "24:00", "event": "已保存的大模型日程",
                             "location": "家中", "emotion": "平静", "energy_rate": 0.0}
                        ],
                        "schedule_source": "llm",
                        "schedule_generated_at": "2026-09-09 00:01:00",
                    },
                    "users": {},
                }
            }
        }
        self.cfg = HumanoidConfig(schedule_provider_name="p1", schedule_allow_global_fallback=False)
        self.clock = FrozenClock(datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc))
        self.scope = RoleScope(self.state, "bot")

    async def test_same_day_never_replaces_saved_schedule_with_template(self):
        service = ScheduleService(self.scope, lambda: self.cfg, self.clock, logger=FakeLog())
        self.assertEqual(service.current_slots()[0]["event"], "已保存的大模型日程")
        self.assertEqual(self.scope.get_self("schedule_source"), "llm")

    async def test_new_day_template_is_transient_until_llm_succeeds(self):
        self.clock.dt = datetime(2026, 9, 10, 0, 1, tzinfo=timezone.utc)
        service = ScheduleService(self.scope, lambda: self.cfg, self.clock, logger=FakeLog())
        temporary = service.current_slots()
        self.assertNotEqual(temporary[0]["event"], "已保存的大模型日程")
        self.assertEqual(self.scope.get_self("today_date"), "2026-09-09")
        self.assertEqual(self.scope.get_self("schedule_source"), "llm")

        service.gateway = FakeGateway(GOOD)
        changed = await service.ensure_fresh()
        self.assertTrue(changed)
        self.assertEqual(self.scope.get_self("today_date"), "2026-09-10")
        self.assertEqual(self.scope.get_self("schedule_source"), "llm")
        self.assertEqual(service.current_slots()[0]["event"], "睡眠")


class BehaviorRegressionTest(unittest.TestCase):
    def test_gap_event_keeps_last_message_for_the_whole_return_turn(self):
        class Mood:
            def profile(self, user_id):
                return {"affection": 50}

        class Core:
            role_id = "bot"
            config = HumanoidConfig(last_interaction_mode="with_last_msg")
            mood = Mood()

        service = BehaviorService(Core())
        event = service.process_interval(
            "u", 1000.0, 400.0, {"text": "离开前的最后一句话"}
        )
        self.assertIsNotNone(event)
        service.add_event("u", event)
        first = service.consume_relevant_events("u", 1001.0)
        second = service.consume_relevant_events("u", 1002.0)
        self.assertEqual(first[0]["data"]["previous_message"], "离开前的最后一句话")
        self.assertEqual(second[0]["data"]["previous_message"], "离开前的最后一句话")
        service.clear_user_events("u")
        self.assertEqual(service.consume_relevant_events("u", 1003.0), [])


if __name__ == "__main__":
    unittest.main()
