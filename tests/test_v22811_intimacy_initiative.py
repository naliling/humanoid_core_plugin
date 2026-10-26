"""v2.28.11：门面 intimacy() 多给 initiative（她这会儿有多想主动）。

社交层「想要了」通道缺的那一半：以前只有「次数/亲近欲」，没有「她想不想主动」。
这里只钉形状与边界：0~1、标签三档、空档案也带字段（调用方不用猜键在不在）。
"""

from __future__ import annotations

import unittest

from humanoid.api import CoreFacade

from .test_v2289_link_identity import _Roles, make_core, seed_user


class IntimacyInitiativeTest(unittest.TestCase):
    def test_empty_record_still_has_the_fields(self):
        core = make_core()
        data = CoreFacade(_Roles([core])).intimacy("bot1", "u1")
        self.assertIn("initiative", data)
        self.assertIn("initiative_label", data)
        # 空档案也要在 [0,1] 里给一个确定值（调用方不用猜键在不在）。
        self.assertGreaterEqual(data["initiative"], 0.0)
        self.assertLessEqual(data["initiative"], 1.0)

    def test_seeded_user_is_bounded_and_labelled(self):
        core = make_core()
        seed_user(core)
        data = CoreFacade(_Roles([core])).intimacy("bot1", "10001")
        self.assertGreaterEqual(data["initiative"], 0.0)
        self.assertLessEqual(data["initiative"], 1.0)
        self.assertIn(data["initiative_label"], ("想主动", "一般", "不想"))

    def test_reading_does_not_create_the_user(self):
        core = make_core()
        facade = CoreFacade(_Roles([core]))
        facade.intimacy("bot1", "stranger")
        self.assertNotIn("stranger", core._scope.all_user_ids())


if __name__ == "__main__":
    unittest.main()
