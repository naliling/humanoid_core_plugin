"""情绪分析要看得见「她说了什么、对方接没接」。

以前分析器**只看得见用户说的话**——于是「她说完没人接」和「她说完对方认真回了」
算成同一件事，而前者其实是件挺难受的事。Core 里唯一能看到「她说了什么」的地方是
进 LLM 之前（`on_llm_request` 的 `req.prompt`）。
"""
import asyncio
import unittest

from humanoid.config import HumanoidConfig
from humanoid.llm import LLMGateway, ProviderResolver
from humanoid.services.mood import MoodService
from tests.fakes import FakeContext, FakeProvider, FrozenClock, RecordingLogger
from tests.test_services import TZ, build_store

REPLY = '{"affection_delta":2,"libido_delta":0,"aggression_delta":0}'


def _svc(interval=10):
    conf = HumanoidConfig.from_raw({
        "mood_use_llm_for_delta": True, "mood_provider_name": "p",
        "mood_sensitivity": 100, "mood_llm_interval_messages": interval})
    st = build_store(conf)
    log = RecordingLogger()
    p = FakeProvider("p", reply=REPLY)
    gw = LLMGateway(ProviderResolver(FakeContext(chat_providers=[p]), log), lambda: conf, log)
    svc = MoodService(st.scope, lambda: conf,
                      FrozenClock(__import__("datetime").datetime(2026, 8, 22, 9, 0, tzinfo=TZ)),
                      gateway=gw, logger=log, time_source=lambda: 1_000_000.0)
    return svc, p


class SpokeLedger(unittest.TestCase):
    def test_recorded(self):
        svc, _ = _svc()
        svc.note_spoke("u1", "你今天吃饭了吗")
        self.assertEqual(len(svc.spoke("u1")), 1)
        self.assertTrue(svc.spoke("u1")[-1]["pending"])

    def test_empty_is_ignored(self):
        svc, _ = _svc()
        svc.note_spoke("u1", "   ")
        self.assertEqual(svc.spoke("u1"), [])

    def test_capped(self):
        svc, _ = _svc()
        for i in range(12):
            svc.note_spoke("u1", f"第{i}句")
        self.assertLessEqual(len(svc.spoke("u1")), MoodService.SPOKE_MAX)

    def test_user_reply_marks_it_landed(self):
        svc, _ = _svc()
        svc.note_spoke("u1", "在忙吗")
        rows = svc.close_last_spoke("u1")
        self.assertFalse(rows[-1]["pending"])
        self.assertTrue(rows[-1]["landed"])

    def test_close_on_empty_is_noop(self):
        svc, _ = _svc()
        self.assertEqual(svc.close_last_spoke("u1"), [])

    def test_close_twice_does_not_flip_back(self):
        svc, _ = _svc()
        svc.note_spoke("u1", "在吗")
        svc.close_last_spoke("u1", landed=False)
        svc.close_last_spoke("u1", landed=True)
        self.assertFalse(svc.spoke("u1")[-1]["landed"], "已结算的不该被第二次改写")


class AnalysisSeesIt(unittest.TestCase):
    def test_prompt_carries_her_own_words_and_whether_they_landed(self):
        svc, p = _svc(interval=3)
        svc.note_spoke("u1", "你今天吃饭了吗")
        svc.close_last_spoke("u1")
        svc.note_spoke("u1", "那家店还开着吗")
        for t in ("在的", "我也去过", "下次一起"):
            asyncio.run(svc.update_from_message_async("u1", t))
        prompt = str(p.last_kwargs.get("prompt", ""))
        self.assertIn("她在这之前说过的", prompt)
        self.assertIn("你今天吃饭了吗", prompt)
        self.assertIn("那家店还开着吗", prompt)
        self.assertIn("接住", prompt)

    def test_silent_when_she_never_spoke(self):
        svc, p = _svc(interval=3)
        for t in ("在的", "我也去过", "下次一起"):
            asyncio.run(svc.update_from_message_async("u1", t))
        self.assertNotIn("她在这之前说过的", str(p.last_kwargs.get("prompt", "")))


if __name__ == "__main__":
    unittest.main()
