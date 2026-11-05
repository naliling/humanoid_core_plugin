"""身体事实只在「该说」的时候递（m08275 现场：她的每一句回复都在说小腹）。

用户原话：「每一句都是这样！小腹小腹！有病啊！……他应该适当的说而不是句句都说！
你能不能全部改完！严格一点！别省啊！」

成因是两条互相加强的回路：

1. **注入每轮都递**：周期句（body_sense.cycle_line）与「身体不适」
   （soma.feelings 的 discomfort 条目，经期时长期 >= 62）每轮都进上下文；
2. **自我强化**：她上一轮说了「肚子难受」，下一轮注入又递一句「小腹坠着」，
   模型于是把这件事当成正在进行的对话主题接着往下说，越说越像播报病情。

闸门三条，全在 humanoid/body_voice.py（纯函数）与 prompt_builder 的三个
*_worth_saying 里：

- 对方**问到了**身体 → 立刻给（问到了却不答，等于她没感觉）；
- 她**自己刚说过**身体上的事 → 四小时内不给（已经在对话里了）；
- 周期三天一次、不适八小时一次（一天一次等于每天开场必提）。

这里守的是「判据本身」与「判据真的接在注入上」两件事；措辞轮换与
「同一天不重复」在 tests/test_v3010_cycle_quiet.py。
"""

from __future__ import annotations

import unittest
from unittest import mock

from humanoid import body_voice, wording

from .test_v3010_cycle_quiet import _cycle_lines_in
from .test_v306_injection_golden import UID, _core


class BodyWordsTest(unittest.TestCase):
    """她自己的话里有没有提到身体——这决定「要不要闭嘴」。"""

    def test_it_hears_her_talking_about_her_body(self):
        self.assertTrue(body_voice.mentions_body("我小腹坠着，腰也酸"))
        self.assertTrue(body_voice.mentions_body("肚子有点胀"))
        self.assertTrue(body_voice.mentions_body("我这两天不舒服"))

    def test_ordinary_small_talk_is_not_about_the_body(self):
        self.assertFalse(body_voice.mentions_body("今天天气不错"))
        self.assertFalse(body_voice.mentions_body("我先去写稿子了"))
        self.assertFalse(body_voice.mentions_body(""))

    def test_a_real_question_counts_as_asking(self):
        self.assertTrue(body_voice.asked_about_body("你肚子还疼吗"))
        self.assertTrue(body_voice.asked_about_body("是不是经期到了"))
        self.assertTrue(body_voice.asked_about_body("你气色不太好"))

    def test_bare_nouns_and_generic_care_do_not_count(self):
        """「肚子」「腰」「你怎么了」这些词天天出现，拿它们当判据等于没有判据。"""
        self.assertFalse(body_voice.asked_about_body("晚上吃什么"))
        self.assertFalse(body_voice.asked_about_body("你怎么了"))
        self.assertFalse(body_voice.asked_about_body("肚子"))
        self.assertFalse(body_voice.asked_about_body("还好吗"))

    def test_due_handles_never_said_and_clock_going_backwards(self):
        self.assertTrue(body_voice.due(1000.0, 0.0, 60.0), "从没说过就该说")
        self.assertFalse(body_voice.due(1000.0, 960.0, 60.0))
        self.assertTrue(
            body_voice.due(1000.0, 1200.0, 60.0),
            "时间倒流（改系统时钟）不该让她永久闭嘴",
        )

    def test_asking_overrides_the_cooldown(self):
        self.assertFalse(body_voice.cycle_due(1000.0, 999.0, asked=False))
        self.assertTrue(body_voice.cycle_due(1000.0, 999.0, asked=True))


class EchoQuietTest(unittest.TestCase):
    """她刚说过身体上的事，注入就不要再递——否则她只能接着说。"""

    def test_her_own_words_stop_the_cycle_line(self):
        core = _core()
        core.note_her_words(UID, "我小腹有点坠，腰也酸")
        body = core.build_injection(UID, text="在吗")
        self.assertEqual(
            _cycle_lines_in(body), [],
            "她刚说完肚子，注入又递一句「小腹坠着」：模型会把同一件事再说一遍",
        )

    def test_unrelated_reply_does_not_silence_it(self):
        core = _core()
        core.note_her_words(UID, "今天天气不错")
        body = core.build_injection(UID, text="在吗")
        self.assertTrue(_cycle_lines_in(body), "她没说身体，闸门不该被踩下")

    def test_it_resumes_after_the_quiet_window(self):
        core = _core()
        core.note_her_words(UID, "我小腹有点坠")
        core.scope.update_self(
            body_echo_at=core.now_epoch() - body_voice.ECHO_QUIET_SECONDS - 60.0
        )
        body = core.build_injection(UID, text="在吗")
        self.assertTrue(_cycle_lines_in(body), "过了静默期该能重新提到")

    def test_asking_still_gets_an_answer_while_quiet(self):
        core = _core()
        core.note_her_words(UID, "我小腹有点坠")
        body = core.build_injection(UID, text="你肚子还疼吗")
        self.assertTrue(_cycle_lines_in(body), "对方追问了却不回答，等于她没感觉")


class DiscomfortQuietTest(unittest.TestCase):
    """「身体不适」是第二个每轮都会命中的来源（经期时 discomfort 长期 >= 62）。"""

    def _builder(self, core):
        return core.prompt_builder

    def test_it_is_not_said_twice_within_eight_hours(self):
        core = _core()
        core.scope.update_self(discomfort_said_at=core.now_epoch() - 3600.0)
        self.assertFalse(self._builder(core)._discomfort_worth_saying("在吗"))

    def test_it_comes_back_after_eight_hours(self):
        core = _core()
        core.scope.update_self(
            discomfort_said_at=core.now_epoch() - body_voice.DISCOMFORT_QUIET_SECONDS - 60.0
        )
        self.assertTrue(self._builder(core)._discomfort_worth_saying("在吗"))

    def test_asking_always_gets_it(self):
        core = _core()
        core.scope.update_self(discomfort_said_at=core.now_epoch())
        self.assertTrue(self._builder(core)._discomfort_worth_saying("你肚子还疼吗"))

    def test_her_own_words_also_silence_it(self):
        core = _core()
        core.note_her_words(UID, "我肚子有点难受")
        self.assertFalse(self._builder(core)._discomfort_worth_saying("在吗"))

    def test_soma_stops_emitting_it_when_told_not_to(self):
        """判据在结构化入参上：拿到 False 就不产出，不靠事后从正文里删词。"""
        core = _core()
        snap = {
            "sleep_pressure": 20.0,
            "sleep_debt": 0.0,
            "asleep": 0.0,
            "hunger": 20.0,
            "discomfort": 80.0,
            "chill": 0.0,
            "arousal": 60.0,
        }
        words = tuple(
            word
            for _, choices in (wording.FEELING_WORDS.get("discomfort") or ())
            for word in choices
        ) + ("略有不适",)

        def _has_discomfort(items):
            return any(text in words for _, text in items)

        with mock.patch.object(core.soma, "snapshot", return_value=snap):
            self.assertTrue(
                _has_discomfort(core.soma.feelings(60.0)),
                "默认（对方问起时）要能把不适说出来",
            )
            self.assertFalse(
                _has_discomfort(core.soma.feelings(60.0, include_discomfort=False)),
                "说了不给，却还是产出了不适：闸门形同虚设",
            )


if __name__ == "__main__":
    unittest.main()
