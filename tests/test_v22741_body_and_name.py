"""两处「状态说法」的修正：

1. 参照名称被存成整条指令（`拟人 参照名称 娜莉灵`），每轮注入都写成
   「只有拟人 参照名称 娜莉灵和TA」；
2. 睡眠压力顶格以后措辞永远是「困得不行」，下午五点和凌晨两点说同一句。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from humanoid.core_instance import strip_command_words
from humanoid.services.soma import SomaService


class StripCommandWordsTest(unittest.TestCase):
    def test_full_command_becomes_just_the_name(self) -> None:
        self.assertEqual(strip_command_words("拟人 参照名称 娜莉灵"), "娜莉灵")

    def test_plain_name_is_untouched(self) -> None:
        self.assertEqual(strip_command_words("娜莉灵"), "娜莉灵")

    def test_repeated_command_words(self) -> None:
        self.assertEqual(strip_command_words("拟人 拟人 参照名称 娜莉灵"), "娜莉灵")

    def test_only_command_words_is_empty(self) -> None:
        self.assertEqual(strip_command_words("参照名称"), "")
        self.assertEqual(strip_command_words("   "), "")

    def test_word_inside_the_name_is_kept(self) -> None:
        # 只在开头剥：名字中间出现同名词是用户自己的选择
        self.assertEqual(strip_command_words("小拟人灵"), "小拟人灵")


class _Clock:
    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def now(self) -> datetime:
        return self._moment


class DrowsyPeakTest(unittest.TestCase):
    """`_drowsy_peak_now`：什么时候才配说「困得不行」。"""

    class _Scope:
        def __init__(self, data: dict) -> None:
            self._data = data
            self.role_id = "test-role"

        def get_self(self, key, default=None):
            return self._data if key == "soma" else default

        def set_self(self, key, value):
            self._data.clear()
            self._data.update(value or {})

    def _soma(self, hour: int, awake_hours: float) -> SomaService:
        moment = datetime(2026, 10, 18, hour, 0, tzinfo=timezone(timedelta(hours=2)))
        soma = SomaService.__new__(SomaService)  # 只测这一个判断，不建整套状态
        soma._clock = _Clock(moment)
        soma._time = lambda: moment.timestamp()
        soma._scope = self._Scope(
            {"last_wake_at": moment.timestamp() - awake_hours * 3600.0}
        )
        return soma

    def test_afternoon_is_not_the_peak(self) -> None:
        self.assertFalse(self._soma(17, 13.0)._drowsy_peak_now())

    def test_late_evening_is_the_peak(self) -> None:
        self.assertTrue(self._soma(22, 13.0)._drowsy_peak_now())

    def test_small_hours_is_the_peak(self) -> None:
        self.assertTrue(self._soma(3, 13.0)._drowsy_peak_now())

    def test_sixteen_hours_awake_is_the_peak(self) -> None:
        self.assertTrue(self._soma(17, 16.5)._drowsy_peak_now())


if __name__ == "__main__":
    unittest.main()
