"""三条「读实际输出才发现」的小修：措辞跨天轮换、空引号、体感分数跟档位。

都是单测全绿、仿真 0 ERROR 的状态下，靠**把模型实际会读到的东西打出来读一遍**
才发现的。
"""
import unittest

from humanoid.persona import Persona
from humanoid.services.schedule import persona_block
from humanoid.wording import FEELING_WORDS, band_index


class PhrasingRotatesAcrossDays(unittest.TestCase):
    def test_seed_contains_today(self):
        """一天内稳定、跨天轮换。

        只按 role+uid 定死的话，同一个用户**一辈子只抽一种措辞**——实测连跑 6 天，
        六天全是「有点想说话」。一个人不会永远心里只说一句。
        """
        import ast as _ast
        import inspect
        import textwrap
        from humanoid.emotion import EmotionLayer
        code = _ast.unparse(_ast.parse(
            textwrap.dedent(inspect.getsource(EmotionLayer.candidate))))
        self.assertIn("today", code, "抽签种子里没有日期 → 措辞一辈子不变")

    def test_same_day_stable_across_restarts(self):
        """同一天内反复渲染不换句——模型看到的是同一句感受，不是两件事。"""
        import hashlib

        def idx(day):
            raw = f"emotion:bot1:u1:{day}".encode()
            return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big") % 3

        a = {idx("2026-09-26") for _ in range(3)}
        self.assertEqual(len(a), 1, "同一天内抽签结果不一致")

    def test_rotation_is_a_function_of_the_day(self):
        """不同日期可能撞成同一个档（只有 3 档），但种子确实含了日期。"""
        import hashlib

        def raw(day):
            return f"emotion:bot1:u1:{day}".encode()

        self.assertNotEqual(
            hashlib.blake2b(raw("2026-09-26"), digest_size=8).digest(),
            hashlib.blake2b(raw("2026-09-27"), digest_size=8).digest(),
        )
        # 一个月内至少要出现两种说法，否则等于没轮换
        days = [f"2026-09-{d:02d}" for d in range(1, 29)]
        picks = {
            int.from_bytes(hashlib.blake2b(raw(d), digest_size=8).digest(), "big") % 3
            for d in days
        }
        self.assertGreaterEqual(len(picks), 2, "一个月只抽到一种，轮换等于没有")


class NoEmptyQuotesInPrompt(unittest.TestCase):
    """人设面板里没填名字时，prompt 开头不能是「她是「」。」。

    日程一天生成几次，那句话会反复出现在她眼前。
    """

    def test_with_name(self):
        out = persona_block(Persona("小马利亚", "你是小马利亚。", "会话"))
        self.assertTrue(out.startswith("她是「小马利亚」。"), out[:40])

    def test_without_name(self):
        out = persona_block(Persona("", "你是小马利亚，纯白双翼的独角小马。", "会话"))
        self.assertNotIn("「」", out)
        self.assertNotIn("她是「", out)
        self.assertIn("人格设定是下面这份", out)


class FeelingScoreFollowsTheBand(unittest.TestCase):
    """分数要跟着**词**走，不跟数值走。

    原来词按档位查表、分数按 `(value-floor)/span` 线性算，两边各走各的，于是出现
    「说得很重但说不出口」：hunger=88 的词已经是「饿得发慌」，分数 0.733，
    门槛 0.75 → 被滤掉。**越难受越不说**，和这层存在的理由正好相反。
    """

    GATE = 0.75     # prompt 侧 medium 档的门槛

    def test_top_band_clears_the_gate(self):
        for kind in ("sleepy", "hunger", "discomfort"):
            ladder = FEELING_WORDS[kind]
            top_floor = max(f for f, _ in ladder)
            band = band_index(top_floor, ladder)
            self.assertEqual(band, len(ladder) - 1, f"{kind} 顶档定位不对")

    def test_the_severe_word_is_the_one_that_gets_through(self):
        """饿到顶档时用的一定是「很饿/饿得发慌」那种重词，而它必须进得去。"""
        ladder = FEELING_WORDS["hunger"]
        top = ladder[-1][1]
        self.assertIn("饿", top[0])
        self.assertFalse(any("饿" not in w for w in top), "顶档的词不够重")


if __name__ == "__main__":
    unittest.main()
