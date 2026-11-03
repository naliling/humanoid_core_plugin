"""B5 注入取舍：两段各自降级（#102）与重放去重（#103）。

两条都是「本来就不该有」的静默缺陷：

1. #102：`state_note`（关系阶段/心境/她这会儿在做什么/时间天气穿搭）原来写在
   `if injection:` 里面，事实块被预算裁空或构建抛异常时整段一起消失——模型只剩
   一条空背景，而插件对外宣称的是「把背景递给她」。
2. #103：`on_llm_request` 会被工具循环逐轮重放、失败重试也会重放，注入拼装却没有
   去重，同一轮上下文里会出现两遍「此刻的她」与两遍身体事实。
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import time
import unittest
from pathlib import Path

from humanoid.prompt_builder import MARK_PREFIX, STATE_MARK

from .fakes import FakeContext, FakeProvider
from .test_main_integration import (
    GOOD_SCHEDULE,
    SKIP_REASON,
    FakeEvent,
    FakeProviderRequest,
    injected_text,
    main_module,
)

HUMAN = "10001"


def _boom(*_args, **_kwargs):
    raise RuntimeError("boom")


@unittest.skipIf(main_module is None, SKIP_REASON)
class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.provider = FakeProvider("p1", reply=GOOD_SCHEDULE)
        self.ctx = FakeContext(chat_providers=[self.provider])
        self.raw = {
            "timezone_city": "北京",
            "schedule_provider_name": "p1",
            "admin_qq": [HUMAN],
            "state_flush_interval_seconds": 1,
        }
        main_module.get_astrbot_data_path = lambda: str(self.tmp)
        self.star = main_module.HumanoidCore(self.ctx, self.raw)
        await self.star.initialize()

    async def asyncTearDown(self) -> None:
        await self.star.terminate()


class InjectReplayDedupTest(_Base):
    async def test_replaying_the_same_request_injects_once(self) -> None:
        event = FakeEvent("你在干嘛")
        req = FakeProviderRequest("")
        await self.star.inject_context(event, req)
        first = injected_text(req)
        self.assertEqual(first.count(STATE_MARK), 1)
        self.assertEqual(first.count(MARK_PREFIX), 1)

        await self.star.inject_context(event, req)
        second = injected_text(req)
        self.assertEqual(second.count(STATE_MARK), 1, "重放又追加了一遍「此刻的她」")
        self.assertEqual(second.count(MARK_PREFIX), 1, "重放又追加了一遍身体事实")

    async def test_a_new_request_still_gets_its_own_injection(self) -> None:
        event = FakeEvent("你在干嘛")
        first_req = FakeProviderRequest("")
        await self.star.inject_context(event, first_req)

        second_req = FakeProviderRequest("")
        await self.star.inject_context(event, second_req)
        text = injected_text(second_req)
        self.assertEqual(text.count(STATE_MARK), 1)
        self.assertEqual(text.count(MARK_PREFIX), 1)

    async def test_cleared_parts_are_injected_again(self) -> None:
        """宿主复用同一个请求对象却清空了临时块：标记还在，文本没了，必须重注入。"""
        event = FakeEvent("你在干嘛")
        req = FakeProviderRequest("")
        await self.star.inject_context(event, req)
        self.assertTrue(req.extra_user_content_parts)

        req.extra_user_content_parts.clear()
        req.system_prompt = ""
        await self.star.inject_context(event, req)
        text = injected_text(req)
        self.assertEqual(text.count(STATE_MARK), 1)
        self.assertEqual(text.count(MARK_PREFIX), 1)


class IndependentDegradeTest(_Base):
    async def test_broken_fact_block_does_not_drop_the_state_note(self) -> None:
        event = FakeEvent("你在干嘛")
        core = self.star._core(event)
        core.build_injection = _boom
        req = FakeProviderRequest("")
        await self.star.inject_context(event, req)
        text = injected_text(req)
        self.assertIn(STATE_MARK, text, "事实块失败时「此刻的她」也一起没了")
        self.assertNotIn(MARK_PREFIX, text)
        self.assertIn("build_injection", self.star._warned_once)

    async def test_broken_state_note_does_not_drop_the_fact_block(self) -> None:
        event = FakeEvent("你在干嘛")
        original = main_module._state_note
        main_module._state_note = _boom
        try:
            req = FakeProviderRequest("")
            await self.star.inject_context(event, req)
        finally:
            main_module._state_note = original
        text = injected_text(req)
        self.assertIn(MARK_PREFIX, text, "「此刻的她」失败时身体事实也一起没了")
        self.assertNotIn(STATE_MARK, text)
        self.assertIn("state_note", self.star._warned_once)

    async def test_each_failure_warns_only_once(self) -> None:
        core = self.star._core(FakeEvent("你好"))
        core.build_injection = _boom
        for index in range(3):
            await self.star.inject_context(
                FakeEvent(f"第 {index} 句"), FakeProviderRequest("")
            )
        self.assertEqual(list(self.star._warned_once).count("build_injection"), 1)


class SocialStateWordingTest(_Base):
    """#166：同一个「自主拟人社交」对接状态，两块面板必须说同一句话。

    改前 `body_lines()` 与 `status_lines()` 各写一份词表：一份漏了 `no_data_root`
    （落到「状态未知」），另一份五态齐全但措辞完全不同。同一次 `/体检` 里两处
    说法不一致时，用户无法判断哪个是真的。
    """

    def _core_with_state(self, state):
        core = self.star._core(FakeEvent("在吗"))
        core.signals.status = lambda: {"state": state}
        return core

    async def test_no_data_root_is_named_in_both_panels(self) -> None:
        core = self._core_with_state("no_data_root")
        body = "\n".join(core.body_lines())
        status = "\n".join(core.status_lines(HUMAN))
        self.assertIn("未装 / 没有数据目录", body)
        self.assertIn("未装 / 没有数据目录", status)
        self.assertNotIn("状态未知", body, "body_lines 又把 no_data_root 说成「状态未知」")

    async def test_unknown_state_does_not_leak_english(self) -> None:
        core = self._core_with_state("some_future_state")
        body = "\n".join(core.body_lines())
        status = "\n".join(core.status_lines(HUMAN))
        self.assertIn("状态未知", body)
        self.assertIn("状态未知", status)
        self.assertNotIn("some_future_state", status, "状态页把英文状态原文吐给用户了")

    async def test_both_panels_share_one_wording(self) -> None:
        from humanoid.core_instance import _SOCIAL_STATE_LINES

        for key, phrase in _SOCIAL_STATE_LINES.items():
            core = self._core_with_state(key)
            body = "\n".join(core.body_lines())
            status = "\n".join(core.status_lines(HUMAN))
            self.assertIn(phrase, body, f"{key} 在 body_lines 里换了说法")
            self.assertIn(phrase, status, f"{key} 在 status_lines 里换了说法")


class LifeLogCascadeTest(_Base):
    """#134：删人时要把她在 life_log 里记着的那个人一起清掉。

    条目里只有 uid，渲染时按 uid 认名字。人被清掉之后条目还在，`_who()` 认不出
    名字只会少个称呼；但 uid 一旦被重新用上（换号、同一 QQ 换了人），旧事就被
    安到新来的人头上。
    """

    def _entry(self, uid: str, text: str) -> dict:
        now = time.time()
        return {"text": text, "uid": uid, "at": now, "until": now + 3600.0, "src": "social"}

    async def test_drop_only_touches_the_deleted_uid(self) -> None:
        core = self.star._core(FakeEvent("在吗"))
        core.scope.set_self(
            "life_log",
            [self._entry("20002", "帮 TA 挑生日礼物"), self._entry("30003", "等 TA 回消息")],
        )
        dropped = core._drop_life_items_of({"20002"})
        self.assertEqual(dropped, 1)
        left = core.scope.get_self("life_log")
        self.assertEqual([item["uid"] for item in left], ["30003"])
        self.assertEqual(left[0]["text"], "等 TA 回消息", "清理动了条目形状")

    async def test_nothing_removed_means_no_write(self) -> None:
        core = self.star._core(FakeEvent("在吗"))
        original = [self._entry("20002", "帮 TA 挑生日礼物")]
        core.scope.set_self("life_log", original)
        self.assertEqual(core._drop_life_items_of(set()), 0)
        self.assertEqual(core.scope.get_self("life_log"), original)

    async def test_maintenance_loop_clears_items_of_pruned_users(self) -> None:
        """端到端：`prune_expired()` 删掉空壳用户后，账本里他的条目同轮消失。"""
        core = self.star._core(FakeEvent("在吗"))
        core.scope.user_state("20002")  # 空壳：没有任何会被 retention 清理的字段
        core.scope.set_self(
            "life_log",
            [self._entry("20002", "帮 TA 挑生日礼物"), self._entry("30003", "等 TA 回消息")],
        )
        task = asyncio.create_task(core._maintenance_loop())
        try:
            for _ in range(200):
                await asyncio.sleep(0.01)
                if core.scope.peek_user("20002", "mood", None) is None:
                    break
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        left = core.scope.get_self("life_log") or []
        self.assertEqual(
            [item["uid"] for item in left],
            ["30003"],
            "被删掉的用户还留在她的未完事项里",
        )


if __name__ == "__main__":
    unittest.main()
