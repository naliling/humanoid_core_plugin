"""穿搭：给她一件贴合的衣服；她知道自己在穿什么；判不准就不给。"""
import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from humanoid import outfit
from humanoid.config import HumanoidConfig
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.persona import PersonaSource
from humanoid.state import StateStore
from tests.fakes import FrozenClock, RecordingLogger


class DetectKind(unittest.TestCase):
    def test_empty_persona_means_nothing(self):
        self.assertEqual(outfit.detect_kind("小雨", ""), "")
        self.assertEqual(outfit.detect_kind("", "   "), "")

    def test_plain_persona_is_human(self):
        self.assertEqual(
            outfit.detect_kind("小雨", "你是一个温柔的女生，喜欢看书。"), outfit.KIND_HUMAN
        )

    def test_beast_ear(self):
        self.assertEqual(
            outfit.detect_kind("阿狸", "你是一只猫娘，有猫耳和尾巴。"), outfit.KIND_BEAST_EAR
        )

    def test_beast(self):
        self.assertEqual(
            outfit.detect_kind("", "你会说话的小马，住在小马利亚。"), outfit.KIND_BEAST
        )

    def test_machine(self):
        self.assertEqual(outfit.detect_kind("", "你是一台仿生机器人。"), outfit.KIND_MACHINE)

    def test_spirit(self):
        self.assertEqual(outfit.detect_kind("", "你是一只精灵，住在森林里。"), outfit.KIND_SPIRIT)

    def test_machine_beats_beast(self):
        self.assertEqual(outfit.detect_kind("", "机械狐狸"), outfit.KIND_MACHINE)


class Resolve(unittest.TestCase):
    def test_preset_wins(self):
        self.assertEqual(
            outfit.resolve(preset="红色毛衣", name="小雨", persona="温柔女生"), "红色毛衣"
        )

    def test_builtin_by_kind(self):
        self.assertEqual(outfit.resolve(name="", persona="你会说话的小马"), "没穿衣服")

    def test_unknown_gives_nothing(self):
        self.assertEqual(outfit.resolve(name="小雨", persona=""), "")

    def test_scene_sleep(self):
        got = outfit.resolve(name="小雨", persona="温柔女生", scene="sleep")
        self.assertIn("睡衣", got)

    def test_scene_bath_is_bare(self):
        self.assertEqual(
            outfit.resolve(name="小雨", persona="温柔女生", scene="bath"), "没穿衣服"
        )

    def test_beast_ignores_scene(self):
        self.assertEqual(
            outfit.resolve(name="", persona="你会说话的小马", scene="sleep"), "没穿衣服"
        )

    def test_scene_variation_off(self):
        self.assertEqual(
            outfit.resolve(name="小雨", persona="温柔女生", scene="sleep", scene_variation=False),
            "日常便服",
        )

    def test_cold_adds_coat(self):
        self.assertIn("外套", outfit.resolve(name="小雨", persona="温柔女生", scene="cold"))


class SceneOf(unittest.TestCase):
    def test_sleep_by_event(self):
        self.assertEqual(outfit.scene_of(event="午睡"), "sleep")
        self.assertEqual(outfit.scene_of(asleep=True), "sleep")

    def test_bath(self):
        self.assertEqual(outfit.scene_of(event="洗澡"), "bath")

    def test_out(self):
        self.assertEqual(outfit.scene_of(event="出门买菜"), "out")

    def test_out_cold(self):
        self.assertEqual(outfit.scene_of(event="出门", temp_c=2.0), "cold")

    def test_unwell_by_cycle(self):
        self.assertEqual(outfit.scene_of(cycle_phase=0), "unwell")

    def test_home(self):
        self.assertEqual(outfit.scene_of(event="看书"), "home")


class Line(unittest.TestCase):
    def test_line(self):
        self.assertEqual(outfit.line("红色毛衣"), "她穿着：红色毛衣")

    def test_empty(self):
        self.assertEqual(outfit.line(""), "")


class EndToEnd(unittest.TestCase):
    """从 Core 实例走一遍：人设 → 识别 → 注入，接线对不对只有跑起来才知道。"""

    def _core(self, persona, conf_extra=None):
        # 关掉「场景变化」：这几个用例测的是基础穿着，场景（睡衣/家居服等）另有专门用例。
        raw = {"timezone_city": "北京", "outfit_scene_variation": False}
        raw.update(conf_extra or {})
        conf = HumanoidConfig.from_raw(raw)
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
            resolver=c, gateway=None, persona_source=src,
        )
        asyncio.run(src.persona("bot1"))
        core.clock = FrozenClock(datetime(2026, 9, 26, 23, 0))
        return core

    def test_human_persona_gets_outfit(self):
        core = self._core("你是一个温柔的女生，喜欢看书。")
        self.assertEqual(core.outfit_text(), "日常便服")
        self.assertIn("她穿着", core.outfit_line())
        self.assertEqual(core.outfit_info()["kind"], outfit.KIND_HUMAN)

    def test_beast_persona_is_unclothed(self):
        core = self._core("你会说话的小马，住在小马利亚。")
        self.assertEqual(core.outfit_text(), "没穿衣服")

    def test_no_persona_no_outfit(self):
        core = self._core("")
        self.assertEqual(core.outfit_text(), "")
        self.assertEqual(core.outfit_line(), "")
        self.assertEqual(core.outfit_info()["source"], "无（判不出她是什么，不注入）")

    def test_override_wins(self):
        core = self._core("你是一个温柔的女生。")
        core.set_outfit_override("白色连衣裙")
        self.assertEqual(core.outfit_text(), "白色连衣裙")
        core.set_outfit_override("自动")
        self.assertEqual(core.outfit_text(), "日常便服")

    def test_disabled_injects_nothing(self):
        core = self._core("你是一个温柔的女生。", {"outfit_enabled": False})
        self.assertEqual(core.outfit_text(), "")
        self.assertFalse(core.outfit_info()["enabled"])


if __name__ == "__main__":
    unittest.main()
