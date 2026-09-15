"""v2.15.1 的两条底线：插件只给身体，不替她说话、不给她立规矩；时区不许静默退化。

第一组断言几乎全是负向的：这类东西写坏了不会报错，只会让她的话变成插件写的句子，
或者让她说的时间与配置里的城市差几个小时，从聊天里根本看不出来。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from humanoid import clock as clock_module
from humanoid.clock import Clock, now_in_city, resolve_zone, resolve_zone_name
from humanoid.config import HumanoidConfig
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.data.cities import DEFAULT_CITY_PLACEHOLDER, lookup_city_time
from humanoid.data.cities import resolve_zone_name as table_zone
from humanoid.link import build_contract
from humanoid.prompt_builder import (
    BOUNDARY_LINE,
    PERMISSION_LINE,
    PromptBuilder,
)
from humanoid.state import StateStore

from .fakes import FakeContext, FrozenClock, RecordingLogger

TZ = ZoneInfo("Asia/Shanghai")
MOMENT = datetime(2026, 8, 22, 15, 20, tzinfo=TZ)
TODAY = MOMENT.strftime("%Y-%m-%d")

# 插件不该出现在模型眼前的说法：台词、禁令、以及「这一轮必须怎样」的调度口吻。
BANNED = (
    "不要",
    "不应回复",
    "必须",
    "控制在",
    "别超过",
    "别展开",
    "别念",
    "别反复",
    "回一句",
    "提一句",
    "明天再聊",
    "简短回应",
    "只说一两句",
    "这一轮",
    "语气要",
    "保持距离就好",
    "说话会短",
)


def cfg(**overrides) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", **overrides})


class InjectionFreedomTest(unittest.TestCase):
    """把身体推到极端，看注入里会不会又长出禁令与台词。"""

    def core(self, mode: str = "low", **conf) -> HumanoidCoreInstance:
        store = StateStore(Path(tempfile.mkdtemp()) / "state.json", lambda: 0.01)
        store.load(TODAY, 28)
        config = cfg(inject_activity_context=mode, **conf)
        core = HumanoidCoreInstance(
            role_id="bot1",
            state_store=store,
            config_provider=lambda: config,
            logger=RecordingLogger(),
            stop_event=asyncio.Event(),
            resolver=FakeContext(),
            gateway=None,
        )
        core.clock = FrozenClock(MOMENT)
        return core

    def wrecked_body(self, core) -> HumanoidCoreInstance:
        """困到极点 + 饿 + 难受 + 刚被冷落：这几条轴最容易让插件开始下命令。"""
        core.soma.data.update(
            {
                "sleep_pressure": 96.0,
                "sleep_debt": 9.0,
                "hunger": 95.0,
                "discomfort": 80.0,
                "arousal": 10.0,
                "social_desire": 0.0,
                "ignored_streak": 4,
            }
        )
        core.soma.data["asleep"] = 1.0
        return core

    def test_extreme_body_writes_no_commands(self):
        for mode in ("low", "full", "mood_only"):
            core = self.wrecked_body(self.core(mode=mode))
            text = core.build_injection("42", is_group=False)
            for word in BANNED:
                self.assertNotIn(word, text, f"{mode} 档的注入里出现了命令/台词「{word}」：\n{text}")

    def test_group_injection_writes_no_commands(self):
        core = self.wrecked_body(self.core(mood_enabled_in_group=True))
        text = core.build_injection("42", is_group=True)
        for word in BANNED:
            self.assertNotIn(word, text, f"群聊注入里出现了「{word}」：\n{text}")

    def test_body_still_speaks_as_a_state(self):
        """去掉禁令不等于去掉身体：她此刻的处境还得说得出。"""
        core = self.wrecked_body(self.core())
        text = core.build_injection("42", is_group=False)
        self.assertIn("字上下的量", text)
        self.assertIn("上面这些是你的身体", text)
        self.assertIn("你自己定", text)

    def test_boundary_line_is_a_frame_not_an_order(self):
        self.assertNotIn("别", BOUNDARY_LINE)
        self.assertNotIn("不要", BOUNDARY_LINE)
        self.assertNotIn("必须", BOUNDARY_LINE)
        self.assertIn("台词", BOUNDARY_LINE)

    def test_permission_line_gives_the_call_back_to_her(self):
        for word in ("不要", "必须", "别"):
            self.assertNotIn(word, PERMISSION_LINE)
        self.assertIn("你自己定", PERMISSION_LINE)

    def test_mood_hints_describe_the_heart_not_the_tone(self):
        from humanoid.prompt_builder import MOOD_TONE_HINTS

        for label, hint in MOOD_TONE_HINTS.items():
            self.assertNotIn("语气", hint, f"{label} 的语气提示又在教她说话：{hint}")

    def test_truncation_tail_is_not_a_script(self):
        """撑到上限时补的那句兜底也不该是命令。"""
        core = self.core(mode="full")
        core.clock = FrozenClock(MOMENT)
        builder = PromptBuilder(core)
        text = builder._finish("【此刻】" + "很长的一段背景。" * 200, cfg())
        self.assertTrue(any(line.startswith("（以上") for line in text.split("\n")), text)
        self.assertIn("不用原样说给对方听", text)


class ZoneNameTest(unittest.TestCase):
    def test_table_city(self):
        self.assertEqual(resolve_zone_name("北京"), "Asia/Shanghai")
        self.assertEqual(resolve_zone_name(" 东京 "), "Asia/Tokyo")

    def test_iana_name_is_accepted_directly(self):
        """表里只有有限的城市：填 IANA 名必须能用，否则她就静默按机器时间过日子。"""
        self.assertEqual(resolve_zone_name("Asia/Ho_Chi_Minh"), "Asia/Ho_Chi_Minh")
        self.assertEqual(resolve_zone_name("America/New_York"), "America/New_York")
        self.assertEqual(table_zone("Europe/Berlin"), "Europe/Berlin")

    def test_placeholder_and_unknown(self):
        self.assertIsNone(resolve_zone_name(DEFAULT_CITY_PLACEHOLDER))
        self.assertIsNone(resolve_zone_name(""))
        self.assertIsNone(resolve_zone_name("  "))
        self.assertIsNone(resolve_zone_name("不存在的地方"))
        self.assertIsNone(resolve_zone_name("Asia"))  # 没有斜杠的不算时区名
        self.assertIsNone(resolve_zone_name("12/34"))

    def test_unknown_city_degrades_to_host_clock_with_a_reason(self):
        tz, note = resolve_zone("不存在的地方")
        self.assertIsNone(tz)
        self.assertIn("认不出城市", note)
        self.assertIn("这台机器", note)

    def test_known_city_has_no_note(self):
        tz, note = resolve_zone("北京")
        self.assertIsNotNone(tz)
        self.assertEqual(note, "")

    def test_missing_tzdata_no_longer_lies_about_being_shanghai(self):
        """旧版这里会静默换成 Asia/Shanghai：设成东京的她其实按北京时间过一天。"""
        real = clock_module.ZoneInfo
        clock_module.ZoneInfo = lambda name: (_ for _ in ()).throw(ZoneInfoNotFoundError(name))
        resolve_zone.cache_clear()
        try:
            tz, note = resolve_zone("东京")
            self.assertIsNone(tz)
            self.assertIn("tzdata", note)
            self.assertNotIn("Shanghai", note)
            moment = now_in_city("东京")
            host = datetime.now().astimezone()
            self.assertEqual(moment.utcoffset(), host.utcoffset())
        finally:
            clock_module.ZoneInfo = real
            resolve_zone.cache_clear()

    def test_city_time_text_explains_the_degradation(self):
        result = lookup_city_time("不存在的地方")
        self.assertIsNotNone(result, "认不出城市时也该给出时间，只是要说明它是机器时间")
        self.assertIn("认不出城市", result.note)
        ok = lookup_city_time("北京")
        self.assertEqual(ok.note, "")


class ClockZoneStateTest(unittest.TestCase):
    def test_state_for_a_table_city(self):
        clock = Clock(lambda: cfg(timezone_city="北京"))
        state = clock.zone_state()
        self.assertEqual(state.city, "北京")
        self.assertEqual(state.zone_name, "Asia/Shanghai")
        self.assertEqual(state.offset_minutes, 480)
        self.assertEqual(state.note, "")

    def test_state_for_placeholder_is_host_time_and_warns(self):
        clock = Clock(lambda: cfg(timezone_city=DEFAULT_CITY_PLACEHOLDER))
        state = clock.zone_state()
        self.assertEqual(state.zone_name, "")
        self.assertEqual(state.note, "")  # 没定城市不是「出错」，诊断里单独提示
        self.assertEqual(state.offset_minutes, int(state.offset_minutes))

    def test_state_for_unknown_city_carries_the_reason(self):
        clock = Clock(lambda: cfg(timezone_city="不存在的地方"))
        state = clock.zone_state()
        self.assertEqual(state.zone_name, "")
        self.assertIn("认不出城市", state.note)


class ContractTimeTest(unittest.TestCase):
    def core(self, city: str = "北京"):
        from .test_link import Harness

        harness = Harness({"timezone_city": city})
        return harness, harness.roles.get_or_create("bot1")

    def test_contract_carries_her_real_offset(self):
        harness, core = self.core("北京")
        core.clock = FrozenClock(MOMENT)
        contract = build_contract(core)
        self.assertEqual(contract["time"]["utc_offset_minutes"], 480)
        self.assertEqual(contract["time"]["tz"], "Asia/Shanghai")
        asyncio.run(harness.roles.stop())

    def test_naive_moment_exports_none_not_zero(self):
        """当成 0 等于把她的城市当 UTC：社交层会静默按错的时间判断该不该说话。"""
        harness, core = self.core("北京")
        core.clock = FrozenClock(datetime(2026, 8, 22, 15, 20))
        contract = build_contract(core)
        self.assertIsNone(contract["time"]["utc_offset_minutes"])
        self.assertEqual(contract["time"]["tz"], "")
        asyncio.run(harness.roles.stop())


class DiagnosticsTimezoneTest(unittest.TestCase):
    def report(self, city: str) -> str:
        from .test_link import Harness

        harness = Harness({"timezone_city": city})
        core = harness.roles.get_or_create("bot1")
        text = harness.engine.diagnostics_text(core)
        asyncio.run(harness.roles.stop())
        return text

    def test_report_has_a_timezone_section(self):
        text = self.report("北京")
        self.assertIn("【时间与时区】", text)
        self.assertIn("Asia/Shanghai", text)

    def test_report_says_so_when_the_city_is_not_recognised(self):
        text = self.report("不存在的地方")
        self.assertIn("认不出城市", text)
        self.assertIn("没拿到可用时区", text)
        self.assertIn("按这台机器的时间排", text, "退化时不该再说「日程按她那个城市算」")

    def test_report_gives_the_install_hint_when_tzdata_is_missing(self):
        real = clock_module.ZoneInfo
        clock_module.ZoneInfo = lambda name: (_ for _ in ()).throw(ZoneInfoNotFoundError(name))
        resolve_zone.cache_clear()
        try:
            text = self.report("东京")
            self.assertIn("tzdata", text)
            self.assertIn("装一份时区数据库", text)
        finally:
            clock_module.ZoneInfo = real
            resolve_zone.cache_clear()

    def test_report_warns_about_the_undecided_city(self):
        text = self.report(DEFAULT_CITY_PLACEHOLDER)
        self.assertIn("还没定所在城市", text)


class MovedCityTest(unittest.TestCase):
    """换城市 = 换时区：state.json 里的墙上时间不能拿新时区去解释。"""

    def make_core(self, store, city: str) -> HumanoidCoreInstance:
        return HumanoidCoreInstance(
            role_id="bot1",
            state_store=store,
            config_provider=lambda: cfg(timezone_city=city),
            logger=RecordingLogger(),
            stop_event=asyncio.Event(),
            resolver=FakeContext(),
            gateway=None,
        )

    def self_state(self, store) -> dict:
        return store.data["roles"]["bot1"]["self"]

    def test_moving_rebases_the_wall_clock_bookkeeping(self):
        from datetime import timedelta

        from humanoid.clock import format_state_timestamp, parse_state_timestamp

        store = StateStore(Path(tempfile.mkdtemp()) / "state.json", lambda: 0.01)
        store.load(TODAY, 28)
        self.make_core(store, "东京")
        state = self.self_state(store)
        self.assertEqual(state.get("tz_city"), "东京", "第一次就该记下她按哪个城市记的时")
        # 旧城市记下的墙上时间（东京的钟点）
        state["last_update"] = format_state_timestamp(now_in_city("东京"))
        tokyo_stamp = state["last_update"]
        state["_last_weather_fetch"] = "2026-08-22 09:00:00"

        self.make_core(store, "北京")
        beijing_stamp = str(state.get("last_update"))
        self.assertEqual(state.get("tz_city"), "北京")
        self.assertEqual(state.get("_last_weather_fetch"), "", "天气该重取，不该拿旧城市的时点算过期")
        now = now_in_city("北京")
        self.assertLess(
            abs((parse_state_timestamp(beijing_stamp, now) - now).total_seconds()), 120.0,
            f"计时没按新城市重新起算：{beijing_stamp}",
        )
        gap = parse_state_timestamp(tokyo_stamp, now) - parse_state_timestamp(beijing_stamp, now)
        self.assertGreater(gap, timedelta(minutes=50), "东京的钟点本来就比北京晚一小时")

    def test_same_city_writes_nothing(self):
        store = StateStore(Path(tempfile.mkdtemp()) / "state.json", lambda: 0.01)
        store.load(TODAY, 28)
        self.make_core(store, "北京")
        stamp = str(self.self_state(store).get("last_update"))
        self.make_core(store, "北京")
        self.assertEqual(str(self.self_state(store).get("last_update")), stamp, "没搬家就别动计时")


if __name__ == "__main__":
    unittest.main()
