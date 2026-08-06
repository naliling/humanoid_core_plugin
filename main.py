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

from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

SHA_TZ = timezone(timedelta(hours=8))
logger = logging.getLogger("AstrBot")

# 多套备用日程模板（基于日期哈希轮换）
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
    """根据日期哈希选择备用模板，保证同一天所有用户一致"""
    seed = int(hashlib.md5(today_str.encode()).hexdigest()[:8], 16)
    idx = seed % len(FALLBACK_TEMPLATES)
    return FALLBACK_TEMPLATES[idx]


# 移除 @register 装饰器，AstrBot 会自动识别继承自 Star 的类
class HumanoidCore(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}
        
        # 使用规范的数据存储路径
        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "humanoid_core"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = str(data_dir / "state.json")
        
        self.lock = threading.Lock()
        self.load_state()

    def get_latest_config(self) -> dict:
        active_config = {
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
            "weather_refresh_minutes": 60
        }
        if isinstance(self.config, dict):
            active_config.update(self.config)

        if hasattr(self, "context") and self.context:
            for getter in ["get_config", "get_plugin_config"]:
                if hasattr(self.context, getter) and callable(getattr(self.context, getter)):
                    try:
                        res = getattr(self.context, getter)()
                        if isinstance(res, dict) and res:
                            active_config.update(res)
                    except Exception:
                        pass
        return active_config

    def load_state(self):
        with self.lock:
            if os.path.exists(self.state_path):
                try:
                    with open(self.state_path, "r", encoding="utf-8") as f:
                        self.state = json.load(f)
                except Exception:
                    self.init_default_state()
            else:
                self.init_default_state()

    def init_default_state(self):
        now_today = datetime.now(SHA_TZ).strftime("%Y-%m-%d")
        seed_date = datetime.now(SHA_TZ).strftime("%Y%m%d")
        seed_hash = int(hashlib.md5(seed_date.encode()).hexdigest()[:8], 16)
        self.state = {
            "energy": 80.0,
            "current_cycle_day": (seed_hash % 28) + 1,
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
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=4)

    def save_state(self):
        with self.lock:
            self.save_state_unsafe()

    def get_target_provider(self, cfg: dict):
        target_name = str(cfg.get("schedule_provider_name", "")).strip()
        provider = None
        if target_name and hasattr(self.context, "get_provider"):
            try:
                provider = self.context.get_provider(target_name)
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
        current_time = "00:00"
        for slot in schedule:
            if slot.get("start", "") != current_time:
                fixed.append({
                    "start": current_time,
                    "end": slot.get("start", "24:00"),
                    "event": "自由活动/休息",
                    "location": "家中",
                    "emotion": "随意",
                    "energy_rate": 0.0
                })
            fixed.append(slot)
            current_time = slot.get("end", "24:00")
        if current_time != "24:00":
            fixed.append({
                "start": current_time,
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
        personality = cfg.get("character_personality", "一位普通人，过着普通的日常生活")
        extra_prompt = cfg.get("schedule_prompt_extra", "偏向普通的日常居家、工作与休闲生活，作息正常")
        base_prompt = (
            f"请为{personality}生成今天的 24 小时生活日程规划。今天是 {today_str}。\n"
            f"额外偏好指导：{extra_prompt}\n"
            "格式要求：\n"
            "1. 必须只返回纯 JSON 字符串列表（格式为 JSON Array），严禁包含任何 Markdown 解释文本。\n"
            "2. 标准 JSON 结构示例：\n"
            "[\n"
            '  {"start": "00:00", "end": "07:30", "event": "睡眠休息", "location": "卧室", "emotion": "平静", "energy_rate": 0.15},\n'
            '  {"start": "07:30", "end": "08:00", "event": "起床洗漱", "location": "卫生间", "emotion": "清醒中", "energy_rate": 0.03},\n'
            '  {"start": "08:00", "end": "08:30", "event": "早餐", "location": "餐厅", "emotion": "轻松", "energy_rate": 0.05}\n'
            "]\n"
            "3. 时间段 start 和 end 必须连续且完美覆盖 00:00 至 24:00。\n"
            "4. 每个时间段的长度可以灵活设置，不一定要整小时，可以是 1小时、30分钟、20分钟、15分钟等，更贴近真实生活（如洗澡20分钟、通勤15分钟）。\n"
            "5. energy_rate 控制精力：休息/睡觉为正数(0.05~0.2)，日常活动为小数值(-0.05~0.05)，工作学习为负数(-0.05~-0.15)。\n"
            "6. 地点变化请考虑合理的通勤时间（例如家→公司至少间隔15分钟）。"
        )

        for attempt in range(3):
            try:
                provider = self.get_target_provider(cfg)
                if not provider:
                    logger.warning(f"[humanoid_core] 无可用 Provider，尝试 {attempt+1}/3")
                    if attempt == 2:
                        break
                    continue

                prompt = base_prompt
                if attempt > 0:
                    prompt += "\n\n【重要】上次返回的JSON格式有误，请确保只返回纯JSON数组，不要包含任何额外文字。"

                logger.info(f"[humanoid_core] 正在调用模型生成今日({today_str})动态日程... (尝试 {attempt+1}/3)")
                try:
                    response = await asyncio.wait_for(
                        provider.text_chat(prompt),
                        timeout=60.0
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"[humanoid_core] 模型调用超时 (尝试 {attempt+1}/3)")
                    if attempt == 2:
                        break
                    continue
                except TypeError:
                    response = await asyncio.wait_for(
                        provider.text_chat(prompt=prompt),
                        timeout=60.0
                    )
                
                raw_res = response.completion_text if hasattr(response, "completion_text") else (response.get_first_text() if hasattr(response, "get_first_text") else str(response))
                json_pattern = r'(\[\s*\{.*?\}\s*\])'
                match = re.search(json_pattern, raw_res, re.DOTALL)
                if match:
                    try:
                        parsed = json.loads(match.group(1))
                        if isinstance(parsed, list) and len(parsed) > 0:
                            logger.info(f"[humanoid_core] 🎉 大模型日程生成成功！共 {len(parsed)} 个时段。")
                            return self.validate_and_fix_schedule(parsed)
                    except Exception as e:
                        logger.warning(f"[humanoid_core] JSON 解析失败 (正则匹配): {str(e)}")
                start_idx = raw_res.find("[")
                end_idx = raw_res.rfind("]")
                if start_idx != -1 and end_idx != -1:
                    try:
                        parsed = json.loads(raw_res[start_idx:end_idx+1])
                        if isinstance(parsed, list) and len(parsed) > 0:
                            logger.info(f"[humanoid_core] 🎉 大模型日程生成成功！共 {len(parsed)} 个时段。")
                            return self.validate_and_fix_schedule(parsed)
                    except Exception as e:
                        logger.warning(f"[humanoid_core] JSON 解析失败 (备选): {str(e)}")
            except Exception as e:
                logger.warning(f"[humanoid_core] 日程生成尝试 {attempt+1} 失败: {str(e)}")
                if attempt == 2:
                    logger.warning(f"[humanoid_core] 所有重试失败，已自动切回备用日程。")
        return get_fallback_schedule(today_str)

    async def get_or_update_today_schedule(self, today_str: str, cfg: dict) -> list:
        if not cfg.get("use_llm_schedule", True):
            return get_fallback_schedule(today_str)
        saved_date = self.state.get("today_date", "")
        saved_schedule = self.state.get("daily_schedule", [])
        if saved_date != today_str or not saved_schedule:
            new_schedule = await self.generate_llm_daily_schedule(today_str, cfg)
            with self.lock:
                self.state["today_date"] = today_str
                self.state["daily_schedule"] = new_schedule
                self.save_state_unsafe()
            return new_schedule
        return saved_schedule

    def get_slot_by_time(self, time_str: str, schedule_list: list) -> dict:
        for slot in schedule_list:
            if slot.get("start", "00:00") <= time_str <= slot.get("end", "24:00"):
                return slot
        return {"event": "休息/自由活动", "location": "卧室/家中", "emotion": "平淡", "energy_rate": 0.0}

    def fetch_real_weather(self, today_str, cfg):
        w_enabled = cfg.get("weather_enabled", True)
        api_key = str(cfg.get("weather_api_key", "")).strip()
        location = str(cfg.get("weather_location", "Zelenogradsk,RU")).strip()
        interval_mins = int(cfg.get("weather_refresh_minutes", 60))
        fallback = {"weather": "晴朗 ☀️", "env": f"当前所在城市 [{location}]（未填有效API Key或关闭，按晴朗处理）"}
        if not w_enabled or not api_key or len(api_key) < 10:
            return fallback
        now = datetime.now(SHA_TZ)
        if self.state.get("_cached_location") == location and self.state.get("_cached_weather_obj"):
            try:
                lt = datetime.strptime(self.state.get("_last_weather_fetch", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHA_TZ)
                if (now - lt).total_seconds() < interval_mins * 60:
                    return self.state["_cached_weather_obj"]
            except Exception:
                pass
        try:
            params = {"q": location, "appid": api_key, "units": "metric", "lang": "zh_cn"}
            url = f"https://api.openweathermap.org/data/2.5/weather?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(url, headers={'User-Agent': 'AstrBot'})
            with urllib.request.urlopen(req, timeout=5) as res:
                data = json.loads(res.read().decode('utf-8'))
                desc, temp, hum = data['weather'][0]['description'], data['main']['temp'], data['main']['humidity']
                obj = {"weather": f"{desc} 🌡️ {temp}°C", "env": f"当前城市 [{location}] 天气：{desc}，气温 {temp}℃，湿度 {hum}%"}
                self.state["_cached_weather_obj"] = obj
                self.state["_last_weather_fetch"] = now.strftime("%Y-%m-%d %H:%M:%S")
                self.state["_cached_location"] = location
                self.save_state_unsafe()
                return obj
        except Exception:
            return self.state.get("_cached_weather_obj") or fallback

    def get_cycle_status(self, today_str, cfg):
        if not cfg.get("enable_cycle", True):
            return ""
        last_date = self.state.get("last_cycle_update", today_str)
        if last_date != today_str:
            try:
                diff = (datetime.strptime(today_str, "%Y-%m-%d") - datetime.strptime(last_date, "%Y-%m-%d")).days
                if diff > 0:
                    with self.lock:
                        length = int(cfg.get("cycle_length", 28))
                        self.state["current_cycle_day"] = ((int(self.state.get("current_cycle_day", 1)) - 1 + diff) % length) + 1
                        self.state["last_cycle_update"] = today_str
                        self.save_state_unsafe()
            except Exception:
                pass
        day = int(self.state.get("current_cycle_day", 1))
        energy = float(self.state.get("energy", 80.0))
        fatigue_suffix = "，此时精力偏低，更显疲惫" if energy < 30 else "，精力充沛" if energy > 80 else ""
        if 1 <= day <= 5:
            desc = f"处于【生理期/经期】，身体易冷伴微腹痛，疲惫情绪敏感{fatigue_suffix}"
        elif 6 <= day <= 13:
            desc = f"处于【卵泡期】，身体舒适，精力回暖{fatigue_suffix}"
        elif 14 <= day <= 16:
            desc = f"处于【排卵期】，无不适，精力充沛{fatigue_suffix}"
        else:
            desc = f"处于【黄体期/经前期】，偶尔水肿，易犯懒疲倦{fatigue_suffix}"
        return f"- 当前生理状况: {desc}\n"

    # ==================== 指令注册 ====================
    @filter.command("查看日程")
    async def view_schedule(self, event: AstrMessageEvent):
        """查看今日完整日程（发送"查看日程"即可）"""
        now = datetime.now(SHA_TZ)
        today_str = now.strftime("%Y-%m-%d")
        cfg = self.get_latest_config()
        self.load_state()
        schedule = await self.get_or_update_today_schedule(today_str, cfg)
        lines = [f"📅 {today_str} 日程表："]
        for slot in schedule:
            lines.append(
                f"{slot.get('start', '')} - {slot.get('end', '')}  "
                f"【{slot.get('event', '')}】"
                f"@{slot.get('location', '')} "
                f"({slot.get('emotion', '')})"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("今日日程")
    async def view_schedule_alias(self, event: AstrMessageEvent):
        """查看今日完整日程（别名）"""
        return await self.view_schedule(event)

    @filter.command("重置日程")
    async def reset_schedule(self, event: AstrMessageEvent):
        """强制重新生成今日日程（仅管理员可用）"""
        cfg = self.get_latest_config()
        admin_qq = cfg.get("admin_qq", [])
        admin_list = [str(a).strip() for a in admin_qq]
        sender_id = str(event.get_sender_id())
        
        if sender_id not in admin_list:
            yield event.plain_result("❌ 权限不足，仅管理员可重置日程。")
            return

        now = datetime.now(SHA_TZ)
        today_str = now.strftime("%Y-%m-%d")
        # 先重新生成，成功后再写入，避免状态不一致
        new_schedule = await self.generate_llm_daily_schedule(today_str, cfg)
        with self.lock:
            self.state["today_date"] = today_str
            self.state["daily_schedule"] = new_schedule
            self.save_state_unsafe()
        yield event.plain_result(f"✅ 已重置今日日程（{today_str}），共 {len(new_schedule)} 个时段。")

    # ==================== 所有消息监听（仅用于注入状态） ====================
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        try:
            if hasattr(event, "get_sender_id") and hasattr(event, "get_self_id"):
                if str(event.get_sender_id()) == str(event.get_self_id()):
                    return
        except Exception:
            pass

        if not hasattr(event, "message_str") or not event.message_str:
            return
        raw_text = event.message_str.strip()

        if "[系统暗示：" in raw_text or raw_text.startswith(("!", ".", "！", "#")) or not raw_text:
            return

        now = datetime.now(SHA_TZ)
        today_str, now_time_str = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
        cfg = self.get_latest_config()
        self.load_state()

        today_schedule = await self.get_or_update_today_schedule(today_str, cfg)
        
        try:
            last_time = datetime.strptime(self.state.get("last_update", now.strftime("%Y-%m-%d %H:%M:%S")), "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHA_TZ)
        except ValueError:
            last_time = now

        delta_m = max(0, int((now - last_time).total_seconds() / 60))
        energy = float(self.state.get("energy", 80.0))
        sim_time = last_time
        for _ in range(delta_m):
            sim_time += timedelta(minutes=1)
            energy += self.get_slot_by_time(sim_time.strftime("%H:%M"), today_schedule).get("energy_rate", 0.0)

        max_e = float(cfg.get("max_energy", 100.0))
        energy = max(0.0, min(max_e, energy))
        
        current_slot = self.get_slot_by_time(now_time_str, today_schedule)
        weather = self.fetch_real_weather(today_str, cfg)
        cycle_txt = self.get_cycle_status(today_str, cfg)

        with self.lock:
            self.state["energy"] = round(energy, 1)
            self.state["last_update"] = now.strftime("%Y-%m-%d %H:%M:%S")
            self.save_state_unsafe()

        subconscious_prompt = (
            f"[系统暗示：以下内容作为状态参考，严禁念出数值或暴露面板]\n"
            f"- 当前天气环境: {weather['env']}\n"
            f"- 当前参考物理位置: {current_slot.get('location', '家中')}\n"
            f"- 当前日程计划: {current_slot.get('event', '休息/自由活动')}\n"
            f"{cycle_txt}"
            f"- 当前基础情绪倾向: {current_slot.get('emotion', '平淡')}\n"
            f"- 当前精力余量: {int(energy)}/{int(max_e)} (若低于30表现得疲惫犯困；高于80充满活力)\n"
            f"要求：拟人自然。\n"
            f"-----------------------------------\n"
        )

        event.message_str = subconscious_prompt + event.message_str