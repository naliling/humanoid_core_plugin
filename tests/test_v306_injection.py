# -*- coding: utf-8 -*-
"""注入取舍层（humanoid/injection.py）的单元测试。

这一层是 R7 的落点：两处注入（「此刻的她」与事实块）共用同一个 select/render，
所以它的不变量必须是**可以直接断言的纯函数行为**，不依赖 Core、配置、时钟。
"""

import unittest

from humanoid.injection import (
    INJECT_MAX_CHARS,
    Fact,
    estimate_tokens,
    render,
    select,
)


class EstimateTest(unittest.TestCase):
    def test_chinese_counts_one_per_char(self):
        self.assertEqual(estimate_tokens("她有点累"), 4)

    def test_ascii_counts_four_per_token(self):
        self.assertEqual(estimate_tokens("abcd"), 1)
        self.assertEqual(estimate_tokens("abcde"), 2)

    def test_empty_is_zero(self):
        self.assertEqual(estimate_tokens(""), 0)


class KeyDedupeTest(unittest.TestCase):
    def test_same_key_keeps_higher_priority(self):
        facts = [Fact("sleep", "她在睡", priority=4.0), Fact("sleep", "她睡着了", priority=8.0)]
        kept = select(facts)
        self.assertEqual([f.text for f in kept], ["她睡着了"])

    def test_same_key_same_priority_keeps_first(self):
        facts = [Fact("x", "第一句"), Fact("x", "第二句")]
        self.assertEqual([f.text for f in select(facts)], ["第一句"])

    def test_blank_text_is_dropped(self):
        facts = [Fact("a", "   "), Fact("b", "她醒着")]
        self.assertEqual([f.text for f in select(facts)], ["她醒着"])


class GroupTest(unittest.TestCase):
    def test_only_one_fact_per_group(self):
        facts = [
            Fact("a", "她在睡", group="sleep", priority=5.0),
            Fact("b", "她刚睡醒", group="sleep", priority=9.0),
        ]
        self.assertEqual([f.text for f in select(facts)], ["她刚睡醒"])

    def test_ungrouped_facts_all_survive(self):
        facts = [Fact("a", "她在睡"), Fact("b", "外头在下雨")]
        self.assertEqual(len(select(facts)), 2)


class TextDedupeTest(unittest.TestCase):
    def test_shorter_variant_of_same_sentence_is_dropped(self):
        # 「她刚睡醒」与「她刚睡醒（7 分钟前）」是同一件事的两种写法。
        facts = [Fact("wake", "她刚睡醒"), Fact("wake2", "她刚睡醒（7 分钟前）")]
        kept = select(facts)
        self.assertEqual([f.text for f in kept], ["她刚睡醒（7 分钟前）"])

    def test_different_sentences_both_survive(self):
        facts = [Fact("a", "她有点累"), Fact("b", "外头在下雨")]
        self.assertEqual(len(select(facts)), 2)


class ConditionTest(unittest.TestCase):
    def test_when_false_drops_the_fact(self):
        facts = [Fact("s", "她在睡", when="sleep_window")]
        self.assertEqual(select(facts, flags={"sleep_window": False}), [])

    def test_when_true_keeps_it(self):
        facts = [Fact("s", "她在睡", when="sleep_window")]
        self.assertEqual(len(select(facts, flags={"sleep_window": True})), 1)

    def test_missing_flag_means_false(self):
        facts = [Fact("s", "她在睡", when="sleep_window")]
        self.assertEqual(select(facts), [])


class BudgetTest(unittest.TestCase):
    def test_low_priority_is_dropped_first(self):
        facts = [
            Fact("scene", "她坐在窗边", priority=10.0),
            Fact("hands", "手上在改一份方案", priority=4.0),
        ]
        kept = select(facts, budget_tokens=12, header_tokens=0)
        self.assertEqual([f.text for f in kept], ["她坐在窗边"])

    def test_kept_order_is_input_order_not_priority_order(self):
        facts = [
            Fact("low", "低优先级那句", priority=1.0),
            Fact("high", "高优先级那句", priority=9.0),
        ]
        kept = select(facts, budget_tokens=100, header_tokens=0)
        self.assertEqual([f.key for f in kept], ["low", "high"])

    def test_char_cap_is_enforced_on_the_rendered_body(self):
        facts = [Fact(f"k{i}", "她" + "啊" * 20, priority=float(i)) for i in range(10)]
        kept = select(facts, max_chars=100)
        self.assertLessEqual(len(render(kept)), 100)

    def test_char_cap_keeps_the_highest_priority(self):
        facts = [
            Fact("low", "啊" * 60, priority=1.0),
            Fact("high", "嗯" * 60, priority=9.0),
        ]
        kept = select(facts, max_chars=60)
        self.assertEqual([f.key for f in kept], ["high"])


class RenderTest(unittest.TestCase):
    def test_same_subject_is_merged(self):
        facts = [Fact("a", "她有点累", group="body"), Fact("b", "她刚睡醒", group="body")]
        self.assertEqual(render(facts), "她有点累，刚睡醒")

    def test_different_subjects_are_not_merged(self):
        facts = [Fact("a", "她有点累", group="body"), Fact("b", "你刚回来", group="body")]
        self.assertEqual(render(facts), "她有点累，你刚回来")

    def test_groups_are_joined_by_period(self):
        facts = [Fact("a", "她在睡", group="sleep"), Fact("b", "外头在下雨", group="weather")]
        self.assertEqual(render(facts), "她在睡。外头在下雨")

    def test_ungrouped_facts_do_not_merge_each_other(self):
        # 没有 group 就是各自独立的一句：不合并主语，免得把不同主题拼成一句。
        facts = [Fact("a", "她有点累"), Fact("b", "她刚睡醒")]
        self.assertEqual(render(facts), "她有点累。她刚睡醒")

    def test_empty_render_is_empty_string(self):
        self.assertEqual(render([]), "")


class MaxCharsConstantTest(unittest.TestCase):
    def test_three_injection_modes_have_a_cap(self):
        self.assertEqual(set(INJECT_MAX_CHARS), {"medium", "full", "mood_only"})

    def test_caps_are_ordered(self):
        self.assertLess(INJECT_MAX_CHARS["mood_only"], INJECT_MAX_CHARS["medium"])
        self.assertLess(INJECT_MAX_CHARS["medium"], INJECT_MAX_CHARS["full"])


if __name__ == "__main__":
    unittest.main()
