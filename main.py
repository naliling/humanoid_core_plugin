"""人形化伴侣插件 —— AstrBot 适配层。

这个文件只做三件事：注册指令与钩子、把 AstrBot 的事件对象解包成普通参数、
把 HTTP 能力注入给业务层。所有业务逻辑都在 `humanoid/` 子包里。

为什么指令必须留在本文件：AstrBot 注册 handler 时记的是 `handler.__module__`
（`astrbot/core/star/register/star_handler.py:57`），派发时用它去查 `star_map`
（`astrbot/core/pipeline/process_stage/method/star_request.py:40-43`），
查不到会直接报错。所以被 `@filter` 装饰的方法不能挪到子模块里。
"""

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
from .humanoid.engine import LOG_PREFIX, HumanoidEngine

DATA_SUBDIR = ("plugin_data", "humanoid_core")

HELP_TEXT = f"""📖 人形化伴侣插件 指令列表 (v{__version__})

/你的状态 - 查看精力、生理、天气、日程、情绪、社交能量
/查看日程 - 查看今日完整日程
/时间 城市 - 查看指定城市当前时间
/叫我 昵称 - 设置 AI 对你的称呼
/好感度 - 查看情绪档案（好感度/亲近欲/攻击性）
/情绪详情 - 查看详细情绪档案（含基线、轮次）
/情绪日志 - 查看情绪波动记录
/拟人帮助 - 显示本帮助

管理员指令：
/拟人诊断 - 排查模型选择问题（可用 id、实际选中、失败原因、冷却状态）
/重置日程 - 立即重新生成今日日程（绕过失败冷却）
/重置状态 - 重置精力、社交能量与生理周期
/重置情绪 - 重置自己的情绪至初始值
/设置好感度 数值 - 手动设置好感度（0-100）
/批量好感度 QQ:数值,QQ:数值 - 批量导入好感度
/查看所有昵称 - 查看所有用户设置的昵称
/重载配置 - 重载插件配置"""

NO_PERMISSION = "❌ 权限不足，该指令仅管理员可用。"


def _arg_after(text: str, command: str) -> str:
    """取指令名之后的参数部分。`/叫我 小明` 与 `叫我小明` 都能取到「小明」。"""
    raw = (text or "").strip()
    index = raw.find(command)
    if index == -1:
        return raw.lstrip("/").strip()
    return raw[index + len(command) :].strip()


def _is_private_chat(event: AstrMessageEvent) -> bool:
    """AstrMessageEvent 有 is_private_chat() / get_group_id()，没有 is_private()。"""
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
        self.config = config
        data_dir = Path(get_astrbot_data_path()).joinpath(*DATA_SUBDIR)
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        self.engine = HumanoidEngine(context, config, data_dir, logger, self._fetch_json)
        logger.info(f"{LOG_PREFIX} 插件已加载 (v{__version__})")

    # ---------- 生命周期 ----------

    async def initialize(self) -> None:
        await self.engine.start()

    async def terminate(self) -> None:
        """插件被禁用 / 重载时的清理入口。

        AstrBot 的生命周期钩子名是 `terminate()`（`astrbot/core/star/base.py:84`，
        由 `star_manager.py:1909` 调用），不是 `cleanup()`。名字写错的话这里不会被
        调用，每次重载都会遗留一个后台任务和一个未关闭的 HTTP 会话。
        """
        await self.engine.stop()
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

    async def cleanup(self) -> None:
        """兼容别名：万一有旧版本框架调用它。"""
        await self.terminate()

    # ---------- HTTP ----------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        # 双重检查加锁：并发首次调用若不加锁会创建两个 session 并泄漏一个
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

    # ---------- 辅助 ----------

    def _sender(self, event: AstrMessageEvent) -> str:
        return str(event.get_sender_id())

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """AstrBot 自身的管理员，或插件 `admin_qq` 里列出的 QQ，都算管理员。"""
        astrbot_admin = False
        checker = getattr(event, "is_admin", None)
        if callable(checker):
            try:
                astrbot_admin = bool(checker())
            except Exception:
                astrbot_admin = False
        return self.engine.is_admin(self._sender(event), astrbot_admin)

    def _umo(self, event: AstrMessageEvent) -> str | None:
        try:
            return str(event.unified_msg_origin)
        except Exception:
            return None

    # ======================== 用户指令 ========================

    @filter.command("你的状态")
    async def cmd_status(self, event: AstrMessageEvent):
        yield event.plain_result("\n".join(self.engine.status_lines(self._sender(event))))

    @filter.command("好感度")
    async def cmd_mood(self, event: AstrMessageEvent):
        if not self.engine.config.mood_enabled:
            yield event.plain_result("情绪系统未开启。")
            return
        yield event.plain_result(self.engine.mood_profile_text(self._sender(event)))

    @filter.command("情绪详情")
    async def cmd_mood_detail(self, event: AstrMessageEvent):
        if not self.engine.config.mood_enabled:
            yield event.plain_result("情绪系统未开启。")
            return
        yield event.plain_result(self.engine.mood_profile_text(self._sender(event), detailed=True))

    @filter.command("情绪日志")
    async def cmd_mood_log(self, event: AstrMessageEvent):
        if not self.engine.config.mood_log_enabled:
            yield event.plain_result("❌ 情绪日志未启用。")
            return
        yield event.plain_result(self.engine.mood_log_text(self._sender(event)))

    @filter.command("查看日程")
    async def cmd_view_schedule(self, event: AstrMessageEvent):
        yield event.plain_result(self.engine.schedule_text())

    @filter.command("时间")
    async def cmd_time(self, event: AstrMessageEvent):
        city = _arg_after(event.message_str, "时间") or self.engine.config.timezone_city
        if not city:
            yield event.plain_result("请指定城市名，或在配置中设置默认时区城市。")
            return
        text = self.engine.city_time_text(city)
        if text is None:
            yield event.plain_result(f"暂不支持 {city}，目前支持中国、俄罗斯、日本的主要城市。")
        else:
            yield event.plain_result(text)

    @filter.command("叫我")
    async def cmd_set_nickname(self, event: AstrMessageEvent):
        nickname = _arg_after(event.message_str, "叫我")
        if not nickname:
            yield event.plain_result("用法：/叫我 昵称")
            return
        if len(nickname) > 32:
            yield event.plain_result("昵称太长了，请控制在 32 个字符以内。")
            return
        self.engine.set_nickname(self._sender(event), nickname)
        yield event.plain_result(f"✅ 记住了，以后叫你：{nickname}")

    @filter.command("拟人帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(HELP_TEXT)

    # ======================== 管理员指令 ========================

    @filter.command("拟人诊断")
    async def cmd_diagnose(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        yield event.plain_result(self.engine.diagnostics_text())

    @filter.command("重载配置")
    async def cmd_reload(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        cfg = self.engine.reload_config(self.config)
        yield event.plain_result(
            f"✅ 配置已重载（第 {self.engine.config_version} 版）。"
            f"当前注入档位：{cfg.inject_activity_context}，"
            f"日程模型：{cfg.schedule_provider_name or '未指定'}"
        )

    @filter.command("重置日程")
    async def cmd_reset_schedule(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        if not self.engine.config.use_llm_schedule:
            yield event.plain_result("⚠️ 当前未启用大模型日程（use_llm_schedule=false），使用的是内置模板。")
            return
        yield event.plain_result("⏳ 已开始在后台重新生成今日日程，完成后 /查看日程 即可看到。")
        changed = await self.engine.reset_schedule()
        status = self.engine.schedule.status()
        if changed:
            await event.send(
                event.plain_result(
                    f"✅ 日程已更新：共 {status['slots']} 个时段（{status['source_text']}）。"
                )
            )
        else:
            await event.send(
                event.plain_result(
                    "❌ 日程生成失败，仍在使用内置模板。\n"
                    f"原因：{status.get('last_error') or status.get('last_attempt') or '未知'}\n"
                    "可用 /拟人诊断 查看完整链路。"
                )
            )

    @filter.command("重置状态")
    async def cmd_reset_state(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        energy, social, cycle_day = self.engine.reset_state()
        yield event.plain_result(
            f"✅ 已重置状态：精力 {int(energy)}，社交能量 {int(social)}，生理周期第 {cycle_day} 天。"
        )

    @filter.command("重置情绪")
    async def cmd_reset_mood(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        record = self.engine.mood.reset(self._sender(event))
        yield event.plain_result(
            f"✅ 已重置情绪：好感度 {record['affection']:.0f}，"
            f"亲近欲 {record['libido']:.0f}，攻击性 {record['aggression']:.0f}。"
        )

    @filter.command("设置好感度")
    async def cmd_set_affection(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        match = re.search(r"(\d+(?:\.\d+)?)", _arg_after(event.message_str, "设置好感度"))
        if not match:
            yield event.plain_result("用法：/设置好感度 数值 (0-100)")
            return
        value = float(match.group(1))
        if not 0 <= value <= 100:
            yield event.plain_result("数值必须在 0-100 之间。")
            return
        applied = self.engine.mood.set_affection(self._sender(event), value)
        yield event.plain_result(f"✅ 好感度已设为 {applied:.0f}")

    @filter.command("批量好感度")
    async def cmd_batch_affection(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        pairs = self.engine.parse_affection_batch(_arg_after(event.message_str, "批量好感度"))
        if not pairs:
            yield event.plain_result("格式错误，请使用：/批量好感度 QQ号:数值, QQ号:数值")
            return
        applied = self.engine.mood.set_affection_batch(pairs)
        skipped = len(pairs) - applied
        text = f"✅ 已批量设置 {applied} 个用户的好感度。"
        if skipped:
            text += f"（{skipped} 条数值超出 0-100 已跳过）"
        yield event.plain_result(text)

    @filter.command("查看所有昵称")
    async def cmd_list_nicknames(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result(NO_PERMISSION)
            return
        names = self.engine.all_nicknames()
        if not names:
            yield event.plain_result("📭 当前没有任何用户设置昵称。")
            return
        lines = ["📋 所有用户昵称列表：", "——————————————"]
        lines += [f"{qq} → {name}" for qq, name in names.items()]
        yield event.plain_result("\n".join(lines))

    # ======================== 钩子 ========================

    @filter.on_llm_request()
    async def inject_context(self, event: AstrMessageEvent, req: ProviderRequest):
        """把状态注入 system_prompt。

        AstrBot 内联 await 这个钩子且没有超时保护
        （`astrbot/core/pipeline/context_utils.py:98`），钩子里花掉的时间会一比一
        变成用户等回复的时间。所以这里**不允许**出现任何可能长时间阻塞的 await：
        `build_injection()` 是纯同步的，日程生成一律走后台任务。
        """
        try:
            is_private = _is_private_chat(event)
            if not self.engine.environment_allows(is_private):
                return
            injection = self.engine.build_injection(
                self._sender(event), is_group=not is_private
            )
        except Exception as exc:
            logger.warning(f"{LOG_PREFIX} 构建注入内容失败，本次跳过注入: {exc!r}")
            return

        if req.system_prompt:
            req.system_prompt = f"{req.system_prompt}\n{injection}"
        else:
            req.system_prompt = injection

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """每条消息的记账。同步部分立即完成，情绪分析（可能调模型）丢后台。"""
        try:
            is_private = _is_private_chat(event)
            if not self.engine.environment_allows(is_private):
                return
            sender = self._sender(event)
            if sender == str(event.get_self_id()):
                return
            text = (getattr(event, "message_str", "") or "").strip()
            if not text:
                return
            self.engine.on_message(sender, is_group=not is_private)

            # 私聊始终允许情绪分析；群聊取决于 mood_enabled_in_group
            if self.engine.mood_allowed(not is_private):
                self.engine.spawn_mood_update(sender, text, self._umo(event))
        except Exception as exc:
            logger.warning(f"{LOG_PREFIX} 消息记账失败: {exc!r}")
