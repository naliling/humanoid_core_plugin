"""v3.0.0 服务层数值/安全修复回归（task-2）。

覆盖审核报告 01-core 中由 services-models 负责的项：
P1-2 天气密钥脱敏、P1-3 精力午夜重置、P2-09 静默期 0 语义、P2-13 时钟回拨账本、
P2-14 enable_cycle 开关、P2-15 情绪句去重表淘汰、P2-16 农历节日表、P2-17 LLM 日预算
时区、P2-18 总时间上限、P2-19 "or 默认值"吞 0、P2-20 soma 零长段、P2-21 只读档案、
P2-22 ccb 纯文本越权（ccb 侧）、P2-23 ccb 数值越界。

这些用例全部用**可控时钟/固定种子数据**，不读网络、不依赖宿主机时区。
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid import ccb
from humanoid.config import HumanoidConfig
from humanoid.data import holidays as holidays_mod
from humanoid.llm import CallGate, LLMGateway, ProviderResolver
from humanoid.prompt_builder import PromptBuilder
from humanoid.services.energy import EnergyService
from humanoid.services.mood import Delta, MoodService
from humanoid.services.soma import SomaService
from humanoid.services.weather import WeatherService, redact_secrets

from .fakes import FakeClock, FakeContext, FakeProvider, RecordingLogger, ScopeStore

TZ = ZoneInfo("Asia/Shanghai")


def cfg(**overrides) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", **overrides})


class FrozenClock:
    """可手动设定「现在」的 Clock 替身（面向 datetime 的服务用）。"""

    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def now(self) -> datetime:
        return self.moment

    def timestamp(self) -> float:
        return self.moment.timestamp()

    def today_str(self) -> str:
        return self.moment.strftime("%Y-%m-%d")

    def weekday(self) -> str:
        return "一二三四五六日"[self.moment.weekday()]

    def holiday(self) -> str:
        return ""

    def is_night(self, moment=None) -> bool:
        return False

    def is_deep_sleep(self, moment=None) -> bool:
        return False

    def advance(self, **kwargs) -> None:
        self.moment = self.moment + timedelta(**kwargs)


def build_store(conf: HumanoidConfig | None = None, today: str = "2026-03-01") -> ScopeStore:
    conf = conf or cfg()
    store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
    store.load(today, conf.cycle_length)
    return store


def build_mood(conf=None, now: float = 1_780_000_000.0, clock=None, logger=None):
    """MoodService + 可变时间源。返回 (service, store, state, clock)。"""
    conf = conf or cfg()
    store = build_store(conf)
    clock = clock or FrozenClock(datetime.fromtimestamp(now, TZ))
    state = {"now": now}

    def _time() -> float:
        return state["now"]

    service = MoodService(
        store.scope, lambda: conf, clock, None, _time, None, logger or RecordingLogger()
    )
    return service, store, state, clock


# ──────────────────────────────────────────────────────────────────────
# P1-2 天气 API Key 脱敏
# ──────────────────────────────────────────────────────────────────────


class WeatherRedactionTest(unittest.IsolatedAsyncioTestCase):
    SECRET = "SECRET123"

    def test_redact_secrets_covers_common_credential_params(self):
        raw = (
            "url=URL('https://api.openweathermap.org/data/2.5/weather"
            "?q=Beijing%2CCN&appid=SECRET123&units=metric') "
            "api_key=AKIA123 token=TOK456&keyword=2"
        )
        out = redact_secrets(raw)
        for leaked in ("SECRET123", "AKIA123", "TOK456"):
            self.assertNotIn(leaked, out, f"脱敏漏了 {leaked}：{out}")
        self.assertIn("appid=***", out)
        # 业务参数不能被误伤（keyword 里含 key 但不是凭证）
        self.assertIn("keyword=2", out)

    async def test_failed_refresh_log_does_not_leak_key(self):
        conf = cfg(
            weather_enabled=True,
            weather_location="Beijing,CN",
            weather_api_key="k" * 24,
        )
        store = build_store(conf)
        log = RecordingLogger()
        clock = FrozenClock(datetime(2026, 3, 1, 12, 0, tzinfo=TZ))

        async def boom(url: str, timeout: float):
            # 与 aiohttp ClientResponseError.__str__ 的实际形状一致：带完整 real_url
            raise RuntimeError(
                "500, message='Internal Server Error', url=URL('"
                + url
                + "')"
            )

        service = WeatherService(store.scope, lambda: conf, clock, boom, log)
        self.assertFalse(await service.refresh_async(force=True))
        text = log.text()
        self.assertIn("天气刷新失败", text)
        self.assertNotIn("k" * 24, text, "warning 日志里出现了完整 API Key")
        self.assertNotIn(self.SECRET, text)
        self.assertIn("appid=***", text, "失败日志应保留可读的脱敏参数名")


# ──────────────────────────────────────────────────────────────────────
# P1-3 精力跨午夜不再无条件重置
# ──────────────────────────────────────────────────────────────────────

NIGHT_OWL = [
    {"start": "00:00", "end": "04:00", "event": "做事", "energy_rate": -0.12},
    {"start": "04:00", "end": "12:00", "event": "睡眠", "energy_rate": 0.10},
    {"start": "12:00", "end": "24:00", "event": "做事", "energy_rate": -0.12},
]
WORKDAY = [
    {"start": "00:00", "end": "08:00", "event": "睡眠", "energy_rate": 0.15},
    {"start": "08:00", "end": "12:00", "event": "工作", "energy_rate": -0.1},
    {"start": "12:00", "end": "13:00", "event": "午休", "energy_rate": 0.1},
    {"start": "13:00", "end": "18:00", "event": "工作", "energy_rate": -0.1},
    {"start": "18:00", "end": "24:00", "event": "休闲", "energy_rate": 0.0},
]


class EnergyMidnightTest(unittest.TestCase):
    def build(self, moment: datetime, slots=None, conf=None):
        conf = conf or cfg()
        store = build_store(conf)
        clock = FrozenClock(moment)
        service = EnergyService(store.scope, lambda: conf, clock, lambda: slots or WORKDAY)
        return service, store, clock

    def test_night_owl_midnight_has_no_magic_refill(self):
        """夜猫子 23:59 -> 00:01 不得出现 +50 级跳变（审核 P1-3 的核心证据）。"""
        clock = FrozenClock(datetime(2026, 3, 1, 23, 59, tzinfo=TZ))
        service, store, _ = self.build(clock.moment, NIGHT_OWL)
        service.reset(30.0)
        store.data["last_update"] = "2026-03-01 23:59:00"
        before = service.energy
        clock.moment = datetime(2026, 3, 2, 0, 1, tzinfo=TZ)
        after = service.advance()
        delta = after - before
        self.assertLess(abs(delta), 5.0, f"跨午夜跳变 {delta:+.2f}，午夜回血又回来了")
        self.assertLess(after, 40.0, f"熬夜状态精力不该回到 {after:.1f}")

    def test_cross_midnight_settles_continuously_with_sleep_recovery(self):
        """跨天走连续结算：睡眠段照常恢复，不需要「新一天起点」这个魔法数。"""
        clock = FrozenClock(datetime(2026, 3, 2, 8, 0, tzinfo=TZ))
        service, store, _ = self.build(clock.moment, WORKDAY)
        store.data["energy"] = 30.0
        store.data["last_update"] = "2026-03-01 23:00:00"
        energy = service.advance()
        # 23:00-24:00 休闲自然恢复 + 00:00-08:00 睡眠（rate 0.15）+ 自然恢复
        self.assertGreater(energy, 60.0, "睡了一夜反而没恢复：睡眠积分没算进去")
        self.assertLessEqual(energy, float(service.config.max_energy))
        self.assertEqual(store.get("last_update"), "2026-03-02 08:00:00")

    def test_multi_day_span_is_split_at_local_midnights(self):
        """跨多天：每个自然日用自己的日程锚点，不能把次日凌晨段错配到前一天。"""
        clock = FrozenClock(datetime(2026, 3, 4, 10, 0, tzinfo=TZ))
        service, store, _ = self.build(clock.moment, WORKDAY)
        store.data["energy"] = 40.0
        store.data["last_update"] = "2026-03-01 22:00:00"
        energy = service.advance()
        self.assertGreaterEqual(energy, 0.0)
        self.assertLessEqual(energy, float(service.config.max_energy))
        self.assertEqual(store.get("last_update"), "2026-03-04 10:00:00")


# ──────────────────────────────────────────────────────────────────────
# P2-14 enable_cycle=false 必须真的关掉周期调制
# ──────────────────────────────────────────────────────────────────────


class EnableCycleSwitchTest(unittest.TestCase):
    def build_energy(self, conf, moment, slots, energy):
        store = build_store(conf)
        clock = FrozenClock(moment)
        service = EnergyService(store.scope, lambda: conf, clock, lambda: slots)
        store.data["energy"] = energy
        store.data["last_update"] = moment.strftime("%Y-%m-%d %H:%M:%S")
        return service, store

    def test_consumption_phase_factor_disappears_when_cycle_off(self):
        slots = [{"start": "00:00", "end": "24:00", "event": "工作", "energy_rate": -0.1}]
        moment = datetime(2026, 3, 2, 13, 0, tzinfo=TZ)
        on_conf = cfg(enable_cycle=True)
        off_conf = cfg(enable_cycle=False)
        on_svc, _ = self.build_energy(on_conf, moment, slots, 80.0)
        off_svc, _ = self.build_energy(off_conf, moment, slots, 80.0)
        # 周期第 1 天 = 月经期，消耗系数 1.3
        on_svc._scope.set_self("current_cycle_day", 1)
        off_svc._scope.set_self("current_cycle_day", 1)
        on_svc._scope.set_self("last_update", "2026-03-02 12:00:00")
        off_svc._scope.set_self("last_update", "2026-03-02 12:00:00")
        on = on_svc.advance()
        off = off_svc.advance()
        self.assertAlmostEqual(off, 80.0 - 0.1 * 0.7 * 0.5 * 60, places=2)
        self.assertGreater(off, on, "关闭周期后仍按月经期 ×1.3 消耗")
        self.assertAlmostEqual(on, 80.0 - 0.1 * 0.7 * 1.3 * 0.5 * 60, places=2)

    def test_recovery_phase_multiplier_disappears_when_cycle_off(self):
        slots = [{"start": "00:00", "end": "24:00", "event": "休息", "energy_rate": 0.1}]
        moment = datetime(2026, 3, 2, 13, 0, tzinfo=TZ)
        on_conf = cfg(enable_cycle=True)
        off_conf = cfg(enable_cycle=False)
        on_svc, on_store = self.build_energy(on_conf, moment, slots, 50.0)
        off_svc, off_store = self.build_energy(off_conf, moment, slots, 50.0)
        for svc in (on_svc, off_svc):
            svc._scope.set_self("current_cycle_day", 1)
            svc._scope.set_self("last_update", "2026-03-02 12:00:00")
        on = on_svc.advance()
        off = off_svc.advance()
        # 自然恢复倍率：周期第 1 天 0.5；关闭时按 1.0
        self.assertAlmostEqual(off, 50.0 + 0.1 * 0.5 * 60 + 0.15 * 1.0 * 0.5 * 60, places=2)
        self.assertAlmostEqual(on, 50.0 + 0.1 * 0.5 * 60 + 0.15 * 0.5 * 0.5 * 60, places=2)
        self.assertGreater(off, on, "关闭周期后恢复仍按 ×0.5 倍率")


class MoodCycleSwitchTest(unittest.TestCase):
    def _modifiers(self, conf, delta: Delta, cycle_day: int) -> Delta:
        service, store, _state, _clock = build_mood(conf)
        store.scope.set_self("energy", 50)
        store.scope.set_self("current_cycle_day", cycle_day)
        return service._apply_modifiers(delta, "u1")

    def test_ovulation_amplification_only_when_cycle_on(self):
        on = self._modifiers(cfg(enable_cycle=True, mood_sensitivity=100), Delta(1.0, 0.5, 0.0), 14)
        off = self._modifiers(cfg(enable_cycle=False, mood_sensitivity=100), Delta(1.0, 0.5, 0.0), 14)
        self.assertAlmostEqual(on.affection, 1.0 * 1.4, places=3)
        self.assertAlmostEqual(off.affection, 1.0, places=3)
        self.assertGreater(on.affection, off.affection)

    def test_menstrual_negative_amplification_only_when_cycle_on(self):
        on = self._modifiers(cfg(enable_cycle=True, mood_sensitivity=100), Delta(0.0, -1.0, 0.0), 1)
        off = self._modifiers(cfg(enable_cycle=False, mood_sensitivity=100), Delta(0.0, -1.0, 0.0), 1)
        self.assertLess(on.libido, off.libido, "关闭周期后亲近欲仍按月经期 ×1.5 放大")
        self.assertAlmostEqual(off.libido, -1.0, places=3)


# ──────────────────────────────────────────────────────────────────────
# P2-13 系统时钟回拨：账本不得按满权重计入「未来」条目
# ──────────────────────────────────────────────────────────────────────


class MoodClockRollbackTest(unittest.TestCase):
    NOW = 1_780_000_000.0

    def build(self):
        service, store, state, clock = build_mood(cfg(), now=self.NOW)
        return service, store, state, clock

    def test_rollback_does_not_jump_affection_up(self):
        service, store, state, clock = self.build()
        record = service.profile("u1")
        record["base_affection"] = 50.0
        record["last_decay"] = self.NOW
        # 20 天暖聊：10 笔 +2，两两相隔 2 天
        for i in range(10):
            service._ledger_append(
                record, Delta(2.0, 0.0, 0.0), self.NOW - (20 - 2 * i) * 86400.0
            )
        record["affection"] = 50.0 + service._ledger_total(record, self.NOW)
        before = float(record["affection"])
        # 系统时钟回拨 30 天
        state["now"] = self.NOW - 30 * 86400.0
        clock.moment = datetime.fromtimestamp(state["now"], TZ)
        self.assertTrue(service.decay_user("u1"), "回拨后衰减不应冻结")
        after = float(service.profile("u1")["affection"])
        self.assertLess(abs(after - before), 1.0, f"回拨让好感凭空跳了 {after - before:+.2f}")
        # 账本时间轴已被平移：最新一笔落在「回拨后的现在」附近
        stamps = [float(item["t"]) for item in record["ledger"]]
        self.assertLessEqual(max(stamps), state["now"] + 300.0)

    def test_decay_resumes_after_rollback(self):
        service, store, state, clock = self.build()
        record = service.profile("u1")
        record["base_affection"] = 50.0
        record["last_decay"] = self.NOW
        for i in range(10):
            service._ledger_append(
                record, Delta(2.0, 0.0, 0.0), self.NOW - (20 - 2 * i) * 86400.0
            )
        record["affection"] = 50.0 + service._ledger_total(record, self.NOW)
        state["now"] = self.NOW - 30 * 86400.0
        clock.moment = datetime.fromtimestamp(state["now"], TZ)
        service.decay_user("u1")
        first = float(service.profile("u1")["affection"])
        # 回拨之后的 12 小时：正常时衰（不是永久冻结）
        state["now"] += 12 * 3600.0
        clock.moment = datetime.fromtimestamp(state["now"], TZ)
        service.decay_user("u1")
        second = float(service.profile("u1")["affection"])
        self.assertLess(second, first, "回拨之后时衰仍然冻结")
        self.assertGreater(second, float(service.profile("u1")["base_affection"]))

    def test_future_ledger_entry_never_counted_at_full_weight(self):
        service, _store, _state, _clock = self.build()
        record = {"base_affection": 50.0, "ledger": [
            {"t": self.NOW + 30 * 86400.0, "w": 20.0, "k": "很上心"},
            {"t": self.NOW - 3600.0, "w": 2.0, "k": "温和"},
        ]}
        total = service._ledger_total(record, self.NOW)
        self.assertLessEqual(total, 2.0, "未来条目被按满权重计入，好感会凭空跳涨")

    def test_future_last_decay_is_reanchored_not_frozen(self):
        service, store, state, clock = self.build()
        record = service.profile("u2")
        record["libido"] = 40.0
        record["base_libido"] = 30.0
        record["last_decay"] = self.NOW + 10 * 86400.0  # 落在未来
        self.assertTrue(service.decay_user("u2"), "last_decay 在未来时衰减被冻结")
        self.assertAlmostEqual(float(record["last_decay"]), self.NOW, places=3)
        state["now"] = self.NOW + 7 * 3600.0
        clock.moment = datetime.fromtimestamp(state["now"], TZ)
        self.assertTrue(service.decay_user("u2"))
        self.assertLess(float(record["libido"]), 40.0, "锚点修好后衰减没有继续")


# ──────────────────────────────────────────────────────────────────────
# P2-15 情绪句去重表：淘汰而不是整表清空
# ──────────────────────────────────────────────────────────────────────


class _EpochCore:
    def __init__(self) -> None:
        self.now = 1000.0

    def now_epoch(self) -> float:
        return self.now


class EmotionSeenLruTest(unittest.TestCase):
    def make_builder(self) -> PromptBuilder:
        builder = PromptBuilder.__new__(PromptBuilder)
        builder._core = _EpochCore()
        builder._emotion_seen = OrderedDict()
        return builder

    def test_same_sentence_is_still_deduped(self):
        builder = self.make_builder()
        self.assertFalse(builder._emotion_repeated("u1", "上回那句话到现在都还窝火"))
        self.assertTrue(builder._emotion_repeated("u1", "上回那句话到现在都还窝火"))
        builder._core.now += builder.EMOTION_REPEAT_SECONDS + 1
        self.assertFalse(builder._emotion_repeated("u1", "上回那句话到现在都还窝火"))

    def test_full_table_evicts_oldest_instead_of_clearing(self):
        builder = self.make_builder()
        old_limit = PromptBuilder.EMOTION_SEEN_LIMIT
        try:
            PromptBuilder.EMOTION_SEEN_LIMIT = 8
            for i in range(8):
                builder._emotion_repeated(f"u{i}", f"内心话{i}")
            self.assertEqual(len(builder._emotion_seen), 8)
            builder._emotion_repeated("new", "新的内心话")
            self.assertEqual(len(builder._emotion_seen), 8, "表满后应只淘汰一条")
            self.assertNotIn("u0", builder._emotion_seen, "最旧的应先被淘汰")
            for i in range(1, 8):
                self.assertIn(f"u{i}", builder._emotion_seen, "整表清空让所有人的去重失效")
            # 仍活着的 u1 窗口内去重照常
            builder._core.now += 1.0
            self.assertTrue(builder._emotion_repeated("u1", "内心话1"))
        finally:
            PromptBuilder.EMOTION_SEEN_LIMIT = old_limit

    def test_expired_entries_are_evicted_before_live_ones(self):
        builder = self.make_builder()
        old_limit = PromptBuilder.EMOTION_SEEN_LIMIT
        try:
            PromptBuilder.EMOTION_SEEN_LIMIT = 4
            builder._core.now = 10_000.0
            for i in range(2):
                builder._emotion_seen[f"dead{i}"] = (f"旧话{i}", 10_000.0)
            for i in range(2):
                builder._emotion_seen[f"live{i}"] = (f"活话{i}", 10_000.0)
            # 把两条标成过期
            builder._emotion_seen["dead0"] = ("旧话0", 0.0)
            builder._emotion_seen["dead1"] = ("旧话1", 0.0)
            builder._emotion_repeated("new", "新话")
            self.assertNotIn("dead0", builder._emotion_seen)
            self.assertNotIn("dead1", builder._emotion_seen)
            for i in range(2):
                self.assertIn(f"live{i}", builder._emotion_seen, "应先淘汰已过期条目，再动活条目")
        finally:
            PromptBuilder.EMOTION_SEEN_LIMIT = old_limit


# ──────────────────────────────────────────────────────────────────────
# P2-16 农历节日表（2026/2028/2029/2030 腊八、2027 端午、2029 重阳）
# ──────────────────────────────────────────────────────────────────────


class LunarHolidayDataTest(unittest.TestCase):
    def day(self, text: str) -> datetime:
        year, month, day = (int(part) for part in text.split("-"))
        return datetime(year, month, day)

    def test_audited_dates_are_correct(self):
        cases = {
            "2025-01-07": "腊八节",
            "2026-01-26": "腊八节",
            "2027-01-15": "腊八节",
            "2028-01-04": "腊八节",
            "2029-01-22": "腊八节",
            "2030-01-11": "腊八节",
        }
        for raw, name in cases.items():
            self.assertEqual(holidays_mod.lunar_holiday(self.day(raw)), name, raw)
        # 错月版本不得再出现
        self.assertEqual(holidays_mod.lunar_holiday(self.day("2026-12-26")), "")
        self.assertEqual(holidays_mod.lunar_holiday(self.day("2029-12-25")), "")
        # 2029-12-25 必须还是圣诞节（农历表不能盖掉它）
        self.assertEqual(holidays_mod.resolve_holiday(self.day("2029-12-25")), "圣诞节")

    def test_other_verified_entries(self):
        self.assertEqual(holidays_mod.lunar_holiday(self.day("2027-06-09")), "端午节")
        self.assertEqual(holidays_mod.lunar_holiday(self.day("2027-06-08")), "")
        self.assertEqual(holidays_mod.lunar_holiday(self.day("2029-10-16")), "重阳节")
        self.assertEqual(holidays_mod.lunar_holiday(self.day("2029-10-15")), "")
        self.assertEqual(holidays_mod.resolve_holiday(self.day("2026-01-26")), "腊八节")
        self.assertEqual(holidays_mod.resolve_holiday(self.day("2026-12-26")), "")

    def test_self_check_against_spring_festival(self):
        """除夕=春节前 1 天，元宵=春节后 14 天，腊八=春节前 21~23 天。"""
        for year, table in holidays_mod.LUNAR_FESTIVAL_DATES.items():
            self.assertEqual(set(table), set(holidays_mod.LUNAR_FESTIVAL_NAMES), year)
            spring = self.day(f"{year}-{table['春节']}")
            self.assertEqual(
                self.day(f"{year}-{table['除夕']}") + timedelta(days=1), spring, year
            )
            self.assertEqual(
                self.day(f"{year}-{table['元宵节']}") - timedelta(days=14), spring, year
            )
            laba = self.day(f"{year}-{table['腊八节']}")
            self.assertIn(
                (spring - laba).days, (21, 22, 23), f"{year} 年腊八与春节的天数不合常理"
            )
            # 同一年表里不能有两个节日撞同一天
            dates = list(table.values())
            self.assertEqual(len(dates), len(set(dates)), f"{year} 年节日日期重复")

    def test_out_of_range_years_return_empty(self):
        self.assertEqual(holidays_mod.lunar_holiday(self.day("2031-02-20")), "")


# ──────────────────────────────────────────────────────────────────────
# P2-17 LLM 日预算按角色城市时区切日
# ──────────────────────────────────────────────────────────────────────

DAY_KEY_CITY = "America/New_York"


def _find_host_midnight_span() -> float | None:
    """找一个墙钟时刻 t：宿主机本地日期在 t→t+3600 之间跨天，而纽约没有。

    宿主机时区在不同机器上不同，写死一个 epoch 会在别的机器上测不出对照。
    """
    tz = ZoneInfo(DAY_KEY_CITY)
    base = 1_772_000_000.0
    for step in range(0, 96):
        t = base + step * 1800.0
        host_before = time.strftime("%Y-%m-%d", time.localtime(t))
        host_after = time.strftime("%Y-%m-%d", time.localtime(t + 3600.0))
        ny_before = datetime.fromtimestamp(t, tz).strftime("%Y-%m-%d")
        ny_after = datetime.fromtimestamp(t + 3600.0, tz).strftime("%Y-%m-%d")
        if host_before != host_after and ny_before == ny_after:
            return t
    return None


class LlmDayBoundaryTest(unittest.TestCase):
    def test_daily_budget_follows_character_city(self):
        stamp = _find_host_midnight_span()
        if stamp is None:  # pragma: no cover - 宿主机与纽约同步跨日的极端环境
            self.skipTest("宿主机与纽约在同一小时跨日，构造不出对照")
        conf = cfg(timezone_city=DAY_KEY_CITY, llm_daily_call_budget=5)
        wall = FakeClock(stamp)
        gate = CallGate(lambda: conf, None, wall_clock=wall, monotonic=FakeClock(0.0))
        gate.consume("日程生成")
        self.assertEqual(gate.used_today, 1)
        wall.value = stamp + 3600.0  # 宿主机已跨日，她的城市还没
        self.assertEqual(gate.used_today, 1, "日预算跟着宿主机时区切了，与她的一天不一致")
        wall.value = stamp + 86400.0  # 她的城市也跨日了
        self.assertEqual(gate.used_today, 0)

    def test_missing_timezone_falls_back_to_host(self):
        class BareCfg:
            llm_daily_call_budget = 5
            schedule_min_interval_minutes = 0
            llm_idle_silence_minutes = 0

        wall = FakeClock(1_772_000_000.0)
        gate = CallGate(lambda: BareCfg(), None, wall_clock=wall, monotonic=FakeClock(0.0))
        gate.consume("日程生成")
        self.assertEqual(gate.used_today, 1)


# ──────────────────────────────────────────────────────────────────────
# P2-18 MAX_TOTAL_CALL_SECONDS 是硬上限，不是下限
# ──────────────────────────────────────────────────────────────────────


class _SpyGateway(LLMGateway):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seen_timeouts: list[float] = []

    async def _invoke(self, provider, prompt, call_kwargs, timeout):
        self.seen_timeouts.append(float(timeout))
        return await super()._invoke(provider, prompt, call_kwargs, timeout)


class _TimeEater(FakeProvider):
    """一次调用就把假单调钟推进 200 秒。"""

    def __init__(self, provider_id: str, state: dict) -> None:
        super().__init__(provider_id, error=RuntimeError("rate limit"))
        self._state = state

    async def text_chat(self, prompt=None, **kwargs):
        self.calls += 1
        self._state["t"] += 200.0
        raise RuntimeError("rate limit")


class LlmTotalTimeCapTest(unittest.IsolatedAsyncioTestCase):
    def build(self, mono, provider):
        conf = cfg()
        log = RecordingLogger()
        resolver = ProviderResolver(FakeContext(chat_providers=[provider]), log)
        gateway = _SpyGateway(resolver, lambda: conf, log, mono)
        return gateway, conf

    async def test_panel_timeout_cannot_exceed_total_cap(self):
        state = {"t": 0.0}
        provider = _TimeEater("slow", state)
        gateway, _conf = self.build(lambda: state["t"], provider)
        result = await gateway.generate(
            prompt="hi",
            chain=(("首选模型", "slow"),),
            allow_global=False,
            timeout=300.0,
            attempts_per_provider=5,
        )
        self.assertFalse(result.ok)
        self.assertEqual(provider.calls, 1, "总闸到期后不该再试")
        self.assertTrue(gateway.seen_timeouts)
        self.assertLessEqual(
            max(gateway.seen_timeouts), 150.0,
            f"面板 timeout=300 时单次尝试仍拿到 {gateway.seen_timeouts}，总闸被放大成下限",
        )

    async def test_default_timeout_unchanged(self):
        provider = FakeProvider("p1", error=RuntimeError("rate limit"))
        gateway, _conf = self.build(FakeClock(0.0), provider)
        result = await gateway.generate(
            prompt="hi",
            chain=(("首选模型", "p1"),),
            allow_global=False,
            timeout=20.0,
            attempts_per_provider=1,
        )
        self.assertFalse(result.ok)
        self.assertEqual(gateway.seen_timeouts, [20.0], "默认 timeout 不该被总闸改动")


# ──────────────────────────────────────────────────────────────────────
# P2-19 "or 默认值" 吞掉合法 0
# ──────────────────────────────────────────────────────────────────────


class MoodZeroValueTest(unittest.TestCase):
    def test_current_tone_reads_zero_affection_as_low(self):
        service, store, _state, _clock = build_mood(cfg())
        store.user("u1")["mood"] = {
            "affection": 0.0,
            "base_affection": 50.0,
            "aggression": 10.0,
            "base_aggression": 10.0,
            "last_decay": 1_780_000_000.0,
            "turn_count": 3,
        }
        self.assertEqual(service.current_tone("u1"), "她此刻有点提不起劲")
        self.assertEqual(service.style_tendency("u1"), "这会儿话不太多")

    def test_recovery_bonus_works_when_affection_is_zero(self):
        service, store, _state, _clock = build_mood(cfg(mood_recovery_bonus=True))
        record = service.profile("u1")
        record["base_affection"] = 10.0
        record["affection"] = 0.0
        out = service._apply_recovery(Delta(0.0, 0.0, 0.0), record, "对不起")
        self.assertGreater(out.affection, 0.0, "好感 0 时道歉回温被当成「不低于基线」吞掉")

    def test_bottom_softening_uses_real_zero(self):
        service, _store, _state, _clock = build_mood(
            cfg(mood_negative_softening=True, mood_negative_streak_required=1)
        )
        low = {"affection": 0.0, "base_affection": 50.0}
        mid = {"affection": 50.0, "base_affection": 50.0}
        low_out = service._throttle_negative(Delta(-2.0, 0.0, 0.0), low, "", severe=True)
        mid_out = service._throttle_negative(Delta(-2.0, 0.0, 0.0), mid, "", severe=True)
        self.assertGreater(low_out.affection, mid_out.affection, "0 好感没有底部软化")
        self.assertAlmostEqual(low_out.affection, -2.0 * 0.3, places=3)

    def test_intimacy_request_does_not_bypass_threshold_at_zero_affection(self):
        """同款第 7 处：好感 0 被当成 50 → 越过 ccb_affection_min 白拿亲近欲。"""
        service, store, _state, _clock = build_mood(cfg(ccb=True, ccb_affection_min=50))
        record = service.profile("u1")
        record["affection"] = 0.0
        out, coerced = service._apply_intimacy_request(Delta(0.0, 0.0, 0.0), record, "我们做爱吧")
        self.assertFalse(coerced)
        self.assertEqual(out.libido, 0.0, "好感 0 却按「已到门槛」白给亲近欲")

    def test_cooling_triggers_at_zero_affection(self):
        """同款第 8 处：好感 0 被当成 50 → 冷静期永远不触发。"""
        service, store, _state, _clock = build_mood(cfg(cooling_enabled=True, cooling_affection=10))
        record = service.profile("u1")
        record["affection"] = 0.0
        service._maybe_enter_cooling("u1", record, 1_780_000_000.0, Delta(0.0, 0.0, 0.0))
        self.assertGreater(float(record.get("cool_until", 0.0) or 0.0), 1_780_000_000.0)


# ──────────────────────────────────────────────────────────────────────
# P2-20 soma slot_at 零长段
# ──────────────────────────────────────────────────────────────────────


class SomaZeroLengthSlotTest(unittest.TestCase):
    def build(self, slots):
        conf = cfg()
        store = build_store(conf)
        clock = FrozenClock(datetime(2026, 3, 1, 12, 0, tzinfo=TZ))
        service = SomaService(
            store.scope, lambda: conf, clock, lambda: slots, lambda: {}, lambda: clock.timestamp()
        )
        return service

    def test_zero_length_sleep_slot_is_not_forever(self):
        slots = [
            {"start": "00:00", "end": "00:00", "event": "睡眠", "energy_rate": 0.1},
            {"start": "08:00", "end": "12:00", "event": "工作", "energy_rate": -0.1},
        ]
        service = self.build(slots)
        moment = datetime(2026, 3, 1, 3, 0, tzinfo=TZ)
        self.assertEqual(service.slot_at(moment), {}, "零长段被当成持续到 24:00")
        self.assertFalse(service.is_sleep_time(moment), "凌晨三点没在睡却被判成睡着")
        self.assertEqual(
            service.slot_at(datetime(2026, 3, 1, 10, 0, tzinfo=TZ)).get("event"), "工作"
        )

    def test_reversed_slot_is_skipped_too(self):
        slots = [{"start": "12:00", "end": "08:00", "event": "睡眠", "energy_rate": 0.1}]
        service = self.build(slots)
        self.assertEqual(service.slot_at(datetime(2026, 3, 1, 10, 0, tzinfo=TZ)), {})


# ──────────────────────────────────────────────────────────────────────
# P2-21 只读档案快照
# ──────────────────────────────────────────────────────────────────────


class MoodPeekProfileTest(unittest.TestCase):
    def test_missing_profile_is_not_created(self):
        service, store, _state, _clock = build_mood(cfg())
        calls: list[int] = []
        store.scope.set_mark_dirty(lambda: calls.append(1))
        self.assertEqual(service.peek_profile("nobody"), {})
        self.assertNotIn("nobody", store.scope.all_user_ids(), "只读快照建出了空壳")
        self.assertEqual(calls, [], "只读快照 mark_dirty 了")

    def test_existing_profile_is_repaired_on_copy_only(self):
        service, store, _state, _clock = build_mood(cfg())
        store.user("u1")["mood"] = {
            "affection": "坏值",
            "libido": 20.0,
            "aggression": 10.0,
            "base_affection": 35.0,
            "last_decay": 1_780_000_000.0,
            "turn_count": 1,
        }
        calls: list[int] = []
        store.scope.set_mark_dirty(lambda: calls.append(1))
        snapshot = service.peek_profile("u1")
        self.assertIsInstance(snapshot, dict)
        self.assertEqual(snapshot["affection"], 35.0, "快照应带上修复后的数值")
        self.assertEqual(store.user("u1")["mood"]["affection"], "坏值", "只读快照改了真实档案")
        self.assertEqual(calls, [], "只读快照 mark_dirty 了")
        # 对照：写路径 profile() 才会回写 + mark_dirty
        service.profile("u1")
        self.assertTrue(calls, "写路径 profile() 应照常 mark_dirty")


# ──────────────────────────────────────────────────────────────────────
# P2-22 / P2-23 ccb 纯文本越权与数值越界
# ──────────────────────────────────────────────────────────────────────


class CcbOccurrenceGuardTest(unittest.TestCase):
    def test_detect_occurrence_itself_unchanged(self):
        # 判定层不改：真正的事情仍然认得出；闸加在「记账/结算」入口
        self.assertTrue(ccb.detect_occurrence("我们做爱了"))
        self.assertFalse(ccb.detect_occurrence("你做了什么"))

    def test_plain_text_outside_scene_is_not_recorded(self):
        state = {}
        self.assertFalse(ccb.note_occurrence_in_scene(state, 1_000.0))
        self.assertNotIn("ccb_done_count", state, "场景外一句纯文本加了次数")
        self.assertNotIn("ccb_last_at", state)
        # ccb_stage=0 同样不算（场景已经收场）
        self.assertFalse(
            ccb.note_occurrence_in_scene({"ccb_stage": 0.0, "ccb_last_at": 900.0}, 1_000.0)
        )
        # 闲置超过 6 小时的那一场也早过去了
        stamp = 100_000.0
        self.assertFalse(
            ccb.note_occurrence_in_scene(
                {"ccb_stage": 2.0, "ccb_last_at": stamp - ccb.CCB_SCENE_IDLE_SECONDS - 1},
                stamp,
            )
        )

    def test_in_scene_still_records(self):
        state = {"ccb_stage": 2.0, "ccb_last_at": 990.0}
        self.assertTrue(ccb.scene_active(state, 1_000.0))
        self.assertTrue(ccb.note_occurrence_in_scene(state, 1_000.0))
        self.assertEqual(state["ccb_done_count"], 1.0)

    def test_scene_active_rejects_bad_shapes(self):
        self.assertFalse(ccb.scene_active(None, 1_000.0))
        self.assertFalse(ccb.scene_active({}, 1_000.0))
        self.assertFalse(ccb.scene_active({"ccb_stage": "坏值"}, 1_000.0))


class CcbNumericRangeTest(unittest.TestCase):
    def test_libido_and_aggression_are_clamped_to_their_ranges(self):
        state = {"ccb_done_count": 3.0}
        profile = {
            "affection": 50.0,
            "base_affection": 40.0,
            "libido": 50.0,
            "aggression": 48.0,
        }
        out = ccb.occurrence_effects(state, profile, refused=False)
        self.assertLessEqual(out["libido"], 50.0, f"亲近欲越界：{out}")
        self.assertEqual(out["libido"], 50.0)
        self.assertLessEqual(out["aggression"], 50.0)

    def test_refused_aggression_is_clamped(self):
        out = ccb.occurrence_effects(
            {"ccb_done_count": 0.0},
            {"affection": 50.0, "base_affection": 40.0, "libido": 30.0, "aggression": 48.0},
            refused=True,
        )
        self.assertLessEqual(out["aggression"], 50.0, f"攻击性越界：{out}")

    def test_zero_axes_are_not_treated_as_missing(self):
        # 0 是合法值：旧写法「x or 默认值」会把 libido=0 当成 34
        out = ccb.occurrence_effects(
            {"ccb_done_count": 1.0},
            {"affection": 0.0, "base_affection": 0.0, "libido": 0.0, "aggression": 0.0},
            refused=False,
        )
        self.assertEqual(out["affection"], 2.0)
        self.assertEqual(out["libido"], 2.0)


# ──────────────────────────────────────────────────────────────────────
# P2-09 stale_user_retention_days=0 => 永不整条删除
# ──────────────────────────────────────────────────────────────────────


class StaleRetentionZeroTest(unittest.TestCase):
    NOW = 1_780_000_000.0

    def seed(self, store) -> dict:
        user = store.user("u1")
        user["nickname"] = "小明"
        user["first_met"] = self.NOW - 200 * 86400.0
        user["last_interaction"] = self.NOW - 200 * 86400.0
        user["mood"] = {
            "affection": 20.0,
            "last_interaction": self.NOW - 200 * 86400.0,
            "first_met": self.NOW - 200 * 86400.0,
            "last_decay": self.NOW - 200 * 86400.0,
        }
        return user

    def build(self, conf):
        service, store, _state, _clock = build_mood(conf, now=self.NOW)
        self.seed(store)
        return service, store

    def test_zero_keeps_silent_user_forever(self):
        service, store = self.build(
            cfg(mood_data_retention_days=7, stale_user_retention_days=0)
        )
        service.prune_expired(self.NOW)
        service.prune_expired(self.NOW)
        self.assertIn("u1", store.scope.all_user_ids(), "0=永不整条删除被退回 90 天")
        self.assertEqual(store.user("u1").get("nickname"), "小明")
        self.assertIn("first_met", store.user("u1"))

    def test_positive_days_still_prunes(self):
        service, store = self.build(
            cfg(mood_data_retention_days=7, stale_user_retention_days=30)
        )
        service.prune_expired(self.NOW)
        service.prune_expired(self.NOW)
        self.assertNotIn("u1", store.scope.all_user_ids(), ">0 时的整条删除语义被改坏")

    def test_empty_shell_is_still_cleaned_even_when_zero(self):
        service, store = self.build(
            cfg(mood_data_retention_days=7, stale_user_retention_days=0)
        )
        store.user("empty")
        service.prune_expired(self.NOW)
        self.assertNotIn("empty", store.scope.all_user_ids(), "纯空壳是 0 语义的明确例外")


if __name__ == "__main__":
    unittest.main()
