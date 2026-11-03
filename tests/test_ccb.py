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
        # E1 三档：<50 拒绝/岔开；50~74 擦边；≥75 深入。
        self.assertIsNone(ccb.evaluate(**_ok(affection=45.0, base_affection=40.0)))
        self.assertIsNotNone(ccb.evaluate(**_ok(affection=70.0, base_affection=46.0)))
        # 涨得不够、又没到深入档 → 不进
        self.assertIsNone(ccb.evaluate(**_ok(affection=60.0, base_affection=56.0)))
        # 深入档（≥75）算「关系已经够深」，不再要求这一阵涨过 8 分——
        # 不然最亲密的那批人（好感 100/100、亲近欲 50/50）永远卡在「涨得不够」上。
        self.assertIsNotNone(ccb.evaluate(**_ok(affection=92.0, base_affection=90.0)))

    def test_deep_affection_unlocks_deeper_stage(self):
        # 好感没到「深入」档时只给最浅一档（擦边）。
        shallow = ccb.evaluate(**_ok(affection=60.0, base_affection=46.0, libido=49.0, base_libido=34.0))
        deep = ccb.evaluate(**_ok(affection=90.0, base_affection=46.0, libido=49.0, base_libido=34.0))
        self.assertIsNotNone(shallow)
        self.assertIsNotNone(deep)
        self.assertEqual(shallow[2], 1)
        self.assertEqual(deep[2], 3)

    def test_coerced_never_fires(self):
        # E4/E6：刚被强迫过 → 不进入。
        self.assertIsNone(ccb.evaluate(**_ok(coerced=True)))

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
        # 
        # 退回关键词）。这一组用例验证的是**数值门槛**，所以直接给一个兜底信号，
        # 让它们继续只测原来那件事——进门判断本身由 `test_v2286_scene.py` 的
        # `SceneGate` 单独钉。
        core._scope.user_state("u1")["ccb_intimacy_at"] = core.now_epoch()

    def _line(self, core, is_group=False):
        import re
        m = re.search(r"内心：([^，。\n]*)", core.build_injection("u1", is_group=is_group))
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
        # 亲近欲掉回基线 → 门槛真的掉了，场景结束、清掉场景状态，不留尾巴
        self._mood(core, libido=35.0, base_libido=34.0)
        self._line(core)
        self.assertEqual(us.get("ccb_stage"), 0.0)

    def test_cooldown_does_not_wipe_records(self):
        """冷却期内再请求，只该保持场景，**不该把记录清掉**。

        原来 `evaluate` 因冷却返回 None 时，`_ccb_line` 一律 `reset_state()`，把
        `ccb_last_at` / `ccb_turns` / `ccb_done_count` 一起清空——冷却随之失效
        （下一轮又能触发），一辈子的次数也归零（社交层靠它判断有没有色色过）。
        """
        core = self._core(True)
        self._mood(core)
        us = core._scope.user_state("u1")
        # 第一次：条件全中 → 进场景。进场景只记「今天第 1 场」，不是「发生过」。
        self._line(core)
        self.assertEqual(us.get("ccb_turns"), 1.0, "进一场 = 当日 1 次")
        self.assertEqual(us.get("ccb_done_count", 0.0), 0.0, "推进场景不是发生")
        # 记一次「真的发生过」（记录层唯一入口），再验证冷却不会把它清掉。
        ccb.note_occurrence(us, float(us["ccb_activity_at"]))
        first_at = us.get("ccb_last_at")
        self.assertGreater(first_at, 0.0)
        # 第二次：同一个人、数值不变、时间没走 → 落在冷却里 → 返回 None
        self._line(core)
        self.assertEqual(us.get("ccb_done_count"), 1.0, "冷却不该把一辈子的次数清掉")
        self.assertEqual(us.get("ccb_last_at"), first_at, "冷却不该把上次发生的时间清掉")
        self.assertEqual(us.get("ccb_turns"), 1.0, "冷却不该把当日次数清掉")

    def test_idle_scene_ends_instead_of_resuming(self):
        """F8：6 小时没人碰过的那一场，状态机也要结束它，而不是隔夜续场。"""
        core = self._core(True)
        # 门槛不满足：结束之后不该立刻又开一场，否则断言分不清「重置」和「续场」。
        self._mood(core, libido=35.0, base_libido=34.0)
        us = core._scope.user_state("u1")
        now = core.now_epoch()
        us.update({"ccb_stage": 2.0, "ccb_satisfy": 40.0, "ccb_pose": 2.0,
                   "ccb_activity_at": now - 7 * 3600.0,
                   "ccb_last_at": now - 7 * 3600.0})
        self.assertFalse(ccb.scene_active(us, now), "7 小时没动静，与面板同口径已过期")
        core.build_injection("u1")
        self.assertEqual(float(us.get("ccb_stage", 0.0) or 0.0), 0.0,
                         "状态机该真的结束这一场，不能隔夜续着姿势与满足度")
        self.assertEqual(float(us.get("ccb_satisfy", 0.0) or 0.0), 0.0, "满足度随场景结束清掉")


class State(unittest.TestCase):
    def test_state_roundtrip(self):
        u = {"ccb_stage": 2.0, "ccb_last_at": 5.0, "ccb_satisfy": 80.0,
             "ccb_day": 3.0, "ccb_turns": 4.0, "ccb_done_count": 7.0}
        st = ccb.read_state(u)
        self.assertEqual(st["ccb_stage"], 2.0)
        self.assertEqual(st["ccb_satisfy"], 80.0)
        # 场景结束只清场景状态，记录（冷却时间/当日次数/一辈子的次数）必须留
        ccb.reset_scene(u)
        self.assertEqual(ccb.read_state(u)["ccb_stage"], 0.0)
        self.assertEqual(ccb.read_state(u)["ccb_satisfy"], 0.0)
        self.assertEqual(u["ccb_last_at"], 5.0)
        self.assertEqual(u["ccb_turns"], 4.0)
        self.assertEqual(u["ccb_done_count"], 7.0)

    def test_scene_lapsed_ignores_transient_conditions(self):
        """「场景结束」只看门槛；冷却/当日封顶/群里/精力见底都**不算**结束。"""
        ok = dict(is_group=False, persona_prompt=FEMALE, affection=92.0,
                  base_affection=46.0, libido=47.0, base_libido=34.0)
        self.assertFalse(ccb.scene_lapsed(**ok))
        # 色欲掉下门槛 → 真的结束
        self.assertTrue(ccb.scene_lapsed(**{**ok, "libido": 30.0}))
        # 好感掉下门槛 → 真的结束
        self.assertTrue(ccb.scene_lapsed(**{**ok, "affection": 40.0, "base_affection": 40.0}))
        # 群里不是「结束」，只是进不去
        self.assertFalse(ccb.scene_lapsed(**{**ok, "is_group": True}))

    def test_config_lands_last_in_schema(self):
        import json
        data = json.loads(
            (Path(__file__).resolve().parent.parent / "_conf_schema.json").read_text(encoding="utf-8"))
        keys = list(data)
        # 开关本身仍然要待在**最后那一组**里——它是个默认关闭的隐藏开关，
        # 排在配置面板最底下才不显眼。
        #
        # 
        # （v2.25.0 从"必须是最后一个"放宽到 5，v2.28.6 改成 8，。
        # 窗口本身是个错误的不变量：它盯的是"恰好有几个"，而真正要守的是
        # **这一组必须在面板最末尾**。改成结构性断言：`"ccb"` 之后**不许再出现
        # 任何不是 ccb_ 开头的配置项**。这样加参数不用改测试，上移一格立刻命中。
        idx = keys.index("ccb")
        strays = [k for k in keys[idx:] if k != "ccb" and not k.startswith("ccb_")]
        self.assertEqual(
            strays, [],
            f"ccb 组之后混进了别的配置项：{strays}（这一组必须待在面板最底下）",
        )
        self.assertLess(keys.index("ccb"), keys.index("ccb_libido_min"),
                        "开关应当在它的参数之前")
        self.assertEqual(data["ccb"]["default"], False)
        # v2.28.0（C6）：这里原来断言 `description == "ccb"`，而面板上的实际描述
        # 早就是一句完整的中文说明。基线因此 1 failed——这是陈旧断言，不是功能回归；
        # 改成盯「它还是一句说清用途的描述」，不再把文案写死在测试里。
        self.assertTrue(
            str(data["ccb"]["description"]).startswith("亲密场景模拟"),
            f"ccb 的描述不像给用户看的那句：{data['ccb']['description']!r}",
        )
        # 四个门槛必须存在且与代码默认值一致——不一致的话用户按面板调，
        # 实际生效的是另一套数。
        from humanoid import ccb as _c
        self.assertEqual(data["ccb_libido_min"]["default"], _c.CCB_LIBIDO_MIN)
        self.assertEqual(data["ccb_libido_rise"]["default"], _c.CCB_LIBIDO_RISE)
        self.assertEqual(data["ccb_affection_min"]["default"], _c.CCB_AFFECTION_MIN)
        self.assertEqual(data["ccb_affection_rise"]["default"], _c.CCB_AFFECTION_RISE)
        self.assertEqual(data["ccb_affection_deep"]["default"], _c.CCB_AFFECTION_DEEP)

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
        self.assertGreater(u["ccb_satisfy"], 0.0, "不该一天就清零")

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
        # 门槛取「基线 + 每日可达涨幅」以内，别让可达性夹取参与进来——
        # 这条测的是「档位跟着门槛走」，不是可达性（那个由 test_v22812_ccb_reach 盯）。
        base = dict(enabled=True, is_group=False, persona_prompt=FEMALE,
                    base_affection=40.0, base_libido=20.0, energy=80.0,
                    last_at=0.0, now=1.0, affection=90.0)
        # 门槛 24 时：24/29/34 三档，stage 依次 1/2/3
        stages = []
        for lib in (25.0, 30.0, 35.0):
            res = ccb.evaluate(libido=lib, libido_min=24.0, libido_rise=0.0,
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

class CcbDayTimezoneTest(unittest.TestCase):
    """#29/#79：ccb 的按天账按**她的日历**算，不能按宿主本地日。

    同一 epoch 秒在东京与洛杉矶不是同一天：日键写死宿主日，跨日时刻就会在
    她那边的半夜翻页——「当天 8 次」与按天回落都跟着错。
    """

    @staticmethod
    def _tz(hours: int):
        from datetime import timedelta, timezone
        return timezone(timedelta(hours=hours))

    def test_day_index_follows_the_injected_timezone(self):
        from datetime import timezone
        moment = datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc).timestamp()
        tokyo, la = self._tz(9), self._tz(-8)
        self.assertEqual(ccb._day_index(moment, tokyo) - ccb._day_index(moment, la), 1,
                         "UTC 23:30 时东京已是 1 月 2 日、洛杉矶还是 1 月 1 日")
        # 不传 tz 时保持旧口径（宿主本地日），注入不改变默认行为。
        self.assertEqual(ccb._day_index(moment), ccb._day_index(moment, None))

    def test_decay_follows_her_midnight(self):
        from datetime import timezone
        moment = datetime(2026, 1, 1, 15, 30, tzinfo=timezone.utc).timestamp()
        tokyo, la = self._tz(9), self._tz(-8)
        # 东京那边已经是 1 月 2 日 00:30（账记在 3 小时前的 1 月 1 日）；
        # 洛杉矶那边还是 1 月 1 日 07:30。
        state = {"ccb_day": float(ccb._day_index(moment - 3 * 3600, tokyo)),
                 "ccb_satisfy": 100.0, "ccb_turns": 3.0}
        ccb.decay_satisfy(state, moment, tz=tokyo)
        self.assertLess(state["ccb_satisfy"], 100.0, "她那边已翻页，必须回落")
        self.assertEqual(state["ccb_turns"], 0.0)
        state2 = {"ccb_day": float(ccb._day_index(moment, la)),
                  "ccb_satisfy": 100.0, "ccb_turns": 3.0}
        ccb.decay_satisfy(state2, moment, tz=la)
        self.assertEqual(state2["ccb_satisfy"], 100.0, "洛杉矶还没翻页，不该动账")
        self.assertEqual(state2["ccb_turns"], 3.0)

    def test_roll_day_and_pending_days_take_the_same_tz(self):
        from datetime import timezone
        moment = datetime(2026, 1, 1, 15, 30, tzinfo=timezone.utc).timestamp()
        tokyo = self._tz(9)
        state = {"ccb_day": float(ccb._day_index(moment - 3 * 3600, tokyo)),
                 "ccb_satisfy": 100.0, "ccb_turns": 8.0}
        self.assertEqual(ccb.pending_days(state, moment, tokyo), 1)
        self.assertTrue(ccb.roll_day(state, moment, tokyo))
        self.assertEqual(state["ccb_turns"], 0.0, "新的一天：当日次数归零")


class ThresholdHasOneStory(unittest.TestCase):
    """#158：同一个门槛在注释里有三套互相矛盾的说法（38/50、45/50、「收到 10」）。

    改门槛的人只能靠猜哪套是真的；更糟的是「涨 10」是配 45/50 那套的历史值，
    绝对值降到 38 之后它让门槛永远够不着。这里钉住两件事：常量就是唯一说法，
    注入层/面板读的是同一组常量（`prompt_builder` 与 `scene_status` 都引用它们）。
    """

    def test_the_floor_is_38_out_of_50(self):
        from humanoid import ccb
        self.assertEqual(ccb.CCB_LIBIDO_MIN, 38.0)
        self.assertEqual(ccb.LIBIDO_MAX, 50.0)

    def test_the_rise_is_paired_with_the_floor(self):
        """默认基线 34 + 涨幅 4 = 38，正好够到绝对门槛；涨 10 就永远过不去。"""
        from humanoid import ccb
        self.assertEqual(ccb.CCB_LIBIDO_RISE, 4.0)
        self.assertEqual(34.0 + ccb.CCB_LIBIDO_RISE, ccb.CCB_LIBIDO_MIN)

    def test_no_second_version_of_the_threshold_in_the_source(self):
        """源码里不许再出现「45/50」「收到 10」这类历史说法。"""
        import inspect
        from humanoid import ccb
        src = inspect.getsource(ccb)
        for stale in ("45/50", "收到 10"):
            self.assertNotIn(stale, src, f"ccb.py 里还留着「{stale}」这套说法")

    def test_the_floor_is_the_same_one_the_injection_uses(self):
        """注入层与面板读常量，不各自抄一份数字。"""
        import inspect
        from humanoid import ccb, prompt_builder
        src = inspect.getsource(prompt_builder)
        self.assertIn("CCB_LIBIDO_MIN", src)
        self.assertIn("CCB_LIBIDO_RISE", src)


class PhaseNamesAreFeelings(unittest.TestCase):
    """#12：阶段名必须是**她的感受**，不是临床分期。

    第 2 档原来写「平台」——Masters & Johnson 四阶段模型里 plateau 的直译，混在
    动情/临界/爆发/余韵 里语域不一致（其余四档都是感受，唯独这个是教科书术语），
    中文里「平台」还首先指产品/场所。改名不能只改一处字符串：注入（scene_note、
    prompt_builder）与面板（scene_status.stage_name）都读 PHASE_NAMES 这一份词表，
    所以这里钉住词表本身，而不是某一条注入句子。
    """

    def test_no_clinical_term_in_the_phase_names(self):
        from humanoid import ccb
        self.assertNotIn("平台", ccb.PHASE_NAMES)
        self.assertEqual(ccb.PHASE_NAMES[2], "沉醉")

    def test_every_phase_name_has_the_same_register_and_length(self):
        """五档同语域：都是两字感受词，空档 0 例外。"""
        from humanoid import ccb
        self.assertEqual(ccb.PHASE_NAMES[0], "")
        for i, name in enumerate(ccb.PHASE_NAMES[1:], 1):
            with self.subTest(phase=i):
                self.assertEqual(len(name), 2, f"第 {i} 档「{name}」长度与邻居不一致")

    def test_injection_never_carries_stage_names(self):
        """注入里不出现状态机档位名（#62，与 #12 改名的分工）。

        面板 / `/状态` / API 仍然读 `PHASE_NAMES` 这一份词表（运营看档位），但**注入侧
        不许**把档位名拼给模型：README 承诺「只把背景递给她」，而「临界/爆发」是状态机
        档位名，模型会当成事实复述。注入侧的措辞由 emotion 的感受表给出，而感受表的
        阈值仍从 ccb 的相位常量推导——是同一份事实的两种受众，不是两套数字（#158 的教训
        是「同一个数字写两遍」，不是「同一个事实换个说法」）。
        """
        import inspect
        from humanoid import ccb, emotion, prompt_builder
        # 面板 / API 侧照旧：档位名给运营看。
        self.assertIn("沉醉", ccb.scene_note(pose=-1, site=-1, phase=2, over=None))
        # 注入侧不许自己拼档位名。
        self.assertNotIn("PHASE_NAMES", inspect.getsource(prompt_builder),
                         "注入侧不许直接拼档位名，感受词由 emotion 的感受表给出")
        # 注入侧唯一取 scene_note 的地方必须把 phase 钉成 0（档位由感受表翻词）。
        # 查的是**调用参数**，不是源码里有没有出现过「余韵」二字——注释和 docstring
        # 里讲这套分工是应该的，按全文子串查会把说明文字也判成违规。
        self.assertIn("phase=0", inspect.getsource(emotion.EmotionLayer._with_scene_note),
                      "注入侧调 scene_note 时必须 phase=0，否则档位名会漏进上下文")
        # 感受词表与档位名表必须同长（索引对齐），且一条都不含档位名。
        self.assertEqual(len(emotion._SCENE_FEELINGS), len(ccb.PHASE_NAMES))
        for name in ccb.PHASE_NAMES[1:]:
            for feeling in emotion._SCENE_FEELINGS:
                self.assertNotIn(name, feeling, f"感受词「{feeling}」里含档位名「{name}」")

class RefusalIsNotSwallowedByAJoke(unittest.TestCase):
    """#80/#127：玩笑词只在命中词附近豁免，不能整句否决拒绝判定。

    旧实现是 `if any(j in s for j in _CCB_JOKE): return False`：只要句子里出现
    「哼 / 才不 / 哈哈 / 讨厌啦」，整句就不是拒绝。于是一条「住手，哈哈」在结算里
    被判成没拒绝——好感 +2 而不是 -4，攻击性还往下走。
    """

    def test_hard_refusal_survives_filler_words(self):
        for text in ("住手，哈哈", "哼，住手！", "放开我，哼", "我不愿意，哈哈"):
            with self.subTest(text=text):
                self.assertTrue(ccb.detect_refusal_hard(text))
                self.assertTrue(ccb.detect_refusal(text))

    def test_joke_exempts_only_near_the_refusal_word(self):
        # 玩笑词紧贴拒绝词 → 当玩笑（欲拒还迎），不判拒绝。
        self.assertFalse(ccb.detect_refusal("不要，逗你玩的"))
        # 玩笑词离得远 → 后半句是真拒绝；硬拒绝也一样，只豁免命中处附近。
        self.assertTrue(ccb.detect_refusal("开玩笑的啦，不过我真的不要了"))
        self.assertTrue(ccb.detect_refusal_hard("笑死，你居然让我住手"))

    def test_filler_words_are_no_longer_joke_words(self):
        for word in ("哼", "才不", "哈哈"):
            self.assertNotIn(word, ccb._CCB_JOKE, f"「{word}」是语气词，不是开玩笑的信号")
        self.assertFalse(ccb.detect_refusal("才不呢"))
        self.assertTrue(ccb.detect_refusal("不行，哈哈"))

    def test_coquettish_tao_yan_la_is_not_a_refusal(self):
        """撒娇「讨厌啦」是玩笑词（lead 拍板），但裸「讨厌」仍算拒绝。"""
        self.assertIn("讨厌啦", ccb._CCB_JOKE)
        self.assertNotIn("讨厌", ccb._CCB_JOKE)
        self.assertFalse(ccb.detect_refusal("讨厌啦"))
        self.assertFalse(ccb.detect_refusal_hard("讨厌啦"))
        # 硬拒绝先查：同一句里带「讨厌啦」也吞不掉「住手」。
        self.assertTrue(ccb.detect_refusal_hard("住手，讨厌啦"))
        self.assertTrue(ccb.detect_refusal("住手，讨厌啦"))
        self.assertTrue(ccb.detect_refusal("真讨厌"))

    def test_a_refused_occurrence_settles_as_refused(self):
        """同一条消息只该有一个结论：判成拒绝 → 好感掉 4，不是涨 2。"""
        profile = {"affection": 50.0, "libido": 34.0, "aggression": 10.0, "base_affection": 46.0}
        state = {"ccb_done_count": 3.0}
        refused = ccb.detect_refusal("住手，哈哈")
        out = ccb.occurrence_effects(state, profile, refused=refused)
        self.assertEqual(out["affection"], 46.0)
        self.assertEqual(out["aggression"], 16.0)
        # 对照：真的在开玩笑时仍按「没拒绝」结算（非第一次且已到基线 → +2+3）。
        joke = ccb.occurrence_effects(state, profile, refused=ccb.detect_refusal("不要，逗你玩的"))
        self.assertEqual(joke["affection"], 55.0)

