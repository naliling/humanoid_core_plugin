"""注入文本的纯函数测试：语气提示词表、夜间提示分档、compose_injection 的必填参数。"""

from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from humanoid.clock import Clock
from humanoid.config import HumanoidConfig
from humanoid.data.mood_map import get_mood_label
from humanoid.prompt import MOOD_TONE_HINTS, build_mood_prompt, build_night_hint

TZ = ZoneInfo("Asia/Shanghai")


def cfg(**overrides) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", **overrides})


class FixedClock(Clock):
    """把 now() 钉在某个小时，夜间/深睡判定仍然走真实的 Clock + config 逻辑。"""

    __slots__ = ("_moment",)

    def __init__(self, conf: HumanoidConfig, hour: int) -> None:
        super().__init__(lambda: conf)
        self._moment = datetime(2026, 8, 22, hour, 0, tzinfo=TZ)

    def now(self) -> datetime:
        return self._moment


def reachable_labels() -> set[str]:
    """穷举 get_mood_label 真的能产出的标签集合。"""
    return {
        get_mood_label(affection, libido, aggression)
        for affection in range(0, 101)
        for libido in range(0, 51)
        for aggression in range(0, 51)
    }


class MoodToneHintsTest(unittest.TestCase):
    def test_every_hint_key_is_a_real_label(self):
        """写成近义词的 key 永远命中不到，等于白加一条语气提示。"""
        labels = reachable_labels()
        dead = sorted(key for key in MOOD_TONE_HINTS if key not in labels)
        self.assertEqual(dead, [], f"这些 key 不在 get_mood_label 的输出里：{dead}")

    def test_hint_is_appended_when_label_matches(self):
        profile = {"affection": 90.0, "libido": 30.0, "aggression": 5.0}
        label = next(iter(MOOD_TONE_HINTS))
        text = build_mood_prompt(profile, label)
        self.assertIn("【语气提示】", text)
        self.assertIn(MOOD_TONE_HINTS[label], text)

    def test_unknown_label_falls_back_silently(self):
        profile = {"affection": 50.0, "libido": 25.0, "aggression": 25.0}
        text = build_mood_prompt(profile, "这不是一个标签")
        self.assertNotIn("【语气提示】", text)
        self.assertIn("当前情绪数值", text)


class NightHintTest(unittest.TestCase):
    """夜间 23:00–06:00、深睡比例 0.5 → 深睡覆盖 23/0/1/2 点，浅睡覆盖 3/4/5 点。"""

    def hint(self, hour: int, *, force: bool = False, **overrides) -> str:
        conf = cfg(night_mode_force_sleep=force, **overrides)
        return build_night_hint(conf, FixedClock(conf, hour))

    def test_no_hint_outside_night_window(self):
        self.assertEqual(self.hint(14), "")
        self.assertEqual(self.hint(22), "")

    def test_no_hint_when_night_mode_disabled(self):
        self.assertEqual(self.hint(1, night_mode_enabled=False), "")

    def test_deep_and_light_segments_differ(self):
        deep = self.hint(0)
        light = self.hint(4)
        self.assertIn("深度睡眠", deep)
        self.assertIn("浅睡", light)

    def test_force_sleep_is_the_stronger_variant(self):
        """night_mode_force_sleep 的说明是「强制回复睡眠提示（否则仅缩短回复）」。

        所以开着它必须比关着更严格 —— 早先的版本正好写反了：不开强制睡眠的深睡提示写着
        「AI 不应回复」，比开了还狠。
        """
        deep_forced = self.hint(0, force=True)
        deep_free = self.hint(0, force=False)
        self.assertIn("明天再聊吧", deep_forced)
        self.assertNotIn("明天再聊吧", deep_free)
        for text in (deep_forced, deep_free, self.hint(4, force=True), self.hint(4)):
            self.assertNotIn("不应回复", text, "插件拦不住回复，不要让模型去猜该不该回")
            self.assertIn("【夜间模式】", text)

    def test_ratio_one_makes_the_whole_night_deep(self):
        for hour in (23, 0, 3, 5):
            self.assertIn("深度睡眠", self.hint(hour, night_deep_sleep_ratio=1.0))

    def test_low_ratio_keeps_most_of_the_night_light(self):
        self.assertIn("深度睡眠", self.hint(23, night_deep_sleep_ratio=0.1))
        for hour in (0, 2, 5):
            self.assertIn("浅睡", self.hint(hour, night_deep_sleep_ratio=0.1))


class ComposeInjectionTest(unittest.TestCase):
    def test_clock_is_required(self):
        """漏传 clock 应当当场报错，而不是静默丢掉整段夜间提示。"""
        from humanoid.prompt import compose_injection

        with self.assertRaises(TypeError):
            compose_injection(object(), cfg())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
