"""v2.16 的两条底线。

一、上下文里给的是她的身体、处境与生活，但**不能是台词、禁令与读数**：插件不替她把
话说完（「我现在需要休息，明天再聊吧」），不规定她怎么回（「不要接着聊」「这一轮控制在
20字」），也不把体检单端给她看（好感 46.0/100、社交能量 100%、UTC+08:00）。

二、时区不许静默退化：她说在哪个城市，就得按那个城市的钟点过日子，做不到要说出来。

这两类毛病共同点是不会报错：前者让她的话变成插件写的句子，后者让她说的时间与配置里的
城市差几个小时。所以断言基本都是负向的。

「有内容但没有命令与台词」这件事本身由 `tests/test_v2151_freedom.py` 守：它把身体推到
极端后渲染三档注入，逐项断言不含命令式句子与台词。本文件只守技术量与上下文形状。
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
from humanoid.data.cities import (
    CITY_TO_TIMEZONE,
    DEFAULT_CITY_PLACEHOLDER,
    IANA_DISPLAY_NAMES,
    lookup_city_time,
)
from humanoid.link import build_contract
from humanoid.state import StateStore

from .fakes import FakeContext, FrozenClock, RecordingLogger

TZ = ZoneInfo("Asia/Shanghai")
MOMENT = datetime(2026, 8, 22, 15, 20, tzinfo=TZ)
TODAY = MOMENT.strftime("%Y-%m-%d")

# 上下文里不该出现的读数与调试形式：那些是给主人核对用的，不是她说话时的形式。
TECHNICAL = ("UTC+", "UTC-", "%", "/100", "Asia/", "Europe/", "→")


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
    core.clock = FrozenClock(MOMENT)
    return core


def wrecked_body(core) -> HumanoidCoreInstance:
    """困到极点、饿、难受、被吵醒、刚被冷落：这些最容易让插件又开始下命令。"""
    # 「她在睡」得有一份此刻写着睡的日程撑着：soma.advance 会把没有日程支撑的
    # asleep_since 当成醒了清掉，而 snapshot(refresh=False) 也会跑 advance。
    # 直写 today_date/daily_schedule，绕开 _install 的夜间窗口重切。
    moment = datetime.fromtimestamp(core.soma.now, TZ)
    core.schedule._scope.update_self(
        today_date=moment.strftime("%Y-%m-%d"),
        daily_schedule=[
            {
                "start": "00:00",
                "end": "24:00",
                "event": "睡眠休息",
                "location": "卧室",
                "emotion": "平静",
                "energy_rate": 0.1,
            }
        ],
    )
    core.soma.data.update(
        {
            "sleep_pressure": 99.0,
            "sleep_debt": 18.0,
            "hunger": 95.0,
            "discomfort": 80.0,
            "arousal": 10.0,
            "social_desire": 0.0,
            "asleep_since": core.soma.now - 3600.0,
            "ignored_streak": 4,
        }
    )
    core.mood.profile("42").update(
        {"affection": 30.0, "libido": 40.0, "aggression": 45.0,
         "base_affection": 46.0, "base_aggression": 28.0}
    )
    core.mood.set_nickname("42", "小鱼")
    return core


class ContextShapeTest(unittest.TestCase):
    """上下文该有她的身体与处境，但不该有读数与调试形式。"""

    def test_no_debug_format_leaks_into_context(self):
        """UTC 偏移、百分比、/100、IANA 时区代码都不该出现在她眼前。"""
        for mode in ("low", "full", "mood_only"):
            core = wrecked_body(core_with(mode=mode))
            text = core.build_injection("42", is_group=False)
            with self.subTest(mode=mode):
                for word in TECHNICAL:
                    self.assertNotIn(word, text, f"{mode} 档把调试形式塞进了上下文：{text}")

    def test_body_and_mood_still_computed_even_though_not_reported_as_numbers(self):
        """不进上下文不等于不算：身体与情绪照旧在跑，契约照旧导出。"""
        core = wrecked_body(core_with())
        snap = core.soma.snapshot()
        self.assertGreater(float(snap["sleep_pressure"]), 90.0, "身体照旧在攒困意")
        self.assertGreater(float(snap["hunger"]), 90.0)
        self.assertAlmostEqual(float(core.mood.profile("42")["aggression"]), 45.0)
        contract = core.refresh_contract()
        self.assertTrue(contract, "契约还得照常导出，主动社交靠它")
        self.assertIn("body", contract)
        self.assertIsNotNone(contract["form"].get("max_chars"), "形式倾向仍给社交层")

    def test_default_tier_carries_time_gap_and_nickname(self):
        core = core_with()
        core.mood.set_nickname("42", "小鱼")
        core.behavior.add_event("42", {
            "type": "long_gap", "timestamp": core.soma.now, "importance": 0.9,
            "data": {"gap_seconds": 11520.0, "previous_message": "我猫今天吐了"},
        })
        text = core.build_injection("42", is_group=False)
        self.assertIn("2026-08-22 15:20", text)
        self.assertIn("小鱼", text)
        self.assertIn("3 小时 12 分", text)
        self.assertIn("我猫今天吐了", text, "离开前原话是间隔实用性的另一半")

    def test_interval_is_an_exact_duration_not_a_bucket_phrase(self):
        """「对方隔了约2～6小时重新出现」把一件客观事描成情境，报准就好。"""
        core = core_with()
        core.behavior.add_event("42", {
            "type": "long_gap", "timestamp": core.soma.now, "importance": 0.9,
            "data": {"gap_seconds": 9000.0},
        })
        text = core.build_injection("42", is_group=False)
        self.assertIn("2 小时 30 分", text)
        for vague in ("约2～6小时", "约30分钟～2小时", "6小时以上"):
            self.assertNotIn(vague, text)


class CityTableTest(unittest.TestCase):
    """三个国家的地区必须全部收录：她说得出地名，就得对得上那个地名的钟点。"""

    # 国家统计局口径的 333 个地级行政区**全名**（带「市/地区/盟/自治州」后缀）。
    # 用全名而不是裸名来断言，是因为用户就是会填全名：「延边朝鲜族自治州」必须也能认出来。
    CHINA_PREFECTURES = (
    "石家庄市", "唐山市", "秦皇岛市", "邯郸市", "邢台市", "保定市",
    "张家口市", "承德市", "沧州市", "廊坊市", "衡水市", "太原市",
    "大同市", "阳泉市", "长治市", "晋城市", "朔州市", "晋中市",
    "运城市", "忻州市", "临汾市", "吕梁市", "呼和浩特市", "包头市",
    "乌海市", "赤峰市", "通辽市", "鄂尔多斯市", "呼伦贝尔市", "巴彦淖尔市",
    "乌兰察布市", "兴安盟", "锡林郭勒盟", "阿拉善盟", "沈阳市", "大连市",
    "鞍山市", "抚顺市", "本溪市", "丹东市", "锦州市", "营口市",
    "阜新市", "辽阳市", "盘锦市", "铁岭市", "朝阳市", "葫芦岛市",
    "长春市", "吉林市", "四平市", "辽源市", "通化市", "白山市",
    "松原市", "白城市", "延边朝鲜族自治州", "哈尔滨市", "齐齐哈尔市", "鸡西市",
    "鹤岗市", "双鸭山市", "大庆市", "伊春市", "佳木斯市", "七台河市",
    "牡丹江市", "黑河市", "绥化市", "大兴安岭地区", "南京市", "无锡市",
    "徐州市", "常州市", "苏州市", "南通市", "连云港市", "淮安市",
    "盐城市", "扬州市", "镇江市", "泰州市", "宿迁市", "杭州市",
    "宁波市", "温州市", "嘉兴市", "湖州市", "绍兴市", "金华市",
    "衢州市", "舟山市", "台州市", "丽水市", "合肥市", "芜湖市",
    "蚌埠市", "淮南市", "马鞍山市", "淮北市", "铜陵市", "安庆市",
    "黄山市", "滁州市", "阜阳市", "宿州市", "六安市", "亳州市",
    "池州市", "宣城市", "福州市", "厦门市", "莆田市", "三明市",
    "泉州市", "漳州市", "南平市", "龙岩市", "宁德市", "南昌市",
    "景德镇市", "萍乡市", "九江市", "新余市", "鹰潭市", "赣州市",
    "吉安市", "宜春市", "抚州市", "上饶市", "济南市", "青岛市",
    "淄博市", "枣庄市", "东营市", "烟台市", "潍坊市", "济宁市",
    "泰安市", "威海市", "日照市", "临沂市", "德州市", "聊城市",
    "滨州市", "菏泽市", "郑州市", "开封市", "洛阳市", "平顶山市",
    "安阳市", "鹤壁市", "新乡市", "焦作市", "濮阳市", "许昌市",
    "漯河市", "三门峡市", "南阳市", "商丘市", "信阳市", "周口市",
    "驻马店市", "武汉市", "黄石市", "十堰市", "宜昌市", "襄阳市",
    "鄂州市", "荆门市", "孝感市", "荆州市", "黄冈市", "咸宁市",
    "随州市", "恩施土家族苗族自治州", "长沙市", "株洲市", "湘潭市", "衡阳市",
    "邵阳市", "岳阳市", "常德市", "张家界市", "益阳市", "郴州市",
    "永州市", "怀化市", "娄底市", "湘西土家族苗族自治州", "广州市", "韶关市",
    "深圳市", "珠海市", "汕头市", "佛山市", "江门市", "湛江市",
    "茂名市", "肇庆市", "惠州市", "梅州市", "汕尾市", "河源市",
    "阳江市", "清远市", "东莞市", "中山市", "潮州市", "揭阳市",
    "云浮市", "南宁市", "柳州市", "桂林市", "梧州市", "北海市",
    "防城港市", "钦州市", "贵港市", "玉林市", "百色市", "贺州市",
    "河池市", "来宾市", "崇左市", "海口市", "三亚市", "三沙市",
    "儋州市", "成都市", "自贡市", "攀枝花市", "泸州市", "德阳市",
    "绵阳市", "广元市", "遂宁市", "内江市", "乐山市", "南充市",
    "眉山市", "宜宾市", "广安市", "达州市", "雅安市", "巴中市",
    "资阳市", "阿坝藏族羌族自治州", "甘孜藏族自治州", "凉山彝族自治州", "贵阳市", "六盘水市",
    "遵义市", "安顺市", "毕节市", "铜仁市", "黔西南布依族苗族自治州", "黔东南苗族侗族自治州",
    "黔南布依族苗族自治州", "昆明市", "曲靖市", "玉溪市", "保山市", "昭通市",
    "丽江市", "普洱市", "临沧市", "楚雄彝族自治州", "红河哈尼族彝族自治州", "文山壮族苗族自治州",
    "西双版纳傣族自治州", "大理白族自治州", "德宏傣族景颇族自治州", "怒江傈僳族自治州", "迪庆藏族自治州", "拉萨市",
    "日喀则市", "昌都市", "林芝市", "山南市", "那曲市", "阿里地区",
    "西安市", "铜川市", "宝鸡市", "咸阳市", "渭南市", "延安市",
    "汉中市", "榆林市", "安康市", "商洛市", "兰州市", "嘉峪关市",
    "金昌市", "白银市", "天水市", "武威市", "张掖市", "平凉市",
    "酒泉市", "庆阳市", "定西市", "陇南市", "临夏回族自治州", "甘南藏族自治州",
    "西宁市", "海东市", "海北藏族自治州", "黄南藏族自治州", "海南藏族自治州", "果洛藏族自治州",
    "玉树藏族自治州", "海西蒙古族藏族自治州", "银川市", "石嘴山市", "吴忠市", "固原市",
    "中卫市", "乌鲁木齐市", "克拉玛依市", "吐鲁番市", "哈密市", "昌吉回族自治州",
    "博尔塔拉蒙古自治州", "巴音郭楞蒙古自治州", "阿克苏地区", "克孜勒苏柯尔克孜自治州", "喀什地区", "和田地区",
    "伊犁哈萨克自治州", "塔城地区", "阿勒泰地区",    )

    def test_every_china_prefecture_resolves(self):
        """中国全部地级行政区：一个都不能掉，掉了她就说不出自己那边几点。"""
        self.assertEqual(len(set(self.CHINA_PREFECTURES)), 333)
        bad = [name for name in self.CHINA_PREFECTURES if resolve_zone_name(name) is None]
        self.assertEqual(bad, [], f"这些地级行政区认不出：{bad}")

    def test_china_provinces_resolves(self):
        """她说「我在广东」而不是「我在广州」，高一级地名同样要有钟点。"""
        for name in ("北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江",
                     "上海", "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南",
                     "湖北", "湖南", "广东", "广西", "海南", "重庆", "四川", "贵州",
                     "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆",
                     "台湾", "香港", "澳门"):
            with self.subTest(name=name):
                self.assertIsNotNone(resolve_zone_name(name))
        self.assertEqual(resolve_zone_name("台湾"), "Asia/Taipei")
        self.assertEqual(resolve_zone_name("香港"), "Asia/Hong_Kong")
        self.assertEqual(resolve_zone_name("澳门"), "Asia/Macau")
        # 中国大陆法定统一北京时间，新疆与西藏也是（想按当地作息填 Asia/Urumqi）
        self.assertEqual(resolve_zone_name("乌鲁木齐"), "Asia/Shanghai")
        self.assertEqual(resolve_zone_name("拉萨"), "Asia/Shanghai")
        self.assertEqual(resolve_zone_name("乌鲁木齐（当地作息）"), "Asia/Urumqi")

    # 85 个联邦主体的首府（含联邦市与自治专区），逐条按 GeoNames RU.txt 的
    # PPLA/PPLA2 记录核对：她说「我在楚科奇」也得报对那边的钟点。
    RU_SUBJECT_CAPITALS = (
        ("迈科普", "Europe/Moscow"), ("戈尔诺-阿尔泰斯克", "Asia/Barnaul"), ("巴尔瑙尔", "Asia/Barnaul"),
        ("布拉戈维申斯克", "Asia/Yakutsk"), ("阿尔汉格尔斯克", "Europe/Moscow"), ("阿斯特拉罕", "Europe/Astrakhan"),
        ("乌法", "Asia/Yekaterinburg"), ("别尔哥罗德", "Europe/Moscow"), ("布良斯克", "Europe/Moscow"),
        ("乌兰乌德", "Asia/Irkutsk"), ("格罗兹尼", "Europe/Moscow"), ("车里雅宾斯克", "Asia/Yekaterinburg"),
        ("阿纳德尔", "Asia/Anadyr"), ("切博克萨雷", "Europe/Moscow"), ("马哈奇卡拉", "Europe/Moscow"),
        ("马加斯", "Europe/Moscow"), ("伊尔库茨克", "Asia/Irkutsk"), ("伊万诺沃", "Europe/Moscow"),
        ("纳尔奇克", "Europe/Moscow"), ("加里宁格勒", "Europe/Kaliningrad"), ("埃利斯塔", "Europe/Moscow"),
        ("卡卢加", "Europe/Moscow"), ("切尔克斯克", "Europe/Moscow"), ("彼得罗扎沃茨克", "Europe/Moscow"),
        ("克麦罗沃", "Asia/Novokuznetsk"), ("哈巴罗夫斯克", "Asia/Vladivostok"), ("阿巴坎", "Asia/Krasnoyarsk"),
        ("汉特-曼西斯克", "Asia/Yekaterinburg"), ("基洛夫", "Europe/Kirov"), ("瑟克特夫卡尔", "Europe/Moscow"),
        ("科斯特罗马", "Europe/Moscow"), ("克拉斯诺达尔", "Europe/Moscow"), ("库尔干", "Asia/Yekaterinburg"),
        ("库尔斯克", "Europe/Moscow"), ("加特契纳", "Europe/Moscow"), ("利佩茨克", "Europe/Moscow"),
        ("马加丹", "Asia/Magadan"), ("约什卡尔奥拉", "Europe/Moscow"), ("萨兰斯克", "Europe/Moscow"),
        ("希姆基", "Europe/Moscow"), ("摩尔曼斯克", "Europe/Moscow"), ("纳尔扬-马尔", "Europe/Moscow"),
        ("下诺夫哥罗德", "Europe/Moscow"), ("大诺夫哥罗德", "Europe/Moscow"), ("新西伯利亚", "Asia/Novosibirsk"),
        ("鄂木斯克", "Asia/Omsk"), ("奥伦堡", "Asia/Yekaterinburg"), ("奥廖尔", "Europe/Moscow"),
        ("奔萨", "Europe/Moscow"), ("符拉迪沃斯托克", "Asia/Vladivostok"), ("普斯科夫", "Europe/Moscow"),
        ("顿河畔罗斯托夫", "Europe/Moscow"), ("梁赞", "Europe/Moscow"), ("雅库茨克", "Asia/Yakutsk"),
        ("南萨哈林斯克", "Asia/Sakhalin"), ("萨马拉", "Europe/Samara"), ("圣彼得堡", "Europe/Moscow"),
        ("萨拉托夫", "Europe/Saratov"), ("弗拉季高加索", "Europe/Moscow"), ("斯摩棱斯克", "Europe/Moscow"),
        ("斯塔夫罗波尔", "Europe/Moscow"), ("叶卡捷琳堡", "Asia/Yekaterinburg"), ("坦波夫", "Europe/Moscow"),
        ("喀山", "Europe/Moscow"), ("托木斯克", "Asia/Tomsk"), ("图拉", "Europe/Moscow"),
        ("特维尔", "Europe/Moscow"), ("秋明", "Asia/Yekaterinburg"), ("克孜勒", "Asia/Krasnoyarsk"),
        ("伊热夫斯克", "Europe/Samara"), ("乌里扬诺夫斯克", "Europe/Ulyanovsk"), ("弗拉基米尔", "Europe/Moscow"),
        ("伏尔加格勒", "Europe/Volgograd"), ("沃洛格达", "Europe/Moscow"), ("沃罗涅日", "Europe/Moscow"),
        ("萨列哈尔德", "Asia/Yekaterinburg"), ("雅罗斯拉夫尔", "Europe/Moscow"), ("比罗比詹", "Asia/Vladivostok"),
        ("彼尔姆", "Asia/Yekaterinburg"), ("克拉斯诺亚尔斯克", "Asia/Krasnoyarsk"), ("彼得罗巴甫洛夫斯克", "Asia/Kamchatka"),
        ("赤塔", "Asia/Chita"), ("莫斯科", "Europe/Moscow"), ("辛菲罗波尔", "Europe/Simferopol"),
        ("塞瓦斯托波尔", "Europe/Simferopol"),
    )

    # 远北这几个是旧表真正错过的地方：Asia/Khandyga 只覆盖上科雷马以西的两个区，
    # 把滕达/涅留恩格里/阿尔丹/奥廖克明斯克算进去会让她们那边走快一小时。
    RU_TRICKY_TOWNS = (
        ("滕达", "Asia/Yakutsk"), ("涅留恩格里", "Asia/Yakutsk"),
        ("阿尔丹", "Asia/Yakutsk"), ("奥廖克明斯克", "Asia/Yakutsk"),
        ("恰拉", "Asia/Chita"), ("比利比诺", "Asia/Anadyr"),
        ("汉德加", "Asia/Khandyga"), ("乌斯季-马亚", "Asia/Khandyga"),
        ("乌斯季-涅拉", "Asia/Ust-Nera"), ("奥伊米亚康", "Asia/Ust-Nera"),
        ("中科雷马", "Asia/Srednekolymsk"), ("米尔内", "Asia/Yakutsk"),
    )

    def test_every_russia_subject_capital_resolves(self):
        for name, zone in self.RU_SUBJECT_CAPITALS:
            with self.subTest(city=name):
                self.assertEqual(resolve_zone_name(name), zone)

    def test_far_north_towns_keep_the_right_zone(self):
        for name, zone in self.RU_TRICKY_TOWNS:
            with self.subTest(city=name):
                self.assertEqual(resolve_zone_name(name), zone)

    def test_russia_spans_all_eleven_zones(self):
        wanted = {"Europe/Kaliningrad", "Europe/Moscow", "Europe/Samara", "Asia/Yekaterinburg",
                  "Asia/Omsk", "Asia/Krasnoyarsk", "Asia/Irkutsk", "Asia/Yakutsk",
                  "Asia/Vladivostok", "Asia/Magadan", "Asia/Kamchatka"}
        got = {z for z in set(CITY_TO_TIMEZONE.values()) if z in wanted}
        self.assertEqual(got, wanted, "俄罗斯 11 个时区没覆盖全")
        self.assertGreaterEqual(sum(1 for z in CITY_TO_TIMEZONE.values() if z in wanted), 60,
                                "俄罗斯只收了几个城市，谈不上「全部地区」")

    # 47 都道府县的**官方名**（含 北海道/东京都/大阪府/京都府）与各自县厅所在地
    JP47 = ("北海道", "青森县", "岩手县", "宫城县", "秋田县", "山形县", "福岛县", "茨城县",
            "栃木县", "群马县", "埼玉县", "千叶县", "东京都", "神奈川县", "新潟县", "富山县",
            "石川县", "福井县", "山梨县", "长野县", "岐阜县", "静冈县", "爱知县", "三重县",
            "滋贺县", "京都府", "大阪府", "兵库县", "奈良县", "和歌山县", "鸟取县", "岛根县",
            "冈山县", "广岛县", "山口县", "德岛县", "香川县", "爱媛县", "高知县", "福冈县",
            "佐贺县", "长崎县", "熊本县", "大分县", "宫崎县", "鹿儿岛县", "冲绳县")
    JP47_SEATS = ("札幌", "青森", "盛冈", "仙台", "秋田", "山形", "福岛", "水户", "宇都宫",
                  "前桥", "埼玉", "千叶", "东京", "横滨", "新潟", "富山", "金泽", "福井",
                  "甲府", "长野", "岐阜", "静冈", "名古屋", "津", "大津", "京都", "大阪",
                  "神户", "奈良", "和歌山", "鸟取", "松江", "冈山", "广岛", "德岛", "高松",
                  "松山", "高知", "福冈", "佐贺", "长崎", "熊本", "大分", "宫崎", "山口",
                  "鹿儿岛", "那霸")

    def test_every_japan_prefecture_and_seat_resolves(self):
        """日本全部 47 都道府县：官方名与县厅所在地都要能认，且都是 Asia/Tokyo。"""
        self.assertEqual(len(self.JP47), 47)
        self.assertEqual(len(self.JP47_SEATS), 47)
        for name in self.JP47 + self.JP47_SEATS:
            with self.subTest(name=name):
                self.assertEqual(resolve_zone_name(name), "Asia/Tokyo")

    def test_japanese_suffix_forms_resolve(self):
        for name in ("大阪市", "东京都", "札幌市", "横滨市", "福冈市", "北海道"):
            self.assertEqual(resolve_zone_name(name), "Asia/Tokyo", name)

    def test_every_entry_is_a_loadable_zone(self):
        for name in set(CITY_TO_TIMEZONE.values()):
            resolve_zone.cache_clear()
            tz, note = resolve_zone(name)
            with self.subTest(zone=name):
                self.assertIsNotNone(tz, f"表里这个 IANA 名机器加载不了：{name}（{note}）")

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
        try:
            tz, note = resolve_zone("东京")
            self.assertIsNone(tz)
            self.assertIn("tzdata", note)
            self.assertNotIn("Shanghai", note)
            self.assertEqual(now_in_city("东京").utcoffset(), datetime.now().astimezone().utcoffset())
        finally:
            clock_module.ZoneInfo = real
            resolve_zone.cache_clear()

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
        try:
            text = self.report("东京")
            self.assertIn("tzdata", text)
            self.assertIn("装一份时区数据库", text)
        finally:
            clock_module.ZoneInfo = real
            resolve_zone.cache_clear()

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


class RegionBulkTest(unittest.TestCase):
    """v2.16.4：批量地名表 + 俄罗斯 85 个联邦主体。她说得出地名就必须对得上钟点。"""

    # 85 个联邦主体名（用户会怎么写就怎么写：裸名与官方写法各测一种）
    RU_SUBJECTS = (
        ("斯维尔德洛夫斯克州", "Asia/Yekaterinburg"), ("萨哈林州", "Asia/Sakhalin"),
        ("堪察加边疆区", "Asia/Kamchatka"), ("哈巴罗夫斯克边疆区", "Asia/Vladivostok"),
        ("滨海边疆区", "Asia/Vladivostok"), ("外贝加尔边疆区", "Asia/Chita"),
        ("阿尔泰边疆区", "Asia/Barnaul"), ("彼尔姆边疆区", "Asia/Yekaterinburg"),
        ("克拉斯诺达尔边疆区", "Europe/Moscow"), ("斯塔夫罗波尔边疆区", "Europe/Moscow"),
        ("鞑靼斯坦共和国", "Europe/Moscow"), ("巴什科尔托斯坦共和国", "Asia/Yekaterinburg"),
        ("萨哈共和国", "Asia/Yakutsk"), ("雅库特", "Asia/Yakutsk"),
        ("布里亚特共和国", "Asia/Irkutsk"), ("图瓦共和国", "Asia/Krasnoyarsk"),
        ("哈卡斯共和国", "Asia/Krasnoyarsk"), ("阿尔泰共和国", "Asia/Barnaul"),
        ("车臣共和国", "Europe/Moscow"), ("达吉斯坦共和国", "Europe/Moscow"),
        ("印古什共和国", "Europe/Moscow"), ("卡巴尔达-巴尔卡尔共和国", "Europe/Moscow"),
        ("北奥塞梯-阿兰共和国", "Europe/Moscow"), ("卡拉恰伊-切尔克斯共和国", "Europe/Moscow"),
        ("阿迪格共和国", "Europe/Moscow"), ("卡尔梅克共和国", "Europe/Moscow"),
        ("克里米亚共和国", "Europe/Simferopol"), ("塞瓦斯托波尔", "Europe/Simferopol"),
        ("楚瓦什共和国", "Europe/Moscow"), ("马里埃尔共和国", "Europe/Moscow"),
        ("莫尔多瓦共和国", "Europe/Moscow"), ("乌德穆尔特共和国", "Europe/Samara"),
        ("科米共和国", "Europe/Moscow"), ("卡累利阿共和国", "Europe/Moscow"),
        ("莫斯科州", "Europe/Moscow"), ("列宁格勒州", "Europe/Moscow"),
        ("新西伯利亚州", "Asia/Novosibirsk"), ("鄂木斯克州", "Asia/Omsk"),
        ("托木斯克州", "Asia/Tomsk"), ("克麦罗沃州", "Asia/Novokuznetsk"),
        ("伊尔库茨克州", "Asia/Irkutsk"), ("阿穆尔州", "Asia/Yakutsk"),
        ("马加丹州", "Asia/Magadan"), ("萨哈林州", "Asia/Sakhalin"),
        ("犹太自治州", "Asia/Vladivostok"), ("汉特-曼西自治区", "Asia/Yekaterinburg"),
        ("亚马尔-涅涅茨自治区", "Asia/Yekaterinburg"), ("涅涅茨自治区", "Europe/Moscow"),
        ("楚科奇自治区", "Asia/Anadyr"), ("加里宁格勒州", "Europe/Kaliningrad"),
        ("伏尔加格勒州", "Europe/Volgograd"), ("阿斯特拉罕州", "Europe/Astrakhan"),
        ("萨拉托夫州", "Europe/Saratov"), ("萨马拉州", "Europe/Samara"),
        ("乌里扬诺夫斯克州", "Europe/Ulyanovsk"), ("基洛夫州", "Europe/Kirov"),
        ("下诺夫哥罗德州", "Europe/Moscow"), ("沃罗涅德州", "Europe/Moscow"),
        ("罗斯托夫州", "Europe/Moscow"), ("梁赞州", "Europe/Moscow"),
        ("坦波夫州", "Europe/Moscow"), ("奔萨州", "Europe/Moscow"),
        ("利佩茨克州", "Europe/Moscow"), ("奥廖尔州", "Europe/Moscow"),
        ("布良斯克州", "Europe/Moscow"), ("斯摩棱斯克州", "Europe/Moscow"),
        ("特维尔州", "Europe/Moscow"), ("雅罗斯拉夫尔州", "Europe/Moscow"),
        ("科斯特罗马州", "Europe/Moscow"), ("伊万诺沃州", "Europe/Moscow"),
        ("弗拉基米尔州", "Europe/Moscow"), ("卡卢加州", "Europe/Moscow"),
        ("图拉州", "Europe/Moscow"), ("沃洛格达州", "Europe/Moscow"),
        ("摩尔曼斯克州", "Europe/Moscow"), ("阿尔汉格尔斯克州", "Europe/Moscow"),
        ("库尔干州", "Asia/Yekaterinburg"), ("秋明州", "Asia/Yekaterinburg"),
        ("车里雅宾斯克州", "Asia/Yekaterinburg"), ("奥伦堡州", "Asia/Yekaterinburg"),
        ("圣彼得堡", "Europe/Moscow"),
    )

    def test_every_russian_subject_resolves(self):
        for name, zone in self.RU_SUBJECTS:
            self.assertEqual(resolve_zone_name(name), zone, f"{name} 认不出或给错了区")

    def test_subject_count_covers_the_federation(self):
        self.assertGreaterEqual(len({name for name, _ in self.RU_SUBJECTS}), 80)
        self.assertGreaterEqual(len(CITY_TO_TIMEZONE), 5000, "批量表没并进解析层")

    def test_china_county_level_places_resolve(self):
        """县级以上一个都不该掉：这些都是会被直接填进配置的名字。"""
        for name in ("敦煌", "满洲里", "乌兰浩特", "霍林郭勒", "二连浩特", "曲阜", "阳朔",
                     "婺源", "香格里拉", "井冈山", "喀什", "伊宁", "延吉", "都江堰", "平遥"):
            self.assertEqual(resolve_zone_name(name), "Asia/Shanghai", f"{name} 认不出")

    def test_japanese_city_names_resolve(self):
        """日本全国一个区时，所以市町村名与简体/繁体两种写法都该认。"""
        for name in ("札幌", "川崎", "横滨", "横浜", "那霸", "那覇", "箱根", "轻井泽",
                     "盛冈", "八户", "小樽", "富良野", "由布院"):
            self.assertEqual(resolve_zone_name(name), "Asia/Tokyo", f"{name} 认不出")

    def test_russian_cities_in_simplified_chinese(self):
        for name, zone in (("摩尔曼斯克", "Europe/Moscow"), ("雅库茨克", "Asia/Yakutsk"),
                           ("乌法", "Asia/Yekaterinburg"), ("马加斯", "Europe/Moscow"),
                           ("纳尔奇克", "Europe/Moscow"), ("南萨哈林斯克", "Asia/Sakhalin"),
                           ("伯力", "Asia/Vladivostok"), ("海参崴", "Asia/Vladivostok")):
            self.assertEqual(resolve_zone_name(name), zone, f"{name} 认不出")

    def test_display_names_can_be_typed_back(self):
        """她说「你在雷克雅未克」，用户把这几个字填回去就必须生效，不能静默退化。"""
        for zone, display in IANA_DISPLAY_NAMES.items():
            bare = display.split("（")[0].strip()
            if not bare or bare == "UTC":
                continue
            self.assertIsNotNone(resolve_zone_name(bare), f"显示名「{bare}」填不回去")

    def test_urumqi_name_keeps_beijing_time_policy(self):
        """「乌鲁木齐」这个中文名同时对应两件事：法定北京时间（写名字）与 UTC+6（写 IANA 名）。
        名字必须归北京时间，想要当地作息的人填 Asia/Urumqi——这是文档里写明的选择。"""
        self.assertEqual(resolve_zone_name("乌鲁木齐"), "Asia/Shanghai")
        self.assertEqual(resolve_zone_name("Asia/Urumqi"), "Asia/Urumqi")

    def test_unrecognised_place_says_so_instead_of_lying(self):
        tz, note = resolve_zone("肯定不存在的地方名")
        self.assertIsNone(tz)
        self.assertIn("认不出", note)

    def test_bulk_table_never_shadows_a_hand_checked_entry(self):
        """手写表里逐条对过 IANA 自注的结论，不能被 GeoNames 的默认值盖掉。"""
        for name, zone in (("滕达", "Asia/Yakutsk"), ("恰拉", "Asia/Chita"),
                           ("比利比诺", "Asia/Anadyr"), ("汉德加", "Asia/Khandyga")):
            self.assertEqual(CITY_TO_TIMEZONE.get(name), zone, f"{name} 被批量表改写了")

    def test_bulk_data_is_compact_and_loadable(self):
        from humanoid.data.cities_bulk import _BULK_BY_ZONE

        self.assertGreaterEqual(len(_BULK_BY_ZONE), 20)
        total = sum(len(blob.split("|")) for blob in _BULK_BY_ZONE.values())
        self.assertGreater(total, 5000)


if __name__ == "__main__":
    unittest.main()
