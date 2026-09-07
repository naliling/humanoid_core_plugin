"""main.py 的集成测试：把 Star 真正实例化，逐条驱动指令与钩子。

main.py 用的是相对导入（AstrBot 以 `data.plugins.<dir>.main` 载入插件），
所以这里给插件目录造一个合成包名再加载，等价于框架里的导入方式。
没有 astrbot / aiohttp 的环境自动跳过。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

from .fakes import FakeContext, FakeProvider

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PKG_NAME = "_humanoid_plugin_under_test"


def _load_main():
    if PKG_NAME + ".main" in sys.modules:
        return sys.modules[PKG_NAME + ".main"]
    package = types.ModuleType(PKG_NAME)
    package.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[PKG_NAME] = package
    spec = importlib.util.spec_from_file_location(PKG_NAME + ".main", PLUGIN_ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


try:  # pragma: no cover - 取决于运行环境
    main_module = _load_main()
    SKIP_REASON = ""
except Exception as exc:  # pragma: no cover
    main_module = None
    SKIP_REASON = f"无法加载 main.py（缺少 astrbot 或 aiohttp）：{exc}"


class FakeResult:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeEvent:
    """只实现 main.py 真正用到的那几个方法。"""

    def __init__(
        self,
        message: str,
        sender: str = "10001",
        self_id: str = "99999",
        private: bool = True,
        admin: bool = False,
    ) -> None:
        self.message_str = message
        self._sender = sender
        self._self_id = self_id
        self._private = private
        self._admin = admin
        self.unified_msg_origin = f"aiocqhttp:{'private' if private else 'group'}:{sender}"
        self.sent: list[str] = []

    def get_sender_id(self) -> str:
        return self._sender

    def get_self_id(self) -> str:
        return self._self_id

    def get_group_id(self) -> str:
        return "" if self._private else "2000"

    def is_private_chat(self) -> bool:
        return self._private

    def is_admin(self) -> bool:
        return self._admin

    def plain_result(self, text: str) -> FakeResult:
        return FakeResult(text)

    async def send(self, result: FakeResult) -> None:
        self.sent.append(result.text)


class FakeProviderRequest:
    def __init__(self, system_prompt: str = "") -> None:
        self.system_prompt = system_prompt


GOOD_SCHEDULE = json.dumps(
    [
        {"start": "00:00", "end": "08:00", "event": "睡眠", "location": "卧室", "emotion": "平静", "energy_rate": 0.15},
        {"start": "08:00", "end": "18:00", "event": "工作", "location": "书房", "emotion": "专注", "energy_rate": -0.1},
        {"start": "18:00", "end": "24:00", "event": "休闲", "location": "客厅", "emotion": "轻松", "energy_rate": 0.05},
    ],
    ensure_ascii=False,
)


async def collect(generator) -> list[str]:
    return [item.text async for item in generator]


@unittest.skipIf(main_module is None, SKIP_REASON)
class MainIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.provider = FakeProvider("p1", reply=GOOD_SCHEDULE)
        self.ctx = FakeContext(chat_providers=[self.provider])
        self.raw = {
            "timezone_city": "北京",
            "schedule_provider_name": "p1",
            "admin_qq": ["10001"],
            "state_flush_interval_seconds": 1,
        }
        # 不碰真实数据目录：直接替换 Star 用的路径
        main_module.get_astrbot_data_path = lambda: str(self.tmp)
        self.star = main_module.HumanoidCore(self.ctx, self.raw)
        await self.star.initialize()

    async def asyncTearDown(self) -> None:
        await self.star.terminate()

    # ---------- 指令 ----------

    async def test_status_command(self):
        out = await collect(self.star.cmd_status(FakeEvent("/你的状态")))
        self.assertEqual(len(out), 1)
        self.assertIn("当前状态", out[0])
        self.assertIn("精力", out[0])

    async def test_help_lists_diagnose(self):
        out = await collect(self.star.cmd_help(FakeEvent("/拟人帮助")))
        self.assertIn("/拟人诊断", out[0])
        self.assertIn(main_module.__version__, out[0])

    async def test_mood_commands(self):
        self.assertIn("情绪档案", (await collect(self.star.cmd_mood(FakeEvent("/好感度"))))[0])
        self.assertIn(
            "情绪详细档案", (await collect(self.star.cmd_mood_detail(FakeEvent("/情绪详情"))))[0]
        )
        self.assertIn("暂无情绪", (await collect(self.star.cmd_mood_log(FakeEvent("/情绪日志"))))[0])

    async def test_time_command(self):
        out = await collect(self.star.cmd_time(FakeEvent("/时间 上海")))
        self.assertIn("上海", out[0])
        bad = await collect(self.star.cmd_time(FakeEvent("/时间 火星")))
        self.assertIn("暂不支持", bad[0])
        default = await collect(self.star.cmd_time(FakeEvent("/时间")))
        self.assertIn("北京", default[0])

    async def test_nickname_command(self):
        out = await collect(self.star.cmd_set_nickname(FakeEvent("/叫我 小灵")))
        self.assertIn("小灵", out[0])
        self.assertEqual(self.star.engine.nickname("10001"), "小灵")
        missing = await collect(self.star.cmd_set_nickname(FakeEvent("/叫我")))
        self.assertIn("用法", missing[0])
        too_long = await collect(self.star.cmd_set_nickname(FakeEvent("/叫我 " + "长" * 40)))
        self.assertIn("太长", too_long[0])

    async def test_view_schedule(self):
        out = await collect(self.star.cmd_view_schedule(FakeEvent("/查看日程")))
        self.assertIn("日程表", out[0])

    # ---------- 权限 ----------

    async def test_admin_commands_reject_outsiders(self):
        stranger = FakeEvent("/拟人诊断", sender="777")
        out = await collect(self.star.cmd_diagnose(stranger))
        self.assertIn("权限不足", out[0])

    async def test_astrbot_admin_is_accepted(self):
        event = FakeEvent("/拟人诊断", sender="777", admin=True)
        out = await collect(self.star.cmd_diagnose(event))
        self.assertIn("拟人诊断", out[0])

    async def test_diagnose_report_content(self):
        out = await collect(self.star.cmd_diagnose(FakeEvent("/拟人诊断")))
        report = out[0]
        self.assertIn("可用对话模型 id", report)
        self.assertIn("p1", report)
        self.assertIn("本次实际将使用", report)

    async def test_reload_config(self):
        self.raw["inject_activity_context"] = "full"
        out = await collect(self.star.cmd_reload(FakeEvent("/重载配置")))
        self.assertIn("配置已重载", out[0])
        self.assertEqual(self.star.engine.config.inject_activity_context, "full")

    async def test_reset_schedule_reports_result(self):
        event = FakeEvent("/重置日程")
        out = await collect(self.star.cmd_reset_schedule(event))
        self.assertIn("已开始在后台", out[0])
        self.assertTrue(event.sent, "生成结束后应追加一条结果消息")
        self.assertIn("日程已更新", event.sent[-1])

    async def test_reset_schedule_reports_failure(self):
        self.provider.error = RuntimeError("connection refused")
        self.raw["schedule_allow_global_fallback"] = False
        self.star.engine.reload_config(self.raw)
        event = FakeEvent("/重置日程")
        await collect(self.star.cmd_reset_schedule(event))
        self.assertIn("失败", event.sent[-1])
        self.assertIn("拟人诊断", event.sent[-1])

    async def test_reset_state_and_mood(self):
        out = await collect(self.star.cmd_reset_state(FakeEvent("/重置状态")))
        self.assertIn("已重置状态", out[0])
        out2 = await collect(self.star.cmd_reset_mood(FakeEvent("/重置情绪")))
        self.assertIn("已重置情绪", out2[0])

    async def test_set_and_batch_affection(self):
        out = await collect(self.star.cmd_set_affection(FakeEvent("/设置好感度 88")))
        self.assertIn("88", out[0])
        bad = await collect(self.star.cmd_set_affection(FakeEvent("/设置好感度 999")))
        self.assertIn("0-100", bad[0])
        usage = await collect(self.star.cmd_set_affection(FakeEvent("/设置好感度")))
        self.assertIn("用法", usage[0])
        batch = await collect(
            self.star.cmd_batch_affection(FakeEvent("/批量好感度 111:30, 222:200"))
        )
        self.assertIn("1 个用户", batch[0])
        self.assertIn("跳过", batch[0])

    async def test_list_nicknames(self):
        empty = await collect(self.star.cmd_list_nicknames(FakeEvent("/查看所有昵称")))
        self.assertIn("没有任何用户", empty[0])
        self.star.engine.set_nickname("555", "阿五")
        out = await collect(self.star.cmd_list_nicknames(FakeEvent("/查看所有昵称")))
        self.assertIn("阿五", out[0])

    # ---------- 钩子 ----------

    async def test_injection_appends_to_existing_prompt(self):
        req = FakeProviderRequest("你是一个助手。")
        await self.star.inject_context(FakeEvent("你好", private=False), req)
        self.assertIn("你是一个助手。", req.system_prompt)
        self.assertIn("系统暗示", req.system_prompt)
        self.assertIn("群聊", req.system_prompt)

    async def test_injection_creates_prompt_when_empty(self):
        req = FakeProviderRequest("")
        await self.star.inject_context(FakeEvent("你好"), req)
        self.assertTrue(req.system_prompt)
        self.assertIn("私聊", req.system_prompt)

    async def test_injection_is_fast_even_with_slow_provider(self):
        self.provider.delay = 5.0
        self.raw["schedule_provider_name"] = "p1"
        self.star.engine.reload_config(self.raw)
        loop = asyncio.get_running_loop()
        started = loop.time()
        for _ in range(30):
            await self.star.inject_context(FakeEvent("你好"), FakeProviderRequest(""))
        elapsed = loop.time() - started
        self.assertLess(elapsed, 1.0, f"30 次注入耗时 {elapsed:.2f}s，说明钩子里有阻塞调用")

    async def test_injection_respects_environment_mode(self):
        self.raw["environment_mode"] = "group"
        self.star.engine.reload_config(self.raw)
        req = FakeProviderRequest("")
        await self.star.inject_context(FakeEvent("你好", private=True), req)
        self.assertEqual(req.system_prompt, "", "私聊在 group 模式下不应被注入")

    async def test_on_message_bookkeeping(self):
        before = self.star.engine.social.value
        await self.star.on_message(FakeEvent("你好呀"))
        await asyncio.sleep(0.05)
        self.assertLessEqual(self.star.engine.social.value, before)

    async def test_on_message_ignores_self_and_empty(self):
        engine = self.star.engine
        engine.mood.profile("99999")
        turns = engine.mood.profile("99999").get("turn_count")
        await self.star.on_message(FakeEvent("你好", sender="99999"))
        await self.star.on_message(FakeEvent("   "))
        await asyncio.sleep(0.02)
        self.assertEqual(engine.mood.profile("99999").get("turn_count"), turns)

    async def test_terminate_is_idempotent_and_closes_session(self):
        await self.star._ensure_session()
        session = self.star._session
        self.assertIsNotNone(session)
        await self.star.terminate()
        self.assertTrue(session.closed)
        await self.star.terminate()  # 再来一次不应抛异常

    async def test_state_file_written_on_terminate(self):
        self.star.engine.set_nickname("10001", "落盘测试")
        await self.star.terminate()
        saved = json.loads(self.star.engine.state.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["nicknames"]["10001"], "落盘测试")


if __name__ == "__main__":
    unittest.main()
