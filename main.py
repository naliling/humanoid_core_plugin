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
from datetime import datetime, timezone, timedelta
from pathlib import Path

from astrbot.api.star import Context, Star
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

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

# ======================== JSON 提取 ========================
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

# ======================== 插件主类 ========================
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

    # ======================== 状态管理 ========================
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
            "_cached_location": ""
        }
        self.save_state_unsafe()

    def save_state_unsafe(self):
        with open(self.state_path, 'w', encoding='utf-8') as f:
            json.dump(self.state, f, ensure_ascii=False, indent=4)

    def save_state(self):
        with self.lock:
            self.save_state_unsafe()

    # ======================== 配置读取 ========================
    def get_latest_config(self) -> dict:
        active = {
            "max_energy": 100.0,
            "enable_cycle": True,
            "cycle_length": 28,
            "use_llm_schedule": True,
            "schedule_provider_name": "",
            "schedule_prompt_extra": "偏向普通的日常居家、工作与休闲生活，作息正常",
            "character_personality": "一位普通人，过着普通的日常生活",
            "admin_qq": [],
            "weather_enabled": True,
            "weather_api_key": "",
            "weather_location": "Zelenogradsk,RU",
            "weather_refresh_minutes": 60,
            "inject_activity_context": False
        }
        if isinstance(self.config, dict):
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

    # ======================== Provider 获取 ========================
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

    # ======================== 日程生成 ========================
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
        prompt = (
            f"请为{personality}生成今天的 24 小时生活日程规划。今天是 {today_str}。\n"
            f"额外偏好指导：{extra}\n"
            "格式要求：\n"
            "1. 必须只返回纯 JSON 字符串列表（格式为 JSON Array），严禁包含任何 Markdown 解释文本。\n"
            "2. 标准 JSON 结构示例：\n"
            "[\n"
            '  {"start": "00:00", "end": "07:30", "event": "睡眠休息", "location": "卧室", "emotion": "平静", "energy_rate": 0.15},\n'
            '  {"start": "07:30", "end": "08:00", "event": "起床洗漱", "location": "卫生间", "emotion": "清醒中", "energy_rate": 0.03}\n'
            "]\n"
            "3. 时间段必须连续覆盖 00:00 至 24:00。\n"
            "4. 粒度可灵活（30分钟、20分钟等）。\n"
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

    # ======================== 天气 ========================
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

    # ======================== 生理周期 ========================
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
        day = self.state.get("current_cycle_day", 1)
        energy = self.state.get("energy", 80.0)
        if energy < 30:
            note = "，精力较低"
        elif energy > 80:
            note = "，精力充沛"
        else:
            note = ""
        if 1 <= day <= 5:
            desc = f"处于【生理期/经期】，身体易冷伴微腹痛，情绪敏感{note}"
        elif 6 <= day <= 13:
            desc = f"处于【卵泡期】，身体舒适，精力回暖{note}"
        elif 14 <= day <= 16:
            desc = f"处于【排卵期】，无不适，精力充沛{note}"
        else:
            desc = f"处于【黄体期/经前期】，偶尔水肿，易犯懒疲倦{note}"
        return desc

    # ======================== 指令 ========================
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

    @filter.command("今日日程")
    async def view_schedule_alias(self, event: AstrMessageEvent):
        return await self.view_schedule(event)

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

    # ======================== 消息监听 ========================
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
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
        cfg = self.get_latest_config()
        self.load_state()

        schedule = await self.get_or_update_today_schedule(today_str, cfg)

        try:
            last_time = datetime.strptime(self.state.get("last_update", now.strftime("%Y-%m-%d %H:%M:%S")), "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHA_TZ)
        except:
            last_time = now

        delta_m = max(0, int((now - last_time).total_seconds() / 60))
        energy = self.state.get("energy", 80.0)
        sim = last_time
        for _ in range(delta_m):
            sim += timedelta(minutes=1)
            energy += self.get_slot_by_time(sim.strftime("%H:%M"), schedule).get("energy_rate", 0.0)
        max_e = float(cfg.get("max_energy", 100.0))
        energy = max(0.0, min(max_e, energy))

        current_slot = self.get_slot_by_time(now_time, schedule)
        weather = self.fetch_real_weather(today_str, cfg)
        cycle = self.get_cycle_status(today_str, cfg)

        with self.lock:
            self.state["energy"] = round(energy, 1)
            self.state["last_update"] = now.strftime("%Y-%m-%d %H:%M:%S")
            self.save_state_unsafe()

        if cfg.get("inject_activity_context", False):
            prompt = (
                f"[系统暗示：以下内容作为状态参考，严禁念出数值或暴露面板]\n"
                f"- 当前天气环境: {weather['env']}\n"
                f"- 当前参考物理位置: {current_slot.get('location', '家中')}\n"
                f"- 当前日程计划: {current_slot.get('event', '休息/自由活动')}\n"
                f"- 当前生理状况: {cycle}\n"
                f"- 当前基础情绪倾向: {current_slot.get('emotion', '平淡')}\n"
                f"- 当前精力余量: {int(energy)}/{int(max_e)} (若低于30表现得疲惫犯困；高于80充满活力)\n"
                f"要求：拟人自然。\n"
                f"-----------------------------------\n"
            )
        else:
            mood = "略带慵懒" if energy < 30 else "比较有活力" if energy > 80 else "状态正常"
            prompt = (
                f"[系统暗示：仅作为语气与情绪背景参考，严禁主动提及你正在做什么或在哪里，除非用户明确询问。]\n"
                f"- 当前精力状态: {mood} ({int(energy)}/{int(max_e)})\n"
                f"- 情绪倾向: {current_slot.get('emotion', '平淡')}\n"
                f"- 生理背景: {cycle}\n"
                f"- 天气: {weather['env']}\n"   # 低侵入模式也显示天气
                f"请以最自然的拟人方式闲聊，不要刻板念出状态。\n"
                f"-----------------------------------\n"
            )

        event.message_str = prompt + event.message_str
