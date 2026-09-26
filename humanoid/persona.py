"""读取 AstrBot 内置人格设定（人设 / Persona），用于日程生成。

日程是「她这个人的一天」，所以它应该由她真正在用的那份人格决定，而不是由插件里一个
写死的性格标签决定——插件里放全局人格配置项在多角色下必然错配：AstrBot 的
unified_msg_origin 格式是 ``platform_id:message_type:session_id``
（core/platform/message_session.py），第一段就是平台适配器实例，而配置画像（含
persona_id）按 ``platform_id::`` 路由到具体角色（core/umop_config_router.py）。
所以按角色记账过的会话来源解析人格，天然就是按角色隔离的。

解析链与 AstrBot 正常聊天完全一致：会话上指定的人格 → 该会话配置画像的默认人格 →
全局默认人格。``[%None]`` 是「这个会话显式不用任何人格」的哨兵，此时返回空人格，
不回退到默认——含义相反。

只使用 Context 上公开的 persona_manager / conversation_manager 属性。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# 人设原文注入上限。日程只需要它的身份与生活方式，全文塞进去会顶掉别的内容；
# 按 1200 字算，中文一字一 token，日程 prompt 总量仍在千余 token 量级。
PERSONA_PROMPT_MAX = 1200

# AstrBot 里表示「这个会话显式不使用任何人格」的哨兵值
NO_PERSONA_MARKER = "[%None]"

NO_PERSONA = "该会话显式不用人格"
FALLBACK_SOURCE = "未取到人设"


@dataclass(frozen=True, slots=True)
class Persona:
    """一份生效的人格设定。三个字段都为空表示「没有可用的人设」。"""

    name: str = ""
    prompt: str = ""
    source: str = FALLBACK_SOURCE

    @property
    def usable(self) -> bool:
        return bool(self.prompt.strip())

    @property
    def label(self) -> str:
        return self.name or self.source


EMPTY = Persona()


def truncate(text: str, limit: int = PERSONA_PROMPT_MAX) -> str:
    """按行边界截断人设，避免把一句话切成半截。"""
    body = str(text or "").strip()
    if len(body) <= limit:
        return body
    cut = body[:limit]
    idx = cut.rfind("\n")
    if idx >= limit // 2:
        cut = cut[:idx]
    return cut.rstrip() + "\n（以上是完整人设的开头部分。）"


def _field(persona: Any, key: str) -> str:
    try:
        if isinstance(persona, dict):
            return str(persona.get(key) or "").strip()
        return str(getattr(persona, key, "") or "").strip()
    except Exception:
        return ""


def _to_persona(persona: Any, source: str) -> Persona:
    if not persona:
        return EMPTY
    name = _field(persona, "name")
    prompt = truncate(_field(persona, "prompt"))
    if not prompt:
        # 人设存在但正文为空：给个名字用于展示，日程仍按「没有人设」来排。
        return Persona(name=name, prompt="", source=source if name else FALLBACK_SOURCE)
    return Persona(name=name or "（未命名人设）", prompt=prompt, source=source)


async def _conversation_persona_id(context: Any, umo: str) -> Optional[str]:
    """取该会话当前对话上设定的人格 id；取不到返回 None。"""
    conv_mgr = getattr(context, "conversation_manager", None)
    if conv_mgr is None or not umo:
        return None
    try:
        cid = await conv_mgr.get_curr_conversation_id(umo)
        if not cid:
            return None
        conv = await conv_mgr.get_conversation(umo, cid)
        return getattr(conv, "persona_id", None) if conv else None
    except Exception:
        return None


async def resolve_persona(context: Any, umo: str = "") -> Persona:
    """解析该会话来源上真正生效的人格。拿不到时返回空 Persona 而不抛错。"""
    mgr = getattr(context, "persona_manager", None)
    if mgr is None:
        return EMPTY

    conv_persona_id = await _conversation_persona_id(context, umo)
    resolver = getattr(mgr, "resolve_selected_persona", None)
    if resolver is not None:
        try:
            resolved = await resolver(
                umo=umo or "::",
                conversation_persona_id=conv_persona_id,
                platform_name="",
                provider_settings=None,
            )
        except Exception:
            resolved = None
        # 返回 (persona_id, persona, force_applied_id, use_webchat_default)
        if isinstance(resolved, tuple) and len(resolved) >= 2:
            if resolved[0] == NO_PERSONA_MARKER:
                return Persona(name="", prompt="", source=NO_PERSONA)
            if resolved[1]:
                return _to_persona(resolved[1], "会话生效人格")

    default_getter = getattr(mgr, "get_default_persona_v3", None)
    if default_getter is not None:
        try:
            return _to_persona(await default_getter(umo or None), "默认人格")
        except Exception:
            pass

    return _to_persona(getattr(mgr, "selected_default_persona_v3", None), "默认人格")


# 人设可能在面板里被改，缓存太久会让改完要等到明天才生效；日程一天只生成 1~2 次，
# 10 分钟的缓存既挡掉重复解析又足够跟上手改完重跑一次 /重置日程。
PERSONA_CACHE_SECONDS = 600.0


@dataclass
class _Entry:
    persona: Persona = field(default_factory=lambda: EMPTY)
    at: float = 0.0
    umo: str = ""


class PersonaSource:
    """按角色解析并缓存生效人格。

    代表会话来源（umo）由消息记账进来：一个角色可能同时在好几个群里说话，
    日程需要的是「她这个人是谁」，取最近互动过的那个会话上生效的人格就够。
    角色刚创建、还没收到过任何消息时 umo 为空，`resolve_persona` 会落到全局默认人格。
    """

    def __init__(
        self,
        context: Any,
        logger: Any = None,
        time_source: Callable[[], float] = time.time,
        ttl: float = PERSONA_CACHE_SECONDS,
    ) -> None:
        self._context = context
        self._log = logger
        self._time = time_source
        self._ttl = ttl
        self._umos: dict[str, str] = {}
        self._cache: dict[str, _Entry] = {}

    def restore(self, role_id: str, umo: Any) -> None:
        """从持久状态里恢复代表会话（重启后第一条消息之前也能用人格生成日程）。"""
        text = str(umo or "").strip()
        if text and not self._umos.get(str(role_id)):
            self._umos[str(role_id)] = text

    def note_umo(self, role_id: str, umo: Any) -> bool:
        """记下该角色最近互动的会话来源。返回是否发生了变化（变了要重解人格）。"""
        text = str(umo or "").strip()
        key = str(role_id)
        if not text or self._umos.get(key) == text:
            return False
        self._umos[key] = text
        self._cache.pop(key, None)
        return True

    def umo_for(self, role_id: str) -> str:
        return self._umos.get(str(role_id), "")

    async def persona(self, role_id: str) -> Persona:
        key = str(role_id)
        umo = self._umos.get(key, "")
        entry = self._cache.get(key)
        if entry and entry.umo == umo and self._time() - entry.at < self._ttl:
            return entry.persona
        try:
            persona = await resolve_persona(self._context, umo)
        except Exception as exc:  # 解析失败不影响生成日程，按「没有人设」排
            if self._log is not None:
                self._log.warning(f"[humanoid_core] 人格设定读取失败，日程按无设 persona 排: {exc}")
            persona = EMPTY
        self._cache[key] = _Entry(persona=persona, at=self._time(), umo=umo)
        return persona

    def invalidate(self, role_id: str = "") -> None:
        if role_id:
            self._cache.pop(str(role_id), None)
        else:
            self._cache.clear()
