"""v3.1.30：「她手上还开着的事」里点名设备的条目不进账本、也不渲染。

这本账由社交层整体替换发布，所以只在日程侧删是删不掉的——下一轮发布又会带回来。
过滤要放在 `core_instance._clean_open_items`（写路径的合并前）和 `life_items()`（读路径）
两处，判据用 `schedule_content.object_issues` 的窄口径：只认设备物件，
「等 TA 回消息」这种讲人的事照留。
"""

from __future__ import annotations

import unittest

from humanoid.core_instance import HumanoidCoreInstance


def item(text: str, uid: str = "u") -> dict:
    return {"text": text, "uid": uid, "at": 1.0}


class CleanOpenItemsTest(unittest.TestCase):
    def test_device_object_items_are_dropped(self):
        kept = HumanoidCoreInstance._clean_open_items(
            [
                item("你咋知道我那手机坏了？"),
                item("帮 TA 挑生日礼物"),
            ]
        )
        self.assertEqual([entry["text"] for entry in kept], ["帮 TA 挑生日礼物"])

    def test_device_actions_about_a_person_are_kept(self):
        """「等 TA 回消息」讲的是那个人在等她，不是她的日程，不能删。"""
        kept = HumanoidCoreInstance._clean_open_items(
            [item("等 TA 回消息"), item("他面试还没回音")]
        )
        self.assertEqual(
            [entry["text"] for entry in kept], ["等 TA 回消息", "他面试还没回音"]
        )

    def test_all_device_kinds_are_dropped(self):
        texts = ["那台电脑又蓝屏了", "耳机落在公司了", "给你发微信没回"]
        kept = HumanoidCoreInstance._clean_open_items([item(t) for t in texts])
        self.assertEqual(kept, [])

    def test_non_list_passes_through(self):
        self.assertIsNone(HumanoidCoreInstance._clean_open_items(None))
        self.assertEqual(HumanoidCoreInstance._clean_open_items("x"), "x")

    def test_plain_strings_are_checked_too(self):
        kept = HumanoidCoreInstance._clean_open_items(["帮他看看平板", "帮 TA 挑礼物"])
        self.assertEqual(kept, ["帮 TA 挑礼物"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
