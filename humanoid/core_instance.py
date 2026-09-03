"""单个角色的完整 Core 实例。"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from .clock import Clock
from .config import HumanoidConfig
from .llm import LLMGateway, ProviderResolver
from .prompt_builder import PromptBuilder
from .role_scope import RoleScope
from .services.energy import EnergyService
from .services.mood import MoodService
from .services.process import ProcessService
from .services.schedule import ScheduleService
from .services.social import SocialEnergyService
from .services.weather import WeatherService

LOG_PREFIX = "[humanoid_core]"


class _EngineCompat:
    def __init__(self, core: HumanoidCoreInstance):
        self._core = core

    def reset_state(self):
        energy = self._core.energy.reset()
        social = self._core.social.reset()
        cycle_day = self._core.energy.reset_cycle(1)
        return energy, social, cycle_day


class HumanoidCoreInstance:
    def __init__(
        self,
        role_id: str,
        state_store,
        config_provider,
        logger: Any,
        stop_event: asyncio.Event,
        resolver: ProviderResolver,
        gateway: LLMGateway,
        fetch_json=None,
    ):
        self.role_id = role_id
        self._state_store = state_store
        self._config_provider = config_provider
        self._log = logger
        self._stop_event = stop_event
        self._fetch_json = fetch_json
        self.last_activity: Optional[float] = None

        self.resolver = resolver
        self.gateway = gateway

        self._scope = RoleScope(state_store.data, role_id)
        self._scope.set_mark_dirty(state_store.mark_dirty)

        self.clock = Clock(lambda: self.config)

        self.energy = EnergyService(self._scope, config_provider, self.clock)
        self.schedule = ScheduleService(
            self._scope, config_provider, self.clock, self._spawn_background, logger
        )
        self.schedule.set_resolver_gateway(resolver, gateway)

        self.process = ProcessService(
            self._scope, config_provider, self.clock, self.schedule, self._spawn_background
        )
        self.mood = MoodService(
            self._scope,
            config_provider,
            self.clock,
            self._spawn_background,
            gateway=self.gateway,
            logger=logger,
        )
        self.social = SocialEnergyService(self._scope, config_provider, self.clock)
        self.weather = WeatherService(
            self._scope, config_provider, self.clock, fetch_json, logger
        )

        self.prompt_builder = PromptBuilder(self)
        self.engine_compat = _EngineCompat(self)

        self._tasks: set[asyncio.Task] = set()
        self._started = False

    @property
    def config(self) -> HumanoidConfig:
        return self._config_provider()

    def _spawn_background(self, coro, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=f"humanoid-{self.role_id}-{name}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def on_message(self, user_id: str, text: str, is_group: bool = False) -> None:
        self.last_activity = time.time()
        cfg = self.config

        self.energy.advance()
        self.energy.consume_for_message()

        if cfg.social_energy_enabled:
            self.social.consume_for_message()

        self.process.tick()

        self.mood.decay_user(user_id)

        if cfg.last_interaction_mode == "with_last_msg" and text:
            self._scope.set_user(user_id, "last_message", {
                "text": text[:100],
                "timestamp": time.time()
            })
        self._scope.set_user(user_id, "last_interaction", time.time())

        self._dispatch_async(user_id, text, is_group=is_group)

    def _dispatch_async(self, user_id: str, text: str, is_group: bool = False):
        if self.process.needs_update():
            self._spawn_background(self.process.update_async(), "process-update")
        cfg = self.config
        mood_allowed = cfg.mood_enabled and (not is_group or cfg.mood_enabled_in_group)
        if mood_allowed:
            self._spawn_background(
                self.mood.update_from_message_async(user_id, text),
                "mood-update"
            )
        if self.weather.is_stale():
            self._spawn_background(self.weather.refresh_async(), "weather-refresh")

    def build_injection(self, user_id: str, is_group: bool = False) -> str:
        # 不再更新任何状态，仅读取当前快照（状态已在 on_message 中更新）
        return self.prompt_builder.build(user_id, is_group)

    def snapshot(self, user_id: Optional[str] = None) -> dict:
        self.energy.advance()
        self.process.tick()
        if self.process.needs_update():
            self.process.update_sync()

        now = self.clock.now()
        result = {
            "role_id": self.role_id,
            "time": now.isoformat(),
            "today": now.strftime("%Y-%m-%d"),
            "weekday": self.clock.weekday(),
            "city": self.clock.display_city,
            "energy": {
                "value": self.energy.energy,
                "max": self.energy.max_energy,
                "text": self.energy.describe(),
            },
            "cycle": self.energy.cycle_description(),
            "process": self.process.current(),
            "schedule": {
                "slots": self.schedule.current_slots(),
                "source": self.schedule.source_text,
            },
            "weather": self.weather.snapshot(),
            "social_energy": {
                "value": self.social.value,
                "text": self.social.text,
            },
        }
        if user_id:
            m = self.mood.profile(user_id)
            result["mood"] = {
                "affection": m["affection"],
                "libido": m["libido"],
                "aggression": m["aggression"],
                "label": self.mood.label(user_id),
            }
            result["nickname"] = self.mood.nickname(user_id)
        return result

    def status_lines(self, user_id: str) -> list[str]:
        s = self.snapshot(user_id)
        lines = [
            f"🧠 角色：{self.role_id}",
            f"- 时间：{s['time']} 星期{s['weekday']}",
            f"- 城市：{s['city']}",
            f"- 精力：{int(s['energy']['value'])}/{int(s['energy']['max'])} ({s['energy']['text']})",
            f"- 生理：{s['cycle'] or '未开启'}",
        ]
        if s['process']:
            p = s['process']
            lines.append(f"- 当前过程：{p.get('name', '休息')}（持续 {p.get('duration_minutes', 0)} 分钟）")
        if s['weather']:
            lines.append(f"- 天气：{s['weather'].get('weather', '未知')}")
        if s['social_energy']:
            lines.append(f"- 社交能量：{int(s['social_energy']['value'])}% ({s['social_energy']['text']})")
        if user_id and 'mood' in s:
            lines.append(f"- 好感度：{s['mood']['affection']:.1f}（{s['mood']['label']}）")
        return lines

    def schedule_text(self) -> str:
        s = self.snapshot()
        lines = [f"📅 {s['today']} 日程表（{s['schedule']['source']}）："]
        for slot in s['schedule']['slots']:
            lines.append(
                f"{slot.get('start', '')} - {slot.get('end', '')}  "
                f"【{slot.get('event', '')}】@{slot.get('location', '')}"
            )
        if self.schedule.generating:
            lines.append("（正在后台生成新日程…）")
        return "\n".join(lines)

    def process_text(self) -> str:
        p = self.process.current()
        return (
            f"📋 当前过程：{p.get('name', '休息')}\n"
            f"开始：{p.get('started_at', '未知')}\n"
            f"预计结束：{p.get('expected_end', '未知')}\n"
            f"持续时长：{p.get('duration_minutes', 0)} 分钟"
        )

    async def start(self):
        if self._started:
            return
        self._started = True
        self._spawn_background(self._social_loop(), "social-recovery")
        self._spawn_background(self._weather_loop(), "weather-refresh")
        self._spawn_background(self._schedule_loop(), "schedule-refresh")
        self.schedule.current_slots()
        self.process.current()
        self._log.info(f"{LOG_PREFIX} 角色 {self.role_id} 已启动")

    async def stop(self):
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*[t for t in self._tasks if not t.done()], return_exceptions=True)
        self._tasks.clear()
        self._started = False
        self._log.info(f"{LOG_PREFIX} 角色 {self.role_id} 已停止")

    async def _social_loop(self):
        while not self._stop_event.is_set():
            cfg = self.config
            interval = float(cfg.social_energy_recovery_interval_seconds)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            if self._stop_event.is_set():
                break
            if cfg.social_energy_enabled:
                self.social.recover(interval)
                self.social.maybe_daily_reset()

    async def _weather_loop(self):
        while not self._stop_event.is_set():
            await self.weather.refresh_async()
            cfg = self.config
            interval = max(60.0, float(cfg.weather_refresh_minutes) * 60)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _schedule_loop(self):
        while not self._stop_event.is_set():
            if self.schedule.retry_after > 0:
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=min(self.schedule.retry_after, 300)
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            self.schedule.request_refresh()
            await asyncio.sleep(60)