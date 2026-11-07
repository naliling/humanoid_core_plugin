"""收尾守卫（v3.1.4）：别让「晚安 → 晚安 → 晚安」变成死循环。

这些用例钉的是**判定边界**，不是实现细节：哪些话算收尾、什么时候该跳过、
跳不掉时会不会吞消息（fail-open）。判定一旦放宽，最坏的后果不是多回一句，
而是把用户一句真正的话当成收尾吞掉——所以「带问号/带请求/带附件/太长」
这几条必须永远是放行。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from humanoid import closing, diagnostics
from humanoid.prompt_builder import STATE_MARK

from .fakes import FakeContext, FakeProvider
from .test_main_integration import (
    GOOD_SCHEDULE,
    SKIP_REASON,
    FakeEvent,
    FakeProviderRequest,
    injected_text,
    main_module,
)


def _in(text: str, ts: float) -> dict:
    return {"dir": "in", "text": text, "ts": ts}


def _out(text: str, ts: float) -> dict:
    return {"dir": "out", "text": text, "ts": ts}


class NormalizeTests(unittest.TestCase):
    def test_normalize_drops_punctuation_space_and_emoji(self):
        self.assertEqual(closing.normalize("晚安！ 😴"), "晚安")

    def test_normalize_keeps_digits(self):
        # 数字是内容：「23:00 晚安」不是纯收尾（时间点本身要回）。
        self.assertEqual(closing.normalize("23:00 晚安"), "2300晚安")

    def test_normalize_removes_media_placeholder(self):
        self.assertEqual(closing.normalize("[图片]晚安"), "晚安")

    def test_has_media_text_detects_placeholder(self):
        self.assertTrue(closing.has_media_text("[图片]晚安"))
        self.assertTrue(closing.has_media_text("【语音】"))
        self.assertFalse(closing.has_media_text("晚安"))


class CategoryTests(unittest.TestCase):
    def test_constants_match_the_contract(self):
        self.assertEqual(closing.MAX_CLOSING_CHARS, 48)
        self.assertEqual(closing.MAX_BOT_CLOSING_CHARS, 120)
        self.assertEqual(closing.DEFAULT_WINDOW_SECONDS, 1800)

    def test_goodnight_is_a_closing(self):
        self.assertEqual(closing.closing_categories("晚安"), ("goodnight",))
        self.assertTrue(closing.is_closing("晚安啦"))

    def test_thanks_and_farewell_are_closings(self):
        self.assertTrue(closing.is_closing("谢谢"))
        self.assertTrue(closing.is_closing("拜拜"))

    def test_question_is_never_a_closing(self):
        # 「晚安，你明天几点起？」是要回话的，不是收尾。
        self.assertEqual(closing.closing_categories("晚安，你明天几点起？"), ())
        self.assertEqual(closing.closing_categories("晚安?"), ())

    def test_overlong_text_is_not_a_closing(self):
        self.assertEqual(closing.closing_categories("晚安" * 40), ())

    def test_plain_sentence_is_not_a_closing(self):
        self.assertEqual(closing.closing_categories("今天上班好累啊，老板又开会"), ())

    def test_empty_text_is_not_a_closing(self):
        self.assertFalse(closing.is_closing(""))
        self.assertFalse(closing.is_closing(None))


class RoleClosedTests(unittest.TestCase):
    def test_role_closed_for_goodnight(self):
        self.assertTrue(closing.role_closed("晚安，好梦"))

    def test_ack_alone_does_not_close_the_role(self):
        # 光一句「好的」是应声，不能把对方第一句晚安直接吞掉。
        self.assertFalse(closing.role_closed("好的"))

    def test_role_closed_is_false_for_question(self):
        self.assertFalse(closing.role_closed("晚安，你睡了吗？"))

    def test_role_closed_allows_filler_tail(self):
        self.assertTrue(closing.role_closed("那我也去睡啦，晚安宝贝"))

    def test_role_closed_empty(self):
        self.assertFalse(closing.role_closed(""))


class DecisionTests(unittest.TestCase):
    def test_pure_goodnight_after_her_goodnight_is_suppressed(self):
        decision = closing.closing_decision(
            current_text="晚安",
            dialogue=[_in("晚安", 990.0), _out("晚安，好梦", 1000.0)],
            at=1010.0,
        )
        self.assertTrue(decision["suppress_reply"])
        self.assertEqual(decision["action"], "silent")
        self.assertEqual(decision["reason"], "role_already_closed")
        self.assertTrue(decision["last_assistant_closing"])
        self.assertEqual(decision["window_seconds"], 1800.0)
        self.assertEqual(decision["last_reply_at"], 1000.0)

    def test_default_window_is_thirty_minutes(self):
        decision = closing.closing_decision(current_text="晚安", dialogue=[], at=1.0)
        self.assertEqual(decision["window_seconds"], closing.DEFAULT_WINDOW_SECONDS)

    def test_disabled_guard_never_suppresses(self):
        decision = closing.closing_decision(
            current_text="晚安",
            dialogue=[_out("晚安", 1000.0)],
            at=1010.0,
            enabled=False,
        )
        self.assertFalse(decision["suppress_reply"])
        self.assertEqual(decision["reason"], "disabled")

    def test_media_input_is_never_suppressed(self):
        decision = closing.closing_decision(
            current_text="晚安",
            dialogue=[_out("晚安", 1000.0)],
            at=1010.0,
            has_media=True,
        )
        self.assertFalse(decision["suppress_reply"])
        self.assertEqual(decision["reason"], "media_input")

    def test_plain_message_is_not_a_closing(self):
        decision = closing.closing_decision(
            current_text="明天记得带伞",
            dialogue=[_out("晚安", 1000.0)],
            at=1010.0,
        )
        self.assertFalse(decision["suppress_reply"])
        self.assertEqual(decision["reason"], "not_a_closing")
        self.assertEqual(decision["categories"], ())

    def test_no_role_reply_yet(self):
        decision = closing.closing_decision(current_text="晚安", dialogue=[], at=1010.0)
        self.assertEqual(decision["reason"], "no_role_reply")
        self.assertFalse(decision["suppress_reply"])

    def test_history_time_unknown_fails_open(self):
        # 时间戳缺失时必须放行：宁可多回一句，不能靠猜吞掉消息。
        decision = closing.closing_decision(
            current_text="晚安",
            dialogue=[{"dir": "out", "text": "晚安"}],
            at=1010.0,
        )
        self.assertEqual(decision["reason"], "history_time_unknown")
        self.assertFalse(decision["suppress_reply"])

    def test_history_after_input_fails_open(self):
        decision = closing.closing_decision(
            current_text="晚安",
            dialogue=[_out("晚安", 1020.0)],
            at=1010.0,
        )
        self.assertEqual(decision["reason"], "history_after_input")
        self.assertFalse(decision["suppress_reply"])

    def test_outside_window_is_not_suppressed(self):
        decision = closing.closing_decision(
            current_text="晚安",
            dialogue=[_out("晚安", 1000.0)],
            at=1000.0 + 1900.0,
        )
        self.assertEqual(decision["reason"], "outside_window")
        self.assertFalse(decision["suppress_reply"])

    def test_repeated_user_closing_is_suppressed(self):
        # 她上一句没在收尾（「我在呢」），但对方上一句已经在道别了：
        # 这一轮再回一句「晚安」就是纯复读，该静默。
        decision = closing.closing_decision(
            current_text="晚安",
            dialogue=[_in("那我先睡啦，晚安", 990.0), _out("我在呢", 1000.0)],
            at=1010.0,
        )
        self.assertTrue(decision["suppress_reply"])
        self.assertEqual(decision["reason"], "repeated_user_closing")
        self.assertTrue(decision["prior_user_closing"])

    def test_role_did_not_close(self):
        decision = closing.closing_decision(
            current_text="晚安",
            dialogue=[_in("今天好热", 990.0), _out("我在呢", 1000.0)],
            at=1010.0,
        )
        self.assertFalse(decision["suppress_reply"])
        self.assertEqual(decision["reason"], "role_did_not_close")

    def test_decision_keys_are_stable(self):
        decision = closing.closing_decision(current_text="晚安", dialogue=[], at=1.0)
        self.assertEqual(
            set(decision),
            {
                "suppress_reply",
                "action",
                "reason",
                "last_reply_at",
                "categories",
                "last_assistant_closing",
                "prior_user_closing",
                "window_seconds",
            },
        )

    def test_all_reasons_come_from_the_reference_set(self):
        allowed = {
            "not_evaluated",
            "disabled",
            "media_input",
            "not_a_closing",
            "no_role_reply",
            "history_time_unknown",
            "history_after_input",
            "outside_window",
            "role_already_closed",
            "repeated_user_closing",
            "role_did_not_close",
        }
        cases = [
            dict(current_text="晚安", dialogue=[], at=1.0),
            dict(current_text="晚安", dialogue=[], at=1.0, enabled=False),
            dict(current_text="晚安", dialogue=[_out("晚安", 1.0)], at=2.0, has_media=True),
            dict(current_text="今天好热", dialogue=[], at=1.0),
        ]
        for kwargs in cases:
            self.assertIn(
                closing.closing_decision(**kwargs)["reason"], allowed
            )


class InstructionTests(unittest.TestCase):
    def _closed(self) -> dict:
        return closing.closing_decision(
            current_text="晚安",
            dialogue=[_in("晚安", 990.0), _out("晚安，好梦", 1000.0)],
            at=1010.0,
        )

    def test_instruction_for_closed_role_mentions_the_closing(self):
        # 带图/带语音时不能硬跳过（那是内容），于是走 fail-open：不吞消息，
        # 只提醒她「已经收过尾了」。
        decision = closing.closing_decision(
            current_text="晚安",
            dialogue=[_in("晚安", 990.0), _out("晚安，好梦", 1000.0)],
            at=1010.0,
            has_media=True,
        )
        self.assertEqual(decision["reason"], "media_input")
        text = closing.closing_instruction(decision)
        self.assertIn("【对话收尾】", text)
        self.assertIn("不要再补一条同义道别", text)

    def test_instruction_is_empty_when_suppressed(self):
        # 已经决定静默时不需要提示（跳不掉时由 main.py 改成 fail-open 再取提示）。
        self.assertEqual(closing.closing_instruction(self._closed()), "")

    def test_instruction_is_empty_when_prompt_disabled(self):
        decision = closing.closing_decision(
            current_text="晚安",
            dialogue=[_in("今天好热", 990.0), _out("我在呢", 1000.0)],
            at=1010.0,
        )
        self.assertEqual(
            closing.closing_instruction(decision, prompt_enabled=False), ""
        )

    def test_instruction_for_prior_user_closing(self):
        # 这句不是收尾（带问号，是要回话的），但对方上一句是：
        # 提示她别追一句，同时照常回应这句话本身。
        decision = closing.closing_decision(
            current_text="明天几点开会？",
            dialogue=[_in("那我先睡啦，晚安", 990.0), _out("我在呢", 1000.0)],
            at=1010.0,
        )
        self.assertEqual(decision["reason"], "not_a_closing")
        self.assertTrue(decision["prior_user_closing"])
        text = closing.closing_instruction(decision)
        self.assertIn("对方上一句已经在道别或收尾", text)

    def test_instruction_is_empty_for_normal_rounds(self):
        decision = closing.closing_decision(
            current_text="明天见", dialogue=[], at=1.0
        )
        self.assertEqual(closing.closing_instruction(decision), "")


class WindowTests(unittest.TestCase):
    def test_clamp_window_bounds(self):
        self.assertEqual(closing.clamp_window(0), 60.0)
        self.assertEqual(closing.clamp_window(99999), 7200.0)
        self.assertEqual(closing.clamp_window("oops"), 1800.0)
        self.assertEqual(closing.clamp_window(None), 1800.0)
        self.assertEqual(closing.clamp_window(600), 600.0)

    def test_dialogue_window_dedupes_same_line(self):
        window = closing.DialogueWindow()
        window.record("out", "晚安", 100.0)
        window.record("out", "晚安", 130.0)  # 90 秒内同句：两条记录路径重叠
        self.assertEqual(len(window), 1)
        window.record("out", "晚安", 300.0)
        self.assertEqual(len(window), 2)

    def test_dialogue_window_is_bounded(self):
        window = closing.DialogueWindow(maxlen=3)
        for index in range(6):
            window.record("in", f"第{index}句", float(index))
        self.assertEqual(len(window), 3)
        self.assertEqual(window.entries()[-1]["text"], "第5句")

    def test_dialogue_window_ignores_empty_text(self):
        window = closing.DialogueWindow()
        window.record("in", "   ", 1.0)
        self.assertEqual(len(window), 0)

    def test_entries_returns_copies(self):
        window = closing.DialogueWindow()
        window.record("in", "晚安", 1.0)
        entries = window.entries()
        entries[0]["text"] = "被改了"
        self.assertEqual(window.entries()[0]["text"], "晚安")


def _plugin_diagnostics():
    """插件包内部的那份 diagnostics。

    测试把 main.py 以合成包名载入（见 test_main_integration），所以 main 里
    from .humanoid.diagnostics import ... 拿到的是包内实例，与测试直接
    from humanoid import diagnostics 拿到的**不是同一个模块对象**。生产里只有
    包内这一份，所以这里也必须读包内那份，否则断言会看到空缓冲。
    """
    import importlib

    if main_module is None:  # pragma: no cover - 由 skipIf 兜住
        return diagnostics
    return importlib.import_module(main_module.__package__ + ".humanoid.diagnostics")


@unittest.skipIf(main_module is None, SKIP_REASON)
class ClosingWiringTest(unittest.IsolatedAsyncioTestCase):
    """接线层：判定对了但没接上等于没做。

    这里只验四件事：能硬跳过时真的跳（stop_event 被调用、请求里什么都没有）、
    跳不掉时**不吞消息**（注入照旧 + 一句收尾提示）、窗口外正常回复、
    以及同一请求被重放时不重复记「对方说了什么」。
    """

    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.provider = FakeProvider("p1", reply=GOOD_SCHEDULE)
        self.ctx = FakeContext(chat_providers=[self.provider])
        self.raw = {
            "timezone_city": "北京",
            "schedule_provider_name": "p1",
            "admin_qq": ["10001"],
            "state_flush_interval_seconds": 1,
        }
        main_module.get_astrbot_data_path = lambda: str(self.tmp)
        self.star = main_module.HumanoidCore(self.ctx, self.raw)
        await self.star.initialize()
        self.diag = _plugin_diagnostics()
        self.diag.clear_reasons()

    async def asyncTearDown(self) -> None:
        await self.star.terminate()
        self.diag.clear_reasons()

    async def _open_and_reply(self, user_text: str = "晚安", reply: str = "晚安，好梦"):
        event = FakeEvent(user_text)
        await self.star.inject_context(event, FakeProviderRequest(""))
        self.star._closing_record_out(event, reply)
        return event

    async def test_her_goodnight_then_users_goodnight_is_hard_skipped(self) -> None:
        await self._open_and_reply()

        second = FakeEvent("晚安")
        req = FakeProviderRequest("")
        await self.star.inject_context(second, req)

        self.assertTrue(second.is_stopped(), "对方只是重复一句晚安，这轮不该再调模型")
        self.assertEqual(injected_text(req).strip(), "", "跳过时不该再注入任何东西")
        top = self.diag.reason_report()["top"]
        self.assertEqual(top[0]["code"], "closing_silence")
        self.assertEqual(top[0]["last_detail"], "role_already_closed")

    async def test_fail_open_when_the_host_cannot_stop_the_model(self) -> None:
        class _NoStopEvent(FakeEvent):
            stop_event = None  # 属性在、但不可调用：模拟宿主不支持硬跳过

        await self._open_and_reply()

        second = _NoStopEvent("晚安")
        req = FakeProviderRequest("")
        await self.star.inject_context(second, req)

        self.assertFalse(second.is_stopped())
        text = injected_text(req)
        self.assertIn("【对话收尾】", text, "跳不掉时要提醒她别再追一句")
        self.assertIn(STATE_MARK, text, "fail-open：注入照旧，绝不吞消息")
        self.assertEqual(self.diag.reason_report()["top"][0]["code"], "closing_prompt")

    async def test_outside_window_replies_normally(self) -> None:
        event = FakeEvent("晚安")
        await self.star.inject_context(event, FakeProviderRequest(""))
        # 把她的收尾挪到窗口外（两小时前）：早就该正常回了。
        self.star._closing_window(event).record(
            "out", "晚安，好梦", time.time() - 3600
        )

        second = FakeEvent("晚安")
        req = FakeProviderRequest("")
        await self.star.inject_context(second, req)

        self.assertFalse(second.is_stopped())
        self.assertNotIn("【对话收尾】", injected_text(req))

    async def test_media_message_is_never_swallowed(self) -> None:
        await self._open_and_reply()

        second = FakeEvent("晚安")
        second.message_obj = type(
            "MsgObj", (), {"message": [type("Image", (), {})()]}
        )()
        req = FakeProviderRequest("")
        await self.star.inject_context(second, req)

        self.assertFalse(second.is_stopped(), "发图是有内容的行为，不能当成纯收尾吞掉")
        self.assertIn("【对话收尾】", injected_text(req))

    async def test_replay_does_not_record_the_input_twice(self) -> None:
        event = FakeEvent("晚安")
        req = FakeProviderRequest("")
        await self.star.inject_context(event, req)
        window = self.star._closing_window(event)
        before = len(window)
        await self.star.inject_context(event, req)
        self.assertEqual(len(window), before, "同一请求重放又记了一遍「对方说了什么」")


if __name__ == "__main__":
    unittest.main()
