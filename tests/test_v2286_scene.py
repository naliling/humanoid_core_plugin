"""亲密场景的静态档案、姿势感知与场景账本。

这一版要解决的是"她换一轮就不知道自己什么姿势"和"深度前后矛盾"，
所以测试几乎全落在**判定准不准**与**数据放对没放对**上：

* `Safety`        —— P3 硬闸：未成年 / 血缘整条拒绝，而且不误伤「哥哥」
* `StaticBody`    —— C 数据层：她的档案角色级、他的按人、初始化一次后固定
* `Depth`         —— 静态阈值判定 + 单调化（不许深→浅乱跳）
* `Phase`         —— 五段性反应周期
* `Judgement`     —— 姿势/位置判定：命中、否定、歧义、误伤
* `Fallback`      —— A 的兜底：判不出就退回单行，绝不报错
* `Injection`     —— 端到端：场景内出那段状态，群里一句都不许漏
* `Aftercare`     —— 到顶之后的余韵窗
* `Exit`          —— 手动脱离
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from humanoid import ccb
from humanoid.config import HumanoidConfig
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.persona import PersonaSource
from humanoid.state import StateStore
from tests.fakes import FrozenClock, RecordingLogger

FEMALE = "你是小满，24岁。她说话很短，喜欢安静。"
MINOR = "你是小满，14岁。她说话很短。"
MINOR_WORD = "你是萝莉小满，说话很短。"
INCEST = "你是小满，和他从小一起长大，是亲兄妹。"
SIBLING_WORD = "你是小满，她有个哥哥。24岁。"


def _core(ccb_on: bool = True, persona: str = FEMALE, **extra) -> HumanoidCoreInstance:
    raw = {"timezone_city": "北京", "ccb": ccb_on}
    raw.update(extra)
    conf = HumanoidConfig.from_raw(raw)
    st = StateStore(Path(tempfile.mkdtemp()) / "s.json", 0.01)
    st.load("2026-09-26", 28)

    class M:
        async def resolve_selected_persona(self, **k):
            return ("p", {"name": "", "prompt": persona, "begin_dialogs": []}, None, False)

        async def get_default_persona_v3(self):
            return {"name": "", "prompt": persona, "begin_dialogs": []}

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


class Safety(unittest.TestCase):
    """P3：未成年与血缘整条拒绝。这是能不能存在的前提，不是可选项。"""

    def test_numeric_age_under_18_is_blocked(self):
        for age in (12, 14, 17):
            with self.subTest(age=age):
                self.assertTrue(ccb.safety_block(f"你是小满，{age}岁。"))

    def test_adult_age_passes(self):
        for age in (18, 24, 30):
            with self.subTest(age=age):
                self.assertEqual(ccb.safety_block(f"你是小满，{age}岁。"), "")

    def test_chinese_minor_age_words(self):
        for word in ("十二岁", "十五岁", "十七岁"):
            with self.subTest(word=word):
                self.assertTrue(ccb.safety_block(f"你是小满，{word}。"))

    def test_minor_markers(self):
        for word in ("幼女", "萝莉", "小学生", "初中生", "未成年", "儿童"):
            with self.subTest(word=word):
                self.assertTrue(ccb.safety_block(f"你是{word}小满。"))

    def test_incest_requires_a_pairing_word(self):
        self.assertTrue(ccb.safety_block(INCEST))
        self.assertTrue(ccb.safety_block("你是小满，父女关系。"))
        # 单独一个「哥哥」是中文里最常见的爱称，收进来会打死绝大多数人设
        self.assertEqual(ccb.safety_block(SIBLING_WORD), "")

    def test_empty_persona_passes(self):
        self.assertEqual(ccb.safety_block(""), "")
        self.assertEqual(ccb.safety_block("   "), "")

    def test_evaluate_refuses_on_safety(self):
        base = dict(
            enabled=True, is_group=False, persona_prompt=FEMALE,
            affection=92.0, base_affection=46.0, libido=47.0, base_libido=34.0,
            energy=70.0, last_at=0.0, now=1_000_000.0,
        )
        self.assertIsNotNone(ccb.evaluate(**base))
        self.assertIsNone(ccb.evaluate(**base, safety_reason="未成年"))

    def test_minor_persona_never_reaches_the_prompt(self):
        """心情层本来就会出「她内心：…」，所以这里盯的是 **ccb 那几句**本身。"""
        band_words = ("想让你抱着我", "想离你近一点", "不想一个人", "想靠你近", "后入")
        for persona in (MINOR, MINOR_WORD, INCEST):
            with self.subTest(persona=persona):
                core = _core(persona=persona)
                _mood(core)
                text = core.build_injection("u1")
                for word in band_words:
                    self.assertNotIn(word, text, f"{persona!r} 漏出了 ccb：{word}")
                self.assertEqual(float(_state(core).get("ccb_done_count", 0.0) or 0.0), 0.0)

    def test_minor_persona_does_not_even_record_occurrence(self):
        core = _core(persona=MINOR)
        _mood(core)

        async def _run():
            core.on_message("u1", "我们做过了", is_group=False)

        asyncio.run(_run())
        self.assertEqual(float(_state(core).get("ccb_done_count", 0.0) or 0.0), 0.0)


class StaticBody(unittest.TestCase):
    """C：她的档案在角色级、他的按人，且初始化一次后固定。"""

    def test_profile_clamps(self):
        self.assertEqual(ccb.body_profile(depth_cap_cm=99)["depth_cap_cm"], 18.0)
        self.assertEqual(ccb.body_profile(depth_cap_cm=-5)["depth_cap_cm"], 1.0)
        self.assertEqual(ccb.body_profile(depth_cap_cm=float("nan"))["depth_cap_cm"], 14.0)
        self.assertEqual(ccb.body_profile(sensitivity=999)["sensitivity"], 100)

    def test_her_profile_lives_at_role_level(self):
        core = _core()
        body = core.ccb_body()
        self.assertIn("depth_cap_cm", body)
        self.assertIn(ccb.BODY_KEY, core._scope.self_state)
        # 不该出现在任何用户条目下
        self.assertNotIn(ccb.BODY_KEY, _state(core))

    def test_it_is_initialised_once_then_frozen(self):
        core = _core(ccb_body_depth_cap_cm=16.0)
        first = core.ccb_body()
        self.assertEqual(first["depth_cap_cm"], 16.0)
        # 换一份配置：已经定下的档案不许变
        core._config_provider = lambda: HumanoidConfig.from_raw(
            {"timezone_city": "北京", "ccb": True, "ccb_body_depth_cap_cm": 9.0}
        )
        self.assertEqual(core.ccb_body()["depth_cap_cm"], 16.0)

    def test_partner_profile_is_per_user(self):
        core = _core(ccb_partner_length_cm=15.0)
        a = core.ccb_partner("u1")
        b = core.ccb_partner("u2")
        self.assertEqual(a["length_cm"], 15.0)
        self.assertEqual(b["length_cm"], 15.0)
        self.assertIsNot(a, b)
        self.assertIn("body", _state(core, "u1"))

    def test_partner_profile_frozen_too(self):
        core = _core(ccb_partner_length_cm=15.0)
        first = core.ccb_partner("u1")
        core._config_provider = lambda: HumanoidConfig.from_raw(
            {"timezone_city": "北京", "ccb": True, "ccb_partner_length_cm": 20.0}
        )
        self.assertEqual(core.ccb_partner("u1")["length_cm"], first["length_cm"])


class Depth(unittest.TestCase):
    def test_ratio_follows_phase(self):
        self.assertEqual(ccb.depth_estimate(20.0, 0), 0.0)
        self.assertEqual(ccb.depth_estimate(20.0, 1), 12.0)
        self.assertEqual(ccb.depth_estimate(20.0, 2), 17.0)
        self.assertEqual(ccb.depth_estimate(20.0, 3), 20.0)
        self.assertEqual(ccb.depth_estimate(0.0, 3), 0.0)

    def test_equal_is_not_over(self):
        self.assertFalse(ccb.is_over_depth(14.0, 14.0), "正好等于上限不算过深")
        self.assertTrue(ccb.is_over_depth(14.1, 14.0))
        self.assertFalse(ccb.is_over_depth(0.0, 14.0))

    def test_peak_depth_is_monotonic(self):
        u = {}
        self.assertEqual(ccb.note_peak_depth(u, 10.0), 10.0)
        self.assertEqual(ccb.note_peak_depth(u, 14.0), 14.0)
        self.assertEqual(
            ccb.note_peak_depth(u, 6.0), 14.0,
            "阶段回退不该让'刚才撑到疼'变成'还没到底'",
        )

    def test_peak_depth_resets_with_the_scene(self):
        u = {"ccb_peak_depth": 14.0}
        ccb.reset_scene(u)
        self.assertEqual(u["ccb_peak_depth"], 0.0)


class Phase(unittest.TestCase):
    def test_phase_ladder(self):
        for satisfy, want in ((0.0, 1), (39.0, 1), (40.0, 2), (79.0, 2),
                              (80.0, 3), (99.0, 3), (100.0, 4)):
            with self.subTest(satisfy=satisfy):
                got = ccb.phase_of(
                    satisfy=satisfy, stage=1, aftercare_until=0.0, now=1_000.0
                )
                self.assertEqual(got, want)

    def test_outside_scene_is_zero(self):
        self.assertEqual(
            ccb.phase_of(satisfy=90.0, stage=0.0, aftercare_until=0.0, now=1_000.0), 0
        )

    def test_aftercare_wins(self):
        self.assertEqual(
            ccb.phase_of(satisfy=0.0, stage=0.0, aftercare_until=2_000.0, now=1_000.0), 5
        )


class Judgement(unittest.TestCase):
    """判定层：这是整套东西能不能用的地方。

    用户的原话是「判断的不好那是很难受的」，所以这里既要钉"该判出来的判出来"，
    也要钉"判不准的时候宁可不判"。
    """

    def test_pose_hits(self):
        cases = (
            ("从后面抱着我", 0),
            ("转过来，面对面", 1),
            ("你坐上来", 2),
            ("躺下别动", 3),
        )
        for text, want in cases:
            with self.subTest(text=text):
                self.assertEqual(ccb.detect_pose(text), want)

    def test_site_hits(self):
        self.assertEqual(ccb.detect_site("后面那里"), 1)
        self.assertEqual(ccb.detect_site("前面那里"), 0)

    def test_bare_direction_words_are_not_a_site(self):
        """裸「前面/后面」不算位置。

        实测「从后面抱住你」里的"后面"说的是体位，不是入口——收了就会把位置判错，
        而判错位置比不知道更出戏。
        """
        self.assertIsNone(ccb.detect_site("从后面抱住你"))
        self.assertIsNone(ccb.detect_site("前面一点"))

    def test_negation_blocks_the_update(self):
        """「别从后面」不能把姿势改成正相反的那个。"""
        self.assertIsNone(ccb.detect_pose("别从后面"))

    def test_ambiguity_is_refused(self):
        """两种姿势同时出现 → 不猜。判错比不知道更出戏。"""
        self.assertIsNone(ccb.detect_pose("转过来，从后面"))

    def test_unrelated_text_is_not_matched(self):
        for text in ("今天吃什么", "你睡了吗", "我在想事情"):
            with self.subTest(text=text):
                self.assertIsNone(ccb.detect_pose(text))

    def test_detail_reports_the_word(self):
        pose, word = ccb.detect_pose_detail("从后面抱着")
        self.assertEqual(pose, 0)
        self.assertIn(word, ("从后面",))


class Fallback(unittest.TestCase):
    """A 的兜底：判不出就少说一句，绝不报错、绝不中断。"""

    def test_scene_note_empty_when_nothing_known(self):
        note = ccb.scene_note(pose=-1, site=-1, phase=0, depth_cm=0.0, cap_cm=14.0)
        self.assertEqual(note, "")

    def test_scene_note_partial_when_only_phase(self):
        note = ccb.scene_note(pose=-1, site=-1, phase=2, depth_cm=0.0, cap_cm=14.0)
        self.assertIn("平台", note)

    def test_injection_degrades_to_one_line(self):
        core = _core(ccb_scene_detail=False)
        _mood(core)
        text = core.build_injection("u1")
        self.assertIn("内心：", text)
        self.assertNotIn("·", text.split("内心：")[1].split("\n")[0].split("。")[0])

    def test_unknown_pose_still_gives_depth_and_phase(self):
        core = _core()
        _mood(core)
        state = _state(core)
        state["ccb_stage"] = 2.0
        # 45 + 40 = 85 → 临界，不会到顶（到顶会清姿势，那是另一条用例）
        state["ccb_satisfy"] = 45.0
        text = core.build_injection("u1")
        self.assertIn("临界", text)
        self.assertIn("cm", text)


class Injection(unittest.TestCase):
    def test_pose_site_reach_the_prompt(self):
        core = _core()
        _mood(core)
        state = _state(core)
        state["ccb_stage"] = 2.0
        # 45 + 40 = 85 → 临界；不设 85 是因为再加 40 会到顶，到顶按设计要清姿势。
        state["ccb_satisfy"] = 45.0
        state["ccb_pose"] = 0.0
        state["ccb_site"] = 1.0
        text = core.build_injection("u1")
        self.assertIn("后入", text)
        self.assertIn("后面", text)
        self.assertIn("临界", text)

    def test_group_never_leaks_the_scene_line(self):
        core = _core()
        _mood(core)
        state = _state(core)
        state["ccb_stage"] = 3.0
        state["ccb_pose"] = 0.0
        state["ccb_site"] = 1.0
        text = core.build_injection("u1", is_group=True)
        for word in ("后入", "临界", "过深"):
            self.assertNotIn(word, text)

    def test_her_words_drive_the_pose_state(self):
        core = _core()
        _mood(core)
        state = _state(core)
        state["ccb_stage"] = 2.0
        core.note_her_words("u1", "从后面抱住你")
        self.assertEqual(int(state.get("ccb_pose", -1)), 0)
        self.assertGreaterEqual(ccb.log_changes(state, "pose"), 1)

    def test_user_words_drive_it_too(self):
        core = _core()
        _mood(core)
        state = _state(core)
        state["ccb_stage"] = 2.0

        async def _run():
            core.on_message("u1", "你坐上来", is_group=False)

        asyncio.run(_run())
        self.assertEqual(int(state.get("ccb_pose", -1)), 2)


class Aftercare(unittest.TestCase):
    def test_peak_ends_the_scene_and_opens_the_window(self):
        core = _core()
        _mood(core)
        state = _state(core)
        state["ccb_stage"] = 2.0
        state["ccb_satisfy"] = 80.0
        state["ccb_pose"] = 0.0
        core.build_injection("u1")
        self.assertEqual(float(state.get("ccb_stage", 0.0) or 0.0), 0.0)
        self.assertGreater(float(state.get("ccb_aftercare_until", 0.0) or 0.0), 0.0)
        self.assertGreaterEqual(ccb.log_changes(state, "peak"), 1)

    def test_aftercare_line_appears_then_expires(self):
        """余韵句作为**普通候选**参与竞争，所以直接测这条通道本身。"""
        from humanoid.emotion import EmotionLayer

        core = _core()
        _mood(core)
        state = _state(core)
        layer = EmotionLayer(core)
        now = core.now_epoch()
        state["ccb_aftercare_until"] = now + 3600.0
        score, text = layer._aftercare_line("u1")
        self.assertGreater(score, 0.0)
        self.assertIn("还没缓过来", text)
        state["ccb_aftercare_until"] = now + 7000.0
        self.assertIn("刚结束", layer._aftercare_line("u1")[1])
        state["ccb_aftercare_until"] = now - 10.0
        self.assertEqual(layer._aftercare_line("u1"), (0.0, ""))

    def test_aftercare_score_decays_over_time(self):
        """余韵显著度随时间衰减：刚收场该它说话，快结束时让位给别的情绪。"""
        from humanoid.emotion import EmotionLayer

        core = _core()
        _mood(core)
        state = _state(core)
        layer = EmotionLayer(core)
        now = core.now_epoch()
        state["ccb_aftercare_until"] = now + 7000.0
        early = layer._aftercare_line("u1")[0]
        state["ccb_aftercare_until"] = now + 600.0
        late = layer._aftercare_line("u1")[0]
        self.assertGreater(early, late, "余韵不能一直保持同一个显著度")
        self.assertGreater(early, 0.85, "刚收场时该压得住普通情绪")
        self.assertLessEqual(late, 0.6, "快到尾声时该让位")


class Cooldown(unittest.TestCase):
    """冷却只挡开新的一场，不挡继续这一场。"""

    def _base(self, **kw):
        base = dict(
            enabled=True, is_group=False, persona_prompt=FEMALE,
            affection=92.0, base_affection=46.0, libido=47.0, base_libido=34.0,
            energy=70.0, last_at=1_000_000.0, now=1_000_001.0,
        )
        base.update(kw)
        return base

    def test_cooldown_blocks_a_new_scene(self):
        self.assertIsNone(ccb.evaluate(**self._base()))

    def test_cooldown_does_not_block_an_ongoing_scene(self):
        """`satisfy` 每进一次 +40、到顶要 3 次。冷却若也挡"继续"，一次完整的场景
        会横跨 15 小时——临界/爆发永远只在第三天出现。"""
        self.assertIsNotNone(ccb.evaluate(**self._base(in_scene=True)))


class Refractory(unittest.TestCase):
    """
    旧实现里收场只扣精力，而 `occurrence_effects` 还给 libido +2/+3 —— 方向是反的。
    """

    def _peak(self, core):
        state = _state(core)
        state["ccb_stage"] = 2.0
        state["ccb_satisfy"] = 80.0
        core.build_injection("u1")
        return state

    def test_peak_drops_libido(self):
        core = _core(ccb_refractory_libido=12.0)
        _mood(core)
        before = float(core.mood.profile("u1")["libido"])
        self._peak(core)
        after = float(core.mood.profile("u1")["libido"])
        self.assertAlmostEqual(after, before - 12.0, places=3)

    def test_it_never_goes_negative(self):
        core = _core(ccb_refractory_libido=30.0)
        _mood(core, libido=5.0)
        self._peak(core)
        self.assertGreaterEqual(float(core.mood.profile("u1")["libido"]), 0.0)

    def test_switch_off_means_no_drop(self):
        core = _core(ccb_refractory_libido=0.0)
        _mood(core)
        before = float(core.mood.profile("u1")["libido"])
        self._peak(core)
        self.assertAlmostEqual(float(core.mood.profile("u1")["libido"]), before, places=3)

    def test_affection_is_untouched(self):
        """欲望退，感情不退——好感是长线评价，不该被一次性事件拉下来。"""
        core = _core(ccb_refractory_libido=12.0)
        _mood(core)
        before = float(core.mood.profile("u1")["affection"])
        self._peak(core)
        self.assertAlmostEqual(float(core.mood.profile("u1")["affection"]), before, places=3)

    def test_no_peak_no_drop(self):
        core = _core(ccb_refractory_libido=12.0)
        _mood(core)
        state = _state(core)
        state["ccb_stage"] = 2.0
        state["ccb_satisfy"] = 0.0
        before = float(core.mood.profile("u1")["libido"])
        core.build_injection("u1")
        self.assertAlmostEqual(float(core.mood.profile("u1")["libido"]), before, places=3)


class Exit(unittest.TestCase):
    """强制收场。"""

    def test_idle_scene_gets_ended(self):
        core = _core()
        _mood(core)
        state = _state(core)
        state["ccb_stage"] = 2.0
        state["ccb_last_at"] = core.now_epoch() - ccb.CCB_SCENE_IDLE_SECONDS - 60
        core._ccb_end_if_stale("u1", core.now_epoch())
        self.assertEqual(float(state.get("ccb_stage", 0.0) or 0.0), 0.0)

    def test_drained_energy_gets_ended(self):
        core = _core()
        _mood(core, energy=10.0)
        state = _state(core)
        state["ccb_stage"] = 2.0
        state["ccb_last_at"] = core.now_epoch()
        core._ccb_end_if_stale("u1", core.now_epoch())
        self.assertEqual(float(state.get("ccb_stage", 0.0) or 0.0), 0.0)


class SceneGate(unittest.TestCase):
    """进门判断：模型分是主力，关键词只在模型不可用时兜底。
    """

    def test_known_score_above_threshold_opens(self):
        self.assertTrue(ccb.scene_gate(in_scene=False, scene_score=0.7, intimacy_hint=False))

    def test_known_score_below_threshold_blocks(self):
        self.assertFalse(ccb.scene_gate(in_scene=False, scene_score=0.5, intimacy_hint=True))

    def test_unknown_score_falls_back_to_the_hint(self):
        self.assertFalse(ccb.scene_gate(in_scene=False, scene_score=-1.0, intimacy_hint=False))
        self.assertTrue(ccb.scene_gate(in_scene=False, scene_score=-1.0, intimacy_hint=True))

    def test_ongoing_scene_is_never_blocked_by_a_new_judgement(self):
        self.assertTrue(ccb.scene_gate(in_scene=True, scene_score=0.0, intimacy_hint=False))

    def test_stale_score_is_treated_as_unknown(self):
        now = 10_000.0
        fresh_at = now - ccb.CCB_SCENE_SCORE_TTL + 10
        old_at = now - ccb.CCB_SCENE_SCORE_TTL - 10
        self.assertTrue(ccb.scene_score_fresh(fresh_at, now))
        self.assertFalse(ccb.scene_score_fresh(old_at, now))
        self.assertFalse(ccb.scene_score_fresh(0.0, now))

    def test_intimacy_fallback_is_narrow(self):
        self.assertTrue(ccb.detect_intimacy("想要你抱我"))
        self.assertFalse(ccb.detect_intimacy("今天吃什么"))
        self.assertFalse(ccb.detect_intimacy(""))

    def test_evaluate_blocks_without_evidence(self):
        base = dict(
            enabled=True, is_group=False, persona_prompt=FEMALE,
            affection=92.0, base_affection=46.0, libido=47.0, base_libido=34.0,
            energy=70.0, last_at=0.0, now=1_000_000.0,
        )
        self.assertIsNone(ccb.evaluate(**base, scene_score=-1.0, intimacy_hint=False))
        self.assertIsNotNone(ccb.evaluate(**base, scene_score=-1.0, intimacy_hint=True))
        self.assertIsNotNone(ccb.evaluate(**base, scene_score=0.8, intimacy_hint=False))
        self.assertIsNone(ccb.evaluate(**base, scene_score=0.4, intimacy_hint=True))


class SceneScorePlumbing(unittest.TestCase):
    """模型分怎么落盘：拿不到必须是 -1（不知道），不是 0（明确闲聊）。"""

    def test_valid_score_is_clamped(self):
        core = _core()
        _mood(core)
        core.mood._note_scene_score("u1", 1.7)
        self.assertEqual(_state(core)["ccb_scene_score"], 1.0)
        core.mood._note_scene_score("u1", -3)
        self.assertEqual(_state(core)["ccb_scene_score"], 0.0)

    def test_missing_or_bad_score_is_unknown(self):
        core = _core()
        _mood(core)
        for bad in (None, "abc", float("nan"), float("inf")):
            with self.subTest(bad=bad):
                core.mood._note_scene_score("u1", bad)
                self.assertEqual(_state(core)["ccb_scene_score"], -1.0)

    def test_score_carries_a_timestamp(self):
        core = _core()
        _mood(core)
        core.mood._note_scene_score("u1", 0.7)
        self.assertGreater(_state(core)["ccb_scene_at"], 0.0)

    def test_low_score_ends_an_ongoing_scene(self):
        core = _core()
        _mood(core)
        state = _state(core)
        state["ccb_stage"] = 2.0
        state["ccb_gate_at"] = core.now_epoch() - 60
        state["ccb_scene_score"] = 0.2
        state["ccb_scene_at"] = core.now_epoch() - 10
        core.build_injection("u1")
        self.assertEqual(float(state.get("ccb_stage", 0.0) or 0.0), 0.0)

    def test_a_judgement_from_before_the_scene_cannot_end_it(self):
        """进场前给出的旧结论没资格赶人——否则她刚进来就出去。"""
        core = _core()
        _mood(core)
        state = _state(core)
        state["ccb_stage"] = 2.0
        state["ccb_gate_at"] = core.now_epoch() - 60
        state["ccb_scene_score"] = 0.2
        state["ccb_scene_at"] = core.now_epoch() - 120
        core.build_injection("u1")
        self.assertEqual(float(state.get("ccb_stage", 0.0) or 0.0), 2.0)


class AuditRegressions(unittest.TestCase):
    """功能逻辑审计抓到的四个真问题（S1/S2/S3/S4）。

    每一条都是实测出来的、**都不会报错**——只会静默错。
    """

    def test_long_lived_personas_are_not_read_as_age_zero(self):
        """S2：`(\\d{1,2})岁` 会在「300岁」里匹配出 `00岁` → 判成 0 岁未成年。

        奇幻人设（300 岁的神、1200 岁的精灵）是常见写法，误伤成本很高：
        用户只会看到"功能没生效"，不会知道是被年龄闸拦了。
        """
        for persona in ("你是300岁的神。", "活了1200岁的精灵。", "你已经1500岁。"):
            with self.subTest(persona=persona):
                self.assertEqual(ccb.safety_block(persona), "")
        # 真的未成年仍然要拦
        self.assertTrue(ccb.safety_block("你看起来17岁。"))

    def test_fallback_words_do_not_admit_casual_chat(self):
        """S1：兜底词面里的「继续」「里面」「要我」把日常话判成亲密信号。

        默认配置下（没配情绪分析模型）走的就是兜底，那等于把"聊着工作突然
        进场景"原样放回来。
        """
        for text in ("继续上班吧", "里面坐吧", "要不要我帮你拿"):
            with self.subTest(text=text):
                self.assertFalse(ccb.detect_intimacy(text))
        for text in ("想要你抱我", "亲我一下", "别停"):
            with self.subTest(text=text):
                self.assertTrue(ccb.detect_intimacy(text))

    def test_compound_negation_does_not_swallow_an_occurrence(self):
        """S4：单字「别」在「告别」「区别」里不是否定。

        原来按整词匹配，这些句子会被判成"没发生"——而且不报错。
        """
        for text in ("告别之后和你做过了", "区别在于我和你做过了"):
            with self.subTest(text=text):
                self.assertTrue(ccb.detect_occurrence(text))
        # 真否定仍然要挡（含词后面的否定）
        self.assertFalse(ccb.detect_occurrence("做过了，不要再提"))
        self.assertFalse(ccb.detect_occurrence("还没做过了"))

    def test_single_negation_still_works_at_sentence_start(self):
        """S4 的坑中坑：`"" in 任何串` 恒为 True，句首的「别」会被当成构词放过。"""
        self.assertIsNone(ccb.detect_pose("别从后面"))
        self.assertIsNone(ccb.detect_pose("别碰我"))

    def test_state_segment_does_not_lag_one_turn(self):
        """S3：状态段原来在写 stage **之前**算，进场那轮整段不出、之后每轮慢一拍。

        进场那次满足度 0 → 推进到 40 → 应该已经是「平台」，不是「动情」。
        """
        core = _core()
        _mood(core)
        state = _state(core)
        state["ccb_satisfy"] = 0.0
        state["ccb_stage"] = 0.0
        # 进门需要一点证据（模型分不在时走兜底），这里直接给兜底信号。
        state["ccb_intimacy_at"] = core.now_epoch()
        text = core.build_injection("u1")
        self.assertIn("平台", text, "进场那一轮就该带状态段，且是推进后的阶段")
        self.assertEqual(float(state.get("ccb_satisfy", 0.0) or 0.0), 40.0)

    def test_peak_turn_still_says_something(self):
        """到顶那一轮 stage 归零，但那是爆发——不该闭嘴。"""
        core = _core()
        _mood(core)
        state = _state(core)
        state["ccb_stage"] = 2.0
        state["ccb_satisfy"] = 80.0
        text = core.build_injection("u1")
        self.assertIn("爆发", text)


class NewSwitches(unittest.TestCase):
    def test_defaults(self):
        cfg = HumanoidConfig.from_raw({})
        self.assertFalse(cfg.ccb)
        self.assertEqual(cfg.ccb_body_depth_cap_cm, 14.0)
        self.assertEqual(cfg.ccb_body_sensitivity, 60)
        self.assertEqual(cfg.ccb_partner_length_cm, 13.0)
        self.assertTrue(cfg.ccb_scene_detail)
        self.assertEqual(cfg.ccb_aftercare_minutes, 120)
        self.assertFalse(cfg.ccb_scene_log_debug)


if __name__ == "__main__":
    unittest.main()
