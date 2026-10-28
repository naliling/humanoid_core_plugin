"""v2.28.16：兄弟 bot 可以聊，但防互刷。

两半一起锁：

1. `humanoid.sibling_chat.SiblingChatGuard` 的纯逻辑：同一 (role, peer) 窗口内
   最多放行 3 次、第 4 次拒绝、窗口过期恢复、真人一开口清零、不同 peer/role 互不影响、
   坏参数/坏时间戳不抛；
2. `main.py` 的接线：回复钩子里对兄弟 bot 用 allow() 决定放不放行（而不是无条件
   stop_event），`on_message` 里仍然不记兄弟 bot 的情绪/身体、但真人消息要清额度。

旧行为（直接挡掉兄弟 bot）的反例写在 `test_..._can_talk_three_times...`：
三个新事件都应放行，第 4 个才被挡。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from humanoid.sibling_chat import (
    DEFAULT_MAX_TURNS,
    DEFAULT_WINDOW_SECONDS,
    SiblingChatGuard,
)

from .fakes import FakeContext, FakeProvider
from .test_main_integration import (
    GOOD_SCHEDULE,
    SKIP_REASON,
    FakeEvent,
    FakeProviderRequest,
    injected_text,
    main_module,
)

SIBLING = "88888"  # 兄弟 bot 的 id：必须出现在 state.roles 里才被 _known_role_ids 认出
HUMAN = "10001"
SELF_ID = "99999"


# ---------------------------------------------------------------------------
# 纯逻辑：SiblingChatGuard
# ---------------------------------------------------------------------------


class SiblingChatGuardTest(unittest.TestCase):
    def test_defaults_are_ten_minutes_and_three_turns(self):
        self.assertEqual(DEFAULT_WINDOW_SECONDS, 600.0)
        self.assertEqual(DEFAULT_MAX_TURNS, 3)
        guard = SiblingChatGuard()
        self.assertEqual(guard.window_seconds, 600.0)
        self.assertEqual(guard.max_turns, 3)

    def test_allows_three_turns_then_denies_the_fourth(self):
        guard = SiblingChatGuard()
        now = 1_000_000.0
        self.assertEqual(
            [guard.allow("role", "peer", now) for _ in range(3)],
            [True, True, True],
            "同一对 peer 的前 3 次都该放行",
        )
        self.assertFalse(guard.allow("role", "peer", now), "第 4 次必须拒绝")

    def test_window_expiry_restores_quota(self):
        guard = SiblingChatGuard(window_seconds=600)
        now = 1_000_000.0
        for _ in range(3):
            self.assertTrue(guard.allow("r", "p", now))
        self.assertFalse(guard.allow("r", "p", now + 599), "窗口内仍该拒绝")
        self.assertTrue(guard.allow("r", "p", now + 600), "窗口一过自动恢复")

    def test_window_slides_from_each_allowed_turn(self):
        """额度按每次放行的时间各自过期，不是「整批一起过期」。"""
        guard = SiblingChatGuard(window_seconds=600)
        t0 = 5_000.0
        self.assertTrue(guard.allow("r", "p", t0))  # [t0]
        self.assertTrue(guard.allow("r", "p", t0 + 300))  # [t0, t0+300]
        self.assertTrue(guard.allow("r", "p", t0 + 300))  # 满 3 次
        self.assertFalse(guard.allow("r", "p", t0 + 301), "还没过期，第 4 次拒绝")
        # t0+601 时最早那一笔（t0）过期，剩两笔仍在窗口内 → 再放行一次
        self.assertTrue(guard.allow("r", "p", t0 + 601))

    def test_denied_attempts_do_not_extend_the_window(self):
        guard = SiblingChatGuard(window_seconds=600)
        t0 = 7_000.0
        for _ in range(3):
            self.assertTrue(guard.allow("r", "p", t0))
        for offset in (1, 100, 300, 599):
            self.assertFalse(guard.allow("r", "p", t0 + offset))
        self.assertTrue(
            guard.allow("r", "p", t0 + 600),
            "拒绝不记账：窗口仍从最后一次**放行**算起",
        )

    def test_human_clears_every_peer_of_that_role(self):
        guard = SiblingChatGuard()
        now = 9_000.0
        for peer in ("p1", "p2", "p3"):
            for _ in range(3):
                guard.allow("role", peer, now)
            self.assertFalse(guard.allow("role", peer, now), f"{peer} 额度应已用完")
        guard.note_human("role")
        for peer in ("p1", "p2", "p3"):
            self.assertTrue(guard.allow("role", peer, now), "真人开口后应重新可聊")

    def test_human_only_clears_that_role(self):
        guard = SiblingChatGuard()
        now = 9_000.0
        for _ in range(3):
            guard.allow("r1", "p", now)
            guard.allow("r2", "p", now)
        guard.note_human("r1")
        self.assertTrue(guard.allow("r1", "p", now))
        self.assertFalse(guard.allow("r2", "p", now), "别的角色的额度不该被连带清掉")

    def test_different_peers_do_not_share_quota(self):
        guard = SiblingChatGuard()
        now = 11_000.0
        for _ in range(3):
            guard.allow("r", "p1", now)
        self.assertFalse(guard.allow("r", "p1", now))
        self.assertTrue(guard.allow("r", "p2", now), "另一个 peer 的第一句应放行")
        self.assertTrue(guard.allow("r2", "p1", now), "另一个 role 的第一句应放行")

    def test_bad_now_values_do_not_raise_and_fall_back_to_real_clock(self):
        guard = SiblingChatGuard()
        for bad in (None, "坏时间", float("nan"), float("inf"), object()):
            with self.subTest(now=repr(bad)):
                self.assertIsInstance(guard.allow("r", "p", bad), bool)
        # 坏时间戳走真实时钟：连放 3 次后第 4 次仍会被拒（不会因为参数坏就关掉护栏）
        guard = SiblingChatGuard()
        self.assertTrue(all(guard.allow("r", "p", None) for _ in range(3)))
        self.assertFalse(guard.allow("r", "p", None))

    def test_bad_ids_are_denied_without_raising(self):
        """认不出谁在说话时不放行、也不记账——防刷是这个类唯一的职责。"""
        guard = SiblingChatGuard()
        for role, peer in ((None, "p"), ("", "p"), ("r", None), ("r", ""), (None, None)):
            with self.subTest(role=role, peer=peer):
                self.assertFalse(guard.allow(role, peer, 1.0))
        # 空 id 的调用不该污染正常 pair 的额度
        for _ in range(3):
            self.assertTrue(guard.allow("r", "p", 1.0))

    def test_corrupt_history_is_ignored_not_fatal(self):
        """运行中被手改坏的历史时间戳（字符串/None/NaN）只该被丢掉，不该抛。"""
        guard = SiblingChatGuard()
        guard._turns[("r", "p")] = ["坏", None, float("nan"), 1_000.0]  # type: ignore[assignment]
        self.assertIsInstance(guard.allow("r", "p", 1_000.0), bool)

    def test_clock_rollback_does_not_grant_free_turns(self):
        """时钟回拨时「未来」的时间戳也算有效：宁可多算一次，不给互刷留口子。"""
        guard = SiblingChatGuard()
        for _ in range(3):
            self.assertTrue(guard.allow("r", "p", 10_000.0))
        # 钟退到 9 分钟前：三笔都还算「未来」，照样拒绝
        self.assertFalse(guard.allow("r", "p", 10_000.0 - 540))

    def test_custom_limits(self):
        guard = SiblingChatGuard(window_seconds=10.0, max_turns=1)
        self.assertTrue(guard.allow("r", "p", 0.0))
        self.assertFalse(guard.allow("r", "p", 5.0))
        self.assertTrue(guard.allow("r", "p", 10.0))

    def test_invalid_constructor_values_fall_back_to_defaults(self):
        for window, turns in (("坏", -1), (0, 0), (float("nan"), float("inf"))):
            with self.subTest(window=window, turns=turns):
                guard = SiblingChatGuard(window_seconds=window, max_turns=turns)
                self.assertEqual(guard.window_seconds, DEFAULT_WINDOW_SECONDS)
                self.assertEqual(guard.max_turns, DEFAULT_MAX_TURNS)

    def test_note_human_tolerates_bad_ids(self):
        guard = SiblingChatGuard()
        for bad in (None, "", object(), 123):
            with self.subTest(role=repr(bad)):
                self.assertIsNone(guard.note_human(bad))  # 不抛就是通过


# ---------------------------------------------------------------------------
# 接线：main.py 的回复钩子与 on_message
# ---------------------------------------------------------------------------


@unittest.skipIf(main_module is None, SKIP_REASON)
class SiblingChatWiringTest(unittest.IsolatedAsyncioTestCase):
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
        # 不碰真实数据目录：直接替换 Star 用的路径
        main_module.get_astrbot_data_path = lambda: str(self.tmp)
        self.star = main_module.HumanoidCore(self.ctx, self.raw)
        await self.star.initialize()
        # `_known_role_ids` 只认本进程见过的角色 id：登记一个「兄弟 bot」。
        # v3.0.0（P2-08）：这个集合改成进程内维护（加载时从状态重建、角色实例创建时
        # 增长），生产路径由兄弟 bot 自己的实例经 _core 登记；测试手工塞状态后要同步
        # 登记一次，否则缓存里看不到它。
        self.star._state_store.data.setdefault("roles", {})[SIBLING] = {
            "self": {},
            "users": {},
        }
        self.star._known_roles.add(SIBLING)

    async def asyncTearDown(self) -> None:
        await self.star.terminate()

    async def _reply(self, text: str = "兄弟说话", event=None):
        """模拟回复钩子跑一轮：返回 (event, req)。"""
        event = event or FakeEvent(text, sender=SIBLING)
        req = FakeProviderRequest("")
        await self.star.inject_context(event, req)
        return event, req

    async def test_sibling_bot_can_talk_three_times_then_gets_blocked(self):
        """旧行为是第 1 次就 stop_event；现在前 3 次必须真的放行（主链路正常注入）。"""
        for index in range(3):
            event, req = await self._reply(f"兄弟第 {index + 1} 句")
            self.assertFalse(event.is_stopped(), f"第 {index + 1} 次互聊应放行")
            self.assertTrue(
                injected_text(req), f"第 {index + 1} 次放行时应正常注入上下文"
            )
        blocked, _req = await self._reply("兄弟第 4 句")
        self.assertTrue(blocked.is_stopped(), "第 4 次必须挡下本轮 LLM")

    async def test_same_event_replayed_by_tool_loop_consumes_one_turn(self):
        """同一条消息被工具循环重放两次，只该消耗 1 次额度（否则一条就吃光 3 次）。"""
        event = FakeEvent("兄弟第一句", sender=SIBLING)
        await self.star.inject_context(event, FakeProviderRequest(""))
        await self.star.inject_context(event, FakeProviderRequest(""))
        self.assertFalse(event.is_stopped(), "重放同一条消息不该被挡，也不该多扣额度")
        for index in range(2):
            later, _req = await self._reply(f"兄弟第 {index + 2} 句")
            self.assertFalse(later.is_stopped(), "额度还剩 2 次，应放行")
        blocked, _req = await self._reply("兄弟第 4 句")
        self.assertTrue(blocked.is_stopped())

    async def test_human_message_clears_the_sibling_chain(self):
        for _index in range(3):
            event, _req = await self._reply()
            self.assertFalse(event.is_stopped())
        blocked, _req = await self._reply()
        self.assertTrue(blocked.is_stopped(), "额度先要真的用完")

        # 真人开口 → 机器对机器那条链结束
        await self.star.on_message(FakeEvent("在吗", sender=HUMAN))
        fresh, _req = await self._reply("兄弟又来了")
        self.assertFalse(fresh.is_stopped(), "真人开口后兄弟 bot 应能重新聊")

    async def test_human_messages_are_not_rate_limited(self):
        """护栏只打在兄弟 bot 上：真人的消息一次都不该被它挡。"""
        for index in range(5):
            event = FakeEvent(f"人话{index}", sender=HUMAN)
            await self.star.inject_context(event, FakeProviderRequest(""))
            self.assertFalse(event.is_stopped())

    async def test_sibling_message_is_not_recorded_as_mood_or_body(self):
        """兄弟 bot 的发言仍然不进她的情绪/身体账——只有「回不回」变了。"""
        core = self.star._core(FakeEvent("/你的状态"))
        await self.star.on_message(FakeEvent("兄弟 bot 在群里说了句话", sender=SIBLING))
        self.assertFalse(core.mood.has_profile(SIBLING), "兄弟 bot 不该有情绪档案")
        users = self.star._state_store.data["roles"][SELF_ID].get("users", {}) or {}
        self.assertNotIn(SIBLING, users, "兄弟 bot 不该进用户档（身体/情绪都不该记）")

    async def test_sibling_is_still_blocked_when_no_quota_left_even_in_group(self):
        """群聊里同样受额度约束（同平台兄弟 bot 最常见的场景）。"""
        for _index in range(3):
            event, _req = await self._reply_event(
                FakeEvent("群里兄弟说话", sender=SIBLING, private=False)
            )
            self.assertFalse(event.is_stopped())
        blocked, _req = await self._reply_event(
            FakeEvent("群里兄弟第 4 句", sender=SIBLING, private=False)
        )
        self.assertTrue(blocked.is_stopped())

    async def _reply_event(self, event):
        req = FakeProviderRequest("")
        await self.star.inject_context(event, req)
        return event, req


if __name__ == "__main__":
    unittest.main()
