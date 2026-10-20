"""v2.27.43：关系档位不该只看好感；昵称别念全名；天气别每轮念。

三条都来自线上实测，不是猜的：见各测试的注释。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from humanoid import relstage
from humanoid.prompt_builder import PromptBuilder, short_name
from humanoid.services.mood import MoodService


class FamiliarityTest(unittest.TestCase):
    """聊过 2000 轮的人，不该被写成「还在互相认识」。

    线上实测：2485 个用户里 98.4% 落在「还在熟悉」，其中 179 人聊过 ≥30 轮，
    最多的一位聊了 **2088 轮**、好感 39.9 —— 注入里永远写「她和TA还在互相认识」。
    """

    def test_both_turns_and_days_are_required(self) -> None:
        # 一口气刷 300 条但只认识一天：不算处久了
        self.assertEqual(relstage.familiarity_floor(turns=300, days=0.5), 0)
        # 认识很久但没说过几句话：也不算
        self.assertEqual(relstage.familiarity_floor(turns=3, days=400), 0)
        # 两个都够
        self.assertEqual(relstage.familiarity_floor(turns=60, days=7), 1)
        self.assertEqual(relstage.familiarity_floor(turns=400, days=30), 2)

    def test_familiarity_never_reaches_the_top_band(self) -> None:
        """「很近」是亲密，不是资历——相处再久也不用好感之外的证据去顶。"""
        self.assertLessEqual(relstage.familiarity_floor(turns=999999, days=9999), 2)

    def test_bad_input_is_safe(self) -> None:
        for turns, days in ((-5, 10), ("x", 10), (100, float("nan")), (None, None)):
            self.assertEqual(relstage.familiarity_floor(turns=turns, days=days), 0)

    def test_a_long_relationship_is_not_strangers(self) -> None:
        _i, _short, line = relstage.stage_of(39.9, turns=2088, days=120)
        self.assertNotIn("还在互相认识", line)
        self.assertIn("不用绕弯子", line)

    def test_old_call_site_is_unchanged(self) -> None:
        """不传相处证据时，结果与改前逐字一致（旧调用方不受影响）。"""
        self.assertEqual(
            relstage.line_for(40.0),
            "她和TA还在互相认识，没到能说私事的地步",
        )
        self.assertEqual(relstage.stage_of(90.0)[0], 3)

    def test_affection_still_wins(self) -> None:
        self.assertEqual(relstage.stage_of(90.0, turns=0, days=0)[0], 3)


class RatchetTest(unittest.TestCase):
    """关系的下限要能长起来，否则好感永远被衰减拉回初始值。"""

    def _svc(self) -> MoodService:
        return MoodService.__new__(MoodService)   # 只测这一个纯函数

    def test_base_creeps_up_only_with_enough_history(self) -> None:
        svc = self._svc()
        fresh = {"turn_count": 3}
        self.assertFalse(svc._ratchet_base(fresh, 35.0, 45.0, 1_000_000.0))
        self.assertEqual(fresh.get("base_affection"), None, "刚认识的人不该被抬基线")

    def test_base_creeps_up_when_affection_is_clearly_higher(self) -> None:
        svc = self._svc()
        rec = {"turn_count": 200}
        self.assertTrue(svc._ratchet_base(rec, 35.0, 45.0, 1_000_000.0))
        self.assertGreater(rec["base_affection"], 35.0)
        self.assertEqual(rec["base_ratchet_at"], 1_000_000.0)

    def test_ratchet_is_rate_limited(self) -> None:
        svc = self._svc()
        rec = {"turn_count": 200, "base_affection": 36.0, "base_ratchet_at": 1_000_000.0}
        # 刚挪过，12 小时内不再动
        self.assertFalse(svc._ratchet_base(rec, 36.0, 50.0, 1_000_000.0 + 60))
        self.assertTrue(
            svc._ratchet_base(rec, 36.0, 50.0, 1_000_000.0 + 13 * 3600)
        )

    def test_base_is_capped_and_stays_below_affection(self) -> None:
        svc = self._svc()
        rec = {"turn_count": 9999}
        svc._ratchet_base(rec, 64.8, 80.0, 1_000_000.0)
        self.assertLessEqual(rec["base_affection"], 65.0)
        self.assertLess(rec["base_affection"], 80.0)


class ShortNameTest(unittest.TestCase):
    """线上真实消息：`(休息) 忙着吃饭，爱筝挽起·冲击波冠军，想你❤️`。"""

    def test_short_names_are_untouched(self) -> None:
        for name in ("小明", "阿澈", "游鱼", "无名又无言"):
            self.assertEqual(short_name(name), name)

    def test_long_chinese_name_uses_the_first_segment(self) -> None:
        self.assertEqual(short_name("爱筝挽起·冲击波冠军"), "爱筝挽起")

    def test_long_chinese_name_without_separator_is_cut(self) -> None:
        self.assertEqual(short_name("超级无敌大风车"), "超级无敌")

    def test_latin_name_is_never_cut(self) -> None:
        for name in ("Regnator", "hanhandouya", "Luture"):
            self.assertEqual(short_name(name), name)

    def test_empty_is_empty(self) -> None:
        self.assertEqual(short_name(""), "")
        self.assertEqual(short_name(None), "")


class WeatherRepeatTest(unittest.TestCase):
    """天气是「永远有值、永远具体」的那一条，每轮都写就会被念成开场白。"""

    def _builder(self, now):
        b = PromptBuilder.__new__(PromptBuilder)
        b._core = SimpleNamespace(now_epoch=lambda: now)
        b._weather_seen = None
        return b

    def test_first_time_is_written(self) -> None:
        self.assertTrue(self._builder(1000.0)._weather_worth_saying("阴，气温 8℃"))

    def test_unchanged_weather_is_skipped_within_four_hours(self) -> None:
        b = self._builder(1000.0)
        self.assertTrue(b._weather_worth_saying("阴，气温 8℃"))
        b._core = SimpleNamespace(now_epoch=lambda: 1000.0 + 3600)
        self.assertFalse(b._weather_worth_saying("阴，气温 8℃"))

    def test_changed_weather_is_always_written(self) -> None:
        b = self._builder(1000.0)
        b._weather_worth_saying("阴，气温 8℃")
        b._core = SimpleNamespace(now_epoch=lambda: 1000.0 + 60)
        self.assertTrue(b._weather_worth_saying("小雨，气温 6℃"))

    def test_unchanged_weather_comes_back_after_four_hours(self) -> None:
        b = self._builder(1000.0)
        b._weather_worth_saying("阴，气温 8℃")
        b._core = SimpleNamespace(now_epoch=lambda: 1000.0 + 4 * 3600 + 1)
        self.assertTrue(b._weather_worth_saying("阴，气温 8℃"))


if __name__ == "__main__":
    unittest.main()
