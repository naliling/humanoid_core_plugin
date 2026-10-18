"""v2.27.6 新增项回归：自定义节日每年重复、着凉小事件、好感度排行榜。

三件事各自的契约：

- 节日：`MM-DD` 每年这一天都算，`YYYY-MM-DD` 只在当年那天命中；
- 着凉：受冻/缺觉后小概率发生，进体感，自己会好；只由客观量起头；
- 排行榜：按当前好感排序、能带上称呼、没档案的人不出现。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from humanoid.config import INT_SETTING_RANGES, HumanoidConfig
from humanoid.data.holidays import resolve_holiday
from humanoid.diagnostics import _signals_line, build_report, config_error_banner
from humanoid.engine import HumanoidEngine
from humanoid.services.soma import SomaService

from .fakes import FakeContext, FakeProvider, FrozenClock, ScopeStore
from .test_main_integration import FakeEvent, collect, main_module


class HolidayRepeatTest(unittest.TestCase):
    """自定义节日的两种写法。"""

    def test_yearless_date_returns_every_year(self):
        cfg = HumanoidConfig.from_raw(
            {"holidays": [{"date": "05-20", "name": "我们的纪念日"}]}
        )
        for year in (2026, 2027, 2035):
            moment = datetime(year, 5, 20, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            self.assertEqual(resolve_holiday(moment, cfg.holidays), "我们的纪念日")

    def test_dated_holiday_only_matches_that_year(self):
        cfg = HumanoidConfig.from_raw(
            {"holidays": [{"date": "2026-09-25", "name": "体检节"}]}
        )
        hit = datetime(2026, 9, 25, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        other = datetime(2027, 9, 25, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(resolve_holiday(hit, cfg.holidays), "体检节")
        self.assertEqual(resolve_holiday(other, cfg.holidays), "")

    def test_unpadded_dates_are_accepted(self):
        """面板里手填的日期不保证补零：6-1 与 06-01 是同一回事。"""
        cfg = HumanoidConfig.from_raw(
            {"holidays": [{"date": "6-1", "name": "小儿童节"}]}
        )
        moment = datetime(2027, 6, 1, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(resolve_holiday(moment, cfg.holidays), "小儿童节")

    def test_builtin_still_wins_when_no_custom(self):
        moment = datetime(2026, 10, 1, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(resolve_holiday(moment, []), "国庆节")

    def test_plain_string_entries_are_accepted(self):
        """按 hint 直接填字符串也要生效——面板的 list 编辑器产出的就是字符串。

        原来只认带 `date` 键的 Mapping，字符串被**静默丢弃**：用户照着
        「日期写 MM-DD（如 05-20）」填进去，节日一个都不生效、日志里一个字都没有。
        """
        cfg = HumanoidConfig.from_raw(
            {"holidays": ["05-20 我们的纪念日", "2026-09-25:体检", "6-1 小儿童节"]}
        )
        self.assertEqual(len(cfg.holidays), 3)
        moment = datetime(2027, 5, 20, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(resolve_holiday(moment, cfg.holidays), "我们的纪念日")
        # 不补零的写法归一化后同样命中
        child = datetime(2028, 6, 1, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(resolve_holiday(child, cfg.holidays), "小儿童节")
        # 带年份的只在当年那天
        hit = datetime(2026, 9, 25, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(resolve_holiday(hit, cfg.holidays), "体检")
        # 存的是归一化后的写法，面板里再手填 6-1 也认得
        self.assertEqual([h["date"] for h in cfg.holidays], ["05-20", "2026-09-25", "06-01"])

    def test_unparseable_entries_are_dropped(self):
        """认不出来的项丢掉，但不能把整份配置带崩。"""
        cfg = HumanoidConfig.from_raw(
            {"holidays": ["不是日期", {"name": "没有日期"}, "05-20 有效"]}
        )
        self.assertEqual([h["date"] for h in cfg.holidays], ["05-20"])


class ChillTest(unittest.TestCase):
    """着凉：小概率、有起头条件、自己会好、进体感。"""

    def _soma(self, *, weather=None):
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load()
        clock = FrozenClock(datetime(2026, 10, 5, 3, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
        return SomaService(
            store.scope, lambda: HumanoidConfig(), clock,
            schedule_provider=lambda: [],
            weather_provider=(lambda: weather) if weather is not None else None,
        )

    def test_no_chill_without_exposure(self):
        """只是难受（不适高）但既不冷也不欠觉：不会着凉。"""
        soma = self._soma()
        for _ in range(2000):
            soma._maybe_chill(10.0, soma._clock.now(), 60.0)
        self.assertEqual(soma.snapshot().get("chill", 0.0), 0.0)

    def test_cold_and_debt_can_trigger_and_recover(self):
        soma = self._soma(weather={"env": "天气：多云，气温 -2℃"})
        soma._set("sleep_debt", 8.0)
        hit = False
        for _ in range(4000):
            soma._maybe_chill(10.0, soma._clock.now(), 60.0)
            if soma.snapshot().get("chill", 0.0) > 0:
                hit = True
                break
        self.assertTrue(hit, "受冻 + 缺觉永远不着凉，概率参数是不是被调没了")
        level = soma.snapshot()["chill"]
        # 起头是「着凉」：轻，落在词表的 28 档附近（不是一上来就重感冒）
        self.assertGreaterEqual(level, 24.0)
        self.assertLessEqual(level, 32.0)
        # 一两天就该好：往前进 48 小时必须痊愈
        soma._maybe_chill(60.0 * 48, soma._clock.now(), 60.0)
        self.assertEqual(soma.snapshot()["chill"], 0.0)

    def test_chill_has_a_rising_then_falling_course(self):
        """B1：着了之后先加重（着凉→感冒），到顶再好转（感冒→恢复）。"""
        soma = self._soma()
        soma._set("chill", 26.0)
        soma._set("chill_peak", 60.0)
        soma._set("chill_rising", 1.0)
        levels = []
        for _ in range(80):
            soma._maybe_chill(60.0, soma._clock.now(), 60.0)
            levels.append(soma.snapshot()["chill"])
        peak = max(levels)
        self.assertGreater(peak, 55.0, "病程该走到「感冒」的顶上")
        peak_idx = levels.index(peak)
        self.assertGreater(peak_idx, 0, "起头不该是最高点：那不是病程，是一根直接下坡")
        self.assertLess(levels[0], peak, "病程前半在加重")
        self.assertLess(levels[-1], peak, "病程后半在好转")
        self.assertEqual(levels[-1], 0.0, "80 小时该痊愈了")

    def test_chill_shows_up_in_feelings(self):
        soma = self._soma()
        soma._set("chill", 40.0)
        lines = [text for _w, text in soma.feelings(80.0)]
        self.assertTrue(
            any("着凉" in t or "感冒" in t or "发冷" in t for t in lines),
            f"着了凉却没有任何一条体感提到它：{lines}",
        )


class PhaseMultipliersTest(unittest.TestCase):
    """周期恢复倍率：老配置里的 6 元素列表不能静默错位。"""

    def test_eight_values_pass_through(self):
        custom = [1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7]
        cfg = HumanoidConfig.from_raw({"energy_recovery_phase_multipliers": custom})
        self.assertEqual(list(cfg.energy_recovery_phase_multipliers), custom)

    def test_legacy_six_values_fall_back_to_default(self):
        """旧版是 6 段，索引语义已改成 8 段——长度对不上就整体用新默认，
        否则中间几段的恢复倍率会静默错位（面板显 8 段，代码按旧顺序取）。"""
        legacy = [0.5, 1.0, 2.0, 1.0, 0.8, 0.7]
        cfg = HumanoidConfig.from_raw({"energy_recovery_phase_multipliers": legacy})
        default = HumanoidConfig().energy_recovery_phase_multipliers
        self.assertEqual(len(default), 8)
        self.assertEqual(cfg.energy_recovery_phase_multipliers, default)
        self.assertNotEqual(list(cfg.energy_recovery_phase_multipliers), legacy)

    def test_every_phase_index_stays_in_range(self):
        """任一周期日都落在 0..7，且两张 8 项表都取得到。"""
        from humanoid.services.energy import CONSUMPTION_PHASE_FACTORS, PHASE_NAMES, PHASE_NOTES
        cfg = HumanoidConfig()
        for day in range(1, 60):
            idx = cfg.cycle_phase_index(day)
            self.assertTrue(0 <= idx <= 7, f"第 {day} 天映射出 {idx}")
            self.assertLess(idx, len(PHASE_NAMES))
            self.assertLess(idx, len(PHASE_NOTES))
            self.assertLess(idx, len(CONSUMPTION_PHASE_FACTORS))


class RankingTest(unittest.TestCase):
    """好感度排行榜。"""

    def _scope(self):
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load()
        scope = store.scope
        for uid, aff, name in (("1001", 70.0, "小明"), ("1002", 30.0, ""), ("1003", 95.0, "阿澈")):
            scope.user_state(uid)["mood"] = {"affection": aff}
            if name:
                scope.set_user(uid, "nickname", name)
        return scope

    def test_ranking_sorted_and_named(self):
        engine = HumanoidEngine.__new__(HumanoidEngine)
        text = engine.affection_ranking_text(SimpleNamespace(scope=self._scope()))
        lines = text.splitlines()
        self.assertEqual(len(lines), 4, text)
        # 展示顺序按好感升序：最高的人在最靠近输入框的最后一行
        self.assertIn("1002", lines[1])
        self.assertIn("小明", lines[2])
        self.assertIn("阿澈", lines[3])
        self.assertIn("1003", lines[3])
        # 序号是**真实名次**，不随展示顺序变；否则序号 1 反而是榜内最低的
        self.assertTrue(lines[1].startswith("  3."), f"30 分应是第 3 名：{lines[1]!r}")
        self.assertTrue(lines[2].startswith("  2."), f"70 分应是第 2 名：{lines[2]!r}")
        self.assertTrue(lines[3].startswith("  1."), f"95 分应是第 1 名：{lines[3]!r}")

    def test_ranking_respects_limit(self):
        engine = HumanoidEngine.__new__(HumanoidEngine)
        text = engine.affection_ranking_text(SimpleNamespace(scope=self._scope()), limit=2)
        # 取前 2 名：95 分的阿澈与 70 分的小明；30 分的 1002 被截掉
        self.assertNotIn("1002", text)
        self.assertIn("阿澈", text)
        self.assertIn("共 3 人", text)

    def test_people_without_profile_do_not_appear(self):
        scope = self._scope()
        scope.user_state("1004")  # 只有昵称、没有情绪档案的空壳
        scope.set_user("1004", "nickname", "路人")
        engine = HumanoidEngine.__new__(HumanoidEngine)
        text = engine.affection_ranking_text(SimpleNamespace(scope=scope))
        self.assertNotIn("1004", text)
        self.assertNotIn("路人", text)


if __name__ == "__main__":
    unittest.main()


class ConfigLayerGuardsTest(unittest.TestCase):
    """配置层的静默失效：合法下限关掉整条功能、字符串 "false" 当开启、无日志退回默认值。"""

    def test_mood_sensitivity_zero_is_lifted_to_the_floor(self):
        """0 不是「最不敏感」而是冻住全部情绪变化（factor = 0/100），下限必须是 1。"""
        self.assertEqual(HumanoidConfig.from_raw({"mood_sensitivity": 0}).mood_sensitivity, 1)
        self.assertEqual(HumanoidConfig.from_raw({"mood_sensitivity": -5}).mood_sensitivity, 1)
        self.assertEqual(HumanoidConfig.from_raw({}).mood_sensitivity, 60)

    def test_mood_negative_streak_floor_is_two(self):
        """1 等于关掉「连续才扣好感」这条规则本身，下限抬到 2。"""
        self.assertEqual(
            HumanoidConfig.from_raw({"mood_negative_streak_required": 1}).mood_negative_streak_required,
            2,
        )
        self.assertEqual(
            HumanoidConfig.from_raw({"mood_negative_streak_required": 4}).mood_negative_streak_required,
            4,
        )

    def test_ccb_goes_through_the_shared_bool_parser(self):
        """`bool(src.get("ccb", False))` 让任何非空字符串都为真：手改 JSON 会**意外打开**。"""
        for raw, expected in (
            ("false", False), ("0", False), ("否", False), ("关", False), ("no", False),
            ("true", True), ("1", True), ("是", True), (True, True), (False, False),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(HumanoidConfig.from_raw({"ccb": raw}).ccb, expected)

    def test_equal_night_hours_warn_that_the_window_is_gone(self):
        """起止相同 = 没有夜里（不是「全天算夜里」）：行为不变，但必须留一条日志。"""
        with self.assertLogs("humanoid.config", level="WARNING") as caught:
            cfg = HumanoidConfig.from_raw({"night_start_hour": 8, "night_end_hour": 8})
        self.assertIn("夜间窗口", "\n".join(caught.output))
        self.assertEqual(cfg.night_span_hours, 0.0)
        self.assertFalse(cfg.is_night_hour(9))

    def test_illegal_enum_logs_a_warning_and_keeps_the_default(self):
        """面板里写 "low"/"GROUP"/"1min" 会静默退回默认：面板显示值与生效值必须能被发现。"""
        with self.assertLogs("humanoid.config", level="WARNING") as caught:
            cfg = HumanoidConfig.from_raw({
                "inject_activity_context": "low",
                "environment_mode": "GROUP",
                "schedule_time_granularity": "1min",
            })
        self.assertEqual(cfg.inject_activity_context, "medium")
        self.assertEqual(cfg.environment_mode, "both")
        self.assertEqual(cfg.schedule_time_granularity, "15min")
        text = "\n".join(caught.output)
        for bad in ("low", "GROUP", "1min"):
            self.assertIn(bad, text)

    def test_phase_multiplier_length_mismatch_logs_a_warning(self):
        """长度不符会整表退回默认：静默退回等于用户以为自己的倍率还在生效。"""
        with self.assertLogs("humanoid.config", level="WARNING") as caught:
            cfg = HumanoidConfig.from_raw(
                {"energy_recovery_phase_multipliers": [1.0] * 6}
            )
        self.assertEqual(cfg.energy_recovery_phase_multipliers, HumanoidConfig().energy_recovery_phase_multipliers)
        self.assertIn("8", "\n".join(caught.output))

    def test_affection_override_tolerates_comma_separated_entries(self):
        """面板 hint 曾教「一个条目里用逗号分隔多条」：不拆的话 float() 抛错、两条一起失效。"""
        cfg = HumanoidConfig.from_raw({"mood_affection_override": ["123:100,456:50"]})
        self.assertEqual(cfg.affection_override_for("123"), 100.0)
        self.assertEqual(cfg.affection_override_for("456"), 50.0)
        # 一条一项的写法照旧
        one = HumanoidConfig.from_raw({"mood_affection_override": ["777:95"]})
        self.assertEqual(one.affection_override_for("777"), 95.0)
        self.assertIsNone(one.affection_override_for("778"))
        # 数值坏掉时该条无效（与旧行为一致）
        self.assertIsNone(HumanoidConfig.from_raw(
            {"mood_affection_override": ["888:abc"]}
        ).affection_override_for("888"))

    def test_cycle_phase_index_survives_non_numeric_input(self):
        """直接 int(cycle_day) 会抛 ValueError，而 phase_recovery_multiplier 的调用方没有 try。"""
        cfg = HumanoidConfig()
        self.assertEqual(cfg.cycle_phase_index("abc"), cfg.cycle_phase_index(1))
        self.assertEqual(cfg.cycle_phase_index(None), cfg.cycle_phase_index(1))
        self.assertIsInstance(cfg.phase_recovery_multiplier("abc"), float)

    def test_int_setting_ranges_are_the_single_source_of_clamping(self):
        """命令行与 from_raw 必须用同一份区间：-1 曾被静默钳成 0（= 不限额度）。"""
        for key, (low, high) in INT_SETTING_RANGES.items():
            with self.subTest(key=key):
                self.assertEqual(getattr(HumanoidConfig.from_raw({key: low - 1}), key), low)
                self.assertEqual(getattr(HumanoidConfig.from_raw({key: high + 1}), key), high)
        self.assertEqual(
            HumanoidConfig.from_raw({"llm_daily_call_budget": -1}).llm_daily_call_budget, 0
        )

    def test_rate_fields_clamp_to_the_hint_magnitude(self):
        """hint 写 0.05~1.0，代码钳制上限却是 100：填 50 会被原样采用。"""
        self.assertEqual(
            HumanoidConfig.from_raw({"social_energy_consumption_per_msg": 50.0}).social_energy_consumption_per_msg,
            1.0,
        )
        self.assertEqual(
            HumanoidConfig.from_raw({"social_energy_recovery_per_minute": 50.0}).social_energy_recovery_per_minute,
            3.0,
        )
        self.assertEqual(
            HumanoidConfig.from_raw({"energy_natural_recovery_per_minute": 50.0}).energy_natural_recovery_per_minute,
            0.9,
        )
        self.assertEqual(
            HumanoidConfig.from_raw({"energy_consumption_per_msg": 50.0}).energy_consumption_per_msg,
            0.9,
        )
        # 默认值（v2.27.38 校准：0.05 → 0.5，见 config.py 里的换算说明）
        self.assertEqual(HumanoidConfig().social_energy_consumption_per_msg, 0.5)

    def test_schedule_persona_max_chars_still_goes_through_the_int_helper(self):
        """全文件唯一曾经不走 i() 的字段：坏值以前会一路冒到 initialize()，插件加载失败。"""
        # 认不出来就退回**默认值**（3000，仍在区间内），关键是别再抛出去
        self.assertEqual(
            HumanoidConfig.from_raw({"schedule_persona_max_chars": "abc"}).schedule_persona_max_chars,
            3000,
        )
        self.assertEqual(
            HumanoidConfig.from_raw({"schedule_persona_max_chars": "3000.0"}).schedule_persona_max_chars,
            3000,
        )
        # 越界钳到区间
        self.assertEqual(
            HumanoidConfig.from_raw({"schedule_persona_max_chars": 50}).schedule_persona_max_chars,
            200,
        )
        self.assertEqual(
            HumanoidConfig.from_raw({"schedule_persona_max_chars": 3000}).schedule_persona_max_chars,
            3000,
        )


class _BoomResolver:
    """available_ids() 就炸——模拟诊断里任何一处取值失败。"""

    def available_ids(self):
        raise RuntimeError("boom: available_ids")


class _QuietResolver:
    def available_ids(self):
        return ["p1"]

    def resolve(self, provider_id):
        return None

    def resolve_global(self, umo=None):
        return None

    def id_of(self, provider):
        return "p1"


class _QuietGateway:
    gate = None

    def cooldown_remaining(self, provider_id, purpose):
        return 0.0

    def cooldowns(self, purpose):
        return {}

    def last_result(self, purpose):
        return None


class DiagnosticsFallbackTest(unittest.TestCase):
    """`/拟人 诊断` 是唯一的排查入口：它自己报错等于把用户挡在门外。"""

    def test_signals_line_survives_a_missing_age(self):
        """link.py 在 stat() 失败时把 age 写成 None，旧代码 f"{None:.0f}" 抛 TypeError。"""
        text = _signals_line({
            "state": "fresh", "path": "/tmp/signals.json",
            "age": None, "last_proactive_age": -1,
        })
        self.assertIn("上次刷新 0s 前", text)

    def test_signals_line_survives_a_non_numeric_age(self):
        text = _signals_line({"state": "fresh", "age": "刚刚", "last_proactive_age": -1})
        self.assertIn("上次刷新 0s 前", text)

    def test_signals_line_reports_a_file_it_could_not_read(self):
        """坏 JSON / 超大 / 目录：`link.py` 报 `unreadable`，不能说成「拿不到 data 目录」。"""
        text = _signals_line({
            "state": "unreadable", "path": "/tmp/signals.json", "age": 3.0, "payload": {},
        })
        self.assertIn("读不出内容", text)
        self.assertIn("/tmp/signals.json", text)
        self.assertNotIn("拿不到 data 目录", text)

    def test_signals_line_counts_an_immediate_proactive_as_having_spoken(self):
        """`last_proactive_age` 正好 0.0 是「刚刚开过口」，不是「还没开过口」。"""
        text = _signals_line({
            "state": "fresh", "path": "/tmp/signals.json",
            "age": 1.0, "last_proactive_age": 0.0, "ignored_streak": 0,
        })
        self.assertIn("上次主动开口 0s 前", text)
        self.assertNotIn("还没主动开过口", text)

    def test_signals_line_says_never_when_the_age_is_the_sentinel(self):
        text = _signals_line({
            "state": "fresh", "path": "/tmp/signals.json",
            "age": 1.0, "last_proactive_age": -1.0,
        })
        self.assertIn("还没主动开过口", text)

    def test_build_report_falls_back_instead_of_raising(self):
        with self.assertLogs("humanoid.diagnostics", level="ERROR"):
            text = build_report(
                cfg=HumanoidConfig(),
                resolver=_BoomResolver(),
                gateway=_QuietGateway(),
                schedule_status={},
                activity_status={},
                version="0.0.0",
            )
        self.assertIn("诊断报告生成失败", text)
        self.assertIn("RuntimeError", text)

    def test_build_report_shows_the_config_load_error_at_the_top(self):
        text = build_report(
            cfg=HumanoidConfig(),
            resolver=_QuietResolver(),
            gateway=_QuietGateway(),
            schedule_status={},
            activity_status={},
            version="0.0.0",
            config_load_error="ValueError: boom",
        )
        self.assertIn("配置解析失败，已整体退回默认值：ValueError: boom", text)
        self.assertLess(
            text.index("配置解析失败"),
            text.index("【AstrBot 可用对话模型 id】"),
            "配置兜底提示必须在报告最顶部，否则用户会把它当成正常的默认配置",
        )

    def test_config_error_banner_is_empty_without_an_error(self):
        self.assertEqual(config_error_banner(""), [])
        banner = config_error_banner("ValueError: boom")
        self.assertTrue(banner)
        self.assertIn("ValueError: boom", banner[0])


class _WritableConfig(dict):
    """带 save_config 的配置对象：模拟 AstrBot 真正交给插件的那个。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved = 0

    def save_config(self):
        self.saved += 1


class DefaultsMigrationMarkerTest(unittest.TestCase):
    """标记只在**真正落盘成功**后写，且记的是「已应用过哪些迁移项」而不是版本号。"""

    LEGACY = {"timezone_city": "北京", "weather_location": "Heyuan,CN"}

    def test_unwritable_config_never_writes_the_marker(self):
        """普通 dict 没有 save_config：改动只在内存，写标记会让迁移**永远**不再重试。"""
        data_dir = Path(tempfile.mkdtemp())
        raw = dict(self.LEGACY)
        main_module.HumanoidCore._migrate_defaults(raw, data_dir)
        self.assertEqual(raw["weather_location"], "", "内存里该改还是改")
        self.assertFalse(
            (data_dir / main_module.DEFAULTS_MIGRATION_MARK).exists(),
            "没落盘却写了标记：下次启动读回旧值，而迁移再也不会跑",
        )

    def test_marker_records_the_applied_keys_not_a_version(self):
        data_dir = Path(tempfile.mkdtemp())
        raw = _WritableConfig(self.LEGACY)
        main_module.HumanoidCore._migrate_defaults(raw, data_dir)
        self.assertEqual(raw.saved, 1)
        marker = data_dir / main_module.DEFAULTS_MIGRATION_MARK
        self.assertTrue(marker.exists())
        applied = json.loads(marker.read_text(encoding="utf-8"))
        self.assertIn("weather_location", applied)
        # 再跑一次：这一项已记录，不重复落盘
        again = _WritableConfig(self.LEGACY)
        main_module.HumanoidCore._migrate_defaults(again, data_dir)
        self.assertEqual(again.saved, 0, "已应用过的迁移不该重跑")

    def test_a_new_migration_still_reaches_old_users(self):
        """标记里没记录的新迁移必须照常生效——旧版把版本号写进**文件名**，于是全被跳过。"""
        data_dir = Path(tempfile.mkdtemp())
        marker = data_dir / main_module.DEFAULTS_MIGRATION_MARK
        data_dir.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(["schedule_max_slots"]), encoding="utf-8")
        raw = _WritableConfig(self.LEGACY)
        main_module.HumanoidCore._migrate_defaults(raw, data_dir)
        self.assertEqual(raw.saved, 1, "标记里没记录的迁移没生效")
        self.assertEqual(raw["weather_location"], "")


@unittest.skipIf(main_module is None, "main.py 加载失败（缺少 astrbot 或 aiohttp）")
class EntryLayerFixesTest(unittest.IsolatedAsyncioTestCase):
    """入口层：参照名称清洗、设置项的区间与回执、管理员指令前缀。"""

    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.ctx = FakeContext(chat_providers=[FakeProvider("p1")])
        self.raw = {
            "timezone_city": "北京",
            "schedule_provider_name": "p1",
            "admin_qq": ["10001"],
            "state_flush_interval_seconds": 1,
            "schedule_max_interval_minutes": 60,
        }
        main_module.get_astrbot_data_path = lambda: str(self.tmp)
        self.star = main_module.HumanoidCore(self.ctx, self.raw)
        await self.star.initialize()

    async def asyncTearDown(self) -> None:
        await self.star.terminate()

    def _core(self):
        return self.star._core(FakeEvent("/你的状态"))

    async def test_char_name_is_sanitized_before_it_reaches_every_prompt(self):
        """名字进的是**每一轮** system_prompt（`role = f"「{who}」"`），必须同 /叫我 口径清洗。"""
        out = await collect(self.star.cmd_set_char_name(FakeEvent("拟人 参照名称 小\n雨")))
        self.assertIn("已去掉", out[-1])
        self.assertEqual(self._core().char_name(), "小雨")
        # 引号/控制字符同样被剥掉，不会闭合注入里的「」
        out = await collect(self.star.cmd_set_char_name(FakeEvent("拟人 参照名称 阿「澈」")))
        self.assertIn("已去掉", out[-1])
        self.assertEqual(self._core().char_name(), "阿澈")
        out = await collect(self.star.cmd_set_char_name(FakeEvent("拟人 参照名称 \x07小雨")))
        self.assertEqual(self._core().char_name(), "小雨")

    async def test_char_name_that_sanitizes_to_nothing_is_rejected(self):
        await collect(self.star.cmd_set_char_name(FakeEvent("拟人 参照名称 小雨")))
        # "\x00" 过得了 `_arg_after` 的 strip，但清洗后为空——正是要拦的那一类
        for bad in ("她", "你", "TA", "\x00", "「」"):
            with self.subTest(bad=bad):
                out = await collect(
                    self.star.cmd_set_char_name(FakeEvent(f"拟人 参照名称 {bad}"))
                )
                self.assertIn("用不了", out[-1])
                self.assertEqual(self._core().char_name(), "小雨", "拒绝的输入不该改掉旧名字")

    async def test_settings_rejects_out_of_range_throttle_values(self):
        """`/拟人 设置 预算 -1` 曾把 -1 写进配置、解析时钳成 0（= 不限额度），回执还说「已设为 -1」。"""
        for word, value in (("预算", "-1"), ("间隔", "-10"), ("静默", "99999")):
            with self.subTest(word=word):
                out = await collect(self.star.cmd_settings(FakeEvent(f"拟人 设置 {word} {value}")))
                self.assertIn("不是这一项能接受的值", out[-1])
        self.assertNotIn("llm_daily_call_budget", self.raw)
        self.assertNotIn("schedule_min_interval_minutes", self.raw)
        self.assertNotIn("llm_idle_silence_minutes", self.raw)

    async def test_settings_receipt_reports_the_effective_value(self):
        out = await collect(self.star.cmd_settings(FakeEvent("拟人 设置 预算 0")))
        self.assertIn("已设为 0", out[-1])
        self.assertEqual(self.star._config.llm_daily_call_budget, 0)
        out = await collect(self.star.cmd_settings(FakeEvent("拟人 设置 间隔 45")))
        self.assertIn(f"已设为 {self.star._config.schedule_min_interval_minutes!r}", out[-1])
        self.assertEqual(self.star._config.schedule_min_interval_minutes, 45)

    async def test_settings_rejects_an_inverted_interval_range(self):
        """`间隔` 只写 min：不校验就能造出 120~60 的倒置区间。"""
        out = await collect(self.star.cmd_settings(FakeEvent("拟人 设置 间隔 120")))
        self.assertIn("不能大于上限", out[-1])
        self.assertNotIn("schedule_min_interval_minutes", self.raw)
        ok = await collect(self.star.cmd_settings(FakeEvent("拟人 设置 间隔 60")))
        self.assertIn("已设为", ok[-1])
        self.assertEqual(self.star._config.schedule_min_interval_minutes, 60)

    async def test_admin_command_usage_hints_use_the_registered_prefix(self):
        """这两条挂在「拟人」组下：提示写成 /设置好感度 的话，照它打必然失败。"""
        usage = await collect(self.star.cmd_set_affection(FakeEvent("/拟人 设置好感度")))
        self.assertIn("/拟人 设置好感度", usage[0])
        batch = await collect(self.star.cmd_batch_affection(FakeEvent("/拟人 批量好感度")))
        self.assertIn("/拟人 批量好感度", batch[0])

    async def test_diagnose_shows_the_config_load_error(self):
        """`ConfigBox.load_error` 以前全仓无人读：配置整体退回默认值时用户完全不知道。"""
        out = await collect(self.star.cmd_diagnose(FakeEvent("拟人 诊断")))
        self.assertNotIn("配置解析失败", out[0])
        # 真的让解析炸一次（而不是手改 _load_error）：走 ConfigBox._parse 的兜底路径。
        # 注意要打**插件自己那份** config 模块里的类：main.py 是以合成包名载入的，
        # 它的 `humanoid.config` 与测试里 import 的顶层 `humanoid.config` 是两个模块对象。
        original = main_module.HumanoidConfig.from_raw

        def boom(raw):
            raise ValueError("boom: from_raw")

        main_module.HumanoidConfig.from_raw = staticmethod(boom)
        try:
            # 兜底会在 ERROR 级别打一条带 traceback 的日志：捕下来，别让它淹了测试输出
            with self.assertLogs(level="ERROR") as caught:
                self.star._config_box.reload()
        finally:
            main_module.HumanoidConfig.from_raw = original
        self.assertIn("配置解析失败", "\n".join(caught.output))
        self.assertTrue(self.star._config_box.load_error.startswith("ValueError"))
        # 退回默认值后 admin_qq 也复位了，这里用 AstrBot 全局管理员身份驱动
        out = await collect(self.star.cmd_diagnose(FakeEvent("拟人 诊断", admin=True)))
        self.assertIn("配置解析失败，已整体退回默认值：ValueError: boom: from_raw", out[0])
        self.assertLess(out[0].index("配置解析失败"), out[0].index("〖拟人诊断〗"))

    async def test_diagnose_never_raises_out_of_the_command(self):
        """engine.diagnostics_text 抛了也要回一段可读文本，而不是让指令整体报错。"""
        def boom(core=None):
            raise TypeError("boom: diagnostics_text")

        original = self.star.engine.diagnostics_text
        self.star.engine.diagnostics_text = boom
        try:
            out = await collect(self.star.cmd_diagnose(FakeEvent("拟人 诊断")))
        finally:
            self.star.engine.diagnostics_text = original
        self.assertIn("诊断生成失败", out[0])
        self.assertIn("TypeError", out[0])


# ── 提示词 / 穿搭 / LLM / 文本处理层的回归 ──


class JsonxRootTypeTest(unittest.TestCase):
    """只认根节点类型：数组响应不能被截成第一个元素，散文里的括号不能抢先。"""

    def test_object_extractor_rejects_an_array_root(self):
        from humanoid.jsonx import extract_json_object

        self.assertIsNone(
            extract_json_object('[{"a":1},{"b":2}]'), "数组响应不能被截成第一个元素"
        )
        self.assertIsNone(extract_json_object("[1,2,3]"))
        self.assertEqual(extract_json_object('他说"{不行}"，然后给出：{"x": 1}'), {"x": 1})

    def test_array_extractor_prefers_the_real_payload(self):
        from humanoid.jsonx import extract_json_array

        self.assertEqual(extract_json_array('prefix [1] then real [{"a":2}]'), [{"a": 2}])
        self.assertEqual(
            extract_json_array('好的{说明}，结果如下：[{"a":1},{"b":2}]'),
            [{"a": 1}, {"b": 2}],
        )
        self.assertEqual(extract_json_array("[1, 2]"), [1, 2])

    def test_fenced_block_is_tried_first(self):
        from humanoid.jsonx import extract_json_array

        raw = '```json\n[{"a": 1}]\n```\n后记 [9]'
        self.assertEqual(extract_json_array(raw), [{"a": 1}], "fence 里的内容最可信")


class InputStateTimeoutTest(unittest.IsolatedAsyncioTestCase):
    """`stop()` 与 `call_action` 都必须有超时——挂住的是**回复本身**。"""

    @staticmethod
    def _log():
        return SimpleNamespace(debug=lambda *a, **k: None, warning=lambda *a, **k: None)

    async def test_stop_does_not_wait_forever(self):
        from humanoid.input_state import STOP_WAIT_SECONDS, InputStateNotifier

        notifier = InputStateNotifier(self._log())

        async def hang():
            await asyncio.sleep(3600)

        task = asyncio.create_task(hang())
        notifier._tasks["k"] = (task, asyncio.Event())
        started = asyncio.get_event_loop().time()
        await notifier.stop("k")
        elapsed = asyncio.get_event_loop().time() - started
        self.assertLess(elapsed, STOP_WAIT_SECONDS + 1.0, f"stop 挂了 {elapsed:.1f}s")
        self.assertTrue(task.cancelled() or task.done())

    async def test_report_has_a_timeout(self):
        from humanoid import input_state

        class Client:
            async def call_action(self, *args, **kwargs):
                await asyncio.sleep(3600)

        notifier = input_state.InputStateNotifier(self._log())
        original = input_state.REPORT_TIMEOUT_SECONDS
        input_state.REPORT_TIMEOUT_SECONDS = 0.05
        try:
            with self.assertRaises(Exception):
                await notifier._report(Client(), "42")
        finally:
            input_state.REPORT_TIMEOUT_SECONDS = original


class PersonaCacheAndSentinelTest(unittest.IsolatedAsyncioTestCase):
    """解析失败不写 10 分钟缓存；`[%None]` 哨兵不能被返回值形状绕过。"""

    class _Manager:
        def __init__(self, resolved=None, boom=False):
            self._resolved = resolved
            self._boom = boom

        async def resolve_selected_persona(self, **kwargs):
            if self._boom:
                raise RuntimeError("boom")
            return self._resolved

        async def get_default_persona_v3(self, *args):
            if self._boom:
                raise RuntimeError("boom")
            return {"name": "默认", "prompt": "默认人格", "begin_dialogs": []}

    class _Context:
        conversation_manager = None

        def __init__(self, manager):
            self.persona_manager = manager

    async def test_marker_survives_a_shape_change(self):
        from humanoid.persona import NO_PERSONA, NO_PERSONA_MARKER, resolve_persona

        for shape in (
            (NO_PERSONA_MARKER, None, None, False),
            [NO_PERSONA_MARKER],
            NO_PERSONA_MARKER,
        ):
            with self.subTest(shape=shape):
                got = await resolve_persona(self._Context(self._Manager(shape)), "umo")
                self.assertEqual(got.source, NO_PERSONA, "哨兵被绕过 → 静默用了默认人格")
                self.assertEqual(got.prompt, "")

    async def test_failed_resolution_is_not_cached(self):
        from humanoid.persona import PersonaSource

        source = PersonaSource(
            self._Context(self._Manager(boom=True)),
            SimpleNamespace(warning=lambda *a, **k: None, debug=lambda *a, **k: None),
        )
        got = await source.persona("bot1")
        self.assertFalse(got.usable)
        self.assertEqual(source._cache, {}, "一次瞬时故障不该变成 10 分钟的「没有人设」")


class PromptBuilderGuardsTest(unittest.TestCase):
    """注入热路径上的防御性修复：一个坏值不该把整轮注入带走。"""

    @staticmethod
    def _builder(core, config):
        from humanoid.prompt_builder import PromptBuilder

        # `PromptBuilder.config` 是只读属性，读的是 `self._core.config`。
        core.config = config
        builder = object.__new__(PromptBuilder)
        builder._core = core
        return builder

    def test_short_weather_tolerates_non_dict(self):
        from humanoid.prompt_builder import _short_weather

        self.assertEqual(_short_weather(None), "")
        self.assertEqual(_short_weather("多云"), "")
        self.assertEqual(_short_weather({}), "")

    def test_feelings_lines_skips_broken_items(self):
        builder = self._builder(
            SimpleNamespace(
                energy=SimpleNamespace(energy=50.0),
                soma=SimpleNamespace(
                    feelings=lambda energy: [(80.0, "很困"), None, ("bad", "x")]
                ),
            ),
            SimpleNamespace(soma_enabled=True, show_sleep_window=True),
        )
        self.assertEqual(builder._feelings_lines(5, 50.0), ["她很困"])

    def test_night_lines_survive_a_broken_snapshot(self):
        builder = self._builder(
            SimpleNamespace(
                clock=SimpleNamespace(now=lambda: SimpleNamespace(hour=23, minute=0)),
                soma=SimpleNamespace(
                    in_scheduled_night=lambda hour: True, snapshot=lambda: "boom"
                ),
            ),
            SimpleNamespace(night_mode_enabled=True, show_sleep_window=True, soma_enabled=True),
        )
        self.assertEqual(builder._night_lines(), ["这会儿是她的睡眠时段"])

    def test_echo_lines_survive_a_broken_snapshot(self):
        builder = self._builder(
            SimpleNamespace(soma=SimpleNamespace(snapshot=lambda: "boom", data=None)),
            SimpleNamespace(),
        )
        self.assertEqual(builder._echo_lines(), [])

    def test_sleep_cause_line_survives_a_bad_value(self):
        builder = self._builder(
            SimpleNamespace(
                soma=SimpleNamespace(snapshot=lambda: {"asleep": "x", "last_sleep_hours": None})
            ),
            SimpleNamespace(),
        )
        self.assertEqual(builder._sleep_cause_line(), "")

    def test_behavior_lines_survive_a_bad_gap_seconds(self):
        from humanoid.prompt_builder import EVENT_TEXT

        builder = self._builder(
            SimpleNamespace(
                role_id="bot1", clock=SimpleNamespace(today_str=lambda: "2026-08-22")
            ),
            SimpleNamespace(),
        )
        events = [{"type": "long_gap", "data": {"gap_seconds": "很久"}}]
        self.assertEqual(
            builder._behavior_lines("42", events, {}, with_previous=False, is_group=True),
            [EVENT_TEXT["long_gap"]],
        )

    def test_sanitize_name_rejects_every_pronoun(self):
        from humanoid.prompt_builder import clean_label, sanitize_name

        for pronoun in ("她", "他", "你", "我", "它", "您", "TA"):
            with self.subTest(pronoun=pronoun):
                self.assertEqual(sanitize_name(f"小{pronoun}雨"), "")
        self.assertEqual(sanitize_name("小雨"), "小雨")
        # 标签路径不弃用代词（`TA：…` 是归属行的兜底名），但仍然清洗字符
        self.assertEqual(clean_label("TA"), "TA")
        self.assertNotIn("\n", clean_label("阿\n哲"))

    def test_personalize_does_not_break_other_words(self):
        from humanoid.prompt_builder import PromptBuilder

        got = PromptBuilder._personalize("其他人都说她温柔", "小雨", "阿哲")
        self.assertIn("其他", got)
        self.assertNotIn("其小雨", got)
        self.assertEqual(
            PromptBuilder._personalize("她今天有点累", "小雨", "阿哲"), "小雨今天有点累"
        )


class WordingGuardsTest(unittest.TestCase):
    def test_pick_treats_a_bare_string_as_one_option(self):
        from humanoid.wording import pick

        self.assertEqual(pick("k", ["seed"], "你好世界"), "你好世界")

    def test_band_index_counts_up_to_the_top_band(self):
        from humanoid.wording import FEELING_WORDS, band_index

        ladder = FEELING_WORDS["sleepy"]
        top = max(floor for floor, _ in ladder)
        self.assertEqual(band_index(top, ladder), len(ladder) - 1, "顶档要拿到最高名次")
        self.assertEqual(band_index(72.0, ladder), band_index(74.9, ladder))
        self.assertNotEqual(band_index(72.0, ladder), band_index(88.0, ladder))


class CcbGenderDayAndBandsTest(unittest.TestCase):
    def test_other_is_not_a_male_signal(self):
        from humanoid import ccb

        self.assertTrue(ccb.looks_female("其他人都很温柔"), "「其他」里的他不是性别信号")
        self.assertTrue(ccb.looks_female("你是小满，24岁。她说话很短。"))
        self.assertFalse(ccb.looks_female("你是阿明。他说话很直接。"))
        self.assertTrue(ccb.looks_female(""))

    def test_gender_marker_past_the_old_400_char_cut_still_counts(self):
        from humanoid import ccb

        persona = "他" + "水" * 500 + "她是一个女孩，说话很轻"
        self.assertTrue(ccb.looks_female(persona), "女性身份写在第 500 字也要算")

    def test_day_index_turns_over_at_local_midnight(self):
        import time as _time
        from datetime import time as dtime

        from humanoid import ccb

        midnight = datetime.combine(datetime.fromtimestamp(_time.time()).date(), dtime.min)
        stamp = midnight.timestamp()
        self.assertEqual(ccb._day_index(stamp), ccb._day_index(stamp + 3600.0))
        self.assertEqual(
            ccb._day_index(stamp) - ccb._day_index(stamp - 1.0), 1, "跨过本地零点就该正好 +1"
        )

    def test_band_scores_are_derived_and_monotonic(self):
        from humanoid import ccb

        band_list = ccb.bands(ccb.CCB_LIBIDO_MIN)
        scores = [score for _floor, score, _words in band_list]
        self.assertEqual(scores, sorted(scores), "强度必须随档位单调")
        self.assertEqual(scores[-1], 1.0, "顶档拿满分")
        self.assertEqual(len(band_list), len(ccb._BAND_WORD_TEXTS))