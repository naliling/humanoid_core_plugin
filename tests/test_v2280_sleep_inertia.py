"""v2.28.0：睡眠懒觉 / 被吵醒 / 睡眠惯性（C2）。

用户原话：「在角色这一睡觉时间段可以加一个睡眠懒觉，没大事就不会别吵起床然后会继续睡；
就算别人来吵或者给角色发消息，也不会显得明明在睡觉却生龙活虎」。

* 「消息不掀被子」是既有机制（睡着的那一段不重掷）：保留；
* 新增的是**痕迹**：被吵到（`note_disturbed`）、醒来后的惯性（`wake_inertia_until`）、
  以及注入里那几句「正睡着 / 被吵到 / 刚醒还没缓过来」；
* 刚醒 30 分钟内可以提一句昨晚的梦，但只说一条。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from humanoid.config import HumanoidConfig
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.services import soma as soma_module
from humanoid.services.soma import (
    SomaService,
    WAKE_DISTURBED_WINDOW_SECONDS,
    WAKE_INERTIA_MAX_MINUTES,
)
from humanoid.state import StateStore

from .fakes import FakeContext, FrozenClock, RecordingLogger, ScopeStore

TZ = ZoneInfo("Asia/Shanghai")
TODAY = "2026-09-26"


def cfg(**kw) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", **kw})


class SomaFixture:
    """一个能手动拨表并推进身体的 soma（与 test_expression 的仿真口径一致）。"""

    def __init__(self, moment: datetime, slots) -> None:
        conf = cfg()
        self.store = ScopeStore(Path(tempfile.mkdtemp()) / "s.json")
        self.store.load(moment.strftime("%Y-%m-%d"), conf.cycle_length)
        self.clock = FrozenClock(moment)
        self.time_value = moment.timestamp()
        self.slots = slots
        self.soma = SomaService(
            self.store.scope,
            lambda: conf,
            self.clock,
            schedule_provider=lambda: self.slots,
            time_source=lambda: self.time_value,
        )

    def to(self, moment: datetime) -> dict:
        self.clock.moment = moment
        self.time_value = moment.timestamp()
        return self.soma.advance()

    def at(self, hour: int, minute: int = 0, day: int = 26) -> datetime:
        return datetime(2026, 9, day, hour, minute, tzinfo=TZ)


class NoteDisturbedTest(unittest.TestCase):
    def test_notes_only_while_asleep_and_does_not_wake_her(self):
        fixture = SomaFixture(
            datetime(2026, 9, 26, 22, 50, tzinfo=TZ),
            [{"start": "22:00", "end": "07:00", "event": "睡眠", "energy_rate": 0.15}],
        )
        fixture.to(fixture.at(23, 30))
        self.assertTrue(fixture.soma.wake_state()["asleep"])
        disturbed_at = fixture.clock.timestamp()
        self.assertTrue(fixture.soma.note_disturbed(disturbed_at))
        self.assertEqual(fixture.soma.data.get("sleep_disturbed_at"), disturbed_at)
        self.assertEqual(fixture.soma.snapshot()["asleep"], 1.0, "消息不该掀被子")

    def test_awake_returns_false_and_writes_nothing(self):
        fixture = SomaFixture(datetime(2026, 9, 26, 10, 0, tzinfo=TZ), [])
        fixture.soma.advance()
        self.assertFalse(fixture.soma.note_disturbed())
        self.assertNotIn("sleep_disturbed_at", fixture.store.data)


class WakeInertiaTest(unittest.TestCase):
    def _run_like(self, start, slots, end):
        fixture = SomaFixture(start, slots)
        fixture.to(end)
        return fixture

    def test_natural_wake_has_short_inertia(self):
        fixture = self._run_like(
            datetime(2026, 9, 25, 22, 50, tzinfo=TZ),
            [
                # 生产里跨午夜的一觉由两段拼成（23:00-24:00 + 结转 00:00-07:00），
                # soma 的 slot_at 只认当天 00:00-24:00 的段。
                {"start": "23:00", "end": "24:00", "event": "睡眠", "energy_rate": 0.15},
                {"start": "00:00", "end": "07:00", "event": "睡眠", "energy_rate": 0.15},
            ],
            datetime(2026, 9, 26, 7, 0, tzinfo=TZ),
        )
        state = fixture.soma.wake_state()
        self.assertFalse(state["asleep"])
        self.assertAlmostEqual(
            state["inertia_until"] - state["wake_at"], 10 * 60, delta=1.0
        )
        self.assertEqual(state["inertia_minutes_left"], 10)
        self.assertFalse(state["disturbed"])
        self.assertAlmostEqual(state["last_sleep_hours"], 8.0, delta=0.1)

    def test_short_sleep_has_longer_inertia(self):
        fixture = self._run_like(
            datetime(2026, 9, 26, 4, 50, tzinfo=TZ),
            [{"start": "05:00", "end": "07:00", "event": "睡眠", "energy_rate": 0.15}],
            datetime(2026, 9, 26, 7, 0, tzinfo=TZ),
        )
        state = fixture.soma.wake_state()
        self.assertAlmostEqual(
            state["inertia_until"] - state["wake_at"], 25 * 60, delta=1.0
        )
        self.assertFalse(state["disturbed"])

    def test_being_disturbed_extends_inertia(self):
        fixture = SomaFixture(
            datetime(2026, 9, 26, 4, 50, tzinfo=TZ),
            [{"start": "05:00", "end": "07:00", "event": "睡眠", "energy_rate": 0.15}],
        )
        fixture.to(fixture.at(6, 50))
        self.assertTrue(fixture.soma.wake_state()["asleep"])
        fixture.soma.note_disturbed(fixture.clock.timestamp())
        fixture.to(fixture.at(7, 0))
        state = fixture.soma.wake_state()
        self.assertTrue(state["disturbed"], "醒来时该记得是被消息吵醒的")
        # 短睡 25 + 被吵醒 15 = 40
        self.assertAlmostEqual(
            state["inertia_until"] - state["wake_at"], 40 * 60, delta=1.0
        )
        self.assertIsNone(fixture.soma.data.get("sleep_disturbed_at"), "本次已结算")

    def test_inertia_is_capped(self):
        fixture = SomaFixture(
            datetime(2026, 9, 26, 4, 50, tzinfo=TZ),
            [{"start": "05:00", "end": "07:00", "event": "睡眠", "energy_rate": 0.15}],
        )
        fixture.to(fixture.at(6, 0))
        with mock.patch.object(soma_module, "WAKE_INERTIA_SHORT_MINUTES", 100):
            fixture.to(fixture.at(7, 0))
        state = fixture.soma.wake_state()
        self.assertAlmostEqual(
            state["inertia_until"] - state["wake_at"],
            WAKE_INERTIA_MAX_MINUTES * 60,
            delta=1.0,
        )

    def test_fragments_do_not_write_inertia(self):
        """日程重排切出的 10 分钟「睡着」不是一觉：不写惯性、不改昨晚睡了多久。"""
        fixture = SomaFixture(
            datetime(2026, 9, 26, 4, 50, tzinfo=TZ),
            [{"start": "05:00", "end": "05:10", "event": "睡眠", "energy_rate": 0.15}],
        )
        fixture.to(fixture.at(5, 20))
        state = fixture.soma.wake_state()
        self.assertEqual(state["inertia_until"], 0.0)
        self.assertEqual(state["last_sleep_hours"], 0.0)

    def test_wake_state_survives_old_and_bad_state(self):
        fixture = SomaFixture(datetime(2026, 9, 26, 10, 0, tzinfo=TZ), [])
        # 旧状态：只有 last_wake_at，没有新字段。
        fixture.store.data.clear()
        fixture.store.data["soma"] = {"last_tick": fixture.clock.timestamp(),
                                      "last_wake_at": 12345.0}
        state = fixture.soma.wake_state()
        self.assertEqual(state["wake_at"], 12345.0, "旧状态该回退到 last_wake_at")
        self.assertEqual(state["inertia_until"], 0.0)
        self.assertFalse(state["disturbed"])
        # 坏值：不改状态、不抛。
        fixture.store.data["soma"].update(
            {"wake_at": "不是时间", "last_wake_at": None,
             "wake_inertia_until": "abc",
             "wake_disturbed": "false", "last_sleep_hours": None}
        )
        state = fixture.soma.wake_state()
        self.assertEqual(state["wake_at"], 0.0)
        self.assertEqual(state["inertia_until"], 0.0)
        self.assertFalse(state["disturbed"], "字符串 \"false\" 不能被当成真")

    def test_snapshot_shape_is_unchanged(self):
        """新状态只走 `wake_state()`：`snapshot()` 的键不许漂移（契约/测试都靠它）。"""
        fixture = SomaFixture(datetime(2026, 9, 26, 10, 0, tzinfo=TZ), [])
        fixture.soma.advance()
        snap = fixture.soma.snapshot()
        self.assertEqual(
            set(snap),
            {"sleep_pressure", "sleep_debt", "hunger", "discomfort", "chill",
             "arousal", "social_desire", "asleep", "last_sleep_hours"},
        )
        self.assertNotIn("wake_inertia_until", snap)
        self.assertNotIn("wake_disturbed", snap)


def make_core(moment: datetime, **conf) -> HumanoidCoreInstance:
    store = StateStore(Path(tempfile.mkdtemp()) / "s.json", lambda: 0.01)
    store.load(moment.strftime("%Y-%m-%d"), 28)
    config = cfg(**conf)
    core = HumanoidCoreInstance(
        role_id="bot1", state_store=store, config_provider=lambda: config,
        logger=RecordingLogger(), stop_event=asyncio.Event(),
        resolver=FakeContext(), gateway=None,
    )
    clock = FrozenClock(moment)
    core.clock = clock
    for name in ("schedule", "soma", "energy", "social", "process", "mood"):
        service = getattr(core, name, None)
        if service is not None and hasattr(service, "_clock"):
            service._clock = clock
    return core


def sleeping_core(moment: datetime | None = None, **conf) -> HumanoidCoreInstance:
    moment = moment or datetime(2026, 9, 26, 3, 5, tzinfo=TZ)
    core = make_core(moment, **conf)
    core.schedule._scope.update_self(
        today_date=moment.strftime("%Y-%m-%d"),
        daily_schedule=[{
            "start": "00:00", "end": "24:00", "event": "睡眠", "location": "卧室",
            "emotion": "平静", "energy_rate": 0.1,
        }],
    )
    core.soma.data["asleep_since"] = core.soma.now - 3600.0
    return core


def awake_core(moment: datetime, **conf) -> HumanoidCoreInstance:
    core = make_core(moment, **conf)
    core.schedule._scope.update_self(
        today_date=moment.strftime("%Y-%m-%d"),
        daily_schedule=[
            {"start": "00:00", "end": "07:00", "event": "睡眠",
             "dreams": ["一片海"], "energy_rate": 0.15},
            {"start": "07:00", "end": "24:00", "event": "工作", "energy_rate": -0.05},
        ],
    )
    return core


class PromptSleepStateTest(unittest.TestCase):
    def test_asleep_line(self):
        text = sleeping_core().build_injection("42", is_group=False)
        self.assertIn("她这会儿正睡着", text)

    def test_disturbed_line_only_within_the_window(self):
        core = sleeping_core()
        core.soma.data["sleep_disturbed_at"] = core.soma.now - 60.0
        text = core.build_injection("42", is_group=False)
        self.assertIn("刚被这条消息吵到", text)
        self.assertIn("还没醒透", text)

        core = sleeping_core()
        core.soma.data["sleep_disturbed_at"] = core.soma.now - (WAKE_DISTURBED_WINDOW_SECONDS + 60)
        text = core.build_injection("42", is_group=False)
        self.assertNotIn("刚被这条消息吵到", text, "十分钟前的吵醒不该还在说")

    def test_inertia_line_after_waking(self):
        core = awake_core(datetime(2026, 9, 26, 9, 0, tzinfo=TZ))
        now = core.now_epoch()
        core.soma.data.update({
            "last_wake_at": now - 120.0, "wake_at": now - 120.0,
            "wake_inertia_until": now + 300.0, "wake_disturbed": True,
            "last_sleep_hours": 7.0,
        })
        text = core.build_injection("42", is_group=False)
        self.assertIn("她刚醒（2 分钟前）", text)
        self.assertIn("人还没完全缓过来", text)
        self.assertIn("被消息吵醒的", text)

    def test_inertia_line_replaces_the_generic_wake_echo(self):
        """「她刚醒，还没缓过来」与「她刚睡醒」说的是同一件事：只留前者。"""
        core = awake_core(datetime(2026, 9, 26, 9, 0, tzinfo=TZ))
        now = core.now_epoch()
        core.soma.data.update({
            "last_wake_at": now - 120.0, "wake_at": now - 120.0,
            "wake_inertia_until": now + 300.0,
            "last_sleep_hours": 7.0,
        })
        text = core.build_injection("42", is_group=False)
        self.assertIn("人还没完全缓过来", text)
        self.assertNotIn("她刚睡醒", text, "同一件事说了两遍")

    def test_no_inertia_line_after_the_window(self):
        core = awake_core(datetime(2026, 9, 26, 9, 0, tzinfo=TZ))
        now = core.now_epoch()
        core.soma.data.update({
            "last_wake_at": now - 3600.0, "wake_at": now - 3600.0,
            "wake_inertia_until": now - 60.0, "wake_disturbed": True,
            "last_sleep_hours": 7.0,
        })
        text = core.build_injection("42", is_group=False)
        self.assertNotIn("人还没完全缓过来", text)

    def test_show_sleep_window_off_hides_everything(self):
        text = sleeping_core(show_sleep_window=False).build_injection("42", is_group=False)
        self.assertNotIn("正睡着", text)
        self.assertNotIn("在睡", text, "关掉睡眠提示后，换句话仍说她睡着")
        self.assertNotIn("醒透", text)

    def test_dream_is_mentioned_once_in_the_first_30_minutes(self):
        core = awake_core(datetime(2026, 9, 26, 7, 10, tzinfo=TZ))
        now = core.now_epoch()
        core.soma.data.update({
            "last_wake_at": now - 600.0, "wake_at": now - 600.0,
            "last_sleep_hours": 7.0,
        })
        text = core.build_injection("42", is_group=False)
        self.assertIn("她刚从梦里醒过来", text)
        self.assertIn("一片海", text)
        self.assertEqual(text.count("一片海"), 1, "梦说了一遍以上")
        self.assertIn("（梦到一片海）", text)

    def test_no_dream_after_30_minutes(self):
        core = awake_core(datetime(2026, 9, 26, 8, 0, tzinfo=TZ))
        now = core.now_epoch()
        core.soma.data.update({
            "last_wake_at": now - 3000.0, "wake_at": now - 3000.0,
            "last_sleep_hours": 7.0,
        })
        text = core.build_injection("42", is_group=False)
        self.assertNotIn("一片海", text, "过了刚醒窗口还在报梦")


class MessageDisturbanceTest(unittest.IsolatedAsyncioTestCase):
    async def test_message_while_asleep_notes_disturbance(self):
        core = sleeping_core()
        try:
            core.on_message("42", "在吗")
            self.assertTrue(core.soma.data.get("sleep_disturbed_at"))
            self.assertIsNotNone(core.soma.data.get("asleep_since"), "消息不该撕被子")
        finally:
            await core.stop()

    async def test_command_does_not_note_disturbance(self):
        core = sleeping_core()
        try:
            core.on_message("42", "/查看日程", is_command=True)
            self.assertFalse(core.soma.data.get("sleep_disturbed_at"))
            self.assertIsNotNone(core.soma.data.get("asleep_since"))
        finally:
            await core.stop()

    async def test_awake_message_does_not_note_disturbance(self):
        core = awake_core(datetime(2026, 9, 26, 10, 0, tzinfo=TZ))
        try:
            core.on_message("42", "在吗")
            self.assertFalse(core.soma.data.get("sleep_disturbed_at"))
        finally:
            await core.stop()


if __name__ == "__main__":
    unittest.main()
