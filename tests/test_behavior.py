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


def _scope_with(users=None):
    from humanoid.role_scope import RoleScope

    return RoleScope(
        {"_state_version": 3, "roles": {"role-a": {"self": {}, "users": dict(users or {})}}},
        "role-a",
        lambda: None,
    )


def _core_with(scope, log=None):
    return SimpleNamespace(
        role_id="role-a",
        config=FakeCore.config,
        mood=FakeMood(),
        scope=scope,
        _log=log,
    )


class PeekUserAcceptsNumericIds(unittest.TestCase):
    """`peek_user(123, ...)` 必须查到 `users["123"]`。

    其余读写口（`user_state` / `get_user` / `set_user` / `delete_user`）全都 `str()`，
    只有 `peek_user` 不做——于是数字 id 的调用方**静默拿到默认值**，看起来像「没这条数据」。
    """

    def test_numeric_id_finds_the_string_entry(self):
        scope = _scope_with()
        scope.set_user("123", "nickname", "小明")
        self.assertEqual(scope.peek_user("123", "nickname"), "小明")
        self.assertEqual(scope.peek_user(123, "nickname"), "小明", "数字 id 查不到 = 静默返回默认值")

    def test_peek_never_creates_an_entry(self):
        scope = _scope_with()
        self.assertIsNone(scope.peek_user("nobody", "nickname"))
        self.assertEqual(scope.all_user_ids(), [], "只读探测不该建条目")


class CareDoesNotCreateStrangerEntries(unittest.TestCase):
    """P1：注入热路径读一下就给群里每个陌生人留一个**永久空壳**。

    `care()` 走 `scope.get_user`，那个方法 `users.setdefault()` 读就建；空壳里没有任何
    会被过期清理的字段（`prune_expired` 直接跳过），于是 state.json 随群规模一直涨。
    """

    def test_stranger_gets_no_entry(self):
        scope = _scope_with()
        service = BehaviorService(_core_with(scope))
        value = service.care("stranger", 1000.0)
        self.assertTrue(0.0 <= value <= 1.0)
        self.assertEqual(scope.all_user_ids(), [], "读一次就给陌生人建了空壳")

    def test_interest_state_does_not_create_either(self):
        scope = _scope_with()
        core = _core_with(scope)
        core.config = SimpleNamespace(
            last_interaction_threshold_minutes=5,
            last_interaction_mode="simple",
            mood_enabled=True,
            mood_enabled_in_group=False,
        )
        core.mood = SimpleNamespace(
            readable=lambda *a, **k: True,
            profile=lambda uid: {"affection": 50.0},
        )
        service = BehaviorService(core)
        service.interest_state("stranger", 1000.0, text="你好")
        self.assertEqual(scope.all_user_ids(), [], "在意度不该顺手把用户条目写出来")

    def test_existing_record_is_still_updated(self):
        scope = _scope_with({"42": {"attention": {"care": 0.1, "care_at": 0.0}}})
        service = BehaviorService(_core_with(scope))
        service.care("42", 100000.0)
        self.assertIn("attention", scope.user_state("42"))


class EventLifecycle(unittest.TestCase):
    """短期事件与「回来这一轮」事件都只在用户下次发言时清——从不按时间回收。"""

    def setUp(self):
        self.service = BehaviorService(FakeCore())

    def test_expired_events_are_dropped_and_the_key_removed(self):
        from humanoid.services.behavior import EVENT_TTL

        self.service.add_event("u", self.service._make_event("user_returned", 1000.0, 0.5))
        self.assertEqual(self.service._clean("u", 1000.0 + EVENT_TTL + 1), [])
        self.assertNotIn("role-a:u", self.service._events, "空列表不该留在字典里")

    def test_active_event_is_swept_by_ttl(self):
        from humanoid.services.behavior import EVENT_TTL

        self.service.add_event("u", self.service._make_event("user_returned", 1000.0, 0.5))
        self.assertIn("role-a:u", self.service._active_event)
        self.service.compute_attention("u", 1000.0 + EVENT_TTL + 1)
        self.assertNotIn(
            "role-a:u", self.service._active_event,
            "只按用户清的话，群里再也不说话的人会一直留着",
        )

    def test_nan_now_warns_instead_of_silently_early_returning(self):
        from .fakes import RecordingLogger

        log = RecordingLogger()
        service = BehaviorService(_core_with(_scope_with(), log=log))
        self.assertIsNone(service.process_interval("u", float("nan"), 1000.0))
        self.assertIn("非有限", log.text("warning"))


class RecallChunkCost(unittest.TestCase):
    """`_chunks` 以前连 2 字片段一起抽（约 117 个/条），再对 6 条原话各跑一遍。"""

    def test_no_two_char_fragments(self):
        from humanoid.services.behavior import _chunks

        frags = _chunks("今天跟产品经理吵了一架")
        self.assertTrue(frags)
        self.assertTrue(all(len(f) >= 3 for f in frags), "2 字片段没有判别力，只会翻倍搜索次数")
        self.assertLess(len(frags), 60)

    def test_touches_recalled_matches_across_lines(self):
        class RecallMood(FakeMood):
            def recall_lines(self, user_id, limit=6):
                return ["TA之前说过「我猫今天吐了」"]

        core = _core_with(_scope_with())
        core.mood = RecallMood()
        service = BehaviorService(core)
        self.assertTrue(service._touches_recalled("u", "我家猫今天吐了"))
        self.assertFalse(service._touches_recalled("u", "今天天气不错"))


if __name__ == "__main__":
    unittest.main()
