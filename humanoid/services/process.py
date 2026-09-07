"""过程服务：管理单个角色的生活过程与过程内阶段变化。

设计原则：
1. “当前过程”是一个持续中的生活主题，不是持续做同一件事。
2. 一个过程内部会自然经历多个阶段/小动作，例如整理资料 → 处理重点 → 喝水/走动 → 收尾。
3. 过程信息只是背景上下文，不构成对模型发言、话题或用户行为的限制。
4. 过程更新不依赖用户消息；Core 启动后会定期检查，因此长过程也会有内部变化。
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta
from typing import Any

from ..config import HumanoidConfig
from ..role_scope import RoleScope
from ..slots import Slot


# 每个大类不再只有几个固定动作，而是由“过程模板 + 阶段”组成。
# name 是一个较长的生活过程，phases 是过程中自然会发生的小阶段。
PROCESS_TEMPLATES: dict[str, list[dict[str, Any]]] = {
    "学习": [
        {"name": "看一会儿资料", "style": "专注", "phases": ["先快速浏览", "挑出重点内容", "认真读一段", "停下来想想", "整理几个要点"]},
        {"name": "整理学习笔记", "style": "整理", "phases": ["翻看之前的笔记", "补上遗漏内容", "重新归类重点", "发现一处需要修改的地方", "把页面整理干净"]},
        {"name": "研究一个小问题", "style": "探索", "phases": ["先确认问题在哪里", "查找相关资料", "对照几个不同说法", "自己推一遍", "记下暂时的结论"]},
        {"name": "慢慢复习", "style": "轻量", "phases": ["从熟悉的内容开始", "回忆几个关键点", "遇到卡住的地方", "换个角度再看", "简单回顾一遍"]},
        {"name": "处理一小块练习", "style": "间歇", "phases": ["先做简单的", "认真处理一道", "起来活动一下", "回来检查答案", "把剩下的收个尾"]},
    ],
    "工作": [
        {"name": "处理手头的工作", "style": "专注", "phases": ["确认手上的事项", "先处理最紧要的一项", "集中整理内容", "停下来喝口水", "检查并收尾"]},
        {"name": "整理工作资料", "style": "整理", "phases": ["把零散资料集中起来", "筛掉暂时不用的内容", "重新分类", "顺手整理文件名", "确认没有漏掉重要内容"]},
        {"name": "推进一个小项目", "style": "推进", "phases": ["看看目前进度", "处理一个具体问题", "记下新的想法", "短暂放松一下", "继续完成剩余部分"]},
        {"name": "做一些例行事务", "style": "日常", "phases": ["先处理简单事项", "回复需要处理的信息", "核对几项内容", "走开活动一会儿", "把桌面上的事情清掉"]},
        {"name": "安静做一段整理工作", "style": "低刺激", "phases": ["慢慢进入状态", "连续处理几项小事", "稍微走神一下", "重新集中注意力", "做最后检查"]},
    ],
    "休息": [
        {"name": "安静放松一会儿", "style": "安静", "phases": ["坐下来缓一缓", "随便听点东西", "发一会儿呆", "起来活动活动", "继续悠闲待着"]},
        {"name": "随意打发时间", "style": "随性", "phases": ["随便看看", "被一个东西吸引注意", "换个内容", "短暂走神", "继续慢悠悠地待着"]},
        {"name": "给自己留一段空闲时间", "style": "留白", "phases": ["暂时不安排事情", "看看周围", "喝点水", "坐着想点零碎的事情", "慢慢恢复精神"]},
        {"name": "听点喜欢的东西", "style": "沉浸", "phases": ["选个内容", "安静听一会儿", "中途切换一下", "顺手做点别的", "继续听到告一段落"]},
    ],
    "社交": [
        {"name": "和人聊一会儿", "style": "交流", "phases": ["先随便聊聊", "聊到一个有趣的话题", "说着说着想到别的事", "停下来看看消息", "继续把话题聊完"]},
        {"name": "和朋友待一阵子", "style": "轻松", "phases": ["先一起坐着", "聊最近发生的事", "顺便吃点东西", "被一个话题逗笑", "慢慢准备结束这段时间"]},
        {"name": "一起做点轻松的事", "style": "陪伴", "phases": ["决定做什么", "开始一起做", "中途换个小活动", "聊几句无关紧要的话", "把这段时间自然收尾"]},
    ],
    "运动": [
        {"name": "做一段轻运动", "style": "渐进", "phases": ["先热身", "保持一会儿节奏", "稍微喘口气", "补点水", "慢慢放松下来"]},
        {"name": "出去走走", "style": "散步", "phases": ["穿好东西出门", "慢慢走一段", "看看沿路的东西", "停下来休息一下", "再走一小段回去"]},
        {"name": "做一套简单拉伸", "style": "舒缓", "phases": ["先活动肩颈", "慢慢拉伸", "换几个动作", "放松一下", "做最后几组舒缓动作"]},
    ],
    "家务": [
        {"name": "收拾一下生活空间", "style": "整理", "phases": ["先处理最乱的地方", "把东西归位", "擦一小块地方", "发现一个顺手能解决的小问题", "把最后的杂物收好"]},
        {"name": "做一轮日常家务", "style": "日常", "phases": ["准备要用的东西", "先做最简单的一项", "处理下一项", "中途歇一下", "检查还有没有遗漏"]},
        {"name": "整理自己的小角落", "style": "细致", "phases": ["看看哪些东西需要整理", "分类摆放", "清理细碎杂物", "重新调整摆放位置", "满意地收尾"]},
        {"name": "准备一点吃的", "style": "生活", "phases": ["看看有什么材料", "准备食材或餐具", "慢慢处理食物", "收拾用过的东西", "准备坐下来吃"]},
    ],
    "创作": [
        {"name": "做一点创作", "style": "自由", "phases": ["先随便找灵感", "开始做第一版", "发现一个有意思的方向", "停下来看看整体", "继续补几个细节"]},
        {"name": "画点东西", "style": "沉浸", "phases": ["先画大概轮廓", "慢慢补细节", "换个角度看画面", "修改一个不满意的地方", "把画面整理完整"]},
        {"name": "写一段东西", "style": "文字", "phases": ["先想一个开头", "写下一小段", "卡住时停一会儿", "换个表达方式", "把这一段收好"]},
        {"name": "折腾一个小想法", "style": "实验", "phases": ["先试一个最简单的方案", "发现一个小问题", "换一种方法", "顺手记下结果", "决定先做到这里"]},
    ],
    "睡眠": [
        {"name": "准备进入休息状态", "style": "放松", "phases": ["慢慢安静下来", "整理好周围", "放下手边的东西", "迷迷糊糊地待着", "逐渐进入睡眠"]},
        {"name": "睡一会儿", "style": "睡眠", "phases": ["刚睡着", "处于较安静的睡眠中", "翻个身继续睡", "短暂变得浅睡", "重新沉下来"]},
    ],
    "用餐": [
        {"name": "慢慢吃点东西", "style": "用餐", "phases": ["先准备好餐具", "开始吃", "中间喝点水", "慢慢吃完主要部分", "收拾一下桌面"]},
        {"name": "吃一顿饭", "style": "日常", "phases": ["准备食物", "先吃几口", "边吃边想事情", "喝点饮料或水", "吃完后缓一会儿"]},
    ],
    "通勤": [
        {"name": "去一个地方", "style": "移动", "phases": ["准备出发", "走到路上", "看看沿途", "途中短暂停留", "继续前往目的地"]},
        {"name": "路上慢慢过去", "style": "随行", "phases": ["刚出门", "进入路程", "看看消息或周围", "继续赶路", "快到目的地了"]},
    ],
    "自由时间": [
        {"name": "随便安排一段时间", "style": "随性", "phases": ["看看当下想做什么", "先做一件小事", "临时换个念头", "喝点水或活动一下", "继续随意安排"]},
        {"name": "折腾自己的小事", "style": "生活感", "phases": ["想到一个小念头", "开始随手处理", "做到一半想到别的", "回来继续", "差不多就先放下"]},
        {"name": "留一段没有计划的时间", "style": "留白", "phases": ["暂时不安排目标", "随便看看东西", "走神一会儿", "想到什么就做一点", "自然结束这段空闲"]},
    ],
}

DEFAULT_TEMPLATE = {
    "name": "随意活动",
    "style": "随性",
    "phases": ["开始活动", "继续一会儿", "短暂调整", "再做一点", "慢慢收尾"],
}

MIN_PROCESS_DURATION = 15
MAX_PROCESS_DURATION = 90
MIN_PHASE_MINUTES = 5
MAX_PHASE_MINUTES = 16


class ProcessService:
    """管理一个角色的持续生活过程，以及过程内部的阶段变化。"""

    def __init__(self, scope: RoleScope, config_provider, clock, schedule_service, spawn_fn=None):
        self._scope = scope
        self._config = config_provider
        self._clock = clock
        self._schedule = schedule_service
        self._spawn = spawn_fn
        self._pending_update = False
        self._last_update_attempt = 0.0

    @property
    def config(self) -> HumanoidConfig:
        return self._config()

    def current(self) -> dict:
        proc = self._scope.get_self("current_process")
        if not isinstance(proc, dict) or not proc:
            proc = self._create_initial_process()
            self._scope.set_self("current_process", proc)
        else:
            # 兼容旧版过程数据，同时让已经存在的长过程获得阶段更新时间。
            self._ensure_process_fields(proc)
        return proc

    def needs_update(self) -> bool:
        proc = self.current()
        now = self._clock.now()

        next_phase = self._parse_dt(proc.get("next_phase_at"))
        if next_phase is not None and now >= next_phase:
            return True

        end = self._parse_dt(proc.get("expected_end"))
        if end is None:
            return True
        return now >= end

    def tick(self):
        """只负责标记更新，不强制阻断当前过程。"""
        if self.needs_update():
            self._pending_update = True

    def update_sync(self) -> dict:
        proc = self._advance_or_generate()
        self._scope.set_self("current_process", proc)
        self._pending_update = False
        self._last_update_attempt = time.time()
        return proc

    async def update_async(self) -> dict:
        # 阶段更新无需频繁调用模型，因此只保留很短的防抖。
        if time.time() - self._last_update_attempt < 2:
            return self.current()
        proc = self._advance_or_generate()
        self._scope.set_self("current_process", proc)
        self._pending_update = False
        self._last_update_attempt = time.time()
        return proc

    def force_update(self) -> dict:
        proc = self._advance_or_generate(force=True)
        self._scope.set_self("current_process", proc)
        self._pending_update = False
        self._last_update_attempt = time.time()
        return proc

    def _create_initial_process(self) -> dict:
        now = self._clock.now()
        return self._generate_process_for_slot(
            now,
            self._schedule.current_slot(now.hour * 60 + now.minute),
        )

    def _advance_or_generate(self, force: bool = False) -> dict:
        now = self._clock.now()
        minutes = now.hour * 60 + now.minute
        slot = self._schedule.current_slot(minutes)
        current = self.current()

        end = self._parse_dt(current.get("expected_end"))
        if end is None or now >= end:
            return self._generate_process_for_slot(now, slot)

        if self._same_slot_context(current, slot) and (
            force or self._phase_due(current, now)
        ):
            return self._advance_phase(current, now)

        # 日程已经变化时，不必等旧过程结束，允许自然切换到新主题。
        if not self._same_slot_context(current, slot) and force:
            return self._generate_process_for_slot(now, slot)

        return current

    def _phase_due(self, proc: dict, now: datetime) -> bool:
        next_phase = self._parse_dt(proc.get("next_phase_at"))
        return next_phase is not None and now >= next_phase

    def _advance_phase(self, proc: dict, now: datetime) -> dict:
        phases = proc.get("phases") or DEFAULT_TEMPLATE["phases"]
        if not isinstance(phases, list) or not phases:
            phases = list(DEFAULT_TEMPLATE["phases"])

        try:
            index = int(proc.get("phase_index", 0))
        except (TypeError, ValueError):
            index = 0

        # 一次可能跨过多个阶段，例如应用长时间暂停后重新启动。
        elapsed = 0
        while True:
            next_phase = self._parse_dt(proc.get("next_phase_at"))
            if next_phase is None or now < next_phase:
                break

            index += 1
            if index >= len(phases):
                # 阶段列表走完后不改变“当前过程”主题，只生成一个自然的微变化。
                index = len(phases) - 1
                proc["phase"] = self._micro_variation(phases[index])
                break

            proc["phase"] = phases[index]
            proc["next_phase_at"] = self._next_phase_time(now, proc).isoformat()
            elapsed += 1
            if elapsed >= len(phases):
                break

        if not proc.get("phase"):
            proc["phase"] = phases[min(index, len(phases) - 1)]

        proc["phase_index"] = index
        proc["last_phase_change_at"] = now.isoformat()

        if index >= len(phases) - 1:
            # 最后阶段仍保留当前过程，只给下一次检查一个短窗口，避免过程卡死。
            proc["next_phase_at"] = min(
                self._parse_dt(proc.get("expected_end")) or (now + timedelta(minutes=5)),
                now + timedelta(minutes=random.randint(5, 10)),
            ).isoformat()
        return proc

    def _ensure_process_fields(self, proc: dict) -> None:
        now = self._clock.now()
        if not proc.get("started_at"):
            proc["started_at"] = now.replace(second=0, microsecond=0).isoformat()
        if not proc.get("expected_end"):
            duration = int(proc.get("duration_minutes") or random.randint(
                self.config.process_min_duration, self.config.process_max_duration
            ))
            proc["expected_end"] = (
                self._parse_dt(proc["started_at"]) + timedelta(minutes=max(5, duration))
            ).isoformat()
        if not proc.get("phases"):
            name = str(proc.get("name", "随意活动"))
            proc["phases"] = [name, "继续处理", "短暂调整", "再做一点", "收尾"]
        if not proc.get("phase"):
            proc["phase"] = proc["phases"][0]
        if not isinstance(proc.get("phase_index"), int):
            proc["phase_index"] = 0
        if not proc.get("style"):
            proc["style"] = "日常"
        if not proc.get("next_phase_at"):
            end = self._parse_dt(proc.get("expected_end")) or now
            next_at = min(end, now + timedelta(minutes=random.randint(MIN_PHASE_MINUTES, MAX_PHASE_MINUTES)))
            proc["next_phase_at"] = next_at.isoformat()
        if not proc.get("last_phase_change_at"):
            proc["last_phase_change_at"] = proc.get("started_at")
        if "slot_event" not in proc:
            proc["slot_event"] = ""
        if "slot_start" not in proc:
            proc["slot_start"] = ""

    def _generate_process_for_slot(self, now: datetime, slot: Slot) -> dict:
        minutes = now.hour * 60 + now.minute
        category = self._infer_category(slot)
        templates = PROCESS_TEMPLATES.get(category) or [DEFAULT_TEMPLATE]
        template = random.choice(templates)

        min_duration = max(5, int(self.config.process_min_duration or MIN_PROCESS_DURATION))
        max_duration = max(min_duration, int(self.config.process_max_duration or MAX_PROCESS_DURATION))
        remaining = self._slot_remaining_minutes(slot, minutes)

        if remaining >= min_duration:
            upper = min(max_duration, remaining)
            duration = random.randint(min_duration, max(min_duration, upper))
        elif remaining > 0:
            duration = max(5, remaining)
        else:
            duration = random.randint(min_duration, min(max_duration, max(30, min_duration + 15)))

        duration = max(5, int(duration * random.uniform(0.9, 1.1)))
        duration = min(duration, max_duration)
        if remaining > 0:
            duration = min(duration, max(5, remaining))

        start = now.replace(second=0, microsecond=0)
        end = start + timedelta(minutes=duration)

        phases = list(template.get("phases") or DEFAULT_TEMPLATE["phases"])
        phase_index = 0
        phase = phases[phase_index]
        next_phase = min(
            end,
            start + timedelta(
                minutes=random.randint(
                    MIN_PHASE_MINUTES,
                    min(MAX_PHASE_MINUTES, max(MIN_PHASE_MINUTES, duration // 2)),
                )
            ),
        )

        return {
            "name": str(template.get("name") or "随意活动"),
            "category": category,
            "style": str(template.get("style") or "日常"),
            "phase": phase,
            "phase_index": phase_index,
            "phases": phases,
            "started_at": start.isoformat(),
            "expected_end": end.isoformat(),
            "duration_minutes": duration,
            "next_phase_at": next_phase.isoformat(),
            "last_phase_change_at": start.isoformat(),
            "slot_event": str(slot.get("event", "")),
            "slot_start": str(slot.get("start", "")),
        }

    def _next_phase_time(self, now: datetime, proc: dict) -> datetime:
        end = self._parse_dt(proc.get("expected_end")) or (now + timedelta(minutes=10))
        remaining = max(1, int((end - now).total_seconds() // 60))
        step = random.randint(MIN_PHASE_MINUTES, min(MAX_PHASE_MINUTES, max(MIN_PHASE_MINUTES, remaining)))
        return min(end, now + timedelta(minutes=step))

    def _micro_variation(self, text: str) -> str:
        variations = {
            "停下来想想": ["停下来想一会儿", "稍微走神了一下", "重新理了一下思路"],
            "喝点水": ["顺手喝了点水", "补了几口水", "起来倒了杯水"],
            "短暂调整": ["稍微调整一下", "起来活动了一下", "停下来缓一缓"],
            "继续慢慢收尾": ["慢慢把最后一点收好", "处理最后的小细节", "差不多准备收尾"],
        }
        choices = variations.get(text)
        return random.choice(choices) if choices else text

    def _same_slot_context(self, current: dict, slot: Slot) -> bool:
        # 旧版状态没有 slot 元数据时，按“仍在同一类生活过程”兼容，
        # 避免升级后必须等整个旧过程结束才能出现阶段变化。
        if not current.get("slot_event") and not current.get("slot_start"):
            return True
        return (
            current.get("slot_event") == slot.get("event")
            and current.get("slot_start") == slot.get("start")
        )

    def _slot_remaining_minutes(self, slot: Slot, current_minutes: int) -> int:
        end_str = slot.get("end", "24:00")
        try:
            parts = end_str.split(":")
            end_minutes = int(parts[0]) * 60 + int(parts[1])
        except (ValueError, IndexError):
            end_minutes = 24 * 60
        return max(0, end_minutes - current_minutes)

    def _infer_category(self, slot: Slot) -> str:
        event = str(slot.get("event", "")).lower()
        if any(k in event for k in ("睡", "眠")):
            return "睡眠"
        if any(k in event for k in ("学习", "读书", "复习", "做题")):
            return "学习"
        if any(k in event for k in ("工作", "办公", "事务", "处理", "会议")):
            return "工作"
        if any(k in event for k in ("社交", "聚会", "聊天", "朋友")):
            return "社交"
        if any(k in event for k in ("运动", "跑步", "健身", "锻炼", "打球")):
            return "运动"
        if any(k in event for k in ("家务", "整理", "打扫", "做饭", "洗衣")):
            return "家务"
        if any(k in event for k in ("创作", "写作", "画", "设计", "手工")):
            return "创作"
        if any(k in event for k in ("餐", "早", "午", "晚", "吃", "饭")):
            return "用餐"
        if any(k in event for k in ("通勤", "路", "出行", "车")):
            return "通勤"
        return "自由时间"

    def describe(self) -> str:
        proc = self.current()
        name = proc.get("name", "休息")
        phase = proc.get("phase")
        if phase and phase != name:
            return f"{name}：{phase}"
        return str(name)

    @staticmethod
    def _parse_dt(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
