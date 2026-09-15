"""城市 ⇄ 时区映射表：中国全部地级行政区、俄罗斯全部联邦主体、日本全部都道府县。

三张表按「她报出这个地名时，那边现在是几点」来组织：

* 中国大陆与港澳台：法定统一使用北京时间（`Asia/Shanghai`），香港、澳门、台北有各自的
  IANA 名但偏移相同。新疆的地州也按北京时间收录——中国铁路、电视、手机都走北京时间；
  想让她的作息按当地 UTC+6 走，把配置项填成 `Asia/Urumqi` 即可（见 `resolve_zone_name`）。
* 俄罗斯：11 个时区（UTC+2 → UTC+12），2014 年起无夏令时。按联邦主体归属逐条映射。
* 日本：全国统一 UTC+9（`Asia/Tokyo`），冲绳亦然。

`IANA_DISPLAY_NAMES` 是给注入用的：她说「你在雷克雅未克」而不是「你在 Atlantic/Reykjavik」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

DEFAULT_CITY_PLACEHOLDER = "河源（记得改~）"

_ZONE_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]*/[A-Za-z0-9_/+-]+")

# ----------------------------------------------------------------------
# 中国：全部地级行政区（293 地级市 + 7 地区 + 30 自治州 + 3 盟）+ 省直辖县级
# 法定时区：Asia/Shanghai（北京时间）；港澳台用各自的 IANA 名，偏移同为 +8。
# ----------------------------------------------------------------------
_CHINA = {
    # 直辖市
    "北京": "Asia/Shanghai", "天津": "Asia/Shanghai", "上海": "Asia/Shanghai", "重庆": "Asia/Shanghai",
    # 河北
    "石家庄": "Asia/Shanghai", "唐山": "Asia/Shanghai", "秦皇岛": "Asia/Shanghai", "邯郸": "Asia/Shanghai",
    "邢台": "Asia/Shanghai", "保定": "Asia/Shanghai", "张家口": "Asia/Shanghai", "承德": "Asia/Shanghai",
    "沧州": "Asia/Shanghai", "廊坊": "Asia/Shanghai", "衡水": "Asia/Shanghai",
    # 山西
    "太原": "Asia/Shanghai", "大同": "Asia/Shanghai", "朔州": "Asia/Shanghai", "忻州": "Asia/Shanghai",
    "阳泉": "Asia/Shanghai", "吕梁": "Asia/Shanghai", "晋中": "Asia/Shanghai", "长治": "Asia/Shanghai",
    "晋城": "Asia/Shanghai", "临汾": "Asia/Shanghai", "运城": "Asia/Shanghai",
    # 内蒙古（9 市 + 3 盟 + 1 地区级市）
    "呼和浩特": "Asia/Shanghai", "包头": "Asia/Shanghai", "乌海": "Asia/Shanghai", "赤峰": "Asia/Shanghai",
    "通辽": "Asia/Shanghai", "鄂尔多斯": "Asia/Shanghai", "乌兰察布": "Asia/Shanghai",
    "巴彦淖尔": "Asia/Shanghai", "呼伦贝尔": "Asia/Shanghai",
    "兴安盟": "Asia/Shanghai", "锡林郭勒盟": "Asia/Shanghai", "阿拉善盟": "Asia/Shanghai",
    # 辽宁
    "沈阳": "Asia/Shanghai", "大连": "Asia/Shanghai", "鞍山": "Asia/Shanghai", "抚顺": "Asia/Shanghai",
    "本溪": "Asia/Shanghai", "丹东": "Asia/Shanghai", "锦州": "Asia/Shanghai", "营口": "Asia/Shanghai",
    "阜新": "Asia/Shanghai", "辽阳": "Asia/Shanghai", "盘锦": "Asia/Shanghai", "铁岭": "Asia/Shanghai",
    "朝阳": "Asia/Shanghai", "葫芦岛": "Asia/Shanghai",
    # 吉林
    "长春": "Asia/Shanghai", "吉林": "Asia/Shanghai", "四平": "Asia/Shanghai", "辽源": "Asia/Shanghai",
    "通化": "Asia/Shanghai", "白山": "Asia/Shanghai", "松原": "Asia/Shanghai", "白城": "Asia/Shanghai",
    "延边": "Asia/Shanghai", "延吉": "Asia/Shanghai",
    # 黑龙江
    "哈尔滨": "Asia/Shanghai", "齐齐哈尔": "Asia/Shanghai", "鸡西": "Asia/Shanghai", "鹤岗": "Asia/Shanghai",
    "双鸭山": "Asia/Shanghai", "大庆": "Asia/Shanghai", "伊春": "Asia/Shanghai", "佳木斯": "Asia/Shanghai",
    "七台河": "Asia/Shanghai", "牡丹江": "Asia/Shanghai", "黑河": "Asia/Shanghai", "绥化": "Asia/Shanghai",
    "大兴安岭": "Asia/Shanghai", "加格达奇": "Asia/Shanghai",
    # 江苏
    "南京": "Asia/Shanghai", "无锡": "Asia/Shanghai", "徐州": "Asia/Shanghai", "常州": "Asia/Shanghai",
    "苏州": "Asia/Shanghai", "南通": "Asia/Shanghai", "连云港": "Asia/Shanghai", "淮安": "Asia/Shanghai",
    "盐城": "Asia/Shanghai", "扬州": "Asia/Shanghai", "镇江": "Asia/Shanghai", "泰州": "Asia/Shanghai",
    "宿迁": "Asia/Shanghai", "昆山": "Asia/Shanghai", "江阴": "Asia/Shanghai", "张家港": "Asia/Shanghai",
    # 浙江
    "杭州": "Asia/Shanghai", "宁波": "Asia/Shanghai", "温州": "Asia/Shanghai", "嘉兴": "Asia/Shanghai",
    "湖州": "Asia/Shanghai", "绍兴": "Asia/Shanghai", "金华": "Asia/Shanghai", "衢州": "Asia/Shanghai",
    "舟山": "Asia/Shanghai", "台州": "Asia/Shanghai", "丽水": "Asia/Shanghai", "义乌": "Asia/Shanghai",
    # 安徽
    "合肥": "Asia/Shanghai", "芜湖": "Asia/Shanghai", "蚌埠": "Asia/Shanghai", "淮南": "Asia/Shanghai",
    "马鞍山": "Asia/Shanghai", "淮北": "Asia/Shanghai", "铜陵": "Asia/Shanghai", "安庆": "Asia/Shanghai",
    "黄山": "Asia/Shanghai", "滁州": "Asia/Shanghai", "阜阳": "Asia/Shanghai", "宿州": "Asia/Shanghai",
    "六安": "Asia/Shanghai", "亳州": "Asia/Shanghai", "池州": "Asia/Shanghai", "宣城": "Asia/Shanghai",
    # 福建
    "福州": "Asia/Shanghai", "厦门": "Asia/Shanghai", "莆田": "Asia/Shanghai", "三明": "Asia/Shanghai",
    "泉州": "Asia/Shanghai", "漳州": "Asia/Shanghai", "南平": "Asia/Shanghai", "龙岩": "Asia/Shanghai",
    "宁德": "Asia/Shanghai",
    # 江西
    "南昌": "Asia/Shanghai", "景德镇": "Asia/Shanghai", "萍乡": "Asia/Shanghai", "九江": "Asia/Shanghai",
    "新余": "Asia/Shanghai", "鹰潭": "Asia/Shanghai", "赣州": "Asia/Shanghai", "吉安": "Asia/Shanghai",
    "宜春": "Asia/Shanghai", "抚州": "Asia/Shanghai", "上饶": "Asia/Shanghai",
    # 山东
    "济南": "Asia/Shanghai", "青岛": "Asia/Shanghai", "淄博": "Asia/Shanghai", "枣庄": "Asia/Shanghai",
    "东营": "Asia/Shanghai", "烟台": "Asia/Shanghai", "潍坊": "Asia/Shanghai", "济宁": "Asia/Shanghai",
    "泰安": "Asia/Shanghai", "威海": "Asia/Shanghai", "日照": "Asia/Shanghai", "临沂": "Asia/Shanghai",
    "德州": "Asia/Shanghai", "聊城": "Asia/Shanghai", "滨州": "Asia/Shanghai", "菏泽": "Asia/Shanghai",
    # 河南
    "郑州": "Asia/Shanghai", "开封": "Asia/Shanghai", "洛阳": "Asia/Shanghai", "平顶山": "Asia/Shanghai",
    "安阳": "Asia/Shanghai", "鹤壁": "Asia/Shanghai", "新乡": "Asia/Shanghai", "焦作": "Asia/Shanghai",
    "濮阳": "Asia/Shanghai", "许昌": "Asia/Shanghai", "漯河": "Asia/Shanghai", "三门峡": "Asia/Shanghai",
    "南阳": "Asia/Shanghai", "商丘": "Asia/Shanghai", "信阳": "Asia/Shanghai", "周口": "Asia/Shanghai",
    "驻马店": "Asia/Shanghai", "济源": "Asia/Shanghai",
    # 湖北
    "武汉": "Asia/Shanghai", "黄石": "Asia/Shanghai", "十堰": "Asia/Shanghai", "宜昌": "Asia/Shanghai",
    "襄阳": "Asia/Shanghai", "鄂州": "Asia/Shanghai", "荆门": "Asia/Shanghai", "孝感": "Asia/Shanghai",
    "荆州": "Asia/Shanghai", "黄冈": "Asia/Shanghai", "咸宁": "Asia/Shanghai", "随州": "Asia/Shanghai",
    "恩施": "Asia/Shanghai", "仙桃": "Asia/Shanghai", "潜江": "Asia/Shanghai", "天门": "Asia/Shanghai",
    # 湖南
    "长沙": "Asia/Shanghai", "株洲": "Asia/Shanghai", "湘潭": "Asia/Shanghai", "衡阳": "Asia/Shanghai",
    "邵阳": "Asia/Shanghai", "岳阳": "Asia/Shanghai", "常德": "Asia/Shanghai", "张家界": "Asia/Shanghai",
    "益阳": "Asia/Shanghai", "郴州": "Asia/Shanghai", "永州": "Asia/Shanghai", "怀化": "Asia/Shanghai",
    "娄底": "Asia/Shanghai", "湘西": "Asia/Shanghai", "吉首": "Asia/Shanghai",
    # 广东
    "广州": "Asia/Shanghai", "韶关": "Asia/Shanghai", "深圳": "Asia/Shanghai", "珠海": "Asia/Shanghai",
    "汕头": "Asia/Shanghai", "佛山": "Asia/Shanghai", "江门": "Asia/Shanghai", "湛江": "Asia/Shanghai",
    "茂名": "Asia/Shanghai", "肇庆": "Asia/Shanghai", "惠州": "Asia/Shanghai", "梅州": "Asia/Shanghai",
    "汕尾": "Asia/Shanghai", "河源": "Asia/Shanghai", "阳江": "Asia/Shanghai", "清远": "Asia/Shanghai",
    "东莞": "Asia/Shanghai", "中山": "Asia/Shanghai", "潮州": "Asia/Shanghai", "揭阳": "Asia/Shanghai",
    "云浮": "Asia/Shanghai",
    # 广西
    "南宁": "Asia/Shanghai", "柳州": "Asia/Shanghai", "桂林": "Asia/Shanghai", "梧州": "Asia/Shanghai",
    "北海": "Asia/Shanghai", "防城港": "Asia/Shanghai", "钦州": "Asia/Shanghai", "贵港": "Asia/Shanghai",
    "玉林": "Asia/Shanghai", "百色": "Asia/Shanghai", "贺州": "Asia/Shanghai", "河池": "Asia/Shanghai",
    "来宾": "Asia/Shanghai", "崇左": "Asia/Shanghai",
    # 海南
    "海口": "Asia/Shanghai", "三亚": "Asia/Shanghai", "三沙": "Asia/Shanghai", "儋州": "Asia/Shanghai",
    "文昌": "Asia/Shanghai", "琼海": "Asia/Shanghai", "万宁": "Asia/Shanghai", "东方": "Asia/Shanghai",
    "五指山": "Asia/Shanghai", "定安": "Asia/Shanghai", "屯昌": "Asia/Shanghai", "澄迈": "Asia/Shanghai",
    "临高": "Asia/Shanghai", "白沙": "Asia/Shanghai", "昌江": "Asia/Shanghai", "乐东": "Asia/Shanghai",
    "陵水": "Asia/Shanghai", "保亭": "Asia/Shanghai", "琼中": "Asia/Shanghai",
    # 四川
    "成都": "Asia/Shanghai", "自贡": "Asia/Shanghai", "攀枝花": "Asia/Shanghai", "泸州": "Asia/Shanghai",
    "德阳": "Asia/Shanghai", "绵阳": "Asia/Shanghai", "广元": "Asia/Shanghai", "遂宁": "Asia/Shanghai",
    "内江": "Asia/Shanghai", "乐山": "Asia/Shanghai", "南充": "Asia/Shanghai", "眉山": "Asia/Shanghai",
    "宜宾": "Asia/Shanghai", "广安": "Asia/Shanghai", "达州": "Asia/Shanghai", "雅安": "Asia/Shanghai",
    "巴中": "Asia/Shanghai", "资阳": "Asia/Shanghai",
    "阿坝": "Asia/Shanghai", "马尔康": "Asia/Shanghai", "甘孜": "Asia/Shanghai", "康定": "Asia/Shanghai",
    "凉山": "Asia/Shanghai", "西昌": "Asia/Shanghai",
    # 贵州
    "贵阳": "Asia/Shanghai", "六盘水": "Asia/Shanghai", "遵义": "Asia/Shanghai", "安顺": "Asia/Shanghai",
    "毕节": "Asia/Shanghai", "铜仁": "Asia/Shanghai",
    "黔西南": "Asia/Shanghai", "兴义": "Asia/Shanghai", "黔东南": "Asia/Shanghai", "凯里": "Asia/Shanghai",
    "黔南": "Asia/Shanghai", "都匀": "Asia/Shanghai",
    # 云南
    "昆明": "Asia/Shanghai", "曲靖": "Asia/Shanghai", "玉溪": "Asia/Shanghai", "保山": "Asia/Shanghai",
    "昭通": "Asia/Shanghai", "丽江": "Asia/Shanghai", "普洱": "Asia/Shanghai", "临沧": "Asia/Shanghai",
    "楚雄": "Asia/Shanghai", "红河": "Asia/Shanghai", "蒙自": "Asia/Shanghai", "文山": "Asia/Shanghai",
    "西双版纳": "Asia/Shanghai", "景洪": "Asia/Shanghai", "大理": "Asia/Shanghai", "德宏": "Asia/Shanghai",
    "芒市": "Asia/Shanghai", "怒江": "Asia/Shanghai", "泸水": "Asia/Shanghai", "迪庆": "Asia/Shanghai",
    "香格里拉": "Asia/Shanghai",
    # 西藏
    "拉萨": "Asia/Shanghai", "日喀则": "Asia/Shanghai", "昌都": "Asia/Shanghai", "林芝": "Asia/Shanghai",
    "山南": "Asia/Shanghai", "泽当": "Asia/Shanghai", "那曲": "Asia/Shanghai", "阿里": "Asia/Shanghai",
    "噶尔": "Asia/Shanghai",
    # 陕西
    "西安": "Asia/Shanghai", "铜川": "Asia/Shanghai", "宝鸡": "Asia/Shanghai", "咸阳": "Asia/Shanghai",
    "渭南": "Asia/Shanghai", "延安": "Asia/Shanghai", "汉中": "Asia/Shanghai", "榆林": "Asia/Shanghai",
    "安康": "Asia/Shanghai", "商洛": "Asia/Shanghai",
    # 甘肃
    "兰州": "Asia/Shanghai", "嘉峪关": "Asia/Shanghai", "金昌": "Asia/Shanghai", "白银": "Asia/Shanghai",
    "天水": "Asia/Shanghai", "武威": "Asia/Shanghai", "张掖": "Asia/Shanghai", "平凉": "Asia/Shanghai",
    "酒泉": "Asia/Shanghai", "庆阳": "Asia/Shanghai", "定西": "Asia/Shanghai", "陇南": "Asia/Shanghai",
    "甘南": "Asia/Shanghai", "合作": "Asia/Shanghai", "临夏": "Asia/Shanghai",
    # 青海
    "西宁": "Asia/Shanghai", "海东": "Asia/Shanghai", "海北": "Asia/Shanghai", "祁连": "Asia/Shanghai",
    "黄南": "Asia/Shanghai", "同仁": "Asia/Shanghai", "海南州": "Asia/Shanghai", "共和": "Asia/Shanghai",
    "果洛": "Asia/Shanghai", "玛沁": "Asia/Shanghai", "玉树": "Asia/Shanghai", "海西": "Asia/Shanghai",
    "格尔木": "Asia/Shanghai", "德令哈": "Asia/Shanghai", "茫崖": "Asia/Shanghai",
    # 宁夏
    "银川": "Asia/Shanghai", "石嘴山": "Asia/Shanghai", "吴忠": "Asia/Shanghai", "固原": "Asia/Shanghai",
    "中卫": "Asia/Shanghai",
    # 新疆（法定北京时间；想按当地 UTC+6 作息就填 Asia/Urumqi）
    "乌鲁木齐": "Asia/Shanghai", "克拉玛依": "Asia/Shanghai", "吐鲁番": "Asia/Shanghai",
    "哈密": "Asia/Shanghai", "昌吉": "Asia/Shanghai", "博尔塔拉": "Asia/Shanghai", "博乐": "Asia/Shanghai",
    "巴音郭楞": "Asia/Shanghai", "库尔勒": "Asia/Shanghai", "阿克苏": "Asia/Shanghai",
    "克孜勒苏": "Asia/Shanghai", "阿图什": "Asia/Shanghai", "喀什": "Asia/Shanghai", "和田": "Asia/Shanghai",
    "伊犁": "Asia/Shanghai", "伊宁": "Asia/Shanghai", "塔城": "Asia/Shanghai", "阿勒泰": "Asia/Shanghai",
    "石河子": "Asia/Shanghai", "阿拉尔": "Asia/Shanghai", "图木舒克": "Asia/Shanghai",
    "五家渠": "Asia/Shanghai", "北屯": "Asia/Shanghai", "铁门关": "Asia/Shanghai", "双河": "Asia/Shanghai",
    "可克达拉": "Asia/Shanghai", "昆玉": "Asia/Shanghai", "霍尔果斯": "Asia/Shanghai",
    "新疆": "Asia/Shanghai", "乌鲁木齐（当地作息）": "Asia/Urumqi",
    # 香港 / 澳门
    "香港": "Asia/Hong_Kong", "九龙": "Asia/Hong_Kong", "新界": "Asia/Hong_Kong",
    "澳门": "Asia/Macau",
    # 台湾
    "台北": "Asia/Taipei", "新北": "Asia/Taipei", "桃园": "Asia/Taipei", "台中": "Asia/Taipei",
    "台南": "Asia/Taipei", "高雄": "Asia/Taipei", "基隆": "Asia/Taipei", "新竹": "Asia/Taipei",
    "嘉义": "Asia/Taipei", "苗栗": "Asia/Taipei", "彰化": "Asia/Taipei", "南投": "Asia/Taipei",
    "云林": "Asia/Taipei", "屏东": "Asia/Taipei", "宜兰": "Asia/Taipei", "花莲": "Asia/Taipei",
    "台东": "Asia/Taipei", "澎湖": "Asia/Taipei", "金门": "Asia/Taipei", "马祖": "Asia/Taipei",
    "台湾": "Asia/Taipei",
}

# ----------------------------------------------------------------------
# 俄罗斯：11 个时区，2014 年起无夏令时。按联邦主体归属逐条收录（首府 + 主要城市）。
# ----------------------------------------------------------------------
_RUSSIA = {
    # UTC+2 Europe/Kaliningrad —— 加里宁格勒州
    "加里宁格勒": "Europe/Kaliningrad", "切尔尼亚霍夫斯克": "Europe/Kaliningrad",
    "波罗的斯克": "Europe/Kaliningrad", "泽列诺格拉茨克": "Europe/Kaliningrad",
    "苏维埃斯克": "Europe/Kaliningrad",
    # UTC+3 Europe/Moscow —— 欧俄大部分（含莫斯科、圣彼得堡两个联邦市）
    "俄罗斯": "Europe/Moscow", "莫斯科": "Europe/Moscow", "圣彼得堡": "Europe/Moscow",
    "下诺夫哥罗德": "Europe/Moscow", "喀山": "Europe/Moscow", "鞑靼斯坦": "Europe/Moscow",
    "希姆基": "Europe/Moscow", "沃洛格达": "Europe/Moscow",
    "大诺夫哥罗德": "Europe/Moscow", "普斯科夫": "Europe/Moscow",
    "顿河畔罗斯托夫": "Europe/Moscow", "克拉斯诺达尔": "Europe/Moscow", "索契": "Europe/Moscow",
    "沃罗涅日": "Europe/Moscow", "伏尔加格勒": "Europe/Volgograd", "雅罗斯拉夫尔": "Europe/Moscow",
    "伊万诺沃": "Europe/Moscow", "库尔斯克": "Europe/Moscow", "别尔哥罗德": "Europe/Moscow",
    "布良斯克": "Europe/Moscow", "奥廖尔": "Europe/Moscow", "坦波夫": "Europe/Moscow",
    "利佩茨克": "Europe/Moscow", "卡卢加": "Europe/Moscow", "图拉": "Europe/Moscow",
    "斯摩棱斯克": "Europe/Moscow", "特维尔": "Europe/Moscow", "弗拉基米尔": "Europe/Moscow",
    "科斯特罗马": "Europe/Moscow", "梁赞": "Europe/Moscow", "阿尔汉格尔斯克": "Europe/Moscow",
    "北德文斯克": "Europe/Moscow", "摩尔曼斯克": "Europe/Moscow", "彼得罗扎沃茨克": "Europe/Moscow",
    "卡累利阿": "Europe/Moscow", "瑟克特夫卡尔": "Europe/Moscow", "科米": "Europe/Moscow",
    "涅涅茨": "Europe/Moscow", "奔萨": "Europe/Moscow", "列宁格勒": "Europe/Moscow",
    "莫斯科州": "Europe/Moscow", "斯塔夫罗波尔": "Europe/Moscow", "马哈奇卡拉": "Europe/Moscow",
    "达吉斯坦": "Europe/Moscow", "格罗兹尼": "Europe/Moscow", "车臣": "Europe/Moscow",
    "纳兹兰": "Europe/Moscow", "印古什": "Europe/Moscow", "纳尔奇克": "Europe/Moscow",
    "弗拉季高加索": "Europe/Moscow", "北奥塞梯": "Europe/Moscow", "马加斯": "Europe/Moscow",
    "切尔克斯克": "Europe/Moscow", "卡拉恰伊": "Europe/Moscow", "五月镇": "Europe/Moscow",
    "阿迪格": "Europe/Moscow", "埃利斯塔": "Europe/Moscow", "卡尔梅克": "Europe/Moscow",
    "塞瓦斯托波尔": "Europe/Simferopol", "辛菲罗波尔": "Europe/Simferopol", "雅尔塔": "Europe/Simferopol",
    "克里米亚": "Europe/Simferopol", "费奥多西亚": "Europe/Simferopol", "克拉斯诺达尔边疆区": "Europe/Moscow",
    # UTC+3 Europe/Kirov —— 基洛夫州（偏移同莫斯科，IANA 单列）
    "基洛夫": "Europe/Kirov",
    # UTC+4 Europe/Samara —— 萨马拉、萨拉托夫、乌里扬诺夫斯克、阿斯特拉罕、乌德穆尔特
    "萨马拉": "Europe/Samara", "陶里亚蒂": "Europe/Samara", "锡兹兰": "Europe/Samara",
    "萨拉托夫": "Europe/Saratov", "恩格斯": "Europe/Saratov", "乌里扬诺夫斯克": "Europe/Ulyanovsk",
    "阿斯特拉罕": "Europe/Astrakhan", "阿赫图宾斯克": "Europe/Astrakhan", "伊热夫斯克": "Europe/Samara", "乌德穆尔特": "Europe/Samara",
    # UTC+5 Asia/Yekaterinburg —— 乌拉尔联邦区
    "叶卡捷琳堡": "Asia/Yekaterinburg", "下塔吉尔": "Asia/Yekaterinburg", "车里雅宾斯克": "Asia/Yekaterinburg",
    "马格尼托哥尔斯克": "Asia/Yekaterinburg", "兹拉托乌斯特": "Asia/Yekaterinburg",
    "彼尔姆": "Asia/Yekaterinburg", "克拉斯诺卡姆斯克": "Asia/Yekaterinburg",
    "乌法": "Asia/Yekaterinburg", "斯捷尔利塔马克": "Asia/Yekaterinburg", "巴什科尔托斯坦": "Asia/Yekaterinburg",
    "奥伦堡": "Asia/Yekaterinburg", "奥尔斯克": "Asia/Yekaterinburg", "秋明": "Asia/Yekaterinburg",
    "托博尔斯克": "Asia/Yekaterinburg", "库尔干": "Asia/Yekaterinburg",
    "汉特-曼西斯克": "Asia/Yekaterinburg", "苏尔古特": "Asia/Yekaterinburg",
    "下瓦尔托夫斯克": "Asia/Yekaterinburg", "新乌连戈伊": "Asia/Yekaterinburg",
    "萨列哈尔德": "Asia/Yekaterinburg", "亚马尔": "Asia/Yekaterinburg",
    # UTC+6 Asia/Omsk —— 鄂木斯克州
    "鄂木斯克": "Asia/Omsk",
    # UTC+7 —— 南西伯利亚（同偏移，IANA 按州分列）
    "新西伯利亚": "Asia/Novosibirsk", "贝尔茨科沃": "Asia/Novosibirsk",
    "巴尔瑙尔": "Asia/Barnaul", "比斯克": "Asia/Barnaul", "戈尔诺-阿尔泰斯克": "Asia/Barnaul",
    "阿尔泰": "Asia/Barnaul",
    "克拉斯诺亚尔斯克": "Asia/Krasnoyarsk", "阿钦斯克": "Asia/Krasnoyarsk",
    "诺里尔斯克": "Asia/Krasnoyarsk", "伊加尔卡": "Asia/Krasnoyarsk", "迪克森": "Asia/Krasnoyarsk",
    "阿巴坎": "Asia/Krasnoyarsk", "哈卡斯": "Asia/Krasnoyarsk", "克孜勒": "Asia/Krasnoyarsk",
    "图瓦": "Asia/Krasnoyarsk",
    "克麦罗沃": "Asia/Novokuznetsk", "新库兹涅茨克": "Asia/Novokuznetsk",
    "普罗科皮耶夫斯克": "Asia/Novokuznetsk", "别洛沃": "Asia/Novokuznetsk",
    "托木斯克": "Asia/Tomsk", "谢韦尔斯克": "Asia/Tomsk",
    # UTC+8 Asia/Irkutsk —— 伊尔库茨克州、布里亚特
    "伊尔库茨克": "Asia/Irkutsk", "安加尔斯克": "Asia/Irkutsk", "布拉茨克": "Asia/Irkutsk",
    "乌斯季-奥尔登斯基": "Asia/Irkutsk", "基廉斯克": "Asia/Irkutsk", "图伦": "Asia/Irkutsk",
    "乌兰乌德": "Asia/Irkutsk", "色楞格": "Asia/Irkutsk", "布里亚特": "Asia/Irkutsk",
    # UTC+9 Asia/Yakutsk / Asia/Chita —— 阿穆尔州、外贝加尔、萨哈西部
    # 奥伊米亚康以东的萨哈东部才是 Asia/Khandyga（IANA 自己的注释就是
    # “Tomponsky, Ust-Maysky”）：滕达/涅留恩格里/阿尔丹/奥廖克明斯克属 Asia/Yakutsk，
    # 恰拉属 Asia/Chita——旧表把它们归进 Khandyga 会让她们那边走快一小时。
    "雅库茨克": "Asia/Yakutsk", "阿尔丹": "Asia/Yakutsk", "奥廖克明斯克": "Asia/Yakutsk",
    "奥列克明斯克": "Asia/Yakutsk", "米尔内": "Asia/Yakutsk", "涅留恩格里": "Asia/Yakutsk",
    "汉德加": "Asia/Khandyga", "乌斯季-马亚": "Asia/Khandyga",
    "滕达": "Asia/Yakutsk", "恰拉": "Asia/Chita",
    "波克罗夫斯克": "Asia/Yakutsk", "萨哈": "Asia/Yakutsk",
    "布拉戈维申斯克": "Asia/Yakutsk", "海兰泡": "Asia/Yakutsk", "斯沃博德内": "Asia/Yakutsk",
    "结雅": "Asia/Yakutsk", "阿穆尔": "Asia/Yakutsk",
    "赤塔": "Asia/Chita", "莫戈恰": "Asia/Chita", "阿克沙": "Asia/Chita", "外贝加尔": "Asia/Chita",
    # UTC+10 Asia/Vladivostok / Asia/Ust-Nera —— 滨海、哈巴罗夫斯克、萨哈中部
    "海参崴": "Asia/Vladivostok", "符拉迪沃斯托克": "Asia/Vladivostok", "乌苏里斯克": "Asia/Vladivostok",
    "纳霍德卡": "Asia/Vladivostok", "阿尔乔姆": "Asia/Vladivostok", "哈巴罗夫斯克": "Asia/Vladivostok",
    "伯力": "Asia/Vladivostok", "阿穆尔河畔共青城": "Asia/Vladivostok", "比罗比詹": "Asia/Vladivostok",
    "犹太自治州": "Asia/Vladivostok", "滨海": "Asia/Vladivostok",
    "乌斯季-涅拉": "Asia/Ust-Nera", "托穆托尔": "Asia/Ust-Nera", "奥伊米亚康": "Asia/Ust-Nera",
    # UTC+11 Asia/Magadan / Asia/Sakhalin / Asia/Srednekolymsk —— 马加丹、萨哈林、萨哈东部
    "马加丹": "Asia/Magadan", "十月镇": "Asia/Magadan", "苏苏曼": "Asia/Magadan",
    "南萨哈林斯克": "Asia/Sakhalin", "萨哈林": "Asia/Sakhalin", "霍尔姆斯克": "Asia/Sakhalin",
    "科尔萨科夫": "Asia/Sakhalin", "波罗奈斯克": "Asia/Sakhalin", "诺格利基": "Asia/Sakhalin",
    "库里尔斯克": "Asia/Sakhalin", "中科雷马": "Asia/Srednekolymsk",
    # UTC+12 Asia/Kamchatka / Asia/Anadyr —— 堪察加、楚科奇
    "堪察加彼得罗巴甫洛夫斯克": "Asia/Kamchatka", "彼得罗巴甫洛夫斯克": "Asia/Kamchatka",
    "叶利佐沃": "Asia/Kamchatka", "堪察加": "Asia/Kamchatka", "楚科奇": "Asia/Kamchatka",
    "阿纳德尔": "Asia/Anadyr", "比利比诺": "Asia/Anadyr",
    "埃格维金诺特": "Asia/Anadyr", "普罗维杰尼亚": "Asia/Anadyr",
}

# ----------------------------------------------------------------------
# 日本：全国统一 UTC+9（Asia/Tokyo），冲绳亦然。47 都道府县厅所在地 + 主要市。
# ----------------------------------------------------------------------
_JAPAN_ZONE = "Asia/Tokyo"
_JAPAN = {
    "札幌": _JAPAN_ZONE, "函馆": _JAPAN_ZONE, "旭川": _JAPAN_ZONE, "小樽": _JAPAN_ZONE,
    "室兰": _JAPAN_ZONE, "带广": _JAPAN_ZONE, "钏路": _JAPAN_ZONE, "北见": _JAPAN_ZONE,
    "青森": _JAPAN_ZONE, "弘前": _JAPAN_ZONE, "盛冈": _JAPAN_ZONE, "一关": _JAPAN_ZONE,
    "仙台": _JAPAN_ZONE, "石卷": _JAPAN_ZONE, "秋田": _JAPAN_ZONE, "大馆": _JAPAN_ZONE,
    "山形": _JAPAN_ZONE, "米泽": _JAPAN_ZONE, "福岛": _JAPAN_ZONE, "会津若松": _JAPAN_ZONE,
    "水户": _JAPAN_ZONE, "日立": _JAPAN_ZONE, "宇都宫": _JAPAN_ZONE, "前桥": _JAPAN_ZONE,
    "太田": _JAPAN_ZONE, "川越": _JAPAN_ZONE, "熊谷": _JAPAN_ZONE, "千叶": _JAPAN_ZONE,
    "船桥": _JAPAN_ZONE, "柏": _JAPAN_ZONE, "市川": _JAPAN_ZONE, "东京": _JAPAN_ZONE,
    "新宿": _JAPAN_ZONE, "涩谷": _JAPAN_ZONE, "池袋": _JAPAN_ZONE, "横滨": _JAPAN_ZONE,
    "川崎": _JAPAN_ZONE, "相模原": _JAPAN_ZONE, "藤泽": _JAPAN_ZONE, "镰仓": _JAPAN_ZONE,
    "小田原": _JAPAN_ZONE, "新潟": _JAPAN_ZONE, "长冈": _JAPAN_ZONE, "富山": _JAPAN_ZONE,
    "金泽": _JAPAN_ZONE, "福井": _JAPAN_ZONE, "甲府": _JAPAN_ZONE, "长野": _JAPAN_ZONE,
    "松本": _JAPAN_ZONE, "上田": _JAPAN_ZONE, "岐阜": _JAPAN_ZONE, "静冈": _JAPAN_ZONE,
    "滨松": _JAPAN_ZONE, "沼津": _JAPAN_ZONE, "名古屋": _JAPAN_ZONE, "丰桥": _JAPAN_ZONE,
    "冈崎": _JAPAN_ZONE, "一宫": _JAPAN_ZONE, "津": _JAPAN_ZONE, "四日市": _JAPAN_ZONE,
    "大津": _JAPAN_ZONE, "京都": _JAPAN_ZONE, "宇治": _JAPAN_ZONE, "大阪": _JAPAN_ZONE,
    "堺": _JAPAN_ZONE, "东大阪": _JAPAN_ZONE, "神户": _JAPAN_ZONE, "姬路": _JAPAN_ZONE,
    "尼崎": _JAPAN_ZONE, "奈良": _JAPAN_ZONE, "和歌山": _JAPAN_ZONE, "鸟取": _JAPAN_ZONE,
    "米子": _JAPAN_ZONE, "松江": _JAPAN_ZONE, "冈山": _JAPAN_ZONE, "仓敷": _JAPAN_ZONE,
    "广岛": _JAPAN_ZONE, "福山": _JAPAN_ZONE, "山口": _JAPAN_ZONE, "下关": _JAPAN_ZONE,
    "德岛": _JAPAN_ZONE, "高松": _JAPAN_ZONE, "丸龟": _JAPAN_ZONE, "松山": _JAPAN_ZONE,
    "今治": _JAPAN_ZONE, "高知": _JAPAN_ZONE, "福冈": _JAPAN_ZONE, "北九州": _JAPAN_ZONE,
    "久留米": _JAPAN_ZONE, "佐贺": _JAPAN_ZONE, "长崎": _JAPAN_ZONE, "佐世保": _JAPAN_ZONE,
    "熊本": _JAPAN_ZONE, "大分": _JAPAN_ZONE, "宫崎": _JAPAN_ZONE, "鹿儿岛": _JAPAN_ZONE,
    "那霸": _JAPAN_ZONE, "冲绳": _JAPAN_ZONE, "宜野湾": _JAPAN_ZONE, "名护": _JAPAN_ZONE,
    "日本": _JAPAN_ZONE, "北海道": _JAPAN_ZONE, "本州": _JAPAN_ZONE, "九州": _JAPAN_ZONE,
    "四国": _JAPAN_ZONE, "关东": _JAPAN_ZONE, "关西": _JAPAN_ZONE, "东北": _JAPAN_ZONE,
}

# 直接填 IANA 名时，注入里该说人话：她说「你在雷克雅未克」而不是「你在 Atlantic/Reykjavik」。
# 省/自治区/直辖市/特别行政区：她会说自己住在「广东」而不是「广州」，所以高一级地名
# 也得认。中国大陆与港澳台均按法定区时（新疆、西藏亦走北京时间）。
_CHINA_PROVINCES = {
    "北京": "Asia/Shanghai", "天津": "Asia/Shanghai", "河北": "Asia/Shanghai",
    "山西": "Asia/Shanghai", "内蒙古": "Asia/Shanghai", "辽宁": "Asia/Shanghai",
    "吉林": "Asia/Shanghai", "黑龙江": "Asia/Shanghai", "上海": "Asia/Shanghai",
    "江苏": "Asia/Shanghai", "浙江": "Asia/Shanghai", "安徽": "Asia/Shanghai",
    "福建": "Asia/Shanghai", "江西": "Asia/Shanghai", "山东": "Asia/Shanghai",
    "河南": "Asia/Shanghai", "湖北": "Asia/Shanghai", "湖南": "Asia/Shanghai",
    "广东": "Asia/Shanghai", "广西": "Asia/Shanghai", "海南": "Asia/Shanghai",
    "重庆": "Asia/Shanghai", "四川": "Asia/Shanghai", "贵州": "Asia/Shanghai",
    "云南": "Asia/Shanghai", "西藏": "Asia/Shanghai", "陕西": "Asia/Shanghai",
    "甘肃": "Asia/Shanghai", "青海": "Asia/Shanghai", "宁夏": "Asia/Shanghai",
    "新疆": "Asia/Shanghai", "台湾": "Asia/Taipei", "香港": "Asia/Hong_Kong",
    "澳门": "Asia/Macau",
}

# 俄罗斯联邦主体名（与首府不同名的那些）：按首府所在时区。偏移自 2014 年起固定，无夏令时。
_RUSSIA_SUBJECTS = {
    "楚瓦什": "Europe/Moscow", "莫尔多瓦": "Europe/Moscow",
    "马里埃尔": "Europe/Moscow", "卡巴尔达-巴尔卡尔": "Europe/Moscow",
    "卡拉恰伊-切尔克斯": "Europe/Moscow", "北奥塞梯-阿兰": "Europe/Moscow",
    "汉特-曼西": "Asia/Yekaterinburg", "亚马尔-涅涅茨": "Asia/Yekaterinburg",
    "萨哈（雅库特）": "Asia/Yakutsk", "伊尔库茨克州": "Asia/Irkutsk",
}

# 日本 47 都道府县：全国唯一区时 UTC+9（包小笠原），所以整张表同一个值。
_JAPAN_PREFECTURES = {name: _JAPAN_ZONE for name in (
    "北海道", "青森", "岩手", "宫城", "秋田", "山形", "福岛", "茨城", "栃木", "群马",
    "埼玉", "千叶", "东京", "神奈川", "新潟", "富山", "石川", "福井", "山梨", "长野",
    "岐阜", "静冈", "爱知", "三重", "滋贺", "京都", "大阪", "兵库", "奈良", "和歌山",
    "鸟取", "岛根", "冈山", "广岛", "山口", "德岛", "香川", "爱媛", "高知", "福冈",
    "佐贺", "长崎", "熊本", "大分", "宫崎", "鹿儿岛", "冲绳",
)}

# 重名与常用译名补丁：
# * 松江既是上海的一个区，也是日本岛根县厅所在地（松江市）——裸名归日本那个，
#   中国这个用带后缀的写法认。
# * 迈科普是阿迪格共和国首府的标准中译，旧表里只有「五月镇」这个直译。
_EXTRA_ALIASES = {
    "松江区": "Asia/Shanghai", "上海松江": "Asia/Shanghai",
    "迈科普": "Europe/Moscow", "切博克萨雷": "Europe/Moscow",
    "约什卡尔奥拉": "Europe/Moscow", "萨兰斯克": "Europe/Moscow",
    "纳尔扬-马尔": "Europe/Moscow", "加特契纳": "Europe/Moscow",
    "亚速": "Europe/Moscow", "乌辛斯克": "Europe/Moscow",
}

IANA_DISPLAY_NAMES = {
    "Asia/Shanghai": "北京时间", "Asia/Urumqi": "乌鲁木齐（当地作息）", "Asia/Hong_Kong": "香港",
    "Asia/Macau": "澳门", "Asia/Taipei": "台北", "Asia/Tokyo": "东京", "Asia/Seoul": "首尔",
    "Asia/Singapore": "新加坡", "Asia/Kuala_Lumpur": "吉隆坡", "Asia/Bangkok": "曼谷",
    "Asia/Jakarta": "雅加达", "Asia/Manila": "马尼拉", "Asia/Ho_Chi_Minh": "胡志明市",
    "Asia/Hanoi": "河内", "Asia/Phnom_Penh": "金边", "Asia/Vientiane": "万象",
    "Asia/Yangon": "仰光", "Asia/Kathmandu": "加德满都", "Asia/Dhaka": "达卡",
    "Asia/Karachi": "卡拉奇", "Asia/Kolkata": "新德里", "Asia/Calcutta": "新德里",
    "Asia/Dubai": "迪拜", "Asia/Riyadh": "利雅得", "Asia/Tehran": "德黑兰",
    "Asia/Jerusalem": "耶路撒冷", "Asia/Istanbul": "伊斯坦布尔", "Asia/Almaty": "阿拉木图",
    "Asia/Bishkek": "比什凯克", "Asia/Tashkent": "塔什干", "Asia/Astana": "阿斯塔纳",
    # 俄罗斯 11 个时区
    "Europe/Kaliningrad": "加里宁格勒", "Europe/Moscow": "莫斯科", "Europe/Samara": "萨马拉",
    "Europe/Kirov": "基洛夫", "Asia/Yekaterinburg": "叶卡捷琳堡", "Asia/Omsk": "鄂木斯克",
    "Asia/Novosibirsk": "新西伯利亚", "Asia/Barnaul": "巴尔瑙尔", "Asia/Krasnoyarsk": "克拉斯诺亚尔斯克",
    "Asia/Novokuznetsk": "新库兹涅茨克", "Asia/Tomsk": "托木斯克", "Asia/Dushanbe": "杜尚别",
    "Asia/Irkutsk": "伊尔库茨克", "Asia/Yakutsk": "雅库茨克", "Asia/Chita": "赤塔",
    "Asia/Vladivostok": "海参崴", "Asia/Ust-Nera": "乌斯季-涅拉", "Asia/Magadan": "马加丹",
    "Asia/Sakhalin": "南萨哈林斯克", "Asia/Srednekolymsk": "中科雷马",
    "Asia/Kamchatka": "堪察加", "Asia/Anadyr": "阿纳德尔",
    # 欧洲 / 美洲 / 大洋洲 / 非洲常填的
    "Europe/London": "伦敦", "Europe/Dublin": "都柏林", "Europe/Lisbon": "里斯本",
    "Europe/Paris": "巴黎", "Europe/Berlin": "柏林", "Europe/Madrid": "马德里",
    "Europe/Rome": "罗马", "Europe/Amsterdam": "阿姆斯特丹", "Europe/Brussels": "布鲁塞尔",
    "Europe/Vienna": "维也纳", "Europe/Zurich": "苏黎世", "Europe/Stockholm": "斯德哥尔摩",
    "Europe/Oslo": "奥斯陆", "Europe/Copenhagen": "哥本哈根", "Europe/Helsinki": "赫尔辛基",
    "Europe/Warsaw": "华沙", "Europe/Prague": "布拉格", "Europe/Budapest": "布达佩斯",
    "Europe/Belgrade": "贝尔格莱德", "Europe/Athens": "雅典", "Europe/Bucharest": "布加勒斯特",
    "Europe/Sofia": "索非亚", "Europe/Kyiv": "基辅", "Europe/Kiev": "基辅",
    "Europe/Minsk": "明斯克", "Atlantic/Reykjavik": "雷克雅未克",
    "Etc/GMT": "UTC", "UTC": "UTC", "GMT": "UTC",
    "America/New_York": "纽约", "America/Chicago": "芝加哥", "America/Denver": "丹佛",
    "America/Los_Angeles": "洛杉矶", "America/Phoenix": "菲尼克斯", "America/Anchorage": "安克雷奇",
    "Pacific/Honolulu": "檀香山", "America/Toronto": "多伦多", "America/Vancouver": "温哥华",
    "America/Mexico_City": "墨西哥城", "America/Sao_Paulo": "圣保罗", "America/Buenos_Aires": "布宜诺斯艾利斯",
    "America/Bogota": "波哥大", "America/Lima": "利马", "America/Santiago": "圣地亚哥",
    "Australia/Sydney": "悉尼", "Australia/Melbourne": "墨尔本", "Australia/Brisbane": "布里斯班",
    "Australia/Perth": "珀斯", "Australia/Adelaide": "阿德莱德", "Pacific/Auckland": "奥克兰",
    "Pacific/Fiji": "斐济", "Africa/Cairo": "开罗", "Africa/Lagos": "拉各斯",
    "Africa/Johannesburg": "约翰内斯堡", "Africa/Nairobi": "内罗毕", "Africa/Casablanca": "卡萨布兰卡",
}

CITY_TO_TIMEZONE: dict[str, str] = {}
CITY_TO_TIMEZONE.update(_CHINA)
CITY_TO_TIMEZONE.update(_CHINA_PROVINCES)
CITY_TO_TIMEZONE.update(_RUSSIA)
CITY_TO_TIMEZONE.update(_RUSSIA_SUBJECTS)
CITY_TO_TIMEZONE.update(_JAPAN)
CITY_TO_TIMEZONE.update(_JAPAN_PREFECTURES)
CITY_TO_TIMEZONE.update(_EXTRA_ALIASES)


def _looks_like_zone_name(raw: str) -> bool:
    """`Asia/Shanghai`、`Etc/GMT+8` 这类 IANA 时区名直接允许填。"""
    if "/" not in raw:
        return False
    return bool(_ZONE_NAME_RE.match(raw))


# 用户写地名不会只写裸名：「广东省」「大阪市」「东京都」「延边朝鲜族自治州」都是同一个地方。
# 表里存的是裸名，所以这里按「先去掉行政后缀、再试前缀」的顺序认，只在前面都认不出时才降级匹配。
# 故意不收的两个后缀：「州」（广州/苏州本身就是键，剪掉会误伤）与「道」（北海道剪成北海
# 会匹到广西的北海市，跨国家错时区）。
_ADMIN_SUFFIXES = (
    "特别行政区", "自治区", "自治州", "地区", "盟", "市", "区", "县", "旗", "省", "府", "都",
)


def _candidates(raw: str):
    """一个地名可能对应的写法，按可信度从高到低。"""
    yield raw
    core = raw.split("（")[0].split("(")[0].strip()
    if core and core != raw:
        yield core
    hit_suffix = False
    for suffix in _ADMIN_SUFFIXES:
        if core.endswith(suffix) and len(core) > len(suffix) + 1:
            hit_suffix = True
            yield core[: -len(suffix)]
    if hit_suffix:
        # 「延边朝鲜族自治州」这类全名：剪到剩下的前缀本身是个收录过的地名才算。
        for cut in range(2, len(core) - 1):
            yield core[:cut]


def resolve_zone_name(city: str) -> str | None:
    """地名 → IANA 时区名。表里没有时，允许把配置项直接写成 IANA 名。

    加这一手是因为表再怎么铺也覆盖不完：填「河内」「曼谷」的人以前会静默退回宿主机
    时钟，她说自己在河内，过的却是机器所在时区的一天。
    """
    raw = (city or "").strip()
    if not raw or raw == DEFAULT_CITY_PLACEHOLDER:
        return None
    for key in _candidates(raw):
        name = CITY_TO_TIMEZONE.get(key)
        if name:
            return name
    return raw if _looks_like_zone_name(raw) else None


def lookup_timezone(city: str) -> str | None:
    return resolve_zone_name(city)


def display_city_name(city: str, zone_name: str | None) -> str:
    """注入与面板上给她看的地名。

    填的是 IANA 名时翻成中文城市名，否则她说「你在Atlantic/Reykjavik」——那不是人话。
    填的是「广东省」「大阪市」这类带后缀的写法时，给她看她自己写的那个名字。
    """
    raw = (city or "").strip()
    if raw in CITY_TO_TIMEZONE:
        return raw
    if zone_name:
        for key in _candidates(raw):
            if key in CITY_TO_TIMEZONE:
                return key
        return IANA_DISPLAY_NAMES.get(zone_name, raw)
    return raw


@dataclass(frozen=True, slots=True)
class CityTime:
    city: str
    display_city: str
    moment: datetime
    text: str
    weekday: str
    note: str = ""


def lookup_city_time(city: str) -> CityTime | None:
    # ✅ 延迟导入，打破循环依赖
    from ..clock import now_in_city, resolve_zone, system_timezone_city, weekday_cn, format_offset

    city = (city or "").strip()
    if not city:
        return None
    is_placeholder = city == DEFAULT_CITY_PLACEHOLDER
    _, note = resolve_zone(city)
    try:
        moment = now_in_city(city)
    except Exception:
        return None
    display = system_timezone_city() if is_placeholder else display_city_name(
        city, resolve_zone_name(city)
    )
    return CityTime(
        city=city,
        display_city=display,
        moment=moment,
        text=f"{moment.strftime('%Y-%m-%d %H:%M:%S')} ({format_offset(moment)})",
        weekday=weekday_cn(moment),
        note=note,
    )
