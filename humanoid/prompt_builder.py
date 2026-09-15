"""把她此刻的处境交给模型：只给事实，不给台词，也不给规矩。

v2.16 起这个模块只输出**客观事实**，一条指导句都没有：

    【时间】2026-09-15 星期二 21:40 晚上，你在上海
    【称呼】你管TA叫小鱼
    【刚刚】距上次说话 3 小时 12 分；TA上一句说的是「我猫今天吐了」

身体（困意、饥饿、睡眠债）、情绪三轴、"这一轮该说多长"这些**都不进上下文**。它们留在
state.json 里驱动她的一天，并通过联动契约交给社交层。原因很直接：把「她此刻的感受」写成
句子塞进去，模型只会照抄，而且会当成必须执行的任务——那就不是参考，是插件替她说话、
替她决定回复多长。她困不困、要不要顶回去、说几句，是模型从「凌晨两点」「她刚跑完一整天」
这些事实里自己幻想出来的。

三档 `inject_activity_context` 只决定**给多少生活事实**：

* `low`（默认）：时间 + 称呼 + 刚刚（隔了多久没说话）
* `full`：再加 今天（日程里此刻在做什么、刚做过、接下来）、TA说过、天气
* `mood_only`：只给称呼与一个关系标签词
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .core_instance import HumanoidCoreInstance

from .config import HumanoidConfig
from .data.mood_map import get_mood_label
from .services.schedule import day_lines, day_phrases

_CJK_RANGES = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")

# 注入块的硬长度上限（字符）。v2.16 把指导句全删了，剩下的都是事实，上限跟着收紧：
# 注入每条聊天请求都追加一次，它越小越不像在给她下任务。
INJECT_MAX_CHARS = {"low": 220, "full": 620, "mood_only": 60}

# 「今天」与「记得」各给多少。这两块只在 full 档出现。
DAY_ITEMS_FULL = 3
TOPICS_FULL = 3


def estimate_tokens(text: str) -> int:
    """估算 token 数：中日韩一字算一 token，其余按 3.5 字符一 token。

    与自主拟人社交侧同口径；没装分词器时宁可高估也不能低估预算。
    """
    if not text:
        return 0
    cjk = len(_CJK_RANGES.findall(text))
    return int(math.ceil(cjk + (len(text) - cjk) / 3.5))


FIRST_CONTACT_LINE = "这是你和TA的第一次对话"


def humanize_gap(seconds: float) -> str:
    """把秒数说成一句时长：12 分钟、3 小时 12 分、2 天 4 小时。"""
    total = max(0, int(round(float(seconds))))
    minutes, hours, days = total // 60, total // 3600, total // 86400
    if hours >= 24:
        rest_h = (minutes - days * 1440) // 60
        return f"{days} 天{f' {rest_h} 小时' if rest_h else ''}"
    if hours:
        return f"{hours} 小时 {minutes % 60} 分" if minutes % 60 else f"{hours} 小时"
    if minutes:
        return f"{minutes} 分钟"
    return "不到一分钟"


class PromptBuilder:
    def __init__(self, core_instance: "HumanoidCoreInstance") -> None:
        self._core = core_instance

    @property
    def config(self) -> HumanoidConfig:
        return self._core.config

    # ------------------------------------------------------------------

    def build(
        self,
        user_id: str,
        is_group: bool = False,
        events: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        cfg = self.config
        events = events or []
        mode = cfg.inject_activity_context

        if mode == "mood_only":
            parts = [self._block("你们", self._relation_lines(user_id, is_group))]
            return self._finish("\n".join(p for p in parts if p), cfg)

        snap = self._core.snapshot(refresh=False)
        parts: List[str] = [self._block("时间", self._time_lines(snap, is_group))]

        who = self._nickname_line(user_id, is_group)
        if who:
            parts.append(self._block("称呼", [who]))

        situ = self._behavior_lines(
            events, with_previous=cfg.last_interaction_mode == "with_last_msg"
        )
        if situ:
            parts.append(self._block("刚刚", situ))

        if mode == "full":
            day = self._block("今天", self._day_lines())
            if day:
                parts.append(day)
            memory = self._block("TA说过", self._memory_lines(user_id))
            if memory:
                parts.append(memory)
            weather = self._block("天气", self._weather_lines(snap))
            if weather:
                parts.append(weather)

        return self._finish("\n".join(p for p in parts if p), cfg)

    # ------------------------------------------------------------------
    # 各块：全部是事实
    # ------------------------------------------------------------------

    def _block(self, title: str, lines: List[str]) -> str:
        lines = [line for line in lines if line]
        if not lines:
            return ""
        return f"【{title}】" + "；".join(lines) + "。"

    def _time_lines(self, snap: Dict[str, Any], is_group: bool) -> List[str]:
        cfg = self.config
        now = self._core.clock.now()
        line = f"{snap['today']} 星期{snap['weekday']} {now.strftime('%H:%M')} {self._time_of_day(now.hour)}"
        holiday = ""
        try:
            holiday = self._core.clock.holiday(now)
        except Exception:
            holiday = ""
        if holiday:
            line += f"，{holiday}"
        lines = [line]
        if cfg.show_city_time_in_low_intrusion:
            # 只地名，不带 UTC±HH:MM：偏移量是给人看的东西，塞进上下文只会让她说话像仪表。
            lines.append(f"你在{snap['city']}")
        if cfg.enable_chat_awareness:
            lines.append("这是群聊" if is_group else "这是私聊")
        return lines

    def _weather_lines(self, snap: Dict[str, Any]) -> List[str]:
        """天气只留一句能用的；没配好时直接不注入，而不是把配置说明书念给模型听。"""
        env = str((snap.get("weather") or {}).get("env", "")).strip()
        if not env:
            return []
        if any(word in env for word in ("未填", "未开启", "获取中", "没配天气")):
            return []
        return [env.replace("当前城市", "这边")[:26]]

    def _relation_lines(self, user_id: str, is_group: bool) -> List[str]:
        """`mood_only` 档：只给一个关系标签词，不给数值、不给态度句、不给语气提示。"""
        cfg = self.config
        core = self._core
        if not (cfg.mood_enabled and (not is_group or cfg.mood_enabled_in_group)):
            return []
        lines: List[str] = []
        try:
            data = core.mood.profile(user_id)
            lines.append(f"对TA的感觉：{get_mood_label(data['affection'], data['libido'], data['aggression'])}")
        except Exception:
            return []
        nickname = self._nickname_line(user_id, is_group)
        if nickname:
            lines.append(nickname)
        return lines

    def _nickname_line(self, user_id: str, is_group: bool) -> str:
        cfg = self.config
        if not (cfg.mood_enabled and (not is_group or cfg.mood_enabled_in_group)):
            return ""
        nickname = self._core.mood.nickname(user_id)
        return f"你管TA叫{nickname}" if nickname else ""

    def _day_lines(self) -> List[str]:
        """今天到这会她干了什么。只从日程算，不额外调任何东西。"""
        core = self._core
        try:
            now = core.clock.now()
            phrases = day_phrases(
                core.schedule.current_slots(),
                now.hour * 60 + now.minute,
                past_limit=DAY_ITEMS_FULL,
                future_limit=1,
            )
        except Exception:
            return []
        return day_lines(phrases, DAY_ITEMS_FULL)

    def _memory_lines(self, user_id: str) -> List[str]:
        """TA 之前说过什么。没记过就不注入，而不是编一个。"""
        try:
            return self._core.mood.recall_lines(user_id, TOPICS_FULL)
        except Exception:
            return []

    def _behavior_lines(
        self, events: List[Dict[str, Any]], with_previous: bool
    ) -> List[str]:
        """间隔就报准确时长。旧版报的是「对方隔了约2~6小时重新出现」这类档位话术，
        那是把一件客观事描述成一种情境。"""
        if not events:
            return []
        top = events[0]
        data = top.get("data") or {}
        lines: List[str] = []
        seconds = data.get("gap_seconds")
        if seconds is None:
            lines.append(FIRST_CONTACT_LINE)
        else:
            lines.append(f"距上次说话 {humanize_gap(float(seconds))}")
        previous = data.get("previous_message") if with_previous else None
        if previous:
            previous = str(previous).replace("\n", " ").strip()[:60]
            if previous:
                lines.append(f"TA上一句说的是「{previous}」")
        return lines

    @staticmethod
    def _time_of_day(hour: int) -> str:
        if 5 <= hour < 8:
            return "清晨"
        if 8 <= hour < 12:
            return "上午"
        if 12 <= hour < 14:
            return "中午"
        if 14 <= hour < 18:
            return "下午"
        if 18 <= hour < 21:
            return "傍晚"
        if 21 <= hour < 24:
            return "晚上"
        return "深夜"

    def _finish(self, text: str, cfg: HumanoidConfig) -> str:
        if not text:
            return ""
        limit = INJECT_MAX_CHARS.get(cfg.inject_activity_context, 220)
        if len(text) > limit:
            # 按块切而不是按字硬截：截到半句上模型会自己把半句补下去。
            kept: List[str] = []
            used = 0
            for line in text.split("\n"):
                cost = len(line) + 1
                if used + cost > limit:
                    break
                kept.append(line)
                used += cost
            text = "\n".join(kept)
        return text
