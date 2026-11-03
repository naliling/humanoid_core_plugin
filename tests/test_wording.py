"""wording 词表与调用点门槛的对齐（B1 批次：饥饿档位 / CARE_WORDS）。

两条回归是同一个形态：**词表的档位边界与调用点的门槛各写一份**。
两边一旦错开，门槛之上就会取到语义相反的档位词（饥饿 56 说「不饿」），
或者某档位被分界挡在外面永远取不到（CARE_WORDS 最高档 0.80 > 0.62）。
"""

import unittest

from humanoid.wording import CARE_WORDS, FEELING_WORDS, scale_word

# prompt_builder._relation_label 在 care >= 0.62 时改用 stable_label，
# 所以 CARE_WORDS 的每一档都必须落在这个分界以下才取得到。
STABLE_LABEL_CUTOFF = 0.62


class HungerBandTest(unittest.TestCase):
    """soma.feelings 用 floor=55 说饥饿：55–60 不能落到「不饿」。"""

    def test_floor_55_lands_on_a_real_band(self):
        options = scale_word(56.0, FEELING_WORDS["hunger"])
        self.assertNotIn("不饿", options, "饥饿 56 说「不饿」：词表档位与 floor 55 错开了")
        self.assertIn("有点饿", options)

    def test_below_55_still_says_not_hungry(self):
        self.assertIn("不饿", scale_word(54.0, FEELING_WORDS["hunger"]))

    def test_hunger_still_discriminates(self):
        self.assertNotEqual(
            scale_word(20, FEELING_WORDS["hunger"]),
            scale_word(95, FEELING_WORDS["hunger"]),
        )


class CareWordsTest(unittest.TestCase):
    def test_every_band_below_stable_label_cutoff(self):
        for floor, _options in CARE_WORDS:
            self.assertLess(
                floor, STABLE_LABEL_CUTOFF, f"档 {floor} 在 0.62 以上，永远取不到"
            )

    def test_every_band_reachable_in_range(self):
        seen = set()
        for i in range(0, 63):
            seen.update(scale_word(i / 100.0, CARE_WORDS))
        for floor, options in CARE_WORDS:
            for word in options:
                self.assertIn(word, seen, f"档 {floor} 的「{word}」在 0..0.62 内取不到")

    def test_0_58_reads_as_intimate(self):
        # 旧词表最高档是 0.80：care=0.58 时只说到「关系普通」，这一档升到 0.55 才对得上
        self.assertIn("关系亲密", scale_word(0.58, CARE_WORDS))
