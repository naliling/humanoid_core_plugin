"""插件配置：唯一的默认值来源 + 类型强制 + 范围钳制。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from typing import Any

from .data.cities import DEFAULT_CITY_PLACEHOLDER

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
LAST_INTERACTION_MODES = ("simple", "with_last_msg")

_TRUE_STRINGS = {"1", "true", "yes", "y", "on", "是", "开"}
_FALSE_STRINGS = {"0", "false", "no", "n", "off", "否", "关"}


class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:
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
    if out != out:
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
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        return ()
    out: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, Mapping) and item.get("date"):
            out.append({"date": str(item["date"]).strip(), "name": str(item.get("name", "")).strip()})
    return tuple(out)


@dataclass(frozen=True, slots=True)
class HumanoidConfig:
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

    use_llm_schedule: bool = True
    schedule_provider_name: str = ""
    schedule_fallback_provider_name: str = ""
    schedule_allow_global_fallback: bool = True
    schedule_retry_interval_seconds: int = 2
    schedule_llm_timeout_seconds: int = 60
    schedule_generation_max_attempts: int = 2
    schedule_max_slots: int = 96
    # 决策窗间隔（分钟）：动态日程每隔这么久掷一次骰子决定「生成或不生成」。
    # 15 分钟与默认粒度对齐；调大可省模型额度。
    schedule_refresh_minutes: int = 15
    # 每个决策窗在「这一段还没做完、身体也没报警」时仍然重新排一段的概率（%）：
    # 她临时改主意的频率。0 = 只在到期/身体报警时才排；100 = 每个窗都重排。
    schedule_change_chance: int = 30
    schedule_provider_cooldown_minutes: int = 30
    # 均衡参考偏好：会作为「仅供参考」的偏好递给模型，与人设和身体状态一起权衡，
    # 不是必须服从的指令。
    schedule_prompt_extra: str = "劳逸结合，有动有静，节奏均衡。"
    # 自由文本配置项，必须卡长度：它逐字进入日程生成 prompt，有人能往里面粘一整篇设定。
    SCHEDULE_PROMPT_EXTRA_MAX = 300
    schedule_time_granularity: str = "15min"
    # 日程直接按 AstrBot 当前生效的人格设定生成（v2.15 起）。关掉后 prompt 里不带入设，
    # 回到中性身份；schedule_prompt_extra 两种情况下都照带。
    schedule_use_persona: bool = True

    admin_qq: tuple[str, ...] = ()

    weather_enabled: bool = True
    weather_api_key: str = ""
    # 留空时直接用 timezone_city（需为 OpenWeather 能识别的英文名+国家码，如 Beijing,CN）。
    # v2.13.2 默认写死 Heyuan,CN，于是「她在北京」但「她那边天气是河源」，两个城市对不上。
    weather_location: str = ""
    weather_refresh_minutes: int = 60

    inject_activity_context: str = "low"
    environment_mode: str = "both"
    enable_chat_awareness: bool = True
    show_city_time_in_low_intrusion: bool = True
    timezone_city: str = DEFAULT_CITY_PLACEHOLDER

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

    social_energy_enabled: bool = True
    social_energy_consumption_per_msg: float = 0.05
    social_energy_recovery_per_minute: float = 1.5
    social_energy_reset_hour: int = 0
    social_energy_recovery_interval_seconds: int = 60

    night_mode_enabled: bool = True
    night_start_hour: int = 23
    night_end_hour: int = 6
    # 上下文里要不要写「这会儿在她的睡眠时段/最该睡的时候」这条事实。
    # 旧名 night_mode_force_sleep 的语义是「睡着时把状态说得多硬」——那已经是替她决定
    # 怎么开口了。现在只有给不给这条事实两种选择，给什么都是同一句身体描述。
    show_sleep_window: bool = True
    night_deep_sleep_ratio: float = 0.5

    debug_mode: bool = False
    holidays: tuple[dict[str, Any], ...] = ()
    state_flush_interval_seconds: int = 5
    last_interaction_threshold_minutes: int = 5
    last_interaction_mode: str = "with_last_msg"

    # v3 新增
    process_min_duration: int = 15
    process_max_duration: int = 90
    process_auto_update: bool = True
    process_history_limit: int = 5

    # v2.14 生理层（soma）
    soma_enabled: bool = True
    body_tick_seconds: int = 60
    sleep_need_hours: float = 8.0
    meal_interval_hours: float = 4.5
    sitting_discomfort_minutes: int = 90
    desire_refill_hours: float = 12.0
    weather_affects_body: bool = True
    contract_enabled: bool = True

    # v2.14.2：让「她几点起」只由夜间窗口一处决定
    schedule_follow_night_window: bool = False

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any] | None) -> HumanoidConfig:
        src: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}

        def pick(key: str) -> Any:
            return src.get(key, _MISSING)

        d = cls()

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
            schedule_max_slots=i("schedule_max_slots", 6, 96),
            schedule_refresh_minutes=i("schedule_refresh_minutes", 1, 1440),
            schedule_change_chance=i("schedule_change_chance", 0, 100),
            schedule_provider_cooldown_minutes=i("schedule_provider_cooldown_minutes", 0, 1440),
            schedule_prompt_extra=s_opt("schedule_prompt_extra")[
                : cls.SCHEDULE_PROMPT_EXTRA_MAX
            ],
            schedule_time_granularity=c("schedule_time_granularity", tuple(GRANULARITY_MINUTES)),
            schedule_use_persona=b("schedule_use_persona"),
            admin_qq=_as_str_tuple(pick("admin_qq")),
            weather_enabled=b("weather_enabled"),
            weather_api_key=s_opt("weather_api_key"),
            weather_location=s_opt("weather_location"),
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
            show_sleep_window=b("show_sleep_window"),
            night_deep_sleep_ratio=f("night_deep_sleep_ratio", 0.1, 1.0),
            debug_mode=b("debug_mode"),
            holidays=_as_mapping_tuple(pick("holidays")),
            state_flush_interval_seconds=i("state_flush_interval_seconds", 1, 60),
            last_interaction_threshold_minutes=i("last_interaction_threshold_minutes", 0, 1440),
            last_interaction_mode=c("last_interaction_mode", LAST_INTERACTION_MODES),
            process_min_duration=i("process_min_duration", 5, 60),
            process_max_duration=i("process_max_duration", 30, 240),
            process_auto_update=b("process_auto_update"),
            process_history_limit=i("process_history_limit", 0, 20),
            soma_enabled=b("soma_enabled"),
            body_tick_seconds=i("body_tick_seconds", 15, 600),
            sleep_need_hours=f("sleep_need_hours", 3.0, 14.0),
            meal_interval_hours=f("meal_interval_hours", 1.5, 12.0),
            sitting_discomfort_minutes=i("sitting_discomfort_minutes", 15, 600),
            desire_refill_hours=f("desire_refill_hours", 0.5, 72.0),
            weather_affects_body=b("weather_affects_body"),
            contract_enabled=b("contract_enabled"),
            schedule_follow_night_window=b("schedule_follow_night_window"),
        )

    # ------------------------------------------------------------------
    # 作息窗口：夜间窗口 = 她的生物钟夜，不是「回复语气开关」那么简单
    # ------------------------------------------------------------------

    @property
    def night_span_hours(self) -> float:
        """夜间窗口的长度（小时）。关掉夜间模式或 start==end 时为 0。"""
        if not self.night_mode_enabled:
            return 0.0
        start, end = self.night_start_hour, self.night_end_hour
        if start == end:
            return 0.0
        return float((end - start) % 24)

    @property
    def night_mid_hour(self) -> float:
        """夜间窗口的中点时刻。

        跨午夜时绝不能取算术平均：23→5 的平均是 14（下午两点），真实中点是凌晨 2 点。
        昼夜低谷的位置由它推出来，算错会把整条节律曲线摆到白天去。
        """
        start, end = self.night_start_hour, self.night_end_hour
        if start == end:
            return float(start)
        return ((start + (end + 24.0 if end < start else end)) / 2.0) % 24.0

    @property
    def sleep_relief_per_hour(self) -> float:
        """睡着时每小时清掉多少睡眠压力。

        定义成「睡够 sleep_need_hours 正好把一夜的困意从满清到 0」，这样那项配置
        真正管的是她睡多久算睡饱，而不是像以前那样只是个面板上的数字。
        """
        return min(34.0, max(5.0, 100.0 / max(1.0, self.sleep_need_hours)))

    @property
    def sleep_debt_gain_per_hour(self) -> float:
        """该睡的时候醒着，每小时欠下多少小时的觉。

        夜间窗口本来就比 sleep_need_hours 短的人，同样熬一小时更补不回来：窗口 6 小时
        却需要睡 8 小时的人，熬夜的代价按 8/6 放大。窗口够长时退化为 1 倍。
        """
        span = self.night_span_hours
        if span <= 0.0:
            return 1.0
        return min(3.0, max(1.0, self.sleep_need_hours / span))

    @property
    def granularity_minutes(self) -> int:
        return GRANULARITY_MINUTES.get(self.schedule_time_granularity, 15)

    @property
    def schedule_provider_ids(self) -> tuple[tuple[str, str], ...]:
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
        if self.mood_provider_name:
            return (("情绪模型", self.mood_provider_name),)
        return self.schedule_provider_ids

    def is_night_hour(self, hour: int) -> bool:
        start, end = self.night_start_hour, self.night_end_hour
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def is_deep_sleep(self, hour: int) -> bool:
        if not self.night_mode_enabled:
            return False
        start, end = self.night_start_hour, self.night_end_hour
        if start == end:
            return False
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
        return str(sender_id) in self.admin_qq

    def affection_override_for(self, qq: str) -> float | None:
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
        length = max(1, self.cycle_length)
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
        out: dict[str, Any] = {}
        for f_ in fields(self):
            value = getattr(self, f_.name)
            if f_.name == "weather_api_key":
                value = f"<已设置 {len(value)} 字符>" if value else "<未设置>"
            out[f_.name] = value
        return out


DEFAULTS = HumanoidConfig()

# 旧版本写死的天气城市。v2.14 起 weather_location 默认留空（直接用她感知的所在城市），
# 但 AstrBot 更新配置只补缺不覆盖，老用户会永远停在 Heyuan,CN 上——于是「她在北京」
# 但「她那边天气是河源」。
LEGACY_WEATHER_LOCATION = "Heyuan,CN"
# v2.16.7 之前的日程默认：时段数 16、旧偏好文案。只迁移仍停在旧默认值的用户。
LEGACY_SCHEDULE_MAX_SLOTS = 16
LEGACY_SCHEDULE_PROMPT_EXTRA = "休闲日常，愉快的生活。"


def plan_default_migrations(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """算出需要一次性提升的旧默认值。

    AstrBot 的 `check_config_integrity` 只在键缺失时插默认值，已有值一律保留，所以调
    默认值对老用户完全无效。这里只处理「当前值仍等于旧版本默认值」的项：用户自己改过
    的值一律不碰。
    """
    src: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    changes: dict[str, Any] = {}
    weather = str(src.get("weather_location") or "").strip()
    city = str(src.get("timezone_city") or "").strip()
    if weather == LEGACY_WEATHER_LOCATION and city and city != DEFAULT_CITY_PLACEHOLDER:
        # 她改了所在城市却没改天气城市（也改不了，以前没人提示这两处要对上）：
        # 清空天气城市，让它跟着所在城市走。
        changes["weather_location"] = ""
    # v2.16.4：作息由人设决定，身体跟着她的日程走。只动「从没碰过作息设置」的人
    # （窗口还是出厂那对 23/6 且对齐开着）——真去改过夜间窗口的人大概正是要锁作息，不碰。
    if (
        src.get("schedule_follow_night_window") is True
        and src.get("night_start_hour") == 23
        and src.get("night_end_hour") == 6
    ):
        changes["schedule_follow_night_window"] = False
    # v2.16.7：日程改为 15 分钟粒度、模型填表覆盖全天的默认。只动仍停在旧默认的项。
    if src.get("schedule_max_slots") == LEGACY_SCHEDULE_MAX_SLOTS:
        changes["schedule_max_slots"] = 96
    if str(src.get("schedule_prompt_extra") or "").strip() == LEGACY_SCHEDULE_PROMPT_EXTRA:
        changes["schedule_prompt_extra"] = DEFAULTS.schedule_prompt_extra
    return changes


class ConfigBox:
    """可热重载的配置持有者。

    插件里所有地方都拿 `lambda: box()` 读配置，/重载配置 只需换掉内部实例就全链路生效。
    v2.13.2 里 main.py 自有一份 `self._config`，engine 又拿原始 dict 自己重新解析，
    于是 `HumanoidEngine.reload_config()` 看起来能热重载、实际影响不到任何行为，而且
    `environment_allows()` 这种每条消息都会走的方法在每条消息上重新解析一遍 90 个字段。
    """

    __slots__ = ("_raw", "_value")

    def __init__(self, raw: Mapping[str, Any] | None) -> None:
        self._raw = raw
        self._value = HumanoidConfig.from_raw(raw)

    def __call__(self) -> HumanoidConfig:
        return self._value

    @property
    def value(self) -> HumanoidConfig:
        return self._value

    @property
    def raw(self) -> Mapping[str, Any] | None:
        return self._raw

    def reload(self, raw: Mapping[str, Any] | None = None) -> HumanoidConfig:
        if raw is not None:
            self._raw = raw
        self._value = HumanoidConfig.from_raw(self._raw)
        return self._value