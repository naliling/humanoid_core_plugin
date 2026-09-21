"""单个角色的完整 Core 实例。"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from .state import seed_cycle_day
from . import __version__
from .clock import Clock, format_state_timestamp
from .config import HumanoidConfig
from .link import SocialSignals, build_contract
from .llm import LLMGateway, ProviderResolver
from .prompt_builder import PromptBuilder
from .role_scope import RoleScope
from .services.energy import EnergyService
from .services.mood import MoodService
from .services.process import ProcessService
from .services.schedule import ScheduleService
from .services.social import SocialEnergyService
from .services.weather import WeatherService
from .services.behavior import BehaviorService
from .services.soma import SomaService
from .slots import is_sleep_event

LOG_PREFIX = "[humanoid_core]"

# 过期用户数据清理的扫描间隔。只需每小时一次：retention 以天为单位，扫得更勤没有收益。
MAINTENANCE_INTERVAL_SECONDS = 3600.0


class _EngineCompat:
    def __init__(self, core: HumanoidCoreInstance):
        self._core = core

    def reset_state(self):
        """重置身体状态，并为精力与生理周期生成新的随机起点。"""
        energy = self._core.energy.reset()
        social = self._core.social.reset()

        # 不再固定回第 1 天（经期）。每次手动重置都在完整周期内重新抽取起点。
        cycle_length = max(1, int(self._core.config.cycle_length))
        import random
        cycle_day = random.randint(1, cycle_length)
        cycle_day = self._core.energy.reset_cycle(cycle_day)
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
        data_root: Any = None,
        persona_source=None,
    ):
        self.role_id = role_id
        self.version = __version__
        self._state_store = state_store
        self._config_provider = config_provider
        self._log = logger
        self._stop_event = stop_event
        self._fetch_json = fetch_json
        self.persona_source = persona_source
        self.last_activity: Optional[float] = None

        self.resolver = resolver
        self.gateway = gateway

        self._scope = RoleScope(state_store.data, role_id)
        self._scope.set_mark_dirty(state_store.mark_dirty)

        # 时区按角色分离：这个 bot 单独设过城市就用它自己的，没设过才用全局配置。
        # 两个机器人各改各的，互不影响。
        self.clock = Clock(lambda: self.config, self._city_override)


        # 新角色的生理周期不能永远从第 1 天（经期）开始：按创建当天错开一个起点，
        # 否则多角色下每一个都是同一个人同一天，且 v2.13.2 从未推进过周期。
        if self._scope.get_self("current_cycle_day") is None:
            cfg = self.config
            self._scope.update_self(
                current_cycle_day=seed_cycle_day(self.clock.today_str(), max(1, int(cfg.cycle_length))),
                last_cycle_update=self.clock.today_str(),
            )

        self.schedule = ScheduleService(
            self._scope,
            config_provider,
            self.clock,
            self._spawn_background,
            logger,
            persona_provider=self._persona_for_schedule,
            body_provider=self._schedule_body,
        )
        self.schedule.set_resolver_gateway(resolver, gateway)
        # 重启后、第一条消息到来之前也要能按人设生成日程：把上次记账的代表会话先恢复。
        if persona_source is not None:
            persona_source.restore(role_id, self._scope.get_self("last_umo", ""))

        # 身体（生理层）的日程与天气都是惰性取数，避开构造顺序上的环形依赖。
        self.soma = SomaService(
            self._scope,
            config_provider,
            self.clock,
            schedule_provider=lambda: self.schedule.current_slots(),
            weather_provider=lambda: self.weather.snapshot(),
        )
        self.schedule.on_install = self._on_schedule_installed

        # 精力由日程驱动，所以 schedule_provider 必须递进去；v2.13.2 漏了这个参数，
        # 导致 _compute_delta 直接返回 0，精力从早到晚纹丝不动。
        self.energy = EnergyService(
            self._scope,
            config_provider,
            self.clock,
            lambda: self.schedule.current_slots(),
            logger,
            body_provider=lambda: self.soma if self.config.soma_enabled else None,
        )

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

        self.behavior = BehaviorService(self)

        # 社交层写回的信号（它替她主动开过口、被谁冷落了几次）。只读，文件不在就是没联动。
        self.signals = SocialSignals(lambda: data_root)

        self.prompt_builder = PromptBuilder(self)
        self.engine_compat = _EngineCompat(self)

        self._rebase_after_move()

        self._tasks: set[asyncio.Task] = set()
        self._started = False

    def _city_override(self) -> str:
        """这个机器人单独设的城市（空 = 没单独设，跟全局配置）。Clock 靠它实现时区分离。"""
        return str(self._scope.get_self("tz_city_override", "") or "")

    def _rebase_after_move(self) -> None:
        """换了城市 = 换了时区：把那些按旧城市钟点记的量重新起算。

        `last_update` 与 `_last_weather_fetch` 存的是 `%Y-%m-%d %H:%M:%S` 这样的墙上时间，
        读回来时附的是**当前**时区（`parse_state_timestamp`）。于是从北京改到东京会把
        15:20 读成东京的 15:20，凭空多出/少掉几个小时：精力会按不存在的区间重算一遍，
        天气可能提前或延后一小时重取。量不大，但错得看不见，所以当场抹平。

        这里取的是**生效城市**（`clock.city`）：角色单独设了就是它自己的，没设才是全局。
        因此只改某一个机器人的城市时，只有它会 rebase，另一个不受影响。
        """
        current = self.clock.city
        stored = str(self._scope.get_self("tz_city", "") or "")
        if stored == current:
            return
        if stored:
            now = self.clock.now()
            self._scope.update_self(
                last_update=format_state_timestamp(now),
                _last_weather_fetch="",
            )
            if self._log:
                self._log.info(
                    f"{LOG_PREFIX} 角色 {self.role_id} 所在城市由「{stored}」改为「{current}」："
                    "精力与天气的计时已按新时区重新起算"
                )
        self._scope.set_self("tz_city", current)

    def set_city_override(self, city: str) -> str:
        """单独设这个机器人的城市（`/拟人设置 城市` 走这里）。

        传空串或「默认/清除」意思的词 = 取消单独设置，回到跟随全局配置。
        写完立即 rebase，让精力与天气计时按新时区起算。返回生效城市（展示用）。
        """
        value = str(city or "").strip()
        if value in ("", "默认", "清除", "跟随全局", "全局"):
            self._scope.set_self("tz_city_override", "")
        else:
            self._scope.set_self("tz_city_override", value)
        self._rebase_after_move()
        return self.clock.city

    @property
    def config(self) -> HumanoidConfig:
        return self._config_provider()

    @property
    def scope(self) -> RoleScope:
        """服务层读写自己那份数据的唯一入口（不递 `self._scope` 那个私名）。"""
        return self._scope

    def _on_schedule_installed(self, previous, current) -> None:
        """新的一段装上后的一次性回调。

        身体只在「睡着→醒着」真的翻转时重置睡眠计时：接着睡的一段不该把这一觉
        劈成两半（昨晚睡了多久会少算）；醒着→睡着由积分自己记起点。过程跟着
        新的一段换锚点：同一件事续了就接着排阶段，换了事就现开一个新过程。
        """
        try:
            was_sleep = is_sleep_event(str((previous or {}).get("event") or ""))
            now_sleep = is_sleep_event(str((current or {}).get("event") or ""))
            if was_sleep and not now_sleep:
                self.soma.note_schedule_changed()
        except Exception:
            pass
        try:
            self.process.note_segment_changed(current or {})
        except Exception:
            pass

    async def _persona_for_schedule(self):
        """日程生成用的当前生效人设。没接上人格源时返回 None，prompt 走中性身份。"""
        if self.persona_source is None:
            return None
        return await self.persona_source.persona(self.role_id)

    def _schedule_body(self) -> dict:
        """日程重排用的身体参考数值：每次重排现取的活值，不是静态设定。

        只给数值与周期描述，不带任何「所以该安排什么」的结论——怎么权衡是模型的事。
        """
        body: dict[str, Any] = {"now": self.clock.now().strftime("%H:%M")}
        try:
            body["energy"] = round(float(self.energy.energy), 1)
        except Exception:
            pass
        if self.config.soma_enabled:
            try:
                snap = self.soma.snapshot()
                body.update(
                    {
                        "hunger": snap["hunger"],
                        "sleep_pressure": snap["sleep_pressure"],
                        "sleep_debt": snap["sleep_debt"],
                        "last_sleep_hours": snap["last_sleep_hours"],
                        "discomfort": snap["discomfort"],
                        "asleep": snap["asleep"],
                    }
                )
            except Exception:
                pass
        try:
            cycle = self.energy.cycle_description()
        except Exception:
            cycle = ""
        if cycle:
            body["cycle"] = cycle
        return body

    def note_umo(self, umo: str) -> bool:
        """记下该角色最近互动的会话来源，日程靠它去解析「她是谁」。

        只在真的变了时写盘：这个字段每条消息都会被看到一次，无脑 set_self 会把
        落盘脏标记变成常驻。
        """
        text = str(umo or "").strip()
        if not text or str(self._scope.get_self("last_umo", "") or "") == text:
            return False
        self._scope.set_self("last_umo", text)
        if self.persona_source is not None:
            self.persona_source.note_umo(self.role_id, text)
            self._spawn_background(self._realign_persona(), "persona-realign")
        return True

    async def _realign_persona(self):
        """今天这份日程是在还没见到人设时排的，现在认出了她是谁 → 重排一次。

        不加这一手，插件刚装好（或重启后）那天的日程会永远停在「没用人设」的版本上，
        要等到半夜跨天才能对上。每角色每人设每天最多重排一次，不会因对方换会话而反复跑。
        """
        cfg = self.config
        if not cfg.use_llm_schedule or self.persona_source is None:
            return
        try:
            persona = await self.persona_source.persona(self.role_id)
        except Exception:
            return
        if persona is None or not persona.usable:
            return
        today = self.clock.today_str()
        stamp = f"{today}:{persona.label}"
        if str(self._scope.get_self("persona_realigned", "") or "") == stamp:
            return
        if str(self._scope.get_self("schedule_persona", "") or "") == persona.label:
            return
        self._scope.set_self("persona_realigned", stamp)
        if self.schedule.source != "llm":
            # 日程还是内置模板，本来就该让后台循环去生成今天的版本，不另开一次。
            return
        self._log.info(f"{LOG_PREFIX} 角色 {self.role_id} 认出了人设「{persona.label}」，重排今日日程")
        self.schedule.request_refresh(force=True, ignore_cooldown=True)

    def persona_label(self) -> str:
        """今天这份日程是按哪个人设排的（展示用，不触发解析）。"""
        stored = str(self._scope.get_self("schedule_persona", "") or "")
        if stored:
            return stored
        return self.schedule.last_persona or ""

    def _spawn_background(self, coro, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=f"humanoid-{self.role_id}-{name}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def on_message(self, user_id: str, text: str, is_group: bool = False, umo: str = "") -> None:
        self.last_activity = time.time()
        now = time.time()
        cfg = self.config

        self.note_umo(umo)

        # 先把身体推进到「现在」，再记账：否则本轮用的是上一轮的困意与饥饿。
        if cfg.soma_enabled:
            self.soma.advance(now)
        self.energy.advance_cycle()

        # 时间间隔属于“事件”，必须先读取旧状态，再写入当前时间。
        # 新用户回合开始，上一回合的“回来事件”到此结束，避免后续消息反复追问同一件事。
        self.behavior.clear_user_events(user_id)

        last_ts = self._scope.get_user(user_id, "last_interaction")
        previous_message = self._scope.get_user(user_id, "last_message")
        event = self.behavior.process_interval(
            user_id=user_id,
            now=now,
            last_ts=last_ts,
            last_message=previous_message,
        )
        if event is not None:
            self.behavior.add_event(user_id, event)

        # 当前消息成为下一次“回来时”可参考的上一条消息。
        self._scope.set_user(user_id, "last_interaction", now)
        if text:
            self._scope.set_user(user_id, "last_message", {
                "text": text[:120],
                "timestamp": now,
            })
            # 记住 TA 说过什么：注入里一句「TA之前说过『我猫今天吐了』」就能让她像个
            # 一直在听的人，而不是每条都重新认识你。本地采样，零额外调用。
            if cfg.mood_enabled and (not is_group or cfg.mood_enabled_in_group):
                self.mood.note_said(user_id, text, now)

        self.energy.advance()
        self.energy.consume_for_message()

        # 聊天频率感靠这个计数：TA今天话不少/很少，中间档不给。
        today = self.clock.today_str()
        if str(self._scope.get_self("daily_msg_date", "") or "") != today:
            self._scope.update_self(daily_msg_date=today, daily_msg_count=1)
        else:
            try:
                count = int(self._scope.get_self("daily_msg_count", 0) or 0)
            except (TypeError, ValueError):
                count = 0
            self._scope.set_self("daily_msg_count", count + 1)

        if cfg.soma_enabled:
            self.soma.note_message()

        if cfg.social_energy_enabled:
            self.social.consume_for_message()

        self.process.tick()
        self.mood.decay_user(user_id)
        if cfg.soma_enabled and not is_group:
            # 私聊算「有人陪」，把想说话的程度泄掉；群聊里潜水不等于被陪。
            self.soma.note_chat(now)
        self._dispatch_async(user_id, text, is_group=is_group)

    def _dispatch_async(self, user_id: str, text: str, is_group: bool = False):
        cfg = self.config
        if cfg.process_auto_update and self.process.needs_update():
            self._spawn_background(self.process.update_async(), "process-update")
        mood_allowed = cfg.mood_enabled and (not is_group or cfg.mood_enabled_in_group)
        if mood_allowed:
            self._spawn_background(
                self.mood.update_from_message_async(user_id, text),
                "mood-update"
            )
        if self.weather.is_stale():
            self._spawn_background(self.weather.refresh_async(), "weather-refresh")

    def build_injection(self, user_id: str, is_group: bool = False, text: str = "") -> str:
        """拼本次请求要追加的事实块。

        只读不写：时间间隔、情绪、注意力都在 `on_message` 里记过账，这里再记一次就会
        把「上一次说话」推到当前这一条上，间隔永远算不出来。
        """
        now = time.time()
        if self.config.soma_enabled:
            self.soma.advance(now)
        events = self.behavior.consume_relevant_events(user_id, now)
        agency = {}
        if events:
            agency = self.behavior.compute_agency(
                user_id=user_id,
                events=events,
                social_energy=self.social.value,
                mood_profile=self.mood.profile(user_id),
                energy=self.energy.energy,
            )
        # 注意力三轴：上心程度要看 TA 这句活本身，所以得把原文递进去。
        try:
            interest = self.behavior.interest_state(user_id, now, text=text, is_group=is_group)
        except Exception:
            interest = {}
        return self.prompt_builder.build(
            user_id, is_group, events=events, agency=agency, text=text, interest=interest
        )

    def refresh_contract(self) -> dict | None:
        """重算并落盘导出给社交层的身体快照。

        只在真正有得看的变化时写：`generated_at` 每分钟都在动，拿它参与比较的话这个
        方法本身就变成常驻写盘源。时间字段会跟着一起刷新，但不单独触发写入。

        身体关着时不导出：契约里的轴全是初始值，社交层读到 `social_desire=0` 会把她当成
        「刚聊完不想说话」而长期压住主动消息。不导出，社交层就退回旧字段，行为与 v1.7.4 一致。
        """
        if not self.config.contract_enabled or not self.config.soma_enabled:
            # 契约关闭时清空已有契约，避免社交层读到过期数据
            previous = self._scope.get_self("contract")
            if previous is not None:
                try:
                    self._scope.set_self("contract", None)
                except Exception:
                    pass
            return None
        try:
            contract = build_contract(self)
        except Exception as exc:
            if self._log:
                self._log.warning(f"{LOG_PREFIX} 契约生成失败: {exc}")
            return None
        if not isinstance(contract, dict) or not contract:
            if self._log:
                self._log.warning(f"{LOG_PREFIX} 契约生成返回空字典")
            return None
        previous = self._scope.get_self("contract")
        keys = ("body", "feelings", "form", "activity")
        try:
            if isinstance(previous, dict) and all(previous.get(key) == contract.get(key) for key in keys):
                return previous
        except Exception as exc:
            if self._log:
                self._log.debug(f"{LOG_PREFIX} 契约比较失败: {exc}")
        try:
            self._scope.set_self("contract", contract)
        except Exception as exc:
            if self._log:
                self._log.warning(f"{LOG_PREFIX} 契约写入失败: {exc}")
            return None
        return contract

    def _apply_social_signals(self) -> None:
        """把社交层写回的信号并入身体。没装社交层时什么都没发生。"""
        if not self.config.soma_enabled:
            return
        try:
            _, at = self.signals.last_proactive()
            streak = self.signals.ignored_streak()
        except Exception as exc:
            if self._log:
                self._log.debug(f"{LOG_PREFIX} 读取社交信号失败: {exc}")
            return
        try:
            self.soma.set_social_feedback(streak)
        except Exception as exc:
            if self._log:
                self._log.debug(f"{LOG_PREFIX} 应用冷落计数失败: {exc}")
        if at <= 0:
            return
        try:
            last_at = float(self._scope.get_self("_last_signal_proactive", 0.0) or 0.0)
            if at <= last_at:
                return
            self._scope.set_self("_last_signal_proactive", at)
            self.soma.note_proactive(at)
        except Exception as exc:
            if self._log:
                self._log.debug(f"{LOG_PREFIX} 应用主动消息反馈失败: {exc}")

    def snapshot(self, user_id: Optional[str] = None, refresh: bool = True) -> dict:
        if refresh:
            self.energy.advance()
            self.process.tick()
            if self.process.needs_update():
                self.process.update_sync()
        if self.config.soma_enabled:
            self.soma.advance()

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
                "source": self.schedule.status()["source_text"],
            },
            "weather": self.weather.snapshot(),
            "social_energy": {
                "value": self.social.value,
                "text": self.social.text,
            },
        }
        if self.config.soma_enabled:
            result["soma"] = self.soma.snapshot()
            result["form_policy"] = self.soma.form_policy(
                float(self.energy.energy), float(self.social.value)
            )
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

    def body_lines(self) -> list[str]:
        """`/你的状态` 用的身体读数。这里给数字没问题，看的人是自己人。"""
        if not self.config.soma_enabled:
            return ["- 生理层：未开启（只有精力一个标量）"]
        snap = self.soma.snapshot()
        lines = [
            f"- 睡眠压力：{snap['sleep_pressure']:.0f}/100",
            f"- 睡眠债：{snap['sleep_debt']:.1f} 小时（次日精力起点 {self.soma.sleep_debt_penalty() * 100:.0f}%）",
            f"- 饥饿：{snap['hunger']:.0f}/100",
            f"- 躯体不适：{snap['discomfort']:.0f}/100",
            f"- 唤醒度：{snap['arousal']:.0f}/100",
            f"- 想找人说说话：{snap['social_desire']:.0f}/100"
            + ("（正在睡）" if snap["asleep"] >= 1 else ""),
        ]
        contract = self._scope.get_self("contract")
        if isinstance(contract, dict) and contract:
            lines.append(
                f"- 联动契约：v{contract.get('v', 1)}，已导出 {len(contract.get('feelings') or [])} 条体感"
            )
        else:
            lines.append("- 联动契约：未生成（身体还没推进过，或 contract_enabled 关着）")
        try:
            state = self.signals.status()
        except Exception:
            state = {}
        # 对接成不成在这就能看出来：没装、没写过、过期、在用，四种说法不一样。
        lines.append({
            "fresh": "- 自主拟人社交：已对接（信号在读）",
            "stale": "- 自主拟人社交：信号超时未刷新，暂不采信",
            "never_written": "- 自主拟人社交：装了但还没写过信号（等它跑一轮）",
            "not_installed": "- 自主拟人社交：未安装（本插件单独也能跑）",
        }.get(str(state.get("state")), "- 自主拟人社交：状态未知"))
        return lines

    def status_lines(self, user_id: str) -> list[str]:
        s = self.snapshot(user_id)
        lines = [
            f"🧠 角色：{self.role_id}",
            f"- 时间：{s['time']} 星期{s['weekday']}",
            f"- 城市：{s['city']}",
            f"- 精力：{int(s['energy']['value'])}/{int(s['energy']['max'])} ({s['energy']['text']})",
            f"- 生理：{s['cycle'] or '未开启'}",
        ]
        lines += self.body_lines()
        if s['process']:
            p = s['process']
            lines.append(f"- 当前过程：{p.get('name', '休息')}（持续 {p.get('duration_minutes', 0)} 分钟）")
        if s['weather']:
            lines.append(f"- 天气：{s['weather'].get('weather', '未知')}")
        if s['social_energy']:
            lines.append(f"- 社交能量：{int(s['social_energy']['value'])}% ({s['social_energy']['text']})")
        if user_id and 'mood' in s:
            lines.append(f"- 好感度：{s['mood']['affection']:.1f}（{s['mood']['label']}）")
            # 注意力三轴只在这个地方给人看数值：进上下文的是措辞，不是百分比。
            try:
                axes = self.behavior.interest_state(user_id, time.time())
            except Exception:
                axes = {}
            if axes:
                lines.append(
                    "- 注意力：在意 {:.0%}、上心 {:.0%}、余量 {:.0%}".format(
                        axes.get("care", 0.0), axes.get("focus", 0.0), axes.get("spare", 0.0)
                    )
                )
                lines.append("  （上心程度拿你刚这句话现算，所以每条消息都在动）")
        return lines

    def schedule_text(self) -> str:
        """她今天过出来的日程：已过完的段 + 当前段，没有预排的未来。"""
        now = self.clock.now()
        segs = self.schedule.segments()
        who = self.persona_label()
        head = f"📅 {now.strftime('%Y-%m-%d')} 她今天过出来的日程（{self.schedule.source_text}"
        head += f"，人设：{who}）：" if who else "）："
        lines = [head]
        if not segs:
            lines.append("（今天还没有排出来的时段）")
        active = self.schedule.active_segment()
        for slot in segs:
            mark = "▶" if active is not None and slot.get("start") == active.get("start") else " "
            lines.append(
                f"{mark} {slot.get('start', '')}-{slot.get('end', '')}  "
                f"【{slot.get('event', '')}】@{slot.get('location', '')}"
            )
        carry_end = self.schedule.carry_end_text()
        if carry_end:
            lines.append(f"（当前这段觉会睡到明天 {carry_end}）")
        lines.append("（动态日程：只排到当前这段，之后的事到了再决定）")
        if self.schedule.generating:
            lines.append("（正在决定下一段…）")
        return "\n".join(lines)

    def process_text(self) -> str:
        """过程的详细状况：当前时段 + 过程 + 行为阶段。"""
        p = self.process.current()
        slot = self.schedule.current_slot() or {}
        lines = [
            f"📋 当前时段：{slot.get('start', '')}-{slot.get('end', '')} "
            f"【{slot.get('event', '')}】@{slot.get('location', '')}",
            f"当前过程：{p.get('name', '休息')}",
            f"行为阶段：{p.get('phase') or '自然进行中'}",
        ]
        style = p.get("style", "")
        if style:
            lines.append(f"过程风格：{style}")
        lines.extend([
            f"开始：{p.get('started_at', '未知')}",
            f"预计结束：{p.get('expected_end', '未知')}",
            f"持续时长：{p.get('duration_minutes', 0)} 分钟",
        ])
        return "\n".join(lines)


    async def start(self):
        if self._started:
            return
        self._started = True
        self._spawn_background(self._social_loop(), "social-recovery")
        self._spawn_background(self._weather_loop(), "weather-refresh")
        self._spawn_background(self._schedule_loop(), "schedule-refresh")
        self._spawn_background(self._process_loop(), "process-refresh")
        self._spawn_background(self._body_loop(), "body-tick")
        self._spawn_background(self._maintenance_loop(), "data-maintenance")
        self.schedule.current_slots()
        self.schedule.seed_first_segment()
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

    async def _maintenance_loop(self):
        """定期把长期不活跃用户的历史情绪数据从 state.json 里掉掉。

        没说话的用户会留下 mood_logs（每人最多 28 条）等数据，而用户条目只增不减，
        整个文件又是全量重写，不清理会让落盘开销随用户数线性增长。
        """
        while not self._stop_event.is_set():
            try:
                pruned = self.mood.prune_expired()
                if pruned:
                    self._log.info(
                        f"{LOG_PREFIX} 角色 {self.role_id} 清理了 {pruned} 个过期用户的情绪数据"
                    )
            except Exception as exc:
                self._log.warning(f"{LOG_PREFIX} 过期数据清理失败: {exc}")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=MAINTENANCE_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                pass

    async def _body_loop(self):
        """身体的时间必须自己流动。

        v2.13.2 里精力、饥饿、久坐全部挂在 on_message 上：没人说话的那十几个小时里
        她不会困也不会饿。这个循环不依赖消息，同时把生理周期按天推下去。
        """
        while not self._stop_event.is_set():
            cfg = self.config
            try:
                if cfg.soma_enabled:
                    self.soma.advance()
                    self._apply_social_signals()
                self.energy.advance()
                self.energy.advance_cycle()
                self.refresh_contract()
            except Exception as exc:
                self._log.warning(f"{LOG_PREFIX} 身体推进失败: {exc}")
            interval = max(15.0, float(cfg.body_tick_seconds))
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

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

    async def _process_loop(self):
        """后台推进过程内部阶段，避免一个过程几十分钟毫无变化。"""
        while not self._stop_event.is_set():
            cfg = self.config
            if cfg.process_auto_update:
                try:
                    self.process.tick()
                    if self.process.needs_update():
                        await self.process.update_async()
                except Exception as exc:
                    self._log.warning(f"{LOG_PREFIX} 过程阶段更新失败: {exc}")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    async def _schedule_loop(self) -> None:
        """动态日程：每 30 秒问一句「该不该决定下一段」。

        该不该由日程服务自己判断：这一段过完了、身体跟手上的事打架、或掷中了
        变动概率才生成，其余时候她接着做手上的事，一次模型都不调。跨天与跨夜
        结转也在服务里处理——睡着的那一段会原样接到明天，零点不需要再问模型。
        """
        while not self._stop_event.is_set():
            try:
                self.schedule.request_refresh()
            except Exception as exc:
                self._log.warning(f"{LOG_PREFIX} 日程后台检查失败: {exc}")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass
