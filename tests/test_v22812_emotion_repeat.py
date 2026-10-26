"""情绪句逐轮重复注入的去重（v2.28.12）。

线上实测（2026-10-26 私聊 1420609667）：她被同一件事硌着，情绪层每轮都吐出同一句
「她内心：上回那句话到现在都还窝火」，模型于是每一句回复都把「心里窝火」照抄进
表演括注里，连续 8 条一字不差。根因不在词，在**重复**：同一件事说一遍是状态，
说到第八遍就是台词。

这里钉住两件事：
  · 同一句普通情绪话在窗口内不重复注入；
  · ccb 那句（场景实时状态）不受去重影响，不然场景里会突然没声音。
"""
import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from humanoid.config import HumanoidConfig
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.persona import PersonaSource
from humanoid.state import StateStore
from tests.fakes import FrozenClock, RecordingLogger

FEMALE = "你是小满，24岁。她说话很短，喜欢安静。"


class _Base(unittest.TestCase):
    def _core(self, raw=None):
        conf = HumanoidConfig.from_raw(raw or {"timezone_city": "北京", "ccb": False})
        st = StateStore(Path(tempfile.mkdtemp()) / "s.json", 0.01)
        st.load("2026-10-26", 28)

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

        c = C()
        src = PersonaSource(c, lambda: RecordingLogger())
        core = HumanoidCoreInstance(
            role_id="bot1", state_store=st, config_provider=lambda: conf,
            logger=RecordingLogger(), stop_event=asyncio.Event(),
            resolver=c, gateway=None, persona_source=src)
        asyncio.run(src.persona("bot1"))
        core.clock = FrozenClock(datetime(2026, 10, 26, 21, 0))
        return core


class EmotionRepeat(_Base):
    def _grudge_mood(self, core):
        us = core._scope.user_state("u1")
        us["mood"] = {
            "affection": 51.4, "base_affection": 53.9,
            "libido": 23.0, "base_libido": 30.0,
            "aggression": 47.7, "base_aggression": 28.0,
            "turn_count": 500, "last_interaction": 0.0,
            "mood_tag": "", "last_decay": 0.0,
        }
        us["mood_logs"] = [
            {"time": "2026-10-26 10:00:00", "event": "亲近欲下降至 30.0，攻击性上升至 50.0"},
            {"time": "2026-10-26 11:00:00", "event": "亲近欲下降至 28.0，攻击性上升至 50.0"},
        ]
        core._scope.set_self("energy", 70.0)

    def test_same_line_is_not_repeated_every_turn(self):
        core = self._core()
        self._grudge_mood(core)
        first = core.build_injection("u1")
        self.assertIn("内心：", first, "第一次该给情绪句")
        second = core.build_injection("u1")
        self.assertNotIn("内心：", second, "同一句不该逐轮复读")

    def test_line_comes_back_after_the_window(self):
        core = self._core()
        self._grudge_mood(core)
        core.build_injection("u1")
        core.clock.moment = core.clock.moment + timedelta(seconds=700)
        third = core.build_injection("u1")
        self.assertIn("内心：", third, "过了窗口该重新给一次，情绪不该永久消失")

    def test_ccb_line_is_not_deduped(self):
        core = self._core({"timezone_city": "北京", "ccb": True})
        us = core._scope.user_state("u1")
        us["mood"] = {
            "affection": 92.0, "base_affection": 46.0,
            "libido": 47.0, "base_libido": 34.0,
            "aggression": 20.0, "base_aggression": 10.0,
            "turn_count": 60, "last_interaction": 0.0,
            "mood_tag": "", "last_decay": 0.0,
        }
        us["ccb_intimacy_at"] = core.now_epoch()
        core._scope.set_self("energy", 70.0)
        words = ("想让你抱着我", "想离你近一点", "不想一个人")
        first = core.build_injection("u1")
        second = core.build_injection("u1")
        self.assertTrue(any(w in first for w in words), first)
        self.assertTrue(any(w in second for w in words),
                        "ccb 的实时状态句不该被去重掉：" + second)
