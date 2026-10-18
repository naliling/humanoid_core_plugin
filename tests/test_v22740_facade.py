"""契约 v2 只读门面（Core 侧）。

门面是给**别的插件**用的接口，所以它的失败方式比它的成功方式更重要：

* 只读——不许建角色、不许写状态、不许推进身体；
* 不抛——任何异常都退化成 `None` / `[]` / `{}`，否则会把对方的消息链路一起带崩；
* 不猜——拿不到就是拿不到，不编默认值。
"""

from __future__ import annotations

import unittest

from humanoid.api import API_VERSION, CAPABILITIES, CoreFacade, get_core_facade

from .test_v2273_life_note import NOW, _Harness


class FacadeDiscoveryTest(unittest.TestCase):
    """拿不到角色管理器时必须返回 None，而不是造一个空门面。"""

    def test_no_roles_returns_none(self):
        self.assertIsNone(get_core_facade(None))

    def test_facade_is_built_from_roles(self):
        h = _Harness()
        try:
            facade = get_core_facade(h.roles)
            self.assertIsNotNone(facade)
            self.assertEqual(facade.api_version(), API_VERSION)
            self.assertEqual(list(facade.capabilities()), list(CAPABILITIES))
            self.assertEqual(facade.role_ids(), ["bot1"])
        finally:
            h.store.flush_sync()


class FacadeReadOnlyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.h = _Harness()
        self.facade = CoreFacade(self.h.roles)

    def tearDown(self) -> None:
        self.h.store.flush_sync()

    def test_unknown_role_is_none_and_creates_nothing(self):
        before = list(self.h.roles.get_all())
        self.assertIsNone(self.facade.brief("不存在的角色"))
        self.assertIsNone(self.facade.brief("不存在的角色", "10001"))
        self.assertEqual(self.facade.clock("不存在的角色"), {})
        self.assertEqual(self.facade.events("不存在的角色"), [])
        self.assertEqual(self.facade.state_fingerprint("不存在的角色"), "")
        self.assertEqual(
            [inst.role_id for inst in self.h.roles.get_all()],
            [inst.role_id for inst in before],
            "门面不该因为一次查询就凭空造出一个角色",
        )

    def test_brief_does_not_touch_state(self):
        before = dict(self.h.core.scope.self_state)
        self.facade.brief("bot1", "10001")
        self.facade.brief("bot1", "10001", task="proactive")
        after = dict(self.h.core.scope.self_state)
        self.assertEqual(before.keys(), after.keys())
        for key in ("energy", "social_energy", "today_date"):
            self.assertEqual(before.get(key), after.get(key), key)


class BriefShapeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.h = _Harness()
        self.facade = CoreFacade(self.h.roles)

    def tearDown(self) -> None:
        self.h.store.flush_sync()

    def test_chat_brief_has_the_shared_framing(self):
        brief = self.facade.brief("bot1", "10001", task="chat")
        self.assertIsNotNone(brief)
        self.assertEqual(brief["v"], API_VERSION)
        self.assertEqual(brief["task"], "chat")
        # 框架句必须与普通回复**同一句**：人格一致性靠这个，而不是靠两边写得像
        from humanoid.prompt_builder import build_framing

        self.assertEqual(brief["persona_framing"], build_framing(self.h.core.char_name(), ""))
        self.assertIn("day", brief)
        self.assertIn("self_state", brief)
        self.assertIn("ongoing", brief)

    def test_proactive_brief_also_carries_events(self):
        chat = self.facade.brief("bot1", "10001", task="chat")
        proactive = self.facade.brief("bot1", "10001", task="proactive")
        self.assertNotIn("events", chat or {}, "chat 任务不该多带事件，省预算")
        self.assertIn("events", proactive or {})

    def test_ongoing_items_carry_ids(self):
        self.h.publish("他上次说的那个面试")
        brief = self.facade.brief("bot1", "10001", task="proactive")
        ongoing = (brief or {}).get("ongoing") or []
        self.assertTrue(ongoing, "发布了未完事项，brief 里应该有")
        for item in ongoing:
            self.assertTrue(str(item.get("id") or "").startswith(("og:", "ll:")), item)
            self.assertTrue(item.get("text"))

    def test_fingerprint_is_stable_until_state_changes(self):
        first = self.facade.state_fingerprint("bot1")
        second = self.facade.state_fingerprint("bot1")
        self.assertEqual(first, second)
        self.h.publish("新的一件事")
        third = self.facade.state_fingerprint("bot1")
        self.assertNotEqual(first, third, "未完事项变了，指纹应该跟着变")

    def test_clock_is_computed_not_read_from_contract(self):
        clock = self.facade.clock("bot1")
        self.assertEqual(clock.get("utc_offset_minutes"), 480, "北京是 UTC+8")
        self.assertAlmostEqual(float(clock.get("epoch") or 0), NOW, delta=1.0)
        self.assertEqual(clock.get("date"), "2026-08-22")


class NeverRaisesTest(unittest.TestCase):
    """角色对象坏掉时，门面只该退化，不该把异常丢给对方。"""

    def test_broken_core_degrades(self):
        class Broken:
            role_id = "bot1"

            def __getattr__(self, name):
                raise RuntimeError("boom")

        class Roles:
            def get_all(self):
                return [Broken()]

        facade = CoreFacade(Roles())
        self.assertIsNone(facade.brief("bot1", "10001"))
        self.assertEqual(facade.clock("bot1"), {})
        self.assertEqual(facade.events("bot1"), [])
        self.assertEqual(facade.state_fingerprint("bot1"), "")


if __name__ == "__main__":
    unittest.main()
