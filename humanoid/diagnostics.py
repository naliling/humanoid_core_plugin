"""`/拟人诊断` 的报告生成。

存在的理由：v2.10.2 里「专用模型不生效」时唯一的线索是一条只在 debug_mode 打开才会
出现的 debug 日志，用户根本无从判断是 id 写错、模型不可用、还是回退开关拦住了。
这条指令把整条链路一次性摊开。
"""

from __future__ import annotations

from typing import Any

from .config import HumanoidConfig
from .llm import GLOBAL_LABEL, LLMGateway, ProviderResolver
from .services.mood import PURPOSE as MOOD_PURPOSE
from .services.schedule import PURPOSE as SCHEDULE_PURPOSE

OK_MARK = "✓"
BAD_MARK = "✗"


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
) -> str:
    for label, provider_id in chain:
        if gateway.cooldown_remaining(provider_id) > 0:
            continue
        if resolver.resolve(provider_id) is not None:
            return f"{label}({provider_id})"
    if allow_global:
        provider = resolver.resolve_global(None)
        if provider is not None:
            return f"{GLOBAL_LABEL}({resolver.id_of(provider)})"
    return "无可用模型 → 将使用内置日程模板"


def build_report(
    *,
    cfg: HumanoidConfig,
    resolver: ProviderResolver,
    gateway: LLMGateway,
    schedule_status: dict[str, Any],
    version: str,
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
        resolver, gateway, cfg.schedule_provider_ids, cfg.schedule_allow_global_fallback
    )
    lines.append(f"- 本次实际将使用: {picked}")

    cooldowns = gateway.cooldowns()
    if cooldowns:
        detail = "，".join(f"{pid} 剩余 {rem / 60:.0f} 分钟" for pid, rem in cooldowns.items())
        lines.append(f"- 冷却中: {detail}")
    else:
        lines.append("- 冷却中: 无")

    lines += ["", "【今日日程】"]
    lines.append(f"- 日期: {schedule_status.get('date') or '未生成'}")
    lines.append(
        f"- 来源: {schedule_status.get('source_text', '')}"
        f"，共 {schedule_status.get('slots', 0)} 个时段"
    )
    if schedule_status.get("generated_at"):
        lines.append(f"- 生成时间: {schedule_status['generated_at']}")
    if schedule_status.get("generating"):
        lines.append("- 状态: 正在后台向模型请求新日程")
    retry_after = float(schedule_status.get("retry_after") or 0.0)
    if retry_after > 0:
        lines.append(f"- 自动重试: {retry_after / 60:.0f} 分钟后（/重置日程 可立即重试）")
    last = gateway.last_result(SCHEDULE_PURPOSE)
    if last is not None:
        lines.append(f"- 上次尝试: {last.summary()}")
    if schedule_status.get("last_error"):
        lines.append(f"- 上次失败原因: {schedule_status['last_error']}")

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

    lines += [
        "",
        "【关键参数】",
        f"- 单次生成超时 {cfg.schedule_llm_timeout_seconds}s"
        f"，每个模型尝试 {cfg.schedule_generation_max_attempts} 次"
        f"，重试间隔 {cfg.schedule_retry_interval_seconds}s",
        f"- 时段上限 {cfg.schedule_max_slots}，时间对齐 {cfg.schedule_time_granularity}",
        f"- 失败冷却 {cfg.schedule_provider_cooldown_minutes} 分钟",
        f"- 大模型日程 {'开启' if cfg.use_llm_schedule else '关闭'}"
        f"，调试日志 {'开启' if cfg.debug_mode else '关闭'}",
    ]

    if not available:
        lines += ["", "→ 请先在 AstrBot 的「服务提供商」里配置至少一个对话模型。"]
    elif cfg.schedule_provider_name and cfg.schedule_provider_name not in available:
        lines += ["", "→ 首选模型 id 不在可用列表里：请在插件配置里用下拉框重新选择。"]

    return "\n".join(lines)
