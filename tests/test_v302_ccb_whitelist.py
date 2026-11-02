"""v3.0.2 — 亲密功能可以只对名单里的人开放（`ccb_allowed_users`）。

为什么要有这批测试：以前「能不能进亲密场景」只由好感 / 亲近欲 / 精力这些**数字**
决定。于是熟人把好感养到 50 就自动解锁，而且 `/你的状态` 会把亲近欲和「还差多少」
摆给他看——对朋友关系来说是件很难解释的事。名单补的是另一个问题：**这个人该不该
看到这一段**。两件事必须都真的生效，只挡住一个出口（比如只挡了注入、忘了状态页）
就等于没挡，所以下面按出口分组逐个盯。

分组：
* `AllowedFor`     —— 名单匹配语义（空 / 星号 / 字符串 / 坏配置不锁死所有人）
* `PanelHidden`    —— `/你的状态` 与 `/亲密状态`：名单外整段不出现
* `PresenceHidden` —— 注入里不再出现「她此刻的欲望」
* `EmotionGate`    —— 情绪候选那一路在进门之前就被挡下（不白跑数值门槛）
* `FacadeHidden`   —— 门面 `intimacy()` / 关系快照不给名单外人亲密数字
"""
from __future__ import annotations

import unittest
from unittest import mock

from tests.astrbot_stub import install as _install_astrbot_stub

_install_astrbot_stub()

from humanoid import api, ccb  # noqa: E402
from humanoid.config import HumanoidConfig  # noqa: E402
from humanoid.emotion import EmotionLayer  # noqa: E402
from tests.test_v2287_ccb_presence import FEMALE, _core, _mood  # noqa: E402

#: 名单外的人看到的唯一一句话：不解释、不给数字、不留差值。
BLOCKED_TEXT = "这部分没有对你开放。"


def _conf(**raw) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", "ccb": True, **raw})


class AllowedFor(unittest.TestCase):
    """名单语义：空 = 不设限（旧行为一字不改），填了才真的挡。"""

    def test_empty_list_keeps_the_old_behaviour(self):
        conf = _conf()
        for uid in ("", "u1", "10001", 123):
            self.assertTrue(ccb.allowed_for(conf, uid), f"{uid!r} 不该被挡住")

    def test_default_config_has_no_whitelist(self):
        self.assertEqual(_conf().ccb_allowed_users, ())

    def test_star_means_everyone(self):
        self.assertTrue(ccb.allowed_for(_conf(ccb_allowed_users=["*"]), "u1"))

    def test_only_listed_ids_pass(self):
        conf = _conf(ccb_allowed_users=["10001", " 10002 "])
        self.assertTrue(ccb.allowed_for(conf, "10001"))
        self.assertTrue(ccb.allowed_for(conf, "10002"))
        self.assertTrue(ccb.allowed_for(conf, " 10001 "))
        self.assertFalse(ccb.allowed_for(conf, "10003"))
        self.assertFalse(ccb.allowed_for(conf, ""))

    def test_a_single_string_still_works(self):
        conf = _conf(ccb_allowed_users="10001")
        self.assertTrue(ccb.allowed_for(conf, "10001"))
        self.assertFalse(ccb.allowed_for(conf, "10002"))

    def test_broken_config_does_not_lock_everyone_out(self):
        """读不到配置时按「不设限」：挡错了是功能整条消失，放过了只是回到旧行为。"""
        self.assertTrue(ccb.allowed_for(None, "u1"))
        self.assertTrue(ccb.allowed_for(object(), "u1"))


class PanelHidden(unittest.TestCase):
    """`/你的状态` 与 `/亲密状态` 是用户最容易撞见「亲近欲 41/50、还差 2」的地方。"""

    def _core(self, allowed) -> object:
        core = _core(True, ccb_allowed_users=list(allowed))
        _mood(core, affection=92.0)
        return core

    def test_status_rows_are_empty_for_outsiders(self):
        core = self._core(["10001"])
        self.assertEqual(core.intimacy_rows("u1"), [])
        self.assertEqual(core.intimacy_text("u1"), BLOCKED_TEXT)

    def test_status_page_hides_the_whole_section(self):
        core = self._core(["10001"])
        text = "\n".join(core.status_lines("u1", is_group=False))
        self.assertNotIn("能进入", text, "亲密段整段不该出现")
        self.assertNotIn("亲近欲", text)
        self.assertNotIn(BLOCKED_TEXT, text, "状态页不该多出一句「没对你开放」的暗示")

    def test_snapshot_says_it_is_not_open(self):
        core = self._core(["10001"])
        snap = core.intimacy_status("u1")
        self.assertFalse(snap["allowed"])
        self.assertFalse(snap["can_enter"])
        self.assertEqual(snap["blockers"], [BLOCKED_TEXT])

    def test_listed_user_still_gets_the_panel(self):
        core = self._core(["u1"])
        self.assertTrue(core.intimacy_status("u1")["allowed"])
        self.assertTrue(core.intimacy_rows("u1"), "名单里的人照常见整段")
        self.assertNotEqual(core.intimacy_text("u1"), BLOCKED_TEXT)


class PresenceHidden(unittest.TestCase):
    """注入里那句常驻的「她此刻的欲望」是另一条出口，单独盯一遍。"""

    def test_injection_has_no_desire_line_for_outsiders(self):
        core = _core(True, ccb_allowed_users=["10001"])
        _mood(core, affection=92.0, libido=41.0)
        text = core.build_injection("u1")
        self.assertNotIn("她此刻的欲望", text)

    def test_listed_user_still_sees_it(self):
        core = _core(True, ccb_allowed_users=["u1"])
        _mood(core, affection=92.0, libido=41.0)
        self.assertIn("她此刻的欲望", core.build_injection("u1"))


class EmotionGate(unittest.TestCase):
    """情绪候选那一路必须**在进门之前**就被挡下：不白跑数值门槛，也不推进场景。"""

    def test_outsider_never_reaches_the_gates(self):
        core = _core(True, ccb_allowed_users=["10001"])
        _mood(core)
        layer = EmotionLayer(core)
        calls = []

        def fake_evaluate(*a, **k):
            calls.append(1)
            return (0.6, "她想他了", 0)

        with mock.patch.object(ccb, "evaluate", fake_evaluate):
            got = layer._ccb_line(
                "u1", {}, is_group=False, persona_prompt=FEMALE, now=core.now_epoch(),
            )
        self.assertIsNone(got)
        self.assertEqual(calls, [], "名单外的用户不该走到数值门槛那一段")


class FacadeHidden(unittest.TestCase):
    """门面是别的插件读的窗口：名单外返回空字典 = 「没有这份资料」。"""

    def _facade(self, core):
        roles = type("R", (), {"get_all": lambda self: [core]})()
        return api.CoreFacade(roles, None)

    def test_intimacy_returns_nothing(self):
        core = _core(True, ccb_allowed_users=["10001"])
        _mood(core, affection=92.0)
        self.assertEqual(self._facade(core).intimacy("bot1", "u1"), {})

    def test_relationship_zeroes_the_intimacy_fields(self):
        core = _core(True, ccb_allowed_users=["10001"])
        _mood(core, affection=92.0)
        got = self._facade(core)._relationship(core, "u1")
        self.assertEqual(got["intimacy_done_count"], 0.0)
        self.assertEqual(got["intimacy_satisfy"], 0.0)
        self.assertEqual(got["stage"], 0)
        self.assertFalse(got["can_enter"])

    def test_listed_user_gets_the_numbers(self):
        core = _core(True, ccb_allowed_users=["u1"])
        _mood(core, affection=92.0)
        got = self._facade(core).intimacy("bot1", "u1")
        self.assertIn("libido", got, "名单里的人照常给到亲密数字")
        self.assertEqual(got["libido"], 41.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
