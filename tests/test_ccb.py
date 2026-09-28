"""ccb：默认关的独立开关，条件全中才触发，群里永远不出。

设计上最容易做歪的两条，这里逐条钉住：
  · **初始值被设高不该天天触发**（所以除了绝对值还卡「涨了多少」）
  · **群里绝对不出**（硬闸，不是打分低所以没选中）
"""
import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from humanoid import ccb
from humanoid.config import HumanoidConfig
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.persona import PersonaSource
from humanoid.state import StateStore
from tests.fakes import FakeContext, FrozenClock, RecordingLogger

FEMALE = "你是小满，24岁。她说话很短，喜欢安静。"
MALE = "你是阿明，26岁男设计师。他说话很直接。"


def _ok(**kw):
    base = dict(enabled=True, is_group=False, persona_prompt=FEMALE,
                affection=92.0, base_affection=46.0,
                libido=47.0, base_libido=34.0,
                energy=70.0, last_at=0.0, now=1_000_000.0)
    base.update(kw)
    return base


class Gate(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(HumanoidConfig.from_raw({}).ccb, "默认必须是关的")
        self.assertIsNone(ccb.evaluate(**_ok(enabled=False)))

    def test_fires_when_all_conditions_hold(self):
        self.assertIsNotNone(ccb.evaluate(**_ok()))

    def test_absolute_threshold(self):
        self.assertEqual(ccb.CCB_LIBIDO_MIN, 38.0)
        self.assertIsNone(ccb.evaluate(**_ok(libido=37.9)))

    def test_rise_and_floor_are_not_contradictory(self):
        """两条是与的关系。涨幅不能比绝对值相对基线还高，否则永远过不了。"""
        base = 34.0
        self.assertLessEqual(
            ccb.CCB_LIBIDO_RISE,
            ccb.CCB_LIBIDO_MIN - base,
            f"基线 {base} 时涨到 {ccb.CCB_LIBIDO_MIN} 只有 +{ccb.CCB_LIBIDO_MIN - base}，"
            f"而涨幅要求 {ccb.CCB_LIBIDO_RISE} —— 互相矛盾",
        )

    def test_must_have_actually_risen(self):
        """初始值被设高不该天天触发。"""
        self.assertIsNone(ccb.evaluate(**_ok(libido=40.0, base_libido=40.0)))
        self.assertIsNone(ccb.evaluate(**_ok(libido=42.0, base_libido=40.0)))
        self.assertIsNotNone(ccb.evaluate(**_ok(libido=45.0, base_libido=40.0)))

    def test_affection_gate(self):
        self.assertIsNone(ccb.evaluate(**_ok(affection=70.0, base_affection=46.0)))
        self.assertIsNone(ccb.evaluate(**_ok(affection=92.0, base_affection=90.0)))

    def test_energy_floor(self):
        self.assertIsNone(ccb.evaluate(**_ok(energy=10.0)))

    def test_sleeping_is_fine_when_energy_holds(self):
        """用户明确说的：她在睡觉没关系，只要有精力。"""
        self.assertIsNotNone(ccb.evaluate(**_ok(energy=70.0)))

    def test_cooldown_is_short_but_not_zero(self):
        recent = ccb.evaluate(**_ok(last_at=1_000_000.0 - 3600.0))
        self.assertIsNone(recent, "一小时前刚出过，不该连着来")
        later = ccb.evaluate(**_ok(last_at=1_000_000.0 - ccb.CCB_COOLDOWN_SECONDS - 60))
        self.assertIsNotNone(later, "过了冷却就该能再来——门槛已经很高了，冷却不该双重收费")

    def test_deterministic_not_a_dice_roll(self):
        """同一个状态跑 30 次，结果必须一致。"""
        results = {ccb.evaluate(**_ok()) is not None for _ in range(30)}
        self.assertEqual(results, {True}, "触发必须是确定的，不能掷骰子")


class HardGates(unittest.TestCase):
    def test_never_in_group(self):
        """群里是硬闸——按语气说的话没法在群里收场。"""
        self.assertIsNone(ccb.evaluate(**_ok(is_group=True)))

    def test_female_only(self):
        self.assertIsNone(ccb.evaluate(**_ok(persona_prompt=MALE)))
        self.assertTrue(ccb.looks_female(FEMALE))
        self.assertFalse(ccb.looks_female(MALE))

    def test_gender_falls_back_to_female_when_unknown(self):
        self.assertTrue(ccb.looks_female(""))
        self.assertTrue(ccb.looks_female("   "))


class Bands(unittest.TestCase):
    def test_no_gap_between_trigger_and_first_band(self):
        """触发阈值到第一档之间不能有断档。

        原来触发和档位都从 45 起。触发降到 38 而档位没跟着降的话，
        38~44 能过门槛却匹配不到任何一档 —— **到了该触发的时候他不触发**，
        比原来还糟。
        """
        first = ccb._BANDS[0][0]
        self.assertEqual(
            first, ccb.CCB_LIBIDO_MIN,
            f"第一档 {first} 与触发阈值 {ccb.CCB_LIBIDO_MIN} 不一致，中间会断档",
        )
        self.assertIsNotNone(
            ccb.evaluate(**_ok(libido=ccb.CCB_LIBIDO_MIN, base_libido=34.0)),
            "刚够到阈值就该触发")

    def test_bands_are_contiguous(self):
        """一档到下一档之间不留空：连续插值每个值都得有档位。"""
        floors = [b[0] for b in ccb._BANDS]
        for lo, hi in zip(floors, floors[1:]):
            mid = (lo + hi) / 2.0
            self.assertIsNotNone(ccb._band_for(mid), f"{lo}~{hi} 之间没有档位")

    def test_three_bands(self):
        for lib, stage in ((38.0, 1), (45.0, 2), (49.0, 3)):
            res = ccb.evaluate(**_ok(libido=lib, base_libido=34.0))
            self.assertIsNotNone(res, f"{lib} 该触发")
            self.assertEqual(res[2], stage, f"{lib} 应该在第 {stage} 档")

    def test_words_are_feelings_not_descriptions(self):
        """措辞只说感受：不写器官结构、不写情节、不第三人称。"""
        for _floor, _score, words in ccb._BANDS:
            for w in words:
                self.assertNotIn("她", w, f"「{w}」出现了第三人称")
                self.assertLessEqual(len(w), 14, f"「{w}」太长，不像一句念头")
                for bad in ("身体", "下面", "进入", "插入", "高潮", "射"):
                    self.assertNotIn(bad, w, f"「{w}」写了不该在这层出现的东西")


class EndToEnd(unittest.TestCase):
    """从 `build_injection` 走一遍——只测 `evaluate` 不知道接线对不对。"""

    def _core(self, ccb_on, persona=FEMALE, is_group=False):
        conf = HumanoidConfig.from_raw({"timezone_city": "北京", "ccb": ccb_on})
        st = StateStore(Path(tempfile.mkdtemp()) / "s.json", 0.01)
        st.load("2026-09-26", 28)

        class M:
            async def resolve_selected_persona(self, **k):
                return ("p", {"name": "", "prompt": persona, "begin_dialogs": []}, None, False)
            async def get_default_persona_v3(self):
                return {"name": "", "prompt": persona, "begin_dialogs": []}

        class C:
            persona_manager = M()
            conversation_manager = None
            def get_config(self, *a, **k):
                return None

        c = C()
        src = PersonaSource(c, lambda: RecordingLogger())
        core = HumanoidCoreInstance(
            role_id="bot1", state_store=st, config_provider=lambda: conf,
            logger=RecordingLogger(), stop_event=asyncio.Event(),
            resolver=c, gateway=None, persona_source=src)
        asyncio.run(src.persona("bot1"))
        core.clock = FrozenClock(datetime(2026, 9, 26, 23, 0))
        return core

    def _mood(self, core, **kw):
        v = {"affection": 92.0, "base_affection": 46.0, "libido": 47.0,
             "base_libido": 34.0, "aggression": 20.0, "base_aggression": 10.0,
             "turn_count": 60, "last_interaction": 0.0, "mood_tag": "", "last_decay": 0.0}
        v.update(kw)
        core._scope.user_state("u1")["mood"] = v
        core._scope.set_self("energy", kw.get("energy", 70.0))

    def _line(self, core, is_group=False):
        import re
        m = re.search(r"内心：([^）]*)）", core.build_injection("u1", is_group=is_group))
        return m.group(1) if m else ""

    def test_injection_carries_it_when_on(self):
        core = self._core(True)
        self._mood(core)
        self.assertIn("想让你抱着我", self._line(core))

    def test_injection_silent_when_off(self):
        core = self._core(False)
        self._mood(core)
        for w in ("想让你抱着我", "想离你近一点", "不想一个人"):
            self.assertNotIn(w, self._line(core))

    def test_injection_never_in_group(self):
        for lib in (38.5, 45.5, 49.5):
            core = self._core(True)
            self._mood(core, libido=lib)
            self.assertEqual(self._line(core, is_group=True), "",
                             f"亲近欲 {lib} 时群里也不该出现")

    def test_injection_silent_for_male_persona(self):
        core = self._core(True, persona=MALE)
        self._mood(core)
        for w in ("想让你抱着我", "想离你近一点", "不想一个人"):
            self.assertNotIn(w, self._line(core))

    def test_state_written_and_cleared(self):
        core = self._core(True)
        self._mood(core)
        self._line(core)
        us = core._scope.user_state("u1")
        self.assertEqual(us.get("ccb_stage"), 2.0)
        # 亲近欲掉回基线 → 状态清掉，不留尾巴
        self._mood(core, libido=35.0, base_libido=34.0)
        self._line(core)
        self.assertEqual(us.get("ccb_stage"), 0.0)


class State(unittest.TestCase):
    def test_state_roundtrip(self):
        u = {"ccb_stage": 2.0, "ccb_last_at": 5.0, "ccb_peak": 3.0, "ccb_turns": 4.0}
        st = ccb.read_state(u)
        self.assertEqual(st["ccb_stage"], 2.0)
        ccb.reset_state(u)
        self.assertEqual(ccb.read_state(u)["ccb_stage"], 0.0)

    def test_config_lands_last_in_schema(self):
        import json
        data = json.loads(
            (Path(__file__).resolve().parent.parent / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(list(data)[-1], "ccb", "ccb 应该在配置项最下面")
        self.assertEqual(data["ccb"]["default"], False)
        self.assertEqual(data["ccb"]["description"], "ccb")

    def test_not_mentioned_in_public_docs(self):
        """ccb 是隐藏开关：README 和 CHANGELOG 都不许出现。"""
        root = Path(__file__).resolve().parent.parent
        for name in ("README.md", "CHANGELOG.md"):
            path = root / name
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for i, line in enumerate(text.splitlines(), 1):
                if "曾经" in line or "更正" in line:
                    continue
                self.assertNotIn("ccb", line.lower(), f"{name}:{i} 提到了 ccb")


if __name__ == "__main__":
    unittest.main()
