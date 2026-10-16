"""v2.27.33 节律审计回归：时钟、日程、过程、身体、情绪映射的边界。

这一版把 `services/{schedule,process,social,weather,soma,energy}.py`、
`humanoid/{clock,relstage,slots,outfit,wording,persona}.py`、`humanoid/data/*.py`
逐个过了一遍，问的是同一组问题：**会抛异常、会静默算错、会随运行时间无限增长、
会跨角色串数据**。这里钉住查实的几处，以及几条本来是对的、但不该被后人改坏的边界。

三处修在 v2.27.33：

1. `clock.resolve_zone` 用 `lru_cache` 把「这台机器缺 tzdata」也记了一辈子，
   于是 `_tzdata_usable()` 的 300 秒重试永远轮不到执行；
2. `process._ensure_process_fields` 对 `duration_minutes` 裸调 `int()`，
   脏数据会掀掉整个心跳（`_advance_phase` 的同类取值早就包了 try）；
3. `data/mood_map.py` 的 NaN 会静默落到**最高**档（好感 100 / 欲望 50）。
"""

from __future__ import annotations

import random
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from humanoid import clock as clock_module
from humanoid.config import HumanoidConfig
from humanoid.data.mood_map import affection_rank, generate_mood_tag, round_to_bucket
from humanoid.role_scope import RoleScope
from humanoid.services.process import ProcessService
from humanoid.services.schedule import (
    CARRY_KEY,
    SEGMENT_MAX_MINUTES,
    SEGMENT_MIN_MINUTES,
    SEGMENT_SLEEP_MAX_MINUTES,
    ScheduleService,
    day_phrases,
    dynamic_segment,
    parse_segment,
    segment_start,
)
from humanoid.services.soma import SomaService
from humanoid.slots import (
    DAY_MINUTES,
    clamp_rate,
    format_time,
    parse_time,
    sleep_window_minutes,
)

from .fakes import FrozenClock, RecordingLogger, ScopeStore

TZ = ZoneInfo("Asia/Shanghai")
MOMENT = datetime(2026, 8, 22, 9, 0, tzinfo=TZ)
TODAY = "2026-08-22"

# `snapshot()` 里这九个量都是 0~100 的百分比（asleep 是 0/1）。任何一条跑出这个
# 区间就说明积分发散或夹紧失效。
BOUNDED_KEYS = (
    "sleep_pressure",
    "sleep_debt",
    "hunger",
    "discomfort",
    "chill",
    "arousal",
    "social_desire",
    "asleep",
    "last_sleep_hours",
)


def cfg(**overrides) -> HumanoidConfig:
    # chance 压到 0：概率重估单独测，不让它污染「这段该不该换」的断言
    return HumanoidConfig.from_raw(
        {"timezone_city": "北京", "schedule_change_chance": 0, **overrides}
    )


# ---------------------------------------------------------------------------
# 时钟：城市 → 时区的缓存
# ---------------------------------------------------------------------------


class ZoneCacheTest(unittest.TestCase):
    """缓存可以记住结论，但不能记住「暂时取不到」。"""

    def setUp(self):
        clock_module.resolve_zone.cache_clear()
        self._real_zoneinfo = clock_module.ZoneInfo
        self._real_usable = clock_module._tzdata_usable

    def tearDown(self):
        clock_module.ZoneInfo = self._real_zoneinfo
        clock_module._tzdata_usable = self._real_usable
        clock_module.resolve_zone.cache_clear()

    def _break_tzdata(self, usable: dict):
        def boom(name):
            raise clock_module.ZoneInfoNotFoundError(name)

        clock_module.ZoneInfo = boom
        clock_module._tzdata_usable = lambda: usable["yes"]

    def test_missing_tzdata_is_reprobed_not_memorized(self):
        """旧写法：`lru_cache` 连「缺 tzdata」这条说明一起记住，300 秒重试形同虚设。"""
        usable = {"yes": False}
        self._break_tzdata(usable)

        tz, note = clock_module.resolve_zone("东京")
        self.assertIsNone(tz)
        self.assertIn("tzdata", note)

        # tzdata 后装好了（容器里补装、依赖加载完）——同一个城市必须重新探测
        usable["yes"] = True
        tz2, note2 = clock_module.resolve_zone("东京")
        self.assertIsNone(tz2)
        self.assertNotIn("tzdata", note2, "缺库的结论被缓存住了，重试永远轮不到")
        self.assertIn("拼错", note2)

    def test_a_resolved_city_is_memorized(self):
        calls = {"n": 0}
        real = clock_module._resolve_zone_uncached

        def counting(city):
            calls["n"] += 1
            return real(city)

        clock_module._resolve_zone_uncached = counting
        try:
            first = clock_module.resolve_zone("北京")
            second = clock_module.resolve_zone("北京")
        finally:
            clock_module._resolve_zone_uncached = real

        self.assertEqual(first, second)
        self.assertEqual(calls["n"], 1, "认得出的城市不必每条消息重查一遍")

    def test_an_unknown_city_is_memorized(self):
        calls = {"n": 0}
        real = clock_module._resolve_zone_uncached

        def counting(city):
            calls["n"] += 1
            return real(city)

        clock_module._resolve_zone_uncached = counting
        try:
            tz, note = clock_module.resolve_zone("火星城")
            clock_module.resolve_zone("火星城")
        finally:
            clock_module._resolve_zone_uncached = real

        self.assertIsNone(tz)
        self.assertIn("认不出城市", note)
        self.assertEqual(calls["n"], 1)

    def test_cache_clear_alias_survives(self):
        """既有测试与调用方用的是 `resolve_zone.cache_clear()` 这个旧名字。"""
        clock_module.resolve_zone("北京")
        self.assertTrue(clock_module._ZONE_CACHE)
        clock_module.resolve_zone.cache_clear()
        self.assertFalse(clock_module._ZONE_CACHE)

    def test_the_cache_does_not_grow_without_bound(self):
        for index in range(clock_module._ZONE_CACHE_MAX + 20):
            clock_module.resolve_zone(f"不存在的城市{index}")
        self.assertLessEqual(len(clock_module._ZONE_CACHE), clock_module._ZONE_CACHE_MAX)


# ---------------------------------------------------------------------------
# 过程：脏数据不能掀掉心跳
# ---------------------------------------------------------------------------


class _FakeScheduleForProcess:
    """只够 `ProcessService` 用的日程替身。"""

    def __init__(self, event: str = "工作", start: str = "09:00", end: str = "18:00"):
        self._slot = {"event": event, "start": start, "end": end}

    def current_slot(self, minutes=None):
        return dict(self._slot)


class ProcessDirtyStateTest(unittest.TestCase):
    """`state.json` 是用户能手改的文件，读路径必须扛得住脏值。"""

    def _service(self, process=None):
        conf = cfg()
        scope = RoleScope({"_state_version": 3, "roles": {}}, "bot1", lambda: None)
        if process is not None:
            scope.set_self("current_process", process)
        clock = clock_module.Clock(lambda: conf)
        return ProcessService(scope, lambda: conf, clock, _FakeScheduleForProcess()), scope

    def test_junk_duration_does_not_raise(self):
        for junk in ("abc", [], {}, "12分", -5, 0, None, float("nan"), True):
            with self.subTest(junk=junk):
                service, _ = self._service(
                    {
                        "name": "整理旧照片",
                        "started_at": "2026-08-22T09:00:00",
                        "duration_minutes": junk,
                    }
                )
                proc = service.current()  # 旧写法在这里抛 ValueError / TypeError
                start = service._parse_dt(proc["started_at"])
                end = service._parse_dt(proc["expected_end"])
                self.assertIsNotNone(end)
                self.assertGreater(end, start, f"duration_minutes={junk!r} 修出来的时长非正")

    def test_junk_phase_index_does_not_raise(self):
        service, _ = self._service(
            {
                "name": "整理旧照片",
                "started_at": "2026-08-22T09:00:00",
                "duration_minutes": 60,
                "phases": ["整理旧照片", "翻相册", "收尾"],
                "phase_index": "第二段",
                "next_phase_at": "2026-08-22T09:30:00",
            }
        )
        proc = service.current()
        service._advance_phase(proc, service._clock.now())
        self.assertIsInstance(proc.get("phase_index"), int)

    def test_a_missing_current_process_is_created(self):
        service, _ = self._service(None)
        proc = service.current()
        self.assertTrue(proc.get("name"))
        self.assertTrue(proc.get("expected_end"))

    def test_process_history_stays_bounded(self):
        """过程历史不能随运行时间无限增长。"""
        service, scope = self._service(None)
        for index in range(60):
            service._push_history({"name": f"第{index}件事"})
        history = scope.get_self("process_history")
        self.assertLessEqual(len(history), 20)
        self.assertEqual(service.recent(1), ["第59件事"])


# ---------------------------------------------------------------------------
# 情绪映射：NaN 不能变成最高档
# ---------------------------------------------------------------------------


class MoodMapNanTest(unittest.TestCase):
    def test_nan_does_not_become_the_top_bucket(self):
        # 旧写法：NaN 与任何数比较都是 False，一路比不过所有桶 → 落到 50.0
        self.assertEqual(round_to_bucket(float("nan")), 0.0)
        # 旧写法：所有 `<` 都不成立 → 落到最后一个 return，即最亲密的 100
        self.assertEqual(affection_rank(float("nan")), 0)

    def test_nan_does_not_raise_when_building_the_tag(self):
        tag = generate_mood_tag(float("nan"), float("nan"), float("nan"), float("nan"))
        self.assertIsInstance(tag, str)

    def test_normal_values_keep_their_bands(self):
        self.assertEqual(round_to_bucket(0.0), 0.0)
        self.assertEqual(round_to_bucket(50.0), 50.0)
        self.assertEqual(round_to_bucket(99.0), 50.0)
        self.assertEqual(affection_rank(0.0), 0)
        self.assertEqual(affection_rank(100.0), 100)


# ---------------------------------------------------------------------------
# 身体：极端值、时钟回拨、长间隔、跨午夜
# ---------------------------------------------------------------------------


class SomaExtremesTest(unittest.TestCase):
    def _soma(self, moment=None, slots=None, weather=None):
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load()
        clock = FrozenClock(moment or MOMENT)
        soma = SomaService(
            store.scope,
            lambda: cfg(),
            clock,
            schedule_provider=(lambda: slots) if slots is not None else (lambda: []),
            weather_provider=(lambda: weather) if weather is not None else None,
        )
        return soma, clock, store

    def _assert_bounded(self, snapshot: dict):
        for key in BOUNDED_KEYS:
            if key not in snapshot:
                continue
            value = float(snapshot[key])
            self.assertTrue(0.0 <= value <= 100.0, f"{key}={value} 跑出了 0~100")

    def test_nan_now_moves_nothing(self):
        soma, _, _ = self._soma()
        soma.advance()
        before = dict(soma.snapshot())
        after = soma.advance(now=float("nan"))
        self.assertEqual(before, after)

    def test_clock_rollback_does_not_diverge(self):
        soma, clock, _ = self._soma()
        soma.advance()
        before = dict(soma.snapshot())
        clock.advance(days=-3)
        after = soma.advance()  # 只挪基准，不倒推
        self._assert_bounded(after)
        self.assertEqual(before, after, "时钟回拨不该凭空改身体")

    def test_a_month_long_gap_stays_bounded(self):
        soma, clock, _ = self._soma()
        soma.advance()
        clock.advance(days=30)
        snapshot = soma.advance()
        self._assert_bounded(snapshot)

    def test_a_year_long_gap_stays_bounded(self):
        soma, clock, _ = self._soma()
        soma.advance()
        clock.advance(days=365)
        self._assert_bounded(soma.advance())

    def test_nan_written_into_state_is_scrubbed_on_read(self):
        soma, _, store = self._soma()
        for key in BOUNDED_KEYS:
            store.set("soma", {**store.get("soma", {}), key: float("nan")})
        self._assert_bounded(soma.advance())

    def test_clamping_a_nan_lands_on_the_floor(self):
        """`min(100.0, nan)` 在 Python 下返回 100.0 —— NaN 会被静默钉成上限。"""
        soma, _, store = self._soma()
        soma._set("hunger", float("nan"))
        self.assertEqual(store.get("soma", {}).get("hunger"), 0.0)

    def test_a_night_written_as_two_slots_is_merged(self):
        """生产里跨午夜的觉是两段：今天 `23:00–24:00` + 结转的次日 `00:00–02:00`。

        `parse_segment` 把超出 24:00 的部分挪进 `carry_end`（schedule.py:650-651），
        `_append_segment` 再把它变成次日 00:00 起的一段，所以日表里从不出现 `hi < lo`。
        """
        slots = [
            {"event": "睡觉", "start": "23:00", "end": "24:00"},
            {"event": "睡觉", "start": "00:00", "end": "02:00"},
        ]
        window = sleep_window_minutes(slots)
        self.assertIsNotNone(window)
        self.assertEqual(window, (23 * 60, 2 * 60), "接缝该被合回一条 23:00→02:00 的夜")
        start, end = window
        self.assertLess(end, start, "跨午夜时 end < start 是这个函数的约定（slots.py:257）")

    def test_a_crossing_slot_is_still_split_by_sleep_window_minutes(self):
        """兜底：真有人塞进 `hi < lo` 的段，这个函数也会拆成两截再合回来。

        跨午夜时返回 `(start, end)` 且 `end < start`——这是它写在 slots.py:257 的口径。
        """
        window = sleep_window_minutes([{"event": "睡觉", "start": "23:00", "end": "07:00"}])
        self.assertIsNotNone(window)
        self.assertEqual(window, (23 * 60, 7 * 60))

    def test_a_non_sleep_event_is_not_mistaken_for_the_night(self):
        """「夜间休息」不含任何 `SLEEP_WORDS`，按睡眠事件接不进来。"""
        self.assertIsNone(sleep_window_minutes([{"event": "夜间休息", "start": "23:00", "end": "07:00"}]))

    def test_sleeping_is_recognised_on_both_sides_of_midnight(self):
        slots = [
            {"event": "睡觉", "start": "23:00", "end": "24:00"},
            {"event": "睡觉", "start": "00:00", "end": "02:00"},
        ]
        for moment, expected in (
            (datetime(2026, 8, 22, 23, 30, tzinfo=TZ), True),
            (datetime(2026, 8, 23, 0, 30, tzinfo=TZ), True),
            (datetime(2026, 8, 23, 1, 30, tzinfo=TZ), True),
            (datetime(2026, 8, 23, 9, 0, tzinfo=TZ), False),
        ):
            with self.subTest(moment=moment):
                soma, _, _ = self._soma(moment=moment, slots=slots)
                self.assertEqual(soma.is_sleep_time(moment), expected)


# ---------------------------------------------------------------------------
# 日程：解析边界、跨午夜、有界增长、未完成事项的过期
# ---------------------------------------------------------------------------


class SlotParsingTest(unittest.TestCase):
    def test_parse_time_bounds(self):
        self.assertEqual(parse_time("00:00"), 0)
        self.assertEqual(parse_time("24:00"), DAY_MINUTES)
        self.assertEqual(parse_time("23:59"), 1439)
        self.assertIsNone(parse_time("23:60"), "60 分不是合法的分钟")
        self.assertIsNone(parse_time("25:00"))
        self.assertIsNone(parse_time(-1))
        self.assertIsNone(parse_time(DAY_MINUTES + 1))

    def test_format_time_wraps_the_day_end(self):
        self.assertEqual(format_time(0), "00:00")
        self.assertEqual(format_time(DAY_MINUTES), "24:00")

    def test_clamp_rate_scrubs_nan(self):
        self.assertEqual(clamp_rate(float("nan")), 0.0)
        self.assertEqual(clamp_rate("不是数字"), 0.0)
        self.assertLessEqual(abs(clamp_rate(99.0)), 0.3)


class SegmentParsingTest(unittest.TestCase):
    def test_segment_start_never_crosses_now(self):
        for now in (0, 1, 59, 60, 61, 599, 1439):
            for step in (1, 5, 15, 30, 60):
                with self.subTest(now=now, step=step):
                    start = segment_start(now, step)
                    self.assertLessEqual(start, now)
                    self.assertGreaterEqual(start, 0)

    def test_parse_segment_rejects_non_mapping(self):
        self.assertIsNone(parse_segment("[]", now_minute=600))
        self.assertIsNone(parse_segment(None, now_minute=600))
        self.assertIsNone(parse_segment(12345, now_minute=600))

    def test_parse_segment_needs_an_event(self):
        self.assertIsNone(parse_segment({"minutes": 60}, now_minute=600))

    def test_minutes_are_clamped_to_the_segment_window(self):
        tiny = parse_segment({"event": "发呆", "minutes": 1}, now_minute=600)
        span = parse_time(tiny["end"]) - parse_time(tiny["start"])
        self.assertGreaterEqual(span, SEGMENT_MIN_MINUTES)

        huge = parse_segment({"event": "写方案", "minutes": 100000}, now_minute=600)
        span = parse_time(huge["end"]) - parse_time(huge["start"])
        self.assertLessEqual(span, SEGMENT_MAX_MINUTES)

    def test_sleep_segments_get_the_longer_cap(self):
        nap = parse_segment({"event": "睡觉", "minutes": 100000}, now_minute=600)
        span = parse_time(nap["end"]) - parse_time(nap["start"])
        self.assertLessEqual(span, SEGMENT_SLEEP_MAX_MINUTES)
        self.assertGreater(span, SEGMENT_MAX_MINUTES, "睡觉段该能比清醒段长")

    def test_a_segment_may_carry_past_midnight(self):
        slot = parse_segment({"event": "写方案", "minutes": 180}, now_minute=23 * 60)
        self.assertEqual(slot["end"], "24:00", "今天这半截截到 24:00")
        self.assertEqual(slot.get("carry_end"), "02:00", "剩下那截结转给明天")

    def test_junk_minutes_fall_back_instead_of_raising(self):
        for junk in ("abc", None, [], {}):
            with self.subTest(junk=junk):
                slot = parse_segment({"event": "写方案", "minutes": junk}, now_minute=600)
                self.assertIsNotNone(slot)
                self.assertGreater(parse_time(slot["end"]), parse_time(slot["start"]))

    def test_continue_extends_the_previous_event(self):
        prev = {"event": "写方案", "location": "书房", "emotion": "专注", "energy_rate": 0.0}
        for flag in ("true", "1", "yes", "是", "继续", True):
            with self.subTest(flag=flag):
                slot = parse_segment({"continue": flag, "minutes": 60}, now_minute=600, prev=prev)
                self.assertEqual(slot["event"], "写方案")
                self.assertEqual(slot["location"], "书房")

    def test_continue_without_a_previous_segment_is_dropped(self):
        self.assertIsNone(parse_segment({"continue": True, "minutes": 60}, now_minute=600))

    def test_fields_are_truncated_to_their_limits(self):
        slot = parse_segment(
            {
                "event": "写" * 60,
                "location": "地" * 60,
                "emotion": "情" * 60,
                "minutes": 60,
            },
            now_minute=600,
        )
        self.assertLessEqual(len(slot["event"]), 24)
        self.assertLessEqual(len(slot["location"]), 16)
        self.assertLessEqual(len(slot["emotion"]), 12)


class DynamicSegmentTest(unittest.TestCase):
    def test_fallback_never_assigns_an_identity_to_the_place(self):
        for hour in range(24):
            with self.subTest(hour=hour):
                slot = dynamic_segment(
                    cfg(), {}, now_minute=hour * 60, step=15, seed=["bot", f"d{hour}"]
                )
                for banned in ("工位", "教室", "书房", "通勤"):
                    self.assertNotIn(banned, slot.get("location", ""))
                    self.assertNotIn(banned, slot["event"])

    def test_fallback_is_deterministic_for_the_same_seed(self):
        first = dynamic_segment(cfg(), {"energy": 20.0}, now_minute=600, step=15, seed=["bot", "d"])
        second = dynamic_segment(cfg(), {"energy": 20.0}, now_minute=600, step=15, seed=["bot", "d"])
        self.assertEqual(first, second)

    def test_extreme_body_values_do_not_raise(self):
        for body in (
            {"energy": 0.0},
            {"energy": 100.0},
            {"hunger": 100.0},
            {"discomfort": 100.0},
            {"sleep_pressure": 100.0},
            {"energy": float("nan"), "hunger": float("nan")},
        ):
            with self.subTest(body=body):
                slot = dynamic_segment(cfg(), body, now_minute=3 * 60, step=15, seed=["bot", "d"])
                self.assertTrue(slot["event"])
                self.assertLess(parse_time(slot["start"]), parse_time(slot["end"]))


class DayPhrasesTest(unittest.TestCase):
    def test_the_four_keys_are_always_present(self):
        """社交侧靠 `day.get("wants")` 区分「缺键」与「今天没有」，键不能省。"""
        out = day_phrases([], 600)
        self.assertEqual(set(out), {"doing", "done", "next", "wants"})
        self.assertEqual(out["wants"], [])

    def test_empty_schedule_does_not_raise(self):
        for minutes in (0, 1, 720, 1439):
            with self.subTest(minutes=minutes):
                out = day_phrases([], minutes)
                self.assertIsInstance(out["doing"], str)


class ScheduleServiceEdgeTest(unittest.TestCase):
    def build(self, config=None, moment=MOMENT, random_source=None):
        conf = config or cfg()
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load(moment.strftime("%Y-%m-%d"), conf.cycle_length)
        clock = FrozenClock(moment)
        service = ScheduleService(
            store.scope,
            lambda: conf,
            clock,
            logger=RecordingLogger(),
            body_provider=None,
            random_source=random_source or random.Random(0),
        )
        return service, store

    # -- 未完成事项 ---------------------------------------------------------

    def test_ongoing_tasks_drop_after_the_ttl(self):
        service, store = self.build()
        now = MOMENT.timestamp()
        store.set(
            "ongoing_tasks",
            [
                {"what": "整理旧照片", "at": now - 5 * 86400},  # 过期
                {"what": "写完那封信", "at": now - 3600},  # 还在
            ],
        )
        self.assertEqual(service.ongoing_tasks(), ["写完那封信"])

    def test_ongoing_tasks_are_capped(self):
        service, store = self.build()
        now = MOMENT.timestamp()
        store.set("ongoing_tasks", [{"what": f"第{i}件事", "at": now} for i in range(8)])
        self.assertEqual(len(service.ongoing_tasks()), service.ONGOING_MAX)

    def test_ongoing_tasks_survive_junk_entries(self):
        service, store = self.build()
        now = MOMENT.timestamp()
        store.set(
            "ongoing_tasks",
            ["不是字典", {"what": "", "at": now}, {"what": "写完那封信", "at": "不是数字"}],
        )
        # `at` 读不出来的按 0 处理（远古），于是被 TTL 滤掉；不能因为一条脏数据整列消失
        self.assertEqual(service.ongoing_tasks(), [])

    def test_reworded_progress_does_not_pile_up(self):
        """「还差 4 份」→「还差 2 份」是同一件事，不该堆成两条。"""
        service, store = self.build()
        service._note_ongoing({"ongoing": "整理那份个案还差4份"})
        service._note_ongoing({"ongoing": "整理那份个案还差2份"})
        items = store.get("ongoing_tasks")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["what"], "整理那份个案还差2份")

    def test_ongoing_notes_keep_only_the_newest(self):
        service, store = self.build()
        for index in range(6):
            service._note_ongoing({"ongoing": f"第{index}件事还没做完"})
        self.assertEqual(len(store.get("ongoing_tasks")), service.ONGOING_MAX)

    # -- 跨天 ---------------------------------------------------------------

    def test_a_new_day_without_a_carry_leaves_yesterday_in_place(self):
        """没有结转段时不动 `today_date`：昨天那份还要留着给 prompt 当收尾。"""
        service, store = self.build()
        store.set("today_date", "2026-08-21")
        store.set("daily_schedule", [{"event": "写方案", "start": "09:00", "end": "12:00"}])
        service._migrate_new_day(TODAY)
        self.assertEqual(store.get("today_date"), "2026-08-21")

    def test_a_new_day_with_a_carry_files_yesterday_away(self):
        service, store = self.build()
        store.set("today_date", "2026-08-21")
        store.set("daily_schedule", [{"event": "写方案", "start": "09:00", "end": "12:00"}])
        store.set(CARRY_KEY, {"event": "睡觉", "start": "00:00", "end": "07:00"})
        service._migrate_new_day(TODAY)
        self.assertEqual(store.get("today_date"), TODAY)
        self.assertIsNone(store.get(CARRY_KEY))
        previous = store.get(service.PREV_DAY_KEY)
        self.assertTrue(previous)
        self.assertEqual(previous[-1]["event"], "写方案")
        self.assertEqual(len(store.get("daily_schedule")), 1)

    def test_migrating_twice_on_the_same_day_is_a_noop(self):
        service, store = self.build()
        store.set("today_date", TODAY)
        store.set("daily_schedule", [{"event": "写方案", "start": "09:00", "end": "12:00"}])
        service._migrate_new_day(TODAY)
        self.assertEqual(len(store.get("daily_schedule")), 1)

    # -- 段落的增删 ---------------------------------------------------------

    def _segment(self, event: str, start_minute: int, minutes: int = 30):
        return parse_segment(
            {"event": event, "minutes": minutes, "energy_rate": 0.0},
            now_minute=start_minute,
        )

    def test_segments_stay_sorted_and_do_not_grow_without_bound(self):
        service, _ = self.build(cfg(schedule_max_slots=6))
        for index in range(12):
            slot = self._segment(f"第{index}件事", start_minute=index * 75, minutes=30)
            service._append_segment(slot, TODAY)
        segments = service.segments()
        self.assertLessEqual(len(segments), 6, "`schedule_max_slots` 是硬上限")
        starts = [parse_time(item["start"]) for item in segments]
        self.assertEqual(starts, sorted(starts), "段落必须按开始时间有序")

    def test_a_junk_slot_does_not_raise(self):
        """脏段落（字段缺失 / 时间不是时间）不该把日表写坏。

        `None` 不在此列：两个调用点都保证拿到 Mapping——`seed_first_segment` 给的是
        `dynamic_segment` 的返回值，`ensure_fresh` 在 `schedule.py:1459-1460`
        先 `if slot is None: return False` 才走到 `_append_segment`。
        """
        service, _ = self.build()
        for slot in ({}, {"event": "", "start": "不是时间", "end": "??"}, {"event": "发呆"}):
            with self.subTest(slot=slot):
                service._append_segment(dict(slot), TODAY)

    # -- 触发判定 -----------------------------------------------------------

    def test_sleeping_segment_is_not_torn_up_by_a_message(self):
        night = datetime(2026, 8, 22, 23, 30, tzinfo=TZ)
        service, _ = self.build(moment=night)
        service._append_segment(self._segment("睡觉", 23 * 60, 180), TODAY)
        self.assertTrue(service.segments(), "23:00 那段此刻该是生效的")
        self.assertFalse(service.refresh_due(), "睡着时不因为来消息就换段")

    def test_the_read_path_does_not_write_state(self):
        """`_due_peek` 是给诊断用的只读入口：不掷骰子、不清过期来访。"""
        service, store = self.build()
        service._append_segment(self._segment("写方案", 9 * 60, 60), TODAY)
        before = dict(store.data)
        service._due_peek()
        self.assertEqual(before, dict(store.data))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
