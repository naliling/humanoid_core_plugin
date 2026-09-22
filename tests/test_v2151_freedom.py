"""v2.15.1 的两条底线：插件只给身体，不替她说话、不给她立规矩；时区不许静默退化。

第一组断言几乎全是负向的：这类东西写坏了不会报错，只会让她的话变成插件写的句子，
或者让她说的时间与配置里的城市差几个小时，从聊天里根本看不出来。
"""

from __future__ import annotations

import asyncio
import tempfile
import time
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
    FRAMING_TEXT,
    MARK_PREFIX,
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
        # 困意措辞按天抽签（很困/困得不行…），断言只能认「说的是困」而不是某一句原话。
        self.assertTrue("困" in text or "疲惫" in text, f"她此刻的困意得说得出：\n{text}")
        self.assertIn("她对TA", text)
        self.assertIn("私聊", text)
        # 但「这副身体这会儿只够说一两句，20字上下的量」那种形式限制不许回来：
        # 插件拦不住她说话，写出来只是让她猜规矩。
        for gone in ("字上下的量", "只够说一两句", "再多就散了", "展开不了一段长篇"):
            self.assertNotIn(gone, text, f"形式限制又回来了：{gone}")

    def test_scene_name_and_time_are_always_there(self):
        """必需品三样：时间、称呼、场景（群聊还是私聊）。三档都得有。"""
        for mode in ("low", "full", "mood_only"):
            core = self.core(mode=mode)
            core.mood.set_nickname("42", "小鱼")
            text = core.build_injection("42", is_group=False)
            self.assertIn("私聊", text, f"{mode} 档没告诉她这是私聊：\n{text}")
            self.assertIn("15:20", text, f"{mode} 档没给她时间：\n{text}")
            self.assertIn("你管TA叫小鱼", text, f"{mode} 档没给她称呼：\n{text}")
            group = core.build_injection("42", is_group=True)
            self.assertIn("群聊", group, f"{mode} 档没告诉她这是群聊：\n{group}")

    def test_framing_is_a_frame_not_an_order(self):
        """给 system_prompt 的那段只讲「这些是什么、怎么读」，不含禁令也不含事实。"""
        for word in ("不要", "必须", "别", "控制在", "只说", "回一句"):
            self.assertNotIn(word, FRAMING_TEXT, f"框架句里出现了「{word}」")
        self.assertIn("处境", FRAMING_TEXT)
        self.assertIn("身体与生活", FRAMING_TEXT, "框架句要指得清它说的是哪几块")
        self.assertIn("由她自己判断", FRAMING_TEXT, "参考边界要说清：插件不替她决定怎么开口")
        for digit in ("%", "UTC+"):
            self.assertNotIn(digit, FRAMING_TEXT)

    def test_facts_block_carries_no_readings(self):
        core = self.wrecked_body(self.core(mode="full"))
        text = core.build_injection("42", is_group=False)
        for word in ("%", "UTC+", "Asia/", "Europe/", "/100", "0.", "好感度"):
            self.assertNotIn(word, text, f"体检单读数又进上下文了：{word}\n{text}")

    def test_interest_axes_read_as_states_not_numbers(self):
        """在意度要变成状态词，而且不同用户词不一样；数值不上下文。"""
        core = self.core()
        builder = PromptBuilder(core)
        mine = builder._relation_lines("42", False, detailed=False,
                                       interest={"care": 0.9, "focus": 0.9, "spare": 0.9})
        cold = builder._relation_lines("43", False, detailed=False,
                                       interest={"care": 0.05, "focus": 0.05, "spare": 0.05})
        self.assertTrue(any("亲密" in line or "在意" in line for line in mine), mine)
        # 措辞按天抽签：0.00 档有「关系疏远」「缺乏交集」两套说法，都得认。
        self.assertTrue(any(word in line for line in cold for word in ("疏远", "缺乏交集")),
                        f"低在意度没说出距离感：{cold}")
        self.assertNotEqual(mine, cold)
        for line in mine + cold:
            self.assertNotIn("0.", line, f"三轴把数值端上来了：{line}")

    def test_mood_label_comes_bare_without_tone_hints(self):
        """情绪只给标签本身（如「亲密」），不附「态度亲昵」这类语气括号。"""
        core = self.core()
        core.mood.profile("42").update({"affection": 90.0, "libido": 40.0, "aggression": 5.0})
        text = core.build_injection("42", is_group=False)
        self.assertIn("她对TA", text)
        for word in ("态度", "语气", "带有敌意", "保持警惕", "充满信任"):
            self.assertNotIn(word, text, f"语气提示又回来了：{word}\n{text}")

    def test_truncation_keeps_whole_blocks_and_the_marker(self):
        """撑到上限时按块丢：截在半句上模型会自己把那半句补下去。"""
        core = self.core(mode="low")
        builder = PromptBuilder(core)
        text = builder._finish("【此刻】" + "很长的一段背景。" * 200, cfg())
        self.assertTrue(text.startswith(MARK_PREFIX), text[:60])
        for line in text.split("\n")[1:]:
            self.assertTrue(line.startswith("【") and line.endswith("。"), f"半块被截进来了：{line}")
        self.assertNotIn("不必逐条回应", text, "兜底话该在 system_prompt 里，不该每条消息重复")

    def test_gap_is_an_experience_not_a_stopwatch(self):
        """间隔两段：隔了多久 + TA离开前那句原话。都是事实，不带「要主动问起」。"""
        core = self.core()
        core.behavior.add_event("42", {
            "type": "user_returned", "timestamp": time.time(), "importance": 0.7,
            "data": {"gap_seconds": 3 * 3600 + 12 * 60, "previous_message": "我先去开会"},
        })
        text = core.build_injection("42", is_group=False)
        self.assertIn("3 小时 12 分", text)
        self.assertIn("我先去开会", text, "离开前原话是间隔实用性的另一半")
        self.assertNotIn("这是一次性的背景", text)
        for word in ("要主动", "问问他", "接上话头"):
            self.assertNotIn(word, text, f"间隔又变成了指令：{word}")


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
