"""人形化伴侣插件 —— AstrBot 适配层 """

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .humanoid import __version__
from .humanoid.config import HumanoidConfig
from .humanoid.engine import LOG_PREFIX, HumanoidEngine
from .humanoid.llm import LLMGateway, ProviderResolver
from .humanoid.migration import migrate_file
from .humanoid.role_manager import RoleManager
from .humanoid.state import StateStore

DATA_SUBDIR = ("plugin_data", "humanoid_core")

HELP_TEXT = f"""📖 人形化伴侣插件 指令列表 (v{__version__})

/你的状态 - 查看精力、生理、天气、日程、过程、社交能量
/查看日程 - 查看今日完整日程
/时间 城市 - 查看指定城市当前时间
/叫我 昵称 - 设置 AI 对你的称呼
/好感度 - 查看情绪档案
/情绪详情 - 查看详细情绪档案
/情绪日志 - 查看情绪波动记录
/拟人帮助 - 显示本帮助

管理员指令：
/拟人诊断 - 排查模型选择问题
/重置日程 - 立即重新生成今日日程
/重置状态 - 重置精力、社交能量与生理周期
/重置情绪 - 重置自己的情绪至初始值
/设置好感度 数值 - 手动设置好感度（0-100）
/批量好感度 QQ:数值 - 批量导入好感度
/查看所有昵称 - 查看所有用户设置的昵称
/重载配置 - 重载插件配置"""

NO_PERMISSION = "❌ 权限不足，该指令仅管理员可用。"


def _arg_after(text: str, command: str) -> str:
    raw = (text or "").strip()
    index = raw.find(command)
    if index == -1:
        return raw.lstrip("/").strip()
    return raw[index + len(command) :].strip()


def _is_private_chat(event: AstrMessageEvent) -> bool:
    checker = getattr(event, "is_private_chat", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            pass
    getter = getattr(event, "get_group_id", None)
    if callable(getter):
        try:
            return not bool(getter())
        except Exception:
            pass
    message_obj = getattr(event, "message_obj", None)
    return not bool(getattr(message_obj, "group_id", None))


class HumanoidCore(Star):
    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self._raw_config = config
        self._config = HumanoidConfig.from_raw(config)
        self._config_version = 1

        data_dir = Path(get_astrbot_data_path()).joinpath(*DATA_SUBDIR)
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()

        state_path = data_dir / "state.json"
        self._state_store = StateStore(state_path, lambda: self._config.state_flush_interval_seconds, logger)
        migrate_file(state_path)

        self.resolver = ProviderResolver(context, logger)
        self.gateway = LLMGateway(self.resolver, lambda: self._config, logger)

        self.role_manager = RoleManager(
            state_store=self._state_store,
            config_provider=lambda: self._config,
            logger=logger,
            fetch_json=self._fetch_json,
            resolver=self.resolver,
            gateway=self.gateway,
        )

        self.engine = HumanoidEngine(
            context, config, data_dir, logger, self._fetch_json, self.role_manager
        )

        logger.info(f"{LOG_PREFIX} 插件已加载 (v{__version__})")

    async def initialize(self) -> None:
        await self._state_store.start()
        await self.role_manager.start()

    async def terminate(self) -> None:
        await self.role_manager.stop()
        await self._state_store.stop()
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

    async def cleanup(self) -> None:
        await self.terminate()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession()
        return self._session

    async def _fetch_json(self, url: str, timeout: float) -> dict[str, Any]:
        session = await self._ensure_session()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
            response.raise_for_status()
            return await response.json()

    def _sender(self, event: AstrMessageEvent) -> str:
        return str(event.get_sender_id())

    def _self_id(self, event: AstrMessageEvent) -> str:
        return str(event.get_self_id())

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        astrbot_admin = False
        checker = getattr(event, "is_admin", None)
        if callable(checker):
            try:
                astrbot_admin = bool(checker())
            except Exception:
                pass
        return self.engine.is_admin(self._sender(event), astrbot_admin)

    def _core(self, event: AstrMessageEvent):
        return self.role_manager.get_or_create(self._self_id(event))

    @filter.command("你的状态")
    async def cmd_status(self, event: AstrMessageEvent):
        core = self._core(event)
        yield event.plain_result("\n".join(core.status_lines(self._sender(event))))

    @filter.command("好感度")
    async def cmd_mood(self, event: AstrMessageEvent):
        if not self._config.mood_enabled:
            yield event.plain_result("情绪系统未开启。")
            return
        core = self._core(event)
        yield event.plain_result(core.mood.profile_text(self._sender(event)))

    @filter.command("情绪详情")
    async def cmd_mood_detail(self, event: AstrMessageEvent):
        if not self._config.mood_enabled:
            yield event.plain_result("情绪系统未开启。")
            return
        core = self._core(event)
        yield event.plain_result(core.mood.profile_text(self._sender(event), detailed=True))

    @filter.command("情绪日志")
    async def cmd_mood_log(self, event: AstrMessageEvent):
        if not self._config.mood_log_enabled:
            yield event.plain_result("❌ 情绪日志未启用。")
            return
        core = self._core(event)
        yield event.plain_result(core.mood.logs_text(self._sender(event)))

    @filter.command("查看日程")
    async def cmd_view_schedule(self, event: AstrMessageEvent):
        core = self._core(event)
        yield event.plain_result(core.schedule_text())

    @filter.command("时间")
    async def cmd_time(self, event: AstrMessageEvent):
        city = _arg_after(event.message_str, "时间") or self._config.timezone_city
        if not city:
            yield event.plain_result("请指定城市名，或在配置中设置默认时区城市。")
            return
        text = self.engine.city_time_text(city)
        if text is None:
            yield event.plain_result(f"暂不支持 {city}")
        else:
            yield event.plain_result(text)

    @filter.command("叫我")
    async def cmd_set_nickname(self, event: AstrMessageEvent):
        nickname = _arg_after(event.message_str, "叫我")
        if not nickname:
            yield event.plain_result("用法：/叫我 昵称")
            return
        if len(nickname) > 32:
            yield event.plain_result("昵称太长了")
            return
        core = self._core(event)
        core.mood.set_nickname(self._sender(event), nickname)
        yield event.plain_result(f"✅ 记住了，以后叫你：{nickname}")

    @filter.command("拟人帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(HELP_TEXT)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("拟人诊断")
    async def cmd_diagnose(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        core = self._core(event)
        yield event.plain_result(self.engine.diagnostics_text(core))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重载配置")
    async def cmd_reload(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        self._config = HumanoidConfig.from_raw(self._raw_config)
        yield event.plain_result(f"✅ 配置已重载。当前注入档位：{self._config.inject_activity_context}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重置日程")
    async def cmd_reset_schedule(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        if not self._config.use_llm_schedule:
            yield event.plain_result("⚠️ 未启用大模型日程。")
            return
        core = self._core(event)
        yield event.plain_result("⏳ 正在后台重新生成…")
        changed = await core.schedule.ensure_fresh(force=True, ignore_cooldown=True)
        yield event.plain_result("✅ 日程已更新。" if changed else "❌ 生成失败，仍使用模板。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重置状态")
    async def cmd_reset_state(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        core = self._core(event)
        energy, social, cycle = core.engine_compat.reset_state()
        yield event.plain_result(f"✅ 已重置：精力 {int(energy)}，社交 {int(social)}，周期第 {cycle} 天。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重置情绪")
    async def cmd_reset_mood(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        core = self._core(event)
        r = core.mood.reset(self._sender(event))
        yield event.plain_result(f"✅ 好感度 {r['affection']:.0f}，亲近欲 {r['libido']:.0f}，攻击性 {r['aggression']:.0f}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置好感度")
    async def cmd_set_affection(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        match = re.search(r"(\d+(?:\.\d+)?)", _arg_after(event.message_str, "设置好感度"))
        if not match:
            yield event.plain_result("用法：/设置好感度 数值 (0-100)")
            return
        v = float(match.group(1))
        if not 0 <= v <= 100:
            yield event.plain_result("数值必须在 0-100 之间。")
            return
        core = self._core(event)
        core.mood.set_affection(self._sender(event), v)
        yield event.plain_result(f"✅ 好感度已设为 {v:.0f}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("批量好感度")
    async def cmd_batch_affection(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        pairs = self.engine.parse_affection_batch(_arg_after(event.message_str, "批量好感度"))
        if not pairs:
            yield event.plain_result("格式错误，请使用：/批量好感度 QQ:数值")
            return
        core = self._core(event)
        applied = core.mood.set_affection_batch(pairs)
        yield event.plain_result(f"✅ 已批量设置 {applied} 个用户。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("查看所有昵称")
    async def cmd_list_nicknames(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        core = self._core(event)
        names = core.mood.all_nicknames()
        if not names:
            yield event.plain_result("📭 暂无昵称。")
            return
        yield event.plain_result("📋 昵称列表：\n" + "\n".join(f"{k} → {v}" for k, v in names.items()))

    @filter.on_llm_request()
    async def inject_context(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            is_group = not _is_private_chat(event)
            if not self.engine.environment_allows(not is_group):
                return
            core = self.role_manager.get_or_create(self._self_id(event))
            injection = core.build_injection(
                self._sender(event),
                is_group=is_group
            )
            if req.system_prompt:
                req.system_prompt = f"{req.system_prompt}\n{injection}"
            else:
                req.system_prompt = injection
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 注入失败: {e}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        try:
            text = (getattr(event, "message_str", "") or "").strip()
            if not text:
                return
            role_id = self._self_id(event)
            user_id = self._sender(event)
            if user_id == role_id:
                return
            is_group = not _is_private_chat(event)
            if not self.engine.environment_allows(not is_group):
                return
            core = self.role_manager.get_or_create(role_id)
            core.on_message(user_id, text, is_group=is_group)
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 消息记账失败: {e}")