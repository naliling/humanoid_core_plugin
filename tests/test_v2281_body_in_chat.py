"""v2.28.2 修正：身体事实在普通聊天里只出现一次，来自每轮的 injection 块。

## 背景

v2.28.1 认为「v2.27.38 把〔她的身体与生活〕标记块从每轮注入改成只在主动消息时注入，
普通聊天里她看不到自己困不困、饿不饿」，于是把 `soma.feelings()` 又拼进
`main._state_note()`。但代码里每轮 `on_llm_request` 本来就会把
`core.build_injection()` 的整块**原样**追加为临时内容块——那块里一直有
`_feelings_lines()`（medium 档 2 条、full 档 5 条，阈值与 v2.28.1 相同）。
两处都发，同一句话在一轮请求里出现两次。

## 这次改什么

- `_state_note()` 不再拼身体事实；
- 身体事实继续由 `core.build_injection()` 的〔她的身体与生活〕块承担；
- 本文件盯两件事：身体事实**进得了**每轮注入；`_state_note()` **不再重复**它。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from humanoid.config import HumanoidConfig
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.prompt_builder import FRAMING_TEXT
from humanoid.state import StateStore

from .fakes import FakeContext, RecordingLogger


BODY_WORDS = ("饿得发慌", "很饿", "身体不适", "困得不行", "有些困意", "睡眠不足")


def core_with(**conf):
    store = StateStore(Path(tempfile.mkdtemp()) / "state.json", lambda: 0.01)
    store.load("2026-08-22", 28)
    config = HumanoidConfig.from_raw(
        {"timezone_city": "北京", "inject_activity_context": "medium", **conf}
    )
    core = HumanoidCoreInstance(
        role_id="bot1",
        state_store=store,
        config_provider=lambda: config,
        logger=RecordingLogger(),
        stop_event=asyncio.Event(),
        resolver=FakeContext(),
        gateway=None,
    )
    return core


def state_note_for(core, said: str = "在吗") -> str:
    """真调 `main._state_note`，拿它实际递出去的那段。"""
    from tests.test_main_integration import main_module

    return main_module._state_note(core, "42", said=said)


class BodyFactsReachThePromptTest(unittest.TestCase):
    """身体事实进得了每轮注入（走 `build_injection`，不是 state_note）。"""

    def test_injection_carries_body_facts(self) -> None:
        core = core_with()
        core.soma.advance(core.now_epoch())
        core.soma._set("hunger", 95.0)
        core.soma._set("discomfort", 88.0)
        core.soma._set("sleep_pressure", 90.0)
        injection = core.build_injection("42", is_group=False, text="在吗")
        self.assertIn("〔她的身体与生活", injection)
        self.assertTrue(
            any(word in injection for word in BODY_WORDS),
            f"身体事实没进每轮注入：{injection}",
        )

    def test_state_note_does_not_repeat_body_facts(self) -> None:
        """v2.28.1 的重复来源：state_note 又拼了一遍。2.28.2 起不许再出现。"""
        core = core_with()
        core.soma.advance(core.now_epoch())
        core.soma._set("hunger", 95.0)
        core.soma._set("discomfort", 88.0)
        core.soma._set("sleep_pressure", 90.0)
        note = state_note_for(core)
        leaked = [word for word in BODY_WORDS if word in note]
        self.assertEqual(leaked, [], f"state_note 重复注入了身体事实：{leaked}；{note}")

    def test_body_fact_appears_once_in_the_whole_prompt(self) -> None:
        """把两份真实文本拼起来数：同一个身体事实只能有一次来源。"""
        core = core_with()
        core.soma.advance(core.now_epoch())
        core.soma._set("hunger", 95.0)
        core.soma._set("discomfort", 88.0)
        core.soma._set("sleep_pressure", 90.0)
        combined = core.build_injection("42", is_group=False, text="在吗") + state_note_for(core)
        for word in ("身体不适", "饿得发慌"):
            self.assertLessEqual(
                combined.count(word), 1, f"「{word}」在一轮请求里出现多次：{combined}"
            )

    def test_soma_off_means_no_body_lines(self) -> None:
        core = core_with(soma_enabled=False)
        core.soma.advance(core.now_epoch())
        core.soma._set("hunger", 95.0)
        injection = core.build_injection("42", is_group=False, text="在吗")
        note = state_note_for(core)
        for word in BODY_WORDS:
            self.assertNotIn(word, injection, f"关掉生理层后注入里还有：{word}")
            self.assertNotIn(word, note, f"关掉生理层后 state_note 里还有：{word}")

    def test_broken_soma_does_not_kill_the_note(self) -> None:
        core = core_with()

        class _Boom:
            def feelings(self, *a, **k):
                raise RuntimeError("boom")

        core.soma = _Boom()
        note = state_note_for(core)
        self.assertIn("【此刻的她】", note)


class StillBackgroundNotContentTest(unittest.TestCase):
    """加了身体事实，但授权口径没有放宽。"""

    def test_framing_unchanged(self) -> None:
        """框架句一个字没动：仍然是「背景，不是要拿出来讲的内容」。"""
        self.assertIn("它们是背景，不是要拿出来讲的内容", FRAMING_TEXT)
        self.assertIn("对方没问起", FRAMING_TEXT)
        self.assertIn("由你自己判断", FRAMING_TEXT)

    def test_framing_has_no_new_order(self) -> None:
        for word in ("不要", "必须", "别", "控制在", "只说", "回一句", "要讲"):
            self.assertNotIn(word, FRAMING_TEXT, f"框架句出现指令：{word}")


if __name__ == "__main__":
    unittest.main()
