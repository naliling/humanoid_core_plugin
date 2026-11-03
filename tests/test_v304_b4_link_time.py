"""B4 / R5 时间口径：UTC 偏移必须说清「它是谁的」。

契约注释一直写着「算不出来时必须是 None 而不是 0：当成 0 等于把她的城市当 UTC」，
但退化路径（城市认不出 / 缺 tzdata）导出的是**宿主机偏移**——形状与「她的偏移」完全
一样。门面 `clock()` 更直接：`int(getattr(zone, "offset_minutes", 0) or 0)` 把「不知道」
压成 0，还把退化原因（`ZoneState.note`）整个丢掉。

这里钉住三件事：

1. `time.utc_offset_minutes` 只在 `utc_offset_source == "zone"` 时是她的偏移；
   `"host"` 时偏移是这台机器的（同机消费方换算 epoch 仍然正确），`"unknown"` 时是 None。
2. `tz` 只在 `zone` 时非空——宿主机时区不是她的城市，导出去会让消费方以为她在那里。
3. 门面 `clock()` 与契约读**同一个** `link.utc_offset()`：偏移、来源、退化原因（`note`）
   三样都能拿到，None 不再变成 0。
"""

from __future__ import annotations

import unittest
from datetime import datetime

from humanoid import link
from humanoid.api import CoreFacade
from humanoid.link import build_contract

from .test_link import FROZEN, Harness, freeze

HOST_OFFSET_MINUTES = int(
    datetime.now().astimezone().utcoffset().total_seconds() // 60
)


class _BrokenZoneStateClock:
    """钟本身还能走、只有诊断接口 `zone_state()` 坏掉的替身。"""

    def __init__(self, moment) -> None:
        self._moment = moment

    def now(self):
        return self._moment

    def timestamp(self) -> float:
        return self._moment.timestamp()

    @property
    def tz(self):
        return self._moment.tzinfo

    def zone_state(self):
        raise RuntimeError("zone_state 坏了")


class ContractOffsetSourceTest(unittest.TestCase):
    """契约 `time` 块：偏移 + 来源 + 她真正的时区名。"""

    def _core(self, city: str):
        harness = Harness({"timezone_city": city})
        self.addCleanup(harness.store.flush_sync)
        return harness, harness.roles.get_or_create("bot1")

    def test_her_own_zone_exports_zone_source(self):
        harness, core = self._core("北京")
        freeze(core)
        time_block = build_contract(core)["time"]
        self.assertEqual(time_block["utc_offset_minutes"], 480)
        self.assertEqual(time_block["utc_offset_source"], "zone")
        self.assertEqual(time_block["tz"], "Asia/Shanghai")

    def test_unrecognized_city_exports_host_offset_but_says_so(self):
        """城市认不出时她按宿主机时间过日子：偏移照给，但**不能**说成她的。"""
        harness, core = self._core("不存在的地方")
        time_block = build_contract(core)["time"]
        self.assertEqual(time_block["utc_offset_source"], "host")
        self.assertEqual(time_block["tz"], "", "宿主机时区不是她的城市，不能当成她的 tz 导出")
        self.assertEqual(
            time_block["utc_offset_minutes"], HOST_OFFSET_MINUTES,
            "host 来源下偏移是这台机器的——它仍然有用，只是不等于「她的时差」",
        )

    def test_naive_moment_is_unknown_not_zero(self):
        harness, core = self._core("北京")
        freeze(core, datetime(2026, 8, 22, 15, 20))
        time_block = build_contract(core)["time"]
        self.assertIsNone(time_block["utc_offset_minutes"])
        self.assertEqual(time_block["utc_offset_source"], "unknown")
        self.assertEqual(time_block["tz"], "")

    def test_offset_helper_is_the_only_source(self):
        """`link.utc_offset()` 与契约 time 块必须是同一份推导。"""
        harness, core = self._core("北京")
        freeze(core)
        info = link.utc_offset(core)
        time_block = build_contract(core)["time"]
        self.assertEqual(info["minutes"], time_block["utc_offset_minutes"])
        self.assertEqual(info["source"], time_block["utc_offset_source"])
        self.assertEqual(info["tz"], time_block["tz"])


class FacadeClockOffsetTest(unittest.TestCase):
    """门面 `clock()`：偏移、来源、退化原因都要给，None 不许变成 0。"""

    def _facade(self, city: str):
        harness = Harness({"timezone_city": city})
        self.addCleanup(harness.store.flush_sync)
        core = harness.roles.get_or_create("bot1")
        return harness, core, CoreFacade(harness.roles)

    def test_her_zone_reports_zone_and_no_note(self):
        harness, core, facade = self._facade("北京")
        freeze(core)
        out = facade.clock("bot1")
        self.assertEqual(out["utc_offset_minutes"], 480)
        self.assertEqual(out["offset_source"], "zone")
        self.assertEqual(out["note"], "")

    def test_degraded_zone_keeps_the_reason_and_the_host_offset(self):
        """旧实现：`int(getattr(zone, "offset_minutes", 0) or 0)`——退化原因被丢掉。"""
        harness, core, facade = self._facade("不存在的地方")
        out = facade.clock("bot1")
        self.assertEqual(out["offset_source"], "host")
        self.assertEqual(out["utc_offset_minutes"], HOST_OFFSET_MINUTES)
        self.assertIn("认不出城市", out["note"], "退化原因必须透传，否则调用方只看得到数字")

    def test_unknown_offset_is_none_not_zero(self):
        harness, core, facade = self._facade("北京")
        freeze(core, datetime(2026, 8, 22, 15, 20))
        out = facade.clock("bot1")
        self.assertIsNone(out["utc_offset_minutes"])
        self.assertEqual(out["offset_source"], "unknown")
        self.assertIn("hour", out, "时钟拿得到，只是偏移不知道")

    def test_facade_and_contract_agree_on_the_offset(self):
        harness, core, facade = self._facade("不存在的地方")
        out = facade.clock("bot1")
        time_block = build_contract(core)["time"]
        self.assertEqual(out["utc_offset_minutes"], time_block["utc_offset_minutes"])
        self.assertEqual(out["offset_source"], time_block["utc_offset_source"])

    def test_broken_zone_state_does_not_take_the_clock_down(self):
        """`zone_state()` 抛异常 = 退化原因拿不到，**不是**「时钟拿不到」。

        旧实现把它和 `now()` 放在同一个 try 里：诊断接口一抛，`clock()` 就整体
        返回 `{}`，调用方连 epoch、她的小时都丢了——为了丢一个 note 丢掉整块时钟。
        """
        harness, core, facade = self._facade("北京")
        core.clock = _BrokenZoneStateClock(FROZEN)
        out = facade.clock("bot1")
        self.assertEqual(out.get("epoch"), FROZEN.timestamp())
        self.assertEqual(out.get("hour"), FROZEN.hour)
        self.assertEqual(out.get("utc_offset_minutes"), 480)
        self.assertEqual(out.get("offset_source"), "zone")
        self.assertEqual(out.get("note"), "", "拿不到退化原因就留空，不要编")
