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
from humanoid.services.schedule import SOURCE_TEMPLATE, ScheduleService

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

    # Stable segments are not rewritten until their planned boundary.
    def test_healed_segment_is_stable_until_natural_boundary(self):
        service, provider = self.build()
        active = self.heal(service)
        self.assertIsNotNone(active)
        self.assertEqual(service.source, SOURCE_TEMPLATE)
        original = dict(active)
        self.assertFalse(service.refresh_due())
        self.assertFalse(asyncio.run(service.ensure_fresh()))
        self.assertEqual(provider.calls, 0)
        self.assertEqual(service._covering(service.segments(), 9 * 60 + 30), original)
        end_hour, end_minute = (int(part) for part in str(active["end"]).split(":"))
        service._clock = FrozenClock(datetime(2026, 8, 22, end_hour, end_minute, tzinfo=TZ))
        self.assertTrue(service.refresh_due())

if __name__ == "__main__":  # pragma: no cover
    unittest.main()
