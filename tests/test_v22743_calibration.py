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
    """天气是「永远有值、永远具体」的那一条，每轮都写就会被念成开场白。

    v3.1.38：判据从「变了 + 4 小时」收紧成「**变天**（雨雪风雾或真的冷热）+ 8 小时」，
    温度漂移不再算变天。所以这里的样例天气用「小雨」而不是「阴」——阴/多云现在
    连背景句都不进（见 `WeatherRepeatTest` 之外的 `test_notable_weather`）。
    """

    def _builder(self, now):
        b = PromptBuilder.__new__(PromptBuilder)
        b._core = SimpleNamespace(now_epoch=lambda: now)
        b._weather_gate = None
        return b

    def test_first_time_is_written(self) -> None:
        self.assertTrue(self._builder(1000.0)._weather_worth_saying("小雨，气温 8℃"))

    def test_unchanged_weather_is_skipped_within_the_window(self) -> None:
        from humanoid.services.weather import WEATHER_REPEAT_SECONDS

        b = self._builder(1000.0)
        self.assertTrue(b._weather_worth_saying("小雨，气温 8℃"))
        b._core = SimpleNamespace(now_epoch=lambda: 1000.0 + WEATHER_REPEAT_SECONDS - 60)
        self.assertFalse(b._weather_worth_saying("小雨，气温 8℃"))

    def test_changed_weather_is_always_written(self) -> None:
        b = self._builder(1000.0)
        b._weather_worth_saying("小雨，气温 8℃")
        b._core = SimpleNamespace(now_epoch=lambda: 1000.0 + 60)
        self.assertTrue(b._weather_worth_saying("中雨，气温 6℃"))

    def test_unchanged_weather_comes_back_after_the_window(self) -> None:
        from humanoid.services.weather import WEATHER_REPEAT_SECONDS

        b = self._builder(1000.0)
        b._weather_worth_saying("小雨，气温 8℃")
        b._core = SimpleNamespace(now_epoch=lambda: 1000.0 + WEATHER_REPEAT_SECONDS + 1)
        self.assertTrue(b._weather_worth_saying("小雨，气温 8℃"))

    def test_temperature_drift_is_not_a_change(self) -> None:
        """v3.1.38：9℃ → 10℃ 不算变天。

        温度每变一度指纹就变，于是「小雨 9℃」→「小雨 10℃」被当成变天，一天能说
        七八次——用户原话「角色会很喜欢说天气」。
        """
        b = self._builder(1000.0)
        self.assertTrue(b._weather_worth_saying("小雨，气温 9℃"))
        b._core = SimpleNamespace(now_epoch=lambda: 1000.0 + 60)
        self.assertFalse(b._weather_worth_saying("小雨，气温 10℃"))

    def test_another_session_still_gets_the_weather(self) -> None:
        """审核 P-08 #41：去重状态不能是角色级单值。

        旧写法是 PromptBuilder 上一个 (文本, 时刻)，整个角色共用一份——A 会话把
        「阴，气温 8℃」讲过后，B 会话 4 小时内再也拿不到天气句（各看各的窗外，
        天气对谁都是同一句，但「刚说过」是对**每个会话**分别成立的）。
        """
        b = self._builder(1000.0)
        self.assertTrue(b._weather_worth_saying("小雨，气温 8℃", key="umo:a"))
        self.assertFalse(b._weather_worth_saying("小雨，气温 8℃", key="umo:a"))
        self.assertTrue(b._weather_worth_saying("小雨，气温 8℃", key="umo:b"))

    def test_empty_key_keeps_the_old_single_bucket(self) -> None:
        """key 为空时退化成旧行为：没接线也不炸、也不会突然每轮复读。"""
        b = self._builder(1000.0)
        self.assertTrue(b._weather_worth_saying("小雨，气温 8℃"))
        self.assertFalse(b._weather_worth_saying("小雨，气温 8℃"))

    def test_city_is_part_of_the_fingerprint(self) -> None:
        """换了城市就是天气变了，该说——指纹里带城市。"""
        b = self._builder(1000.0)
        self.assertTrue(b._weather_worth_saying("小雨，气温 8℃", key="umo:a", city="河源"))
        self.assertTrue(b._weather_worth_saying("小雨，气温 8℃", key="umo:a", city="北京"))

    def test_gate_buckets_are_bounded(self) -> None:
        """桶上限 64、淘汰最旧：只放内存，但不能无界增长。"""
        from humanoid.services.weather import WEATHER_SEEN_LIMIT, WeatherSayGate

        gate = WeatherSayGate()
        for i in range(WEATHER_SEEN_LIMIT + 10):
            gate.worth_saying("小雨，气温 8℃", key="umo:%d" % i, now=1000.0 + i)
        self.assertEqual(len(gate._seen), WEATHER_SEEN_LIMIT)
        # 最旧的桶被淘汰了：它再问一次算「第一次」，该说。
        self.assertTrue(gate.worth_saying("小雨，气温 8℃", key="umo:0", now=2000.0))

    def _full_builder(self, now):
        import datetime

        b = PromptBuilder.__new__(PromptBuilder)
        clock = SimpleNamespace(
            now=lambda: datetime.datetime(2024, 3, 1, 15, 20, 33),
            holiday=lambda *a, **k: "",
            weekday=lambda: "五",
            city="河源",
        )
        b._core = SimpleNamespace(
            config=SimpleNamespace(show_city_time_in_low_intrusion=True),
            clock=clock,
            now_epoch=lambda: now,
            snapshot=lambda refresh=False: {
                "today": "3月1日",
                "city": "河源",
                "weather": {"env": "天气：小雨，气温 8℃"},
            },
        )
        b._weather_gate = None
        return b

    def test_background_note_is_per_session(self) -> None:
        """整条链路：A 会话讲过天气，B 会话的【此刻的她】里照样有（#41）。"""
        b = self._full_builder(1000.0)
        self.assertIn("外头小雨，气温 8℃", b.background_note(key="umo:a"))
        self.assertNotIn("外头小雨，气温 8℃", b.background_note(key="umo:a"))
        self.assertIn("外头小雨，气温 8℃", b.background_note(key="umo:b"))

    def test_plain_weather_never_reaches_the_background(self) -> None:
        """v3.1.38：多云/晴这类天气不进她的背景，无论隔多久。

        这是用户原话「角色会很喜欢说天气」的直接治法：不是让模型别说，而是不给它。
        """
        b = self._full_builder(1000.0)
        b._core = SimpleNamespace(
            config=SimpleNamespace(show_city_time_in_low_intrusion=True),
            clock=b._core.clock,
            now_epoch=lambda: 1000.0 + 48 * 3600,
            snapshot=lambda refresh=False: {
                "today": "3月1日",
                "city": "河源",
                "weather": {"env": "天气：多云，气温 22℃"},
            },
        )
        self.assertNotIn("外头", b.background_note(key="umo:c"))


class NotableWeatherTest(unittest.TestCase):
    """「值得开口的天气」判据：雨雪风雾、或真的冷热。"""

    def test_rain_and_snow_and_wind_are_notable(self) -> None:
        from humanoid.services.weather import notable_weather

        for text in ("小雨，气温 9℃", "大雪，气温 -2℃", "雷阵雨", "大风", "雾", "霾", "沙尘"):
            self.assertTrue(notable_weather(text), text)

    def test_plain_weather_is_not_notable(self) -> None:
        from humanoid.services.weather import notable_weather

        for text in ("多云，气温 22℃", "晴，气温 25℃", "阴，气温 18℃", ""):
            self.assertFalse(notable_weather(text), text)

    def test_extreme_temperature_is_notable(self) -> None:
        from humanoid.services.weather import notable_weather

        self.assertTrue(notable_weather("晴，气温 3℃"))
        self.assertTrue(notable_weather("晴，气温 36℃"))
        self.assertFalse(notable_weather("晴，气温 12℃"))


if __name__ == "__main__":
    unittest.main()
