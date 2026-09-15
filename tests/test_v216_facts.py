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
from humanoid.state import StateStore

from .fakes import FakeContext, FrozenClock, RecordingLogger

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
    core.clock = FrozenClock(MOMENT)
    return core


def wrecked_body(core) -> HumanoidCoreInstance:
    """困到极点、饿、难受、被吵醒、刚被冷落：这些最容易让插件又开始下命令。"""
    core.soma.data.update(
        {
            "sleep_pressure": 99.0,
            "sleep_debt": 18.0,
            "hunger": 95.0,
            "discomfort": 80.0,
            "arousal": 10.0,
            "social_desire": 0.0,
            "asleep": 1.0,
            "ignored_streak": 4,
        }
    )
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

    def test_no_debug_format_leaks_into_context(self):
        """UTC 偏移、百分比、IANA 时区代码都是给主人核对用的，不该出现在她眼前。"""
        for mode in ("low", "full"):
            core = core_with(mode=mode)
            text = core.build_injection("42", is_group=False)
            with self.subTest(mode=mode):
                for word in ("UTC+", "UTC-", "%", "Asia/", "Europe/", "→"):
                    self.assertNotIn(word, text, f"{mode} 档把调试形式塞进了上下文：{text}")

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


if __name__ == "__main__":
    unittest.main()
