"""契约导出与社交层信号。

联动最容易坏在两个地方：契约字段被改掉导致对方静默读空，以及信号文件过期后还在影响身体。
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.config import ConfigBox
from humanoid.engine import HumanoidEngine
from humanoid.link import (
    CONTRACT_VERSION,
    SIGNALS_FILE_NAME,
    SOCIAL_PLUGIN_DIR_NAME,
    build_contract,
    signals_path,
)
from humanoid.llm import LLMGateway, ProviderResolver
from humanoid.role_manager import RoleManager
from humanoid.state import StateStore

from .fakes import FakeContext, RecordingLogger

TZ = ZoneInfo("Asia/Shanghai")


class Harness:
    """一个带 data_root 的最小可运行插件实例。"""

    def __init__(self, raw=None, moment=None) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / "plugin_data" / "humanoid_core").mkdir(parents=True)
        self.box = ConfigBox(raw or {"timezone_city": "北京"})
        log = RecordingLogger()
        self.store = StateStore(
            self.root / "plugin_data" / "humanoid_core" / "state.json", lambda: 1.0, log
        )
        self.store.load((moment or datetime.now(TZ)).strftime("%Y-%m-%d"), self.box.value.cycle_length)
        ctx = FakeContext()
        resolver = ProviderResolver(ctx, log)
        gateway = LLMGateway(resolver, self.box, log)
        self.roles = RoleManager(
            self.store, self.box, log, resolver, gateway, None, data_root=self.root
        )
        self.engine = HumanoidEngine(ctx, self.box, self.root, log, None, self.roles)
        self.log = log


class ContractTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.core = self.harness.roles.get_or_create("bot1")
        # 给身体一点可观察的状态。
        self.core.soma._set("sleep_pressure", 78.0)
        self.core.soma._set("hunger", 65.0)

    def tearDown(self):
        self.harness.store.flush_sync()

    def test_contract_shape_is_stable(self):
        contract = build_contract(self.core)
        self.assertEqual(contract["v"], CONTRACT_VERSION)
        for key in ("time", "body", "feelings", "form", "activity", "weather", "paths", "routine"):
            self.assertIn(key, contract)
        for key in (
            "energy", "sleep_pressure", "sleep_debt", "hunger", "discomfort",
            "arousal", "asleep", "social_energy", "social_desire", "cycle_day",
        ):
            self.assertIn(key, contract["body"], f"契约 body 少了 {key}")
        self.assertEqual(contract["body"]["sleep_pressure"], 78.0)
        self.assertIn("困意上来了", " ".join(contract["feelings"]))
        self.assertIn("有点饿了", " ".join(contract["feelings"]))
        self.assertLessEqual(contract["form"]["max_chars"], 60, "困成这样还允许长篇说明 form 没联动")

    def test_contract_carries_clock_offset_and_routine(self):
        """社交层跟 Core 跑在同一台机器上：不导出偏移，它就只能拿本机时钟判断作息。"""
        contract = build_contract(self.core)
        self.assertEqual(contract["time"]["utc_offset_minutes"], 8 * 60)
        routine = contract["routine"]
        self.assertEqual(routine["night_start_hour"], 23)
        self.assertEqual(routine["night_end_hour"], 6)
        self.assertEqual(routine["night_span_hours"], 7.0)
        self.assertEqual(routine["sleep_need_hours"], 8.0)
        self.assertEqual(routine["wake_at"], "06:00", "日程里的起床点应该贴到夜间窗口结束")
        self.assertTrue(routine["sleep_spans"], "契约里该看得到她今晚几点睡到几点")

    def test_contract_is_written_into_state_and_only_on_change(self):
        first = self.core.refresh_contract()
        self.assertIsNotNone(first)
        self.store_dirty_after = self.harness.store.dirty
        self.harness.store.flush_sync()
        second = self.core.refresh_contract()
        self.assertEqual(second["body"], first["body"])
        self.assertFalse(self.harness.store.dirty, "身体没变化时重复刷新不该再写盘")
        self.core.soma._set("sleep_pressure", 91.0)
        third = self.core.refresh_contract()
        self.assertNotEqual(third["body"]["sleep_pressure"], first["body"]["sleep_pressure"])
        self.assertTrue(self.harness.store.dirty)

    def test_contract_can_be_disabled(self):
        self.box_off = self.harness.box
        self.box_off.raw["contract_enabled"] = False
        self.box_off.reload()
        self.assertIsNone(self.core.refresh_contract())

    def test_no_contract_when_body_is_off(self):
        """生理层关着时不能导出契约。

        轴全是初始值，社交层读到 social_desire=0 会把她当成「刚聊完不想说话」而长期
        压住主动消息。不导出，它就退回旧字段，行为与 v1.7.4 一致。
        """
        harness = Harness({"timezone_city": "北京", "soma_enabled": False})
        core = harness.roles.get_or_create("bot1")
        self.assertIsNone(core.refresh_contract())
        self.assertIsNone(core._scope.get_self("contract"))

    def test_signals_path_layout(self):
        social_dir = self.harness.root / "plugin_data" / SOCIAL_PLUGIN_DIR_NAME
        self.assertIsNone(signals_path(self.harness.root))
        social_dir.mkdir(parents=True, exist_ok=True)
        (social_dir / SIGNALS_FILE_NAME).write_text('{"last_proactive_at": 1}', encoding="utf-8")
        self.assertEqual(
            signals_path(self.harness.root), social_dir / SIGNALS_FILE_NAME
        )


class SignalsTest(unittest.TestCase):
    def write_signals(self, payload: dict, mtime_age: float = 0.0) -> Path:
        root = Path(tempfile.mkdtemp())
        social = root / "plugin_data" / SOCIAL_PLUGIN_DIR_NAME
        social.mkdir(parents=True)
        path = social / SIGNALS_FILE_NAME
        path.write_text(json.dumps(payload), encoding="utf-8")
        if mtime_age:
            stamp = time.time() - mtime_age
            import os
            os.utime(path, (stamp, stamp))
        return root

    def test_missing_file_reads_empty(self):
        from humanoid.link import SocialSignals

        signals = SocialSignals(lambda: Path(tempfile.mkdtemp()))
        self.assertEqual(signals.read(), {})
        self.assertEqual(signals.last_proactive(), ("", 0.0))
        self.assertEqual(signals.ignored_streak(), 0)

    def test_stale_file_is_ignored(self):
        """社交层停了三小时后，它写的「刚主动找过谁」不该继续压着身体。"""
        from humanoid.link import SocialSignals

        root = self.write_signals({"last_proactive_at": 1_800_000_000.0, "ignored_streak": 4}, mtime_age=10_800)
        signals = SocialSignals(lambda: root)
        self.assertEqual(signals.read(), {}, "超过 TTL 的信号必须作废")
        self.assertEqual(signals.ignored_streak(), 0)

    def test_garbage_file_does_not_crash(self):
        from humanoid.link import SocialSignals

        root = Path(tempfile.mkdtemp())
        social = root / "plugin_data" / SOCIAL_PLUGIN_DIR_NAME
        social.mkdir(parents=True)
        (social / SIGNALS_FILE_NAME).write_text("not json at all", encoding="utf-8")
        signals = SocialSignals(lambda: root)
        self.assertEqual(signals.read(), {})

    def test_proactive_signal_drains_desire_once(self):
        harness = Harness()
        core = harness.roles.get_or_create("bot1")
        root = harness.root / "plugin_data" / SOCIAL_PLUGIN_DIR_NAME
        root.mkdir(parents=True, exist_ok=True)
        path = root / SIGNALS_FILE_NAME
        moment = datetime(2026, 8, 22, 15, 0, tzinfo=TZ)
        payload = {"last_proactive_at": moment.timestamp(), "last_target_uid": "42", "ignored_streak": 3}
        path.write_text(json.dumps(payload), encoding="utf-8")

        core.soma._set("social_desire", 90.0)
        core._apply_social_signals()
        drained = core.soma.snapshot()["social_desire"]
        self.assertLess(drained, 60.0, "主动说过一句就该泄掉一大截")
        self.assertEqual(int(core.soma.data["ignored_streak"]), 3)

        # 同一条信号不该被反复消费。
        core.soma._set("social_desire", 90.0)
        core._apply_social_signals()
        self.assertEqual(core.soma.snapshot()["social_desire"], 90.0, "旧信号被重复消费了")

    def test_ignored_streak_becomes_a_feeling(self):
        harness = Harness()
        core = harness.roles.get_or_create("bot1")
        core.soma.set_social_feedback(3)
        texts = [text for _, text in core.soma.feelings(70.0)]
        self.assertTrue(any("没回" in text for text in texts), texts)
        core.soma.set_social_feedback(0)
        texts = [text for _, text in core.soma.feelings(70.0)]
        self.assertFalse(any("没回" in text for text in texts))


class InjectionBudgetTest(unittest.TestCase):
    """注入块每次聊天请求都会追加，必须有个硬上限，而且不能靠「禁止提及数值」补。"""

    def build(self, raw=None):
        harness = Harness({"timezone_city": "北京", "weather_api_key": "0" * 16, **(raw or {})})
        core = harness.roles.get_or_create("bot1")
        core.soma._set("sleep_pressure", 99.0)
        core.soma._set("sleep_debt", 18.0)
        core.soma._set("hunger", 99.0)
        core.soma._set("discomfort", 95.0)
        core.soma._set("arousal", 100.0)
        core.soma._set("social_desire", 100.0)
        core.mood.set_nickname("42", "一个长得离谱的昵称" * 12)
        core.behavior.add_event("42", {
            "type": "long_gap", "timestamp": core.soma.now, "importance": 1.0,
            "data": {"gap_bucket": "very_long_return", "gap_seconds": 90000,
                     "previous_message": "明天上午我要去面试你能不能陪我聊聊" * 20},
        })
        return harness, core

    def test_every_mode_stays_under_its_cap(self):
        from humanoid.prompt_builder import INJECT_MAX_CHARS, estimate_tokens

        for mode, cap in INJECT_MAX_CHARS.items():
            harness, core = self.build({"inject_activity_context": mode})
            text = core.build_injection("42", is_group=False)
            with self.subTest(mode=mode):
                self.assertLessEqual(len(text), cap, f"{mode} 档注入 {len(text)} 字符，上限 {cap}")
                self.assertLessEqual(estimate_tokens(text), 1000, f"{mode} 档 token 超标")
                # 截断要落在块边界上，不能留半句话给模型去补
                self.assertFalse(text.endswith("：") or text.endswith("、"), text[-20:])

    def test_config_help_text_never_reaches_the_model(self):
        """天气没配好时，给管理员看的说明书不该进 prompt。"""
        harness, core = self.build({"inject_activity_context": "full"})
        text = core.build_injection("42", is_group=False)
        for leak in ("weather_location", "timezone_city", "没配天气城市", "API Key"):
            self.assertNotIn(leak, text, f"配置提示漏进了模型上下文：{leak}")

    def test_injection_has_no_guidance_sentences(self):
        """v2.16：注入里只有事实。旧版那两句「上面这些是你的身体…」「你自己定」全部拿掉——
        它们也是插件在告诉她该怎么做，而模型会把它们当任务。"""
        harness, core = self.build({"inject_activity_context": "low"})
        text = core.build_injection("42", is_group=False)
        for word in ("不是要你", "上面这些", "你自己定", "不必逐条", "只供你参考", "台词"):
            self.assertNotIn(word, text, f"注入里又长出指导句：{word}")
        self.assertIn("【时间】", text)


class DiagnosticsTest(unittest.TestCase):
    def test_report_includes_body_and_linkage(self):
        harness = Harness()
        core = harness.roles.get_or_create("bot1")
        core.refresh_contract()
        text = harness.engine.diagnostics_text(core)
        self.assertIn("身体与联动", text)
        self.assertIn("联动契约", text)
        self.assertIn("社交层信号", text)
        self.assertIn("Token 预算", text)
        self.assertIn("实测", text)

    def test_report_shows_routine_and_wake_time(self):
        harness = Harness()
        core = harness.roles.get_or_create("bot1")
        text = harness.engine.diagnostics_text(core)
        self.assertIn("【作息】", text)
        self.assertIn("生物钟夜 23:00 → 06:00", text)
        self.assertIn("今日日程里的起床时间：06:00", text)

    def test_report_flags_a_window_shorter_than_the_sleep_she_needs(self):
        harness = Harness({"timezone_city": "北京", "night_start_hour": 23, "night_end_hour": 5})
        core = harness.roles.get_or_create("bot1")
        text = harness.engine.diagnostics_text(core)
        self.assertIn("窗口比她需要的睡眠短", text)

    def test_report_says_so_when_body_disabled(self):
        harness = Harness({"timezone_city": "北京", "soma_enabled": False})
        core = harness.roles.get_or_create("bot1")
        text = harness.engine.diagnostics_text(core)
        self.assertIn("生理层已关闭", text)


if __name__ == "__main__":
    unittest.main()
