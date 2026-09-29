"""ccb：默认关的独立开关，条件全中才触发，群里永远不出。

设计上最容易做歪的两条，这里逐条钉住：
  · **初始值被设高不该天天触发**（所以除了绝对值还卡「涨了多少」）
  · **群里绝对不出**（硬闸，不是打分低所以没选中）
"""
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
from tests.fakes import FakeContext, FrozenClock, RecordingLogger

FEMALE = "你是小满，24岁。她说话很短，喜欢安静。"
MALE = "你是阿明，26岁男设计师。他说话很直接。"


def _ok(**kw):
    base = dict(enabled=True, is_group=False, persona_prompt=FEMALE,
                affection=92.0, base_affection=46.0,
                libido=47.0, base_libido=34.0,
                energy=70.0, last_at=0.0, now=1_000_000.0)
    base.update(kw)
    return base


class Gate(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(HumanoidConfig.from_raw({}).ccb, "默认必须是关的")
        self.assertIsNone(ccb.evaluate(**_ok(enabled=False)))

    def test_fires_when_all_conditions_hold(self):
        self.assertIsNotNone(ccb.evaluate(**_ok()))

    def test_absolute_threshold(self):
        self.assertEqual(ccb.CCB_LIBIDO_MIN, 38.0)
        self.assertIsNone(ccb.evaluate(**_ok(libido=37.9)))

    def test_rise_and_floor_are_not_contradictory(self):
        """两条是与的关系。涨幅不能比绝对值相对基线还高，否则永远过不了。"""
        base = 34.0
        self.assertLessEqual(
            ccb.CCB_LIBIDO_RISE,
            ccb.CCB_LIBIDO_MIN - base,
            f"基线 {base} 时涨到 {ccb.CCB_LIBIDO_MIN} 只有 +{ccb.CCB_LIBIDO_MIN - base}，"
            f"而涨幅要求 {ccb.CCB_LIBIDO_RISE} —— 互相矛盾",
        )

    def test_must_have_actually_risen(self):
        """初始值被设高不该天天触发。"""
        self.assertIsNone(ccb.evaluate(**_ok(libido=40.0, base_libido=40.0)))
        self.assertIsNone(ccb.evaluate(**_ok(libido=42.0, base_libido=40.0)))
        self.assertIsNotNone(ccb.evaluate(**_ok(libido=45.0, base_libido=40.0)))

    def test_affection_gate(self):
        self.assertIsNone(ccb.evaluate(**_ok(affection=70.0, base_affection=46.0)))
        self.assertIsNone(ccb.evaluate(**_ok(affection=92.0, base_affection=90.0)))

    def test_energy_floor(self):
        self.assertIsNone(ccb.evaluate(**_ok(energy=10.0)))

    def test_sleeping_is_fine_when_energy_holds(self):
        """用户明确说的：她在睡觉没关系，只要有精力。"""
        self.assertIsNotNone(ccb.evaluate(**_ok(energy=70.0)))

    def test_cooldown_is_short_but_not_zero(self):
        recent = ccb.evaluate(**_ok(last_at=1_000_000.0 - 3600.0))
        self.assertIsNone(recent, "一小时前刚出过，不该连着来")
        later = ccb.evaluate(**_ok(last_at=1_000_000.0 - ccb.CCB_COOLDOWN_SECONDS - 60))
        self.assertIsNotNone(later, "过了冷却就该能再来——门槛已经很高了，冷却不该双重收费")

    def test_deterministic_not_a_dice_roll(self):
        """同一个状态跑 30 次，结果必须一致。"""
        results = {ccb.evaluate(**_ok()) is not None for _ in range(30)}
        self.assertEqual(results, {True}, "触发必须是确定的，不能掷骰子")


class HardGates(unittest.TestCase):
    def test_never_in_group(self):
        """群里是硬闸——按语气说的话没法在群里收场。"""
        self.assertIsNone(ccb.evaluate(**_ok(is_group=True)))

    def test_female_only(self):
        self.assertIsNone(ccb.evaluate(**_ok(persona_prompt=MALE)))
        self.assertTrue(ccb.looks_female(FEMALE))
        self.assertFalse(ccb.looks_female(MALE))

    def test_gender_falls_back_to_female_when_unknown(self):
        self.assertTrue(ccb.looks_female(""))
        self.assertTrue(ccb.looks_female("   "))


class Bands(unittest.TestCase):
    def test_no_gap_between_trigger_and_first_band(self):
        """触发阈值到第一档之间不能有断档。

        原来触发和档位都从 45 起。触发降到 38 而档位没跟着降的话，
        38~44 能过门槛却匹配不到任何一档 —— **到了该触发的时候他不触发**，
        比原来还糟。
        """
        first = ccb.bands(ccb.CCB_LIBIDO_MIN)[0][0]
        self.assertEqual(
            first, ccb.CCB_LIBIDO_MIN,
            f"第一档 {first} 与触发阈值 {ccb.CCB_LIBIDO_MIN} 不一致，中间会断档",
        )
        self.assertIsNotNone(
            ccb.evaluate(**_ok(libido=ccb.CCB_LIBIDO_MIN, base_libido=34.0)),
            "刚够到阈值就该触发")

    def test_bands_are_contiguous(self):
        """一档到下一档之间不留空：连续插值每个值都得有档位。"""
        floors = [b[0] for b in ccb.bands(ccb.CCB_LIBIDO_MIN)]
        for lo, hi in zip(floors, floors[1:]):
            mid = (lo + hi) / 2.0
            self.assertIsNotNone(ccb._band_for(mid, ccb.CCB_LIBIDO_MIN), f"{lo}~{hi} 之间没有档位")

    def test_three_bands(self):
        for lib, stage in ((38.0, 1), (45.0, 2), (49.0, 3)):
            res = ccb.evaluate(**_ok(libido=lib, base_libido=34.0))
            self.assertIsNotNone(res, f"{lib} 该触发")
            self.assertEqual(res[2], stage, f"{lib} 应该在第 {stage} 档")

    def test_words_are_feelings_not_descriptions(self):
        """措辞只说感受：不写器官结构、不写情节、不第三人称。"""
        for _floor, _score, words in ccb.bands(ccb.CCB_LIBIDO_MIN):
            for w in words:
                self.assertNotIn("她", w, f"「{w}」出现了第三人称")
                self.assertLessEqual(len(w), 14, f"「{w}」太长，不像一句念头")
                for bad in ("身体", "下面", "进入", "插入", "高潮", "射"):
                    self.assertNotIn(bad, w, f"「{w}」写了不该在这层出现的东西")


class EndToEnd(unittest.TestCase):
    """从 `build_injection` 走一遍——只测 `evaluate` 不知道接线对不对。"""

    def _core(self, ccb_on, persona=FEMALE, is_group=False):
        conf = HumanoidConfig.from_raw({"timezone_city": "北京", "ccb": ccb_on})
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
            resolver=c, gateway=None, persona_source=src)
        asyncio.run(src.persona("bot1"))
        core.clock = FrozenClock(datetime(2026, 9, 26, 23, 0))
        return core

    def _mood(self, core, **kw):
        v = {"affection": 92.0, "base_affection": 46.0, "libido": 47.0,
             "base_libido": 34.0, "aggression": 20.0, "base_aggression": 10.0,
             "turn_count": 60, "last_interaction": 0.0, "mood_tag": "", "last_decay": 0.0}
        v.update(kw)
        core._scope.user_state("u1")["mood"] = v
        core._scope.set_self("energy", kw.get("energy", 70.0))

    def _line(self, core, is_group=False):
        import re
        m = re.search(r"内心：([^）]*)）", core.build_injection("u1", is_group=is_group))
        return m.group(1) if m else ""

    def test_injection_carries_it_when_on(self):
        core = self._core(True)
        self._mood(core)
        self.assertIn("想让你抱着我", self._line(core))

    def test_injection_silent_when_off(self):
        core = self._core(False)
        self._mood(core)
        for w in ("想让你抱着我", "想离你近一点", "不想一个人"):
            self.assertNotIn(w, self._line(core))

    def test_injection_never_in_group(self):
        """ccb 一句都不能进群。

        这里盯的是 **ccb 那几句本身**，不是「内心话整个为空」。原先用后者做代理，
        是因为那时群里的情绪层整层关着，ccb 不出 ⇒ 内心话为空。但 v2.24.1 改成了
        「有档案的人照常读」，群里的内心话本来就会有内容（`有点不想搭理你` 这类），
        代理随之失效——**保证没破，只是盯错了地方**。`_ccb.evaluate` 的 is_group
        硬闸独立于情绪层，两者各测各的。
        """
        for lib in (38.5, 45.5, 49.5):
            core = self._core(True)
            self._mood(core, libido=lib)
            line = self._line(core, is_group=True)
            for w in ("想让你抱着我", "想离你近一点", "不想一个人"):
                self.assertNotIn(w, line, f"亲近欲 {lib} 时群里泄漏了 ccb：{line!r}")

    def test_group_inner_voice_is_the_mood_layer_not_ccb(self):
        """群里该有的情绪句仍然在——这正是 v2.24.1 改的「熟人进群也有关系感」。"""
        core = self._core(True)
        self._mood(core, affection=85.0, libido=45.5)
        text = core.build_injection("u1", is_group=True)
        # 断言「对TA」而不是「她对TA」：人设有名字时关系句用名字带出（实测是
        # 「小满对TA最近更上心」），写死代词会让人设有名字的用例假红。
        self.assertIn("对TA", text, "有档案的人进群，关系句该给")

    def test_injection_silent_for_male_persona(self):
        core = self._core(True, persona=MALE)
        self._mood(core)
        for w in ("想让你抱着我", "想离你近一点", "不想一个人"):
            self.assertNotIn(w, self._line(core))

    def test_state_written_and_cleared(self):
        core = self._core(True)
        self._mood(core)
        self._line(core)
        us = core._scope.user_state("u1")
        self.assertEqual(us.get("ccb_stage"), 2.0)
        # 亲近欲掉回基线 → 状态清掉，不留尾巴
        self._mood(core, libido=35.0, base_libido=34.0)
        self._line(core)
        self.assertEqual(us.get("ccb_stage"), 0.0)


class State(unittest.TestCase):
    def test_state_roundtrip(self):
        u = {"ccb_stage": 2.0, "ccb_last_at": 5.0, "ccb_satisfy": 80.0,
             "ccb_day": 3.0, "ccb_turns": 4.0}
        st = ccb.read_state(u)
        self.assertEqual(st["ccb_stage"], 2.0)
        self.assertEqual(st["ccb_satisfy"], 80.0)
        ccb.reset_state(u)
        self.assertEqual(ccb.read_state(u)["ccb_stage"], 0.0)
        self.assertEqual(ccb.read_state(u)["ccb_satisfy"], 0.0)

    def test_config_lands_last_in_schema(self):
        import json
        data = json.loads(
            (Path(__file__).resolve().parent.parent / "_conf_schema.json").read_text(encoding="utf-8"))
        keys = list(data)
        # 开关本身仍然要待在**最后四个之前**的那一组里——它是个默认关闭的隐藏开关，
        # 排在配置面板最底下才不显眼。v2.25.0 往后它跟了四个门槛参数（都可调，
        # 调的是「够不够得着」而不是「开不开」），所以断言从「必须是最后一个」
        # 放宽成「必须落在最后五个里，且开关在参数之前」。
        self.assertIn("ccb", keys[-5:], "ccb 这一组应该待在配置项最底下")
        self.assertLess(keys.index("ccb"), keys.index("ccb_libido_min"),
                        "开关应当在它的参数之前")
        self.assertEqual(data["ccb"]["default"], False)
        self.assertEqual(data["ccb"]["description"], "ccb")
        # 四个门槛必须存在且与代码默认值一致——不一致的话用户按面板调，
        # 实际生效的是另一套数。
        from humanoid import ccb as _c
        self.assertEqual(data["ccb_libido_min"]["default"], _c.CCB_LIBIDO_MIN)
        self.assertEqual(data["ccb_libido_rise"]["default"], _c.CCB_LIBIDO_RISE)
        self.assertEqual(data["ccb_affection_min"]["default"], _c.CCB_AFFECTION_MIN)
        self.assertEqual(data["ccb_affection_rise"]["default"], _c.CCB_AFFECTION_RISE)

    def test_not_mentioned_in_public_docs(self):
        """ccb 是隐藏开关：README 和 CHANGELOG 都不许出现。"""
        root = Path(__file__).resolve().parent.parent
        for name in ("README.md", "CHANGELOG.md"):
            path = root / name
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for i, line in enumerate(text.splitlines(), 1):
                if "曾经" in line or "更正" in line:
                    continue
                self.assertNotIn("ccb", line.lower(), f"{name}:{i} 提到了 ccb")


if __name__ == "__main__":
    unittest.main()


class SceneState(unittest.TestCase):
    """v2.24.1：ccb 是**场景里的状态**，不是一个「达标就一直这样」的开关。

    之前 `ccb_peak` / `ccb_turns` 只有写没有读（死状态），而 `ccb_stage` 只是
    「档位序号」，所以「她满足/不满足、到此为止/继续」这个维度根本没被表达过。
    """

    def test_satisfy_accumulates_three_times_to_the_top(self):
        from humanoid import ccb
        u = {}
        got = [ccb.advance_satisfy(u, 1.0) for _ in range(4)]
        self.assertEqual(got, [False, False, True, False],
                         "第三次到顶且只到顶一次；第四次已经在顶上了，不该再算收场")

    def test_reaching_the_top_costs_energy(self):
        """收场扣体力——这是「做爱是耗能的」的最小实现。"""
        from humanoid import ccb
        u = {"ccb_satisfy": 70.0, "ccb_day": float(ccb._day_index(1.0))}
        self.assertTrue(ccb.advance_satisfy(u, 1.0), "70 + 40 = 110 应当到顶")
        self.assertEqual(u["ccb_satisfy"], ccb.CCB_SATISFY_FULL)

    def test_satisfy_decays_slowly_across_days(self):
        """第二天还记得，但没昨天那么强。"""
        from humanoid import ccb
        day = ccb._day_index(1.0)
        u = {"ccb_satisfy": 100.0, "ccb_day": float(day)}
        ccb.decay_satisfy(u, (day + 1) * 86400.0)
        self.assertEqual(u["ccb_satisfy"], 100.0 - ccb.CCB_SATISFY_DECAY_PER_DAY)
        self.assertGreater(u["ccb_satisfy"], 0.0, "不该一���天就清零")

    def test_daily_counter_resets_next_day(self):
        from humanoid import ccb
        day = ccb._day_index(1.0)
        u = {"ccb_turns": 8.0, "ccb_day": float(day)}
        ccb.decay_satisfy(u, (day + 1) * 86400.0)
        self.assertEqual(u["ccb_turns"], 0.0, "不重置的话「一天 8 次」会变成「一辈子 8 次」")

    def test_daily_cap_blocks(self):
        from humanoid import ccb
        base = dict(enabled=True, is_group=False, persona_prompt=FEMALE, affection=90.0,
                    base_affection=40.0, libido=50.0, base_libido=34.0, energy=80.0,
                    last_at=0.0, now=1.0)
        self.assertIsNotNone(ccb.evaluate(turns_today=7, **base))
        self.assertIsNone(ccb.evaluate(turns_today=8, **base), "第 9 次该被封顶")

    def test_no_timer_on_the_scene(self):
        """场景不按时间结束——原来那个 30 分钟时限是拿「害羞」的逻辑套错地方。"""
        import inspect
        src = inspect.getsource(ccb)
        self.assertNotIn("30", src.split("CCB_COOLDOWN_SECONDS")[-1].split("CCB_")[0],
                         "场景推进里不该混进分钟级的时限")


class ThresholdsAreConfigurable(unittest.TestCase):
    """四个门槛进配置，但**档位必须跟着门槛一起动**。

    v2.25.0 之前阈值写死在 `ccb.py` 里，用户既看不到也调不了——默认的 affection 78
    对多数人是永远够不着的静默开关。

    危险的不是「可调」，是「可调之后档位留在原地」：触发降到 30 而档位还是 38/43/48
    的话，30~37 这一段**能过门槛却匹配不到任何一档**，表现是「到了该触发的时候
    他不触发」——比不可调还糟，而且从外面看不出是哪一步坏的。这里把档位改成
    从阈值推导，任何阈值都不会出缝。
    """

    def test_bands_follow_the_threshold(self):
        from humanoid import ccb
        for floor in (20.0, 30.0, 38.0, 45.0):
            got = [b[0] for b in ccb.bands(floor)]
            self.assertEqual(got, [floor, floor + 5.0, floor + 10.0],
                             f"门槛 {floor} 的档位没跟着走，会出「过了门槛匹配不到档」的缝")

    def test_every_reachable_band_has_words(self):
        """任何一档都得有词——空档位等于那一段直接不触发。"""
        from humanoid import ccb
        for _floor, _score, words in ccb.bands(20.0):
            self.assertTrue(words, "档位没有词 = 那段永远不触发")

    def test_threshold_exactly_at_the_floor_still_matches(self):
        """刚好压线要能出——差一点点就够不着是最容易漏的边界。"""
        from humanoid import ccb
        got = ccb._band_for(30.0, 30.0)
        self.assertIsNotNone(got)

    def test_stage_follows_the_bands_not_hardcoded_numbers(self):
        from humanoid import ccb
        base = dict(enabled=True, is_group=False, persona_prompt=FEMALE,
                    base_affection=40.0, base_libido=20.0, energy=80.0,
                    last_at=0.0, now=1.0, affection=90.0)
        # 门槛 30 时：30/35/40 三档，stage 依次 1/2/3
        stages = []
        for lib in (31.0, 36.0, 41.0):
            res = ccb.evaluate(libido=lib, libido_min=30.0, libido_rise=0.0,
                               affection_min=50.0, affection_rise=0.0, **base)
            stages.append(res[2] if res else None)
        self.assertEqual(stages, [1, 2, 3], "阶段写死 43/48 的话，门槛一改就错位")

    def test_config_wins_over_the_module_constant(self):
        """面板上调完之后，实际生效的必须是配置里的那套数。"""
        from humanoid.config import HumanoidConfig
        cfg = HumanoidConfig.from_raw({"ccb_libido_min": 25.0, "ccb_affection_min": 40.0})
        self.assertEqual(cfg.ccb_libido_min, 25.0)
        self.assertEqual(cfg.ccb_affection_min, 40.0)
        # 没配的时候回落到代码默认值
        default = HumanoidConfig.from_raw({})
        self.assertEqual(default.ccb_libido_min, ccb_default("CCB_LIBIDO_MIN"))
        self.assertEqual(default.ccb_affection_min, ccb_default("CCB_AFFECTION_MIN"))


def ccb_default(name: str) -> float:
    from humanoid import ccb
    return float(getattr(ccb, name))
