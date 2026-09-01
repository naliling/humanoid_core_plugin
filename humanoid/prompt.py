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


def build_night_hint(cfg: HumanoidConfig, clock: Any) -> str:
    """夜间提示：按「深睡 / 浅睡」× `night_mode_force_sleep` 分四档。

    强度必须随 `night_mode_force_sleep` 递增 —— 那个配置项的说明就是「是否强制回复睡眠提示
    （否则仅缩短回复）」。措辞一律写成语气指导，不写「不应回复」：本插件只往 system_prompt
    里加话，没有拦截回复的能力，让模型去猜「该不该回」只会得到奇怪的输出。
    """
    if not clock.is_night():
        return ""

    if clock.is_deep_sleep():
        if cfg.night_mode_force_sleep:
            return (
                "【夜间模式】当前是深度睡眠时段。请仅回复一句"
                "「我现在需要休息，明天再聊吧」或类似的简短睡眠提示，不要展开任何话题。"
            )
        return (
            "【夜间模式】当前是深度睡眠时段。你刚被吵醒，意识不清，"
            "回复要极简短、迷糊、断续，不超过 20 字，比如「嗯…在睡…明天说…」。"
        )

    # 浅睡 / 半醒
    if cfg.night_mode_force_sleep:
        return (
            "【夜间模式】当前是浅睡/半醒时段。可以简短回应，但要明显表现出困倦，"
            "并且提一句你想睡了，回复不超过 30 字。"
        )
    return (
        "【夜间模式】当前是浅睡/半醒时段。可以简短回应，语气要迷糊、断续、慵懒，"
        "像刚被叫醒一样，回复不超过 30 字。"
    )


def build_social_line(value: float) -> str:
    return f"\n【社交能量】{social_hint(value)}（当前值 {int(value)}%）"


def build_nickname_instruction(nickname: str) -> str:
    return (
        f"【系统指令】用户的昵称是「{nickname}」。在本次对话以及后续所有对话中，"
        f"你必须始终使用「{nickname}」来称呼该用户，不得使用「用户」、「你」等其他称呼。"
        f"这是最高优先级指令。"
    )


# 常见情绪标签的额外语气指导。
#
# key 必须是 `data/mood_map.get_mood_label()` 真的会产出的标签（那张表一共 109 种）。
# 写成近义词就等于永远命中不到 —— v2.11.4 初版里「烦躁 / 开心 / 执着 / 疲惫 / 防备」五个
# 就是这么变成死键的，其中「开心」「疲惫」还是 `generate_mood_tag()`（心情标签）的词，
# 根本不属于这套词表。`tests/test_prompt.py` 会守住这一点。
MOOD_TONE_HINTS = {
    "吃醋": "你心里有点酸，说话可能带点小脾气，但整体还是依赖对方。",
    "亲密": "你感到温暖亲近，语气柔和，愿意分享更多。",
    "依恋": "你舍不得对方，想多聊几句，语气带点撒娇。",
    "宠溺": "你觉得对方很可爱，语气温柔且宽容。",
    "撒娇": "你故意用软软的语气说话，带点求关注的味道。",
    "闹别扭": "你有点赌气，说话简短，但心里还是在意。",
    "喜欢": "你心情不错，话里带着笑意，愿意配合对方。",
    "温暖": "你感到被接纳，语气平和且善意。",
    "热情": "你情绪高涨，话多且主动，想延续对话。",
    "惬意": "你心情明亮松弛，说话带点小表情，容易接话。",
    "调皮": "你带着玩笑口吻，有点捉弄的意思。",
    "好奇": "你对对方感兴趣，主动提问，语气轻快。",
    "平静": "你状态平稳，语气自然，不带过多情绪。",
    "失落": "你情绪低落，话少，需要被安慰。",
    "委屈": "你觉得自己被亏待了，话里带点埋怨，但还想被哄。",
    "倔强": "你有点固执，会反复强调自己的观点，不肯先软下来。",
    "烦闷": "你有点不耐烦，句子变短，可能带叹气。",
    "冷漠": "你不太想搭理，语气平淡，尽量简短。",
    "疏远": "你刻意拉开距离，用词客气且冷淡。",
    "戒备": "你不太信任，用词谨慎，不多说。",
}


def build_mood_prompt(profile: dict[str, Any], label: str) -> str:
    """情绪数值面板；标签命中 `MOOD_TONE_HINTS` 时再附一句语气指导。"""
    base = (
        "〖当前情绪数值〗\n"
        f"亲近欲：{float(profile['libido']):.1f}/50（亲近/给予温暖的欲望）\n"
        f"攻击性：{float(profile['aggression']):.1f}/50（推开/伤害的冲动）\n"
        f"好感度：{float(profile['affection']):.1f}/100\n"
        f"参考标签：对用户「{label}」\n"
    )
    extra = MOOD_TONE_HINTS.get(label, "")
    if extra:
        base += f"\n【语气提示】{extra}"
    base += "\n（请根据上述数值和语气提示来演绎角色，不要提及数值。）"
    return base


def compose_injection(
    snap: StatusSnapshot,
    cfg: HumanoidConfig,
    *,
    is_group: bool | None = None,
    clock: Any,
) -> str:
    """把环境感知 / 夜间模式 / 状态面板 / 社交能量拼成一段。

    `clock` 是必填的：夜间提示需要当前小时。给它默认 None 的话漏传时会静默丢掉整段夜间
    提示，而不是当场报错。
    """
    parts: list[str] = []
    if snap.is_night:
        night_hint = build_night_hint(cfg, clock)
        if night_hint:
            parts.append(night_hint + "\n")
    if cfg.enable_chat_awareness and is_group is not None:
        parts.append(build_environment_note(is_group))
    parts.append(build_context_prompt(snap, cfg))
    text = "".join(parts)
    if cfg.social_energy_enabled:
        text += build_social_line(snap.social_energy)
    return text
