"""B6：契约记账的落点、记忆桥的「当前故障」、生活事件幂等键的发生时刻。

三条都是「同一件事在两个地方各记一遍、结论不一致」这一类：

* `refresh_contract` 把「已计算」的摘要与时间戳写在 `build_contract` **之前**——
  一次失败就把闸门推进到新值，接下来 `CONTRACT_MAX_AGE_SECONDS` 内直接返回盘上的
  旧契约，既不重试也不再告警（审核 core F4）。
* `memory_link.last_error()` 只写不清：一次超时之后 /拟人 诊断 永远显示那条陈年故障，
  而桥接早就恢复了（审核 M1）。
* 生活事件写回用「内容」当幂等键：同一个名字的过程第二次结束，键一模一样，
  被记忆插件当成重放丢掉，于是「这件事再发生一次」永远写不进记忆库（审核 契约 14）。
"""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest import mock

from humanoid import memory_link

from .test_main_integration import main_module
from .test_v303_b3_runtime import make_core


class ContractBookkeepingTest(unittest.TestCase):
    """摘要与时间戳只在真的算出来之后落（core F4）。"""

    def test_a_failed_build_does_not_advance_the_bookkeeping(self):
        core = make_core()
        self.assertEqual(core._contract_built_at, 0.0)
        with mock.patch(
            "humanoid.core_instance.build_contract", side_effect=RuntimeError("炸了")
        ):
            self.assertIsNone(core.refresh_contract())
        self.assertEqual(
            core._contract_built_at,
            0.0,
            "算失败也把时间戳推进了：接下来 60 秒直接返回盘上的旧契约，不再重试、不再告警",
        )
        self.assertIsNone(core._contract_digest, "算失败不该把输入摘要记成已计算")

    def test_the_next_round_retries_instead_of_serving_the_stale_snapshot(self):
        core = make_core()
        with mock.patch(
            "humanoid.core_instance.build_contract", side_effect=RuntimeError("炸了")
        ):
            self.assertIsNone(core.refresh_contract())
        fresh = {"identity": {"char_name": "小雨"}}
        with mock.patch("humanoid.core_instance.build_contract", return_value=fresh) as build:
            self.assertEqual(core.refresh_contract(), fresh)
        self.assertTrue(build.called, "上一轮失败之后这一轮必须重算，不能直接吃缓存")
        self.assertNotEqual(core._contract_built_at, 0.0)

    def test_an_empty_contract_does_not_advance_the_bookkeeping(self):
        """空字典也是「没算出来」：旧实现把它当成算完了，同样卡 60 秒。"""
        core = make_core()
        with mock.patch("humanoid.core_instance.build_contract", return_value={}):
            self.assertIsNone(core.refresh_contract())
        self.assertEqual(core._contract_built_at, 0.0)
        self.assertIsNone(core._contract_digest)


class CurrentFailureTest(unittest.IsolatedAsyncioTestCase):
    """last_error() 的语义是「此刻是否还坏着」，不是「历史上最近一次失败」（M1）。"""

    def setUp(self):
        memory_link.clear_cache()
        memory_link._LAST_ERROR = ""

    def tearDown(self):
        memory_link.clear_cache()
        memory_link._LAST_ERROR = ""

    async def test_a_successful_read_clears_the_stale_failure(self):
        memory_link._LAST_ERROR = "timeout: compose_context() 超过 0.5s 未返回"

        class _Bridge:
            async def compose_context(self, **_kw):
                return "团子"

        text = await memory_link.fetch_memory_text(_Bridge(), "umo", "u1", query="猫")
        self.assertEqual(text, "团子")
        self.assertEqual(
            memory_link.last_error(),
            "",
            "桥接已经通了，诊断里还挂着陈年故障",
        )

    async def test_a_successful_write_clears_the_stale_failure(self):
        memory_link._LAST_ERROR = "exception: 对面炸了"

        class _Bridge:
            async def record_external_memory(self, **_kw):
                return True

        self.assertTrue(await memory_link.write_life_event(_Bridge(), "u1", "甲", umo="umo"))
        self.assertEqual(memory_link.last_error(), "")

    async def test_a_new_failure_after_recovery_is_recorded_again(self):
        class _Flaky:
            def __init__(self):
                self.calls = 0

            async def compose_context(self, **_kw):
                self.calls += 1
                if self.calls == 1:
                    return "团子"
                raise RuntimeError("又坏了")

        bridge = _Flaky()
        await memory_link.fetch_memory_text(bridge, "umo", "u1", query="a")
        self.assertEqual(memory_link.last_error(), "")
        await memory_link.fetch_memory_text(bridge, "umo", "u1", query="b")
        self.assertIn("exception", memory_link.last_error())


class LifeEventKeyTest(unittest.IsolatedAsyncioTestCase):
    """同一件事的不同次必须得到不同的幂等键（契约 14）。"""

    class _Recorder:
        def __init__(self):
            self.calls = []

        async def record_external_memory(self, **kw):
            self.calls.append(kw)

    async def test_two_occurrences_of_the_same_event_get_different_keys(self):
        first, second = self._Recorder(), self._Recorder()
        await memory_link.write_life_event(
            first, "u1", "她做完了整理档案", umo="umo", key="proc:整理档案", occurred_at=1000.0
        )
        await memory_link.write_life_event(
            second, "u1", "她做完了整理档案", umo="umo", key="proc:整理档案", occurred_at=2000.0
        )
        self.assertNotEqual(
            first.calls[0]["idempotency_key"],
            second.calls[0]["idempotency_key"],
            "同一件事第二次发生用了同一个幂等键 → 被对面当成重放丢掉",
        )

    async def test_a_replay_of_the_same_occurrence_stays_idempotent(self):
        first, second = self._Recorder(), self._Recorder()
        await memory_link.write_life_event(
            first, "u1", "她做完了整理档案", umo="umo", key="proc:整理档案", occurred_at=1000.0
        )
        await memory_link.write_life_event(
            second, "u1", "她做完了整理档案", umo="umo", key="proc:整理档案", occurred_at=1000.0
        )
        self.assertEqual(
            first.calls[0]["idempotency_key"], second.calls[0]["idempotency_key"]
        )

    async def test_without_a_time_the_key_is_unchanged(self):
        """旧调用方不传时刻：键与以前完全一样，行为不变。"""
        bridge = self._Recorder()
        await memory_link.write_life_event(bridge, "u1", "甲", umo="umo", key="p1")
        self.assertEqual(bridge.calls[0]["idempotency_key"], "life:umo:p1")


class LifeEventWiringTest(unittest.IsolatedAsyncioTestCase):
    """main.py 把「这一次被看见的时刻」传下去，且重试期间不变（契约 14）。"""

    def setUp(self):
        self.calls: list = []
        self.wrote = True

        async def fake_write(bridge, uid, content, **kw):
            self.calls.append({**kw, "content": content, "uid": uid})
            return self.wrote

        self.patch = mock.patch.object(
            main_module.memory_link, "write_life_event", new=fake_write
        )
        self.patch.start()
        obj = main_module.HumanoidCore.__new__(main_module.HumanoidCore)
        obj._last_written_process = {}
        obj._life_write_stamps = {}
        obj._life_write_failed_at = {}
        obj._life_event_seen_at = {}
        obj._known_roles = {"bot1"}
        obj._memory_bridge = lambda: object()
        obj._memory_timeout = lambda default: default
        self.obj = obj
        self.name = "整理档案"
        self.core = SimpleNamespace(
            role_id="bot1", process=SimpleNamespace(recent=lambda n=None: [self.name])
        )

    def tearDown(self):
        self.patch.stop()

    async def _run(self):
        await self.obj._write_finished_process(self.core, "umo", "u1")

    async def test_the_first_sight_only_records(self):
        await self._run()
        self.assertEqual(self.calls, [], "重启后第一次看到只记账，不写")
        self.assertEqual(self.obj._last_written_process["bot1"], self.name)

    async def test_the_observation_time_reaches_the_key_builder(self):
        await self._run()
        self.name = "煮了碗面"
        await self._run()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["key"], "proc:煮了碗面")
        self.assertGreater(self.calls[0]["occurred_at"], 0.0, "没把发生时刻传下去")

    async def test_a_failed_write_keeps_the_same_occurrence_time_for_the_retry(self):
        await self._run()
        self.name = "煮了碗面"
        self.wrote = False
        await self._run()
        first_seen = self.calls[0]["occurred_at"]
        self.wrote = True
        self.obj._life_write_failed_at["bot1"] = 0.0  # 退避期过了
        time.sleep(0.01)
        await self._run()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(
            self.calls[1]["occurred_at"],
            first_seen,
            "重试换了发生时刻：同一件事会被写成两条记忆",
        )

    async def test_a_successful_write_forgets_the_occurrence_time(self):
        await self._run()
        self.name = "煮了碗面"
        await self._run()
        self.assertNotIn("bot1", self.obj._life_event_seen_at)

    async def test_the_seen_table_does_not_grow_with_deleted_roles(self):
        self.obj._life_event_seen_at = {"gone": 1.0}
        self.obj._known_roles = {"bot1"}
        await self._run()
        self.name = "煮了碗面"
        await self._run()
        self.assertNotIn("gone", self.obj._life_event_seen_at, "删掉的角色留下了死键")
