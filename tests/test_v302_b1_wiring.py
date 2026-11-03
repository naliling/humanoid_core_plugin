"""B1 收尾：三处「越界」修复的守卫。

对应审计发现：

* 契约 13 —— 兄弟 bot 护栏的窗口/次数以前硬编码 600 秒 / 3 次，部署里改不了；
  而且调用方传的是墙钟 `time.time()`，回拨一次护栏就永久卡死。
* 配置 F3 —— `ccb_partner_length_cm` 出厂 13 < 上限 14 时「过深」这一档永不可达，
  面板上完全看不出来。
* 配置 F9 —— `mood_data_retention_days=0`（永不清理）会让 `stale_user_retention_days`
  静默失效，两项互相抵消却没有任何提示。
"""
from __future__ import annotations

import datetime
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from humanoid import ccb
from humanoid.config import HumanoidConfig
from humanoid.services.mood import MoodService
from humanoid.sibling_chat import SiblingChatGuard

from .fakes import RecordingLogger
from .test_main_integration import SKIP_REASON, main_module
from .test_services import TZ, build_store, cfg, FrozenClock  # noqa: F401
from .test_v2286_scene import _core


class SiblingGuardConfigTest(unittest.TestCase):
    """契约 13：窗口与次数来自配置，坏值退回默认。"""

    def test_configured_values_reach_the_guard(self):
        conf = HumanoidConfig.from_raw(
            {"sibling_window_seconds": 900, "sibling_max_turns": 5}
        )
        guard = SiblingChatGuard(
            window_seconds=conf.sibling_window_seconds,
            max_turns=conf.sibling_max_turns,
        )
        self.assertEqual(guard.window_seconds, 900.0)
        self.assertEqual(guard.max_turns, 5)
        now = 1_000.0
        self.assertEqual([guard.allow("r", "p", now) for _ in range(5)], [True] * 5)
        self.assertFalse(guard.allow("r", "p", now), "第 6 次必须拒绝")

    def test_defaults_are_unchanged(self):
        conf = HumanoidConfig.from_raw({})
        self.assertEqual(conf.sibling_window_seconds, 600.0)
        self.assertEqual(conf.sibling_max_turns, 3)

    def test_out_of_range_values_are_clamped_not_trusted(self):
        # 解析层把越界值夹到边界（不是退回默认）：0 秒窗口会让护栏「永远已过期」，
        # 负数次数会让它「永远拒绝」，两个方向都不能放进去。
        conf = HumanoidConfig.from_raw(
            {"sibling_window_seconds": 0, "sibling_max_turns": -2}
        )
        self.assertEqual(conf.sibling_window_seconds, 1.0)
        self.assertEqual(conf.sibling_max_turns, 1)

    def test_garbage_values_are_rejected_by_the_guard_itself(self):
        guard = SiblingChatGuard(window_seconds="坏值", max_turns=None)
        self.assertEqual(guard.window_seconds, 600.0)
        self.assertEqual(guard.max_turns, 3)

    def test_schema_declares_both_switches(self):
        import json

        path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
        schema = json.loads(path.read_text(encoding="utf-8"))
        for key in ("sibling_window_seconds", "sibling_max_turns"):
            with self.subTest(key=key):
                self.assertIn("minimum", schema[key])
                self.assertIn("maximum", schema[key])


class SiblingGuardClockTest(unittest.TestCase):
    """契约 13：默认时钟必须是单调时钟。墙钟回拨会让窗口恒真，护栏永久卡死。"""

    def test_missing_now_does_not_touch_the_wall_clock(self):
        guard = SiblingChatGuard()
        with mock.patch("humanoid.sibling_chat.time.time", side_effect=AssertionError("不许用墙钟")):
            self.assertTrue(guard.allow("role", "peer"))

    def test_main_passes_a_monotonic_stamp(self):
        path = Path(__file__).resolve().parent.parent / "main.py"
        src = path.read_text(encoding="utf-8")
        self.assertIn(
            "self._sibling_guard.allow(",
            src,
            "护栏的调用点还在（见下一行断言的具体实参）",
        )
        self.assertIn(
            "role_id, peer_id, time.monotonic(), self._umo_of(event)",
            src,
            "护栏的时间戳必须用单调时钟（墙钟回拨会让额度永久用完），"
            "并且额度要带会话（否则一个群聊满，另一个群也静默）",
        )


class CcbDepthWarnTest(unittest.TestCase):
    """配置 F3：长度够不到上限时，启动日志必须说清楚，而不是面板静默无效。"""

    def test_unreachable_over_depth_is_reported(self):
        core = _core(ccb_partner_length_cm=5.0, ccb_body_depth_cap_cm=14.0)
        self.assertFalse(ccb.depth_reachable(5.0, 14.0))
        core.ccb_partner("u1")
        text = core._log.text("warning")
        self.assertIn("过深", text)
        self.assertIn("永远到不了", text)

    def test_reachable_over_depth_is_silent(self):
        core = _core(ccb_partner_length_cm=15.0, ccb_body_depth_cap_cm=14.0)
        self.assertTrue(ccb.depth_reachable(15.0, 14.0))
        core.ccb_partner("u1")
        self.assertNotIn("永远到不了", core._log.text("warning"))

    def test_helpers_tolerate_bad_values(self):
        self.assertEqual(ccb.max_depth_cm("坏值"), 0.0)
        self.assertEqual(ccb.max_depth_cm(-3), 0.0)
        self.assertFalse(ccb.depth_reachable(0.0, 14.0))


class MoodRetentionWarnTest(unittest.TestCase):
    """配置 F9：字段级清理被关掉时，整条删除的静默期等于失效，必须提示。"""

    def _service(self, conf, logger):
        store = build_store(conf)
        return MoodService(
            store.scope,
            lambda: conf,
            FrozenClock(datetime.datetime(2026, 8, 22, 9, 0, tzinfo=TZ)),
            logger=logger,
        )

    def test_zero_field_retention_with_stale_pruning_warns(self):
        log = RecordingLogger()
        conf = cfg(mood_data_retention_days=0, stale_user_retention_days=90)
        service = self._service(conf, log)
        self.assertEqual(service.prune_expired(), 0)
        self.assertIn("完全失效", log.text("warning"))

    def test_both_zero_is_consistent_and_silent(self):
        log = RecordingLogger()
        conf = cfg(mood_data_retention_days=0, stale_user_retention_days=0)
        service = self._service(conf, log)
        self.assertEqual(service.prune_expired(), 0)
        self.assertNotIn("完全失效", log.text("warning"))


@unittest.skipIf(main_module is None, SKIP_REASON)
class SiblingGuardWiringTest(unittest.IsolatedAsyncioTestCase):
    """契约 13 端到端：改配置真的会改护栏行为（不是只存在 schema 里）。"""

    async def test_plugin_builds_the_guard_from_config(self):
        from .fakes import FakeContext, FakeProvider
        from .test_main_integration import GOOD_SCHEDULE

        tmp = Path(tempfile.mkdtemp())
        provider = FakeProvider("p1", reply=GOOD_SCHEDULE)
        ctx = FakeContext(chat_providers=[provider])
        raw = {
            "timezone_city": "北京",
            "schedule_provider_name": "p1",
            "state_flush_interval_seconds": 1,
            "sibling_window_seconds": 900,
            "sibling_max_turns": 5,
        }
        main_module.get_astrbot_data_path = lambda: str(tmp)
        star = main_module.HumanoidCore(ctx, raw)
        try:
            await star.initialize()
            self.assertEqual(star._sibling_guard.window_seconds, 900.0)
            self.assertEqual(star._sibling_guard.max_turns, 5)
        finally:
            await star.terminate()


if __name__ == "__main__":
    unittest.main()
