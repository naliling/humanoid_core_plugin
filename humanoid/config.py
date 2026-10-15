"""插件配置：唯一的默认值来源 + 类型强制 + 范围钳制。"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from typing import Any

from .data.cities import DEFAULT_CITY_PLACEHOLDER
from .data.holidays import canonical_date

logger = logging.getLogger(__name__)

GRANULARITY_MINUTES: dict[str, int] = {
    "flexible": 0,
    "5min": 5,
    "10min": 10,
    "15min": 15,
    "20min": 20,
    "30min": 30,
    "hourly": 60,
}

INJECT_MODES = ("medium", "full", "mood_only")
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


def _phase_multipliers(value: Any, default: tuple[float, ...]) -> tuple[float, ...]:
    """周期恢复倍率：长度不等于阶段数（8）时按默认值走。

    老版本是 6 段，本版索引语义改成了 8 段（月经/卵泡早中晚/排卵/黄体早中晚）。
    旧列表留着会**静默错位**：面板显示 8 段顺序，代码却按旧顺序取值，
    中间几段的恢复倍率对不上。长度对不上就整体用新默认（6 个数本来就写不出 8 段）。
    """
    out = _as_float_tuple(value, default)
    return out if len(out) == len(default) else default


def _date_entry(item: Any) -> dict[str, str] | None:
    """把日期列表里的一项解析成 `{"date": …, "name": …}`；认不出来返回 None。

    两种形态都收：Mapping（带 `templates` 的列表编辑器产出的正常形状），
    以及**纯字符串**——面板的 list 编辑器在没有 `templates` 时产出的就是一串字符串，
    而 `holidays` 的 hint 一直教用户「日期写 MM-DD」，所以这是真实可达的输入。
    字符串支持 `05-20`、`2026-05-20`，日期后面可以用空格或冒号跟一个名称。
    """
    if isinstance(item, Mapping):
        raw_date = item.get("date", "")
        name = str(item.get("name", "") or "").strip()
    else:
        text = str(item or "").strip()
        if not text:
            return None
        head, sep, tail = text.partition(":")
        if not sep:
            parts = head.split(None, 1)
            head = parts[0] if parts else ""
            tail = parts[1] if len(parts) > 1 else ""
        raw_date, name = head, tail.strip()
    full, month_day = canonical_date(raw_date)
    if not (full or month_day):
        return None
    # 存归一化后的写法：`6-1` → `06-01`，下游 resolve_holiday 再用一次 canonical 也不会走样。
    return {"date": full or month_day, "name": name}


def _as_mapping_tuple(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        return ()
    out: list[dict[str, Any]] = []
    for item in value:
        entry = _date_entry(item)
        if entry is None:
            if item not in ("", None):
                # 原来是**静默丢弃**：用户按 hint 填了日期却完全不生效，日志里一个字都没有。
                logger.warning("日期列表项无法解析，已忽略：%r", item)
            continue
        out.append(entry)
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
    energy_recovery_phase_multipliers: tuple[float, ...] = (
        0.5, 1.0, 1.6, 1.9, 2.0, 1.1, 0.9, 0.7
    )

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
    # 日程两次调模型之间的间隔（分钟）：每次生成后在这个区间里**随机**抽一个
    # 「下次最早可再排」的时间。随机是为了让节奏像「随机发生的事」，而不是固定
    # 周期的计时器（这是拟人插件，不该按表打卡）。两项相等 = 固定间隔。
    schedule_min_interval_minutes: int = 30
    schedule_max_interval_minutes: int = 60
    # 多久没人发消息就停掉后台的模型调用（分钟）。没人看着时，背景日程不值得
    # 每次都去问模型；用户一说话立刻恢复。0 = 不静默。
    llm_idle_silence_minutes: int = 60
    # 每日调用预算（次）：所有 bot 共享一个自然日计数，防止多开几个 bot 撞穿额度。
    # 情绪分析不计入。0 = 不限。
    llm_daily_call_budget: int = 200
    # 均衡参考偏好：会作为「仅供参考」的偏好递给模型，与人设和身体状态一起权衡，
    # 不是必须服从的指令。
    # ↓ 面板最后一项。默认关。
    ccb: bool = False
    schedule_prompt_extra: str = "劳逸结合，有动有静，节奏均衡。"
    # 生成日程时人设最多注入多少字。
    #
    # 原来写死 1200。人设里写着外貌、物种、年纪、偏好——这些正是决定日程长什么样的
    # 东西，写得越细越容易被砍掉尾巴，于是模型只看到「会说话的小马」，看不到
    # 「18 岁、有主见、喜欢夜里出去」，排出来的东西退回通用模板。
    #
    # 调大会让请求变大（日程一天跑 1~2 次），所以留成配置项而不是无限放开；
    # 人设再长也只到这里，超出部分照常按行边界截断。
    schedule_persona_max_chars: int = 3000
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

    # 多消息合并（防抖）：连续触发机器人的多条消息，合并成一次模型请求、只回一条。
    # 判定「说完了吗」用规则（humanoid/merge.py）而不是固定时间窗：句末有标点/语气词的
    # 零延迟放行，只有像「今天真的好累啊」这种拿不准的才等一句。整体做法参考
    # astrbot_plugin_debounce，但它靠 BERT 模型（要 onnxruntime+transformers+modelscope
    # 四个依赖、运行时再从 ModelScope 拉模型），本机实测装那几个包跑了 5 分钟没装完，
    # 拉不到模型还会静默失效，所以不引入。
    message_merge_enabled: bool = True
    # 输入状态（正在输入）。
    #
    # 回复期间向 QQ 报「正在输入」，做法照市场插件 astrbot_plugin_input_state_by_nc：
    # 只在 NapCat（aiocqhttp）私聊里生效，其他平台/群聊自动跳过。
    # QQ 的输入状态约 8~10 秒自己会消失，所以要按 interval 高频重报；
    # timeout 是兜底：万一响应钩子没回来，别让「正在输入」一直闪下去。
    input_state_enabled: bool = True
    input_state_interval_seconds: float = 0.5
    input_state_timeout_seconds: float = 120.0
    # 规则判不出来时（没标点、长度中等）最多等这么久，等后续那句一起并进来。
    # 0 = 不等待，等于只看标点。
    message_merge_timeout_seconds: float = 3.0
    # 已经说完的消息也再等一小拍，看有没有连发。
    #
    # 原来「句末有标点/语气词」是零延迟直接发的——那对「一句话」确实最快，
    # 但用户快速连发三条完整消息时，第一条立刻就被放走了，时机已经错过，
    # 后两条只能各自回一次，防抖等于没开。所以完整消息也要等一个短确认窗口：
    # 窗口内没有新消息就发（只多这么多延迟），有新消息就把它们并成一条。
    # 0 = 恢复到旧行为（完整消息零延迟）。
    message_merge_settle_seconds: float = 1.5
    # 一批最多合并多少条：防刷屏把 prompt 撑爆，只保留最近这么多条。
    message_merge_max_count: int = 6

    inject_activity_context: str = "medium"
    # 注入的 Token 硬预算：超了按显著度从低到高丢句，必需品（时间/称呼/场景）永不丢。
    inject_token_budget: int = 3500
    environment_mode: str = "both"
    enable_chat_awareness: bool = True
    show_city_time_in_low_intrusion: bool = True
    timezone_city: str = DEFAULT_CITY_PLACEHOLDER

    # 穿搭（humanoid/outfit.py）：给她一件贴身的衣服。
    # 手写预设（outfit_preset / 按角色覆盖）优先；没有就按识别到的物种取内置默认；
    # 两者都没有（判不出她是什么）→ **什么都不注入**（宁缺勿错）。
    outfit_enabled: bool = True
    outfit_preset: str = ""
    # 要不要按场景调（睡觉→睡衣、洗澡→不穿、出门冷天→加外套、生理期/不适→宽松）
    outfit_scene_variation: bool = True

    mood_enabled: bool = True
    mood_provider_name: str = ""
    mood_sensitivity: int = 60
    mood_decay_hours: float = 6.0
    mood_initial_affection: int = 35
    # ccb 的四个门槛，与 ccb.py 里的同名常量保持一致（schema 有测试钉住）。
    # 面板上不解释它们是什么：那是用户自己去试的事，写清楚反而是替他们做判断。
    ccb_libido_min: float = 38.0
    ccb_libido_rise: float = 4.0
    ccb_affection_min: float = 50.0
    ccb_affection_rise: float = 8.0
    ccb_affection_deep: float = 75.0
    mood_initial_libido: int = 34
    mood_initial_aggression: int = 28
    mood_affection_override: tuple[str, ...] = ()
    mood_affection_delta_cap: int = 2
    # v2.27.8：一句孤立的难听话只动心情不动好感，连着说才扣（A12）。
    mood_negative_streak_required: int = 2
    # v2.27.8：底部减速 + 情绪韧性（A7/A11）。
    mood_negative_softening: bool = True
    # v2.27.8：恢复侧——道歉回温 + 低于基线时暖话加成 + 连着哄加速消气（B1/B2/B3）。
    mood_recovery_bonus: bool = True
    # v2.27.8：冷静期（D）——好感跌破门槛后她带情绪冷着，并在这段时间里不主动找 TA。
    cooling_enabled: bool = True
    cooling_affection: int = 10
    cooling_minutes: int = 15
    cooling_no_proactive_days: int = 3
    mood_log_enabled: bool = True
    mood_log_max_entries: int = 28
    mood_log_threshold_affection: int = 3
    mood_log_threshold_libido: int = 2
    # 原为 1：单次 delta 上限是 2，所以每一条负面消息的攻击性变化都跨过这个门槛，
    # 情绪日志被单条对话刷满，而 last_emotional_event 只看最新一条——于是「今天TA惹她
    # 不痛快了」会连续挂一整天。提到 3 后只有真的明显波动才记。
    mood_log_threshold_aggression: int = 3
    mood_update_timeout: float = 120.0
    mood_tag_enabled: bool = True
    mood_use_llm_for_delta: bool = True
    mood_provider_cooldown_minutes: int = 5
    mood_llm_interval_messages: int = 10
    # 攒到间隔就触发时，一次最多把这么多条合起来分析（旧的在前）
    mood_llm_batch_max: int = 10
    # 每条截多少字进分析（10 条加起来仍可控）
    mood_llm_message_chars: int = 300
    mood_verbose_log: bool = False
    mood_enabled_in_group: bool = False
    mood_data_retention_days: int = 7
    # 自动认定称呼：用户还没设过称呼时，从消息带的名字（群聊优先群名片）认一次怎么叫 TA。
    # 只填空白、门槛很严、认过之后永不改动——用户 /叫我 设的更不会被它碰。
    auto_nickname: bool = True
    # 自动识别用户：开着就把**这条消息的发送者名**（群名片 / QQ 昵称）取来当对方的标识，
    # 注入里用真名区分「她」和「你」；关着统一写「TA」。默认关。
    auto_identify_user: bool = False

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
    state_flush_interval_seconds: int = 60
    last_interaction_threshold_minutes: int = 5
    last_interaction_mode: str = "with_last_msg"

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
            energy_recovery_phase_multipliers=_phase_multipliers(
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
            schedule_min_interval_minutes=i("schedule_min_interval_minutes", 0, 1440),
            schedule_max_interval_minutes=i("schedule_max_interval_minutes", 0, 1440),
            llm_idle_silence_minutes=i("llm_idle_silence_minutes", 0, 1440),
            llm_daily_call_budget=i("llm_daily_call_budget", 0, 1_000_000),
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
            message_merge_enabled=b("message_merge_enabled"),
            input_state_enabled=b("input_state_enabled"),
            input_state_interval_seconds=f("input_state_interval_seconds", 0.1, 10.0),
            input_state_timeout_seconds=f("input_state_timeout_seconds", 10.0, 600.0),
            message_merge_timeout_seconds=f("message_merge_timeout_seconds", 0.0, 30.0),
            message_merge_settle_seconds=f("message_merge_settle_seconds", 0.0, 10.0),
            message_merge_max_count=i("message_merge_max_count", 1, 50),
            inject_activity_context=c("inject_activity_context", INJECT_MODES),
            # 这一项曾经是 113 个字段里唯一不走 i()/f() 的（直接 int(src.get(...))），
            # 面板里填个 'abc' 或 '3000.0' 就会抛 ValueError 一路冒到 initialize()，
            # 插件加载失败，而坏值还躺在配置文件里 —— 每次重启再崩一次。
            schedule_persona_max_chars=i("schedule_persona_max_chars", 200, 8000),
            inject_token_budget=i("inject_token_budget", 200, 20000),
            environment_mode=c("environment_mode", ENVIRONMENT_MODES),
            enable_chat_awareness=b("enable_chat_awareness"),
            show_city_time_in_low_intrusion=b("show_city_time_in_low_intrusion"),
            timezone_city=s("timezone_city"),
            outfit_enabled=b("outfit_enabled"),
            outfit_preset=s_opt("outfit_preset")[:200],
            outfit_scene_variation=b("outfit_scene_variation"),
            mood_enabled=b("mood_enabled"),
            mood_provider_name=s_opt("mood_provider_name"),
            mood_sensitivity=i("mood_sensitivity", 0, 100),
            mood_decay_hours=f("mood_decay_hours", 0.1, 720.0),
            mood_initial_affection=i("mood_initial_affection", 0, 100),
            ccb_libido_min=f("ccb_libido_min", 0.0, 50.0),
            ccb_libido_rise=f("ccb_libido_rise", 0.0, 20.0),
            ccb_affection_min=f("ccb_affection_min", 0.0, 100.0),
            ccb_affection_rise=f("ccb_affection_rise", 0.0, 50.0),
            ccb_affection_deep=f("ccb_affection_deep", 0.0, 100.0),
            mood_initial_libido=i("mood_initial_libido", 0, 50),
            mood_initial_aggression=i("mood_initial_aggression", 0, 50),
            mood_affection_override=_as_str_tuple(pick("mood_affection_override")),
            mood_affection_delta_cap=i("mood_affection_delta_cap", 1, 10),
            mood_negative_streak_required=i("mood_negative_streak_required", 1, 10),
            mood_negative_softening=b("mood_negative_softening"),
            mood_recovery_bonus=b("mood_recovery_bonus"),
            cooling_enabled=b("cooling_enabled"),
            cooling_affection=i("cooling_affection", 0, 100),
            cooling_minutes=i("cooling_minutes", 1, 1440),
            cooling_no_proactive_days=i("cooling_no_proactive_days", 0, 30),
            mood_log_enabled=b("mood_log_enabled"),
            mood_log_max_entries=i("mood_log_max_entries", 1, 1000),
            mood_log_threshold_affection=i("mood_log_threshold_affection", 0, 100),
            mood_log_threshold_libido=i("mood_log_threshold_libido", 0, 50),
            mood_log_threshold_aggression=i("mood_log_threshold_aggression", 0, 50),
            mood_update_timeout=f("mood_update_timeout", 5.0, 600.0),
            mood_tag_enabled=b("mood_tag_enabled"),
            mood_use_llm_for_delta=b("mood_use_llm_for_delta"),
            mood_provider_cooldown_minutes=i("mood_provider_cooldown_minutes", 0, 1440),
            ccb=bool(src.get("ccb", False)),
            mood_llm_interval_messages=i("mood_llm_interval_messages", 1, 100),
            mood_llm_batch_max=i("mood_llm_batch_max", 1, 30),
            mood_llm_message_chars=i("mood_llm_message_chars", 50, 2000),
            mood_verbose_log=b("mood_verbose_log"),
            mood_enabled_in_group=b("mood_enabled_in_group"),
            mood_data_retention_days=i("mood_data_retention_days", 0, 365),
            auto_nickname=b("auto_nickname"),
            auto_identify_user=b("auto_identify_user"),
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
        """把周期第几天映到 8 个阶段之一（按 28 天标准轴缩放，返回值 0..7）。

        阶段划分（标准 28 天）：
          1-5 月经期 / 6-8 卵泡早期 / 9-11 卵泡中期 / 12-13 卵泡晚期 /
          14 排卵期 / 15-19 黄体早期 / 20-22 黄体中期 / 23-28 黄体晚期。
        cycle_length 不是 28 时按比例缩放到这条标准轴上，所以自定义周期照样能用。
        """
        length = max(1, self.cycle_length)
        day = ((int(cycle_day) - 1) % length) * 28.0 / length + 1
        for upper, idx in ((5, 0), (8, 1), (11, 2), (13, 3), (14, 4), (19, 5), (22, 6)):
            if day <= upper:
                return idx
        return 7

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
# v2.27.2 之前的初始好感默认值。46 起步偏亲（叠加当时的单句 2 分收益，“几个指令”就能起两度）；
# 改成中性起点 35，只动仍停在 46 的配置——自己改过的一律不碰。
LEGACY_MOOD_INITIAL_AFFECTION = 46


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
    # v2.18.2：注入档位 low 升格为 medium（插件已不是轻量级）。只动仍停在旧默认的项，
    # 用户自己改成 full/mood_only 的不碰。
    if str(src.get("inject_activity_context") or "").strip() == "low":
        changes["inject_activity_context"] = "medium"
    # v2.27.2：初始好感默认 46 → 35。只动仍停在 46 的项——手动设过 46 以外值的不碰。
    # **用户现有的好感数据（state.json）一律不动**，这条只影响新档案的起点。
    if src.get("mood_initial_affection") == LEGACY_MOOD_INITIAL_AFFECTION:
        changes["mood_initial_affection"] = DEFAULTS.mood_initial_affection
    return changes


class ConfigBox:
    """可热重载的配置持有者。

    插件里所有地方都拿 `lambda: box()` 读配置，/拟人 重载 只需换掉内部实例就全链路生效。
    v2.13.2 里 main.py 自有一份 `self._config`，engine 又拿原始 dict 自己重新解析，
    于是 `HumanoidEngine.reload_config()` 看起来能热重载、实际影响不到任何行为，而且
    `environment_allows()` 这种每条消息都会走的方法在每条消息上重新解析一遍 90 个字段。
    """

    __slots__ = ("_raw", "_value", "_load_error")

    def __init__(self, raw: Mapping[str, Any] | None) -> None:
        self._raw = raw
        self._load_error = ""
        self._value = self._parse(raw)

    def _parse(self, raw: Mapping[str, Any] | None) -> HumanoidConfig:
        """解析配置，**任何异常都不许把插件带崩**。

        113 个字段现在全部走 `i()/f()/b()/s()/c()` 这些带兜底的助手，正常抛不出来。
        这里再兜一层，是因为代价不对等：异常会顺着 `main.py` 的 `initialize()` 冒到
        AstrBot，结果是**整个插件加载失败**，而坏值还躺在配置文件里 —— 每次重启都再崩
        一次，用户只能手改 JSON 才能恢复。宁可退回默认值，并在日志里喊一声。
        """
        try:
            self._load_error = ""
            return HumanoidConfig.from_raw(raw)
        except Exception as exc:  # noqa: BLE001 - 兜底就是为了不让插件起不来
            self._load_error = f"{type(exc).__name__}: {exc}"
            logger.error(
                "配置解析失败，已整体退回默认值，请检查配置项：%s", self._load_error, exc_info=True
            )
            return HumanoidConfig()

    def __call__(self) -> HumanoidConfig:
        return self._value

    @property
    def value(self) -> HumanoidConfig:
        return self._value

    @property
    def load_error(self) -> str:
        """上次解析失败的原因；空串表示正常。`/拟人 诊断` 用它告诉用户配置有问题。"""
        return self._load_error

    @property
    def raw(self) -> Mapping[str, Any] | None:
        return self._raw

    def reload(self, raw: Mapping[str, Any] | None = None) -> HumanoidConfig:
        if raw is not None:
            self._raw = raw
        self._value = self._parse(self._raw)
        return self._value