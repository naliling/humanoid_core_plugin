"""人设正文临时取不到时，安全闸沿用上次那份（v2.28.13）。

线上日志里那行「拿不到人设正文，安全闸本轮不生效」不是噪音：人设源偶发取不到
（还没加载完 / 缓存没命中）会让成人档的年龄/血缘判定**静默失效**。这里钉住：
  · 取到过一次就记住，之后临时失败沿用；
  · 一次都没取到过才真的空着，且仍记一次警；
  · 人设换了，缓存跟着换。
"""
import asyncio
import datetime
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from humanoid.config import HumanoidConfig
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.state import StateStore
from tests.fakes import FrozenClock, RecordingLogger

MINOR = "你是小满，14岁，初中生。"
ADULT = "你是小满，24岁，心理咨询师。"


class _StubPersona:
    """按顺序吐人设正文；吐空/None 模拟「这一轮取不到」。"""

    def __init__(self, prompts):
        self.prompts = list(prompts)
        self.calls = 0

    def cached_persona(self, role_id):
        i = min(self.calls, len(self.prompts) - 1)
        self.calls += 1
        text = self.prompts[i] if self.prompts else ""
        return SimpleNamespace(prompt=text) if text else None


class _Ctx:
    persona_manager = None
    conversation_manager = None

    def get_config(self, *a, **k):
        return None


def _core(logger):
    conf = HumanoidConfig.from_raw({"timezone_city": "北京", "ccb": True})
    st = StateStore(Path(tempfile.mkdtemp()) / "s.json", 0.01)
    st.load("2026-10-26", 28)
    core = HumanoidCoreInstance(
        role_id="bot1", state_store=st, config_provider=lambda: conf,
        logger=logger, stop_event=asyncio.Event(), resolver=_Ctx(),
        gateway=None, persona_source=None,
    )
    core.clock = FrozenClock(datetime.datetime(2026, 10, 26, 23, 0))
    core.persona_source = None
    return core


class PersonaCache(unittest.TestCase):
    def test_cached_persona_is_used_when_source_misses(self):
        logger = RecordingLogger()
        core = _core(logger)
        core.persona_source = _StubPersona([MINOR, "", "", None])
        self.assertEqual(core.ccb_persona_text(), MINOR)
        self.assertEqual(core.ccb_persona_text(), MINOR, "取不到时应沿用上次那份")
        self.assertEqual(core.ccb_persona_text(), MINOR)
        self.assertTrue(core.ccb_safety(), "未成年必须仍然被拦下")

    def test_never_loaded_persona_warns_once_and_allows(self):
        logger = RecordingLogger()
        core = _core(logger)
        core.persona_source = _StubPersona([""])
        self.assertEqual(core.ccb_persona_text(), "")
        self.assertEqual(core.ccb_safety(), "")
        warn = [m for lvl, m in logger.records if "安全闸" in m]
        self.assertEqual(len(warn), 1, "一次都没取到时应当且只应当报一次警")

    def test_persona_change_replaces_the_cache(self):
        logger = RecordingLogger()
        core = _core(logger)
        core.persona_source = _StubPersona([MINOR, ADULT, ""])
        self.assertEqual(core.ccb_persona_text(), MINOR)
        self.assertEqual(core.ccb_persona_text(), ADULT, "人设换了，缓存要跟着换")
        self.assertEqual(core.ccb_persona_text(), ADULT, "换了之后再取不到，沿用新那份")
        self.assertEqual(core.ccb_safety(), "", "成年人设不该被安全闸拦")

    def test_exception_in_source_uses_the_cache(self):
        logger = RecordingLogger()
        core = _core(logger)
        cache = _StubPersona([ADULT])

        class Boom:
            def cached_persona(self, role_id):
                raise RuntimeError("boom")

        core.persona_source = cache
        self.assertEqual(core.ccb_persona_text(), ADULT)
        core.persona_source = Boom()
        self.assertEqual(core.ccb_persona_text(), ADULT)


if __name__ == "__main__":
    unittest.main()
