"""设备词与含糊词不进日程（v3.1.12 起；v3.1.16 换了做法）。

线上症状（2026-11-09）：soc 一天 15 条主动消息尝试全部被验收层判掉，理由清一色
「打破第四面墙（提了设备「手机」/「屏幕」）」。追下去发现不是模型乱编——是 Core
自己交出去的素材在教她玩手机。

v3.1.12 的做法是**生成完再换词**（手机→手边的东西、屏幕→窗玻璃、消息→话）。
那个替换器是永动机：

    模型写「用蹄子夹着手机」→ 换成「夹着手边的东西」→ 存进日程
      → 这句又注入回提示词 → 模型看到「手边的东西」自己补全回「手机」→ 再被换掉

换出来的词还常常是病句（「给姐姐发消息」→「给姐姐发话」）和不合逻辑的句子
（「盯着窗玻璃等回音」）。所以 v3.1.16 把替换器整个删了，改成两头夹：

* 生成前：提示词说清「她的世界没有手机、电脑、屏幕」，并且递上过过筛的候选；
* 生成后：`schedule_content.content_issues` 判定，重写一次，仍脏就降级成处境词。

本文件守的是「素材与兜底池本身不能教她玩手机」这条底线，外加「替换器不许回来」。
"""

from __future__ import annotations

import unittest

from humanoid import prompt_builder
from humanoid.services import life_material, process, schedule
from humanoid.services.schedule import EXAMPLE_BANDS, parse_segment
from humanoid.services.schedule_content import content_issues, low_info_issues

#: 含糊占位词：替换器当年造出来的那批。它们不是设备词，但同样不该出现在素材里——
#: 模型看到「手边的东西」会自己补回「手机」，补完又被判成设备，一圈回到原点。
VAGUE_WORDS = ("手边", "手头的东西", "手上的东西", "某个东西")


class PoolsAreCleanTest(unittest.TestCase):
    """素材池、兜底池、感官默认值、过程阶段：都不许教她玩手机。"""

    def test_life_materials(self):
        for item in life_material.MATERIALS:
            text = str(item.get("event") or "") + "|" + str(item.get("sensory") or "")
            self.assertEqual(content_issues(text), [], f"素材不干净：{text}")

    def test_fallback_pool(self):
        for band in EXAMPLE_BANDS:
            for slot in band:
                event = str(slot.get("event") or "")
                self.assertEqual(content_issues(event), [], f"兜底日程不干净：{event}")

    def test_night_sensory_defaults(self):
        for hour, lines in prompt_builder._SENSORY_SLOT_LINES.items():
            for line in lines:
                self.assertEqual(content_issues(line), [], f"{hour} 的感官默认值不干净：{line}")

    def test_process_phases(self):
        for name, entries in process.PROCESS_TEMPLATES.items():
            for entry in entries:
                texts = [str(entry.get("name") or "")] + [
                    str(phase) for phase in entry.get("phases") or ()
                ]
                for text in texts:
                    self.assertEqual(content_issues(text), [], f"过程模板 {name} 不干净：{text}")

    def test_no_vague_placeholder_left(self):
        """「手边的东西」这类词必须绝迹——它本身就在教模型偷懒。"""
        pools: list[str] = []
        pools += [
            str(item.get("event") or "") + "|" + str(item.get("sensory") or "")
            for item in life_material.MATERIALS
        ]
        pools += [str(slot.get("event") or "") for band in EXAMPLE_BANDS for slot in band]
        pools += [
            line
            for lines in prompt_builder._SENSORY_SLOT_LINES.values()
            for line in lines
        ]
        for text in pools:
            for word in VAGUE_WORDS:
                self.assertNotIn(word, text, f"含糊占位词「{word}」又回到素材里：{text}")


class ScrubberIsGoneTest(unittest.TestCase):
    """替换器删掉就别再回来：它一回来，这个循环就重新开始。"""

    def test_no_scrub_function(self):
        self.assertFalse(hasattr(schedule, "scrub_device_text"))
        self.assertFalse(hasattr(schedule, "_DEVICE_SWAPS"))


class ParseSegmentDoesNotRewriteTest(unittest.TestCase):
    """解析层只做结构/时间，**不换词**：判脏是质量闸的活，换词只会造病句。"""

    def test_model_invented_device_is_kept_for_the_gate_to_judge(self):
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
        self.assertIn("手机", str(slot.get("event") or ""), "解析层不该偷偷换词")
        self.assertTrue(content_issues(str(slot.get("event") or "")), "但闸门要能判出来")

    def test_resume_path_is_not_rewritten(self):
        slot = parse_segment(
            {"resume": True},
            now_minute=600,
            step=5,
            suspended={"event": "靠着台面摸出手机", "remaining_minutes": 20, "location": "厨房"},
        )
        self.assertIsNotNone(slot)
        self.assertIn("手机", str(slot.get("event") or ""))


class PoolsAreNotVagueTest(unittest.TestCase):
    """素材池里也不许有空话（v3.1.27）。

    线上症状：`3887128092` 的日程里落进「什么也不干，坐着发了会儿呆」，来源不是模型，
    是素材池第 214 行那句本身就空——自愈从 slate 挑中它就直接落库。池子自己得先干净。
    """

    def test_life_materials_have_no_low_info_events(self):
        for item in life_material.MATERIALS:
            event = str(item.get("event") or "")
            self.assertEqual(
                low_info_issues(event),
                [],
                f"素材池里的空话：{event}",
            )

    def test_fallback_bands_have_no_low_info_events(self):
        for band in EXAMPLE_BANDS:
            for item in band:
                event = str(item.get("event") or "")
                self.assertEqual(low_info_issues(event), [], f"兜底池里的空话：{event}")


if __name__ == "__main__":
    unittest.main()
