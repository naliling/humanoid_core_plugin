"""v3.1.52：注入源去重——在塞进提示词之前识别她刚说过的话。

真机根因：她一天里反复说「鼻尖冻得发麻」「外头正落着四度寒雨」，不是模型复读，
是注入源每轮把同一句重新塞进去——感官抽签种子含「今天+时段」每轮命中同一句，
体感句只看身体状态、天气门只看时间/指纹，都不看她刚没刚说过。上一版还把出站
删句的闸放松了，等于源头一直产复读、兜底也松了。这一版在源头去重。

锁三件事：
* 归一化：℃/°C→度、去句首主语，「她鼻尖冻得发麻」=「鼻尖冻得发麻」；
* _already_said：刚说过的天气/体感/感官被识别，无关句不误伤；
* 取最近窗口：只读 spokes、坏账本 fail-open。
"""
from __future__ import annotations

import unittest

from humanoid.prompt_builder import (
    _already_said,
    _norm_spoken,
    _recent_spoken,
)


class NormTest(unittest.TestCase):
    def test_temperature_units_are_unified(self):
        self.assertEqual(_norm_spoken("外头小雨 9℃"), _norm_spoken("外头小雨9度"))

    def test_leading_subject_is_stripped(self):
        self.assertEqual(_norm_spoken("她鼻尖冻得发麻"), _norm_spoken("鼻尖冻得发麻"))

    def test_punctuation_is_stripped(self):
        self.assertEqual(
            _norm_spoken("外头，正落着四度寒雨呀。"),
            _norm_spoken("外头正落着四度寒雨呀"),
        )


class AlreadySaidTest(unittest.TestCase):
    def _spoken(self, *lines: str):
        return [_norm_spoken(line) for line in lines]

    def test_weather_copied_with_different_unit_is_caught(self):
        self.assertTrue(_already_said("外头小雨 9℃", self._spoken("外头小雨9度，有点冷呢")))

    def test_weather_progression_is_caught(self):
        self.assertTrue(
            _already_said("外头正落着四度寒雨", self._spoken("外头正落着四度寒雨呀"))
        )

    def test_sensory_detail_is_caught_despite_subject(self):
        self.assertTrue(
            _already_said("她鼻尖冻得发麻", self._spoken("鼻尖冻得发麻，指尖也凉透了"))
        )

    def test_feeling_is_caught(self):
        self.assertTrue(
            _already_said("她昨晚没睡好", self._spoken("昨晚没睡好，今天精神差点"))
        )

    def test_unrelated_line_is_not_caught(self):
        self.assertFalse(_already_said("窗外的雪下大了", self._spoken("我在看书，别吵")))

    def test_short_filler_is_never_treated_as_repeat(self):
        self.assertFalse(_already_said("嗯", self._spoken("嗯")))

    def test_empty_spoken_never_matches(self):
        self.assertFalse(_already_said("她有点累", []))


class _FakeMood:
    def __init__(self, rows):
        self._rows = rows

    def spoke(self, user_id):
        return self._rows


class _FakeCore:
    def __init__(self, rows):
        self.mood = _FakeMood(rows)


class RecentSpokenTest(unittest.TestCase):
    def test_only_recent_short_rows_are_returned(self):
        core = _FakeCore([
            {"text": "这是一句够长的旧话"},
            {"text": "鼻尖冻得发麻，指尖也凉"},
            {"text": "嗯"},  # 太短，滤掉
            {"text": "外头正落着四度寒雨"},
        ])
        out = _recent_spoken(core, "u1")
        self.assertIn("鼻尖冻得发麻指尖也凉", out)
        self.assertIn("外头正落着四度寒雨", out)
        self.assertNotIn("嗯", out)

    def test_bad_ledger_fails_open(self):
        class Broken:
            def spoke(self, user_id):
                raise RuntimeError("boom")

        core = type("C", (), {"mood": Broken()})()
        self.assertEqual(_recent_spoken(core, "u1"), [])


if __name__ == "__main__":
    unittest.main()
