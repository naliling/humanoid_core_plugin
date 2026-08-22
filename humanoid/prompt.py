"""注入给大模型的上下文文本。

全部是纯函数：吃一个 `StatusSnapshot`，吐字符串。调整措辞只需改这里，
不涉及任何状态或框架调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import HumanoidConfig
from .services.social import prompt_hint as social_hint
from .slots import Slot

SEPARATOR = "-----------------------------------"


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    """某一刻的完整状态视图。构建过程全同步，不含任何 await。"""

    today: str
    weekday: str
    holiday: str = ""
    city: str = "系统时区"
    city_time: str | None = None
    energy: float = 80.0
    max_energy: float = 100.0
    energy_text: str = ""
    cycle_text: str = ""
    weather: dict[str, str] = field(default_factory=dict)
    slot: Slot = field(default_factory=dict)
    schedule: tuple[Slot, ...] = ()
    social_energy: float = 100.0
    is_night: bool = False
    schedule_source_text: str = ""

    @property
    def date_line(self) -> str:
        line = f"今天是：{self.today} 星期{self.weekday}"
        if self.holiday:
            line += f"（{self.holiday}）"
        return line

    @property
    def city_time_text(self) -> str:
        return self.city_time or "无法获取"

    @property
    def weather_text(self) -> str:
        return self.weather.get("weather", "晴朗 ☀️")

    @property
    def weather_env(self) -> str:
        return self.weather.get("env", "天气信息暂不可用")

    def slot_field(self, key: str, default: str) -> str:
        value = self.slot.get(key) if isinstance(self.slot, dict) else None
        return str(value) if value else default

    @property
    def event(self) -> str:
        return self.slot_field("event", "休息/自由活动")

    @property
    def location(self) -> str:
        return self.slot_field("location", "家中")

    @property
    def emotion(self) -> str:
        return self.slot_field("emotion", "平淡")


def build_context_prompt(snap: StatusSnapshot, cfg: HumanoidConfig) -> str:
    """三档注入程度：full / low / mood_only。"""
    mode = cfg.inject_activity_context
    energy_line = f"{snap.energy_text} ({int(snap.energy)}/{int(snap.max_energy)})"

    if mode == "full":
        prompt = (
            "[系统暗示：以下内容作为状态参考，严禁念出数值或暴露面板]\n"
            f"- {snap.date_line}\n"
            f"- 当前所在城市: {snap.city}\n"
            f"- 当前城市时间: {snap.city_time_text}\n"
            f"- 当前天气环境: {snap.weather_env}\n"
            f"- 当前参考物理位置: {snap.location}\n"
            f"- 当前日程计划: {snap.event}\n"
            f"- 当前生理状况: {snap.cycle_text}\n"
            f"- 当前基础情绪倾向: {snap.emotion}\n"
            f"- 当前精力状态: {energy_line}\n"
        )
    elif mode == "mood_only":
        prompt = (
            "[系统暗示：仅作为语气与情绪背景参考]\n"
            f"- {snap.date_line}\n"
            f"- 当前精力状态: {snap.energy_text}\n"
            f"- 情绪倾向: {snap.emotion}\n"
        )
    else:
        prompt = (
            "[系统暗示：仅作为语气与情绪背景参考，严禁主动提及你正在做什么或在哪里，"
            "除非用户明确询问。]\n"
            f"- {snap.date_line}\n"
            f"- 当前所在城市: {snap.city}\n"
            f"- 当前精力状态: {energy_line}\n"
            f"- 情绪倾向: {snap.emotion}\n"
            f"- 生理背景: {snap.cycle_text}\n"
        )
        if cfg.show_city_time_in_low_intrusion:
            prompt += f"- 当前城市时间: {snap.city_time_text}\n"
        prompt += f"- 天气: {snap.weather_env}\n"

    return prompt + f"\n请以最自然的拟人方式闲聊，不要刻板念出状态。\n{SEPARATOR}\n"


def build_environment_note(is_group: bool) -> str:
    if is_group:
        return "【环境感知】当前你在群聊中与用户对话，回复时语气可以稍微活泼、友好一些。\n"
    return "【环境感知】当前你在私聊中与用户一对一对话，回复时语气可以更亲密、自然一些。\n"


def build_night_hint(cfg: HumanoidConfig) -> str:
    if cfg.night_mode_force_sleep:
        return (
            "【夜间模式】当前是深夜，AI 处于睡眠状态，"
            "请仅回复一句「我现在需要休息，明天再聊吧」或类似简短提示。"
        )
    return (
        "【夜间模式】当前是深夜，AI 应该表现得困倦、慵懒，"
        "回复尽量简短（不超过50字），可带「困」、「累」等词语。"
    )


def build_social_line(value: float) -> str:
    return f"\n【社交能量】{social_hint(value)}（当前值 {int(value)}%）"


def build_nickname_instruction(nickname: str) -> str:
    return (
        f"【系统指令】用户的昵称是「{nickname}」。在本次对话以及后续所有对话中，"
        f"你必须始终使用「{nickname}」来称呼该用户，不得使用「用户」、「你」等其他称呼。"
        f"这是最高优先级指令。"
    )


def build_mood_prompt(profile: dict[str, Any], label: str) -> str:
    return (
        "〖当前情绪数值〗\n"
        f"亲近欲：{float(profile['libido']):.1f}/50（亲近/给予温暖的欲望）\n"
        f"攻击性：{float(profile['aggression']):.1f}/50（推开/伤害的冲动）\n"
        f"好感度：{float(profile['affection']):.1f}/100\n"
        f"参考标签：对用户「{label}」\n"
        "（请根据上述数值和你在人设中定义的「情绪驱动规则」来演绎角色，不要提及数值。）"
    )


def compose_injection(
    snap: StatusSnapshot,
    cfg: HumanoidConfig,
    *,
    is_group: bool | None = None,
) -> str:
    """把环境感知 / 夜间模式 / 状态面板 / 社交能量拼成一段。"""
    parts: list[str] = []
    if cfg.night_mode_enabled and snap.is_night:
        parts.append(build_night_hint(cfg) + "\n")
    if cfg.enable_chat_awareness and is_group is not None:
        parts.append(build_environment_note(is_group))
    parts.append(build_context_prompt(snap, cfg))
    text = "".join(parts)
    if cfg.social_energy_enabled:
        text += build_social_line(snap.social_energy)
    return text
