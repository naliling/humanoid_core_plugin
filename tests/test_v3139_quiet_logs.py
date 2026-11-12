"""v3.1.39：日志里那两条「看着像故障」的提示不再刷屏。

1. 状态体积告警：每个角色各有一份 StateStore，以前各报一次，日志里会出现 7 条
   一模一样的「状态文件已达 2014 KB」。现在同一档位全进程只报一次，体积翻倍
   （512 KB → 1 MB → 2 MB → 4 MB…）才再报一条。
2. 模型 id 告警：AstrBot 刚启动、模型列表还没注册好时，`available_ids()` 是空的。
   以前这种情况照样说「配置的 id 不存在」，用户看到的就是「明明有这个模型」。
   现在两种情形分开措辞，并且同一条话半小时内只说一次。
3. 顺带：面板里显示的是「供应商/模型」（如「娜莉灵/agnes-3.0-flash」），provider
   的 id 只有后半截时也要能解析到，不能因为写法报「不存在」。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from humanoid import llm as llm_module
from humanoid import state as state_module
from humanoid.config import HumanoidConfig
from humanoid.llm import LLMGateway, ProviderResolver
from humanoid.state import StateStore

from .fakes import FakeContext, FakeProvider, RecordingLogger


def _store(tmp: str, log: RecordingLogger) -> StateStore:
    return StateStore(Path(tmp) / "state.json", lambda: 1_000_000.0, log)


def _gateway(resolver: ProviderResolver, log: RecordingLogger) -> LLMGateway:
    return LLMGateway(resolver, lambda: HumanoidConfig.from_raw({}), log)


class VolumeWarningsAreProcessWideTest(unittest.TestCase):
    """体积告警：同一档位全进程一次，涨档才再报。"""

    def setUp(self) -> None:
        state_module.reset_volume_warnings()

    def test_seven_stores_warn_once(self) -> None:
        log = RecordingLogger()
        with tempfile.TemporaryDirectory() as tmp:
            for index in range(7):
                store = _store(str(Path(tmp) / str(index)), log)
                store._last_payload_bytes = 700 * 1024
                store._maybe_warn_volume()
        self.assertEqual(log.text("warning").count("状态文件已达"), 1)

    def test_warning_returns_when_volume_doubles(self) -> None:
        log = RecordingLogger()
        with tempfile.TemporaryDirectory() as tmp:
            first = _store(str(Path(tmp) / "a"), log)
            first._last_payload_bytes = 700 * 1024
            first._maybe_warn_volume()
            # 同一档位（512 KB~1 MB）再报一次也不行
            second = _store(str(Path(tmp) / "b"), log)
            second._last_payload_bytes = 900 * 1024
            second._maybe_warn_volume()
            self.assertEqual(log.text("warning").count("状态文件已达"), 1)
            # 涨到 2 MB 档：这是新信息，必须再报一条
            third = _store(str(Path(tmp) / "c"), log)
            third._last_payload_bytes = 3 * 1024 * 1024
            third._maybe_warn_volume()
            self.assertEqual(log.text("warning").count("状态文件已达"), 2)

    def test_slow_serialize_warns_once_per_process(self) -> None:
        log = RecordingLogger()
        with tempfile.TemporaryDirectory() as tmp:
            for index in range(5):
                store = _store(str(Path(tmp) / str(index)), log)
                store._last_payload_bytes = 100 * 1024
                store._last_serialize_seconds = 0.5
                store._maybe_warn_volume()
        self.assertEqual(log.text("warning").count("序列化耗时"), 1)


class ModelWarningTellsTheTruthTest(unittest.TestCase):
    """模型告警：拿不到列表 ≠ 配置写错；同一条话半小时内只说一次。"""

    def setUp(self) -> None:
        llm_module.reset_model_warnings()

    def test_empty_provider_list_is_not_reported_as_missing_config(self) -> None:
        log = RecordingLogger()
        gateway = _gateway(ProviderResolver(FakeContext()), log)
        gateway._warn_not_found("日程生成", "首选模型", "娜莉灵/agnes-3.0-flash", False)
        text = log.text("warning")
        self.assertIn("暂时读不到可用模型列表", text)
        self.assertNotIn("不存在", text)

    def test_same_warning_is_throttled(self) -> None:
        log = RecordingLogger()
        gateway = _gateway(ProviderResolver(FakeContext()), log)
        for _ in range(7):
            gateway._warn_not_found("日程生成", "首选模型", "娜莉灵/agnes-3.0-flash", False)
        self.assertEqual(log.text("warning").count("暂时读不到可用模型列表"), 1)

    def test_real_missing_id_lists_available_ones(self) -> None:
        log = RecordingLogger()
        resolver = ProviderResolver(FakeContext(chat_providers=[FakeProvider("agnes-3.0-flash")]))
        gateway = _gateway(resolver, log)
        gateway._warn_not_found("日程生成", "首选模型", "写错的-id", False)
        text = log.text("warning")
        self.assertIn("不存在", text)
        self.assertIn("agnes-3.0-flash", text)
        gateway._warn_not_found("日程生成", "首选模型", "写错的-id", False)
        self.assertEqual(log.text("warning").count("不存在"), 1)


class SlashIdResolvesTest(unittest.TestCase):
    """「供应商/模型」写法：provider id 只有后半截时也要能解析到。"""

    def test_resolves_by_tail_after_slash(self) -> None:
        chat = FakeProvider("agnes-3.0-flash")
        resolver = ProviderResolver(FakeContext(chat_providers=[chat]))
        self.assertIs(resolver.resolve("娜莉灵/agnes-3.0-flash"), chat)

    def test_resolves_by_head_before_slash(self) -> None:
        chat = FakeProvider("娜莉灵")
        resolver = ProviderResolver(FakeContext(chat_providers=[chat]))
        self.assertIs(resolver.resolve("娜莉灵/agnes-3.0-flash"), chat)

    def test_unknown_slash_id_is_still_none(self) -> None:
        resolver = ProviderResolver(FakeContext(chat_providers=[FakeProvider("agnes-3.0-flash")]))
        self.assertIsNone(resolver.resolve("别的供应商/别的模型"))
