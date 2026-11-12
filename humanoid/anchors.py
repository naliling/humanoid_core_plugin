"""由头 → 证据卡：把「她今天确实有的事」翻成社交层能直接说的卡。

Core 的契约里，她今天的事实散在 `day`（doing / done / wants / dreams）、`ongoing`、
`event`、`weather`、`holiday` 里，形状各不相同（字符串 / 列表 / 块字典）。社交层
每处都要猜一遍形状，猜错就静默少一条由头。这里把它们统一翻成
`humanoid.opportunity` 定义的卡片，只做「翻译 + 打出处 + 定有效期」，不做判断。

第二层是 `life_material` 那 78 条「她这样的人可能做的事」：它们不是既成事实，
是「还没发生、但她真可能做」的事。社交层拿它问「要不要去楼下走走」，比拿一条
假事实开口更像人。

**这里不读盘、不写盘**：契约由 `link.build_contract` 传进来，时钟由调用方传进来
（不给就取 `core.clock.now()`）。每一步都单独兜住异常：一条坏卡、一次取不到，
都不该让整份契约生成失败——Core 崩了，社交层就整片瞎掉。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Mapping

from . import clock as _clock
from . import opportunity
from .services import life_material
from .services.weather import notable_weather

#: 卡片出处（中文标签）。社交层可以直接把它念出来，也方便排查一张卡从哪来。
SOURCE_DAY = "日程"
SOURCE_ONGOING = "未完事项"
SOURCE_WANT = "想做的事"
SOURCE_EVENT = "刚发生"
SOURCE_WEATHER = "天气"
SOURCE_HOLIDAY = "节日"
SOURCE_DREAM = "梦境"
SOURCE_GREETING = "问候时机"


def _texts(raw: Any, limit: int = 4) -> list[str]:
    """从字符串 / 列表里取干净的非空文本（旧契约可能给 None 或单个字符串）。"""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        text = " ".join(str(item or "").split())
        if text and text not in out:
            out.append(text)
    return out[:limit]


def _card(
    kind: str,
    text: Any,
    source: str,
    ts: float,
    *,
    ttl: Any = None,
    detail: str = "",
) -> dict[str, Any] | None:
    """造一张卡；造不出来就返回 None（单条丢弃，不影响其它卡）。"""
    try:
        return opportunity.make_card(
            kind=kind,
            title=text,
            detail=detail,
            source=source,
            captured_at=ts,
            ttl=ttl,
        )
    except (TypeError, ValueError):
        return None


def cards_from_contract(contract: Any, *, now: Any) -> list[dict[str, Any]]:
    """把契约里的既成事实翻成卡（顺序稳定，不依赖字典遍历顺序）。"""
    ts = opportunity.to_epoch(now)
    if not isinstance(contract, Mapping):
        return []
    cards: list[dict[str, Any] | None] = []
    day = contract.get("day") if isinstance(contract.get("day"), dict) else {}

    # 正在做的那件事：只在「这一段」里成立，所以用 activity 的短 TTL。
    doing = " ".join(str(day.get("doing") or "").split())
    if doing:
        cards.append(_card("activity", doing, SOURCE_DAY, ts))

    # 今天已经做过的事：这是「我刚从 X 回来」最自然的原料。
    for text in _texts(day.get("done")):
        cards.append(_card("life", text, SOURCE_DAY, ts))

    # 今天想做的事（日程没标 wants 时由 plans 兜底，见 schedule._wants_from_plans）。
    for text in _texts(contract.get("wants"), 3):
        cards.append(_card("life", text, SOURCE_WANT, ts))

    # 还没做完的事：最像人的一种主动开口——说自己手上真在干的事。
    for text in _texts(contract.get("ongoing"), 3):
        cards.append(_card("reminder", text, SOURCE_ONGOING, ts))

    # 刚发生的重事件（被夸 / 被骂 / 被敷衍）：有时效，两小时。
    event = contract.get("event") if isinstance(contract.get("event"), dict) else {}
    phrase = " ".join(str(event.get("phrase") or "").split())
    if phrase:
        cards.append(_card("life", phrase, SOURCE_EVENT, ts))

    # 今天最近一段睡眠的梦：中午之后再说「我昨晚梦见」就开始假了。
    for text in _texts(day.get("dreams"), 2):
        cards.append(_card("journal", text, SOURCE_DREAM, ts))

    holiday = " ".join(str(contract.get("holiday") or "").split())
    if holiday:
        cards.append(_card("holiday", holiday, SOURCE_HOLIDAY, ts))

    # 天气：只有「变天」才成卡（v3.1.38）。契约侧 `link._contract_weather` 已经
    # 过了一道 `notable_weather`，这里再判一次是因为 `cards_from_contract` 也会被
    # 直接喂一份手搓契约（测试、诊断、别的插件），不能指望上游一定洗过。
    weather = " ".join(str(contract.get("weather") or "").split())
    if weather and notable_weather(weather):
        cards.append(_card("weather", weather, SOURCE_WEATHER, ts))

    return [card for card in cards if card]


def _persona_text(core: Any) -> str:
    """人设原文（同步读缓存）：素材里的工作 / 宠物 / 照护 / 社交条目要靠它筛。"""
    getter = getattr(core, "_persona_text", None)
    if not callable(getter):
        return ""
    try:
        return str(getter() or "")
    except Exception:
        return ""


def _role_materials(core: Any) -> list[dict[str, Any]]:
    """角色专属素材（按人设生成过才有）。只读，拿不到就当没有。"""
    scope = getattr(core, "scope", None)
    getter = getattr(scope, "get_self", None)
    if not callable(getter):
        return []
    try:
        stored = getter("role_materials")
    except Exception:
        return []
    if not isinstance(stored, list):
        return []
    return [dict(item) for item in stored if isinstance(item, Mapping)]


def _species_profile(core: Any) -> dict[str, Any]:
    """她的物种档案（`{"kind": "beast", "traits": ["鳞片"], …}`）；读不到就是空表。

    v3.1.37 补的漏点：这条链路（社交层证据卡）原来调 `materials_for` 时根本没传
    species，日程那条筛了、证据卡这条没筛，同一批「把门口的鞋摆整齐」照样能递到
    小马面前。传整份档案而不只是 kind，是因为「龙」和「狼」都是 beast：只有 traits
    里那个「鳞片」能告诉素材该说鳞片而不是皮毛。
    """
    try:
        profile = core.species_profile()
    except Exception:
        return {}
    if not isinstance(profile, Mapping):
        return {}
    return dict(profile)


def _used_materials(core: Any, now_ts: float) -> set[str]:
    """近 7 天已经用过的素材键（card_id / anchor_key）。

    读不到就返回空集合 = 不过滤：去重是为了少重复，不该因为一条读盘失败就让
    她今天一条素材都没有。
    """
    scope = getattr(core, "scope", None)
    getter = getattr(scope, "get_self", None)
    if not callable(getter):
        return set()
    try:
        records = getter(life_material.MATERIALS_USED_KEY, [])
    except Exception:
        return set()
    return life_material.used_keys(records, now_ts)


def _segment_text(contract: Any) -> str:
    """当前日程段在做什么（契约 activity 块，取不到就退 day.doing）。"""
    if not isinstance(contract, Mapping):
        return ""
    activity = contract.get("activity")
    if isinstance(activity, Mapping):
        text = str(activity.get("schedule_event") or activity.get("name") or "").strip()
        if text:
            return text
    day = contract.get("day")
    if isinstance(day, Mapping):
        return str(day.get("doing") or "").strip()
    return ""


def _asleep(contract: Any) -> bool:
    """契约里标了深睡没有（没有这个键就当醒着）。"""
    if not isinstance(contract, Mapping):
        return False
    time_block = contract.get("time")
    if not isinstance(time_block, Mapping):
        return False
    return bool(time_block.get("is_deep_sleep"))


def material_cards(
    core: Any,
    contract: Any,
    *,
    now: Any,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """把生活素材翻成卡。

    种子只取「角色 + 日期」、钟点按小时取整：同一天同一时段两次重建拿到的素材
    完全相同——契约是「变了才重写」，素材每次抖一下就等于每分钟重写 state.json。

    读 materials_used 过滤近 7 天用过的（读不到就不过滤）；当前日程段是上班 /
    午休 / 睡觉时，把冲突的素材档拿掉（见 life_material.segment_kind）。
    """
    ts = opportunity.to_epoch(now)
    if isinstance(now, _dt.datetime):
        moment = now
    else:
        moment = _clock.local(ts)
    minute = int(moment.hour) * 60
    day = contract.get("day") if isinstance(contract, Mapping) and isinstance(contract.get("day"), dict) else {}
    recent = _texts(day.get("done"), 8) + _texts(day.get("doing"), 1)
    if isinstance(contract, Mapping):
        recent += _texts(contract.get("ongoing"), 3)
    seed = [str(getattr(core, "role_id", "") or ""), moment.strftime("%Y-%m-%d")]
    used = _used_materials(core, ts)
    species = _species_profile(core)
    try:
        items = life_material.materials_for(
            seed,
            _persona_text(core),
            now_minute=minute,
            recent_events=recent,
            count=max(1, int(limit)),
            extra=_role_materials(core),
            used=used,
            segment=_segment_text(contract),
            asleep=_asleep(contract),
            species=species,
        )
        return life_material.materials_as_cards(
            items, captured_at=ts, seed=seed, skip=used, species=species
        )
    except Exception:
        return []


def contract_blocks(core: Any, contract: Any, now: Any = None) -> dict[str, list[dict[str, Any]]]:
    """给 `link.build_contract` 用的一行挂载：返回契约里那两个新键。

    返回形状固定是 `{"opportunities": [...], "materials": [...]}`，两个键永远都在
    （没有卡就是空列表）：社交层按「键在不在」判新旧版本，缺键与空列表是两件事。
    """
    if now is None:
        clock = getattr(core, "clock", None)
        getter = getattr(clock, "now", None)
        try:
            now = getter() if callable(getter) else None
        except Exception:
            now = None
        if now is None:
            now = _dt.datetime.now()
    ts = opportunity.to_epoch(now)
    try:
        opportunities = cards_from_contract(contract, now=ts)
    except Exception:
        opportunities = []
    try:
        materials = material_cards(core, contract, now=now, limit=opportunity.MATERIAL_LIMIT)
    except Exception:
        materials = []
    return opportunity.contract_blocks(opportunities, materials, ts)


#: 问候窗口（与 social 侧 greeting_windows() 的默认值对齐）：左闭右开，
#: 24 表示午夜 0 点。窗口只说明「这个点开口不算突兀」，不代表对方刚醒/刚吃完饭。
GREETING_WINDOWS: tuple[tuple[str, int, int, str], ...] = (
    ("morning", 7, 11, "早安窗口"),
    ("midday", 12, 14, "午间窗口"),
    ("night", 21, 24, "晚安窗口"),
)

_GREETING_DETAIL = (
    "「{label}」窗口 {start:02d}:00-{end:02d}:00：窗口只提供时机，"
    "不代表对方刚醒；说什么由她自己定。"
)


def _offset_minutes(block: Any) -> int | None:
    """契约里的时区偏移（分钟）；拿不到或不可信就返回 None。"""
    if not isinstance(block, Mapping):
        return None
    raw = block.get("utc_offset_minutes")
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if abs(value) > 24 * 60:
        return None
    return value


def _parse_local(block: Any) -> _dt.datetime | None:
    """契约里 time.local 的本地时刻；格式不对就当没有。"""
    if not isinstance(block, Mapping):
        return None
    raw = block.get("local")
    if not isinstance(raw, str):
        return None
    try:
        return _dt.datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None


def _local_moment(contract: Any, now: Any) -> _dt.datetime | None:
    """把「此刻」换成本地时间：优先用调用方给的时钟，其次契约里的 time.local。"""
    if isinstance(now, _dt.datetime):
        return now.replace(tzinfo=None)
    block = contract.get("time") if isinstance(contract, Mapping) else None
    moment = None
    if isinstance(now, (int, float)) and not isinstance(now, bool):
        offset = _offset_minutes(block)
        try:
            stamp = float(now)
        except (TypeError, ValueError):
            stamp = 0.0
        # 时间戳只接受「像样的秒数」：NaN/inf/负数换算出来是假时刻。
        if 0.0 < stamp < 1e12:
            try:
                if offset is not None:
                    moment = _clock.local(
                        stamp + offset * 60.0, _dt.timezone.utc
                    ).replace(tzinfo=None)
                else:
                    moment = _clock.local(stamp)
            except (OverflowError, OSError, ValueError):
                moment = None
    if moment is not None:
        return moment
    parsed = _parse_local(block)
    if parsed is not None:
        return parsed
    return _dt.datetime.now()


def _greeting_card(
    name: str, title: str, start: int, end: int, moment: _dt.datetime, ts: float
) -> dict[str, Any] | None:
    """造一张问候卡；窗口已经过去（剩余秒数 <= 0）就返回 None。"""
    base = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if end >= 24:
        finish = base + _dt.timedelta(days=1)
    elif end <= start:
        finish = base + _dt.timedelta(days=1, hours=end)
    else:
        finish = base + _dt.timedelta(hours=end)
    seconds = (finish - moment).total_seconds()
    if seconds <= 0:
        return None
    label = title[:2] if title else name
    detail = _GREETING_DETAIL.format(label=label, start=start, end=end % 24)
    return opportunity.make_card(
        kind="greeting",
        title=title,
        detail=detail,
        source=SOURCE_GREETING,
        captured_at=ts,
        expires_at=ts + seconds,
        purpose="greeting",
        anchor_key=opportunity.anchor_key_for(
            "greeting", moment.strftime("%Y-%m-%d") + "-" + name
        ),
    )


def greeting_opportunities(contract: Any, *, now: Any = None) -> list[dict[str, Any]]:
    """把「此刻正处在问候窗口」翻成一张 kind=greeting 的卡（0 或 1 张）。

    窗口只提供**时机**，不提供**内容**：detail 里明说「不代表对方刚醒」，免得社交层
    把 7 点的窗口读成「她刚醒」。卡片有效期取窗口结束——11 点还说「早安」就是假话。
    同一天同一窗口的 anchor_key 固定，社交层据此记账、不会一天问三遍早安。

    `contract` 就是 `scope.get_self(contract)` 那份字典，脏数据/None/空字典一律返回
    空列表：问候卡是锦上添花，绝不能让它把整份契约生成搞挂。
    """
    try:
        moment = _local_moment(contract, now)
    except Exception:
        return []
    if moment is None:
        return []
    ts = opportunity.to_epoch(now)
    if ts <= 0:
        try:
            ts = moment.timestamp()
        except (OverflowError, OSError, ValueError):
            ts = 0.0
    hour = int(moment.hour)
    for name, start, end, title in GREETING_WINDOWS:
        inside = (
            (start <= hour < end) if start < end else (hour >= start or hour < end)
        )
        if not inside:
            continue
        try:
            card = _greeting_card(name, title, start, end, moment, ts)
        except (TypeError, ValueError, OverflowError, OSError):
            card = None
        return [card] if card else []
    return []

