"""v2.28.1 选项 B：普通聊天里她也知道自己的身体。

## 背景

v2.27.38 把「〔她的身体与生活〕」标记块从**每轮**注入改成只在**主动消息**时注入
（理由是那里面混着契约版本号、uid、块头，每轮重发既胀 token 又像「要回应的内容」）。
后果是普通聊天里她只剩时间/日程/关系/心境——用户回一句「在干嘛」，她看不到自己
困不困、饿不饿。原话：「上下文里没有插件的任何痕迹」。

## 这次做什么

不把标记块原样加回来，只把**中性的身体事实**补进每轮都重发的那段【此刻的她】：

* 来源是 `soma.feelings()`——它已经过滤掉台词与情绪倾向，只留 sleepy/debt/hunger/
  discomfort/arousal 五个轴的**档位词**；
* 只取显著度 >= 0.75 的前三条，不进数字、不进块头、不进契约版本号；
* 走的是同一条 `mark_as_temp()` 临时块，不落会话历史，前缀缓存代价为零；
* 「要不要提」仍由 system_prompt 的框架句管，本文件盯着它没有被放宽成指令。
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

from .fakes import FakeContext, FrozenClock, RecordingLogger


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
    import sys

    from tests.test_main_integration import main_module

    return main_module._state_note(core, "42", said=said)


class BodyFactsInChatTest(unittest.TestCase):
    """身体事实进得了普通聊天的注入。"""

    def test_body_facts_reach_the_state_note(self) -> None:
        core = core_with()
        core.soma.advance(core.now_epoch())
        core.soma._set("hunger", 95.0)
        core.soma._set("discomfort", 88.0)
        core.soma._set("sleep_pressure", 90.0)
        note = state_note_for(core)
        self.assertIn("饿得发慌", note, f"身体事实没进注入：{note}")

    def test_only_significant_axes_are_shown(self) -> None:
        """不显著的轴（肚子咕了一声）不该出现。"""
        core = core_with()
        core.soma.advance(core.now_epoch())
        core.soma._set("hunger", 2.0)
        core.soma._set("discomfort", 1.0)
        core.soma._set("sleep_pressure", 3.0)
        note = state_note_for(core)
        for word in ("很饿", "饿得发慌", "身体不适", "困得不行"):
            self.assertNotIn(word, note, f"不显著的轴被注入了：{note}")

    def test_at_most_three_axes(self) -> None:
        """最多三条：再多这段就变成体检单。"""
        core = core_with()
        core.soma.advance(core.now_epoch())
        core.soma._set("hunger", 99.0)
        core.soma._set("discomfort", 99.0)
        core.soma._set("sleep_pressure", 99.0)
        core.soma._set("sleep_debt", 9.0)
        note = state_note_for(core)
        # 由「、」连起来的身体句只有一组；数一下轴词个数
        axes = [w for w in ("很饿", "身体不适", "困得不行", "睡眠不足") if w in note]
        self.assertLessEqual(len(axes), 3, f"身体句超过三条：{axes}")

    def test_subject_is_not_repeated(self) -> None:
        """「她困得不行、她饿得发慌」读起来像两条独立通报，主语只出现一次。"""
        core = core_with()
        core.soma.advance(core.now_epoch())
        core.soma._set("hunger", 95.0)
        core.soma._set("discomfort", 88.0)
        note = state_note_for(core)
        self.assertNotIn("她饿得发慌、她", note)
        self.assertNotIn("、她", note, f"连接后还带着主语：{note}")

    def test_no_numbers_leak_in(self) -> None:
        """体检单读数不许进上下文——那些是 /你的状态 给人看的。"""
        core = core_with()
        core.soma.advance(core.now_epoch())
        core.soma._set("hunger", 95.0)
        note = state_note_for(core)
        for bad in ("/100", "%", "95", "饥饿", "躯体不适："):
            self.assertNotIn(bad, note, f"读数漏进注入：{bad}")

    def test_no_contract_header_or_uid(self) -> None:
        """标记块的头（版本号/uid/块名）不许回来。"""
        core = core_with()
        core.soma.advance(core.now_epoch())
        core.soma._set("hunger", 95.0)
        note = state_note_for(core)
        for bad in ("〔她的身体与生活", "她的身体与生活 v", "uid=", "契约"):
            self.assertNotIn(bad, note, f"标记块成分回来了：{bad}")

    def test_soma_off_means_no_body_lines(self) -> None:
        """关掉生理层就别编身体事实。"""
        core = core_with(soma_enabled=False)
        core.soma.advance(core.now_epoch())
        core.soma._set("hunger", 95.0)
        note = state_note_for(core)
        self.assertNotIn("饿得发慌", note)

    def test_broken_soma_does_not_kill_the_note(self) -> None:
        """身体层炸了不该把整段 state_note 带走——时间/关系还在。"""
        core = core_with()

        class _Boom:
            def feelings(self, *a, **k):
                raise RuntimeError("boom")

        core.soma = _Boom()
        note = state_note_for(core)
        self.assertIn("【此刻的她】", note)

    def test_sleep_switch_still_respected(self) -> None:
        """关掉「提她睡没睡」时，睡眠事实也不在这里换句话出现。"""
        core = core_with(show_sleep_window=False)
        core.soma.advance(core.now_epoch())
        core.soma._set("sleep_pressure", 99.0)
        note = state_note_for(core)
        self.assertNotIn("困得不行", note)
        self.assertNotIn("在睡", note)


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