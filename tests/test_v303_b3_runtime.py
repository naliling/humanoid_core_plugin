"""B3 / R1 收尾：读路径不建档、身体只在 refresh 时推进、闸门按角色判、start() 原子。

这四条都不是「数值」问题，而是**契约**问题，所以用例直接盯契约：

* `snapshot(user_id)` / `status_lines(user_id)` 是**读**路径。旧实现里
  `mood.profile()` / `user_state()` 读一眼就建档，于是面板、跨插件门面、
  `/你的状态` 问一句「TA 怎么样」就在 state.json 里给陌生人留一条永久条目
  （审核 F3）。
* `snapshot(refresh=False)` 的契约是「只读」。旧实现里 `soma.advance()` 在
  `if refresh` 之外，注入链一轮问 5 次快照就把身体推进 5 次（审核 F5）。
* 空闲静默按**角色**判：闸门全局一份，旧实现一个标量让 A 角色的一句话松开
  B 角色的静默（审核 main.py 6）。
* `start()` 必须原子：`_started` 先落、后续任何一步抛异常都要退回 False，
  否则实例永久停在「已启动但零循环」，role_manager 的退避重试被一句
  `if self._started: return True` 挡掉（#96 交叉要求）。
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from humanoid.config import HumanoidConfig
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.llm import CallGate, PURPOSE_SCHEDULE
from humanoid.state import StateStore

from .fakes import FakeContext, FrozenClock, RecordingLogger

TZ = ZoneInfo("Asia/Shanghai")
MOMENT = datetime(2026, 8, 22, 15, 20, tzinfo=TZ)
TODAY = MOMENT.strftime("%Y-%m-%d")


def cfg(**overrides) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", **overrides})


def make_core(**conf) -> HumanoidCoreInstance:
    store = StateStore(Path(tempfile.mkdtemp()) / "state.json", lambda: 0.01)
    store.load(TODAY, 28)
    config = cfg(**conf)
    core = HumanoidCoreInstance(
        role_id="bot1",
        state_store=store,
        config_provider=lambda: config,
        logger=RecordingLogger(),
        stop_event=asyncio.Event(),
        resolver=FakeContext(),
        gateway=None,
    )
    core.clock = FrozenClock(MOMENT)
    return core


class ReadPathNeverBuildsTest(unittest.TestCase):
    """问一句「TA 怎么样」不该在档案里留下一个陌生人。"""

    def test_snapshot_for_unknown_user_does_not_create_a_mood_profile(self):
        core = make_core()
        core.snapshot("stranger-1")
        self.assertIsNone(
            core.scope.peek_user("stranger-1", "mood", None),
            "snapshot(user_id) 是读路径，不该给没有档案的人现建一条",
        )

    def test_status_lines_for_unknown_user_does_not_create_a_mood_profile(self):
        core = make_core()
        core.status_lines("stranger-2")
        self.assertIsNone(
            core.scope.peek_user("stranger-2", "mood", None),
            "/你的状态 是读路径，不该顺手建档",
        )

    def test_snapshot_still_reports_a_real_profile(self):
        """只读不等于不给：已有档案的人照样要看到关系数据。"""
        core = make_core()
        core.mood.set_affection("known-1", 61.0)
        snap = core.snapshot("known-1")
        self.assertIn("mood", snap)
        self.assertAlmostEqual(float(snap["mood"]["affection"]), 61.0, places=1)

    def test_on_message_does_not_build_for_a_stranger_when_mood_is_off(self):
        """情绪层关着时，那条「衰减」不该把人建成档案（审核 F2）。"""
        core = make_core(mood_enabled=False)
        core.on_message("stranger-3", "在吗", is_group=False)
        self.assertIsNone(
            core.scope.peek_user("stranger-3", "mood", None),
            "mood_enabled=False 时不该有 mood 档案可衰减",
        )

    def test_on_message_does_not_build_for_a_group_stranger(self):
        """群聊里情绪层默认不工作：陌生人发言不该留档案（审核 main.py 4）。"""
        core = make_core(mood_enabled=True, mood_enabled_in_group=False)
        core.on_message("stranger-4", "在吗", is_group=True)
        self.assertIsNone(
            core.scope.peek_user("stranger-4", "mood", None),
            "群里不给陌生人建档",
        )


class SnapshotRefreshContractTest(unittest.TestCase):
    """`refresh=False` 就是只读：注入链一轮问 5 次，不能推进 5 次。"""

    def test_snapshot_without_refresh_does_not_advance_the_body(self):
        core = make_core(soma_enabled=True)
        calls = []
        core.soma.advance = lambda *a, **k: calls.append(1)
        core.snapshot(refresh=False)
        core.snapshot(refresh=False)
        self.assertEqual(calls, [], "refresh=False 不该推进身体")

    def test_snapshot_with_refresh_advances_once(self):
        core = make_core(soma_enabled=True)
        calls = []
        core.soma.advance = lambda *a, **k: calls.append(1)
        core.snapshot(refresh=True)
        self.assertEqual(len(calls), 1, "refresh=True 推进一次，且只一次")


class StartIsAtomicTest(unittest.TestCase):
    """启动失败要能被重试：`_started` 不能停在 True。"""

    def test_failed_start_leaves_the_instance_restartable(self):
        core = make_core()

        def boom():
            raise RuntimeError("日程初始化炸了")

        with mock.patch.object(core.schedule, "seed_first_segment", side_effect=boom):
            with self.assertRaises(RuntimeError):
                asyncio.run(core.start())
        self.assertFalse(core._started, "启动失败后 _started 必须退回 False，否则重试被挡")
        # 失败时只回收本次起的任务，**不能**调 stop()：stop() 置 _stop_event 之后
        # start() 开头就直接 return False，重试机制会被自己的清理动作废掉。
        self.assertFalse(core._stop_event.is_set(), "回收任务不许碰 _stop_event")
        self.assertEqual(set(core._tasks), set(), "失败那一轮起的循环必须全部回收")
        # 重试（这次不炸）必须真的起来
        self.assertTrue(asyncio.run(core.start()), "修好之后重试要能成功")
        self.assertTrue(core._started)
        names = [task.get_name() for task in core._tasks]
        self.assertEqual(len(names), len(set(names)), "重试不许留下两份同名循环")
        asyncio.run(core.stop())


class GateIsPerRoleTest(unittest.TestCase):
    """闸门全局一份，但「有没有人看着她」是每个角色各自的事实。"""

    def gate(self, **conf):
        conf.setdefault("llm_idle_silence_minutes", 60)
        return CallGate(lambda: cfg(**conf))

    def test_one_role_talking_does_not_release_another(self):
        gate = self.gate()
        gate.note_interaction(role_id="botA")
        self.assertTrue(
            gate.check(PURPOSE_SCHEDULE, role_id="botA").allowed,
            "跟 A 说过话，A 的静默该松开",
        )
        self.assertFalse(
            gate.check(PURPOSE_SCHEDULE, role_id="botB").allowed,
            "A 的对话不该松开 B 的空闲静默（旧实现一个标量就会）",
        )

    def test_never_talked_role_reports_never_interacted(self):
        gate = self.gate()
        gate.note_interaction(role_id="botA")
        snap = gate.snapshot(role_id="botB")
        self.assertTrue(snap["never_interacted"], "B 从没被说过话")
        self.assertIn("botA", snap["roles"], "诊断要能看见每个角色各自多久没人理")

    def test_legacy_call_without_role_keeps_old_behaviour(self):
        """还没按角色传参的调用方：行为和旧的全局标量一致，不能永久静默。"""
        gate = self.gate()
        gate.note_interaction()
        self.assertTrue(gate.check(PURPOSE_SCHEDULE).allowed)


if __name__ == "__main__":
    unittest.main()
