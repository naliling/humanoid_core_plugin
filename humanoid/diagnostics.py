"""`/拟人诊断` 的报告生成。"""

from __future__ import annotations

from typing import Any

from .config import HumanoidConfig
from .clock import DEFAULT_CITY_PLACEHOLDER, weekday_cn, format_offset
from .slots import parse_time
from .llm import (
    GLOBAL_LABEL,
    PURPOSE_MOOD as MOOD_PURPOSE,
    PURPOSE_SCHEDULE as SCHEDULE_PURPOSE,
    LLMGateway,
    ProviderResolver,
)

OK_MARK = "✓"
BAD_MARK = "✗"
WARN_MARK = "⚠"


def _signals_line(signals: dict) -> str:
    """社交层信号那一行：把「没读到」拆成三种完全不同的情况说。"""
    state = str(signals.get("state") or "")
    path = str(signals.get("path") or "")
    tail = f"（看的是 {path}）" if path else ""
    if state == "fresh":
        age = signals.get("age")
        proactive = signals.get("last_proactive_age", -1)
        extra = (
            f"上次刷新 {age:.0f}s 前；上次主动开口 {proactive:.0f}s 前、"
            f"冷落计数 {signals.get('ignored_streak', 0)}"
            if proactive and proactive >= 0
            else f"上次刷新 {age:.0f}s 前；还没主动开过口"
        )
        return f"- 社交层信号：读到并在用。{extra}{tail}"
    if state == "stale":
        age = float(signals.get("age") or 0.0)
        return (
            f"- 社交层信号：文件在，但已经 {age / 60:.1f} 分钟没刷新（超过 15 分钟不采信）："
            f"社交层被停用、后台循环卡住，或刚重启还没跑一轮{tail}"
        )
    if state == "never_written":
        return (
            f"- 社交层信号：装了自主拟人社交（它的目录在），但还没写过信号文件。"
            f"v1.8.2 起它在启动时就会写一份；用旧版的话要等它跑完一轮（2~9 分钟）{tail}"
        )
    if state == "not_installed":
        return "- 社交层信号：没装「自主拟人社交」插件。这不是故障——本插件单独也能跑。"
    return f"- 社交层信号：读不到（拿不到 data 目录）{tail}"


def _hhmm(hour_of_day: float) -> str:
    """21.0 → 21:00、21.5 → 21:30。日程推出来的窗口带分钟，配置里只有整点。"""
    minutes = int(round(float(hour_of_day) * 60)) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _routine_lines(
    cfg: HumanoidConfig,
    schedule_status: dict[str, Any],
    body_status: dict[str, Any] | None,
) -> list[str]:
    """作息：把「她几点起」这件事的三个参与方摊开说。"""
    lines: list[str] = []
    night = (body_status or {}).get("biological_night")
    if not cfg.night_mode_enabled:
        lines.append("- 生物钟夜已关闭：不会攒睡眠债，也没日程时她永远不算在睡")
    else:
        start_h, end_h = cfg.night_start_hour, cfg.night_end_hour
        span = cfg.night_span_hours
        lines.append(
            f"- 配置里的夜间窗口 {start_h:02d}:00 → {end_h:02d}:00（{span:g} 小时），"
            f"一晚需要 {cfg.sleep_need_hours:g} 小时"
        )
        if night:
            n_start, n_end = night
            n_span = n_end - n_start if n_end > n_start else (24.0 - n_start) + n_end
            lines.append(
                f"- 她的生物钟夜（按今天的日程算）：{_hhmm(n_start)} → {_hhmm(n_end)}"
                f"（{n_span:g} 小时）——昼夜低谷、睡眠债记账都跟着这一段走"
            )
            if span and cfg.sleep_need_hours > n_span + 0.5:
                lines.append(
                    f"  {WARN_MARK} 这一段比她需要的睡眠短 {cfg.sleep_need_hours - n_span:.1f} 小时："
                    "一夜睡不够，早上会带着没清完的困意起床"
                )
        else:
            lines.append("  还没从日程里找到睡眠段，暂时按上面那个配置窗口算")
        lines.append(
            f"- 日程贴合作息：{'开启（模型排的睡眠区间会被强制改到配置窗口上）' if cfg.schedule_follow_night_window else '关闭（几点睡由她是谁决定，身体跟着她的日程走）'}"
        )

    wake = str(schedule_status.get("wake_at") or "")
    spans = schedule_status.get("sleep_spans") or []
    if wake:
        lines.append(f"- 今日日程里的起床时间：{wake}；睡眠段：{'、'.join(spans) or '无'}")
    elif spans:
        lines.append(f"- 今日日程里的睡眠段：{'、'.join(spans)}（凌晨没找到固定的起床点）")
    else:
        lines.append("- 今日日程里没有标「睡眠」的时段：她白天也不会算作在睡")

    snap = (body_status or {}).get("soma") or {}
    if snap:
        slept = float(snap.get("last_sleep_hours") or 0.0)
        if slept:
            gap = cfg.sleep_need_hours - slept
            tail = f"，比该睡的少 {gap:.1f} 小时" if gap > 0.5 else ""
            lines.append(f"- 昨夜实际睡了 {slept:.1f} 小时{tail}")
        lines.append(
            f"- 此刻：困意 {float(snap.get('sleep_pressure', 0)):.0f}/100、"
            f"睡眠债 {float(snap.get('sleep_debt', 0)):.1f}h、"
            f"{'正在睡' if snap.get('asleep') else '醒着'}"
        )
    return lines


def _timezone_lines(cfg: HumanoidConfig, zone: Any, moment: Any) -> list[str]:
    """时间与时区：她按哪个钟过日子，以及时在哪一步退化了。

    这一节存在的原因：以前时区只能从注入里那句「你在东京」间接推，而插件在缺 tzdata
    时会静默拿另一个城市的钟替她过日子，从聊天里根本看不出来。
    """
    from datetime import datetime

    lines: list[str] = []
    if zone is None:
        return ["- 拿不到时区状态（角色还没创建）"]
    host = datetime.now().astimezone()
    host_offset = int(host.utcoffset().total_seconds() // 60)
    city = str(zone.city or "").strip()
    degraded = not zone.zone_name
    if zone.zone_name:
        lines.append(
            f"- {city} → {zone.zone_name}（{format_offset(moment)}），此刻她那边 "
            f"{moment.strftime('%H:%M')} 星期{weekday_cn(moment)}"
        )
    else:
        label = city or "未填城市"
        lines.append(
            f"- 「{label}」没拿到可用时区 → 她此刻按这台机器的时间过日子："
            f"{format_offset(moment)}，{moment.strftime('%H:%M')}"
        )
    if host_offset != zone.offset_minutes:
        diff = (zone.offset_minutes - host_offset) / 60
        lines.append(
            f"- 机器本地时间 {host.strftime('%H:%M')}（{format_offset(host)}）："
            f"{OK_MARK} 两者差 {diff:+g} 小时，这是预期的（她过的不是机器时间）"
        )
    else:
        lines.append(f"- 机器本地时间与她的钟一致（{format_offset(host)}）")
    if zone.note:
        lines.append(f"  {WARN_MARK} {zone.note}")
        if "tzdata" in zone.note:
            lines.append("  → 在这台机器里装一份时区数据库（或 `pip install tzdata`）然后 /重载配置")
    elif not city or city == DEFAULT_CITY_PLACEHOLDER:
        lines.append(
            f"  {WARN_MARK} 还没定所在城市（`timezone_city` 仍是默认的「{DEFAULT_CITY_PLACEHOLDER}」）："
            "她住在哪个城市会直接改变她说出口的时间、作息、天气与日程"
        )
    weather_city = (cfg.weather_location or "").strip() or city
    if not cfg.weather_enabled:
        lines.append("- 天气：已关闭")
    elif not weather_city or weather_city == DEFAULT_CITY_PLACEHOLDER:
        lines.append(f"  {WARN_MARK} 天气城市没配（weather_location 为空且所在城市未定）：她不会报天气")
    else:
        same = "与时间城市同一处" if not (cfg.weather_location or "").strip() else "单独指定"
        lines.append(f"- 天气城市：{weather_city}（{same}）")
    lines.append(
        "- 日程、夜间窗口、睡眠债记账都按她那个城市的钟点算，不按机器时间"
        if not degraded
        else "- 现在日程、夜间窗口、睡眠债记账全部按这台机器的时间排（她拿不到自己城市的钟）"
    )
    return lines


def _resolve_line(
    resolver: ProviderResolver,
    label: str,
    provider_id: str,
    available: list[str],
) -> str:
    if not provider_id:
        return f"- {label}: 未配置"
    provider = resolver.resolve(provider_id)
    if provider is not None:
        actual = resolver.id_of(provider)
        suffix = "" if actual == provider_id else f"（实际匹配到 {actual}）"
        return f"- {label}: 「{provider_id}」{OK_MARK} 已解析{suffix}"
    near = [pid for pid in available if pid.strip().casefold() == provider_id.strip().casefold()]
    if near:
        hint = f"（注意大小写：可用列表里是 {near[0]}）"
    elif available:
        hint = "（可用列表里没有它）"
    else:
        hint = ""
    return f"- {label}: 「{provider_id}」{BAD_MARK} 未找到{hint}"


def _chain_pick(
    resolver: ProviderResolver,
    gateway: LLMGateway,
    chain: tuple[tuple[str, str], ...],
    allow_global: bool,
    purpose: str,
) -> str:
    for label, provider_id in chain:
        if gateway.cooldown_remaining(provider_id, purpose) > 0:
            continue
        if resolver.resolve(provider_id) is not None:
            return f"{label}({provider_id})"
    if allow_global:
        provider = resolver.resolve_global(None)
        if provider is not None:
            return f"{GLOBAL_LABEL}({resolver.id_of(provider)})"
    return "无可用模型 → 按身体与钟点现算一段（不调模型）"


def build_report(
    *,
    cfg: HumanoidConfig,
    resolver: ProviderResolver,
    gateway: LLMGateway,
    schedule_status: dict[str, Any],
    process_status: dict[str, Any],   # 新增参数
    version: str,
    body_status: dict[str, Any] | None = None,
    inject_estimate: dict[str, int] | None = None,
    zone_status: dict[str, Any] | None = None,
    state_size_lines: list[str] | None = None,
) -> str:
    available = resolver.available_ids()
    lines = [f"〖拟人诊断〗v{version}", "", "【AstrBot 可用对话模型 id】"]
    lines.append(f"  {available}" if available else "  （空 —— AstrBot 还没有配置任何对话模型）")

    lines += ["", "【日程模型链】"]
    lines.append(_resolve_line(resolver, "首选模型", cfg.schedule_provider_name, available))
    lines.append(_resolve_line(resolver, "备用模型", cfg.schedule_fallback_provider_name, available))

    if cfg.schedule_allow_global_fallback:
        provider = resolver.resolve_global(None)
        if provider is not None:
            lines.append(f"- 全局默认回退: 已开启 {OK_MARK} → {resolver.id_of(provider)}")
        else:
            lines.append(f"- 全局默认回退: 已开启，但 AstrBot 没设默认对话模型 {BAD_MARK}")
    else:
        lines.append("- 全局默认回退: 已关闭（schedule_allow_global_fallback = false）")

    picked = _chain_pick(
        resolver,
        gateway,
        cfg.schedule_provider_ids,
        cfg.schedule_allow_global_fallback,
        SCHEDULE_PURPOSE,
    )
    lines.append(f"- 本次实际将使用: {picked}")

    for purpose in (SCHEDULE_PURPOSE, MOOD_PURPOSE):
        cooldowns = gateway.cooldowns(purpose)
        if cooldowns:
            detail = "，".join(f"{pid} 剩余 {rem / 60:.0f} 分钟" for pid, rem in cooldowns.items())
            lines.append(f"- {purpose}冷却中: {detail}")
        else:
            lines.append(f"- {purpose}冷却中: 无")

    lines += ["", "【今日日程】"]
    lines.append(f"- 日期: {schedule_status.get('date') or '未生成'}")
    lines.append(
        f"- 来源: {schedule_status.get('source_text', '')}"
        f"，今天已过出 {schedule_status.get('slots', 0)} 段（滚动分段，未来不预排）"
    )
    active = str(schedule_status.get("active") or "")
    if active:
        lines.append(f"- 当前段: {active}")
    who = str(schedule_status.get("persona") or "")
    if not cfg.schedule_use_persona:
        lines.append("- 排日程用的人设：已关掉（schedule_use_persona=false），只按额外偏好排")
    elif who:
        lines.append(f"- 排日程用的人设：{who}（AstrBot 当前生效人格）")
    else:
        lines.append(
            "- 排日程用的人设：还没认出来——这个角色还没在任何会话里说过话，"
            "或者那个会话没绑人设；先跟她说一句，再 /重置日程 就会按人设重排"
        )
    if schedule_status.get("generated_at"):
        lines.append(f"- 生成时间: {schedule_status['generated_at']}")
    if schedule_status.get("generating"):
        lines.append("- 状态: 正在向模型请求下一段")
    retry_after = float(schedule_status.get("retry_after") or 0.0)
    if retry_after > 0:
        lines.append(f"- 自动重试: {retry_after / 60:.0f} 分钟后（/重置日程 可立即重试）")
    last = gateway.last_result(SCHEDULE_PURPOSE)
    if last is not None:
        lines.append(f"- 上次尝试: {last.summary()}")
    if schedule_status.get("last_error"):
        lines.append(f"- 上次失败原因: {schedule_status['last_error']}")

    lines += ["", "【时间与时区】", *_timezone_lines(cfg, (zone_status or {}).get("zone"), (zone_status or {}).get("moment"))]

    lines += ["", "【作息】", *_routine_lines(cfg, schedule_status, body_status)]

    lines += ["", "【当前过程】"]
    if process_status:
        name = process_status.get("name", "未知")
        duration = process_status.get("duration_minutes", 0)
        started = process_status.get("started_at", "")
        ended = process_status.get("expected_end", "")
        lines.append(f"- 正在：{name}（已持续约 {duration} 分钟）")
        if started and ended:
            lines.append(f"- 开始：{started}，预计结束：{ended}")
    else:
        lines.append("- 无活跃过程")

    lines += ["", "【情绪模型】"]
    if not cfg.mood_use_llm_for_delta:
        lines.append("- 未启用 LLM 情绪分析（仅本地规则），不消耗模型调用")
    else:
        lines.append(
            _resolve_line(
                resolver,
                "情绪模型",
                cfg.mood_provider_name or cfg.schedule_provider_name,
                available,
            )
        )
        mood_last = gateway.last_result(MOOD_PURPOSE)
        if mood_last is not None:
            lines.append(f"- 上次尝试: {mood_last.summary()}")
        lines.append(
            f"- 每个用户各自每 {cfg.mood_llm_interval_messages} 条消息分析一次（不是全局每 N 条："
            f"10 个人各自满 5 条就是 10 次）"
            f"，失败冷却 {cfg.mood_provider_cooldown_minutes} 分钟"
            f"，群聊{'启用' if cfg.mood_enabled_in_group else '不启用'}"
        )

    lines += ["", "【身体与联动】"]
    if not cfg.soma_enabled:
        lines.append("- 生理层已关闭：只剩精力标量，不会困、不会饿、也不会攒出想说话的心思")
    elif body_status:
        snap = body_status.get("soma") or {}
        lines.append(
            f"- 轴：困意 {snap.get('sleep_pressure', 0):.0f}、睡眠债 {snap.get('sleep_debt', 0):.1f}h、"
            f"饿 {snap.get('hunger', 0):.0f}、不适 {snap.get('discomfort', 0):.0f}、"
            f"唤醒 {snap.get('arousal', 0):.0f}、想说 {snap.get('social_desire', 0):.0f}"
        )
        lines.append(
            f"- 身体推进间隔 {cfg.body_tick_seconds}s；周期第 {body_status.get('cycle_day', 1)} 天"
            f"；精力 {body_status.get('energy', 0):.0f}"
        )
        signals = body_status.get("signals") or {}
        contract = body_status.get("contract")
        if isinstance(contract, dict) and contract:
            lines.append(
                f"- 联动契约 v{contract.get('v')} 已导出（{len(contract.get('feelings') or [])} 条体感、"
                f"max_chars {contract.get('form', {}).get('max_chars')}）；写在她自己的 state.json 里"
            )
        elif not cfg.contract_enabled:
            lines.append("- 联动契约：配置里关着（contract_enabled=false），不会导出")
        elif not cfg.soma_enabled:
            lines.append("- 联动契约：生理层关着时不导出（轴全是初始值，社交层读到会误判）")
        else:
            lines.append(
                f"- 联动契约：还没写出（身体 tick 每 {cfg.body_tick_seconds}s 一次，"
                "启动后等一轮再看；角色还没收到过任何消息也不会有）"
            )
        lines.append(_signals_line(signals))
    else:
        lines.append("- 生理层已开启，但还没有角色实例")

    lines += ["", "【Token 预算】"]
    if inject_estimate:
        parts = "，".join(f"{mode} ≈ {tok}" for mode, tok in inject_estimate.items())
        lines.append(f"- 聊天时追加的上下文：{parts} token")
    for note in state_size_lines or []:
        lines.append(f"- {note}")
    lines.append(
        f"- 日程生成：滚动分段，一次只决定下一段（输入约 240 + 输出一个小 JSON）；"
        f"决策窗 {cfg.schedule_refresh_minutes} 分钟，变动概率 {cfg.schedule_change_chance}%，"
        "没到期没报警没掷中就不调模型"
    )
    lines.append(
        "- 上面这条的频率只由「到期/掷中」决定，硬上限看下面的【调用节流】："
        "没有节流时，全天无人也能烧掉一百多次"
    )
    lines.append(
        "- 情绪分析："
        + (
            f"每个用户各自每 {cfg.mood_llm_interval_messages} 条私聊 1 次小 prompt（≤ 500 字）；"
            "不是全局每 N 条一次，10 个人同时聊就是 10 次"
            if cfg.mood_use_llm_for_delta
            else "已关闭，不消耗模型调用"
        )
    )
    lines.append(
        "- 本插件不会为聊天回复额外调模型：上下文里只有她的事实（时间/场景/称呼/身体/今天/"
        "隔了多久/注意力三轴的措辞），回复走 AstrBot 主链路。"
    )

    gate = gateway.gate
    if gate is not None:
        snap = gate.snapshot()
        lines += ["", "【调用节流】"]
        idle_limit = snap["idle_limit_minutes"]
        if idle_limit <= 0:
            lines.append("- 空闲静默：已关闭（一直挂着也会继续调模型）")
        elif snap["never_interacted"]:
            lines.append(
                f"- 空闲静默：开启（{int(idle_limit)} 分钟）。"
                "还没人跟她说过话，后台日程现在全靠本地现算"
            )
        else:
            idle_min = snap["idle_minutes"] or 0.0
            state = "正在静默" if snap["idle_active"] else "活跃中"
            lines.append(
                f"- 空闲静默：开启（{int(idle_limit)} 分钟），已 {int(idle_min)} 分钟没人说话，当前 {state}"
            )
        interval = snap["interval_minutes"]
        lines.append(
            f"- 最小调用间隔：{int(interval)} 分钟（两次日程调用至少隔这么久）"
            if interval > 0
            else "- 最小调用间隔：未限制（改主意概率仍可能连着触发）"
        )
        budget = snap["budget"]
        used = snap["used_today"]
        lines.append(
            f"- 今日预算：{used}/{budget} 次（所有 bot 共享，情绪分析不计入）"
            if budget > 0
            else f"- 今日预算：未限制（今天已用 {used} 次）"
        )
        verdict = snap["verdict"]
        lines.append(
            f"- 当前判定：{OK_MARK} {verdict.describe()}"
            if verdict.allowed
            else f"- 当前判定：{verdict.describe()}（本次不调模型，走本地兜底）"
        )

    lines += [
        "",
        "【关键参数】",
        f"- 单次生成超时 {cfg.schedule_llm_timeout_seconds}s"
        f"，每个模型尝试 {cfg.schedule_generation_max_attempts} 次"
        f"，重试间隔 {cfg.schedule_retry_interval_seconds}s",
        f"- 当天最多保留 {cfg.schedule_max_slots} 段，时间对齐 {cfg.schedule_time_granularity}",
        f"- 日程失败冷却 {cfg.schedule_provider_cooldown_minutes} 分钟"
        f"，情绪失败冷却 {cfg.mood_provider_cooldown_minutes} 分钟（两者各自记账）",
        f"- 大模型日程 {'开启' if cfg.use_llm_schedule else '关闭'}"
        f"，调试日志 {'开启' if cfg.debug_mode else '关闭'}",
    ]

    if not available:
        lines += ["", "→ 请先在 AstrBot 的「服务提供商」里配置至少一个对话模型。"]
    elif cfg.schedule_provider_name and cfg.schedule_provider_name not in available:
        lines += ["", "→ 首选模型 id 不在可用列表里：请在插件配置里用下拉框重新选择。"]

    return "\n".join(lines)

def state_size_lines(core) -> list[str]:
    """状态文件有多大、有多少用户条目——写盘成本全看这两项。

    每 5 秒（有变更时）整份重写一次 `state.json`，所以它的体积就是写盘开销的倍数。
    而体积的真正来源是**每用户条目**：被自动认定过称呼的群成员会永久留一条（只有
    nickname、没有任何会被清理的字段），群越大涨得越快。这些数字平时看不见，
    涨到影响体验时已经晚了，所以放进诊断里。
    """
    out: list[str] = []
    try:
        store = getattr(core, "_state_store", None)
        path = getattr(store, "path", None) if store is not None else None
        if path is not None and path.exists():
            size_kb = path.stat().st_size / 1024.0
            hint = ""
            if size_kb > 512:
                hint = "（已经偏大：写盘是每 5 秒整份重写，可调大「写盘间隔」或调小「情绪数据保留」）"
            out.append(f"状态文件：{size_kb:.0f} KB{hint}")
    except Exception:
        pass
    try:
        users = core.scope.all_user_ids()
        out.append(f"记录在案的用户：{len(users)} 人")
    except Exception:
        pass
    return out
