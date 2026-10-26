"""指令审计：21 条指令「注册了」还不够，得真的能执行、能被权限挡住、能被当成指令认出来。

## 为什么要有这一组

指令是用户唯一摸得到的入口，而它有三处只在**真实会话**里才会断的地方：

1. **注册名与代码对不上**：`@filter.command("亲密状态")` 注册了，但 `COMMAND_NAMES`
   里漏了它——框架照样执行这条指令，可 `on_message` 会把它当聊天记账（涨好感、
   把「亲密状态」写进「TA 说过的话」、扣精力与社交能量、还可能触发情绪分析调用）。
   一条只读的状态指令因此**改状态**，而且事后没有任何线索。v2.28.9 之前
   `/亲密状态` 就是这个状态。
2. **权限只写在文档里**：管理指令如果只有 docstring 说「管理员」，非管理员打进去
   照样执行。这里逐条验证「非管理员拿到的是拒绝，而不是执行结果」。
3. **指令跑不完**：参数解析、配置关掉时的早退、异常路径，任何一处抛出来在群里就是
   一条「出错了」。这里把 21 条全驱动一遍。

另外钉住契约的**能力 ↔ 键**对应关系，以及 `refresh_contract` 的比较键覆盖所有能力块
——这两处漏一个，社交层就静默拿不到那个块（`event` 与 `wake` 都真出过这种事）。
"""

from __future__ import annotations

import inspect
import re
import tempfile
import unittest
from pathlib import Path

from humanoid.api import CAPABILITIES
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.link import CONTRACT_CAPABILITIES, build_contract

from .test_main_integration import FakeEvent, collect, main_module
from .test_v2280_contract import make_core
from .fakes import FakeContext, FakeProvider

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

# 顶层指令（所有人可用）与「拟人」组指令（管理员）。名字必须与 main.py 里的
# 装饰器一字不差——这份表就是「22 条」这个数字的出处。
TOP_LEVEL = (
    ("cmd_status", "/你的状态"),
    ("cmd_intimacy_status", "/亲密状态"),
    ("cmd_mood", "/好感度"),
    ("cmd_mood_detail", "/情绪详情"),
    ("cmd_mood_log", "/情绪日志"),
    ("cmd_view_schedule", "/查看日程"),
    ("cmd_time", "/时间 上海"),
    ("cmd_set_nickname", "/叫我 小灵"),
    ("cmd_help", "/拟人帮助"),
)

ADMIN_GROUP = (
    ("cmd_set_city", "拟人 时间 大阪"),
    ("cmd_set_char_name", "拟人 参照名称 小雨"),
    ("cmd_diagnose", "拟人 诊断"),
    ("cmd_settings", "拟人 设置"),
    ("cmd_reload", "拟人 重载"),
    ("cmd_reset_schedule", "拟人 重置日程"),
    ("cmd_schedule_interrupt", "拟人 日程打断"),
    ("cmd_schedule_continue", "拟人 日程继续"),
    ("cmd_reset_state", "拟人 重置状态"),
    ("cmd_reset_mood", "拟人 重置情绪"),
    ("cmd_set_affection", "拟人 设置好感度 88"),
    ("cmd_batch_affection", "拟人 批量好感度 111:30"),
    ("cmd_list_nicknames", "拟人 查看昵称"),
)


def _registered_from_source() -> tuple[set, set]:
    """从 main.py 的装饰器解析真实注册名（不靠手抄）。"""
    src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
    top = set(re.findall(r'@filter\.command\("([^"]+)"\)', src))
    grouped = set(re.findall(r'@humanoid_group\.command\("([^"]+)"\)', src))
    return top, grouped


class CommandRegistrationTest(unittest.TestCase):
    """注册表、`COMMAND_NAMES`、帮助文本三份名单必须一致。"""

    def test_the_plugin_registers_nine_plus_thirteen_commands(self):
        top, grouped = _registered_from_source()
        self.assertEqual(len(top), 9, f"顶层指令数变了：{sorted(top)}")
        # v2.28.11 多了 `/拟人 时间 城市`（设置她的城市），12 → 13。
        self.assertEqual(len(grouped), 13, f"管理指令数变了：{sorted(grouped)}")

    def test_command_names_cover_every_registered_command(self):
        """`COMMAND_NAMES` 是「这条消息是不是指令」的唯一判据，漏一条就少一条。

        `亲密状态` 就漏过：它被当成聊天记账，一条只读状态指令因此改了好感、
        扣了精力、还把「亲密状态」写进了「TA 说过的话」。
        """
        top, _grouped = _registered_from_source()
        self.assertEqual(
            set(main_module.COMMAND_NAMES),
            top | {"拟人"},
            "COMMAND_NAMES 与真实注册的指令对不上（多一条会误伤聊天，少一条会误记账）",
        )

    def test_help_text_lists_every_command(self):
        """帮助是用户在聊天里唯一的指令清单：漏一条就等于那条指令不存在。"""
        top, grouped = _registered_from_source()
        for name in top:
            self.assertIn(f"/{name}", main_module.HELP_TEXT, f"帮助里没有顶层指令 /{name}")
        for name in grouped:
            self.assertIn(f"/拟人 {name}", main_module.HELP_TEXT, f"帮助里没有 /拟人 {name}")

    def test_every_registered_handler_exists(self):
        for fn, _text in TOP_LEVEL + ADMIN_GROUP:
            with self.subTest(handler=fn):
                self.assertTrue(
                    callable(getattr(main_module.HumanoidCore, fn, None)),
                    f"main.py 里没有可调用的 {fn}",
                )


class CommandExecutionTest(unittest.IsolatedAsyncioTestCase):
    """22 条指令真的能跑完：有回复、不抛异常。"""

    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.provider = FakeProvider("p1", reply="[]")
        self.ctx = FakeContext(chat_providers=[self.provider])
        self.raw = {
            "timezone_city": "北京",
            "schedule_provider_name": "p1",
            "admin_qq": ["10001"],
            "state_flush_interval_seconds": 1,
        }
        self._original_path = main_module.get_astrbot_data_path
        main_module.get_astrbot_data_path = lambda: str(self.tmp)
        self.star = main_module.HumanoidCore(self.ctx, self.raw)
        await self.star.initialize()

    async def asyncTearDown(self) -> None:
        await self.star.terminate()
        main_module.get_astrbot_data_path = self._original_path

    async def test_every_command_replies_to_an_admin(self):
        for fn, text in TOP_LEVEL + ADMIN_GROUP:
            with self.subTest(command=text):
                out = await collect(getattr(self.star, fn)(FakeEvent(text)))
                self.assertTrue(out, f"{text} 一条回复都没有")
                self.assertTrue(str(out[0]).strip(), f"{text} 回了一条空消息")

    async def test_every_admin_command_rejects_a_stranger(self):
        for fn, text in ADMIN_GROUP:
            with self.subTest(command=text):
                out = await collect(getattr(self.star, fn)(FakeEvent(text, sender="777")))
                self.assertTrue(out, f"{text} 对非管理员没有任何回复")
                self.assertIn(
                    "权限不足", out[0],
                    f"{text} 没有拒绝非管理员，实际回的是：{out[0][:60]}",
                )

    async def test_ranking_subcommand_is_admin_only(self):
        """/好感度 本身人人可用，但「排行榜」看的是所有人的排名：只有管理员能看。"""
        stranger = await collect(self.star.cmd_mood(FakeEvent("/好感度 排行榜", sender="777")))
        self.assertIn("权限不足", stranger[0])
        # 排行榜要有「人」才排得出来：先建一份档案（命令本身会建，这里提前建）。
        self.star._core(FakeEvent("x")).mood.profile("10001")
        admin = await collect(self.star.cmd_mood(FakeEvent("/好感度 排行榜")))
        self.assertNotIn("权限不足", admin[0])
        self.assertIn("排名", admin[0])

    async def test_astrbot_admin_counts_as_admin(self):
        """`admin_qq` 没写的人，靠 AstrBot 全局管理员身份也该能用管理指令。"""
        out = await collect(self.star.cmd_diagnose(FakeEvent("拟人 诊断", sender="888", admin=True)))
        self.assertNotIn("权限不足", out[0])

    async def test_disabled_features_exit_early_instead_of_crashing(self):
        self.star._config_box.reload({**self.raw, "mood_enabled": False, "mood_log_enabled": False,
                                      "use_llm_schedule": False})
        for fn, text in (
            ("cmd_mood", "/好感度"),
            ("cmd_mood_detail", "/情绪详情"),
            ("cmd_mood_log", "/情绪日志"),
            ("cmd_reset_schedule", "拟人 重置日程"),
            ("cmd_schedule_interrupt", "拟人 日程打断"),
        ):
            with self.subTest(command=text):
                out = await collect(getattr(self.star, fn)(FakeEvent(text)))
                self.assertTrue(out and str(out[0]).strip())


class CommandIsNotChatTest(unittest.IsolatedAsyncioTestCase):
    """`/亲密状态` 曾经不在 `COMMAND_NAMES` 里：一条只读指令被当聊天记账。"""

    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.provider = FakeProvider("p1", reply="[]")
        self.ctx = FakeContext(chat_providers=[self.provider])
        self.raw = {
            "timezone_city": "北京",
            "schedule_provider_name": "p1",
            "admin_qq": ["10001"],
            "state_flush_interval_seconds": 1,
            "mood_use_llm_for_delta": False,
        }
        self._original_path = main_module.get_astrbot_data_path
        main_module.get_astrbot_data_path = lambda: str(self.tmp)
        self.star = main_module.HumanoidCore(self.ctx, self.raw)
        await self.star.initialize()

    async def asyncTearDown(self) -> None:
        await self.star.terminate()
        main_module.get_astrbot_data_path = self._original_path

    def test_intimacy_status_is_recognised_as_a_command(self):
        fn = main_module._is_command_message
        self.assertTrue(fn("亲密状态"), "/亲密状态 没被认成指令")
        self.assertTrue(fn("亲密状态", wake_command=True))
        self.assertFalse(fn("亲密状态", wake_command=False), "没被唤醒的群发言不该当指令")

    async def test_intimacy_status_does_not_touch_chat_bookkeeping(self):
        """指令不是交谈：不记「TA 说过的话」、不算今日发言数、不扣精力。"""
        core = self.star._core(FakeEvent("x"))
        before_count = core.scope.get_self("daily_msg_count", 0)
        before_energy = float(core.energy.energy)
        await self.star.on_message(FakeEvent("亲密状态", sender="10001"))
        self.assertIsNone(
            core.scope.get_user("10001", "last_message"),
            "指令被当成了聊天，写进了 last_message",
        )
        self.assertEqual(
            core.scope.get_self("daily_msg_count", 0), before_count,
            "指令被算进了今日发言数",
        )
        self.assertAlmostEqual(float(core.energy.energy), before_energy, places=3,
                               msg="指令不该扣精力")

    async def test_a_real_chat_message_still_books(self):
        """对照组：真聊天必须照常记账——否则上面那条断言只是「什么都没生效」。"""
        core = self.star._core(FakeEvent("x"))
        before_count = core.scope.get_self("daily_msg_count", 0)
        await self.star.on_message(FakeEvent("今天好累啊", sender="10002"))
        self.assertGreater(
            core.scope.get_self("daily_msg_count", 0), before_count,
            "真聊天没被算进今日发言数",
        )
        self.assertIsNotNone(core.scope.get_user("10002", "last_message"))


class ContractCapabilityTest(unittest.TestCase):
    """能力清单里的每一项，都得在契约里真有一个块；比较键也得覆盖它。"""

    #: 能力名 -> 契约里的路径。`dreams` 挂在 `day` 下面，所以路径是两段。
    CAPABILITY_PATHS = {
        "body": ("body",),
        "feelings": ("feelings",),
        "form": ("form",),
        "activity": ("activity",),
        "day": ("day",),
        "ongoing": ("ongoing",),
        "routine": ("routine",),
        "dreams": ("day", "dreams"),
        "wake": ("wake",),
        "event": ("event",),
        "identity": ("identity",),
        "outfit": ("outfit",),
    }

    def test_every_capability_has_a_real_block(self):
        self.assertEqual(
            set(CONTRACT_CAPABILITIES), set(self.CAPABILITY_PATHS),
            "契约能力清单与这里登记的路径不一致：新增能力必须同时加一条路径",
        )
        contract = build_contract(make_core())
        for capability, path in self.CAPABILITY_PATHS.items():
            with self.subTest(capability=capability):
                node = contract
                for step in path:
                    self.assertIsInstance(node, dict, f"{capability} 的 {step} 不是块")
                    self.assertIn(step, node, f"{capability} 声明了能力，契约里却没有 {step}")
                    node = node[step]

    def test_rewrite_keys_cover_every_capability_block(self):
        """`refresh_contract` 只比较 keys 里列出的块：漏一个，旧 state 里那个块永远不更新。

        `event` / `identity` / `outfit` 都是这么补进去的；`wake` 是这一轮补的。
        """
        src = inspect.getsource(HumanoidCoreInstance.refresh_contract)
        top_level = {path[0] for path in self.CAPABILITY_PATHS.values()}
        for key in sorted(top_level):
            self.assertIn(f'"{key}"', src, f"refresh_contract 的比较键里没有 {key}")

    def test_facade_advertises_only_real_capabilities(self):
        """门面的能力名是版本谈判依据：声明了却拿不到，社交层会以为「已经接上」。"""
        facade_caps = set(CAPABILITIES)
        self.assertEqual(facade_caps, {
            "brief", "clock", "events", "fingerprint", "dreams", "wake",
            "intimacy", "identity", "user",
        })


if __name__ == "__main__":
    unittest.main()
