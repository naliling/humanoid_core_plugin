"""v3.1.12 生命周期补丁的回归测试。

线上同一周里暴露的三件事，形态都是「进程活着、插件活着、状态正常，功能已经死了」：

  1. 插件重载/重启之后，后台循环只会在「收到该角色的消息」时创建——
     `role_manager.start()` 遍历的是一个空字典。于是一个角色可以静好几天：
     `last_tick` 冻在关机那一刻、`today_date` 停在前天、`schedule_source`
     永远停在 template（线上被误判成「一直在睡」）。
  2. 循环抛异常退出只打一条 warning，`_started` 仍是 True，没有任何东西会把它
     拉起来（线上身体循环停摆 20 小时）。
  3. 体力/社交能量按「每条消息」扣，群消息也算——71 个群的背景流量一天就能扣到
     0，真正需要她开口的私聊反而被社交层 20 分硬闸挡掉。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from humanoid.config import ConfigBox
from humanoid.llm import LLMGateway, ProviderResolver
from humanoid.role_manager import RoleManager
from humanoid.state import StateStore

from .fakes import FakeContext, RecordingLogger
from .test_engine import DictConfig


def _build():
    raw = DictConfig({"timezone_city": "北京"})
    ctx = FakeContext()
    log = RecordingLogger()
    data_dir = Path(tempfile.mkdtemp())
    box = ConfigBox(raw)
    store = StateStore(data_dir / "state.json", lambda: 1.0, log)
    store.load("2026-11-08", box.value.cycle_length)
    resolver = ProviderResolver(ctx, log)
    gateway = LLMGateway(resolver, box, log)
    return RoleManager(store, box, log, resolver, gateway, None), store, log


class PrewarmTest(unittest.IsolatedAsyncioTestCase):
    async def test_roles_already_in_state_are_started_on_load(self):
        """状态里出现过的角色，加载时就要把循环起起来，而不是等人来敲一下。"""
        manager, store, _log = _build()
        roles = store.data.setdefault("roles", {})
        roles["alice"] = {}
        roles["bob"] = {}
        await manager.start()
        try:
            self.assertEqual(set(manager._instances), {"alice", "bob"})
            for inst in manager.get_all():
                self.assertTrue(inst._started, f"{inst.role_id} 没有启动")
                self.assertTrue(inst._tasks, f"{inst.role_id} 没有后台循环")
        finally:
            await manager.stop()

    async def test_prewarm_is_idempotent_with_message_path(self):
        """预热建过的实例，消息路径再 get_or_create 不该叠出第二份循环。"""
        manager, store, _log = _build()
        store.data.setdefault("roles", {})["alice"] = {}
        await manager.start()
        try:
            first = manager.get_or_create("alice")
            await first.start()  # 已经启动过：必须直接 True，不重建循环
            names = [t.get_name() for t in first._tasks if not t.done()]
            self.assertEqual(len(names), len(set(names)), f"出现同名循环: {names}")
        finally:
            await manager.stop()


class DeadLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_dead_loop_is_restarted_and_live_ones_are_left_alone(self):
        """一个循环死了，只重启那一个；还活着的不能被重复拉起来。"""
        manager, _store, _log = _build()
        await manager.start()
        core = manager.get_or_create("default")
        await core.start()
        try:
            dead = [t for t in list(core._tasks) if t.get_name().endswith("body-tick")]
            self.assertTrue(dead, "前置条件：body-tick 应该在跑")
            for task in dead:
                task.cancel()
            await asyncio.gather(*dead, return_exceptions=True)

            restarted = core.restart_dead_loops()
            self.assertIn("body-tick", restarted)
            self.assertNotIn("social-recovery", restarted, "活着的循环不该被重启")
            self.assertIn("body-tick", core._live_loop_names())
        finally:
            await manager.stop()

    async def test_restart_is_refused_after_stop(self):
        """停止信号发出后不许再拉循环（否则循环会活过 stop()）。"""
        manager, _store, _log = _build()
        await manager.start()
        core = manager.get_or_create("default")
        await core.start()
        await manager.stop()
        self.assertEqual(core.restart_dead_loops(), [])


class GroupEnergyTest(unittest.IsolatedAsyncioTestCase):
    async def test_group_message_does_not_drain_private_battery(self):
        """群里的背景流量不该算她自己的活动量。"""
        manager, _store, _log = _build()
        await manager.start()
        core = manager.get_or_create("default")
        await core.start()
        try:
            # 这两个服务带 __slots__，实例属性改不了：在类上打补丁。
            energy_calls: list[int] = []
            social_calls: list[int] = []
            with mock.patch.object(
                type(core.energy),
                "consume_for_message",
                lambda _self: energy_calls.append(1) or 0.0,
            ), mock.patch.object(
                type(core.social),
                "consume_for_message",
                lambda _self: social_calls.append(1) or 0.0,
            ):
                core.on_message("u1", "群里有人聊天", is_group=True, umo="g1")
                self.assertEqual(energy_calls, [], "群消息不该扣体力")
                self.assertEqual(social_calls, [], "群消息不该扣社交能量")

                core.on_message("u1", "私聊找我", is_group=False, umo="p1")
                self.assertEqual(len(energy_calls), 1, "私聊要扣体力")
                self.assertEqual(len(social_calls), 1, "私聊要扣社交能量")
        finally:
            await manager.stop()


class AffectionMigrationTest(unittest.TestCase):
    def test_old_neutral_default_35_is_migrated(self):
        """35 只在 v2.27.2→v3.1.0 之间当过出厂值，且低于亲密门槛，按旧默认迁移。"""
        from humanoid.config import plan_default_migrations

        self.assertEqual(
            plan_default_migrations({"mood_initial_affection": 35}),
            {"mood_initial_affection": 55},
        )

    def test_hand_picked_affection_is_never_touched(self):
        from humanoid.config import plan_default_migrations

        self.assertEqual(plan_default_migrations({"mood_initial_affection": 60}), {})
        self.assertEqual(plan_default_migrations({"mood_initial_affection": 20}), {})
        self.assertEqual(
            plan_default_migrations(
                {"mood_initial_affection": 35}, applied=["mood_initial_affection"]
            ),
            {},
            "已迁移过的键不该再动",
        )
