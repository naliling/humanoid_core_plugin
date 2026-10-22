"""v2.28.1：她自己发出去的话，Core 也要知道。

社交插件（autonomous_social）用 `context.send_message` 发主动消息——那条路
**不经过 respond 阶段、不产生消息事件**，所以 Core 的 `on_message` 永远看不到
她主动说过的话。后果有两个：

1. 她主动找过这个人，Core 却以为这人一直在沉默。用户下次开口时
   `process_interval` 算出的间隔是「从上次**用户**说话至今」，把她主动那次接触
   整段吞掉——注入里出现「你们有 N 天没说话了」，而实际几天前她才找过他；
2. `mood.spoke_log` 只记主链路回复，情绪分析看不到她主动开口这件事。

修法：给 main.py 加官方 `after_message_sent` 钩子。AstrBot 在 `event.send()` 之后、
`event.clear_result()` 之前触发它（见 `core/pipeline/respond/stage.py`），
此时 `event.get_result()` 拿得到刚发出去的内容。

三条限定各有守门测试：只收 LLM 类结果、只记私聊、不与主链路记账重复。
"""

from __future__ import annotations

import unittest
from pathlib import Path
import tempfile

from humanoid.services.mood import MoodService

from .fakes import RecordingLogger, ScopeStore
from .test_main_integration import (
    FakeEvent,
    FakeResult,
    SKIP_REASON,
    main_module,
)


@unittest.skipIf(main_module is None, SKIP_REASON)
class OutgoingSpokeTest(unittest.IsolatedAsyncioTestCase):
    """`note_her_outgoing`：把她发出去的话记进 Core。"""

    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        main_module.get_astrbot_data_path = lambda: str(self.tmp)
        from .test_main_integration import FakeContext

        self.star = main_module.HumanoidCore(FakeContext(), {
            "timezone_city": "北京",
            "mood_enabled": True,
            "state_flush_interval_seconds": 1,
        })
        await self.star.initialize()

    async def asyncTearDown(self) -> None:
        await self.star.terminate()

    def _event(self, text: str, *, private: bool = True,
               content_type: str = "LLM_RESULT") -> FakeEvent:
        ev = FakeEvent(" whatever ", private=private)
        ev._result = FakeResult(text, content_type=content_type)
        ev.get_result = lambda: ev._result
        return ev

    def _core(self):
        """这个 Star 实例对应的角色 Core（`_self_id(event)` 要一个事件对象）。"""
        return self.star.role_manager.get_or_create(self.star._self_id(FakeEvent("x")))

    def _spoke(self, user_id: str = "10001"):
        return self._core().mood.spoke(user_id)

    async def test_her_outgoing_is_recorded(self) -> None:
        before = self._spoke()
        await self.star.note_her_outgoing(self._event("刚下班，今天累死了"))
        after = self._spoke()
        self.assertEqual(len(after), len(before) + 1, "她发出去的话没被记进 spoke_log")
        self.assertEqual(after[-1]["text"], "刚下班，今天累死了")

    async def test_it_advances_last_interaction(self) -> None:
        """不推进这个时间戳，用户下次开口会被算成「你们有 N 天没说话了」。"""
        core = self._core()
        core.scope.set_user("10001", "last_interaction", 1000.0)
        await self.star.note_her_outgoing(self._event("在吗"))
        stamp = float(core.scope.get_user("10001", "last_interaction", 0) or 0)
        self.assertGreater(stamp, 1000.0, "她主动说过话之后，间隔还是从用户上次说话算起")

    async def test_command_echo_is_not_her_speech(self) -> None:
        """指令回显、报错回显不是她的话：结果类型不对就不记。

        指令回显的 content_type 不是 LLM_RESULT / STREAMING_FINISH。
        """
        ev = self._event("权限不足", content_type="COMMAND_RESULT")
        before = self._spoke()
        await self.star.note_her_outgoing(ev)
        self.assertEqual(self._spoke(), before, "非 LLM 结果被当成了她的话")

    async def test_group_message_is_not_recorded(self) -> None:
        """群里的一句话可能是回复任何一个人的，按发话人记会串。"""
        before = self._spoke()
        await self.star.note_her_outgoing(self._event("大家早上好", private=False))
        self.assertEqual(self._spoke(), before, "群聊发言被记进了这个人的 spoke_log")

    async def test_empty_result_is_ignored(self) -> None:
        ev = self._event("")
        before = self._spoke()
        await self.star.note_her_outgoing(ev)
        self.assertEqual(self._spoke(), before)

    async def test_duplicate_within_window_is_deduped(self) -> None:
        """与 note_her_reply / capture_reply_on_send 共用 note_spoke 的短窗去重。"""
        ev = self._event("同一句话")
        await self.star.note_her_outgoing(ev)
        first = len(self._spoke())
        await self.star.note_her_outgoing(self._event("同一句话"))
        self.assertEqual(len(self._spoke()), first, "同一句话被记了两遍")


@unittest.skipIf(main_module is None, SKIP_REASON)
class SpokeLogShapeTest(unittest.TestCase):
    """`spoke_log` 的读写形状（供上面的用例断言）。"""

    def test_note_spoke_and_read_back(self) -> None:
        store = ScopeStore(Path(tempfile.mkdtemp()) / "s.json")
        svc = MoodService(store.scope, lambda: None, None, logger=RecordingLogger())
        svc.note_spoke("u1", "第一句")
        svc.note_spoke("u1", "第二句")
        rows = svc.spoke("u1")
        self.assertEqual([r["text"] for r in rows], ["第一句", "第二句"])
        self.assertTrue(all(r.get("pending") is True for r in rows))

    def test_old_state_without_spoke_log_is_empty(self) -> None:
        """旧存档没有 spoke_log：读出来是空列表，不许抛。"""
        store = ScopeStore(Path(tempfile.mkdtemp()) / "s.json")
        svc = MoodService(store.scope, lambda: None, None, logger=RecordingLogger())
        self.assertEqual(svc.spoke("nobody"), [])


if __name__ == "__main__":
    unittest.main()
