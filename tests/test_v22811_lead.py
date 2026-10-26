"""v2.28.11：谁在动 / 主导度 / ccb_lead。

背景：模型被动不是「状态没给够」那么简单——注入的措辞本身是「想让你别停」
这种等对方动的句式，她就被写成了受的一方。这一版补：
1. 动作识别（她说的 / 他说的动作分开记）；
2. 主导度纯函数（人设词 + 实际动作比例 + 心气 + 关系档）；
3. ccb_lead = auto/her/him，主动档措辞主语是她（想自己来 / 不想再等了）；
4. 全都仍然只是「念头」，不是指令、不写器官、不写动作脚本。
"""

from __future__ import annotations

import logging
import json
import sys
import types
import unittest
from pathlib import Path

if "astrbot" not in sys.modules:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("ccb_lead_test")
    astrbot.api = api
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api})

from humanoid import ccb  # noqa: E402
from humanoid.config import HumanoidConfig  # noqa: E402

_HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = _HERE.parent if (_HERE.parent / "_conf_schema.json").is_file() else _HERE.parent
SCHEMA = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))


class DetectActionTest(unittest.TestCase):
    def test_her_action(self):
        for text in ("她按住他的手腕", "自己坐上来", "把他推倒", "跨坐在他身上"):
            self.assertGreater(ccb.detect_action(text), 0, text)

    def test_no_action(self):
        for text in ("今天天气不错", "我好想你", "按手机看了眼时间", "推开门走进来"):
            self.assertEqual(ccb.detect_action(text), 0, text)

    def test_negated_action_not_counted(self):
        self.assertEqual(ccb.detect_action("别按住我"), 0)
        self.assertEqual(ccb.detect_action("没有抱紧"), 0)


class DominanceTest(unittest.TestCase):
    def test_persona_words_move_it(self):
        dom = ccb.dominance(persona_prompt="她是强势的女王，习惯掌控")
        sub = ccb.dominance(persona_prompt="她很害羞，性子顺从又乖")
        self.assertGreater(dom, sub)
        self.assertGreater(dom, 0.5)
        self.assertLess(sub, 0.5)

    def test_acts_ratio_moves_it(self):
        her = ccb.dominance(persona_prompt="普通女孩", her_acts=8, his_acts=2)
        him = ccb.dominance(persona_prompt="普通女孩", her_acts=2, his_acts=8)
        self.assertGreater(her, him)

    def test_bounds(self):
        for value in (
            ccb.dominance(persona_prompt="", aggression=-99, her_acts=-5),
            ccb.dominance(persona_prompt="女王" * 500, aggression=999, her_acts=999),
        ):
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)


class EvaluateActiveWordsTest(unittest.TestCase):
    """lead=her（或 auto + 高主导）时，措辞必须换成「主语是她」的那一套。"""

    def _ev(self, **kw):
        params = dict(
            enabled=True,
            is_group=False,
            persona_prompt="成年女性",
            affection=90.0,
            base_affection=40.0,
            libido=50.0,
            base_libido=10.0,
            energy=80.0,
            last_at=0.0,
            now=10_000.0,
            affection_min=0.0,
            affection_rise=0.0,
            affection_deep=0.0,
            libido_min=0.0,
            libido_rise=0.0,
            initiative_score=0.0,
        )
        params.update(kw)
        return ccb.evaluate(**params)

    def test_her_mode_uses_active_words(self):
        res = self._ev(lead="her")
        self.assertIsNotNone(res)
        self.assertIn(res[1], ccb._ACTIVE_WORDS)

    def test_him_mode_keeps_passive_words(self):
        res = self._ev(lead="him")
        self.assertIsNotNone(res)
        self.assertNotIn(res[1], ccb._ACTIVE_WORDS)

    def test_auto_high_dominance_uses_active_words(self):
        res = self._ev(lead="auto", dominance_score=0.9)
        self.assertIsNotNone(res)
        self.assertIn(res[1], ccb._ACTIVE_WORDS)

    def test_auto_low_dominance_keeps_passive_words(self):
        res = self._ev(lead="auto", dominance_score=0.1)
        self.assertIsNotNone(res)
        self.assertNotIn(res[1], ccb._ACTIVE_WORDS)


class LeadConfigTest(unittest.TestCase):
    def test_default_is_auto(self):
        self.assertEqual(HumanoidConfig.from_raw({}).ccb_lead, "auto")

    def test_valid_values_kept(self):
        for value in ("her", "him", "auto"):
            self.assertEqual(HumanoidConfig.from_raw({"ccb_lead": value}).ccb_lead, value)

    def test_bad_value_falls_back(self):
        self.assertEqual(HumanoidConfig.from_raw({"ccb_lead": "谁都行"}).ccb_lead, "auto")

    def test_schema_has_it(self):
        self.assertIn("ccb_lead", SCHEMA)
        self.assertEqual(SCHEMA["ccb_lead"]["default"], "auto")


class ResetSceneTest(unittest.TestCase):
    def test_action_counters_are_cleared(self):
        state = {"ccb_stage": 3.0, "ccb_her_acts": 5.0, "ccb_his_acts": 2.0,
                 "ccb_last_actor": 1.0}
        ccb.reset_scene(state)
        self.assertEqual(state["ccb_her_acts"], 0.0)
        self.assertEqual(state["ccb_his_acts"], 0.0)
        self.assertEqual(state["ccb_last_actor"], 0.0)


if __name__ == "__main__":
    unittest.main()
