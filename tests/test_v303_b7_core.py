"""B7 / t4-runtime：人格重排标记、主动消息文本、ccb_settle 文档。

三条都是「记账写在副作用之前」的同型缺陷，用例直接盯记账位置：

* #137 `_realign_persona` 把 `persona_realigned` 标记写在「日程是不是模型排的」判断
  之前：兼底日程那天认出人设 → 标记落了、重排没发生，等模型那版接手时反倒不重排。
  同时 `note_umo` 在多会话部署里每条消息都可能 spawn 一次重排（umo 一变就 spawn），
  而重排只跟「人设是谁 + 今天」有关。
* #138 `_note_social_outgoing` 先落去重时间戳再取文本：取文本抛一次异常，这一次
  「她主动开口」就永久丢失（下一轮 `at <= last_at` 直接返回），且没有任何日志。
* #165 `ccb_settle` 的 docstring 说做两件事（回落欲望 + 记 peak），实现只做一件。
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.config import HumanoidConfig
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.services.schedule import SOURCE_LLM, SOURCE_TEMPLATE
from humanoid.state import StateStore

from .fakes import FakeContext, FrozenClock, RecordingLogger

TZ = ZoneInfo("Asia/Shanghai")
MOMENT = datetime(2026, 8, 22, 15, 20, tzinfo=TZ)
TODAY = MOMENT.strftime("%Y-%m-%d")


def cfg(**overrides) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", **overrides})


def make_core(**conf) -> HumanoidCoreInstance:
    store = StateStore(Path(tempfile.mkdtemp()) / "state.json", lambda: 0.01)
    store.load(TODAY, 28)
    config = cfg(**conf)
    core = HumanoidCoreInstance(
        role_id="bot1",
        state_store=store,
        config_provider=lambda: config,
        logger=RecordingLogger(),
        stop_event=asyncio.Event(),
        resolver=FakeContext(),
        gateway=None,
    )
    core.clock = FrozenClock(MOMENT)
    return core


class _Persona:
    def __init__(self, label: str = "小雨") -> None:
        self.label = label
        self.name = label
        self.usable = True
        self.text = ""


class _PersonaSource:
    """只实现 core 用到的那三个口：cached_persona / note_umo / persona。"""

    def __init__(self, label: str = "小雨") -> None:
        self.label = label
        self._cached = _Persona(label)
        self.umos: list[tuple[str, str]] = []

    def cached_persona(self, role_id: str):
        return self._cached

    def cached_name(self, role_id: str) -> str:
        return self.label

    def note_umo(self, role_id: str, umo: str) -> bool:
        """真实实现一改 umo 就把人格缓存丢掉（persona.py:205）。"""
        self.umos.append((role_id, umo))
        self._cached = None
        return True

    async def persona(self, role_id: str):
        return _Persona(self.label)


class PersonaRealignStampTest(unittest.TestCase):
    """#137：标记只在实际重排之后落。"""

    def test_a_day_without_llm_schedule_does_not_consume_the_stamp(self):
        core = make_core()
        core.persona_source = _PersonaSource()
        core.scope.set_self("schedule_source", SOURCE_TEMPLATE)
        calls: list[dict] = []
        core.schedule.request_refresh = lambda **kw: calls.append(kw)
        asyncio.run(core._realign_persona())
        self.assertEqual(
            core.scope.get_self("persona_realigned", ""),
            "",
            "兼底日程那天没有重排，就不该把这一天记成「已重排」",
        )
        self.assertEqual(calls, [], "兼底日程那天不请求重排")

    def test_the_same_day_still_realigns_once_the_llm_schedule_takes_over(self):
        core = make_core()
        core.persona_source = _PersonaSource()
        core.scope.set_self("schedule_source", SOURCE_TEMPLATE)
        calls: list[dict] = []
        core.schedule.request_refresh = lambda **kw: calls.append(kw)
        asyncio.run(core._realign_persona())
        # 模型排的那一版接手了：同一天、同一个人设，现在必须真的重排一次。
        core.scope.set_self("schedule_source", SOURCE_LLM)
        asyncio.run(core._realign_persona())
        self.assertEqual(calls, [{"force": True, "ignore_cooldown": True}])
        self.assertEqual(core.scope.get_self("persona_realigned", ""), f"{TODAY}:小雨")

    def test_note_umo_does_not_spawn_again_once_today_is_realigned(self):
        core = make_core()
        source = _PersonaSource()
        core.persona_source = source
        core.scope.set_self("persona_realigned", f"{TODAY}:小雨")
        spawned: list[str] = []

        def fake_spawn(coro, name):
            spawned.append(name)
            close = getattr(coro, "close", None)
            if close is not None:
                close()
            return None

        core._spawn_background = fake_spawn
        self.assertTrue(core.note_umo("qq:GroupMessage:2"))
        self.assertEqual(spawned, [], "今天这份人设已经重排过，换个群说话不该再 spawn")
        self.assertEqual(source.umos, [("bot1", "qq:GroupMessage:2")], "umo 仍要记下来")

    def test_note_umo_still_spawns_when_the_label_changed(self):
        core = make_core()
        source = _PersonaSource("小雨")
        core.persona_source = source
        core.scope.set_self("persona_realigned", f"{TODAY}:别人")
        spawned: list[str] = []

        def fake_spawn(coro, name):
            spawned.append(name)
            close = getattr(coro, "close", None)
            if close is not None:
                close()
            return None

        core._spawn_background = fake_spawn
        core.note_umo("qq:GroupMessage:3")
        self.assertEqual(spawned, ["persona-realign"], "人设不是同一个，必须去重排")


class SocialOutgoingTextTest(unittest.TestCase):
    """#138：文本取到了才落去重时间戳。"""

    def test_a_failed_read_does_not_advance_the_stamp(self):
        core = make_core()

        def boom(role_id: str = "") -> str:
            raise RuntimeError("信号文件坏了")

        core.signals.last_proactive_text = boom
        core._note_social_outgoing("u1", 100.0)
        self.assertEqual(
            core.scope.get_self("_last_signal_spoke_at", 0.0),
            0.0,
            "取文本失败不能把这一次「她主动开口」记成已处理",
        )

    def test_the_next_round_still_records_the_same_event(self):
        core = make_core()
        core.signals.last_proactive_text = lambda role_id="": "晚安"
        core.mood.readable = lambda *a, **k: True
        spoke: list[tuple] = []
        core.mood.note_spoke = lambda uid, text, at: spoke.append((uid, text, at))
        core._note_social_outgoing("u1", 100.0)
        self.assertEqual(core.scope.get_self("_last_signal_spoke_at", 0.0), 100.0)
        self.assertEqual(spoke, [("u1", "晚安", 100.0)])

    def test_the_same_signal_is_not_recorded_twice(self):
        core = make_core()
        core.signals.last_proactive_text = lambda role_id="": "晚安"
        core.mood.readable = lambda *a, **k: True
        spoke: list[tuple] = []
        core.mood.note_spoke = lambda uid, text, at: spoke.append((uid, text, at))
        core._note_social_outgoing("u1", 100.0)
        core._note_social_outgoing("u1", 100.0)
        self.assertEqual(len(spoke), 1, "同一个 at 只记一次")

    def test_an_old_plugin_without_text_still_counts_as_handled(self):
        core = make_core()
        core.signals.last_proactive_text = lambda role_id="": ""
        core._note_social_outgoing("u1", 100.0)
        self.assertEqual(
            core.scope.get_self("_last_signal_spoke_at", 0.0),
            100.0,
            "旧版社交插件永远没有文本：不写 spoke_log，但也不能每轮重试",
        )


class CcbSettleDocTest(unittest.TestCase):
    """#165：docstring 与实现一致，且实现只动 libido。"""

    def test_the_docstring_does_not_promise_a_second_thing(self):
        doc = HumanoidCoreInstance.ccb_settle.__doc__ or ""
        self.assertNotIn("这里做两件事", doc)
        self.assertIn("只做一件事", doc)
        self.assertIn("`peak` 不在这里记", doc)

    def test_it_only_drops_the_libido(self):
        core = make_core()
        seen: list[tuple] = []
        core.mood.note_refractory = lambda uid, drop: seen.append((uid, drop))
        core.ccb_settle("u1", 123.0)
        self.assertEqual(seen, [("u1", 12.0)])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
