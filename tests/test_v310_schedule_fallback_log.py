"""v3.1.0（P0-4）：日程 LLM 生成失败、回落到「只含睡一觉」的模板时，必须留下 warning。

3.0.11 里这条路完全静默：模型报错、解析失败、超时都直接换成模板，用户看到的
是一份只有睡眠的日程，却没有任何日志说明「今天这份不可信」。本文件钉住三件事：
真失败要告警并计数、没开 LLM 的模板路径不许误报、生成成功时也不许误报。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.config import HumanoidConfig
from humanoid.llm import LLMGateway, ProviderResolver
from humanoid.services.schedule import SOURCE_LLM, SOURCE_TEMPLATE, ScheduleService

from .fakes import FakeContext, FakeProvider, FrozenClock, RecordingLogger, ScopeStore
from .test_v3011_review import seg_reply

TZ = ZoneInfo("Asia/Shanghai")
FIXED_TODAY = "2026-08-22"
FIXED_MOMENT = datetime(2026, 8, 22, 9, 0, tzinfo=TZ)


def cfg(**kw) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", **kw})


class TemplateFallbackLogTest(unittest.IsolatedAsyncioTestCase):
    def build(self, conf=None, providers=None):
        self.conf = conf or cfg(schedule_provider_name="p")
        tmp = Path(tempfile.mkdtemp()) / "state.json"
        store = ScopeStore(tmp)
        store.load(FIXED_TODAY, self.conf.cycle_length)
        log = RecordingLogger()
        ctx = FakeContext(chat_providers=list(providers or []))
        resolver = ProviderResolver(ctx, log)
        gateway = LLMGateway(resolver, lambda: self.conf, log, time.monotonic)
        service = ScheduleService(
            store.scope,
            lambda: self.conf,
            FrozenClock(FIXED_MOMENT),
            logger=log,
            monotonic=time.monotonic,
        )
        service.set_resolver_gateway(resolver, gateway)
        return service, store, log

    async def test_failed_generation_warns_and_counts(self):
        provider = FakeProvider("p", error=RuntimeError("模型挂了"))
        service, _store, log = self.build(cfg(schedule_provider_name="p"), [provider])
        await service.ensure_fresh(force=True, ignore_cooldown=True)
        self.assertEqual(service.source, SOURCE_TEMPLATE, "生成失败仍然应该回落到模板")
        warning = log.text("warning")
        self.assertIn("日程 LLM 生成失败", warning, "回落点必须留下 warning（P0-4）")
        self.assertIn("今日日程不可信", warning, "日志要说清这份日程不能信")
        self.assertEqual(
            service._quality.get("template_fallback"), 1, "回落次数要能被统计到"
        )

    async def test_template_path_without_llm_does_not_warn(self):
        service, _store, log = self.build(cfg(use_llm_schedule=False), [])
        await service.ensure_fresh(force=True, ignore_cooldown=True)
        self.assertEqual(service.source, SOURCE_TEMPLATE)
        self.assertNotIn(
            "日程 LLM 生成失败", log.text("warning"), "用户主动关掉 LLM 时模板是正常出路，不该告警"
        )
        self.assertEqual(
            service._quality.get("template_fallback"), 0, "没开 LLM 的模板路径不该被记成生成失败"
        )

    async def test_successful_generation_does_not_warn(self):
        provider = FakeProvider("p", reply=seg_reply(event="改海报", minutes=40))
        service, _store, log = self.build(cfg(schedule_provider_name="p"), [provider])
        await service.ensure_fresh(force=True, ignore_cooldown=True)
        self.assertEqual(service.source, SOURCE_LLM)
        self.assertNotIn("日程 LLM 生成失败", log.text("warning"))
        self.assertEqual(service._quality.get("template_fallback"), 0)


if __name__ == "__main__":
    unittest.main()
