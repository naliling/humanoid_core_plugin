"""P4 验收：注入成品读起来必须是「我自己的」，不是一个叫某名字的角色卡。

为什么单独一组用例：P0 之前模型在 thinking 里写「我在扮演这个角色」，用户看到的
症状不是「某个字段错了」，而是**整块文本的归属错了**——抬头是〔她的身体与生活〕、
正文全第三人称、句式是评估（「她对 TA 还不算熟」）。这一组用例守的是成品文本的
三条外部特征，任何一条被改回去，用户那边的症状就会原样回来：

1. 成品里不再出现第三人称「她」（`to_first_person` 在渲染出口统一换掉）；
2. 成品里不出现账本与读数（今日第几次、还剩几分钟、满足 85/100、12cm）；
3. 生理周期的感觉句在、且关掉周期功能时一句都不留。

三条都只扫**成品**（`build_injection` 与 `_state_note` 的返回值），不扫行级函数：
行级函数按约定仍输出「她」，出口才换人称（见 `body_sense` 与 `prompt_builder` 的
docstring）。扫行级函数会把这条约定判成 bug。
"""

from __future__ import annotations

import unittest

from humanoid import body_sense, ccb, prompt_builder

from .test_v2287_ccb_presence import _core as _ccb_core, _in_scene, _mood
from .test_v306_injection_golden import (
    _asleep_body,
    _body,
    _cooling_body,
    _group_body,
    _injection_bodies,
    _schedule_bodies,
    _short_sleep_body,
    _tier_body,
)

#: 账本语言：系统口径，模型读到会切回「我在读一个系统的记录」。
LEDGER_WORDS = (
    "今日第", "还剩", "刚换过", "第 1 次", "第1次", "满足度", "次数",
    "/100", "/50", "cm", "厘米", "分钟",
)
#: 第三人称的三种残留形态。
THIRD_PERSON = ("她", "他的", "她自己")


def _scene_body() -> str:
    """在场景里的一份成品（`_ccb_core` 带 ccb 开关与人设）。"""
    core = _ccb_core()
    _mood(core)
    state = _in_scene(core, stage=2.0, satisfy=45.0)
    state["ccb_activity_at"] = core.now_epoch() - 600.0
    return core.build_injection("u1")


def _aftercare_body() -> str:
    """余韵窗里的一份成品。"""
    core = _ccb_core()
    _mood(core)
    state = _in_scene(core, stage=2.0, satisfy=45.0)
    state["ccb_stage"] = 0.0
    state["ccb_aftercare_until"] = core.now_epoch() + 7000.0
    return core.build_injection("u1")


def _all_bodies() -> dict:
    """所有成品正文：十种状态 × 场景两态 + 状态段。"""
    out = dict(_injection_bodies())
    out["s9_scene"] = _scene_body()
    out["s10_aftercare"] = _aftercare_body()
    out["s11_state_note"] = _schedule_bodies()[1]
    return out


class FirstPersonOnlyTest(unittest.TestCase):
    """成品里不许再有第三人称：那是「角色卡」的语法特征。"""

    def test_no_third_person_anywhere(self):
        bad = {
            key: body for key, body in _all_bodies().items()
            if any(word in body for word in THIRD_PERSON)
        }
        self.assertEqual(bad, {}, "成品里残留第三人称：%r" % bad)

    def test_the_body_speaks_in_the_first_person(self):
        for key, body in _all_bodies().items():
            self.assertIn("我", body, "%s 里一句第一人称都没有" % key)

    def test_the_framing_belongs_to_the_model(self):
        framing = prompt_builder.build_framing("小满", "TA")
        self.assertNotIn("她", framing, "框架句还在教模型「她指你自己」")
        self.assertIn("我", framing)
        self.assertIn("你此刻的身体和处境", framing)
        for word in ("扮演", "角色", "设定成", "你扮演"):
            self.assertNotIn(word, framing, "框架句里出现了「%s」" % word)

    def test_the_marks_speak_in_the_first_person(self):
        self.assertEqual(prompt_builder.MARK_PREFIX, "〔我现在的身体")
        self.assertEqual(prompt_builder.STATE_MARK, "【我现在的情况】")
        self.assertIn(prompt_builder.MARK_PREFIX, _body())
        self.assertIn(prompt_builder.STATE_MARK, _schedule_bodies()[1])


class NoLedgerTest(unittest.TestCase):
    """账本与读数：用户要的是「身上什么样」，不是「系统记了几次」。"""

    def test_no_ledger_words(self):
        bad = {}
        for key, body in _all_bodies().items():
            hits = [word for word in LEDGER_WORDS if word in body]
            if hits:
                bad[key] = hits
        self.assertEqual(bad, {}, "成品里出现了账本/读数：%r" % bad)

    def test_no_stage_names_in_the_injected_body(self):
        for key in ("s9_scene", "s10_aftercare"):
            body = _all_bodies()[key]
            for name in ccb.PHASE_NAMES[1:]:
                if name:
                    self.assertNotIn(name, body, "%s 里出现了档位名「%s」" % (key, name))

    def test_the_scene_says_feelings_not_counts(self):
        body = _scene_body()
        self.assertIn("我正在亲密里", body)
        self.assertTrue(any(f in body for f in body_sense._POSE_FEELINGS.values()), "没有出现姿势感受")
        self.assertNotIn("今日第", body)


class CycleConsistencyTest(unittest.TestCase):
    """周期只给感觉：在、只说感受、关掉就一句不留。"""

    def test_the_cycle_line_is_present_in_both_tiers(self):
        for mode in ("medium", "full"):
            body = _tier_body(mode)
            self.assertTrue(
                any(line in body for line in body_sense._CYCLE_FEELINGS),
                "%s 档没有周期感受" % mode,
            )

    def test_the_cheapest_tier_does_not_carry_it(self):
        body = _tier_body("mood_only")
        self.assertFalse(
            any(line in body for line in body_sense._CYCLE_FEELINGS),
            "最省的那一档不该带周期句",
        )

    def test_it_survives_being_asleep(self):
        body = _asleep_body()
        self.assertTrue(
            any(line in body for line in body_sense._CYCLE_FEELINGS),
            "睡着也会痛经，周期句不该跟着「互动前置」一起省掉",
        )

    def test_turning_it_off_leaves_nothing(self):
        for body in (
            _body(enable_cycle=False),
            _asleep_body(enable_cycle=False),
            _body(inject_activity_context="full", enable_cycle=False),
        ):
            for line in body_sense._CYCLE_FEELINGS:
                self.assertNotIn(line, body)

    def test_it_does_not_carry_numbers(self):
        for line in body_sense._CYCLE_FEELINGS:
            self.assertNotRegex(line, r"[0-9０-９]")


class BehaviourUnchangedTest(unittest.TestCase):
    """归属改造不该动行为：同一状态下的判定结果与改造前一致。"""

    def test_the_default_body_still_says_the_same_facts(self):
        body = _body()
        for fact in ("私聊", "我内心：", "前面聊的还能接上"):
            self.assertIn(fact, body)

    def test_cooling_still_appears_once(self):
        body = _cooling_body()
        self.assertEqual(body.count("气头上"), 1)

    def test_group_still_filters_by_speaker(self):
        body = _group_body()
        self.assertIn("我自己的事", body)
        self.assertNotIn("别人的私事", body)

    def test_short_sleep_is_still_reported_once(self):
        body = _short_sleep_body()
        self.assertEqual(body.count("只睡了"), 1)


if __name__ == "__main__":
    unittest.main()