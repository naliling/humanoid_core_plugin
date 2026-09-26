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

from .astrbot_stub import install as install_astrbot_stub
from .fakes import FakeContext, FakeProvider

# 真实环境里 AstrBot 自己会把这些模块准备好；只有在缺框架的机器上才需要补。
install_astrbot_stub()

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
        sender_name: str = "",
    ) -> None:
        self.message_str = message
        self._sender = sender
        self._self_id = self_id
        self._private = private
        self._admin = admin
        self._sender_name = sender_name
        self.unified_msg_origin = f"aiocqhttp:{'private' if private else 'group'}:{sender}"
        self.sent: list[str] = []
        self._extras: dict = {}
        self._stopped = False

    def get_sender_id(self) -> str:
        return self._sender

    def get_sender_name(self) -> str:
        return self._sender_name

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

    def set_extra(self, key, value) -> None:
        self._extras[key] = value

    def get_extra(self, key=None, default=None):
        if key is None:
            return self._extras
        return self._extras.get(key, default)

    def stop_event(self) -> None:
        self._stopped = True

    def is_stopped(self) -> bool:
        return self._stopped


class FakeProviderRequest:
    def __init__(self, system_prompt: str = "", prompt: str = "") -> None:
        self.system_prompt = system_prompt
        self.prompt = prompt


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
        self.assertIn("精力", out[0])
        self.assertIn("城市", out[0])

    async def test_settings_writes_throttle_numbers_as_ints(self):
        """/拟人设置 间隔 45 必须存成 int 45，不能是字符串 "45"。"""
        out = await collect(self.star.cmd_settings(FakeEvent("/拟人设置 间隔 45")))
        self.assertIn("已设为", out[-1])
        self.assertIsInstance(self.star._config_box.raw["schedule_min_interval_minutes"], int)
        self.assertEqual(self.star._config.schedule_min_interval_minutes, 45)
        self.assertEqual(self.star._config_box.raw["schedule_min_interval_minutes"], 45)

    async def test_settings_rejects_non_numeric_throttle_value(self):
        out = await collect(self.star.cmd_settings(FakeEvent("/拟人设置 预算 一堆")))
        self.assertIn("不是这一项能接受的值", out[-1])
        self.assertNotIn("llm_daily_call_budget", self.star._config_box.raw)

    async def test_settings_shows_throttle_line(self):
        out = await collect(self.star.cmd_settings(FakeEvent("/拟人设置")))
        self.assertIn("调用节流", out[0])
        self.assertIn("最短间隔", out[0])

    async def test_gate_is_installed_and_shared(self):
        """闸门挂在全局网关上：所有 bot 共用同一份预算。"""
        self.assertIsNotNone(self.star.call_gate)
        self.assertIs(self.star.gateway.gate, self.star.call_gate)

    async def test_on_message_releases_idle_silence(self):
        self.star.call_gate.note_interaction(12345.0)
        snap = self.star.call_gate.snapshot()
        self.assertFalse(snap["never_interacted"], "一条真实消息就该松开静默")

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
        self.assertIn("认不出城市", bad[0])
        default = await collect(self.star.cmd_time(FakeEvent("/时间")))
        self.assertIn("北京", default[0])

    async def test_nickname_command(self):
        out = await collect(self.star.cmd_set_nickname(FakeEvent("/叫我 小灵")))
        self.assertIn("小灵", out[0])
        core = self.star._core(FakeEvent("/你的状态"))
        self.assertEqual(core.mood.nickname("10001"), "小灵")
        # 已有称呼的人再发空 /叫我：告诉他现状与来路，不让人重复设
        shown = await collect(self.star.cmd_set_nickname(FakeEvent("/叫我")))
        self.assertIn("现在叫你「小灵」", shown[0])
        self.assertIn("你自己设的", shown[0])
        missing = await collect(self.star.cmd_set_nickname(FakeEvent("/叫我", sender="8888")))
        self.assertIn("用法", missing[0])
        too_long = await collect(self.star.cmd_set_nickname(FakeEvent("/叫我 " + "长" * 40)))
        self.assertIn("太长", too_long[0])
        # 已有称呼的人，自动认定硬塞也塞不进去
        core.mood.auto_nickname("10001", "谁都不许改的名")  # 已非空，不碰
        self.assertEqual(core.mood.nickname("10001"), "小灵")

    async def test_view_schedule(self):
        out = await collect(self.star.cmd_view_schedule(FakeEvent("/查看日程")))
        self.assertIn("日程", out[0])
        self.assertIn("动态日程", out[0], "要说明只排到当前这段，不预排未来")

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
        out = await collect(self.star.cmd_reset_schedule(FakeEvent("/重置日程")))
        self.assertIn("正在决定下一段", out[0])
        self.assertIn("新的一段已排好", out[-1])

    async def test_reset_schedule_reports_failure(self):
        """模型挂了也不失败：按身体现排一段，并指路诊断。"""
        self.provider.error = RuntimeError("connection refused")
        self.raw["schedule_allow_global_fallback"] = False
        self.star.engine.reload_config(self.raw)
        out = await collect(self.star.cmd_reset_schedule(FakeEvent("/重置日程")))
        self.assertIn("按身体现排", out[-1])
        self.assertIn("拟人诊断", out[-1])

    async def test_reset_state_and_mood(self):
        out = await collect(self.star.cmd_reset_state(FakeEvent("/重置状态")))
        self.assertIn("已重置", out[0])
        out2 = await collect(self.star.cmd_reset_mood(FakeEvent("/重置情绪")))
        self.assertIn("好感度", out2[0])

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
        self.assertIn("暂无昵称", empty[0])
        core = self.star._core(FakeEvent("/你的状态"))
        core.mood.set_nickname("555", "阿五")
        out = await collect(self.star.cmd_list_nicknames(FakeEvent("/查看所有昵称")))
        self.assertIn("阿五", out[0])
        self.assertIn("用户自设", out[0])
        core.mood.auto_nickname("556", "自动的")
        out = await collect(self.star.cmd_list_nicknames(FakeEvent("/查看所有昵称")))
        self.assertIn("自动的（自动认定）", out[0])
        self.assertIn("阿五（用户自设）", out[0])

    # ---------- 称呼自动认定（v2.19.0） ----------

    async def test_auto_nickname_fills_once_on_message(self):
        await self.star.on_message(FakeEvent("你好呀", sender="7101", sender_name="阿哲"))
        core = self.star._core(FakeEvent("/你的状态"))
        self.assertEqual(core.mood.nickname("7101"), "阿哲")
        self.assertEqual(core.mood.nickname_source("7101"), "auto")
        # 换了群名片也不会改口
        await self.star.on_message(FakeEvent("在吗", sender="7101", sender_name="改名了"))
        self.assertEqual(core.mood.nickname("7101"), "阿哲")

    async def test_auto_nickname_never_touches_user_set_one(self):
        await collect(self.star.cmd_set_nickname(FakeEvent("/叫我 小灵", sender="7102")))
        core = self.star._core(FakeEvent("/你的状态"))
        self.assertEqual(core.mood.nickname_source("7102"), "user")
        await self.star.on_message(FakeEvent("你好", sender="7102", sender_name="QQ昵称别认"))
        self.assertEqual(core.mood.nickname("7102"), "小灵")

    async def test_auto_nickname_rejects_junk_names(self):
        for index, junk in enumerate(("N/A", "123456789", "😀😘❤️", "李 白", "这是一个长得不能再长的QQ昵称")):
            await self.star.on_message(FakeEvent("你好", sender=f"710{index}", sender_name=junk))
        core = self.star._core(FakeEvent("/你的状态"))
        for index in range(5):
            self.assertEqual(core.mood.nickname(f"710{index}"), "", f"垃圾名被认了: {junk!r}")

    async def test_auto_nickname_disabled_by_config(self):
        self.raw["auto_nickname"] = False
        self.star._config_box.reload(self.raw)
        await self.star.on_message(FakeEvent("你好", sender="7110", sender_name="小芳"))
        core = self.star._core(FakeEvent("/你的状态"))
        self.assertEqual(core.mood.nickname("7110"), "")

    async def test_junk_name_still_allows_later_valid_one(self):
        """第一条消息名片是垃圾不认；之后来了个像样的名字才认——只填空白不拦以后。"""
        await self.star.on_message(FakeEvent("你好", sender="7111", sender_name="123"))
        core = self.star._core(FakeEvent("/你的状态"))
        self.assertEqual(core.mood.nickname("7111"), "")
        await self.star.on_message(FakeEvent("早", sender="7111", sender_name="阿早"))
        self.assertEqual(core.mood.nickname("7111"), "阿早")

    async def test_once_set_never_retriggered_across_restart(self):
        """设置过一次就只有一次：落盘重启后依然如此，名片再怎么变也不改口；
        唯一能再改的是用户自己发 /叫我。"""
        await self.star.on_message(FakeEvent("你好", sender="7130", sender_name="阿哲"))
        await self.star.terminate()
        # 模拟重启：同一数据目录再造一个实例，称呼从 state.json 读回
        star2 = main_module.HumanoidCore(self.ctx, self.raw)
        await star2.initialize()
        try:
            await star2.on_message(FakeEvent("在吗", sender="7130", sender_name="换了的名片"))
            await star2.on_message(FakeEvent("再来一条", sender="7130", sender_name="N/A"))
            core2 = star2._core(FakeEvent("/你的状态"))
            self.assertEqual(core2.mood.nickname("7130"), "阿哲")
            self.assertEqual(core2.mood.nickname_source("7130"), "auto")
            # 用户自己重新设置是唯一的例外
            out = await collect(star2.cmd_set_nickname(FakeEvent("/叫我 小灵", sender="7130")))
            self.assertIn("小灵", out[0])
            self.assertEqual(core2.mood.nickname("7130"), "小灵")
            self.assertEqual(core2.mood.nickname_source("7130"), "user")
            await star2.on_message(FakeEvent("又一条", sender="7130", sender_name="又换的名片"))
            self.assertEqual(core2.mood.nickname("7130"), "小灵")
        finally:
            await star2.terminate()
            self.star = star2  # teardown 再终止一次是幂等的

    async def test_injection_without_nickname_does_not_nudge_asking(self):
        """旧版「还没问过TA叫什么」会被模型当待办逢人就问名字：这行必须在任何注入里消失。"""
        req = FakeProviderRequest("")
        await self.star.inject_context(FakeEvent("你好", sender="7120"), req)
        self.assertNotIn("还没问过", req.system_prompt)
        self.assertNotIn("叫什么", req.system_prompt)
        # 认下名字后同一个位置换成「你管TA叫X」
        await self.star.on_message(FakeEvent("你好", sender="7120", sender_name="阿名"))
        req2 = FakeProviderRequest("")
        await self.star.inject_context(FakeEvent("在吗", sender="7120"), req2)
        self.assertIn("你管TA叫阿名", req2.system_prompt)

    # ---------- 钩子 ----------

    async def test_injection_appends_to_existing_prompt(self):
        req = FakeProviderRequest("你是一个助手。")
        await self.star.inject_context(FakeEvent("你好", private=False), req)
        self.assertIn("你是一个助手。", req.system_prompt)
        self.assertIn("【她的身体与生活】", req.system_prompt)
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

    # ---------- 多消息合并（消息防抖） ----------

    def _set_merge(self, **kw):
        self.raw.update(kw)
        self.star._config_box.reload(self.raw)

    async def test_merge_lone_message_proceeds(self):
        self._set_merge(message_merge_window_seconds=0.05)
        ev = FakeEvent("你好", sender="555")
        await self.star.debounce_merge(ev)
        self.assertFalse(ev.is_stopped(), "孤消息不该被停，正常回复")
        self.assertIsNone(ev.get_extra(main_module.MERGE_EXTRA_KEY), "只有一条时不挂合并块")

    async def test_merge_two_messages_only_last_replies(self):
        self._set_merge(message_merge_window_seconds=0.1)
        e1 = FakeEvent("在吗", sender="556")
        e2 = FakeEvent("帮我看个问题", sender="556")
        t1 = asyncio.create_task(self.star.debounce_merge(e1))
        await asyncio.sleep(0.03)  # e1 先进入窗口
        t2 = asyncio.create_task(self.star.debounce_merge(e2))
        await asyncio.gather(t1, t2)
        self.assertTrue(e1.is_stopped(), "先到的被后到的取代，不单独回")
        self.assertFalse(e2.is_stopped(), "后到的胜出，负责合并回复")
        self.assertEqual(e2.get_extra(main_module.MERGE_EXTRA_KEY), ["在吗"])

    async def test_merge_next_batch_starts_clean(self):
        """胜出者清空缓冲：下一批不该带上一批的内容。"""
        self._set_merge(message_merge_window_seconds=0.05)
        first = FakeEvent("第一批", sender="559")
        await self.star.debounce_merge(first)
        self.assertFalse(first.is_stopped())
        second = FakeEvent("第二批", sender="559")
        await self.star.debounce_merge(second)
        self.assertFalse(second.is_stopped())
        self.assertIsNone(second.get_extra(main_module.MERGE_EXTRA_KEY), "新一批不带旧内容")

    async def test_merge_prepends_earlier_into_prompt(self):
        ev = FakeEvent("帮我看个问题", sender="557")
        ev.set_extra(main_module.MERGE_EXTRA_KEY, ["在吗", "有空吗"])
        req = FakeProviderRequest(prompt="帮我看个问题")
        await self.star.inject_context(ev, req)
        self.assertEqual(req.prompt, "在吗\n有空吗\n帮我看个问题", "更早几条按时间正序前置进 prompt")

    async def test_merge_disabled_is_noop(self):
        self._set_merge(message_merge_enabled=False, message_merge_window_seconds=0.05)
        ev = FakeEvent("你好", sender="558")
        await self.star.debounce_merge(ev)
        self.assertFalse(ev.is_stopped())
        self.assertIsNone(ev.get_extra(main_module.MERGE_EXTRA_KEY))

    async def test_merge_skips_empty_text(self):
        self._set_merge(message_merge_window_seconds=0.05)
        ev = FakeEvent("   ", sender="560")
        await self.star.debounce_merge(ev)
        self.assertFalse(ev.is_stopped(), "纯图片/空文本消息不参与合并")

    async def test_merge_respects_environment_mode(self):
        self._set_merge(environment_mode="private", message_merge_window_seconds=0.05)
        group_ev = FakeEvent("群里问", sender="561", private=False)
        await self.star.debounce_merge(group_ev)
        self.assertFalse(group_ev.is_stopped(), "仅私聊模式下不该对群消息做合并")

    async def test_on_message_bookkeeping(self):
        core = self.star._core(FakeEvent("/你的状态"))
        before = core.social.value
        await self.star.on_message(FakeEvent("你好呀"))
        await asyncio.sleep(0.05)
        self.assertLessEqual(core.social.value, before)

    async def test_on_message_ignores_self_and_empty(self):
        core = self.star._core(FakeEvent("/你的状态"))
        core.mood.profile("99999")
        turns = core.mood.profile("99999").get("turn_count")
        await self.star.on_message(FakeEvent("你好", sender="99999"))
        await self.star.on_message(FakeEvent("   "))
        await asyncio.sleep(0.02)
        self.assertEqual(core.mood.profile("99999").get("turn_count"), turns)

    async def test_terminate_is_idempotent_and_closes_session(self):
        await self.star._ensure_session()
        session = self.star._session
        self.assertIsNotNone(session)
        await self.star.terminate()
        self.assertTrue(session.closed)
        await self.star.terminate()  # 再来一次不应抛异常

    async def test_state_file_written_on_terminate(self):
        core = self.star._core(FakeEvent("/你的状态"))
        core.mood.set_nickname("10001", "落盘测试")
        await self.star.terminate()
        state_files = list(Path(self.tmp).rglob("state.json"))
        self.assertTrue(state_files, "终止时应把状态写到 state.json")
        saved = json.loads(state_files[0].read_text(encoding="utf-8"))
        self.assertEqual(
            saved["roles"]["99999"]["users"]["10001"]["nickname"], "落盘测试"
        )


if __name__ == "__main__":
    unittest.main()
