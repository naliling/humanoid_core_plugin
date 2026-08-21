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

# ======================== 情绪映射表（调整后） ========================
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
    0: {
        (50.0, 0.0): "漠然", (50.0, 12.5): "疏远", (50.0, 25.0): "防范", (50.0, 37.5): "戒备", (50.0, 50.0): "敌视",
        (37.5, 0.0): "冷淡", (37.5, 12.5): "避让", (37.5, 25.0): "嫌弃", (37.5, 37.5): "恼火", (37.5, 50.0): "厌恶",
        (25.0, 0.0): "客气", (25.0, 12.5): "距离", (25.0, 25.0): "隔阂", (25.0, 37.5): "抵触", (25.0, 50.0): "反感",
        (12.5, 0.0): "礼貌", (12.5, 12.5): "陌生", (12.5, 25.0): "谨慎", (12.5, 37.5): "不安", (12.5, 50.0): "警惕",
        (0.0, 0.0): "无视", (0.0, 12.5): "透明", (0.0, 25.0): "无视", (0.0, 37.5): "排斥", (0.0, 50.0): "驱逐",
    },
    25: {
        (50.0, 0.0): "留意", (50.0, 12.5): "好奇", (50.0, 25.0): "琢磨", (50.0, 37.5): "在意", (50.0, 50.0): "纠结",
        (37.5, 0.0): "平淡", (37.5, 12.5): "观察", (37.5, 25.0): "试探", (37.5, 37.5): "较劲", (37.5, 50.0): "不服",
        (25.0, 0.0): "普通", (25.0, 12.5): "随意", (25.0, 25.0): "平常", (25.0, 37.5): "别扭", (25.0, 50.0): "嘴硬",
        (12.5, 0.0): "温和", (12.5, 12.5): "淡然", (12.5, 25.0): "淡然", (12.5, 37.5): "淡漠", (12.5, 50.0): "冷漠",
        (0.0, 0.0): "漠视", (0.0, 12.5): "无视", (0.0, 25.0): "回避", (0.0, 37.5): "回避", (0.0, 50.0): "抗拒",
    },
    50: {
        (50.0, 0.0): "友好", (50.0, 12.5): "亲近", (50.0, 25.0): "在意", (50.0, 37.5): "吃醋", (50.0, 50.0): "闹腾",
        (37.5, 0.0): "好感", (37.5, 12.5): "喜欢", (37.5, 25.0): "欣赏", (37.5, 37.5): "调皮", (37.5, 50.0): "炸毛",
        (25.0, 0.0): "舒适", (25.0, 12.5): "放松", (25.0, 25.0): "自然", (25.0, 37.5): "较真", (25.0, 50.0): "倔强",
        (12.5, 0.0): "友善", (12.5, 12.5): "温和", (12.5, 25.0): "随性", (12.5, 37.5): "小脾气", (12.5, 50.0): "不讲理",
        (0.0, 0.0): "中立", (0.0, 12.5): "平静", (0.0, 25.0): "游离", (0.0, 37.5): "冷落", (0.0, 50.0): "疏离",
    },
    75: {
        (50.0, 0.0): "亲密", (50.0, 12.5): "依恋", (50.0, 25.0): "吃醋", (50.0, 37.5): "撒娇", (50.0, 50.0): "闹别扭",
        (37.5, 0.0): "温暖", (37.5, 12.5): "热情", (37.5, 25.0): "宠溺", (37.5, 37.5): "黏人", (37.5, 50.0): "使性子",
        (25.0, 0.0): "欣赏", (25.0, 12.5): "心动", (25.0, 25.0): "喜欢", (25.0, 37.5): "暧昧", (25.0, 50.0): "较劲",
        (12.5, 0.0): "随意", (12.5, 12.5): "自在", (12.5, 25.0): "惬意", (12.5, 37.5): "烦闷", (12.5, 50.0): "闹心",
        (0.0, 0.0): "安静", (0.0, 12.5): "沉默", (0.0, 25.0): "独处", (0.0, 37.5): "冷淡", (0.0, 50.0): "冷处理",
    },
    100: {
        (50.0, 0.0): "信赖", (50.0, 12.5): "依恋", (50.0, 25.0): "痴迷", (50.0, 37.5): "占有", (50.0, 50.0): "热烈",
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

# ======================== 内置公历节日 ========================
BUILTIN_HOLIDAYS = {
    "01-01": "元旦",
    "02-14": "情人节",
    "03-08": "妇女节",
    "03-12": "植树节",
    "04-01": "愚人节",
    "05-01": "劳动节",
    "06-01": "儿童节",
    "07-01": "建党节",
    "08-01": "建军节",
    "09-10": "教师节",
    "10-01": "国庆节",
    "12-25": "圣诞节",
}

# ======================== 时区工具函数（支持默认值回退） ========================
def get_timezone(city: str):
    """获取城市对应的时区名，若 city 为默认占位符则返回 None（由调用方处理回退）"""
    if city == "河源（记得改~）":
        return None
    return CITY_TO_TIMEZONE.get(city)

def get_system_timezone_city() -> str:
    """尝试从系统本地时区反查一个城市名，若无法反查则返回 '系统时区'"""
    try:
        local_tz = datetime.now().astimezone().tzinfo
        if local_tz and hasattr(local_tz, 'key'):
            tz_name = local_tz.key
            for city, tz in CITY_TO_TIMEZONE.items():
                if tz == tz_name:
                    return city
        return "系统时区"
    except:
        return "系统时区"

def get_time_in_city(city: str):
    """获取指定城市的时间，若 city 为默认占位符则回退到系统本地时区"""
    if city == "河源（记得改~）":
        try:
            now = datetime.now().astimezone()
            tz = now.tzinfo
            offset = now.strftime("%z")
            time_str = now.strftime(f"%Y-%m-%d %H:%M:%S (UTC{offset[:3]}:{offset[3:]})")
            weekday_names = ["一", "二", "三", "四", "五", "六", "日"]
            weekday = weekday_names[now.weekday()]
            return time_str, weekday, now
        except:
            return None, None, None
    tz_name = CITY_TO_TIMEZONE.get(city)
    if not tz_name:
        return None, None, None
    try:
        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)
        offset = now.strftime("%z")
        time_str = now.strftime(f"%Y-%m-%d %H:%M:%S (UTC{offset[:3]}:{offset[3:]})")
        weekday_names = ["一", "二", "三", "四", "五", "六", "日"]
        weekday = weekday_names[now.weekday()]
        return time_str, weekday, now
    except:
        return None, None, None

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
        self._stop_event = asyncio.Event()
        self._social_energy_task = None
        self._negative_pattern = re.compile(
            r'(傻|蠢|笨|白痴|废物|垃圾|去死|死吧|滚蛋|操|妈|逼|贱|恶心|讨厌|恨|烦|骂|吵|滚|弱智|脑残|sb|煞笔)',
            re.IGNORECASE
        )
        self._positive_pattern = re.compile(
            r'(爱|喜欢|好|棒|厉害|赞|开心|谢谢|感谢|乖|可爱|聪明)',
            re.IGNORECASE
        )
        logger.info("[humanoid_core] 插件加载成功 (v2.10.0)")
        self._start_social_energy_recovery()

    # ---------- 生命周期 ----------
    async def cleanup(self):
        self._stop_event.set()
        if self._social_energy_task and not self._social_energy_task.done():
            try:
                await asyncio.wait_for(self._social_energy_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._social_energy_task.cancel()
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
        logger.info("[humanoid_core] 已清理资源")

    # ---------- 后台任务安全运行包装 ----------
    async def _run_forever(self, coro_func, name: str):
        while not self._stop_event.is_set():
            try:
                await coro_func()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{name}] 异常退出，30秒后重启: {e}")
                await asyncio.sleep(30)

    # ---------- 社交能量恢复任务 ----------
    def _start_social_energy_recovery(self):
        if self._social_energy_task is None or self._social_energy_task.done():
            self._social_energy_task = asyncio.create_task(
                self._run_forever(self._social_energy_recovery_loop, "社交能量恢复")
            )
            logger.info("[humanoid_core] 社交能量恢复任务已启动")

    async def _social_energy_recovery_loop(self):
        cfg = self.get_latest_config()
        last_cfg_version = self._config_version
        while not self._stop_event.is_set():
            if self._config_version != last_cfg_version:
                cfg = self.get_latest_config()
                last_cfg_version = self._config_version

            interval = cfg.get("social_energy_recovery_interval_seconds", 60)
            interval = max(60, min(300, interval))

            if cfg.get("social_energy_enabled", True):
                recovery_per_min = cfg.get("social_energy_recovery_per_minute", 2.0)
                recovery = recovery_per_min * (interval / 60.0)
                async with self.lock:
                    se = self.state.get("social_energy", 100.0)
                    se = min(100.0, se + recovery)
                    self.state["social_energy"] = se

                    reset_hour = cfg.get("social_energy_reset_hour", 0)
                    if reset_hour >= 0:
                        now = self._get_plugin_now(cfg)
                        today = now.strftime("%Y-%m-%d")
                        last_reset = self.state.get("_last_social_energy_reset_date", "")
                        if last_reset != today and now.hour >= reset_hour:
                            self.state["social_energy"] = 100.0
                            self.state["_last_social_energy_reset_date"] = today
                    self.save_state_unsafe()
                    if cfg.get("debug_mode", False):
                        logger.debug(f"[humanoid_core] 社交能量恢复: {se:.1f}%")
            await asyncio.sleep(interval)

    # ---------- 基础方法 ----------
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
        logger.info("[humanoid_core] 配置已重载")

    def _merge_config(self, overrides: dict) -> dict:
        defaults = {
            "max_energy": 100.0,
            "enable_cycle": True,
            "cycle_length": 28,
            "energy_decay_rate": "0.5",
            "cycle_description_style": "default",
            "use_llm_schedule": True,
            "schedule_provider_name": "",
            "schedule_fallback_provider_name": "",
            "schedule_retry_interval_seconds": 2,
            "schedule_prompt_extra": "休闲日常，愉快的生活。",
            "schedule_time_granularity": "15min",
            "character_personality": "温柔体贴",
            "admin_qq": [],
            "weather_enabled": True,
            "weather_api_key": "",
            "weather_location": "Heyuan,CN",
            "weather_refresh_minutes": 60,
            "inject_activity_context": "low",
            "environment_mode": "both",
            "show_city_time_in_low_intrusion": True,
            "timezone_city": "河源（记得改~）",
            "enable_chat_awareness": True,
            "mood_enabled": True,
            "mood_sensitivity": 60,
            "mood_decay_hours": 6.0,
            "mood_initial_affection": 46,
            "mood_initial_libido": 34,
            "mood_initial_aggression": 28,
            "mood_affection_override": [],
            "mood_affection_delta_cap": 2,
            "mood_log_enabled": True,
            "mood_log_max_entries": 28,
            "mood_log_threshold_affection": 2,
            "mood_log_threshold_libido": 2,
            "mood_log_threshold_aggression": 1,
            "mood_update_timeout": 120.0,
            "mood_tag_enabled": True,
            "mood_use_llm_for_delta": False,
            "social_energy_enabled": True,
            "social_energy_consumption_per_msg": 0.05,
            "social_energy_recovery_per_minute": 2.0,
            "social_energy_reset_hour": 0,
            "social_energy_recovery_interval_seconds": 60,
            "night_mode_enabled": True,
            "night_start_hour": 23,
            "night_end_hour": 6,
            "night_mode_force_sleep": False,
            "debug_mode": False,
            "holidays": [],
            "enable_energy_natural_recovery": True,
            "energy_natural_recovery_per_minute": 0.9,
            "energy_natural_recovery_interval_minutes": 1,
            "energy_consumption_per_msg": 0.03,
            "energy_recovery_phase_multipliers": [0.5, 1.0, 2.0, 1.0, 0.8, 0.7],
        }
        if isinstance(self.config, dict):
            defaults.update(self.config)
        defaults.update(overrides)
        return defaults

    def get_latest_config(self):
        if self._config_cache is None:
            self.reload_config()
        return self._config_cache

    # ======================== 状态加载（增强健壮性） ========================
    def _ensure_state_fields(self, state: dict) -> dict:
        """确保 state 包含所有必要字段，缺失则用默认值补充"""
        defaults = {
            "energy": 80.0,
            "social_energy": 100.0,
            "mood_tags": {},
            "_last_social_energy_reset_date": "",
            "current_cycle_day": 1,
            "last_cycle_update": "",
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
        }
        for key, default_val in defaults.items():
            if key not in state:
                state[key] = default_val
        if state.get("current_cycle_day", 0) < 1 or state.get("current_cycle_day", 0) > 28:
            state["current_cycle_day"] = 1
        return state

    def load_state(self):
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, 'r', encoding='utf-8') as f:
                    raw_state = json.load(f)
                self.state = self._ensure_state_fields(raw_state)
                if not isinstance(self.state.get("daily_schedule"), list):
                    self.state["daily_schedule"] = []
                if not isinstance(self.state.get("nicknames"), dict):
                    self.state["nicknames"] = {}
                if not isinstance(self.state.get("moods"), dict):
                    self.state["moods"] = {}
                if not isinstance(self.state.get("mood_logs"), dict):
                    self.state["mood_logs"] = {}
                if not isinstance(self.state.get("mood_tags"), dict):
                    self.state["mood_tags"] = {}
                logger.info("[humanoid_core] 状态加载成功（部分字段已自动修复）")
            except Exception as e:
                logger.warning(f"[humanoid_core] 状态文件损坏，使用默认状态: {e}")
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
            "social_energy": 100.0,
            "mood_tags": {},
            "_last_social_energy_reset_date": "",
        }
        self.save_state_unsafe()

    def save_state_unsafe(self):
        temp_path = self.state_path + ".tmp"
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(self.state, f, ensure_ascii=False, indent=4)
        os.replace(temp_path, self.state_path)

    async def save_state(self):
        async with self.lock:
            self.save_state_unsafe()

    # ======================== 时区获取（支持默认值回退） ========================
    def _get_plugin_tz(self, cfg: dict = None):
        if cfg is None:
            cfg = self.get_latest_config()
        city = cfg.get("timezone_city", "河源（记得改~）")
        if city == "河源（记得改~）":
            try:
                return datetime.now().astimezone().tzinfo
            except:
                return ZoneInfo("Asia/Shanghai")
        tz_name = get_timezone(city)
        if tz_name:
            return ZoneInfo(tz_name)
        return ZoneInfo("Asia/Shanghai")

    def _get_plugin_now(self, cfg: dict = None):
        return datetime.now(self._get_plugin_tz(cfg))

    # ======================== 精力计算 ========================
    def _parse_time_to_datetime(self, time_str: str, base_date: datetime.date) -> datetime:
        if time_str == "24:00":
            return datetime.combine(base_date, datetime.min.time()) + timedelta(days=1)
        else:
            return datetime.combine(base_date, datetime.strptime(time_str, "%H:%M").time())

    def _compute_energy_delta(self, start_time: datetime, end_time: datetime, schedule: list, decay_rate: float) -> float:
        if start_time >= end_time:
            return 0.0
        total_minutes = (end_time - start_time).total_seconds() / 60
        if total_minutes <= 0:
            return 0.0

        boundaries = {start_time, end_time}
        for slot in schedule:
            try:
                s_dt = self._parse_time_to_datetime(slot["start"], start_time.date())
                e_dt = self._parse_time_to_datetime(slot["end"], start_time.date())
                if e_dt < s_dt:
                    e_dt += timedelta(days=1)
                if e_dt > start_time and s_dt < end_time:
                    boundaries.add(max(s_dt, start_time))
                    boundaries.add(min(e_dt, end_time))
            except Exception:
                continue

        sorted_times = sorted(boundaries)
        delta = 0.0
        consumption_discount = 0.7
        cycle_day = self.state.get("current_cycle_day", 1)
        if 1 <= cycle_day <= 5:
            cycle_factor = 1.3
        elif 6 <= cycle_day <= 12:
            cycle_factor = 0.8
        elif 13 <= cycle_day <= 15:
            cycle_factor = 1.0
        elif 16 <= cycle_day <= 21:
            cycle_factor = 1.1
        elif 22 <= cycle_day <= 26:
            cycle_factor = 1.2
        else:
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
                    s = self._parse_time_to_datetime(slot["start"], mid_time.date())
                    e = self._parse_time_to_datetime(slot["end"], mid_time.date())
                    if e < s:
                        e += timedelta(days=1)
                    if s <= mid_time <= e:
                        raw_rate = slot.get("energy_rate", 0.0)
                        try:
                            rate = float(raw_rate)
                        except (TypeError, ValueError):
                            rate = 0.0
                        break
                except Exception:
                    continue
            minutes = (seg_end - seg_start).total_seconds() / 60
            if rate < 0:
                effective_rate = rate * consumption_discount * cycle_factor
            else:
                effective_rate = rate
            delta += effective_rate * decay_rate * minutes

        cfg = self.get_latest_config()
        if cfg.get("debug_mode", False):
            logger.debug(f"[humanoid_core] 精力计算: 时段 {start_time} - {end_time}, 总变化 {delta:.2f}")
        return delta

    # ======================== 上下文获取 ========================
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

            if last_time.date() < now.date():
                new_energy = 80.0 * random.uniform(0.95, 1.05)
                max_e = float(cfg.get("max_energy", 100.0))
                new_energy = max(5.0, min(max_e, new_energy))
                async with self.lock:
                    self.state["energy"] = round(new_energy, 1)
                    self.state["last_update"] = now.replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
                    self.state["_energy_noise_date"] = today_str
                    self.save_state_unsafe()
                last_time = datetime.strptime(self.state["last_update"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=now.tzinfo)

            if last_time < now:
                decay_rate = self._safe_float(cfg.get("energy_decay_rate", "1.0"), 1.0)
                delta = self._compute_energy_delta(last_time, now, schedule, decay_rate)
                energy = self.state.get("energy", 80.0) + delta

                if cfg.get("enable_energy_natural_recovery", True):
                    minutes = (now - last_time).total_seconds() / 60.0
                    recovery_per_min = cfg.get("energy_natural_recovery_per_minute", 0.9)
                    cycle_day = self.state.get("current_cycle_day", 1)
                    phase_multipliers = cfg.get("energy_recovery_phase_multipliers", [0.5, 1.0, 2.0, 1.0, 0.8, 0.7])
                    if 1 <= cycle_day <= 5:
                        phase_idx = 0
                    elif 6 <= cycle_day <= 12:
                        phase_idx = 1
                    elif 13 <= cycle_day <= 15:
                        phase_idx = 2
                    elif 16 <= cycle_day <= 21:
                        phase_idx = 3
                    elif 22 <= cycle_day <= 26:
                        phase_idx = 4
                    else:
                        phase_idx = 5
                    phase_factor = phase_multipliers[phase_idx] if phase_idx < len(phase_multipliers) else 1.0
                    natural_delta = minutes * recovery_per_min * phase_factor
                    energy += natural_delta

                max_e = float(cfg.get("max_energy", 100.0))
                energy = max(0.0, min(max_e, energy))
                async with self.lock:
                    self.state["energy"] = round(energy, 1)
                    self.state["last_update"] = now.strftime("%Y-%m-%d %H:%M:%S")
                    self.save_state_unsafe()
                if cfg.get("debug_mode", False):
                    logger.debug(f"[humanoid_core] 精力更新: 变化后为 {energy:.1f}")

        energy = self.state.get("energy", 80.0)
        max_e = float(cfg.get("max_energy", 100.0))
        current_slot = self.get_slot_by_time(now_time, schedule)
        location_city = cfg.get("timezone_city", "未知")
        if location_city == "河源（记得改~）":
            location_city = "系统时区"
        location_time, weekday_ignore = get_time_in_city(cfg.get("timezone_city", "河源（记得改~）"))[:2]
        weekday_names = ["一", "二", "三", "四", "五", "六", "日"]
        weekday = weekday_names[now.weekday()]

        holiday = self._get_holiday_for_date(now, cfg)

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
            "now_time": now_time,
            "holiday": holiday,
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
        location = str(cfg.get("weather_location", "Heyuan,CN")).strip()
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
                        if cfg.get("debug_mode", False):
                            logger.debug(f"[humanoid_core] 天气获取成功: {desc}, {temp}°C")
                        return obj
            except Exception as e:
                logger.warning(f"[humanoid_core] 天气请求失败: {e}")
        return self.state.get("_cached_weather_obj") or {"weather": "晴朗 ☀️", "env": "天气获取失败"}

    # ---------- 原有 get_target_provider 保留给情绪分析 ----------
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

    # ======================== 【核心修改】Provider 获取（修正回退逻辑 + 增强匹配） ========================
    def get_schedule_provider(self, cfg: dict):
        target = str(cfg.get("schedule_provider_name", "")).strip()
        fallback = str(cfg.get("schedule_fallback_provider_name", "")).strip()

        def _normalize(name: str) -> str:
            # 统一分隔符、忽略大小写、去除前缀
            n = name.lower().replace('/', ':').replace('\\', ':')
            # 去掉常见的 provider 前缀，如 "openai:", "azure:" 等
            if ':' in n:
                parts = n.split(':', 1)
                if len(parts) == 2:
                    return parts[1]  # 只取模型名部分
            return n

        def _match_provider(input_name: str, provider_key: str) -> bool:
            # 精确匹配
            if input_name == provider_key:
                return True
            # 归一化后匹配
            norm_input = _normalize(input_name)
            norm_key = _normalize(provider_key)
            if norm_input == norm_key:
                return True
            # 子串匹配（输入包含在 key 中，或 key 包含在输入中）
            if norm_input in norm_key or norm_key in norm_input:
                return True
            return False

        # 获取所有可用的 Provider
        available = {}
        if hasattr(self.context, "providers") and isinstance(self.context.providers, dict):
            available = self.context.providers
        elif hasattr(self.context, "get_providers") and callable(self.context.get_providers):
            try:
                available = self.context.get_providers()
            except:
                pass

        def _get_by_name(name: str):
            if not name:
                return None
            # 1. 精确匹配（原方法）
            if hasattr(self.context, "get_provider"):
                try:
                    prov = self.context.get_provider(name)
                    if prov:
                        return prov
                except Exception:
                    pass
            # 2. 遍历可用 Provider，进行模糊匹配
            for key, prov in available.items():
                if _match_provider(name, key):
                    logger.info(f"[humanoid_core] 通过模糊匹配找到 Provider: {key} (输入名称: {name})")
                    return prov
            return None

        # 尝试首选
        if target:
            prov = _get_by_name(target)
            if prov:
                return prov
            logger.warning(f"[humanoid_core] 指定 Provider '{target}' 未找到")
            if cfg.get("debug_mode", False):
                logger.debug(f"[humanoid_core] 可用 Provider 列表: {list(available.keys())}")

        # 尝试备用
        if fallback:
            prov = _get_by_name(fallback)
            if prov:
                return prov
            logger.warning(f"[humanoid_core] 备用 Provider '{fallback}' 也未找到")
            if cfg.get("debug_mode", False):
                logger.debug(f"[humanoid_core] 可用 Provider 列表: {list(available.keys())}")

        # 最终回退到全局默认（并记录警告）
        if hasattr(self.context, "get_using_provider"):
            try:
                prov = self.context.get_using_provider()
                if prov:
                    logger.warning(f"[humanoid_core] 首选和备用均不可用，已回退到全局默认 Provider")
                    return prov
            except Exception:
                pass

        logger.error("[humanoid_core] 没有任何可用 Provider，将使用备用日程模板")
        return None

    # ======================== 日程相关 ========================
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
            try:
                rate = float(rate)
            except:
                rate = 0.0
            if rate > 0.3:
                slot["energy_rate"] = 0.3
            elif rate < -0.3:
                slot["energy_rate"] = -0.3
            else:
                slot["energy_rate"] = rate
        return fixed

    async def generate_llm_daily_schedule(self, today_str: str, cfg: dict) -> list:
        now = self._get_plugin_now(cfg)
        weekday_names = ["一", "二", "三", "四", "五", "六", "日"]
        weekday = weekday_names[now.weekday()]
        personality = cfg.get("character_personality", "温柔体贴")
        extra = cfg.get("schedule_prompt_extra", "休闲日常，愉快的生活。")
        granularity = cfg.get("schedule_time_granularity", "15min")
        granularity_hint = ""
        if granularity == "5min":
            granularity_hint = "请严格按每5分钟划分时间段（如 00:00-00:05-00:10...），每个时段的起止时间必须是5分钟的倍数。"
        elif granularity == "10min":
            granularity_hint = "请严格按每10分钟划分时间段（如 00:00-00:10-00:20...），每个时段的起止时间必须是10分钟的倍数。"
        elif granularity == "15min":
            granularity_hint = "请严格按每15分钟划分时间段（如 00:00-00:15-00:30...），每个时段的起止时间必须是15分钟的倍数。"
        elif granularity == "20min":
            granularity_hint = "请严格按每20分钟划分时间段（如 00:00-00:20-00:40...），每个时段的起止时间必须是20分钟的倍数。"
        elif granularity == "30min":
            granularity_hint = "请严格按每30分钟划分时间段（如 00:00-00:30-01:00...），每个时段的起止时间必须是30分钟的倍数。"
        elif granularity == "hourly":
            granularity_hint = "请按整小时划分时间段（如 00:00-08:00-09:00...），不要出现30分钟或45分钟等非整点时间。"
        else:
            granularity_hint = "时间粒度可以灵活自由，可以是5分钟、10分钟、15分钟、20分钟、30分钟、1小时等，根据活动内容自然决定。"

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
        if cfg.get("debug_mode", False):
            logger.debug(f"[humanoid_core] 日程生成 prompt: {prompt}")

        retry_interval = cfg.get("schedule_retry_interval_seconds", 2)
        for attempt in range(3):
            try:
                provider = self.get_schedule_provider(cfg)
                if not provider:
                    logger.warning(f"[humanoid_core] 无可用 Provider，尝试 {attempt+1}/3")
                    if attempt == 2:
                        break
                    await asyncio.sleep(retry_interval)
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
                    await asyncio.sleep(retry_interval)
                    continue
                raw = response.completion_text if hasattr(response, "completion_text") else str(response)
                if cfg.get("debug_mode", False):
                    logger.debug(f"[humanoid_core] 日程生成原始响应: {raw[:200]}...")
                parsed = extract_json_from_response(raw)
                if parsed and len(parsed) > 0:
                    logger.info(f"[humanoid_core] 生成成功，{len(parsed)} 个时段")
                    return self.validate_and_fix_schedule(parsed)
            except Exception as e:
                logger.warning(f"[humanoid_core] 生成失败 (尝试 {attempt+1}/3): {e}")
                if attempt == 2:
                    break
                await asyncio.sleep(retry_interval)
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

    # ======================== 周期状态 ========================
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
        else:
            phase = "经前期"
            desc_full = f"处于【{phase}】，能量状态略有波动{note}"
            desc_simple = f"经前期（第{day}天）"

        if style == "simple":
            return desc_simple
        return desc_full

    # ======================== 精力描述 ========================
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

    # ======================== 节日处理 ========================
    def _get_holiday_for_date(self, date_obj: datetime, cfg: dict) -> str:
        date_str = date_obj.strftime("%Y-%m-%d")
        holidays = cfg.get("holidays", [])
        for h in holidays:
            if isinstance(h, dict) and h.get("date") == date_str:
                return h.get("name", "")
        month_day = date_obj.strftime("%m-%d")
        return BUILTIN_HOLIDAYS.get(month_day, "")

    # ======================== 心情标签 ========================
    def _generate_mood_tag(self, affection: float, libido: float, aggression: float, energy: float) -> str:
        parts = []
        if energy >= 70:
            parts.append("精力充沛")
        elif energy >= 40:
            parts.append("状态平稳")
        else:
            parts.append("有点疲惫")

        if affection >= 70:
            parts.append("开心")
        elif affection >= 40:
            parts.append("平静")
        else:
            parts.append("冷淡")

        if libido >= 30 and aggression < 20:
            parts.append("亲切")
        elif aggression >= 30 and libido < 20:
            parts.append("疏离")
        elif aggression >= 30 and libido >= 30:
            parts.append("矛盾")
        else:
            parts.append("自然")

        unique = []
        for p in parts:
            if p not in unique:
                unique.append(p)
        return "，".join(unique)

    def _get_mood_tag(self, qq: str, cfg: dict) -> str:
        if not cfg.get("mood_tag_enabled", True):
            return ""
        if "mood_tags" not in self.state:
            self.state["mood_tags"] = {}
        return self.state["mood_tags"].get(qq, "")

    def _update_mood_tag(self, qq: str, cfg: dict, mood_data: dict, energy: float):
        if not cfg.get("mood_tag_enabled", True):
            return
        tag = self._generate_mood_tag(
            mood_data["affection"],
            mood_data["libido"],
            mood_data["aggression"],
            energy
        )
        if "mood_tags" not in self.state:
            self.state["mood_tags"] = {}
        self.state["mood_tags"][qq] = tag
        self.save_state_unsafe()

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
                "libido": float(cfg.get("mood_initial_libido", 34)),
                "aggression": float(cfg.get("mood_initial_aggression", 28)),
                "base_affection": affection,
                "base_libido": float(cfg.get("mood_initial_libido", 34)),
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
            duration = float(cfg.get("mood_decay_hours", 6.0))
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

    def _local_mood_delta(self, text: str) -> dict:
        if self._negative_pattern.search(text):
            return {"affection_delta": random.uniform(-4, -2),
                    "libido_delta": random.uniform(-2, -1),
                    "aggression_delta": random.uniform(2, 4)}
        elif self._positive_pattern.search(text):
            return {"affection_delta": random.uniform(1, 3),
                    "libido_delta": random.uniform(0.5, 2),
                    "aggression_delta": random.uniform(-1, -0.5)}
        else:
            return {"affection_delta": random.uniform(-0.5, 0.5),
                    "libido_delta": random.uniform(-0.3, 0.3),
                    "aggression_delta": random.uniform(-0.3, 0.3)}

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

        local_delta = self._local_mood_delta(user_msg)
        llm_delta = None

        if cfg.get("mood_use_llm_for_delta", False):
            for attempt in range(2):
                try:
                    provider = self.get_target_provider(cfg)
                    if not provider:
                        break
                    timeout = cfg.get("mood_update_timeout", 120.0)
                    prompt = (
                        f"用户说：{user_msg}\n"
                        f"当前情绪状态：好感度 {mood_data['affection']:.1f}/100，亲近欲 {mood_data['libido']:.1f}/50，攻击性 {mood_data['aggression']:.1f}/50\n"
                        "请分析这句话会让AI对用户的情绪产生什么变化。只返回JSON：{\"affection_delta\": 数值(-5~5), \"libido_delta\": 数值(-5~5), \"aggression_delta\": 数值(-5~5)}"
                    )
                    if cfg.get("debug_mode", False):
                        logger.debug(f"[humanoid_core] 情绪分析 prompt: {prompt}")
                    response = await asyncio.wait_for(provider.text_chat(prompt=prompt), timeout=timeout)
                    raw = response.completion_text if hasattr(response, "completion_text") else str(response)
                    if cfg.get("debug_mode", False):
                        logger.debug(f"[humanoid_core] 情绪分析响应: {raw}")
                    match = re.search(r'\{[^{}]*\}', raw)
                    if match:
                        llm_delta = json.loads(match.group())
                        break
                except Exception as e:
                    logger.warning(f"[humanoid_core] LLM情绪分析失败(尝试{attempt+1}): {e}")
                    continue

        if llm_delta:
            if local_delta["affection_delta"] < -1.5:
                aff_delta = local_delta["affection_delta"] * 1.2
                lib_delta = local_delta["libido_delta"] * 1.2
                agg_delta = local_delta["aggression_delta"] * 1.2
            else:
                weight_llm = 0.3
                aff_delta = llm_delta.get("affection_delta", 0) * weight_llm + local_delta["affection_delta"] * (1 - weight_llm)
                lib_delta = llm_delta.get("libido_delta", 0) * weight_llm + local_delta["libido_delta"] * (1 - weight_llm)
                agg_delta = llm_delta.get("aggression_delta", 0) * weight_llm + local_delta["aggression_delta"] * (1 - weight_llm)
        else:
            aff_delta = local_delta["affection_delta"]
            lib_delta = local_delta["libido_delta"]
            agg_delta = local_delta["aggression_delta"]

        sensitivity = cfg.get("mood_sensitivity", 60) / 100.0
        delta_cap = cfg.get("mood_affection_delta_cap", 2)
        aff_delta *= sensitivity
        lib_delta *= sensitivity
        agg_delta *= sensitivity
        aff_delta = max(-delta_cap, min(delta_cap, aff_delta))
        lib_delta = max(-delta_cap, min(delta_cap, lib_delta))
        agg_delta = max(-delta_cap, min(delta_cap, agg_delta))

        energy = self.state.get("energy", 80)
        if energy > 70:
            if aff_delta > 0: aff_delta *= 1.3
            if lib_delta > 0: lib_delta *= 1.3
            if agg_delta > 0: agg_delta *= 1.3
        elif energy < 40:
            aff_delta *= 0.8
            lib_delta *= 0.8
            agg_delta *= 0.8

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
            if lib_delta > 0: lib_delta *= 1.4
            if agg_delta > 0: agg_delta *= 1.2

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

        if aff_delta < -1:
            mood_data["base_affection"] = max(0.0, mood_data["base_affection"] + aff_delta * 0.3)

        mood_data["turn_count"] = turn + 1
        mood_data["last_interaction"] = now

        self._log_mood_event(qq, old_affection, old_libido, old_aggression,
                             mood_data["affection"], mood_data["libido"], mood_data["aggression"], cfg)
        self._update_mood_tag(qq, cfg, mood_data, energy)
        self._save_user_mood(qq, mood_data)

    def _log_mood_event(self, qq: str, old_aff, old_lib, old_agg, new_aff, new_lib, new_agg, cfg: dict):
        if not cfg.get("mood_log_enabled", True):
            return
        thresh_aff = cfg.get("mood_log_threshold_affection", 2)
        thresh_lib = cfg.get("mood_log_threshold_libido", 2)
        thresh_agg = cfg.get("mood_log_threshold_aggression", 1)

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
        max_entries = cfg.get("mood_log_max_entries", 28)
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

    # ======================== 指令 ========================
    @filter.command("重载配置")
    async def reload_config_cmd(self, event: AstrMessageEvent):
        cfg = self.get_latest_config()
        admin_list = [str(a).strip() for a in cfg.get("admin_qq", [])]
        if str(event.get_sender_id()) not in admin_list:
            yield event.plain_result("❌ 权限不足，仅管理员可重载配置。")
            return
        self.reload_config(self.config)
        yield event.plain_result("✅ 配置已重载，当前版本已生效。")

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
            f"- 今日是：{ctx['today_str']} 星期{ctx['weekday']}" + (f"（{ctx['holiday']}）" if ctx['holiday'] else "")
        ]
        qq = str(event.get_sender_id())
        if cfg.get("mood_enabled", True):
            await self._apply_mood_decay(cfg)
            mood = self._get_user_mood(qq)
            label = get_mood_label(mood["affection"], mood["libido"], mood["aggression"])
            lines.append(f"- 情绪: {label} (好感度 {mood['affection']:.1f})")
        if cfg.get("mood_tag_enabled", True):
            tag = self._get_mood_tag(qq, cfg)
            if tag:
                lines.append(f"- 心情标签: {tag}")
        if cfg.get("social_energy_enabled", True):
            se = self.state.get("social_energy", 100.0)
            se_desc = "充足" if se > 70 else "一般" if se > 40 else "较低"
            lines.append(f"- 社交能量: {int(se)}% ({se_desc})")
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
        if cfg.get("mood_tag_enabled", True):
            tag = self._get_mood_tag(qq, cfg)
            if tag:
                msg += f"\n心情标签：{tag}"
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
        if cfg.get("mood_tag_enabled", True):
            tag = self._get_mood_tag(qq, cfg)
            if tag:
                msg += f"\n心情标签：{tag}"
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
        libido = float(cfg.get("mood_initial_libido", 34))
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
        yield event.plain_result(f"✅ 已重置情绪：好感度 {affection}，亲近欲 {libido}，攻击性 {aggression}。")

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

    @filter.command("时间")
    async def query_time(self, event: AstrMessageEvent):
        cfg = self.get_latest_config()
        raw = event.message_str.strip()
        match = re.search(r'时间\s+(.+)', raw)
        if match:
            city = match.group(1).strip()
        else:
            city = cfg.get("timezone_city", "河源（记得改~）")
        if not city:
            yield event.plain_result("请指定城市名，或在配置中设置默认时区城市。")
            return
        display_city = city
        if city == "河源（记得改~）":
            display_city = get_system_timezone_city()
        time_str, weekday, now_dt = get_time_in_city(city)
        if time_str is None:
            yield event.plain_result(f"暂不支持 {display_city}，目前支持中国、俄罗斯、日本的主要城市。")
        else:
            holiday = self._get_holiday_for_date(now_dt, cfg) if now_dt else ""
            msg = f"📍 {display_city} 当前时间: {time_str}（星期{weekday}）"
            if holiday:
                msg += f"，今日节日：{holiday}"
            yield event.plain_result(msg)

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
            "📖 人形化伴侣插件 指令列表 (v2.10.0)\n"
            "\n"
            "/查看日程 - 查看今日完整日程\n"
            "/重置日程 - 强制重新生成日程（管理员）\n"
            "/重置状态 - 重置精力与生理周期（管理员）\n"
            "/你的状态 - 查看当前精力、生理、天气、情绪、社交能量状态\n"
            "/时间 城市 - 查看指定城市当前时间（所有用户可用）\n"
            "/叫我 昵称 - 设置你的昵称\n"
            "/查看所有昵称 - 查看所有用户昵称（管理员）\n"
            "/好感度 - 查看情绪档案（好感度/亲近欲/攻击性）\n"
            "/情绪详情 - 查看详细情绪档案（含基线、轮次）\n"
            "/情绪日志 - 查看情绪波动记录\n"
            "/重置情绪 - 重置情绪至初始值（管理员）\n"
            "/设置好感度 数值 - 手动设置好感度（管理员）\n"
            "/批量好感度 QQ:数值,QQ:数值 - 批量导入好感度（管理员）\n"
            "/重载配置 - 重载插件配置（管理员）\n"
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
        summary = " → ".join([f"{s.get('event', '')}" for s in new[:3]])
        yield event.plain_result(f"✅ 已重置今日日程，共 {len(new)} 个时段。\n📋 前3项：{summary}")

    @filter.command("重置状态")
    async def reset_state(self, event: AstrMessageEvent):
        cfg = self.get_latest_config()
        admin_list = [str(a).strip() for a in cfg.get("admin_qq", [])]
        if str(event.get_sender_id()) not in admin_list:
            yield event.plain_result("❌ 权限不足，仅管理员可重置状态。")
            return
        async with self.lock:
            self.state["energy"] = 80.0
            self.state["social_energy"] = 100.0
            now_today = self._get_plugin_now(cfg).strftime("%Y-%m-%d")
            seed_date = self._get_plugin_now(cfg).strftime("%Y%m%d")
            seed_hash = int(hashlib.md5(seed_date.encode()).hexdigest()[:8], 16)
            new_cycle_day = (seed_hash % 28) + 1
            self.state["current_cycle_day"] = new_cycle_day
            self.state["last_cycle_update"] = now_today
            self.state["_energy_noise_date"] = ""
            self.save_state_unsafe()
        yield event.plain_result(f"✅ 已重置状态：精力恢复至 80，社交能量恢复至 100，生理周期第 {new_cycle_day} 天。")

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

        if cfg.get("night_mode_enabled", True):
            now = self._get_plugin_now(cfg)
            start = cfg.get("night_start_hour", 23)
            end = cfg.get("night_end_hour", 6)
            if start <= end:
                is_night = start <= now.hour < end
            else:
                is_night = now.hour >= start or now.hour < end
            if is_night:
                force_sleep = cfg.get("night_mode_force_sleep", False)
                if force_sleep:
                    night_hint = "【夜间模式】当前是深夜，AI 处于睡眠状态，请仅回复一句“我现在需要休息，明天再聊吧”或类似简短提示。"
                else:
                    night_hint = "【夜间模式】当前是深夜，AI 应该表现得困倦、慵懒，回复尽量简短（不超过50字），可带“困”、“累”等词语。"
                context_prompt = night_hint + "\n" + context_prompt

        if cfg.get("social_energy_enabled", True):
            se = self.state.get("social_energy", 100.0)
            if se > 70:
                se_desc = "社交能量充足，回复可以热情、话多"
            elif se > 40:
                se_desc = "社交能量一般，回复保持正常"
            else:
                se_desc = "社交能量较低，回复应尽量简短，避免长篇大论"
            context_prompt += f"\n【社交能量】{se_desc}（当前值 {int(se)}%）"

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

    def _build_context_prompt(self, ctx: dict, cfg: dict) -> str:
        chat_mode = cfg.get("inject_activity_context", "low")
        energy_desc = self.get_energy_description(ctx["energy"])
        max_e = ctx["max_e"]
        date_line = f"今天是：{ctx['today_str']} 星期{ctx['weekday']}"
        if ctx.get("holiday"):
            date_line += f"（{ctx['holiday']}）"

        if chat_mode == "full":
            prompt = (
                f"[系统暗示：以下内容作为状态参考，严禁念出数值或暴露面板]\n"
                f"- {date_line}\n"
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
                f"- {date_line}\n"
                f"- 当前精力状态: {energy_desc}\n"
                f"- 情绪倾向: {ctx['current_slot'].get('emotion', '平淡')}\n"
            )
        else:
            prompt = (
                f"[系统暗示：仅作为语气与情绪背景参考，严禁主动提及你正在做什么或在哪里，除非用户明确询问。]\n"
                f"- {date_line}\n"
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
        if not raw:
            return

        await self._get_current_context(update_energy=True)

        if cfg.get("energy_consumption_per_msg", 0.03) > 0:
            consume = cfg.get("energy_consumption_per_msg", 0.03)
            async with self.lock:
                energy = self.state.get("energy", 80.0)
                energy = max(0.0, energy - consume)
                self.state["energy"] = energy
                self.save_state_unsafe()

        if cfg.get("social_energy_enabled", True):
            consumption = cfg.get("social_energy_consumption_per_msg", 0.05)
            async with self.lock:
                se = self.state.get("social_energy", 100.0)
                se = max(0.0, se - consumption)
                self.state["social_energy"] = se
                reset_hour = cfg.get("social_energy_reset_hour", 0)
                if reset_hour >= 0:
                    now = self._get_plugin_now(cfg)
                    today = now.strftime("%Y-%m-%d")
                    last_reset = self.state.get("_last_social_energy_reset_date", "")
                    if last_reset != today and now.hour >= reset_hour:
                        self.state["social_energy"] = 100.0
                        self.state["_last_social_energy_reset_date"] = today
                self.save_state_unsafe()

        if cfg.get("mood_enabled", True):
            qq = str(event.get_sender_id())
            await self._apply_mood_decay(cfg)
            asyncio.create_task(self._update_mood_by_message(event, qq, cfg))