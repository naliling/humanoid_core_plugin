"""v2.28.7：让模型每一轮都看得见「她的欲望在什么位置 / 在不在亲密场景里」。

用户原话：「core 的 ccb 场景没有在进行，也看不到性欲状态，以至于在操对方时
完全不知道 bot 有没有进入状态」。原因有两条，这一版各补一条：

* `PromptBuilder._ccb_presence_line()`：**常驻事实句**。原来 ccb 只有
  「条件全中、真的进场景」时才出一句内心话，其余轮次注入里关于欲望一个字都没有；
  而 `_emotion_line` 又是多候选竞争、每轮只出一条，欲望句经常被别的情绪压掉。
* `EmotionLayer._ccb_line()` 在读路径按天滚动状态：`decay_satisfy` 只挂在
  `advance_satisfy` 上，而进场景又被「当日 >= 8 次」挡住，于是第 8 次之后
  `ccb_turns` 永久卡死、ccb 再也不触发（*"计数不会变"* 的那个 P0）。

测试盯三类最容易静默出错的地方：

* **该出的时候出了**：不在场景给数字 + 相对基线的方向；在场景给「亲密 + 满足 + 次数」；
  余韵窗给剩余时间。
* **不该出的时候一个字都不出**：群聊 / ccb 关 / 开关关 / 人设安全闸 / 男角色。
* **跨天能重新进**：P0 bug 的回归。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from humanoid import ccb
from humanoid.config import HumanoidConfig
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.emotion import EmotionLayer
from humanoid.persona import PersonaSource
from humanoid.state import StateStore
from tests.fakes import FrozenClock, RecordingLogger

FEMALE = "你是小满，24岁。她说话很短，喜欢安静。"
MALE = "你是阿明，26岁男设计师。他说话很直接。"
MINOR = "你是小满，14岁。她说话很短。"

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "_conf_schema.json"


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


def _mood(core: HumanoidCoreInstance, **kw) -> dict:
    """给她一份「亲近欲 41（基线 34）」的情绪档案：够热但还没过 38/Δ4 那道门槛。"""
    value = {
        "affection": 92.0, "base_affection": 46.0, "libido": 41.0,
        "base_libido": 34.0, "aggression": 20.0, "base_aggression": 10.0,
        "turn_count": 60, "last_interaction": 0.0, "mood_tag": "", "last_decay": 0.0,
    }
    value.update(kw)
    core._scope.user_state("u1")["mood"] = value
    core._scope.set_self("energy", kw.get("energy", 70.0))
    return value


def _state(core: HumanoidCoreInstance, uid: str = "u1") -> dict:
    return core._scope.user_state(uid)


def _in_scene(core: HumanoidCoreInstance, stage: float = 2.0, satisfy: float = 45.0) -> dict:
    state = _state(core)
    now = core.now_epoch()
    state["ccb_stage"] = stage
    state["ccb_satisfy"] = satisfy
    state["ccb_pose"] = 0.0
    state["ccb_site"] = 1.0
    state["ccb_gate_at"] = now - 60.0
    state["ccb_last_at"] = now - 60.0
    return state


class DesireOutsideTheScene(unittest.TestCase):
    """不在场景时的常驻那句：数字 + 相对基线的方向，让模型判断"到没到"。"""

    def test_the_prompt_carries_number_and_direction(self):
        core = _core()
        _mood(core)
        text = core.build_injection("u1")
        self.assertIn("亲近欲 41/50", text)
        self.assertIn("比平常高 7", text)

    def test_when_shell_not_have_the_mood_it_says_so(self):
        core = _core()
        _mood(core, libido=22.0)
        text = core.build_injection("u1")
        self.assertIn("没有那个心思", text)
        self.assertIn("亲近欲 22/50", text)
        self.assertIn("比平常低 12", text)

    def test_it_appears_even_though_ccb_did_not_fire(self):
        """用户的核心诉求：场景没在进行，也要看得见欲望在哪。"""
        core = _core()
        _mood(core, libido=41.0)
        text = core.build_injection("u1")
        state = _state(core)
        self.assertEqual(float(state.get("ccb_stage", 0.0) or 0.0), 0.0, "这个用例里不该进场景")
        self.assertIn("她此刻的欲望", text)

    def test_near_the_threshold_it_says_how_far(self):
        core = _core()
        _mood(core, libido=36.0)
        text = core.build_injection("u1")
        self.assertIn("亲近欲 36/50", text)
        self.assertIn("还差 2 才到忍不住", text)

    def test_mood_only_mode_also_carries_it(self):
        """最轻的一档也要看得见——那是最常被用到的模式。"""
        core = _core(inject_activity_context="mood_only")
        _mood(core)
        self.assertIn("她此刻的欲望", core.build_injection("u1"))


class InsideTheScene(unittest.TestCase):
    """在场景里：明确写「正在亲密里」＋阶段＋满足度。"""

    def test_the_prompt_says_shell_in_a_scene(self):
        core = _core()
        _mood(core)
        _in_scene(core)
        text = core.build_injection("u1")
        self.assertIn("正在亲密里", text)
        self.assertIn("满足", text)
        self.assertRegex(text, r"满足 \d+/100")
        # 阶段名由 `_with_scene_note` 写在同一块事实里（45 + 40 = 85 → 临界）
        self.assertIn("临界", text)
        self.assertIn("后入", text)
        self.assertIn("今日第 1 次", text)

    def test_presence_line_does_not_repeat_the_phase_when_detail_is_on(self):
        """场景 note 已经写了阶段，presence 行就不再写一遍（别两句同一状态）。"""
        core = _core()
        _mood(core)
        _in_scene(core, satisfy=45.0)  # 45 → 平台
        line = core.prompt_builder._ccb_presence_line("u1")
        self.assertIn("正在亲密里", line)
        self.assertIn("满足 45/100", line)
        self.assertNotIn("平台", line, f"与场景 note 重复了阶段：{line!r}")

    def test_phase_name_fills_in_when_scene_detail_is_off(self):
        """场景 note 关掉时阶段只能由 presence 行给，否则模型彻底看不到阶段。"""
        core = _core(ccb_scene_detail=False)
        _mood(core)
        _in_scene(core, satisfy=45.0)
        line = core.prompt_builder._ccb_presence_line("u1")
        self.assertIn("平台", line)

    def test_a_recent_pose_change_is_reported(self):
        core = _core()
        _mood(core)
        state = _in_scene(core)
        state["ccb_log"] = [{"kind": "pose", "value": 0, "at": core.now_epoch() - 60.0}]
        self.assertIn("刚换过姿势", core.prompt_builder._ccb_presence_line("u1"))

    def test_an_old_pose_change_is_not_called_recent(self):
        core = _core()
        _mood(core)
        state = _in_scene(core)
        state["ccb_log"] = [{"kind": "pose", "value": 0, "at": core.now_epoch() - 3600.0}]
        self.assertNotIn("刚换过姿势", core.prompt_builder._ccb_presence_line("u1"))


class AftercarePresence(unittest.TestCase):
    def test_the_window_reports_how_long_is_left(self):
        core = _core()
        _mood(core)
        state = _state(core)
        state["ccb_stage"] = 0.0
        state["ccb_aftercare_until"] = core.now_epoch() + 3600.0
        line = core.prompt_builder._ccb_presence_line("u1")
        self.assertIn("余韵里", line)
        self.assertIn("还剩 60 分钟", line)

    def test_after_the_window_it_falls_back_to_desire(self):
        core = _core()
        _mood(core)
        state = _state(core)
        state["ccb_aftercare_until"] = core.now_epoch() - 10.0
        line = core.prompt_builder._ccb_presence_line("u1")
        self.assertNotIn("余韵里", line)
        self.assertIn("她此刻的欲望", line)


class HardGates(unittest.TestCase):
    """不该出的人/场合一个字都不出。"""

    def _assert_absent(self, text: str) -> None:
        for word in ("欲望", "正在亲密里", "余韵里"):
            self.assertNotIn(word, text, f"不该出现却出现了「{word}」：{text}")

    def test_ccb_off(self):
        core = _core(ccb_on=False)
        _mood(core)
        text = core.build_injection("u1")
        self.assertEqual(core.prompt_builder._ccb_presence_line("u1"), "")
        self._assert_absent(text)

    def test_group_chat(self):
        core = _core()
        _mood(core)
        _in_scene(core)
        text = core.build_injection("u1", is_group=True)
        self._assert_absent(text)

    def test_presence_switch_off(self):
        core = _core(ccb_presence=False)
        _mood(core)
        text = core.build_injection("u1")
        self.assertEqual(core.prompt_builder._ccb_presence_line("u1"), "")
        self._assert_absent(text)

    def test_minor_persona(self):
        core = _core(persona=MINOR)
        _mood(core)
        self._assert_absent(core.build_injection("u1"))

    def test_male_persona(self):
        core = _core(persona=MALE)
        _mood(core)
        self._assert_absent(core.build_injection("u1"))

    def test_ccb_off_keeps_the_other_three_modes_untouched(self):
        """ccb 关掉时这条注入必须与旧版一致：任何模式下都不多一个字。"""
        for mode in ("mood_only", "medium", "full"):
            with self.subTest(mode=mode):
                core = _core(ccb_on=False, inject_activity_context=mode)
                _mood(core)
                self._assert_absent(core.build_injection("u1"))


class Significance(unittest.TestCase):
    """显著度必须够高：token 预算裁剪时它是先被保住的那一批。"""

    def _presence_salience(self, sents) -> float:
        for text, salience in sents:
            if text and ("欲望" in text or "正在亲密里" in text):
                return float(salience)
        self.fail(f"组装结果里没有常驻状态句：{sents!r}")
        return 0.0

    def test_medium_and_full_keep_it_high(self):
        core = _core()
        _mood(core)
        builder = core.prompt_builder
        for name, builder_fn in (
            ("medium", builder._build_medium),
            ("full", builder._build_full),
        ):
            with self.subTest(mode=name):
                salience = self._presence_salience(
                    builder_fn("u1", False, [], {}, "", {})
                )
                self.assertGreaterEqual(salience, 9.0)
                self.assertLess(salience, 10.0, "不该压过场景/称呼那些必需品")


class BadData(unittest.TestCase):
    """拿不到数据 / 坏值 → 空串或少说一句，绝不抛、绝不写 nan。"""

    def test_broken_scope_returns_empty(self):
        core = _core()
        _mood(core)
        core._scope = object()  # 连 peek_user 都没有
        self.assertEqual(core.prompt_builder._ccb_presence_line("u1"), "")

    def test_nan_readings_do_not_reach_the_prompt(self):
        core = _core()
        core._scope.user_state("u1")["mood"] = {
            "libido": float("nan"), "base_libido": "不是数字",
        }
        line = core.prompt_builder._ccb_presence_line("u1")
        self.assertNotIn("nan", line.lower())
        self.assertIn("亲近欲 34/50", line)

    def test_no_profile_means_nothing_to_say(self):
        core = _core()
        self.assertEqual(core.prompt_builder._ccb_presence_line("stranger"), "")


class NewSwitch(unittest.TestCase):
    def test_default_on_and_round_trips(self):
        self.assertTrue(HumanoidConfig.from_raw({}).ccb_presence)
        self.assertFalse(HumanoidConfig.from_raw({"ccb_presence": False}).ccb_presence)

    def test_schema_registers_it_as_a_bool_default_on(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertIn("ccb_presence", schema)
        self.assertEqual(schema["ccb_presence"]["type"], "bool")
        self.assertIs(schema["ccb_presence"]["default"], True)
        self.assertIn("欲望", schema["ccb_presence"]["description"])
        # 面板顺序的不变量（test_ccb.py 也盯着这条）：ccb 组之后只能还是 ccb_ 项。
        keys = list(schema)
        idx = keys.index("ccb")
        strays = [k for k in keys[idx:] if k != "ccb" and not k.startswith("ccb_")]
        self.assertEqual(strays, [])


class DailyCapRegression(unittest.TestCase):
    """P0：第 8 次之后 `ccb_turns` 卡死、第二天再也进不去。

    根因链：`decay_satisfy` 只被 `advance_satisfy` 调（那是"真的进场景"才走的路），
    而"进场景"的条件里又有 `turns_today < 8`。打满 8 次之后两条路互为前提，
    `ccb_turns` 永久停在 8。修法是在读路径、进 `evaluate` 之前先按天滚一次。
    """

    def test_roll_is_a_noop_within_the_same_day(self):
        core = _core()
        _mood(core)
        state = _state(core)
        layer = EmotionLayer(core)
        now = core.now_epoch()
        state["ccb_day"] = float(ccb._day_index(now))
        state["ccb_turns"] = 3.0
        state["ccb_satisfy"] = 50.0
        self.assertFalse(layer._roll_ccb_day(state, now), "同一天不该动状态")
        self.assertEqual(float(state["ccb_turns"]), 3.0)

    def test_roll_resets_the_counter_across_days(self):
        core = _core()
        _mood(core)
        state = _state(core)
        layer = EmotionLayer(core)
        now = core.now_epoch()
        state["ccb_day"] = float(ccb._day_index(now))
        state["ccb_turns"] = 8.0
        state["ccb_satisfy"] = 80.0
        self.assertTrue(layer._roll_ccb_day(state, now + 86400.0))
        self.assertEqual(float(state["ccb_turns"]), 0.0, "「一天 8 次」不能变成「一辈子 8 次」")
        self.assertLess(float(state["ccb_satisfy"]), 80.0)

    def _capped_state(self, core) -> dict:
        state = _state(core)
        now = core.now_epoch()
        gate = now - ccb.CCB_COOLDOWN_SECONDS - 60.0
        state["ccb_turns"] = 8.0
        state["ccb_day"] = float(ccb._day_index(now))
        state["ccb_satisfy"] = 0.0
        state["ccb_gate_at"] = gate
        state["ccb_last_at"] = gate
        return state

    def test_the_counter_rolls_on_the_read_path(self):
        core = _core()
        _mood(core, libido=47.0)
        state = self._capped_state(core)
        # 当天：封顶，进不去，计数也不该变（这里没有跨天）
        core.build_injection("u1")
        self.assertEqual(float(state["ccb_turns"]), 8.0)
        # 第二天：即使这一轮仍然进不去（没有亲密信号），计数也必须已经回到 0
        core.clock.advance(days=1)
        core.build_injection("u1")
        self.assertEqual(float(state["ccb_turns"]), 0.0)

    def test_she_can_enter_again_the_next_day(self):
        core = _core()
        _mood(core, libido=47.0)
        state = self._capped_state(core)
        core.build_injection("u1")
        self.assertEqual(float(state.get("ccb_stage", 0.0) or 0.0), 0.0, "当天已经封顶")
        core.clock.advance(days=1)
        state["ccb_intimacy_at"] = core.now_epoch()  # 第二天有亲密信号
        core.build_injection("u1")
        self.assertGreater(
            float(state.get("ccb_stage", 0.0) or 0.0), 0.0,
            "第二天必须能重新进场景（否则就是 `ccb_turns` 永久卡死的 P0）",
        )
        self.assertEqual(
            float(state.get("ccb_day", 0.0) or 0.0),
            float(ccb._day_index(core.now_epoch())),
            "跨天滚动必须把 ccb_day 推到今天",
        )


if __name__ == "__main__":
    unittest.main()
