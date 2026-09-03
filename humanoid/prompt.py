"""注入给大模型的上下文文本。

全部是纯函数：吃一个 `StatusSnapshot`，吐字符串。调整措辞只需改这里，
不涉及任何状态或框架调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import HumanoidConfig
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

    lines = [
        "[系统边界] 以下信息仅供内部状态参考，禁止在回复中提及任何具体数据。",
        f"- {snap.date_line}"
    ]

    if mode == "full":
        lines += [
            f"- 城市: {snap.city}",
            f"- 时间: {snap.city_time_text}",
            f"- 天气: {snap.weather_env}",
            f"- 位置: {snap.location}",
            f"- 日程: {snap.event}",
            f"- 生理: {snap.cycle_text}",
            f"- 精力: {energy_line}"
        ]
    elif mode == "mood_only":
        lines += [
            f"- 精力: {snap.energy_text}",
            f"- 情绪倾向: {snap.emotion}"
        ]
    else:
        lines += [
            f"- 城市: {snap.city}",
            f"- 精力: {energy_line}",
            f"- 情绪: {snap.emotion}",
            f"- 生理背景: {snap.cycle_text}"
        ]
        if cfg.show_city_time_in_low_intrusion:
            lines.append(f"- 时间: {snap.city_time_text}")
        lines.append(f"- 天气: {snap.weather_env}")

    return "\n".join(lines) + f"\n{SEPARATOR}"


def build_environment_note(is_group: bool) -> str:
    return "群聊中。" if is_group else "私聊中。"


def build_night_hint(cfg: HumanoidConfig, clock: Any) -> str:
    if not clock.is_night():
        return ""
    if clock.is_deep_sleep():
        return "【夜间】深度睡眠时段。"
    return "【夜间】浅睡时段。"


def build_social_line(value: float) -> str:
    if value > 70:
        desc = "充足"
    elif value > 40:
        desc = "一般"
    else:
        desc = "较低"
    return f"\n【社交能量】{desc}（{int(value)}%）"


def build_nickname_instruction(nickname: str) -> str:
    return f"【指令】用户昵称：{nickname}"


# ---------- 情绪语气提示表 ----------
MOOD_TONE_HINTS = {
    "亲密": "语气温柔，带一点亲昵",
    "依恋": "语气柔和，略带撒娇",
    "信赖": "语气坚定，充满信任",
    "热情": "语气活泼，表现出兴趣",
    "友好": "语气友善，保持礼貌",
    "平常": "语气自然，不刻意",
    "疏远": "语气客气，保持距离",
    "冷淡": "语气平淡，不热情",
    "敌视": "语气冷硬，保持警惕",
    "警惕": "语气谨慎，观察为主",
}


def build_mood_prompt(profile: dict[str, Any], label: str) -> str:
    lines = [
        "〖关系〗",
        f"好感度：{float(profile['affection']):.1f}/100",
        f"亲近欲：{float(profile['libido']):.1f}/50",
        f"标签：{label}",
    ]
    # 附加语气提示
    hint = MOOD_TONE_HINTS.get(label)
    if hint:
        lines.append(f"【语气提示】{hint}")
    return "\n".join(lines)


def compose_injection(
    snap: StatusSnapshot,
    cfg: HumanoidConfig,
    *,
    is_group: bool | None = None,
    clock: Any,
) -> str:
    parts = []
    parts.append("[系统边界] 以下信息仅供内部状态参考，禁止在回复中提及任何具体数据。")
    if snap.is_night:
        parts.append(build_night_hint(cfg, clock))
    if cfg.enable_chat_awareness and is_group is not None:
        parts.append(build_environment_note(is_group))
    parts.append(build_context_prompt(snap, cfg))
    if cfg.social_energy_enabled:
        parts.append(build_social_line(snap.social_energy))
    return "\n".join(parts)