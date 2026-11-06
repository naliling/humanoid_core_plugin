"""v3.1.0（P0-3）：mood_affection_override 对**已有档案**的老用户也必须生效。

老实现只在新建档案时读一次 override：`profile()` 发现档案已存在就直接
`return self._repair(...)`，面板上给老用户钉的 100 从来没生效过；就算生效，
`update_from_message` 的 delta 也会在下一句话里把它改走。本文件钉住两件事：
老档案照样吃 override，钉死之后聊天与衰减都动不了它。
"""

from __future__ import annotations

import unittest
from datetime import datetime

from humanoid.config import HumanoidConfig
from humanoid.llm import LLMGateway, ProviderResolver
from humanoid.services.mood import PIN_KEY, MoodService, get_mood_label

from .fakes import FakeContext, FrozenClock, RecordingLogger
from .test_services import TZ, build_store


def cfg(**kw) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", **kw})


class MoodPinTest(unittest.IsolatedAsyncioTestCase):
    def build(self, conf=None):
        self.conf = conf or cfg()
        self.time_value = 1_000_000.0
        self.store = build_store(self.conf)
        self.log = RecordingLogger()
        gateway = LLMGateway(
            ProviderResolver(FakeContext(), self.log), lambda: self.conf, self.log
        )
        self.clock = FrozenClock(datetime(2026, 8, 22, 9, 0, tzinfo=TZ))
        self.service = MoodService(
            self.store.scope,
            lambda: self.conf,
            self.clock,
            gateway=gateway,
            logger=self.log,
            time_source=lambda: self.time_value,
        )
        return self.service

    def old_user(self, uid="777", affection=30.0):
        """先建档，再把好感改小：模拟 3.0.11 之前就已经存在的老用户。"""
        self.service.profile(uid)
        mood = self.store.user(uid)["mood"]
        mood["affection"] = affection
        mood["base_affection"] = affection
        return mood

    async def test_override_applies_to_an_existing_profile(self):
        service = self.build(cfg(mood_affection_override=["777:95"]))
        self.old_user("777", 30.0)
        record = service.profile("777")
        self.assertEqual(record["affection"], 95.0, "老档案没吃到 override（P0-3 主症状）")
        self.assertEqual(record["base_affection"], 95.0, "基线也要钉住，否则下一次 delta 就把她拽回来")
        self.assertEqual(
            self.store.user("777")["mood"]["affection"], 95.0, "钉死值必须落盘，不能只改返回的副本"
        )

    async def test_pinned_affection_survives_chat_deltas(self):
        service = self.build(cfg(mood_affection_override=["777:95"], mood_sensitivity=100))
        self.old_user("777", 30.0)
        for text in ("你好棒", "谢谢你", "滚"):
            await service.update_from_message("777", text, 80.0, 10)
            self.assertEqual(
                service.profile("777")["affection"], 95.0, f"钉死值被「{text}」的 delta 改走了"
            )

    async def test_user_without_override_is_still_moved_by_chat(self):
        service = self.build(cfg(mood_sensitivity=100))
        self.old_user("888", 30.0)
        await service.update_from_message("888", "你好棒", 80.0, 10)
        self.assertGreater(
            service.profile("888")["affection"], 30.0, "没配 override 的用户被误钉死了"
        )

    async def test_decay_cannot_drag_the_pinned_value_down(self):
        service = self.build(cfg(mood_affection_override=["777:95"]))
        self.old_user("777", 30.0)
        service.profile("777")
        # 六小时没聊天，衰减本该把基线往下拽。
        self.store.user("777")["mood"]["last_decay"] = self.time_value - 6 * 3600
        service.decay_user("777")
        self.assertEqual(service.profile("777")["affection"], 95.0, "衰减把钉死值拖走了")

    async def test_removing_the_override_releases_the_pin(self):
        service = self.build(cfg(mood_affection_override=["777:95"]))
        self.old_user("777", 30.0)
        self.assertEqual(service.profile("777")["affection"], 95.0)
        self.conf = cfg()  # 面板上把钉死删掉
        record = service.profile("777")
        self.assertNotIn(PIN_KEY, record, "取消 override 后 pin 还留在档案里")
        self.assertEqual(record["affection"], 95.0, "取消钉死不该把她打回旧值")

    async def test_label_and_profile_text_follow_the_pinned_value(self):
        service = self.build(cfg(mood_affection_override=["777:95"]))
        self.old_user("777", 30.0)
        service.profile("777")
        # 面板刚改过、还没聊过天：档案里还是旧数字，展示必须按钉死值来。
        mood = self.store.user("777")["mood"]
        mood["affection"] = 30.0
        mood["base_affection"] = 30.0
        self.assertEqual(
            service.label("777"),
            get_mood_label(95.0, float(mood["libido"]), float(mood["aggression"])),
            "label() 没跟着钉死值走",
        )
        self.assertIn("好感度：95.0/100", service.profile_text("777"), "profile_text() 没跟着钉死值走")


if __name__ == "__main__":
    unittest.main()
