"""`/拟人 诊断` 的报告生成。"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from astrbot.api import logger

from . import memory_link
from .config import HumanoidConfig
from .clock import DEFAULT_CITY_PLACEHOLDER, weekday_cn, format_offset
from .services.weather import effective_location as _weather_effective_location
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

#: 诊断报告里最多列多少个模型 id。装了几十个模型时，这一段会长到把别的检查项挤掉。
MODEL_ID_ROWS = 40


def config_error_banner(load_error: str) -> list[str]:
    """配置整体退回默认值时，报告最顶上那几行。空串=正常，返回空列表。

    `ConfigBox.load_error` 的文档一直说「`/拟人 诊断` 用它告诉用户配置有问题」，
    但全仓无人读它：解析真抛时插件用**全默认值**跑起来（城市、模型链、管理员名单、
    情绪开关全部复位），诊断只字不提，用户只能靠翻日志。
    """
    if not load_error:
        return []
    return [
        f"{WARN_MARK} 配置解析失败，已整体退回默认值：{load_error}",
        "  → 下面看到的城市、模型链、开关全是出厂默认，不是你配的那份；"
        "改好配置项后 /拟人 重载。",
        "",
    ]


def _age_seconds(value: Any) -> float:
    """signals 里的秒数：拿不到或不是数时当 0，绝不让格式化抛出去。

    `link.py` 在 `stat()` 失败时把 `age` 写成 None 而 state 仍是 `"fresh"`，
    旧代码 `f"{age:.0f}"` 抛 TypeError，一路冒到 `/拟人 诊断`——唯一的排查入口
    直接报错。`stale` 分支本来就有兜底，两处现在同口径。
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _signals_line(signals: dict) -> str:
    """社交层信号那一行：把「没读到」拆成三种完全不同的情况说。"""
    state = str(signals.get("state") or "")
    path = str(signals.get("path") or "")
    tail = f"（看的是 {path}）" if path else ""
    if state == "fresh":
        # age 可能是 None（link.py 的 stat() 失败）：以前这里直接 `{age:.0f}`，
        # 抛 TypeError 让整条 /拟人 诊断 报错。
        age = _age_seconds(signals.get("age"))
        proactive = _age_seconds(signals.get("last_proactive_age", -1))
        # 判 `>= 0` 而不是「真值」：她刚好在这一次刷新前开口时 age 正好是 0.0，
        # 用真值判会把「刚刚主动开过口」说成「还没主动开过口」。
        extra = (
            f"上次刷新 {age:.0f}s 前；上次主动开口 {proactive:.0f}s 前、"
            f"冷落计数 {signals.get('ignored_streak', 0)}"
            if proactive >= 0
            else f"上次刷新 {age:.0f}s 前；还没主动开过口"
        )
        return f"- 社交层信号：读到并在用。{extra}{tail}"
    if state == "unreadable":
        return (
            f"- 社交层信号：文件在、时间也新鲜，但**读不出内容**（JSON 坏了 / 编码不对 / "
            f"没权限 / 是个目录 / 大到不像信号文件）{tail}。"
            "这不是「她手上没事」——本插件按「读不到」处理，账本原样留着；"
            "去社交层看一眼它写盘时有没有报错"
        )
    if state == "stale":
        age = _age_seconds(signals.get("age"))
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


def _loop_failure_lines(failed: dict[str, float] | None, now: float | None = None) -> list[str]:
    """后台循环启动失败的角色：以前这件事只在日志里有一行 WARN。

    症状是「功能看起来存在但不运行」：日程不再换段、情绪不再分析、天气不再刷新、
    社交信号不再读——而面板与聊天里一切正常，用户没有任何线索。这里把它摊开，
    并说明「消息仍然能用」（失败实例留在缓存里正是为了读状态）。
    """
    if not failed:
        return []
    stamp = time.time() if now is None else float(now)
    parts: list[str] = []
    for role_id, at in failed.items():
        try:
            age = max(0.0, stamp - float(at))
        except (TypeError, ValueError):
            age = 0.0
        parts.append(f"{role_id}（{age:.0f}s 前失败）")
    return [
        f"{WARN_MARK} 后台循环启动失败：{'、'.join(parts)}——"
        "日程换段 / 情绪分析 / 天气刷新 / 社交信号都不会自己跑（消息仍能读到她的状态）；"
        "插件会每 30 秒重试一次，持续失败请看日志里那行启动异常"
    ]


def _memory_link_line() -> str:
    """长期记忆桥接的状态行（v3.0.0，P2-24）。

    `memory_link.last_error()` 以前定义了却全仓无消费方：桥接解析失败/超时/对面关了
    开关，用户在 /拟人 诊断 里一个字都看不到，只能去翻日志。这里把它摊开说，并给
    出「该去查什么」的方向（没装 / 接口不匹配 / 卡住 / 开关关着）。
    """
    try:
        error = str(memory_link.last_error() or "").strip()
    except Exception:
        error = ""
    if not error:
        # 这一栏只显示**此刻**的故障：`last_error()` 在成功读写后会清空（v3.0.2 起
        # 语义从「最近一次失败」改成「当前故障」）。所以「没有」不能写成「没有失败
        # 记录」——那会让人以为这里留着历史，实际恢复之后什么都看不到。
        return (
            "- 长期记忆联动（memory_companion）：当前没有故障"
            "（没装 / 对面关了桥接时是静默降级，不报错；过去失败过但已恢复不会留痕）"
        )
    kind = error.split(":", 1)[0].strip()
    hint = {
        "missing": "对面没装 / 未激活 / 宿主没有注册表 API",
        "signature": "找到对面了但接口对不上（双方版本不匹配）",
        "exception": "调用对面时抛了异常",
        "timeout": "对面在限定时间内没返回——已按超时降级，不会卡住回复",
        "disabled": "对面装了但桥接开关关着（配置页「记忆插件协同 → 启用桥接 API」）",
    }.get(kind, "未知类别")
    return f"- 长期记忆联动（memory_companion）：最近一次失败 = {error}（{hint}）"


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
    elif not degraded:
        # 降级时她的钟就是机器时间，上面那句已经说清「按这台机器的时间过日子」；
        # 再说一句「一致」等于把「没拿到时区」和「时区正常」说成同一件事。
        lines.append(f"- 机器本地时间与她的钟一致（{format_offset(host)}）")
    if zone.note:
        lines.append(f"  {WARN_MARK} {zone.note}")
        if "tzdata" in zone.note:
            lines.append("  → 在这台机器里装一份时区数据库（或 `pip install tzdata`）然后 /拟人 重载")
    elif not city or city == DEFAULT_CITY_PLACEHOLDER:
        lines.append(
            f"  {WARN_MARK} 还没定所在城市（`timezone_city` 仍是默认的「{DEFAULT_CITY_PLACEHOLDER}」）："
            "她住在哪个城市会直接改变她说出口的时间、作息、天气与日程"
        )
    # 天气城市**只问天气子系统自己的判定**（模块级 effective_location，与 snapshot()
    # 同一套归一化）。以前这里自己写「weather_location or 时间城市」，于是面板说
    # 「天气城市：河源（记得改~）」而天气模块判定「未配置」——同一件事两处各算一遍，
    # 结论就不一样（审核 引擎与诊断 M3）。zone 有 .city，正是 clock.city。
    weather_city = _weather_effective_location(zone, cfg)
    if not cfg.weather_enabled:
        lines.append("- 天气：已关闭")
    elif not weather_city:
        configured = (cfg.weather_location or "").strip()
        detail = f"（weather_location 填的「{configured}」认不出英文城市名）" if configured else ""
        lines.append(
            f"  {WARN_MARK} 天气城市：未配置（天气子系统判定）{detail}：她不会报天气"
        )
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


# ─── 跳过原因码（v3.1.4） ─────────────────────────────────────────────
#
# 为什么要记：humanoid 里「这一轮什么都没发生」的分支很多——安全闸挡住 ccb、
# 冷却期、每日上限、注入块拼不出内容、后台任务在收到停止信号后不再起……它们
# 全都只是 `return None` / `return ""`，日志里一个字都没有。用户看到的现象是
# 「她今天好像不太理我」，而排查时没有任何线索。
#
# 这里**只做观测**：每个关键早退点写一个稳定的机器码进内存环，`/拟人 诊断`
# 汇总最近 24 小时出现最多的几个。不落盘（诊断是排查入口，不是账本）、
# 不改行为、满了丢最旧的（内存有界，长时间运行也不会涨）。

REASON_BUFFER_SIZE = 512
REASON_WINDOW_SECONDS = 86400.0
_REASON_BUFFER: deque = deque(maxlen=REASON_BUFFER_SIZE)

# 码是给机器看的，报告是给人看的：用户看「/你的状态」时不该对着一串英文猜。
# 没登记的码原样显示（新增码忘了登记也不会把报告弄坏）。
REASON_LABELS = {
    "closing_silence": "收尾静默：对方只是重复道别，这轮没再回",
    "closing_prompt": "收尾提示：跳不过模型，只提醒她别再追一句",
    "inject_empty": "本轮没有可注入的背景",
    "ccb_safety_blocked": "CCB 被安全策略挡下（未成年/血缘人设）",
    "ccb_not_qualified": "CCB 的好感或欲望没到门槛",
    "ccb_cooldown": "CCB 还在冷却",
    "ccb_daily_cap": "CCB 今日次数已用完",
    "input_state_unsupported": "「正在输入」不支持这个会话",
    "background_after_stop": "停止后仍有后台任务被拉起",
    "opportunity_used": "本轮把具体可提的事递进了上下文（带出处与时效）",
    "opportunity_expired": "有由头但已过期，被丢掉了（宁可少一条，不给过时的）",
    "opportunity_missing": "这一轮没有任何可指认的具体的事（只能少说，不要编）",
    "greeting_opportunity": "本轮把问候窗口翻成了可提的由头（只给时机，不代表对方刚醒）",
    "share_provenance_missing": "她最近主动说过的话找不到出处（不补细节，不编）",
    "materials_used": "本轮把生活素材摆进了上下文，并记进 7 天去重台账",
}

# 证据卡三码是「每轮都会记一次」的状态码（有卡 / 全过期 / 一张都没有），
# 不是「这轮跳过了什么」：混进 reason_report 的 top 会让它们凭条数长期霸榜，
# 把真正让她不回话的原因（CCB 冷却、收尾静默……）挤下去。所以它们照样进环、
# 照样有中文文案，只是默认不参与跳过原因排名——要看就传 include_context=True。
CONTEXT_CODES = frozenset({
    "opportunity_used",
    "opportunity_expired",
    "opportunity_missing",
    # v3.1.6：同样是「每轮都可能记一次」的上下文状态，不是跳过原因。
    "greeting_opportunity",
    "share_provenance_missing",
    # v3.1.7：素材去重台账的写入，同样是每轮可能记一次的状态码。
    "materials_used",
})


def note_reason(code: str, detail: str = "") -> None:
    """记一次「这轮为什么没做某事」。code 是稳定的机器码（英文小写下划线）。

    为什么不直接写日志：这些分支都在热路径上（每条消息、每次后台循环），写日志
    会把正常日志淹掉；环缓冲只在用户主动看诊断时才被读出来。
    """
    text = str(code or "").strip()
    if not text:
        return
    try:
        _REASON_BUFFER.append({
            "code": text[:48],
            "detail": str(detail or "")[:80],
            "ts": time.time(),
        })
    except Exception:  # pragma: no cover - 纯内存操作，理论上不会失败
        pass


def clear_reasons() -> None:
    """清空环缓冲（测试与「看过了」用；不影响任何行为）。"""
    _REASON_BUFFER.clear()


def _ago_text(seconds: float) -> str:
    """把「多少秒以前」说成人话；看不懂就返回空串，绝不抛。"""
    try:
        value = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return ""
    if value < 90:
        return f"{int(value)} 秒"
    if value < 5400:
        return f"{int(value // 60)} 分钟"
    return f"{int(value // 3600)} 小时"


def reason_report(
    now: float | None = None,
    window_seconds: float = REASON_WINDOW_SECONDS,
    include_context: bool = False,
) -> dict:
    """最近一段时间里各类跳过原因的次数，按次数降序。

    返回 `{"window_seconds", "total", "capacity", "stored", "top"}`，`top` 里每项是
    `{"code", "count", "last_at", "last_detail"}`。窗口按时间戳过滤（不是按条数），
    所以 24 小时前的记录自动不计入，也不需要后台清理。

    `include_context=True` 时把证据卡三码（CONTEXT_CODES）也算进来——它们每轮都会
    记一次，属于上下文状态而不是跳过原因，默认排除，免得把真原因挤下榜首。
    """
    try:
        moment = time.time() if now is None else float(now)
    except (TypeError, ValueError):
        moment = time.time()
    try:
        window = max(0.0, float(window_seconds))
    except (TypeError, ValueError):
        window = REASON_WINDOW_SECONDS
    floor = moment - window
    counts: dict[str, dict] = {}
    for item in list(_REASON_BUFFER):
        ts = float(item.get("ts") or 0.0)
        if ts < floor:
            continue
        code = str(item.get("code") or "")
        if not code:
            continue
        if not include_context and code in CONTEXT_CODES:
            continue
        bucket = counts.get(code)
        if bucket is None:
            counts[code] = {
                "code": code,
                "count": 1,
                "last_at": ts,
                "last_detail": str(item.get("detail") or ""),
            }
        else:
            bucket["count"] += 1
            if ts >= bucket["last_at"]:
                bucket["last_at"] = ts
                bucket["last_detail"] = str(item.get("detail") or "")
    top = sorted(
        counts.values(), key=lambda item: (-item["count"], -item["last_at"], item["code"])
    )
    return {
        "window_seconds": window,
        "total": sum(item["count"] for item in top),
        "capacity": REASON_BUFFER_SIZE,
        "stored": len(_REASON_BUFFER),
        "top": top,
    }


def reason_lines(
    now: float | None = None,
    window_seconds: float = REASON_WINDOW_SECONDS,
    limit: int = 5,
) -> list[str]:
    """诊断报告里的【跳过原因码】小节。**没有记录就返回空列表**。

    为什么要「没有就不出现」：报告本身已经有七八段，绝大多数部署根本不会碰到
    这些分支；空小节只会让真正重要的告警更难被看见。
    """
    try:
        report = reason_report(now=now, window_seconds=window_seconds)
    except Exception:  # pragma: no cover - 观测代码不许把诊断页弄挂
        return []
    top = report["top"][: max(1, int(limit or 5))]
    if not top:
        return []
    moment = time.time() if now is None else float(now)
    hours = report["window_seconds"] / 3600.0
    lines = [f"【跳过原因码】最近 {hours:.0f} 小时共 {report['total']} 次"]
    for item in top:
        detail = f"：{item['last_detail']}" if item["last_detail"] else ""
        label = REASON_LABELS.get(item["code"], "")
        suffix = f" —— {label}" if label else ""
        lines.append(
            f"- {item['code']} ×{item['count']}"
            f"（{_ago_text(moment - item['last_at'])}前{detail}）{suffix}"
        )
    lines.append("  （只在进程内存里记；重启即清空，不落盘）")
    return lines


def evidence_lines(
    now: float | None = None,
    window_seconds: float = REASON_WINDOW_SECONDS,
) -> list[str]:
    """诊断报告里的【证据卡】小节：有卡可用 / 全过期 / 一张都没有各多少次。

    为什么单列而不并进【跳过原因码】：这三码每轮都会记一次（见 CONTEXT_CODES），
    并进去会凭条数霸榜。单列一行既能看到「她最近是不是一直在没素材地聊」，
    又不影响跳过原因的排序。同样**没有记录就不出现**，不给报告添空行。
    """
    try:
        report = reason_report(
            now=now, window_seconds=window_seconds, include_context=True
        )
    except Exception:  # pragma: no cover - 观测代码不许把诊断页弄挂
        return []
    counts = {
        str(item.get("code") or ""): int(item.get("count") or 0)
        for item in report["top"]
        if item.get("code") in CONTEXT_CODES
    }
    if not counts:
        return []
    hours = report["window_seconds"] / 3600.0
    parts = []
    for code, text in (
        ("opportunity_used", "有卡可用"),
        ("opportunity_expired", "全过期"),
        ("opportunity_missing", "一张都没有"),
        ("greeting_opportunity", "问候时机"),
        ("share_provenance_missing", "出处缺失"),
        ("materials_used", "素材记档"),
    ):
        count = counts.get(code, 0)
        if count:
            parts.append(f"{text} ×{count}")
    return [f"【证据卡】最近 {hours:.0f} 小时：{'、'.join(parts)}"]


def build_report(
    *,
    cfg: HumanoidConfig,
    resolver: ProviderResolver,
    gateway: LLMGateway,
    schedule_status: dict[str, Any],
    activity_status: dict[str, Any],
    version: str,
    body_status: dict[str, Any] | None = None,
    inject_estimate: dict[str, Any] | None = None,
    zone_status: dict[str, Any] | None = None,
    state_size_lines: list[str] | None = None,
    config_load_error: str = "",
    role_id: str = "",
    failed_loops: dict[str, float] | None = None,
) -> str:
    """诊断报告。**任何一处取值/格式化炸了都不能让这条命令整体报错**。

    它是唯一的排查入口，自己报错等于把用户挡在门外（signals 的 `age` 为 None 时
    `f"{None:.0f}"` 抛 TypeError，一路冒到 `/拟人 诊断`）。出错时退回一段可读的
    最小报告；正常路径逐字不变。
    """
    try:
        return _build_report_body(
            cfg=cfg,
            resolver=resolver,
            gateway=gateway,
            schedule_status=schedule_status,
            activity_status=activity_status,
            version=version,
            body_status=body_status,
            inject_estimate=inject_estimate,
            zone_status=zone_status,
            state_size_lines=state_size_lines,
            config_load_error=config_load_error,
            role_id=role_id,
            failed_loops=failed_loops,
        )
    except Exception as exc:  # noqa: BLE001 - 兜底就是为了诊断页还能打开
        logger.exception("诊断报告生成失败")
        return (
            f"〖拟人诊断〗v{version}\n"
            f"{WARN_MARK} 诊断报告生成失败：{type(exc).__name__}: {exc}\n"
            "（其余检查项已跳过；这本身是插件缺陷，请连同上面的日志一起反馈）"
        )


def _build_report_body(
    *,
    cfg: HumanoidConfig,
    resolver: ProviderResolver,
    gateway: LLMGateway,
    schedule_status: dict[str, Any],
    activity_status: dict[str, Any],
    version: str,
    body_status: dict[str, Any] | None = None,
    inject_estimate: dict[str, Any] | None = None,
    zone_status: dict[str, Any] | None = None,
    state_size_lines: list[str] | None = None,
    config_load_error: str = "",
    role_id: str = "",
    failed_loops: dict[str, float] | None = None,
) -> str:
    available = resolver.available_ids()
    lines = [f"〖拟人诊断〗v{version}", ""]
    lines += config_error_banner(config_load_error)
    lines += _loop_failure_lines(failed_loops)
    # v3.1.41：报告开头先说清「怎么读」。以前一上来就是模型 id 列表，而且整份列表
    # 挤成一行——用户配了六十来个模型时，那一行就超过整条消息的长度预算，后面所有
    # 检查项被截掉，只剩一句「还有 69 行没显示」（issue #10）。
    lines.append(
        f"（下面每一段是一项检查：{OK_MARK} 正常、{BAD_MARK} 有问题、{WARN_MARK} 注意；"
        "带「-」的行是这一项的具体数值）"
    )
    lines.append("")

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
    quality = schedule_status.get("quality")
    if isinstance(quality, dict):
        # 只报计数，不报分数：没有标注就没有分母，「自然度 7/10」是编出来的。
        lines.append(
            "- 质量闸: "
            f"重写 {int(quality.get('rewrite', 0) or 0)} 次"
            f"，重复放行 {int(quality.get('repeat', 0) or 0)} 段"
            f"，编造降级 {int(quality.get('invented', 0) or 0)} 段"
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

    lines += ["", "【此刻在做什么】"]
    if activity_status:
        name = activity_status.get("name", "")
        duration = activity_status.get("duration_minutes", 0)
        started = activity_status.get("started_at", "")
        ended = activity_status.get("expected_end", "")
        location = str(activity_status.get("location", "") or "").strip()
        lines.append(f"- 正在：{name}（已持续约 {duration} 分钟）")
        if location:
            lines.append(f"- 地点：{location}")
        if started and ended:
            lines.append(f"- 开始：{started}，预计结束：{ended}")
    else:
        lines.append("- 此刻还没排出来的时段")

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
            f"10 个人各自满 {cfg.mood_llm_interval_messages} 条就是 10 次）"
            f"，失败冷却 {cfg.mood_provider_cooldown_minutes} 分钟"
            f"，群聊{'启用' if cfg.mood_enabled_in_group else '不启用'}"
        )

    lines += ["", "【身体与联动】"]
    body_status = body_status or {}
    if not cfg.soma_enabled:
        lines.append("- 生理层已关闭：只剩精力标量，不会困、不会饿、也不会攒出想说话的心思")
        # v3.0.0（P1-C/P2-26）：契约不再连带停发，这里必须说清当前契约与社交信号的状态，
        # 否则用户以为「关身体 = 断联动」的现象还在（社交层那边也会误诊成旧版 Core）。
        contract = body_status.get("contract")
        if isinstance(contract, dict) and contract:
            lines.append(
                f"- 联动契约：v{contract.get('v')} 已导出（身体块给中性表示，其余块照常）；"
                "写在她自己的 state.json 里"
            )
        elif not cfg.contract_enabled:
            lines.append("- 联动契约：配置里关着（contract_enabled=false），不会导出")
        else:
            lines.append(
                "- 联动契约：已开启；生理层关闭也照常导出（身体块中性表示），"
                "启动后等一轮再看"
            )
        lines.append(_signals_line(body_status.get("signals") or {}))
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
        # v3.0.0（P1-C/P2-26）：soma_enabled 不再切断契约，旧的「生理层关着时不导出」
        # 分支已不存在（上面 if not cfg.soma_enabled 分支单独说明）。
        else:
            lines.append(
                f"- 联动契约：还没写出（身体 tick 每 {cfg.body_tick_seconds}s 一次，"
                "启动后等一轮再看；角色还没收到过任何消息也不会有）"
            )
        lines.append(_signals_line(signals))
    else:
        lines.append("- 生理层已开启，但还没有角色实例")
    # v3.0.0（P2-24）：长期记忆桥接的失败原因不能只躺在 last_error() 里没人读。
    lines.append(_memory_link_line())

    lines += ["", "【Token 预算】"]
    # 探针失败也要说话：以前返回空 dict，这一节只剩一个标题，用户看不出是「没算」
    # 还是「算失败了」。
    if not inject_estimate:
        lines.append("- 注入估算：没算（还没有角色实例）")
    elif inject_estimate.get("error"):
        lines.append(f"- 注入估算：测不出来：{inject_estimate['error']}")
    else:
        scene = str(inject_estimate.get("scene") or "探针")
        lines.append(
            f"- {scene}（一次假用户私聊，群聊注入另计）："
            f"≈ {inject_estimate.get('tokens')} token，"
            f"预算 {int(cfg.inject_token_budget)} token"
        )
    if state_size_lines:
        # v3.0.0（P2-32）：状态落盘本身的可观测性单独成节——Windows 上 state.json
        # 被占用时落盘会连续失败，以前只有日志里一条 WARN，用户在诊断页看不到。
        lines += ["", "【状态落盘】"]
        for note in state_size_lines:
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
        "- 本插件不会为聊天回复额外调模型：上下文里只有她的事实（时间/场景/称呼/身体/"
        "隔了多久/注意力三轴的措辞），回复走 AstrBot 主链路。"
    )

    gate = gateway.gate
    if gate is not None:
        # 空闲静默按角色判（闸门全局一份）：诊断要看的是**这个角色**多久没人理，
        # 拿全局最近一次会在多角色时把真正被静默的那个藏起来。
        snap = gate.snapshot(role_id=role_id)
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
        max_interval = snap.get("max_interval_minutes", 0)
        if interval > 0 and max_interval > interval:
            lines.append(f"- 调用间隔：{int(interval)}~{int(max_interval)} 分钟随机（每次生成后抽一个）")
        elif interval > 0:
            lines.append(f"- 调用间隔：{int(interval)} 分钟")
        else:
            lines.append("- 调用间隔：未限制（改主意概率仍可能连着触发）")
        budget = snap["budget"]
        used = snap["used_today"]
        lines.append(
            f"- 今日预算：{used}/{budget} 次（所有 bot 共享，情绪分析不计入）"
            if budget > 0
            else f"- 今日预算：未限制（今天已用 {used} 次）"
        )
        # 按用途拆开。以前只有一个总数，「今天为什么只剩 20 次」是没法回答的问题——
        # 可能是日程排太满，也可能是情绪分析在批量跑，处置方式完全不同。
        by = snap.get("used_by_purpose") or {}
        if by:
            # 用途常量本身就是中文（`PURPOSE_SCHEDULE = "日程生成"`），不用再翻。
            parts = "、".join(f"{k} {v}" for k, v in
                              sorted(by.items(), key=lambda kv: -kv[1]))
            lines.append(f"- 花在哪：{parts}")
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

    # 跳过原因码（v3.1.4）：只在有记录时才出现，不给既有报告添空行。
    reasons = reason_lines()
    if reasons:
        lines += [""] + reasons

    # 证据卡状态（v3.1.5）：与跳过原因分开——这三码每轮都记，并进上面会把真原因挤掉。
    evidence = evidence_lines()
    if evidence:
        lines += [""] + evidence

    if not available:
        lines += ["", "→ 请先在 AstrBot 的「服务提供商」里配置至少一个对话模型。"]
    elif cfg.schedule_provider_name and cfg.schedule_provider_name not in available:
        lines += ["", "→ 首选模型 id 不在可用列表里：请在插件配置里用下拉框重新选择。"]

    # v3.1.41：模型 id 列表挪到最末尾、一行一个。它是最长的一段（几十个 id），
    # 放在开头会把整份报告挤没；一行一个也才看得清该抄哪个。太多时只列前
    # MODEL_ID_ROWS 个，剩下的指路到面板——不然一装几十个模型，报告光这一段就上千字。
    lines += ["", "【AstrBot 可用对话模型 id】"]
    if available:
        lines.append("  填面板上的「日程模型」「情绪模型」时，照抄下面任意一行的 id：")
        for name in available[:MODEL_ID_ROWS]:
            lines.append(f"  - {name}")
        if len(available) > MODEL_ID_ROWS:
            lines.append(
                f"  ……还有 {len(available) - MODEL_ID_ROWS} 个没列"
                "（去 AstrBot 的「服务提供商」页面看完整列表）"
            )
    else:
        lines.append("  （空 —— AstrBot 还没有配置任何对话模型，先去「服务提供商」加一个）")

    return "\n".join(lines)

def _user_entries(scope: Any) -> dict[str, Any] | None:
    """只读地拿 `users` 映射；拿不到返回 None。

    `RoleScope` 没有公开的 entries 视图（只有 `all_user_ids()` 与单键 `peek_user()`），
    而诊断要数「只剩称呼的空壳」必须看到每个条目的键集合。这里直接读它的 `_root`：
    **不建条目、不标脏**——诊断是只读入口，不能因为看一眼就让 state.json 变脏。
    """
    try:
        root = getattr(scope, "_root", None)
        if isinstance(root, dict):
            users = root.get("users")
            if isinstance(users, dict):
                return users
    except Exception:
        pass
    return None


def _shell_fields() -> tuple[str, ...]:
    """「只剩称呼、没有任何用户数据」的字段名单。

    **唯一真源在 `services.mood` 的 `_STALE_SHELL_FIELDS`**（整条删除判定用的就是它）；
    这里只读，不另写一份——否则「空壳」在诊断与清理里各算一遍，数出来又不一样。
    """
    try:
        from .services.mood import _STALE_SHELL_FIELDS

        return tuple(_STALE_SHELL_FIELDS)
    except Exception:
        return ("nickname",)


def state_size_lines(core) -> list[str]:
    """状态文件有多大、有多少用户条目——写盘成本全看这两项。

    默认每 60 秒（有变更时）整份重写一次 `state.json`，所以它的体积就是写盘开销的倍数。
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
                # 旧 hint 让用户调小「情绪数据保留」，但体积的主要来源是每用户空壳
                # （只有称呼、没有任何可清理字段），调小保留天数对它完全无效——
                # 那句处置方向指向了一个清不掉的东西（审核 引擎与诊断 M4）。
                hint = (
                    "（已经偏大：写盘是默认每 60 秒整份重写；主要来源是每用户空壳，"
                    "见「整条用户记录保留(天)」，空壳按静默期清掉后会回落）"
                )
            out.append(f"状态文件：{size_kb:.0f} KB{hint}")
    except Exception:
        pass
    try:
        store = getattr(core, "_state_store", None)

        def _store_attr(name: str):
            """读 StateStore 暴露的诊断属性（A 在 P2-32 加的）；拿不到就是 None。"""
            try:
                return getattr(store, name, None)
            except Exception:
                return None

        interval = _store_attr("effective_flush_interval")
        serial_ms = _store_attr("last_serialize_ms")
        payload_bytes = _store_attr("last_payload_bytes")
        consec = _store_attr("consecutive_write_failures")
        failures = _store_attr("flush_failures")
        error = _store_attr("last_write_error")
        extra: list[str] = []
        try:
            if interval is not None:
                extra.append(f"生效落盘间隔：{float(interval):g} 秒")
        except (TypeError, ValueError):
            pass
        try:
            if serial_ms is not None:
                extra.append(f"上次序列化耗时：{float(serial_ms):.1f} ms")
        except (TypeError, ValueError):
            pass
        try:
            if payload_bytes is not None:
                extra.append(f"上次写入体积：{float(payload_bytes) / 1024.0:.0f} KB")
        except (TypeError, ValueError):
            pass
        if extra:
            out.append("；".join(extra))
        try:
            consec_n = int(consec or 0)
        except (TypeError, ValueError):
            consec_n = 0
        try:
            failures_n = int(failures or 0)
        except (TypeError, ValueError):
            failures_n = 0
        error_text = str(error or "").strip()
        reason_text = error_text or "原因未知"
        if consec_n > 0:
            out.append(
                f"{WARN_MARK} 状态落盘连续失败 {consec_n} 次：{reason_text}。"
                "文件可能被杀软 / 同步盘（OneDrive 等）占用；状态仍在内存里，恢复后会自动补写。"
                "（Windows 上可把 data 目录加入排除项，或关掉同步盘对该目录的实时同步。）"
            )
        elif failures_n > 0:
            tail = f"：{error_text}" if error_text else ""
            out.append(f"累计落盘失败 {failures_n} 次（最近一次已恢复）{tail}")
    except Exception:
        pass
    try:
        ids = core.scope.all_user_ids()
        entries = _user_entries(getattr(core, "scope", None))
        if entries is None:
            # 拿不到条目内容（测试替身 / 换过存储实现）：只报人数，不猜空壳数。
            out.append(f"记录在案的用户：{len(ids)} 人")
        else:
            shell_fields = set(_shell_fields())
            shells = sum(
                1
                for entry in entries.values()
                if isinstance(entry, dict) and entry and set(entry) <= shell_fields
            )
            # 同一行给两个数：让用户能判断「人真的多」还是「空壳多」——空壳会随
            # 「整条用户记录保留(天)」清掉，真实用户不会（审核 引擎与诊断 M4）。
            out.append(f"记录在案的用户：{len(ids)} 人（其中只剩称呼的空壳 {shells} 条）")
    except Exception:
        pass
    return out
