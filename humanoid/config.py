"""插件配置：唯一的默认值来源 + 类型强制 + 范围钳制。

默认值只写在这里一份，`_conf_schema.json` 只负责 WebUI 的展示与控件类型
（两边的一致性由 `tests/test_schema.py` 守护）。

键名与 v2.10.x 一致，老配置文件可直接加载。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from typing import Any

from .data.cities import DEFAULT_CITY_PLACEHOLDER

# 时间粒度选项 → 对齐到多少分钟。flexible 表示不做对齐要求。
GRANULARITY_MINUTES: dict[str, int] = {
    "flexible": 0,
    "5min": 5,
    "10min": 10,
    "15min": 15,
    "20min": 20,
    "30min": 30,
    "hourly": 60,
}

INJECT_MODES = ("full", "low", "mood_only")
ENVIRONMENT_MODES = ("both", "private", "group")
CYCLE_STYLES = ("default", "simple")

_TRUE_STRINGS = {"1", "true", "yes", "y", "on", "是", "开"}
_FALSE_STRINGS = {"0", "false", "no", "n", "off", "否", "关"}


class _Missing:
    """哨兵：区分「键不存在」与「键存在但值为空」。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "<MISSING>"


_MISSING = _Missing()


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in _TRUE_STRINGS:
            return True
        if low in _FALSE_STRINGS:
            return False
    return default


def _as_int(value: Any, default: int, low: int | None = None, high: int | None = None) -> int:
    try:
        out = int(float(value))
    except (TypeError, ValueError):
        out = default
    if low is not None:
        out = max(low, out)
    if high is not None:
        out = min(high, out)
    return out


def _as_float(
    value: Any,
    default: float,
    low: float | None = None,
    high: float | None = None,
) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        out = default
    if out != out:  # NaN
        out = default
    if low is not None:
        out = max(low, out)
    if high is not None:
        out = min(high, out)
    return out


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _as_choice(value: Any, options: tuple[str, ...], default: str) -> str:
    text = _as_str(value, default)
    return text if text in options else default


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, Iterable):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _as_float_tuple(value: Any, default: tuple[float, ...]) -> tuple[float, ...]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        return default
    out: list[float] = []
    for item in value:
        try:
            out.append(float(item))
        except (TypeError, ValueError):
            return default
    return tuple(out) if out else default


def _as_mapping_tuple(value: Any) -> tuple[dict[str, Any], ...]:
    """节日列表：只保留形如 {"date": ..., "name": ...} 的项。"""
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        return ()
    out: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, Mapping) and item.get("date"):
            out.append({"date": str(item["date"]).strip(), "name": str(item.get("name", "")).strip()})
    return tuple(out)


@dataclass(frozen=True, slots=True)
class HumanoidConfig:
    """一份不可变的配置快照。热重载时整体替换，不做就地修改。"""

    # ---------- 生理 / 精力 ----------
    max_energy: float = 100.0
    enable_cycle: bool = True
    cycle_length: int = 28
    energy_decay_rate: float = 0.5
    cycle_description_style: str = "default"
    enable_energy_natural_recovery: bool = True
    energy_natural_recovery_per_minute: float = 0.15
    energy_natural_recovery_interval_minutes: int = 1
    energy_consumption_per_msg: float = 0.04
    energy_recovery_phase_multipliers: tuple[float, ...] = (0.5, 1.0, 2.0, 1.0, 0.8, 0.7)

    # ---------- 日程 ----------
    use_llm_schedule: bool = True
    schedule_provider_name: str = ""
    schedule_fallback_provider_name: str = ""
    schedule_allow_global_fallback: bool = True
    schedule_retry_interval_seconds: int = 2
    schedule_llm_timeout_seconds: int = 60
    schedule_generation_max_attempts: int = 2
    schedule_max_slots: int = 16
    schedule_provider_cooldown_minutes: int = 30
    schedule_prompt_extra: str = "休闲日常，愉快的生活。"
    schedule_time_granularity: str = "15min"
    character_personality: str = "温柔体贴"

    # ---------- 权限 ----------
    admin_qq: tuple[str, ...] = ()

    # ---------- 天气 ----------
    weather_enabled: bool = True
    weather_api_key: str = ""
    weather_location: str = "Heyuan,CN"
    weather_refresh_minutes: int = 60

    # ---------- 聊天模式 / 环境 ----------
    inject_activity_context: str = "low"
    environment_mode: str = "both"
    enable_chat_awareness: bool = True
    show_city_time_in_low_intrusion: bool = True
    timezone_city: str = DEFAULT_CITY_PLACEHOLDER

    # ---------- 情绪 ----------
    mood_enabled: bool = True
    mood_provider_name: str = ""
    mood_sensitivity: int = 60
    mood_decay_hours: float = 6.0
    mood_initial_affection: int = 46
    mood_initial_libido: int = 34
    mood_initial_aggression: int = 28
    mood_affection_override: tuple[str, ...] = ()
    mood_affection_delta_cap: int = 2
    mood_log_enabled: bool = True
    mood_log_max_entries: int = 28
    mood_log_threshold_affection: int = 2
    mood_log_threshold_libido: int = 2
    mood_log_threshold_aggression: int = 1
    mood_update_timeout: float = 120.0
    mood_tag_enabled: bool = True
    mood_use_llm_for_delta: bool = True
    mood_provider_cooldown_minutes: int = 5
    mood_llm_interval_messages: int = 5
    mood_verbose_log: bool = False
    mood_enabled_in_group: bool = False
    mood_data_retention_days: int = 7

    # ---------- 社交能量 ----------
    social_energy_enabled: bool = True
    social_energy_consumption_per_msg: float = 0.05
    social_energy_recovery_per_minute: float = 1.5
    social_energy_reset_hour: int = 0
    social_energy_recovery_interval_seconds: int = 60

    # ---------- 夜间模式 ----------
    night_mode_enabled: bool = True
    night_start_hour: int = 23
    night_end_hour: int = 6
    night_mode_force_sleep: bool = False
    night_deep_sleep_ratio: float = 0.5

    # ---------- 其它 ----------
    debug_mode: bool = False
    holidays: tuple[dict[str, Any], ...] = ()
    state_flush_interval_seconds: int = 5
    last_interaction_threshold_minutes: int = 10

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any] | None) -> HumanoidConfig:
        """从 AstrBotConfig（dict 子类）构造。缺失键用默认值，越界值就地钳制。"""
        src: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}

        def pick(key: str) -> Any:
            return src.get(key, _MISSING)

        d = cls()  # 默认值来源

        def b(key: str) -> bool:
            v = pick(key)
            return getattr(d, key) if v is _MISSING else _as_bool(v, getattr(d, key))

        def i(key: str, low: int | None = None, high: int | None = None) -> int:
            v = pick(key)
            return getattr(d, key) if v is _MISSING else _as_int(v, getattr(d, key), low, high)

        def f(key: str, low: float | None = None, high: float | None = None) -> float:
            v = pick(key)
            return getattr(d, key) if v is _MISSING else _as_float(v, getattr(d, key), low, high)

        def s(key: str) -> str:
            v = pick(key)
            return getattr(d, key) if v is _MISSING else _as_str(v, getattr(d, key))

        def c(key: str, options: tuple[str, ...]) -> str:
            v = pick(key)
            return getattr(d, key) if v is _MISSING else _as_choice(v, options, getattr(d, key))

        def s_opt(key: str) -> str:
            """允许空串的字符串项：空串是有意义的取值（未设置 / 无附加要求）。"""
            v = pick(key)
            return getattr(d, key) if v is _MISSING else ("" if v is None else str(v).strip())

        return cls(
            max_energy=f("max_energy", 1.0, 10_000.0),
            enable_cycle=b("enable_cycle"),
            cycle_length=i("cycle_length", 1, 365),
            energy_decay_rate=f("energy_decay_rate", 0.0, 100.0),
            cycle_description_style=c("cycle_description_style", CYCLE_STYLES),
            enable_energy_natural_recovery=b("enable_energy_natural_recovery"),
            energy_natural_recovery_per_minute=f("energy_natural_recovery_per_minute", 0.0, 100.0),
            energy_natural_recovery_interval_minutes=i("energy_natural_recovery_interval_minutes", 1, 1440),
            energy_consumption_per_msg=f("energy_consumption_per_msg", 0.0, 100.0),
            energy_recovery_phase_multipliers=_as_float_tuple(
                pick("energy_recovery_phase_multipliers"), d.energy_recovery_phase_multipliers
            ),
            use_llm_schedule=b("use_llm_schedule"),
            schedule_provider_name=s_opt("schedule_provider_name"),
            schedule_fallback_provider_name=s_opt("schedule_fallback_provider_name"),
            schedule_allow_global_fallback=b("schedule_allow_global_fallback"),
            schedule_retry_interval_seconds=i("schedule_retry_interval_seconds", 0, 600),
            schedule_llm_timeout_seconds=i("schedule_llm_timeout_seconds", 10, 300),
            schedule_generation_max_attempts=i("schedule_generation_max_attempts", 1, 5),
            schedule_max_slots=i("schedule_max_slots", 6, 48),
            schedule_provider_cooldown_minutes=i("schedule_provider_cooldown_minutes", 0, 1440),
            schedule_prompt_extra=s_opt("schedule_prompt_extra"),
            schedule_time_granularity=c("schedule_time_granularity", tuple(GRANULARITY_MINUTES)),
            character_personality=s("character_personality"),
            admin_qq=_as_str_tuple(pick("admin_qq")),
            weather_enabled=b("weather_enabled"),
            weather_api_key=s_opt("weather_api_key"),
            weather_location=s("weather_location"),
            weather_refresh_minutes=i("weather_refresh_minutes", 1, 1440),
            inject_activity_context=c("inject_activity_context", INJECT_MODES),
            environment_mode=c("environment_mode", ENVIRONMENT_MODES),
            enable_chat_awareness=b("enable_chat_awareness"),
            show_city_time_in_low_intrusion=b("show_city_time_in_low_intrusion"),
            timezone_city=s("timezone_city"),
            mood_enabled=b("mood_enabled"),
            mood_provider_name=s_opt("mood_provider_name"),
            mood_sensitivity=i("mood_sensitivity", 0, 100),
            mood_decay_hours=f("mood_decay_hours", 0.1, 720.0),
            mood_initial_affection=i("mood_initial_affection", 0, 100),
            mood_initial_libido=i("mood_initial_libido", 0, 50),
            mood_initial_aggression=i("mood_initial_aggression", 0, 50),
            mood_affection_override=_as_str_tuple(pick("mood_affection_override")),
            mood_affection_delta_cap=i("mood_affection_delta_cap", 1, 10),
            mood_log_enabled=b("mood_log_enabled"),
            mood_log_max_entries=i("mood_log_max_entries", 1, 1000),
            mood_log_threshold_affection=i("mood_log_threshold_affection", 0, 100),
            mood_log_threshold_libido=i("mood_log_threshold_libido", 0, 50),
            mood_log_threshold_aggression=i("mood_log_threshold_aggression", 0, 50),
            mood_update_timeout=f("mood_update_timeout", 5.0, 600.0),
            mood_tag_enabled=b("mood_tag_enabled"),
            mood_use_llm_for_delta=b("mood_use_llm_for_delta"),
            mood_provider_cooldown_minutes=i("mood_provider_cooldown_minutes", 0, 1440),
            mood_llm_interval_messages=i("mood_llm_interval_messages", 1, 100),
            mood_verbose_log=b("mood_verbose_log"),
            mood_enabled_in_group=b("mood_enabled_in_group"),
            mood_data_retention_days=i("mood_data_retention_days", 0, 365),
            social_energy_enabled=b("social_energy_enabled"),
            social_energy_consumption_per_msg=f("social_energy_consumption_per_msg", 0.0, 100.0),
            social_energy_recovery_per_minute=f("social_energy_recovery_per_minute", 0.0, 100.0),
            social_energy_reset_hour=i("social_energy_reset_hour", -1, 23),
            social_energy_recovery_interval_seconds=i("social_energy_recovery_interval_seconds", 60, 300),
            night_mode_enabled=b("night_mode_enabled"),
            night_start_hour=i("night_start_hour", 0, 23),
            night_end_hour=i("night_end_hour", 0, 23),
            night_mode_force_sleep=b("night_mode_force_sleep"),
            night_deep_sleep_ratio=f("night_deep_sleep_ratio", 0.1, 1.0),
            debug_mode=b("debug_mode"),
            holidays=_as_mapping_tuple(pick("holidays")),
            state_flush_interval_seconds=i("state_flush_interval_seconds", 1, 60),
            last_interaction_threshold_minutes=i("last_interaction_threshold_minutes", 0, 1440),
        )

    # ---------- 派生属性 ----------

    @property
    def granularity_minutes(self) -> int:
        """时间点需要对齐到的分钟数；0 表示不要求对齐。"""
        return GRANULARITY_MINUTES.get(self.schedule_time_granularity, 15)

    @property
    def schedule_provider_ids(self) -> tuple[tuple[str, str], ...]:
        """日程模型的显式候选链：((用途标签, provider_id), ...)，已去空去重。"""
        chain: list[tuple[str, str]] = []
        seen: set[str] = set()
        for label, pid in (
            ("首选模型", self.schedule_provider_name),
            ("备用模型", self.schedule_fallback_provider_name),
        ):
            if pid and pid not in seen:
                seen.add(pid)
                chain.append((label, pid))
        return tuple(chain)

    @property
    def mood_provider_ids(self) -> tuple[tuple[str, str], ...]:
        """情绪分析模型链。留空时沿用日程链。"""
        if self.mood_provider_name:
            return (("情绪模型", self.mood_provider_name),)
        return self.schedule_provider_ids

    def is_night_hour(self, hour: int) -> bool:
        """夜间模式判定，支持跨零点区间（如 23 → 6）。"""
        start, end = self.night_start_hour, self.night_end_hour
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def is_deep_sleep(self, hour: int) -> bool:
        """判断当前小时是否属于深度睡眠段（基于 night_deep_sleep_ratio）。"""
        if not self.night_mode_enabled:
            return False
        start, end = self.night_start_hour, self.night_end_hour
        if start == end:
            return False
        # 计算夜间总时长（小时）
        if start < end:
            total_hours = end - start
            offset = hour - start
        else:
            total_hours = 24 - start + end
            offset = hour - start
            if offset < 0:
                offset += 24
        ratio = max(0.1, min(1.0, self.night_deep_sleep_ratio))
        deep_hours = total_hours * ratio
        return 0 <= offset < deep_hours

    def is_admin(self, sender_id: str) -> bool:
        """仅检查插件自己的 admin_qq；AstrBot 全局管理员由 main.py 另行叠加。"""
        return str(sender_id) in self.admin_qq

    def affection_override_for(self, qq: str) -> float | None:
        """解析 mood_affection_override 里 `QQ:数值` 形式的指定好感度。"""
        for item in self.mood_affection_override:
            if ":" not in item:
                continue
            key, _, raw = item.partition(":")
            if key.strip() != str(qq):
                continue
            try:
                return max(0.0, min(100.0, float(raw.strip())))
            except ValueError:
                return None
        return None

    def cycle_phase_index(self, cycle_day: int) -> int:
        """生理周期第 N 天 → 6 个阶段的下标（0=经期 … 5=经前期）。"""
        length = max(1, self.cycle_length)
        # 把可配周期长度线性映射回 28 天的阶段划分，避免 cycle_length≠28 时错位
        day = ((int(cycle_day) - 1) % length) * 28.0 / length + 1
        for upper, idx in ((5, 0), (12, 1), (15, 2), (21, 3), (26, 4)):
            if day <= upper:
                return idx
        return 5

    def phase_recovery_multiplier(self, cycle_day: int) -> float:
        idx = self.cycle_phase_index(cycle_day)
        multipliers = self.energy_recovery_phase_multipliers
        return multipliers[idx] if idx < len(multipliers) else 1.0

    def as_dict(self) -> dict[str, Any]:
        """用于诊断输出；不包含 API Key 明文。"""
        out: dict[str, Any] = {}
        for f_ in fields(self):
            value = getattr(self, f_.name)
            if f_.name == "weather_api_key":
                value = f"<已设置 {len(value)} 字符>" if value else "<未设置>"
            out[f_.name] = value
        return out


DEFAULTS = HumanoidConfig()
"""默认配置快照。`_conf_schema.json` 的默认值应与此保持一致（有测试守护）。"""
