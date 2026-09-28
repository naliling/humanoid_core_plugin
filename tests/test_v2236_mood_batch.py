"""v2.23.6：情绪分析改成「攒够一批、合起来看」。

起因的反馈是：情绪只涨不减、模型感觉不会真的分析、好像只分析了那一条。

逐条核实之后：
- **「只涨不减」不成立**——衰减是朝基线回归（`current - (current-base)*ratio`），
  基线自己不动，所以涨上去不会落回原处。这条本来就对。
- **「只分析那一条」是真的**——原来 system_prompt 写死
  「只分析用户这条消息对角色的即时影响」，prompt 里也只有当条那一句。
  于是十条互不相干的片段里，模型只看最新的一句，语气和反转全丢。

这一版改的就是后一条。
"""
import json
import unittest

from humanoid.config import HumanoidConfig
from humanoid.services.mood import MoodService
from tests.fakes import FakeContext, FakeProvider, RecordingLogger
from tests.test_services import FrozenClock, TZ, build_store

from datetime import datetime

from humanoid.llm import LLMGateway, ProviderResolver

DELTA = '{"affection_delta":3,"libido_delta":0,"aggression_delta":0}'


def _last_prompt(provider) -> str:
    """FakeProvider 把本次调用的 prompt / system_prompt 都记在 last_kwargs 里。"""
    kw = getattr(provider, "last_kwargs", {}) or {}
    return str(kw.get("prompt", "")) + "\n" + str(kw.get("system_prompt", ""))


def cfg(**kw) -> HumanoidConfig:
    return HumanoidConfig.from_raw(kw)


class MoodBatchTest(unittest.IsolatedAsyncioTestCase):
    def build(self, conf=None, provider=None):
        conf = conf or cfg()
        store = build_store(conf)
        log = RecordingLogger()
        ctx = FakeContext(chat_providers=[provider] if provider else [])
        gateway = LLMGateway(ProviderResolver(ctx, log), lambda: conf, log)
        clock = FrozenClock(datetime(2026, 8, 22, 9, 0, tzinfo=TZ))
        self.time_value = 1_000_000.0
        service = MoodService(
            store.scope, lambda: conf, clock, gateway=gateway, logger=log,
            time_source=lambda: self.time_value,
        )
        return service, store, conf

    async def _feed(self, service, n, prefix="消息"):
        for i in range(n):
            await service.update_from_message("u1", f"{prefix}{i}", 80.0, 10)

    async def test_default_interval_is_ten(self):
        self.assertEqual(cfg().mood_llm_interval_messages, 10,
                         "默认应该是 10 条触发一次")

    async def test_fires_only_on_the_tenth_message(self):
        provider = FakeProvider("p", reply=DELTA)
        service, _store, _ = self.build(
            cfg(mood_use_llm_for_delta=True, mood_provider_name="p", mood_sensitivity=100),
            provider)
        await self._feed(service, 9)
        self.assertEqual(provider.calls, 0, "第 9 条不该触发")
        await service.update_from_message("u1", "消息9", 80.0, 10)
        self.assertEqual(provider.calls, 1, "第 10 条该触发")

    async def test_sends_the_whole_batch_not_just_the_last(self):
        provider = FakeProvider("p", reply=DELTA)
        service, _store, _ = self.build(
            cfg(mood_use_llm_for_delta=True, mood_provider_name="p", mood_sensitivity=100),
            provider)
        await self._feed(service, 10, prefix="第")
        self.assertEqual(provider.calls, 1)
        blob = _last_prompt(provider)
        # 十条都应该在，不只是最后一条
        for i in (0, 5, 9):
            self.assertIn(f"第{i}", blob, f"第 {i} 条没进 prompt——只送了最后一条的话只有第 9 条")

    async def test_each_message_gets_its_own_boundary_tag(self):
        """批量之后更要防注入：一条都不能漏标记。"""
        provider = FakeProvider("p", reply=DELTA)
        service, _store, _ = self.build(
            cfg(mood_use_llm_for_delta=True, mood_provider_name="p", mood_sensitivity=100),
            provider)
        await self._feed(service, 10, prefix="M")
        blob = _last_prompt(provider)
        self.assertIn("<user_message", blob, "缺少边界标记")
        self.assertGreaterEqual(blob.count("<user_message"), 10, "每条都该有自己的边界")

    async def test_batch_is_capped(self):
        service, store, conf = self.build()
        service._append_llm_batch("u1", "第一条")
        for i in range(1, 30):
            batch = service._peek_llm_batch("u1", f"第{i}条")
        self.assertLessEqual(len(batch), conf.mood_llm_batch_max)

    async def test_failed_analysis_does_not_lose_the_messages(self):
        """provider 挂一次，这 10 条不能就这么没了。

        取批和清空必须分开：`_llm_delta` 是一次网络调用，中间要让出事件循环。
        先取先清的话，失败一次就丢一段，而 `messages_since_llm` 归零了也没人知道。
        """
        provider = FakeProvider("p", error=RuntimeError("provider 挂了"))
        service, _s, _c = self.build(
            cfg(mood_use_llm_for_delta=True, mood_provider_name="p", mood_sensitivity=100),
            provider)
        await self._feed(service, 10, prefix="M")
        record = service._scope.user_state("u1")["mood"]
        kept = list(record.get("llm_batch") or [])
        self.assertTrue(kept, "分析失败后这批被丢了")
        self.assertIn("M9", kept)
        # 计数归零，重新攒
        self.assertEqual(int(record.get("messages_since_llm", 0)), 0)

    async def test_successful_analysis_clears_the_batch(self):
        provider = FakeProvider("p", reply=DELTA)
        service, _s, _c = self.build(
            cfg(mood_use_llm_for_delta=True, mood_provider_name="p", mood_sensitivity=100),
            provider)
        await self._feed(service, 10, prefix="M")
        record = service._scope.user_state("u1")["mood"]
        self.assertEqual(list(record.get("llm_batch") or []), [],
                         "分析成功了还留着，下一批会重复分析")

    async def test_prompt_says_combined_not_single(self):
        provider = FakeProvider("p", reply=DELTA)
        service, _store, _ = self.build(
            cfg(mood_use_llm_for_delta=True, mood_provider_name="p", mood_sensitivity=100),
            provider)
        await self._feed(service, 10)
        blob = _last_prompt(provider)
        self.assertIn("合起来", blob, "提示词还在说只看单条")
        self.assertNotIn("只分析用户这条消息", blob)


class MoodDecayNotOneWay(unittest.TestCase):
    """「情绪只涨不减」那条要钉住：衰减是回归基线，不是单向下滑。"""

    def test_decay_pulls_back_to_base_not_to_zero(self):
        conf = cfg()
        store = build_store(conf)
        log = RecordingLogger()
        clock = FrozenClock(datetime(2026, 8, 22, 9, 0, tzinfo=TZ))
        service = MoodService(store.scope, lambda: conf, clock, logger=log)
        rec = service.profile("u1")
        base = float(rec["base_affection"])
        rec["affection"] = base + 20.0
        rec["last_decay"] = 0.0
        # 用**配置时长的一半**：走完整个时长本来就会回到基线（那是设计），
        # 这里要验的是部分衰减——回一点，但不该跌破基线。
        half = float(conf.mood_decay_hours) * 1800.0
        service._decay_record(rec, store.scope.user_state("u1"), conf, now=half)
        after = float(rec["affection"])
        self.assertGreater(after, base, "部分衰减不该把好感度拉回基线以下")
        self.assertLess(after, base + 20.0, "偏离基线的部分应该被收回去")
        self.assertEqual(float(rec["base_affection"]), base, "基线本身不该动")

    def test_full_decay_lands_exactly_on_base(self):
        conf = cfg()
        store = build_store(conf)
        service = MoodService(
            store.scope, lambda: conf,
            FrozenClock(datetime(2026, 8, 22, 9, 0, tzinfo=TZ)), logger=RecordingLogger())
        rec = service.profile("u1")
        base = float(rec["base_affection"])
        rec["affection"] = base + 30.0
        rec["last_decay"] = 0.0
        service._decay_record(
            rec, store.scope.user_state("u1"), conf,
            now=float(conf.mood_decay_hours) * 3600.0 + 60.0)
        self.assertAlmostEqual(float(rec["affection"]), base, places=3,
                               msg="走完整个时长应该正好回到基线，而不是归零")


if __name__ == "__main__":
    unittest.main()
