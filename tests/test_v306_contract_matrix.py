"""v3.0.6 / B5 —— 契约消费矩阵：每个门面入口都必须把「不知道」说出来。

B5 不是修某个数值，而是把**读法**变成可复跑的断言。历史教训：R3 之前所有降级
（soma 关掉、契约没生成、信号文件读坏、没有档案）都被编码成与真值同形的
0 / 50.0 / False，于是消费方（别的插件、门面自己）读到的永远是「一切正常」。

本文件分三层：

1. SentinelTest：humanoid/contract.py 的三态原语本身——UNKNOWN 必须与
   真值 0 / False / 50.0 可区分；缺键 / 块不可用必须是 UNKNOWN，而不是默认值。
2. Facade*Test：humanoid/api.py 每个入口在「角色不存在 / 契约缺失 / 契约损坏 /
   soma 关掉 / 无档案」下的返回形状。允许两种答案：带 available|status|known
   的显式降级标记，或空容器 {} / [] / "" / None；**不允许**中性数值。
3. SignalsMatrixTest / ContractWakeConsistencyTest：社交信号文件与落盘契约
   自身的内部一致性（同一对象不能对同一份文件给出两个答案）。

D1–D7（见 audit/fix/b5/t5-link-matrix.md）原本是 @unittest.skip 的缺口，断言写的
是**修好之后**的形状；B5 收口（audit/fix/b5/t5-link.md）已把 9 个 skip 全部摘掉，
本文件现在是全绿的回归守卫：任何一个「不知道」再退化成 0 / False / 中性值，
对应的用例会直接红。
"""

from __future__ import annotations

import json
import os
import time
import unittest

from humanoid import api
from humanoid import contract as _contract
from humanoid.contract import (
    UNKNOWN,
    available,
    is_known,
    tri,
    tri_bool,
    tri_number,
    unavailable,
    unavailable_reason,
)
from humanoid.link import (
    SIGNALS_FILE_NAME,
    SIGNALS_TTL_SECONDS,
    build_contract,
    social_data_dir,
)

from .test_link import Harness
from .test_v2287_ccb_visible import _core, _mood, _ready


def _facade(core):
    roles = type("R", (), {"get_all": lambda self: [core]})()
    return api.CoreFacade(roles, None)


def _empty_facade():
    """角色表里一个都没有：门面拿不到 core 时的形状。"""
    roles = type("R", (), {"get_all": lambda self: []})()
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


class SentinelTest(unittest.TestCase):
    """三态原语：UNKNOWN 是哨兵，不是 0/False/50 里的任何一个。"""

    def test_unknown_is_distinguishable_from_zero_and_false(self):
        self.assertFalse(is_known(UNKNOWN))
        self.assertTrue(is_known(0))
        self.assertTrue(is_known(False))
        self.assertTrue(is_known(50.0))
        self.assertFalse(is_known(None), "None 是「没有值」，不是「不知道」——两者都不可用")

    def test_unknown_is_falsy_so_truthiness_is_a_trap(self):
        """bool(UNKNOWN) 是 False：任何 if x: / x or default 都会把「不知道」读成假。"""
        self.assertFalse(bool(UNKNOWN))
        self.assertTrue(is_known(0), "0 是真值意义上的假，但它**是**已知的")

    def test_missing_key_yields_unknown_not_a_default(self):
        self.assertIs(tri_bool({}, "asleep"), UNKNOWN)
        self.assertIs(tri_number({}, "hunger"), UNKNOWN)
        self.assertIs(tri({}, "anything"), UNKNOWN)

    def test_unavailable_block_makes_every_key_unknown(self):
        block = unavailable("soma_disabled")
        self.assertIs(tri_number(block, "hunger"), UNKNOWN)
        self.assertIs(tri_bool(block, "asleep"), UNKNOWN)
        self.assertEqual(unavailable_reason(block), "soma_disabled")

    def test_explicit_zero_survives_the_tristate(self):
        self.assertEqual(tri_number({"hunger": 0}, "hunger"), 0.0)
        self.assertIs(tri_bool({"asleep": False}, "asleep"), False)
        self.assertTrue(is_known(tri_number({"hunger": 0}, "hunger")))

    def test_caller_default_collapses_the_tristate(self):
        """tri(payload, key, 0.0) 等于调用方自己决定把「不知道」写成 0——这是允许的，
        但必须是**显式**的选择，不能是读取层的默认行为。"""
        self.assertEqual(tri({}, "x", 0.0), 0.0)
        self.assertIs(tri({}, "x"), UNKNOWN)

    def test_unmarked_dict_counts_as_available(self):
        """没有标记的**字典**仍然可用（v3.0.2 之前的老数据没有这个键）。

        但「块根本不存在 / 形状不对」必须判不可用（B5 矩阵 D1）：原来
        `available()` 只认显式的 `available: false`，`None` 与字符串也判 True，
        于是「缺块」与「一切正常」在读取层完全同形。
        """
        self.assertIs(available({}), True)
        self.assertIs(available({"hunger": 0.0}), True)
        self.assertIs(available(unavailable("x")), False)
        self.assertIs(available(None), False, "缺块不是可用")
        self.assertIs(available("junk"), False, "形状坏不是可用")
        self.assertEqual(unavailable_reason(None), "missing")
        self.assertEqual(unavailable_reason("junk"), "corrupt")

    def test_nan_and_garbage_numbers_are_unknown(self):
        self.assertIs(tri_number({"x": float("nan")}, "x"), UNKNOWN)
        self.assertIs(tri_number({"x": "not-a-number"}, "x"), UNKNOWN)
        self.assertIs(tri_bool({"asleep": "yes"}, "asleep"), True)

    def test_unparseable_bool_string_is_not_false(self):
        self.assertIs(tri_bool({"asleep": "maybe"}, "asleep"), UNKNOWN,
                      "「maybe」不是「醒着」")


class FacadeDeadRoleTest(unittest.TestCase):
    """角色不存在 / 门面拿不到 core：允许空容器，不允许中性数值。"""

    CASES = (
        ("brief", lambda f: f.brief("nope", "u1"), None),
        ("clock", lambda f: f.clock("nope"), {}),
        ("events", lambda f: f.events("nope"), []),
        ("state_fingerprint", lambda f: f.state_fingerprint("nope"), ""),
        ("intimacy", lambda f: f.intimacy("nope", "u1"), {}),
    )

    def test_every_entry_returns_an_empty_shape(self):
        facade = _empty_facade()
        for name, call, expected in self.CASES:
            with self.subTest(entry=name):
                self.assertEqual(call(facade), expected)

    def test_no_entry_encodes_absence_as_a_number_or_bool(self):
        facade = _empty_facade()
        for name, call, _expected in self.CASES:
            with self.subTest(entry=name):
                got = call(facade)
                self.assertNotIsInstance(
                    got, (int, float, bool),
                    "%s 把「拿不到」写成了中性值 %r" % (name, got),
                )

    def test_metadata_entries_are_static_and_honest(self):
        """版本/能力表/角色表不依赖 core：空门面也要给出静态事实，不能返回 None。"""
        facade = _empty_facade()
        self.assertEqual(facade.api_version(), api.API_VERSION)
        self.assertEqual(facade.capabilities(), list(api.CAPABILITIES))
        self.assertEqual(facade.role_ids(), [])
        self.assertIs(facade.is_current(), True, "没绑校验回调时一律视为有效")


class FacadeStaleGenerationTest(unittest.TestCase):
    """插件重载后旧门面必须立刻失效：返回空形状，而不是继续读已 stop 的数据。"""

    def test_stale_facade_returns_empty_shapes(self):
        core = _core(True)
        roles = type("R", (), {"get_all": lambda self: [core]})()
        facade = api.CoreFacade(roles, None, generation=1, is_current=lambda: False)
        self.assertIs(facade.is_current(), False)
        self.assertIsNone(facade.brief("bot1", "u1", task="chat"))
        self.assertEqual(facade.clock("bot1"), {})
        self.assertEqual(facade.events("bot1"), [])
        self.assertEqual(facade.state_fingerprint("bot1"), "")
        self.assertEqual(facade.intimacy("bot1", "u1"), {})


class FacadeMissingContractTest(unittest.TestCase):
    """契约还没生成（生产启动第一秒、soma 关掉且从未 refresh）：D1 / D2。"""

    def setUp(self):
        self.harness = Harness({"timezone_city": "北京", "soma_enabled": False})
        self.core = self.harness.roles.get_or_create("bot1")
        self.facade = _facade(self.core)

    def tearDown(self):
        self.harness.store.flush_sync()

    def test_brief_without_contract_still_has_the_documented_keys(self):
        got = self.facade.brief("bot1", "u1", task="chat")
        self.assertIsInstance(got, dict)
        for key in ("self_state", "wake", "user", "relationship", "fingerprint"):
            self.assertIn(key, got)

    def test_self_state_without_contract_must_not_read_as_available(self):
        got = self.facade.brief("bot1", "u1", task="chat")["self_state"]
        self.assertIs(got["available"], False, "契约没生成 = 不知道，不是可用")
        self.assertIsNone(got["asleep"], "契约没生成时睡着是不知道，不是醒着")

    def test_wake_without_body_must_not_probe_live_soma(self):
        got = self.facade.brief("bot1", "u1", task="chat")["wake"]
        self.assertNotEqual(got.get("source"), "live", "身体轴没在跑时不该现算 wake")

    def test_corrupt_contract_body_must_not_read_as_available(self):
        self.core.scope.set_self(
            "contract",
            {"body": "junk", "day": "junk", "generated_at": "not-a-number"},
        )
        got = self.facade.brief("bot1", "u1", task="chat")["self_state"]
        self.assertIs(got["available"], False, "body 坏了 = 不知道")


class FacadeSomaDisabledTest(unittest.TestCase):
    """soma 关掉 + 已 refresh：显式降级，而不是中性数值。"""

    def setUp(self):
        self.harness = Harness({"timezone_city": "北京", "soma_enabled": False})
        self.core = self.harness.roles.get_or_create("bot1")
        self.core.refresh_contract()
        self.facade = _facade(self.core)

    def tearDown(self):
        self.harness.store.flush_sync()

    def test_self_state_says_unavailable_and_leaves_axes_unknown(self):
        state = self.facade.brief("bot1", "u1", task="chat")["self_state"]
        self.assertIs(state["available"], False)
        self.assertEqual(state["reason"], "soma_disabled")
        self.assertIsNone(state["asleep"])
        for key in ("sleep_pressure", "sleep_debt", "hunger", "discomfort",
                    "arousal", "social_desire", "last_sleep_hours"):
            self.assertNotIn(key, state, "%s 不该在不可用时给出中性数值" % key)

    def test_wake_says_unavailable(self):
        wake = self.facade.brief("bot1", "u1", task="chat")["wake"]
        self.assertIs(wake["available"], False)
        self.assertEqual(wake["source"], "unavailable")
        self.assertIsNone(wake["asleep"])

    def test_fingerprint_is_stable_and_matches_state_fingerprint(self):
        first = self.facade.brief("bot1", "u1", task="chat")
        second = self.facade.brief("bot1", "u1", task="chat")
        self.assertEqual(first["fingerprint"], second["fingerprint"],
                         "什么都没变时指纹不该变（旧实现掺了 now//60）")
        self.assertEqual(first["fingerprint"], self.facade.state_fingerprint("bot1"))


class FacadeClockMatrixTest(unittest.TestCase):
    """时钟入口：时区不知道时要给 null + unknown，不能落成 0（=UTC）。"""

    def test_naive_clock_reports_unknown_offset(self):
        core = _core(True)
        got = _facade(core).clock("bot1")
        self.assertIsNone(got["utc_offset_minutes"])
        self.assertEqual(got["offset_source"], "unknown")
        self.assertIn("night_window", got)
        self.assertIsInstance(got["is_night"], bool)


class FacadeIntimacyMatrixTest(unittest.TestCase):
    """亲密入口：拒绝 / 拿不到 / 没有记录，三种形状要分得开。"""

    def test_whitelist_refusal_is_denied(self):
        core = _core(True, ccb_allowed_users=["10001"])
        _mood(core, affection=92.0)
        self.assertEqual(_facade(core).intimacy("bot1", "u1"), {"status": "denied"})

    def test_empty_uid_is_an_empty_dict(self):
        core = _core(True)
        self.assertEqual(_facade(core).intimacy("bot1", ""), {})

    def test_ok_path_carries_status_ok(self):
        core = _core(True)
        _ready(core)
        got = _facade(core).intimacy("bot1", "u1")
        self.assertEqual(got.get("status"), "ok")
        self.assertIs(got.get("known"), True, "有记录时这些数字是真的")

    def test_zero_record_must_not_look_like_a_measured_libido(self):
        core = _core(True)
        got = _facade(core).intimacy("bot1", "stranger")
        self.assertIs(got.get("known"), False, "没有档案时要标 known=false")
        self.assertIsNone(got.get("libido"), "配置初始值不是她的实测性欲")


class FacadeUserRelationshipMatrixTest(unittest.TestCase):
    """档案入口：配置初始值必须标 config_default，且不能伪造注意力。"""

    def test_unknown_user_is_marked_as_config_default(self):
        core = _core(True)
        got = _facade(core)._user(core, "bot1", "stranger")
        self.assertIs(got["known"], False)
        self.assertEqual(got["affection_source"], "config_default")

    def test_unknown_user_has_no_attention(self):
        core = _core(True)
        got = _facade(core)._user(core, "bot1", "stranger")
        self.assertEqual(got["attention"], {}, "没有档案就没有注意力分配，不能给 0/0/0")

    def test_existing_record_is_marked_as_record(self):
        core = _core(True)
        _mood(core)
        got = _facade(core)._user(core, "bot1", "u1")
        self.assertIs(got["known"], True)
        self.assertEqual(got["affection_source"], "record")

    def test_relationship_unknown_user_is_marked(self):
        core = _core(True)
        got = _facade(core)._relationship(core, "stranger")
        self.assertIs(got["known"], False)
        self.assertEqual(got["affection_source"], "config_default")

    def test_missing_snapshot_must_not_encode_as_false(self):
        fields = _facade(_core(True))._status_fields({})
        self.assertIsNone(fields["in_scene"], "快照缺失时「是否在场景」是不知道")
        self.assertIsNone(fields["can_enter"], "快照缺失时「能不能进」是不知道")


class SignalsMatrixTest(unittest.TestCase):
    """社交信号：读坏 / 过期 / 不在 都要带原因；坏文件不能静默沿用旧数据。"""

    def build(self):
        harness = Harness()
        return harness, harness.roles.get_or_create("bot1")

    def test_missing_file_is_not_an_empty_dict(self):
        harness, core = self.build()
        try:
            data = core.signals.read()
            self.assertIs(available(data), False)
            self.assertIn(unavailable_reason(data), ("missing", "no_data_root"))
        finally:
            harness.store.flush_sync()

    def test_stale_file_is_not_the_old_payload(self):
        harness, core = self.build()
        try:
            _write_signals(harness, {"last_proactive_at": time.time()},
                           age_seconds=SIGNALS_TTL_SECONDS + 3600)
            self.assertEqual(core.signals.read(), unavailable("stale"))
        finally:
            harness.store.flush_sync()

    def test_corrupt_file_without_history_is_unavailable(self):
        harness, core = self.build()
        try:
            _write_signals(harness, {}, raw="{不是 JSON")
            self.assertEqual(core.signals.read(), unavailable("corrupt"))
            self.assertEqual(core.signals.status()["state"], "unreadable")
        finally:
            harness.store.flush_sync()

    def test_fresh_file_keeps_the_old_shape(self):
        harness, core = self.build()
        try:
            _write_signals(harness, {"last_proactive_at": 123.0})
            data = core.signals.read()
            self.assertNotIn("available", data)
            self.assertEqual(core.signals.status()["state"], "fresh")
        finally:
            harness.store.flush_sync()

    def test_corrupt_file_after_a_good_read_is_unavailable(self):
        harness, core = self.build()
        try:
            _write_signals(harness, {"last_proactive_at": 123.0})
            self.assertNotIn("available", core.signals.read())
            _write_signals(harness, {}, raw="{这次坏了，长度也不同")
            data = core.signals.read()
            self.assertEqual(data, unavailable("corrupt"),
                             "坏文件必须说 corrupt，不能把旧数据当这次的")
        finally:
            harness.store.flush_sync()

    def test_read_and_status_agree_after_corruption(self):
        harness, core = self.build()
        try:
            _write_signals(harness, {"last_proactive_at": 123.0})
            core.signals.read()
            _write_signals(harness, {}, raw="{坏掉了但长度不同")
            readable = available(core.signals.read())
            fresh = core.signals.status()["state"] == "fresh"
            self.assertEqual(readable, fresh, "读得到 ⇔ 状态是 fresh")
        finally:
            harness.store.flush_sync()


class ContractWakeConsistencyTest(unittest.TestCase):
    """落盘契约自己不能自相矛盾：body 说不知道，wake 就不能说「醒着」。"""

    def test_soma_disabled_body_is_null(self):
        harness = Harness({"timezone_city": "北京", "soma_enabled": False})
        core = harness.roles.get_or_create("bot1")
        try:
            body = build_contract(core)["body"]
            self.assertIs(body["available"], False)
            self.assertIsNone(body["asleep"])
        finally:
            harness.store.flush_sync()

    def test_live_body_wake_is_a_bool(self):
        core = _core(True)
        wake = build_contract(core)["wake"]
        self.assertIsInstance(wake["asleep"], bool)

    def test_wake_block_must_be_unavailable_when_body_is(self):
        harness = Harness({"timezone_city": "北京", "soma_enabled": False})
        core = harness.roles.get_or_create("bot1")
        try:
            wake = build_contract(core)["wake"]
            self.assertTrue(
                wake.get("available") is False or wake.get("asleep") is None,
                "body 不可用时 wake 不能断言「醒着」",
            )
        finally:
            harness.store.flush_sync()
