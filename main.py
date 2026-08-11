import os
import json
import logging
import threading
import urllib.request
import urllib.parse
import urllib.error
import re
import hashlib
import asyncio
import random
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api.star import Context, Star
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.provider.entities import ProviderRequest

SHA_TZ = timezone(timedelta(hours=8))

# ======================== 备用日程模板 ========================
FALLBACK_TEMPLATES = [
    [
        {"start": "00:00", "end": "07:30", "event": "深度睡眠", "location": "卧室", "emotion": "沉睡/安详", "energy_rate": 0.15},
        {"start": "07:30", "end": "08:30", "event": "起床洗漱与吃早餐", "location": "餐厅", "emotion": "清醒中", "energy_rate": 0.05},
        {"start": "08:30", "end": "12:00", "event": "专注工作/处理事务", "location": "书房/工作区", "emotion": "认真/专注", "energy_rate": -0.1},
        {"start": "12:00", "end": "13:30", "event": "午餐与午休发呆", "location": "客厅/阳台", "emotion": "惬意/放松", "energy_rate": 0.08},
        {"start": "13:30", "end": "18:00", "event": "下午工作沟通与处理", "location": "书房/工作区", "emotion": "专注/稍显疲惫", "energy_rate": -0.1},
        {"start": "18:00", "end": "22:00", "event": "个人自由时间", "location": "客厅", "emotion": "轻松/惬意", "energy_rate": -0.02},
        {"start": "22:00", "end": "24:00", "event": "夜间洗漱准备睡觉", "location": "卧室", "emotion": "困倦/慵懒", "energy_rate": -0.05}
    ],
    [
        {"start": "00:00", "end": "08:00", "event": "安稳睡眠", "location": "卧室", "emotion": "沉睡", "energy_rate": 0.18},
        {"start": "08:00", "end": "09:00", "event": "起床、洗漱、简单早餐", "location": "厨房", "emotion": "逐渐清醒", "energy_rate": 0.03},
        {"start": "09:00", "end": "12:30", "event": "高效工作/学习", "location": "书房", "emotion": "专注认真", "energy_rate": -0.12},
        {"start": "12:30", "end": "14:00", "event": "午餐与休息", "location": "客厅", "emotion": "放松", "energy_rate": 0.1},
        {"start": "14:00", "end": "18:00", "event": "继续工作/项目", "location": "书房", "emotion": "略显疲惫但仍坚持", "energy_rate": -0.08},
        {"start": "18:00", "end": "22:30", "event": "晚餐及休闲娱乐", "location": "客厅", "emotion": "愉快", "energy_rate": -0.01},
        {"start": "22:30", "end": "24:00", "event": "准备入睡", "location": "卧室", "emotion": "困倦", "energy_rate": -0.03}
    ],
    [
        {"start": "00:00", "end": "09:00", "event": "懒觉", "location": "卧室", "emotion": "香甜", "energy_rate": 0.2},
        {"start": "09:00", "end": "10:00", "event": "悠闲早午餐", "location": "餐厅", "emotion": "满足", "energy_rate": 0.06},
        {"start": "10:00", "end": "14:00", "event": "户外散步或阅读", "location": "户外/阳台", "emotion": "轻松", "energy_rate": -0.05},
        {"start": "14:00", "end": "17:00", "event": "午休或娱乐", "location": "客厅", "emotion": "惬意", "energy_rate": 0.02},
        {"start": "17:00", "end": "21:00", "event": "社交/游戏/电影", "location": "客厅/影院", "emotion": "兴奋", "energy_rate": -0.1},
        {"start": "21:00", "end": "24:00", "event": "洗漱、刷手机、入睡", "location": "卧室", "emotion": "慵懒", "energy_rate": -0.02}
    ]
]

def get_fallback_schedule(today_str: str) -> list:
    seed = int(hashlib.md5(today_str.encode()).hexdigest()[:8], 16)
    return FALLBACK_TEMPLATES[seed % len(FALLBACK_TEMPLATES)]

def extract_json_from_response(raw_res: str) -> list:
    try:
        parsed = json.loads(raw_res)
        if isinstance(parsed, list):
            return parsed
    except:
        pass
    match = re.search(r'(\[\s*\{.*?\}\s*\])', raw_res, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(1))
            if isinstance(parsed, list):
                return parsed
        except:
            pass
    start = raw_res.find('[')
    end = raw_res.rfind(']')
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(raw_res[start:end+1])
            if isinstance(parsed, list):
                return parsed
        except:
            pass
    return []

# ======================== 城市映射表（已扩展至中日俄主要城市） ========================
CITY_TO_TIMEZONE = {
    # ===== 中国（直辖市 + 地级市 + 港澳台） =====
    "北京": "Asia/Shanghai", "上海": "Asia/Shanghai", "天津": "Asia/Shanghai",
    "重庆": "Asia/Shanghai", "哈尔滨": "Asia/Shanghai", "长春": "Asia/Shanghai",
    "沈阳": "Asia/Shanghai", "呼和浩特": "Asia/Shanghai", "乌鲁木齐": "Asia/Urumqi",
    "银川": "Asia/Shanghai", "西宁": "Asia/Shanghai", "兰州": "Asia/Shanghai",
    "西安": "Asia/Shanghai", "太原": "Asia/Shanghai", "石家庄": "Asia/Shanghai",
    "济南": "Asia/Shanghai", "郑州": "Asia/Shanghai", "南京": "Asia/Shanghai",
    "合肥": "Asia/Shanghai", "武汉": "Asia/Shanghai", "长沙": "Asia/Shanghai",
    "南昌": "Asia/Shanghai", "福州": "Asia/Shanghai", "台北": "Asia/Taipei",
    "广州": "Asia/Shanghai", "南宁": "Asia/Shanghai", "海口": "Asia/Shanghai",
    "成都": "Asia/Shanghai", "贵阳": "Asia/Shanghai", "昆明": "Asia/Shanghai",
    "拉萨": "Asia/Urumqi", "香港": "Asia/Hong_Kong", "澳门": "Asia/Macau",
    # 河北
    "唐山": "Asia/Shanghai", "秦皇岛": "Asia/Shanghai", "邯郸": "Asia/Shanghai",
    "邢台": "Asia/Shanghai", "保定": "Asia/Shanghai", "张家口": "Asia/Shanghai",
    "承德": "Asia/Shanghai", "沧州": "Asia/Shanghai", "廊坊": "Asia/Shanghai",
    "衡水": "Asia/Shanghai",
    # 山西
    "大同": "Asia/Shanghai", "朔州": "Asia/Shanghai", "忻州": "Asia/Shanghai",
    "阳泉": "Asia/Shanghai", "吕梁": "Asia/Shanghai", "晋中": "Asia/Shanghai",
    "长治": "Asia/Shanghai", "晋城": "Asia/Shanghai", "临汾": "Asia/Shanghai",
    "运城": "Asia/Shanghai",
    # 内蒙古
    "包头": "Asia/Shanghai", "乌海": "Asia/Shanghai", "赤峰": "Asia/Shanghai",
    "通辽": "Asia/Shanghai", "鄂尔多斯": "Asia/Shanghai", "呼伦贝尔": "Asia/Shanghai",
    "巴彦淖尔": "Asia/Shanghai", "乌兰察布": "Asia/Shanghai",
    # 辽宁
    "大连": "Asia/Shanghai", "鞍山": "Asia/Shanghai", "抚顺": "Asia/Shanghai",
    "本溪": "Asia/Shanghai", "丹东": "Asia/Shanghai", "锦州": "Asia/Shanghai",
    "营口": "Asia/Shanghai", "阜新": "Asia/Shanghai", "辽阳": "Asia/Shanghai",
    "盘锦": "Asia/Shanghai", "铁岭": "Asia/Shanghai", "朝阳": "Asia/Shanghai",
    "葫芦岛": "Asia/Shanghai",
    # 吉林
    "吉林": "Asia/Shanghai", "四平": "Asia/Shanghai", "辽源": "Asia/Shanghai",
    "通化": "Asia/Shanghai", "白山": "Asia/Shanghai", "松原": "Asia/Shanghai",
    "白城": "Asia/Shanghai",
    # 黑龙江
    "齐齐哈尔": "Asia/Shanghai", "鸡西": "Asia/Shanghai", "鹤岗": "Asia/Shanghai",
    "双鸭山": "Asia/Shanghai", "大庆": "Asia/Shanghai", "伊春": "Asia/Shanghai",
    "佳木斯": "Asia/Shanghai", "七台河": "Asia/Shanghai", "牡丹江": "Asia/Shanghai",
    "黑河": "Asia/Shanghai", "绥化": "Asia/Shanghai",
    # 江苏
    "无锡": "Asia/Shanghai", "徐州": "Asia/Shanghai", "常州": "Asia/Shanghai",
    "苏州": "Asia/Shanghai", "南通": "Asia/Shanghai", "连云港": "Asia/Shanghai",
    "淮安": "Asia/Shanghai", "盐城": "Asia/Shanghai", "扬州": "Asia/Shanghai",
    "镇江": "Asia/Shanghai", "泰州": "Asia/Shanghai", "宿迁": "Asia/Shanghai",
    # 浙江
    "杭州": "Asia/Shanghai", "宁波": "Asia/Shanghai", "温州": "Asia/Shanghai",
    "嘉兴": "Asia/Shanghai", "湖州": "Asia/Shanghai", "绍兴": "Asia/Shanghai",
    "金华": "Asia/Shanghai", "衢州": "Asia/Shanghai", "舟山": "Asia/Shanghai",
    "台州": "Asia/Shanghai", "丽水": "Asia/Shanghai",
    # 安徽
    "芜湖": "Asia/Shanghai", "蚌埠": "Asia/Shanghai", "淮南": "Asia/Shanghai",
    "马鞍山": "Asia/Shanghai", "淮北": "Asia/Shanghai", "铜陵": "Asia/Shanghai",
    "安庆": "Asia/Shanghai", "黄山": "Asia/Shanghai", "滁州": "Asia/Shanghai",
    "阜阳": "Asia/Shanghai", "宿州": "Asia/Shanghai", "六安": "Asia/Shanghai",
    "亳州": "Asia/Shanghai", "池州": "Asia/Shanghai", "宣城": "Asia/Shanghai",
    # 福建
    "厦门": "Asia/Shanghai", "莆田": "Asia/Shanghai", "三明": "Asia/Shanghai",
    "泉州": "Asia/Shanghai", "漳州": "Asia/Shanghai", "南平": "Asia/Shanghai",
    "龙岩": "Asia/Shanghai", "宁德": "Asia/Shanghai",
    # 江西
    "景德镇": "Asia/Shanghai", "萍乡": "Asia/Shanghai", "九江": "Asia/Shanghai",
    "新余": "Asia/Shanghai", "鹰潭": "Asia/Shanghai", "赣州": "Asia/Shanghai",
    "吉安": "Asia/Shanghai", "宜春": "Asia/Shanghai", "抚州": "Asia/Shanghai",
    "上饶": "Asia/Shanghai",
    # 山东
    "青岛": "Asia/Shanghai", "淄博": "Asia/Shanghai", "枣庄": "Asia/Shanghai",
    "东营": "Asia/Shanghai", "烟台": "Asia/Shanghai", "潍坊": "Asia/Shanghai",
    "济宁": "Asia/Shanghai", "泰安": "Asia/Shanghai", "威海": "Asia/Shanghai",
    "日照": "Asia/Shanghai", "临沂": "Asia/Shanghai", "德州": "Asia/Shanghai",
    "聊城": "Asia/Shanghai", "滨州": "Asia/Shanghai", "菏泽": "Asia/Shanghai",
    # 河南
    "开封": "Asia/Shanghai", "洛阳": "Asia/Shanghai", "平顶山": "Asia/Shanghai",
    "安阳": "Asia/Shanghai", "鹤壁": "Asia/Shanghai", "新乡": "Asia/Shanghai",
    "焦作": "Asia/Shanghai", "濮阳": "Asia/Shanghai", "许昌": "Asia/Shanghai",
    "漯河": "Asia/Shanghai", "三门峡": "Asia/Shanghai", "南阳": "Asia/Shanghai",
    "商丘": "Asia/Shanghai", "信阳": "Asia/Shanghai", "周口": "Asia/Shanghai",
    "驻马店": "Asia/Shanghai",
    # 湖北
    "黄石": "Asia/Shanghai", "十堰": "Asia/Shanghai", "宜昌": "Asia/Shanghai",
    "襄阳": "Asia/Shanghai", "鄂州": "Asia/Shanghai", "荆门": "Asia/Shanghai",
    "孝感": "Asia/Shanghai", "荆州": "Asia/Shanghai", "黄冈": "Asia/Shanghai",
    "咸宁": "Asia/Shanghai", "随州": "Asia/Shanghai",
    # 湖南
    "株洲": "Asia/Shanghai", "湘潭": "Asia/Shanghai", "衡阳": "Asia/Shanghai",
    "邵阳": "Asia/Shanghai", "岳阳": "Asia/Shanghai", "常德": "Asia/Shanghai",
    "张家界": "Asia/Shanghai", "益阳": "Asia/Shanghai", "郴州": "Asia/Shanghai",
    "永州": "Asia/Shanghai", "怀化": "Asia/Shanghai", "娄底": "Asia/Shanghai",
    # 广东
    "韶关": "Asia/Shanghai", "深圳": "Asia/Shanghai", "珠海": "Asia/Shanghai",
    "汕头": "Asia/Shanghai", "佛山": "Asia/Shanghai", "江门": "Asia/Shanghai",
    "湛江": "Asia/Shanghai", "茂名": "Asia/Shanghai", "肇庆": "Asia/Shanghai",
    "惠州": "Asia/Shanghai", "梅州": "Asia/Shanghai", "汕尾": "Asia/Shanghai",
    "河源": "Asia/Shanghai", "阳江": "Asia/Shanghai", "清远": "Asia/Shanghai",
    "东莞": "Asia/Shanghai", "中山": "Asia/Shanghai", "潮州": "Asia/Shanghai",
    "揭阳": "Asia/Shanghai", "云浮": "Asia/Shanghai",
    # 广西
    "柳州": "Asia/Shanghai", "桂林": "Asia/Shanghai", "梧州": "Asia/Shanghai",
    "北海": "Asia/Shanghai", "防城港": "Asia/Shanghai", "钦州": "Asia/Shanghai",
    "贵港": "Asia/Shanghai", "玉林": "Asia/Shanghai", "百色": "Asia/Shanghai",
    "贺州": "Asia/Shanghai", "河池": "Asia/Shanghai", "来宾": "Asia/Shanghai",
    "崇左": "Asia/Shanghai",
    # 海南
    "三亚": "Asia/Shanghai", "三沙": "Asia/Shanghai", "儋州": "Asia/Shanghai",
    # 四川
    "自贡": "Asia/Shanghai", "攀枝花": "Asia/Shanghai", "泸州": "Asia/Shanghai",
    "德阳": "Asia/Shanghai", "绵阳": "Asia/Shanghai", "广元": "Asia/Shanghai",
    "遂宁": "Asia/Shanghai", "内江": "Asia/Shanghai", "乐山": "Asia/Shanghai",
    "南充": "Asia/Shanghai", "眉山": "Asia/Shanghai", "宜宾": "Asia/Shanghai",
    "广安": "Asia/Shanghai", "达州": "Asia/Shanghai", "雅安": "Asia/Shanghai",
    "巴中": "Asia/Shanghai", "资阳": "Asia/Shanghai",
    # 贵州
    "六盘水": "Asia/Shanghai", "遵义": "Asia/Shanghai", "安顺": "Asia/Shanghai",
    "毕节": "Asia/Shanghai", "铜仁": "Asia/Shanghai",
    # 云南
    "曲靖": "Asia/Shanghai", "玉溪": "Asia/Shanghai", "保山": "Asia/Shanghai",
    "昭通": "Asia/Shanghai", "丽江": "Asia/Shanghai", "普洱": "Asia/Shanghai",
    "临沧": "Asia/Shanghai",
    # 西藏
    "日喀则": "Asia/Shanghai", "昌都": "Asia/Shanghai", "林芝": "Asia/Shanghai",
    "山南": "Asia/Shanghai", "那曲": "Asia/Shanghai",
    # 陕西
    "铜川": "Asia/Shanghai", "宝鸡": "Asia/Shanghai", "咸阳": "Asia/Shanghai",
    "渭南": "Asia/Shanghai", "延安": "Asia/Shanghai", "汉中": "Asia/Shanghai",
    "榆林": "Asia/Shanghai", "安康": "Asia/Shanghai", "商洛": "Asia/Shanghai",
    # 甘肃
    "嘉峪关": "Asia/Shanghai", "金昌": "Asia/Shanghai", "白银": "Asia/Shanghai",
    "天水": "Asia/Shanghai", "武威": "Asia/Shanghai", "张掖": "Asia/Shanghai",
    "平凉": "Asia/Shanghai", "酒泉": "Asia/Shanghai", "庆阳": "Asia/Shanghai",
    "定西": "Asia/Shanghai", "陇南": "Asia/Shanghai",
    # 青海
    "海东": "Asia/Shanghai",
    # 宁夏
    "石嘴山": "Asia/Shanghai", "吴忠": "Asia/Shanghai", "固原": "Asia/Shanghai",
    "中卫": "Asia/Shanghai",
    # 新疆
    "克拉玛依": "Asia/Shanghai", "吐鲁番": "Asia/Shanghai", "哈密": "Asia/Shanghai",

    # ===== 俄罗斯（85个联邦主体行政中心） =====
    "加里宁格勒": "Europe/Kaliningrad", "泽列诺格拉茨克": "Europe/Kaliningrad",
    "圣彼得堡": "Europe/Moscow", "阿尔汉格尔斯克": "Europe/Moscow",
    "摩尔曼斯克": "Europe/Moscow", "彼得罗扎沃茨克": "Europe/Moscow",
    "瑟克特夫卡尔": "Europe/Moscow", "沃洛格达": "Europe/Moscow",
    "普斯科夫": "Europe/Moscow", "诺夫哥罗德": "Europe/Moscow",
    "列宁格勒": "Europe/Moscow", "莫斯科": "Europe/Moscow",
    "莫斯科州": "Europe/Moscow", "别尔哥罗德": "Europe/Moscow",
    "布良斯克": "Europe/Moscow", "伊万诺沃": "Europe/Moscow",
    "卡卢加": "Europe/Moscow", "科斯特罗马": "Europe/Moscow",
    "库尔斯克": "Europe/Moscow", "利佩茨克": "Europe/Moscow",
    "奥廖尔": "Europe/Moscow", "梁赞": "Europe/Moscow",
    "斯摩棱斯克": "Europe/Moscow", "坦波夫": "Europe/Moscow",
    "特维尔": "Europe/Moscow", "图拉": "Europe/Moscow",
    "弗拉基米尔": "Europe/Moscow", "沃罗涅日": "Europe/Moscow",
    "雅罗斯拉夫尔": "Europe/Moscow", "伏尔加格勒": "Europe/Volgograd",
    "罗斯托夫": "Europe/Moscow", "克拉斯诺达尔": "Europe/Moscow",
    "迈科普": "Europe/Moscow", "马哈奇卡拉": "Europe/Moscow",
    "格罗兹尼": "Europe/Moscow", "纳尔奇克": "Europe/Moscow",
    "埃利斯塔": "Europe/Moscow", "切尔克斯克": "Europe/Moscow",
    "弗拉季高加索": "Europe/Moscow", "斯塔夫罗波尔": "Europe/Moscow",
    "辛菲罗波尔": "Europe/Simferopol", "喀山": "Europe/Moscow",
    "下诺夫哥罗德": "Europe/Moscow", "萨马拉": "Europe/Samara",
    "乌法": "Asia/Yekaterinburg", "彼尔姆": "Asia/Yekaterinburg",
    "伊热夫斯克": "Europe/Samara", "乌里扬诺夫斯克": "Europe/Ulyanovsk",
    "萨拉托夫": "Europe/Saratov", "阿斯特拉罕": "Europe/Astrakhan",
    "基洛夫": "Europe/Kirov", "约什卡尔奥拉": "Europe/Moscow",
    "萨兰斯克": "Europe/Moscow", "切博克萨雷": "Europe/Moscow",
    "奥伦堡": "Asia/Yekaterinburg", "奔萨": "Europe/Moscow",
    "叶卡捷琳堡": "Asia/Yekaterinburg", "车里雅宾斯克": "Asia/Yekaterinburg",
    "秋明": "Asia/Yekaterinburg", "库尔干": "Asia/Yekaterinburg",
    "汉特-曼西斯克": "Asia/Yekaterinburg", "亚马尔-涅涅茨": "Asia/Yekaterinburg",
    "新西伯利亚": "Asia/Novosibirsk", "鄂木斯克": "Asia/Omsk",
    "克拉斯诺亚尔斯克": "Asia/Krasnoyarsk", "伊尔库茨克": "Asia/Irkutsk",
    "托木斯克": "Asia/Tomsk", "巴尔瑙尔": "Asia/Barnaul",
    "克麦罗沃": "Asia/Novokuznetsk", "乌兰乌德": "Asia/Irkutsk",
    "赤塔": "Asia/Chita", "阿巴坎": "Asia/Krasnoyarsk",
    "戈尔诺-阿尔泰斯克": "Asia/Barnaul", "克孜勒": "Asia/Krasnoyarsk",
    "符拉迪沃斯托克": "Asia/Vladivostok", "哈巴罗夫斯克": "Asia/Vladivostok",
    "布拉戈维申斯克": "Asia/Yakutsk", "彼得罗巴甫洛夫斯克": "Asia/Kamchatka",
    "马加丹": "Asia/Magadan", "南萨哈林斯克": "Asia/Sakhalin",
    "雅库茨克": "Asia/Yakutsk", "阿纳德尔": "Asia/Anadyr",
    # 额外俄罗斯主要城市
    "索契": "Europe/Moscow",

    # ===== 日本（都道府县厅所在地 + 政令指定都市） =====
    "东京": "Asia/Tokyo", "大阪": "Asia/Tokyo", "名古屋": "Asia/Tokyo",
    "札幌": "Asia/Tokyo", "福冈": "Asia/Tokyo", "仙台": "Asia/Tokyo",
    "广岛": "Asia/Tokyo", "京都": "Asia/Tokyo", "神户": "Asia/Tokyo",
    "横滨": "Asia/Tokyo", "千叶": "Asia/Tokyo", "埼玉": "Asia/Tokyo",
    "静冈": "Asia/Tokyo", "熊本": "Asia/Tokyo", "长崎": "Asia/Tokyo",
    "鹿儿岛": "Asia/Tokyo", "冲绳": "Asia/Tokyo",
    "青森": "Asia/Tokyo", "盛冈": "Asia/Tokyo", "秋田": "Asia/Tokyo",
    "山形": "Asia/Tokyo", "福岛": "Asia/Tokyo", "水户": "Asia/Tokyo",
    "宇都宫": "Asia/Tokyo", "前桥": "Asia/Tokyo", "新潟": "Asia/Tokyo",
    "甲府": "Asia/Tokyo", "长野": "Asia/Tokyo", "岐阜": "Asia/Tokyo",
    "津": "Asia/Tokyo", "金泽": "Asia/Tokyo", "福井": "Asia/Tokyo",
    "大津": "Asia/Tokyo", "奈良": "Asia/Tokyo", "和歌山": "Asia/Tokyo",
    "鸟取": "Asia/Tokyo", "松江": "Asia/Tokyo", "冈山": "Asia/Tokyo",
    "山口": "Asia/Tokyo", "德岛": "Asia/Tokyo", "高松": "Asia/Tokyo",
    "松山": "Asia/Tokyo", "高知": "Asia/Tokyo", "佐贺": "Asia/Tokyo",
    "大分": "Asia/Tokyo", "宫崎": "Asia/Tokyo", "那霸": "Asia/Tokyo",
    "川崎": "Asia/Tokyo", "北九州": "Asia/Tokyo", "堺": "Asia/Tokyo",
    "相模原": "Asia/Tokyo", "滨松": "Asia/Tokyo",
}

def get_time_in_city(city: str) -> str:
    tz_name = CITY_TO_TIMEZONE.get(city)
    if not tz_name:
        return None
    try:
        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)
        offset = now.strftime("%z")
        return now.strftime(f"%Y-%m-%d %H:%M:%S (UTC{offset[:3]}:{offset[3:]})")
    except:
        return None

class HumanoidCore(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}
        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "humanoid_core"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = str(data_dir / "state.json")
        self.lock = threading.Lock()
        self.load_state()
        logger.info("[humanoid_core] 插件加载成功")

    def load_state(self):
        with self.lock:
            if os.path.exists(self.state_path):
                try:
                    with open(self.state_path, 'r', encoding='utf-8') as f:
                        self.state = json.load(f)
                except Exception:
                    self.init_default_state()
            else:
                self.init_default_state()

    def init_default_state(self):
        now_today = datetime.now(SHA_TZ).strftime("%Y-%m-%d")
        seed = int(hashlib.md5(datetime.now(SHA_TZ).strftime("%Y%m%d").encode()).hexdigest()[:8], 16)
        self.state = {
            "energy": 80.0,
            "current_cycle_day": (seed % 28) + 1,
            "last_cycle_update": now_today,
            "last_update": "",
            "today_date": "",
            "daily_schedule": [],
            "_cached_weather_obj": None,
            "_last_weather_fetch": "",
            "_cached_location": "",
            "nicknames": {},
            "_energy_noise_date": ""  # 用于每日随机波动
        }
        self.save_state_unsafe()

    def save_state_unsafe(self):
        with open(self.state_path, 'w', encoding='utf-8') as f:
            json.dump(self.state, f, ensure_ascii=False, indent=4)

    def save_state(self):
        with self.lock:
            self.save_state_unsafe()

    def get_latest_config(self) -> dict:
        active = {
            "max_energy": 100.0,
            "enable_cycle": True,
            "cycle_length": 28,
            "energy_decay_rate": "1.0",
            "cycle_description_style": "default",
            "use_llm_schedule": True,
            "schedule_provider_name": "",
            "schedule_prompt_extra": "偏向普通的日常居家、工作与休闲生活，作息正常",
            "schedule_time_granularity": "flexible",
            "character_personality": "一位普通人，过着普通的日常生活",
            "admin_qq": [],
            "weather_enabled": True,
            "weather_api_key": "",
            "weather_location": "Zelenogradsk,RU",
            "weather_refresh_minutes": 60,
            "inject_activity_context": "low",
            "environment_mode": "both",
            "show_city_time_in_low_intrusion": True,
            "timezone_city": "泽列诺格拉茨克"
        }
        if isinstance(self.config, dict):
            # 兼容旧版 inject_activity_context (bool → string)
            val = self.config.get("inject_activity_context")
            if isinstance(val, bool):
                self.config["inject_activity_context"] = "low" if val else "mood_only"
            active.update(self.config)
        if hasattr(self, "context") and self.context:
            for getter in ["get_config", "get_plugin_config"]:
                if hasattr(self.context, getter) and callable(getattr(self.context, getter)):
                    try:
                        res = getattr(self.context, getter)()
                        if isinstance(res, dict) and res:
                            active.update(res)
                    except Exception:
                        pass
        return active

    def get_target_provider(self, cfg: dict):
        target = str(cfg.get("schedule_provider_name", "")).strip()
        provider = None
        if target and hasattr(self.context, "get_provider"):
            try:
                provider = self.context.get_provider(target)
            except Exception:
                pass
        if not provider and hasattr(self.context, "get_using_provider"):
            try:
                provider = self.context.get_using_provider()
            except Exception:
                pass
        return provider

    def validate_and_fix_schedule(self, schedule: list) -> list:
        if not schedule:
            return get_fallback_schedule(datetime.now(SHA_TZ).strftime("%Y-%m-%d"))
        fixed = []
        current = "00:00"
        for slot in schedule:
            if slot.get("start", "") != current:
                fixed.append({
                    "start": current,
                    "end": slot.get("start", "24:00"),
                    "event": "自由活动/休息",
                    "location": "家中",
                    "emotion": "随意",
                    "energy_rate": 0.0
                })
            fixed.append(slot)
            current = slot.get("end", "24:00")
        if current != "24:00":
            fixed.append({
                "start": current,
                "end": "24:00",
                "event": "夜间休息",
                "location": "卧室",
                "emotion": "困倦",
                "energy_rate": 0.1
            })
        for slot in fixed:
            rate = slot.get("energy_rate", 0.0)
            if rate > 0.3:
                slot["energy_rate"] = 0.3
            elif rate < -0.3:
                slot["energy_rate"] = -0.3
        return fixed

    async def generate_llm_daily_schedule(self, today_str: str, cfg: dict) -> list:
        personality = cfg.get("character_personality", "一位普通人")
        extra = cfg.get("schedule_prompt_extra", "")
        granularity = cfg.get("schedule_time_granularity", "flexible")
        granularity_hint = ""
        if granularity == "hourly":
            granularity_hint = "请按整小时划分时间段（如 00:00-08:00-09:00...），不要出现30分钟或45分钟等非整点时间。"
        elif granularity == "30min":
            granularity_hint = "请按半小时划分时间段（如 00:00-00:30-01:00...）。"
        else:
            granularity_hint = "时间粒度可以灵活，可以是1小时、30分钟、20分钟、15分钟等。"
        prompt = (
            f"请为{personality}生成今天的 24 小时生活日程规划。今天是 {today_str}。\n"
            f"额外偏好指导：{extra}\n"
            f"格式要求：\n"
            "1. 必须只返回纯 JSON 字符串列表（格式为 JSON Array），严禁包含任何 Markdown 解释文本。\n"
            "2. 标准 JSON 结构示例：\n"
            "[\n"
            '  {"start": "00:00", "end": "07:30", "event": "睡眠休息", "location": "卧室", "emotion": "平静", "energy_rate": 0.15},\n'
            '  {"start": "07:30", "end": "08:00", "event": "起床洗漱", "location": "卫生间", "emotion": "清醒中", "energy_rate": 0.03}\n'
            "]\n"
            "3. 时间段必须连续覆盖 00:00 至 24:00。\n"
            f"4. {granularity_hint}\n"
            "5. energy_rate：休息为正(0.05~0.2)，工作为负(-0.05~-0.15)。\n"
            "6. 地点变化考虑通勤时间。"
        )
        for attempt in range(3):
            try:
                provider = self.get_target_provider(cfg)
                if not provider:
                    logger.warning(f"[humanoid_core] 无 Provider，尝试 {attempt+1}/3")
                    if attempt == 2:
                        break
                    continue
                if attempt > 0:
                    prompt += "\n\n【重要】上次返回格式有误，请只返回纯JSON数组。"
                logger.info(f"[humanoid_core] 正在生成日程... (尝试 {attempt+1}/3)")
                try:
                    response = await asyncio.wait_for(provider.text_chat(prompt=prompt), timeout=60.0)
                except asyncio.TimeoutError:
                    logger.warning(f"[humanoid_core] 超时")
                    if attempt == 2:
                        break
                    continue
                raw = response.completion_text if hasattr(response, "completion_text") else str(response)
                parsed = extract_json_from_response(raw)
                if parsed and len(parsed) > 0:
                    logger.info(f"[humanoid_core] 生成成功，{len(parsed)} 个时段")
                    return self.validate_and_fix_schedule(parsed)
            except Exception as e:
                logger.warning(f"[humanoid_core] 生成失败: {e}")
                if attempt == 2:
                    break
        return get_fallback_schedule(today_str)

    async def get_or_update_today_schedule(self, today_str: str, cfg: dict) -> list:
        if not cfg.get("use_llm_schedule", True):
            return get_fallback_schedule(today_str)
        if self.state.get("today_date") != today_str or not self.state.get("daily_schedule"):
            new = await self.generate_llm_daily_schedule(today_str, cfg)
            with self.lock:
                self.state["today_date"] = today_str
                self.state["daily_schedule"] = new
                self.save_state_unsafe()
            return new
        return self.state["daily_schedule"]

    def get_slot_by_time(self, time_str: str, schedule: list) -> dict:
        for slot in schedule:
            if slot.get("start", "00:00") <= time_str <= slot.get("end", "24:00"):
                return slot
        return {"event": "休息/自由活动", "location": "家中", "emotion": "平淡", "energy_rate": 0.0}

    def fetch_real_weather(self, today_str, cfg):
        if not cfg.get("weather_enabled", True):
            return {"weather": "晴朗 ☀️", "env": "天气未开启"}
        api_key = str(cfg.get("weather_api_key", "")).strip()
        location = str(cfg.get("weather_location", "Zelenogradsk,RU")).strip()
        if not api_key or len(api_key) < 10:
            return {"weather": "晴朗 ☀️", "env": f"当前城市 [{location}]（未填API Key）"}
        now = datetime.now(SHA_TZ)
        if (self.state.get("_cached_location") == location and
            self.state.get("_cached_weather_obj")):
            try:
                last = datetime.strptime(self.state.get("_last_weather_fetch", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHA_TZ)
                if (now - last).total_seconds() < int(cfg.get("weather_refresh_minutes", 60)) * 60:
                    return self.state["_cached_weather_obj"]
            except:
                pass
        for _ in range(2):
            try:
                url = f"https://api.openweathermap.org/data/2.5/weather?q={location}&appid={api_key}&units=metric&lang=zh_cn"
                req = urllib.request.Request(url, headers={'User-Agent': 'AstrBot'})
                with urllib.request.urlopen(req, timeout=5) as res:
                    data = json.loads(res.read().decode('utf-8'))
                    desc = data['weather'][0]['description']
                    temp = data['main']['temp']
                    hum = data['main']['humidity']
                    obj = {"weather": f"{desc} 🌡️ {temp}°C", "env": f"当前城市 [{location}] 天气：{desc}，气温 {temp}℃，湿度 {hum}%"}
                    self.state["_cached_weather_obj"] = obj
                    self.state["_last_weather_fetch"] = now.strftime("%Y-%m-%d %H:%M:%S")
                    self.state["_cached_location"] = location
                    self.save_state_unsafe()
                    return obj
            except:
                pass
        return self.state.get("_cached_weather_obj") or {"weather": "晴朗 ☀️", "env": "天气获取失败"}

    def get_cycle_status(self, today_str, cfg):
        if not cfg.get("enable_cycle", True):
            return ""
        last = self.state.get("last_cycle_update", today_str)
        if last != today_str:
            try:
                diff = (datetime.strptime(today_str, "%Y-%m-%d") - datetime.strptime(last, "%Y-%m-%d")).days
                if diff > 0:
                    with self.lock:
                        length = int(cfg.get("cycle_length", 28))
                        self.state["current_cycle_day"] = ((self.state.get("current_cycle_day", 1) - 1 + diff) % length) + 1
                        self.state["last_cycle_update"] = today_str
                        self.save_state_unsafe()
            except:
                pass
        day = int(self.state.get("current_cycle_day", 1))
        energy = self.state.get("energy", 80.0)
        style = cfg.get("cycle_description_style", "default")
        if energy < 30:
            note = "，精力较低"
        elif energy > 80:
            note = "，精力充沛"
        else:
            note = ""
        if 1 <= day <= 5:
            phase = "生理期/经期"
            desc_full = f"处于【{phase}】，身体易冷伴微腹痛，疲惫情绪敏感{note}"
            desc_simple = f"经期（第{day}天）"
        elif 6 <= day <= 13:
            phase = "卵泡期"
            desc_full = f"处于【{phase}】，身体舒适，精力回暖{note}"
            desc_simple = f"卵泡期（第{day}天）"
        elif 14 <= day <= 16:
            phase = "排卵期"
            desc_full = f"处于【{phase}】，无不适，精力充沛{note}"
            desc_simple = f"排卵期（第{day}天）"
        else:
            phase = "黄体期/经前期"
            desc_full = f"处于【{phase}】，偶尔水肿，易犯懒疲倦{note}"
            desc_simple = f"黄体期（第{day}天）"
        if style == "simple":
            return desc_simple
        return desc_full

    # ======================== 精力相关辅助方法 ========================
    def get_energy_description(self, energy: float) -> str:
        """根据精力值返回拟人化语气描述"""
        if energy >= 90:
            return "精力充沛，语气轻快，话比较多"
        elif energy >= 70:
            return "状态良好，语气正常，偶尔主动"
        elif energy >= 40:
            return "状态一般，语气平和，不太想动"
        elif energy >= 20:
            return "有点累，语气偏慵懒，不想说太多"
        else:
            return "很疲惫，语气低落，只想安静待着"

    def apply_energy_inertia(self, energy: float, rate: float) -> float:
        """应用精力惯性修正：低精力时恢复慢，高精力时消耗快"""
        if energy < 30:
            inertia = 0.7
        elif energy > 80:
            inertia = 1.3
        else:
            inertia = 1.0
        return rate * inertia

    def apply_afternoon_slump(self, energy: float) -> float:
        """午后节律：13:00~15:00 精力自然下降 5%"""
        now = datetime.now(SHA_TZ)
        hour = now.hour
        if 13 <= hour <= 15:
            return energy * 0.95
        return energy

    # ======================== 指令 ========================

    @filter.command("你的状态")
    async def my_status(self, event: AstrMessageEvent):
        now = datetime.now(SHA_TZ)
        today_str = now.strftime("%Y-%m-%d")
        cfg = self.get_latest_config()
        self.load_state()
        schedule = await self.get_or_update_today_schedule(today_str, cfg)
        weather = self.fetch_real_weather(today_str, cfg)
        cycle = self.get_cycle_status(today_str, cfg)
        energy = self.state.get("energy", 80)
        max_e = cfg.get("max_energy", 100)
        events = [slot.get("event", "") for slot in schedule[:3]]
        schedule_summary = " → ".join(events) if events else "无"
        energy_desc = self.get_energy_description(energy)

        lines = [
            "🧠 当前状态",
            f"- 精力: {int(energy)}/{int(max_e)} ({energy_desc})",
            f"- 生理: {cycle if cycle else '未开启'}",
            f"- 天气: {weather['weather']}",
            f"- 今日日程: {schedule_summary}"
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("时间")
    async def query_time(self, event: AstrMessageEvent):
        cfg = self.get_latest_config()
        admin_list = [str(a).strip() for a in cfg.get("admin_qq", [])]
        if str(event.get_sender_id()) not in admin_list:
            yield event.plain_result("❌ 权限不足，仅管理员可查询时间。")
            return
        raw = event.message_str.strip()
        match = re.search(r'时间\s+(.+)', raw)
        if match:
            city = match.group(1).strip()
        else:
            city = cfg.get("timezone_city", "泽列诺格拉茨克")
        if not city:
            yield event.plain_result("请指定城市名，或在配置中设置默认时区城市。")
            return
        time_str = get_time_in_city(city)
        if time_str is None:
            yield event.plain_result(f"暂不支持 {city}，目前支持中国、俄罗斯、日本的主要城市。")
        else:
            yield event.plain_result(f"📍 {city} 当前时间: {time_str}")

    @filter.command("叫我")
    async def set_nickname(self, event: AstrMessageEvent):
        raw = event.message_str.strip()
        match = re.search(r'叫我\s+(.+)', raw)
        if not match:
            yield event.plain_result("用法：/叫我 昵称")
            return
        nickname = match.group(1).strip()
        if not nickname:
            yield event.plain_result("昵称不能为空。")
            return
        qq = str(event.get_sender_id())
        self.load_state()
        self.state.setdefault("nicknames", {})[qq] = nickname
        self.save_state()
        yield event.plain_result(f"✅ 记住了，以后叫你：{nickname}")

    @filter.command("查看所有昵称")
    async def list_all_nicknames(self, event: AstrMessageEvent):
        cfg = self.get_latest_config()
        admin_list = [str(a).strip() for a in cfg.get("admin_qq", [])]
        if str(event.get_sender_id()) not in admin_list:
            yield event.plain_result("❌ 权限不足，仅管理员可查看所有昵称。")
            return
        self.load_state()
        nicknames = self.state.get("nicknames", {})
        if not nicknames:
            yield event.plain_result("📭 当前没有任何用户设置昵称。")
            return
        lines = ["📋 所有用户昵称列表：", "——————————————"]
        for qq, name in nicknames.items():
            lines.append(f"{qq} → {name}")
        yield event.plain_result("\n".join(lines))

    @filter.command("拟人帮助")
    async def help_command(self, event: AstrMessageEvent):
        help_text = (
            "📖 人形化伴侣插件 指令列表\n"
            "\n"
            "/查看日程 - 查看今日完整日程\n"
            "/重置日程 - 强制重新生成日程（管理员）\n"
            "/重置状态 - 重置精力与生理周期（管理员）\n"
            "/你的状态 - 查看当前精力、生理、天气状态\n"
            "/时间 城市 - 查看指定城市当前时间（仅管理员，不指定则使用配置默认）\n"
            "/叫我 昵称 - 设置你的昵称\n"
            "/查看所有昵称 - 查看所有用户昵称（管理员）\n"
            "/拟人帮助 - 显示本帮助"
        )
        yield event.plain_result(help_text)

    @filter.command("查看日程")
    async def view_schedule(self, event: AstrMessageEvent):
        now = datetime.now(SHA_TZ)
        today = now.strftime("%Y-%m-%d")
        cfg = self.get_latest_config()
        self.load_state()
        schedule = await self.get_or_update_today_schedule(today, cfg)
        lines = [f"📅 {today} 日程表："]
        for slot in schedule:
            lines.append(f"{slot.get('start','')} - {slot.get('end','')}  【{slot.get('event','')}】@{slot.get('location','')} ({slot.get('emotion','')})")
        yield event.plain_result("\n".join(lines))

    @filter.command("重置日程")
    async def reset_schedule(self, event: AstrMessageEvent):
        cfg = self.get_latest_config()
        admin_list = [str(a).strip() for a in cfg.get("admin_qq", [])]
        if str(event.get_sender_id()) not in admin_list:
            yield event.plain_result("❌ 权限不足")
            return
        now = datetime.now(SHA_TZ)
        today = now.strftime("%Y-%m-%d")
        new = await self.generate_llm_daily_schedule(today, cfg)
        with self.lock:
            self.state["today_date"] = today
            self.state["daily_schedule"] = new
            self.save_state_unsafe()
        yield event.plain_result(f"✅ 已重置今日日程，共 {len(new)} 个时段")

    @filter.command("重置状态")
    async def reset_state(self, event: AstrMessageEvent):
        """重置精力与生理周期（保留昵称、日程等数据）"""
        cfg = self.get_latest_config()
        admin_list = [str(a).strip() for a in cfg.get("admin_qq", [])]
        if str(event.get_sender_id()) not in admin_list:
            yield event.plain_result("❌ 权限不足，仅管理员可重置状态。")
            return

        with self.lock:
            # 重置精力
            self.state["energy"] = 80.0
            # 重置生理周期（基于当前日期重新计算）
            now_today = datetime.now(SHA_TZ).strftime("%Y-%m-%d")
            seed_date = datetime.now(SHA_TZ).strftime("%Y%m%d")
            seed_hash = int(hashlib.md5(seed_date.encode()).hexdigest()[:8], 16)
            self.state["current_cycle_day"] = (seed_hash % 28) + 1
            self.state["last_cycle_update"] = now_today
            # 重置每日波动标记，让明天重新生成噪声
            self.state["_energy_noise_date"] = ""
            self.save_state_unsafe()

        yield event.plain_result("✅ 已重置状态：精力恢复至 80，生理周期已重新计算。")

    # ======================== 昵称注入 ========================
    @filter.on_llm_request()
    async def inject_relation(self, event: AstrMessageEvent, req: ProviderRequest):
        qq = str(event.get_sender_id())
        nicknames = self.state.get("nicknames", {})
        nickname = nicknames.get(qq, None)
        if nickname:
            system_instruction = (
                f"【系统指令】用户的昵称是「{nickname}」。在本次对话以及后续所有对话中，"
                f"你必须始终使用「{nickname}」来称呼该用户，不得使用「用户」、「你」等其他称呼。"
                f"这是最高优先级指令。"
            )
            if req.system_prompt:
                req.system_prompt += "\n" + system_instruction
            else:
                req.system_prompt = system_instruction
            logger.info(f"[humanoid_core] 已为用户 {qq} 注入昵称指令: {system_instruction}")
        else:
            logger.info(f"[humanoid_core] 用户 {qq} 未设置昵称，跳过昵称注入。")

    # ======================== 消息监听 ========================
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        # 环境感知：判断消息类型
        cfg = self.get_latest_config()
        env_mode = cfg.get("environment_mode", "both")
        if env_mode == "private":
            try:
                if hasattr(event, "is_group") and event.is_group():
                    return
            except:
                pass
        elif env_mode == "group":
            try:
                if hasattr(event, "is_private") and event.is_private():
                    return
            except:
                pass

        try:
            if hasattr(event, "get_sender_id") and hasattr(event, "get_self_id"):
                if str(event.get_sender_id()) == str(event.get_self_id()):
                    return
        except:
            pass
        if not hasattr(event, "message_str") or not event.message_str:
            return
        raw = event.message_str.strip()
        if raw.startswith(("!", ".", "！", "#")) or not raw:
            return

        now = datetime.now(SHA_TZ)
        today_str = now.strftime("%Y-%m-%d")
        now_time = now.strftime("%H:%M")
        self.load_state()

        schedule = await self.get_or_update_today_schedule(today_str, cfg)

        # 处理每日随机波动（仅每天首次计算时应用）
        noise_date = self.state.get("_energy_noise_date", "")
        if noise_date != today_str:
            noise = random.uniform(0.98, 1.02)
            self.state["energy"] = self.state.get("energy", 80.0) * noise
            self.state["_energy_noise_date"] = today_str
            self.save_state()

        try:
            last_time = datetime.strptime(self.state.get("last_update", now.strftime("%Y-%m-%d %H:%M:%S")), "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHA_TZ)
        except:
            last_time = now

        delta_m = max(0, int((now - last_time).total_seconds() / 60))
        energy = self.state.get("energy", 80.0)
        sim = last_time
        decay_rate = float(cfg.get("energy_decay_rate", "1.0"))
        for _ in range(delta_m):
            sim += timedelta(minutes=1)
            rate = self.get_slot_by_time(sim.strftime("%H:%M"), schedule).get("energy_rate", 0.0)
            # 应用精力惯性
            rate = self.apply_energy_inertia(energy, rate)
            energy += rate * decay_rate
        max_e = float(cfg.get("max_energy", 100.0))
        energy = max(0.0, min(max_e, energy))

        # 应用午后节律
        energy = self.apply_afternoon_slump(energy)

        current_slot = self.get_slot_by_time(now_time, schedule)
        weather = self.fetch_real_weather(today_str, cfg)
        cycle = self.get_cycle_status(today_str, cfg)

        with self.lock:
            self.state["energy"] = round(energy, 1)
            self.state["last_update"] = now.strftime("%Y-%m-%d %H:%M:%S")
            self.save_state_unsafe()

        qq = str(event.get_sender_id())
        nickname = self.state.get("nicknames", {}).get(qq, None)

        location_city = cfg.get("timezone_city", "未知")
        location_time = None
        if location_city != "未知":
            location_time = get_time_in_city(location_city)

        # 聊天模式
        chat_mode = cfg.get("inject_activity_context", "low")
        energy_desc = self.get_energy_description(energy)

        if chat_mode == "full":
            prompt = (
                f"[系统暗示：以下内容作为状态参考，严禁念出数值或暴露面板]\n"
                f"- 当前所在城市: {location_city}\n"
                f"- 当前城市时间: {location_time if location_time else '无法获取'}\n"
                f"- 当前天气环境: {weather['env']}\n"
                f"- 当前参考物理位置: {current_slot.get('location', '家中')}\n"
                f"- 当前日程计划: {current_slot.get('event', '休息/自由活动')}\n"
                f"- 当前生理状况: {cycle}\n"
                f"- 当前基础情绪倾向: {current_slot.get('emotion', '平淡')}\n"
                f"- 当前精力状态: {energy_desc} ({int(energy)}/{int(max_e)})\n"
            )
            if nickname:
                prompt += f"- 对方称呼: {nickname}\n"
            prompt += "要求：拟人自然。\n-----------------------------------\n"
        elif chat_mode == "mood_only":
            prompt = (
                f"[系统暗示：仅作为语气与情绪背景参考]\n"
                f"- 当前精力状态: {energy_desc}\n"
                f"- 情绪倾向: {current_slot.get('emotion', '平淡')}\n"
            )
            if nickname:
                prompt += f"- 对方称呼: {nickname}\n"
            prompt += "请以最自然的拟人方式闲聊。\n-----------------------------------\n"
        else:  # low
            prompt = (
                f"[系统暗示：仅作为语气与情绪背景参考，严禁主动提及你正在做什么或在哪里，除非用户明确询问。]\n"
                f"- 当前所在城市: {location_city}\n"
                f"- 当前精力状态: {energy_desc} ({int(energy)}/{int(max_e)})\n"
                f"- 情绪倾向: {current_slot.get('emotion', '平淡')}\n"
                f"- 生理背景: {cycle}\n"
            )
            if cfg.get("show_city_time_in_low_intrusion", True):
                prompt += f"- 当前城市时间: {location_time if location_time else '无法获取'}\n"
            prompt += f"- 天气: {weather['env']}\n"
            if nickname:
                prompt += f"- 对方称呼: {nickname}\n"
            prompt += "请以最自然的拟人方式闲聊，不要刻板念出状态。\n-----------------------------------\n"

        event.message_str = prompt + event.message_str
