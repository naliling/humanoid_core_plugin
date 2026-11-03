"""v3.0.4 B5：冷静期一句话只有一个模板（#7/#8），余韵不归「场景细节」管（#82）。

两条都是「同一件事在两个地方各说一遍」的变体：

* 冷静期的句子原来由 `mood.cooling_note`（关系层）和 `emotion._cooling_line`
  （内心层）**各拼一遍** f-string。两边都把 reason 当成「…的事」的名词短语，
  而 reason 本身是「你刚才说的话」，于是注入里出现「还在为你刚才说的话的事烦着」；
  内心通道那份还带祈使句「先别理他」，把给模型的指令写进了她的独白。
* 余韵句是一条约 **独立**的情绪候选（在 pools 里比显著度），却被
  `ccb_scene_detail` 那道「场景细节」闸一起关掉了——关掉细节后连「她还没缓过来」
  也消失，与开关的文档承诺不符。
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from humanoid import ccb
from humanoid import emotion as emotion_module
from humanoid.config import HumanoidConfig
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.emotion import EmotionLayer
from humanoid.persona import PersonaSource
from humanoid.state import StateStore
from tests.fakes import FrozenClock, RecordingLogger

FEMALE = "你是小满，24岁。她说话很短，喜欢安静。"


def _core(**extra):
    raw = {"timezone_city": "北京", "ccb": True}
    raw.update(extra)
    conf = HumanoidConfig.from_raw(raw)
    st = StateStore(Path(tempfile.mkdtemp()) / "s.json", 0.01)
    st.load("2026-09-26", 28)

    class M:
        async def resolve_selected_persona(self, **k):
            return ("p", {"name": "", "prompt": FEMALE, "begin_dialogs": []}, None, False)

        async def get_default_persona_v3(self):
            return {"name": "", "prompt": FEMALE, "begin_dialogs": []}

    class C:
        persona_manager = M()
        conversation_manager = None

        def get_config(self, *a, **k):
            return None

    src = PersonaSource(C(), lambda: RecordingLogger())
    core = HumanoidCoreInstance(
        role_id="bot1", state_store=st, config_provider=lambda: conf,
        logger=RecordingLogger(), stop_event=asyncio.Event(),
        resolver=C(), gateway=None, persona_source=src,
    )
    asyncio.run(src.persona("bot1"))
    core.clock = FrozenClock(datetime(2026, 9, 26, 23, 0))
    return core


def _cooling_mood(core, reason: str, uid: str = "u1") -> dict:
    record = {
        "affection": 20.0, "base_affection": 46.0,
        "libido": 20.0, "base_libido": 34.0,
        "aggression": 40.0, "base_aggression": 10.0,
        "turn_count": 5, "last_interaction": 0.0, "last_decay": 0.0,
        "cool_until": float(core.now_epoch()) + 600.0,
        "cool_reason": reason,
    }
    core._scope.user_state(uid)["mood"] = record
    return record


class CoolingTemplate(unittest.TestCase):
    """#7/#8：冷静期的句子只能有一个模板，内心通道只给感受。"""

    def test_inner_line_is_a_feeling_not_a_command(self):
        core = _core()
        line = core.mood.cooling_line({"cool_reason": "你刚才说的话"}, inner=True)
        self.assertIn("气头上", line)
        self.assertNotIn("的事", line, "reason 是完整短语，再套「的事」就是病句")
        self.assertNotIn("先别理他", line, "祈使句不能进她的内心独白")
        self.assertNotIn("想让他先别来吵", line, "内心通道只给感受，不给态度指令")

    def test_legacy_reason_also_reads_well(self):
        core = _core()
        for reason in ("你刚才说的话", "你一直没好好理我"):
            with self.subTest(reason=reason):
                line = core.mood.cooling_line({"cool_reason": reason}, inner=True)
                self.assertIn(reason, line)
                self.assertNotIn("的事", line)

    def test_relation_line_keeps_her_attitude(self):
        core = _core()
        line = core.mood.cooling_line({"cool_reason": "你刚才说的话"})
        self.assertIn("想让他先别来吵", line, "关系层要描述她的态度")
        self.assertIn("（因为你刚才说的话）", line)
        self.assertNotIn("的事", line)

    def test_both_channels_share_one_template(self):
        """两条通道都从 `cooling_line` 出：只有外层多一个祈使尾。"""
        core = _core()
        record = _cooling_mood(core, "你刚才说的话")
        layer = EmotionLayer(core)
        got = layer._cooling_line("u1")
        self.assertIsNotNone(got)
        self.assertEqual(got[0], 0.95)
        self.assertEqual(got[1], core.mood.cooling_line(record, inner=True))
        outer = core.mood.cooling_note("u1")
        self.assertIn("气头上", outer)
        self.assertTrue(outer.endswith("过几分钟再说"))
        self.assertEqual(outer, core.mood.cooling_line(record))

    def test_cooling_candidate_is_its_own_kind_with_inner_line(self):
        core = _core()
        record = _cooling_mood(core, "你刚才说的话")
        layer = EmotionLayer(core)
        score, text, kind = layer.candidate_with_kind("u1", interest={}, agency={})
        # kind 从 "emotion" 改成 "cooling"：冷静期是会过期的状态（cool_until），
        # 不能进 prompt_builder._emotion_line 的「同一句 10 分钟内不再说」去重——
        # 冷却期默认 15 分钟，去重会让最后 5 分钟注入里一个字都不提她还在生气。
        self.assertEqual(kind, "cooling")
        self.assertEqual(score, 0.95)
        self.assertEqual(text, core.mood.cooling_line(record, inner=True))


class AftercareIsNotSceneDetail(unittest.TestCase):
    """#82：`ccb_scene_detail` 只管场景状态段，不该关掉余韵这一条候选。"""

    def test_aftercare_survives_scene_detail_off(self):
        core = _core(ccb_scene_detail=False)
        layer = EmotionLayer(core)
        state = core._scope.user_state("u1")
        state["ccb_aftercare_until"] = float(core.now_epoch()) + 600.0
        score, text = layer._aftercare_line("u1")
        self.assertIn(text, ("刚结束，还软着", "还没缓过来"), "关掉细节后余韵句不该消失")
        self.assertGreater(score, 0.5)

    def test_scene_detail_off_still_suppresses_the_scene_segment(self):
        """反向控制：细节开关关着时，场景状态段本身仍然不出。"""
        core = _core(ccb_scene_detail=False)
        layer = EmotionLayer(core)
        state = core._scope.user_state("u1")
        state["ccb_aftercare_until"] = float(core.now_epoch()) + 600.0
        self.assertEqual(layer._scene_text("原来那句", state, core.now_epoch(), "u1",
                                           core.config), "原来那句")


class PhaseLabelsStayInThePanel(unittest.TestCase):
    """#62：档位名是面板给运营看的内部刻度，注入只给同一相位的感受词。

    两边**同源**：感受表按 `ccb.phase_of()` 的返回值索引，长度与
    `ccb.PHASE_NAMES` 对齐，边界值仍由 ccb 的阈值常量决定——这里不复制任何数字。
    """

    def test_feeling_table_is_indexed_by_phase_names(self):
        self.assertEqual(len(emotion_module._SCENE_FEELINGS), len(ccb.PHASE_NAMES))
        self.assertEqual(emotion_module._SCENE_FEELINGS[0], "", "相位 0 不在场景里，不出话")
        for word in emotion_module._SCENE_FEELINGS[1:]:
            self.assertTrue(word, "每一档都要有词，空串等于静默丢信息")
            for label in ccb.PHASE_NAMES:
                if label:
                    self.assertNotIn(
                        label, word,
                        f"感受词「{word}」里含档位名「{label}」，守卫按子串查会漏",
                    )

    def test_injection_never_carries_a_phase_label(self):
        core = _core()
        layer = EmotionLayer(core)
        state = core._scope.user_state("u1")
        state["ccb_stage"] = 2.0
        state["ccb_pose"] = 0.0
        state["ccb_site"] = 1.0
        seen = set()
        for satisfy in range(0, 121, 5):
            state["ccb_satisfy"] = float(satisfy)
            now = core.now_epoch()
            phase = ccb.phase_of(
                satisfy=float(satisfy), stage=2.0, aftercare_until=0.0, now=now,
            )
            text = layer._with_scene_note("原来那句", state, now, "u1")
            seen.add(phase)
            for label in ccb.PHASE_NAMES:
                if label:
                    self.assertNotIn(
                        label, text,
                        f"satisfy={satisfy} phase={phase} 把档位名递进了注入：{text!r}",
                    )
            if phase > 0:
                self.assertIn(emotion_module._SCENE_FEELINGS[phase], text)
        self.assertTrue({1, 2, 3, 4} <= seen, f"用例没覆盖到全部相位：{seen}")

    def test_out_of_range_phase_says_nothing(self):
        for bad in (0, -1, 99, "x", None):
            with self.subTest(phase=bad):
                self.assertEqual(emotion_module._scene_feeling(bad), "")


if __name__ == "__main__":
    unittest.main()
