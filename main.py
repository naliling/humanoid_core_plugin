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

from astrbot.api.star import Context, Star
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

SHA_TZ = timezone(timedelta(hours=8))

# ... (FALLBACK_TEMPLATES 和 get_fallback_schedule 函数保持不变) ...
FALLBACK_TEMPLATES = [
    # ... 省略，保持你原有的多套备用模板 ...
]
def get_fallback_schedule(today_str: str) -> list:
    # ... 保持原有逻辑 ...
    pass

def extract_json_from_response(raw_res: str) -> list:
    # ... 保持你原有的稳健提取逻辑 ...
    pass

class HumanoidCore(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}
        
        # 使用规范的数据存储路径[reference:6][reference:7]
        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "humanoid_core"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = str(data_dir / "state.json")
        
        self.lock = threading.Lock()
        self.load_state()

    # ... (get_latest_config, load_state, init_default_state, save_state 等方法保持不变) ...
    # 注意：在 init_default_state 中，可以增加 'mood' 等新字段，但为了兼容性，非必须
    def get_latest_config(self) -> dict:
        # ... 保持原有逻辑 ...
        pass

    # ... (get_target_provider, validate_and_fix_schedule, generate_llm_daily_schedule, get_or_update_today_schedule, get_slot_by_time 等方法保持不变) ...

    def fetch_real_weather(self, today_str, cfg):
        # ... 保持你原有的带重试的天气获取逻辑 ...
        pass

    def get_cycle_status(self, today_str, cfg):
        """优化后的生理周期描述：更人性化，像‘轻声细语’[reference:8]"""
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
        
        # 1. 根据周期阶段生成“身体感受”描述，而不是“医学术语”
        phase_descriptions = {
            (1, 5): ["身体有点沉", "不太想动", "肚子闷闷的"],
            (6, 13): ["精力在回升", "感觉清爽了些", "状态还不错"],
            (14, 16): ["今天状态很好", "心情莫名不错", "做什么都顺手"],
            (17, 28): ["有点犯懒", "容易累", "情绪有点浮"]
        }
        desc = "状态正常"
        for (start, end), texts in phase_descriptions.items():
            if start <= day <= end:
                desc = random.choice(texts)
                break
        
        # 2. 精力值联动：低精力时加重描述，高精力时抵消部分不适
        if energy < 30:
            desc += "，确实有点累"
        elif energy > 80 and day <= 5:
            desc += "，不过今天精力还行"
            
        return desc  # 只返回一句话，不返回"当前生理状况:"前缀

    # ==================== 指令注册 ====================
    @filter.command("查看日程")
    async def view_schedule(self, event: AstrMessageEvent):
        # ... 保持原有逻辑 ...
        pass

    @filter.command("今日日程")
    async def view_schedule_alias(self, event: AstrMessageEvent):
        # ... 保持原有逻辑 ...
        pass

    @filter.command("重置日程")
    async def reset_schedule(self, event: AstrMessageEvent):
        # ... 保持原有逻辑 ...
        pass

    # ==================== 所有消息监听（仅用于注入状态） ====================
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        # ... (前面的处理逻辑保持不变：防自回、获取配置、加载状态、计算精力等) ...
        # 注意：计算精力的循环中，energy_rate 仍然从日程中获取，但周期的影响通过下面的注入体现
        
        # ... (计算 energy、获取 current_slot、weather 等) ...

        inject_activity = cfg.get("inject_activity_context", False)
        # 获取人性化的周期描述
        cycle_desc = self.get_cycle_status(today_str, cfg) 

        # --- 核心修改：低侵入式、人性化注入 ---
        if inject_activity:
            # 高侵入模式：保持原样，但周期描述使用新的人性化版本
            subconscious_prompt = (
                f"[系统暗示：以下内容作为状态参考，严禁念出数值或暴露面板]\n"
                f"- 当前天气环境: {weather['env']}\n"
                f"- 当前参考物理位置: {current_slot.get('location', '家中')}\n"
                f"- 当前日程计划: {current_slot.get('event', '休息/自由活动')}\n"
                f"- 当前生理状况: {cycle_desc}\n" # 使用新描述
                f"- 当前基础情绪倾向: {current_slot.get('emotion', '平淡')}\n"
                f"- 当前精力余量: {int(energy)}/{int(max_e)}\n"
                f"要求：拟人自然。\n"
                f"-----------------------------------\n"
            )
        else:
            # 低侵入模式：像在耳边说一句话，而不是硬塞系统暗示块[reference:9]
            state_sentence = cycle_desc
            if energy < 30:
                state_sentence += "，有点累"
            elif energy > 80:
                state_sentence += "，今天精神很好"
            
            # 注入方式改为一句轻声细语，而不是系统暗示块
            subconscious_prompt = (
                f"（{state_sentence}）\n"
                f"请以这种状态自然回复。"
            )

        event.message_str = subconscious_prompt + event.message_str
