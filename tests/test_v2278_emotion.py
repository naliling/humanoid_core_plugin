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
from humanoid.emotion import EmotionLayer
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

    def test_cold_passes_the_negative_gates(self):
        """冷淡只豁免「同类递减」与「连续才扣好感」，其余负向闸门照过。

        原来 `if aff >= 0 or neg_key == "cold": return delta` 让它把单条封顶、
        连击门槛、每日封顶**一起**绕开：实测单日 200 句「嗯」把好感从 60 打到 0，
        而正向一天最多 +6.5。v2.28.9 修掉了这条 P0。
        """
        svc, _clock = _svc()
        record = svc.profile("1")
        # 单条封顶生效（原来 -9.0 会原样返回）
        got = svc._throttle_negative(Delta(-9.0, 0.0, 0.0), record, "cold")
        self.assertLess(got.affection, 0.0)
        self.assertGreaterEqual(got.affection, -M.NEGATIVE_MESSAGE_CAP - 1e-6)
        # 每日净损额度生效（这条路径以前一分钱都不进任何闸门）
        self.assertIsInstance(record.get("loss_day"), dict)


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

    def test_apology_bonus_respects_the_cap_and_is_counted(self):
        """恢复侧也必须过三道闸：单句不超过面板 cap，且计入当日净增额度。

        原来它跑在 `_apply_modifiers` **之后**，等于绕开单句封顶、同类递减与每日封顶：
        道歉一次实测能加到 4.7（远超这里声明的 2.4），而且一分钱不进 `warm_day`——
        于是「每日净增封顶 6.5」当天还以为自己有全额余额。
        """
        cap = 2
        svc, clock = _svc(
            mood_initial_affection=60, mood_sensitivity=100, mood_affection_delta_cap=cap
        )
        svc.profile("1")
        for _ in range(6):
            asyncio.run(svc.update_from_message_async("1", "嗯"))
            clock.advance(10)
        hurt = svc.profile("1")["affection"]
        self.assertLess(hurt, 60.0, "先得真的低于基线")
        delta = asyncio.run(svc.update_from_message_async("1", "对不起，我错了"))
        self.assertLessEqual(
            abs(delta.affection), float(cap) + 1e-9, "道歉加成绕过了单句 cap"
        )
        self.assertLessEqual(
            abs(svc.profile("1")["affection"] - hurt), float(cap) + 1e-9
        )
        day = svc.profile("1").get("warm_day") or {}
        self.assertGreater(
            float(day.get("gain", 0.0) or 0.0), 0.0, "道歉的加成没有计入当日额度"
        )


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
    """E3/E4：双方同意推亲近欲；强迫扣好感且不进场景。

    这一组全部显式打开 `ccb`：E3/E4 是**成人档分级解锁**的功能，`ccb` 是总开关，
    而它的默认值是 false。原实现只读 `ccb_affection_min`、从不读 `ccb`，于是
    默认配置下这几条也会生效——那正是 `test_disabled_without_ccb` 现在盯的回归。
    """

    def test_disabled_without_ccb(self):
        """没开成人档时整段不生效：既不推亲近欲，也不记「刚被强迫」。"""
        svc, _clock = _svc(mood_initial_affection=60, ccb_affection_min=50, ccb=False)
        svc.profile("1")
        before = svc.profile("1")["libido"]
        asyncio.run(svc.update_from_message_async("1", "不许拒绝，我们上床吧"))
        self.assertAlmostEqual(svc.profile("1")["libido"], before, places=4)
        self.assertEqual(
            float(svc.profile("1").get("ccb_coerced_at", 0.0)), 0.0, "没开成人档不该记这一笔"
        )

    def test_consent_boosts_libido(self):
        svc, _clock = _svc(mood_initial_affection=60, ccb_affection_min=50, ccb=True)
        svc.profile("1")
        before = svc.profile("1")["libido"]
        asyncio.run(svc.update_from_message_async("1", "我们上床吧"))
        self.assertGreater(svc.profile("1")["libido"], before)

    def test_low_affection_does_not_boost(self):
        svc, _clock = _svc(mood_initial_affection=20, ccb_affection_min=50, ccb=True)
        svc.profile("1")
        before = svc.profile("1")["libido"]
        asyncio.run(svc.update_from_message_async("1", "我们上床吧"))
        self.assertAlmostEqual(svc.profile("1")["libido"], before, places=4)

    def test_consent_boost_is_capped_and_counted(self):
        """求亲密那一笔也必须过闸：单句不超过 1.2，且计入当日亲近欲额度。

        原来它跑在三道闸**之后**，等于每句白加 2.0、且一分钱不进当日额度。
        """
        svc, _clock = _svc(mood_initial_affection=60, ccb_affection_min=50, ccb=True)
        svc.profile("1")
        total = 0.0
        for _ in range(12):
            before = svc.profile("1")["libido"]
            delta = asyncio.run(svc.update_from_message_async("1", "我们上床吧"))
            total += delta.libido
            after = svc.profile("1")["libido"]
            self.assertLessEqual(delta.libido, M.LIBIDO_MESSAGE_CAP + 1e-9, "单句亲近欲超了上限")
            self.assertLessEqual(abs(after - before), M.LIBIDO_MESSAGE_CAP + 1e-9)
        self.assertLessEqual(total, M.DAILY_LIBIDO_GAIN_CAP + 1e-9, "当日亲近欲额度被刷穿")

    def test_coercion_drops_affection_and_marks(self):
        svc, _clock = _svc(mood_initial_affection=60, ccb_affection_min=50, ccb=True)
        svc.profile("1")
        start = svc.profile("1")["affection"]
        asyncio.run(svc.update_from_message_async("1", "不许拒绝，我们上床吧"))
        self.assertLess(svc.profile("1")["affection"], start, "强迫该扣好感")
        self.assertGreater(float(svc.profile("1").get("ccb_coerced_at", 0.0)), 0.0)

    def test_coercion_deducts_even_on_first_offense(self):
        """强迫**不被「连续才扣好感」的门槛归零**：第一句就要扣。

        `mood_negative_streak_required` 默认 2，那一关是给「一句孤立的难听话」留的
        缓冲；强迫若也被它归零，E4 就等于从来没生效过。
        """
        svc, _clock = _svc(mood_initial_affection=60, ccb_affection_min=50, ccb=True)
        svc.profile("1")
        start = svc.profile("1")["affection"]
        asyncio.run(svc.update_from_message_async("1", "不许拒绝，我们上床吧"))
        self.assertLess(
            svc.profile("1")["affection"], start - 0.1, "第一句强迫就该实扣好感"
        )


class NegStreakTest(unittest.TestCase):
    """P1：`neg_streak` 被任意中性消息清零 → 「连续才扣好感」几乎不触发。"""

    def test_neutral_message_does_not_break_the_streak(self):
        """中性闲聊不该打断连击。

        原来 `_apply_modifiers` 对**每条**非负向消息都清零 `neg_streak`，而中性闲聊
        返回的是 `Delta(0, 0, 0)` —— 于是「骂一句 → 说句'在吗' → 再骂一句」永远凑不满
        门槛（默认 2），好感在骂人路径上**恒不减**。
        """
        svc, _clock = _svc(mood_initial_affection=60)
        svc.profile("1")
        start = svc.profile("1")["affection"]
        asyncio.run(svc.update_from_message_async("1", "你真烦"))
        asyncio.run(svc.update_from_message_async("1", "在吗"))
        asyncio.run(svc.update_from_message_async("1", "你真烦"))
        self.assertLess(
            svc.profile("1")["affection"], start, "中性消息把连击打断了，第二句骂人没扣好感"
        )

    def test_praise_still_breaks_the_streak(self):
        """真的在说好话才清零——好话确实代表这一段翻篇了。"""
        svc, _clock = _svc(mood_initial_affection=60)
        svc.profile("1")
        start = svc.profile("1")["affection"]
        asyncio.run(svc.update_from_message_async("1", "你真烦"))
        asyncio.run(svc.update_from_message_async("1", "谢谢你，你真好"))
        asyncio.run(svc.update_from_message_async("1", "你真烦"))
        self.assertGreaterEqual(
            svc.profile("1")["affection"], start, "中间的夸奖没把连击清零"
        )

    def test_streak_expires_after_the_window(self):
        """隔了半天还叫「连续」就太苛刻了——那是两件独立的事。"""
        svc, clock = _svc(mood_initial_affection=60)
        svc.profile("1")
        asyncio.run(svc.update_from_message_async("1", "你真烦"))
        clock.advance(int(M.NEG_STREAK_WINDOW_SECONDS / 60) + 5)
        before = svc.profile("1")["affection"]
        asyncio.run(svc.update_from_message_async("1", "你真烦"))
        self.assertAlmostEqual(
            svc.profile("1")["affection"], before, places=4,
            msg="超过连击时间窗的第二句应当只算新的第一句",
        )


class DecayRobustness(unittest.TestCase):
    """P1-3：坏 mood 记录不许让这个用户此后**每条消息**的记账都静默失效。

    `decay_user` 是 `on_message` 里最早碰 mood 记录的一步，而唯一的修复路径
    `_repair` 只在 `profile()` 里跑——也就是在崩溃点之后。所以坏数据必须在这里
    就地修好（非 dict 就清掉，让 `profile()` 按初始值重建）。
    """

    def test_non_dict_mood_is_healed(self):
        svc, _clock = _svc(mood_initial_affection=60)
        svc._scope.user_state("1")["mood"] = ["坏", "数据"]
        self.assertFalse(svc.decay_user("1"), "没有可衰减的记录")
        self.assertEqual(svc.profile("1")["affection"], 60.0, "应按初始值重建")

    def test_string_axis_does_not_raise(self):
        svc, _clock = _svc(mood_initial_affection=60)
        record = svc.profile("1")
        record["affection"] = "不是数字"
        record["last_decay"] = 0.0
        svc.decay_user("1")  # 不许抛
        value = svc.profile("1")["affection"]
        self.assertIsInstance(value, float)
        self.assertTrue(0.0 <= value <= 100.0, f"修复后仍在界内，实际 {value!r}")

    def test_nan_is_not_clamped_to_full_marks(self):
        """NaN 不许被 `min/max` 静默夹成满分。

        Python 里 `min(100.0, nan)` 返回 100.0 —— 一个 NaN 好感会变成满分。
        """
        svc, _clock = _svc(mood_initial_affection=60)
        record = svc.profile("1")
        record["affection"] = float("nan")
        record["last_decay"] = 0.0
        svc.decay_user("1")
        self.assertNotEqual(svc.profile("1")["affection"], 100.0, "NaN 被夹成了满分")


class _EmotionCore:
    """`EmotionLayer._today_events` 只用到 `core.clock.today_str` 与 `core.mood.logs`。"""

    def __init__(self, svc, clock):
        self.clock = clock
        self.mood = svc


# 日上限作用在**进账本之前**的 delta 上，而账本对负向另有 ×1.2 的放大
# （`LEDGER_NEGATIVE_AMPLIFY`）。所以实际好感的单日跌幅上限是 6.5 × 1.2 = 7.8。
_LOSS_CEILING = M.DAILY_NET_LOSS_CAP * M.LEDGER_NEGATIVE_AMPLIFY


class ColdAndSilenceDailyCap(unittest.TestCase):
    """P0（#2）：冷淡与「被冷落」并入负向闸门，并受每日净损封顶约束。"""

    def test_a_day_of_cold_cannot_zero_affection(self):
        svc, clock = _svc(mood_initial_affection=60)
        svc.profile("1")
        for _ in range(200):
            asyncio.run(svc.update_from_message_async("1", "嗯"))
            clock.advance(1)  # 09:00 起 200 分钟，仍在同一个自然日里
        record = svc.profile("1")
        drop = 60.0 - record["affection"]
        self.assertLessEqual(drop, _LOSS_CEILING + 1e-6,
                             f"单日 200 句冷淡掉了 {drop}，超过日上限 {_LOSS_CEILING}")
        self.assertGreater(record["affection"], 0.0, "好感被一天打到了 0")
        self.assertIsInstance(record.get("loss_day"), dict,
                              "冷淡仍然绕开负向闸门（当日额度没被记账）")

    def test_ignored_by_peer_has_a_daily_cap(self):
        svc, _clock = _svc(mood_initial_affection=60)
        svc.profile("1")
        for streak in range(1, 61):
            svc.note_ignored_by_peer("1", streak)
        drop = 60.0 - svc.profile("1")["affection"]
        self.assertLessEqual(drop, _LOSS_CEILING + 1e-6,
                             f"单日 60 次冷落掉了 {drop}，超过日上限 {_LOSS_CEILING}")
        self.assertGreater(svc.profile("1")["affection"], 0.0)

    def test_the_budget_comes_back_next_day(self):
        svc, clock = _svc(mood_initial_affection=60)
        svc.profile("1")
        for streak in range(1, 61):
            svc.note_ignored_by_peer("1", streak)
        today = svc.profile("1")["affection"]
        clock.advance(24 * 60)
        svc.note_ignored_by_peer("1", 61)
        self.assertLess(svc.profile("1")["affection"], today, "第二天额度没有恢复")


class MoodLogNotStarved(unittest.TestCase):
    """P0（#1/#3）：真实链路必须写得进 mood_logs，且方向跟着本条消息走。"""

    def _chat(self, svc, clock, n=10):
        pool = ["谢谢你", "你真好", "抱抱", "我喜欢你", "你好可爱"]
        for i in range(n):
            asyncio.run(svc.update_from_message_async("1", pool[i % len(pool)]))
            clock.advance(6)

    def test_real_chain_writes_logs(self):
        svc, clock = _svc(mood_initial_affection=35)
        self._chat(svc, clock)
        logs = svc.logs("1")
        self.assertTrue(logs, "真实链路一条情绪日志都没写出来（emotion 的今日心境被饿死）")
        self.assertTrue(any("上升" in entry["event"] for entry in logs), logs)

    def test_emotion_layer_reads_nonzero_events(self):
        svc, clock = _svc(mood_initial_affection=35)
        self._chat(svc, clock)
        ups, downs = EmotionLayer(_EmotionCore(svc, clock))._today_events("1")
        self.assertGreater(ups, 0, "emotion 读到的正向事件仍是 0")

    def test_direction_follows_this_message_delta(self):
        """方向只看本次 `delta`：账本时衰不许把好话写成「好感度下降」。"""
        svc, _clock = _svc(mood_initial_affection=60)
        record = svc.profile("1")
        record["affection"] = 13.74  # 展示值（含时衰的合成变化此时是负的）
        svc._log_event("1", record, Delta(0.9, 0.0, 0.0))
        events = [entry["event"] for entry in svc.logs("1")]
        self.assertTrue(events, "好话没写出日志")
        self.assertIn("上升", events[-1], f"好话被写成 {events[-1]}")
        self.assertNotIn("下降", events[-1])


def _log_core(today, logs):
    """只给 `_today_events` 用的假 core（它只要 `clock.today_str` 与 `mood.logs`）。"""
    core = type("C", (), {})()
    core.clock = type("K", (), {"today_str": staticmethod(lambda: today)})()
    core.mood = type("M", (), {"logs": staticmethod(lambda uid, limit=0: logs)})()
    return core


class TodayEventsPolarity(unittest.TestCase):
    """P0（#3）：事件的「上升/下降」要按轴判正负，不能按字面猜。"""

    def test_aggression_rise_is_negative(self):
        """「攻击性上升」= 她更生气了，不能被算成正向事件。"""
        layer = EmotionLayer(_log_core("2026-08-22", [
            {"time": "2026-08-22 10:00:00", "event": "攻击性上升至 40.0"},
            {"time": "2026-08-22 10:05:00", "event": "攻击性上升至 44.0"},
        ]))
        self.assertEqual(layer._today_events("1"), (0, 2))
        self.assertIn("不太顺", layer._day_tone("1", 0)[1])

    def test_a_warm_day_is_not_read_as_a_grudge(self):
        """「攻击性下降」是气消了（正向）——不然好好聊一天会被说成「还窝火」。"""
        layer = EmotionLayer(_log_core("2026-08-22", [
            {"time": "2026-08-22 10:00:00", "event": "好感度上升至 36.2"},
            {"time": "2026-08-22 10:05:00", "event": "亲近欲上升至 34.8，攻击性下降至 27.1"},
        ]))
        ups, downs = layer._today_events("1")
        self.assertEqual(downs, 0, "「攻击性下降」被算成了负向事件")
        self.assertGreaterEqual(ups, 2)
        self.assertEqual(layer._day_tone("1", 0)[1], "今天心情不错")
        line = layer._attitude(
            {"affection": 40.0, "base_affection": 35.0,
             "aggression": 27.0, "base_aggression": 28.0},
            "1", 0,
        )[1]
        self.assertNotIn("窝火", line)
        self.assertNotIn("记着", line)

    def test_yesterday_does_not_count(self):
        layer = EmotionLayer(_log_core("2026-08-22", [
            {"time": "2026-08-21 23:59:00", "event": "好感度下降至 10.0"},
        ]))
        self.assertEqual(layer._today_events("1"), (0, 0))


class GainCapRecalibration(unittest.TestCase):
    """D2：正向日上限收到 3.1，与注释里的「ledger≈33 / 12~14 天」口径一致。"""

    def test_cap_value(self):
        self.assertAlmostEqual(M.DAILY_NET_GAIN_CAP, 3.1, places=6)

    def test_a_day_of_spam_stays_within_the_cap(self):
        svc, clock = _svc(mood_initial_affection=35)
        pool = ["喜欢你", "谢谢", "你真棒", "抱抱", "想你了", "你好可爱"]
        for i in range(100):
            asyncio.run(svc.update_from_message_async("1", pool[i % len(pool)]))
            clock.advance(2)  # 共 200 分钟，同一个自然日
        aff = svc.profile("1")["affection"]
        self.assertLessEqual(aff, 35.0 + M.DAILY_NET_GAIN_CAP + 0.01,
                             f"一天刷 100 句超出日封顶：{aff}")


class StaleShellPrune(unittest.TestCase):
    """D4：只剩昵称/初识时间的条目，90 天不活跃就整条删除。"""

    def _stale(self, svc, uid, days, **extra):
        now = svc._time()
        svc._scope.set_user(uid, "last_interaction", now - days * 86400.0)
        for key, value in extra.items():
            svc._scope.set_user(uid, key, value)

    def test_stale_shell_is_deleted(self):
        svc, _clock = _svc()
        svc._scope.set_user("ghost", "nickname", "小明")
        self._stale(svc, "ghost", 100, nickname_src="auto")
        self.assertEqual(svc.prune_expired(), 1)
        self.assertNotIn("ghost", svc._scope.all_user_ids())

    def test_recent_shell_is_kept(self):
        svc, _clock = _svc()
        svc._scope.set_user("new", "nickname", "小明")
        self._stale(svc, "new", 10)
        self.assertEqual(svc.prune_expired(), 0)
        self.assertIn("new", svc._scope.all_user_ids())

    def test_shell_with_other_state_is_kept(self):
        """有亲密计数/记忆的用户不删。"""
        svc, _clock = _svc()
        self._stale(svc, "old", 200, ccb_done_count=3.0)
        self.assertEqual(svc.prune_expired(), 0)
        self.assertIn("old", svc._scope.all_user_ids())

    def test_shell_without_any_timestamp_is_kept(self):
        """判不出时间戳的一律不删（宁可不清也不误删）。"""
        svc, _clock = _svc()
        svc._scope.set_user("unknown", "nickname", "小明")
        self.assertEqual(svc.prune_expired(), 0)
        self.assertIn("unknown", svc._scope.all_user_ids())

    def test_pruned_record_leaves_the_timestamp_for_later(self):
        svc, _clock = _svc()
        svc.profile("7")
        svc._scope.set_user("7", "nickname", "阿哲")
        # 10 天：情绪档案（7 天窗口）该清掉，但记录还在（整条删除的窗口默认 30 天）
        self._stale(svc, "7", 10)
        self.assertEqual(svc.prune_expired(), 1, "情绪档案该被清掉")
        state = svc._scope.user_state("7")
        self.assertNotIn("mood", state)
        self.assertIsNotNone(state.get("last_interaction"), "没留下下次判定的时间戳")
        self.assertEqual(svc.prune_expired(), 0, "还没到整条删除的窗口")
        self.assertEqual(
            svc.prune_expired(now=svc._time() + 31 * 86400.0), 1, "过了窗口该整条删除"
        )
        self.assertNotIn("7", svc._scope.all_user_ids())

    def test_retention_window_is_configurable(self):
        """整条删除的静默期现在是配置项（v2.27.38）。原来写死 90 天，面板上改不动——
        一个 bot 攒到 7000+ 用户、state.json 2.5 MB 就是这么来的。"""
        svc, _clock = _svc(stale_user_retention_days=5)
        self._stale(svc, "x", 6, nickname="小明")
        self.assertEqual(svc.prune_expired(), 1, "窗口 5 天时，6 天前的空壳该被删")

        svc2, _clock2 = _svc(stale_user_retention_days=5)
        self._stale(svc2, "y", 4, nickname="小明")
        self.assertEqual(svc2.prune_expired(), 0, "窗口内不该删")


class IgnoredSeenRetention(unittest.TestCase):
    """#8：`ignored_seen` 必须跟着清档一起清，否则冷落惩罚永久免罚。"""

    def test_field_is_dropped_by_prune(self):
        self.assertIn("ignored_seen", M.RETENTION_DROPPED_FIELDS)
        svc, _clock = _svc()
        svc.profile("1")
        svc._scope.set_user("1", "ignored_seen", 9.0)
        svc._scope.set_user("1", "last_interaction", svc._time() - 30 * 86400.0)
        self.assertGreater(svc.prune_expired(), 0)
        self.assertIsNone(svc._scope.user_state("1").get("ignored_seen"))

    def test_reset_clears_the_counter(self):
        svc, _clock = _svc()
        svc.profile("1")
        svc._scope.set_user("1", "ignored_seen", 5.0)
        svc.reset("1")
        self.assertIsNone(svc._scope.user_state("1").get("ignored_seen"))


class ReadOnlyEntryPoints(unittest.TestCase):
    """#7：只读入口不许给陌生人建永久空壳条目。"""

    def test_readers_do_not_create_entries(self):
        svc, _clock = _svc()
        before = svc._scope.all_user_ids()
        for i in range(5):
            uid = f"stranger{i}"
            self.assertFalse(svc.has_profile(uid))
            self.assertEqual(svc.cooling_note(uid), "")
            self.assertEqual(svc.current_tone(uid), "")
            self.assertEqual(svc.style_tendency(uid), "")
            self.assertEqual(svc.tag(uid), "")
            self.assertEqual(svc.logs(uid), [])
            self.assertEqual(svc.recent_events(uid), [])
            self.assertIsNone(svc.last_emotional_event(uid))
            self.assertTrue(svc.label(uid), "初始值那一档标签不该是空的")
            self.assertTrue(svc.stable_label(uid))
            self.assertIn("好感度", svc.profile_text(uid))
            self.assertIn("亲密", svc.intimacy_summary(uid))
        self.assertEqual(svc._scope.all_user_ids(), before, "只读入口建了用户条目")


class MoodTagIsStable(unittest.TestCase):
    """#6：标签抽签要有种子——状态没变就不该换说法、不该反复写盘。"""

    def test_same_seed_same_tag(self):
        from humanoid.data.mood_map import generate_mood_tag

        args = (55.0, 20.0, 10.0, 80.0)
        first = generate_mood_tag(*args, seed=("bot1", "2026-08-22"))
        for _ in range(50):
            self.assertEqual(
                generate_mood_tag(*args, seed=("bot1", "2026-08-22")), first,
                "同一个 (角色, 日期, 档位) 抽到了不同说法",
            )

    def test_seed_parts_change_the_draw(self):
        from humanoid.data.mood_map import generate_mood_tag

        args = (55.0, 20.0, 10.0, 80.0)
        tags = {
            generate_mood_tag(*args, seed=("bot1", f"2026-08-{day:02d}"))
            for day in range(1, 29)
        }
        self.assertGreater(len(tags), 1, "不同日期抽到的说法完全一样，种子没起作用")

    def test_refresh_tag_is_idempotent(self):
        svc, _clock = _svc()
        record = svc.profile("1")
        svc._refresh_tag("1", record)
        first = svc.tag("1")
        self.assertTrue(first, "心情标签没生成")
        for _ in range(20):
            svc._refresh_tag("1", record)
        self.assertEqual(svc.tag("1"), first, "状态没变，标签却被重抽了")


if __name__ == "__main__":
    unittest.main()
