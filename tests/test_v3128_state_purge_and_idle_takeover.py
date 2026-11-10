"""v3.1.28：旧状态里的设备词真删掉 + 整段本地兜底的角色也能被模型接手一次。

两条都来自线上形态：

* 3.1.18 之前设备词已经落过盘，读的时候过滤只能不展示，`_stored_slots()` 还会把它们
  喂回提示词——所以要做一次性迁移，在 state 里把含设备/含糊词的条目删掉。
* `3951331639` 这类角色没人说话，`_healed_at` 只在内存，重载后丢；自愈补的又是具体
  事（不是 `status`），老逻辑判「像模型写的」就不给接班，于是整段一直停在 template。
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from humanoid.llm import LLMGateway, LLMResult, OUTCOME_OK, ProviderResolver
from humanoid.services.schedule import (
    HEAL_GEN_KEY,
    PURGE_KEY,
    PURGE_VERSION,
    SOURCE_TEMPLATE,
    ScheduleService,
)

from .fakes import FakeContext, FakeProvider, FrozenClock, RecordingLogger, ScopeStore
from .test_schedule import cfg, seg_reply

TZ = ZoneInfo("Asia/Shanghai")
FIXED_TODAY = "2026-08-22"
MOMENT = datetime(2026, 8, 22, 9, 30, tzinfo=TZ)


def slot(start: str, end: str, event: str, *, status: bool = False) -> dict:
    out = {
        "start": start,
        "end": end,
        "event": event,
        "location": "家里",
        "energy_rate": 0.0,
    }
    if status:
        out["status"] = True
    return out


class _Gate:
    """闸门替身：只按「空闲静默」拒绝，`budget_only` 放行。"""

    def __init__(self, reason: str = "idle") -> None:
        self.reason = reason

    def check(self, purpose, *, now=None, budget_only=False, role_id="", ignore_interval=False):
        if budget_only:
            return SimpleNamespace(allowed=True, reason="off", detail="")
        return SimpleNamespace(allowed=False, reason=self.reason, detail="")

    def consume(self, purpose) -> None:
        """替身不记账。"""

    def interaction_at(self, role_id=""):
        """没人说话：真实线上就是 None。"""
        return None

    def log_block(self, *args, **kwargs) -> None:
        """替身不写日志。"""


class _Recorder:
    """记账式模型：证明接班真的走到了 `gateway.generate`。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0
        self.prompts: list[str] = []
        self.gate = _Gate("idle")

    async def generate(self, **kwargs):
        self.prompts.append(str(kwargs.get("prompt") or ""))
        self.calls += 1
        text = '{"ok": true}' if "【审查这一段】" in str(kwargs.get("prompt") or "") else self.reply
        return LLMResult(ok=True, text=text, outcome=OUTCOME_OK, label="fake", provider_id="p")


class StatePurgeTest(unittest.TestCase):
    def build(self, moment=MOMENT):
        conf = cfg(schedule_provider_name="p")
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load(FIXED_TODAY, conf.cycle_length)
        log = RecordingLogger()
        provider = FakeProvider("p", reply=seg_reply(minutes=40))
        resolver = ProviderResolver(FakeContext(chat_providers=[provider]), log)
        gateway = LLMGateway(resolver, lambda: conf, log, time.monotonic)
        service = ScheduleService(
            store.scope, lambda: conf, FrozenClock(moment), logger=log, monotonic=time.monotonic
        )
        service.set_resolver_gateway(resolver, gateway)
        return service

    def test_dirty_entries_are_deleted_from_state(self):
        service = self.build()
        scope = service._scope
        scope.update_self(
            today_date=FIXED_TODAY,
            daily_schedule=[
                slot("08:00", "08:40", "躺着刷手机"),
                slot("08:40", "09:20", "把碗洗了"),
            ],
            prev_day_schedule=[slot("21:00", "21:40", "用手边的东西垫着")],
            schedule_history={
                "2026-08-21": [
                    [1260, 1300, "看手机", "卧室"],
                    [1300, 1340, "叠衣服", "卧室"],
                ]
            },
            process_history=["自由活动", "给姐姐发消息", "把地扫了"],
            life_log=[
                {"text": "刷手机刷到眼睛酸", "uid": "u", "at": 1.0},
                {"text": "他面试还没回音", "uid": "u", "at": 2.0},
            ],
            day_frame="阴天，被电话搅了清静",
            schedule_suspended={"event": "忙自己的事", "location": "家里"},
        )

        service._purge_dirty_history()  # 写路径触发的一次性迁移

        daily = [item["event"] for item in scope.get_self("daily_schedule")]
        self.assertEqual(daily, ["把碗洗了"], "脏段整条删掉，干净段保留")
        self.assertEqual(scope.get_self("prev_day_schedule"), [])
        history = scope.get_self("schedule_history")["2026-08-21"]
        self.assertEqual([item[2] for item in history], ["叠衣服"])
        self.assertEqual(scope.get_self("process_history"), ["自由活动", "把地扫了"])
        self.assertEqual(
            [item["text"] for item in scope.get_self("life_log")],
            ["他面试还没回音"],
            "带设备词的未完事项也删掉（这本账会被渲染进提示词），干净的保留",
        )
        self.assertEqual(scope.get_self("day_frame"), "")
        self.assertFalse(scope.get_self("schedule_suspended"), "脏挂起要清掉")
        self.assertEqual(scope.get_self(PURGE_KEY), PURGE_VERSION, "跑过要打标记")

    def test_dirty_dreams_are_dropped_but_the_sleep_stays(self):
        service = self.build()
        sleep_slot = slot("19:18", "19:40", "侧躺着慢慢睡着了")
        sleep_slot["sleep"] = True
        sleep_slot["dreams"] = ["旧相册里那张合影", "娜莉灵在电话里撒娇", "阳台的花又蔫了"]
        service._scope.update_self(today_date=FIXED_TODAY, prev_day_schedule=[sleep_slot])
        service._purge_dirty_history()
        kept = service._scope.get_self("prev_day_schedule")
        self.assertEqual(len(kept), 1, "睡段不能整条删，否则睡眠连续性断了")
        self.assertEqual(kept[0]["dreams"], ["旧相册里那张合影", "阳台的花又蔫了"])

    def test_purge_runs_once(self):
        service = self.build()
        service._scope.update_self(today_date=FIXED_TODAY, daily_schedule=[])
        service._purge_dirty_history()
        service._scope.set_self("daily_schedule", [slot("08:00", "08:40", "刷手机")])
        service._purge_dirty_history()
        self.assertEqual(
            [item["event"] for item in service._scope.get_self("daily_schedule")],
            ["刷手机"],
            "迁移只跑一次：之后写进来的东西不再被扫",
        )


class IdleTakeoverTest(unittest.TestCase):
    def build(self, segments):
        conf = cfg(
            schedule_provider_name="p",
            schedule_min_interval_minutes=0,
            schedule_max_interval_minutes=0,
            llm_idle_silence_minutes=60,
            llm_daily_call_budget=100,
        )
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load(FIXED_TODAY, conf.cycle_length)
        log = RecordingLogger()
        provider = FakeProvider("p", reply=seg_reply(minutes=40))
        resolver = ProviderResolver(FakeContext(chat_providers=[provider]), log)
        gateway = LLMGateway(resolver, lambda: conf, log, time.monotonic)
        gateway._gate = _Gate("idle")
        service = ScheduleService(
            store.scope, lambda: conf, FrozenClock(MOMENT), logger=log, monotonic=time.monotonic
        )
        service.set_resolver_gateway(resolver, gateway)
        service._scope.update_self(today_date=FIXED_TODAY, daily_schedule=segments)
        service._set_source(SOURCE_TEMPLATE)
        return service, gateway

    def test_concrete_local_segment_earns_a_takeover(self):
        """重载后 `_healed_at` 丢了，具体事也还是本地兜底——要给一次接班。"""
        service, _gateway = self.build([slot("08:40", "10:10", "翻出以前的照片看了一会儿")])
        active = service._covering(service.segments(), 9 * 60 + 30)
        self.assertIsNotNone(active)
        self.assertEqual(service.source, SOURCE_TEMPLATE)
        self.assertTrue(service._has_real_activity())
        self.assertTrue(service.refresh_due(), "整段是本地兜底时也该把模型请回来")

    def test_takeover_really_reaches_the_model(self):
        service, _gateway = self.build([slot("08:40", "10:10", "翻出以前的照片看了一会儿")])
        recorder = _Recorder(seg_reply(event="去菜市场买了把青菜", minutes=50))
        service.set_resolver_gateway(service.resolver, recorder)
        self.assertTrue(asyncio.run(service.ensure_fresh()))
        self.assertGreaterEqual(recorder.calls, 1)
        events = [item.get("event") for item in service.segments()]
        self.assertIn("去菜市场买了把青菜", events)
        self.assertEqual(service.source, "llm")

    def test_takeover_limit_is_restored_from_disk(self):
        """限频落盘：重载后不能因为内存清零又白送一次模型调用。"""
        service, _gateway = self.build([slot("08:40", "10:10", "翻出以前的照片看了一会儿")])
        service._scope.set_self(HEAL_GEN_KEY, float(service._clock.timestamp()))
        fresh = ScheduleService(
            service._scope,
            lambda: cfg(schedule_provider_name="p"),
            FrozenClock(MOMENT),
            logger=RecordingLogger(),
            monotonic=time.monotonic,
        )
        fresh.set_resolver_gateway(service.resolver, service.gateway)
        self.assertGreater(fresh._heal_gen_at, 0.0, "落盘的限频要换算回单调钟")
        self.assertFalse(fresh.refresh_due(), "刚接班过，四小时内不再打扰")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
