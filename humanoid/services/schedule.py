"""日程服务 —— 根因 2 的修复。

v2.10.2 的读路径是「缺日程就当场同步生成」：`on_llm_request` 钩子 →
`_get_current_context()` → `get_or_update_today_schedule()` → 3 轮 × 60 秒
`asyncio.wait_for`。AstrBot 内联 await 这个钩子且没有超时保护
（`astrbot/core/pipeline/context_utils.py:98`），于是最坏情况下用户的消息要等约 184 秒。

这里把读写彻底分开：

* 读（`current_slots` / `current_slot`）**完全同步**，只碰内存缓存。跨天时立刻就地
  装上确定性模板，保证状态自洽，绝不 await。
* 写（`ensure_fresh`）在后台跑，单飞去重，成功后热替换缓存，下一条消息自然读到新日程。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from typing import Any

from ..clock import Clock
from ..config import HumanoidConfig
from ..data.schedule_templates import get_fallback_schedule
from ..jsonx import extract_json_array
from ..llm import LLMGateway, LLMResult
from ..slots import Slot, coverage_is_complete, find_slot, normalize_slots
from ..state import StateStore

SOURCE_TEMPLATE = "template"
SOURCE_LLM = "llm"
PURPOSE = "日程生成"

# 生成失败后，最少隔这么久再自动重试一次。
# 否则「每条消息都投递一次后台生成」会在模型持续不可用时反复空转，
# 而 provider 冷却只覆盖具名模型，不覆盖全局默认模型（后者不该被本插件拉黑）。
MIN_RETRY_BACKOFF_SECONDS = 60.0

TaskSpawner = Callable[[Any, str], "asyncio.Task[Any]"]


def build_prompt(cfg: HumanoidConfig, today: str, weekday: str) -> str:
    """限制时段数量的日程 prompt。

    v2.10.2 要求「按 15 分钟切片且连续覆盖 24 小时」，等于让模型一次吐 96 个 6 字段
    对象（5min 档 288 个），这是 60 秒超时最直接的诱因。改为要求合并后的活动块，
    粒度退化为「时间点对齐要求」，输出量降到原来的十分之一左右。
    """
    max_slots = cfg.schedule_max_slots
    min_slots = max(4, min(max_slots, max_slots // 2))
    step = cfg.granularity_minutes
    if step > 1:
        example = "、".join(f"{7:02d}:{m:02d}" for m in range(0, 60, step)[:3]) if step < 60 else "07:00、08:00"
        align_hint = f"所有 start / end 必须对齐到 {step} 分钟的整数倍（例如 {example}）。"
    else:
        align_hint = "时间点可以按活动内容自然决定，不必对齐到固定分钟。"

    extra = cfg.schedule_prompt_extra.strip()
    extra_line = f"额外偏好：{extra}\n" if extra else ""

    return (
        f"请为「{cfg.character_personality}」这个人设，规划今天一整天的生活日程。\n"
        f"今天是 {today}，星期{weekday}。\n"
        f"{extra_line}"
        "\n输出要求：\n"
        "1. 只输出一个 JSON 数组，不要 Markdown 代码块，不要任何解释文字。\n"
        "2. 每个元素的结构如下：\n"
        '   {"start": "00:00", "end": "07:30", "event": "睡眠休息", '
        '"location": "卧室", "emotion": "平静", "energy_rate": 0.15}\n'
        f"3. 把连续的同类活动合并成一整块，总共只输出 {min_slots}~{max_slots} 个时段，"
        "不要逐格切分。\n"
        "4. 时段必须首尾相连：第一个从 00:00 开始，最后一个到 24:00 结束，"
        "中间不留空隙、不重叠。\n"
        f"5. {align_hint}\n"
        "6. energy_rate 表示每分钟精力变化：睡眠/休息为正（0.05~0.2），"
        "工作/外出/社交为负（-0.05~-0.15）。\n"
        "7. 地点切换要留出合理的通勤时间。\n"
    )


class ScheduleService:
    """今日日程的唯一入口。读同步、写后台。"""

    __slots__ = (
        "_clock",
        "_config",
        "_gateway",
        "_generating",
        "_lock",
        "_log",
        "_monotonic",
        "_retry_after",
        "_spawn",
        "_state",
        "_task",
        "last_error",
    )

    def __init__(
        self,
        state: StateStore,
        config_provider: Callable[[], HumanoidConfig],
        clock: Clock,
        gateway: LLMGateway,
        logger: Any = None,
        spawn: TaskSpawner | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._state = state
        self._config = config_provider
        self._clock = clock
        self._gateway = gateway
        self._log = logger
        self._spawn = spawn
        self._monotonic = monotonic
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[Any] | None = None
        self._generating = False
        self._retry_after = 0.0
        self.last_error: str = ""

    # ---------- 读路径：同步，绝不 await ----------

    def current_slots(self) -> list[Slot]:
        """今天的日程。跨天或缓存为空时就地装上确定性模板并返回。"""
        today = self._clock.today_str()
        data = self._state.data
        slots = data.get("daily_schedule")
        if data.get("today_date") == today and isinstance(slots, list) and slots:
            return slots
        return self._install(self._template_slots(today), today, SOURCE_TEMPLATE)

    def current_slot(self, minutes: int | None = None) -> Slot:
        if minutes is None:
            now = self._clock.now()
            minutes = now.hour * 60 + now.minute
        return find_slot(self.current_slots(), minutes)

    @property
    def source(self) -> str:
        return str(self._state.get("schedule_source", "") or SOURCE_TEMPLATE)

    @property
    def source_text(self) -> str:
        return "大模型生成" if self.source == SOURCE_LLM else "内置模板"

    @property
    def generated_at(self) -> str:
        return str(self._state.get("schedule_generated_at", "") or "")

    @property
    def generating(self) -> bool:
        """真的有一次模型调用在飞行中。诊断报告用这个，而不是「有任务对象存在」。"""
        return self._generating

    @property
    def busy(self) -> bool:
        """生成中，或还有已投递但未跑完的刷新任务。生命周期管理用这个。"""
        return self._generating or bool(self._task and not self._task.done())

    def _template_slots(self, today: str) -> list[Slot]:
        cfg = self._config()
        raw = get_fallback_schedule(today)
        return normalize_slots(raw, max_slots=max(8, cfg.schedule_max_slots)) or raw

    def _install(self, slots: list[Slot], today: str, source: str) -> list[Slot]:
        data = self._state.data
        data["today_date"] = today
        data["daily_schedule"] = slots
        data["schedule_source"] = source
        data["schedule_generated_at"] = self._clock.now().strftime("%Y-%m-%d %H:%M:%S")
        self._state.mark_dirty()
        return slots

    def _has_llm_schedule(self, today: str) -> bool:
        data = self._state.data
        return (
            data.get("today_date") == today
            and data.get("schedule_source") == SOURCE_LLM
            and bool(data.get("daily_schedule"))
        )

    # ---------- 写路径：后台，单飞 ----------

    def request_refresh(self, *, force: bool = False, ignore_cooldown: bool = False) -> bool:
        """投递一次后台刷新。已有任务在跑、或还在失败退避窗口内，都直接返回 False。"""
        if self._task is not None and not self._task.done():
            return False
        cfg = self._config()
        today = self._clock.today_str()
        if not cfg.use_llm_schedule:
            return False
        if not force and self._has_llm_schedule(today):
            return False
        if not force and self.retry_after > 0:
            return False
        coro = self.ensure_fresh(force=force, ignore_cooldown=ignore_cooldown)
        name = "humanoid-schedule-refresh"
        try:
            self._task = self._spawn(coro, name) if self._spawn else asyncio.create_task(coro, name=name)
        except RuntimeError:
            # 没有运行中的事件循环（理论上不该发生）。关掉协程，避免 never-awaited 警告。
            coro.close()
            return False
        return True

    @property
    def retry_after(self) -> float:
        """距离下一次允许自动重试还有多少秒。0 表示可以立刻试。"""
        return max(0.0, self._retry_after - self._monotonic())

    async def ensure_fresh(self, *, force: bool = False, ignore_cooldown: bool = False) -> bool:
        """确保今天有一份日程；需要时调模型重建。返回是否真的换上了新日程。

        并发调用会被锁 + 复查折叠成一次生成（single-flight）。
        """
        self.current_slots()  # 先让本地状态自洽，即便后面生成失败也有日程可用
        cfg = self._config()
        if not cfg.use_llm_schedule:
            return False
        today = self._clock.today_str()
        if not force and self._has_llm_schedule(today):
            return False
        if not force and self.retry_after > 0:
            return False

        async with self._lock:
            if not force and self._has_llm_schedule(today):
                return False
            return await self._generate(cfg, today, ignore_cooldown)

    async def _generate(self, cfg: HumanoidConfig, today: str, ignore_cooldown: bool) -> bool:
        self._generating = True
        try:
            ok = await self._generate_inner(cfg, today, ignore_cooldown)
        finally:
            self._generating = False
        if ok:
            self._retry_after = 0.0
        else:
            backoff = max(
                MIN_RETRY_BACKOFF_SECONDS, float(cfg.schedule_provider_cooldown_minutes) * 60.0
            )
            self._retry_after = self._monotonic() + backoff
            self._info(f"日程生成失败，{backoff / 60:.0f} 分钟内不再自动重试（/重置日程 可立即重试）")
        return ok

    async def _generate_inner(self, cfg: HumanoidConfig, today: str, ignore_cooldown: bool) -> bool:
        prompt = build_prompt(cfg, today, self._clock.weekday())
        if cfg.debug_mode:
            self._debug(f"日程 prompt:\n{prompt}")

        result: LLMResult = await self._gateway.generate(
            prompt=prompt,
            chain=cfg.schedule_provider_ids,
            allow_global=cfg.schedule_allow_global_fallback,
            timeout=float(cfg.schedule_llm_timeout_seconds),
            attempts_per_provider=cfg.schedule_generation_max_attempts,
            retry_interval=float(cfg.schedule_retry_interval_seconds),
            purpose=PURPOSE,
            ignore_cooldown=ignore_cooldown,
        )
        if not result.ok:
            self.last_error = result.summary()
            self._warn(f"日程生成失败，继续沿用现有日程（{self.source_text}）：{self.last_error}")
            return False

        if cfg.debug_mode:
            self._debug(f"日程原始响应（前 400 字）：{result.text[:400]}")

        parsed = extract_json_array(result.text)
        slots = (
            normalize_slots(
                parsed,
                max_slots=cfg.schedule_max_slots,
                align_minutes=cfg.granularity_minutes,
            )
            if parsed is not None
            else None
        )
        if not slots or not coverage_is_complete(slots):
            self.last_error = f"{result.label}({result.provider_id}) 返回的内容无法解析成有效日程"
            self._warn(f"{self.last_error}；片段：{result.text[:160]!r}")
            return False

        self._install(slots, today, SOURCE_LLM)
        self.last_error = ""
        self._info(f"日程已更新：{len(slots)} 个时段，来自 {result.label}({result.provider_id})")
        return True

    async def aclose(self) -> None:
        task, self._task = self._task, None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # ---------- 诊断 ----------

    def status(self) -> dict[str, Any]:
        last = self._gateway.last_result(PURPOSE)
        return {
            "date": str(self._state.get("today_date", "") or ""),
            "slots": len(self.current_slots()),
            "source": self.source,
            "source_text": self.source_text,
            "generated_at": self.generated_at,
            "last_error": self.last_error,
            "last_attempt": last.summary() if last else "",
            "generating": self.generating,
            "retry_after": self.retry_after,
        }

    # ---------- 日志 ----------

    def _info(self, message: str) -> None:
        if self._log is not None:
            self._log.info(f"[humanoid_core] {message}")

    def _warn(self, message: str) -> None:
        if self._log is not None:
            self._log.warning(f"[humanoid_core] {message}")

    def _debug(self, message: str) -> None:
        if self._log is not None:
            self._log.debug(f"[humanoid_core] {message}")

