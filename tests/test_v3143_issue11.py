"""v3.1.43：issue #11 的四处改动各钉一条断言。

issue #11（KiWinger）提了五条，这一版做了四条，第五条（接 livingmemory）没做，
理由写在 issue #11 的回复里：`memory_link.py` 只对接 `astrbot_plugin_memory_companion`，
换引擎要一份稳定的桥接接口，不能照着猜。

这里钉的是「改了之后不会悄悄退回去」的四件事：

1. 面板留空 = 跟随 AstrBot 官方默认模型。以前 `allow_global=False` + 空链会直接
   判 `OUTCOME_NO_CANDIDATE`，用户把面板清空就等于「永远只按身体与钟点现算」，
   看着像插件坏了。
2. 天气过期要能在 `/拟人 诊断` 里看出**为什么**过期：`WeatherService.status()`
   摊开最后成功时间、数据年龄、宽限、连续失败次数、下次重试、最近一次错误原文。
   同时天气出网可以填代理（`weather_proxy`），因为 issue 里那台机器连不上
   `api.openweathermap.org`（日志里是 `getaddrinfo failed` 与 `TimeoutError`）。
3. 诊断超过阈值改走文转图，按【小节】边界分页、单节超长硬切、超过上限丢尾巴；
   转图失败必须回落纯文本——诊断不能因为渲染挂了就打不开。
4. 异机 bot：`preset_role_ids` 让「没在这个进程里发过言的号」也有一份身份档案。
   这只多给一份档案，本插件不会去驱动另一台机器上的 bot。
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.config import HumanoidConfig
from humanoid.llm import (
    GLOBAL_LABEL,
    OUTCOME_NO_CANDIDATE,
    LLMGateway,
    ProviderResolver,
)
from humanoid.role_manager import RoleManager
from humanoid.services.weather import WeatherService

from .fakes import FakeClock, FakeContext, FakeProvider, FrozenClock, RecordingLogger, ScopeStore
from .test_main_integration import SKIP_REASON, main_module

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

TZ = ZoneInfo("Asia/Shanghai")
MOMENT = datetime(2026, 8, 22, 9, 0, tzinfo=TZ)
PAYLOAD = {
    "weather": [{"description": "多云"}],
    "main": {"temp": 26.5, "humidity": 70},
}
NEWLINE = chr(10)


def _read(name: str) -> str:
    with open(os.path.join(_ROOT, name), "r", encoding="utf-8") as handle:
        return handle.read()


def _schema() -> dict:
    return json.loads(_read("_conf_schema.json"))


def cfg(**overrides: object) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", **overrides})


def weather_cfg(**overrides: object) -> HumanoidConfig:
    base = {"weather_api_key": "0123456789abcdef", "weather_location": "Beijing,CN"}
    return cfg(**{**base, **overrides})


async def _ok_fetch(url: str, timeout: float) -> dict:
    return PAYLOAD


class PresetRoleIdsConfigTest(unittest.TestCase):
    """`preset_role_ids` 是人填的一串号：逗号、全角逗号、分号、空格都得认。"""

    def test_empty_means_only_roles_that_spoke(self):
        self.assertEqual(cfg().preset_role_id_list(), ())
        self.assertEqual(cfg(preset_role_ids="   ").preset_role_id_list(), ())

    def test_splits_every_common_separator(self):
        conf = cfg(preset_role_ids="10001,10002，10003;10004 10005")
        self.assertEqual(
            conf.preset_role_id_list(),
            ("10001", "10002", "10003", "10004", "10005"),
        )

    def test_dedupes_and_strips(self):
        conf = cfg(preset_role_ids=" 10001 ,10001，10002 ")
        self.assertEqual(conf.preset_role_id_list(), ("10001", "10002"))

    def test_accepts_a_list_when_state_was_written_by_hand(self):
        # from_raw 会把列表 str() 成 "['10001', ...]"，所以这里直接替换 dataclass 字段，
        # 模拟「手改存档里写成列表」那条路径。
        conf = dataclasses.replace(cfg(), preset_role_ids=["10001", " 10002 ", "10001"])
        self.assertEqual(conf.preset_role_id_list(), ("10001", "10002"))


class PresetRoleWarmupTest(unittest.TestCase):
    """角色管理器启动时要能把预置号一起预热，配置读不到也不许炸。"""

    def _manager(self, config_provider) -> RoleManager:
        store = types.SimpleNamespace(data={"roles": {}})
        return RoleManager(store, config_provider, RecordingLogger(), None, None)

    def test_reads_preset_ids_through_config_provider(self):
        conf = cfg(preset_role_ids="10001，10002 10001")
        manager = self._manager(lambda: conf)
        self.assertEqual(manager._preset_role_ids(), ["10001", "10002"])

    def test_broken_config_provider_returns_empty(self):
        def boom() -> HumanoidConfig:
            raise RuntimeError("配置还没加载完")

        manager = self._manager(boom)
        self.assertEqual(manager._preset_role_ids(), [])


class WeatherStatusTest(unittest.IsolatedAsyncioTestCase):
    """诊断要能回答「三天没取到天气，到底是网络还是逻辑」。"""

    def build(self, conf=None, fetch=None):
        self.conf = conf or weather_cfg()
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load("2026-08-22", self.conf.cycle_length)
        clock = FrozenClock(MOMENT, city="北京")
        self.log = RecordingLogger()
        service = WeatherService(store.scope, lambda: self.conf, clock, fetch or _ok_fetch, self.log)
        return service

    def test_status_before_first_fetch_is_stale_and_shows_the_grace_window(self):
        service = self.build(weather_cfg(weather_refresh_minutes=30))
        status = service.status()
        self.assertTrue(status["enabled"])
        self.assertTrue(status["has_key"])
        self.assertIsNone(status["fetched_at"])
        self.assertTrue(status["stale"], "还没取过就是过期，不能让注入里显示旧天气")
        self.assertEqual(status["grace_seconds"], 30 * 60 * 3)
        self.assertEqual(status["fail_streak"], 0)
        self.assertEqual(status["last_error"], "")
        self.assertIsNone(status["next_retry_seconds"])

    async def test_status_after_success_is_fresh(self):
        service = self.build()
        self.assertTrue(await service.refresh_async())
        status = service.status()
        self.assertIsNotNone(status["fetched_at"])
        self.assertFalse(status["stale"])
        self.assertLessEqual(status["age_seconds"], 1.0)
        self.assertEqual(status["fail_streak"], 0)
        self.assertEqual(status["last_error"], "")

    def test_status_reports_the_proxy_that_will_be_used(self):
        service = self.build(weather_cfg(weather_proxy="http://127.0.0.1:7890"))
        self.assertEqual(service.status()["proxy"], "http://127.0.0.1:7890")

    async def test_status_keeps_the_last_error_after_a_failure(self):
        async def boom(url: str, timeout: float) -> dict:
            raise RuntimeError(
                "Cannot connect to host api.openweathermap.org:443 ssl:default [getaddrinfo failed]"
            )

        service = self.build(fetch=boom)
        self.assertFalse(await service.refresh_async())
        status = service.status()
        self.assertEqual(status["fail_streak"], 1)
        self.assertIn("getaddrinfo failed", status["last_error"])
        self.assertIsNotNone(status["last_error_ago"])
        self.assertGreater(status["next_retry_seconds"], 0, "退避期内不该再撞接口")

    async def test_success_clears_the_previous_error(self):
        calls = {"n": 0}

        async def flaky(url: str, timeout: float) -> dict:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("TimeoutError")
            return PAYLOAD

        service = self.build(fetch=flaky)
        self.assertFalse(await service.refresh_async())
        self.assertEqual(service.status()["fail_streak"], 1)
        # 退避还没到期，所以这里要 force：模拟「过了一阵网络终于通了」。
        self.assertTrue(await service.refresh_async(force=True))
        status = service.status()
        self.assertEqual(status["fail_streak"], 0)
        self.assertEqual(status["last_error"], "")
        self.assertIsNone(status["last_error_ago"])


@unittest.skipIf(main_module is None, SKIP_REASON or "main.py 无法加载")
class DiagnosticsPaginationTest(unittest.TestCase):
    """诊断转图的分页：每页都塞得进一张图，尾巴按上限丢。"""

    def _pages(self, text: str, **kwargs) -> list[str]:
        return main_module._diagnostics_pages(text, **kwargs)

    def test_short_report_stays_one_page(self):
        text = "〖拟人诊断〗v3.1.43" + NEWLINE + "【身体】好"
        self.assertEqual(self._pages(text), [text])

    def test_every_page_fits_the_budget(self):
        text = NEWLINE.join(
            ["【日程】" + "甲" * 900, "【情绪】" + "乙" * 900, "【记忆】" + "丙" * 900]
        )
        pages = self._pages(text, per_page=1000, max_pages=10)
        self.assertGreater(len(pages), 1)
        for page in pages:
            self.assertLessEqual(len(page), 1000, f"这一页超了：{len(page)} 字")
        joined = "".join(pages).replace(NEWLINE, "")
        for marker in ("甲" * 900, "乙" * 900, "丙" * 900):
            self.assertIn(marker, joined, "分页不该丢正文")

    def test_oversized_section_is_hard_cut_not_left_huge(self):
        text = "【AstrBot 可用对话模型 id】" + NEWLINE + "m" * 5000
        pages = self._pages(text, per_page=1000, max_pages=10)
        self.assertEqual(len(pages), 6, "5022 字按 1000 硬切 = 6 页")
        for page in pages:
            self.assertLessEqual(len(page), 1000)

    def test_tail_beyond_max_pages_is_dropped(self):
        text = NEWLINE.join(f"【第{index}节】" + "字" * 900 for index in range(8))
        pages = self._pages(text, per_page=1000, max_pages=3)
        self.assertEqual(len(pages), 3)


@unittest.skipIf(main_module is None, SKIP_REASON or "main.py 无法加载")
class DiagnoseConstantsTest(unittest.TestCase):
    def test_threshold_is_lower_than_the_text_budget(self):
        self.assertLess(
            main_module.DIAGNOSTICS_T2I_THRESHOLD,
            main_module.DIAGNOSTICS_OUTPUT_CHARS,
            "阈值必须落在纯文本预算之内，否则短报告也会被转成图",
        )

    def test_page_budget_is_sane(self):
        self.assertGreaterEqual(main_module.DIAGNOSTICS_T2I_MAX_PAGES, 1)
        self.assertGreater(
            main_module.DIAGNOSTICS_T2I_PAGE_CHARS, main_module.DIAGNOSTICS_T2I_THRESHOLD
        )

    def test_helpers_are_attached_to_the_plugin_class(self):
        plugin_cls = main_module.HumanoidCore
        self.assertTrue(hasattr(plugin_cls, "_weather_proxy"))
        self.assertTrue(hasattr(plugin_cls, "cmd_roles"), "「拟人 角色」指令要挂在插件类上")


class GlobalFallbackTest(unittest.IsolatedAsyncioTestCase):
    """面板留空 = 跟随官方；一个模型都没有时仍然要报「没有候选」。"""

    def build(self, ctx, conf=None):
        self.conf = conf or cfg()
        log = RecordingLogger()
        resolver = ProviderResolver(ctx, log)
        return LLMGateway(resolver, lambda: self.conf, log, FakeClock()), log

    async def test_empty_chain_follows_astrbot_default_model(self):
        glob = FakeProvider("global_one", reply="OK-GLOBAL")
        ctx = FakeContext(chat_providers=[], global_provider=glob)
        gateway, _ = self.build(ctx)
        result = await gateway.generate(prompt="hi", chain=(), allow_global=False, timeout=5)
        self.assertTrue(result.ok, result)
        self.assertEqual(result.label, GLOBAL_LABEL)
        self.assertEqual(result.provider_id, "global_one")

    async def test_no_default_model_is_not_guessed(self):
        """有模型注册、但 AstrBot 没指定默认模型时，插件不自己挑一个去调。"""
        provider = FakeProvider("only_one", reply="OK")
        ctx = FakeContext(chat_providers=[provider])
        gateway, _ = self.build(ctx)
        result = await gateway.generate(prompt="hi", chain=(), allow_global=False, timeout=5)
        self.assertFalse(result.ok)
        self.assertNotEqual(result.outcome, OUTCOME_NO_CANDIDATE)

    async def test_nothing_registered_still_reports_no_candidate(self):
        gateway, _ = self.build(FakeContext())
        result = await gateway.generate(prompt="hi", chain=(), allow_global=False, timeout=5)
        self.assertFalse(result.ok)
        self.assertEqual(result.outcome, OUTCOME_NO_CANDIDATE)


class SchemaKeysTest(unittest.TestCase):
    """两项新配置的形状，以及群聊情绪那条文案不再假装有总闸。"""

    def test_preset_role_ids_is_a_plain_text_field(self):
        item = _schema()["preset_role_ids"]
        self.assertEqual(item["type"], "string")
        self.assertEqual(item["default"], "")
        self.assertNotIn("condition", item, "这项跟天气无关，不该跟着别的开关显隐")

    def test_weather_proxy_follows_the_weather_master_switch(self):
        item = _schema()["weather_proxy"]
        self.assertEqual(item["type"], "string")
        self.assertEqual(item.get("default"), "")
        self.assertIs(item.get("condition", {}).get("weather_enabled"), True)

    def test_group_mood_hint_states_that_there_is_no_master_switch(self):
        hint = _schema()["mood_enabled_in_group"]["hint"]
        self.assertIn("读取所有人好感", hint)
        self.assertIn("不建档", hint)
        self.assertIn("私聊", hint)


if __name__ == "__main__":
    unittest.main()
