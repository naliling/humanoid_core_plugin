"""过程服务：管理单个角色的当前过程。"""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta
from typing import Any, Optional

from ..config import HumanoidConfig
from ..role_scope import RoleScope
from ..slots import Slot

CATEGORY_ACTIVITIES = {
    "学习": ["阅读书籍", "整理笔记", "写代码", "做练习题", "观看教学视频", "复习资料", "研究课题"],
    "工作": ["处理邮件", "编写报告", "开会讨论", "数据分析", "项目规划", "客户沟通", "整理文档"],
    "休息": ["闭目养神", "听音乐", "喝茶", "发呆", "刷手机", "散步", "冥想"],
    "社交": ["聊天", "一起吃饭", "玩游戏", "外出散步", "看电影", "讨论话题"],
    "运动": ["跑步", "瑜伽", "拉伸", "健身", "打球", "散步"],
    "家务": ["打扫卫生", "整理房间", "做饭", "洗衣", "浇花", "收纳"],
    "创作": ["写作", "绘画", "编曲", "设计", "手工", "摄影"],
    "睡眠": ["深度睡眠", "浅睡", "休息中", "做梦"],
    "用餐": ["吃早餐", "吃午餐", "吃晚餐", "吃点心", "喝咖啡"],
    "通勤": ["走路", "坐车", "骑车", "开车"],
    "自由时间": ["随意活动", "休闲放松", "做自己喜欢的事", "发呆"],
}

MIN_PROCESS_DURATION = 15
MAX_PROCESS_DURATION = 90


class ProcessService:
    """管理单个角色的当前过程。"""

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
        if not proc or not isinstance(proc, dict):
            proc = self._create_initial_process()
            self._scope.set_self("current_process", proc)
        return proc

    def needs_update(self) -> bool:
        proc = self.current()
        end_str = proc.get("expected_end")
        if not end_str:
            return True
        try:
            end = datetime.fromisoformat(end_str)
        except ValueError:
            return True
        return self._clock.now() >= end

    def tick(self):
        if self.needs_update():
            self._pending_update = True

    def update_sync(self) -> dict:
        proc = self._generate_next_process()
        self._scope.set_self("current_process", proc)
        self._pending_update = False
        self._last_update_attempt = time.time()
        return proc

    async def update_async(self) -> dict:
        if time.time() - self._last_update_attempt < 30:
            return self.current()
        proc = self._generate_next_process()
        self._scope.set_self("current_process", proc)
        self._pending_update = False
        self._last_update_attempt = time.time()
        return proc

    def force_update(self) -> dict:
        proc = self._generate_next_process()
        self._scope.set_self("current_process", proc)
        self._pending_update = False
        self._last_update_attempt = time.time()
        return proc

    def _create_initial_process(self) -> dict:
        now = self._clock.now()
        return self._generate_process_for_slot(now)

    def _generate_next_process(self) -> dict:
        now = self._clock.now()
        return self._generate_process_for_slot(now)

    def _generate_process_for_slot(self, now: datetime) -> dict:
        minutes = now.hour * 60 + now.minute
        slot = self._schedule.current_slot(minutes)
        category = self._infer_category(slot)

        activities = CATEGORY_ACTIVITIES.get(category, ["休息"])
        name = random.choice(activities)

        remaining = self._slot_remaining_minutes(slot, minutes)
        if remaining <= 0:
            duration = random.randint(MIN_PROCESS_DURATION, min(30, MAX_PROCESS_DURATION))
        else:
            duration = min(
                random.randint(MIN_PROCESS_DURATION, MAX_PROCESS_DURATION),
                max(MIN_PROCESS_DURATION, remaining)
            )
        duration = max(MIN_PROCESS_DURATION, int(duration * random.uniform(0.8, 1.2)))

        start = now.replace(second=0, microsecond=0)
        end = start + timedelta(minutes=duration)

        return {
            "name": name,
            "category": category,
            "started_at": start.isoformat(),
            "expected_end": end.isoformat(),
            "duration_minutes": duration,
        }

    def _slot_remaining_minutes(self, slot: Slot, current_minutes: int) -> int:
        end_str = slot.get("end", "24:00")
        try:
            parts = end_str.split(":")
            end_minutes = int(parts[0]) * 60 + int(parts[1])
        except (ValueError, IndexError):
            end_minutes = 24 * 60
        return max(0, end_minutes - current_minutes)

    def _infer_category(self, slot: Slot) -> str:
        event = slot.get("event", "").lower()
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
        start_str = proc.get("started_at")
        if start_str:
            try:
                start = datetime.fromisoformat(start_str)
                now = self._clock.now()
                elapsed = int((now - start).total_seconds() // 60)
                return f"{name}（已持续约 {elapsed} 分钟）"
            except ValueError:
                pass
        return name