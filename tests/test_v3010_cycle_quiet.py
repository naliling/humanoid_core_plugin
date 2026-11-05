"""周期句一天只说一次：身体事实不该每轮重复播报。

用户原话（m07427）：「经期那句每轮被复述这个得修」。

现场：月经期横跨 1-5 天，而 _CYCLE_FEELINGS 每个阶段只有一句，于是同一句
「我这两天小腹坠着，腰也酸」一天几十轮、连着五天逐字进上下文。模型读到的
是「她一直在说这件事」，于是每轮都把它当台词复述一遍。

修法三条，这里各守一条：

1. 措辞按天轮换（body_sense._CYCLE_FEELING_SETS，8 阶段 × 3 句）；
2. 递送**三天一次**，除非对方问到了身体（prompt_builder._cycle_worth_saying）；
3. 她自己刚说过身体上的事，四小时内一律不递（humanoid/body_voice.py）。

为什么从「一天一次」收紧到「三天一次」（m08275 现场）：月经期横跨五天，
一天一次就是连着五天、每天第一轮都在说同一件事；而模型看到注入里写着
「小腹坠着」，就会在**每一句**回复里带一遍。闸门必须比「一天」更稀，
才可能让她偶尔提一句，而不是把它当口癖。
"""

from __future__ import annotations

import unittest

from humanoid import body_sense

from .test_v306_injection_golden import UID, _core

#: 所有候选措辞（8 个阶段 × 每阶段 3 句）。断言用「一句都不在」而不是只查主句：
#: 换天换句之后，主句不在不等于周期句不在。
ALL_CYCLE_LINES = tuple(
    line for choices in body_sense._CYCLE_FEELING_SETS for line in choices
)


def _cycle_lines_in(body: str) -> list:
    return [line for line in ALL_CYCLE_LINES if line in body]


class CooldownTest(unittest.TestCase):
    """第一轮说，之后三天内不再说。"""

    def test_the_first_build_carries_it(self):
        body = _core().build_injection(UID, text="在吗")
        self.assertTrue(_cycle_lines_in(body), "第一次该把周期感觉递过去")

    def test_the_second_build_the_same_day_drops_it(self):
        core = _core()
        self.assertTrue(_cycle_lines_in(core.build_injection(UID, text="在吗")))
        second = core.build_injection(UID, text="在吗")
        self.assertEqual(
            _cycle_lines_in(second), [],
            "同一天第二次还在说：模型会把它当台词逐字复述",
        )

    def test_the_next_day_it_stays_quiet(self):
        core = _core()
        core.build_injection(UID, text="在吗")
        core.scope.update_self(cycle_said_at=core.now_epoch() - 86400.0)
        body = core.build_injection(UID, text="在吗")
        self.assertEqual(
            _cycle_lines_in(body), [],
            "隔天就再说一遍，五天经期就是五遍同一件事",
        )

    def test_it_comes_back_after_three_days(self):
        core = _core()
        core.build_injection(UID, text="在吗")
        core.scope.update_self(cycle_said_at=core.now_epoch() - 3 * 86400.0 - 60.0)
        body = core.build_injection(UID, text="在吗")
        self.assertTrue(_cycle_lines_in(body), "过了三天就该重新说一次")


class AskedAboutBodyTest(unittest.TestCase):
    """对方问到了，就算今天说过也要给。"""

    def test_asking_brings_it_back_the_same_day(self):
        core = _core()
        core.build_injection(UID, text="在吗")
        body = core.build_injection(UID, text="你肚子还疼吗")
        self.assertTrue(_cycle_lines_in(body), "问到了身体却不回答，等于她没感觉")

    def test_an_unrelated_message_does_not_bring_it_back(self):
        core = _core()
        core.build_injection(UID, text="在吗")
        body = core.build_injection(UID, text="晚上吃什么")
        self.assertEqual(_cycle_lines_in(body), [])


class WordingRotatesTest(unittest.TestCase):
    """同一个阶段横跨多天时，措辞要换；同一天必须稳定。"""

    def test_two_days_in_the_same_phase_do_not_use_the_same_words(self):
        first = _core()
        first.scope.update_self(current_cycle_day=9)
        second = _core()
        second.scope.update_self(current_cycle_day=10)
        self.assertNotEqual(
            body_sense.cycle_line(first), body_sense.cycle_line(second),
            "月经期五天用同一句，等于没有轮换",
        )

    def test_the_same_day_is_stable(self):
        core = _core()
        self.assertEqual(
            body_sense.cycle_line(core), body_sense.cycle_line(core),
            "一轮一个说法比重复更糟",
        )


class SwitchOffTest(unittest.TestCase):
    """关掉周期功能就一句都不许留。"""

    def test_no_cycle_line_when_disabled(self):
        body = _core(enable_cycle=False).build_injection(UID, text="在吗")
        self.assertEqual(_cycle_lines_in(body), [])

    def test_no_cycle_line_when_disabled_and_asked(self):
        body = _core(enable_cycle=False).build_injection(UID, text="你肚子疼吗")
        self.assertEqual(_cycle_lines_in(body), [])


if __name__ == "__main__":
    unittest.main()
