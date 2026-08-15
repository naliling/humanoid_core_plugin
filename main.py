import os
import json
import asyncio
import random
import re
import hashlib
import time
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

# ======================== 情绪映射表 ========================
def _round_to_bucket(value: float) -> float:
    buckets = [0.0, 12.5, 25.0, 37.5, 50.0]
    for b in buckets:
        if value <= b:
            return b
    return 50.0

def _get_affection_rank(affection: float) -> int:
    if affection < 12.5: return 0
    elif affection < 37.5: return 25
    elif affection < 62.5: return 50
    elif affection < 87.5: return 75
    else: return 100

AFFECTION_MAP = {
    0: {  # 好感度 0~12.5
        (50.0, 0.0): "漠然", (50.0, 12.5): "疏远", (50.0, 25.0): "防范", (50.0, 37.5): "戒备", (50.0, 50.0): "敌视",
        (37.5, 0.0): "冷淡", (37.5, 12.5): "避让", (37.5, 25.0): "嫌弃", (37.5, 37.5): "恼火", (37.5, 50.0): "厌恶",
        (25.0, 0.0): "客气", (25.0, 12.5): "距离", (25.0, 25.0): "隔阂", (25.0, 37.5): "抵触", (25.0, 50.0): "反感",
        (12.5, 0.0): "礼貌", (12.5, 12.5): "陌生", (12.5, 25.0): "谨慎", (12.5, 37.5): "不安", (12.5, 50.0): "警惕",
        (0.0, 0.0): "无视", (0.0, 12.5): "透明", (0.0, 25.0): "无视", (0.0, 37.5): "排斥", (0.0, 50.0): "驱逐",
    },
    25: {  # 好感度 12.5~37.5
        (50.0, 0.0): "留意", (50.0, 12.5): "好奇", (50.0, 25.0): "琢磨", (50.0, 37.5): "在意", (50.0, 50.0): "纠结",
        (37.5, 0.0): "平淡", (37.5, 12.5): "观察", (37.5, 25.0): "试探", (37.5, 37.5): "较劲", (37.5, 50.0): "不服",
        (25.0, 0.0): "普通", (25.0, 12.5): "随意", (25.0, 25.0): "平常", (25.0, 37.5): "别扭", (25.0, 50.0): "嘴硬",
        (12.5, 0.0): "温和", (12.5, 12.5): "淡然", (12.5, 25.0): "淡然", (12.5, 37.5): "淡漠", (12.5, 50.0): "冷漠",
        (0.0, 0.0): "漠视", (0.0, 12.5): "无视", (0.0, 25.0): "回避", (0.0, 37.5): "回避", (0.0, 50.0): "抗拒",
    },
    50: {  # 好感度 37.5~62.5
        (50.0, 0.0): "友好", (50.0, 12.5): "亲近", (50.0, 25.0): "在意", (50.0, 37.5): "吃醋", (50.0, 50.0): "闹腾",
        (37.5, 0.0): "好感", (37.5, 12.5): "喜欢", (37.5, 25.0): "欣赏", (37.5, 37.5): "调皮", (37.5, 50.0): "炸毛",
        (25.0, 0.0): "舒适", (25.0, 12.5): "放松", (25.0, 25.0): "自然", (25.0, 37.5): "较真", (25.0, 50.0): "倔强",
        (12.5, 0.0): "友善", (12.5, 12.5): "温和", (12.5, 25.0): "随性", (12.5, 37.5): "任性", (12.5, 50.0): "不讲理",
        (0.0, 0.0): "中立", (0.0, 12.5): "平静", (0.0, 25.0): "游离", (0.0, 37.5): "冷落", (0.0, 50.0): "疏离",
    },
    75: {  # 好感度 62.5~87.5
        (50.0, 0.0): "亲密", (50.0, 12.5): "依恋", (50.0, 25.0): "吃醋", (50.0, 37.5): "撒娇", (50.0, 50.0): "闹别扭",
        (37.5, 0.0): "温暖", (37.5, 12.5): "热情", (37.5, 25.0): "宠溺", (37.5, 37.5): "黏人", (37.5, 50.0): "使性子",
        (25.0, 0.0): "欣赏", (25.0, 12.5): "心动", (25.0, 25.0): "喜欢", (25.0, 37.5): "拉扯", (25.0, 50.0): "较劲",
        (12.5, 0.0): "随意", (12.5, 12.5): "自在", (12.5, 25.0): "惬意", (12.5, 37.5): "烦闷", (12.5, 50.0): "闹心",
        (0.0, 0.0): "安静", (0.0, 12.5): "沉默", (0.0, 25.0): "独处", (0.0, 37.5): "冷淡", (0.0, 50.0): "冷处理",
    },
    100: {  # 好感度 87.5~100
        (50.0, 0.0): "信赖", (50.0, 12.5): "依恋", (50.0, 25.0): "痴迷", (50.0, 37.5): "占有", (50.0, 50.0): "共依存",
        (37.5, 0.0): "眷恋", (37.5, 12.5): "深爱", (37.5, 25.0): "宠溺", (37.5, 37.5): "痴缠", (37.5, 50.0): "虐恋",
        (25.0, 0.0): "温柔", (25.0, 12.5): "呵护", (25.0, 25.0): "娇惯", (25.0, 37.5): "吃醋", (25.0, 50.0): "心疼",
        (12.5, 0.0): "安稳", (12.5, 12.5): "恬静", (12.5, 25.0): "安心", (12.5, 37.5): "委屈", (12.5, 50.0): "赌气",
        (0.0, 0.0): "默契", (0.0, 12.5): "平和", (0.0, 25.0): "淡然", (0.0, 37.5): "难过", (0.0, 50.0): "失落",
    },
}

def get_mood_label(affection: float, libido: float, aggression: float) -> str:
    rank = _get_affection_rank(affection)
    lib_bucket = _round_to_bucket(libido)
    agg_bucket = _round_to_bucket(aggression)
    table = AFFECTION_MAP.get(rank, {})
    return table.get((lib_bucket, agg_bucket), "普通")

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

# ======================== 城市 ⇄ 时区映射表 ========================
CITY_TO_TIMEZONE = {
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
        self.config = config
        self._config_cache = None
        self._config_version = 0
        self.reload_config(config)

        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "humanoid_core"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = str(data_dir / "state.json")
        self.lock = asyncio.Lock()
        self.load_state()
        self.http_session = None
        self._persona_switch_task = None
        logger.info("[humanoid_core] 插件加载成功")
        self._start_persona_auto_switch()

    def _start_persona_auto_switch(self):
        if self._persona_switch_task is None or self._persona_switch_task.done():
            self._persona_switch_task = asyncio.create_task(self._persona_auto_switch_loop())
            logger.info("[humanoid_core] 人格自动切换后台任务已启动")

    async def _persona_auto_switch_loop(self):
        cfg = self.get_latest_config()
        while True:
            try:
                await self._check_and_recover_expired_personas(cfg)

                if not cfg.get("persona_enabled", False):
                    await asyncio.sleep(60)
                    cfg = self.get_latest_config()
                    continue
                mode = cfg.get("persona_switch_mode", "manual")
                if mode == "manual":
                    await asyncio.sleep(60)
                    cfg = self.get_latest_config()
                    continue

                now = self._get_plugin_now(cfg)
                if mode == "daily":
                    next_run = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    if now >= next_run:
                        next_run += timedelta(days=1)
                    wait_seconds = (next_run - now).total_seconds()
                    await asyncio.sleep(wait_seconds)
                    await self._apply_global_persona_switch(cfg)
                elif mode == "hourly":
                    next_run = now.replace(minute=0, second=0, microsecond=0)
                    if now >= next_run:
                        next_run += timedelta(hours=1)
                    wait_seconds = (next_run - now).total_seconds()
                    await asyncio.sleep(wait_seconds)
                    await self._apply_global_persona_switch(cfg)
                elif mode == "custom":
                    custom_time = cfg.get("persona_switch_custom_time", "08:00")
                    try:
                        target_hour, target_minute = map(int, custom_time.split(":"))
                        if not (0 <= target_hour <= 23 and 0 <= target_minute <= 59):
                            raise ValueError
                    except:
                        target_hour, target_minute = 8, 0
                    next_run = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
                    if now >= next_run:
                        next_run += timedelta(days=1)
                    wait_seconds = (next_run - now).total_seconds()
                    await asyncio.sleep(wait_seconds)
                    await self._apply_global_persona_switch(cfg)
                else:
                    await asyncio.sleep(60)
                cfg = self.get_latest_config()
            except Exception as e:
                logger.error(f"[humanoid_core] 人格自动切换循环异常: {e}")
                await asyncio.sleep(60)

    async def _check_and_recover_expired_personas(self, cfg: dict):
        if not cfg.get("persona_auto_recover_enabled", True):
            return
        now = self._get_plugin_now(cfg)
        expiry_dict = self.state.get("user_persona_expiry", {})
        default_name = cfg.get("persona_default_name", "")
        if not default_name:
            return
        to_recover = []
        for qq, expiry_str in expiry_dict.items():
            try:
                expiry = datetime.fromisoformat(expiry_str)
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=now.tzinfo)
                if now >= expiry:
                    to_recover.append(qq)
            except:
                continue
        if not to_recover:
            return
        async with self.lock:
            user_persona = self.state.get("user_persona", {})
            for qq in to_recover:
                if user_persona.get(qq) == cfg.get("persona_switch_name", ""):
                    user_persona[qq] = default_name
                self.state.get("user_persona_expiry", {}).pop(qq, None)
            self.state["user_persona"] = user_persona
            self.save_state_unsafe()
            logger.info(f"[humanoid_core] 已恢复 {len(to_recover)} 个用户的人格为默认")

    async def _apply_global_persona_switch(self, cfg: dict):
        try:
            default_name = cfg.get("persona_default_name", "")
            switch_name = cfg.get("persona_switch_name", "")
            if not default_name or not switch_name:
                return
            new_default = switch_name
            new_switch = default_name
            default_desc = cfg.get("persona_default_description", "")
            switch_desc = cfg.get("persona_switch_description", "")
            cfg["persona_default_name"] = new_default
            cfg["persona_switch_name"] = new_switch
            cfg["persona_default_description"] = switch_desc
            cfg["persona_switch_description"] = default_desc

            async with self.lock:
                user_personas = self.state.get("user_persona", {})
                for qq, pname in list(user_personas.items()):
                    if pname == default_name:
                        user_personas[qq] = new_default
                    elif pname == switch_name:
                        user_personas[qq] = new_switch
                self.state["user_persona"] = user_personas
                self.save_state_unsafe()
            logger.info(f"[humanoid_core] 全局人格自动切换: {default_name} ↔ {switch_name}")
        except Exception as e:
            logger.error(f"[humanoid_core] 全局人格切换失败: {e}")

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
            "energy_decay_rate": "0.5",   # 修改默认值为0.5
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
            "enable_chat_awareness": True,
            "mood_enabled": True,
            "mood_sensitivity": 28,
            "mood_decay_hours": 2.0,
            "mood_initial_affection": 46,
            "mood_initial_libido": 24,
            "mood_initial_aggression": 28,
            "mood_affection_override": [],
            "mood_affection_delta_cap": 3,
            "mood_log_enabled": True,
            "mood_log_max_entries": 30,
            "mood_log_threshold_affection": 15,
            "mood_log_threshold_libido": 12,
            "mood_log_threshold_aggression": 12,
            "persona_enabled": False,
            "persona_default_name": "",
            "persona_default_description": "",
            "persona_switch_name": "",
            "persona_switch_description": "",
            "persona_switch_mode": "manual",
            "persona_switch_custom_time": "08:00",
            "persona_reset_mood_on_switch": False,
            "persona_auto_recover_enabled": True,
            "mood_update_timeout": 30.0
        }
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
            "_energy_noise_date": "",
            "moods": {},
            "_mood_decay_last_run": 0.0,
            "mood_logs": {},
            "user_persona": {},
            "global_persona_index": 0,
            "user_persona_expiry": {}
        }
        self.save_state_unsafe()

    def save_state_unsafe(self):
        with open(self.state_path, 'w', encoding='utf-8') as f:
            json.dump(self.state, f, ensure_ascii=False, indent=4)

    async def save_state(self):
        async with self.lock:
            self.save_state_unsafe()

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

    # ======================== 修改后的 _compute_energy_delta ========================
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
        consumption_discount = 0.7   # 消耗打7折

        # 获取当前周期日，用于周期系数
        cycle_day = self.state.get("current_cycle_day", 1)
        # 定义周期影响系数（经期消耗1.3倍，卵泡期0.8倍等）
        if 1 <= cycle_day <= 5:          # 经期
            cycle_factor = 1.3
        elif 6 <= cycle_day <= 12:       # 卵泡期
            cycle_factor = 0.8
        elif 13 <= cycle_day <= 15:      # 排卵期
            cycle_factor = 1.0
        elif 16 <= cycle_day <= 21:      # 黄体早期
            cycle_factor = 1.1
        elif 22 <= cycle_day <= 26:      # 黄体晚期
            cycle_factor = 1.2
        else:                            # 经前期 (27~28)
            cycle_factor = 1.15

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
            if rate < 0:
                # 消耗部分：基础打折 × 周期系数
                effective_rate = rate * consumption_discount * cycle_factor
            else:
                # 恢复部分不变
                effective_rate = rate
            delta += effective_rate * decay_rate * minutes
        return delta

    # ======================== 修改后的 _get_current_context ========================
    async def _get_current_context(self, update_energy=True):
        cfg = self.get_latest_config()
        now = self._get_plugin_now(cfg)
        today_str = now.strftime("%Y-%m-%d")
        now_time = now.strftime("%H:%M")

        schedule = await self.get_or_update_today_schedule(today_str, cfg)
        weather = await self.fetch_real_weather(today_str, cfg)
        cycle = await self.get_cycle_status(today_str, cfg)

        if update_energy:
            now = self._get_plugin_now(cfg)
            today_str = now.strftime("%Y-%m-%d")
            last_time_str = self.state.get("last_update", now.strftime("%Y-%m-%d %H:%M:%S"))
            try:
                last_time = datetime.strptime(last_time_str, "%Y-%m-%d %H:%M:%S")
                last_time = last_time.replace(tzinfo=now.tzinfo)
            except:
                last_time = now

            # 若跨天，重置精力并调整 last_time 为当天 00:00
            if last_time.date() < now.date():
                new_energy = 80.0 * random.uniform(0.95, 1.05)
                max_e = float(cfg.get("max_energy", 100.0))
                new_energy = max(5.0, min(max_e, new_energy))   # 保底改为5
                async with self.lock:
                    self.state["energy"] = round(new_energy, 1)
                    self.state["last_update"] = now.replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
                    self.state["_energy_noise_date"] = today_str
                    self.save_state_unsafe()
                last_time = datetime.strptime(self.state["last_update"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=now.tzinfo)

            # 同一天或已重置，计算从 last_time 到现在的增量
            if last_time < now:
                decay_rate = self._safe_float(cfg.get("energy_decay_rate", "1.0"), 1.0)
                delta = self._compute_energy_delta(last_time, now, schedule, decay_rate)
                energy = self.state.get("energy", 80.0) + delta
                max_e = float(cfg.get("max_energy", 100.0))
                energy = max(0.0, min(max_e, energy))
                if 13 <= now.hour <= 15:
                    energy *= 0.98
                if energy < 5.0:        # 保底改为5
                    energy = 5.0
                async with self.lock:
                    self.state["energy"] = round(energy, 1)
                    self.state["last_update"] = now.strftime("%Y-%m-%d %H:%M:%S")
                    self.save_state_unsafe()

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

    # ======================== 修改后的 get_cycle_status ========================
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
        if energy < 10:
            note = "，精力很低"
        elif energy < 30:
            note = "，精力偏低"
        elif energy > 80:
            note = "，精力充沛"
        else:
            note = ""

        if 1 <= day <= 5:
            phase = "经期"
            desc_full = f"处于【{phase}】，身体能量消耗较大，宜放慢节奏{note}"
            desc_simple = f"经期（第{day}天）"
        elif 6 <= day <= 12:
            phase = "卵泡期"
            desc_full = f"处于【{phase}】，能量水平逐步回升{note}"
            desc_simple = f"卵泡期（第{day}天）"
        elif 13 <= day <= 15:
            phase = "排卵期"
            desc_full = f"处于【{phase}】，能量储备相对充足{note}"
            desc_simple = f"排卵期（第{day}天）"
        elif 16 <= day <= 21:
            phase = "黄体早期"
            desc_full = f"处于【{phase}】，能量开始趋于平稳{note}"
            desc_simple = f"黄体早期（第{day}天）"
        elif 22 <= day <= 26:
            phase = "黄体晚期"
            desc_full = f"处于【{phase}】，能量水平逐渐回落{note}"
            desc_simple = f"黄体晚期（第{day}天）"
        else:  # 27~28
            phase = "经前期"
            desc_full = f"处于【{phase}】，能量状态略有波动{note}"
            desc_simple = f"经前期（第{day}天）"

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
        else:
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

    # ======================== 情绪方法 ========================
    def _get_user_mood(self, qq: str) -> dict:
        if "moods" not in self.state:
            self.state["moods"] = {}
        if qq not in self.state["moods"]:
            cfg = self.get_latest_config()
            override_list = cfg.get("mood_affection_override", [])
            override_val = None
            for item in override_list:
                if isinstance(item, str) and ":" in item:
                    qq_part, val_part = item.split(":", 1)
                    if qq_part.strip() == qq:
                        try:
                            override_val = float(val_part.strip())
                        except:
                            pass
                        break
            affection = float(cfg.get("mood_initial_affection", 46))
            if override_val is not None:
                affection = min(100.0, max(0.0, override_val))
            self.state["moods"][qq] = {
                "affection": affection,
                "libido": float(cfg.get("mood_initial_libido", 24)),
                "aggression": float(cfg.get("mood_initial_aggression", 28)),
                "base_affection": affection,
                "base_libido": float(cfg.get("mood_initial_libido", 24)),
                "base_aggression": float(cfg.get("mood_initial_aggression", 28)),
                "last_interaction": 0.0,
                "turn_count": 0
            }
            self.save_state_unsafe()
        return self.state["moods"][qq]

    def _save_user_mood(self, qq: str, data: dict):
        if "moods" not in self.state:
            self.state["moods"] = {}
        self.state["moods"][qq] = data
        self.save_state_unsafe()

    async def _apply_mood_decay(self, cfg: dict):
        async with self.lock:
            now = time.time()
            last_run = self.state.get("_mood_decay_last_run", now)
            elapsed_hours = (now - last_run) / 3600.0
            if elapsed_hours < 0.1:
                return
            duration = float(cfg.get("mood_decay_hours", 2.0))
            if duration <= 0:
                duration = 0.5

            moods = self.state.get("moods", {})
            updated = False
            for qq, data in moods.items():
                for key, base_key in [("affection", "base_affection"), ("libido", "base_libido"), ("aggression", "base_aggression")]:
                    base = data.get(base_key, 25)
                    current = data.get(key, 25)
                    deviation = current - base
                    if abs(deviation) < 0.001:
                        continue
                    if elapsed_hours >= duration:
                        delta = -deviation
                    else:
                        ratio = elapsed_hours / duration
                        delta = -deviation * (ratio ** 2)
                    new_val = current + delta
                    if key == "affection":
                        new_val = max(0.0, min(100.0, new_val))
                    else:
                        new_val = max(0.0, min(50.0, new_val))
                    if abs(new_val - current) > 0.0001:
                        data[key] = new_val
                        updated = True
            if updated:
                self.state["moods"] = moods
            self.state["_mood_decay_last_run"] = now
            if updated:
                self.save_state_unsafe()

    async def _update_mood_by_message(self, event: AstrMessageEvent, qq: str, cfg: dict):
        user_msg = event.message_str.strip() if hasattr(event, "message_str") else ""
        if not user_msg:
            return
        mood_data = self._get_user_mood(qq)
        now = time.time()
        if mood_data.get("last_interaction", 0) == 0:
            mood_data["last_interaction"] = now
            mood_data["turn_count"] = 1
            self._save_user_mood(qq, mood_data)
            return

        prompt = (
            f"用户说：{user_msg}\n"
            f"当前情绪状态：好感度 {mood_data['affection']:.1f}/100，亲近欲 {mood_data['libido']:.1f}/50，攻击性 {mood_data['aggression']:.1f}/50\n"
            "请分析这句话会让AI对用户的情绪产生什么变化。只返回JSON：{\"affection_delta\": 数值(-5~5), \"libido_delta\": 数值(-5~5), \"aggression_delta\": 数值(-5~5)}"
        )

        for attempt in range(2):
            try:
                provider = self.get_target_provider(cfg)
                if not provider:
                    return
                timeout = cfg.get("mood_update_timeout", 30.0)
                response = await asyncio.wait_for(provider.text_chat(prompt=prompt), timeout=timeout)
                raw = response.completion_text if hasattr(response, "completion_text") else str(response)
                match = re.search(r'\{[^{}]*\}', raw)
                if not match:
                    logger.warning(f"[humanoid_core] 情绪更新: 未匹配到JSON，原始响应: {raw[:100]}")
                    continue
                delta = json.loads(match.group())
                sensitivity = cfg.get("mood_sensitivity", 28) / 100.0
                delta_cap = cfg.get("mood_affection_delta_cap", 3)

                aff_delta = delta.get("affection_delta", 0) * sensitivity
                lib_delta = delta.get("libido_delta", 0) * sensitivity
                agg_delta = delta.get("aggression_delta", 0) * sensitivity

                aff_delta = max(-delta_cap, min(delta_cap, aff_delta))
                lib_delta = max(-delta_cap, min(delta_cap, lib_delta))
                agg_delta = max(-delta_cap, min(delta_cap, agg_delta))

                energy = self.state.get("energy", 80)
                if energy > 70:
                    if aff_delta > 0: aff_delta *= 1.3
                    else: aff_delta *= 0.7
                    if lib_delta > 0: lib_delta *= 1.3
                    else: lib_delta *= 0.7
                    if agg_delta > 0: agg_delta *= 1.3
                    else: agg_delta *= 0.7
                elif energy < 40:
                    aff_delta *= 0.5
                    lib_delta *= 0.5
                    agg_delta *= 0.5

                cycle_day = self.state.get("current_cycle_day", 1)
                if 1 <= cycle_day <= 5:
                    if aff_delta > 0: aff_delta *= 0.5
                    else: aff_delta *= 1.5
                    if lib_delta > 0: lib_delta *= 0.5
                    else: lib_delta *= 1.5
                    if agg_delta > 0: agg_delta *= 0.8
                    else: agg_delta *= 1.5
                elif 14 <= cycle_day <= 16:
                    if aff_delta > 0: aff_delta *= 1.4
                    else: aff_delta *= 0.6
                    if lib_delta > 0: lib_delta *= 1.4
                    else: lib_delta *= 0.6
                    if agg_delta > 0: agg_delta *= 1.2
                    else: agg_delta *= 0.8

                old_affection = mood_data["affection"]
                old_libido = mood_data["libido"]
                old_aggression = mood_data["aggression"]

                mood_data["affection"] = max(0.0, min(100.0, mood_data["affection"] + aff_delta))
                mood_data["libido"] = max(0.0, min(50.0, mood_data["libido"] + lib_delta))
                mood_data["aggression"] = max(0.0, min(50.0, mood_data["aggression"] + agg_delta))

                turn = mood_data.get("turn_count", 1)
                base_coef = 1.0 if turn <= 10 else 0.2
                mood_data["base_affection"] = max(0.0, min(100.0, mood_data["base_affection"] + aff_delta * base_coef * 0.5))
                mood_data["base_libido"] = max(0.0, min(50.0, mood_data["base_libido"] + lib_delta * base_coef * 0.5))
                mood_data["base_aggression"] = max(0.0, min(50.0, mood_data["base_aggression"] + agg_delta * base_coef * 0.5))

                mood_data["turn_count"] = turn + 1
                mood_data["last_interaction"] = now

                self._log_mood_event(qq, old_affection, old_libido, old_aggression,
                                     mood_data["affection"], mood_data["libido"], mood_data["aggression"], cfg)

                self._save_user_mood(qq, mood_data)
                return

            except asyncio.TimeoutError:
                if attempt == 1:
                    logger.warning(f"[humanoid_core] 情绪更新超时，已重试2次，超时时间 {timeout}秒")
                    return
            except Exception as e:
                import traceback
                if attempt == 1:
                    logger.warning(f"[humanoid_core] 情绪更新失败: {e}\n{traceback.format_exc()}")
                    return

    def _log_mood_event(self, qq: str, old_aff, old_lib, old_agg, new_aff, new_lib, new_agg, cfg: dict):
        if not cfg.get("mood_log_enabled", True):
            return
        thresh_aff = cfg.get("mood_log_threshold_affection", 15)
        thresh_lib = cfg.get("mood_log_threshold_libido", 12)
        thresh_agg = cfg.get("mood_log_threshold_aggression", 12)

        aff_delta = new_aff - old_aff
        lib_delta = new_lib - old_lib
        agg_delta = new_agg - old_agg

        events = []
        if abs(aff_delta) >= thresh_aff:
            events.append(f"好感度{'上升' if aff_delta > 0 else '下降'}至 {new_aff:.1f}")
        if abs(lib_delta) >= thresh_lib:
            events.append(f"亲近欲{'上升' if lib_delta > 0 else '下降'}至 {new_lib:.1f}")
        if abs(agg_delta) >= thresh_agg:
            events.append(f"攻击性{'上升' if agg_delta > 0 else '下降'}至 {new_agg:.1f}")

        if not events:
            return

        if "mood_logs" not in self.state:
            self.state["mood_logs"] = {}
        if qq not in self.state["mood_logs"]:
            self.state["mood_logs"][qq] = []
        max_entries = cfg.get("mood_log_max_entries", 30)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.state["mood_logs"][qq].append({
            "time": now_str,
            "event": "，".join(events),
            "affection": round(new_aff, 1),
            "libido": round(new_lib, 1),
            "aggression": round(new_agg, 1)
        })
        if len(self.state["mood_logs"][qq]) > max_entries:
            self.state["mood_logs"][qq] = self.state["mood_logs"][qq][-max_entries:]
        self.save_state_unsafe()

    # ======================== 多人格方法 ========================
    def _get_user_persona(self, qq: str, cfg: dict) -> dict:
        if not cfg.get("persona_enabled", False):
            return None
        user_persona = self.state.get("user_persona", {})
        current_name = user_persona.get(qq)
        if not current_name:
            current_name = cfg.get("persona_default_name", "")
            current_desc = cfg.get("persona_default_description", "")
        else:
            if current_name == cfg.get("persona_default_name"):
                current_desc = cfg.get("persona_default_description", "")
            elif current_name == cfg.get("persona_switch_name"):
                current_desc = cfg.get("persona_switch_description", "")
            else:
                current_name = cfg.get("persona_default_name", "")
                current_desc = cfg.get("persona_default_description", "")
        if not current_name:
            return None
        return {
            "name": current_name,
            "description": current_desc
        }

    def _get_available_personas(self, cfg: dict) -> list:
        personas = []
        if cfg.get("persona_default_name"):
            personas.append({
                "name": cfg["persona_default_name"],
                "description": cfg.get("persona_default_description", "")
            })
        if cfg.get("persona_switch_name"):
            personas.append({
                "name": cfg["persona_switch_name"],
                "description": cfg.get("persona_switch_description", "")
            })
        return personas

    async def _switch_persona(self, qq: str, cfg: dict, expire_time: datetime = None):
        switch_name = cfg.get("persona_switch_name", "")
        if not switch_name:
            return
        if "user_persona" not in self.state:
            self.state["user_persona"] = {}
        self.state["user_persona"][qq] = switch_name
        if expire_time:
            self.state.setdefault("user_persona_expiry", {})[qq] = expire_time.isoformat()
        else:
            self.state.get("user_persona_expiry", {}).pop(qq, None)
        if cfg.get("persona_reset_mood_on_switch", False):
            mood_data = self._get_user_mood(qq)
            mood_data["affection"] = cfg.get("mood_initial_affection", 46)
            mood_data["libido"] = cfg.get("mood_initial_libido", 24)
            mood_data["aggression"] = cfg.get("mood_initial_aggression", 28)
            mood_data["base_affection"] = cfg.get("mood_initial_affection", 46)
            mood_data["base_libido"] = cfg.get("mood_initial_libido", 24)
            mood_data["base_aggression"] = cfg.get("mood_initial_aggression", 28)
            self._save_user_mood(qq, mood_data)
        self.save_state_unsafe()

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
        if cfg.get("mood_enabled", True):
            qq = str(event.get_sender_id())
            await self._apply_mood_decay(cfg)
            mood = self._get_user_mood(qq)
            label = get_mood_label(mood["affection"], mood["libido"], mood["aggression"])
            lines.append(f"- 情绪: {label} (好感度 {mood['affection']:.1f})")
        if cfg.get("persona_enabled", False):
            qq = str(event.get_sender_id())
            persona = self._get_user_persona(qq, cfg)
            if persona:
                lines.append(f"- 人格: {persona.get('name', '未知')}")
        yield event.plain_result("\n".join(lines))

    @filter.command("好感度")
    async def cmd_mood_status(self, event: AstrMessageEvent):
        cfg = self.get_latest_config()
        if not cfg.get("mood_enabled", True):
            yield event.plain_result("情绪系统未开启。")
            return
        qq = str(event.get_sender_id())
        await self._apply_mood_decay(cfg)
        data = self._get_user_mood(qq)
        label = get_mood_label(data["affection"], data["libido"], data["aggression"])
        msg = (
            f"〖情绪档案〗\n"
            f"好感度：{data['affection']:.1f}/100\n"
            f"亲近欲：{data['libido']:.1f}/50（基线 {data['base_libido']:.1f}）\n"
            f"攻击性：{data['aggression']:.1f}/50（基线 {data['base_aggression']:.1f}）\n"
            f"当前标签：{label}"
        )
        yield event.plain_result(msg)

    @filter.command("情绪详情")
    async def cmd_mood_detail(self, event: AstrMessageEvent):
        cfg = self.get_latest_config()
        if not cfg.get("mood_enabled", True):
            yield event.plain_result("情绪系统未开启。")
            return
        qq = str(event.get_sender_id())
        await self._apply_mood_decay(cfg)
        data = self._get_user_mood(qq)
        label = get_mood_label(data["affection"], data["libido"], data["aggression"])
        msg = (
            f"〖情绪详细档案〗\n"
            f"好感度：{data['affection']:.1f}/100（基线 {data['base_affection']:.1f}）\n"
            f"亲近欲：{data['libido']:.1f}/50（基线 {data['base_libido']:.1f}）\n"
            f"攻击性：{data['aggression']:.1f}/50（基线 {data['base_aggression']:.1f}）\n"
            f"当前标签：{label}\n"
            f"交互轮次：{data.get('turn_count', 0)}"
        )
        yield event.plain_result(msg)

    @filter.command("情绪日志")
    async def cmd_mood_log(self, event: AstrMessageEvent):
        cfg = self.get_latest_config()
        if not cfg.get("mood_log_enabled", True):
            yield event.plain_result("❌ 情绪日志未启用。")
            return
        qq = str(event.get_sender_id())
        logs = self.state.get("mood_logs", {}).get(qq, [])
        if not logs:
            yield event.plain_result("📭 暂无情绪波动记录。")
            return
        recent = logs[-10:]
        lines = ["📋 情绪波动记录（最近10条）：", "——————————————"]
        for entry in recent:
            lines.append(f"{entry['time']} | {entry['event']}")
        yield event.plain_result("\n".join(lines))

    @filter.command("重置情绪")
    async def cmd_reset_mood(self, event: AstrMessageEvent):
        cfg = self.get_latest_config()
        admin_list = [str(a).strip() for a in cfg.get("admin_qq", [])]
        if str(event.get_sender_id()) not in admin_list:
            yield event.plain_result("❌ 权限不足，仅管理员可重置情绪。")
            return
        qq = str(event.get_sender_id())
        mood_data = self._get_user_mood(qq)
        affection = float(cfg.get("mood_initial_affection", 46))
        libido = float(cfg.get("mood_initial_libido", 24))
        aggression = float(cfg.get("mood_initial_aggression", 28))
        mood_data["affection"] = affection
        mood_data["libido"] = libido
        mood_data["aggression"] = aggression
        mood_data["base_affection"] = affection
        mood_data["base_libido"] = libido
        mood_data["base_aggression"] = aggression
        mood_data["turn_count"] = 0
        mood_data["last_interaction"] = 0
        self._save_user_mood(qq, mood_data)
        yield event.plain_result(f"✅ 已重置情绪至初始值（好感度 {affection}）。")

    @filter.command("设置好感度")
    async def cmd_set_affection(self, event: AstrMessageEvent):
        cfg = self.get_latest_config()
        admin_list = [str(a).strip() for a in cfg.get("admin_qq", [])]
        if str(event.get_sender_id()) not in admin_list:
            yield event.plain_result("❌ 权限不足。")
            return
        raw = event.message_str.strip()
        match = re.search(r'设置好感度\s+(\d+(?:\.\d+)?)', raw)
        if not match:
            yield event.plain_result("用法：/设置好感度 数值 (0-100)")
            return
        val = float(match.group(1))
        if val < 0 or val > 100:
            yield event.plain_result("数值必须在 0-100 之间。")
            return
        qq = str(event.get_sender_id())
        mood_data = self._get_user_mood(qq)
        mood_data["affection"] = val
        mood_data["base_affection"] = val
        self._save_user_mood(qq, mood_data)
        yield event.plain_result(f"✅ 好感度已设为 {val}")

    @filter.command("批量好感度")
    async def cmd_batch_affection(self, event: AstrMessageEvent):
        cfg = self.get_latest_config()
        admin_list = [str(a).strip() for a in cfg.get("admin_qq", [])]
        if str(event.get_sender_id()) not in admin_list:
            yield event.plain_result("❌ 权限不足，仅管理员可批量导入好感度。")
            return
        raw = event.message_str.strip()
        data = raw.replace("批量好感度", "").strip()
        if not data:
            yield event.plain_result("用法：/批量好感度 QQ号:数值, QQ号:数值 或 JSON 数组")
            return
        parsed = []
        try:
            arr = json.loads(data)
            if isinstance(arr, list):
                for item in arr:
                    if isinstance(item, dict) and "qq" in item and "value" in item:
                        parsed.append((str(item["qq"]), float(item["value"])))
        except:
            pass
        if not parsed:
            for part in re.split(r'[,，\s]+', data):
                if ":" in part:
                    parts = part.split(":", 1)
                    if len(parts) == 2:
                        try:
                            parsed.append((parts[0].strip(), float(parts[1].strip())))
                        except:
                            pass
        if not parsed:
            yield event.plain_result("格式错误，请使用：/批量好感度 QQ号:数值, QQ号:数值")
            return
        success = 0
        for qq, val in parsed:
            if val < 0 or val > 100:
                continue
            mood_data = self._get_user_mood(qq)
            mood_data["affection"] = val
            mood_data["base_affection"] = val
            self._save_user_mood(qq, mood_data)
            success += 1
        yield event.plain_result(f"✅ 已批量设置 {success} 个用户的好感度。")

    @filter.command("切换人格")
    async def cmd_switch_persona(self, event: AstrMessageEvent):
        cfg = self.get_latest_config()
        if not cfg.get("persona_enabled", False):
            yield event.plain_result("❌ 多人格系统未启用，请在配置中开启 `persona_enabled`。")
            return
        qq = str(event.get_sender_id())
        raw = event.message_str.strip()
        cmd_part = "切换人格"
        rest = raw[len(cmd_part):].strip()
        if not rest:
            yield event.plain_result("用法：/切换人格 人格名 [时间] （时间如 2h、30m、18:00）")
            return
        parts = rest.split()
        target_name = parts[0]
        personas = self._get_available_personas(cfg)
        target_persona = next((p for p in personas if p["name"] == target_name), None)
        if not target_persona:
            yield event.plain_result(f"❌ 不存在名为「{target_name}」的人格，可用的有：{', '.join([p['name'] for p in personas])}")
            return
        default_name = cfg.get("persona_default_name", "")
        expire_time = None
        if len(parts) > 1:
            time_arg = parts[1]
            now = self._get_plugin_now(cfg)
            if time_arg.endswith("h") or time_arg.endswith("H"):
                try:
                    hours = float(time_arg[:-1])
                    expire_time = now + timedelta(hours=hours)
                except:
                    yield event.plain_result("❌ 时间格式错误，示例：2h 或 18:00")
                    return
            elif time_arg.endswith("m") or time_arg.endswith("M"):
                try:
                    minutes = float(time_arg[:-1])
                    expire_time = now + timedelta(minutes=minutes)
                except:
                    yield event.plain_result("❌ 时间格式错误，示例：30m 或 18:00")
                    return
            elif ":" in time_arg:
                try:
                    hour, minute = map(int, time_arg.split(":"))
                    target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    if target_time <= now:
                        target_time += timedelta(days=1)
                    expire_time = target_time
                except:
                    yield event.plain_result("❌ 时间格式错误，示例：18:00")
                    return
            else:
                yield event.plain_result("❌ 无法识别时间参数，请使用如 2h、30m 或 18:00")
                return
        await self._switch_persona(qq, cfg, expire_time)
        if expire_time:
            time_str = expire_time.strftime("%Y-%m-%d %H:%M")
            yield event.plain_result(f"✅ 已切换至人格「{target_name}」，将于 {time_str} 自动恢复为「{default_name}」")
        else:
            yield event.plain_result(f"✅ 已永久切换至人格「{target_name}」")

    @filter.command("查看人格")
    async def cmd_view_persona(self, event: AstrMessageEvent):
        cfg = self.get_latest_config()
        if not cfg.get("persona_enabled", False):
            yield event.plain_result("❌ 多人格系统未启用。")
            return
        qq = str(event.get_sender_id())
        current_persona = self._get_user_persona(qq, cfg)
        current_name = current_persona.get("name", "未设置") if current_persona else "未设置"
        personas = self._get_available_personas(cfg)
        lines = [f"📋 当前人格：{current_name}", "", "可用人格列表："]
        for p in personas:
            name = p.get("name", "未命名")
            desc = p.get("description", "无描述")
            marker = "✅" if name == current_name else "  "
            lines.append(f"{marker} {name}：{desc}")
        if not personas:
            lines.append("（未配置任何人格，请在配置中填写 persona_default_name 和 persona_switch_name）")
        yield event.plain_result("\n".join(lines))

    @filter.command("人格详情")
    async def cmd_persona_detail(self, event: AstrMessageEvent):
        cfg = self.get_latest_config()
        if not cfg.get("persona_enabled", False):
            yield event.plain_result("❌ 多人格系统未启用。")
            return
        raw = event.message_str.strip()
        match = re.search(r'人格详情\s*(.+)', raw)
        if match:
            name = match.group(1).strip()
        else:
            qq = str(event.get_sender_id())
            persona = self._get_user_persona(qq, cfg)
            name = persona.get("name", "") if persona else ""
        if not name:
            yield event.plain_result("请指定人格名称，或先切换到一个已存在的人格。")
            return
        personas = self._get_available_personas(cfg)
        target = None
        for p in personas:
            if p.get("name") == name:
                target = p
                break
        if not target:
            yield event.plain_result(f"❌ 不存在名为「{name}」的人格。")
            return
        msg = (
            f"〖人格详情〗\n"
            f"名称：{target.get('name', '未命名')}\n"
            f"描述：{target.get('description', '无')}"
        )
        yield event.plain_result(msg)

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
            "/你的状态 - 查看当前精力、生理、天气、情绪、人格状态\n"
            "/时间 城市 - 查看指定城市当前时间（所有用户可用）\n"
            "/叫我 昵称 - 设置你的昵称\n"
            "/查看所有昵称 - 查看所有用户昵称（管理员）\n"
            "/好感度 - 查看情绪档案（好感度/亲近欲/攻击性）\n"
            "/情绪详情 - 查看详细情绪档案（含基线、轮次）\n"
            "/情绪日志 - 查看情绪波动记录\n"
            "/重置情绪 - 重置情绪至初始值（管理员）\n"
            "/设置好感度 数值 - 手动设置好感度（管理员）\n"
            "/批量好感度 QQ:数值,QQ:数值 - 批量导入好感度（管理员）\n"
            "/切换人格 人格名 [时间] - 切换到指定人格，可带时间（2h/30m/18:00）自动恢复\n"
            "/查看人格 - 查看当前人格和可用人格列表\n"
            "/人格详情 [名称] - 查看人格详细设定\n"
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

        if cfg.get("mood_enabled", True):
            await self._apply_mood_decay(cfg)
            mood_data = self._get_user_mood(qq)
            label = get_mood_label(mood_data["affection"], mood_data["libido"], mood_data["aggression"])
            mood_prompt = (
                f"〖当前情绪数值〗\n"
                f"亲近欲：{mood_data['libido']:.1f}/50（亲近/给予温暖的欲望）\n"
                f"攻击性：{mood_data['aggression']:.1f}/50（推开/伤害的冲动）\n"
                f"好感度：{mood_data['affection']:.1f}/100\n"
                f"参考标签：对用户「{label}」\n"
                "（请根据上述数值和你在人设中定义的「情绪驱动规则」来演绎角色，不要提及数值。）"
            )
            req.system_prompt += "\n\n" + mood_prompt

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
        if raw.startswith(("/", "!", ".", "！", "#")) or not raw:
            return

        await self._get_current_context(update_energy=True)

        if cfg.get("mood_enabled", True):
            qq = str(event.get_sender_id())
            await self._apply_mood_decay(cfg)
            asyncio.create_task(self._update_mood_by_message(event, qq, cfg))
