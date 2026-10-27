"""v2.28.15：异步情绪判定的世代号——reset / set_affection / 关开关要作废在飞结果。

`update_from_message_async` 是「锁内取批 → **锁外** await 模型 → 锁内结算」。中间
那一次网络调用最长可以等 `mood_update_timeout`（默认 120 秒），而这段时间里用户
完全可以执行 `/拟人 重置情绪` 或 `/设置好感度`：

- 旧 delta 回来以后会落进**重置后**的档案（好感被顶回去、设置的值被覆盖）；
- 重置前攒进 `llm_batch` 的那批消息，下一轮还会被送去分析。

修法是每条档案带一个单调递增的「判定世代」：`reset()` / `set_affection()` 推进
一格，发起请求时记下当时的世代，await 回来在锁内比对；对不上就整批作废——结果
不落账、这批消息也不放回缓冲（等待期间新 append 的照旧保留）。

这里的 provider 用 `asyncio.Event` 卡住 `text_chat`：主测试在「模型还在飞」的
窗口里做重置 / 改值 / 关开关，然后再放行，模拟真实的两条并发路径。
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime

from humanoid.config import HumanoidConfig
from humanoid.llm import LLMGateway, ProviderResolver
from humanoid.services.mood import JUDGE_GEN_KEY, MoodService
from tests.fakes import (
    FakeContext,
    FakeMeta,
    FakeResponse,
    RecordingLogger,
    ScopeStore,
)
from tests.test_services import FrozenClock, TZ, build_store

# 一次「模型判定好感 +10」——比单条消息的本地 delta 大得多，落不落账一眼看得出来。
DELTA = '{"affection_delta":10,"libido_delta":0,"aggression_delta":0}'


def cfg(**kw: object) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", **kw})


class DeferredProvider:
    """`text_chat` 停在 `release` 事件上，测试放行前它一直处于「在飞」状态。"""

    def __init__(self, provider_id: str = "p", reply: str = DELTA, error=None) -> None:
        self._id = provider_id
        self.reply = reply
        self.error = error
        self.calls = 0
        self.last_kwargs: dict[str, object] = {}
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def meta(self) -> FakeMeta:
        return FakeMeta(self._id)

    async def text_chat(self, prompt=None, **kwargs):
        self.calls += 1
        self.last_kwargs = {"prompt": prompt, **kwargs}
        self.started.set()
        await self.release.wait()
        if self.error is not None:
            raise self.error
        return FakeResponse(self.reply)


class StaleJudgmentTest(unittest.IsolatedAsyncioTestCase):
    """五种窗口：reset / set_affection / 关开关 / 正常 / 失败。"""

    def build(self, conf=None, provider=None):
        """`lambda: holder["cfg"]` 让测试可以在飞行中途换掉整份配置（配置是 frozen 的）。"""
        holder = {"cfg": conf or cfg()}
        store = build_store(holder["cfg"])
        log = RecordingLogger()
        ctx = FakeContext(chat_providers=[provider] if provider else [])
        gateway = LLMGateway(ProviderResolver(ctx, log), lambda: holder["cfg"], log)
        clock = FrozenClock(datetime(2026, 8, 22, 9, 0, tzinfo=TZ))
        self.time_value = 1_000_000.0
        service = MoodService(
            store.scope, lambda: holder["cfg"], clock,
            gateway=gateway, logger=log, time_source=lambda: self.time_value,
        )
        return service, store, holder, log

    def conf(self, **kw):
        # 每 10 条消息触发一次模型（默认），sensitivity=100 让 delta 原样落地。
        return cfg(
            mood_use_llm_for_delta=True, mood_provider_name="p",
            mood_sensitivity=100, **kw,
        )

    async def _feed(self, service, n, prefix="消息"):
        for i in range(n):
            await service.update_from_message("u1", f"{prefix}{i}", 80.0, 10)

    async def _start_inflight(self, service, provider, text="触发这条"):
        """把「第 10 条消息」跑成后台任务，等模型真的被调起来。"""
        task = asyncio.create_task(service.update_from_message_async("u1", text))
        await asyncio.wait_for(provider.started.wait(), 5)
        return task

    async def _release(self, provider, task):
        provider.release.set()
        return await asyncio.wait_for(task, 5)

    # ── a) reset 在飞 ──────────────────────────────────────────────
    async def test_reset_during_flight_discards_result_and_batch(self):
        provider = DeferredProvider()
        service, store, holder, _ = self.build(self.conf(), provider)
        await self._feed(service, 9, prefix="重置前")
        task = await self._start_inflight(service, provider, "重置前9")

        # 模型还在飞的时候用户执行了 `/拟人 重置情绪`
        service.reset("u1")

        self.assertIsNone(await self._release(provider, task),
                          "作废的结果不该再返回给调用方")
        record = store.scope.user_state("u1")["mood"]
        initial = float(holder["cfg"].mood_initial_affection)
        self.assertAlmostEqual(float(record["affection"]), initial, places=3,
                               msg="重置前发出的 delta 落回了重置后的档案")
        self.assertAlmostEqual(float(record["base_affection"]), initial, places=3)
        self.assertEqual(list(record.get("llm_batch") or []), [],
                         "重置前攒的那批消息还留着，下一轮又会被送去分析")
        self.assertEqual(int(record.get("messages_since_llm", -1)), 0)

    # ── b) set_affection 在飞 ─────────────────────────────────────
    async def test_set_affection_during_flight_is_not_overwritten(self):
        provider = DeferredProvider()
        service, store, _holder, _ = self.build(self.conf(), provider)
        await self._feed(service, 9)
        task = await self._start_inflight(service, provider)

        service.set_affection("u1", 80.0)

        self.assertIsNone(await self._release(provider, task))
        record = store.scope.user_state("u1")["mood"]
        self.assertAlmostEqual(float(record["affection"]), 80.0, places=3,
                               msg="旧 delta 把刚设置的好感覆盖了")
        self.assertAlmostEqual(float(record["base_affection"]), 80.0, places=3)
        self.assertNotIn("ledger", record, "作废的结果仍然记进了账本")

    # ── c) 关开关在飞 ─────────────────────────────────────────────
    async def test_disabling_mood_during_flight_discards_result(self):
        provider = DeferredProvider()
        service, store, holder, _ = self.build(self.conf(), provider)
        await self._feed(service, 9)
        before = float(store.scope.user_state("u1")["mood"]["affection"])
        task = await self._start_inflight(service, provider)

        # 面板上把「情绪系统」关掉（frozen 配置 → 换一份）
        holder["cfg"] = self.conf(mood_enabled=False)

        self.assertIsNone(await self._release(provider, task))
        record = store.scope.user_state("u1")["mood"]
        self.assertAlmostEqual(float(record["affection"]), before, places=3,
                               msg="开关关了，在飞的判定结果还是落了账")

    # ── d) 正常在飞（防误伤）─────────────────────────────────────
    async def test_normal_flight_still_commits(self):
        provider = DeferredProvider()
        service, store, _holder, _ = self.build(self.conf(), provider)
        await self._feed(service, 9)
        before = float(store.scope.user_state("u1")["mood"]["affection"])
        task = await self._start_inflight(service, provider)

        delta = await self._release(provider, task)

        self.assertIsNotNone(delta, "没有任何作废理由时结果必须照常返回")
        record = store.scope.user_state("u1")["mood"]
        self.assertGreater(float(record["affection"]), before + 0.5,
                           "正常路径的 delta 没落账（被误判成作废了）")
        self.assertEqual(list(record.get("llm_batch") or []), [],
                         "正常成功后这批消息应该清掉")

    # ── e) 失败在飞（回归）───────────────────────────────────────
    async def test_failure_during_flight_puts_the_batch_back(self):
        provider = DeferredProvider(error=RuntimeError("provider 挂了"))
        service, store, _holder, _ = self.build(self.conf(), provider)
        await self._feed(service, 9, prefix="M")
        task = await self._start_inflight(service, provider, "M9")

        delta = await self._release(provider, task)

        self.assertIsNotNone(delta, "模型挂了不影响本地 delta 落账")
        record = store.scope.user_state("u1")["mood"]
        kept = list(record.get("llm_batch") or [])
        self.assertIn("M0", kept, "失败后整批应该放回缓冲")
        self.assertIn("M9", kept)
        self.assertEqual(int(record.get("messages_since_llm", -1)), 0,
                         "失败后计数要归零，重新攒")

    # ── 额外：等待期间新 append 的消息不能被连坐 ─────────────────
    async def test_message_appended_while_waiting_survives_invalidation(self):
        provider = DeferredProvider()
        service, store, _holder, _ = self.build(self.conf(), provider)
        await self._feed(service, 9, prefix="旧")
        task = await self._start_inflight(service, provider, "旧9")

        service.reset("u1")
        # 重置之后、模型返回之前，对方又发了一条——这条属于「现在」。
        await service.update_from_message("u1", "新的这条", 80.0, 10)

        self.assertIsNone(await self._release(provider, task))
        record = store.scope.user_state("u1")["mood"]
        self.assertEqual(list(record.get("llm_batch") or []), ["新的这条"],
                         "作废时把等待期间新攒的消息一起丢掉了")
        self.assertEqual(int(record.get("messages_since_llm", -1)), 1)


class JudgeGenStorageTest(unittest.TestCase):
    """世代号必须能持久化，老档案缺字段按 0 读。"""

    def _service(self, store, conf):
        return MoodService(
            store.scope, lambda: conf,
            FrozenClock(datetime(2026, 8, 22, 9, 0, tzinfo=TZ)),
            logger=RecordingLogger(),
        )

    def test_old_record_without_field_counts_as_zero(self):
        conf = cfg()
        store = build_store(conf)
        service = self._service(store, conf)
        record = service.profile("u1")
        record.pop(JUDGE_GEN_KEY, None)
        self.assertEqual(service._judge_gen(record), 0, "老档案应该按 0 读")
        self.assertEqual(service._judge_gen({"judge_gen": "坏值"}), 0)
        self.assertEqual(service._judge_gen(None), 0)

    def test_reset_and_set_affection_bump_the_generation(self):
        conf = cfg()
        store = build_store(conf)
        service = self._service(store, conf)
        record = service.profile("u1")
        before = service._judge_gen(record)
        service.reset("u1")
        after_reset = service._judge_gen(store.scope.user_state("u1")["mood"])
        self.assertEqual(after_reset, before + 1)
        service.set_affection("u1", 60.0)
        after_set = service._judge_gen(store.scope.user_state("u1")["mood"])
        self.assertEqual(after_set, after_reset + 1)

    def test_generation_survives_reload(self):
        conf = cfg()
        store = build_store(conf)
        service = self._service(store, conf)
        service.reset("u1")
        gen = service._judge_gen(store.scope.user_state("u1")["mood"])
        self.assertGreaterEqual(gen, 1)
        store.flush_sync()

        reloaded = ScopeStore(store.path)
        reloaded.load("2026-08-22", conf.cycle_length)
        service2 = self._service(reloaded, conf)
        # 读一次 profile 会跑 `_repair`：它不能把这个键弄丢。
        record = service2.profile("u1")
        self.assertEqual(service2._judge_gen(record), gen,
                         "判定世代没有持久化（或 _repair 把它丢了）")


if __name__ == "__main__":
    unittest.main()
