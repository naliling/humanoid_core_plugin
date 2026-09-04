"""注入文本构建器：为单个角色构建注入大模型的上下文。"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .core_instance import HumanoidCoreInstance

from .config import HumanoidConfig


class PromptBuilder:
    def __init__(self, core_instance: HumanoidCoreInstance):
        self._core = core_instance

    @property
    def config(self) -> HumanoidConfig:
        return self._core.config

    def build(self, user_id: str, is_group: bool = False) -> str:
        parts = []

        # 1. 时间与环境信息（system-reminder 格式，英文系统暗示）
        parts.append(self._build_time_reminder())

        # 2. 环境感知（中文，柔和）
        if self.config.enable_chat_awareness:
            parts.append(f"【环境】{'群聊' if is_group else '私聊'}中。")

        # 3. 自身状态（根据注入模式）
        parts.append(self._build_self_state())

        # 4. 过程简述
        parts.append(self._build_process())

        # 5. 情绪（仅标签，除非 full 模式带数值）
        mood_allowed = self.config.mood_enabled and (not is_group or self.config.mood_enabled_in_group)
        if mood_allowed:
            parts.append(self._build_mood(user_id, detailed=(self.config.inject_activity_context == "full")))

        # 6. 行为指令（昵称、夜间、社交能量）
        parts.append(self._build_behavior_instructions(user_id))

        # 7. 对话间隔（拟人化氛围描述）
        interval = self._build_interval_note(user_id)
        if interval:
            parts.append(interval)

        # 过滤空行后拼接
        return "\n".join(part for part in parts if part)

    # ========== 工具方法 ==========

    def _get_time_of_day(self, hour: int) -> str:
        if 5 <= hour < 8:
            return "early morning"
        elif 8 <= hour < 12:
            return "morning"
        elif 12 <= hour < 14:
            return "noon"
        elif 14 <= hour < 18:
            return "afternoon"
        elif 18 <= hour < 21:
            return "evening"
        elif 21 <= hour < 24:
            return "night"
        else:
            return "late night"

    def _get_time_of_day_cn(self, hour: int) -> str:
        if 5 <= hour < 8:
            return "清晨"
        elif 8 <= hour < 12:
            return "上午"
        elif 12 <= hour < 14:
            return "中午"
        elif 14 <= hour < 18:
            return "下午"
        elif 18 <= hour < 21:
            return "傍晚"
        elif 21 <= hour < 24:
            return "晚上"
        else:
            return "深夜"

    # ========== 核心构建方法 ==========

    def _build_time_reminder(self) -> str:
        """官方推荐格式：<system-reminder> + 英文系统暗示 + 知而不言指令"""
        now = self._core.clock.now()
        tz_name = self._core.clock.city
        iso_time = now.isoformat(timespec="seconds")
        weekday_en = now.strftime("%A")
        date_en = now.strftime("%B %d, %Y")
        hour = now.hour
        time_of_day = self._get_time_of_day(hour)
        time_str = now.strftime("%H:%M")
        time_of_day_cn = self._get_time_of_day_cn(hour)

        return (
            "<system-reminder>\n"
            f"Current date and time: {iso_time} ({tz_name})\n"
            f"Today is {weekday_en}, {date_en}.\n"
            f"Time of day: {time_of_day} ({time_str})\n"
            "\n"
            "This is contextual information to help you understand the current temporal environment.\n"
            "Use it naturally in conversation when relevant—for example, to greet appropriately,\n"
            "understand time-related references, or answer if asked about the time.\n"
            "Do not explicitly announce or repeat these values unless directly asked.\n"
            "</system-reminder>"
        )

    def _build_self_state(self) -> str:
        cfg = self.config
        mode = cfg.inject_activity_context
        snap = self._core.snapshot()
        now = self._core.clock.now()
        hour = now.hour

        time_of_day_cn = self._get_time_of_day_cn(hour)
        city = snap['city']
        weekday_cn = f"星期{snap['weekday']}"
        date_str = snap['today']

        if mode == "full":
            lines = [
                f"日期：{date_str} {weekday_cn}",
                f"城市：{city}",
                f"时段：{time_of_day_cn}",
                f"精力：{snap['energy']['text']} ({int(snap['energy']['value'])}/{int(snap['energy']['max'])})",
                f"生理：{snap['cycle'] or '正常'}",
                f"天气：{snap['weather'].get('env', '未知')}"
            ]
        elif mode == "mood_only":
            lines = [
                f"日期：{date_str}",
                f"精力：{snap['energy']['text']}"
            ]
        else:  # low
            lines = [
                f"日期：{date_str} {weekday_cn}",
                f"城市：{city}",
                f"精力：{snap['energy']['text']}",
                f"生理背景：{snap['cycle'] or '正常'}"
            ]
            if cfg.show_city_time_in_low_intrusion:
                lines.append(f"时段：{time_of_day_cn}")
            if snap['weather']:
                lines.append(f"天气：{snap['weather'].get('env', '未知')}")

        return "\n".join(lines)

    def _build_process(self) -> str:
        proc = self._core.process.current()
        name = proc.get("name", "休息")
        if self.config.inject_activity_context == "full":
            start_str = proc.get("started_at")
            if start_str:
                try:
                    start = datetime.fromisoformat(start_str)
                    now = self._core.clock.now()
                    elapsed = int((now - start).total_seconds() // 60)
                    return f"【当前过程】正在{name}（已持续约 {elapsed} 分钟）"
                except ValueError:
                    pass
        return f"【当前过程】正在{name}"

    def _build_mood(self, user_id: str, detailed: bool) -> str:
        data = self._core.mood.profile(user_id)
        label = self._core.mood.label(user_id)
        if detailed:
            return (
                f"【关系状态】\n"
                f"好感度：{data['affection']:.1f}/100\n"
                f"亲近欲：{data['libido']:.1f}/50\n"
                f"情绪标签：{label}"
            )
        else:
            return f"【关系状态】当前情绪标签：{label}"

    def _build_behavior_instructions(self, user_id: str) -> str:
        instructions = []

        nickname = self._core.mood.nickname(user_id)
        if nickname:
            instructions.append(f"【重要指令】用户的昵称是「{nickname}」，请用此称呼。")

        night = self._build_night_instruction()
        if night:
            instructions.append(night)

        social = self._build_social_instruction()
        if social:
            instructions.append(social)

        return "\n".join(instructions) if instructions else ""

    def _build_night_instruction(self) -> str:
        clock = self._core.clock
        cfg = self.config
        if not cfg.night_mode_enabled or not clock.is_night():
            return ""
        force = cfg.night_mode_force_sleep
        if clock.is_deep_sleep():
            if force:
                return "【重要指令】深度睡眠时段，请仅回复一句简短的话并建议稍后再聊。"
            else:
                return "【状态】深度睡眠时段，你意识模糊，如有回复应极简短（≤20字）。"
        else:
            if force:
                return "【重要指令】浅睡时段，请简短回应并暗示想睡了（≤30字）。"
            else:
                return "【状态】浅睡时段，你有些迷糊，回复应简短（≤30字）。"

    def _build_social_instruction(self) -> str:
        value = self._core.social.value
        if value > 70:
            return ""
        elif value > 40:
            return "【状态】社交能量一般，可保持正常交流长度。"
        else:
            return "【重要指令】社交能量较低，请尽量用简洁的句子回应。"

    def _build_interval_note(self, user_id: str) -> str:
        cfg = self.config
        threshold = cfg.last_interaction_threshold_minutes * 60
        pending = getattr(self._core, "_pending_interval", None)
        if not pending:
            return ""
        elapsed = pending["elapsed"]
        if elapsed < threshold:
            return ""

        mood_data = self._core.mood.profile(user_id)
        affection = mood_data.get("affection", 50)

        if elapsed < 1800:
            vibe = "仿佛刚才还在聊，余音未散" if affection > 65 else "短暂的间隙，刚好够喝杯茶" if affection > 35 else "安静的片刻，互不打扰"
        elif elapsed < 10800:
            vibe = "心里空了一小块，总觉得少了点什么" if affection > 65 else "不知不觉，午后已过半" if affection > 35 else "各自忙碌，相安无事"
        elif elapsed < 86400:
            vibe = "像是隔了一个黄昏，思念在悄悄发酵" if affection > 65 else "一天的光阴，就这么平静地流过" if affection > 35 else "久违的沉默，界限更清晰"
        else:
            vibe = "像隔了一个季节，这份想念变得清晰" if affection > 65 else "时光流转，生活依旧平淡" if affection > 35 else "长久的疏离，各自安好"

        last_msg_line = ""
        if cfg.last_interaction_mode == "with_last_msg":
            last_msg = self._core._scope.get_user(user_id, "last_message")
            if last_msg and last_msg.get("text"):
                last_msg_line = f"上次分别时，你留下了一句话：「{last_msg['text']}」。"
                self._core._scope.set_user(user_id, "last_message", None)

        lines = [
            "【时间感知】",
            vibe + "。",
            last_msg_line,
            "让这份时间带来的自然情绪微妙地渗透进你的语气中。",
            "严禁直接提及'过了多少分钟/小时/天'这种具体时间数据，用人类直觉去流露。"
        ]
        return "\n".join(line for line in lines if line)
