"""v2.28.7 — 「她这会儿在不在状态」必须看得见，而且数字不能停在昨天。

用户的两句原话就是这一批的全部验收标准：

* 「core 的 ccb 场景没有在进行，也看不到性欲状态，以至于在操对方时完全不知道
  bot 有没有进入状态」→ 四个出口（`/你的状态`、`/亲密状态`、`/好感度`、门面）
  都要能读到同一份「亲近欲 / 门槛 / 场景 / 余韵 / 冷却 / 能不能进」。
* 「展示出来的东西可能被误导，以前留下的计数不会变，尤其是指令显示出来的」
  → 计数必须按天滚动；第 8 次打满之后第二天必须能重新进入（这是 P0：
  `decay_satisfy` 只在 `advance_satisfy` 里跑，而后者只在真的进场景之后才跑，
  于是 `ccb_turns=8` → `evaluate` 永远 None → 永远不结账 → 永远卡在 8）。

分组：
* `DailyRoll`     —— P0 回归：跨天结账、第 8 次后第二天仍可进入
* `Snapshot`      —— `ccb.scene_status()`：每一道闸/过期场景/余韵冷却/纯函数性
* `StatusPage`    —— `/你的状态` 的「亲密」段（私聊露、群聊不露、ccb 关不露）
* `IntimacyText`  —— `/亲密状态` 指令与状态页同一份数据
* `MoodText`      —— `/好感度` 那句亲密档案（**无副作用**）
* `Facade`        —— `api.intimacy()` / `api._relationship()`
* `Command`       —— `main.py` 的新指令（私聊答、群聊不答、ccb 关不答）
"""
from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from tests.astrbot_stub import install as _install_astrbot_stub

_install_astrbot_stub()

from humanoid import api, ccb  # noqa: E402
from humanoid.config import HumanoidConfig  # noqa: E402
from humanoid.core_instance import HumanoidCoreInstance  # noqa: E402
from humanoid.persona import PersonaSource  # noqa: E402
from humanoid.state import StateStore  # noqa: E402
from tests.fakes import FrozenClock, RecordingLogger  # noqa: E402

FEMALE = "你是小满，24岁。她说话很短，喜欢安静。"
MALE = "你是阿明，26岁男设计师。他说话很直接。"


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


def _mood(core: HumanoidCoreInstance, uid: str = "u1", **kw) -> dict:
    """给这个用户一份「一切都够了」的情绪档案（改哪一项由 kw 决定）。"""
    value = {
        "affection": 92.0, "base_affection": 46.0, "libido": 47.0,
        "base_libido": 34.0, "aggression": 20.0, "base_aggression": 10.0,
        "turn_count": 60, "last_interaction": 0.0, "mood_tag": "", "last_decay": 0.0,
    }
    value.update({k: v for k, v in kw.items() if k in value})
    core._scope.user_state(uid)["mood"] = value
    core._scope.set_self("energy", kw.get("energy", 70.0))
    return value


def _state(core: HumanoidCoreInstance, uid: str = "u1") -> dict:
    return core._scope.user_state(uid)


def _ready(core: HumanoidCoreInstance, uid: str = "u1") -> dict:
    """所有门槛都够、刚收到亲密信号的状态：`can_enter` 必须为真。"""
    _mood(core, uid)
    now = core.now_epoch()
    state = _state(core, uid)
    state["ccb_intimacy_at"] = now
    state["ccb_day"] = float(ccb._day_index(now))
    return state


def _snap(**kw):
    """`ccb.scene_status()` 的默认参数：全中（能进）。"""
    user_state = kw.pop("user_state", {})
    now = kw.pop("now", 1_000_000.0)
    base = dict(
        libido=47.0, base_libido=34.0, affection=92.0, base_affection=46.0,
        energy=70.0, intimacy_hint=True,
    )
    base.update(kw)
    return ccb.scene_status(user_state, now, **base)


class DailyRoll(unittest.TestCase):
    """P0：计数永久锁死。"""

    def test_pending_days_counts_natural_days(self):
        now = 1_000_000.0
        state = {"ccb_day": float(ccb._day_index(now))}
        self.assertEqual(ccb.pending_days(state, now), 0)
        self.assertEqual(ccb.pending_days(state, now + 3600.0), 0, "同一天不该算跨天")
        self.assertEqual(ccb.pending_days(state, now + 86400.0), 1)
        self.assertEqual(ccb.pending_days(state, now + 3 * 86400.0), 3)

    def test_roll_day_resets_the_daily_counter(self):
        now = 1_000_000.0
        state = {
            "ccb_day": float(ccb._day_index(now)),
            "ccb_turns": 8.0,
            "ccb_satisfy": 100.0,
            "ccb_done_count": 12.0,
        }
        self.assertTrue(ccb.roll_day(state, now + 86400.0), "跨天应当真的滚动了")
        self.assertEqual(state["ccb_turns"], 0.0, "当日次数必须归零，否则永远卡在封顶")
        self.assertEqual(state["ccb_satisfy"], 100.0 - ccb.CCB_SATISFY_DECAY_PER_DAY)
        self.assertEqual(state["ccb_done_count"], 12.0, "一辈子的次数不该被跨天清掉")

    def test_roll_day_is_idempotent_within_the_same_day(self):
        now = 1_000_000.0
        state = {"ccb_day": float(ccb._day_index(now)), "ccb_turns": 3.0,
                 "ccb_satisfy": 50.0}
        self.assertFalse(ccb.roll_day(state, now))
        self.assertFalse(ccb.roll_day(state, now + 60.0))
        self.assertEqual(state["ccb_turns"], 3.0, "同一天重复调用不该动计数")
        self.assertEqual(state["ccb_satisfy"], 50.0)

    def test_first_day_eighth_time_then_next_day_can_enter(self):
        """回归本体：day1 打满 8 次，day2 必须能重新进场景。

        原来的链条：`turns_today=8` → `evaluate` 直接 None → 不进场景 →
        `advance_satisfy`/`decay_satisfy` 都不跑 → 第二天仍读到 8 → 永远不进。
        所以这里同时钉「roll_day 之后 evaluate 放行」与「status dict 的封顶消失」。
        """
        core = _core(True)
        state = _ready(core)
        now = core.now_epoch()
        state.update({
            "ccb_turns": 8.0,
            "ccb_done_count": 9.0,
            "ccb_satisfy": 40.0,
            "ccb_last_at": now - 6 * 3600.0,
            "ccb_gate_at": now - 6 * 3600.0,
            "ccb_intimacy_at": now,
        })
        day1 = core.intimacy_status("u1")
        self.assertEqual(day1["turns_today"], 8)
        self.assertFalse(day1["can_enter"])
        self.assertTrue(
            any("封顶" in b for b in day1["blockers"]),
            f"day1 应该被当日封顶挡住：{day1['blockers']}",
        )

        core.clock.advance(days=1)
        # day2 又来了一句亲密的话：兜底信号刷新（`emotion._ccb_line` 的读路径也
        # 会做同一件事——先把当天的账结掉，再进 evaluate）。
        _state(core, "u1")["ccb_intimacy_at"] = core.now_epoch()
        day2 = core.intimacy_status("u1")
        self.assertEqual(day2["turns_today"], 0, "跨天之后当日次数必须归零")
        self.assertTrue(day2["can_enter"], f"day2 应当能重新进入：{day2['blockers']}")
        self.assertFalse(any("封顶" in b for b in day2["blockers"]))
        self.assertEqual(_state(core, "u1").get("ccb_turns"), 0.0, "读路径要真的写回")

        turns_today = day2["turns_today"]
        self.assertIsNotNone(ccb.evaluate(
            enabled=True, is_group=False, persona_prompt=FEMALE,
            affection=92.0, base_affection=46.0, libido=47.0, base_libido=34.0,
            energy=70.0, last_at=0.0, now=core.now_epoch(),
            turns_today=turns_today, intimacy_hint=True,
        ), "结账之后 evaluate 必须重新放行")

    def test_status_read_path_marks_dirty_only_when_it_rolls(self):
        core = _core(True)
        state = _ready(core)
        now = core.now_epoch()
        state.update({"ccb_turns": 8.0, "ccb_last_at": now - 6 * 3600.0,
                      "ccb_gate_at": now - 6 * 3600.0})
        core._state_store.flush_sync()  # 先把脏标记清掉，看看读一次会不会写
        if core._state_store.dirty:  # 落盘失败时不影响断言（环境相关的保守处理）
            return
        core.intimacy_status("u1")
        self.assertFalse(core._state_store.dirty, "同一天看一眼状态不该标脏")
        core.clock.advance(days=1)
        core.intimacy_status("u1")
        self.assertTrue(core._state_store.dirty, "真的跨天了，结账结果必须落盘")


class Snapshot(unittest.TestCase):
    """`ccb.scene_status()`：只读快照的每一格都要说得清。"""

    def test_all_conditions_met_has_no_blocker(self):
        snap = _snap()
        self.assertTrue(snap["can_enter"])
        self.assertEqual(snap["blockers"], [])

    def test_each_gate_reports_a_chinese_reason(self):
        cases = {
            "群聊": dict(is_group=True),
            "人设安全闸": dict(safety_reason="人设年龄 16 岁，未成年"),
            "精力不足": dict(energy=5.0),
            "冷却中": dict(user_state={"ccb_gate_at": 1_000_000.0 - 600.0}),
            "当日已封顶": dict(user_state={"ccb_turns": 8.0}),
            "好感不够": dict(affection=20.0, base_affection=20.0),
            "好感涨得不够": dict(affection=52.0, base_affection=48.0),
            "亲近欲不够": dict(libido=20.0, base_libido=20.0),
            "亲近欲涨得不够": dict(libido=40.0, base_libido=38.0),
            "场景分未达标": dict(scene_score=0.4),
            "暂无模型判断": dict(scene_score=-1.0, intimacy_hint=False),
            "她刚拒绝": dict(user_state={"ccb_refused_at": 1_000_000.0 - 60.0}),
            "男角色": dict(persona_prompt=MALE),
            "他刚越界": dict(coerced=True),
        }
        for keyword, kwargs in cases.items():
            with self.subTest(gate=keyword):
                snap = _snap(**kwargs)
                self.assertFalse(snap["can_enter"], f"{keyword} 应当挡住")
                self.assertTrue(
                    any(keyword in reason for reason in snap["blockers"]),
                    f"{keyword} 没出现在 blockers 里：{snap['blockers']}",
                )

    def test_blockers_include_the_numbers(self):
        """光说「不够」没用：面板要能看出还差多少。

        基线 30 时绝对门槛按「可达上限」算成 36（见 test_v22812_ccb_reach），
        所以这里取 31.5 才能看到「差 4」——旧版 35 已经过得了可达门槛了。
        """
        snap = _snap(libido=31.5, base_libido=30.0, energy=12.0)
        text = "；".join(snap["blockers"])
        self.assertIn("差 4", text, f"亲近欲离门槛还差多少没给出：{text}")
        self.assertIn("30", text, f"精力门槛数字没给出：{text}")

    def test_in_scene_reports_phase_and_counts(self):
        now = 1_000_000.0
        snap = _snap(
            now=now,
            user_state={
                "ccb_stage": 2.0, "ccb_satisfy": 50.0, "ccb_turns": 3.0,
                "ccb_done_count": 7.0, "ccb_last_at": now - 600.0,
                "ccb_gate_at": now - 600.0, "ccb_peak_depth": 11.0,
                "ccb_pose": 0.0, "ccb_site": 1.0,
            },
        )
        self.assertTrue(snap["in_scene"])
        self.assertFalse(snap["expired"])
        self.assertEqual(snap["stage"], 2)
        self.assertEqual(snap["stage_name"], "平台")
        self.assertEqual(snap["satisfy"], 50.0)
        self.assertEqual(snap["satisfy_phase"], 2)
        self.assertEqual(snap["turns_today"], 3)
        self.assertEqual(snap["daily_max"], ccb.CCB_DAILY_MAX)
        self.assertEqual(snap["done_count"], 7)
        self.assertAlmostEqual(snap["last_ago"], 600.0, places=1)
        self.assertEqual(snap["cooldown_left"], 0.0, "场景里冷却不适用，不该报倒计时")
        self.assertEqual(snap["peak_depth"], 11.0)
        self.assertEqual(snap["pose_name"], "后入")
        self.assertEqual(snap["site_name"], "后面")
        self.assertTrue(snap["can_enter"])
        self.assertEqual(snap["blockers"], [])

    def test_idle_scene_is_reported_as_expired(self):
        """`ccb_stage>0` 但 6 小时没人碰：不能显示成「进行中」。"""
        now = 1_000_000.0
        snap = _snap(now=now, user_state={
            "ccb_stage": 3.0, "ccb_last_at": now - ccb.CCB_SCENE_IDLE_SECONDS - 3600.0,
        })
        self.assertTrue(snap["expired"])
        self.assertFalse(snap["in_scene"], "过期的场景不算在场景里")
        self.assertEqual(snap["stage_name"], "")
        self.assertEqual(snap["stage"], 3, "原始阶段号照给，方便诊断")

    def test_aftercare_window(self):
        now = 1_000_000.0
        snap = _snap(now=now, user_state={"ccb_aftercare_until": now + 600.0})
        self.assertAlmostEqual(snap["aftercare_left"], 600.0, places=1)
        self.assertEqual(snap["satisfy_phase"], 5)
        self.assertEqual(snap["satisfy_phase_name"], "余韵")

    def test_cooldown_left_is_reported_out_of_scene(self):
        now = 1_000_000.0
        snap = _snap(now=now, user_state={"ccb_gate_at": now - 3600.0})
        self.assertGreater(snap["cooldown_left"], 0.0)
        self.assertLessEqual(snap["cooldown_left"], ccb.CCB_COOLDOWN_SECONDS)

    def test_days_are_projected_not_written(self):
        """跨天之后快照说今天的数，但**不改**传进来的 state（只读入口靠它）。"""
        now = 1_000_000.0
        state = {
            "ccb_day": float(ccb._day_index(now)),
            "ccb_turns": 8.0,
            "ccb_satisfy": 100.0,
        }
        before = copy.deepcopy(state)
        snap = _snap(now=now + 86400.0, user_state=state)
        self.assertEqual(snap["turns_today"], 0, "跨天之后应当按今天的账显示")
        self.assertLess(snap["satisfy"], 100.0, "满足度应当按天回落")
        self.assertEqual(state, before, "只读快照不许改状态")

    def test_formatters_are_human_readable(self):
        self.assertEqual(ccb.delta_text(13.0), "涨了 13")
        self.assertEqual(ccb.delta_text(-7.0), "跌了 7")
        self.assertEqual(ccb.delta_text(0.0), "没动")
        self.assertEqual(ccb.threshold_text(47.0, 34.0, 38.0, 4.0), "够")
        self.assertIn("差", ccb.threshold_text(30.0, 34.0, 38.0, 4.0))
        self.assertIn("还差", ccb.threshold_text(39.0, 37.0, 38.0, 4.0))
        self.assertEqual(ccb.duration_text(3700.0), "1 小时 1 分")
        self.assertEqual(ccb.duration_text(35.0), "不到 1 分钟")

    def test_nan_does_not_leak_into_the_snapshot(self):
        snap = _snap(user_state={"ccb_stage": float("nan"), "ccb_turns": float("nan")},
                     libido=float("nan"), base_libido=float("nan"))
        self.assertEqual(snap["stage"], 0)
        self.assertEqual(snap["turns_today"], 0)
        self.assertEqual(snap["libido"], 0.0)
        import json as _json
        dumped = _json.dumps(snap, ensure_ascii=False)
        self.assertNotIn("nan", dumped.lower())


class StatusPage(unittest.TestCase):
    """`/你的状态` 的「亲密」段。"""

    def test_section_appears_in_private_chat_when_ccb_on(self):
        core = _core(True)
        _ready(core)
        text = "\n".join(core.status_lines("u1"))
        self.assertIn("亲密", text)
        self.assertIn("亲近欲", text)
        self.assertIn("门槛", text)
        self.assertIn("能进入", text)
        self.assertIn("/8", text, "今日次数要带分母，用户才知道封顶在哪")

    def test_section_shows_even_without_a_mood_profile(self):
        """只要 ccb 开着、私聊、有 user_id 就露——不再等「有没有情绪档案」。"""
        core = _core(True)
        text = "\n".join(core.status_lines("ghost"))
        self.assertIn("亲密", text)
        self.assertIn("亲近欲", text)
        self.assertIn("能进入", text)

    def test_section_is_silent_in_group_chat(self):
        core = _core(True)
        _ready(core)
        text = "\n".join(core.status_lines("u1", is_group=True))
        self.assertNotIn("亲密", text, "群里一个字都不许露")

    def test_section_is_silent_when_ccb_off(self):
        core = _core(False)
        _ready(core)
        text = "\n".join(core.status_lines("u1"))
        self.assertNotIn("亲密", text)

    def test_page_surfaces_the_blocking_gate(self):
        core = _core(True)
        _mood(core, libido=35.0, base_libido=34.0)
        now = core.now_epoch()
        _state(core)["ccb_intimacy_at"] = now
        text = "\n".join(core.status_lines("u1"))
        self.assertIn("亲近欲不够", text, f"页面没说被哪道闸挡着：{text}")

    def test_page_tells_the_truth_about_an_idle_scene(self):
        core = _core(True)
        _ready(core)
        now = core.now_epoch()
        _state(core).update({
            "ccb_stage": 2.0,
            "ccb_last_at": now - ccb.CCB_SCENE_IDLE_SECONDS - 3600.0,
            "ccb_satisfy": 80.0,
        })
        text = "\n".join(core.status_lines("u1"))
        self.assertIn("已闲置过期", text)
        self.assertNotIn("进行中", text, "过期的场景不许再显示成进行中")

    def test_intimacy_status_does_not_create_an_entry(self):
        core = _core(True)
        core.intimacy_status("stranger")
        self.assertNotIn("stranger", core._scope.all_user_ids(),
                         "看一眼状态不该给陌生人建条目")

    def test_intimacy_status_when_ccb_off_says_so(self):
        core = _core(False)
        _mood(core)
        snap = core.intimacy_status("u1")
        self.assertFalse(snap["enabled"])
        self.assertFalse(snap["can_enter"])
        self.assertTrue(any("未开启" in b for b in snap["blockers"]), snap["blockers"])
        self.assertEqual(snap["libido"], 47.0, "亲近欲是情绪层的真实读数，关掉 ccb 也照给")


class IntimacyText(unittest.TestCase):
    """`/亲密状态` 与状态页同一份数据。"""

    def test_same_rows_as_the_status_page(self):
        core = _core(True)
        _ready(core)
        page = [line for line in core.status_lines("u1") if "亲近欲" in line]
        command = [line for line in core.intimacy_text("u1").splitlines() if "亲近欲" in line]
        self.assertEqual(page, command, "指令与状态页不能各说一套")

    def test_cooldown_and_aftercare_both_show_up(self):
        core = _core(True)
        _ready(core)
        now = core.now_epoch()
        _state(core).update({
            "ccb_gate_at": now - 600.0,
            "ccb_aftercare_until": now + 1200.0,
            "ccb_stage": 0.0,
        })
        text = core.intimacy_text("u1")
        self.assertIn("余韵", text)
        self.assertIn("冷却", text)

    def test_text_when_ccb_off(self):
        core = _core(False)
        _mood(core)
        self.assertIn("未开启", core.intimacy_text("u1"))


class MoodText(unittest.TestCase):
    """`/好感度`：亲近欲要带基线/涨幅，亲密档案要能看出在不在场景里。"""

    def test_profile_text_shows_the_delta(self):
        core = _core(True)
        _mood(core)
        # 只看「亲近欲：」这一行——`intimacy_summary` 那句里也会出现「亲近欲」。
        lines = [line for line in core.mood.profile_text("u1").splitlines()
                 if line.startswith("亲近欲：")]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("基线", lines[0])
        self.assertIn("涨了", lines[0])
        self.assertIn("门槛", lines[0], "ccb 开着时该给出离门槛还差多少")

    def test_intimacy_summary_shows_scene_state(self):
        core = _core(True)
        state = _ready(core)
        now = core.now_epoch()
        state.update({"ccb_stage": 2.0, "ccb_satisfy": 80.0, "ccb_turns": 2.0,
                      "ccb_last_at": now, "ccb_done_count": 3.0})
        text = core.mood.intimacy_summary("u1")
        self.assertTrue(text.startswith("亲密："), text)
        self.assertIn("在场景里", text)
        self.assertIn("平台", text)
        self.assertIn("今日 2/8", text)
        self.assertIn("门槛", text)

    def test_intimacy_summary_shows_aftercare_and_cooldown(self):
        core = _core(True)
        state = _ready(core)
        now = core.now_epoch()
        state.update({"ccb_done_count": 2.0, "ccb_last_at": now - 600.0,
                      "ccb_gate_at": now - 600.0, "ccb_aftercare_until": now + 1800.0})
        self.assertIn("余韵", core.mood.intimacy_summary("u1"))
        state["ccb_aftercare_until"] = 0.0
        self.assertIn("冷却", core.mood.intimacy_summary("u1"))

    def test_intimacy_summary_is_side_effect_free(self):
        core = _core(True)
        core.mood.intimacy_summary("ghost")
        core.mood.profile_text("ghost")
        self.assertNotIn("ghost", core._scope.all_user_ids(),
                         "只读入口建了用户条目")

    def test_daily_numbers_roll_in_the_summary_without_writing(self):
        """跨天之后 `/好感度` 也该说「今日 0/8」，但它不许写状态。"""
        core = _core(True)
        state = _ready(core)
        now = core.now_epoch()
        state.update({"ccb_turns": 8.0, "ccb_last_at": now - 6 * 3600.0})
        core.clock.advance(days=1)
        before = dict(state)
        text = core.mood.intimacy_summary("u1")
        self.assertIn("今日 0/8", text)
        self.assertEqual(dict(state), before, "只读摘要不许改状态")
        self.assertEqual(state["ccb_turns"], 8.0, "写回由注入/状态页那条路径负责")


class Facade(unittest.TestCase):
    """社交层（AS）也要看得到「她这会儿在不在状态」。"""

    def _facade(self, core):
        roles = type("R", (), {"get_all": lambda self: [core]})()
        return api.CoreFacade(roles, None)

    def test_intimacy_exposes_the_scene_state(self):
        core = _core(True)
        state = _ready(core)
        now = core.now_epoch()
        state.update({"ccb_stage": 2.0, "ccb_satisfy": 80.0, "ccb_turns": 2.0,
                      "ccb_done_count": 3.0, "ccb_last_at": now})
        got = self._facade(core).intimacy("bot1", "u1")
        for key in ("stage", "in_scene", "stage_name", "turns_today", "cooldown_left",
                    "aftercare_left", "libido", "base_libido", "can_enter", "blockers"):
            self.assertIn(key, got, f"门面少了 {key}")
        self.assertEqual(got["stage"], 2)
        self.assertTrue(got["in_scene"])
        self.assertEqual(got["stage_name"], "平台")
        self.assertEqual(got["turns_today"], 2)
        self.assertEqual(got["libido"], 47.0)
        self.assertEqual(got["base_libido"], 34.0)
        self.assertEqual(got["blockers"], [])

    def test_intimacy_no_record_still_carries_the_gate_info(self):
        core = _core(True)
        _mood(core)
        got = self._facade(core).intimacy("bot1", "u1")
        self.assertEqual(got["done_count"], 0.0)
        self.assertFalse(got["can_enter"])
        self.assertTrue(got["blockers"], "进不去就该说清是哪道闸")

    def test_intimacy_reading_does_not_create_an_entry(self):
        core = _core(True)
        self._facade(core).intimacy("bot1", "stranger")
        self.assertNotIn("stranger", core._scope.all_user_ids())

    def test_want_line_authorization_is_untouched(self):
        off = _core(True, ccb_proactive=False)
        state = _ready(off)
        state["ccb_done_count"] = 2.0
        self.assertEqual(self._facade(off).intimacy("bot1", "u1")["want_line"], "")
        on = _core(True, ccb_proactive=True)
        state_on = _ready(on)
        state_on.update({"ccb_done_count": 2.0, "ccb_satisfy": 70.0})
        self.assertTrue(self._facade(on).intimacy("bot1", "u1")["want_line"])

    def test_relationship_carries_the_same_fields(self):
        core = _core(True)
        state = _ready(core)
        now = core.now_epoch()
        state.update({"ccb_stage": 1.0, "ccb_satisfy": 40.0, "ccb_done_count": 2.0,
                      "ccb_last_at": now})
        got = self._facade(core)._relationship(core, "u1")
        for key in ("stage", "in_scene", "stage_name", "turns_today", "cooldown_left",
                    "aftercare_left", "libido", "base_libido", "can_enter", "blockers"):
            self.assertIn(key, got, f"brief.relationship 少了 {key}")
        self.assertTrue(got["in_scene"])
        self.assertEqual(got["intimacy_done_count"], 2.0)

    def test_ccb_off_reports_disabled(self):
        core = _core(False)
        _mood(core)
        got = self._facade(core).intimacy("bot1", "u1")
        self.assertFalse(got["can_enter"])
        self.assertTrue(any("未开启" in b for b in got["blockers"]), got["blockers"])


try:  # 真实环境里 AstrBot 会把这些模块准备好；缺框架时整组跳过。
    from tests.test_main_integration import (  # noqa: E402
        GOOD_SCHEDULE, FakeEvent, collect, main_module as _main_module,
    )
    _MAIN_SKIP = "" if _main_module is not None else "main.py 不可加载"
except Exception as _exc:  # pragma: no cover
    _MAIN_SKIP = f"main.py 不可加载：{_exc}"
    _main_module = None


@unittest.skipIf(_main_module is None, _MAIN_SKIP)
class Command(unittest.IsolatedAsyncioTestCase):
    """`/亲密状态`：私聊答、群聊不答、ccb 关不答。"""

    async def _star(self, ccb_on: bool = True):
        from tests.fakes import FakeContext, FakeProvider

        tmp = Path(tempfile.mkdtemp())
        ctx = FakeContext(chat_providers=[FakeProvider("p1", reply=GOOD_SCHEDULE)])
        raw = {"timezone_city": "北京", "schedule_provider_name": "p1", "ccb": ccb_on}
        _main_module.get_astrbot_data_path = lambda: str(tmp)
        star = _main_module.HumanoidCore(ctx, raw)
        await star.initialize()
        return star

    async def asyncSetUp(self):
        self.star = await self._star(True)

    async def asyncTearDown(self):
        await self.star.terminate()

    async def test_private_chat_answers_with_the_same_fields(self):
        out = await collect(self.star.cmd_intimacy_status(FakeEvent("/亲密状态")))
        self.assertEqual(len(out), 1)
        self.assertIn("亲近欲", out[0])
        self.assertIn("能进入", out[0])
        self.assertIn("门槛", out[0])

    async def test_group_chat_is_refused(self):
        out = await collect(self.star.cmd_intimacy_status(
            FakeEvent("/亲密状态", private=False)))
        self.assertIn("私聊", out[0])

    async def test_ccb_off_says_so(self):
        star = await self._star(False)
        try:
            out = await collect(star.cmd_intimacy_status(FakeEvent("/亲密状态")))
            self.assertIn("未开启", out[0])
        finally:
            await star.terminate()

    async def test_status_command_hides_the_section_in_group(self):
        private = (await collect(self.star.cmd_status(FakeEvent("/你的状态"))))[0]
        group = (await collect(self.star.cmd_status(
            FakeEvent("/你的状态", private=False))))[0]
        self.assertIn("亲密", private)
        self.assertNotIn("亲密", group)

    async def test_help_lists_the_command(self):
        out = await collect(self.star.cmd_help(FakeEvent("/拟人 帮助")))
        self.assertIn("/亲密状态", out[0])


if __name__ == "__main__":
    unittest.main()
