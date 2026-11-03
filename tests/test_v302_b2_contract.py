"""v3.0.3 / B2 —— 「不知道」要能说出来（契约 v3、信号三态、门面三态）。

B2 这一批的公共根是审计里的 R3：**契约没有「不可用」的表达位**。于是

* 身体轴关掉时导出 `hunger=0.0 / arousal=50.0 / asleep=false`，与真值同形；
* 社交层的信号文件读坏 / 过期时返回上一次的旧数据或空表；
* 门面的 `_user()` 在没有档案时把**配置初始值**当这个人的好感返回；
* `intimacy()` 把「白名单拒绝」与「拿不到」都返回 `{}`。

这些都不是「数值算错了」，而是**形状错了**：接收方没法区分「确定是 0」和「我不知道」。
本文件锁住的就是形状：块级 `available/reason`、数值 `null`、拒绝 `{"status":"denied"}`、
档案 `known/affection_source`。
"""

from __future__ import annotations

import json
import os
import time
import unittest

from humanoid import api
from humanoid import contract as _contract
from humanoid.link import (
    CONTRACT_SCHEMA,
    CONTRACT_VERSION,
    SIGNALS_FILE_NAME,
    SIGNALS_TTL_SECONDS,
    build_contract,
    social_data_dir,
)

from .test_link import Harness
from .test_v2287_ccb_visible import FEMALE, _core, _mood, _ready


def _facade(core):
    roles = type("R", (), {"get_all": lambda self: [core]})()
    return api.CoreFacade(roles, None)


def _write_signals(harness, payload, *, age_seconds: float = 0.0, raw: str = ""):
    directory = social_data_dir(harness.root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / SIGNALS_FILE_NAME
    path.write_text(raw or json.dumps(payload), encoding="utf-8")
    if age_seconds:
        stamp = path.stat().st_mtime - age_seconds
        os.utime(path, (stamp, stamp))
    return path


class ContractVersionTest(unittest.TestCase):
    """契约 v3：数值域里出现 null，版本号必须跟着抬。"""

    def setUp(self):
        self.harness = Harness()
        self.core = self.harness.roles.get_or_create("bot1")

    def tearDown(self):
        self.harness.store.flush_sync()

    def test_version_and_schema_are_bumped(self):
        contract = build_contract(self.core)
        self.assertEqual(contract["v"], CONTRACT_VERSION)
        self.assertEqual(CONTRACT_VERSION, 3, "body 里出现 null 是破坏性变更，v 必须 >2")
        self.assertEqual(contract["schema"], CONTRACT_SCHEMA)
        self.assertEqual(CONTRACT_SCHEMA, "humanoid.contract/3")

    def test_capabilities_did_not_grow_a_fake_block(self):
        """三态是**读法**，不是新块：capabilities 里每多一个名字，消费方就会去找那个块。"""
        contract = build_contract(self.core)
        caps = contract.get("capabilities") or []
        self.assertNotIn("tristate", caps)
        self.assertEqual(len(caps), len(set(caps)), "能力表不能有重复项")

    def test_time_city_is_always_a_string(self):
        """地名不知道时写空串，不写 null：按字符串读的消费方很多。"""
        contract = build_contract(self.core)
        self.assertIsInstance(contract["time"]["city"], str)


class ContractBodyTriStateTest(unittest.TestCase):
    """身体轴关掉 = 不知道，不是「数值恰好是 0 / 50 / 醒着」。"""

    def test_disabled_body_is_marked_and_null(self):
        harness = Harness({"timezone_city": "北京", "soma_enabled": False})
        core = harness.roles.get_or_create("bot1")
        try:
            body = build_contract(core)["body"]
            self.assertIs(body["available"], False)
            self.assertEqual(body["reason"], "soma_disabled")
            for key in ("hunger", "arousal", "asleep", "sleep_pressure"):
                self.assertIsNone(body[key], key)
            # 三态读法：拿到的是「不知道」哨兵，而不是 0/False
            self.assertIs(_contract.tri(body, "hunger"), _contract.UNKNOWN)
            self.assertIs(_contract.tri_bool(body, "asleep"), _contract.UNKNOWN)
            self.assertFalse(_contract.is_known(_contract.tri_number(body, "arousal")))
        finally:
            harness.store.flush_sync()

    def test_live_body_is_available_and_readable(self):
        harness = Harness()
        core = harness.roles.get_or_create("bot1")
        try:
            core.soma._set("hunger", 65.0)
            body = build_contract(core)["body"]
            self.assertIs(body["available"], True)
            self.assertEqual(_contract.unavailable_reason(body), "")
            self.assertEqual(_contract.tri_number(body, "hunger"), 65.0)
        finally:
            harness.store.flush_sync()


class SignalsTriStateTest(unittest.TestCase):
    """信号文件：读坏 / 过期 / 不在，三种「读不到」都要带原因。"""

    def build(self):
        harness = Harness()
        return harness, harness.roles.get_or_create("bot1")

    def test_corrupt_file_is_not_an_empty_dict(self):
        harness, core = self.build()
        try:
            _write_signals(harness, {}, raw="{不是 JSON")
            data = core.signals.read()
            self.assertEqual(data, _contract.unavailable("corrupt"))
            self.assertIs(_contract.available(data), False)
            self.assertEqual(core.signals.status()["state"], "unreadable")
        finally:
            harness.store.flush_sync()

    def test_stale_file_is_not_the_old_payload(self):
        harness, core = self.build()
        try:
            _write_signals(harness, {"last_proactive_at": time.time()},
                           age_seconds=SIGNALS_TTL_SECONDS + 3600)
            data = core.signals.read()
            self.assertEqual(data, _contract.unavailable("stale"))
            self.assertEqual(core.signals.status()["state"], "stale")
        finally:
            harness.store.flush_sync()

    def test_missing_file_is_not_an_empty_dict(self):
        harness, core = self.build()
        try:
            data = core.signals.read()
            self.assertIs(_contract.available(data), False)
            # 没有数据根 / 文件不在，都是「不知道」，但原因要分得开
            self.assertIn(_contract.unavailable_reason(data), ("missing", "no_data_root"))
        finally:
            harness.store.flush_sync()

    def test_fresh_file_reads_like_the_old_shape(self):
        """读得到时**不**加标记：旧消费方拿到的还是它原本那份字典。"""
        harness, core = self.build()
        try:
            _write_signals(harness, {"last_proactive_at": 123.0})
            data = core.signals.read()
            self.assertNotIn("available", data)
            self.assertEqual(core.signals.status()["state"], "fresh")
        finally:
            harness.store.flush_sync()


class SelfStateTriStateTest(unittest.TestCase):
    """门面的 self_state：睡着可以是「不知道」，而不是 `False`。"""

    def test_live_state_is_available(self):
        core = _core(True)
        _mood(core)
        contract = build_contract(core)
        got = _facade(core)._self_state(core, contract, contract["body"])
        self.assertIs(got["available"], True)
        self.assertIsInstance(got["asleep"], bool)
        for row in got["feelings"]:
            self.assertEqual(row["salience_source"], "rank_estimate",
                             "显著度是按名次估的，必须标出来，别让消费方当成实测值")

    def test_soma_disabled_says_unavailable(self):
        core = _core(True, soma_enabled=False)
        contract = build_contract(core)
        got = _facade(core)._self_state(core, contract, contract["body"])
        self.assertIs(got["available"], False)
        self.assertTrue(got["reason"])
        self.assertIsNone(got["asleep"], "关掉身体轴时睡着是不知道，不是醒着")


class WakeTriStateTest(unittest.TestCase):
    """wake 段要说明它从哪来（现算 / 契约 / 都没有）。"""

    def test_live_wake_is_marked_live(self):
        core = _core(True)
        contract = build_contract(core)
        got = _facade(core)._wake(core, contract, contract["body"])
        self.assertIs(got["available"], True)
        self.assertEqual(got["source"], "live")

    def test_no_contract_and_no_soma_is_unavailable(self):
        core = _core(True, soma_enabled=False)
        contract = build_contract(core)
        got = _facade(core)._wake(core, contract, contract["body"])
        self.assertIs(got["available"], False)
        self.assertEqual(got["source"], "unavailable")


class FacadeIntimacyTriStateTest(unittest.TestCase):
    """白名单拒绝 ≠ 拿不到。"""

    def test_whitelist_refusal_is_denied(self):
        core = _core(True, ccb_allowed_users=["10001"])
        _mood(core, affection=92.0)
        self.assertEqual(_facade(core).intimacy("bot1", "u1"), {"status": "denied"})

    def test_ok_path_carries_status_ok(self):
        core = _core(True)
        _ready(core)
        got = _facade(core).intimacy("bot1", "u1")
        self.assertEqual(got.get("status"), "ok")
        self.assertIn("stage", got)

    def test_missing_user_is_still_an_empty_dict(self):
        core = _core(True)
        self.assertEqual(_facade(core).intimacy("bot1", ""), {})


class FacadeKnownTest(unittest.TestCase):
    """没有档案时给的是配置初始值，必须标 known=false。"""

    def test_unknown_user_is_marked_as_default(self):
        core = _core(True)
        got = _facade(core)._user(core, "bot1", "stranger")
        self.assertIs(got["known"], False)
        self.assertEqual(got["affection_source"], "config_default")

    def test_existing_record_is_marked_as_record(self):
        core = _core(True)
        _mood(core)
        got = _facade(core)._user(core, "bot1", "u1")
        self.assertIs(got["known"], True)
        self.assertEqual(got["affection_source"], "record")
        self.assertEqual(got["affection"], 92.0)

    def test_relationship_carries_known_too(self):
        core = _core(True)
        got = _facade(core)._relationship(core, "stranger")
        self.assertIs(got["known"], False)
        self.assertEqual(got["affection_source"], "config_default")


class CcbBodyRebuildTest(unittest.TestCase):
    """面板上改了她身体的参数，档案必须跟着重建（否则改了永远不生效）。"""

    def test_config_change_rebuilds_the_profile(self):
        core = _core(True, ccb_body_depth_cap_cm=16.0)
        self.assertEqual(core.ccb_body()["depth_cap_cm"], 16.0)
        from humanoid.config import HumanoidConfig

        core._config_provider = lambda: HumanoidConfig.from_raw(
            {"timezone_city": "北京", "ccb": True, "ccb_body_depth_cap_cm": 9.0}
        )
        self.assertEqual(core.ccb_body()["depth_cap_cm"], 9.0)

    def test_sensitivity_zero_is_not_swallowed(self):
        core = _core(True, ccb_body_sensitivity=0)
        self.assertEqual(core.ccb_body()["sensitivity"], 0)


class OutfitSourceTest(unittest.TestCase):
    """面板要说出这句穿着是从哪来的：人设原文 ≠ 按物种自动挑。"""

    def test_persona_clothing_is_reported_as_persona(self):
        core = _core(True, persona=FEMALE + "她今天穿着一件米白色的针织开衫。")
        self.assertEqual(core.outfit_info()["source"], "人设（原文）")
        self.assertIn("针织开衫", core.outfit_text())

    def test_unknown_kind_is_not_reported_as_auto(self):
        core = _core(True, persona="你是小满。")
        info = core.outfit_info()
        self.assertEqual(info["kind"], "")
        self.assertNotIn("自动（按物种", info["source"])
        self.assertEqual(info["text"], "", "判不出物种又不该编一件衣服出来")
