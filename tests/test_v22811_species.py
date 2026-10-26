"""v2.28.11：物种/身体结构识别（CCB 用）。

为什么单独测：物种判错的代价不对称——把人类判成小马只是多一句怪话，
把「小马甲」判成兽形、把「陆马没有翅膀」判成天马，会让整段场景出戏。
所以语料里**误判源与否定句**占了一半。

判据：`detect()` 返回 kind/label/traits/confidence；`body_line()` 低于
INJECT_MIN 返回空串（判不出就不注入）。
"""

from __future__ import annotations

import logging
import sys
import types
import unittest

# ─── 最小 astrbot 桩（species 会 import outfit，outfit 取 logger）───
if "astrbot" not in sys.modules:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("species_test")
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api})

from humanoid import species  # noqa: E402


class SpeciesCorpusTest(unittest.TestCase):
    """正例：该判出来的必须判出来，结构要对。"""

    def check(self, text, kind, label=None, traits=(), min_conf=0.65):
        profile = species.detect(text)
        self.assertEqual(profile["kind"], kind, f"{text} → {profile}")
        if label:
            self.assertEqual(profile["label"], label, f"{text} → {profile}")
        for trait in traits:
            self.assertIn(trait, profile["traits"], f"{text} → {profile}")
        self.assertGreaterEqual(profile["confidence"], min_conf, f"{text} → {profile}")
        line = species.body_line(profile)
        self.assertTrue(line, f"{text} 应该能注入一句身体事实")
        self.assertLessEqual(len(line), 34, f"{text} 的身体事实太长：{line}")
        return profile

    def test_explicit_mlp(self):
        self.check("你是小马宝莉里的一匹小马，可爱标志是一颗星星。",
                   "pony", "小马", ("可爱标志",), 0.9)

    def test_mlp_country(self):
        self.check("彩虹小马国来的访客，鬃毛是彩虹色。",
                   "pony", "小马", ("鬃毛",), 0.9)

    def test_english_pony(self):
        self.check("My Little Pony 风格的独角兽。",
                   "pony", "独角兽", ("角",), 0.9)

    def test_subtle_no_pony_word(self):
        # 没有「小马」两个字，只靠翅膀+四蹄+鬃毛
        self.check("她有一对翅膀，四蹄踏在云上，鬃毛被风吹乱。",
                   "pony", "天马", ("翅膀", "四蹄", "鬃毛"), 0.8)

    def test_one_pony_word_with_anchor(self):
        self.check("一匹栗色小马，鬃毛很长，尾巴是浅金色。",
                   "pony", "小马", ("鬃毛", "尾巴"), 0.9)

    def test_cutie_mark_alone(self):
        self.check("她的可爱标志是一颗星星，平时总把鬃毛扎起来。",
                   "pony", "小马", ("鬃毛", "可爱标志"), 0.8)

    def test_pegasus(self):
        self.check("你是天马，能在云上走。", "pony", "天马", ("翅膀",), 0.8)

    def test_night_pony(self):
        self.check("她是夜骐，翅膀是蝠翼。", "pony", "夜骐", ("翅膀",), 0.8)

    def test_changeling(self):
        self.check("幻形灵可以变成任何小马。", "pony", "幻形灵", ("翅膀",), 0.8)

    def test_land_pony_negation(self):
        """「陆马没有翅膀」不能判成天马。"""
        profile = self.check("陆马没有翅膀，但蹄子很稳。",
                             "pony", "陆马", ("四蹄",), 0.8)
        self.assertNotIn("翅膀", profile["traits"])

    def test_centaur(self):
        self.check("她是半人马，马身有四条腿。", "beast", "半人马", ("四蹄",), 0.8)

    def test_mermaid(self):
        self.check("她是人鱼，下半身是鱼尾。", "beast", "人鱼", ("尾巴",), 0.8)

    def test_dragon(self):
        self.check("你是一条龙，有龙角和鳞片。", "beast", "龙", ("角", "鳞片"), 0.8)

    def test_succubus(self):
        self.check("她是一只魅魔，有尾巴和角。", "spirit", "魅魔", ("尾巴", "角"), 0.8)

    def test_qilin(self):
        self.check("她是麒麟，头上有角，身上有鳞。", "pony", "麒麟", ("角",), 0.8)

    def test_catgirl(self):
        self.check("你是一只猫娘，有猫耳和尾巴。",
                   "beast_ear", "半兽人形", ("尾巴", "兽耳"), 0.7)

    def test_tentacles_on_human(self):
        self.check("她背后有触手，能自主活动。", "human", "人类", ("触手",), 0.65)

    def test_machine(self):
        self.check("她是机械身体，仿生人。", "machine", "机械身体", (), 0.7)

    def test_structure_declaration_beats_other_mentions(self):
        """真实人设写法：「绝对无独角」不能被别处的「双龙角」盖过去。"""
        profile = self.check(
            "我是飞马马族。结构：有翅膀，绝对无独角。",
            "pony", "天马", ("翅膀",), 0.65,
        )
        self.assertNotIn("角", profile["traits"])

    def test_race_declaration_beats_rule_examples(self):
        """「种族：独角兽」不能被规则示例里的「天角兽」盖过去。"""
        profile = self.check(
            "种族：独角兽。结构：有独角，绝对无翅膀。"
            "（判断规则：出现翅膀时按天马/天角兽处理）",
            "pony", "独角兽", ("角",), 0.65,
        )
        self.assertNotIn("翅膀", profile["traits"])

    def test_alicorn_declaration(self):
        self.check("结构：天角兽（同时拥有翅膀和独角）。",
                   "pony", "天角兽", ("角", "翅膀"), 0.65)

    def test_override(self):
        profile = species.detect("普通人设", override="半人马+触手")
        self.assertEqual(profile["source"], "override")
        self.assertEqual(profile["label"], "半人马")
        self.assertIn("触手", profile["traits"])
        self.assertEqual(profile["confidence"], 1.0)

    def test_override_pony(self):
        profile = species.detect("", override="小马宝莉")
        self.assertEqual(profile["kind"], "pony")
        self.assertEqual(profile["label"], "小马")


class SpeciesFalsePositiveTest(unittest.TestCase):
    """误判源：这些必须一句都不注入。"""

    def test_common_words_do_not_trigger(self):
        for text in (
            "小马哥今天又迟到了。",
            "马上到，别催。",
            "周末去骑马。",
            "他在玩木马。",
            "这条马路很宽。",
            "她穿了一件小马甲。",
            "独角戏演得不错。",
            "这家独角兽公司上市了。",
            "章鱼烧很好吃。",
            "触手可及的距离。",
            "角度不对，角色也换了。",
            "人鱼线练出来了。",
            "恐龙园周末人很多。",
            "她是一个普通人类女孩，喜欢看书。",
        ):
            profile = species.detect(text)
            self.assertEqual(species.body_line(profile), "",
                             f"{text} 误判成 {profile}")

    def test_empty_persona(self):
        self.assertEqual(species.body_line(species.detect("")), "")

    def test_negated_traits_are_not_counted(self):
        profile = species.detect("她没有尾巴，也没有翅膀。")
        self.assertNotIn("尾巴", profile["traits"])
        self.assertNotIn("翅膀", profile["traits"])


if __name__ == "__main__":
    unittest.main()
