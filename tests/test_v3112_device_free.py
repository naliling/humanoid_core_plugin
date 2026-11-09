"""v3.1.12 补丁：设备词不进日程。

线上症状（2026-11-09）：soc 一天 15 条主动消息尝试全部被验收层判掉，理由清一色
「打破第四面墙（提了设备「手机」/「屏幕」）」。追下去发现不是模型乱编——是 Core
自己交出去的素材在教她玩手机：

* `life_material.MATERIALS` 里有「在沙发上躺着刷手机」（sensory 就是「屏幕的光照在脸上」）
  「把手机里没用的截图和照片清了一遍」「在客厅坐着，翻了会儿手机」…
* 日程兜底池（`EXAMPLE_BANDS`）里有「躺着刷手机，什么正事也没做」
* 日程提示词里写着「懒散、拖延、躺一天、刷手机…」「在被窝里刷一会儿手机」
* 夜间感官默认值第一句就是「屏幕的光照在脸上」

模型顺着素材写 → 社交层按「她活在对话里，不活在设备里」整条丢掉 → 私聊一条发不出。
所以两头一起改：素材/提示词不再教她玩手机；`parse_segment` 再把模型自己编出来的
设备词换掉（兜底，防止以后又从哪里漏进来）。
"""

from __future__ import annotations

import unittest

from humanoid import prompt_builder
from humanoid.services import life_material, schedule
from humanoid.services.schedule import EXAMPLE_BANDS, parse_segment, scrub_device_text

DEVICE_WORDS = (
    "手机", "屏幕", "电脑", "平板", "键盘", "鼠标", "充电器",
    # v3.1.14：只删「手机/屏幕」不够——模型会从「回消息」「打字」「戴耳机」
    # 再推回设备。实测 16:17/16:54 两条破冰就是这么写出来的。
    "打字", "消息", "耳机", "充电线",
)


class MaterialsNeverMentionDevicesTest(unittest.TestCase):
    def test_life_materials(self):
        for item in life_material.MATERIALS:
            text = str(item.get("event") or "") + "|" + str(item.get("sensory") or "")
            for word in DEVICE_WORDS:
                self.assertNotIn(word, text, f"素材含设备词「{word}」：{text}")

    def test_fallback_pool(self):
        for band in EXAMPLE_BANDS:
            for slot in band:
                event = str(slot.get("event") or "")
                for word in DEVICE_WORDS:
                    self.assertNotIn(word, event, f"兜底日程含设备词「{word}」：{event}")

    def test_night_sensory_defaults(self):
        for hour, lines in prompt_builder._SENSORY_SLOT_LINES.items():
            for line in lines:
                for word in DEVICE_WORDS:
                    self.assertNotIn(word, line, f"{hour} 的感官默认值含「{word}」：{line}")


class ScrubTest(unittest.TestCase):
    def test_replaces_device_words(self):
        self.assertEqual(scrub_device_text("躺着刷手机"), "躺着翻两页书")
        self.assertEqual(scrub_device_text("屏幕的光照在脸上"), "窗玻璃的光照在脸上")
        self.assertNotIn("手机", scrub_device_text("把手机充上电"))

    def test_clean_text_untouched(self):
        text = "在窗边站了会儿，看了会儿楼下"
        self.assertEqual(scrub_device_text(text), text)

    def test_empty_safe(self):
        self.assertEqual(scrub_device_text(""), "")

    def test_message_semantics_scrubbed(self):
        # 线上原文：「翅膀托着手机给姐姐打字外头晴得晃眼才1℃」
        out = scrub_device_text("翅膀托着手边的东西给姐姐打字")
        for word in ("手机", "打字", "消息"):
            self.assertNotIn(word, out, f"清洗后仍含「{word}」：{out}")
        self.assertNotIn("消息", scrub_device_text("把攒着的几条消息回完"))


class ParseSegmentScrubsTest(unittest.TestCase):
    def test_model_invented_device_is_rewritten(self):
        slot = parse_segment(
            {
                "event": "窝在沙发上刷手机，什么正事也没做",
                "location": "客厅",
                "emotion": "有点空",
                "energy_rate": 0.02,
                "minutes": 40,
            },
            now_minute=600,
            step=5,
        )
        self.assertIsNotNone(slot)
        event = str(slot.get("event") or "")
        for word in DEVICE_WORDS:
            self.assertNotIn(word, event, f"落库日程仍含「{word}」：{event}")

    def test_resume_path_is_scrubbed_too(self):
        slot = parse_segment(
            {"resume": True},
            now_minute=600,
            step=5,
            suspended={"event": "靠着台面摸出手机", "remaining_minutes": 20, "location": "厨房"},
        )
        self.assertIsNotNone(slot)
        event = str(slot.get("event") or "")
        for word in DEVICE_WORDS:
            self.assertNotIn(word, event, f"resume 回来的日程仍含「{word}」：{event}")


if __name__ == "__main__":
    unittest.main()
