"""人形化伴侣插件 —— AstrBot 适配层 """

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, List, Optional

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.agent.message import TextPart  # 新增导入

from .humanoid import __version__
from .humanoid.config import ConfigBox, HumanoidConfig, plan_default_migrations
from .humanoid.engine import LOG_PREFIX, HumanoidEngine
from .humanoid.llm import LLMGateway, ProviderResolver
from .humanoid.persona import PersonaSource
from .humanoid.role_manager import RoleManager
from .humanoid.state import StateStore
from .humanoid.clock import Clock

DATA_SUBDIR = ("plugin_data", "humanoid_core")

HELP_TEXT = f"""📖 人形化伴侣插件 指令列表 (v{__version__})

/你的状态 - 查看精力、身体轴（困意/睡眠债/饥饿/不适…）、生理、天气、日程、过程
/查看日程 - 看她今天过出来的日程（动态，一段一段现排，不预排未来）
/时间 城市 - 查看指定城市当前时间
/叫我 昵称 - 设置 AI 对你的称呼（没设过时 AI 会自动认一次，认过之后不再改）
/好感度 - 查看情绪档案
/情绪详情 - 查看详细情绪档案
/情绪日志 - 查看情绪波动记录
/拟人帮助 - 显示本帮助

管理员指令：
/拟人设置 - 看常用项；写成「/拟人设置 城市 大阪」就改一项，立即生效
/拟人诊断 - 排查模型选择、时区是否真的生效、上下文多大
/重置日程 - 立即重新决定她当前这一段在做什么
/重置状态 - 重置精力、社交能量与生理周期
/重置情绪 - 重置自己的情绪至初始值
/设置好感度 数值 - 手动设置好感度（0-100）
/批量好感度 QQ:数值 - 批量导入好感度
/查看所有昵称 - 查看所有用户设置的昵称
/重载配置 - 重载插件配置"""

NO_PERMISSION = "❌ 权限不足，该指令仅管理员可用。"

# 多消息合并：防抖胜出的那条把同一批里更早的几条原话挂在事件上，交给 on_llm_request
# 前置进 prompt。走事件自带的 extra、不走全局字典，合并数据随事件走，跨事件不会串。
MERGE_EXTRA_KEY = "humanoid_merge_earlier"

# 默认值一次性迁移的标记文件。AstrBot 更新配置只补缺不覆盖，不调这一手的话老用户
# 会永远停在装插件那一版的行为上。
DEFAULTS_MIGRATION_MARK = ".defaults-migrated-v2.16.7"


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


def _sender_name(event: AstrMessageEvent) -> str:
    """消息自带的发送者名字：群聊是群名片（没名片时是 QQ 昵称），私聊是 QQ 昵称。

    AstrBot 的 get_sender_name() 在部分适配器下会拿不到，拿不到就当没有，不抛。"""
    getter = getattr(event, "get_sender_name", None)
    if not callable(getter):
        return ""
    try:
        return str(getter() or "")
    except Exception:
        return ""


def _append_framing(req: Any) -> None:
    """把「这些事实该怎么读」放进 system_prompt，一个请求只放一次。

    AstrBot 在 `build_main_agent` 里先把人设写进 system_prompt，再跑 OnLLMRequestEvent
    钩子，所以这里追加的内容排在人设后面；而初始 system 消息不会落进会话历史，
    这段话不会越滚越多。
    """
    from .humanoid.prompt_builder import FRAMING_TEXT, MARK_PREFIX

    current = getattr(req, "system_prompt", None)
    if not isinstance(current, str):
        current = ""
    if MARK_PREFIX in current:
        return
    req.system_prompt = current + FRAMING_TEXT


def _drop_stale_blocks(req: Any, current_text: str = "") -> int:
    """从本次递出去的上下文里抹掉以前每一份身体事实块，返回抹掉了多少份。

    事实块走的是用户消息后面，而 AstrBot 存的就是拼装后的用户消息，不清的话长对话里
    会堆几十份过期状态（她上午很困、下午很饿，模型看到的是同时成立的十几条）。
    只动 user 消息，只认自己那个标记开头的块，不碰用户真正说过的字。
    """
    from .humanoid.prompt_builder import MARK_PREFIX

    contexts = getattr(req, "contexts", None)
    if not isinstance(contexts, list):
        return 0
    removed = 0
    for item in contexts:
        if not isinstance(item, dict) or str(item.get("role", "")) != "user":
            continue
        content = item.get("content")
        if isinstance(content, str):
            index = content.find(MARK_PREFIX)
            if index >= 0:
                item["content"] = content[:index].rstrip()
                removed += 1
            continue
        if not isinstance(content, list):
            continue
        kept: List[Any] = []
        for part in content:
            text = _part_text(part)
            if text is None:
                kept.append(part)
                continue
            index = text.find(MARK_PREFIX)
            if index < 0:
                kept.append(part)
                continue
            removed += 1
            head = text[:index].rstrip()
            if head:
                _set_part_text(part, head)
                kept.append(part)
        if len(kept) != len(content):
            # 只剩一个文本块时收回成纯字符串：content 要么是 str 要么是 parts 列表，
            # 不能留一个单独的 part 对象在列表里（那不是合法的 OpenAI 消息体）。
            only = _part_text(kept[0]) if len(kept) == 1 else None
            item["content"] = only if only is not None else kept
    return removed


def _apply_merged_prompt(event: Any, req: Any) -> None:
    """把这一批里更早的几条原话前置进 req.prompt，让模型一次看到全部。

    防抖胜出的那条（最后到达）自己的话已经是 req.prompt；更早那几条通过事件 extra 传进来，
    按时间正序放到它前面。只 prepend、不覆盖，保住 AstrBot 已经做过的 prompt 前缀处理。
    """
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return
    try:
        earlier = getter(MERGE_EXTRA_KEY)
    except Exception:
        earlier = None
    if not earlier or not isinstance(earlier, list):
        return
    if not hasattr(req, "prompt"):
        return
    current = getattr(req, "prompt", "") or ""
    lines = [str(x) for x in earlier if str(x).strip()]
    if not lines:
        return
    if current:
        lines.append(current)
    req.prompt = "\n".join(lines)


# 常用项里各类型的写法：布尔能接受 开/关/是/否/true/false，枚举认面板那几个值。
_BOOL_WORDS = {
    "开": True, "关": False, "是": True, "否": False, "on": True, "off": False,
    "true": True, "false": False, "1": True, "0": False, "要": True, "不要": False,
}
_ENUM_WORDS = {
    "inject_activity_context": {"medium", "full", "mood_only"},
    "environment_mode": {"private", "group", "both"},
}


def _coerce_setting(key: str, value: str, current: Any) -> Any:
    """把命令行上的一句话翻成这一项该存的类型。认不出来时返回 None，让调用方报错。"""
    text = (value or "").strip()
    if isinstance(current, bool) or key in ("schedule_use_persona", "use_llm_schedule", "debug_mode"):
        lowered = text.lower()
        for word, flag in _BOOL_WORDS.items():
            if lowered == word.lower():
                return flag
        return None
    if key in _ENUM_WORDS:
        lowered = text.lower()
        return lowered if lowered in _ENUM_WORDS[key] else None
    if key == "admin_qq":
        items = [part.strip() for part in re.split(r"[,，\s]+", text) if part.strip()]
        return items or None
    if not text:
        return None
    return text


class _MergeSession:
    """一个会话（umo）的防抖状态：序号单调递增，缓冲区按到达顺序放原话。

    同一会话的多条消息各自是一个并发的 pipeline 任务，共享这个状态；每条先 seq += 1
    再 append（两步之间无 await，单线程下原子），然后睡等窗口；醒来后 seq 还是自己的
    就是这一批的最后一条（胜出者），否则被后来者取代。
    """

    __slots__ = ("seq", "buffer", "last_active")

    def __init__(self) -> None:
        self.seq = 0
        self.buffer: List[str] = []
        self.last_active = time.monotonic()


# 会话表上限：超过才做一次全表清扫（平时 O(1)，均摊开销可忽略）。
# 清扫只删一小时没动静的会话——刚用过的（含正在睡等窗口里的）永远不会被误删。
_MERGE_SESSION_CAP = 256
_MERGE_SESSION_IDLE_SECONDS = 3600.0


def _sweep_merge_sessions(sessions: dict, *, cap: int = _MERGE_SESSION_CAP) -> None:
    """惰性清理防抖会话表：每个见过的会话永久留一个小对象，长期运行（机器人被拉进
    越来越多群）只会慢慢积累；超上限时把一小时没动静的删掉，活跃的不动。"""
    if len(sessions) <= cap:
        return
    now = time.monotonic()
    stale = [key for key, sess in sessions.items() if now - sess.last_active > _MERGE_SESSION_IDLE_SECONDS]
    for key in stale:
        sessions.pop(key, None)


def _part_text(part: Any) -> Optional[str]:
    if isinstance(part, dict):
        if str(part.get("type", "text")) not in ("text", ""):
            return None
        value = part.get("text")
        return value if isinstance(value, str) else None
    value = getattr(part, "text", None)
    return value if isinstance(value, str) else None


def _set_part_text(part: Any, value: str) -> bool:
    if isinstance(part, dict):
        part["text"] = value
        return True
    try:
        part.text = value
        return True
    except Exception:
        return False


class HumanoidCore(Star):
    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        data_root = Path(get_astrbot_data_path())
        data_dir = data_root.joinpath(*DATA_SUBDIR)
        self._migrate_defaults(config, data_dir)
        self._config_box = ConfigBox(config)
        self._config_version = 1

        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        # 多消息合并（消息防抖）的按会话状态；进程内内存，键是 unified_msg_origin。
        self._merge_sessions: dict[str, _MergeSession] = {}

        state_path = data_dir / "state.json"
        self._state_store = StateStore(state_path, lambda: self._config.state_flush_interval_seconds, logger)

        clock = Clock(lambda: self._config)
        today = clock.today_str()
        self._state_store.load(today, self._config.cycle_length)

        self.resolver = ProviderResolver(context, logger)
        self.gateway = LLMGateway(self.resolver, lambda: self._config, logger)
        # 日程得按她真正在用的那个人格来排，而不是按插件里一个十二选一的标签。
        self.persona_source = PersonaSource(context, logger)

        self.role_manager = RoleManager(
            state_store=self._state_store,
            config_provider=lambda: self._config,
            logger=logger,
            resolver=self.resolver,
            gateway=self.gateway,
            fetch_json=self._fetch_json,
            data_root=data_root,
            persona_source=self.persona_source,
        )

        self.engine = HumanoidEngine(
            context, self._config_box, data_dir, logger, self._fetch_json, self.role_manager
        )

        logger.info(f"{LOG_PREFIX} 插件已加载 (v{__version__})")

    @property
    def _config(self) -> HumanoidConfig:
        return self._config_box.value

    @staticmethod
    def _migrate_defaults(raw_config: Any, data_dir: Path) -> None:
        """把仍等于旧默认值的配置项一次性提升本版默认值，并写回配置文件。"""
        if not isinstance(raw_config, dict):
            return
        marker = data_dir / DEFAULTS_MIGRATION_MARK
        if marker.exists():
            return
        changes = plan_default_migrations(raw_config)
        if changes:
            for key, value in changes.items():
                raw_config[key] = value
            save = getattr(raw_config, "save_config", None)
            if callable(save):
                try:
                    save()
                except Exception as exc:
                    logger.warning(f"{LOG_PREFIX} 默认值迁移写回失败，下次启动重试: {exc}")
                    return
            logger.info(
                f"{LOG_PREFIX} 已提升旧默认值："
                + "、".join(f"{key}={value!r}" for key, value in changes.items())
            )
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            marker.write_text(__version__, encoding="utf-8")
        except OSError:
            pass

    async def initialize(self) -> None:
        await self._state_store.start()
        await self.role_manager.start()
        for core in self.role_manager.get_all():
            core.schedule.current_slots()
            core.process.current()

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

    # -------------------- 用户指令 --------------------

    @filter.command("你的状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看当前角色的完整状态（精力、身体轴、生理、天气、日程、过程、社交能量等）。"""
        core = self._core(event)
        yield event.plain_result("\n".join(core.status_lines(self._sender(event))))

    @filter.command("好感度")
    async def cmd_mood(self, event: AstrMessageEvent):
        """查看当前用户的好感度、亲近欲、攻击性及情绪标签。"""
        if not self._config.mood_enabled:
            yield event.plain_result("情绪系统未开启。")
            return
        core = self._core(event)
        yield event.plain_result(core.mood.profile_text(self._sender(event)))

    @filter.command("情绪详情")
    async def cmd_mood_detail(self, event: AstrMessageEvent):
        """查看详细情绪档案，包含基线值和交互轮次。"""
        if not self._config.mood_enabled:
            yield event.plain_result("情绪系统未开启。")
            return
        core = self._core(event)
        yield event.plain_result(core.mood.profile_text(self._sender(event), detailed=True))

    @filter.command("情绪日志")
    async def cmd_mood_log(self, event: AstrMessageEvent):
        """查看最近的情绪波动记录（事件列表）。"""
        if not self._config.mood_log_enabled:
            yield event.plain_result("❌ 情绪日志未启用。")
            return
        core = self._core(event)
        yield event.plain_result(core.mood.logs_text(self._sender(event)))

    @filter.command("查看日程")
    async def cmd_view_schedule(self, event: AstrMessageEvent):
        """查看今日完整的日程表。"""
        core = self._core(event)
        yield event.plain_result(core.schedule_text())

    @filter.command("时间")
    async def cmd_time(self, event: AstrMessageEvent):
        """查看指定城市（或当前机器人生效城市）的当前时间、星期和节日。"""
        # 默认用当前 bot 的生效城市（含角色级时区覆盖），而不是全局配置：
        # 两个机器人各自「/时间」得到的是各自城市的时间。
        city = _arg_after(event.message_str, "时间") or self._core(event).clock.city
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
        """设置 AI 对你的称呼（昵称）。"""
        nickname = _arg_after(event.message_str, "叫我")
        core = self._core(event)
        user_id = self._sender(event)
        if not nickname:
            current = core.mood.nickname(user_id)
            if current:
                source = "你自己设的" if core.mood.nickname_source(user_id) == "user" else "AI 自动认的"
                yield event.plain_result(
                    f"现在叫你「{current}」（{source}）。要改就写：/叫我 新称呼"
                )
                return
            yield event.plain_result("用法：/叫我 昵称")
            return
        if len(nickname) > 32:
            yield event.plain_result("昵称太长了")
            return
        core.mood.set_nickname(user_id, nickname, src="user")
        yield event.plain_result(f"✅ 记住了，以后叫你：{nickname}")

    @filter.command("拟人帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """显示所有指令的帮助信息。"""
        yield event.plain_result(HELP_TEXT)

    # -------------------- 管理员指令 --------------------

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("拟人诊断")
    async def cmd_diagnose(self, event: AstrMessageEvent):
        """诊断模型链配置、冷却状态、日程生成情况等。"""
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        core = self._core(event)
        yield event.plain_result(self.engine.diagnostics_text(core))

    # 面板上 89 项、AstrBot 又不支持分组，改一个城市要翻半天。这一条命令只管常用那几项，
    # 进阶项仍然去面板改——两边改的是同一份配置文件。
    SETTINGS_ALIASES = {
        "城市": "timezone_city",
        "所在地": "timezone_city",
        "人设": "schedule_use_persona",
        "日程人设": "schedule_use_persona",
        "模型日程": "use_llm_schedule",
        "偏好": "schedule_prompt_extra",
        "上下文": "inject_activity_context",
        "环境": "environment_mode",
        "管理员": "admin_qq",
        "调试": "debug_mode",
    }

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("拟人设置")
    async def cmd_settings(self, event: AstrMessageEvent):
        """`/拟人设置` 看常用项；`/拟人设置 城市 大阪` 改一项，改完立即生效。"""
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        raw = event.message_str or ""
        argument = _arg_after(raw, "拟人设置")
        core = self._core(event)
        if not argument:
            yield event.plain_result(self._settings_summary(core))
            return
        parts = argument.replace("=", " ").replace("：", " ").split(None, 1)
        key_word = parts[0] if parts else ""
        value = parts[1].strip() if len(parts) > 1 else ""
        key = self.SETTINGS_ALIASES.get(key_word)
        if not key:
            yield event.plain_result(
                "不认识这一项。能改的：" + "、".join(self.SETTINGS_ALIASES)
                + "\n例：/拟人设置 城市 大阪"
            )
            return
        # 城市按机器人分离：写当前这个 bot 自己的覆盖，不动全局配置。
        # 两个机器人各发各的 /拟人设置 城市，互不影响（以前共一份全局，改一个另一个也变）。
        if key == "timezone_city":
            if not value:
                yield event.plain_result(
                    "用法：/拟人设置 城市 大阪（只改当前这个机器人）；"
                    "写「/拟人设置 城市 默认」可取消单独设置、回到跟随全局。"
                )
                return
            effective = core.set_city_override(value)
            follow = str(core.scope.get_self("tz_city_override", "") or "")
            if follow:
                yield event.plain_result(
                    f"✅ 当前机器人的城市已单独设为 {effective!r}（只影响这个 bot，其他机器人不变）。"
                )
            else:
                yield event.plain_result(
                    f"✅ 已取消当前机器人的单独城市，回到跟随全局配置（{effective!r}）。"
                )
            return
        raw_config = self._config_box.raw
        if not isinstance(raw_config, dict):
            yield event.plain_result("配置不是可写的字典，这次没改。请去面板里改。")
            return
        current = raw_config.get(key)
        new_value = _coerce_setting(key, value, current)
        if new_value is None:
            yield event.plain_result(f"「{value}」不是这一项能接受的值。当前值：{current!r}")
            return
        raw_config[key] = new_value
        saved = True
        save = getattr(raw_config, "save_config", None)
        if callable(save):
            try:
                save()
            except Exception as exc:
                saved = False
                logger.warning(f"{LOG_PREFIX} 设置写回失败: {exc}")
        # 所有服务都拿 `lambda: box()` 读配置，换掉内部实例就全链路生效，不用逐个通知角色。
        self._config_box.reload()
        yield event.plain_result(
            f"✅ {key_word} 已设为 {new_value!r}"
            + ("（未能写入配置文件，重启后会回到旧值）" if not saved else "")
        )

    def _settings_summary(self, core=None) -> str:
        cfg = self._config
        lines = [f"⚙️ 常用设置（共 89 项，其余标了【进阶】，在面板里改）"]
        # 城市是每个机器人独立的：优先显示当前 bot 生效城市，并标明是否单独设。
        if core is not None:
            override = str(core.scope.get_self("tz_city_override", "") or "")
            effective = core.clock.city or "（未定）"
            tag = "当前 bot 单独设" if override else f"跟随全局 {cfg.timezone_city or '（未定）'}"
            lines.append(f"- 城市：{effective}（{tag}）　→ /拟人设置 城市 大阪")
        else:
            lines.append(f"- 城市（全局默认）：{cfg.timezone_city or '（未定）'}　→ /拟人设置 城市 大阪")
        lines.append(f"- 日程用人设：{'开' if cfg.schedule_use_persona else '关'}　→ /拟人设置 人设 开")
        lines.append(f"- 大模型日程：{'开' if cfg.use_llm_schedule else '关'}（每 {cfg.schedule_refresh_minutes} 分钟决定一次要不要排下一段，变动概率 {cfg.schedule_change_chance}%）")
        lines.append(f"- 日程额外偏好：{cfg.schedule_prompt_extra or '（空）'}")
        lines.append(f"- 上下文详略：{cfg.inject_activity_context}（medium/full/mood_only）")
        lines.append(f"- 参与环境：{cfg.environment_mode}（private/group/both）")
        admins = "、".join(cfg.admin_qq) if cfg.admin_qq else "（未设，靠 AstrBot 全局 admins_id）"
        lines.append(f"- 管理员：{admins}")
        lines.append(f"- 调试日志：{'开' if cfg.debug_mode else '关'}")
        lines.append("改完立即生效；进阶项用 /重载配置 刷新。")
        return "\n".join(lines)

    @filter.command("重载配置")
    async def cmd_reload(self, event: AstrMessageEvent):
        """热重载插件配置，无需重启。"""
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        self._config_box.reload()
        yield event.plain_result(f"✅ 配置已重载。当前注入档位：{self._config.inject_activity_context}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重置日程")
    async def cmd_reset_schedule(self, event: AstrMessageEvent):
        """立即强制重新决定当前这一段（绕过冷却）。"""
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        if not self._config.use_llm_schedule:
            yield event.plain_result("⚠️ 未启用大模型日程。")
            return
        core = self._core(event)
        yield event.plain_result("⏳ 正在决定下一段…")
        changed = await core.schedule.ensure_fresh(force=True, ignore_cooldown=True)
        if changed:
            if core.schedule.source == "llm":
                activity = core.schedule.current_activity()
                what = activity.get("name", "")
                until = activity.get("expected_end", "")
                detail = f"：{what}（到 {until}）" if what else ""
                yield event.plain_result(f"✅ 新的一段已排好{detail}。")
            else:
                yield event.plain_result("✅ 已按身体现排一段（模型不可用，可用 /拟人诊断 查看配置）。")
        else:
            yield event.plain_result(
                f"❌ 生成失败：{core.schedule.last_error}（可用 /拟人诊断 查看模型配置）"
            )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重置状态")
    async def cmd_reset_state(self, event: AstrMessageEvent):
        """重置精力、社交能量和生理周期至初始值。"""
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        core = self._core(event)
        energy, social, cycle = core.engine_compat.reset_state()
        yield event.plain_result(f"✅ 已重置：精力 {int(energy)}，社交 {int(social)}，周期第 {cycle} 天。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重置情绪")
    async def cmd_reset_mood(self, event: AstrMessageEvent):
        """重置当前用户的情绪至初始值。"""
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        core = self._core(event)
        r = core.mood.reset(self._sender(event))
        yield event.plain_result(f"✅ 好感度 {r['affection']:.0f}，亲近欲 {r['libido']:.0f}，攻击性 {r['aggression']:.0f}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置好感度")
    async def cmd_set_affection(self, event: AstrMessageEvent):
        """手动设置当前用户的好感度（0-100）。"""
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
        """批量导入好感度（格式：QQ:数值, QQ:数值）。"""
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        pairs, malformed = self.engine.parse_affection_batch(_arg_after(event.message_str, "批量好感度"))
        if not pairs:
            yield event.plain_result("格式错误，请使用：/批量好感度 QQ:数值")
            return
        core = self._core(event)
        applied = core.mood.set_affection_batch(pairs)
        skipped = len(pairs) - applied + malformed
        if skipped:
            yield event.plain_result(
                f"✅ 已批量设置 {applied} 个用户，跳过 {skipped} 个"
                "（数值需在 0-100 之间，条目得写成 QQ:数值）。"
            )
        else:
            yield event.plain_result(f"✅ 已批量设置 {applied} 个用户。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("查看所有昵称")
    async def cmd_list_nicknames(self, event: AstrMessageEvent):
        """列出所有用户设置的昵称。"""
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        core = self._core(event)
        names = core.mood.all_nicknames()
        if not names:
            yield event.plain_result("📭 暂无昵称。")
            return
        lines = [
            f"{uid} → {name}（{'自动认定' if source == 'auto' else '用户自设'}）"
            for uid, (name, source) in names.items()
        ]
        yield event.plain_result("📋 昵称列表：\n" + "\n".join(lines))

    # -------------------- LLM 请求注入钩子 --------------------

    @filter.on_llm_request()
    async def inject_context(self, event: AstrMessageEvent, req: ProviderRequest):
        """身体与生活进上下文：事实进用户消息，「怎么读它」进 system_prompt。

        为什么分两处：事实每条消息都在变，而 AstrBot 保存历史时存的是拼装后的用户
        消息（`agent_sub_stages/internal.py` 里 `_save_to_history` 存的是 run_context.messages），
        所以往 `extra_user_content_parts` 里塞东西会永久留在会话历史里——以前没人管过，
        聊得越久历史里堆的过期身体状态越多。现在：

        1. 拼新块之前先把上下文里旧的块抹掉（改的是本次递出去的历史，跟着一起落盘），
           所以全程只有一份、永远是新鲜的；
        2. 「这些事实是什么、按什么方式读」这段不随消息变的说明只进 system_prompt——
           它是参考，不该被当成用户刚说的一句话。初始 system 消息不入历史（同一处代码
           里 `skipped_initial_system` 就是干这个的）。
        """
        try:
            is_group = not _is_private_chat(event)
            if not self.engine.environment_allows(not is_group):
                return
            # 多消息合并：若本次是防抖胜出的合并回复，把更早那几条前置进 prompt。
            _apply_merged_prompt(event, req)
            core = self.role_manager.get_or_create(self._self_id(event))
            user_id = self._sender(event)
            text = (getattr(event, "message_str", "") or "").strip()
            # 时间间隔由 Core.on_message 统一记账，这里只读。
            injection = core.build_injection(user_id, is_group=is_group, text=text)

            if self._config.debug_mode:
                from .humanoid.prompt_builder import estimate_tokens

                logger.debug(
                    f"[humanoid_core] 注入上下文 {len(injection)} 字符"
                    f" ≈{estimate_tokens(injection)} token（{self._config.inject_activity_context} 档）:\n{injection}"
                )

            _append_framing(req)
            if injection:
                stale = _drop_stale_blocks(req, text)
                if stale:
                    logger.debug(f"{LOG_PREFIX} 从上下文里抹掉 {stale} 份旧的身体事实块")
                if hasattr(req, "extra_user_content_parts"):
                    parts = getattr(req, "extra_user_content_parts", None)
                    if not isinstance(parts, list):
                        parts = []
                        req.extra_user_content_parts = parts
                    parts.append(TextPart(text=injection))
                else:  # 老版本 AstrBot 没这个字段：退回 system_prompt，至少事实是新鲜的。
                    req.system_prompt = f"{getattr(req, 'system_prompt', '') or ''}\n{injection}"

        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 注入失败: {e}")

    # -------------------- 多消息合并（消息防抖） --------------------

    @filter.on_waiting_llm_request()
    async def debounce_merge(self, event: AstrMessageEvent):
        """把同一会话短时间内连续触发机器人的多条消息，合并成一次回复。

        为什么放在这个钩子：OnWaitingLLMRequestEvent 只在确定要调 LLM 时才触发（指令
        不会走到这一步），且它在抢会话锁之前——睡等窗口不占锁、不挡别的会话。同一会话的
        每条消息各自是一个并发的 pipeline 任务（event_bus 用 create_task 派发），所以睡的
        时候后面的消息能进来刷新序号。

        胜出规则：每条消息登记一个递增序号并把原话入缓冲，睡一个防抖窗口；醒来后序号
        还是自己的→窗口内没有新消息，它就是这一批最后一条，继续走回复并把更早几条挂上
        事件交给 inject_context 并进 prompt；序号被后来者赶超→停事件，这一条不再单独回。

        改了文案不用担心旧会话：被停的消息未入历史（未走到 _save_to_history），它们的内容
        都进了胜出者的合并 prompt，所以身体/情绪记账（走 on_message）照旧逐条算，只是回复合一。
        """
        try:
            cfg = self._config
            if not cfg.message_merge_enabled:
                return
            window = float(cfg.message_merge_window_seconds)
            if window <= 0:
                return
            is_group = not _is_private_chat(event)
            if not self.engine.environment_allows(not is_group):
                return
            text = (getattr(event, "message_str", "") or "").strip()
            if not text:
                # 只对纯文本消息做合并；图片/语音这类照常单独回复，不去合并。
                return
            try:
                umo = str(getattr(event, "unified_msg_origin", "") or "")
            except Exception:
                umo = ""
            if not umo:
                return

            sess = self._merge_sessions.setdefault(umo, _MergeSession())
            sess.seq += 1
            my_seq = sess.seq
            sess.buffer.append(text)
            sess.last_active = time.monotonic()
            _sweep_merge_sessions(self._merge_sessions)
            max_count = max(1, int(cfg.message_merge_max_count))
            if len(sess.buffer) > max_count:
                del sess.buffer[:-max_count]

            await asyncio.sleep(window)

            if my_seq != sess.seq:
                # 窗口内又来了新消息 → 交给后来者合并回复，这一条不再单独回。
                event.stop_event()
                return
            # 我是这一批的最后一条：把更早几条（自己已在 prompt 里）挂到事件上，清空缓冲开新一批。
            batch = sess.buffer
            sess.buffer = []
            earlier = batch[:-1]
            if earlier:
                setter = getattr(event, "set_extra", None)
                if callable(setter):
                    try:
                        setter(MERGE_EXTRA_KEY, earlier)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 消息合并失败: {e}")

    # -------------------- 消息事件监听 --------------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """
        监听所有消息，用于更新精力、社交能量、情绪衰减、过程 tick 以及记录 last_message。
        """
        try:
            role_id = self._self_id(event)
            user_id = self._sender(event)
            if user_id == role_id:
                return
            is_group = not _is_private_chat(event)
            if not self.engine.environment_allows(not is_group):
                return
            core = self.role_manager.get_or_create(role_id)
            # 自动认定称呼：只在这个人还没有任何称呼时填一次，之后换名片也不会改口；
            # 用户 /叫我 设过的更碰不到。纯图片消息也带名字，所以放在正文判空之前。
            if self._config.auto_nickname:
                core.mood.auto_nickname(user_id, _sender_name(event))
            text = (getattr(event, "message_str", "") or "").strip()
            if not text:
                return
            try:
                umo = str(getattr(event, "unified_msg_origin", "") or "")
            except Exception:
                umo = ""
            core.on_message(user_id, text, is_group=is_group, umo=umo)
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 消息记账失败: {e}")