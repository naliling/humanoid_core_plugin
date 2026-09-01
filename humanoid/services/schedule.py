"""日程服务 - 使用 RoleScope 版本。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from ..clock import Clock
from ..config import HumanoidConfig
from ..data.schedule_templates import get_fallback_schedule
from ..jsonx import extract_json_array
from ..llm import LLMGateway, LLMResult, ProviderResolver
from ..role_scope import RoleScope
from ..slots import Slot, coverage_is_complete, find_slot, normalize_slots

PURPOSE = "日程生成"
SOURCE_TEMPLATE = "template"
SOURCE_LLM = "llm"
MIN_RETRY_BACKOFF_SECONDS = 60.0


def build_prompt(cfg: HumanoidConfig, today: str, weekday: str) -> str:
    max_slots = cfg.schedule_max_slots
    min_slots = max(4, min(max_slots, max_slots // 2))
    step = cfg.granularity_minutes
    if step > 1:
        align_hint = f"所有 start / end 必须对齐到 {step} 分钟的整数倍。"
    else:
        align_hint = "时间点可以自然决定，不必对齐。"

    extra = cfg.schedule_prompt_extra.strip()
    extra_line = f"额外偏好：{extra}\n" if extra else ""

    return (
        f"请为「{cfg.character_personality}」这个人设，规划今天一整天的生活日程。\n"
        f"今天是 {today}，星期{weekday}。\n"
        f"{extra_line}"
        "\n输出要求：\n"
        "1. 只输出一个 JSON 数组，不要 Markdown 代码块。\n"
        "2. 每个元素：{\"start\": \"00:00\", \"end\": \"07:30\", \"event\": \"睡眠休息\", "
        "\"location\": \"卧室\", \"emotion\": \"平静\", \"energy_rate\": 0.15}\n"
        f"3. 总共输出 {min_slots}~{max_slots} 个时段，把连续同类活动合并。\n"
        "4. 时段必须首尾相连：00:00 开始，24:00 结束，不重叠。\n"
        f"5. {align_hint}\n"
        "6. energy_rate：睡眠/休息为正（0.05~0.2），工作/外出/社交为负（-0.05~-0.15）。\n"
    )


class ScheduleService:
    def __init__(
        self,
        scope: RoleScope,
        config_provider: Callable[[], HumanoidConfig],
        clock: Clock,
        spawn_fn=None,
        logger=None,
    ):
        self._scope = scope
        self._config = config_provider
        self._clock = clock
        self._log = logger
        self._spawn = spawn_fn
        self._generating = False
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self.last_error = ""
        self._retry_after = 0.0
        self._monotonic = time.monotonic

        self.resolver = None
        self.gateway = None

    def set_resolver_gateway(self, resolver, gateway):
        self.resolver = resolver
        self.gateway = gateway

    @property
    def config(self) -> HumanoidConfig:
        return self._config()

    def current_slots(self) -> list[Slot]:
        today = self._clock.today_str()
        data = self._scope.self_state
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
        return str(self._scope.get_self("schedule_source", SOURCE_TEMPLATE))

    @property
    def source_text(self) -> str:
        return "大模型生成" if self.source == SOURCE_LLM else "内置模板"

    @property
    def generating(self) -> bool:
        return self._generating

    @property
    def retry_after(self) -> float:
        return max(0.0, self._retry_after - self._monotonic())

    def _template_slots(self, today: str) -> list[Slot]:
        cfg = self.config
        raw = get_fallback_schedule(today)
        return normalize_slots(raw, max_slots=max(8, cfg.schedule_max_slots)) or raw

    def _install(self, slots: list[Slot], today: str, source: str) -> list[Slot]:
        self._scope.update_self(
            today_date=today,
            daily_schedule=slots,
            schedule_source=source,
            schedule_generated_at=self._clock.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        return slots

    def request_refresh(self, force: bool = False, ignore_cooldown: bool = False) -> bool:
        if self._task and not self._task.done():
            return False
        cfg = self.config
        today = self._clock.today_str()
        if not cfg.use_llm_schedule:
            return False
        if not force and self._scope.get_self("schedule_source") == SOURCE_LLM:
            return False
        if not force and self.retry_after > 0:
            return False

        coro = self.ensure_fresh(force=force, ignore_cooldown=ignore_cooldown)
        name = f"humanoid-schedule-refresh-{self._scope.role_id}"
        if self._spawn:
            self._task = self._spawn(coro, name)
        else:
            self._task = asyncio.create_task(coro, name=name)
        return True

    async def ensure_fresh(self, force: bool = False, ignore_cooldown: bool = False) -> bool:
        self.current_slots()
        cfg = self.config
        if not cfg.use_llm_schedule:
            return False
        today = self._clock.today_str()
        if not force and self._scope.get_self("schedule_source") == SOURCE_LLM:
            return False
        if not force and self.retry_after > 0:
            return False

        async with self._lock:
            if not force and self._scope.get_self("schedule_source") == SOURCE_LLM:
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
            backoff = max(MIN_RETRY_BACKOFF_SECONDS, float(cfg.schedule_provider_cooldown_minutes) * 60)
            self._retry_after = self._monotonic() + backoff
        return ok

    async def _generate_inner(self, cfg: HumanoidConfig, today: str, ignore_cooldown: bool) -> bool:
        if self.gateway is None:
            self.last_error = "LLM Gateway 未初始化"
            return False

        prompt = build_prompt(cfg, today, self._clock.weekday())
        result: LLMResult = await self.gateway.generate(
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
            return False

        parsed = extract_json_array(result.text)
        slots = (
            normalize_slots(parsed, max_slots=cfg.schedule_max_slots, align_minutes=cfg.granularity_minutes)
            if parsed is not None
            else None
        )
        if not slots or not coverage_is_complete(slots):
            self.last_error = f"无法解析日程：{result.text[:160]}"
            return False

        self._install(slots, today, SOURCE_LLM)
        self.last_error = ""
        return True

    def status(self) -> dict:
        return {
            "date": self._scope.get_self("today_date", ""),
            "slots": len(self.current_slots()),
            "source": self.source,
            "source_text": self.source_text,
            "generated_at": self._scope.get_self("schedule_generated_at", ""),
            "last_error": self.last_error,
            "generating": self.generating,
            "retry_after": self.retry_after,
        }

    async def aclose(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass