"""v2.27.8：负向不再比正向凶 + 冷静期 + 成人档分级解锁。

用户反馈「情绪下降特别快」，以及成人档够不着、亲密请求没分寸。这一版给负向也上了
对称的闸、补了恢复侧与冷静期，并把成人档从单一高门槛改成分级解锁。本文件盯的是
这些行为在**修改后不许回退**，外加两处易误判词面（「操心」「妈妈」「裸辞」）。
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile

from humanoid.config import HumanoidConfig
from humanoid.services import mood as M
from humanoid.services.mood import Delta, MoodService

from .fakes import RecordingLogger, ScopeStore

TZ = timezone(timedelta(hours=8))


class _Clock:
    def __init__(self, dt):
        self.dt = dt

    def now(self):
        return self.dt

    def advance(self, minutes):
        self.dt = self.dt + timedelta(minutes=minutes)

    def today_str(self):
        return self.dt.strftime("%Y-%m-%d")


def _svc(**over):
    conf = HumanoidConfig.from_raw(
        {"timezone_city": "北京", "mood_use_llm_for_delta": False, **over}
    )
    store = ScopeStore(Path(tempfile.mkdtemp()) / "s.json")
    store.load("2026-08-22", conf.cycle_length)
    clock = _Clock(datetime(2026, 8, 22, 9, 0, tzinfo=TZ))
    # `_time()` 默认是真实时钟（time.time）；接上可控时钟，时间推进才可测。
    svc = MoodService(
        store.scope, lambda: conf, clock,
        time_source=lambda: clock.now().timestamp(),
        logger=RecordingLogger(),
    )
    return svc, clock


class Tiers(unittest.TestCase):
    """A10：骂人分严重 / 明显 / 轻微，只有严重才大扣。"""

    def test_severity_order(self):
        self.assertEqual(M._neg_severity("你去死吧"), "severe")
        self.assertEqual(M._neg_severity("你真烦"), "moderate")
        self.assertEqual(M._neg_severity("我恨你"), "mild")
        self.assertEqual(M._neg_severity("今天天气不错"), "")

    def test_severe_hits_harder_than_mild(self):
        import random
        random.seed(1)
        severe = min(M.local_delta("你去死吧").affection for _ in range(30))
        random.seed(1)
        mild = max(M.local_delta("我恨你").affection for _ in range(30))
        self.assertLess(severe, mild)

    def test_no_false_positive_on_normal_words(self):
        """「操心」「妈妈」「裸辞」不是骂人、也不是求欢。"""
        for t in ("我有点操心你", "妈妈今天做了饭", "我想裸辞了"):
            self.assertEqual(M._neg_severity(t), "")
            self.assertEqual(M.local_delta(t).affection, 0.0, f"「{t}」被误判成负向")
            self.assertFalse(M._SEXUAL_REQUEST_PATTERN.search(t), f"「{t}」被误判成求欢")


class NegativeThrottle(unittest.TestCase):
    """A1/A2/A12：封顶 / 同类递减 / 一句孤立难听话不动好感。"""

    def test_single_insult_moves_mood_not_affection(self):
        svc, clock = _svc(mood_initial_affection=60)
        svc.profile("1")
        start = svc.profile("1")["affection"]
        d = asyncio.run(svc.update_from_message_async("1", "你真烦"))
        self.assertEqual(d.affection, 0.0, "一句孤立的难听话不该动好感")
        self.assertAlmostEqual(svc.profile("1")["affection"], start, places=4)
        self.assertGreater(d.aggression, 0.0, "但脾气该动")

    def test_consecutive_insults_do_drop(self):
        svc, clock = _svc(mood_initial_affection=60)
        svc.profile("1")
        start = svc.profile("1")["affection"]
        asyncio.run(svc.update_from_message_async("1", "你真烦"))
        asyncio.run(svc.update_from_message_async("1", "你真烦"))
        self.assertLess(svc.profile("1")["affection"], start, "连着说才扣好感")

    def test_repeat_word_decays(self):
        svc, _clock = _svc(mood_initial_affection=90)
        svc.profile("1")
        first = asyncio.run(svc.update_from_message_async("1", "你真烦"))
        second = asyncio.run(svc.update_from_message_async("1", "你真烦"))
        self.assertLess(second.affection, first.affection, "同一个词第二次该递减")

    def test_cap_applies_to_any_negative(self):
        """模型判出的负向没有词键（neg_key 为空）——照样要过封顶。"""
        svc, _clock = _svc()
        record = svc.profile("1")
        got = svc._throttle_negative(Delta(-5.0, 0.0, 0.0), record, "")
        self.assertGreaterEqual(got.affection, -M.NEGATIVE_MESSAGE_CAP - 1e-6)

    def test_cold_is_not_throttled(self):
        """冷淡靠长期累积显形，不该被递减削没。"""
        svc, _clock = _svc()
        record = svc.profile("1")
        cold = Delta(-0.8, 0.0, 0.0)
        self.assertIs(svc._throttle_negative(cold, record, "cold"), cold)


class IgnorePenalty(unittest.TestCase):
    """A5：被冷落单次惩罚下调、封顶。"""

    def test_single_ignore_is_bounded(self):
        svc, _clock = _svc(mood_initial_affection=70)
        svc.profile("1")
        start = svc.profile("1")["affection"]
        svc.note_ignored_by_peer("1", 1)
        drop = start - svc.profile("1")["affection"]
        self.assertGreater(drop, 0.0, "被冷落该掉好感")
        self.assertLessEqual(drop, M.IGNORE_PENALTY_CAP * M.LEDGER_NEGATIVE_AMPLIFY + 1e-6,
                             f"单次被冷落扣了 {drop}，超过封顶")

    def test_streak_is_not_counted_twice(self):
        svc, _clock = _svc(mood_initial_affection=70)
        svc.profile("1")
        svc.note_ignored_by_peer("1", 3)
        once = svc.profile("1")["affection"]
        for _ in range(20):
            svc.note_ignored_by_peer("1", 3)
        self.assertAlmostEqual(svc.profile("1")["affection"], once, places=6)


class Recovery(unittest.TestCase):
    """B1：明确道歉在低于基线时快速回温。"""

    def test_apology_recovers_below_baseline(self):
        svc, clock = _svc(mood_initial_affection=60, mood_sensitivity=100)
        svc.profile("1")
        for _ in range(6):
            asyncio.run(svc.update_from_message_async("1", "嗯"))
            clock.advance(10)
        hurt = svc.profile("1")["affection"]
        self.assertLess(hurt, 60.0, "先得真的低于基线")
        asyncio.run(svc.update_from_message_async("1", "对不起，我错了"))
        self.assertGreater(svc.profile("1")["affection"], hurt, "道歉该回温")

    def test_apology_without_hurt_does_not_inflate(self):
        """她本来就好好的，道歉不该当加分。"""
        svc, _clock = _svc(mood_initial_affection=60)
        svc.profile("1")
        start = svc.profile("1")["affection"]
        asyncio.run(svc.update_from_message_async("1", "对不起"))
        self.assertAlmostEqual(svc.profile("1")["affection"], start, places=4)


class Cooling(unittest.TestCase):
    """D：好感跌破门槛后，她带情绪冷着，并在一段时间里不主动找 TA。"""

    def test_enters_and_expires(self):
        svc, clock = _svc(cooling_affection=50)  # 基线 35 < 50 → 一开口就进
        svc.profile("1")
        asyncio.run(svc.update_from_message_async("1", "在吗"))
        rec = svc.profile("1")
        self.assertGreater(float(rec.get("cool_until", 0.0)), 0.0)
        self.assertGreater(float(rec.get("cool_no_proactive_until", 0.0)),
                           float(rec.get("cool_until", 0.0)),
                           "「不主动找」要比「语气冷淡」久")
        self.assertTrue(svc.cooling_note("1"))
        clock.advance(16)
        self.assertEqual(svc.cooling_note("1"), "", "过了语气时长就该恢复")

    def test_disabled_switch(self):
        svc, _clock = _svc(cooling_enabled=False, cooling_affection=50)
        svc.profile("1")
        asyncio.run(svc.update_from_message_async("1", "在吗"))
        self.assertFalse(svc.cooling_note("1"))


class Intimacy(unittest.TestCase):
    """E3/E4：双方同意推亲近欲；强迫扣好感且不进场景。"""

    def test_consent_boosts_libido(self):
        svc, _clock = _svc(mood_initial_affection=60, ccb_affection_min=50)
        svc.profile("1")
        before = svc.profile("1")["libido"]
        asyncio.run(svc.update_from_message_async("1", "我们上床吧"))
        self.assertGreater(svc.profile("1")["libido"], before)

    def test_low_affection_does_not_boost(self):
        svc, _clock = _svc(mood_initial_affection=20, ccb_affection_min=50)
        svc.profile("1")
        before = svc.profile("1")["libido"]
        asyncio.run(svc.update_from_message_async("1", "我们上床吧"))
        self.assertAlmostEqual(svc.profile("1")["libido"], before, places=4)

    def test_coercion_drops_affection_and_marks(self):
        svc, _clock = _svc(mood_initial_affection=60, ccb_affection_min=50)
        svc.profile("1")
        start = svc.profile("1")["affection"]
        asyncio.run(svc.update_from_message_async("1", "不许拒绝，我们上床吧"))
        self.assertLess(svc.profile("1")["affection"], start, "强迫该扣好感")
        self.assertGreater(float(svc.profile("1").get("ccb_coerced_at", 0.0)), 0.0)


if __name__ == "__main__":
    unittest.main()
