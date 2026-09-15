"""v2.16 的两条底线。

一、聊天上下文里只放事实（时间、称呼、隔了多久没说话，以及 full 档的生活事实）。
身体数值、体感句、"这一轮说多长"、情绪态度句、语气提示、夜间禁令——一律不进上下文。
写进去的东西模型不会当参考，只会当任务；插件的职责是给身体与生活，不是替她说话。

二、时区不许静默退化：她说在哪个城市，就得按那个城市的钟点过日子，做不到要说出来。

这两类毛病共同点是不会报错：前者让她的话变成插件写的句子，后者让她说的时间与配置里的
城市差几个小时。所以断言基本都是负向的。
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
from humanoid.data.cities import CITY_TO_TIMEZONE, DEFAULT_CITY_PLACEHOLDER, lookup_city_time
from humanoid.link import build_contract
from humanoid.services.schedule import SOURCE_LLM, normalize_slots
from humanoid.state import StateStore

from .fakes import FakeContext, RecordingLogger, freeze

TZ = ZoneInfo("Asia/Shanghai")
MOMENT = datetime(2026, 8, 22, 15, 20, tzinfo=TZ)
TODAY = MOMENT.strftime("%Y-%m-%d")

# 模型不该在上下文里读到的说法：台词、禁令、形式要求、身体指标、情绪解释、元指令。
BANNED = (
    "不要",
    "不应回复",
    "必须",
    "控制在",
    "别超过",
    "别展开",
    "别念",
    "回一句",
    "提一句",
    "明天再聊",
    "简短回应",
    "只说一两句",
    "字上下",
    "这一轮",
    "语气",
    "眼皮很沉",
    "迷迷糊糊",
    "积了点火",
    "精力",
    "社交能量",
    "/100",
    "上面这些",
    "你自己定",
    "不是要你",
)

# 关系信息只在 mood_only 档出现，low / full 不该有。
RELATION_ONLY_IN_MOOD = ("对TA的感觉",)


def cfg(**overrides) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", **overrides})


def core_with(mode: str = "low", **conf) -> HumanoidCoreInstance:
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
    return freeze(core, MOMENT)


def wrecked_body(core) -> HumanoidCoreInstance:
    """困到极点、饿、难受、正在睡觉、刚被冷落：这些最容易让插件又开始下命令。"""
    # 直接写进作用域而不走 _install：_install 会按夜间窗口把 14:00 的睡眠重切到夜里，
    # 而这里要的就是「日程此刻写着睡」。标记也不能直接拧 soma 的 asleep：那会被下一次
    # advance 清掉——身体只认日程。
    core._scope.update_self(
        today_date=TODAY,
        schedule_source=SOURCE_LLM,
        daily_schedule=normalize_slots(
            [{"start": "14:00", "end": "16:00", "event": "睡眠休息", "location": "卧室",
              "emotion": "平静", "energy_rate": 0.15}],
            max_slots=16,
        ),
    )
    core.soma.data.update(
        {
            "sleep_pressure": 99.0,
            "sleep_debt": 18.0,
            "hunger": 95.0,
            "discomfort": 80.0,
            "arousal": 10.0,
            "social_desire": 0.0,
            "ignored_streak": 4,
        }
    )
    core.soma.data["last_tick"] = MOMENT.timestamp() - 600.0
    core.soma.advance()
    core.mood.profile("42").update(
        {"affection": 30.0, "libido": 40.0, "aggression": 45.0,
         "base_affection": 46.0, "base_aggression": 28.0}
    )
    core.mood.set_nickname("42", "小鱼")
    return core


class FactsOnlyTest(unittest.TestCase):
    def test_every_tier_is_facts_only(self):
        for mode in ("low", "full", "mood_only"):
            core = wrecked_body(core_with(mode=mode))
            text = core.build_injection("42", is_group=False)
            with self.subTest(mode=mode):
                for word in BANNED:
                    self.assertNotIn(word, text, f"{mode} 档把「{word}」塞进了上下文：\n{text}")
                if mode != "mood_only":
                    for word in RELATION_ONLY_IN_MOOD:
                        self.assertNotIn(word, text, f"{mode} 档不该给关系解释")

    def test_group_tier_is_facts_only(self):
        core = wrecked_body(core_with(mood_enabled_in_group=True))
        text = core.build_injection("42", is_group=True)
        for word in BANNED + RELATION_ONLY_IN_MOOD:
            self.assertNotIn(word, text, f"群聊注入里出现了「{word}」：\n{text}")

    def test_sleeping_body_does_not_change_the_reply_contract(self):
        """她在睡觉：插件不压字数、不赶人、也不替她说「我要睡了」。

        身体照旧在攒债、日程照旧写着睡眠，但聊天上下文里只有钟点。
        """
        core = wrecked_body(core_with())
        text = core.build_injection("42", is_group=False)
        self.assertIn("【时间】", text)
        self.assertIn("15:20", text)
        self.assertEqual(core.soma.snapshot()["asleep"], 1.0, "身体该知道她在睡")
        self.assertNotIn("睡", text, "上下文里不该出现「她在睡觉」这种会被她念出来的话")

    def test_default_tier_carries_time_gap_and_nickname(self):
        core = core_with()
        core.mood.set_nickname("42", "小鱼")
        core.behavior.add_event("42", {
            "type": "long_gap", "timestamp": core.soma.now, "importance": 0.9,
            "data": {"gap_seconds": 11520.0, "previous_message": "我猫今天吐了"},
        })
        text = core.build_injection("42", is_group=False)
        self.assertIn("【时间】", text)
        self.assertIn("小鱼", text)
        self.assertIn("距上次说话 3 小时 12 分", text)
        self.assertIn("我猫今天吐了", text)

    def test_full_tier_adds_life_not_body(self):
        core = wrecked_body(core_with(mode="full"))
        text = core.build_injection("42", is_group=False)
        self.assertIn("【时间】", text)
        self.assertIn("【称呼】", text)
        self.assertNotIn("【我的感觉】", text)
        self.assertNotIn("【话的份量】", text)

    def test_no_injected_block_tells_her_what_to_do(self):
        """整块扫一遍：注入里出现的每个字都该是名词性的事实。"""
        for mode in ("low", "full", "mood_only"):
            core = wrecked_body(core_with(mode=mode))
            text = core.build_injection("42", is_group=False)
            for verb in ("记住", "记得要", "请", "应该", "务必", "试着", "不妨"):
                self.assertNotIn(verb, text, f"{mode} 档在给她下指令（{verb}）：{text}")


class CityTableTest(unittest.TestCase):
    """三个国家的地区必须全部收录：她说得出地名，就得对得上那个地名的钟点。"""

    def test_china_prefecture_cities_are_covered(self):
        for city in ("北京", "上海", "深圳", "广州", "成都", "杭州", "武汉", "西安",
                     "哈尔滨", "乌鲁木齐", "拉萨", "昆明", "台北", "香港", "澳门"):
            self.assertIn(city, CITY_TO_TIMEZONE, f"中国大陆/港澳台城市没收录：{city}")

    def test_russia_spans_all_eleven_zones(self):
        wanted = {"Europe/Kaliningrad", "Europe/Moscow", "Europe/Samara", "Asia/Yekaterinburg",
                  "Asia/Omsk", "Asia/Krasnoyarsk", "Asia/Irkutsk", "Asia/Yakutsk",
                  "Asia/Vladivostok", "Asia/Magadan", "Asia/Kamchatka"}
        got = {z for z in set(CITY_TO_TIMEZONE.values()) if z in wanted}
        self.assertEqual(got, wanted, "俄罗斯 11 个时区没覆盖全")
        self.assertGreaterEqual(sum(1 for z in CITY_TO_TIMEZONE.values() if z in wanted), 60,
                                "俄罗斯只收了几个城市，谈不上「全部地区」")

    def test_japan_prefectures_are_covered(self):
        for city in ("东京", "大阪", "札幌", "名古屋", "京都", "横滨", "神户", "福冈",
                     "那霸", "仙台", "广岛", "长崎"):
            self.assertEqual(CITY_TO_TIMEZONE.get(city), "Asia/Tokyo", city)
        self.assertGreaterEqual(sum(1 for z in CITY_TO_TIMEZONE.values() if z == "Asia/Tokyo"), 47,
                                "日本 47 都道府县没收全")

    def test_every_entry_is_a_loadable_zone(self):
        for name in set(CITY_TO_TIMEZONE.values()):
            resolve_zone.cache_clear()
            tz, note = resolve_zone(name)
            with self.subTest(zone=name):
                self.assertIsNotNone(tz, f"表里这个 IANA 名机器加载不了：{name}（{note}）")

    # 三个国家在 IANA 里的全部时区。少一个就意味着住在那片地方的人拿不到自己的钟点。
    IANA_CN = {"Asia/Shanghai", "Asia/Urumqi"}
    IANA_JP = {"Asia/Tokyo"}
    IANA_RU = {
        "Europe/Kaliningrad", "Europe/Moscow", "Europe/Simferopol", "Europe/Volgograd",
        "Europe/Kirov", "Europe/Astrakhan", "Europe/Samara", "Europe/Saratov",
        "Europe/Ulyanovsk", "Asia/Yekaterinburg", "Asia/Omsk", "Asia/Novosibirsk",
        "Asia/Barnaul", "Asia/Novokuznetsk", "Asia/Krasnoyarsk", "Asia/Irkutsk",
        "Asia/Tomsk", "Asia/Yakutsk", "Asia/Khandyga", "Asia/Chita", "Asia/Vladivostok",
        "Asia/Ust-Nera", "Asia/Magadan", "Asia/Sakhalin", "Asia/Srednekolymsk",
        "Asia/Kamchatka", "Asia/Anadyr",
    }
    # 港澳台用各自的 IANA 名（偏移同为 +8）
    EXTRA_CN = {"Asia/Hong_Kong", "Asia/Macau", "Asia/Taipei"}

    def test_every_iana_zone_of_the_three_countries_is_reachable(self):
        used = set(CITY_TO_TIMEZONE.values())
        for label, wanted in (("中国", self.IANA_CN | self.EXTRA_CN), ("俄罗斯", self.IANA_RU), ("日本", self.IANA_JP)):
            missing = wanted - used
            with self.subTest(country=label):
                self.assertEqual(missing, set(), f"{label}这些地方一个城市都没收：{sorted(missing)}")

    def test_minimum_regional_counts(self):
        cn = sum(1 for z in CITY_TO_TIMEZONE.values() if z in self.IANA_CN | self.EXTRA_CN)
        ru = sum(1 for z in CITY_TO_TIMEZONE.values() if z in self.IANA_RU)
        jp = sum(1 for z in CITY_TO_TIMEZONE.values() if z in self.IANA_JP)
        # 中国 333 个地级行政区 + 省直辖县级；俄罗斯 85 个联邦主体；日本 47 都道府县。
        self.assertGreaterEqual(cn, 380, f"中国只收了 {cn} 处，称不上全部地区")
        self.assertGreaterEqual(ru, 150, f"俄罗斯只收了 {ru} 处，称不上全部地区")
        self.assertGreaterEqual(jp, 100, f"日本只收了 {jp} 处，称不上全部地区")

    RUSSIAN_PLACES = (
        "莫斯科", "圣彼得堡", "塞瓦斯托波尔", "叶卡捷琳堡", "下诺夫哥罗德", "喀山", "乌法",
        "彼尔姆", "车里雅宾斯克", "奥伦堡", "秋明", "鄂木斯克", "新西伯利亚", "克拉斯诺亚尔斯克",
        "伊尔库茨克", "赤塔", "雅库茨克", "堪察加彼得罗巴甫洛夫斯克", "马加丹", "南萨哈林斯克",
        "哈巴罗夫斯克", "阿纳德尔", "符拉迪沃斯托克", "巴尔瑙尔", "克孜勒", "戈尔诺-阿尔泰斯克",
        "纳尔奇克", "埃利斯塔", "马哈奇卡拉", "格罗兹尼", "弗拉季高加索", "瑟克特夫卡尔",
        "彼得罗扎沃茨克", "阿尔汉格尔斯克", "摩尔曼斯克", "沃洛格达", "科斯特罗马", "伊万诺沃",
        "弗拉基米尔", "雅罗斯拉夫尔", "特维尔", "梁赞", "图拉", "卡卢加", "布良斯克", "斯摩棱斯克",
        "普斯科夫", "大诺夫哥罗德", "诺夫哥罗德", "坦波夫", "利佩茨克", "别尔哥罗德", "库尔斯克",
        "奥廖尔", "奔萨", "萨拉托夫", "萨马拉", "乌里扬诺夫斯克", "阿斯特拉罕", "伏尔加格勒",
        "顿河畔罗斯托夫", "克拉斯诺达尔", "斯塔夫罗波尔", "马加斯", "切尔克斯克", "五月镇",
        "汉德加", "奥廖克明斯克", "米尔内", "涅留恩格里", "布拉戈维申斯克", "乌苏里斯克",
        "比罗比詹", "佩韦克", "比利比诺", "诺里尔斯克", "迪克森", "伊加尔卡", "库尔干",
        "托木斯克", "克麦罗沃", "新库兹涅茨克", "阿巴坎", "加里宁格勒", "乌里扬诺夫斯克",
    )

    JAPANESE_PLACES = (
        "东京", "大阪", "京都", "名古屋", "札幌", "青森", "盛冈", "仙台", "秋田", "山形",
        "水户", "宇都宫", "前桥", "埼玉", "千叶", "横滨", "新潟", "富山", "金泽", "福井",
        "甲府", "长野", "岐阜", "静冈", "津", "大津", "神户", "鸟取", "松江", "冈山",
        "广岛", "德岛", "高知", "松山", "高松", "福冈", "佐贺", "长崎", "熊本", "大分",
        "宫崎", "鹿儿岛", "那霸", "川崎", "北九州", "堺", "滨松", "冈崎", "旭川", "八户",
    )

    CHINESE_PLACES = (
        "北京", "天津", "上海", "重庆", "石家庄", "太原", "呼和浩特", "沈阳", "长春", "哈尔滨",
        "南京", "杭州", "合肥", "福州", "南昌", "济南", "郑州", "武汉", "长沙", "广州",
        "南宁", "海口", "成都", "贵阳", "昆明", "拉萨", "西安", "兰州", "西宁", "银川",
        "乌鲁木齐", "台北", "香港", "澳门", "深圳", "大连", "青岛", "宁波", "厦门", "苏州",
        "喀什", "伊犁", "日喀则", "林芝", "阿里", "那曲", "昌都", "山南", "霍尔果斯", "石河子",
    )

    def test_russian_places_are_covered(self):
        missing = sorted({c for c in self.RUSSIAN_PLACES if c not in CITY_TO_TIMEZONE})
        self.assertEqual(missing, [], f"这些俄罗斯首府/城市没进表：{missing}")

    def test_japanese_places_are_covered(self):
        missing = sorted({c for c in self.JAPANESE_PLACES if CITY_TO_TIMEZONE.get(c) != "Asia/Tokyo"})
        self.assertEqual(missing, [], f"这些日本都道府县厅所在地/市没进表：{missing}")

    def test_chinese_places_are_covered(self):
        missing = sorted({c for c in self.CHINESE_PLACES if c not in CITY_TO_TIMEZONE})
        self.assertEqual(missing, [], f"这些中国地名没进表：{missing}")

    def test_display_name_exists_for_every_zone_in_the_table(self):
        """她说「你在伏尔加格勒」，而不是「你在 Europe/Volgograd」。"""
        from humanoid.data.cities import IANA_DISPLAY_NAMES

        missing = sorted({z for z in CITY_TO_TIMEZONE.values()} - set(IANA_DISPLAY_NAMES))
        self.assertEqual(missing, [], f"这些时区没有中文名可显示：{missing}")

    def test_iana_name_in_config_comes_out_as_a_city(self):
        clock = Clock(lambda: cfg(timezone_city="Europe/Volgograd"))
        self.assertEqual(clock.display_city, "伏尔加格勒")
        self.assertEqual(Clock(lambda: cfg(timezone_city="Atlantic/Reykjavik")).display_city, "雷克雅未克")
        # 表内地名原样说，不绕回时区名
        self.assertEqual(Clock(lambda: cfg(timezone_city="摩尔曼斯克")).display_city, "摩尔曼斯克")
        # 端到端：真走一次上下文编译
        text = core_with(mode="low", timezone_city="Europe/Volgograd").build_injection("42", is_group=False)
        self.assertIn("你在伏尔加格勒", text, f"上下文里的城市没跟着配置：{text}")

    def test_day_line_does_not_tell_her_when_to_sleep(self):
        """「夜里就该睡了」听着像谁在管她，事实只需要说「夜里要睡了」。"""
        core = core_with(mode="full")
        text = core.build_injection("42", is_group=False)
        self.assertNotIn("就该", text, text)

    def test_a_typoed_zone_name_says_typo_not_missing_tzdata(self):
        """填错名字不该让人去装 tzdata：两种退化的改法完全不同。"""
        clock_module._tzdata_usable.cache_clear()
        tz, note = resolve_zone("Asia/Volgograd")   # 真实名字是 Europe/Volgograd
        self.assertIsNone(tz)
        self.assertIn("拼错", note)
        self.assertNotIn("tzdata", note)

    def test_injection_has_no_technical_noise(self):
        """上下文里不该出现斜杠地名、UTC 偏移、百分比这类给人看的东西。"""
        for mode in ("low", "full", "mood_only"):
            core = wrecked_body(core_with(mode=mode))
            text = core.build_injection("42", is_group=False)
            with self.subTest(mode=mode):
                self.assertNotIn("/", text, f"{mode} 档出现了斜杠：{text}")
                self.assertNotIn("UTC", text, f"{mode} 档出现了偏移量：{text}")
                self.assertNotIn("%", text, f"{mode} 档出现了百分比：{text}")

    def test_iana_name_is_accepted_directly(self):
        self.assertEqual(resolve_zone_name("Asia/Ho_Chi_Minh"), "Asia/Ho_Chi_Minh")
        self.assertEqual(resolve_zone_name("America/New_York"), "America/New_York")

    def test_placeholder_and_unknown(self):
        self.assertIsNone(resolve_zone_name(DEFAULT_CITY_PLACEHOLDER))
        self.assertIsNone(resolve_zone_name(""))
        self.assertIsNone(resolve_zone_name("不存在的地方"))
        self.assertIsNone(resolve_zone_name("Asia"))
        self.assertIsNone(resolve_zone_name("12/34"))


class ZoneDegradationTest(unittest.TestCase):
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
        clock_module._tzdata_usable.cache_clear()
        try:
            tz, note = resolve_zone("东京")
            self.assertIsNone(tz)
            self.assertIn("tzdata", note)
            self.assertNotIn("Shanghai", note)
            self.assertEqual(now_in_city("东京").utcoffset(), datetime.now().astimezone().utcoffset())
        finally:
            clock_module.ZoneInfo = real
            resolve_zone.cache_clear()
            clock_module._tzdata_usable.cache_clear()

    def test_city_time_text_explains_the_degradation(self):
        result = lookup_city_time("不存在的地方")
        self.assertIsNotNone(result, "认不出城市时也该给出时间，只是要说明它是机器时间")
        self.assertIn("认不出城市", result.note)
        self.assertEqual(lookup_city_time("北京").note, "")

    def test_state_for_a_table_city(self):
        state = Clock(lambda: cfg(timezone_city="北京")).zone_state()
        self.assertEqual(state.zone_name, "Asia/Shanghai")
        self.assertEqual(state.offset_minutes, 480)
        self.assertEqual(state.note, "")

    def test_state_for_unknown_city_carries_the_reason(self):
        state = Clock(lambda: cfg(timezone_city="不存在的地方")).zone_state()
        self.assertEqual(state.zone_name, "")
        self.assertIn("认不出城市", state.note)


class ContractTimeTest(unittest.TestCase):
    def core(self, city: str = "北京", moment=MOMENT):
        from .test_link import Harness

        harness = Harness({"timezone_city": city})
        return harness, freeze(harness.roles.get_or_create("bot1"), moment)

    def test_contract_carries_her_real_offset(self):
        harness, core = self.core("北京")
        contract = build_contract(core)
        self.assertEqual(contract["time"]["utc_offset_minutes"], 480)
        self.assertEqual(contract["time"]["tz"], "Asia/Shanghai")
        asyncio.run(harness.roles.stop())

    def test_naive_moment_exports_none_not_zero(self):
        """当成 0 等于把她的城市当 UTC：社交层会静默按错的时间判断该不该说话。"""
        harness, core = self.core("北京", datetime(2026, 8, 22, 15, 20))
        contract = build_contract(core)
        self.assertIsNone(contract["time"]["utc_offset_minutes"])
        self.assertEqual(contract["time"]["tz"], "")
        asyncio.run(harness.roles.stop())

    def test_body_still_travels_in_the_contract(self):
        """不进上下文不等于不进契约：社交层靠这些轴决定要不要开口。"""
        harness, core = self.core("北京")
        wrecked_body(core)
        contract = build_contract(core)
        self.assertTrue(contract["body"]["asleep"])
        self.assertLess(contract["form"]["max_chars"], 60)
        self.assertTrue(contract["feelings"], "体感短句仍导出给社交层，只是不塞进聊天")
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
        clock_module._tzdata_usable.cache_clear()
        try:
            text = self.report("东京")
            self.assertIn("tzdata", text)
            self.assertIn("装一份时区数据库", text)
        finally:
            clock_module.ZoneInfo = real
            resolve_zone.cache_clear()
            clock_module._tzdata_usable.cache_clear()

    def test_report_warns_about_the_undecided_city(self):
        self.assertIn("还没定所在城市", self.report(DEFAULT_CITY_PLACEHOLDER))


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
