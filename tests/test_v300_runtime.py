"""v3.0.0 运行时/兼容/跨插件联动回归（C 域：P1-1/P1-6/联动 P1-B/P1-C 等）。

覆盖：

* P1-1  框架兼容能力探测：没有 astrbot.core.agent.message / mark_as_temp 时插件仍能
        加载，注入降级到 req.contexts / system_prompt 而不是静默丢失；
* P1-6  跨插件记忆调用有超时（读 0.5s / 写 1s 上限），对面卡住不拖死回复；
* P2-24 记忆桥接短 TTL 重解析：对面后加载/热重载能自愈；last_error 进 /拟人 诊断；
* P1-B/P2-03 门面绑定插件实例代次；旧实例 terminate 不清新实例登记；
* P1-C/P2-26 contract_enabled 与 soma_enabled 解耦；
* P2-02/P2-01 initialize 失败回滚、terminate try/finally；
* P2-06/P2-07 契约重建缓存闸、tick 按 monotonic 截止时刻调度；
* P2-08 兄弟 bot 角色集合走进程内缓存；
* P2-11 /拟人 重载 从磁盘读配置（有 config_path 时）；
* P2-12 输入状态最小间隔下限 + 单条回复帧数上限；
* P2-21 门面读路径不建 mood 档案；
* P2-25 契约 paths 覆盖 social 实际读取的用户级键、能力词表可自证；
* P2-27 多 Bot 分片缺失返回「不知道」而不是「全结了」；
* P2-28 版本自述用真实 __version__；
* P2-32 状态落盘可观测性；
* 联动 P2-2/P2-3 relationship 全 task、TA 说话/她说话分开记；
* 联动 P2-5 core 写 user 级 name/base_affection。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from humanoid import __version__ as humanoid_version
from humanoid import api, link, memory_link
from humanoid.api import CAPABILITIES, API_VERSION, CoreFacade
from humanoid.config import HumanoidConfig
from humanoid.diagnostics import _memory_link_line, build_report, state_size_lines
from humanoid.engine import HumanoidEngine
from humanoid.input_state import InputStateNotifier
from humanoid.link import (
    CONTRACT_CAPABILITIES,
    CONTRACT_VERSION,
    SocialSignals,
    build_contract,
)
from humanoid.llm import CallGate, LLMGateway, ProviderResolver
from humanoid.role_manager import RoleManager
from humanoid.state import StateStore

from .fakes import FakeContext, FakeProvider, RecordingLogger
from .test_link import FROZEN, Harness, freeze

try:
    from .test_main_integration import (
        GOOD_SCHEDULE,
        SKIP_REASON,
        FakeEvent,
        FakeProviderRequest,
        injected_text,
        main_module,
    )
except Exception as _exc:  # pragma: no cover - 取决于环境
    main_module = None
    SKIP_REASON = f"无法加载 main.py：{_exc}"
    FakeEvent = FakeProviderRequest = None  # type: ignore[assignment]
    injected_text = GOOD_SCHEDULE = None  # type: ignore[assignment]


# ────────────────────────── 工具 ──────────────────────────


class _QuietResolver:
    def available_ids(self):
        return []

    def resolve(self, provider_id):
        return None

    def resolve_global(self, _umo=None):
        return None

    def id_of(self, provider):
        return ""


class _QuietGateway:
    gate = None

    def cooldowns(self, purpose):
        return {}

    def cooldown_remaining(self, provider_id, purpose):
        return 0.0

    def last_result(self, purpose):
        return None


def _fresh_plugin(raw=None):
    """起一个真的 HumanoidCore（数据目录在临时目录里）。"""
    tmp = Path(tempfile.mkdtemp())
    main_module.get_astrbot_data_path = lambda: str(tmp)
    ctx = FakeContext(chat_providers=[FakeProvider("p1", reply=GOOD_SCHEDULE)])
    conf = {
        "timezone_city": "北京",
        "schedule_provider_name": "p1",
        "admin_qq": ["10001"],
        "state_flush_interval_seconds": 1,
    }
    conf.update(raw or {})
    return main_module.HumanoidCore(ctx, conf), tmp


def _simple_req(system_prompt: str = "", contexts=None, extra=None):
    req = SimpleNamespace()
    req.system_prompt = system_prompt
    req.contexts = list(contexts) if contexts is not None else []
    if extra is not None:
        req.extra_user_content_parts = extra
    return req


# ────────────────────────── P1-1 框架兼容 ──────────────────────────


@unittest.skipIf(main_module is None, SKIP_REASON)
class InjectionFallbackTest(unittest.TestCase):
    """注入通道的三级降级：临时块 → req.contexts → system_prompt。

    旧实现顶层裸 import TextPart + 无条件 mark_as_temp()：4.0-4.5 插件加载即崩，
    4.6-4.23 每轮抛 AttributeError 被吞成一条 warning，身体/状态注入整块静默丢失。
    """

    def setUp(self):
        self._saved_text_part = main_module.TextPart

    def tearDown(self):
        main_module.TextPart = self._saved_text_part

    def test_temp_parts_used_when_supported(self):
        req = _simple_req(extra=[])
        channel = main_module._append_injected_parts(req, ["甲", "乙"])
        self.assertEqual(channel, "temp_parts")
        self.assertEqual([p.text for p in req.extra_user_content_parts], ["甲", "乙"])
        self.assertEqual(req.contexts, [], "有临时块时不该再往 contexts 里塞")
        for part in req.extra_user_content_parts:
            self.assertTrue(getattr(part, "_no_save", False), "临时块必须标记不落历史")

    def test_falls_back_to_contexts_when_mark_as_temp_is_missing(self):
        """4.6-4.23：TextPart 存在但没有 mark_as_temp。"""

        class OldTextPart:
            def __init__(self, text: str = "") -> None:
                self.text = text

        main_module.TextPart = OldTextPart
        req = _simple_req(extra=[])
        channel = main_module._append_injected_parts(req, ["此刻的她"])
        self.assertEqual(channel, "contexts")
        self.assertEqual(req.extra_user_content_parts, [], "没有 mark_as_temp 不能用临时块")
        self.assertEqual(len(req.contexts), 1)
        self.assertEqual(req.contexts[0]["role"], "user")
        self.assertIn("此刻的她", req.contexts[0]["content"])
        self.assertIn("不是用户发来的消息", req.contexts[0]["content"])

    def test_falls_back_to_contexts_when_text_part_is_none(self):
        """4.0-4.5：整个模块都没有。"""
        main_module.TextPart = None
        req = _simple_req(extra=[])
        channel = main_module._append_injected_parts(req, ["她此刻在书房"])
        self.assertEqual(channel, "contexts")
        self.assertIn("她此刻在书房", req.contexts[0]["content"])

    def test_falls_back_to_system_prompt_without_contexts(self):
        main_module.TextPart = None
        req = _simple_req(system_prompt="人设", contexts=None)
        req.contexts = "坏值（不是 list）"
        channel = main_module._append_injected_parts(req, ["事实"])
        self.assertEqual(channel, "system_prompt")
        self.assertIn("人设", req.system_prompt)
        self.assertIn("事实", req.system_prompt)

    def test_no_channel_reports_empty_instead_of_raising(self):
        main_module.TextPart = None
        req = SimpleNamespace(system_prompt=None, contexts=None)
        self.assertEqual(main_module._append_injected_parts(req, ["x"]), "")
        self.assertEqual(main_module._append_injected_parts(req, ["   "]), "")


@unittest.skipIf(main_module is None, SKIP_REASON)
class ImportCompatTest(unittest.TestCase):
    """屏蔽 astrbot.core.agent.message / astrbot_path 后 import main 与注入仍正常。

    用 importlib.reload 在同一个模块对象上重跑 main.py，测完再 reload 回去：比起子进程
    快，也不会把插件包重复 import 成两份。
    """

    def test_import_and_injection_survive_a_framework_without_message_module(self):
        agent_pkg = sys.modules.get("astrbot.core.agent")
        agent_msg = sys.modules.get("astrbot.core.agent.message")
        path_pkg = sys.modules.get("astrbot.core.utils")
        path_mod = sys.modules.get("astrbot.core.utils.astrbot_path")
        saved_msg_attr = getattr(agent_pkg, "message", None) if agent_pkg else None
        saved_path_attr = getattr(path_pkg, "astrbot_path", None) if path_pkg else None
        try:
            # 模拟 4.0-4.5：两个内部模块都不存在。
            sys.modules.pop("astrbot.core.agent.message", None)
            if agent_pkg is not None and hasattr(agent_pkg, "message"):
                delattr(agent_pkg, "message")
            sys.modules.pop("astrbot.core.utils.astrbot_path", None)
            if path_pkg is not None and hasattr(path_pkg, "astrbot_path"):
                delattr(path_pkg, "astrbot_path")

            reloaded = importlib.reload(main_module)
            self.assertIsNone(reloaded.TextPart, "没有 message 模块时 TextPart 必须是 None")
            self.assertIsNone(
                reloaded.get_astrbot_data_path,
                "没有 astrbot_path 时数据目录导入必须降级而不是让插件加载失败",
            )
            # 数据目录三级回退不能抛。
            self.assertIsInstance(reloaded._resolve_data_root(None), Path)
            # 注入降级：没有临时块 API，但注入不能丢。
            req = _simple_req(extra=[])
            channel = reloaded._append_injected_parts(req, ["她此刻在书房"])
            self.assertEqual(channel, "contexts")
            self.assertIn("她此刻在书房", req.contexts[0]["content"])
        finally:
            if agent_msg is not None:
                sys.modules["astrbot.core.agent.message"] = agent_msg
                if agent_pkg is not None:
                    agent_pkg.message = agent_msg
            if path_mod is not None:
                sys.modules["astrbot.core.utils.astrbot_path"] = path_mod
                if path_pkg is not None:
                    path_pkg.astrbot_path = path_mod
            importlib.reload(main_module)
        self.assertIsNotNone(main_module.TextPart, "测试结束必须把 stub 恢复回去")

    def test_injection_end_to_end_with_contexts_fallback(self):
        """整条链路：屏蔽临时块 API 后，一轮 on_llm_request 仍把事实注进去。"""
        agent_pkg = sys.modules.get("astrbot.core.agent")
        agent_msg = sys.modules.get("astrbot.core.agent.message")
        saved_msg_attr = getattr(agent_pkg, "message", None) if agent_pkg else None
        tmp = Path(tempfile.mkdtemp())
        try:
            sys.modules.pop("astrbot.core.agent.message", None)
            if agent_pkg is not None and hasattr(agent_pkg, "message"):
                delattr(agent_pkg, "message")
            reloaded = importlib.reload(main_module)
            reloaded.get_astrbot_data_path = lambda: str(tmp)
            ctx = FakeContext(chat_providers=[FakeProvider("p1", reply=GOOD_SCHEDULE)])
            star = reloaded.HumanoidCore(ctx, {
                "timezone_city": "北京",
                "schedule_provider_name": "p1",
                "state_flush_interval_seconds": 1,
            })

            async def run():
                await star.initialize()
                try:
                    event = FakeEvent("今天在忙什么", sender="10002", self_id="99999")
                    req = FakeProviderRequest("")  # 有 contexts，字段列表是 stub 的
                    await star.inject_context(event, req)
                    return req
                finally:
                    await star.terminate()

            req = asyncio.run(run())
            # 没有临时块 API 时，事实进了本轮 contexts，而不是静默消失。
            self.assertTrue(req.contexts, "注入消失：contexts 里必须有事实块")
            self.assertTrue(injected_text(req).strip(), "system_prompt + 注入不能是空的")
            self.assertIn("事实", str(req.contexts[0].get("content", "")))
        finally:
            if agent_msg is not None:
                sys.modules["astrbot.core.agent.message"] = agent_msg
                if agent_pkg is not None:
                    agent_pkg.message = agent_msg
            importlib.reload(main_module)


# ────────────────────────── P1-6 跨插件超时 ──────────────────────────


class _SlowBridge:
    """对面卡住的桥接：compose_context / record_external_memory 都 sleep 5 秒。"""

    def __init__(self) -> None:
        self.read_calls = 0
        self.write_calls = 0

    async def compose_context(self, **_kw):
        self.read_calls += 1
        await asyncio.sleep(5.0)
        return "迟到的记忆"

    async def record_external_memory(self, **_kw):
        self.write_calls += 1
        await asyncio.sleep(5.0)


class MemoryTimeoutTest(unittest.IsolatedAsyncioTestCase):
    """P1-6：注入挂在每条消息的 LLM 请求钩子上，跨插件 await 必须有上限。"""

    def setUp(self):
        memory_link.clear_cache()
        memory_link._LAST_ERROR = ""

    def tearDown(self):
        memory_link.clear_cache()
        memory_link._LAST_ERROR = ""

    async def test_compose_context_timeout_returns_empty_without_raising(self):
        bridge = _SlowBridge()
        started = time.monotonic()
        text = await memory_link.fetch_memory_text(
            bridge, "aiocqhttp:private:1", "42", query="你好", timeout=0.05
        )
        elapsed = time.monotonic() - started
        self.assertEqual(text, "")
        self.assertLess(elapsed, 1.5, f"超时没有生效，等了 {elapsed:.2f}s")
        self.assertEqual(bridge.read_calls, 1)
        self.assertIn("timeout", memory_link.last_error())
        self.assertIn("compose_context", memory_link.last_error())
        self.assertEqual(memory_link._READ_CACHE, {}, "超时结果不能被缓存成「没有记忆」")

    async def test_record_external_memory_timeout_returns_without_raising(self):
        bridge = _SlowBridge()
        started = time.monotonic()
        await memory_link.write_life_event(
            bridge, "42", "她做完了写方案", umo="aiocqhttp:private:1", timeout=0.05
        )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.5, f"写回超时没有生效，等了 {elapsed:.2f}s")
        self.assertEqual(bridge.write_calls, 1)
        self.assertIn("timeout", memory_link.last_error())

    async def test_default_timeouts_are_bounded(self):
        """默认值就带上限：0.5s / 1.0s，且调用方传再大也被夹到 2s。"""
        self.assertEqual(memory_link.READ_TIMEOUT_SECONDS, 0.5)
        self.assertEqual(memory_link.WRITE_TIMEOUT_SECONDS, 1.0)
        self.assertEqual(memory_link._clamp_timeout(99.0, 0.5), memory_link.MAX_TIMEOUT_SECONDS)
        self.assertEqual(memory_link._clamp_timeout(-1, 0.5), 0.5)
        self.assertEqual(memory_link._clamp_timeout("坏值", 0.5), 0.5)
        self.assertLessEqual(memory_link.MAX_TIMEOUT_SECONDS, 2.0)

    async def test_plugin_injection_still_completes_when_the_bridge_hangs(self):
        """整链路：记忆桥接卡 5s，注入钩子仍要在配置上限内完成并带上状态。"""
        if main_module is None:  # pragma: no cover
            self.skipTest(SKIP_REASON or "main.py 不可用")
        star, _tmp = _fresh_plugin({"memory_bridge_timeout_seconds": 0.2})
        bridge = _SlowBridge()
        star._memory_bridge_obj = bridge
        star._memory_bridge_resolved = True
        star._memory_bridge_checked_at = time.monotonic()

        async def run():
            await star.initialize()
            try:
                event = FakeEvent("在吗", sender="10001", self_id="99999")
                req = FakeProviderRequest("")
                started = time.monotonic()
                await star.inject_context(event, req)
                return req, time.monotonic() - started
            finally:
                await star.terminate()

        req, elapsed = await run()
        self.assertLess(elapsed, 2.0, f"记忆桥接卡住把注入拖了 {elapsed:.2f}s")
        self.assertTrue(injected_text(req).strip(), "记忆超时不该让注入整块消失")


# ────────────────────────── P2-24 桥接 TTL 自愈 ──────────────────────────


class MemoryBridgeTtlTest(unittest.TestCase):
    """桥接解析带短 TTL：对面后加载/热重载后要能自己接上，失败不许永久负缓存。"""

    def _plugin(self):
        obj = main_module.HumanoidCore.__new__(main_module.HumanoidCore)
        obj._memory_bridge_obj = None
        obj._memory_bridge_resolved = False
        obj._memory_bridge_checked_at = 0.0
        obj.context = SimpleNamespace(get_all_stars=lambda: [])
        return obj

    def test_late_loaded_plugin_is_picked_up_after_the_retry_window(self):
        bridge = object()
        calls = []

        def finder(_ctx):
            calls.append(1)
            return bridge if len(calls) > 1 else None

        # 注意：main.py 以插件包名导入 humanoid（与测试里的顶层 humanoid 是两份模块对象），
        # 所以要打插件自己那份 memory_link 的补丁。
        with mock.patch.object(
            main_module.memory_link, "find_memory_bridge", side_effect=finder
        ):
            obj = self._plugin()
            self.assertIsNone(obj._memory_bridge(), "第一次对面还没就绪")
            self.assertEqual(len(calls), 1)
            self.assertIsNone(obj._memory_bridge(), "60 秒内不该反复解析")
            self.assertEqual(len(calls), 1, "失败后的负缓存窗口内不该重试")
            # 模拟过了重试窗口：对面后来加载好了。
            obj._memory_bridge_checked_at -= obj.MEMORY_BRIDGE_RETRY_SECONDS + 1.0
            self.assertIs(obj._memory_bridge(), bridge, "对面后加载后必须能自愈")
            self.assertEqual(len(calls), 2)

    def test_successful_bridge_is_cached_until_ttl(self):
        bridge = object()
        calls = []

        def finder(_ctx):
            calls.append(1)
            return bridge

        with mock.patch.object(
            main_module.memory_link, "find_memory_bridge", side_effect=finder
        ):
            obj = self._plugin()
            self.assertIs(obj._memory_bridge(), bridge)
            self.assertIs(obj._memory_bridge(), bridge)
            self.assertEqual(len(calls), 1, "成功解析后应在 TTL 内复用")
            obj._memory_bridge_checked_at -= obj.MEMORY_BRIDGE_TTL_SECONDS + 1.0
            self.assertIs(obj._memory_bridge(), bridge)
            self.assertEqual(len(calls), 2, "TTL 到了要重新问一次（热重载会换新桥接）")

    def test_resolve_exception_does_not_poison_forever(self):
        bridge = object()
        calls = []

        def finder(_ctx):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("注册表坏了")
            return bridge

        with mock.patch.object(
            main_module.memory_link, "find_memory_bridge", side_effect=finder
        ):
            obj = self._plugin()
            self.assertIsNone(obj._memory_bridge())
            obj._memory_bridge_checked_at -= obj.MEMORY_BRIDGE_RETRY_SECONDS + 1.0
            self.assertIs(obj._memory_bridge(), bridge)


class MemoryLastErrorDiagnosticsTest(unittest.TestCase):
    def tearDown(self):
        memory_link._LAST_ERROR = ""

    def test_timeout_hint_is_visible_in_diagnostics(self):
        memory_link._LAST_ERROR = "timeout: compose_context() 超过 0.5s 未返回"
        text = _memory_link_line()
        self.assertIn("长期记忆联动", text)
        self.assertIn("超时", text)
        self.assertIn("不会卡住回复", text)

    def test_no_failure_says_so(self):
        memory_link._LAST_ERROR = ""
        self.assertIn("没有失败记录", _memory_link_line())


# ────────────────────────── P1-B / P2-03 门面生命周期 ──────────────────────────


class FacadeLifecycleTest(unittest.TestCase):
    """旧门面必须在插件换实例后立刻失效；旧实例 terminate 不许清新实例登记。"""

    def setUp(self):
        self.harness = Harness()
        self.core = self.harness.roles.get_or_create("bot1")
        self._saved_globals = (
            main_module._ACTIVE_PLUGIN, main_module._FACADE, main_module._PLUGIN_GENERATION,
        )

    def tearDown(self):
        (
            main_module._ACTIVE_PLUGIN,
            main_module._FACADE,
            main_module._PLUGIN_GENERATION,
        ) = self._saved_globals
        self.harness.store.flush_sync()

    def _plugin_stub(self, generation):
        return SimpleNamespace(role_manager=self.harness.roles, _generation=generation)

    def test_facade_is_current_for_the_registered_instance(self):
        plugin = self._plugin_stub(7)
        main_module._ACTIVE_PLUGIN = plugin
        main_module._FACADE = None
        facade = main_module.get_core_facade()
        self.assertIsNotNone(facade)
        self.assertTrue(facade.is_current())
        self.assertEqual(facade.role_ids(), ["bot1"])
        self.assertEqual(facade.api_version(), API_VERSION)
        self.assertIsNotNone(facade.brief("bot1", "10001", task="proactive"))

    def test_old_facade_goes_dead_after_the_plugin_instance_changes(self):
        old_plugin = self._plugin_stub(7)
        main_module._ACTIVE_PLUGIN = old_plugin
        main_module._FACADE = None
        old_facade = main_module.get_core_facade()
        self.assertTrue(old_facade.is_current())

        new_plugin = self._plugin_stub(8)
        main_module._ACTIVE_PLUGIN = new_plugin
        main_module._FACADE = None
        # 旧门面（别的插件缓存着它）每次调用都要能看出失效。
        self.assertFalse(old_facade.is_current())
        self.assertIsNone(old_facade.brief("bot1", "10001"))
        self.assertEqual(old_facade.clock("bot1"), {})
        self.assertEqual(old_facade.events("bot1"), [])
        self.assertEqual(old_facade.role_ids(), [])
        self.assertEqual(old_facade.capabilities(), [])
        self.assertIsNone(old_facade.api_version())
        self.assertEqual(old_facade.state_fingerprint("bot1"), "")
        # 新的取用方拿到的是新实例的门面。
        new_facade = main_module.get_core_facade()
        self.assertIsNot(new_facade, old_facade)
        self.assertTrue(new_facade.is_current())


@unittest.skipIf(main_module is None, SKIP_REASON)
class FacadeInstanceLifecycleTest(unittest.IsolatedAsyncioTestCase):
    """P2-03：重载时新旧实例短暂共存，旧实例 terminate 不许清新实例的登记。"""

    async def test_old_terminate_does_not_clear_the_new_registration(self):
        first, _tmp1 = _fresh_plugin()
        await first.initialize()
        second, _tmp2 = _fresh_plugin()
        await second.initialize()
        try:
            self.assertIs(main_module._ACTIVE_PLUGIN, second)
            self.assertIsNotNone(main_module.get_core_facade())
            await first.terminate()  # 旧实例的清理
            self.assertIs(main_module._ACTIVE_PLUGIN, second, "旧实例不能清掉新实例登记")
            facade = main_module.get_core_facade()
            self.assertIsNotNone(facade, "旧实例 terminate 之后新门面必须仍然可用")
            self.assertTrue(facade.is_current())
        finally:
            await second.terminate()
        self.assertIsNone(main_module._ACTIVE_PLUGIN)


# ────────────────────────── P1-C / P2-26 契约门禁 ──────────────────────────


class ContractGatingTest(unittest.TestCase):
    """contract_enabled=false 才整份停发；soma_enabled=false 只让身体块变中性。"""

    def test_contract_survives_soma_disabled_with_neutral_body(self):
        harness = Harness({"timezone_city": "北京", "soma_enabled": False})
        core = harness.roles.get_or_create("bot1")
        try:
            contract = core.refresh_contract()
            self.assertIsNotNone(contract)
            self.assertIs(core._scope.get_self("contract"), contract)
            for key in ("identity", "day", "outfit", "routine", "weather", "holiday",
                        "persona", "time", "wake", "event", "ongoing", "wants"):
                self.assertIn(key, contract, f"非身体块 {key} 不该因身体轴关闭而消失")
            body = contract.get("body") or {}
            self.assertEqual(set(body), {
                "energy", "energy_text", "sleep_pressure", "sleep_debt", "hunger",
                "discomfort", "arousal", "asleep", "last_sleep_hours",
                "social_energy", "social_desire", "cycle_day", "cycle_phase",
            })
            self.assertEqual(body["sleep_pressure"], 0.0)
            self.assertEqual(body["social_desire"], 50.0)
            self.assertFalse(body["asleep"])
        finally:
            harness.store.flush_sync()

    def test_contract_disabled_stops_the_whole_contract(self):
        harness = Harness({"timezone_city": "北京", "contract_enabled": False})
        core = harness.roles.get_or_create("bot1")
        try:
            self.assertIsNone(core.refresh_contract())
            self.assertIsNone(core._scope.get_self("contract"))
        finally:
            harness.store.flush_sync()


# ────────────────────────── 联动 P2-2 / P2-3 / P2-21 门面读路径 ──────────────────────────


class FacadeRelationshipTest(unittest.TestCase):
    """relationship 对所有 task 返回；TA 说话 / 她说话分开记；读路径不建 mood 档案。"""

    def setUp(self):
        self.harness = Harness()
        self.core = self.harness.roles.get_or_create("bot1")
        self.facade = CoreFacade(self.harness.roles)

    def tearDown(self):
        self.harness.store.flush_sync()

    def test_relationship_is_returned_for_every_task(self):
        for task in ("chat", "proactive", "status"):
            brief = self.facade.brief("bot1", "10001", task=task)
            self.assertIsNotNone(brief, task)
            self.assertIn("relationship", brief, f"{task} 缺 relationship（联动 P2-2）")
            self.assertIn("turns", brief["relationship"], task)

    def test_proactive_relationship_carries_turns(self):
        self.core.scope.set_user("10001", "nickname", "阿哲")
        try:
            self.core.mood.profile("10001")  # 已有档案：模拟聊过的人
        except Exception:
            pass
        brief = self.facade.brief("bot1", "10001", task="proactive")
        rel = brief.get("relationship") or {}
        self.assertEqual(rel.get("nickname"), "阿哲")
        self.assertGreaterEqual(int(rel.get("turns") or 0), 0)

    def test_user_block_exposes_peer_and_her_said_at(self):
        self.core.scope.set_user("10001", "last_interaction", 111.0)
        self.core.scope.set_user("10001", "peer_said_at", 111.0)
        self.core.scope.set_user("10001", "her_said_at", 222.0)
        user = self.facade.brief("bot1", "10001", task="proactive")["user"]
        self.assertEqual(user["peer_said_at"], 111.0)
        self.assertEqual(user["her_said_at"], 222.0)
        rel = self.facade.brief("bot1", "10001", task="status")["relationship"]
        self.assertEqual(rel["peer_said_at"], 111.0)
        self.assertEqual(rel["her_said_at"], 222.0)

    def test_facade_read_does_not_create_a_mood_profile(self):
        """P2-21：门面自称只读，读一眼不能给陌生人建档案并 mark_dirty。"""
        uid = "424242"
        self.assertIsNone(self.core.scope.peek_user(uid, "mood"))
        # intimacy() 对没有记录的人返回「确实还没有」的确定答案（只读快照），
        # 关键是它不能顺手建 mood 档案。
        self.assertIsInstance(self.facade.intimacy("bot1", uid), dict)
        self.facade.brief("bot1", uid, task="proactive")
        self.facade.brief("bot1", uid, task="status")
        self.facade.state_fingerprint("bot1")
        self.assertIsNone(
            self.core.scope.peek_user(uid, "mood"),
            "门面读路径给陌生人建了 mood 档案（P2-21 回归）",
        )
        self.assertIsNone(self.core.scope.peek_user(uid, "last_interaction"))


class LastInteractionSplitTest(unittest.IsolatedAsyncioTestCase):
    """v3.0.0（联动 P2-3）：last_interaction = TA 上次说话；她说话记 her_said_at。"""

    def setUp(self):
        self.harness = Harness()
        self.core = self.harness.roles.get_or_create("bot1")

    def tearDown(self):
        self.harness.store.flush_sync()

    async def test_peer_message_advances_both_peer_fields_not_her_field(self):
        self.core.on_message("u9", "你好", is_group=False)
        last = float(self.core.scope.get_user("u9", "last_interaction", 0.0) or 0.0)
        peer = float(self.core.scope.get_user("u9", "peer_said_at", 0.0) or 0.0)
        her = float(self.core.scope.get_user("u9", "her_said_at", 0.0) or 0.0)
        self.assertGreater(last, 0.0)
        self.assertEqual(last, peer, "TA 说话时 last_interaction 与 peer_said_at 同刻")
        self.assertEqual(her, 0.0)

    async def test_her_words_advance_her_said_at_without_touching_last_interaction(self):
        self.core.scope.set_user("u9", "last_interaction", 1000.0)
        self.core.scope.set_user("u9", "peer_said_at", 1000.0)
        self.core.note_her_words("u9", "嗯，我在", is_group=False, now=2000.0)
        her = float(self.core.scope.get_user("u9", "her_said_at", 0.0) or 0.0)
        last = float(self.core.scope.get_user("u9", "last_interaction", 0.0) or 0.0)
        self.assertEqual(her, 2000.0)
        self.assertEqual(last, 1000.0, "她说话不能推进「TA 上次说话」")


class UserFallbackFieldsTest(unittest.TestCase):
    """联动 P2-5：core 写 user 级 name / base_affection，social 兜底分支才可用。"""

    def setUp(self):
        self.harness = Harness()
        self.core = self.harness.roles.get_or_create("bot1")

    def tearDown(self):
        self.harness.store.flush_sync()

    def test_mirror_writes_the_two_fallback_fields(self):
        uid = "7001"
        self.core.mood.profile(uid)  # 让档案存在（base_affection 才有来源）
        base = float(self.core.scope.peek_user(uid, "mood").get("base_affection"))
        self.core._mirror_social_fallback_fields(uid, "★阿哲 2 号")
        self.assertEqual(self.core.scope.peek_user(uid, "name"), "★阿哲 2 号")
        self.assertAlmostEqual(
            float(self.core.scope.peek_user(uid, "base_affection")), base, places=2
        )

    def test_mirror_is_idempotent_and_skips_unknown_users_mood(self):
        uid = "7002"
        self.core._mirror_social_fallback_fields(uid, "阿哲")
        self.assertEqual(self.core.scope.peek_user(uid, "name"), "阿哲")
        self.assertIsNone(
            self.core.scope.peek_user(uid, "base_affection"),
            "没有 mood 档案时不该补 base_affection（不为陌生人建档）",
        )
        dirty_before = self.harness.store.dirty
        self.core._mirror_social_fallback_fields(uid, "阿哲")
        self.assertEqual(self.harness.store.dirty, dirty_before, "值没变不该再标脏")


# ────────────────────────── P2-27 多 Bot 分片 ──────────────────────────


class MultiBotOpenItemsTest(unittest.TestCase):
    """本角色分片缺失时返回「不知道」(None)，不能拿顶层 [] 把账本清空。"""

    def _signals(self, payload):
        sig = SocialSignals.__new__(SocialSignals)
        sig._payload = payload
        sig.read = lambda: sig._payload
        return sig

    def test_missing_shard_returns_none_not_empty_list(self):
        sig = self._signals({
            "open_items": [],
            "by_bid": {"A": {"open_items": [{"text": "整理个案"}]}},
        })
        self.assertIsNone(sig.open_items("B"), "分片缺失必须返回 None（不知道），不是 []")
        self.assertEqual(
            [item.get("text") for item in sig.open_items("A") or []], ["整理个案"]
        )

    def test_single_bot_top_level_still_read(self):
        sig = self._signals({"open_items": [{"text": "写方案"}]})
        items = sig.open_items("") or []
        self.assertEqual([item.get("text") for item in items], ["写方案"])

    def test_ingest_does_not_clear_ledger_when_shard_is_missing(self):
        from humanoid import life_log

        harness = Harness()
        core = harness.roles.get_or_create("bot1")
        try:
            existing = [{"text": "她答应过的事", "uid": "u1", "at": 1.0, "src": "test"}]
            core._scope.set_self(life_log.LIFE_KEY, existing)
            with mock.patch.object(core.signals, "is_fresh", return_value=True), \
                    mock.patch.object(core.signals, "open_items", return_value=None):
                core._ingest_open_items()
            kept = core._scope.get_self(life_log.LIFE_KEY)
            self.assertEqual(kept, existing, "分片缺失时把她的未完事项账本清掉了")
        finally:
            harness.store.flush_sync()


# ────────────────────────── P2-25 契约 paths / 能力词表 ──────────────────────────


class ContractPathsTest(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.core = harness_core = self.harness.roles.get_or_create("bot1")
        freeze(harness_core)

    def tearDown(self):
        self.harness.store.flush_sync()

    def test_paths_cover_the_user_keys_social_reads(self):
        contract = build_contract(self.core)
        paths = contract.get("paths") or {}
        for key in (
            "user_affection", "user_base_affection", "user_nickname", "user_display_name",
            "user_name", "user_nickname_src", "user_mood_tag", "user_mood",
            "user_mood_libido", "user_mood_base_libido", "user_mood_turn_count",
            "user_attention", "user_last_message", "user_first_met",
            "user_last_interaction", "user_said",
            "user_peer_said_at", "user_her_said_at",
            "user_ccb_done_count", "user_ccb_last_at", "user_ccb_satisfy",
            "user_ccb_stage", "user_ccb_turns", "user_ccb_aftercare_until",
        ):
            self.assertIn(key, paths, f"契约 paths 缺 social 实际读取的 {key}")
        self.assertGreaterEqual(len(paths), 18)

    def test_contract_capabilities_all_have_a_block(self):
        contract = build_contract(self.core)
        mapping = {
            "dreams": ("day", "dreams"),
            "wake": ("wake",),
            "event": ("event",),
            "identity": ("identity",),
            "outfit": ("outfit",),
            "routine": ("routine",),
            "weather": ("weather",),
            "holiday": ("holiday",),
            "persona": ("persona",),
        }
        for name in CONTRACT_CAPABILITIES:
            path = mapping.get(name, (name,))
            node = contract
            for key in path:
                self.assertIsInstance(node, dict, f"{name} 的路径不对")
                self.assertIn(key, node, f"能力 {name} 在契约里没有对应块")
                node = node[key]

    def test_facade_capabilities_are_backed_by_methods_or_brief_blocks(self):
        facade = CoreFacade(self.harness.roles)
        brief = facade.brief("bot1", "10001", task="proactive") or {}
        methods = {
            "fingerprint": "state_fingerprint",
        }
        blocks = {
            "dreams": ("day", "dreams"),
            "wake": ("wake",),
            "identity": ("her",),
            "user": ("user",),
        }
        for name in CAPABILITIES:
            method = methods.get(name, name)
            if callable(getattr(CoreFacade, method, None)):
                continue
            path = blocks.get(name)
            self.assertIsNotNone(path, f"门面能力 {name} 既没有方法也没有 brief 块")
            node = brief
            for key in path:
                self.assertIsInstance(node, dict, f"能力 {name} 的 brief 路径不对")
                self.assertIn(key, node, f"能力 {name} 声明的 brief 块不存在")
                node = node[key]

    def test_contract_core_version_uses_the_real_package_version(self):
        contract = build_contract(self.core)
        self.assertEqual(contract["core_version"], humanoid_version)
        self.assertEqual(contract["v"], CONTRACT_VERSION)


# ────────────────────────── P2-06 / P2-07 tick 成本与节律 ──────────────────────────


class ContractRebuildCacheTest(unittest.TestCase):
    """P2-06：契约输入摘要没变就不重算整份；变了立刻重建。"""

    def setUp(self):
        self.harness = Harness()
        self.core = self.harness.roles.get_or_create("bot1")
        freeze(self.core)

    def tearDown(self):
        self.harness.store.flush_sync()

    def test_second_refresh_with_same_inputs_reuses_the_cached_contract(self):
        import humanoid.core_instance as ci

        with mock.patch.object(ci, "build_contract", wraps=ci.build_contract) as build:
            first = self.core.refresh_contract()
            self.assertIsNotNone(first)
            self.assertEqual(build.call_count, 1)
            second = self.core.refresh_contract()
            self.assertIs(second, first, "输入没变时该复用同一份契约")
            self.assertEqual(build.call_count, 1, "输入没变却重算了整份契约（P2-06 回归）")
            # 输入真的变了（精力/社交能量）→ 立刻重建。
            self.core._scope.set_self("social_energy", 12.0)
            third = self.core.refresh_contract()
            self.assertIsNotNone(third)
            self.assertEqual(build.call_count, 2, "输入变了必须重建")

    def test_digest_is_cheap_and_tolerates_broken_services(self):
        self.assertIsInstance(self.core._contract_input_digest(), tuple)
        with mock.patch.object(self.core.weather, "snapshot", side_effect=RuntimeError("boom")):
            self.assertIsNone(
                self.core._contract_input_digest(),
                "取不到输入时返回 None = 不做缓存，行为不退化",
            )


class BodyLoopSchedulingTest(unittest.IsolatedAsyncioTestCase):
    """P2-07：按 monotonic 下一截止时刻调度（不是「干完再等」），并带小 jitter。"""

    async def test_tick_period_is_aligned_to_deadlines_not_step_cost(self):
        harness = Harness()
        core = harness.roles.get_or_create("bot1")
        try:
            core._stop_event.clear()
            ticks: list[float] = []

            async def step() -> None:
                ticks.append(time.monotonic())
                await asyncio.sleep(0)

            core._body_step = step  # type: ignore[assignment]
            with mock.patch.object(core, "_body_interval", return_value=0.05):
                task = asyncio.create_task(core._body_loop())
                try:
                    await asyncio.sleep(0.55)
                finally:
                    core._stop_event.set()
                    await asyncio.wait_for(task, timeout=2.0)
            self.assertGreaterEqual(len(ticks), 5, f"0.55s 内只跳了 {len(ticks)} 次")
            gaps = [b - a for a, b in zip(ticks, ticks[1:])]
            if gaps:
                average = sum(gaps) / len(gaps)
                # 宽松断言：平均周期应在 interval 附近（jitter ≤ 0.05*interval）。
                self.assertLess(average, 0.25, f"实际平均周期 {average:.3f}s 明显偏长")
                self.assertLess(max(gaps), 0.5, f"出现 {max(gaps):.2f}s 的长时间停顿")
        finally:
            await asyncio.sleep(0)
            harness.store.flush_sync()


# ────────────────────────── P2-08 兄弟 bot 角色集合 ──────────────────────────


@unittest.skipIf(main_module is None, SKIP_REASON)
class KnownRolesCacheTest(unittest.TestCase):
    """P2-08：兄弟 bot 识别走进程内集合，不再每条消息扫 StateStore 的 roles 字典。"""

    def test_known_roles_grows_without_rescanning_state(self):
        star, _tmp = _fresh_plugin()
        self.assertEqual(star._known_roles, set())
        star._core(FakeEvent("/你的状态", self_id="88888"))
        self.assertIn("88888", star._known_roles)
        unknown = star._known_role_ids(exclude="99999")
        self.assertIn("88888", unknown)
        self.assertNotIn("default", unknown)
        star._core(FakeEvent("/你的状态", self_id="99999"))
        self.assertNotIn("99999", star._known_role_ids(exclude="99999"))

    def test_broken_state_does_not_break_the_cache(self):
        star, _tmp = _fresh_plugin()
        with mock.patch.object(star, "_state_store") as store:
            store.data = None
            star._refresh_known_roles()
        self.assertEqual(star._known_roles, set())


# ────────────────────────── P2-11 /拟人 重载 从磁盘读 ──────────────────────────


class _FakeConfig(dict):
    """冒充 AstrBotConfig：dict 子类 + config_path + save_config。"""

    def __init__(self, path: Path, data: dict) -> None:
        super().__init__(data)
        self.config_path = str(path)

    def save_config(self, **_kw) -> None:
        Path(self.config_path).write_text(json.dumps(dict(self), ensure_ascii=False), encoding="utf-8")


@unittest.skipIf(main_module is None, SKIP_REASON)
class ConfigReloadTest(unittest.IsolatedAsyncioTestCase):
    async def _collect(self, generator) -> list[str]:
        return [item.text async for item in generator]

    async def test_reload_reads_the_config_file_from_disk(self):
        tmp = Path(tempfile.mkdtemp())
        config_path = tmp / "plugin_config.json"
        config_path.write_text(json.dumps({"timezone_city": "北京"}), encoding="utf-8")
        raw = _FakeConfig(config_path, json.loads(config_path.read_text(encoding="utf-8")))
        main_module.get_astrbot_data_path = lambda: str(tmp)
        star = main_module.HumanoidCore(FakeContext(), raw)
        self.assertEqual(star._config.timezone_city, "北京")
        # 模拟用户手改配置文件（面板不会走这条路，但用户会）。
        config_path.write_text(json.dumps({"timezone_city": "大阪"}), encoding="utf-8")
        out = await self._collect(star.cmd_reload(FakeEvent("拟人 重载", admin=True)))
        self.assertIn("配置已重载", out[0])
        self.assertEqual(star._config.timezone_city, "大阪", "重载没有从磁盘读到新配置")
        self.assertIn("配置文件", "\n".join(out))
        self.assertEqual(star._config_box.raw.config_path, str(config_path), "raw 类型被换掉了")

    async def test_reload_reports_when_there_is_no_config_path(self):
        main_module.get_astrbot_data_path = lambda: str(Path(tempfile.mkdtemp()))
        star = main_module.HumanoidCore(FakeContext(), {"timezone_city": "北京"})
        out = await self._collect(star.cmd_reload(FakeEvent("拟人 重载", admin=True)))
        text = "\n".join(out)
        self.assertIn("配置已重载", text)
        self.assertIn("没有 config_path", text)
        self.assertIn("重启/重载插件", text)

    async def test_reload_survives_a_corrupt_config_file(self):
        tmp = Path(tempfile.mkdtemp())
        config_path = tmp / "plugin_config.json"
        config_path.write_text(json.dumps({"timezone_city": "北京"}), encoding="utf-8")
        raw = _FakeConfig(config_path, json.loads(config_path.read_text(encoding="utf-8")))
        main_module.get_astrbot_data_path = lambda: str(tmp)
        star = main_module.HumanoidCore(FakeContext(), raw)
        config_path.write_text("{ 坏 JSON", encoding="utf-8")
        out = await self._collect(star.cmd_reload(FakeEvent("拟人 重载", admin=True)))
        text = "\n".join(out)
        self.assertIn("读取配置文件失败", text)
        self.assertEqual(star._config.timezone_city, "北京", "读盘失败应退回内存配置")


# ────────────────────────── P2-12 输入状态频率 ──────────────────────────


class _CountingClient:
    def __init__(self, stop_after: int = 0, stop_event=None) -> None:
        self.calls = 0
        self._stop_after = stop_after
        self._stop_event = stop_event

    async def call_action(self, action: str, **kwargs):
        self.calls += 1
        if self._stop_event is not None and self._stop_after and self.calls >= self._stop_after:
            self._stop_event.set()


class InputStateRateLimitTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.log = RecordingLogger()

    async def test_min_interval_is_a_floor_for_the_configured_interval(self):
        notifier = InputStateNotifier(
            self.log, interval=0.1, timeout=30.0, min_interval=0.3
        )
        self.assertEqual(notifier.interval, 0.3, "0.1s 的配置必须被下限抬到 0.3s")
        notifier.configure(0.3, 1.0, 30.0)
        self.assertEqual(notifier.interval, 1.0)
        notifier.configure(0.3, 0.1, 30.0)
        self.assertEqual(notifier.interval, 0.3)

    async def test_max_frames_caps_the_calls_for_one_reply(self):
        stop = asyncio.Event()
        client = _CountingClient()
        notifier = InputStateNotifier(
            self.log, interval=0.001, timeout=30.0, min_interval=0.0, max_frames=4
        )
        await asyncio.wait_for(
            notifier._run("k", client, "42", stop), timeout=2.0
        )
        self.assertEqual(client.calls, 4, "单条回复必须被帧数上限封住")

    async def test_zero_max_frames_keeps_the_old_behaviour(self):
        stop = asyncio.Event()
        client = _CountingClient(stop_after=5, stop_event=stop)
        notifier = InputStateNotifier(self.log, interval=0.001, timeout=30.0)
        await asyncio.wait_for(notifier._run("k", client, "42", stop), timeout=2.0)
        self.assertEqual(client.calls, 5)


# ────────────────────────── P2-32 状态落盘可见性 ──────────────────────────


class StatePersistenceDiagnosticsTest(unittest.TestCase):
    def _core_with_store(self, **attrs):
        tmp = Path(tempfile.mkdtemp())
        state_path = tmp / "state.json"
        state_path.write_text("{}", encoding="utf-8")
        store = SimpleNamespace(path=state_path, **attrs)
        core = SimpleNamespace(
            _state_store=store,
            scope=SimpleNamespace(all_user_ids=lambda: ["1", "2"]),
        )
        return core

    def test_healthy_state_renders_interval_and_serialize_cost(self):
        core = self._core_with_store(
            effective_flush_interval=180.0,
            last_serialize_ms=12.5,
            last_payload_bytes=2048,
            consecutive_write_failures=0,
            flush_failures=0,
            last_write_error="",
        )
        lines = state_size_lines(core)
        text = "\n".join(lines)
        self.assertIn("状态文件", text)
        self.assertIn("生效落盘间隔：180 秒", text)
        self.assertIn("上次序列化耗时：12.5 ms", text)
        self.assertIn("记录在案的用户：2 人", text)
        self.assertNotIn("连续失败", text)

    def test_write_failures_are_visible_with_window_hint(self):
        core = self._core_with_store(
            effective_flush_interval=300.0,
            last_serialize_ms=230.0,
            last_payload_bytes=2_273_145,
            consecutive_write_failures=3,
            flush_failures=27,
            last_write_error="PermissionError: [WinError 5] 拒绝访问",
        )
        text = "\n".join(state_size_lines(core))
        self.assertIn("连续失败 3 次", text)
        self.assertIn("WinError 5", text)
        self.assertIn("同步盘", text)
    def test_diagnostics_report_gets_its_own_section(self):
        core = self._core_with_store(
            effective_flush_interval=120.0,
            last_serialize_ms=3.0,
            last_payload_bytes=512,
            consecutive_write_failures=0,
            flush_failures=0,
            last_write_error="",
        )
        text = build_report(
            cfg=HumanoidConfig(),
            resolver=_QuietResolver(),
            gateway=_QuietGateway(),
            schedule_status={},
            activity_status={},
            version="3.0.0",
            state_size_lines=state_size_lines(core),
        )
        self.assertIn("【状态落盘】", text)
        self.assertIn("生效落盘间隔", text)

    def test_missing_store_attrs_degrade_silently(self):
        core = SimpleNamespace(
            _state_store=SimpleNamespace(path=Path(tempfile.mkdtemp()) / "none.json"),
            scope=SimpleNamespace(all_user_ids=lambda: ["1"]),
        )
        lines = state_size_lines(core)
        text = "\n".join(lines)
        self.assertIn("记录在案的用户：1 人", text)
        self.assertNotIn("生效落盘间隔", text)


# ────────────────────────── P2-01 / P2-02 生命周期 ──────────────────────────


@unittest.skipIf(main_module is None, SKIP_REASON)
class LifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_failure_rolls_back_and_deregisters(self):
        star, _tmp = _fresh_plugin()
        with mock.patch.object(
            star.role_manager, "start", side_effect=RuntimeError("角色起不来")
        ):
            with self.assertRaises(RuntimeError):
                await star.initialize()
        self.assertIsNone(main_module._ACTIVE_PLUGIN, "失败后插件仍在登记（半死状态）")
        self.assertIsNone(star._state_store._flush_task, "失败后落盘循环还在跑")

    async def test_terminate_stops_state_and_closes_session_even_if_roles_explode(self):
        star, _tmp = _fresh_plugin()
        await star.initialize()
        session = await star._ensure_session()
        with mock.patch.object(
            star.role_manager, "stop", side_effect=RuntimeError("停不下来")
        ):
            await star.terminate()  # 不许把异常抛给卸载流程
        self.assertTrue(session.closed, "terminate 异常路径也必须关掉 aiohttp session")
        self.assertIsNone(star._state_store._flush_task, "terminate 异常路径也必须停掉落盘并落盘")


if __name__ == "__main__":
    unittest.main()
