"""v2.19.0 称呼自动认定的三条承诺。

1. **只填空白，不碰存量**：自动认定只在「还没有任何称呼」时写入一次；用户 /叫我 设过的、
   或以前自动认下的，之后换名片也不改口——持久化数据不会被它变。
2. **门槛高**：来源是消息带的名字（群名片/QQ 昵称），什么 junk 都有；纯数字、含空白、
   表情符号串、占位词、超 12 字一律不认。宁可不叫，不能叫错。
3. **不诱导问名字**：注入里的称呼行要么「你管TA叫X」要么整行没有，不再有「还没问过TA叫
   什么」这种被模型当待办的句子。
"""

from __future__ import annotations

import unittest

from humanoid.config import HumanoidConfig
from humanoid.prompt_builder import PromptBuilder
from humanoid.role_scope import RoleScope
from humanoid.services.mood import MoodService, validate_auto_nickname


def make_service() -> tuple[MoodService, dict]:
    root: dict = {}
    scope = RoleScope(root, "default")
    service = MoodService(scope, lambda: HumanoidConfig(), time_source=lambda: 1_000_000.0)
    return service, root


class ValidateTest(unittest.TestCase):
    def test_accepts_plausible_names(self):
        for name in ("小鱼", "阿哲", "Linda", "Momo酱", "娜-莉-灵", "李·明浩", "王小明"):
            self.assertEqual(validate_auto_nickname(name), name.strip(), name)

    def test_rejects_junk(self):
        junk = [
            "",
            "   ",
            "N/A",
            "颖",  # 单字不足以确认身份
            "12",
            "123456",
            "白开水123",  # 含任何数字的名片不认
            "2024届",
            "user_123",
            "QQ12345678",
            "😀😘❤️",
            "★彡小鱼彡★",
            "李 白",  # 名字里带空白
            "这是一个长得不能再长的QQ昵称超过十二个字了",
            "默认昵称",
            "未设置",
            "已注销用户",
            "XX群管理员",
            "小机器人",
            "客服小美",
            "user",
            "abc",  # 占位字母串/颜文字/缩写不算名字
            "QwQ",
            "AB",
            "游客abc",
            "匿名网友",
            "测试",
            "已设置昵称",
            "你好",
        ]
        for name in junk:
            self.assertEqual(validate_auto_nickname(name), "", f"该被拒的没拒: {name!r}")

    def test_rejects_decorated_forms(self):
        # 首尾挂装饰符的是昵称装饰，括号装饰名串根本进不了字符集
        for name in ("-小鱼", "小鱼.", "·阿布·", "【小鱼】", "(小鱼)", "小鱼❤", "__小鱼", "小鱼——"):
            self.assertEqual(validate_auto_nickname(name), "", f"装饰形态被认了: {name!r}")

    def test_strips_outer_whitespace(self):
        self.assertEqual(validate_auto_nickname("  小鱼  "), "小鱼")


class AutoNicknameTest(unittest.TestCase):
    def test_fills_once_when_empty(self):
        service, _ = make_service()
        self.assertEqual(service.auto_nickname("7", "阿哲"), "阿哲")
        self.assertEqual(service.nickname("7"), "阿哲")
        self.assertEqual(service.nickname_source("7"), "auto")

    def test_never_overwrites_user_nickname(self):
        service, _ = make_service()
        service.set_nickname("7", "小灵")
        self.assertEqual(service.auto_nickname("7", "QQ上叫阿哲"), "")
        self.assertEqual(service.nickname("7"), "小灵")
        self.assertEqual(service.nickname_source("7"), "user")

    def test_never_retriggers_after_auto(self):
        """认过一次就定终身：换群名片、换设备名都不会改口。"""
        service, _ = make_service()
        service.auto_nickname("7", "阿哲")
        self.assertEqual(service.auto_nickname("7", "改名之后的名片"), "")
        self.assertEqual(service.nickname("7"), "阿哲")

    def test_legacy_nickname_without_source_counts_as_user(self):
        """自动认定上线前存的称呼只可能来自 /叫我：按 user 算，来源显示不会把它误标成自动。"""
        service, root = make_service()
        root["roles"]["default"]["users"]["7"] = {"nickname": "老数据"}
        self.assertEqual(service.nickname_source("7"), "user")
        self.assertEqual(service.auto_nickname("7", "新来的名片"), "")
        self.assertEqual(service.nickname("7"), "老数据")

    def test_invalid_candidate_is_not_written(self):
        service, _ = make_service()
        self.assertEqual(service.auto_nickname("7", "123456"), "")
        self.assertEqual(service.nickname("7"), "")
        self.assertEqual(service.nickname_source("7"), "")

    def test_all_nicknames_carries_source(self):
        service, _ = make_service()
        service.auto_nickname("a", "阿自")
        service.set_nickname("b", "自己设的")
        result = service.all_nicknames()
        self.assertEqual(result["a"], ("阿自", "auto"))
        self.assertEqual(result["b"], ("自己设的", "user"))

    def test_prune_keeps_nickname_and_source(self):
        """过期清理只删情绪重数据：称呼与来源必须原样留下。"""
        service, root = make_service()
        service.auto_nickname("7", "阿哲")
        service.profile("7")
        users = root["roles"]["default"]["users"]
        users["7"]["last_interaction"] = 0.0  # 远古时间 → 过期
        service.prune_expired(now=1_000_000.0)
        self.assertEqual(users["7"].get("nickname"), "阿哲")
        self.assertEqual(users["7"].get("nickname_src"), "auto")
        self.assertNotIn("mood", users["7"])


class _MoodOnlyCore:
    """PromptBuilder._nickname_line 只用到 core.mood，不必造整个实例。"""

    def __init__(self, mood: MoodService) -> None:
        self.mood = mood


class NicknameLineTest(unittest.TestCase):
    def test_line_absent_when_no_nickname(self):
        service, _ = make_service()
        pb = PromptBuilder(_MoodOnlyCore(service))
        line = pb._nickname_line("7", is_group=False)
        self.assertEqual(line, "", "没有称呼时整行不提")
        self.assertNotIn("还没问过", line)
        self.assertNotIn("叫什么", line)

    def test_line_uses_nickname_when_present(self):
        service, _ = make_service()
        service.auto_nickname("7", "阿哲")
        pb = PromptBuilder(_MoodOnlyCore(service))
        self.assertEqual(pb._nickname_line("7", is_group=False), "你管TA叫阿哲")


if __name__ == "__main__":
    unittest.main()
