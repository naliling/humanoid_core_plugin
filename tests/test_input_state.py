"""输入状态（正在输入）的回归测试。

两条防线：

1. 上报器本身：起停、去重、超时、失败不炸、平台/群聊门槛；
2. main.py 的三个钩子接线：请求起、响应停、发送前补一帧。

所有用例都用一个假 bot 记录 set_input_status 调用，不需要真 QQ。
"""

from __future__ import annotations

import asyncio
import unittest

from humanoid.input_state import InputStateNotifier

from .test_main_integration import main_module

if main_module is None:  # pragma: no cover
    raise unittest.SkipTest("main.py 加载失败，跳过输入状态集成用例")


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def call_action(self, action: str, **params) -> None:
        self.calls.append({"action": action, **params})


class FakeBot:
    def __init__(self) -> None:
        self.api = FakeApi()


class FakeNapcatEvent:
    """最小可用的 NapCat 私聊事件替身。"""

    def __init__(self, sender: str = "10001", private: bool = True) -> None:
        self._sender = sender
        self._private = private
        self.bot = FakeBot()

    def get_platform_name(self) -> str:
        return "aiocqhttp"

    def is_private_chat(self) -> bool:
        return self._private

    def get_sender_id(self) -> str:
        return self._sender


class _Log:
    def debug(self, *a): pass
    def info(self, *a): pass
    def warning(self, *a): pass


class NotifierTest(unittest.IsolatedAsyncioTestCase):
    async def test_start_reports_and_stop_ends(self):
        n = InputStateNotifier(_Log(), interval=0.01, timeout=5.0)
        ev = FakeNapcatEvent()
        await n.start("umo-1", ev)
        await asyncio.sleep(0.05)
        await n.stop("umo-1")
        calls = ev.bot.api.calls
        self.assertTrue(calls, "启动后应该有至少一次上报")
        self.assertTrue(all(c["action"] == "set_input_status" for c in calls))
        self.assertTrue(all(c["user_id"] == "10001" for c in calls), "user_id 必须是字符串")
        self.assertTrue(all(c["event_type"] == 1 for c in calls))
        # 停掉之后不再新增
        count = len(calls)
        await asyncio.sleep(0.05)
        self.assertEqual(len(ev.bot.api.calls), count)

    async def test_duplicate_start_does_not_spawn_second_task(self):
        n = InputStateNotifier(_Log(), interval=0.01, timeout=5.0)
        ev = FakeNapcatEvent()
        await n.start("umo-1", ev)
        await n.start("umo-1", ev)
        self.assertEqual(len(n._tasks), 1)
        await n.stop_all()

    async def test_group_and_other_platform_are_skipped(self):
        n = InputStateNotifier(_Log(), interval=0.01, timeout=5.0)
        group = FakeNapcatEvent(private=False)
        await n.start("umo-g", group)
        self.assertEqual(group.bot.api.calls, [])
        self.assertEqual(n._tasks, {})

        class OtherPlatform(FakeNapcatEvent):
            def get_platform_name(self): return "qq_official"

        other = OtherPlatform()
        await n.start("umo-o", other)
        self.assertEqual(other.bot.api.calls, [])

    async def test_failure_is_swallowed(self):
        n = InputStateNotifier(_Log(), interval=0.01, timeout=5.0)
        ev = FakeNapcatEvent()

        async def boom(action, **params):
            raise RuntimeError("not napcat")

        ev.bot.api.call_action = boom
        await n.start("umo-1", ev)  # 不该抛
        await asyncio.sleep(0.03)
        await n.stop("umo-1")

    async def test_timeout_stops_itself(self):
        # timeout 有下限（1 秒），这里等过下限看它自己清掉。
        n = InputStateNotifier(_Log(), interval=0.01, timeout=0.05)
        self.assertEqual(n.timeout, 1.0, "超时下限应该是 1 秒")
        ev = FakeNapcatEvent()
        await n.start("umo-1", ev)
        await asyncio.sleep(1.3)
        self.assertNotIn("umo-1", n._tasks, "超时后任务要自己清掉")
        self.assertTrue(ev.bot.api.calls)

    async def test_orphan_task_not_dropped_by_previous_finally(self):
        """stop() 与 start() 交织时：旧任务的 finally 不能把新任务的记录删掉。

        否则新任务再也没人能停（只能等它自带的 timeout），期间一直上报「正在输入」。
        """
        n = InputStateNotifier(_Log(), interval=0.01, timeout=5.0)
        ev = FakeNapcatEvent()
        await n.start("umo-1", ev)
        await n.stop("umo-1")        # 旧任务进入收尾
        await n.start("umo-1", ev)   # 同一会话又起一个新任务
        await asyncio.sleep(0.1)     # 给旧任务 finally 执行的机会
        entry = n._tasks.get("umo-1")
        self.assertIsNotNone(entry, "新任务的记录被旧任务的 finally 删掉了——它会变成停不掉的孤儿")
        self.assertFalse(entry[0].done(), "新任务应该还活着")
        await n.stop_all()
        self.assertNotIn("umo-1", n._tasks)

    async def test_last_active_cleared_after_natural_timeout(self):
        n = InputStateNotifier(_Log(), interval=0.01, timeout=0.05)
        ev = FakeNapcatEvent()
        await n.start("umo-t", ev)
        await asyncio.sleep(1.3)
        self.assertNotIn("umo-t", n._tasks)
        self.assertNotIn("umo-t", n._last_active, "任务自然超时后 _last_active 要一起清")


class HookWiringTest(unittest.IsolatedAsyncioTestCase):
    async def test_hooks_start_stop_and_refresh(self):
        star = main_module.HumanoidCore.__new__(main_module.HumanoidCore)
        # 最小装配：只测钩子接线
        from humanoid.input_state import InputStateNotifier as N
        star._input_state = N(_Log(), interval=0.01, timeout=5.0)
        star._config_box = type("B", (), {"value": None})()
        star._config_box.value = type("C", (), {
            "input_state_enabled": True,
            "input_state_interval_seconds": 0.01,
            "input_state_timeout_seconds": 5.0,
        })()

        ev = FakeNapcatEvent()
        ev.unified_msg_origin = "aiocqhttp:private:10001"
        await star.show_input_state(ev, None)
        await asyncio.sleep(0.03)
        self.assertIn(ev.unified_msg_origin, star._input_state._tasks)

        await star.keep_input_state_before_send(ev)
        count = len(ev.bot.api.calls)
        self.assertGreaterEqual(count, 2, "发送前应该补过一帧")

        await star.hide_input_state(ev, None)
        self.assertNotIn(ev.unified_msg_origin, star._input_state._tasks)

    async def test_disabled_does_nothing(self):
        star = main_module.HumanoidCore.__new__(main_module.HumanoidCore)
        from humanoid.input_state import InputStateNotifier as N
        star._input_state = N(_Log(), interval=0.01, timeout=5.0)
        star._config_box = type("B", (), {"value": None})()
        star._config_box.value = type("C", (), {
            "input_state_enabled": False,
            "input_state_interval_seconds": 0.01,
            "input_state_timeout_seconds": 5.0,
        })()

        ev = FakeNapcatEvent()
        ev.unified_msg_origin = "aiocqhttp:private:10001"
        await star.show_input_state(ev, None)
        await asyncio.sleep(0.02)
        self.assertEqual(ev.bot.api.calls, [])
        self.assertEqual(star._input_state._tasks, {})


if __name__ == "__main__":
    unittest.main()
