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
        raw_text: str = "",
        wake_command: bool = True,
    ) -> None:
        self.message_str = message
        self._sender = sender
        self._self_id = self_id
        self._private = private
        self._admin = admin
        self._sender_name = sender_name
        # 唤醒标志：真实框架在 waking_check 里设置，指令只有当它为真时才执行。
        self.is_at_or_wake_command = wake_command
        # 消息链里的原始文本（未经唤醒前缀裁剪）。空时不造 message_obj，
        # 与旧版测试保持兼容。
        if raw_text:
            self.message_obj = type("MsgObj", (), {
                "message": [type("Plain", (), {"text": raw_text})()],
            })()
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
        out = await collect(self.star.cmd_help(FakeEvent("/拟人 帮助")))
        self.assertIn("/拟人 诊断", out[0])
        self.assertIn("/拟人 参照名称", out[0])
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
        # 认下名字后同一个位置换成「对TA的称呼是X」
        await self.star.on_message(FakeEvent("你好", sender="7120", sender_name="阿名"))
        req2 = FakeProviderRequest("")
        await self.star.inject_context(FakeEvent("在吗", sender="7120"), req2)
        self.assertIn("对TA的称呼是「阿名」", req2.system_prompt)

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

    # ---------- 消息合并（防抖） ----------

    def _set_merge(self, **kw):
        self.raw.update(kw)
        self.star._config_box.reload(self.raw)

    async def test_merge_lone_message_proceeds(self):
        """带句末标点的消息零延迟直接走：不为它白等一拍。"""
        self._set_merge(message_merge_timeout_seconds=5.0)
        ev = FakeEvent("你好呀。", sender="555")
        await self.star.debounce_merge(ev)
        self.assertFalse(ev.is_stopped(), "孤消息不该被停，正常回复")
        self.assertIsNone(ev.get_extra(main_module.MERGE_EXTRA_KEY), "只有一条时不挂合并块")

    async def test_merge_two_messages_only_last_replies(self):
        """两条**没说完**的半句要并成一条，只回一次。"""
        self._set_merge(message_merge_timeout_seconds=0.2)
        e1 = FakeEvent("我今天", sender="556")
        e2 = FakeEvent("特别累", sender="556")
        t1 = asyncio.create_task(self.star.debounce_merge(e1))
        await asyncio.sleep(0.03)  # e1 先到
        t2 = asyncio.create_task(self.star.debounce_merge(e2))
        await asyncio.gather(t1, t2)
        self.assertTrue(e1.is_stopped(), "先到的被后到的取代，不单独回")
        self.assertFalse(e2.is_stopped(), "后到的胜出，负责合并回复")
        got = e2.get_extra(main_module.MERGE_EXTRA_KEY)
        self.assertIn("已合并成一条", got[0], "两条合并也要标明，否则模型会当成两个人")
        self.assertEqual(got[1:], ["TA：我今天"], "合并后每条都带说话人——不然模型分不清自己在回谁")

    async def test_merge_answered_complete_sentence_is_not_delayed(self):
        """句末语气词的消息零延迟：「在吗」是两三个字，但它是完整的一问。"""
        self._set_merge(message_merge_timeout_seconds=5.0)
        ev = FakeEvent("在吗", sender="5563")
        await self.star.debounce_merge(ev)
        self.assertFalse(ev.is_stopped())
        self.assertEqual(self.star._debounce.peek_text(ev, ""), "在吗")

    async def test_merge_swallows_typing_one_by_one(self):
        """逐字连发（「你」「今天」「我好累啊」）不能被切成两半。

        「攒够两条就发」不算数：用户可能还在一个字一个字打。所以攒够条数只等一个短的
        确认窗口（Debouncer.SETTLE_CAP_SECONDS），期间来的后续都并进同一句。
        """
        from humanoid import debounce as debounce_mod

        self._set_merge(message_merge_timeout_seconds=0.3)
        texts = ["你", "今天", "我好累啊"]
        events = [FakeEvent(t, sender="5561") for t in texts]
        gap = debounce_mod.SETTLE_CAP_SECONDS / 4

        async def feed(ev):
            await asyncio.sleep(gap)
            await self.star.debounce_merge(ev)

        await asyncio.gather(*[feed(ev) for ev in events])
        alive = [ev for ev in events if not ev.is_stopped()]
        self.assertEqual(len(alive), 1, f"应该只有一条胜出，实际 {len(alive)} 条")
        got = alive[0].get_extra(main_module.MERGE_EXTRA_KEY)
        # 合并结果现在带说话人，且开头标明「这是合并出来的一条」。两条都要有：
        # 没标明的话模型会当成几个人各说各的，写一条长消息把人一个个回一遍。
        self.assertIn("已合并成一条", got[0], "必须标明这是合并出来的一条")
        self.assertEqual(got[1:], ["TA：你", "TA：今天"], "每条都要标明是谁说的")
        self.assertEqual(
            self.star._debounce.peek_text(alive[0], ""), "你 今天 我好累啊",
            "三句应该拼成一条完整的话",
        )

    async def test_merge_waits_when_unsure_then_sends_itself(self):
        """拿不准又没人接：等满等待时间也要发出去，不能卡住不回。"""
        self._set_merge(message_merge_timeout_seconds=0.15)
        ev = FakeEvent("今天真的好累", sender="5562")
        await self.star.debounce_merge(ev)
        self.assertFalse(ev.is_stopped(), "等到时间就该自己发")
        self.assertEqual(self.star._debounce.peek_text(ev, ""), "今天真的好累")

    async def test_merge_next_batch_starts_clean(self):
        """胜出者清空缓冲：下一批不该带上一批的内容。"""
        self._set_merge(message_merge_timeout_seconds=0.05)
        first = FakeEvent("第一批。", sender="559")
        await self.star.debounce_merge(first)
        self.assertFalse(first.is_stopped())
        second = FakeEvent("第二批。", sender="559")
        await self.star.debounce_merge(second)
        self.assertFalse(second.is_stopped())
        self.assertIsNone(second.get_extra(main_module.MERGE_EXTRA_KEY), "新一批不带旧内容")

    async def test_merge_prepends_earlier_into_prompt(self):
        ev = FakeEvent("帮我看个问题", sender="557")
        # 合并结果现在带说话人，而且**开头会标一句「这是合并出来的」**。
        # 不标的话模型会当成几个人各说各的，于是写一条长消息把人一个个回一遍。
        ev.set_extra(main_module.MERGE_EXTRA_KEY, [
            "（下面这 3 句是同一个人连着发的，已合并成一条；回一条就行，不用逐句回应）",
            "小鱼：在吗", "小鱼：有空吗",
        ])
        req = FakeProviderRequest(prompt="帮我看个问题")
        await self.star.inject_context(ev, req)
        text = req.prompt
        self.assertIn("在吗", text)
        self.assertIn("有空吗", text)
        self.assertIn("帮我看个问题", text)
        self.assertLess(text.index("在吗"), text.index("帮我看个问题"),
                        "更早几条按时间正序前置进 prompt")
        self.assertIn("已合并成一条", text, "必须标明这是合并出来的一条")

    async def test_merge_disabled_is_noop(self):
        self._set_merge(message_merge_enabled=False, message_merge_timeout_seconds=0.05)
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


class MergeKnowsWhoIsTalking(MainIntegrationTest):
    """合并这一层的两个洞。群里回错人、或者对着合并结果逐个回应，都出在这里。

    复用 `MainIntegrationTest` 的 `self.star` 与 `self._set_merge`——这两个是
    真实的合并配置，手搓一个 `Debouncer` 测不到 `debounce_merge` 里的那道
    「跳过自己发的」检查（它在外层，不在 Debouncer 里）。
    """

    async def _merge(self, sender, texts, sender_name="小鱼"):
        events = [FakeEvent(t, sender=sender) for t in texts]
        for ev in events:
            ev.get_sender_name = lambda n=sender_name: n
        tasks = [asyncio.create_task(self.star.debounce_merge(ev)) for ev in events]
        for t in tasks[:-1]:
            await asyncio.sleep(0.03)
        await asyncio.gather(*tasks)
        return events[-1].get_extra(main_module.MERGE_EXTRA_KEY)

    async def test_each_merged_line_carries_its_speaker(self):
        self._set_merge(message_merge_timeout_seconds=0.2)
        got = await self._merge("557", ["我今天", "特别累", "想找人说说话"])
        self.assertIn("已合并成一条", got[0], "必须标明是合并出来的，否则模型会当成两个人")
        self.assertEqual(got[1:], ["小鱼：我今天", "小鱼：特别累"],
                         "每条都要带说话人——不然模型不知道自己在回谁")

    async def test_it_tells_the_model_to_reply_once(self):
        self._set_merge(message_merge_timeout_seconds=0.2)
        got = await self._merge("557", ["我今天", "特别累", "想找人说说话"])
        self.assertIn("回一条就行", got[0])
        self.assertIn("不用逐句回应", got[0],
                      "不明确禁掉逐句回应，模型看到两句话就会写长消息挨个回")

    async def test_her_own_message_is_never_buffered(self):
        """**「随时都在触发」的真凶。**

        `on_message` 有一道「跳过自己发的」检查，`debounce_merge` 原来没有——
        所以她自己在群里说的每句（尤其主动消息插件发的那几条）都被当成
        「用户刚说了什么」攒进 buffer。表现出来就是随时都在触发，而且分不清
        那是不是在回对方。

        这条钉的是**那道检查存在**，不是它的效果：效果断了顶多少是慢一点，
        缺了它则是「她在等自己说话」。
        """
        import inspect
        src = inspect.getsource(main_module.HumanoidCore.debounce_merge)
        self.assertIn("_self_id", src,
                      "合并这一层必须跳过自己发的，否则她自己的话会被当成用户的话")
        self.assertLess(src.index("_self_id"), src.index("self._debounce.hold"),
                        "必须在进 hold 之前就跳掉")


class ProactiveMessageMustNotFeedTheDebouncer(unittest.TestCase):
    """端到端复现：**主动消息发出去 → 事件回到事件总线 → 不该进防抖 buffer**。

    这个洞是这么发现的：容器里她「随时都在触发」，而触发量远超用户的实际发言。
    原因不在 social——它发消息走 `send_message`，不经过 Core 的
    `on_waiting_llm_request`。真正的原因在 Core 这一层：

      · `on_message` 有一道「跳过自己发的」检查
      · `debounce_merge` 原来**没有**

    所以她自己在群里说的每一句（尤其主动消息插件发的那几条）都被当成
    「用户刚说了什么」攒进 buffer。表现出来就是：她没在等用户，她在等自己。

    这条测试不复述代码里有没有那句话，而是**真的走一遍**——把自己的话当事件
    送进 `debounce_merge`，断言它没被攒下、也没挡住下一条真人消息。
    """

    def setUp(self):
        self._raw = {"llm_daily_call_budget": 0, "message_merge_enabled": True,
                     "message_merge_timeout_seconds": 0.2}
        self.ctx = FakeContext()
        self.star = main_module.HumanoidCore(self.ctx, self._raw)
        self.ctx.star = self.star

    def test_her_own_line_is_dropped_and_the_next_real_one_still_merges(self):
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self.star.initialize())

            # 她自己刚发出去的那条（主动消息插件发的），被平台当成事件派发回来。
            mine = FakeEvent("今天吃的好饱", sender="bot1")
            mine._self_id = "bot1"
            mine._sender_name = "小夜"
            loop.run_until_complete(self.star.debounce_merge(mine))
            self.assertIsNone(mine.get_extra(main_module.MERGE_EXTRA_KEY),
                              "她自己的话被当成用户消息攒起来了——这是随时都在触发的真凶")

            # 紧跟着真人来两条真话：这两个人**该**被合并。
            a = FakeEvent("我今天", sender="557")
            a._self_id = "bot1"
            b = FakeEvent("特别累", sender="557")
            b._self_id = "bot1"
            t1 = loop.create_task(self.star.debounce_merge(a))
            loop.run_until_complete(asyncio.sleep(0.03))
            t2 = loop.create_task(self.star.debounce_merge(b))
            loop.run_until_complete(asyncio.gather(t1, t2))
            got = b.get_extra(main_module.MERGE_EXTRA_KEY)
            self.assertIsNotNone(got, "真人消息该照常合并——别为了挡自己发的把功能一起关了")
            self.assertIn("已合并成一条", got[0])
            loop.run_until_complete(self.star.terminate())
        finally:
            asyncio.set_event_loop(None)
            loop.close()
