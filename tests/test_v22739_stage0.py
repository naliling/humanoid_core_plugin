"""阶段 0 回归：三处「静默失效」——都不会报错，只会悄悄不工作。

1. `soma_enabled=False` 时 `_apply_social_signals()` 根本不跑（唯一调用点在闸门里），
   于是关掉身体层的人连「跨轮还记得什么」一起失去。函数自己的注释写着「刻意放在
   闸门之前」，可调用点在里面——注释对、代码错，这类最难被发现。
2. `ccb_coerced_at` 写进 `users.<uid>.mood`、读却从 `users.<uid>` 顶层取，两侧永不
   相等，「刚被强迫 → 本轮不进场景」这道安全阀恒为 False。
3. `open_items()` 把「写方没写这个键」和「写方说没有了」都返回成空表，于是一个还没
   写 `open_items` 的旧版社交层，会让 Core 每轮把她的未完事项整本清空一次。
"""

from __future__ import annotations

import json
import os
import time
import unittest

from humanoid.emotion import ccb_coerced_within
from humanoid.link import SIGNALS_FILE_NAME, SOCIAL_PLUGIN_DIR_NAME, SocialSignals

from .test_v2273_life_note import NOW, _Harness


def write_raw_signals(h: _Harness, payload: dict) -> None:
    """写一份**新鲜**的信号文件，但不触发任何摄取——让调用方自己走真路径。"""
    social = h.root / "plugin_data" / SOCIAL_PLUGIN_DIR_NAME
    social.mkdir(parents=True, exist_ok=True)
    path = social / SIGNALS_FILE_NAME
    path.write_text(json.dumps(payload), encoding="utf-8")
    stamp = time.time()
    os.utime(path, (stamp, stamp))


class SocialSignalsRunWithoutSomaTest(unittest.IsolatedAsyncioTestCase):
    """M3：社交信号不归身体层管。"""

    async def test_ledger_is_ingested_when_soma_is_off(self):
        h = _Harness({"timezone_city": "北京", "soma_enabled": False})
        try:
            self.assertFalse(h.core.config.soma_enabled)
            write_raw_signals(h, {"by_bid": {"bot1": {"open_items": [
                {"text": "甲", "uid": "10001", "at": h.core.now_epoch()}
            ]}}})
            # 走**真路径**（后台循环的那一格），而不是直接调 _apply_social_signals
            await h.core._body_step()
            self.assertIn("甲", h.block())
        finally:
            h.store.flush_sync()

    async def test_ignored_by_reaches_the_mood_ledger_when_soma_is_off(self):
        h = _Harness({"timezone_city": "北京", "soma_enabled": False})
        try:
            h.core.mood.profile("10001")  # 先让这个人存在于本角色下
            write_raw_signals(h, {"by_bid": {"bot1": {
                "open_items": [],
                "ignored_by": {"10001": 2},
            }}})
            await h.core._body_step()
            seen = float(h.core._scope.user_state("10001").get("ignored_seen", 0) or 0)
            self.assertEqual(seen, 2.0, "关掉身体层时「被冷落」也该记进好感账本")
        finally:
            h.store.flush_sync()

    async def test_ledger_still_works_when_soma_is_on(self):
        """对照组：开着身体层时行为不变。"""
        h = _Harness({"timezone_city": "北京"})
        try:
            self.assertTrue(h.core.config.soma_enabled)
            write_raw_signals(h, {"by_bid": {"bot1": {"open_items": [
                {"text": "乙", "uid": "10001", "at": h.core.now_epoch()}
            ]}}})
            await h.core._body_step()
            self.assertIn("乙", h.block())
        finally:
            h.store.flush_sync()


class CcbCoercedLayerTest(unittest.TestCase):
    """M2：安全阀读的位置必须和写的位置一致。"""

    def test_mood_sub_dict_is_read(self):
        """写侧实际写的就是这里（`services/mood.py::_apply_intimacy_request`）。"""
        state = {"mood": {"affection": 50.0, "ccb_coerced_at": NOW - 60}}
        self.assertTrue(ccb_coerced_within(state, NOW))

    def test_top_level_is_also_read(self):
        """未来迁移到顶层时仍然要认。"""
        state = {"ccb_coerced_at": NOW - 60, "mood": {"affection": 50.0}}
        self.assertTrue(ccb_coerced_within(state, NOW))

    def test_old_stamp_is_not_coerced(self):
        state = {"mood": {"ccb_coerced_at": NOW - 7200}}
        self.assertFalse(ccb_coerced_within(state, NOW))

    def test_missing_or_broken_stamp_is_not_coerced(self):
        for state in ({}, {"mood": {}}, {"mood": {"ccb_coerced_at": "x"}},
                      {"mood": {"ccb_coerced_at": float("nan")}}, None):
            self.assertFalse(ccb_coerced_within(state, NOW), state)


class OpenItemsMissingKeyTest(unittest.TestCase):
    """M7：「写方没写这个键」不能当成「全结了」。"""

    def setUp(self) -> None:
        self.h = _Harness()
        self.core = self.h.core

    def tearDown(self) -> None:
        self.h.store.flush_sync()

    def _signals(self, payload: dict) -> SocialSignals:
        write_raw_signals(self.h, payload)
        return self.core.signals

    def test_missing_key_returns_none(self):
        signals = self._signals({"by_bid": {"bot1": {"ignored_by": {}}}})
        self.assertIsNone(signals.open_items("bot1"), "没写这个键 = 不知道，不是空表")

    def test_present_empty_list_returns_empty(self):
        signals = self._signals({"by_bid": {"bot1": {"open_items": []}}})
        self.assertEqual(signals.open_items("bot1"), [])

    def test_missing_key_does_not_wipe_the_ledger(self):
        self.h.publish("甲")
        self.assertIn("甲", self.h.block())
        # 换成一份新鲜、但**没有 open_items 键**的信号（旧版社交层的样子）
        write_raw_signals(self.h, {"by_bid": {"bot1": {"ignored_by": {}}}})
        self.core._apply_social_signals()
        self.assertIn("甲", self.h.block(), "缺键不该被当成「全结了」")

    def test_present_empty_list_still_ends_items(self):
        """对照：写方明确写了空表，那就是「确实没有了」——整体替换的语义不变。"""
        self.h.publish("甲")
        write_raw_signals(self.h, {"by_bid": {"bot1": {"open_items": []}}})
        self.core._apply_social_signals()
        self.assertNotIn("甲", self.h.block())


if __name__ == "__main__":
    unittest.main()
