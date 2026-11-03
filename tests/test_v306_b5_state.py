"""B5 测试补充（t1b-state）：睡词优先（#58）与特质互斥（#46/#55）的回归。

这两个修复都在「判定」层，改坏了不会有异常、也不会让别处报错——只会让角色在错误的时间
睡觉、或者收到互相打架的两句背景。所以每条用例都写清：对应哪个编号、以及**将来哪一行
改动会让它变红**。

#58（humanoid/slots.py）：判定顺序固定为 强醒 → 强睡 → 弱醒 → 默认醒着。
* 旧写法只要句中有睡字就算睡，「起床后洗了个澡，准备睡个午觉」被判成正在睡。
* 「洗」不在 AWAKE_WORDS 里：它在睡前也会出现，不能否掉 slot_sleeps 的复合判据，
  否则「洗漱完在卧室安静待着」这种没有睡字的真睡段会被判醒。
* SLEEP_EXCLUDE_WORDS（失眠/睡不着/睡眠不足/睡眠浅）在 slot_sleeps 的复合判据之前也生效：
  只查 is_sleep_event 时，卧室 + 睡量级消耗 + 8 小时的「失眠」会被判成睡着。
* 「精神/清醒」留在 AWAKE_WORDS：「洗了个澡，精神了」在卧室 + 睡量级消耗 + 8 小时的
  复合判据下必须判醒（这是 #58 实测出来的反例）。
* 「熬夜」不在 SLEEP_EXCLUDE_WORDS（那张表在睡词之前生效，会把「熬夜后睡到中午」
  的睡一起否掉），只在 AWAKE_WORDS 里否掉复合判据。

#46/#55（humanoid/traits.py）：同一事实只发一条背景，且只陈述不命令。
* 作息组（倒班 > 夜猫子 > 早起型）、勤快组、社交组、职业组各自互斥；「夜班」人设
  原来同时拿到「夜猫子」「倒班/夜班」「上班族」三行。
* 线索是第三人称状态陈述；出现「请/要/应该/记得/别/可以」就把背景变成了指令。
"""

from __future__ import annotations

import unittest

from humanoid import traits
from humanoid.slots import is_sleep_event, slot_sleeps


def _bed_slot(event: str, start: str = "23:00", end: str = "07:00", rate: float = 0.1) -> dict:
    """卧室 + 睡量级消耗 + 8 小时：复合判据会成立，除非事件名里有醒着的证据。"""
    return {
        "event": event,
        "location": "卧室",
        "emotion": "困倦",
        "energy_rate": rate,
        "start": start,
        "end": end,
    }


class SleepWordOrderTest(unittest.TestCase):
    """#58：一句话里同时有睡词和醒词时，判定顺序决定她在不在睡。"""

    def test_washing_then_bed_is_sleeping(self):
        """"洗漱后上床睡觉"：弱醒词「洗」不该压掉强睡词「上床/睡」。"""
        self.assertTrue(is_sleep_event("洗漱后上床睡觉"))
        self.assertTrue(slot_sleeps(_bed_slot("洗漱后上床睡觉")))

    def test_washed_and_ready_to_sleep_is_sleeping(self):
        """"洗完澡准备睡了"：只要句中有「睡」，洗澡不构成醒着的证据。"""
        self.assertTrue(is_sleep_event("洗完澡准备睡了"))
        self.assertTrue(slot_sleeps(_bed_slot("洗完澡准备睡了")))

    def test_shower_that_wakes_her_up_is_awake(self):
        """「洗了个澡，精神了」：#58 的实测反例——卧室 + 0.1 + 8 小时也不能判睡。"""
        self.assertFalse(is_sleep_event("洗了个澡，精神了"))
        self.assertFalse(slot_sleeps(_bed_slot("洗了个澡，精神了")))

    def test_just_woke_up_washing_is_awake(self):
        """「刚起床，去洗漱」：强醒词先于一切睡词/复合判据。"""
        self.assertFalse(is_sleep_event("刚起床，去洗漱"))
        self.assertFalse(slot_sleeps(_bed_slot("刚起床，去洗漱")))

    def test_strong_awake_beats_strong_sleep(self):
        """「起床后洗了个澡，准备睡个午觉」：刚起床的人不能被记成正在睡。"""
        self.assertFalse(is_sleep_event("起床后洗了个澡，准备睡个午觉"))
        self.assertFalse(slot_sleeps(_bed_slot("起床后洗了个澡，准备睡个午觉")))

    def test_staying_up_late_is_awake(self):
        """「熬夜到三点」：熬夜在 AWAKE_WORDS 里，是否掉复合判据的清醒动作。"""
        self.assertFalse(is_sleep_event("熬夜到三点"))
        self.assertFalse(slot_sleeps(_bed_slot("熬夜到三点")))

    def test_slept_until_noon_after_staying_up_late_is_sleeping(self):
        """「熬夜后睡到中午」：熬夜不能进 SLEEP_EXCLUDE_WORDS，后半句的睡词要赢。"""
        self.assertTrue(is_sleep_event("熬夜后睡到中午"))
        self.assertTrue(slot_sleeps(_bed_slot("熬夜后睡到中午", start="09:00", end="12:00")))

    def test_insomnia_is_awake(self):
        """「失眠」「睡不着」：床上醒着，排除表先于睡词生效。"""
        for event in ("失眠", "睡不着翻来覆去", "睡眠不足"):
            with self.subTest(event=event):
                self.assertFalse(is_sleep_event(event))

    def test_insomnia_slot_is_not_sleeping(self):
        """同一条排除表也必须在 slot_sleeps 的复合判据前生效（#58 残留修复）。

        没有 AWAKE_WORDS 的词（「翻来覆去」不在表里）时，只有排除表能挡住复合判据：
        删掉 slot_sleeps 里那一行，这里立刻变红。
        """
        for event in ("失眠", "睡不着翻来覆去", "睡眠不足", "睡眠浅"):
            with self.subTest(event=event):
                self.assertFalse(is_sleep_event(event))
                self.assertFalse(slot_sleeps(_bed_slot(event)))

    def test_meal_wins_over_nap(self):
        """「午餐与午休发呆」：在吃饭，不是在睡觉（吃饭判据在最前面）。"""
        self.assertFalse(is_sleep_event("午餐与午休发呆"))
        self.assertFalse(slot_sleeps(_bed_slot("午餐与午休发呆", start="12:00", end="13:00")))

    def test_washing_does_not_block_the_bed_compound_rule(self):
        """「洗漱完，在卧室安静待着」：没有睡字，但「洗」不该挡掉复合判据。"""
        slot = _bed_slot("洗漱完，在卧室安静待着")
        self.assertFalse(is_sleep_event(slot["event"]))
        self.assertTrue(slot_sleeps(slot))

    def test_compound_rule_still_needs_all_four_signs(self):
        """复合判据不能被放宽成「在卧室就算睡」：躺着刷手机仍是醒着。"""
        self.assertFalse(slot_sleeps(_bed_slot("躺在床上刷手机", rate=0.05)))
        self.assertFalse(slot_sleeps(_bed_slot("在卧室收拾东西", rate=0.2)))


class TraitsExclusiveHintsTest(unittest.TestCase):
    """#46/#55：同一事实只发一条背景，且只陈述、不命令。"""

    def test_shift_worker_gets_one_sleep_line(self):
        """「上夜班，三班倒」：原来同时拿到夜猫子 + 倒班 + 上班族三行。"""
        hints = traits.detect("她在医院上夜班，三班倒").hints
        self.assertEqual(len(hints), 1, hints)
        self.assertIn("倒班", hints[0])
        self.assertNotIn("夜猫子", "".join(hints))
        self.assertNotIn("上班族", "".join(hints))

    def test_night_owl_alone_when_not_shift_worker(self):
        """没有倒班证据时，夜猫子照常输出一条。"""
        hints = traits.detect("夜猫子，凌晨才睡").hints
        self.assertEqual(len(hints), 1, hints)
        self.assertIn("夜猫子", hints[0])

    def test_early_bird_alone(self):
        """早起型与夜猫子互斥：只写早起的人设不该拿到凌晨清醒那条。"""
        hints = traits.detect("早睡早起，清晨晨跑").hints
        self.assertEqual(len(hints), 1, hints)
        self.assertIn("早起型", hints[0])

    def test_lazy_beats_diligent(self):
        """「懒散」比「认真」更明确时，只给懒散那条，不让 prompt 自己打架。"""
        hints = traits.detect("她很懒散，但工作很认真").hints
        self.assertEqual(len(hints), 1, hints)
        self.assertIn("不算勤快", hints[0])

    def test_diligent_alone_when_nothing_lazy(self):
        """没有懒散证据时，勤快那条要出得来（互斥不能退化成永远不发）。"""
        hints = traits.detect("自律又上进，工作狂").hints
        self.assertEqual(len(hints), 1, hints)
        self.assertIn("勤快自律", hints[0])

    def test_homebody_beats_outgoing(self):
        """宅与外向互斥：优先级高的一条胜出。"""
        hints = traits.detect("她很宅，但也爱出门").hints
        self.assertEqual(len(hints), 1, hints)
        self.assertIn("偏宅", hints[0])

    def test_student_beats_worker(self):
        """学生与上班族互斥：「大学生也在打工」不该收到两句职业背景。"""
        hints = traits.detect("她是大学生，也在打工上班").hints
        self.assertEqual(len(hints), 1, hints)
        self.assertIn("学生", hints[0])

    def test_worker_alone_when_not_student(self):
        """只有上班证据时，上班族那条要出得来。"""
        hints = traits.detect("上班族，每天通勤").hints
        self.assertEqual(len(hints), 1, hints)
        self.assertIn("上班族", hints[0])

    def test_every_group_emits_at_most_one_line(self):
        """一份把人设写满的档案：四组各最多一条，总数不超过 4。"""
        hints = traits.detect(
            "她是大学生，上夜班三班倒，夜猫子，早起，很懒散也很自律，"
            "宅但不爱出门，外向爱社交，朋友多"
        ).hints
        self.assertLessEqual(len(hints), 4, hints)
        for marker in ("倒班", "夜猫子", "早起型"):
            self.assertLessEqual("".join(hints).count(marker), 1, hints)
        for marker in ("不算勤快", "勤快自律", "偏宅", "外向爱出门", "学生", "上班族"):
            self.assertLessEqual("".join(hints).count(marker), 1, hints)

    def test_hints_are_statements_not_commands(self):
        """#55：线索是「她是什么样的人」，不是对模型的指令。"""
        forbidden = ("请", "应该", "记得", "别", "可以", "要")
        personas = (
            "她在医院上夜班，三班倒",
            "夜猫子，凌晨才睡",
            "早睡早起，清晨晨跑",
            "她很懒散，但工作很认真",
            "自律又上进，工作狂",
            "她很宅，但也爱出门",
            "外向爱社交，朋友多",
            "她是大学生，也在打工上班",
            "上班族，每天通勤",
        )
        for persona in personas:
            for hint in traits.detect(persona).hints:
                with self.subTest(persona=persona, hint=hint):
                    for word in forbidden:
                        self.assertNotIn(word, hint, "背景变成了指令：%s" % hint)
                    self.assertNotIn("你", hint, "线索要用第三人称：%s" % hint)


if __name__ == "__main__":
    unittest.main()
