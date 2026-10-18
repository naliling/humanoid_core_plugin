"""「她这会儿正在X」只在被问到的时候出现。

线上原话：「忙就忙呗，人家忙着关我啥事」。这句话原来每轮都进注入，而日程事件
（「忙手里的活」）一挂几个小时，于是每条回复都带一句「我在忙」「正在热餐」。
"""
from __future__ import annotations

import unittest

from tests.test_main_integration import main_module

_asked = main_module._asked_about_activity


class AskedAboutActivityTest(unittest.TestCase):
    def test_questions_about_what_she_is_doing(self) -> None:
        for text in ("你在干嘛", "在忙吗", "忙啥呢", "你干什么呢", "忙不忙", "在做什么"):
            self.assertTrue(_asked(text), text)

    def test_greetings_are_not_questions_about_activity(self) -> None:
        for text in ("在吗", "早安", "我饿了", "睡了吗", "今天好累", ""):
            self.assertFalse(_asked(text), text)


class StateNoteActivityLineTest(unittest.TestCase):
    """真正调 `_state_note`，用假 Core 把日程钉成「忙手里的活」。"""

    class _Clock:
        @staticmethod
        def now():
            import datetime as _dt

            return _dt.datetime(2026, 10, 18, 17, 26)

    class _Schedule:
        @staticmethod
        def current_slot():
            return {"start": "17:15", "end": "18:45", "event": "忙手里的活", "location": "家里"}

        @staticmethod
        def ongoing_tasks():
            return []

    class _PromptBuilder:
        config = type("C", (), {"soma_enabled": True})()

        @staticmethod
        def background_note():
            return ""

    class _Mood:
        @staticmethod
        def readable(*a, **kw):
            return False

    class _Core:
        clock = None
        schedule = None
        prompt_builder = None
        mood = None

        def outfit_line(self):
            return ""

    def _core(self):
        core = self._Core()
        core.clock = self._Clock()
        core.schedule = self._Schedule()
        core.prompt_builder = self._PromptBuilder()
        core.mood = self._Mood()
        return core

    def test_activity_is_omitted_when_not_asked(self) -> None:
        note = main_module._state_note(self._core(), "u1", said="在吗")
        self.assertNotIn("忙手里的活", note)

    def test_activity_is_present_when_asked(self) -> None:
        note = main_module._state_note(self._core(), "u1", said="你在干嘛")
        self.assertIn("忙手里的活", note)


if __name__ == "__main__":
    unittest.main()
