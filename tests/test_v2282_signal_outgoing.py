"""v2.28.2：主动消息的原文走信号桥补记进 Core（钩子看不到它）。

## 为什么

v2.28.1 用 `after_message_sent` 钩子去接「别的插件用 `context.send_message` 发出的
主动消息」。但这个钩子只在 AstrBot 的 respond 阶段末尾触发；主动消息走的是
`StarContext.send_message` → `platform.send_by_session`，**根本不经过 respond 阶段**。
于是 2.28.1 里那条「Core 现在知道她主动说了什么」的功能实际从未触发。

## 修法

社交插件（v1.28.3 起）把最近一次主动开口的**原文**写进
`humanoid_signals.json` 的 `last_proactive_text`；Core 在下一轮取信号时：
- 只在 `last_proactive_at` 前进时记一次；
- 原文缺失（旧版社交插件）就不记，不拿占位文本污染 `spoke_log`；
- `last_interaction` 只往后推，不覆盖用户后来的发言。

本文件盯的就是这四条。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from humanoid.config import HumanoidConfig
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.state import StateStore

from .fakes import FakeContext, RecordingLogger


SOCIAL_DIR = "plugin_data/astrbot_plugin_autonomous_social"
SIGNALS_NAME = "humanoid_signals.json"


def make_core(root: Path, **conf):
    store = StateStore(root / "state.json", lambda: 0.01)
    store.load("2026-08-22", 28)
    config = HumanoidConfig.from_raw(
        {"timezone_city": "北京", "soma_enabled": True, "mood_enabled": True, **conf}
    )
    return HumanoidCoreInstance(
        role_id="bot1",
        state_store=store,
        config_provider=lambda: config,
        logger=RecordingLogger(),
        stop_event=asyncio.Event(),
        resolver=FakeContext(),
        gateway=None,
        data_root=str(root),
    )


def write_signals(root: Path, **fields) -> None:
    path = root / SOCIAL_DIR / SIGNALS_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": 2, "by_bid": {"bot1": fields}}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class SocialOutgoingSignalTest(unittest.TestCase):
    def test_proactive_text_reaches_spoke_log(self) -> None:
        root = Path(tempfile.mkdtemp())
        core = make_core(root)
        at = time.time() - 5.0
        write_signals(
            root,
            last_proactive_at=at,
            last_target_uid="u9",
            last_proactive_text="刚下班，今天累死了",
        )
        core._apply_social_signals()
        rows = core.mood.spoke("u9")
        self.assertEqual([r["text"] for r in rows], ["刚下班，今天累死了"])
        self.assertAlmostEqual(float(rows[-1]["at"]), at, places=3)
        self.assertTrue(rows[-1]["pending"], "主动发出去、对方还没回，应当是 pending")

    def test_her_said_at_advances_to_the_send_time(self) -> None:
        """v3.0.0（联动 P2-3）：她主动开口推进 her_said_at。

        last_interaction 保持兼容语义 = 「TA 上次说话」，不能被她的主动消息推进；
        否则社交层会把「她刚发过消息」误当成「对方刚说过话」，掩盖真实沉默时长。
        """
        root = Path(tempfile.mkdtemp())
        core = make_core(root)
        at = time.time() - 30.0
        core.scope.set_user("u9", "last_interaction", at - 3600.0)
        write_signals(root, last_proactive_at=at, last_target_uid="u9",
                      last_proactive_text="在吗")
        core._apply_social_signals()
        her = float(core.scope.get_user("u9", "her_said_at", 0) or 0)
        self.assertAlmostEqual(her, at, places=3)
        peer = float(core.scope.get_user("u9", "last_interaction", 0) or 0)
        self.assertAlmostEqual(
            peer, at - 3600.0, places=3,
            msg="last_interaction 是 TA 上次说话，不能被她的主动消息推进",
        )

    def test_it_does_not_rewind_a_newer_her_said_at(self) -> None:
        """信号可能晚一轮才被取到：不能把更新的「她说话」时间戳推回去。"""
        root = Path(tempfile.mkdtemp())
        core = make_core(root)
        at = time.time() - 600.0
        newer = at + 300.0
        core.scope.set_user("u9", "her_said_at", newer)
        write_signals(root, last_proactive_at=at, last_target_uid="u9",
                      last_proactive_text="在吗")
        core._apply_social_signals()
        stamp = float(core.scope.get_user("u9", "her_said_at", 0) or 0)
        self.assertAlmostEqual(stamp, newer, places=3)

    def test_same_signal_is_not_recorded_twice(self) -> None:
        root = Path(tempfile.mkdtemp())
        core = make_core(root)
        at = time.time() - 5.0
        write_signals(root, last_proactive_at=at, last_target_uid="u9",
                      last_proactive_text="同一句")
        core._apply_social_signals()
        core._apply_social_signals()
        self.assertEqual(len(core.mood.spoke("u9")), 1)

    def test_old_social_plugin_without_text_is_ignored(self) -> None:
        """旧版社交插件没有 last_proactive_text：只当没发生，不写占位句。"""
        root = Path(tempfile.mkdtemp())
        core = make_core(root)
        write_signals(root, last_proactive_at=time.time() - 5.0, last_target_uid="u9")
        core._apply_social_signals()
        self.assertEqual(core.mood.spoke("u9"), [])

    def test_soma_off_still_records_the_message(self) -> None:
        """这句话归情绪账本，不归身体层：关掉生理层也该记。"""
        root = Path(tempfile.mkdtemp())
        core = make_core(root, soma_enabled=False)
        at = time.time() - 5.0
        write_signals(root, last_proactive_at=at, last_target_uid="u9",
                      last_proactive_text="我先睡了")
        core._apply_social_signals()
        self.assertEqual([r["text"] for r in core.mood.spoke("u9")], ["我先睡了"])

    def test_top_level_single_bot_signal_still_works(self) -> None:
        """单 Bot 时社交层写顶层（不是 by_bid）：Core 也要读得到。"""
        root = Path(tempfile.mkdtemp())
        core = make_core(root)
        at = time.time() - 5.0
        path = root / SOCIAL_DIR / SIGNALS_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "schema": 2,
                "last_proactive_at": at,
                "last_target_uid": "u9",
                "last_proactive_text": "晚安",
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        core._apply_social_signals()
        self.assertEqual([r["text"] for r in core.mood.spoke("u9")], ["晚安"])


if __name__ == "__main__":
    unittest.main()
