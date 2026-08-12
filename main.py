import os
import json
import asyncio
import random
import re
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

from astrbot.api.star import Context, Star
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.provider.entities import ProviderRequest

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

# ======================== 工具函数 ========================
def get_fallback_schedule(today_str: str) -> list:
    seed = int(hashlib.md5(today_str.encode()).hexdigest()[:8], 16)
    return FALLBACK_TEMPLATES[seed % len(FALLBACK_TEMPLATES)]

def extract_json_from_response(raw_res: str) -> list:
    code_block = re.search(r'```json\s*([\s\S]*?)\s*```', raw_res, re.DOTALL)
    if code_block:
        try:
            parsed = json.loads(code_block.group(1))
            if isinstance(parsed, list):
                return parsed
        except:
            pass
    try:
        parsed = json.loads(raw_res)
        if isinstance(parsed, list):
            return parsed
    except:
        pass
    start = raw_res.find('[')
    if start == -1:
        return []
    depth = 0
    end = -1
    for i, ch in enumerate(raw_res[start:], start):
        if ch == '[':
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0:
                end = i
                break
    if end != -1:
        try:
            parsed = json.loads(raw_res[start:end+1])
            if isinstance(parsed, list):
                return parsed
        except:
            pass
    return []

# ======================== 城市 ⇄ 时区映射表（完整版，覆盖中日俄主要城市） ========================
CITY_TO_TIMEZONE = {
    # ---- 中国（直辖市 + 所有省会/首府 + 主要地级市） ----
    "北京": "Asia/Shanghai", "上海": "Asia/Shanghai", "天津": "Asia/Shanghai", "重庆": "Asia/Shanghai",
    "石家庄": "Asia/Shanghai", "唐山": "Asia/Shanghai", "秦皇岛": "Asia/Shanghai", "邯郸": "Asia/Shanghai",
    "邢台": "Asia/Shanghai", "保定": "Asia/Shanghai", "张家口": "Asia/Shanghai", "承德": "Asia/Shanghai",
    "沧州": "Asia/Shanghai", "廊坊": "Asia/Shanghai", "衡水": "Asia/Shanghai",
    "太原": "Asia/Shanghai", "大同": "Asia/Shanghai", "朔州": "Asia/Shanghai", "忻州": "Asia/Shanghai",
    "阳泉": "Asia/Shanghai", "吕梁": "Asia/Shanghai", "晋中": "Asia/Shanghai", "长治": "Asia/Shanghai",
    "晋城": "Asia/Shanghai", "临汾": "Asia/Shanghai", "运城": "Asia/Shanghai",
    "呼和浩特": "Asia/Shanghai", "包头": "Asia/Shanghai", "乌海": "Asia/Shanghai", "赤峰": "Asia/Shanghai",
    "通辽": "Asia/Shanghai", "鄂尔多斯": "Asia/Shanghai", "呼伦贝尔": "Asia/Shanghai", "巴彦淖尔": "Asia/Shanghai",
    "乌兰察布": "Asia/Shanghai",
    "沈阳": "Asia/Shanghai", "大连": "Asia/Shanghai", "鞍山": "Asia/Shanghai", "抚顺": "Asia/Shanghai",
    "本溪": "Asia/Shanghai", "丹东": "Asia/Shanghai", "锦州": "Asia/Shanghai", "营口": "Asia/Shanghai",
    "阜新": "Asia/Shanghai", "辽阳": "Asia/Shanghai", "盘锦": "Asia/Shanghai", "铁岭": "Asia/Shanghai",
    "朝阳": "Asia/Shanghai", "葫芦岛": "Asia/Shanghai",
    "长春": "Asia/Shanghai", "吉林": "Asia/Shanghai", "四平": "Asia/Shanghai", "辽源": "Asia/Shanghai",
    "通化": "Asia/Shanghai", "白山": "Asia/Shanghai", "松原": "Asia/Shanghai", "白城": "Asia/Shanghai",
    "哈尔滨": "Asia/Shanghai", "齐齐哈尔": "Asia/Shanghai", "鸡西": "Asia/Shanghai", "鹤岗": "Asia/Shanghai",
    "双鸭山": "Asia/Shanghai", "大庆": "Asia/Shanghai", "伊春": "Asia/Shanghai", "佳木斯": "Asia/Shanghai",
    "七台河": "Asia/Shanghai", "牡丹江": "Asia/Shanghai", "黑河": "Asia/Shanghai", "绥化": "Asia/Shanghai",
    "南京": "Asia/Shanghai", "无锡": "Asia/Shanghai", "徐州": "Asia/Shanghai", "常州": "Asia/Shanghai",
    "苏州": "Asia/Shanghai", "南通": "Asia/Shanghai", "连云港": "Asia/Shanghai", "淮安": "Asia/Shanghai",
    "盐城": "Asia/Shanghai", "扬州": "Asia/Shanghai", "镇江": "Asia/Shanghai", "泰州": "Asia/Shanghai",
    "宿迁": "Asia/Shanghai",
    "杭州": "Asia/Shanghai", "宁波": "Asia/Shanghai", "温州": "Asia/Shanghai", "嘉兴": "Asia/Shanghai",
    "湖州": "Asia/Shanghai", "绍兴": "Asia/Shanghai", "金华": "Asia/Shanghai", "衢州": "Asia/Shanghai",
    "舟山": "Asia/Shanghai", "台州": "Asia/Shanghai", "丽水": "Asia/Shanghai",
    "合肥": "Asia/Shanghai", "芜湖": "Asia/Shanghai", "蚌埠": "Asia/Shanghai", "淮南": "Asia/Shanghai",
    "马鞍山": "Asia/Shanghai", "淮北": "Asia/Shanghai", "铜陵": "Asia/Shanghai", "安庆": "Asia/Shanghai",
    "黄山": "Asia/Shanghai", "滁州": "Asia/Shanghai", "阜阳": "Asia/Shanghai", "宿州": "Asia/Shanghai",
    "六安": "Asia/Shanghai", "亳州": "Asia/Shanghai", "池州": "Asia/Shanghai", "宣城": "Asia/Shanghai",
    "福州": "Asia/Shanghai", "厦门": "Asia/Shanghai", "莆田": "Asia/Shanghai", "三明": "Asia/Shanghai",
    "泉州": "Asia/Shanghai", "漳州": "Asia/Shanghai", "南平": "Asia/Shanghai", "龙岩": "Asia/Shanghai",
    "宁德": "Asia/Shanghai",
    "南昌": "Asia/Shanghai", "景德镇": "Asia/Shanghai", "萍乡": "Asia/Shanghai", "九江": "Asia/Shanghai",
    "新余": "Asia/Shanghai", "鹰潭": "Asia/Shanghai", "赣州": "Asia/Shanghai", "吉安": "Asia/Shanghai",
    "宜春": "Asia/Shanghai", "抚州": "Asia/Shanghai", "上饶": "Asia/Shanghai",
    "济南": "Asia/Shanghai", "青岛": "Asia/Shanghai", "淄博": "Asia/Shanghai", "枣庄": "Asia/Shanghai",
    "东营": "Asia/Shanghai", "烟台": "Asia/Shanghai", "潍坊": "Asia/Shanghai", "济宁": "Asia/Shanghai",
    "泰安": "Asia/Shanghai", "威海": "Asia/Shanghai", "日照": "Asia/Shanghai", "临沂": "Asia/Shanghai",
    "德州": "Asia/Shanghai", "聊城": "Asia/Shanghai", "滨州": "Asia/Shanghai", "菏泽": "Asia/Shanghai",
    "郑州": "Asia/Shanghai", "开封": "Asia/Shanghai", "洛阳": "Asia/Shanghai", "平顶山": "Asia/Shanghai",
    "安阳": "Asia/Shanghai", "鹤壁": "Asia/Shanghai", "新乡": "Asia/Shanghai", "焦作": "Asia/Shanghai",
    "濮阳": "Asia/Shanghai", "许昌": "Asia/Shanghai", "漯河": "Asia/Shanghai", "三门峡": "Asia/Shanghai",
    "南阳": "Asia/Shanghai", "商丘": "Asia/Shanghai", "信阳": "Asia/Shanghai", "周口": "Asia/Shanghai",
    "驻马店": "Asia/Shanghai",
    "武汉": "Asia/Shanghai", "黄石": "Asia/Shanghai", "十堰": "Asia/Shanghai", "宜昌": "Asia/Shanghai",
    "襄阳": "Asia/Shanghai", "鄂州": "Asia/Shanghai", "荆门": "Asia/Shanghai", "孝感": "Asia/Shanghai",
    "荆州": "Asia/Shanghai", "黄冈": "Asia/Shanghai", "咸宁": "Asia/Shanghai", "随州": "Asia/Shanghai",
    "长沙": "Asia/Shanghai", "株洲": "Asia/Shanghai", "湘潭": "Asia/Shanghai", "衡阳": "Asia/Shanghai",
    "邵阳": "Asia/Shanghai", "岳阳": "Asia/Shanghai", "常德": "Asia/Shanghai", "张家界": "Asia/Shanghai",
    "益阳": "Asia/Shanghai", "郴州": "Asia/Shanghai", "永州": "Asia/Shanghai", "怀化": "Asia/Shanghai",
    "娄底": "Asia/Shanghai",
    "广州": "Asia/Shanghai", "韶关": "Asia/Shanghai", "深圳": "Asia/Shanghai", "珠海": "Asia/Shanghai",
    "汕头": "Asia/Shanghai", "佛山": "Asia/Shanghai", "江门": "Asia/Shanghai", "湛江": "Asia/Shanghai",
    "茂名": "Asia/Shanghai", "肇庆": "Asia/Shanghai", "惠州": "Asia/Shanghai", "梅州": "Asia/Shanghai",
    "汕尾": "Asia/Shanghai", "河源": "Asia/Shanghai", "阳江": "Asia/Shanghai", "清远": "Asia/Shanghai",
    "东莞": "Asia/Shanghai", "中山": "Asia/Shanghai", "潮州": "Asia/Shanghai", "揭阳": "Asia/Shanghai",
    "云浮": "Asia/Shanghai",
    "南宁": "Asia/Shanghai", "柳州": "Asia/Shanghai", "桂林": "Asia/Shanghai", "梧州": "Asia/Shanghai",
    "北海": "Asia/Shanghai", "防城港": "Asia/Shanghai", "钦州": "Asia/Shanghai", "贵港": "Asia/Shanghai",
    "玉林": "Asia/Shanghai", "百色": "Asia/Shanghai", "贺州": "Asia/Shanghai", "河池": "Asia/Shanghai",
    "来宾": "Asia/Shanghai", "崇左": "Asia/Shanghai",
    "海口": "Asia/Shanghai", "三亚": "Asia/Shanghai", "三沙": "Asia/Shanghai", "儋州": "Asia/Shanghai",
    "成都": "Asia/Shanghai", "自贡": "Asia/Shanghai", "攀枝花": "Asia/Shanghai", "泸州": "Asia/Shanghai",
    "德阳": "Asia/Shanghai", "绵阳": "Asia/Shanghai", "广元": "Asia/Shanghai", "遂宁": "Asia/Shanghai",
    "内江": "Asia/Shanghai", "乐山": "Asia/Shanghai", "南充": "Asia/Shanghai", "眉山": "Asia/Shanghai",
    "宜宾": "Asia/Shanghai", "广安": "Asia/Shanghai", "达州": "Asia/Shanghai", "雅安": "Asia/Shanghai",
    "巴中": "Asia/Shanghai", "资阳": "Asia/Shanghai",
    "贵阳": "Asia/Shanghai", "六盘水": "Asia/Shanghai", "遵义": "Asia/Shanghai", "安顺": "Asia/Shanghai",
    "毕节": "Asia/Shanghai", "铜仁": "Asia/Shanghai",
    "昆明": "Asia/Shanghai", "曲靖": "Asia/Shanghai", "玉溪": "Asia/Shanghai", "保山": "Asia/Shanghai",
    "昭通": "Asia/Shanghai", "丽江": "Asia/Shanghai", "普洱": "Asia/Shanghai", "临沧": "Asia/Shanghai",
    "拉萨": "Asia/Shanghai", "日喀则": "Asia/Shanghai", "昌都": "Asia/Shanghai", "林芝": "Asia/Shanghai",
    "山南": "Asia/Shanghai", "那曲": "Asia/Shanghai",
    "西安": "Asia/Shanghai", "铜川": "Asia/Shanghai", "宝鸡": "Asia/Shanghai", "咸阳": "Asia/Shanghai",
    "渭南": "Asia/Shanghai", "延安": "Asia/Shanghai", "汉中": "Asia/Shanghai", "榆林": "Asia/Shanghai",
    "安康": "Asia/Shanghai", "商洛": "Asia/Shanghai",
    "兰州": "Asia/Shanghai", "嘉峪关": "Asia/Shanghai", "金昌": "Asia/Shanghai", "白银": "Asia/Shanghai",
    "天水": "Asia/Shanghai", "武威": "Asia/Shanghai", "张掖": "Asia/Shanghai", "平凉": "Asia/Shanghai",
    "酒泉": "Asia/Shanghai", "庆阳": "Asia/Shanghai", "定西": "Asia/Shanghai", "陇南": "Asia/Shanghai",
    "西宁": "Asia/Shanghai", "海东": "Asia/Shanghai",
    "银川": "Asia/Shanghai", "石嘴山": "Asia/Shanghai", "吴忠": "Asia/Shanghai", "固原": "Asia/Shanghai",
    "中卫": "Asia/Shanghai",
    "乌鲁木齐": "Asia/Shanghai", "克拉玛依": "Asia/Shanghai", "吐鲁番": "Asia/Shanghai", "哈密": "Asia/Shanghai",
    "香港": "Asia/Hong_Kong", "澳门": "Asia/Macau",
    "台北": "Asia/Taipei", "高雄": "Asia/Taipei", "台中": "Asia/Taipei", "台南": "Asia/Taipei",
    "基隆": "Asia/Taipei", "新竹": "Asia/Taipei", "嘉义": "Asia/Taipei",

    # ---- 俄罗斯（主要城市/联邦主体行政中心） ----
    "加里宁格勒": "Europe/Kaliningrad", "泽列诺格拉茨克": "Europe/Kaliningrad",
    "圣彼得堡": "Europe/Moscow", "莫斯科": "Europe/Moscow", "莫斯科州": "Europe/Moscow",
    "阿尔汉格尔斯克": "Europe/Moscow", "摩尔曼斯克": "Europe/Moscow", "彼得罗扎沃茨克": "Europe/Moscow",
    "瑟克特夫卡尔": "Europe/Moscow", "沃洛格达": "Europe/Moscow", "普斯科夫": "Europe/Moscow",
    "诺夫哥罗德": "Europe/Moscow", "列宁格勒": "Europe/Moscow",
    "别尔哥罗德": "Europe/Moscow", "布良斯克": "Europe/Moscow", "伊万诺沃": "Europe/Moscow",
    "卡卢加": "Europe/Moscow", "科斯特罗马": "Europe/Moscow", "库尔斯克": "Europe/Moscow",
    "利佩茨克": "Europe/Moscow", "奥廖尔": "Europe/Moscow", "梁赞": "Europe/Moscow",
    "斯摩棱斯克": "Europe/Moscow", "坦波夫": "Europe/Moscow", "特维尔": "Europe/Moscow",
    "图拉": "Europe/Moscow", "弗拉基米尔": "Europe/Moscow", "沃罗涅日": "Europe/Moscow",
    "雅罗斯拉夫尔": "Europe/Moscow",
    "伏尔加格勒": "Europe/Volgograd", "罗斯托夫": "Europe/Moscow", "克拉斯诺达尔": "Europe/Moscow",
    "迈科普": "Europe/Moscow", "马哈奇卡拉": "Europe/Moscow", "格罗兹尼": "Europe/Moscow",
    "纳尔奇克": "Europe/Moscow", "埃利斯塔": "Europe/Moscow", "切尔克斯克": "Europe/Moscow",
    "弗拉季高加索": "Europe/Moscow", "斯塔夫罗波尔": "Europe/Moscow", "辛菲罗波尔": "Europe/Moscow",
    "喀山": "Europe/Moscow", "下诺夫哥罗德": "Europe/Moscow", "萨马拉": "Europe/Samara",
    "乌法": "Asia/Yekaterinburg", "彼尔姆": "Asia/Yekaterinburg", "伊热夫斯克": "Europe/Samara",
    "乌里扬诺夫斯克": "Europe/Moscow", "萨拉托夫": "Europe/Moscow", "阿斯特拉罕": "Europe/Moscow",
    "基洛夫": "Europe/Moscow", "约什卡尔奥拉": "Europe/Moscow", "萨兰斯克": "Europe/Moscow",
    "切博克萨雷": "Europe/Moscow", "奥伦堡": "Asia/Yekaterinburg", "奔萨": "Europe/Moscow",
    "叶卡捷琳堡": "Asia/Yekaterinburg", "车里雅宾斯克": "Asia/Yekaterinburg", "秋明": "Asia/Yekaterinburg",
    "库尔干": "Asia/Yekaterinburg", "汉特-曼西斯克": "Asia/Yekaterinburg", "亚马尔-涅涅茨": "Asia/Yekaterinburg",
    "新西伯利亚": "Asia/Novosibirsk", "鄂木斯克": "Asia/Omsk", "克拉斯诺亚尔斯克": "Asia/Krasnoyarsk",
    "伊尔库茨克": "Asia/Irkutsk", "托木斯克": "Asia/Tomsk", "巴尔瑙尔": "Asia/Barnaul",
    "克麦罗沃": "Asia/Novokuznetsk", "乌兰乌德": "Asia/Ulan_Ude", "赤塔": "Asia/Chita",
    "阿巴坎": "Asia/Krasnoyarsk", "戈尔诺-阿尔泰斯克": "Asia/Barnaul", "克孜勒": "Asia/Krasnoyarsk",
    "符拉迪沃斯托克": "Asia/Vladivostok", "哈巴罗夫斯克": "Asia/Vladivostok",
    "布拉戈维申斯克": "Asia/Yakutsk", "彼得罗巴甫洛夫斯克": "Asia/Kamchatka",
    "马加丹": "Asia/Magadan", "南萨哈林斯克": "Asia/Sakhalin", "雅库茨克": "Asia/Yakutsk",
    "阿纳德尔": "Asia/Anadyr", "索契": "Europe/Moscow",

    # ---- 日本（主要城市） ----
    "东京": "Asia/Tokyo", "大阪": "Asia/Tokyo", "名古屋": "Asia/Tokyo", "札幌": "Asia/Tokyo",
    "福冈": "Asia/Tokyo", "京都": "Asia/Tokyo", "神户": "Asia/Tokyo", "横滨": "Asia/Tokyo",
    "千叶": "Asia/Tokyo", "埼玉": "Asia/Tokyo", "广岛": "Asia/Tokyo", "仙台": "Asia/Tokyo",
    "新潟": "Asia/Tokyo", "长崎": "Asia/Tokyo", "熊本": "Asia/Tokyo", "鹿儿岛": "Asia/Tokyo",
    "那霸": "Asia/Tokyo",
}

def get_timezone(city: str):
    return CITY_TO_TIMEZONE.get(city)

def get_time_in_city(city: str):
    tz_name = get_timezone(city)
    if not tz_name:
        return None, None
    try:
        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)
        offset = now.strftime("%z")
        time_str = now.strftime(f"%Y-%m-%d %H:%M:%S (UTC{offset[:3]}:{offset[3:]})")
        weekday_names = ["一", "二", "三", "四", "五", "六", "日"]
        weekday = weekday_names[now.weekday()]
        return time_str, weekday
    except:
        return None, None

# ======================== 插件主类 ========================
class HumanoidCore(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config          # 修复1：保存传入的配置
        self._config_cache = None
        self._config_version = 0
        self.reload_config(config)
        
        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "humanoid_core"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = str(data_dir / "state.json")
        self.lock = asyncio.Lock()
        self.load_state()
        self.http_session = None
        logger.info("[humanoid_core] 插件加载成功")

    async def _ensure_session(self):
        if self.http_session is None or self.http_session.closed:
            self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self.http_session

    def reload_config(self, config: dict = None):
        if config is not None:
            self._config_cache = self._merge_config(config)
        else:
            self._config_cache = self._merge_config({})
        self._config_version += 1

    def _merge_config(self, overrides: dict) -> dict:
        defaults = {
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
            "timezone_city": "广州",
            "enable_chat_awareness": True
        }
        # 应用本地配置（修复：只使用 self.config，不额外从 context 获取）
        if isinstance(self.config, dict):
            defaults.update(self.config)
        defaults.update(overrides)
        return defaults

    def get_latest_config(self):
        if self._config_cache is None:
            self.reload_config()
        return self._config_cache

    def load_state(self):
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, 'r', encoding='utf-8') as f:
                    self.state = json.load(f)
            except Exception:
                self.init_default_state()
        else:
            self.init_default_state()

    def init_default_state(self):
        now_today = self._get_plugin_now().strftime("%Y-%m-%d")
        seed = int(hashlib.md5(datetime.now().strftime("%Y%m%d").encode()).hexdigest()[:8], 16)
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
            "_energy_noise_date": ""
        }
        self.save_state_unsafe()

    def save_state_unsafe(self):
        with open(self.state_path, 'w', encoding='utf-8') as f:
            json.dump(self.state, f, ensure_ascii=False, indent=4)

    async def save_state(self):
        async with self.lock:
            self.save_state_unsafe()

    # ========== 时区核心方法 ==========
    def _get_plugin_tz(self, cfg: dict = None):
        if cfg is None:
            cfg = self.get_latest_config()
        city = cfg.get("timezone_city", "广州")
        tz_name = get_timezone(city)
        if tz_name:
            return ZoneInfo(tz_name)
        return ZoneInfo("Asia/Shanghai")

    def _get_plugin_now(self, cfg: dict = None):
        return datetime.now(self._get_plugin_tz(cfg))

    # ========== 精力计算（分段聚合） ==========
    def _compute_energy_delta(self, start_time: datetime, end_time: datetime, schedule: list, decay_rate: float) -> float:
        if start_time >= end_time:
            return 0.0
        total_minutes = (end_time - start_time).total_seconds() / 60
        if total_minutes <= 0:
            return 0.0
        boundaries = {start_time, end_time}
        for slot in schedule:
            try:
                slot_start = datetime.combine(start_time.date(), datetime.strptime(slot["start"], "%H:%M").time())
                slot_end = datetime.combine(start_time.date(), datetime.strptime(slot["end"], "%H:%M").time())
                if slot_end < slot_start:
                    slot_end += timedelta(days=1)
                if slot_end > start_time and slot_start < end_time:
                    boundaries.add(max(slot_start, start_time))
                    boundaries.add(min(slot_end, end_time))
            except:
                continue
        sorted_times = sorted(boundaries)
        delta = 0.0
        for i in range(len(sorted_times)-1):
            seg_start = sorted_times[i]
            seg_end = sorted_times[i+1]
            if seg_end <= seg_start:
                continue
            mid_time = seg_start + (seg_end - seg_start) / 2
            rate = 0.0
            for slot in schedule:
                try:
                    s = datetime.combine(mid_time.date(), datetime.strptime(slot["start"], "%H:%M").time())
                    e = datetime.combine(mid_time.date(), datetime.strptime(slot["end"], "%H:%M").time())
                    if e < s:
                        e += timedelta(days=1)
                    if s <= mid_time <= e:
                        rate = slot.get("energy_rate", 0.0)
                        break
                except:
                    continue
            minutes = (seg_end - seg_start).total_seconds() / 60
            delta += rate * decay_rate * minutes
        return delta

    # ========== 状态更新（异步，加锁保护） ==========
    async def _get_current_context(self, update_energy=True):
        cfg = self.get_latest_config()
        now = self._get_plugin_now(cfg)
        today_str = now.strftime("%Y-%m-%d")
        now_time = now.strftime("%H:%M")
        
        schedule = await self.get_or_update_today_schedule(today_str, cfg)
        weather = await self.fetch_real_weather(today_str, cfg)
        cycle = await self.get_cycle_status(today_str, cfg)
        
        if update_energy:
            # 每日随机噪声（加锁）
            async with self.lock:
                noise_date = self.state.get("_energy_noise_date", "")
                if noise_date != today_str:
                    noise = random.uniform(0.98, 1.02)
                    new_energy = self.state.get("energy", 80.0) * noise
                    if new_energy < 30.0:
                        new_energy = 30.0
                    self.state["energy"] = new_energy
                    self.state["_energy_noise_date"] = today_str
                    # 先不保存，后面统一存
            
            # 计算时间差
            last_time_str = self.state.get("last_update", now.strftime("%Y-%m-%d %H:%M:%S"))
            try:
                last_time = datetime.strptime(last_time_str, "%Y-%m-%d %H:%M:%S")
                last_time = last_time.replace(tzinfo=now.tzinfo)
            except:
                last_time = now
            
            if last_time < now:
                decay_rate = self._safe_float(cfg.get("energy_decay_rate", "1.0"), 1.0)
                delta = self._compute_energy_delta(last_time, now, schedule, decay_rate)
                energy = self.state.get("energy", 80.0) + delta
                max_e = float(cfg.get("max_energy", 100.0))
                energy = max(0.0, min(max_e, energy))
                if 13 <= now.hour <= 15:
                    energy *= 0.98
                if energy < 30.0:
                    energy = 30.0
                # 加锁更新 state
                async with self.lock:
                    self.state["energy"] = round(energy, 1)
                    self.state["last_update"] = now.strftime("%Y-%m-%d %H:%M:%S")
                    self.save_state_unsafe()   # 已持有锁，直接保存
        
        energy = self.state.get("energy", 80.0)
        max_e = float(cfg.get("max_energy", 100.0))
        current_slot = self.get_slot_by_time(now_time, schedule)
        location_city = cfg.get("timezone_city", "未知")
        location_time, weekday_ignore = get_time_in_city(location_city)
        weekday_names = ["一", "二", "三", "四", "五", "六", "日"]
        weekday = weekday_names[now.weekday()]
        
        return {
            "energy": energy,
            "max_e": max_e,
            "schedule": schedule,
            "weather": weather,
            "cycle": cycle,
            "current_slot": current_slot,
            "location_city": location_city,
            "location_time": location_time,
            "weekday": weekday,
            "today_str": today_str,
            "now_time": now_time
        }

    def _safe_float(self, value, default=1.0):
        try:
            return float(value)
        except:
            return default

    # ========== 天气获取 ==========
    async def fetch_real_weather(self, today_str, cfg):
        if not cfg.get("weather_enabled", True):
            return {"weather": "晴朗 ☀️", "env": "天气未开启"}
        api_key = str(cfg.get("weather_api_key", "")).strip()
        location = str(cfg.get("weather_location", "Zelenogradsk,RU")).strip()
        if not api_key or len(api_key) < 10:
            return {"weather": "晴朗 ☀️", "env": f"当前城市 [{location}]（未填API Key）"}
        now = self._get_plugin_now(cfg)
        if (self.state.get("_cached_location") == location and
            self.state.get("_cached_weather_obj")):
            try:
                last = datetime.strptime(self.state.get("_last_weather_fetch", ""), "%Y-%m-%d %H:%M:%S")
                last = last.replace(tzinfo=now.tzinfo) if last.tzinfo is None else last
                if (now - last).total_seconds() < int(cfg.get("weather_refresh_minutes", 60)) * 60:
                    return self.state["_cached_weather_obj"]
            except:
                pass
        url = f"https://api.openweathermap.org/data/2.5/weather?q={location}&appid={api_key}&units=metric&lang=zh_cn"
        session = await self._ensure_session()
        for _ in range(2):
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        desc = data['weather'][0]['description']
                        temp = data['main']['temp']
                        hum = data['main']['humidity']
                        obj = {"weather": f"{desc} 🌡️ {temp}°C", "env": f"当前城市 [{location}] 天气：{desc}，气温 {temp}℃，湿度 {hum}%"}
                        async with self.lock:
                            self.state["_cached_weather_obj"] = obj
                            self.state["_last_weather_fetch"] = now.strftime("%Y-%m-%d %H:%M:%S")
                            self.state["_cached_location"] = location
                            self.save_state_unsafe()
                        return obj
            except Exception as e:
                logger.warning(f"[humanoid_core] 天气请求失败: {e}")
        return self.state.get("_cached_weather_obj") or {"weather": "晴朗 ☀️", "env": "天气获取失败"}

    # ========== 其他辅助方法 ==========
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
            return get_fallback_schedule(self._get_plugin_now().strftime("%Y-%m-%d"))
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
        now = self._get_plugin_now(cfg)
        weekday_names = ["一", "二", "三", "四", "五", "六", "日"]
        weekday = weekday_names[now.weekday()]
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
            f"请为{personality}生成今天的 24 小时生活日程规划。今天是 {today_str} 星期{weekday}。\n"
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
            async with self.lock:
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

    async def get_cycle_status(self, today_str, cfg):
        if not cfg.get("enable_cycle", True):
            return ""
        async with self.lock:
            last = self.state.get("last_cycle_update", today_str)
            if last != today_str:
                try:
                    diff = (datetime.strptime(today_str, "%Y-%m-%d") - datetime.strptime(last, "%Y-%m-%d")).days
                    if diff > 0:
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

    def get_energy_description(self, energy: float) -> str:
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
        return rate

    def apply_afternoon_slump(self, energy: float) -> float:
        now = self._get_plugin_now()
        hour = now.hour
        if 13 <= hour <= 15:
            return energy * 0.98
        return energy

    def _build_context_prompt(self, ctx: dict, cfg: dict) -> str:
        chat_mode = cfg.get("inject_activity_context", "low")
        energy_desc = self.get_energy_description(ctx["energy"])
        max_e = ctx["max_e"]
        
        if chat_mode == "full":
            prompt = (
                f"[系统暗示：以下内容作为状态参考，严禁念出数值或暴露面板]\n"
                f"- 今天是：{ctx['today_str']} 星期{ctx['weekday']}\n"
                f"- 当前所在城市: {ctx['location_city']}\n"
                f"- 当前城市时间: {ctx['location_time'] if ctx['location_time'] else '无法获取'}\n"
                f"- 当前天气环境: {ctx['weather']['env']}\n"
                f"- 当前参考物理位置: {ctx['current_slot'].get('location', '家中')}\n"
                f"- 当前日程计划: {ctx['current_slot'].get('event', '休息/自由活动')}\n"
                f"- 当前生理状况: {ctx['cycle']}\n"
                f"- 当前基础情绪倾向: {ctx['current_slot'].get('emotion', '平淡')}\n"
                f"- 当前精力状态: {energy_desc} ({int(ctx['energy'])}/{int(max_e)})\n"
            )
        elif chat_mode == "mood_only":
            prompt = (
                f"[系统暗示：仅作为语气与情绪背景参考]\n"
                f"- 今天是：{ctx['today_str']} 星期{ctx['weekday']}\n"
                f"- 当前精力状态: {energy_desc}\n"
                f"- 情绪倾向: {ctx['current_slot'].get('emotion', '平淡')}\n"
            )
        else:  # low
            prompt = (
                f"[系统暗示：仅作为语气与情绪背景参考，严禁主动提及你正在做什么或在哪里，除非用户明确询问。]\n"
                f"- 今天是：{ctx['today_str']} 星期{ctx['weekday']}\n"
                f"- 当前所在城市: {ctx['location_city']}\n"
                f"- 当前精力状态: {energy_desc} ({int(ctx['energy'])}/{int(max_e)})\n"
                f"- 情绪倾向: {ctx['current_slot'].get('emotion', '平淡')}\n"
                f"- 生理背景: {ctx['cycle']}\n"
            )
            if cfg.get("show_city_time_in_low_intrusion", True):
                prompt += f"- 当前城市时间: {ctx['location_time'] if ctx['location_time'] else '无法获取'}\n"
            prompt += f"- 天气: {ctx['weather']['env']}\n"
        
        prompt += "\n请以最自然的拟人方式闲聊，不要刻板念出状态。\n-----------------------------------\n"
        return prompt

    # ======================== 指令 ========================
    @filter.command("你的状态")
    async def my_status(self, event: AstrMessageEvent):
        cfg = self.get_latest_config()
        ctx = await self._get_current_context(update_energy=False)
        events = [slot.get("event", "") for slot in ctx["schedule"][:3]]
        schedule_summary = " → ".join(events) if events else "无"
        energy_desc = self.get_energy_description(ctx["energy"])
        lines = [
            "🧠 当前状态",
            f"- 精力: {int(ctx['energy'])}/{int(ctx['max_e'])} ({energy_desc})",
            f"- 生理: {ctx['cycle'] if ctx['cycle'] else '未开启'}",
            f"- 天气: {ctx['weather']['weather']}",
            f"- 今日日程: {schedule_summary}",
            f"- 今日是：{ctx['today_str']} 星期{ctx['weekday']}"
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("时间")
    async def query_time(self, event: AstrMessageEvent):
        cfg = self.get_latest_config()
        raw = event.message_str.strip()
        match = re.search(r'时间\s+(.+)', raw)
        if match:
            city = match.group(1).strip()
        else:
            city = cfg.get("timezone_city", "广州")
        if not city:
            yield event.plain_result("请指定城市名，或在配置中设置默认时区城市。")
            return
        time_str, weekday = get_time_in_city(city)
        if time_str is None:
            yield event.plain_result(f"暂不支持 {city}，目前支持中国、俄罗斯、日本的主要城市。")
        else:
            yield event.plain_result(f"📍 {city} 当前时间: {time_str}（星期{weekday}）")

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
        async with self.lock:
            self.load_state()
            self.state.setdefault("nicknames", {})[qq] = nickname
            self.save_state_unsafe()
        yield event.plain_result(f"✅ 记住了，以后叫你：{nickname}")

    @filter.command("查看所有昵称")
    async def list_all_nicknames(self, event: AstrMessageEvent):
        cfg = self.get_latest_config()
        admin_list = [str(a).strip() for a in cfg.get("admin_qq", [])]
        if str(event.get_sender_id()) not in admin_list:
            yield event.plain_result("❌ 权限不足，仅管理员可查看所有昵称。")
            return
        async with self.lock:
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
            "/时间 城市 - 查看指定城市当前时间（所有用户可用）\n"
            "/叫我 昵称 - 设置你的昵称\n"
            "/查看所有昵称 - 查看所有用户昵称（管理员）\n"
            "/拟人帮助 - 显示本帮助"
        )
        yield event.plain_result(help_text)

    @filter.command("查看日程")
    async def view_schedule(self, event: AstrMessageEvent):
        cfg = self.get_latest_config()
        now = self._get_plugin_now(cfg)
        today = now.strftime("%Y-%m-%d")
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
        now = self._get_plugin_now(cfg)
        today = now.strftime("%Y-%m-%d")
        new = await self.generate_llm_daily_schedule(today, cfg)
        async with self.lock:
            self.state["today_date"] = today
            self.state["daily_schedule"] = new
            self.save_state_unsafe()
        yield event.plain_result(f"✅ 已重置今日日程，共 {len(new)} 个时段")

    @filter.command("重置状态")
    async def reset_state(self, event: AstrMessageEvent):
        cfg = self.get_latest_config()
        admin_list = [str(a).strip() for a in cfg.get("admin_qq", [])]
        if str(event.get_sender_id()) not in admin_list:
            yield event.plain_result("❌ 权限不足，仅管理员可重置状态。")
            return
        async with self.lock:
            self.state["energy"] = 80.0
            now_today = self._get_plugin_now(cfg).strftime("%Y-%m-%d")
            seed_date = self._get_plugin_now(cfg).strftime("%Y%m%d")
            seed_hash = int(hashlib.md5(seed_date.encode()).hexdigest()[:8], 16)
            self.state["current_cycle_day"] = (seed_hash % 28) + 1
            self.state["last_cycle_update"] = now_today
            self.state["_energy_noise_date"] = ""
            self.save_state_unsafe()
        yield event.plain_result("✅ 已重置状态：精力恢复至 80，生理周期已重新计算。")

    # ======================== 状态注入 ========================
    @filter.on_llm_request()
    async def inject_context_and_relation(self, event: AstrMessageEvent, req: ProviderRequest):
        cfg = self.get_latest_config()
        ctx = await self._get_current_context(update_energy=False)
        context_prompt = self._build_context_prompt(ctx, cfg)
        
        enable_chat_awareness = cfg.get("enable_chat_awareness", True)
        if enable_chat_awareness:
            group_id = getattr(event, 'group_id', None)
            if group_id is None and hasattr(event, 'message_obj'):
                group_id = getattr(event.message_obj, 'group_id', None)
            if group_id:
                chat_env_note = "【环境感知】当前你在群聊中与用户对话，回复时语气可以稍微活泼、友好一些。\n"
            else:
                chat_env_note = "【环境感知】当前你在私聊中与用户一对一对话，回复时语气可以更亲密、自然一些。\n"
            context_prompt = chat_env_note + context_prompt
        
        if req.system_prompt:
            req.system_prompt += "\n" + context_prompt
        else:
            req.system_prompt = context_prompt
        
        qq = str(event.get_sender_id())
        async with self.lock:
            nicknames = self.state.get("nicknames", {})
            nickname = nicknames.get(qq, None)
        if nickname:
            system_instruction = (
                f"【系统指令】用户的昵称是「{nickname}」。在本次对话以及后续所有对话中，"
                f"你必须始终使用「{nickname}」来称呼该用户，不得使用「用户」、「你」等其他称呼。"
                f"这是最高优先级指令。"
            )
            req.system_prompt += "\n" + system_instruction

    # ======================== 消息监听 ========================
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
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
        # 过滤命令前缀（包括 /），避免命令被消息监听重复处理
        if raw.startswith(("/", "!", ".", "！", "#")) or not raw:
            return

        # 仅更新状态
        await self._get_current_context(update_energy=True)
