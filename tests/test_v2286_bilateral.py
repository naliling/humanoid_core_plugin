"""v2.28.6 — ccb 双边记账（用户 + bot 自己）与五个 P0 缺陷的守门测试。

这一批改的是**账怎么记**，不是门槛怎么定。所以断言几乎全落在「记了几次、
什么时候记、谁说的话算数」上，而不是「什么时候该触发」——后者由
`tests/test_ccb.py` 继续守。

分成五组：

* `FalsePositives`  —— P0-2：普通闲聊不能再被记成「发生过」
* `KeySplit`        —— P0-3 / P0-4：冷却锚点与两个去重键各归各位
* `GroupSilence`    —— P0-1：群里不记账（状态机本来就是硬闸，账上也得是）
* `HerSide`         —— P1：她自己的话、她自己的拒绝
* `HerInitiative`   —— P1：`initiative()` 与第四档
* `Facade`          —— P0-5：`CoreFacade.intimacy()` 与它承诺的「只读、不建壳」
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from humanoid import api, ccb
from humanoid.config import HumanoidConfig
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.persona import PersonaSource
from humanoid.state import StateStore
from tests.fakes import FrozenClock, RecordingLogger

FEMALE = "你是小满，24岁。她说话很短，喜欢安静。"


def _core(ccb_on: bool = True, **extra) -> HumanoidCoreInstance:
    raw = {"timezone_city": "北京", "ccb": ccb_on}
    raw.update(extra)
    conf = HumanoidConfig.from_raw(raw)
    st = StateStore(Path(tempfile.mkdtemp()) / "s.json", 0.01)
    st.load("2026-09-26", 28)

    class M:
        async def resolve_selected_persona(self, **k):
            return ("p", {"name": "", "prompt": FEMALE, "begin_dialogs": []}, None, False)

        async def get_default_persona_v3(self):
            return {"name": "", "prompt": FEMALE, "begin_dialogs": []}

    class C:
        persona_manager = M()
        conversation_manager = None

        def get_config(self, *a, **k):
            return None

    c = C()
    src = PersonaSource(c, lambda: RecordingLogger())
    core = HumanoidCoreInstance(
        role_id="bot1", state_store=st, config_provider=lambda: conf,
        logger=RecordingLogger(), stop_event=asyncio.Event(),
        resolver=c, gateway=None, persona_source=src,
    )
    asyncio.run(src.persona("bot1"))
    core.clock = FrozenClock(datetime(2026, 9, 26, 23, 0))
    return core


def _mood(core, **kw):
    v = {
        "affection": 92.0, "base_affection": 46.0, "libido": 47.0,
        "base_libido": 34.0, "aggression": 20.0, "base_aggression": 10.0,
        "turn_count": 60, "last_interaction": 0.0, "mood_tag": "", "last_decay": 0.0,
    }
    v.update(kw)
    core._scope.user_state("u1")["mood"] = v
    core._scope.set_self("energy", kw.get("energy", 70.0))


def _state(core, uid: str = "u1") -> dict:
    return core._scope.user_state(uid)


class FalsePositives(unittest.TestCase):
    """P0-2：词的边界。

    线上最伤的不是「漏记」（少一次就少一次），是**误记**——它同时改三样东西：
    次数、好感/亲近欲/攻击性，外加冷却锚点。一句「你做了什么」能把真触发推后
    5 小时，而用户完全看不出是哪一步坏的。
    """

    def test_talking_about_it_is_not_having_done_it(self):
        for text in (
            "你做了什么",
            "你今天做什么了呀",
            "你进来吧",
            "你要我怎么说",
            "我问你做了没有",
        ):
            with self.subTest(text=text):
                self.assertFalse(ccb.detect_occurrence(text), f"「{text}」不该被记成发生过")

    def test_real_occurrence_still_counts(self):
        for text in (
            "我们做过了",
            "昨晚我们做爱了",
            "已经做过了，我确定",
        ):
            with self.subTest(text=text):
                self.assertTrue(ccb.detect_occurrence(text), f"「{text}」该记一次")

    def test_negation_still_blocks(self):
        for text in ("还没做过了", "不想做爱", "别弄坏了"):
            with self.subTest(text=text):
                self.assertFalse(ccb.detect_occurrence(text))


class KeySplit(unittest.TestCase):
    """P0-3 / P0-4：三个时间键语义不同，不许互相覆盖。"""

    def test_note_occurrence_does_not_touch_the_cooldown_anchor(self):
        u = {}
        self.assertTrue(ccb.note_occurrence(u, 1_000.0))
        self.assertEqual(u["ccb_last_at"], 1_000.0, "上次发生的时间该记")
        self.assertNotIn("ccb_gate_at", u, "记录层不该写冷却锚点")
        self.assertNotIn("ccb_advance_at", u, "记录层不该占用渲染去重键")
        self.assertEqual(u["ccb_event_dedup_at"], 1_000.0)

    def test_talking_does_not_refresh_the_cooldown(self):
        """普通闲聊命中一次，不该让下一轮真触发被冷却挡回去。"""
        core = _core()
        _mood(core)
        state = _state(core)
        now = core.now_epoch()
        state["ccb_gate_at"] = now - ccb.CCB_COOLDOWN_SECONDS - 10
        # 直接调记录层，模拟「被记了一次」（现在这句本身已经不会命中）
        ccb.note_occurrence(state, now)
        gate = float(state.get("ccb_gate_at") or 0.0)
        self.assertGreaterEqual(
            now - gate, ccb.CCB_COOLDOWN_SECONDS,
            "记录层不该动冷却锚点——动了，真触发就会被推后 5 小时",
        )

    def test_two_dedup_windows_do_not_interfere(self):
        u = {}
        ccb.note_occurrence(u, 1_000.0)
        u["ccb_advance_at"] = 1_000.0
        # 记录层窗口内：同一条消息再记一次 → 不重复计
        self.assertFalse(ccb.note_occurrence(u, 1_030.0))
        self.assertEqual(u["ccb_done_count"], 1.0)
        # 出了记录层窗口，但渲染窗口还在 → 记录层该放行
        self.assertTrue(ccb.note_occurrence(u, 1_061.0))
        self.assertEqual(u["ccb_done_count"], 2.0)

    def test_one_event_is_counted_once_across_both_paths(self):
        """场景渲染与文本命中是**同一件事**，不该各记一次。"""
        core = _core()
        _mood(core)
        state = _state(core)
        state["ccb_stage"] = 2.0
        # 先在场景里渲染一轮
        core.build_injection("u1")
        first = float(state.get("ccb_done_count", 0.0) or 0.0)
        self.assertEqual(first, 1.0)
        # 同一分钟里她再说到这件事 → 不重复计
        core.note_her_words("u1", "我们做过了")
        self.assertEqual(float(state.get("ccb_done_count", 0.0) or 0.0), first)


class GroupSilence(unittest.TestCase):
    """P0-1：群里一句都不能进账。

    状态机的 `is_group` 是硬闸，但记录层原来没有同一道闸——群里一句
    「你做了什么」照样 +1 次、照样改好感。这是一条**保证裂开**，不是参数问题。
    """

    def test_group_message_never_reaches_the_ledger(self):
        core = _core()
        _mood(core)
        core.on_message("u1", "我们做过了", is_group=True)
        state = _state(core)
        self.assertEqual(float(state.get("ccb_done_count", 0.0) or 0.0), 0.0)

    def test_private_message_still_reaches_it_inside_a_scene(self):
        """v3.0.0（P2-22）：私聊文本入账的前提是**真的在场景里**。"""
        core = _core()
        _mood(core)
        state = _state(core)
        state["ccb_stage"] = 2.0
        state["ccb_last_at"] = core.now_epoch()

        async def _run():
            core.on_message("u1", "我们做过了", is_group=False)

        asyncio.run(_run())
        self.assertGreater(float(_state(core).get("ccb_done_count", 0.0) or 0.0), 0.0)

    def test_private_text_outside_a_scene_is_not_recorded(self):
        """v3.0.0（P2-22）：纯聊天一句话不能再刷关系数值。

        旧行为：私聊里任何一句「我们做过了」→ done_count+1，并直接加好感/亲近欲/
        降攻击性，绕过 38/50 门槛、场景分与冷却；社交层又拿 ccb_done_count 判断
        「是否发生过」。现在场景外（ccb_stage<=0 或闲置超时）一律不入账。
        """
        core = _core()
        _mood(core)
        state = _state(core)
        before_aff = float(state["mood"].get("affection", 0.0))

        async def _run():
            core.on_message("u1", "我们做过了", is_group=False)

        asyncio.run(_run())
        state = _state(core)
        self.assertEqual(float(state.get("ccb_done_count", 0.0) or 0.0), 0.0)
        self.assertNotIn("ccb_last_at", state, "场景外不该写「上次发生」时间")
        self.assertLessEqual(
            float(state["mood"].get("affection", 0.0)),
            before_aff + 1e-6,
            "场景外一句纯文本把好感刷上去了",
        )

    def test_command_is_not_a_conversation(self):
        core = _core()
        _mood(core)
        core.on_message("u1", "我们做过了", is_group=False, is_command=True)
        self.assertEqual(float(_state(core).get("ccb_done_count", 0.0) or 0.0), 0.0)


class HerSide(unittest.TestCase):
    """P1：她那一半。"""

    def test_her_words_count_inside_a_scene(self):
        core = _core()
        _mood(core)
        state = _state(core)
        state["ccb_stage"] = 2.0
        core.note_her_words("u1", "我们做过了")
        self.assertEqual(float(state.get("ccb_done_count", 0.0) or 0.0), 1.0)

    def test_her_words_are_ignored_outside_a_scene(self):
        """没有场景时她说到这件事不算。

        这是**自证**防护：她的回复是从注入里长出来的，模型复述一句内心状态
        就能把计数器刷上去——那不是发生，那是回声。
        """
        core = _core()
        _mood(core)
        core.note_her_words("u1", "我们做过了")
        self.assertEqual(float(_state(core).get("ccb_done_count", 0.0) or 0.0), 0.0)

    def test_her_refusal_ends_the_scene_and_is_remembered(self):
        core = _core()
        _mood(core)
        state = _state(core)
        state["ccb_stage"] = 3.0
        state["ccb_satisfy"] = 80.0
        core.note_her_words("u1", "住手，放开我")
        self.assertEqual(float(state.get("ccb_stage", 0.0) or 0.0), 0.0, "她拒绝之后场景该结束")
        self.assertGreater(float(state.get("ccb_refused_at", 0.0) or 0.0), 0.0)
        self.assertEqual(float(state.get("ccb_done_count", 0.0) or 0.0), 0.0, "拒绝不是发生")

    def test_her_playful_word_is_not_a_refusal(self):
        """撒娇不是拒绝：宽词表用在她身上会把一次「讨厌啦」判成 30 分钟静默。"""
        core = _core()
        _mood(core)
        state = _state(core)
        state["ccb_stage"] = 3.0
        core.note_her_words("u1", "讨厌啦，哼")
        self.assertEqual(float(state.get("ccb_stage", 0.0) or 0.0), 3.0)
        self.assertEqual(float(state.get("ccb_refused_at", 0.0) or 0.0), 0.0)

    def test_refusal_blocks_the_scene_for_a_while(self):
        state = {"ccb_refused_at": 1_000.0}
        self.assertTrue(ccb.refused_within(state, 1_000.0 + 60.0))
        self.assertFalse(ccb.refused_within(state, 1_000.0 + ccb.CCB_REFUSE_COOLDOWN_SECONDS + 1))

    def test_her_refusal_blocks_evaluate(self):
        base = dict(
            enabled=True, is_group=False, persona_prompt=FEMALE,
            affection=92.0, base_affection=46.0, libido=47.0, base_libido=34.0,
            energy=70.0, last_at=0.0, now=1_000_000.0,
        )
        self.assertIsNotNone(ccb.evaluate(**base))
        self.assertIsNone(ccb.evaluate(**base, refused=True))


class HerInitiative(unittest.TestCase):
    """P1：`initiative()` 与它开的第四档。"""

    def test_bounds(self):
        for kw in (
            dict(libido=0.0, base_libido=50.0, arousal=0.0, aggression=50.0,
                 relstage_index=0, affection=0.0),
            dict(libido=50.0, base_libido=0.0, arousal=100.0, aggression=0.0,
                 relstage_index=3, affection=100.0),
            dict(libido=float("nan"), base_libido=34.0),
        ):
            got = ccb.initiative(**kw)
            self.assertGreaterEqual(got, 0.0)
            self.assertLessEqual(got, 1.0)

    def test_arousal_can_be_switched_off(self):
        kw = dict(libido=47.0, base_libido=34.0, arousal=95.0, aggression=10.0,
                  relstage_index=3, affection=90.0)
        on = ccb.initiative(**kw, use_arousal=True)
        off = ccb.initiative(**kw, use_arousal=False)
        self.assertGreater(on, off, "关掉身体唤醒之后该更低")
        # 关掉时结果必须与「arousal=50」逐字一致——否则「关掉」就不是真的关掉
        self.assertAlmostEqual(off, ccb.initiative(**{**kw, "arousal": 50.0},
                                                   use_arousal=False), places=6)

    def test_cold_state_has_low_initiative(self):
        cold = ccb.initiative(libido=34.0, base_libido=34.0, arousal=50.0,
                              aggression=40.0, relstage_index=0, affection=46.0)
        hot = ccb.initiative(libido=49.0, base_libido=34.0, arousal=90.0,
                             aggression=8.0, relstage_index=3, affection=92.0)
        self.assertLess(cold, ccb.CCB_INITIATIVE_DEEP)
        self.assertGreater(hot, cold)

    def test_fourth_stage_needs_all_three_conditions(self):
        base = dict(
            enabled=True, is_group=False, persona_prompt=FEMALE,
            affection=92.0, base_affection=46.0, libido=49.0, base_libido=34.0,
            energy=70.0, last_at=0.0, now=1_000_000.0,
        )
        plain = ccb.evaluate(**base)
        hot = ccb.evaluate(**base, initiative_score=0.9)
        self.assertEqual(plain[2], 3, "没有主动度时仍是第三档")
        self.assertEqual(hot[2], 4, "主动度够高时开第四档")
        # 好感没进「深入」档：主动度再高也不许越过 E1 那条线
        shallow = ccb.evaluate(**{**base, "affection": 60.0}, initiative_score=0.9)
        self.assertEqual(shallow[2], 1)
        # 数值没到顶：也开不了
        mid = ccb.evaluate(**{**base, "libido": 45.0}, initiative_score=0.9)
        self.assertEqual(mid[2], 2)

    def test_fourth_stage_words_keep_the_discipline(self):
        """第四档可以说行为，但不写器官结构学名、不第三人称、不长于一句念头。"""
        banned = ("阴道", "阴茎", "龟头", "子宫", "阴蒂", "性器", "射精", "内射")
        for word in ccb._INITIATIVE_WORDS:
            with self.subTest(word=word):
                self.assertNotIn("她", word, "不该出现第三人称")
                self.assertLessEqual(len(word), 20, "该是一句念头，不是一段描写")
                for bad in banned:
                    self.assertNotIn(bad, word, "不该出现器官结构学名")


class Facade(unittest.TestCase):
    """P0-5：`CoreFacade.intimacy()`。

    `CHANGELOG` 从 v2.27.13 起就写着「契约快照多导出亲密次数 / 上次时间 /
    上头值，供社交层读」，而契约里从来没有这三个键。补的是门面，不是契约：
    契约是角色级快照，塞进 per-user 数据会把 state.json 撑大。
    """

    def _facade(self, core):
        roles = type("R", (), {"get_all": lambda self: [core]})()
        return api.CoreFacade(roles, None)

    def test_capability_is_advertised(self):
        self.assertIn("intimacy", api.CAPABILITIES)

    def test_unknown_role_is_empty_not_an_error(self):
        core = _core()
        facade = api.CoreFacade(type("R", (), {"get_all": lambda self: []})(), None)
        self.assertEqual(facade.intimacy("nobody", "u1"), {})

    def test_no_record_is_a_definite_empty_not_unknown(self):
        core = _core()
        _mood(core)
        got = self._facade(core).intimacy("bot1", "u1")
        self.assertEqual(got["done_count"], 0.0)
        self.assertEqual(got["last_at"], 0.0)
        self.assertEqual(got["want_line"], "")

    def test_reading_it_does_not_create_a_user_entry(self):
        """门面是只读的：社交层问一句，不该在 state.json 里留一个永久空壳。"""
        core = _core()
        facade = self._facade(core)
        facade.intimacy("bot1", "stranger")
        self.assertNotIn("stranger", core._scope.all_user_ids())

    def test_it_returns_the_real_numbers(self):
        core = _core()
        _mood(core)
        state = _state(core)
        state["ccb_done_count"] = 3.0
        state["ccb_satisfy"] = 42.0
        state["ccb_last_at"] = 1_000.0
        state["ccb_stage"] = 2.0
        got = self._facade(core).intimacy("bot1", "u1")
        self.assertEqual(got["done_count"], 3.0)
        self.assertEqual(got["satisfy"], 42.0)
        self.assertEqual(got["stage"], 2)

    def test_want_line_is_gated_by_the_proactive_switch(self):
        core_off = _core(ccb_proactive=False)
        _mood(core_off)
        state = _state(core_off)
        state["ccb_done_count"] = 2.0
        state["ccb_satisfy"] = 70.0
        self.assertEqual(self._facade(core_off).intimacy("bot1", "u1")["want_line"], "")

        core_on = _core(ccb_proactive=True)
        _mood(core_on)
        state_on = _state(core_on)
        state_on["ccb_done_count"] = 2.0
        state_on["ccb_satisfy"] = 70.0
        self.assertTrue(self._facade(core_on).intimacy("bot1", "u1")["want_line"])


class NewSwitches(unittest.TestCase):
    def test_defaults(self):
        cfg = HumanoidConfig.from_raw({})
        self.assertFalse(cfg.ccb, "成人档总开关必须仍然默认关")
        self.assertTrue(cfg.ccb_use_arousal)
        self.assertFalse(cfg.ccb_proactive, "她主动开口默认关")


if __name__ == "__main__":
    unittest.main()
