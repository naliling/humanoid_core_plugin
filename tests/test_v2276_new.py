"""v2.27.6 新增项回归：自定义节日每年重复、着凉小事件、好感度排行榜。

三件事各自的契约：

- 节日：`MM-DD` 每年这一天都算，`YYYY-MM-DD` 只在当年那天命中；
- 着凉：受冻/缺觉后小概率发生，进体感，自己会好；只由客观量起头；
- 排行榜：按当前好感排序、能带上称呼、没档案的人不出现。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from humanoid.config import HumanoidConfig
from humanoid.data.holidays import resolve_holiday
from humanoid.engine import HumanoidEngine
from humanoid.services.soma import SomaService

from .fakes import FrozenClock, ScopeStore


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
