"""情绪服务 - 使用 RoleScope 版本。"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections.abc import Callable
from datetime import datetime
from dataclasses import dataclass
from typing import Any, Optional

from ..config import HumanoidConfig
from ..data.mood_map import generate_mood_tag, get_mood_label, mood_complex
from ..jsonx import extract_json_object
from ..llm import PURPOSE_MOOD, LLMGateway
from ..recall import MAX_SNIPPETS, note_said, recall_lines
from ..wording import pick
from ..role_scope import RoleScope

NEGATIVE_PATTERN = re.compile(
    r"(傻|蠢|笨|白痴|废物|垃圾|去死|死吧|滚蛋|操|妈|逼|贱|恶心|讨厌|恨|烦|骂|吵|滚|弱智|脑残|sb|煞笔)",
    re.IGNORECASE,
)
POSITIVE_PATTERN = re.compile(r"(爱|喜欢|好|棒|厉害|赞|开心|谢谢|感谢|乖|可爱|聪明)", re.IGNORECASE)

AFFECTION_RANGE = (0.0, 100.0)
LIBIDO_RANGE = (0.0, 50.0)
AGGRESSION_RANGE = (0.0, 50.0)
DIMENSIONS = (
    ("affection", "base_affection", AFFECTION_RANGE),
    ("libido", "base_libido", LIBIDO_RANGE),
    ("aggression", "base_aggression", AGGRESSION_RANGE),
)

# 长期不活跃用户会被丢掉的历史字段。nickname/nickname_src 故意保留：那是用户自己让 bot
# 怎么称呼自己（或是自动认定后不再改口的依据），丢了会当场改变说话方式；last_interaction
# 也保留，它是下次判定过期的依据。
# mood 删掉即可：profile() 会用配置里的初始值重建，正是文档承诺的「好感度清回初始值」。
RETENTION_DROPPED_FIELDS = ("mood", "mood_logs", "mood_tag", "last_message", "said")

# 自动认定称呼的硬性门槛：来源是消息带的名字（群聊是群名片、私聊是 QQ 昵称），
# 都是用户随手可改的东西——宁缺勿滥：叫错人比不称呼难看得多，认不上就一个都不认。
_AUTO_NICK_REJECT_EXACT = {
    "n/a", "na", "null", "none", "unknown", "user", "匿名", "未设置",
    "未命名", "默认", "默认昵称", "新朋友", "新用户", "该用户", "名字", "游客",
    "abc", "abcd", "test", "qwq", "orz", "emmm", "aaa", "qq", "wx", "lol",
    "xx", "sb", "nb", "cv", "你好", "在吗", "哈哈",
}
# 名字里出现这些词的基本是身份/角色标注或系统占位（已注销用户、XX管理员、测试号），不是拿来叫人的
_AUTO_NICK_REJECT_SUBSTR = (
    "注销", "未命名", "默认", "管理员", "机器人", "群主", "成员",
    "通知", "公告", "用户", "客服", "助手", "游客", "匿名", "名字", "重置", "昵称", "测试",
)
# 只放汉字与基本拉丁字母加少量名字里真会用的连接符：数字、下划线、emoji、括号装饰
# （【】（）★彡这类）一律进不来，带数字的名片也就进不来。
_AUTO_NICK_ALLOWED = re.compile(r"[A-Za-z\u4e00-\u9fff\u00b7\u30fb\-\u2010-\u2015.．]+")
_AUTO_NICK_HAS_LETTER = re.compile(r"[A-Za-z\u4e00-\u9fff]")
_AUTO_NICK_LATIN_ONLY = re.compile(r"[A-Za-z]+")
_AUTO_NICK_EDGE_PUNCT = "·・-‐‑‒–—．."


def validate_auto_nickname(candidate: str) -> str:
    """自动认定的名字校验：过得了全部规则才返回名字，否则返回空（不认定）。

    宁可一个都不认：单字不足以确认身份；含任何数字/下划线/emoji/装饰括号的名片不认；
    纯字母名至少 3 位（AB 这种缩写不算名）；首尾挂标点的是昵称装饰不是名字；
    身份/角色词与颜文字直接拒。"""
    name = (candidate or "").strip()
    if not (2 <= len(name) <= 12):
        return ""
    if any(ch.isspace() for ch in name):
        return ""
    if name.lower() in _AUTO_NICK_REJECT_EXACT:
        return ""
    if any(word in name for word in _AUTO_NICK_REJECT_SUBSTR):
        return ""
    if not _AUTO_NICK_HAS_LETTER.search(name):
        return ""
    if not _AUTO_NICK_ALLOWED.fullmatch(name):
        return ""
    if _AUTO_NICK_LATIN_ONLY.fullmatch(name) and len(name) < 3:
        return ""  # 两个拉丁字母多半是缩写/占位，不当名字
    if name[0] in _AUTO_NICK_EDGE_PUNCT or name[-1] in _AUTO_NICK_EDGE_PUNCT:
        return ""
    if "--" in name or ".." in name:
        return ""
    return name


@dataclass(frozen=True, slots=True)
class Delta:
    affection: float
    libido: float
    aggression: float

    def scaled(self, factor: float) -> "Delta":
        return Delta(self.affection * factor, self.libido * factor, self.aggression * factor)

    def capped(self, cap: float) -> "Delta":
        return Delta(
            max(-cap, min(cap, self.affection)),
            max(-cap, min(cap, self.libido)),
            max(-cap, min(cap, self.aggression)),
        )

    def blend(self, other: "Delta", weight: float) -> "Delta":
        weight = max(0.0, min(1.0, weight))
        return Delta(
            self.affection * (1 - weight) + other.affection * weight,
            self.libido * (1 - weight) + other.libido * weight,
            self.aggression * (1 - weight) + other.aggression * weight,
        )

def local_delta(text: str) -> Delta:
    if NEGATIVE_PATTERN.search(text):
        return Delta(random.uniform(-4, -2), random.uniform(-2, -1), random.uniform(2, 4))
    if POSITIVE_PATTERN.search(text):
        return Delta(random.uniform(1, 3), random.uniform(0.5, 2), random.uniform(-1, -0.5))
    return Delta(random.uniform(-0.5, 0.5), random.uniform(-0.3, 0.3), random.uniform(-0.3, 0.3))


class MoodService:
    def __init__(
        self,
        scope: RoleScope,
        config_provider: Callable[[], HumanoidConfig],
        clock=None,
        spawn_fn=None,
        time_source: Callable[[], float] = time.time,
        gateway: LLMGateway | None = None,
        logger=None,
    ):
        self._scope = scope
        self._config = config_provider
        self._clock = clock
        self._spawn = spawn_fn
        self._time = time_source
        self._gateway = gateway
        self._log = logger
        # 按用户细粒度锁，避免不同用户互相阻塞
        self._user_locks: dict[str, asyncio.Lock] = {}

    def _get_user_lock(self, user_id: str) -> asyncio.Lock:
        if user_id not in self._user_locks:
            self._user_locks[user_id] = asyncio.Lock()
        return self._user_locks[user_id]

    @property
    def config(self) -> HumanoidConfig:
        return self._config()

    def profile(self, user_id: str) -> dict[str, Any]:
        user_state = self._scope.user_state(user_id)
        record = user_state.get("mood")
        if isinstance(record, dict):
            return self._repair(record, user_state)

        cfg = self.config
        affection = float(cfg.mood_initial_affection)
        override = cfg.affection_override_for(user_id)
        if override is not None:
            affection = override
        record = {
            "affection": affection,
            "libido": float(cfg.mood_initial_libido),
            "aggression": float(cfg.mood_initial_aggression),
            "base_affection": affection,
            "base_libido": float(cfg.mood_initial_libido),
            "base_aggression": float(cfg.mood_initial_aggression),
            "first_met": self._time(),
            "last_interaction": self._time(),
            "last_decay": self._time(),
            "turn_count": 0,
            "messages_since_llm": 0,
        }
        user_state["mood"] = record
        self._scope.mark_dirty()
        return record

    def _repair(self, record: dict, user_state: dict) -> dict:
        changed = False
        for key, base_key, bounds in DIMENSIONS:
            for target in (key, base_key):
                try:
                    value = float(record.get(target, bounds[0]))
                except (TypeError, ValueError):
                    value = bounds[0]
                clamped = max(bounds[0], min(bounds[1], value))
                if record.get(target) != clamped:
                    record[target] = clamped
                    changed = True
        for field in ("last_decay", "messages_since_llm", "turn_count"):
            if field not in record:
                record[field] = 0 if field == "turn_count" else self._time()
                changed = True
        # 关系时长靠 first_met：老档案没有时用 last_interaction 补（不丢历史，只是少算一段）。
        if "first_met" not in record:
            record["first_met"] = record.get("last_interaction", self._time())
            changed = True
        if changed:
            user_state["mood"] = record
            self._scope.mark_dirty()
        return record

    def label(self, user_id: str) -> str:
        data = self.profile(user_id)
        return get_mood_label(data["affection"], data["libido"], data["aggression"])

    def tag(self, user_id: str) -> str:
        return self._scope.get_user(user_id, "mood_tag", "")

    def said(self, user_id: str) -> list:
        """隔一段时间采样下来的对方原话。"""
        return self._scope.get_user(user_id, "said") or []

    def note_said(self, user_id: str, text: str, now: float | None = None) -> list:
        """按采样规则记一句对方原话。没变化时不写盘。"""
        fresh = note_said(
            self.said(user_id), text, self._time() if now is None else now, limit=MAX_SNIPPETS
        )
        if fresh == self.said(user_id):
            return fresh
        self._scope.set_user(user_id, "said", fresh)
        return fresh

    def recall_lines(self, user_id: str, max_items: int = 2) -> list[str]:
        """给注入用的一行：TA 之前说过什么。没记过就是空。"""
        return recall_lines(self.said(user_id), self._time(), max_items=max_items)

    def nickname(self, user_id: str) -> str:
        return self._scope.get_user(user_id, "nickname", "")

    def first_met(self, user_id: str) -> float:
        """第一次互动的时间。老档案由 _repair 用 last_interaction 补，不会缺。"""
        record = self.profile(user_id)
        try:
            return float(record.get("first_met", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def last_emotional_event(self, user_id: str) -> tuple[str, float] | None:
        """最近一次情绪大波动，转成无数字的事实句 + 距今小时数。

        logs 本来就只在波动超过阈值时才写，所以任何一条都是大波动。24 小时外的
        不算「今天发生过的事」。拿不到方向或维度时返回 None，不编一个。
        """
        entries = self.logs(user_id, limit=1)
        if not entries:
            return None
        entry = entries[0]
        try:
            parsed = datetime.strptime(
                str(entry.get("time", "")), "%Y-%m-%d %H:%M:%S"
            )
        except (TypeError, ValueError):
            return None
        # 日志写的是她那个城市的墙上时间（见 _log_event），读回来必须附同一个时区：
        # naive datetime 直接 .timestamp() 会按宿主机时区解释，城市与机器不在一个
        # 时区时「今天/刚才/昨天」整体偏移几个小时。
        if parsed.tzinfo is None and self._clock is not None:
            try:
                zone = self._clock.now().tzinfo
            except Exception:
                zone = None
            if zone is not None:
                parsed = parsed.replace(tzinfo=zone)
        at = parsed.timestamp()
        age_hours = (self._time() - at) / 3600.0
        if age_hours < 0 or age_hours > 24.0:
            return None
        event = str(entry.get("event", ""))
        up = "上升" in event
        if "攻击性" in event:
            if up:
                sentence = pick(
                    "emo_ev_angry_up",
                    [user_id, entry.get("time", "")],
                    ("今天TA惹她不痛快了", "今天被TA气到了"),
                )
            else:
                sentence = "今天气消了些"
        elif "好感度" in event:
            if up:
                sentence = pick(
                    "emo_ev_aff_up",
                    [user_id, entry.get("time", "")],
                    ("今天TA让她挺开心", "今天心情被TA弄好了"),
                )
            else:
                sentence = "今天心里对TA凉了些"
        else:
            return None
        if age_hours < 1.0:
            sentence = "刚才" + sentence[2:]
        elif age_hours > 20.0:
            sentence = "昨天" + sentence[2:]
        return sentence, age_hours

    def set_nickname(self, user_id: str, nickname: str, src: str = "user") -> str:
        self._scope.set_user(user_id, "nickname", nickname)
        self._scope.set_user(user_id, "nickname_src", src)
        return nickname

    def nickname_source(self, user_id: str) -> str:
        """称呼的来路：user=用户自己设的，auto=自动认定的，""=还没有称呼。

        自动认定上线之前的称呼只可能来自 /叫我，没记来源的老数据按 user 算。"""
        if not self._scope.get_user(user_id, "nickname", ""):
            return ""
        src = str(self._scope.get_user(user_id, "nickname_src", "") or "")
        return src if src in ("user", "auto") else "user"

    def auto_nickname(self, user_id: str, candidate: str) -> str:
        """自动认定称呼：只在「还没有任何称呼」时填一次，认过之后永不改动。

        已有称呼（用户 /叫我 设的、或早先自动认定的）一律不碰；候选名过不了
        validate_auto_nickname 的高门槛也不认。返回真正认定的名字，没认定返回空。"""
        if self._scope.get_user(user_id, "nickname", ""):
            return ""
        name = validate_auto_nickname(candidate)
        if not name:
            return ""
        return self.set_nickname(user_id, name, src="auto")

    def all_nicknames(self) -> dict[str, tuple[str, str]]:
        """所有有称呼的用户：uid -> (称呼, 来源)。来源 user=自己设的，auto=自动认定的。"""
        result = {}
        for uid in self._scope.all_user_ids():
            name = self._scope.get_user(uid, "nickname")
            if name:
                result[uid] = (name, self.nickname_source(uid))
        return result

    def decay_user(self, user_id: str) -> bool:
        cfg = self.config
        if not cfg.mood_enabled:
            return False
        user_state = self._scope.user_state(user_id)
        record = user_state.get("mood")
        if not record:
            return False
        return self._decay_record(record, user_state, cfg, self._time())

    def decay_all(self) -> int:
        cfg = self.config
        if not cfg.mood_enabled:
            return 0
        now = self._time()
        changed = 0
        for uid in self._scope.all_user_ids():
            user_state = self._scope.user_state(uid)
            record = user_state.get("mood")
            if record and self._decay_record(record, user_state, cfg, now):
                changed += 1
        return changed

    def prune_expired(self, now: float | None = None) -> int:
        """清理超过 mood_data_retention_days 没说话的用户的历史情绪数据。

        返回被清理的用户数。0 或负数的 retention 表示永不清理。
        没有可判定时间戳的记录一律跳过，宁可不清也不误删。
        """
        cfg = self.config
        try:
            days = int(cfg.mood_data_retention_days)
        except (TypeError, ValueError):
            return 0
        if days <= 0:
            return 0

        now = self._time() if now is None else float(now)
        cutoff = now - days * 86400.0
        pruned = 0
        for uid in self._scope.all_user_ids():
            user_state = self._scope.user_state(uid)
            if not any(key in user_state for key in RETENTION_DROPPED_FIELDS):
                continue
            stamp = user_state.get("last_interaction")
            if stamp is None:
                mood = user_state.get("mood")
                stamp = mood.get("last_interaction") if isinstance(mood, dict) else None
            try:
                stamp = float(stamp)
            except (TypeError, ValueError):
                continue
            if stamp > cutoff:
                continue
            for key in RETENTION_DROPPED_FIELDS:
                user_state.pop(key, None)
            pruned += 1
        if pruned:
            self._scope.mark_dirty()
        return pruned

    def _decay_record(self, record: dict, user_state: dict, cfg: HumanoidConfig, now: float) -> bool:
        try:
            last = float(record.get("last_decay", now))
        except (TypeError, ValueError):
            last = now
        elapsed_hours = (now - last) / 3600.0
        if elapsed_hours < 0.1:
            return False

        duration = max(0.5, float(cfg.mood_decay_hours))
        ratio = 1.0 if elapsed_hours >= duration else (elapsed_hours / duration) ** 2
        changed = False
        for key, base_key, bounds in DIMENSIONS:
            current = float(record.get(key, bounds[0]))
            base = float(record.get(base_key, bounds[0]))
            deviation = current - base
            if abs(deviation) < 1e-3:
                continue
            updated = max(bounds[0], min(bounds[1], current - deviation * ratio))
            if abs(updated - current) > 1e-4:
                record[key] = updated
                changed = True
        if changed:
            record["last_decay"] = now
            user_state["mood"] = record
            self._scope.mark_dirty()
        return changed

    async def update_from_message(self, user_id: str, text: str, *args, **kwargs) -> Optional[Delta]:
        return await self.update_from_message_async(user_id, text)

    async def update_from_message_async(self, user_id: str, text: str) -> Optional[Delta]:
        cfg = self.config
        if not cfg.mood_enabled or not text:
            return None

        base_delta = self._local_delta(text)

        # 使用用户级锁代替全局锁
        lock = self._get_user_lock(user_id)
        async with lock:
            user_state = self._scope.user_state(user_id)
            record = user_state.get("mood")
            if not record:
                record = self.profile(user_id)
                user_state = self._scope.user_state(user_id)

            messages_since = int(record.get("messages_since_llm", 0)) + 1
            record["messages_since_llm"] = messages_since
            interval = max(1, cfg.mood_llm_interval_messages)
            should_call_llm = cfg.mood_use_llm_for_delta and messages_since >= interval
            if should_call_llm:
                record["messages_since_llm"] = 0
            self._scope.mark_dirty()
            if cfg.mood_verbose_log and self._log is not None:
                self._log.debug(
                    f"[humanoid_core] 情绪计数 {user_id}: {messages_since}/{interval}"
                    f"{'，本轮调用模型' if should_call_llm else ''}"
                )

        llm_delta = None
        if should_call_llm:
            llm_delta = await self._llm_delta(user_id, text)

        async with lock:
            user_state = self._scope.user_state(user_id)
            record = user_state.get("mood")
            if not record:
                return None

            delta = self._resolve_delta(base_delta, llm_delta)
            delta = self._apply_modifiers(delta, user_id)
            self._commit(user_id, record, user_state, delta)
            return delta

    def _local_delta(self, text: str) -> Delta:
        return local_delta(text)

    def _resolve_delta(self, base: Delta, llm: Delta | None) -> Delta:
        if llm is None:
            return base
        if base.affection < -1.5:
            return base.scaled(1.2)
        return base.blend(llm, 0.3)

    def _apply_modifiers(self, delta: Delta, user_id: str) -> Delta:
        cfg = self.config
        record = self.profile(user_id)
        factor = cfg.mood_sensitivity / 100.0
        delta = delta.scaled(factor)

        def adjust(value: float) -> float:
            if energy > 70 and value > 0:
                return value * 1.3
            if energy < 40:
                return value * 0.8
            return value

        energy = self._scope.get_self("energy", 80)
        delta = Delta(adjust(delta.affection), adjust(delta.libido), adjust(delta.aggression))

        cycle_day = self._scope.get_self("current_cycle_day", 1)
        phase = cfg.cycle_phase_index(cycle_day)
        if phase == 0:
            delta = Delta(
                delta.affection * (0.5 if delta.affection > 0 else 1.5),
                delta.libido * (0.5 if delta.libido > 0 else 1.5),
                delta.aggression * (0.5 if delta.aggression > 0 else 1.5),
            )
        elif phase == 2:
            delta = Delta(
                delta.affection * (1.4 if delta.affection > 0 else 1.0),
                delta.libido * (1.4 if delta.libido > 0 else 1.0),
                delta.aggression * (1.4 if delta.aggression > 0 else 1.0),
            )

        return delta.capped(float(cfg.mood_affection_delta_cap))

    def _commit(self, user_id: str, record: dict, user_state: dict, delta: Delta):
        before = {k: float(record[k]) for k, _, _ in DIMENSIONS}
        values = {
            "affection": delta.affection,
            "libido": delta.libido,
            "aggression": delta.aggression,
        }
        for key, _, bounds in DIMENSIONS:
            record[key] = max(bounds[0], min(bounds[1], before[key] + values.get(key, 0)))

        turn = int(record.get("turn_count", 0)) + 1
        base_coef = 1.0 if turn <= 10 else 0.2
        for key, base_key, bounds in DIMENSIONS:
            drift = values.get(key, 0) * base_coef * 0.5
            record[base_key] = max(bounds[0], min(bounds[1], float(record[base_key]) + drift))

        record["turn_count"] = turn
        record["last_interaction"] = self._time()
        user_state["mood"] = record
        self._scope.mark_dirty()
        self._refresh_tag(user_id, record)
        self._log_event(user_id, before, record)

    def _refresh_tag(self, user_id: str, record: dict) -> None:
        """刷新心情标签（如「有点疲惫，开心」）。

        v2.11.x 起 README 一直在宣传这个标签，但 mood_tag 只有读没有写，实际永远为空。
        """
        cfg = self.config
        if not cfg.mood_tag_enabled:
            return
        tag = generate_mood_tag(
            float(record["affection"]),
            float(record["libido"]),
            float(record["aggression"]),
            float(self._scope.get_self("energy", 80.0)),
        )
        if self._scope.get_user(user_id, "mood_tag") != tag:
            self._scope.set_user(user_id, "mood_tag", tag)

    async def _llm_delta(self, user_id: str, text: str) -> Delta | None:
        if self._gateway is None:
            return None
        cfg = self.config
        prompt = (
            "你是情绪变化分析器。只分析用户这条消息对角色的即时影响。\n"
            "返回严格 JSON，不要 Markdown："
            '{"affection_delta": 0, "libido_delta": 0, "aggression_delta": 0}.\n'
            "数值范围：affection -10~10，libido -5~5，aggression -5~5。\n"
            f"用户消息：{text[:500]}"
        )
        if cfg.debug_mode and self._log:
            self._log.debug(f"[humanoid_core] 情绪分析请求: {prompt}")

        result = await self._gateway.generate(
            prompt=prompt,
            chain=cfg.mood_provider_ids,
            allow_global=cfg.schedule_allow_global_fallback,
            timeout=float(cfg.mood_update_timeout),
            attempts_per_provider=1,
            purpose=PURPOSE_MOOD,
        )
        if not result.ok:
            if self._log is not None:
                self._log.warning(f"[humanoid_core] 情绪分析失败：{result.summary()}")
            return None
        data = extract_json_object(result.text)
        if not data:
            if self._log is not None:
                self._log.warning("[humanoid_core] 情绪分析失败：模型返回不是有效 JSON")
            return None
        try:
            delta = Delta(
                float(data.get("affection_delta", 0)),
                float(data.get("libido_delta", 0)),
                float(data.get("aggression_delta", 0)),
            ).capped(10.0)
            if cfg.debug_mode and self._log:
                self._log.debug(f"[humanoid_core] 情绪分析结果: {delta}")
            return delta
        except (TypeError, ValueError):
            if self._log is not None:
                self._log.warning("[humanoid_core] 情绪分析失败：JSON 数值无效")
            return None

    def _log_event(self, user_id: str, before: dict, record: dict):
        cfg = self.config
        if not cfg.mood_log_enabled:
            return
        events = []
        for key, (threshold, name) in {
            "affection": (cfg.mood_log_threshold_affection, "好感度"),
            "libido": (cfg.mood_log_threshold_libido, "亲近欲"),
            "aggression": (cfg.mood_log_threshold_aggression, "攻击性"),
        }.items():
            change = float(record[key]) - before.get(key, 0)
            if abs(change) >= max(0.0, float(threshold)):
                events.append(f"{name}{'上升' if change > 0 else '下降'}至 {record[key]:.1f}")
        if not events:
            return

        logs = self._scope.user_state(user_id).setdefault("mood_logs", [])
        logs.append({
            "time": self._clock.now().strftime("%Y-%m-%d %H:%M:%S") if self._clock else datetime.now().isoformat(),
            "event": "，".join(events),
            "affection": round(float(record["affection"]), 1),
            "libido": round(float(record["libido"]), 1),
            "aggression": round(float(record["aggression"]), 1),
        })
        limit = max(1, cfg.mood_log_max_entries)
        if len(logs) > limit:
            # 必须写回 user_state：只重绑局部名字的话上限从不生效，日志会无限增长。
            del logs[:-limit]
        self._scope.mark_dirty()

    def reset(self, user_id: str) -> dict:
        cfg = self.config
        record = self.profile(user_id)
        values = {
            "affection": float(cfg.mood_initial_affection),
            "libido": float(cfg.mood_initial_libido),
            "aggression": float(cfg.mood_initial_aggression),
        }
        for key in values:
            record[key] = values[key]
            record[f"base_{key}"] = values[key]
        record["turn_count"] = 0
        record["last_interaction"] = self._time()
        record["last_decay"] = self._time()
        record["messages_since_llm"] = 0
        self._scope.user_state(user_id)["mood"] = record
        self._scope.mark_dirty()
        return record

    def set_affection(self, user_id: str, value: float) -> float:
        clamped = max(0.0, min(100.0, value))
        record = self.profile(user_id)
        record["affection"] = clamped
        record["base_affection"] = clamped
        self._scope.user_state(user_id)["mood"] = record
        self._scope.mark_dirty()
        return clamped

    def set_affection_batch(self, pairs: list[tuple[str, float]]) -> int:
        applied = 0
        for uid, value in pairs:
            if 0 <= value <= 100:
                self.set_affection(uid, value)
                applied += 1
        return applied

    def logs(self, user_id: str, limit: int = 0) -> list[dict[str, Any]]:
        """情绪波动记录（原始条目）。limit > 0 时只取最近若干条。"""
        entries = self._scope.user_state(user_id).get("mood_logs", [])
        if not isinstance(entries, list):
            return []
        return entries[-limit:] if limit > 0 else list(entries)

    def logs_text(self, user_id: str, limit: int = 10) -> str:
        logs = self._scope.user_state(user_id).get("mood_logs", [])
        if not logs:
            return "📭 暂无情绪波动记录。"
        entries = logs[-limit:] if limit > 0 else logs
        lines = [f"📋 情绪波动记录（最近{len(entries)}条）：", "——————————————"]
        for entry in entries:
            lines.append(f"{entry.get('time', '')} | {entry.get('event', '')}")
        return "\n".join(lines)

    def profile_text(self, user_id: str, detailed: bool = False) -> str:
        data = self.profile(user_id)
        title = "〖情绪详细档案〗" if detailed else "〖情绪档案〗"
        lines = [title]
        if detailed:
            lines.append(f"好感度：{data['affection']:.1f}/100（基线 {data['base_affection']:.1f}）")
        else:
            lines.append(f"好感度：{data['affection']:.1f}/100")
        lines += [
            f"亲近欲：{data['libido']:.1f}/50（基线 {data['base_libido']:.1f}）",
            f"攻击性：{data['aggression']:.1f}/50（基线 {data['base_aggression']:.1f}）",
            f"当前标签：{self.label(user_id)}",
        ]
        if detailed:
            lines.append(f"交互轮次：{int(data.get('turn_count', 0))}")
        tag = self.tag(user_id)
        if tag:
            lines.append(f"心情标签：{tag}")
        return "\n".join(lines)