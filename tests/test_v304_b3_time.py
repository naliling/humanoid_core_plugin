"""B3 / R1：时钟「夜间」语义、天气城市唯一判定、诊断页两行（审核 26 / M3 / M4）。

钉住的三件事：

* `Clock.is_night_hour()` 只问钟点，`Clock.is_night()` 才是「夜间模式生效中」——
  旧实现把两件事 AND 在一起，同一条 payload 里的 `is_night` 与 `night_window` 互相矛盾；
* 天气城市只有一份判定（`services.weather.effective_location`），诊断页问的是同一份，
  不再自己写「weather_location or 时间城市」，于是不会出现「面板说配好了、天气说未配置」；
* 状态体积告警指向真实来源（每用户空壳）并给出空壳数，不再让用户去调一个对它无效的
  「情绪数据保留」。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from humanoid import diagnostics
from humanoid.clock import DEFAULT_CITY_PLACEHOLDER, Clock, ZoneState
from humanoid.config import HumanoidConfig
from humanoid.services.weather import effective_location

NIGHT = datetime(2026, 1, 1, 23, 30)
NOON = datetime(2026, 1, 1, 12, 0)


class NightHourSemanticsTest(unittest.TestCase):
    """is_night_hour 只由小时与窗口决定；is_night 仍是「开关 AND 钟点」。"""

    def test_night_hour_ignores_switch(self):
        off = HumanoidConfig(night_mode_enabled=False)
        clock = Clock(lambda: off)
        self.assertTrue(clock.is_night_hour(NIGHT))
        self.assertFalse(clock.is_night_hour(NOON))

    def test_is_night_keeps_switch_semantics(self):
        off = HumanoidConfig(night_mode_enabled=False)
        clock = Clock(lambda: off)
        # 旧语义（已弃用）：开关关掉时恒 False，即使钟点在窗口里。
        self.assertFalse(clock.is_night(NIGHT))
        on = HumanoidConfig(night_mode_enabled=True)
        self.assertTrue(Clock(lambda: on).is_night(NIGHT))

    def test_payload_fields_no_longer_contradict(self):
        # 同一条 payload 会同时给 is_night 与 night_window：开关关着时
        # 23:30 必须仍是「窗口内」，否则两个字段对同一个问题给相反答案。
        off = HumanoidConfig(night_mode_enabled=False, night_start_hour=23, night_end_hour=6)
        clock = Clock(lambda: off)
        self.assertTrue(clock.is_night_hour(NIGHT))
        self.assertFalse(off.night_mode_enabled)


class WeatherLocationSingleSourceTest(unittest.TestCase):
    """模块级 effective_location：诊断与天气子系统问同一个函数。"""

    @staticmethod
    def _zone(city: str, zone_name: str = "") -> ZoneState:
        return ZoneState(city=city, zone_name=zone_name, offset_minutes=480, note="")

    def test_placeholder_means_unconfigured(self):
        cfg = HumanoidConfig()
        self.assertEqual(effective_location(self._zone(DEFAULT_CITY_PLACEHOLDER), cfg), "")

    def test_iana_name_is_translated(self):
        cfg = HumanoidConfig()
        zone = self._zone("Asia/Shanghai", "Asia/Shanghai")
        self.assertEqual(effective_location(zone, cfg), "Shanghai,CN")

    def test_explicit_english_city_wins(self):
        cfg = HumanoidConfig(weather_location="Beijing,CN")
        zone = self._zone("Asia/Shanghai", "Asia/Shanghai")
        self.assertEqual(effective_location(zone, cfg), "Beijing,CN")


class DiagnosticsWeatherCityLineTest(unittest.TestCase):
    """诊断页的天气行必须和天气子系统同一结论。"""

    MOMENT = datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=8)))

    def _text(self, cfg, zone) -> str:
        return "\n".join(diagnostics._timezone_lines(cfg, zone, self.MOMENT))

    def test_default_placeholder_is_reported_unconfigured(self):
        cfg = HumanoidConfig()
        zone = ZoneState(city=DEFAULT_CITY_PLACEHOLDER, zone_name="", offset_minutes=0, note="")
        text = self._text(cfg, zone)
        self.assertIn("未配置（天气子系统判定）", text)
        # 旧行为：拿 timezone_city 顶替，面板显示「天气城市：河源（记得改~）」。
        self.assertNotIn("天气城市：河源", text)

    def test_iana_city_shows_the_normalized_name(self):
        cfg = HumanoidConfig()
        zone = ZoneState(city="Asia/Shanghai", zone_name="Asia/Shanghai", offset_minutes=480, note="")
        text = self._text(cfg, zone)
        self.assertIn("天气城市：Shanghai,CN（与时间城市同一处）", text)

    def test_unrecognized_explicit_value_is_explained(self):
        cfg = HumanoidConfig(weather_location="火星")
        zone = ZoneState(city="Asia/Shanghai", zone_name="Asia/Shanghai", offset_minutes=480, note="")
        text = self._text(cfg, zone)
        self.assertIn("未配置（天气子系统判定）", text)
        self.assertIn("认不出英文城市名", text)


class StateSizeShellCountTest(unittest.TestCase):
    """体积告警：hint 指向空壳，用户行给出空壳数。"""

    @staticmethod
    def _core(size_kb: int, users: dict | None, ids: list[str] | None = None):
        tmp = tempfile.mkdtemp()
        path = Path(tmp) / "state.json"
        with open(path, "wb") as fh:
            fh.write(b"x" * (size_kb * 1024))
        scope = SimpleNamespace(all_user_ids=lambda: list(ids if ids is not None else (users or {}).keys()))
        if users is not None:
            scope._root = {"users": users}
        return SimpleNamespace(_state_store=SimpleNamespace(path=path), scope=scope)

    def test_hint_points_at_shells_not_retention(self):
        core = self._core(600, {"1": {"nickname": "a"}})
        text = "\n".join(diagnostics.state_size_lines(core))
        self.assertIn("空壳", text)
        # 旧 hint：「可调小「情绪数据保留」」——那个开关清不掉只有称呼的条目。
        self.assertNotIn("情绪数据保留", text)

    def test_shell_count_is_reported(self):
        users = {
            "1": {"nickname": "a"},
            "2": {"mood": 0.5},
            "3": {"nickname": "b", "first_met": 1.0},
            "4": {},
        }
        core = self._core(10, users)
        text = "\n".join(diagnostics.state_size_lines(core))
        self.assertIn("记录在案的用户：4 人（其中只剩称呼的空壳 2 条）", text)

    def test_missing_entries_degrades_to_plain_count(self):
        core = self._core(10, None, ids=["1", "2"])
        text = "\n".join(diagnostics.state_size_lines(core))
        self.assertIn("记录在案的用户：2 人", text)
        self.assertNotIn("空壳", text)


if __name__ == "__main__":
    unittest.main()
