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

        # 1. 柔和的系统边界（鼓励参考而非禁止提及）
        parts.append("【状态参考】以下信息反映当前环境与自身状态，可辅助你理解对话氛围，无需直接复述数值。")

        # 2. 环境感知
        if self.config.enable_chat_awareness:
            parts.append(f"【环境】{'群聊' if is_group else '私聊'}中。")

        # 3. 自身状态（根据模式）
        parts.append(self._build_self_state())

        # 4. 过程简述（仅名称，除非 full 模式带时长）
        parts.append(self._build_process())

        # 5. 情绪（仅标签，除非 full 模式带数值）
        mood_allowed = self.config.mood_enabled and (not is_group or self.config.mood_enabled_in_group)
        if mood_allowed:
            parts.append(self._build_mood(user_id, detailed=(self.config.inject_activity_context == "full")))

        # 6. 行为指令（昵称、夜间、社交能量）
        parts.append(self._build_behavior_instructions(user_id))

        # 7. 对话间隔（仅陈述时长）
        interval = self._build_interval_note(user_id)
        if interval:
            parts.append(interval)

        return "\n".join(part for part in parts if part)

    def _build_self_state(self) -> str:
        cfg = self.config
        mode = cfg.inject_activity_context
        snap = self._core.snapshot()
        now = self._core.clock.now()
        time_str = now.strftime("%Y.%m.%d.%H.%M")
        city = snap['city']
        weekday = f"星期{snap['weekday']}"
        date_str = snap['today']

        if mode == "full":
            lines = [
                f"日期：{date_str} {weekday}",
                f"城市：{city}",
                f"时间：{time_str}",
                f"精力：{snap['energy']['text']} ({int(snap['energy']['value'])}/{int(snap['energy']['max'])})",
                f"生理：{snap['cycle'] or '正常'}",
                f"天气：{snap['weather'].get('env', '未知')}"
            ]
        elif mode == "mood_only":
            lines = [f"日期：{date_str}", f"精力：{snap['energy']['text']}"]
        else:  # low
            lines = [
                f"日期：{date_str} {weekday}",
                f"城市：{city}",
                f"精力：{snap['energy']['text']}",
                f"生理背景：{snap['cycle'] or '正常'}"
            ]
            if cfg.show_city_time_in_low_intrusion:
                lines.append(f"时间：{time_str}")
            if snap['weather']:
                lines.append(f"天气：{snap['weather'].get('env', '未知')}")
        return "\n".join(lines)

    def _build_process(self) -> str:
        proc = self._core.process.current()
        name = proc.get("name", "休息")
        # 只在 full 模式下显示时长
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
        # 昵称
        nickname = self._core.mood.nickname(user_id)
        if nickname:
            instructions.append(f"【重要指令】用户的昵称是「{nickname}」，请用此称呼。")
        # 夜间模式
        night = self._build_night_instruction()
        if night:
            instructions.append(night)
        # 社交能量
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
        last_ts = self._core._scope.get_user(user_id, "last_interaction")
        if last_ts is None:
            return ""
        try:
            elapsed = time.time() - float(last_ts)
        except (TypeError, ValueError):
            return ""
        if elapsed < threshold:
            return ""
        if elapsed < 60:
            time_text = "不到1分钟"
        elif elapsed < 3600:
            time_text = f"约 {int(elapsed / 60)} 分钟"
        elif elapsed < 86400:
            time_text = f"约 {int(elapsed / 3600)} 小时"
        else:
            time_text = f"约 {int(elapsed / 86400)} 天"

        # 只陈述时长，不加“新话题”标签
        if cfg.last_interaction_mode == "with_last_msg":
            last_msg = self._core._scope.get_user(user_id, "last_message")
            if last_msg and last_msg.get("text"):
                text = last_msg["text"]
                self._core._scope.set_user(user_id, "last_message", None)  # 清空
                return f"【上次对话】已过去 {time_text}，用户最后说：「{text}」。"
        return f"【上次对话】已过去 {time_text}。"