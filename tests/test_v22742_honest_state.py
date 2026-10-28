"""v2.27.42：把「假情绪」和「串台的记忆」从注入里清出去。

四条，全部来自线上真实注入（`_audit/check_injection_live.py` 抓的原文）：

1. `娜莉灵内心：今天跟你说话挺正常的` —— 对 `turn_count=0`、从没说过话的用户也这么写。
   这是 `_intimacy` 的最后一条兜底（0.45 分），它在提示词里和真信号长得一模一样，
   模型分不出来，就把它当成她此刻的心情照演。
2. `和星辰之间还有件事没完：讲述姐姐们的事情` —— 出现在**别的人**的私聊里。
   未完事项账本是角色级的，原来不分人一律塞进每一轮。
3. `TA之前说过「抱着亲爱的灵灵摸摸头)」` —— 从一句语C动作中间截出来的半句，
   右括号还在、左括号落在窗口外。
4. `〔她的身体与生活 v12 uid=12345678〕` —— 那个 uid 是**机器人自己**的 id 末 8 位，
   每个用户都一样，对模型是一串没有来源的编号。
"""
from __future__ import annotations

import unittest

from humanoid import recall
from humanoid.services.weather import parse_payload


class IntimacyDoesNotFakeTest(unittest.TestCase):
    """没信号就别出声——不要拿「今天跟平时一样」占位。"""

    def _layer(self):
        from humanoid.emotion import EmotionLayer

        return EmotionLayer.__new__(EmotionLayer)

    def test_missing_care_emits_nothing(self) -> None:
        layer = self._layer()
        score, text = layer._intimacy({}, {"affection": 46.0}, 0)
        self.assertEqual((score, text), (0.0, ""), "读不到 care 就不该猜她的心情")

    def test_middling_care_emits_nothing(self) -> None:
        """care 在中间区间（0.25~0.62）没有可说的话，原来给的是填充句。"""
        layer = self._layer()
        score, text = layer._intimacy({"care": 0.5}, {"affection": 46.0}, 0)
        self.assertEqual((score, text), (0.0, ""))

    def test_close_care_still_speaks(self) -> None:
        layer = self._layer()
        score, text = layer._intimacy({"care": 0.8}, {"affection": 46.0}, 0)
        self.assertGreater(score, 0)
        self.assertTrue(text)

    def test_distant_care_still_speaks(self) -> None:
        layer = self._layer()
        score, text = layer._intimacy({"care": 0.1}, {"affection": 46.0}, 0)
        self.assertGreater(score, 0)
        self.assertTrue(text)

    def test_the_old_filler_words_are_gone(self) -> None:
        layer = self._layer()
        for care in (0.0, 0.2, 0.4, 0.6, 1.0):
            _score, text = layer._intimacy({"care": care}, {"affection": 46.0}, 0)
            self.assertNotIn("今天跟平时一样", text)
            self.assertNotIn("今天跟你说话挺正常的", text)


class RecallBracketsTest(unittest.TestCase):
    """读出来的时候也要配平：旧数据已经坏在盘上了。"""

    def test_unbalanced_right_paren_is_trimmed(self) -> None:
        lines = recall.recall_lines(
            [{"said": "抱着亲爱的灵灵摸摸头) 今天好累", "at": 100.0}], now=200.0
        )
        self.assertTrue(lines)
        self.assertNotIn("摸摸头)", lines[0])

    def test_snippet_keeps_balanced_text(self) -> None:
        self.assertEqual(recall.snippet("我猫今天吐了，怎么办啊"), "我猫今天吐了，怎么办啊")

    def test_short_leftover_is_dropped(self) -> None:
        """摘完只剩几个字就不该被当成「TA 说过的话」。"""
        lines = recall.recall_lines(
            [{"said": "(笑)", "at": 100.0}], now=200.0
        )
        self.assertEqual(lines, [])

    def test_note_said_does_not_store_a_broken_fragment(self) -> None:
        """截断落在括号里时，存下来的必须是配平的（不能留下半个动作）。"""
        long_text = "(轻轻抱着你，声音发软，凑到你耳边小声说)晚上想吃什么我都可以"
        out = recall.note_said([], long_text, now=1000.0, gap=0.0, min_len=1)
        self.assertTrue(out)
        said = out[0]["said"]
        self.assertEqual(
            said.count("(") + said.count("（"), said.count(")") + said.count("）"),
            f"存下来的摘录括号不配平：{said!r}",
        )


class WeatherRoundingTest(unittest.TestCase):
    """「气温 8.44℃」不是人话——没有人知道自己那边的气温是 8.44 度。"""

    def test_temperature_is_an_integer(self) -> None:
        out = parse_payload({
            "weather": [{"description": "多云"}],
            "main": {"temp": 8.44, "humidity": 73.2},
        })
        self.assertIsNotNone(out)
        self.assertIn("气温 8℃", out["env"])
        self.assertNotIn("8.44", out["env"])
        self.assertNotIn("73.2", out["env"])


class OpenItemsArePerPersonTest(unittest.TestCase):
    """私聊里不该出现「和星辰之间还有件事没完」。

    没完事项账本是**角色级**的（社交层写进来的），原来不分人一律拼进每一轮，
    于是每个人都会看到别人的私事；模型拿它当话头，就会对着陌生人问起第三个人的事。
    """

    class _Mood:
        def nickname(self, uid):
            return {"u1": "小明", "u2": "星辰"}.get(uid, "")

    class _Core:
        def __init__(self, items):
            self._items = items
            self.mood = OpenItemsArePerPersonTest._Mood()

        def life_items(self):
            return self._items

        def now_epoch(self):
            return 10_000.0

    def _builder(self, items):
        from humanoid.prompt_builder import PromptBuilder

        builder = PromptBuilder.__new__(PromptBuilder)
        builder._core = self._Core(items)
        return builder

    def _items(self):
        return [
            {"text": "关于我的事", "uid": "u1", "at": 9_900.0},
            {"text": "关于星辰的事", "uid": "u2", "at": 9_800.0},
            {"text": "不知道跟谁的事", "uid": "", "at": 9_700.0},
        ]

    def test_private_chat_only_sees_own_items(self) -> None:
        lines = self._builder(self._items())._open_lines("u1", is_group=False)
        joined = "\n".join(lines)
        self.assertIn("关于我的事", joined)
        self.assertNotIn("关于星辰的事", joined)
        # 来源不明的那条留着：不知道是谁的，但确实是她的待办
        self.assertIn("不知道跟谁的事", joined)

    def test_group_keeps_everything(self) -> None:
        joined = "\n".join(self._builder(self._items())._open_lines("u1", is_group=True))
        self.assertIn("关于星辰的事", joined)

    def test_empty_user_keeps_everything(self) -> None:
        """调用方没传 user_id（旧用法）时退回不筛，行为与改前一致。"""
        joined = "\n".join(self._builder(self._items())._open_lines())
        self.assertIn("关于星辰的事", joined)


if __name__ == "__main__":
    unittest.main()
