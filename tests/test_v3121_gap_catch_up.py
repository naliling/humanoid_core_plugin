"""v3.1.21：自愈补出来的处境段，要能把模型请回来一次。

线上形态：没人跟她说话 → 闸门按「空闲静默」一直挡着 → `heal_gap()` 每断一次就补一段
`dynamic_segment` 的处境词（「安静待着」「忙自己的事」）→ 时间线连续了，但一整天没有
一件真事。这组测试盯住：补位之后的那一段可以越过**空闲静默**一次，让模型接手；但
预算用完不算、没补过不算、刚补过又刚接手过也不算（一小时最多一次）。
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
    HEAL_CATCHUP_SECONDS,
    HEAL_CATCHUP_WINDOW_SECONDS,
    SOURCE_LLM,
    SOURCE_TEMPLATE,
    ScheduleService,
)

from .fakes import FakeContext, FakeProvider, FrozenClock, RecordingLogger, ScopeStore
from .test_schedule import cfg, seg_reply

TZ = ZoneInfo("Asia/Shanghai")
FIXED_TODAY = "2026-08-22"
HOLE_MOMENT = datetime(2026, 8, 22, 9, 30, tzinfo=TZ)


def slot(start: str, end: str, event: str = "在窗边坐着", *, status: bool = False) -> dict:
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
    """闸门替身：按给定理由拒绝，记录被问了几次。

    `budget_only=True` 是「用户明确要求」那条路（`ignore_cooldown` 会传成它）：
    只查每日预算，绕开空闲静默与最小间隔——替身里预算永远够。
    """

    def __init__(self, reason: str = "idle") -> None:
        self.reason = reason
        self.calls = 0

    def check(
        self,
        purpose,
        *,
        now=None,
        budget_only=False,
        role_id="",
        ignore_interval=False,
    ):
        self.calls += 1
        if budget_only:
            return SimpleNamespace(allowed=True, reason="off", detail="")
        return SimpleNamespace(allowed=False, reason=self.reason, detail="")

    def consume(self, purpose) -> None:
        """放行之后记一次额度：替身不记账。"""

    def interaction_at(self, role_id=""):
        """有人跟她说过话吗：空闲静默的替身算「说过」，全新部署（真闸门）返回 None。"""
        return time.time() if self.reason == "idle" else None

    def log_block(self, *args, **kwargs) -> None:
        """被挡回时的日志：替身不写。"""


class _RecordingGateway:
    """把模型调用换成记账：用来证明「接班」真的走到了 `gateway.generate`。

    v3.1.21 的初版就是在这一步断的——决策层放行了，但 `ensure_fresh` 内部的第二次
    `refresh_due()` 看到刚写下的限频标记又返回 False，于是永远没有一次模型调用。
    """

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0
        self.prompts: list[str] = []
        # `_gate_allows()` 会读 `self.gateway.gate`：这里照样给一个「空闲静默」的闸门。
        self.gate = _Gate("idle")

    async def generate(self, **kwargs):
        prompt = str(kwargs.get("prompt") or "")
        self.prompts.append(prompt)
        self.calls += 1
        text = '{"ok": true}' if "【审查这一段】" in prompt else self.reply
        return LLMResult(
            ok=True,
            text=text,
            outcome=OUTCOME_OK,
            label="fake",
            provider_id="p",
        )


class GapCatchUpTest(unittest.TestCase):
    def build(self, moment=HOLE_MOMENT, *, idle=60, budget=100, gate_reason="idle"):
        conf = cfg(
            schedule_provider_name="p",
            schedule_min_interval_minutes=0,
            schedule_max_interval_minutes=0,
            llm_idle_silence_minutes=idle,
            llm_daily_call_budget=budget,
        )
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load(FIXED_TODAY, conf.cycle_length)
        log = RecordingLogger()
        provider = FakeProvider("p", reply=seg_reply(minutes=40))
        resolver = ProviderResolver(FakeContext(chat_providers=[provider]), log)
        gateway = LLMGateway(resolver, lambda: conf, log, time.monotonic)
        service = ScheduleService(
            store.scope,
            lambda: conf,
            FrozenClock(moment),
            logger=log,
            monotonic=time.monotonic,
        )
        if gate_reason is not None:
            gateway._gate = _Gate(gate_reason)
        service.set_resolver_gateway(resolver, gateway)
        return service, provider

    def seed(self, service, segments):
        service._scope.update_self(today_date=FIXED_TODAY, daily_schedule=segments)

    def heal(self, service):
        """真走一次自愈：造一个空档再让 heal_gap 补上。"""
        self.seed(service, [slot("00:00", "01:35", "肚子空着")])
        self.assertTrue(service.heal_gap())
        return service._covering(service.segments(), 9 * 60 + 30)

    # ------------------------------------------------------------------
    # 资格判定
    # ------------------------------------------------------------------

    def test_healed_segment_earns_one_catch_up(self):
        service, _provider = self.build()
        active = self.heal(service)
        self.assertFalse(active.get("status"), "v3.1.23 起自愈补的是具体事")
        self.assertEqual(service.source, SOURCE_TEMPLATE)
        self.assertTrue(service.refresh_due(), "补位之后应该把模型请回来一次")
        self.assertTrue(service._catch_up_armed, "放行只是「待办」，等真去调模型才落时间戳")
        self.assertEqual(service._heal_gen_at, 0.0, "决策层不许提前写限频标记")

    def test_catch_up_is_used_once_then_rate_limited(self):
        service, provider = self.build()
        self.heal(service)
        self.assertTrue(service.refresh_due())
        self.assertTrue(asyncio.run(service.ensure_fresh()), "接班这一次真的调到模型")
        self.assertEqual(provider.calls, 1)
        self.assertGreater(service._heal_gen_at, 0.0, "真走到模型那一步才算接班过")
        self.assertFalse(service.refresh_due(), "一小时最多接班一次")

    def test_plain_event_from_llm_does_not_earn_catch_up(self):
        """模型自己写的段不该再问一遍：只有本地兜底段才需要接班（v3.1.28）。"""
        service, _provider = self.build()
        service._set_source(SOURCE_LLM)
        self.seed(service, [slot("08:00", "10:30", "在窗边坐着")])
        self.assertFalse(service.refresh_due(), "模型写过的段不再越过空闲静默")

    def test_inherited_status_segment_still_earns_one_catch_up(self):
        """重启后内存里没有补位时刻：处境段照样该被请一次模型，别僵到段尾。"""
        service, _provider = self.build()
        self.seed(service, [slot("08:00", "10:30", "在窗边坐着", status=True)])
        self.assertEqual(service._healed_at, 0.0, "新进程里没有补位时刻")
        self.assertTrue(service.refresh_due(), "继承来的处境段也算补出来的")
        service._heal_gen_at = float(service._monotonic())
        self.assertFalse(service.refresh_due(), "但一小时仍然只接班一次")

    def test_catch_up_window_expires(self):
        service, _provider = self.build()
        self.heal(service)
        service._healed_at = time.monotonic() - HEAL_CATCHUP_WINDOW_SECONDS - 1
        self.assertFalse(service.refresh_due(), "补位太久了，那一段已经是「旧安排」")

    def test_budget_refusal_does_not_earn_catch_up(self):
        service, _provider = self.build()
        self.heal(service)
        service.gateway._gate = _Gate("budget")
        self.assertFalse(service.refresh_due(), "每日预算用完不能绕")

    def test_interval_refusal_does_not_earn_catch_up(self):
        service, _provider = self.build()
        self.heal(service)
        service.gateway._gate = _Gate("interval")
        self.assertFalse(service.refresh_due(), "最小间隔那条本来就该由 ignore_interval 处理")

    def test_peek_never_consumes_the_catch_up(self):
        service, _provider = self.build()
        self.heal(service)
        self.assertFalse(service._due_peek(), "只读路径不改状态")
        self.assertEqual(service._heal_gen_at, 0.0)

    # ------------------------------------------------------------------
    # 真跑一遍：接班之后模型段落地，欠账清零
    # ------------------------------------------------------------------

    def test_model_takes_over_after_a_heal(self):
        service, provider = self.build(gate_reason=None)  # 没有闸门，接班这次真的放行
        self.heal(service)
        self.assertGreater(service._healed_at, 0.0)
        self.assertTrue(asyncio.run(service.ensure_fresh()))
        self.assertEqual(provider.calls, 1)
        self.assertEqual(service.source, "llm")
        self.assertEqual(service._healed_at, 0.0, "模型接上了，欠账清零")

    def test_catch_up_really_reaches_the_model_under_idle(self):
        """v3.1.22 的回归：空闲静默挡着时，接班必须真的调到模型一次。

        初版在 `_due()` 里就写了 `_heal_gen_at`，`ensure_fresh` 内部的第二次
        `refresh_due()` 于是判 False，模型一次都没被叫过——线上表现就是「只有自愈、
        没有一件真事」。
        """
        service, _provider = self.build()
        recorder = _RecordingGateway(seg_reply(event="去超市买菜", minutes=85))
        service.set_resolver_gateway(service.resolver, recorder)
        self.heal(service)
        self.assertTrue(asyncio.run(service.ensure_fresh()), "接班这一次要真的走到模型")
        self.assertGreaterEqual(recorder.calls, 1, "至少一次生成调用")
        self.assertIn("【可以选的事】", recorder.prompts[0], "走的是日程生成那条路")
        events = [seg.get("event") for seg in service.segments()]
        self.assertIn("去超市买菜", events, "模型回的段要真的落地")
        self.assertEqual(service.source, "llm")
        self.assertEqual(service._healed_at, 0.0, "模型接上了，欠账清零")

    def test_idle_gate_does_not_freeze_the_healed_segment(self):
        service, provider = self.build()  # 空闲静默一直挡着：接班是唯一的出口
        self.heal(service)
        self.assertTrue(asyncio.run(service.ensure_fresh()), "接班要把模型请回来")
        self.assertEqual(provider.calls, 1)
        tail = service.segments()[-1]
        self.assertNotIn("status", tail, "接班之后落地的是真事，不是处境词")
        self.assertIsNotNone(
            service._covering(service.segments(), 9 * 60 + 30),
            "换段也不能留空档",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
