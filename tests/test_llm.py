"""Provider 解析与回退链。"""

from __future__ import annotations

import asyncio
import unittest

from humanoid.config import HumanoidConfig
from humanoid.llm import (
    GLOBAL_LABEL,
    OUTCOME_COOLDOWN,
    OUTCOME_EMPTY,
    OUTCOME_ERROR,
    OUTCOME_NO_CANDIDATE,
    OUTCOME_NOT_FOUND,
    OUTCOME_TIMEOUT,
    LLMGateway,
    ProviderResolver,
    extract_text,
)

from .fakes import (
    FakeChainResponse,
    FakeClock,
    FakeContext,
    FakeLegacyContext,
    FakeNonChatProvider,
    FakeProvider,
    RecordingLogger,
)


def cfg(**overrides) -> HumanoidConfig:
    return HumanoidConfig.from_raw(overrides)


class ResolverTest(unittest.TestCase):
    def test_resolves_by_id_using_real_astrbot_api(self):
        chat = FakeProvider("deepseek_v3")
        resolver = ProviderResolver(FakeContext(chat_providers=[chat]))
        self.assertIs(resolver.resolve("deepseek_v3"), chat)

    def test_falls_back_to_scanning_all_providers(self):
        chat = FakeProvider("gemini_flash")
        ctx = FakeContext(chat_providers=[chat])
        # get_provider_by_id 抛异常时仍应通过 get_all_providers 命中
        ctx.raise_on_get_by_id = True
        resolver = ProviderResolver(ctx)
        self.assertIs(resolver.resolve("gemini_flash"), chat)

    def test_case_insensitive_courtesy_match(self):
        chat = FakeProvider("OpenAI_Default")
        resolver = ProviderResolver(FakeContext(chat_providers=[chat]))
        self.assertIs(resolver.resolve("openai_default"), chat)

    def test_rejects_non_chat_provider(self):
        tts = FakeNonChatProvider("my_tts")
        resolver = ProviderResolver(FakeContext(other_providers=[tts]))
        self.assertIsNone(resolver.resolve("my_tts"))

    def test_missing_id_returns_none_and_lists_available(self):
        resolver = ProviderResolver(
            FakeContext(chat_providers=[FakeProvider("a"), FakeProvider("b")])
        )
        self.assertIsNone(resolver.resolve("does-not-exist"))
        self.assertEqual(resolver.available_ids(), ["a", "b"])

    def test_blank_id_is_not_resolved(self):
        resolver = ProviderResolver(FakeContext(chat_providers=[FakeProvider("a")]))
        self.assertIsNone(resolver.resolve(""))
        self.assertIsNone(resolver.resolve("   "))

    def test_survives_context_without_provider_apis(self):
        resolver = ProviderResolver(FakeLegacyContext())
        self.assertIsNone(resolver.resolve("anything"))
        self.assertIsNone(resolver.resolve_global("umo"))
        self.assertEqual(resolver.available_ids(), [])

    def test_global_provider_receives_umo(self):
        glob = FakeProvider("global_one")
        ctx = FakeContext(global_provider=glob)
        resolver = ProviderResolver(ctx)
        self.assertIs(resolver.resolve_global("aiocqhttp:private:123"), glob)
        self.assertEqual(ctx.using_calls[0], ("aiocqhttp:private:123",))

    def test_global_provider_rejects_non_chat(self):
        ctx = FakeContext(global_provider=FakeNonChatProvider("tts"))
        self.assertIsNone(ProviderResolver(ctx).resolve_global())

    def test_available_ids_tolerates_errors(self):
        ctx = FakeContext(chat_providers=[FakeProvider("a")], raise_on_get_all=True)
        self.assertEqual(ProviderResolver(ctx).available_ids(), [])


class ExtractTextTest(unittest.TestCase):
    def test_prefers_completion_text(self):
        self.assertEqual(extract_text(FakeProvider("x") and _resp("hello")), "hello")

    def test_falls_back_to_result_chain(self):
        self.assertEqual(extract_text(FakeChainResponse("chained")), "chained")

    def test_none_is_empty(self):
        self.assertEqual(extract_text(None), "")


def _resp(text: str):
    from .fakes import FakeResponse

    return FakeResponse(text)


class RootCauseWitnessTest(unittest.TestCase):
    """钉死 Provider API 的形状：这三个名字在 AstrBot 4.x 上不存在。

    用它们做探测会静默失败，表现为「下拉框选了模型却不生效」。哪天 AstrBot 真的
    加回这些名字，这个测试会失败并提醒复查；在此之前它防止代码又写回那套探测逻辑。
    """

    LEGACY_NAMES = ("get_provider", "providers", "get_providers")
    REAL_NAMES = ("get_provider_by_id", "get_all_providers", "get_using_provider")

    def test_fake_context_mirrors_real_api_surface(self):
        ctx = FakeContext()
        for name in self.LEGACY_NAMES:
            self.assertFalse(hasattr(ctx, name), f"假 Context 不应有 {name}")
        for name in self.REAL_NAMES:
            self.assertTrue(callable(getattr(ctx, name, None)), f"假 Context 缺少 {name}")

    def test_real_astrbot_context_if_available(self):
        try:
            from astrbot.core.star.context import Context  # type: ignore
        except Exception as exc:  # pragma: no cover - 无 AstrBot 环境时跳过
            self.skipTest(f"AstrBot 不可导入：{exc}")
        for name in self.LEGACY_NAMES:
            self.assertFalse(hasattr(Context, name), f"AstrBot Context 意外出现了 {name}")
        for name in self.REAL_NAMES:
            self.assertTrue(callable(getattr(Context, name, None)), f"AstrBot Context 缺少 {name}")



class GatewayTest(unittest.IsolatedAsyncioTestCase):
    def build(self, ctx, config=None, clock=None, logger=None):
        conf = config or cfg()
        log = logger or RecordingLogger()
        resolver = ProviderResolver(ctx, log)
        gateway = LLMGateway(resolver, lambda: conf, log, clock or FakeClock())
        return gateway, log

    async def test_uses_primary_when_healthy(self):
        primary = FakeProvider("p1", reply="OK-PRIMARY")
        ctx = FakeContext(chat_providers=[primary])
        gw, _ = self.build(ctx)
        res = await gw.generate(
            prompt="hi", chain=(("首选模型", "p1"),), allow_global=False, timeout=5
        )
        self.assertTrue(res.ok)
        self.assertEqual(res.text, "OK-PRIMARY")
        self.assertEqual(res.provider_id, "p1")
        self.assertEqual(primary.calls, 1)

    async def test_primary_not_found_falls_back_to_secondary(self):
        secondary = FakeProvider("p2", reply="OK-SECONDARY")
        ctx = FakeContext(chat_providers=[secondary])
        gw, log = self.build(ctx)
        res = await gw.generate(
            prompt="hi",
            chain=(("首选模型", "typo_id"), ("备用模型", "p2")),
            allow_global=False,
            timeout=5,
        )
        self.assertTrue(res.ok)
        self.assertEqual(res.provider_id, "p2")
        self.assertEqual(res.attempts[0].outcome, OUTCOME_NOT_FOUND)
        # 失败日志必须给出可用 id，否则用户无从排查
        self.assertIn("typo_id", log.text("warning"))
        self.assertIn("p2", log.text("warning"))

    async def test_primary_timeout_falls_back_to_secondary(self):
        slow = FakeProvider("slow", delay=5.0)
        fast = FakeProvider("fast", reply="OK")
        ctx = FakeContext(chat_providers=[slow, fast])
        gw, _ = self.build(ctx)
        res = await gw.generate(
            prompt="hi",
            chain=(("首选模型", "slow"), ("备用模型", "fast")),
            allow_global=False,
            timeout=0.05,
        )
        self.assertTrue(res.ok)
        self.assertEqual(res.provider_id, "fast")
        self.assertEqual(res.attempts[0].outcome, OUTCOME_TIMEOUT)

    async def test_primary_error_falls_back_to_secondary(self):
        broken = FakeProvider("broken", error=RuntimeError("401 unauthorized"))
        fast = FakeProvider("fast", reply="OK")
        ctx = FakeContext(chat_providers=[broken, fast])
        gw, _ = self.build(ctx)
        res = await gw.generate(
            prompt="hi",
            chain=(("首选模型", "broken"), ("备用模型", "fast")),
            allow_global=False,
            timeout=5,
        )
        self.assertTrue(res.ok)
        self.assertEqual(res.attempts[0].outcome, OUTCOME_ERROR)
        self.assertIn("401", res.attempts[0].detail)

    async def test_empty_reply_counts_as_failure(self):
        empty = FakeProvider("empty", reply="   ")
        good = FakeProvider("good", reply="OK")
        ctx = FakeContext(chat_providers=[empty, good])
        gw, _ = self.build(ctx)
        res = await gw.generate(
            prompt="hi",
            chain=(("首选模型", "empty"), ("备用模型", "good")),
            allow_global=False,
            timeout=5,
        )
        self.assertTrue(res.ok)
        self.assertEqual(res.attempts[0].outcome, OUTCOME_EMPTY)

    async def test_both_fail_then_global(self):
        glob = FakeProvider("global_one", reply="OK-GLOBAL")
        ctx = FakeContext(chat_providers=[], global_provider=glob)
        gw, _ = self.build(ctx)
        res = await gw.generate(
            prompt="hi",
            chain=(("首选模型", "x"), ("备用模型", "y")),
            allow_global=True,
            timeout=5,
            umo="aiocqhttp:private:1",
        )
        self.assertTrue(res.ok)
        self.assertEqual(res.label, GLOBAL_LABEL)
        self.assertEqual(res.provider_id, "global_one")

    async def test_global_disabled_returns_failure(self):
        ctx = FakeContext(chat_providers=[], global_provider=FakeProvider("g"))
        gw, _ = self.build(ctx)
        res = await gw.generate(
            prompt="hi", chain=(("首选模型", "x"),), allow_global=False, timeout=5
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.outcome, OUTCOME_NOT_FOUND)
        self.assertEqual(len(res.attempts), 1)

    async def test_no_candidate_at_all(self):
        gw, _ = self.build(FakeContext())
        res = await gw.generate(prompt="hi", chain=(), allow_global=False, timeout=5)
        self.assertFalse(res.ok)
        self.assertEqual(res.outcome, OUTCOME_NO_CANDIDATE)

    async def test_retries_same_provider_then_gives_up(self):
        broken = FakeProvider("broken", error=RuntimeError("rate limit"))
        gw, _ = self.build(FakeContext(chat_providers=[broken]))
        res = await gw.generate(
            prompt="hi",
            chain=(("首选模型", "broken"),),
            allow_global=False,
            timeout=5,
            attempts_per_provider=3,
        )
        self.assertFalse(res.ok)
        self.assertEqual(broken.calls, 3)
        self.assertEqual(len(res.attempts), 3)

    async def test_cooldown_skips_failed_provider_then_expires(self):
        clock = FakeClock()
        broken = FakeProvider("broken", error=RuntimeError("down"))
        good = FakeProvider("good", reply="OK")
        ctx = FakeContext(chat_providers=[broken, good])
        gw, _ = self.build(ctx, cfg(schedule_provider_cooldown_minutes=30), clock)
        chain = (("首选模型", "broken"), ("备用模型", "good"))

        first = await gw.generate(prompt="a", chain=chain, allow_global=False, timeout=5)
        self.assertTrue(first.ok)
        self.assertEqual(broken.calls, 1)

        # 冷却期内：坏 provider 应被直接跳过，不再白等一轮
        second = await gw.generate(prompt="b", chain=chain, allow_global=False, timeout=5)
        self.assertTrue(second.ok)
        self.assertEqual(broken.calls, 1, "冷却期内不应再调用坏 provider")
        self.assertEqual(second.attempts[0].outcome, OUTCOME_COOLDOWN)
        self.assertIn("broken", gw.cooldowns())

        # 冷却到期后重新尝试
        clock.advance(30 * 60 + 1)
        self.assertEqual(gw.cooldowns(), {})
        third = await gw.generate(prompt="c", chain=chain, allow_global=False, timeout=5)
        self.assertTrue(third.ok)
        self.assertEqual(broken.calls, 2)

    async def test_cooldown_disabled_when_zero(self):
        broken = FakeProvider("broken", error=RuntimeError("down"))
        good = FakeProvider("good", reply="OK")
        ctx = FakeContext(chat_providers=[broken, good])
        gw, _ = self.build(ctx, cfg(schedule_provider_cooldown_minutes=0))
        chain = (("首选模型", "broken"), ("备用模型", "good"))
        await gw.generate(prompt="a", chain=chain, allow_global=False, timeout=5)
        await gw.generate(prompt="b", chain=chain, allow_global=False, timeout=5)
        self.assertEqual(broken.calls, 2)
        self.assertEqual(gw.cooldowns(), {})

    async def test_ignore_cooldown_for_manual_retry(self):
        clock = FakeClock()
        broken = FakeProvider("broken", error=RuntimeError("down"))
        gw, _ = self.build(
            FakeContext(chat_providers=[broken]), cfg(schedule_provider_cooldown_minutes=30), clock
        )
        chain = (("首选模型", "broken"),)
        await gw.generate(prompt="a", chain=chain, allow_global=False, timeout=5)
        await gw.generate(prompt="b", chain=chain, allow_global=False, timeout=5, ignore_cooldown=True)
        self.assertEqual(broken.calls, 2, "/重置日程 这类显式操作应能绕过冷却")

    async def test_success_clears_cooldown(self):
        clock = FakeClock()
        flaky = FakeProvider("flaky", error=RuntimeError("down"))
        gw, _ = self.build(
            FakeContext(chat_providers=[flaky]), cfg(schedule_provider_cooldown_minutes=30), clock
        )
        chain = (("首选模型", "flaky"),)
        await gw.generate(prompt="a", chain=chain, allow_global=False, timeout=5)
        self.assertIn("flaky", gw.cooldowns())
        flaky.error = None
        flaky.reply = "OK"
        res = await gw.generate(
            prompt="b", chain=chain, allow_global=False, timeout=5, ignore_cooldown=True
        )
        self.assertTrue(res.ok)
        self.assertEqual(gw.cooldowns(), {})

    async def test_global_failure_never_enters_cooldown(self):
        ctx = FakeContext(global_provider=FakeProvider("g", error=RuntimeError("down")))
        gw, _ = self.build(ctx, cfg(schedule_provider_cooldown_minutes=30))
        res = await gw.generate(prompt="a", chain=(), allow_global=True, timeout=5)
        self.assertFalse(res.ok)
        self.assertEqual(gw.cooldowns(), {}, "全局默认模型不该被本插件拉黑")

    async def test_last_result_recorded_for_diagnostics(self):
        ctx = FakeContext(chat_providers=[FakeProvider("p", reply="OK")])
        gw, _ = self.build(ctx)
        await gw.generate(
            prompt="a", chain=(("首选模型", "p"),), allow_global=False, timeout=5, purpose="schedule"
        )
        last = gw.last_result("schedule")
        self.assertIsNotNone(last)
        self.assertTrue(last.ok)
        self.assertIn("成功", last.summary())
        self.assertIsNone(gw.last_result("mood"))

    async def test_cancellation_propagates(self):
        slow = FakeProvider("slow", delay=10)
        gw, _ = self.build(FakeContext(chat_providers=[slow]))
        task = asyncio.create_task(
            gw.generate(prompt="a", chain=(("首选模型", "slow"),), allow_global=False, timeout=30)
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_chain_from_config_dedupes_and_skips_blanks(self):
        conf = cfg(schedule_provider_name="p1", schedule_fallback_provider_name="")
        self.assertEqual(conf.schedule_provider_ids, (("首选模型", "p1"),))
        good = FakeProvider("p1", reply="OK")
        gw, _ = self.build(FakeContext(chat_providers=[good]), conf)
        res = await gw.generate(
            prompt="a",
            chain=conf.schedule_provider_ids,
            allow_global=conf.schedule_allow_global_fallback,
            timeout=5,
        )
        self.assertTrue(res.ok)




if __name__ == "__main__":
    unittest.main()
