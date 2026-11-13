"""v3.1.42：出站守卫的两处补丁。

锁两件事：

* `reply_guard.trim_repeats` 删掉**末尾**那句复读时，不再把前一句留下的分隔符
  挂在句尾（旧行为：「刚把碗洗完，。」）。
* `reply_guard.looks_like_refusal` 不再把「一句中文都没有的长句」一律当拒绝：
  英文 / 非中文人设的正常句子要能发出去，真带拒绝语义特征的样板仍然拦。
"""

from __future__ import annotations

import unittest

from humanoid import reply_guard


class TrimRepeatsTrailingClauseTest(unittest.TestCase):
    """删掉末尾小句后，句尾不能留分隔符。"""

    def test_dropping_the_last_clause_does_not_leave_a_comma(self):
        # 「我在厨房呢」与 recent 完全重复 → 末尾那句被删；前面那句留下的「，」
        # 必须一起清掉，再按原句末标点补一次。
        new_text, dropped = reply_guard.trim_repeats(
            "刚把碗洗完，我在厨房呢。", ["我在厨房呢"]
        )
        self.assertEqual(new_text, "刚把碗洗完。")
        self.assertEqual(dropped, ["我在厨房呢"])
        self.assertNotIn("，。", new_text)

    def test_dropping_the_last_clause_keeps_the_sentence_punctuation(self):
        new_text, dropped = reply_guard.trim_repeats(
            "刚把碗洗完，我在厨房呢！", ["我在厨房呢"]
        )
        self.assertEqual(new_text, "刚把碗洗完！")
        self.assertEqual(dropped, ["我在厨房呢"])


class LooksLikeRefusalTest(unittest.TestCase):
    """非中文回复：正常句子放行，拒绝样板仍拦。"""

    def test_a_normal_english_reply_is_not_a_refusal(self):
        text = "I just finished the dishes and I'm heading to the balcony."
        self.assertFalse(reply_guard.looks_like_refusal(text))

    def test_a_short_english_word_is_not_a_refusal(self):
        self.assertFalse(reply_guard.looks_like_refusal("Wind"))

    def test_an_english_refusal_sample_is_still_blocked(self):
        text = "I'm sorry, but I can't help with that request."
        self.assertTrue(reply_guard.looks_like_refusal(text))

    def test_a_non_cjk_refusal_without_the_exact_marker_is_still_blocked(self):
        # 不命中 REFUSAL_MARKERS 的整句英文，靠「拒绝语义特征」这一层仍然拦住。
        text = "This assistant will not continue with that kind of content."
        self.assertTrue(reply_guard.looks_like_refusal(text))


if __name__ == "__main__":
    unittest.main()
