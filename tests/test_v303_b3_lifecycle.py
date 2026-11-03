"""B3 生命周期：启动失败要能重试、读路径不许建档、加载时清掉空壳。

三条都是「表面照常跑、其实坏着」的问题：

1. 角色实例启动抛异常后实例留在缓存里，`get_or_create` 之后什么都不做——
   后台循环（日程/身体/过程/天气/社交）直到插件重载都不会跑，而消息仍能正常回；
2. `RoleScope.get_user` 走 get-or-create：每读一眼就给这个人留一条 `{}`，
   它不含任何会被过期清理的字段，`prune_expired` 跳过，state.json 随群规模只增不减；
3. 已经留在盘上的空壳要靠下次 prune 才消失，加载时顺手清掉更早一步。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from humanoid.config import HumanoidConfig
from humanoid.state_snapshot import relation_of
from humanoid.role_manager import RETRY_SECONDS, RoleManager
from humanoid.role_scope import RoleScope
from humanoid.services.mood import MoodService
from humanoid.state import StateStore

from .fakes import RecordingLogger, ScopeStore


def cfg(**overrides) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", **overrides})


class _StubStore:
    """`RoleManager.stop()` 只会 await `flush()`。"""

    def __init__(self):
        self.flushed = 0

    async def flush(self):
        self.flushed += 1


def _fake_instance_class(outcomes):
    """造一个假 `HumanoidCoreInstance`：每次 start() 按 outcomes 依次给结果。"""

    class _FakeInstance:
        instances: list = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.role_id = kwargs.get("role_id", "")
            self.start_results = list(outcomes)
            self.start_calls = 0
            _FakeInstance.instances.append(self)

        async def start(self):
            self.start_calls += 1
            result = self.start_results.pop(0) if self.start_results else True
            if isinstance(result, BaseException):
                raise result
            return result

        async def stop(self):
            return None

    _FakeInstance.instances = []
    return _FakeInstance


async def _drain(mgr: RoleManager) -> None:
    """等所有启动任务结束，再让 done_callback 跑完（callback 在下一个循环迭代执行）。"""
    tasks = [t for t in mgr._start_tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)
    await asyncio.sleep(0)


class StartFailureIsRetriedTest(unittest.IsolatedAsyncioTestCase):
    def _manager(self, fake_cls):
        patcher = mock.patch("humanoid.role_manager.HumanoidCoreInstance", fake_cls)
        patcher.start()
        self.addCleanup(patcher.stop)
        mgr = RoleManager(None, None, RecordingLogger(), None, None)
        mgr._state_store = _StubStore()
        mgr._started = True
        return mgr

    async def test_failed_start_keeps_the_instance_and_records_it(self):
        fake = _fake_instance_class([RuntimeError("日程服务还没就绪")])
        mgr = self._manager(fake)
        inst = mgr.get_or_create("r1")
        await _drain(mgr)
        self.assertEqual(list(mgr.failed_roles()), ["r1"], "启动失败没有被记下来")
        self.assertIs(mgr.get_or_create("r1"), inst, "失败后实例被丢掉了，消息会读不到状态")
        self.assertEqual(len(mgr.get_all()), 1)

    async def test_backoff_blocks_immediate_retry(self):
        fake = _fake_instance_class([RuntimeError("boom")])
        mgr = self._manager(fake)
        mgr.get_or_create("r1")
        await _drain(mgr)
        before = fake.instances[0].start_calls
        mgr.get_or_create("r1")  # 退避期内：不该再起任务
        await _drain(mgr)
        self.assertEqual(fake.instances[0].start_calls, before, "退避期内又重试了")

    async def test_retry_after_backoff_starts_and_clears_the_failure(self):
        fake = _fake_instance_class([RuntimeError("boom"), True])
        mgr = self._manager(fake)
        mgr.get_or_create("r1")
        await _drain(mgr)
        self.assertEqual(list(mgr.failed_roles()), ["r1"])
        mgr._failed_at["r1"] = time.monotonic() - (RETRY_SECONDS + 1.0)
        mgr.get_or_create("r1")
        await _drain(mgr)
        self.assertEqual(fake.instances[0].start_calls, 2, "退避到期后没有重试")
        self.assertEqual(mgr.failed_roles(), {}, "成功后失败记录没清掉")

    async def test_stop_clears_failure_records(self):
        fake = _fake_instance_class([RuntimeError("boom")])
        mgr = self._manager(fake)
        mgr.get_or_create("r1")
        await _drain(mgr)
        await mgr.stop()
        self.assertEqual(mgr.failed_roles(), {})


class ReadDoesNotCreateShellTest(unittest.TestCase):
    def _scope(self):
        data = {"roles": {"r": {"self": {}, "users": {}}}}
        dirty = []
        scope = RoleScope(data, "r", lambda: dirty.append(1))
        return data, dirty, scope

    def test_get_user_does_not_create_an_entry(self):
        data, dirty, scope = self._scope()
        self.assertIsNone(scope.get_user("u1", "last_interaction"))
        self.assertEqual(data["roles"]["r"]["users"], {}, "只读一次就留下了空壳")
        self.assertEqual(dirty, [], "只读一次不该标脏")

    def test_get_user_reads_an_existing_entry(self):
        data, _dirty, scope = self._scope()
        data["roles"]["r"]["users"]["u1"] = {"nickname": "阿哲"}
        self.assertEqual(scope.get_user("u1", "nickname"), "阿哲")
        self.assertIsNone(scope.get_user("u1", "missing"))

    def test_ensure_user_still_creates_for_writers(self):
        data, _dirty, scope = self._scope()
        entry = scope.ensure_user("u1")
        self.assertEqual(entry, {})
        self.assertIn("u1", data["roles"]["r"]["users"])


class EmptyShellsAreDroppedOnLoadTest(unittest.TestCase):
    def test_empty_dict_user_is_dropped_but_nickname_is_kept(self):
        path = Path(tempfile.mkdtemp()) / "state.json"
        payload = {
            "_state_version": 3,
            "roles": {
                "default": {
                    "self": {},
                    "users": {"ghost": {}, "42": {"nickname": "阿哲"}},
                }
            },
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        store = StateStore(path, lambda: 60.0, RecordingLogger())
        store.load("2026-01-01", 28)
        users = store.data["roles"]["default"]["users"]
        self.assertNotIn("ghost", users, "空壳没有被清掉")
        self.assertEqual(users["42"]["nickname"], "阿哲", "有称呼的条目被连带删了")

class RelationStageDoesNotRollBackTest(unittest.TestCase):
    """#45：过期清理后重建档案，关系阶段不许倒退。

    `state_snapshot.relation_of` 取 `max(turn_count, turn_count_peak)`，所以
    `turn_count` 被清理/归零时，阶段靠峰值撑住。这里钉住整条链：
    prune 把峰值搬到 user 级 → 重建档案继承 → 阶段不变。
    好感压在 40（档位 0）而轮数 400（熟悉度地板 2）——只有峰值真的接回来，
    阶段才不会从「熟到可以直说」掉回「还在熟悉」。
    """

    def _service(self):
        conf = cfg()
        store = ScopeStore(Path(tempfile.mkdtemp()) / "s.json")
        store.load("2026-01-01", conf.cycle_length)
        return MoodService(store.scope, lambda: conf, None, time_source=lambda: 1_000_000.0)

    def test_prune_carries_turn_peak_and_rebuild_keeps_the_stage(self):
        svc = self._service()
        now = 2_000_000_000.0
        rec = svc.profile("42")
        rec["affection"] = 40.0
        rec["turn_count"] = 400
        rec["turn_count_peak"] = 400
        rec["first_met"] = now - 200 * 86400.0
        rec["last_interaction"] = now - 200 * 86400.0
        svc._scope.set_user("42", "last_interaction", now - 200 * 86400.0)
        self.assertEqual(relation_of(rec, now).stage_id, 2, "前提：400 轮、200 天本该到档位 2")

        self.assertEqual(svc.prune_expired(now=now), 1, "情绪档案该被清掉")
        user_state = svc._scope.user_state("42")
        self.assertEqual(
            int(user_state.get("turn_count_peak", 0)), 400,
            "清理时峰值没搬到 user 级——重建档案会从「刚认识」重新数",
        )

        rebuilt = svc.profile("42")
        self.assertEqual(int(rebuilt.get("turn_count", 0)), 400, "重建档案没继承峰值")
        self.assertEqual(
            relation_of(rebuilt, now).stage_id, 2,
            "清理重建后关系阶段倒退了",
        )

    def test_reset_is_an_explicit_restart(self):
        """管理员 `/重置` 是唯一允许归零峰值的路径（否则好感回到初始值、阶段却还停在旧档）。"""
        svc = self._service()
        rec = svc.profile("42")
        rec["affection"] = 90.0
        rec["turn_count"] = 400
        rec["turn_count_peak"] = 400
        svc.reset("42")
        self.assertEqual(rec["turn_count"], 0)
        self.assertEqual(rec["turn_count_peak"], 0)
