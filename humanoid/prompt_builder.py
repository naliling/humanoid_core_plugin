"""注入文本构建器：为单个角色构建注入大模型的上下文。"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .core_instance import HumanoidCoreInstance

from .config import HumanoidConfig


class PromptBuilder:
    """为单个角色构建注入文本。"""

    def __init__(self, core_instance: HumanoidCoreInstance):
        self._core = core_instance

    @property
    def config(self) -> HumanoidConfig:
        return self._core.config

    def build(self, user_id: str, is_group: bool = False) -> str:
        parts: list[str] = []

        parts.append(self._build_environment(user_id, is_group))
        parts.append(self._build_self_state())

        process_text = self._build_process()
        if process_text:
            parts.append(process_text)

        night_hint = self._build_night_hint()
        if night_hint:
            parts.append(night_hint)

        if self.config.social_energy_enabled:
            parts.append(self._build_social_energy())

        mood_allowed = self.config.mood_enabled and (not is_group or self.config.mood_enabled_in_group)
        if mood_allowed:
            parts.append(self._build_mood(user_id))
            nickname = self._core.mood.nickname(user_id)
            if nickname:
                parts.append(self._build_nickname_instruction(nickname))

        interval = self._build_interval_note(user_id)
        if interval:
            parts.append(interval)

        return "\n".join(parts)

    def _build_environment(self, user_id: str, is_group: bool) -> str:
        lines = ["[系统暗示：以下信息仅供角色参考，不要念出或暴露这些细节]"]
        if self.config.enable_chat_awareness:
            if is_group:
                lines.append("【环境】你正在群聊中与用户对话。")
            else:
                lines.append("【环境】你正在私聊中与用户一对一对话。")
        return "\n".join(lines)

    def _build_self_state(self) -> str:
        cfg = self.config
        mode = cfg.inject_activity_context
        snap = self._core.snapshot()

        # 使用自定义时间格式：YYYY.MM.DD.HH.MM
        now = self._core.clock.now()
        time_str = now.strftime("%Y.%m.%d.%H.%M")  # 例如 2026.09.03.12.35

        lines = []
        if mode == "full":
            lines.extend([
                f"- 日期：{snap['today']} 星期{snap['weekday']}",
                f"- 城市：{snap['city']}",
                f"- 时间：{time_str}",
                f"- 精力：{snap['energy']['text']}",
                f"- 生理：{snap['cycle'] or '正常'}",
            ])
        elif mode == "mood_only":
            lines.extend([
                f"- 日期：{snap['today']}",
                f"- 精力：{snap['energy']['text']}",
            ])
        else:
            lines.extend([
                f"- 日期：{snap['today']} 星期{snap['weekday']}",
                f"- 城市：{snap['city']}",
                f"- 精力：{snap['energy']['text']}",
                f"- 生理背景：{snap['cycle'] or '正常'}",
            ])
            if cfg.show_city_time_in_low_intrusion:
                lines.append(f"- 当前时间：{time_str}")

        if snap['weather']:
            lines.append(f"- 天气：{snap['weather'].get('env', '未知')}")

        return "\n".join(lines)

    def _build_process(self) -> str:
        proc = self._core.process.current()
        name = proc.get("name", "休息")
        start_str = proc.get("started_at")
        end_str = proc.get("expected_end")

        if start_str and end_str:
            try:
                start = datetime.fromisoformat(start_str)
                end = datetime.fromisoformat(end_str)
                now = self._core.clock.now()
                elapsed = int((now - start).total_seconds() // 60)
                remaining = int((end - now).total_seconds() // 60)
                if remaining > 0:
                    return f"【当前过程】正在{name}（已持续约 {elapsed} 分钟，预计还将持续约 {remaining} 分钟）"
                return f"【当前过程】正在{name}（已持续约 {elapsed} 分钟）"
            except ValueError:
                pass
        return f"【当前过程】正在{name}"

    def _build_night_hint(self) -> str:
        clock = self._core.clock
        cfg = self.config
        if not cfg.night_mode_enabled or not clock.is_night():
            return ""

        if clock.is_deep_sleep():
            if cfg.night_mode_force_sleep:
                return "【夜间模式】深度睡眠中，请仅简短回复休息提示。"
            return "【夜间模式】深度睡眠中，你刚被吵醒，回复应极简短、迷糊。"
        else:
            if cfg.night_mode_force_sleep:
                return "【夜间模式】浅睡中，可以简短回应，但要表现出困倦。"
            return "【夜间模式】浅睡中，语气迷糊、慵懒，不超过30字。"

    def _build_social_energy(self) -> str:
        value = self._core.social.value
        hint = self._core.social.hint()
        return f"【社交能量】{hint}（当前值 {int(value)}%）"

    def _build_mood(self, user_id: str) -> str:
        """单独列出好感度和情绪标签，实时从 core.mood 读取最新值。"""
        data = self._core.mood.profile(user_id)
        label = self._core.mood.label(user_id)
        return (
            f"〖与当前用户的关系〗\n"
            f"好感度：{data['affection']:.1f}/100\n"
            f"亲近欲：{data['libido']:.1f}/50\n"
            f"情绪标签：{label}\n"
            "（请根据上述数值自然演绎，不要直接提及数值）"
        )

    def _build_nickname_instruction(self, nickname: str) -> str:
        return (
            f"【系统指令】用户的昵称是「{nickname}」。"
            f"在本次对话中必须用「{nickname}」称呼该用户。"
        )

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

        # 生成精确时间描述
        if elapsed < 60:
            time_text = "不到1分钟"
        elif elapsed < 3600:
            time_text = f"约 {int(elapsed / 60)} 分钟"
        elif elapsed < 86400:
            time_text = f"约 {int(elapsed / 3600)} 小时"
        else:
            time_text = f"约 {int(elapsed / 86400)} 天"

        # 情感描述（保留原有）
        if elapsed < 900:
            sense = "她/他刚离开一小会儿。"
        elif elapsed < 3600:
            sense = "她/他离开有一阵子了。"
        elif elapsed < 14400:
            sense = "她/他消失了半天，你有点在意。"
        elif elapsed < 43200:
            sense = "她/他大半天没有音讯，你越来越在意了。"
        else:
            sense = "她/他已经很久没有出现了，你心里有点空落落的。"

        # 当间隔超过1小时，明确标注新话题
        new_topic_hint = ""
        if elapsed >= 3600:
            new_topic_hint = f" 这算是一个新话题，距离上次对话已过去{time_text}。"

        # 如果有 last_message 且模式为 with_last_msg
        last_msg = self._core._scope.get_user(user_id, "last_message")
        if last_msg and cfg.last_interaction_mode == "with_last_msg":
            text = last_msg.get("text", "")
            if text:
                self._core._scope.set_user(user_id, "last_message", None)
                return (
                    f"【上次对话】已过去 {time_text}。"
                    f"用户最后说：「{text}」\n{sense}{new_topic_hint}"
                )

        return f"【上次对话】已过去 {time_text}。{sense}{new_topic_hint}"
