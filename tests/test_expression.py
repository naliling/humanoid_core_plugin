"""表达层：体感显著度门控、夜间只给事实、措辞去模板感。

这一层的价值全在「什么不该注入」上，所以断言大多是负向的。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.config import HumanoidConfig
from humanoid.prompt_builder import PromptBuilder
from humanoid.wording import FEELING_WORDS, pick, scale_word
from humanoid.services.soma import SomaService
from humanoid.role_scope import RoleScope

from .fakes import FrozenClock, ScopeStore

TZ = ZoneInfo("Asia/Shanghai")


def cfg(**overrides) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", **overrides})


class _StubCore:
    """只给 `_night_lines` 用得着的三样：配置、她的钟、她的身体。"""

    def __init__(self, config, night: bool, asleep: bool) -> None:
        from types import SimpleNamespace

        self.config = config
        self.role_id = "bot1"
        self.clock = SimpleNamespace(is_night=lambda: night, today_str=lambda: "2026-08-22")
        self.soma = SimpleNamespace(snapshot=lambda: {"asleep": 1.0 if asleep else 0.0})


def night_lines(**conf) -> list[str]:
    night = conf.pop("night", True)
    asleep = conf.pop("asleep", False)
    builder = PromptBuilder.__new__(PromptBuilder)
    builder._core = _StubCore(cfg(**conf), night, asleep)
    return builder._night_lines()


class NightFactsTest(unittest.TestCase):
    """夜间与午睡：只报身体与日程的事实。

    v2.16.3 那版写的是「你还在睡，是手机震醒的那种：眼睛睁不开，脑子没转，话到嘴边
    只剩一两个字」——手机、眼睛、脑子都不是算出来的，是插件替她写的人设；「只剩一两个字」
    是插件在管她说多长。这一组断言卡的就是这两样不许回来。
    """

    def test_sleep_window_is_a_fact(self):
        text = " ".join(night_lines(night=True, asleep=False, soma_enabled=True))
        self.assertIn("睡眠时段", text)
        self.assertNotIn("她在睡", text, "到点还醒着的人，不许替她说「她在睡」")

    def test_asleep_is_said_once(self):
        """她在睡这件事由体感那块说，夜间块不再重复一遍。"""
        self.assertEqual(night_lines(night=True, asleep=True, soma_enabled=True), [])

    def test_daytime_writes_nothing(self):
        self.assertEqual(night_lines(night=False), [])

    def test_toggle_hides_the_fact_only(self):
        """关掉只是不提这条事实，不是让她少说话。"""
        self.assertEqual(night_lines(show_sleep_window=False), [])

    def test_no_invented_details_or_length_rules(self):
        banned = (
            "手机", "眼睛", "脑子", "字", "吵醒", "迷糊", "不要", "必须", "回一句",
            "明天再聊", "简短", "控制在", "只说一两句", "语气",
        )
        for asleep in (True, False):
            for force in (True, False):
                text = " ".join(night_lines(asleep=asleep, show_sleep_window=force))
                for word in banned:
                    self.assertNotIn(word, text, f"夜间事实里不该有「{word}」：{text}")

    def test_body_off_still_says_the_window(self):
        text = " ".join(night_lines(soma_enabled=False))
        self.assertIn("夜间作息", text)


class WordingTest(unittest.TestCase):
    """措辞层：同一状态多套说法，按天抽签稳定。"""

    def test_feelings_have_variants(self):
        for kind, ladder in FEELING_WORDS.items():
            for floor, options in ladder:
                self.assertGreaterEqual(len(options), 1, f"{kind} 的 {floor} 档没词了")

    def test_draw_is_stable_within_a_day_and_swaps_next_day(self):
        seed_a = ["bot1", "2026-08-22", "42"]
        seed_b = ["bot1", "2026-08-23", "42"]
        options = scale_word(90, FEELING_WORDS["sleepy"])
        self.assertIsInstance(options, tuple)
        self.assertEqual(pick("sleepy", seed_a, options), pick("sleepy", seed_a, options))
        # 只有一档一种说法时换不了，所以挑一个确实有多套说法的档位卡这件事
        self.assertGreater(len(set(options)), 1)

    def test_different_days_may_read_differently(self):
        options = scale_word(90, FEELING_WORDS["sleepy"])
        seen = {pick("sleepy", ["bot1", f"2026-08-{day:02d}", "42"], options) for day in range(1, 20)}
        self.assertGreater(len(seen), 1, "换了一天措辞还一个字不变，那就是预制菜")

    def test_degree_comes_from_the_number(self):
        self.assertNotEqual(scale_word(20, FEELING_WORDS["hunger"]), scale_word(95, FEELING_WORDS["hunger"]))


class SomaFixture:
    """一个只带身体服务的角色作用域，用来做时间推进仿真。"""

    def __init__(self, moment: datetime, config: HumanoidConfig, slots=None, weather=None) -> None:
        self.store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        self.store.load(moment.strftime("%Y-%m-%d"), config.cycle_length)
        self.scope: RoleScope = self.store.scope
        self.clock = FrozenClock(moment)
        self.time_value = moment.timestamp()
        self.slots = slots if slots is not None else []
        self.soma = SomaService(
            self.scope,
            lambda: config,
            self.clock,
            schedule_provider=lambda: self.slots,
            weather_provider=lambda: weather or {},
            time_source=lambda: self.time_value,
        )

    def to(self, moment: datetime) -> dict:
        self.clock.moment = moment
        self.time_value = moment.timestamp()
        return self.soma.advance()


DAY_SCHEDULE = [
    {"start": "00:00", "end": "07:30", "event": "睡眠", "energy_rate": 0.18},
    {"start": "07:30", "end": "08:30", "event": "早餐", "energy_rate": 0.05},
    {"start": "08:30", "end": "12:30", "event": "工作", "energy_rate": -0.12},
    {"start": "12:30", "end": "13:30", "event": "午餐与午休", "energy_rate": 0.1},
    {"start": "13:30", "end": "18:30", "event": "工作", "energy_rate": -0.12},
    {"start": "18:30", "end": "23:00", "event": "休闲", "energy_rate": 0.0},
    {"start": "23:00", "end": "24:00", "event": "睡眠", "energy_rate": 0.18},
]


class SomaTest(unittest.TestCase):
    def test_body_moves_without_messages(self):
        """没人聊天的那 15 小时里，困意与饥饿必须自己涨起来。"""
        start = datetime(2026, 8, 22, 8, 0, tzinfo=TZ)
        fixture = SomaFixture(start, cfg(), DAY_SCHEDULE)
        begin = fixture.soma.snapshot()
        end = fixture.to(start + timedelta(hours=15))
        self.assertGreater(end["sleep_pressure"], begin["sleep_pressure"] + 60)
        self.assertGreater(end["hunger"], begin["hunger"])

    def test_sleep_relieves_pressure_and_pays_debt(self):
        start = datetime(2026, 8, 22, 20, 0, tzinfo=TZ)
        fixture = SomaFixture(start, cfg(), DAY_SCHEDULE)
        fixture.to(start + timedelta(hours=5))  # 熬到凌晨 1 点，其中 23 点后在睡
        before = fixture.soma.snapshot()
        after = fixture.to(datetime(2026, 8, 23, 7, 30, tzinfo=TZ))
        self.assertLess(after["sleep_pressure"], before["sleep_pressure"])
        self.assertEqual(after["asleep"], 0.0, "醒来后不该还挂着「正在睡」")

    def test_staying_up_builds_debt(self):
        """整夜不睡：睡眠债按小时累积，并压低第二天的精力起点。"""
        start = datetime(2026, 8, 22, 22, 0, tzinfo=TZ)
        no_sleep = [
            {"start": "00:00", "end": "24:00", "event": "通宵做东西", "energy_rate": -0.1},
        ]
        fixture = SomaFixture(start, cfg(), no_sleep)
        fixture.to(datetime(2026, 8, 23, 6, 0, tzinfo=TZ))
        debt = fixture.soma.snapshot()["sleep_debt"]
        self.assertGreater(debt, 6.0, "23 点到 6 点醒着就该记成欠睡")
        self.assertLess(fixture.soma.sleep_debt_penalty(), 0.75)

    def test_sitting_and_weather_feed_discomfort(self):
        start = datetime(2026, 8, 22, 9, 0, tzinfo=TZ)
        cold = SomaFixture(start, cfg(), DAY_SCHEDULE, weather={"env": "当前城市 [北京] 天气：冷，气温 3℃"})
        cold.to(start + timedelta(hours=6))
        warm = SomaFixture(datetime(2026, 8, 22, 9, 0, tzinfo=TZ), cfg(), DAY_SCHEDULE,
                           weather={"env": "当前城市 [北京] 天气：舒适，气温 22℃"})
        warm.to(datetime(2026, 8, 22, 15, 0, tzinfo=TZ))
        self.assertGreater(cold.soma.snapshot()["discomfort"], warm.soma.snapshot()["discomfort"])

    def test_chat_drains_desire_and_alone_time_refills_it(self):
        start = datetime(2026, 8, 22, 10, 0, tzinfo=TZ)
        fixture = SomaFixture(start, cfg(desire_refill_hours=4.0), DAY_SCHEDULE)
        fixture.to(start + timedelta(hours=3))
        filled = fixture.soma.snapshot()["social_desire"]
        fixture.soma.note_chat((start + timedelta(hours=3)).timestamp())
        drained = fixture.soma.snapshot()["social_desire"]
        self.assertLess(drained, filled)
        fixture.to(start + timedelta(hours=9))
        self.assertGreater(fixture.soma.snapshot()["social_desire"], drained)

    def test_offline_catchup_is_capped(self):
        """停机两周后重启，不该攒出离谱的睡眠压力。"""
        start = datetime(2026, 8, 22, 9, 0, tzinfo=TZ)
        fixture = SomaFixture(start, cfg(), DAY_SCHEDULE)
        snap = fixture.to(start + timedelta(days=14))
        self.assertLessEqual(snap["sleep_pressure"], 100.0)
        self.assertGreater(snap["sleep_pressure"], 0.0)

    def test_clock_skew_does_not_run_the_body_backwards(self):
        start = datetime(2026, 8, 22, 9, 0, tzinfo=TZ)
        fixture = SomaFixture(start, cfg(), DAY_SCHEDULE)
        before = fixture.soma.snapshot()
        after = fixture.to(start - timedelta(hours=5))
        self.assertEqual(before["sleep_pressure"], after["sleep_pressure"])

    def test_schedule_change_mid_sleep_does_not_dump_debt(self):
        """凌晨重新生成的日程把睡眠区间从 23:00–07:00 推到了 01:00–09:00。

        正在睡的身体会瞬间变成「醒着」。以前 `_record_wake` 会拿「只睡了两小时」去
        比 sleep_need_hours，当场记一笔六小时的债；现在债只由「夜里醒着多久」积分，
        并且新日程装上时会先清掉 asleep_since，不会把日程改动当成真的醒来。
        """
        start = datetime(2026, 8, 23, 0, 0, tzinfo=TZ)
        late = [
            {"start": "00:00", "end": "01:00", "event": "还没睡", "energy_rate": 0.0},
            {"start": "01:00", "end": "09:00", "event": "睡眠", "energy_rate": 0.18},
            {"start": "09:00", "end": "24:00", "event": "活动", "energy_rate": 0.0},
        ]
        fixture = SomaFixture(start, cfg(), late)
        fixture.to(datetime(2026, 8, 23, 2, 0, tzinfo=TZ))
        debt = fixture.soma.snapshot()["sleep_debt"]
        self.assertLess(debt, 2.5, f"凌晨只醒了两小时，不该记成 {debt} 小时债")

        # 睡眠区间中途改了：不能把「还在睡」的身体算成刚醒。
        fixture.soma.note_schedule_changed()
        self.assertEqual(fixture.soma.snapshot()["asleep"], 0.0)
        before = fixture.soma.snapshot()["sleep_debt"]
        fixture.to(datetime(2026, 8, 23, 8, 0, tzinfo=TZ))
        self.assertLessEqual(fixture.soma.snapshot()["sleep_debt"], before,
                            "睡着的这段时间不该再涨债")

    def test_debt_is_repaid_while_asleep(self):
        """欠了债之后早点睡，债要能降下来。"""
        start = datetime(2026, 8, 22, 22, 0, tzinfo=TZ)
        fixture = SomaFixture(
            start, cfg(),
            [{"start": "00:00", "end": "24:00", "event": "通宵", "energy_rate": 0.0}],
        )
        fixture.to(datetime(2026, 8, 23, 2, 0, tzinfo=TZ))
        fixture.soma.data["sleep_debt"] = 6.0
        owed = fixture.soma.snapshot()["sleep_debt"]
        normal = [
            {"start": "00:00", "end": "08:00", "event": "睡眠", "energy_rate": 0.18},
            {"start": "08:00", "end": "22:00", "event": "活动", "energy_rate": 0.0},
            {"start": "22:00", "end": "24:00", "event": "睡眠", "energy_rate": 0.18},
        ]
        fixture.slots = normal
        fixture.soma.note_schedule_changed()
        fixture.to(datetime(2026, 8, 23, 6, 0, tzinfo=TZ))
        paid = fixture.soma.snapshot()["sleep_debt"]
        self.assertGreater(owed, 4.0, owed)
        self.assertLess(paid, owed, "好好睡一觉该把债还掉一截")

    def test_feelings_are_gated_by_salience(self):
        """刚睡醒时不该有任何体感被注入：真人不会每条消息都报备身体。"""
        start = datetime(2026, 8, 22, 9, 0, tzinfo=TZ)
        fresh = SomaFixture(start, cfg(enable_cycle=False), DAY_SCHEDULE)
        fresh.soma._set("sleep_pressure", 8.0)
        fresh.soma._set("sleep_debt", 0.0)
        fresh.soma._set("hunger", 10.0)
        fresh.soma._set("discomfort", 5.0)
        fresh.soma._set("arousal", 60.0)
        self.assertEqual([item for item in fresh.soma.feelings(85.0) if item[0] >= 0.55], [])

    def test_form_policy_tightens_when_sleepy(self):
        start = datetime(2026, 8, 22, 9, 0, tzinfo=TZ)
        fixture = SomaFixture(start, cfg(enable_cycle=False), DAY_SCHEDULE)
        fixture.soma._set("sleep_pressure", 20.0)
        fixture.soma._set("sleep_debt", 0.0)
        fixture.soma._set("discomfort", 0.0)
        alert = fixture.soma.form_policy(90.0, 90.0)
        fixture.soma._set("sleep_pressure", 92.0)
        sleepy = fixture.soma.form_policy(30.0, 20.0)
        self.assertGreater(alert["max_chars"], sleepy["max_chars"])
        self.assertLess(sleepy["question_bias"], alert["question_bias"])

    def test_burst_ok_means_she_has_the_stamina(self):
        """`burst_ok` 是「有余力才能连着发」，不是「难受到可以连着发」。

        旧写法是从 max_chars 推的（`<= 45`），于是它只在她最难受的时候为真，语义正好
        倒过来。社交层拿它决定「主动开话题时能不能接二条」——判反了就是越难受越被连着戳。
        """
        start = datetime(2026, 8, 22, 9, 0, tzinfo=TZ)
        fixture = SomaFixture(start, cfg(enable_cycle=False), DAY_SCHEDULE)
        fixture.soma._set("sleep_pressure", 15.0)
        fixture.soma._set("sleep_debt", 0.0)
        fixture.soma._set("discomfort", 0.0)
        self.assertTrue(
            fixture.soma.form_policy(90.0, 90.0)["burst_ok"],
            "精神好的时候应该允许连着发",
        )
        fixture.soma._set("sleep_pressure", 92.0)
        self.assertFalse(
            fixture.soma.form_policy(30.0, 20.0)["burst_ok"],
            "累到 20 字上限时不该被连着发",
        )

    def test_tick_does_not_write_state_every_minute(self):
        """60 秒一次的 tick 不该把状态文件写成常驻磁盘写入。"""
        start = datetime(2026, 8, 22, 9, 0, tzinfo=TZ)
        fixture = SomaFixture(start, cfg(), DAY_SCHEDULE)
        fixture.to(start + timedelta(hours=2))
        fixture.store.flush_sync()

        writes = 0
        for minute in range(60):
            fixture.to(start + timedelta(hours=2, minutes=minute + 1))
            if fixture.store.dirty:
                fixture.store.flush_sync()
                writes += 1
        self.assertGreater(writes, 0, "轴一直在变，不能永远不落盘")
        self.assertLessEqual(writes, 20, "一小时 60 次 tick 不该近乎每次都写盘")


if __name__ == "__main__":
    unittest.main()
