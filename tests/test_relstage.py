"""关系阶段：好感是连续的数，但它不该连续地改变行为。

好感 60 和 90 以前看起来一样——措辞、主动性、能聊到哪都没区别。真人不是这样：
熟到一定程度才会开玩笑、才会在对方不说话时直接问「你怎么了」。

**不新增状态**：阶段是算出来的，掉回基线它自己就回去了。
"""
import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from humanoid.config import HumanoidConfig
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.relstage import BANDS, line_for, stage_of
from humanoid.state import StateStore
from tests.fakes import FakeContext, FrozenClock, RecordingLogger


class Bands(unittest.TestCase):
    def test_four_stages(self):
        self.assertEqual(len(BANDS), 4)

    def test_monotonic(self):
        prev = -1
        for a in (0, 50, 60, 80, 92, 100):
            idx, short, line = stage_of(a)
            self.assertGreaterEqual(idx, prev, f"好感 {a} 的档位倒退了")
            prev = idx
            self.assertTrue(short and line)

    def test_ascending_floors(self):
        floors = [b[0] for b in BANDS]
        self.assertEqual(floors, sorted(floors))

    def test_bad_input_is_safe(self):
        for bad in (None, "", "x", [], {}):
            self.assertEqual(stage_of(bad)[0], 0)

    def test_lines_are_state_not_instructions(self):
        """只说状态，不对模型下指令。"""
        for _i, _s, line in BANDS:
            self.assertNotIn("你应该", line)
            self.assertNotIn("必须", line)


class InInjection(unittest.TestCase):
    def _inject(self, affection):
        conf = HumanoidConfig.from_raw({"timezone_city": "北京"})
        st = StateStore(Path(tempfile.mkdtemp()) / "s.json", 0.01)
        st.load("2026-09-26", 28)
        core = HumanoidCoreInstance(
            role_id="bot1", state_store=st, config_provider=lambda: conf,
            logger=RecordingLogger(), stop_event=asyncio.Event(),
            resolver=FakeContext(), gateway=None)
        core.clock = FrozenClock(datetime(2026, 9, 26, 14, 0))
        core.mood.profile("u1")["affection"] = affection
        return core.build_injection("u1")

    def test_each_stage_shows_its_own_line(self):
        markers = {
            50: "互相试探", 60: "讲点自己的事",
            80: "不用绕弯子", 92: "几乎没有边界",
        }
        for aff, mark in markers.items():
            self.assertIn(mark, self._inject(aff), f"好感 {aff} 的注入里没有「{mark}」")

    def test_high_and_low_differ(self):
        """这是这一项的全部意义：60 和 90 不该长得一样。"""
        self.assertNotEqual(self._inject(60), self._inject(90))

    def test_bad_affection_does_not_crash(self):
        self.assertTrue(self._inject(46))


if __name__ == "__main__":
    unittest.main()
