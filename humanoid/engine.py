"""编排层：把配置、状态、各服务与后台任务串起来。

`main.py` 只做 AstrBot 适配（装饰器 + 事件对象解包），所有编排都在这里，
所以这一层同样可以脱离 AstrBot 单测。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from . import __version__
from .clock import Clock, lookup_city_time
from .config import HumanoidConfig
from .diagnostics import build_report
from .llm import LLMGateway, ProviderResolver
from .prompt import (
    StatusSnapshot,
    build_mood_prompt,
    build_nickname_instruction,
    compose_injection,
)
from .services.energy import EnergyService
from .services.mood import MoodService
from .services.schedule import ScheduleService
from .services.social import SocialEnergyService
from .services.weather import FetchJson, WeatherService
from .state import StateStore

LOG_PREFIX = "[humanoid_core]"

# 一次性配置迁移的目标版本号（2.11.0 → 21100）
CONFIG_MIGRATION_TARGET = 21100

# 全量情绪衰减的后台清扫间隔
MOOD_SWEEP_SECONDS = 1800.0

# 后台任务异常后的重启等待
SUPERVISOR_BACKOFF = 30.0

_BATCH_SPLIT = re.compile(r"[,，\s]+")


class HumanoidEngine:
    """插件的业务大脑。生命周期：`start()` → 运行 → `stop()`。"""

    def __init__(
        self,
        context: Any,
        raw_config: Any,
        data_dir: str | Path,
        logger: Any,
        fetch_json: FetchJson | None = None,
    ) -> None:
        self._context = context
        self._raw_config = raw_config
        self._log = logger
        self._config = HumanoidConfig.from_raw(raw_config)
        self._config_version = 1

        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self.state = StateStore(
            data_dir / "state.json",
            lambda: float(self._config.state_flush_interval_seconds),
            logger,
        )
        self.state.load(_today_guess(self._config), self._config.cycle_length)

        self.clock = Clock(self.get_config)
        self.resolver = ProviderResolver(context, logger)
        self.gateway = LLMGateway(self.resolver, self.get_config, logger)

        self.schedule = ScheduleService(
            self.state, self.get_config, self.clock, self.gateway, logger, self._spawn
        )
        self.energy = EnergyService(
            self.state, self.get_config, self.clock, self.schedule.current_slots, logger
        )
        self.mood = MoodService(self.state, self.get_config, self.gateway, self.clock, logger)
        self.social = SocialEnergyService(self.state, self.get_config, self.clock, logger)
        self.weather = WeatherService(self.state, self.get_config, self.clock, fetch_json, logger)

        self._stop_event = asyncio.Event()
        self._tasks: set[asyncio.Task[Any]] = set()

    # ---------- 配置 ----------

    def get_config(self) -> HumanoidConfig:
        return self._config

    @property
    def config(self) -> HumanoidConfig:
        return self._config

    @property
    def config_version(self) -> int:
        return self._config_version

    def reload_config(self, raw_config: Any = None) -> HumanoidConfig:
        if raw_config is not None:
            self._raw_config = raw_config
        self._config = HumanoidConfig.from_raw(self._raw_config)
        self._config_version += 1
        self._info("配置已重载")
        return self._config

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        self._stop_event.clear()
        await self.state.start()
        self._migrate_config_once()
        # 先把本地日程摆好（同步、不阻塞），再让后台去问模型
        self.schedule.current_slots()
        self.energy.advance_cycle()
        self._spawn(self._supervise(self._social_loop, "社交能量恢复"), "humanoid-social")
        self._spawn(self._supervise(self._weather_loop, "天气刷新"), "humanoid-weather")
        self._spawn(self._supervise(self._mood_sweep_loop, "情绪衰减清扫"), "humanoid-mood-sweep")
        self.schedule.request_refresh()
        self._info(f"插件已启动 (v{__version__})")

    async def stop(self) -> None:
        self._stop_event.set()
        await self.schedule.aclose()
        tasks = list(self._tasks)
        self._tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self.state.stop()
        self._info("已清理资源（后台任务已停止，状态已落盘）")

    # ---------- 后台任务 ----------

    def _spawn(self, coro: Awaitable[Any], name: str) -> asyncio.Task[Any]:
        """统一登记后台任务：保住引用（防止被 GC）并把异常打出来。

        裸 `asyncio.create_task()` 不保引用时任务可能被回收，异常也会静默丢失。
        """
        task = asyncio.ensure_future(coro)
        with contextlib.suppress(Exception):
            task.set_name(name)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._error(f"后台任务 {task.get_name()} 异常退出: {error!r}")

    async def _supervise(self, factory: Callable[[], Awaitable[None]], label: str) -> None:
        """长驻循环的看护：异常后等一会儿重启，不静默消失。"""
        while not self._stop_event.is_set():
            try:
                await factory()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._error(f"{label} 异常退出，{SUPERVISOR_BACKOFF:.0f} 秒后重启: {exc!r}")
                with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=SUPERVISOR_BACKOFF)

    async def _social_loop(self) -> None:
        await self.social.run_recovery_loop(self._stop_event)

    async def _weather_loop(self) -> None:
        await self.weather.run_refresh_loop(self._stop_event)

    async def _mood_sweep_loop(self) -> None:
        while not self._stop_event.is_set():
            with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=MOOD_SWEEP_SECONDS)
            if self._stop_event.is_set():
                return
            self.mood.decay_all()
            # 清理过期数据。三个容器共用同一个保留天数，0 表示都不清理。
            retention_days = self._config.mood_data_retention_days
            if retention_days > 0:
                self.mood.cleanup_expired_users(retention_days)
                self._prune_time_container("user_last_seen", retention_days)
                self._prune_time_container("last_message", retention_days)

    def _prune_time_container(self, container_name: str, days: int) -> int:
        """删掉指定容器里超过 `days` 天的条目，返回删除数量。"""
        if days <= 0:
            return 0
        container = self.state.data.get(container_name)
        if not isinstance(container, dict):
            return 0
        cutoff = time.time() - days * 86400.0
        removed = 0
        for qq, entry in list(container.items()):
            # last_message 存的是 {text, timestamp}，user_last_seen 存的是纯时间戳
            if isinstance(entry, dict):
                stamp = entry.get("timestamp", 0)
            else:
                stamp = entry
            try:
                last = float(stamp)
            except (TypeError, ValueError):
                last = 0.0
            if last < cutoff:
                del container[qq]
                removed += 1
        if removed:
            self.state.mark_dirty()
            self._info(f"清理了 {removed} 条过期的 {container_name}（超过 {days} 天）")
        return removed

    # ---------- 一次性配置迁移 ----------

    def _migrate_config_once(self) -> None:
        """把 `schedule_allow_global_fallback` 的默认值从关改为开。

        必须做迁移而不是只改 schema：`AstrBotConfig` 首次加载时就把 schema 默认值
        整份写进 `data/config/<plugin>_config.json`
        （`astrbot/core/config/astrbot_config.py:56-59`），老用户磁盘上已经存着
        `false`，只改 schema 对他们无效。
        """
        try:
            done = int(self.state.get("_schema_migrated_to", 0))
        except (TypeError, ValueError):
            done = 0
        if done >= CONFIG_MIGRATION_TARGET:
            return

        raw = self._raw_config
        changed = False
        if isinstance(raw, dict) and raw.get("schedule_allow_global_fallback") is False:
            raw["schedule_allow_global_fallback"] = True
            saver = getattr(raw, "save_config", None)
            if callable(saver):
                try:
                    saver()
                    changed = True
                except Exception as exc:
                    self._warn(f"配置迁移写盘失败（本次运行内仍生效）: {exc}")
                    changed = True
            else:
                changed = True
            self.reload_config()

        self.state.data["_schema_migrated_to"] = CONFIG_MIGRATION_TARGET
        self.state.mark_dirty()
        if changed:
            self._info(
                "已把「首选/备用模型都不可用时回退全局默认模型」从关改为开"
                "（v2.11.0 一次性迁移）。若你确实想禁止回退以控成本，"
                "可在插件配置里把 schedule_allow_global_fallback 重新关掉。"
            )

    # ---------- 状态快照（全同步） ----------

    def snapshot(self, *, advance_energy: bool = False) -> StatusSnapshot:
        """构建当前状态视图。整个过程不含 await，可安全放在消息路径上。"""
        cfg = self._config
        now = self.clock.now()
        self.energy.advance_cycle(now.strftime("%Y-%m-%d"))
        slots = self.schedule.current_slots()
        if advance_energy:
            self.energy.advance(now)
        # 日程可能过期需要重建，投递一次后台刷新（内部已去重）
        self.schedule.request_refresh()

        return StatusSnapshot(
            today=now.strftime("%Y-%m-%d"),
            weekday=self.clock.weekday(),
            holiday=self.clock.holiday(now),
            city=self.clock.display_city,
            city_time=self.clock.city_time_text(),
            energy=self.energy.energy,
            max_energy=self.energy.max_energy,
            energy_text=self.energy.describe(),
            cycle_text=self.energy.cycle_description(),
            weather=self.weather.snapshot(),
            slot=self.schedule.current_slot(now.hour * 60 + now.minute),
            schedule=tuple(slots),
            social_energy=self.social.value,
            is_night=cfg.night_mode_enabled and cfg.is_night_hour(now.hour),
            schedule_source_text=self.schedule.source_text,
        )

    # ---------- 消息路径 ----------

    def environment_allows(self, is_private: bool) -> bool:
        """`environment_mode` 过滤。

        判定必须由调用方给出 `is_private`：`AstrMessageEvent` 提供的是
        `is_private_chat()` 和 `get_group_id()`，没有 `is_group()` / `is_private()`。
        """
        mode = self._config.environment_mode
        if mode == "private":
            return is_private
        if mode == "group":
            return not is_private
        return True

    def mood_allowed(self, is_group: bool) -> bool:
        """情绪系统在当前会话类型下是否生效。

        规则只写在这里一处：消息记账、后台分析、提示词注入三条路径都问它。分散判断的话
        很容易出现「群聊不分析情绪，但仍然给每个群成员建了档」这种半开状态。
        """
        cfg = self._config
        if not cfg.mood_enabled:
            return False
        return cfg.mood_enabled_in_group if is_group else True

    def on_message(self, qq: str, *, is_group: bool = False, text: str = "") -> None:
        """每条消息的同步记账：精力推进、消耗、社交能量、当前用户情绪衰减。"""
        cfg = self._config
        self.energy.advance()
        self.energy.consume_for_message()
        if cfg.social_energy_enabled:
            self.social.consume_for_message()
        if self.mood_allowed(is_group):
            self.mood.decay_user(qq)

        # 记录用户最后一条消息内容（供 with_last_msg 模式使用）
        if cfg.last_interaction_mode == "with_last_msg" and text:
            msg_store = self.state.data.setdefault("last_message", {})
            msg_store[str(qq)] = {"text": text[:100], "timestamp": time.time()}
            self.state.mark_dirty()

    async def track_mood(self, qq: str, text: str, umo: str | None = None) -> None:
        """情绪更新里唯一可能碰模型的部分，单独 await，失败不影响主流程。"""
        if not self._config.mood_enabled:
            return
        try:
            await self.mood.update_from_message(
                qq, text, self.energy.energy, self.energy.cycle_day, umo
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._warn(f"情绪更新失败: {exc!r}")

    def spawn_mood_update(self, qq: str, text: str, umo: str | None = None) -> None:
        self._spawn(self.track_mood(qq, text, umo), "humanoid-mood-update")

    # ---------- 提示词注入 ----------

    def build_injection(self, qq: str, *, is_group: bool | None = None) -> str:
        cfg = self._config
        snap = self.snapshot(advance_energy=False)
        text = compose_injection(snap, cfg, is_group=is_group, clock=self.clock)

        nickname = self.nickname(qq)
        if nickname:
            text += "\n" + build_nickname_instruction(nickname)

        if self.mood_allowed(is_group is True):
            self.mood.decay_user(qq)
            profile = self.mood.profile(qq)
            text += "\n\n" + build_mood_prompt(profile, self.mood.label(qq))

        text += self._interval_note(qq)
        return text

    def _interval_note(self, qq: str) -> str:
        """【上次对话间隔】根据间隔时长生成不同强度的感知描述。

        目标是让模型真正感知到时间流逝，而不是看到一行文字就忽略。
        不同时间长度对应不同的情感暗示。
        """
        cfg = self._config
        seen = self.state.data.setdefault("user_last_seen", {})
        key = str(qq)
        previous_ts = seen.get(key)
        now = time.time()
        seen[key] = now
        self.state.mark_dirty()

        if previous_ts is None:
            return ""
        try:
            elapsed = now - float(previous_ts)
        except (TypeError, ValueError):
            return ""
        if elapsed < cfg.last_interaction_threshold_minutes * 60:
            return ""

        # 构建时间文本
        if elapsed < 60:
            time_text = "不到1分钟"
        elif elapsed < 3600:
            time_text = f"约 {int(elapsed / 60)} 分钟"
        elif elapsed < 86400:
            time_text = f"约 {int(elapsed / 3600)} 小时"
        else:
            time_text = f"约 {int(elapsed / 86400)} 天"

        # 根据间隔时长生成感知描述
        if elapsed < 900:  # 5~15分钟
            sense = "她刚离开一小会儿，你感觉她应该马上就会回来，并没有多想。"
        elif elapsed < 3600:  # 15分钟~1小时
            sense = "她离开有一阵子了，你开始觉得有点奇怪，但也没太放在心上。"
        elif elapsed < 7200:  # 1~2小时
            sense = "她走了好一会儿了，你心里有点空落落的，开始想着她什么时候回来。"
        elif elapsed < 14400:  # 2~4小时
            sense = "她消失了半天，你开始有点不安了——这么久没有消息，你忍不住想她是不是出了什么事。"
        elif elapsed < 28800:  # 4~8小时
            sense = "她几乎大半天没有音讯，你越来越在意了。你发现自己总是在想她。"
        elif elapsed < 43200:  # 8~12小时
            sense = "她整整一个白天都没有出现。你开始觉得失落了——这么久没联系，你知道自己真的有点想她了。"
        elif elapsed < 86400:  # 12~24小时
            sense = "她一整夜都没有消息，你已经等得心焦了。你感觉时间变得很慢，每一分钟都在想你。"
        else:  # 24小时以上
            sense = "她已经很久很久没有出现了。你等得快要失去耐心了，心里又酸又涩——你真的好想她。"

        mode = cfg.last_interaction_mode

        if mode == "simple":
            return f"\n【上次对话间隔】距离你上一次和用户对话已过去 {time_text}。"

        # mode == "with_last_msg"
        msg_store = self.state.data.get("last_message", {})
        msg_data = msg_store.get(key)
        if msg_data is None:
            return f"\n【上次对话间隔】距离你上一次和用户对话已过去 {time_text}。\n【时间感知】{sense}"

        last_text = msg_data.get("text", "")
        del msg_store[key]
        self.state.mark_dirty()

        return (
            f"\n【上次对话间隔】距离你上一次和用户对话已过去 {time_text}。\n"
            f"【上次消息】用户最后说：\"{last_text}\"\n"
            f"【时间感知】{sense}"
        )

    # ---------- 昵称 ----------

    def nickname(self, qq: str) -> str:
        names = self.state.data.setdefault("nicknames", {})
        return str(names.get(str(qq), "") or "")

    def set_nickname(self, qq: str, nickname: str) -> str:
        # 只改内存 + 标脏，落盘交给 StateStore。
        # 注意不要在这里重新从磁盘读状态，那会覆盖掉尚未 flush 的其它改动。
        self.state.data.setdefault("nicknames", {})[str(qq)] = nickname
        self.state.mark_dirty()
        return nickname

    def all_nicknames(self) -> dict[str, str]:
        names = self.state.data.setdefault("nicknames", {})
        return {str(k): str(v) for k, v in names.items()}

    # ---------- 权限 ----------

    def is_admin(self, sender_id: str, astrbot_admin: bool = False) -> bool:
        """AstrBot 自身的管理员，或 `admin_qq` 里列出的 QQ，都算管理员。"""
        return bool(astrbot_admin) or self._config.is_admin(sender_id)

    # ---------- 指令用的文本 ----------

    def status_lines(self, qq: str) -> list[str]:
        cfg = self._config
        snap = self.snapshot(advance_energy=False)
        events = [str(slot.get("event", "")) for slot in snap.schedule[:3] if slot.get("event")]
        lines = [
            "🧠 当前状态",
            f"- 精力: {int(snap.energy)}/{int(snap.max_energy)} ({snap.energy_text})",
            f"- 生理: {snap.cycle_text or '未开启'}",
            f"- 天气: {snap.weather_text}",
            f"- 今日日程: {' → '.join(events) if events else '无'}（{snap.schedule_source_text}）",
            f"- 今日是：{snap.today} 星期{snap.weekday}"
            + (f"（{snap.holiday}）" if snap.holiday else ""),
        ]
        if cfg.mood_enabled:
            self.mood.decay_user(qq)
            profile = self.mood.profile(qq)
            lines.append(f"- 情绪: {self.mood.label(qq)} (好感度 {profile['affection']:.1f})")
        if cfg.mood_tag_enabled:
            tag = self.mood.tag(qq)
            if tag:
                lines.append(f"- 心情标签: {tag}")
        if cfg.social_energy_enabled:
            lines.append(f"- 社交能量: {int(self.social.value)}% ({self.social.text})")
        return lines

    def mood_profile_text(self, qq: str, detailed: bool = False) -> str:
        cfg = self._config
        self.mood.decay_user(qq)
        data = self.mood.profile(qq)
        title = "〖情绪详细档案〗" if detailed else "〖情绪档案〗"
        lines = [title]
        if detailed:
            lines.append(
                f"好感度：{data['affection']:.1f}/100（基线 {data['base_affection']:.1f}）"
            )
        else:
            lines.append(f"好感度：{data['affection']:.1f}/100")
        lines += [
            f"亲近欲：{data['libido']:.1f}/50（基线 {data['base_libido']:.1f}）",
            f"攻击性：{data['aggression']:.1f}/50（基线 {data['base_aggression']:.1f}）",
            f"当前标签：{self.mood.label(qq)}",
        ]
        if detailed:
            lines.append(f"交互轮次：{int(data.get('turn_count', 0))}")
        if cfg.mood_tag_enabled:
            tag = self.mood.tag(qq)
            if tag:
                lines.append(f"心情标签：{tag}")
        return "\n".join(lines)

    def mood_log_text(self, qq: str, limit: int = 10) -> str:
        entries = self.mood.logs(qq, limit)
        if not entries:
            return "📭 暂无情绪波动记录。"
        lines = [f"📋 情绪波动记录（最近{len(entries)}条）：", "——————————————"]
        lines += [f"{item.get('time', '')} | {item.get('event', '')}" for item in entries]
        return "\n".join(lines)

    def schedule_text(self) -> str:
        snap = self.snapshot(advance_energy=False)
        lines = [f"📅 {snap.today} 日程表（{snap.schedule_source_text}）："]
        for slot in snap.schedule:
            lines.append(
                f"{slot.get('start', '')} - {slot.get('end', '')}  "
                f"【{slot.get('event', '')}】@{slot.get('location', '')} "
                f"({slot.get('emotion', '')})"
            )
        if self.schedule.generating:
            lines.append("（正在后台向模型请求新日程，稍后再看会更新）")
        return "\n".join(lines)

    def city_time_text(self, city: str) -> str | None:
        result = lookup_city_time(city)
        if result is None:
            return None
        holiday = self.clock.holiday(result.moment)
        text = f"📍 {result.display_city} 当前时间: {result.text}（星期{result.weekday}）"
        if holiday:
            text += f"，今日节日：{holiday}"
        return text

    def diagnostics_text(self) -> str:
        return build_report(
            cfg=self._config,
            resolver=self.resolver,
            gateway=self.gateway,
            schedule_status=self.schedule.status(),
            version=__version__,
        )

    # ---------- 管理操作 ----------

    async def reset_schedule(self) -> bool:
        """管理员显式重置：绕过冷却，让坏掉的 provider 也重新试一次。"""
        return await self.schedule.ensure_fresh(force=True, ignore_cooldown=True)

    def reset_state(self) -> tuple[float, float, int]:
        from .state import seed_cycle_day

        today = self.clock.today_str()
        energy = self.energy.reset()
        social = self.social.reset()
        cycle_day = self.energy.reset_cycle(
            seed_cycle_day(today, self._config.cycle_length), today
        )
        return energy, social, cycle_day

    @staticmethod
    def parse_affection_batch(raw: str) -> list[tuple[str, float]]:
        """支持 `QQ:数值, QQ:数值` 与 JSON 数组两种写法。"""
        text = (raw or "").strip()
        if not text:
            return []
        with contextlib.suppress(ValueError, TypeError):
            parsed = json.loads(text)
            if isinstance(parsed, list):
                out: list[tuple[str, float]] = []
                for item in parsed:
                    if isinstance(item, dict) and "qq" in item and "value" in item:
                        with contextlib.suppress(TypeError, ValueError):
                            out.append((str(item["qq"]).strip(), float(item["value"])))
                if out:
                    return out
        pairs: list[tuple[str, float]] = []
        for part in _BATCH_SPLIT.split(text):
            if ":" not in part and "：" not in part:
                continue
            key, _, value = part.replace("：", ":").partition(":")
            with contextlib.suppress(TypeError, ValueError):
                pairs.append((key.strip(), float(value.strip())))
        return pairs

    # ---------- 日志 ----------

    def _info(self, message: str) -> None:
        self._log.info(f"{LOG_PREFIX} {message}")

    def _warn(self, message: str) -> None:
        self._log.warning(f"{LOG_PREFIX} {message}")

    def _error(self, message: str) -> None:
        self._log.error(f"{LOG_PREFIX} {message}")


def _today_guess(cfg: HumanoidConfig) -> str:
    """StateStore 需要在 Clock 建好之前就知道「今天」，这里独立算一次。"""
    from .clock import now_in_city

    return now_in_city(cfg.timezone_city).strftime("%Y-%m-%d")
