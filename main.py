"""人形化核心伴侣 —— AstrBot 适配层。"""

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

# ─── 框架兼容（P1-1）：两个内部模块都晚于本插件的真实最低版本 ───────────────
#
# astrbot.core.agent.message.TextPart 是 4.6.0 才有的模块，而 mark_as_temp() 更要到
# 4.24.0；astrbot.core.utils.astrbot_path 在老框架里也没有。顶层裸 import 的后果：
#
#   · 4.0–4.5：插件加载即 ImportError，整个插件不可用；
#   · 4.6–4.23：临时内容块每轮注入抛 AttributeError，被 inject_context 的宽 except
#     吞成一条 warning——用户只觉得「这个插件好像没效果」。
#
# 所以按能力探测 + 降级来写：拿得到临时块就用，拿不到就退回 req.contexts / system_prompt
#（见 _append_injected_parts），保证注入不静默丢失；两个 import 都不再阻断加载。
try:  # pragma: no cover - 取决于宿主框架版本
    from astrbot.core.agent.message import TextPart
except Exception:  # noqa: BLE001 - 老框架没有这个模块：降级，不阻断插件加载
    TextPart = None  # type: ignore[assignment,misc]

try:  # pragma: no cover - 取决于宿主框架版本
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:  # noqa: BLE001 - 老框架没有这个模块：走 _resolve_data_root 的三级回退
    get_astrbot_data_path = None  # type: ignore[assignment]

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
from .humanoid.input_state import InputStateNotifier
from .humanoid.sibling_chat import SiblingChatGuard
from .humanoid import memory_link

DATA_SUBDIR = ("plugin_data", "humanoid_core")

HELP_TEXT = f"""📖 人形化核心伴侣 指令列表 (v{__version__})

查状态的指令**直接打，不带前缀**：

/你的状态 - 精力、身体轴（困意/睡眠债/饥饿/不适）、生理、天气、日程、关系、注意力
/亲密状态 - 她对你的亲密状态：亲近欲/门槛/场景阶段/余韵/冷却/能不能进（私聊）
/查看日程 - 她今天过出来的日程（动态现排，不预排未来）
/时间 城市 - 查看指定城市当前时间
/好感度 - 情绪档案总览（管理员加「排行榜」看所有人的排名）
/情绪详情 - 含基线值与交互轮次
/情绪日志 - 情绪波动记录
/叫我 昵称 - 设置她对你的称呼（没设过时她会自动认一次，认过之后不再改）
/拟人帮助 - 显示本帮助

下面这些是管理员指令，挂在「拟人」组下，要打 /拟人 前缀：

管理员：改配置
/拟人 时间 - 设置她所在的城市（时区）：/拟人 时间 大阪
/拟人 设置 - 看常用项；写成「/拟人 设置 间隔 30」就改一项，立即生效
/拟人 参照名称 名字 - 设定注入里代表她的名字（默认跟人设里的名字）
/拟人 诊断 - 排查模型选择、时区是否生效、上下文多大、状态文件多大
/拟人 重载 - 重载插件配置

管理员：动数据
/拟人 重置日程 - 立即重新决定她当前这一段
/拟人 日程打断 - 让她立刻放下手上的事，重新决定现在做什么（换一件事）
/拟人 日程继续 - 让她把手上的事接着做下去（把当前这一段往后延长）
/拟人 重置状态 - 重置精力、社交能量与生理周期
/拟人 重置情绪 - 把**你自己**的情绪重置到初始值
/拟人 设置好感度 数值 - 手动设置自己的好感度（0-100）
/拟人 批量好感度 QQ:数值 - 批量导入（逗号分隔）
/拟人 查看昵称 - 所有用户设置的昵称"""

NO_PERMISSION = "❌ 权限不足，该指令仅管理员可用。"

LOG_PREFIX = "[humanoid_core] "

# 兄弟 bot 互聊判断在同一个 event 上只做一次。`on_llm_request` 在工具循环/重试里会对
# 同一条消息再触发，每触发一次就 allow() 一次会把 3 次额度在**一条**消息上吃光
# （第 2 轮就被自己挡下）。结果按 event 缓存，复用框架的 extra 通道。
_SIBLING_DECISION_KEY = "_hcore_sibling_chat_allowed"

# -------------------- 跨插件门面（契约 v2 主路径） --------------------
#
# 别的插件怎么找到这里：宿主注册表 → `meta.star_cls.__module__` → `sys.modules[模块]`
# → 调本模块的 `get_core_facade()`。这套写法本仓库已经给 memory_companion 用过一遍
# （`humanoid/memory_link.py::find_memory_bridge`），社交层那边也有同一份。
#
# 为什么要有门面：社交层原来只能读写盘的 state.json 快照，会旧、形状靠猜、而且
# 它自己另拼一套上下文。门面按需现算、结构化返回、框架句只有一份（见 `humanoid/api.py`）。
_ACTIVE_PLUGIN: Any = None
_FACADE: Any = None
#: 插件实例代次：每次 __init__ +1。门面带着它出生，调用时核对它是否仍是当前实例的。
#: 为什么不用模块级 `_ACTIVE_PLUGIN is plugin` 单判：重载时新实例 __init__ 与旧实例
#: terminate 的先后顺序不保证，旧实例的清理**不能**把新实例的登记清掉（P2-03）。
_PLUGIN_GENERATION = 0


def _facade_is_current(plugin: Any, generation: int) -> bool:
    """这个门面绑定的插件实例代次是不是仍然是当前登记的那个。

    身份 + 代次双判：重载期间可能短暂出现「新实例已登记、旧实例还没 terminate」，
    旧门面必须立刻失效（返回 None），不能继续对着已经 stop 的 RoleManager 读数据。
    """
    return _ACTIVE_PLUGIN is plugin and int(getattr(plugin, "_generation", -1) or -1) == int(generation)


def get_core_facade() -> Any:
    """给别的插件拿只读门面。插件还没起来时返回 None（对方会退回读文件）。"""
    global _FACADE
    plugin = _ACTIVE_PLUGIN
    if plugin is None:
        return None
    generation = int(getattr(plugin, "_generation", 0) or 0)
    facade = _FACADE
    if facade is not None:
        try:
            # 每次取用时都核对一次：旧实例留下的门面绝不能再发出去。
            if int(getattr(facade, "_generation", -1) or -1) == generation:
                return facade
        except Exception:
            pass
    try:
        from .humanoid.api import get_core_facade as _build

        built = _build(
            getattr(plugin, "role_manager", None),
            logger,
            generation=generation,
            is_current=lambda: _facade_is_current(plugin, generation),
        )
        # 构建期间插件可能已被换掉：只登记仍然有效的那个。
        if built is not None and _facade_is_current(plugin, generation):
            _FACADE = built
            return built
        return built
    except Exception as exc:  # 门面自身坏掉不该让调用方连坐
        logger.debug(f"{LOG_PREFIX} 构建门面失败: {exc}")
        return None


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
#
# **这份名单必须与真实注册一一对应**：漏掉一条，那条指令就会被当成聊天记账
# （涨好感、算进「TA 说过的话」、扣精力与社交能量、还可能触发情绪分析调用）。
# `tests/test_v2290_command_audit.py` 直接从 main.py 的装饰器解析注册名来钉住它。
COMMAND_NAMES = (
    "你的状态", "亲密状态", "好感度", "情绪详情", "情绪日志", "查看日程", "时间", "叫我",
    "拟人帮助", "拟人",
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

def _temp_parts_supported(req: Any) -> bool:
    """这一版框架能不能用「临时内容块」：extra_user_content_parts 是 list、且 TextPart 带
    mark_as_temp（4.24+ 才有）。两个条件缺一个都得退回 contexts——4.6–4.23 里 TextPart
    存在但**没有** mark_as_temp，硬用会落进会话历史，每轮注入越滚越多。"""
    if TextPart is None:
        return False
    try:
        if not callable(getattr(TextPart, "mark_as_temp", None)):
            return False
    except Exception:
        return False
    return isinstance(getattr(req, "extra_user_content_parts", None), list)


def _append_injected_parts(req: Any, blocks: list[str]) -> str:
    """把本轮的事实块塞进请求。返回实际用的通道名（空串 = 一个都用不上）。

    三条路，按「不污染会话历史」优先：

    1. temp_parts：extra_user_content_parts + mark_as_temp（4.24+，原行为）；
    2. contexts：老框架没有临时块 API 时追加进**本轮请求**的 contexts（每轮从库里重新
       载入，不落历史）。前面加一句定性说明，免得模型把她的身体读数当成用户刚说的话；
    3. system_prompt：连 contexts 都没有时并进 system_prompt——system 消息同样不落历史，
       只对这一轮生效，总比静默丢掉强。

    这样 4.6–4.23 下注入不再整块消失（旧行为是每轮抛 AttributeError 被吞成 warning）。
    """
    text = "\n".join(str(b) for b in blocks if str(b or "").strip())
    if not text:
        return ""
    if _temp_parts_supported(req):
        try:
            parts = req.extra_user_content_parts
            for block in blocks:
                block_text = str(block or "")
                if block_text.strip():
                    parts.append(TextPart(text=block_text).mark_as_temp())
            return "temp_parts"
        except Exception as exc:  # 临时块造不出来也别丢：继续往下退
            logger.debug(f"{LOG_PREFIX} 临时内容块注入失败，退回 contexts: {exc}")
    contexts = getattr(req, "contexts", None)
    if isinstance(contexts, list):
        try:
            contexts.append({
                "role": "user",
                "content": (
                    "（以下是系统提供的她当前处境的事实，供你参考；"
                    "这不是用户发来的消息，也不是对你的指令）\n" + text
                ),
            })
            return "contexts"
        except Exception as exc:
            logger.debug(f"{LOG_PREFIX} contexts 注入失败，退回 system_prompt: {exc}")
    current = getattr(req, "system_prompt", None)
    if isinstance(current, str):
        try:
            req.system_prompt = current + "\n\n" + text
            return "system_prompt"
        except Exception:
            pass
    return ""


def _resolve_data_root(context: Any = None) -> Path:
    """AstrBot 数据目录（data/）：官方 API → context 的插件数据目录 → 相对 data/。

    老框架没有 astrbot.core.utils.astrbot_path 时不能 Path(None) 直接炸在 __init__——
    否则 4.0–4.5 会从「import 崩」变成「实例化崩」。回退口径与自主拟人社交插件一致
    （那侧早已是三级回退），这里补齐。"""
    api = get_astrbot_data_path
    if callable(api):
        try:
            path = api()
            if path:
                return Path(path)
        except Exception as exc:
            logger.debug(f"{LOG_PREFIX} get_astrbot_data_path() 失败: {exc}")
    getter = getattr(context, "get_plugin_data_dir", None)
    if callable(getter):
        try:
            plugin_dir = getter()
            if plugin_dir:
                candidate = Path(plugin_dir).resolve()
                # get_plugin_data_dir 返回 data/plugin_data/<插件名>；目标是 data/。
                if candidate.parent.name == "plugin_data":
                    return candidate.parent.parent
                return candidate.parent
        except Exception as exc:
            logger.debug(f"{LOG_PREFIX} context.get_plugin_data_dir() 失败: {exc}")
    return Path("data").resolve()

# 「你在干嘛 / 在忙什么」这类问法：只有被问到，才把「她这会儿正在X」写进去。
# 为什么不一贯写：这句话每轮都在，而 X 一挂就是几个小时，模型就每一条都提一句
# 「我在忙」「正在热餐」——用户的原话是「忙就忙呗，人家忙着关我啥事」。
# 不问就不说；问了答得出（`/查看日程` 也照常有）。
_ASK_ACTIVITY = (
    "干嘛", "干啥", "干什么", "做什么", "在忙", "忙什么", "忙啥", "忙吗", "忙不忙",
    "在做什么", "在干什么", "干啥呢", "忙完了", "忙完了吗",
)


def _asked_about_activity(text: str) -> bool:
    body = (text or "").strip()
    return bool(body) and any(word in body for word in _ASK_ACTIVITY)


def _state_note(core: Any, user_id: str, char_name: str = "", user_name: str = "",
                is_group: bool = False, said: str = "") -> str:
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
            _profile = core.mood.profile(user_id) or {}
            affection = float(_profile.get("affection", 0.0) or 0.0)
            # 档位不只看好感：相处够久的人不该被写成「还在互相认识」（见 relstage 顶部）。
            # `first_met` 在没有情绪档案时也可能存在（称呼是故意保留的），所以这里两条
            # 都从同一个 profile 取，取不到就当 0（退回纯好感判定，与改前一致）。
            try:
                _turns = int(_profile.get("turn_count") or 0)
            except (TypeError, ValueError):
                _turns = 0
            try:
                _first = float(_profile.get("first_met") or 0.0)
                _days = max(0.0, (core.now_epoch() - _first) / 86400.0) if _first > 0 else 0.0
            except (TypeError, ValueError, AttributeError):
                _days = 0.0
            stage = relstage.line_for(affection, turns=_turns, days=_days)
            tone = core.mood.current_tone(user_id)
            style = core.mood.style_tendency(user_id)
        day = ""
        try:
            slot = core.schedule.current_slot() or {}
            event = str(slot.get("event", "") or "").strip()
            loc = str(slot.get("location", "") or "").strip()
            if event and _asked_about_activity(said):
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
        # 身体事实**不在这里**：每轮 `core.build_injection()` 返回的「〔她的身体与生活〕」
        # 事实块已经带着 `soma.feelings()` 的档位词（medium 档 2 条、full 档 5 条，
        # 同一套阈值）。v2.28.1 曾在这里再拼一遍，结果是同一句话在一轮请求里出现两次
        # ——现在由 `tests/test_v2281_body_in_chat.py` 盯着「只出现一次、且来自 injection」。
        #
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
        data_root = _resolve_data_root(context)
        data_dir = data_root.joinpath(*DATA_SUBDIR)
        self._migrate_defaults(config, data_dir)
        self._config_box = ConfigBox(config)
        self._config_version = 1

        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        # 输入状态（正在输入）：按会话跑一个高频上报的小任务，纯装饰，出错不影响回复。
        # P2-12：最小上报间隔下限 + 单条回复最大帧数（新配置项由 A 补；未补时用默认值兜底）。
        self._input_state = InputStateNotifier(
            logger,
            interval=getattr(self._config, "input_state_interval_seconds", 0.5),
            timeout=getattr(self._config, "input_state_timeout_seconds", 120.0),
            min_interval=getattr(self._config, "input_state_min_interval_seconds", 0.3),
            max_frames=getattr(self._config, "input_state_max_frames", 60),
        )
        # 记忆联动的桥接对象：短 TTL 缓存 + 失败后限时重试（P2-24）。插件加载顺序不保证，
        # 构造时对面可能还没就绪；对面后加载/热重载之后必须能自愈，不能「解析失败一次就永久失效」。
        self._memory_bridge_obj: Any = None
        self._memory_bridge_checked_at = 0.0
        self._memory_bridge_resolved = False
        # 注入通道（temp_parts/contexts/system_prompt/空）：给 /拟人 诊断 与测试看。
        self._inject_channel = ""
        self._inject_fallback_warned = False
        # 写回侧：每个角色最近一次**已经写过**的过程名，以及最近 24 小时写了几条。
        # 进程内内存即可——重启后第一次只记账不写（见 `_write_finished_process`）。
        self._last_written_process: dict[str, str] = {}
        self._life_write_stamps: dict[str, list[float]] = {}
        # 兄弟 bot 互聊护栏：同平台另一个 bot 可以聊，但同一对角色在窗口内最多连续
        # 放行 3 次；真人一开口清零（见 `humanoid/sibling_chat.py`）。
        self._sibling_guard = SiblingChatGuard()

        state_path = data_dir / "state.json"
        self._state_store = StateStore(state_path, lambda: self._config.state_flush_interval_seconds, logger)

        clock = Clock(lambda: self._config)
        today = clock.today_str()
        self._state_store.load(today, self._config.cycle_length)
        # P2-08：进程内维护「见过的角色 id」（兄弟 bot 识别 + 门面/门禁判断用），
        # 从已加载的状态初始化一次；之后只在新角色实例创建时增长。
        self._known_roles: set[str] = set()
        self._refresh_known_roles()

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

        # 把活着的实例登记到模块级，供 `get_core_facade()` 取用。插件被卸载/重载时
        # `terminate()` 会清掉它，否则别的插件会拿到一个已经停掉的角色管理器。
        # 代次自增：重载时新旧实例可能短暂共存，旧实例的 terminate 不许清掉新实例
        # 的登记（P2-03），门面按代次校验（P1-B）。
        global _ACTIVE_PLUGIN, _FACADE, _PLUGIN_GENERATION
        _PLUGIN_GENERATION += 1
        self._generation = _PLUGIN_GENERATION
        _ACTIVE_PLUGIN = self
        _FACADE = None

        logger.info(f"{LOG_PREFIX} 插件已加载 (v{__version__})")

    @property
    def _config(self) -> HumanoidConfig:
        return self._config_box.value

    @staticmethod
    def _migrate_defaults(raw_config: Any, data_dir: Path) -> None:
        """把仍等于旧默认值的配置项一次性提升

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
        """插件初始化：起落盘循环与各个角色的后台循环（v3.0.0 加失败回滚，P2-02）。

        以前没有回滚：`_state_store.start()` 起了 flush 任务、`_ACTIVE_PLUGIN` 也已在
        `__init__` 登记；任一步失败后插件处于「半死」状态（后台任务泄漏、门面可用但
        角色没起来），框架重试时还可能叠出第二个 flush 循环。现在失败就按启动逆序回滚
        并把异常上抛，让框架看到一个明确的加载失败。
        """
        global _ACTIVE_PLUGIN, _FACADE
        try:
            await self._state_store.start()
            await self.role_manager.start()
            for core in self.role_manager.get_all():
                core.schedule.current_slots()
            self._refresh_known_roles()
        except Exception as exc:
            logger.warning(f"{LOG_PREFIX} 初始化失败，回滚已启动的资源: {exc}", exc_info=True)
            try:
                await self.role_manager.stop()
            except Exception as stop_exc:
                logger.warning(f"{LOG_PREFIX} 回滚角色管理器失败: {stop_exc}")
            try:
                await self._state_store.stop()
            except Exception as stop_exc:
                logger.warning(f"{LOG_PREFIX} 回滚状态落盘失败: {stop_exc}")
            # 门面也必须失效：否则别的插件会拿到一个「上下文已按失败路径停掉」的门面。
            if _ACTIVE_PLUGIN is self:
                _ACTIVE_PLUGIN = None
                _FACADE = None
            raise

    async def terminate(self) -> None:
        global _ACTIVE_PLUGIN, _FACADE
        # 先摘掉门面再停：停的过程中别的插件来问，应该拿到 None 而不是半个死对象。
        # 只摘**自己**那一份登记（P2-03）：重载时新实例可能已经登记，旧实例的
        # terminate 绝不能把新实例清掉。
        if _ACTIVE_PLUGIN is self:
            _ACTIVE_PLUGIN = None
            _FACADE = None
        # P2-01：清理段用 try/finally 保证「状态一定落盘、session 一定关」。
        # 以前任何一步抛错就跳过后续，卸载变成报错事件、这轮状态还可能丢。
        try:
            await self._input_state.stop_all()
        except Exception as exc:
            logger.warning(f"{LOG_PREFIX} 停止输入状态上报失败: {exc}")
        try:
            await self.role_manager.stop()
        except Exception as exc:
            logger.warning(f"{LOG_PREFIX} 停止角色管理器失败: {exc}")
        finally:
            try:
                await self._state_store.stop()
            except Exception as exc:
                logger.warning(f"{LOG_PREFIX} 卸载时状态落盘失败（不影响使用）: {exc}")
            finally:
                session, self._session = self._session, None
                if session is not None and not session.closed:
                    try:
                        await session.close()
                    except Exception as exc:
                        logger.debug(f"{LOG_PREFIX} 关闭 aiohttp session 失败: {exc}")

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
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                response.raise_for_status()
                return await response.json()
        except Exception as exc:
            # aiohttp 的 ClientResponseError.__str__ 里带 real_url（含 appid=<KEY>）。
            # 上浮前统一脱敏，避免调用方的失败日志把天气密钥写进长期日志（P1-2）。
            # 脱敏函数在 services/weather.py（与天气侧共用一份），拿不到时宁可只留异常类型。
            detail = type(exc).__name__
            try:
                from .humanoid.services.weather import redact_secrets

                detail = redact_secrets(f"{type(exc).__name__}: {exc}")
            except Exception:
                pass
            raise RuntimeError(detail) from None

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
        role_id = self._self_id(event)
        core = self.role_manager.get_or_create(role_id)
        # 新角色可能来自一条消息（多 bot 同进程部署的常规路径）：登记进「见过的角色」。
        try:
            self._known_roles.add(str(core.role_id))
        except Exception:
            pass
        return core

    def _refresh_known_roles(self) -> None:
        """从已加载状态重置「见过的角色 id」集合（只在加载/重载后跑一次）。

        P2-08：原实现每条消息都直读 `StateStore.data["roles"]`（绕过 RoleScope 约定、
        且多一个数据访问入口）。角色集合在运行期只增不减（删角色后状态里会残留，
        旧实现同样会读到），所以一次性快照 + 创建时增长与旧语义一致。
        """
        known: set[str] = set()
        try:
            roles = (self._state_store.data or {}).get("roles") or {}
            for rid in roles:
                rid = str(rid or "")
                if rid:
                    known.add(rid)
        except Exception:
            known = set()
        self._known_roles = known

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
        """人形化核心伴侣 · 管理指令（全部需要管理员）

        改配置：参照名称 / 诊断 / 设置 / 重载
        动数据：重置日程 / 日程打断 / 日程继续 / 重置状态 / 重置情绪 / 设置好感度 / 批量好感度 / 查看昵称

        查状态的指令不带前缀，直接用：/你的状态 /亲密状态 /好感度 /情绪详情 /情绪日志
        /查看日程 /时间 /叫我 /拟人帮助
        """

    @filter.command("你的状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看当前角色的完整状态（精力、身体轴、生理、天气、日程、过程、社交能量等）。"""
        core = self._core(event)
        # 群聊里 `status_lines` 会自己隐掉「亲密」段：ccb 在群里是硬闸，
        # 面板也不该有那一屏（用户明确要求群里一句话都不许露）。
        yield event.plain_result("\n".join(
            core.status_lines(self._sender(event), is_group=not _is_private_chat(event))
        ))

    @filter.command("亲密状态")
    async def cmd_intimacy_status(self, event: AstrMessageEvent):
        """查看她对你的亲密状态：亲近欲离门槛多远、在不在场景里、余韵/冷却、能不能进。

        只在私聊里回答：这是「她这会儿想不想」的明细，群里说出来既冒犯也没意义
        （ccb 本身在群里就是硬闸）。
        """
        if not _is_private_chat(event):
            yield event.plain_result("亲密状态只在私聊里看。")
            return
        core = self._core(event)
        if not bool(getattr(self._config, "ccb", False)):
            yield event.plain_result("亲密场景未开启（ccb=false）。")
            return
        yield event.plain_result(core.intimacy_text(self._sender(event)))

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
    @humanoid_group.command("时间")
    async def cmd_set_city(self, event: AstrMessageEvent):
        """`/拟人 时间 大阪`：设置她所在的城市（时区），只改当前这个机器人。

        与 `/时间 城市` 分工：那条谁都能用、只是**查**别处的时间；
        这条是管理员**改**她的城市（决定作息、天气、季节）。
        """
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        value = _arg_after(event.message_str or "", "拟人 时间", "拟人时间").strip()
        core = self._core(event)
        if not value:
            effective = core.clock.city or "（未定）"
            override = str(core.scope.get_self("tz_city_override", "") or "")
            tag = "当前 bot 单独设" if override else "跟随全局"
            yield event.plain_result(
                f"她所在的城市：{effective}（{tag}）\n"
                "用法：/拟人 时间 大阪（只改当前这个机器人）；"
                "写「/拟人 时间 默认」可取消单独设置、回到跟随全局。"
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
            lines.append(f"- 城市：{effective}（{tag}）　→ /拟人 时间 大阪")
        else:
            lines.append(f"- 城市（全局默认）：{cfg.timezone_city or '（未定）'}　→ /拟人 时间 大阪")
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

    def _reload_config(self) -> dict:
        """重载配置：能读盘就从盘上读（P2-11），返回一段可自查的来源信息。

        AstrBot 的插件配置对象是 AstrBotConfig（dict 子类，带 config_path）：面板保存
        改的就是这份内存对象并同步 save_config() 到 config_path，所以正常路径下
        内存与磁盘一致，ConfigBox.reload() 重解析内存 dict 就够了。但**用户直接手改
        配置文件**时内存不会变，回执说「已重载」而改动不生效——这就是 P2-11。这里先从
        config_path 读回磁盘 JSON，再**原地**更新同一个配置对象：

        * 必须原地 clear/update，不能把 raw 换成普通 dict —— 换了之后 /拟人 设置 的
          save_config() 会静默消失（回执说已保存、实际写不进文件）；
        * 读盘失败（文件被占用/JSON 坏/没有 config_path）就退回内存重解析，并在回执里
          说清「面板改动会同步到内存，手改文件请重启/重载插件」，不再假装成功。
        """
        info = {"source": "memory", "path": "", "error": "", "mtime": ""}
        raw = self._config_box.raw
        config_path = getattr(raw, "config_path", None) if raw is not None else None
        if isinstance(config_path, str) and config_path:
            info["path"] = config_path
            try:
                data = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
                if not isinstance(data, dict):
                    raise ValueError("配置文件顶层不是对象")
                if isinstance(raw, dict):
                    raw.clear()
                    raw.update(data)
                info["source"] = "disk"
                try:
                    info["mtime"] = time.strftime(
                        "%Y-%m-%d %H:%M:%S",
                        time.localtime(Path(config_path).stat().st_mtime),
                    )
                except Exception:
                    info["mtime"] = ""
            except FileNotFoundError:
                info["error"] = "配置文件不存在"
            except Exception as exc:
                info["error"] = f"{type(exc).__name__}: {exc}"
        self._config_box.reload()
        return info

    @humanoid_group.command("重载")
    async def cmd_reload(self, event: AstrMessageEvent):
        """热重载插件配置，无需重启。"""
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        info = self._reload_config()
        self._input_state.configure(
            getattr(self._config, "input_state_min_interval_seconds", 0.0),
            getattr(self._config, "input_state_interval_seconds", 0.5),
            getattr(self._config, "input_state_timeout_seconds", 120.0),
            max_frames=getattr(self._config, "input_state_max_frames", 0),
        )
        lines = [
            f"✅ 配置已重载。当前注入档位：{self._config.inject_activity_context}"
        ]
        if info["source"] == "disk":
            detail = f"来源：配置文件 {info['path']}"
            if info["mtime"]:
                detail += f"（修改时间 {info['mtime']}）"
            lines.append(detail + "，已按磁盘内容重新解析。")
        elif info["error"]:
            lines.append(
                "⚠ 读取配置文件失败，本次按内存里的配置重解析："
                f"{info['error']}。"
                "面板保存会同步到内存；如果你手改了磁盘文件，请重启/重载插件再试。"
            )
        else:
            lines.append(
                "（当前配置对象没有 config_path，只能按内存配置重解析：面板改动需重启/重载插件。）"
            )
        if self._config_box.load_error:
            lines.append(f"⚠ 配置解析失败，已整体退回默认值：{self._config_box.load_error}")
        lines.append("自查：/拟人 诊断 顶部会显示配置解析错误；上面一行是本次读取的配置文件。")
        yield event.plain_result("\n".join(lines))

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
    @humanoid_group.command("日程打断")
    async def cmd_schedule_interrupt(self, event: AstrMessageEvent):
        """让她立刻放下手上的事，重新决定现在做什么（换一件，不是继续）。"""
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        if not self._config.use_llm_schedule:
            yield event.plain_result("⚠️ 未启用大模型日程，无法「打断换一件」。")
            return
        core = self._core(event)
        try:
            queued = bool(core.schedule.interrupt_now())
        except Exception as exc:
            logger.warning(f"{LOG_PREFIX} 日程打断失败: {exc}")
            yield event.plain_result(f"❌ 打断失败：{exc}")
            return
        if queued:
            yield event.plain_result("✅ 已让她停下手上的事，正在决定换成什么（稍等几秒再 /查看日程）。")
        else:
            yield event.plain_result(
                "⏳ 这会儿已经有一轮日程生成在跑了，等它落定再看 /查看日程。"
            )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @humanoid_group.command("日程继续")
    async def cmd_schedule_continue(self, event: AstrMessageEvent):
        """让她把手上的事接着做下去（把当前这一段往后延长，不另外生成）。"""
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        core = self._core(event)
        try:
            extended = core.schedule.continue_now()
        except Exception as exc:
            logger.warning(f"{LOG_PREFIX} 日程继续失败: {exc}")
            yield event.plain_result(f"❌ 继续失败：{exc}")
            return
        if not extended:
            yield event.plain_result("❌ 这会儿没有正在做的事可以接着做（或已到当日上限）。")
            return
        activity = core.schedule.current_activity()
        what = activity.get("name", "")
        until = activity.get("expected_end", "")
        detail = f"：{what}（到 {until}）" if what else ""
        yield event.plain_result(f"✅ 让她接着做下去了{detail}。")

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

    # 桥接对象的缓存时长：解析成功用 5 分钟；失败/没装 60 秒后重试（P2-24）。
    # 关键是对面**后加载或热重载**之后能自愈：以前解析失败一次就永久负缓存，
    # 记忆联动要到下次重载本插件才可能恢复。
    MEMORY_BRIDGE_TTL_SECONDS = 300.0
    MEMORY_BRIDGE_RETRY_SECONDS = 60.0

    def _memory_timeout(self, default: float) -> float:
        """记忆桥接的等待上限；配置项还没加（或对象是测试替身）时用模块默认值。

        跨插件等待必须有界（P1-6），所以这里不因为「读配置失败」就退回无限等待。
        """
        try:
            value = getattr(self._config, "memory_bridge_timeout_seconds", default)
            return float(value)
        except Exception:
            return float(default)

    def _memory_bridge(self):
        """拿 memory_companion 的桥接对象；没装 / 没就绪返回 None。

        带 TTL 的懒解析：插件加载顺序不保证，构造时对面可能还没就绪；对面之后才加载、
        或热重载换了新的桥接实例，这里到点会重新问一次宿主注册表，能自己接上。
        只查宿主注册表，不 importlib——没装时 `import_module` 会凭空造一个空模块
        （「没装却说装了」）。

        先解析、成功后再更新缓存时间戳：旧实现先把 resolved 置真再解析，解析中途抛异常
        就变成「已解析且为 None」，永久失效。
        """
        now = time.monotonic()
        cached = getattr(self, "_memory_bridge_obj", None)
        ttl = (
            self.MEMORY_BRIDGE_TTL_SECONDS
            if cached is not None
            else self.MEMORY_BRIDGE_RETRY_SECONDS
        )
        try:
            checked_at = float(self._memory_bridge_checked_at)
        except (AttributeError, TypeError, ValueError):
            # 兼容用 __new__ 直接构造的测试替身/旧对象：第一次被问到就当刚检查过并补上时间戳。
            checked_at = now
            self._memory_bridge_checked_at = now
        if getattr(self, "_memory_bridge_resolved", False) and (now - checked_at) < ttl:
            return cached
        bridge = None
        try:
            bridge = memory_link.find_memory_bridge(getattr(self, "context", None))
        except Exception as exc:  # 注册表 API 自身坏掉时也只是「这轮没有记忆」
            logger.debug(f"{LOG_PREFIX} 解析记忆桥接失败: {exc}")
            bridge = None
        self._memory_bridge_resolved = True
        self._memory_bridge_checked_at = now
        if bridge is not None and bridge is not self._memory_bridge_obj:
            logger.info(f"{LOG_PREFIX} 已接上长期记忆桥接（memory_companion）")
        self._memory_bridge_obj = bridge
        return bridge

    async def _memory_note(self, umo: str, user_id: str, said: str, is_group: bool) -> str:
        """按 TA 这条消息去记忆库要几条相关长期记忆；拿不到返回空串。

        只读。拿不到时静默降级——没有长期记忆也该照常回话，不能因为对面没装就少说话。
        """
        bridge = self._memory_bridge()
        if bridge is None:
            return ""
        try:
            return await memory_link.fetch_memory_text(
                bridge,
                umo,
                user_id,
                query=said,
                is_group=is_group,
                timeout=self._memory_timeout(memory_link.READ_TIMEOUT_SECONDS),
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
                # 写回也限时：它被注入链路 await，对面卡住不能连带把回复挂住。
                timeout=self._memory_timeout(memory_link.WRITE_TIMEOUT_SECONDS),
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
            role_id = self._self_id(event)
            core = self._core(event)
            user_id = self._sender(event)
            text = (getattr(event, "message_str", "") or "").strip()
            # 兄弟 bot 的发言：**可以聊，但防互刷**。
            #
            # 以前这里无条件 stop_event，同机另一台 bot 永远得不到回复（看起来像装死）；
            # 完全放开又会两台机器你一句我一句刷到天亮。所以按
            # `SiblingChatGuard` 的额度判：放行 → 不 stop，让主链路正常回它；
            # 额度用完 → stop_event 挡下本轮 LLM（光 return 挡不住）。
            #
            # **只在真正会触发回复的这个钩子里判**（另一个 on_llm_request 是输入状态，
            # 不碰它），并按 event 缓存结论，同一条消息被工具循环重放时不重复吃额度。
            if user_id and user_id in self._known_role_ids(exclude=role_id):
                if not self._sibling_chat_allowed(role_id, user_id, event):
                    stopper = getattr(event, "stop_event", None)
                    if callable(stopper):
                        stopper()
                    logger.debug(
                        f"{LOG_PREFIX} 兄弟 bot {user_id} 互聊额度用完，挡下这一轮"
                    )
                    return
                logger.debug(f"{LOG_PREFIX} 兄弟 bot {user_id} 互聊放行")
            said = text
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
                # 「此刻的她」与身体事实优先走临时内容块：只发给模型、不落会话历史。
                # 老框架没有这个 API 时退到 contexts/system_prompt（P1-1），不再静默丢注入。
                state_note = _state_note(
                    core, user_id, char_name, user_name, is_group=is_group, said=said
                )
                channel = _append_injected_parts(
                    req, [block for block in (state_note, injection) if block]
                )
                self._inject_channel = channel
                if not channel:
                    logger.warning(
                        f"{LOG_PREFIX} 本轮注入没有可用通道（既无临时块也无 contexts/system_prompt），"
                        "事实未进入上下文"
                    )
                elif channel != "temp_parts" and not self._inject_fallback_warned:
                    # 只提示一次：每轮回执会刷屏，但这说明宿主的 AstrBot 版本偏低。
                    self._inject_fallback_warned = True
                    logger.warning(
                        f"{LOG_PREFIX} 当前 AstrBot 不支持临时内容块（mark_as_temp），"
                        f"注入已降级为 {channel} 通道；建议升级 AstrBot 到 4.24+"
                    )

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
            core = self._core(event)
            user_id = self._sender(event)
            if not core.mood.readable(user_id, is_group, enabled=cfg.mood_enabled,
                                      in_group=cfg.mood_enabled_in_group):
                return
            core.mood.note_spoke(user_id, text)
            # P1：她**自己说出口**的话也进 ccb 的账。原来只有用户那一路
            # （`core.on_message`），她先开口或她在场景里说的，一条都不算。
            core.note_her_words(user_id, text, is_group=is_group)
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
            core = self._core(event)
            user_id = self._sender(event)
            if not core.mood.readable(user_id, is_group, enabled=cfg.mood_enabled,
                                      in_group=cfg.mood_enabled_in_group):
                return
            # note_spoke 带 90 秒同文去重：on_llm_response 已经记过时这一步是空操作。
            core.mood.note_spoke(user_id, text)
            # P1：与 on_llm_response 同一条口径。note_occurrence 自带 60 秒去重，
            # 两条路径都走到时只算一次。
            core.note_her_words(user_id, text, is_group=is_group)
        except Exception as e:
            logger.debug(f"{LOG_PREFIX} 发送前记录她的发言失败: {e}")

    # -------------------- 兄弟 bot 识别（多 bot 同进程部署） --------------------

    def _known_role_ids(self, exclude: str = "") -> set:
        """本进程里已经见过的其它角色 id（多 bot 同进程部署时的「兄弟」）。

        它们发出来的消息在事件层看起来就是普通用户——另一台 bot 的自发消息、
        被平台回灌过来时，sender 是那台 bot 的 id，不等于本台，所以「跳过自己发的」
        那道闸挡不住。不认出来的话，本台会把兄弟 bot 当成一个陌生用户去回话，
        情绪与身体账上也多出一个人（多 bot 串台）。
        只认本进程见过的 id；跨进程的 bot 认不出，README 里写清了边界。

        v3.0.0（P2-08）：改读进程内维护的 `_known_roles` 集合，不再每条消息直读
        `StateStore.data["roles"]`——那是绕过 RoleScope 的第二个数据访问入口，
        生产 6 个角色每次消息都要构造一遍列表。集合在加载/重载时从状态重建、
        新角色实例创建时增长（见 `_refresh_known_roles` / `_core`）。
        """
        skip = {"default", str(exclude or "")}
        return {rid for rid in self._known_roles if rid and rid not in skip}

    def _sibling_chat_allowed(
        self, role_id: str, peer_id: str, event: AstrMessageEvent
    ) -> bool:
        """这条兄弟 bot 的发言放不放行；同一个 event 只判一次（结论缓存在 extra 上）。

        为什么缓存：`on_llm_request` 在工具循环/重试里会对同一条消息反复触发。每触发
        一次就 `allow()` 一次的话，3 次额度会被**一条**消息吃光（第 2 轮就被自己挡下），
        而实际只发生了一轮对话，额度就白扣了。

        读缓存。取不到 / 框架没有 extra 通道时退回现算——宁可多判一次，也不能不判。
        """
        reader = getattr(event, "get_extra", None)
        if callable(reader):
            try:
                cached = reader(_SIBLING_DECISION_KEY)
            except Exception:
                cached = None
            if cached is not None:
                return bool(cached)
        allowed = self._sibling_guard.allow(role_id, peer_id, time.time())
        writer = getattr(event, "set_extra", None)
        if callable(writer):
            try:
                writer(_SIBLING_DECISION_KEY, allowed)
            except Exception:
                pass
        return allowed


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
            # 兄弟 bot 的消息不是「用户说了什么」：不记账、也不该改她的情绪/身体。
            # 多台 bot 同进程部署时，另一台 bot 的群发言在事件层就是普通成员，
            # sender 不等于本台，前面那道「跳过自己」挡不住。只认状态里见过的角色 id。
            #
            # 注意分工：**回复与否**由 `inject_context` 里的 `_sibling_chat_allowed`
            # 决定（放行时主链路照常回它）；这里只做记账隔离，不让 bot 的发言进
            # 情绪/身体账本——她会因为另一个 bot 的话改心情，那才是真串台。
            if user_id and user_id in self._known_role_ids(exclude=role_id):
                return
            # 真人一开口，机器对机器那条链就该结束：清掉本角色名下所有 peer 的互聊额度。
            # 放在空文本/指令早退之前：一条「嗯」或一条指令也是人回来了。
            self._sibling_guard.note_human(role_id)
            # 任何一条真实用户消息都算「有人在用」：松开空闲静默，让日程重新跟上。
            # 放在后续所有早退之前，否则「被过滤的环境」会把静默一直吊着。
            self.call_gate.note_interaction()
            is_group = not _is_private_chat(event)
            if not self.engine.environment_allows(not is_group):
                return
            core = self._core(event)
            # 自动认定称呼：只在这个人还没有任何称呼时填一次，之后换名片也不会改口；
            # 用户 /叫我 设过的更碰不到。纯图片消息也带名字，所以放在正文判空之前。
            if self._config.auto_nickname:
                core.mood.auto_nickname(user_id, _sender_name(event))
            # 跨插件联动要知道「TA 叫什么」：`auto_nickname` 是**认定称呼**，名字里
            # 带数字/emoji/装饰的名片过不了校验，于是 Core 什么都留不下，主动消息
            # 那边就永远不知道怎么叫人。这里另存一份「名片名」，只用于称呼参考，
            # 不参与身份认定、也不进聊天注入。
            sender_name = _sender_name(event)
            if sender_name:
                try:
                    core.mood.note_display_name(user_id, sender_name)
                except Exception:
                    pass
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
            core.on_message(
                user_id, text, is_group=is_group, umo=umo, is_command=is_command,
                user_name=sender_name,
            )
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 消息记账失败: {e}")