"""v2.27.2：好感不再廉价（递减+封顶）、三处错账修复。

三条反馈各有实证：
- 「发几个指令好感就能提升两度」——/好感度 里的「好」命中夸奖词表，命令消息没过滤；
- 「重复刷同一句话不贬值」——单句上限 2 分、无递减无封顶；
- 「她说过的话」记的是用户的话——req.prompt 是用户消息，不是她的回复。

本文件盯的是这些行为在**修改后不许回退**。
"""
from __future__ import annotations

import asyncio
import random
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile

from humanoid.config import HumanoidConfig, plan_default_migrations
from humanoid.services import mood as M
from humanoid.services.mood import (
    DAILY_NET_GAIN_CAP,
    MoodService,
    REPEAT_DECAY,
)

from .fakes import RecordingLogger, ScopeStore

TZ = timezone(timedelta(hours=8))


class _Clock:
    """可推进的时钟：MoodService 只需要 now()/today_str()。"""

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
    svc = MoodService(store.scope, lambda: conf, clock, logger=RecordingLogger())
    return svc, clock


class DictionaryFixes(unittest.TestCase):
    """词典三处误判的回归锁。"""

    def test_tiredness_is_not_a_compliment(self):
        for t in ("今天好累", "累死了", "我好累啊", "烦死了", "有点emo"):
            d = M.local_delta(t)
            self.assertLessEqual(d.affection, 0.5, f"「{t}」不该被当夸奖：{d}")

    def test_apology_is_not_a_compliment(self):
        for t in ("不好意思", "不好说", "不用了", "没关系的"):
            d = M.local_delta(t)
            self.assertLessEqual(d.affection, 0.5, f"「{t}」不该被当夸奖：{d}")

    def test_intimate_words_actually_register(self):
        """「抱抱」「亲亲」以前 ≈0，现在要走亲近通道。"""
        for t in ("抱抱", "亲亲", "贴贴", "想你了"):
            d = M.local_delta(t)
            self.assertGreater(d.libido, 0.3, f"「{t}」该有亲近感：{d}")

    def test_small_talk_is_zero(self):
        for t in ("你好", "好的", "在吗", "收到了"):
            d = M.local_delta(t)
            self.assertLess(abs(d.affection), 0.01, f"「{t}」是寒暄，不该动好感：{d}")

    def test_cold_still_cold(self):
        for t in ("嗯", "哦", "随你", "算了"):
            d = M.local_delta(t)
            self.assertLessEqual(d.affection, -0.55, f"「{t}」该算冷淡：{d}")


class ThrottleAndCap(unittest.TestCase):
    """三道闸：单句封顶、同类递减、每日封顶。"""

    def test_same_line_decays(self):
        """连刷同一句话越刷越不值钱。"""
        svc, clock = _svc()
        random.seed(5)
        first = None
        last = None
        for i in range(12):
            d = asyncio.run(svc.update_from_message_async("u1", "我喜欢你"))
            if i == 0:
                first = d.affection
            last = d.affection
            clock.advance(30)
        self.assertIsNotNone(first)
        self.assertLessEqual(last, max(0.0, first * REPEAT_DECAY[-1]),
                             f"第 12 次仍拿 {last}，first={first}")

    def test_daily_cap_holds(self):
        """一天 300 条刷屏也不许超过当日封顶。"""
        svc, clock = _svc()
        pool = ["喜欢你", "谢谢", "你真棒", "抱抱", "想你了", "你好可爱"]
        for i in range(300):
            asyncio.run(svc.update_from_message_async("u1", pool[i % len(pool)]))
            clock.advance(3)
        aff = svc.profile("u1")["affection"]
        self.assertLessEqual(aff, 35.0 + DAILY_NET_GAIN_CAP + 0.01,
                             f"刷屏 300 条超出日封顶：{aff}")

    def test_normal_chat_grows(self):
        """正常聊天一天要有明显正积累（不然 12 天到 70 无从谈起）。"""
        svc, clock = _svc()
        everyday = [
            "早上好", "今天开会好累", "中午吃了面", "下午摸鱼了", "晚上好",
            "你觉得这个怎么样", "周末准备去哪玩", "谢谢你陪我聊这些",
            "你真好", "好开心", "笑死我了", "哈哈哈", "我去洗澡了",
            "回来了", "今天遇到个有意思的事", "晚安", "今天早点睡",
            "你也早点休息", "今天心情不错", "你好可爱", "不错不错", "挺好的",
            "嗯嗯", "好的", "行", "我先忙了", "晚点聊", "改天再聊",
            "抱抱", "辛苦你了", "你最好了", "今天谢谢你",
        ]
        before = svc.profile("u1")["affection"]
        for i in range(40):
            asyncio.run(svc.update_from_message_async("u1", everyday[i % len(everyday)]))
            clock.advance(6)
        after = svc.profile("u1")["affection"]
        self.assertGreater(after - before, 1.5,
                           f"正常聊天一天只涨了 {after - before:.2f}")


class InitialValueAndMigration(unittest.TestCase):
    def test_default_is_35(self):
        self.assertEqual(HumanoidConfig().mood_initial_affection, 35)

    def test_old_default_migrates(self):
        changes = plan_default_migrations({"mood_initial_affection": 46})
        self.assertEqual(changes.get("mood_initial_affection"), 35)

    def test_custom_value_is_never_touched(self):
        self.assertEqual(
            plan_default_migrations({"mood_initial_affection": 80}), {}
        )

    def test_existing_records_survive(self):
        """旧数据保留：已有档案的好感值不为迁移改变。"""
        svc, _ = _svc()
        rec = svc.profile("u1")
        rec["affection"] = 77.0
        rec["base_affection"] = 77.0
        svc._scope.user_state("u1")["mood"] = rec
        self.assertEqual(svc.profile("u1")["affection"], 77.0)


class _Event:
    def __init__(self, message, raw=""):
        self.message_str = message
        if raw:
            self.message_obj = type("M", (), {
                "message": [type("P", (), {"text": raw})()],
            })()


class CommandFilter(unittest.TestCase):
    """指令消息不进聊天记账。"""

    def test_command_detection(self):
        """判据是「命中已注册指令名 + 唤醒标志」，不是「有没有 /」。"""
        from tests.test_main_integration import main_module
        fn = main_module._is_command_message
        # 指令：唤醒前缀已被框架剪掉，只剩指令名（或带参数）
        self.assertTrue(fn("好感度"))
        self.assertTrue(fn("查看日程"))
        self.assertTrue(fn("拟人 诊断"))
        self.assertTrue(fn("叫我 小灵"))
        self.assertTrue(fn("时间 上海"))
        # 聊天：即使带唤醒前缀说普通话，也不该当指令
        self.assertFalse(fn("今天好累"))
        self.assertFalse(fn("你好"))
        self.assertFalse(fn("好感度好高呀"))   # 开头撞字但不是指令
        self.assertFalse(fn("拟人的意思是"))
        self.assertFalse(fn(""))
        # 没被唤醒的发言（群里没 @）：说「好感度」也不当指令
        self.assertFalse(fn("好感度", wake_command=False))

    def test_command_does_not_grow_affection(self):
        from tests.test_main_integration import main_module, FakeEvent
        from tests.fakes import FakeContext, FakeProvider

        async def scenario():
            tmp = tempfile.mkdtemp()
            main_module.get_astrbot_data_path = lambda: tmp
            ctx = FakeContext(chat_providers=[FakeProvider("p1", reply="[]")])
            raw = {"timezone_city": "北京", "schedule_provider_name": "p1",
                   "admin_qq": ["10001"], "state_flush_interval_seconds": 1,
                   "mood_use_llm_for_delta": False}
            star = main_module.HumanoidCore(ctx, raw)
            await star.initialize()
            core = star._core(FakeEvent("x", sender="10001"))
            random.seed(0)
            before = core.mood.profile("10001")["affection"]
            for _ in range(6):
                # 真实形态：AstrBot 在唤醒阶段已把 / 从 message_str 剪掉，
                # 交到插件手里的是「好感度」（消息链里才是原形 /好感度）。
                await star.on_message(FakeEvent("好感度", sender="10001", raw_text="/好感度"))
                await asyncio.sleep(0.05)
            after = core.mood.profile("10001")["affection"]
            rec = core.scope.user_state("10001").get("mood") or {}
            await star.terminate()
            return before, after, rec

        before, after, rec = asyncio.run(scenario())
        self.assertAlmostEqual(before, after, places=2,
                               msg=f"/好感度 指令不该涨好感：{before} → {after}")
        self.assertFalse(rec.get("llm_batch"), f"指令混进了情绪分析批：{rec.get('llm_batch')}")


class ReplyCapture(unittest.TestCase):
    """她的发言记录：两路钩子各自到位、互斥去重。"""

    class _Result:
        def __init__(self, text, ctype="LLM_RESULT"):
            self._t = text
            class CT: name = ctype
            self.result_content_type = CT()
        def get_plain_text(self):
            return self._t

    class _Resp:
        def __init__(self, text):
            self.completion_text = text

    def _run(self, scenario):
        from tests.test_main_integration import main_module, FakeEvent
        from tests.fakes import FakeContext, FakeProvider

        async def wrapped():
            tmp = tempfile.mkdtemp()
            main_module.get_astrbot_data_path = lambda: tmp
            ctx = FakeContext(chat_providers=[FakeProvider("p1", reply="[]")])
            raw = {"timezone_city": "北京", "schedule_provider_name": "p1",
                   "admin_qq": ["10001"], "state_flush_interval_seconds": 1,
                   "mood_use_llm_for_delta": False}
            star = main_module.HumanoidCore(ctx, raw)
            await star.initialize()
            try:
                return await scenario(star, FakeEvent)
            finally:
                await star.terminate()

        return asyncio.run(wrapped())

    def test_response_hook_records_and_decorating_does_not_dup(self):
        async def scenario(star, FakeEvent):
            ev = FakeEvent("x", sender="90009")
            core = star._core(ev)
            await star.note_her_reply(ev, self._Resp("今晚我做饭了。"))
            ev.get_result = lambda: self._Result("（前缀）今晚我做饭了。")
            await star.capture_reply_on_send(ev)
            spoke = core.mood.spoke("90009")
            self.assertEqual(len(spoke), 1, f"同一回复被记了多条：{spoke}")

        self._run(scenario)

    def test_decorating_fallback_for_third_party_runner(self):
        """没有 on_llm_response 时（第三方 runner），发送前兜底必须记上。"""
        async def scenario(star, FakeEvent):
            ev = FakeEvent("x", sender="90010")
            core = star._core(ev)
            ev.get_result = lambda: self._Result("在的，刚收拾完厨房。")
            await star.capture_reply_on_send(ev)
            spoke = core.mood.spoke("90010")
            self.assertEqual(len(spoke), 1, f"兜底没记上：{spoke}")
            self.assertEqual(spoke[0]["text"], "在的，刚收拾完厨房。")

        self._run(scenario)

    def test_decorating_ignores_command_replies(self):
        """指令回复（GENERAL_RESULT）不是她的发言，不入库。"""
        async def scenario(star, FakeEvent):
            ev = FakeEvent("x", sender="90011")
            core = star._core(ev)
            ev.get_result = lambda: self._Result("好感度 35.0/100", ctype="GENERAL_RESULT")
            await star.capture_reply_on_send(ev)
            self.assertEqual(core.mood.spoke("90011"), [])

        self._run(scenario)


class WakeWordChat(unittest.TestCase):
    """唤醒词聊天不该被指令名单误伤（上一轮按 `/` 判的回归）。"""

    def test_wake_word_chat_still_records(self):
        from tests.test_main_integration import main_module, FakeEvent
        from tests.fakes import FakeContext, FakeProvider

        async def scenario():
            tmp = tempfile.mkdtemp()
            main_module.get_astrbot_data_path = lambda: tmp
            ctx = FakeContext(chat_providers=[FakeProvider("p1", reply="[]")])
            raw = {"timezone_city": "北京", "schedule_provider_name": "p1",
                   "admin_qq": ["10001"], "state_flush_interval_seconds": 1,
                   "mood_use_llm_for_delta": False}
            star = main_module.HumanoidCore(ctx, raw)
            await star.initialize()
            try:
                # 群里用 / 唤醒说普通聊天：message_str 已被剪成「今天好累」
                ev = FakeEvent("今天好累", sender="90012", private=False, raw_text="/今天好累")
                core = star._core(ev)
                await star.on_message(ev)
                lm = core.scope.get_user("90012", "last_message")
                self.assertTrue(lm, "唤醒词聊天被当成指令丢了：last_message 为空")
            finally:
                await star.terminate()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
