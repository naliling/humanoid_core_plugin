from __future__ import annotations

import unittest
from types import SimpleNamespace

from humanoid.services.behavior import BehaviorService


class FakeMood:
    def profile(self, user_id):
        return {"affection": 50.0}


class FakeCore:
    role_id = "role-a"
    config = SimpleNamespace(last_interaction_threshold_minutes=5, last_interaction_mode="simple")
    mood = FakeMood()


class BehaviorServiceTest(unittest.TestCase):
    def setUp(self):
        self.service = BehaviorService(FakeCore())

    def test_no_event_before_five_minutes(self):
        self.assertIsNone(self.service.process_interval("u", 1299, 1000))

    def test_gap_event_keeps_old_timestamp(self):
        event = self.service.process_interval("u", 1000 + 10800, 1000)
        self.assertEqual(event["type"], "user_returned")
        self.assertEqual(event["data"]["gap_bucket"], "long_return")
        self.assertEqual(event["data"]["gap_seconds"], 10800)

    def test_previous_message_is_captured_when_enabled(self):
        self.service._core.config.last_interaction_mode = "with_last_msg"
        event = self.service.process_interval(
            "u", 5000, 4000, {"text": "上一句话"}
        )
        self.assertEqual(event["data"]["previous_message"], "上一句话")

    def test_attention_is_explicit(self):
        event = self.service.process_interval("u", 10000, 1000)
        self.service.add_event("u", event)
        events = self.service.compute_attention("u", 10000)
        self.assertEqual(len(events), 1)
        self.assertIn("attention", events[0])
        self.assertGreater(events[0]["attention"], 0)

    def test_agency_always_exists(self):
        agency = self.service.compute_agency("u", [], 80, {"affection": 50}, 80)
        self.assertEqual(
            set(agency),
            {"initiative", "curiosity", "care", "social_willingness", "continuation"},
        )
        self.assertTrue(all(0 <= v <= 1 for v in agency.values()))


if __name__ == "__main__":
    unittest.main()
