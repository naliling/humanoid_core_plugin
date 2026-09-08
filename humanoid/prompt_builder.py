"""低 Token 注入构建器。

v2.13.0：默认注入改为“紧凑状态卡”，只发送模型真正需要的少量状态。
行为事件为一次性短期上下文，避免同一事件在一个小时内反复进入每次 LLM 请求。
"""

from __future__ import annotations

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
        cfg = self.config
        events = events or []
        agency = agency or {}

        # v2.13.0：默认 low 模式走极简状态卡。
        # full 模式仍保留较完整的信息，兼容原有配置。
        if cfg.inject_activity_context == "full":
            return self._build_full(user_id, is_group, events, agency)

        return self._build_compact(user_id, is_group, events, agency)

    # ========================================================
    # v2.13.0 默认紧凑模式
    # ========================================================

    def _build_compact(
        self,
        user_id: str,
        is_group: bool,
        events: List[Dict[str, Any]],
        agency: Dict[str, float],
    ) -> str:
        snap = self._core.snapshot(refresh=False)
        now = self._core.clock.now()

        # 一行状态，避免大量标题、解释和重复系统指令。
        parts = [
            f"日期{snap['today']}·{self._get_time_of_day_cn(now.hour)}",
            f"地点{snap['city']}",
            f"精力{snap['energy']['text']}",
        ]

        if snap.get("weather"):
            env = str(snap["weather"].get("env", "")).strip()
            if env:
                parts.append(f"天气{env[:18]}")

        cycle = str(snap.get("cycle") or "").strip()
        if cycle:
            parts.append(f"生理{cycle[:18]}")

        proc = self._core.process.current()
        process_name = str(proc.get("name", "")).strip()
        phase = str(proc.get("phase", "")).strip()
        if process_name and process_name not in {"休息", "自由活动"}:
            if phase and phase != process_name:
                parts.append(f"过程{process_name}/{phase}")
            else:
                parts.append(f"过程{process_name}")

        mood_allowed = self.config.mood_enabled and (
            not is_group or self.config.mood_enabled_in_group
        )
        if mood_allowed:
            label = self._core.mood.label(user_id)
            if label:
                parts.append(f"情绪{label}")

        nickname = self._core.mood.nickname(user_id) if mood_allowed else ""
        if nickname:
            parts.append(f"称呼{nickname}")

        if self.config.social_energy_enabled:
            social = float(self._core.social.value)
            if social < 30:
                parts.append("社交能量低")
            elif social < 60:
                parts.append("社交能量一般")

        if self.config.night_mode_enabled and self._core.clock.is_night():
            if self._core.clock.is_deep_sleep():
                parts.append("深夜偏困")
            else:
                parts.append("夜间偏困")

        text = "【拟人状态】" + "；".join(parts) + "。"

        behavior = self._build_compact_behavior(events, agency)
        if behavior:
            text += behavior

        # 保护极端情况下的注入长度。
        return text[:900]

    def _build_compact_behavior(
        self,
        events: List[Dict[str, Any]],
        agency: Dict[str, float],
    ) -> str:
        if not events:
            # v2.13.0：没有事件时不注入 agency。
            # 这是本次 Token 优化最重要的改动之一。
            return ""

        top = events[0]
        event_type = str(top.get("type", ""))
        data = top.get("data") or {}

        if event_type == "conversation_started":
            event_text = "刚开始交流"
        elif event_type == "conversation_resumed":
            event_text = "刚重新接上对话"
        elif event_type == "user_returned":
            event_text = "用户隔了一段时间重新出现"
        elif event_type == "long_gap":
            event_text = "用户久别后重新出现"
        else:
            event_text = "最近出现了交流变化"

        text = f"【近期情境】{event_text}。"

        previous = data.get("previous_message")
        if previous:
            # with_last_msg 才允许携带上一句话，并进一步截断。
            previous = str(previous).replace("\n", " ").strip()[:70]
            if previous:
                text += f"离开前用户说：{previous}。"

        # 只给最高相关的 1～2 个倾向，不再塞 5 行枚举。
        if agency:
            ranked = sorted(
                agency.items(),
                key=lambda item: float(item[1]),
                reverse=True,
            )
            labels = {
                "initiative": "主动",
                "curiosity": "好奇",
                "care": "关心",
                "social_willingness": "社交意愿",
                "continuation": "延续话题",
            }
            selected = []
            for key, value in ranked:
                value = float(value)
                if value >= 0.62 and key in labels:
                    selected.append(labels[key])
                if len(selected) >= 2:
                    break
            if selected:
                text += "倾向：" + "、".join(selected) + "。"

        return text

    # ========================================================
    # Full 模式，兼容原功能但去掉冗长解释
    # ========================================================

    def _build_full(
        self,
        user_id: str,
        is_group: bool,
        events: List[Dict[str, Any]],
        agency: Dict[str, float],
    ) -> str:
        snap = self._core.snapshot(refresh=False)
        now = self._core.clock.now()
        tod = self._get_time_of_day_cn(now.hour)

        lines = [
            "【拟人状态】",
            f"日期：{snap['today']} 星期{snap['weekday']}，{tod}",
            f"城市：{snap['city']}",
            f"精力：{snap['energy']['text']} ({int(snap['energy']['value'])}/{int(snap['energy']['max'])})",
            f"生理：{snap.get('cycle') or '正常'}",
        ]

        weather = snap.get("weather") or {}
        if weather.get("env"):
            lines.append(f"天气：{str(weather['env'])[:50]}")

        proc = self._core.process.current()
        name = str(proc.get("name", "休息"))
        phase = str(proc.get("phase", ""))
        if phase and phase != name:
            lines.append(f"生活过程：{name}/{phase}")
        else:
            lines.append(f"生活过程：{name}")
        lines.append("过程只是生活背景，可随时暂停、改变或忽略，不限制回复。")

        mood_allowed = self.config.mood_enabled and (
            not is_group or self.config.mood_enabled_in_group
        )
        if mood_allowed:
            data = self._core.mood.profile(user_id)
            label = self._core.mood.label(user_id)
            lines.append(
                f"关系：好感{float(data['affection']):.1f}/100，"
                f"亲近{float(data['libido']):.1f}/50，情绪{label}"
            )
            nickname = self._core.mood.nickname(user_id)
            if nickname:
                lines.append(f"用户称呼：{nickname}")

        if self.config.social_energy_enabled:
            lines.append(f"社交能量：{int(self._core.social.value)}%")

        if self.config.night_mode_enabled and self._core.clock.is_night():
            lines.append(
                "夜间：深睡" if self._core.clock.is_deep_sleep() else "夜间：浅睡"
            )

        behavior = self._build_compact_behavior(events, agency)
        if behavior:
            lines.append(behavior)

        return "\n".join(lines)

    # ========================================================
    # 工具
    # ========================================================

    @staticmethod
    def _get_time_of_day_cn(hour: int) -> str:
        if 5 <= hour < 8:
            return "清晨"
        if 8 <= hour < 12:
            return "上午"
        if 12 <= hour < 14:
            return "中午"
        if 14 <= hour < 18:
            return "下午"
        if 18 <= hour < 21:
            return "傍晚"
        if 21 <= hour < 24:
            return "晚上"
        return "深夜"
