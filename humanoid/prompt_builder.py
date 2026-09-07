"""注入文本构建器：为单个角色构建注入大模型的上下文。"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Optional, List, Dict, Any

if TYPE_CHECKING:
    from .core_instance import HumanoidCoreInstance

from .config import HumanoidConfig


class PromptBuilder:
    def __init__(self, core_instance: HumanoidCoreInstance):
        self._core = core_instance

    @property
    def config(self) -> HumanoidConfig:
        return self._core.config

    def build(
        self,
        user_id: str,
        is_group: bool = False,
        events: Optional[List[Dict[str, Any]]] = None,
        agency: Optional[Dict[str, float]] = None,
    ) -> str:
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

        # 7. 短期行为上下文：事件、当前注意力、行为倾向。
        behavior_context = self._build_behavior_context(events or [], agency or {})
        if behavior_context:
            parts.append(behavior_context)

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
        snap = self._core.snapshot(refresh=False)
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
        """把过程作为生活背景注入，明确禁止把它变成发言锁。"""
        proc = self._core.process.current()
        name = str(proc.get("name", "休息"))
        phase = str(proc.get("phase", "") or "")
        style = str(proc.get("style", "") or "")

        start_str = proc.get("started_at")
        elapsed_text = ""
        if self.config.inject_activity_context == "full" and start_str:
            try:
                start = datetime.fromisoformat(start_str)
                now = self._core.clock.now()
                elapsed = max(0, int((now - start).total_seconds() // 60))
                elapsed_text = f"（已持续约 {elapsed} 分钟）"
            except (TypeError, ValueError):
                pass

        lines = [f"【生活过程】{name}{elapsed_text}"]
        if phase and phase != name:
            lines.append(f"当前阶段：{phase}")
        if style:
            lines.append(f"过程风格：{style}")

        lines.append(
            "过程说明：这是角色当前生活中的背景活动，不是正在执行的强制任务。"
            "收到用户消息时，可以随时暂停、改变、结束或暂时忽略当前过程，"
            "不得因为当前过程而拒绝用户、禁止换话题、要求用户等待或声称必须把过程做完。"
            "除非对话本身涉及该活动，否则无需主动提及过程。"
        )
        return "\n".join(lines)

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

    def _format_gap(self, seconds: Optional[float]) -> str:
        if seconds is None:
            return "刚开始"
        seconds = max(0.0, float(seconds))
        if seconds < 60:
            return "不到一分钟"
        if seconds < 3600:
            minutes = max(1, int(seconds // 60))
            return f"约 {minutes} 分钟"
        if seconds < 86400:
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            if minutes >= 45:
                return f"约 {hours + 1} 小时"
            if minutes >= 15:
                return f"约 {hours} 小时 {minutes // 15 * 15} 分钟"
            return f"约 {hours} 小时"
        days = max(1, int(seconds // 86400))
        return f"约 {days} 天"

    @staticmethod
    def _agency_level(value: float) -> str:
        value = max(0.0, min(1.0, float(value)))
        if value >= 0.78:
            return "明显偏高"
        if value >= 0.55:
            return "偏高"
        if value >= 0.35:
            return "平稳"
        if value >= 0.20:
            return "偏低"
        return "很低"

    def _build_behavior_context(
        self, events: List[Dict[str, Any]], agency: Dict[str, float]
    ) -> str:
        if not events and not agency:
            return ""

        parts = []
        if events:
            recent = []
            for event in events:
                event_type = event.get("type")
                data = event.get("data") or {}
                gap = data.get("gap_seconds")
                if event_type == "conversation_started":
                    recent.append("刚开始与这位用户交流，这是初次接触。")
                elif event_type == "conversation_resumed":
                    recent.append(f"对话中断后重新接上，间隔{self._format_gap(gap)}。")
                elif event_type == "user_returned":
                    recent.append(f"用户重新出现，距离上次消息已有{self._format_gap(gap)}。")
                elif event_type == "long_gap":
                    recent.append(f"用户在较长时间后重新出现，距离上次消息已有{self._format_gap(gap)}。")

                previous = data.get("previous_message")
                if previous:
                    recent.append(f"离开前用户最后说过：{previous}")

            if recent:
                parts.append("【最近发生】\n" + "\n".join(f"- {line}" for line in recent))

            top = events[0]
            top_type = top.get("type")
            if top_type in {"user_returned", "long_gap"}:
                focus = "用户重新出现这件事目前较受关注，但不代表必须询问原因。"
            elif top_type == "conversation_resumed":
                focus = "对话刚重新接上，优先保持当前交流的自然连续性。"
            else:
                focus = "当前重点是自然建立这次交流，不必刻意强调内部状态。"
            parts.append("【当前关注】" + focus)

        if agency:
            labels = (
                ("主动性", "initiative"),
                ("好奇心", "curiosity"),
                ("关心程度", "care"),
                ("社交意愿", "social_willingness"),
                ("延续当前交流", "continuation"),
            )
            lines = ["【当前行为倾向】"]
            for label, key in labels:
                lines.append(f"- {label}：{self._agency_level(agency.get(key, 0.5))}")
            parts.append("\n".join(lines))

        parts.append(
            "【行为原则】\n"
            "这些信息是内部情境，不是固定台词或强制任务。\n"
            "根据当前话题、角色自身状态和上下文自行决定是否关注、追问、延续或转换话题。\n"
            "不要机械复述‘事件’‘行为倾向’等内部字段，也不要把内部数值说给用户。"
        )
        return "\n".join(parts)
