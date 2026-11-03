"""B3 批次（R1 单一真源）：跨插件契约的夜、门面时钟、状态指纹。

守四件事：

1. **「她实际的夜」只有一份推导。** `link.night_window()` 是唯一来源，契约作息块与
   门面 `clock()` 都读它；没有夜（夜间模式关 / 窗口长度为 0）就是**没有夜**，
   不能算成 24 小时，也不能被 `max(1.0, ...)` 抬成 1 小时。
2. **同一份返回里只有一个「现在是不是夜里」。** `is_night` 与 `night_window` 同源；
   落盘契约被改坏/过期也不影响现算。
3. **指纹不随时间流逝而变。** 状态没变，社交层的缓存就不该失效；反过来，
   睡着了、精力掉档这些**状态**变了，指纹必须变。
4. **两份指纹同一个口径。** `brief()["fingerprint"]` 与 `state_fingerprint()` 相等。
"""

from __future__ import annotations

import copy
import unittest
from unittest import mock

from humanoid import link
from humanoid.api import CoreFacade
from humanoid.link import build_contract

from .fakes import FrozenClock
from .test_v2273_life_note import FROZEN, _Harness


def _install_schedule(core, slots) -> None:
    core.scope.set_self("daily_schedule", slots)
    core.scope.set_self("today_date", core.clock.today_str())


def _reclock(harness, moment) -> None:
    """把实例与各服务一起挪到另一个时刻（只挪钟，不动状态）。"""
    clock = FrozenClock(moment)
    harness.core.clock = clock
    for service in (
        harness.core.schedule, harness.core.soma, harness.core.energy,
        harness.core.mood, harness.core.social, harness.core.weather,
    ):
        service._clock = clock


class NightWindowTest(unittest.TestCase):
    """唯一一份「她的夜」的推导。"""

    def _harness(self, raw=None):
        h = _Harness(raw or {"timezone_city": "北京"})
        self.addCleanup(h.store.flush_sync)
        return h

    def test_config_window_is_the_fallback(self):
        h = self._harness()
        window = link.night_window(h.core)
        self.assertEqual(window["source"], "config")
        self.assertEqual((window["start"], window["end"]), (23.0, 6.0))
        self.assertEqual(window["span"], 7.0)

    def test_night_mode_off_means_there_is_no_night(self):
        h = self._harness({"timezone_city": "北京", "night_mode_enabled": False})
        window = link.night_window(h.core)
        self.assertEqual(window["source"], "none")
        self.assertEqual(window["span"], 0.0)
        self.assertFalse(link.in_night_window(0.0, window))

    def test_zero_length_window_means_there_is_no_night(self):
        """面板上把起止设成同一时刻 = 关掉这个窗口。

        旧实现 `span = end - start if end > start else (24.0 - start) + end` 会把它算成
        **24 小时**，于是社交层收到「她一天 24 小时都在夜里」。
        """
        h = self._harness({"timezone_city": "北京", "night_start_hour": 23, "night_end_hour": 23})
        window = link.night_window(h.core)
        self.assertEqual(window["source"], "none")
        self.assertEqual(window["span"], 0.0, "没有夜就是 0，不能被 max(1.0, span) 抬成 1")

    def test_schedule_wins_over_config(self):
        h = self._harness()
        _install_schedule(h.core, [
            {"start": "04:00", "end": "11:30", "event": "睡眠", "energy_rate": 0.18},
            {"start": "11:30", "end": "24:00", "event": "白天活动", "energy_rate": -0.05},
            {"start": "00:00", "end": "04:00", "event": "白天活动", "energy_rate": -0.05},
        ])
        window = link.night_window(h.core)
        self.assertEqual(window["source"], "schedule")
        self.assertEqual((window["start"], window["end"]), (4.0, 11.5))
        self.assertEqual(window["span"], 7.5)
        routine = build_contract(h.core)["routine"]
        self.assertEqual(routine["night_source"], "schedule")
        self.assertEqual(routine["night_span_hours"], 7.5)
        self.assertEqual((routine["night_start_hour"], routine["night_end_hour"]), (4.0, 11.5))

    def test_in_night_window_edges(self):
        window = {"start": 23.0, "end": 6.0, "span": 7.0, "source": "config"}
        for moment in (23.0, 23.5, 0.0, 5.99):
            self.assertTrue(link.in_night_window(moment, window), moment)
        for moment in (6.0, 12.0, 22.99):
            self.assertFalse(link.in_night_window(moment, window), moment)
        self.assertFalse(link.in_night_window(23.0, {"start": 23, "end": 23, "span": 0, "source": "none"}))

    def test_deep_sleep_is_the_front_of_the_same_window(self):
        window = {"start": 23.0, "end": 6.0, "span": 7.0, "source": "config"}
        self.assertTrue(link.in_deep_window(23.0, window, 0.5))
        self.assertFalse(link.in_deep_window(4.0, window, 0.5), "后半段不是深睡")


class ContractNightConsistencyTest(unittest.TestCase):
    """契约里「现在是不是夜里」必须与它自己给出的窗口一致。"""

    def setUp(self) -> None:
        self.h = _Harness()
        self.addCleanup(self.h.store.flush_sync)
        self.core = self.h.core

    def test_is_night_agrees_with_the_exported_window(self):
        _install_schedule(self.core, [
            {"start": "10:00", "end": "20:00", "event": "睡眠", "energy_rate": 0.15},
            {"start": "20:00", "end": "24:00", "event": "白天活动", "energy_rate": -0.05},
            {"start": "00:00", "end": "10:00", "event": "白天活动", "energy_rate": -0.05},
        ])
        contract = build_contract(self.core)
        routine = contract["routine"]
        hour = self.core.clock.now().hour + self.core.clock.now().minute / 60.0
        self.assertEqual(routine["night_source"], "schedule")
        self.assertTrue(contract["time"]["is_night"], "15:00 在她的睡段里（10:00-20:00）")
        self.assertTrue(link.in_night_window(hour, link.night_window(self.core)))

    def test_night_off_exports_no_window(self):
        h = _Harness({"timezone_city": "北京", "night_mode_enabled": False})
        self.addCleanup(h.store.flush_sync)
        contract = build_contract(h.core)
        self.assertEqual(contract["routine"]["night_source"], "none")
        self.assertEqual(contract["routine"]["night_span_hours"], 0.0)
        self.assertFalse(contract["time"]["is_night"])


class FacadeClockTest(unittest.TestCase):
    """门面时钟：现算，不读落盘契约。"""

    def setUp(self) -> None:
        self.h = _Harness()
        self.addCleanup(self.h.store.flush_sync)
        self.facade = CoreFacade(self.h.roles)

    def test_clock_matches_the_single_source(self):
        out = self.facade.clock("bot1")
        window = link.night_window(self.h.core)
        self.assertEqual(out["night_window"], [window["start"] % 24.0, window["end"] % 24.0])
        self.assertEqual(out["night_source"], window["source"])
        hour = out["hour"] + out["minute"] / 60.0
        self.assertEqual(out["is_night"], link.in_night_window(hour, window))

    def test_stale_or_corrupt_contract_cannot_change_the_answer(self):
        """落盘契约最多旧 60 秒，被改坏就跟着错——所以它不再是来源。"""
        self.h.core.scope.set_self("contract", {"routine": {
            "night_start_hour": 1.0, "night_end_hour": 2.0,
        }})
        out = self.facade.clock("bot1")
        self.assertEqual(out["night_window"], [23.0, 6.0])
        self.assertEqual(out["night_source"], "config")

    def test_night_off_is_reported_as_no_night(self):
        h = _Harness({"timezone_city": "北京", "night_mode_enabled": False})
        self.addCleanup(h.store.flush_sync)
        out = CoreFacade(h.roles).clock("bot1")
        self.assertEqual(out["night_source"], "none")
        self.assertFalse(out["is_night"])
        self.assertFalse(out["is_deep_sleep"])


class FingerprintTest(unittest.TestCase):
    """状态指纹：只认状态，不认时间。"""

    def setUp(self) -> None:
        self.h = _Harness()
        self.addCleanup(self.h.store.flush_sync)
        self.facade = CoreFacade(self.h.roles)
        self.core = self.h.core

    def test_time_passing_does_not_change_the_fingerprint(self):
        """旧实现最后一项是 `str(int(now) // 60)`：每 60 秒**必然**变一次。"""
        before = self.facade.state_fingerprint("bot1")
        _reclock(self.h, FROZEN.replace(minute=FROZEN.minute + 5 if FROZEN.minute < 55 else 0))
        after = self.facade.state_fingerprint("bot1")
        self.assertEqual(before, after)
        self.assertTrue(before)

    def test_brief_and_state_fingerprint_agree(self):
        brief = self.facade.brief("bot1", "10001")
        self.assertEqual(brief["fingerprint"], self.facade.state_fingerprint("bot1"))

    def test_a_state_change_does_change_the_fingerprint(self):
        contract = self.core.scope.get_self("contract") or {}
        body = contract.get("body") if isinstance(contract.get("body"), dict) else {}
        payload = self.facade._fingerprint_payload(self.core, contract, body)
        before = self.facade._fingerprint(self.core, payload)
        changed = copy.deepcopy(payload)
        changed["self_state"]["asleep"] = not bool(changed["self_state"].get("asleep"))
        self.assertNotEqual(before, self.facade._fingerprint(self.core, changed))


class AggressionZeroTest(unittest.TestCase):
    """门面把 aggression=0 读成 25（`or 25.0` 的老毛病）。"""

    def setUp(self) -> None:
        self.h = _Harness()
        self.addCleanup(self.h.store.flush_sync)
        self.facade = CoreFacade(self.h.roles)
        self.core = self.h.core

    def test_zero_aggression_survives_to_the_initiative_call(self):
        self.core.scope.set_user("10001", "mood", {
            "affection": 60.0, "libido": 30.0, "base_libido": 30.0, "aggression": 0,
        })
        seen = {}

        def fake_initiative(**kwargs):
            seen.update(kwargs)
            return 0.0

        with mock.patch("humanoid.ccb.initiative", fake_initiative):
            out = self.facade._user(self.core, "bot1", "10001")
        self.assertEqual(seen.get("aggression"), 0.0, out)
