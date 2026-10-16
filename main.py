"""人形化伴侣插件 —— AstrBot 适配层 """

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.agent.message import TextPart

from .humanoid import __version__
from .humanoid.config import (
    INT_SETTING_RANGES,
    ConfigBox,
    HumanoidConfig,
    plan_default_migrations,
)
from .humanoid.diagnostics import config_error_banner
from .humanoid.engine import HumanoidEngine
from .humanoid.llm import CallGate, LLMGateway, ProviderResolver
from .humanoid.persona import PersonaSource
from .humanoid.role_manager import RoleManager
from .humanoid.state import StateStore
from .humanoid.clock import Clock
from .humanoid.debounce import MERGE_EXTRA_KEY, Debouncer
from .humanoid.input_state import InputStateNotifier
from .humanoid import memory_link

DATA_SUBDIR = ("plugin_data", "humanoid_core")

HELP_TEXT = f"""📖 人形化伴侣插件 指令列表 (v{__version__})

查状态的指令**直接打，不带前缀**：

/你的状态 - 精力、身体轴（困意/睡眠债/饥饿/不适）、生理、天气、日程、关系、注意力
/查看日程 - 她今天过出来的日程（动态现排，不预排未来）
/时间 城市 - 查看指定城市当前时间
/好感度 - 情绪档案总览（管理员加「排行榜」看所有人的排名）
/情绪详情 - 含基线值与交互轮次
/情绪日志 - 情绪波动记录
/叫我 昵称 - 设置她对你的称呼（没设过时她会自动认一次，认过之后不再改）
/拟人帮助 - 显示本帮助

下面这些是管理员指令，挂在「拟人」组下，要打 /拟人 前缀：

管理员：改配置
/拟人 设置 - 看常用项；写成「/拟人 设置 城市 大阪」就改一项，立即生效
/拟人 参照名称 名字 - 设定注入里代表她的名字（默认跟人设里的名字）
/拟人 诊断 - 排查模型选择、时区是否生效、上下文多大、状态文件多大
/拟人 重载 - 重载插件配置

管理员：动数据
/拟人 重置日程 - 立即重新决定她当前这一段
/拟人 重置状态 - 重置精力、社交能量与生理周期
/拟人 重置情绪 - 把**你自己**的情绪重置到初始值
/拟人 设置好感度 数值 - 手动设置自己的好感度（0-100）
/拟人 批量好感度 QQ:数值 - 批量导入（逗号分隔）
/拟人 查看昵称 - 所有用户设置的昵称"""

NO_PERMISSION = "❌ 权限不足，该指令仅管理员可用。"

# 多消息合并：防抖胜出的那条把同一批里更早的几条原话挂在事件上，交给 on_llm_request
# 前置进 prompt。走事件自带的 extra、不走全局字典，合并数据随事件走，跨事件不会串。
LOG_PREFIX = "[humanoid_core] "

# 默认值一次性迁移的标记文件。AstrBot 更新配置只补缺不覆盖，不调这一手的话老用户
# 会永远停在装插件那一版的行为上。
#
# **文件名不绑版本号**：以前叫 `.defaults-migrated-v2.27.20`，而判据就是「文件在不在」，
# 于是后续版本新增的迁移对已升级用户整体跳过（老用户永远停在旧默认值上）。现在标记里
# 记的是**已应用的迁移项集合**，新增迁移只跳过做过的那几项，其余照常对老用户生效。
DEFAULTS_MIGRATION_MARK = ".defaults-migrated"


def _read_applied_migrations(marker: Path) -> set[str]:
    """标记文件里记的「已应用过哪些迁移项」。读不出来（旧格式/坏文件）当空集。"""
    try:
        raw = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if isinstance(raw, list):
        return {str(item) for item in raw}
    return set()


def _write_applied_migrations(marker: Path, applied: set[str]) -> None:
    """落盘已应用的迁移项集合。写不进去只是下次重跑一遍（迁移本身幂等）。"""
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(sorted(applied), ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _arg_after(text: str, *commands: str) -> str:
    """取指令名之后的剩余文本。

    组指令在真实会话里到达的写法是「拟人 设置 …」（组名和指令名之间一个空格），
    直接调用（旧写法、测试）里是「拟人设置 …」——两种都认，按给的顺序找第一个命中的。
    一个都没命中时退回整串（保留老的兜底行为）。
    """
    raw = (text or "").strip()
    for command in commands:
        index = raw.find(command)
        if index != -1:
            return raw[index + len(command) :].strip()
    return raw.lstrip("/").strip()


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


# 本插件注册的全部指令名（顶层 + 「拟人」组前缀）。
# 用于区分「指令」与「聊天」——**绝不能按「有没有 /」判**：
# 群聊里 `/` 是唤醒前缀（waking_check 会把它从 message_str 上剪掉再交给我们），
# 用户用 `/今天好累` 打招呼时 message_str 是「今天好累」——按 `/` 判会把
# 唤醒词聊天全部误伤成指令、不进情绪记账；而 `/好感度` 被剪成「好感度」也不带 `/`。
# 正确判据与框架的 CommandFilter 一致：剪掉唤醒前缀后的文本（message_str）
# 是否**等于或空格接续**某个已注册指令名。`/今天好累` → 不匹配 → 当聊天；
# `/查看日程` → 「查看日程」→ 命中 → 当指令。
COMMAND_NAMES = (
    "你的状态", "好感度", "情绪详情", "情绪日志", "查看日程", "时间", "叫我", "拟人帮助",
    "拟人",
)


def _is_command_message(text: str, wake_command: bool = True) -> bool:
    """这条（已被唤醒阶段剪掉前缀的）消息是不是一次**真的要执行的指令**。

    两条闸：
    1. 文本等于/以「指令名+空格」开头——与框架 CommandFilter 同口径；
    2. 这条消息是唤醒消息（`is_at_or_wake_command`）——框架只在唤醒时才执行指令，
       群里没 @ 的普通发言说「好感度」不会被当命令，这里也不该拿名单误伤它。
    """
    body = (text or "").strip()
    if not body or not wake_command:
        return False
    for cmd in COMMAND_NAMES:
        if body == cmd or body.startswith(f"{cmd} "):
            return True
    return False


def _append_framing(req: Any, char_name: str = "", user_name: str = "") -> None:
    """把「这些事实该怎么读、指的是谁」放进 system_prompt，一个请求只放一次。

    AstrBot 在 `build_main_agent` 里先把人设写进 system_prompt，再跑 OnLLMRequestEvent
    钩子，所以这里追加的内容排在人设后面；而初始 system 消息不会落进会话历史，
    这段话不会越滚越多。它每轮文本都一样，放 system_prompt 里不会破坏前缀缓存。
    """
    from .humanoid.prompt_builder import FRAMING_MARK, build_framing

    current = getattr(req, "system_prompt", None)
    if not isinstance(current, str):
        current = ""
    if FRAMING_MARK in current:
        return
    req.system_prompt = current + build_framing(char_name, user_name)


def _state_note(core: Any, user_id: str, char_name: str = "", user_name: str = "",
                is_group: bool = False) -> str:
    """I1/I2：算「此刻的她」那一段（关系阶段 + 心境 + 她这会儿在过什么）。

    它每轮都在变，调用方把它当临时内容块发出去（不落会话历史），不写进 system_prompt——
    否则整段提示词的前缀缓存会逐轮失效。日程与外界走这里、不进事实块（I2）：
    单独成段只当她「知道」，要不要提由她自己定。
    """
    try:
        from .humanoid import relstage
        from .humanoid.prompt_builder import build_state_note
        # 关系阶段/心境只在 mood 可读时才算：mood_enabled_in_group=False 时，群聊里的
        # 陌生人直接 mood.profile() 会现建一份档案（并 mark_dirty），绕过「群里不给
        # 陌生人建档」的闸门。readable() 是插件里唯一的判据入口，三处都调它。
        cfg = core.prompt_builder.config
        try:
            readable = core.mood.readable(
                user_id, is_group,
                enabled=cfg.mood_enabled, in_group=cfg.mood_enabled_in_group,
            )
        except Exception:
            readable = False
        affection = 0.0
        stage = tone = style = ""
        if readable:
            affection = float((core.mood.profile(user_id) or {}).get("affection", 0.0) or 0.0)
            stage = relstage.line_for(affection)
            tone = core.mood.current_tone(user_id)
            style = core.mood.style_tendency(user_id)
        day = ""
        try:
            slot = core.schedule.current_slot() or {}
            event = str(slot.get("event", "") or "").strip()
            loc = str(slot.get("location", "") or "").strip()
            if event:
                day = f"她这会儿正在{event}" + (f"（{loc}）" if loc else "")
        except Exception:
            day = ""
        # 她手上还没做完的那几件事（`ongoing_tasks`，跨天跟着她）也进这一段。
        # 它本来就只被送去社交侧，从没进过普通对话的注入——于是她「手头正压着件事」
        # 这件事，在跟人聊天时根本不在场。放这里是**免费的**：这一段本来就是每轮重发的
        # 临时块，多几行不增加任何前缀缓存代价（真正要省的是 system_prompt）。
        # 它属于 I2「日程与外界」：单独成段只当她「知道」，要不要提由她自己定；
        # 不进事实块，免得被当成一条要回应的内容。
        try:
            ongoing = [str(t).strip() for t in (core.schedule.ongoing_tasks() or []) if str(t).strip()]
        except Exception:
            ongoing = []
        if ongoing:
            # 一件也说「还没做完的事」，不说「几件」——件数在 1~3 之间，写死量词会数错。
            day = "；".join(p for p in (day, "她手上还没做完的事：" + "、".join(ongoing)) if p)
        # 外界（几点/几号/周几/季节/城市/天气）与穿搭都进这一段当背景，不进事实块：
        # 单独成段、只当她「知道」，要不要提由她自己定。
        world = ""
        try:
            world = core.prompt_builder.background_note()
        except Exception:
            world = ""
        # 穿搭：与天气/季节同一类背景——她只「知道」自己穿着什么，要不要提由她自己定。
        try:
            outfit_note = core.outfit_line()
        except Exception:
            outfit_note = ""
        if outfit_note:
            world = "；".join(p for p in (world, outfit_note) if p)
        return build_state_note(char_name, user_name, stage, tone, day, style, world)
    except Exception:
        return ""


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


def _response_text(response: Any) -> str:
    """从 LLM 响应里取她真正输出的话。取不到就空串——**不拿对象 repr 冒充**。

    AstrBot 的 LLMResponse 用 completion_text；旧版/兼容对象可能只有 result_chain。
    """
    if response is None:
        return ""
    for attr in ("completion_text", "text"):
        value = getattr(response, attr, None)
        if isinstance(value, str) and value.strip():
            return value
    chain = getattr(response, "result_chain", None)
    getter = getattr(chain, "get_plain_text", None)
    if callable(getter):
        try:
            plain = getter()
            if isinstance(plain, str) and plain.strip():
                return plain
        except Exception:
            pass
    return ""


# 常用项里各类型的写法：布尔能接受 开/关/是/否/true/false，枚举认面板那几个值。
_BOOL_WORDS = {
    "开": True, "关": False, "是": True, "否": False, "on": True, "off": False,
    "true": True, "false": False, "1": True, "0": False, "要": True, "不要": False,
}
_ENUM_WORDS = {
    "inject_activity_context": {"medium", "full", "mood_only"},
    "environment_mode": {"private", "group", "both"},
}
# 命令行只能传字符串，这些项必须显式转成 int，否则写回配置文件的是 "60" 而不是 60。
# 合法区间与 `HumanoidConfig.from_raw` 共用 config.py 里的那一份：以前这里只做
# `int(text)`、没有范围检查，`/拟人 设置 预算 -1` 会把 -1 原样写进配置，解析时被
# 钳到 0（= 不限额度），而回执说「已设为 -1」——意图完全反转且事后无从察觉。
_INT_KEYS = frozenset(INT_SETTING_RANGES)


def _interval_conflict(new_min: int, current_max: Any) -> int | None:
    """`/拟人 设置 间隔` 的交叉校验：下限越过上限时返回那个上限，否则 None。

    `间隔` 只写 `schedule_min_interval_minutes`，没有这一道就能造出 `120~60` 的
    倒置区间：抽随机时 `hi <= lo` 会静默退化成固定值，而 `_settings_summary` 与
    诊断还各按自己的口径打印（一个说「120~60 随机」、一个说「120 分钟」）。
    """
    try:
        limit = int(current_max)
    except (TypeError, ValueError):
        return None
    return limit if new_min > limit else None


def _coerce_setting(key: str, value: str, current: Any) -> Any:
    """把命令行上的一句话翻成这一项该存的类型。认不出来时返回 None，让调用方报错。"""
    text = (value or "").strip()
    if isinstance(current, bool) or key in ("schedule_use_persona", "use_llm_schedule", "debug_mode"):
        lowered = text.lower()
        for word, flag in _BOOL_WORDS.items():
            if lowered == word.lower():
                return flag
        return None
    if key in _INT_KEYS:
        # 不走下面的默认分支：那一路会把 "60" 当字符串写进配置文件。
        try:
            number = int(text)
        except ValueError:
            return None
        low, high = INT_SETTING_RANGES[key]
        # 越界不钳制、直接拒绝：写进去的是用户的原话，钳制只会让回执与生效值对不上。
        return number if low <= number <= high else None
    if key in _ENUM_WORDS:
        lowered = text.lower()
        return lowered if lowered in _ENUM_WORDS[key] else None
    if key == "admin_qq":
        items = [part.strip() for part in re.split(r"[,，\s]+", text) if part.strip()]
        return items or None
    if not text:
        return None
    return text


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
        # 输入状态（正在输入）：按会话跑一个高频上报的小任务，纯装饰，出错不影响回复。
        self._input_state = InputStateNotifier(
            logger,
            interval=self._config.input_state_interval_seconds,
            timeout=self._config.input_state_timeout_seconds,
        )
        # 多消息合并（防抖）的按会话状态；进程内内存，键是 unified_msg_origin。
        self._debounce = Debouncer(lambda: self._config, logger)
        # 记忆联动的桥接对象：懒解析一次（插件加载顺序不保证，构造时对面可能还没就绪）。
        self._memory_bridge_obj: Any = None
        self._memory_bridge_resolved = False
        # 写回侧：每个角色最近一次**已经写过**的过程名，以及最近 24 小时写了几条。
        # 进程内内存即可——重启后第一次只记账不写（见 `_write_finished_process`）。
        self._last_written_process: dict[str, str] = {}
        self._life_write_stamps: dict[str, list[float]] = {}

        state_path = data_dir / "state.json"
        self._state_store = StateStore(state_path, lambda: self._config.state_flush_interval_seconds, logger)

        clock = Clock(lambda: self._config)
        today = clock.today_str()
        self._state_store.load(today, self._config.cycle_length)

        self.resolver = ProviderResolver(context, logger)
        # 闸门建在网关这一层：网关全局只有一份，所有 bot 共用它，
        # 所以每日预算与最小间隔是整个插件一起算的，不是每个 bot 各有一份。
        self.call_gate = CallGate(lambda: self._config, logger)
        # 每日额度落盘。以前只在内存里，重启一次就归零——「一天 N 次」实际上
        # 是「每次重启再来 N 次」，而诊断页里看不出来。
        self.call_gate.bind_ledger(data_dir / "llm_usage.json")
        self.gateway = LLMGateway(
            self.resolver, lambda: self._config, logger, gate=self.call_gate
        )
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
        """把仍等于旧默认值的配置项一次性提升本版默认值，并写回配置文件。

        标记只在**真正落盘成功之后**才写。以前不是：`save_config` 不可调用时
        （普通 dict，或框架换了配置对象）改动只留在内存里，日志却宣告「已提升」、
        标记也照样写下——下次启动 AstrBot 从磁盘读回旧值，而标记让迁移**永远**
        不再跑。用户看到的是一句「已提升」，实际一条都没生效。
        """
        if not isinstance(raw_config, dict):
            return
        marker = data_dir / DEFAULTS_MIGRATION_MARK
        applied = _read_applied_migrations(marker)
        changes = {
            key: value
            for key, value in plan_default_migrations(raw_config).items()
            if key not in applied
        }
        if not changes:
            return
        for key, value in changes.items():
            raw_config[key] = value
        save = getattr(raw_config, "save_config", None)
        if not callable(save):
            # 改动只在内存里，重启就没了。**绝不能写标记**：写了之后下次启动读到的是
            # 旧值，而标记让这几条迁移永远不再重试。
            logger.warning(
                f"{LOG_PREFIX} 配置对象不可写（没有 save_config），默认值迁移未落盘、本次跳过："
                + "、".join(changes)
            )
            return
        try:
            save()
        except Exception as exc:
            logger.warning(f"{LOG_PREFIX} 默认值迁移写回失败，下次启动重试: {exc}")
            return
        logger.info(
            f"{LOG_PREFIX} 已提升旧默认值："
            + "、".join(f"{key}={value!r}" for key, value in changes.items())
        )
        _write_applied_migrations(marker, applied | set(changes))

    async def initialize(self) -> None:
        await self._state_store.start()
        await self.role_manager.start()
        for core in self.role_manager.get_all():
            core.schedule.current_slots()

    async def terminate(self) -> None:
        # 合并缓冲里可能还压着没发出的内容（用户发了半句就没下文了）。卸载时丢掉是对的：
        # 它的主人已经不在了，放到下一个会话里说出去反而是惊吓。
        self._debounce.reset()
        await self._input_state.stop_all()
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

    # -------------------- 指令 --------------------
    #
    # 以前 18 个全挂在 `@command_group("拟人")` 下面，于是全都得打 `/拟人 XXX`。
    # 有使用者反馈这比早期版本难用——早期就是直接 `/状态`。
    #
    # 现在分成两层：
    #   · **所有人能用的 8 个走顶层**，恢复成 `/你的状态`、`/查看日程`、`/拟人帮助`…
    #     不带任何前缀。
    #   · **管理员的 10 个留在「拟人」组里**（`/拟人 设置`），它们本来就不该随手可用，
    #     多一层前缀反而是好事。
    #
    # 当初担心过「三个同名 `@command_group` 会不会互相覆盖」，所以才把所有指令塞进
    # 一个组里。改完之后**只剩一个组**，那个顾虑不存在了。

    @filter.command_group("拟人")
    def humanoid_group(self):
        """人形化伴侣 · 管理指令（全部需要管理员）

        改配置：参照名称 / 诊断 / 设置 / 重载
        动数据：重置日程 / 重置状态 / 重置情绪 / 设置好感度 / 批量好感度 / 查看昵称

        查状态的指令不带前缀，直接用：/你的状态 /好感度 /情绪详情 /情绪日志
        /查看日程 /时间 /叫我 /拟人帮助
        """

    @filter.command("你的状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看当前角色的完整状态（精力、身体轴、生理、天气、日程、过程、社交能量等）。"""
        core = self._core(event)
        yield event.plain_result("\n".join(core.status_lines(self._sender(event))))

    @filter.command("好感度")
    async def cmd_mood(self, event: AstrMessageEvent):
        """查看当前用户的好感度；带「排行榜」看所有人的排名（管理员）。"""
        if not self._config.mood_enabled:
            yield event.plain_result("情绪系统未开启。")
            return
        argument = _arg_after(event.message_str, "好感度")
        if argument in ("排行榜", "排行", "排名", "榜"):
            if not self._is_admin(event):
                yield event.plain_result(NO_PERMISSION)
                return
            core = self._core(event)
            yield event.plain_result(self.engine.affection_ranking_text(core))
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
            yield event.plain_result("昵称太长了（最多 32 字）")
            return
        # 这个名字会被拼进上下文（`对TA的称呼是「…」`），所以在入口就按同一套白名单清洗。
        # 原来只查长度：填个带换行或「」的昵称能闭合注入里的引号，而且此后**每一轮**都生效。
        # 清洗结果与原输入不同时明说一句，别让用户以为存进去的是他写的那个。
        from .humanoid.prompt_builder import sanitize_name

        cleaned = sanitize_name(nickname)
        if not cleaned:
            yield event.plain_result(
                "这个称呼用不了：请只用字母、数字、汉字（可带 - . 空格与表情），"
                "不要带引号、换行，也不要含「你 / 她 / TA」这类代词。"
            )
            return
        core.mood.set_nickname(user_id, cleaned, src="user")
        if cleaned != nickname.strip():
            yield event.plain_result(f"✅ 记住了，以后叫你：{cleaned}（已去掉称呼里不能用的字符）")
        else:
            yield event.plain_result(f"✅ 记住了，以后叫你：{cleaned}")

    @humanoid_group.command("参照名称")
    async def cmd_set_char_name(self, event: AstrMessageEvent):
        """管理员：设定注入里代表这个角色的名字。

        注入里现在不写「她」，写这个名字——模型在 system_prompt 里认的是「你叫XX」，
        于是「小雨今天有点堵」它当成自己说的话，「她今天有点堵」它当成在转述别人的事。
        名字要先用管理员设的（这里），没设才跟随人设里的名字。
        """
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        value = _arg_after(event.message_str, "参照名称")
        core = self._core(event)
        if not value:
            current = core.char_name()
            yield event.plain_result(
                f"当前参照名称：{current or '（未设定）'}"
                + "\n（没设时自动跟用人设里的名字；写「/拟人 参照名称 清除」可以去掉设定）"
            )
            return
        if value in ("清除", "默认", "跟随", "跟随人设", "无"):
            core.set_char_name("")
            yield event.plain_result("✅ 已清除，之后跟随人设里的名字")
            return
        if len(value) > 24:
            yield event.plain_result("名字太长了，24 个字以内。")
            return
        # 这个名字会被拼进**每一轮**的 system_prompt（`prompt_builder.build_framing`
        # 里 `role = f"「{who}」"`），所以必须和 /叫我 同一套白名单清洗：原来只查
        # 长度与「她/你/TA」，带换行、「」、引号、控制字符的名字能闭合引号并另起一行，
        # 此后每一轮请求都生效。
        from .humanoid.prompt_builder import sanitize_name

        cleaned = sanitize_name(value)
        if not cleaned:
            yield event.plain_result(
                "这个名字用不了：请只用字母、数字、汉字（可带 - . 空格与表情），"
                "不要带引号、换行，也不要含「她 / 你 / TA」这类代词。"
            )
            return
        core.set_char_name(cleaned)
        if cleaned != value.strip():
            yield event.plain_result(
                f"✅ 参照名称已设为「{cleaned}」（已去掉名字里不能用的字符），"
                "注入里会用这个名字称呼她。"
            )
        else:
            yield event.plain_result(f"✅ 参照名称已设为「{cleaned}」，注入里会用这个名字称呼她。")

    @filter.command("拟人帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """显示所有指令的帮助信息。"""
        yield event.plain_result(HELP_TEXT)

    # -------------------- 管理员指令 --------------------

    @filter.permission_type(filter.PermissionType.ADMIN)
    @humanoid_group.command("诊断")
    async def cmd_diagnose(self, event: AstrMessageEvent):
        """诊断模型链配置、冷却状态、日程生成情况等。"""
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        # 诊断是唯一的排查入口，它自己报错等于把用户挡在门外（signals 的 age 为 None
        # 曾让 `f"{None:.0f}"` 抛 TypeError 一路冒到这里）。整条包一层兜底。
        try:
            core = self._core(event)
            text = self.engine.diagnostics_text(core)
        except Exception as exc:  # noqa: BLE001 - 兜底就是为了诊断页还能打开
            logger.warning(f"{LOG_PREFIX} 诊断生成失败: {exc}")
            text = f"⚠ 诊断生成失败：{type(exc).__name__}: {exc}"
        # 配置整体退回默认值时，报告最上面必须写明——否则用户看到的城市/模型链/开关
        # 全是出厂值，却以为是自己在面板上配的那份（ConfigBox.load_error 以前没人读）。
        banner = config_error_banner(self._config_box.load_error)
        if banner:
            text = "\n".join(banner) + "\n" + text
        yield event.plain_result(text)

    # 面板上 89 项、AstrBot 又不支持分组，改一个城市要翻半天。这一条命令只管常用那几项，
    # 进阶项仍然去面板改——两边改的是同一份配置文件。
    SETTINGS_ALIASES = {
        "城市": "timezone_city",
        "所在地": "timezone_city",
        "人设": "schedule_use_persona",
        "日程人设": "schedule_use_persona",
        "模型日程": "use_llm_schedule",
        "偏好": "schedule_prompt_extra",
        "间隔": "schedule_min_interval_minutes",
        "调用间隔": "schedule_min_interval_minutes",
        "静默": "llm_idle_silence_minutes",
        "空闲静默": "llm_idle_silence_minutes",
        "预算": "llm_daily_call_budget",
        "日预算": "llm_daily_call_budget",
        "上下文": "inject_activity_context",
        "环境": "environment_mode",
        "穿搭": "__outfit__",
        "衣服": "__outfit__",
        "穿着": "__outfit__",
        "管理员": "admin_qq",
        "调试": "debug_mode",
    }

    @filter.permission_type(filter.PermissionType.ADMIN)
    @humanoid_group.command("设置")
    async def cmd_settings(self, event: AstrMessageEvent):
        """`/拟人 设置` 看常用项；`/拟人 设置 城市 大阪` 改一项，改完立即生效。"""
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        raw = event.message_str or ""
        argument = _arg_after(raw, "拟人 设置", "拟人设置")
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
                + "\n例：/拟人 设置 城市 大阪"
            )
            return
        # 城市按机器人分离：写当前这个 bot 自己的覆盖，不动全局配置。
        # 两个机器人各发各的 /拟人 设置 城市，互不影响（以前共一份全局，改一个另一个也变）。
        if key == "timezone_city":
            if not value:
                yield event.plain_result(
                    "用法：/拟人 设置 城市 大阪（只改当前这个机器人）；"
                    "写「/拟人 设置 城市 默认」可取消单独设置、回到跟随全局。"
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
        # 穿搭按机器人分离：写当前这个 bot 自己的覆盖（与城市同一套做法）。
        if key == "__outfit__":
            if not value:
                cur = core.outfit_override() or "（自动：按人设判物种）"
                yield event.plain_result(
                    f"当前穿搭：{cur}\n"
                    "用法：/拟人 设置 穿搭 白色连衣裙（只改当前这个机器人）；"
                    "写「/拟人 设置 穿搭 自动」可取消，回到按人设自动判。"
                )
                return
            effective = core.set_outfit_override(value)
            if effective:
                yield event.plain_result(f"✅ 当前机器人的穿搭已单独设为：{effective}")
            else:
                yield event.plain_result("✅ 已取消单独穿搭，回到按人设自动判。")
            return
        raw_config = self._config_box.raw
        if not isinstance(raw_config, dict):
            yield event.plain_result("配置不是可写的字典，这次没改。请去面板里改。")
            return
        current = raw_config.get(key)
        if not value:
            yield event.plain_result(
                f"「{key_word}」现在是 {current!r}；要改就写：/拟人 设置 {key_word} 新值"
            )
            return
        new_value = _coerce_setting(key, value, current)
        if new_value is None:
            limits = INT_SETTING_RANGES.get(key)
            extra = f"（合法范围 {limits[0]}~{limits[1]}）" if limits is not None else ""
            yield event.plain_result(
                f"「{value}」不是这一项能接受的值{extra}。当前值：{current!r}"
            )
            return
        # 间隔是 min≤max 的一对，而这一条只写 min：越过上限会造出 120~60 的倒置区间。
        if key == "schedule_min_interval_minutes":
            limit = _interval_conflict(new_value, raw_config.get("schedule_max_interval_minutes"))
            if limit is not None:
                yield event.plain_result(
                    f"间隔下限不能大于上限（当前上限 {limit} 分钟）。"
                    f"先写「/拟人 设置 间隔 {limit}」，或去面板把「间隔上限」改大。"
                )
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
        # 回执报**生效值**而不是用户输入：解析层会钳制/退回默认值，报原话会让
        # 「回执说 -1、实际 0」这类不一致再出现（设置项与生效值必须是同一个数）。
        effective = getattr(self._config, key, new_value)
        yield event.plain_result(
            f"✅ {key_word} 已设为 {effective!r}"
            + ("（未能写入配置文件，重启后会回到旧值）" if not saved else "")
        )

    def _settings_summary(self, core=None) -> str:
        cfg = self._config
        lines = [f"⚙️ 常用设置（共 {len(HumanoidConfig.__dataclass_fields__)} 项，其余标了【进阶】，在面板里改）"]
        # 城市是每个机器人独立的：优先显示当前 bot 生效城市，并标明是否单独设。
        if core is not None:
            override = str(core.scope.get_self("tz_city_override", "") or "")
            effective = core.clock.city or "（未定）"
            tag = "当前 bot 单独设" if override else f"跟随全局 {cfg.timezone_city or '（未定）'}"
            lines.append(f"- 城市：{effective}（{tag}）　→ /拟人 设置 城市 大阪")
        else:
            lines.append(f"- 城市（全局默认）：{cfg.timezone_city or '（未定）'}　→ /拟人 设置 城市 大阪")
        lines.append(f"- 日程用人设：{'开' if cfg.schedule_use_persona else '关'}　→ /拟人 设置 人设 开")
        lines.append(f"- 大模型日程：{'开' if cfg.use_llm_schedule else '关'}（每 {cfg.schedule_refresh_minutes} 分钟决定一次要不要排下一段，变动概率 {cfg.schedule_change_chance}%）")
        _lo = cfg.schedule_min_interval_minutes
        _hi = cfg.schedule_max_interval_minutes
        _gap = f"{_lo}~{_hi} 分钟随机" if _hi > _lo else f"{_lo} 分钟"
        lines.append(
            f"- 调用节流：间隔 {_gap}"
            f"，空闲 {cfg.llm_idle_silence_minutes} 分钟静默"
            f"，今日已用 {self.call_gate.used_today}/"
            f"{cfg.llm_daily_call_budget or '不限'} 次（所有 bot 共享）"
            f"　→ /拟人 设置 间隔 30"
        )
        lines.append(f"- 日程额外偏好：{cfg.schedule_prompt_extra or '（空）'}")
        lines.append(f"- 上下文详略：{cfg.inject_activity_context}（medium/full/mood_only）")
        lines.append(f"- 参与环境：{cfg.environment_mode}（private/group/both）")
        if core is not None:
            oi = core.outfit_info()
            if oi.get("enabled"):
                lines.append(
                    f"- 穿搭：{oi.get('text') or '（不注入）'}（{oi.get('source')}）"
                    "　→ /拟人 设置 穿搭 白色连衣裙"
                )
            else:
                lines.append("- 穿搭：关　→ 面板里开 outfit_enabled")
        else:
            lines.append(
                f"- 穿搭：{'开' if cfg.outfit_enabled else '关'}"
                f"（手写预设：{cfg.outfit_preset or '（空，按人设自动判）'}）"
            )
        admins = "、".join(cfg.admin_qq) if cfg.admin_qq else "（未设，靠 AstrBot 全局 admins_id）"
        lines.append(f"- 管理员：{admins}")
        lines.append(f"- 调试日志：{'开' if cfg.debug_mode else '关'}")
        lines.append("改完立即生效；进阶项用 /拟人 重载 刷新。")
        return "\n".join(lines)

    @humanoid_group.command("重载")
    async def cmd_reload(self, event: AstrMessageEvent):
        """热重载插件配置，无需重启。"""
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        self._config_box.reload()
        # 合并等待秒数可能被改了，把压着的半句清掉，免得它按旧配置等完才发。
        self._debounce.reset()
        self._input_state.configure(
            self._config.input_state_interval_seconds,
            self._config.input_state_timeout_seconds,
        )
        yield event.plain_result(f"✅ 配置已重载。当前注入档位：{self._config.inject_activity_context}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @humanoid_group.command("重置日程")
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
                yield event.plain_result("✅ 已按身体现排一段（模型不可用，可用 /拟人 诊断 查看配置）。")
        else:
            yield event.plain_result(
                f"❌ 生成失败：{core.schedule.last_error}（可用 /拟人 诊断 查看模型配置）"
            )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @humanoid_group.command("重置状态")
    async def cmd_reset_state(self, event: AstrMessageEvent):
        """重置精力、社交能量和生理周期至初始值。"""
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        core = self._core(event)
        energy, social, cycle = core.engine_compat.reset_state()
        yield event.plain_result(f"✅ 已重置：精力 {int(energy)}，社交 {int(social)}，周期第 {cycle} 天。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @humanoid_group.command("重置情绪")
    async def cmd_reset_mood(self, event: AstrMessageEvent):
        """重置当前用户的情绪至初始值。"""
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        core = self._core(event)
        r = core.mood.reset(self._sender(event))
        yield event.plain_result(f"✅ 好感度 {r['affection']:.0f}，亲近欲 {r['libido']:.0f}，攻击性 {r['aggression']:.0f}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @humanoid_group.command("设置好感度")
    async def cmd_set_affection(self, event: AstrMessageEvent):
        """手动设置当前用户的好感度（0-100）。"""
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        match = re.search(r"(\d+(?:\.\d+)?)", _arg_after(event.message_str, "设置好感度"))
        if not match:
            yield event.plain_result("用法：/拟人 设置好感度 数值 (0-100)")
            return
        v = float(match.group(1))
        if not 0 <= v <= 100:
            yield event.plain_result("数值必须在 0-100 之间。")
            return
        core = self._core(event)
        core.mood.set_affection(self._sender(event), v)
        yield event.plain_result(f"✅ 好感度已设为 {v:.0f}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @humanoid_group.command("批量好感度")
    async def cmd_batch_affection(self, event: AstrMessageEvent):
        """批量导入好感度（格式：QQ:数值, QQ:数值）。"""
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        pairs, malformed = self.engine.parse_affection_batch(_arg_after(event.message_str, "批量好感度"))
        if not pairs:
            yield event.plain_result("格式错误，请使用：/拟人 批量好感度 QQ:数值")
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
    @humanoid_group.command("查看昵称")
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

    # -------------------- 输入状态（正在输入） --------------------
    #
    # 做法照市场插件 astrbot_plugin_input_state_by_nc：LLM 请求开始起一个高频上报的小任务，
    # 响应回来就停。只需 NapCat（aiocqhttp）私聊；其它平台/群聊在 is_supported 里直接跳过。

    @filter.on_llm_request(priority=1)
    async def show_input_state(self, event: AstrMessageEvent, req: ProviderRequest):
        """她要开始写回复了：给 QQ 报「正在输入」。

        priority=1 是为了比其它插件的 on_llm_request 先跑，不然得等它们做完准备工作
        才开始显示（参考插件也是这么标的）。
        """
        try:
            if not self._config.input_state_enabled:
                return
            await self._input_state.start(event.unified_msg_origin, event)
        except Exception as e:
            logger.debug(f"{LOG_PREFIX} 启动输入状态失败: {e}")

    @filter.on_llm_response()
    async def hide_input_state(self, event: AstrMessageEvent, response: Any):
        """回复生成了：停上报。"""
        try:
            await self._input_state.stop(event.unified_msg_origin)
        except Exception as e:
            logger.debug(f"{LOG_PREFIX} 停止输入状态失败: {e}")

    @filter.on_decorating_result()
    async def keep_input_state_before_send(self, event: AstrMessageEvent):
        """马上要发消息了：再补一帧，别在「写好了、还没发」的空档里断掉。"""
        try:
            if not self._config.input_state_enabled:
                return
            await self._input_state.refresh(event.unified_msg_origin, event)
        except Exception as e:
            logger.debug(f"{LOG_PREFIX} 补齐输入状态失败: {e}")

    # -------------------- 长期记忆联动 --------------------

    def _memory_bridge(self):
        """拿 memory_companion 的桥接对象；没装 / 没就绪返回 None。

        懒解析一次：插件加载顺序不保证，构造时对面可能还没就绪。只查宿主注册表，
        不 importlib——没装时 `import_module` 会凭空造一个空模块（「没装却说装了」）。
        """
        if not self._memory_bridge_resolved:
            self._memory_bridge_resolved = True
            try:
                self._memory_bridge_obj = memory_link.find_memory_bridge(
                    getattr(self, "context", None)
                )
            except Exception:
                self._memory_bridge_obj = None
        return self._memory_bridge_obj

    async def _memory_note(self, umo: str, user_id: str, said: str, is_group: bool) -> str:
        """按 TA 这条消息去记忆库要几条相关长期记忆；拿不到返回空串。

        只读。拿不到时静默降级——没有长期记忆也该照常回话，不能因为对面没装就少说话。
        """
        bridge = self._memory_bridge()
        if bridge is None:
            return ""
        try:
            return await memory_link.fetch_memory_text(
                bridge, umo, user_id, query=said, is_group=is_group
            )
        except Exception:
            return ""

    # 她生活里的事一天最多写几条。过程几小时换一次，不封顶的话记忆库会被她自己的
    # 流水账淹掉——那些事在**聊天里看不见**，本来就不该占掉「关于这个人」的位置。
    LIFE_WRITE_PER_DAY = 3
    LIFE_WRITE_WINDOW_SECONDS = 86400.0

    async def _write_finished_process(self, core, umo: str, user_id: str) -> None:
        """她做完一件事之后，往长期记忆里补一条。

        为什么由这边写：这件事**在聊天里看不见**。过程一结束，`schedule.ongoing_tasks()`
        也再报不出它，记忆插件自己更读不到——只有她（Core）知道。中间层（`life_log`）到此
        为止，但这件事不该随过程一起消失。

        第一次看到某个名字只记账不写：插件重启后 `process_history` 里已经躺着若干条，
        照单全写等于每次重启都往记忆库塞一批旧事。
        """
        bridge = self._memory_bridge()
        if bridge is None or not user_id:
            return
        try:
            names = core.process.recent(1)
        except Exception:
            return
        name = str(names[0]).strip() if names else ""
        if not name:
            return
        bid = str(getattr(core, "role_id", "") or "")
        previous = self._last_written_process.get(bid)
        self._last_written_process[bid] = name
        if previous is None or previous == name:
            return
        now = time.time()
        stamps = [
            t
            for t in self._life_write_stamps.get(bid, [])
            if now - t < self.LIFE_WRITE_WINDOW_SECONDS
        ]
        self._life_write_stamps[bid] = stamps
        if len(stamps) >= self.LIFE_WRITE_PER_DAY:
            return
        try:
            await memory_link.write_life_event(
                bridge,
                user_id,
                # 不写「刚刚」：过程是 Core 按自己的节拍推进的，这边只在她下一条消息
                # 到达时才发现它结束了，中间可能隔了几个小时。写死时间就是写错。
                f"她做完了{name}",
                umo=umo,
                # 幂等键按内容算：同一件事重放不会写两条（时刻参与的话每次都是新键）。
                key=f"proc:{name}",
            )
        except Exception:
            return
        stamps.append(now)

    # -------------------- LLM 请求注入钩子 --------------------

    @filter.on_llm_request()
    async def inject_context(self, event: AstrMessageEvent, req: ProviderRequest):
        """身体与生活进上下文：稳定的说明进 system_prompt，每轮变化的内容进临时内容块。

        为什么这么分：

        1. 「这些事实是什么、按什么方式读」这段不随消息变，只进 system_prompt 一次——
           它是参考，不该被当成用户刚说的一句话。初始 system 消息不入历史
           （`agent_sub_stages/internal.py` 里 `skipped_initial_system` 就是干这个的）。
        2. 「此刻的她」与身体事实每轮都在变，走 `extra_user_content_parts` 并
           `mark_as_temp()`：临时块只发给模型、不落会话历史（`_save_to_history` 落盘时
           会丢掉带 `_no_save` 的块），所以既不用回写清理旧块，也把变化的文本压在整个
           提示词最末尾——前缀缓存不会被逐轮击穿。
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
            # 兄弟 bot 的发言不该被回复：合并层那道只是不合并，不拦的话另一台 bot
            # 在群里 @ 一下，本台照样会正经回它一条（多 bot 串台）。
            # 必须 stop_event 才能真的挡下本轮 LLM，光 return 挡不住。
            if user_id and user_id in self._known_role_ids(exclude=self._self_id(event)):
                stopper = getattr(event, "stop_event", None)
                if callable(stopper):
                    stopper()
                return
            # 注意力（上心程度）要看用户到底说了什么：合并了几条时看合并后的全貌，
            # 否则前几条里的问句、提到的话题都不参与判断，注意力会被算低。
            said = self._debounce.peek_text(event, text)
            # 对话对方的名字：开关开着才用真名（默认关=写「TA」）。取的是**这条消息的
            # 发送者名**——群聊是群名片、私聊是 QQ 昵称，不依赖 AstrBot 的任何配置开关。
            user_name = ""
            if self._config.auto_identify_user:
                user_name = _sender_name(event)
            char_name = core.char_name()
            # 长期记忆：只读，按 TA 这条消息检索（同一会话 60 秒内只查一次，
            # 连着发三条不会连查三次记忆库）。
            memory_text = await self._memory_note(
                str(getattr(event, "unified_msg_origin", "") or ""),
                user_id,
                said,
                is_group,
            )
            # 时间间隔由 Core.on_message 统一记账，这里只读。
            injection = core.build_injection(
                user_id,
                is_group=is_group,
                text=said,
                user_name=user_name,
                memory_text=memory_text,
            )
            # 长期记忆：写。她刚做完一件事的话，交一条给记忆库——这件事只有她知道。
            # 只在过程真的换了名字时写，所以不在回复的热路径上多出一次 IO。
            await self._write_finished_process(
                core, str(getattr(event, "unified_msg_origin", "") or ""), user_id
            )

            if self._config.debug_mode:
                from .humanoid.prompt_builder import estimate_tokens

                logger.debug(
                    f"[humanoid_core] 注入上下文 {len(injection)} 字符"
                    f" ≈{estimate_tokens(injection)} token（{self._config.inject_activity_context} 档）:\n{injection}"
                )

            if injection:
                # 框架句在 system_prompt 里说的是「以下是她当前的**真实处境**」。
                # 注入为空时（预算卡太小、构建抛异常）留着它，模型收下一句宣告却一条事实都
                # 拿不到——它会自己编。所以没有事实就不要宣告。
                _append_framing(req, char_name, user_name)
                # 「此刻的她」与身体事实走临时内容块：只发给模型、不落历史。
                state_note = _state_note(core, user_id, char_name, user_name, is_group=is_group)
                parts = getattr(req, "extra_user_content_parts", None)
                if not isinstance(parts, list):
                    parts = []
                    req.extra_user_content_parts = parts
                if state_note:
                    parts.append(TextPart(text=state_note).mark_as_temp())
                parts.append(TextPart(text=injection).mark_as_temp())

        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 注入失败: {e}")

    # -------------------- LLM 响应钩子 --------------------

    @filter.on_llm_response()
    async def note_her_reply(self, event: AstrMessageEvent, response: Any):
        """把她**真正说出口的话**记一笔，供情绪分析判断「她说的这句对方接没接」。

        这个记录以前挂在 `on_llm_request` 上、拿 `req.prompt` 当「她要发的话」——
        但那是**用户刚发来的消息**（AstrBot 里 `req.prompt = event.message_str`）。
        于是整段记录张冠李戴：分析器看到「她说『在吗』——对方还没回应」，
        实际是用户在说话、她在回。错误记录比不记更糟——它直接搅她对这段关系的感受。
        响应钩子拿到的 `response` 才是她的回复本身。

        第三方 Agent runner 不触发本钩子（见 `tool_loop_agent_runner` 只在自己完稿时
        调 `on_agent_done`）——那种部署由 `capture_reply_on_send` 在发送前兜底。
        两边用 `note_spoke` 自带的短窗去重，不会双重记账。
        """
        try:
            is_group = not _is_private_chat(event)
            if not self.engine.environment_allows(not is_group):
                return
            text = _response_text(response)
            if not text:
                return
            cfg = self._config
            core = self.role_manager.get_or_create(self._self_id(event))
            user_id = self._sender(event)
            if not core.mood.readable(user_id, is_group, enabled=cfg.mood_enabled,
                                      in_group=cfg.mood_enabled_in_group):
                return
            core.mood.note_spoke(user_id, text)
            # 给发送前兜底留一个「这轮已记过」的标记：本地 runner 两条路径都会经过，
            # 没有它的时候，若框架给回复加了前缀（先发设置里有「回复前缀」），
            # 兜底看到的文本与 completion_text 不同、去重拦不住，同一句话会被记两遍。
            setter = getattr(event, "set_extra", None)
            if callable(setter):
                try:
                    setter("_hcore_reply_captured", True)
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"{LOG_PREFIX} 记录她的发言失败: {e}")

    # -------------------- 发送前兜底（第三方 runner / 流式） --------------------

    @filter.on_decorating_result()
    async def capture_reply_on_send(self, event: AstrMessageEvent):
        """发送前把她的回复再兑一道底——只在 `on_llm_response` 没记到时生效。

        为什么需要它：第三方 Agent runner（`agent_runner_type != "local"`）的流程
        不触发 `OnLLMResponseEvent`，本地 runner 的流式收尾也绕开了它。那两种部署下
        「她说了什么」会一片空白，情绪分析就退化回只看见用户的话。发送前钩子在
        所有模式都会经过，拿 `event.get_result()` 里的纯文本当素材。

        只收 LLM 类结果（`LLM_RESULT` / 流式收尾）：指令回复、报错回显这类
        不是她的发言。
        """
        try:
            read_extra = getattr(event, "get_extra", None)
            if callable(read_extra):
                try:
                    if read_extra("_hcore_reply_captured"):
                        return
                except Exception:
                    pass
            result = event.get_result()
            if result is None:
                return
            ctype = getattr(result, "result_content_type", None)
            name = getattr(ctype, "name", "")
            if name not in ("LLM_RESULT", "STREAMING_FINISH"):
                return
            getter = getattr(result, "get_plain_text", None)
            if not callable(getter):
                return
            text = str(getter() or "").strip()
            if not text:
                return
            is_group = not _is_private_chat(event)
            if not self.engine.environment_allows(not is_group):
                return
            cfg = self._config
            core = self.role_manager.get_or_create(self._self_id(event))
            user_id = self._sender(event)
            if not core.mood.readable(user_id, is_group, enabled=cfg.mood_enabled,
                                      in_group=cfg.mood_enabled_in_group):
                return
            # note_spoke 带 90 秒同文去重：on_llm_response 已经记过时这一步是空操作。
            core.mood.note_spoke(user_id, text)
        except Exception as e:
            logger.debug(f"{LOG_PREFIX} 发送前记录她的发言失败: {e}")

    # -------------------- 多消息合并（消息防抖） --------------------

    def _known_role_ids(self, exclude: str = "") -> set:
        """本插件状态里已经见过的其它角色 id（多 bot 同进程部署时的「兄弟」）。

        它们发出来的消息在事件层看起来就是普通用户——另一台 bot 的自发消息、
        被平台回灌过来时，sender 是那台 bot 的 id，不等于本台，所以「跳过自己发的」
        那道闸挡不住。叠进合并 buffer 就会变成「她分不清自己在回谁」。
        只认状态里出现过的 id；跨进程的 bot 认不出，README 里写清了边界。
        """
        out: set = set()
        try:
            roles = self._state_store.data.get("roles", {}) or {}
            for rid in roles:
                rid = str(rid or "")
                if rid and rid != "default" and rid != str(exclude):
                    out.add(rid)
        except Exception:
            pass
        return out

    @filter.on_waiting_llm_request()
    async def debounce_merge(self, event: AstrMessageEvent):
        """把同一会话连续触发机器人的多条消息合并成一次回复。

        放这个钩子有三个原因：

        1. 它只在**确定要调 LLM** 时才触发，指令不会走到这里（否则 `/好感度` 也会被等 3 秒）。
        2. 它在**抢会话锁之前**：等一句不占锁，也不挡别的会话。
        3. 同一会话的多条消息各自是并发的 pipeline 任务（event_bus 用 create_task 派发），
           所以等的时候后面的消息能进来把自己顶掉。

        判定「这句话说完了吗」用 `humanoid/merge.py` 的规则而不是固定时间窗：句末有标点或
        语气词的消息等一个短确认窗口（`message_merge_settle_seconds`），拿不准的等满
        `message_merge_timeout_seconds`。两者都会把等待期间到达的后续句并进来——
        「用户发三条、他就回三条」正是因为第一条说完就被零延迟放走了。

        被合并掉的旧消息走 `stop_event()`，它**没有走到 on_llm_request**（所以不会被注入
        事实块），但 `on_message` 照旧逐条记账——身体/情绪/记忆按每条真实发言算，
        只有「回复」是合一的。内容不会丢：一直在 buffer 里，由胜出者带走。
        """
        try:
            cfg = self._config
            if not cfg.message_merge_enabled:
                return
            is_group = not _is_private_chat(event)
            if not self.engine.environment_allows(not is_group):
                return
            text = (getattr(event, "message_str", "") or "").strip()
            if not text:
                # 只对纯文本消息做合并；图片/语音这类照常单独回复。
                return
            # 防御：真指令不该进合并（框架上指令不走到 LLM 阶段，但插件委托的
            # provider_request 可能从其他路径进来）。命中指令名单时直接放行。
            if _is_command_message(text, bool(getattr(event, "is_at_or_wake_command", True))):
                return
            # 只对同一会话做合并，但**分桶的键要带上发送者**：
            # 群里一个 umo 下所有人共用一格，原来只按 umo 合并，会把 A 一句 B 一句
            # 拼成一条批处理——模型拿到两个不相干的人的话，分不清在回谁，
            # 于是写一条长消息把人逐个回一遍（比不合并还糟）。
            # 按（会话, 发送者）分开：同一个人连发才合并，不同的人各回各的。
            try:
                umo = str(getattr(event, "unified_msg_origin", "") or "")
            except Exception:
                umo = ""
            try:
                sender_id = str(self._sender(event) or "")
            except Exception:
                sender_id = ""
            # **跳过自己发的**。`on_message` 有这道检查，合并这里原来没有——
            # 于是她自己在群里说的每句话（尤其是主动消息插件发的那些）都可能被
            # 攒进 buffer 当成「用户刚说了什么」，表现出来就是**随时都在触发**，
            # 而且模型分不清那是不是在回对方。
            if sender_id and sender_id == str(self._self_id(event)):
                return
            # 兄弟 bot 的消息也不进合并：那是机器人，不是「用户刚说了什么」。
            if sender_id and sender_id in self._known_role_ids(exclude=str(self._self_id(event))):
                return
            if not umo:
                return
            key = f"{umo}|{sender_id}" if sender_id else umo
            proceed = await self._debounce.hold(
                event, key=key, text=text, stop_event=event.stop_event,
                sender=_sender_name(event) or "TA",
            )
            if not proceed:
                return
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
            # 兄弟 bot 的消息不是「用户说了什么」：不记账、也不该触发回复。
            # 多台 bot 同进程部署时，另一台 bot 的群发言在事件层就是普通成员，
            # sender 不等于本台，前面那道「跳过自己」挡不住。只认状态里见过的角色 id。
            if user_id and user_id in self._known_role_ids(exclude=role_id):
                return
            # 任何一条真实用户消息都算「有人在用」：松开空闲静默，让日程重新跟上。
            # 放在后续所有早退之前，否则「被过滤的环境」会把静默一直吊着。
            self.call_gate.note_interaction()
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
            # 指令不是交谈：命中了真指令的消息不进聊天记账。
            # 唤醒标志跟框架一致——只有「这条真会被当指令执行」时才跳过；
            # 群里没 @ 的普通发言（唤醒=False）说「好感度」不会被误伤。
            wake_command = bool(getattr(event, "is_at_or_wake_command", True))
            is_command = _is_command_message(text, wake_command)
            try:
                umo = str(getattr(event, "unified_msg_origin", "") or "")
            except Exception:
                umo = ""
            # 记下这个角色最近在哪个会话说话，并预热一次人设：日程要靠 umo 挑生效人格，
            # 注入要用**角色名**（`char_name`，同步读缓存）。解析是 await 且有 10 分钟
            # 缓存，正常只第一次真的跑。
            try:
                source = getattr(core, "persona_source", None)
                if source is not None and umo:
                    source.note_umo(core.role_id, umo)
                    if not source.cached_name(core.role_id):
                        await source.persona(core.role_id)
            except Exception:
                pass
            core.on_message(user_id, text, is_group=is_group, umo=umo, is_command=is_command)
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 消息记账失败: {e}")