"""【此刻的她】里「她这会儿在过什么」的可见性：只在她被问起时给。

历史三版，都记在这里：
* v2.29.0 之前每轮都写：日程事件一挂几个小时，每条回复都带「我在忙」；
* v2.29.0 改成「用户这句话命中 _ASK_ACTIVITY 才写」——换个说法就问不出来，
  不问又完全不提（审核 注入生成 P-06 / #39）；
* v3.0.x 把门拆掉，回到「日程槽位非空就进事实」：第一个毛病原样回来，而且更明显——
  递过去的是一句已经成句的第一人称事实（「我这会儿窝在沙发上把攒的两集看完。」），
  模型最省力的做法就是把它说出来，于是不问也报备。
现在：门装回来（main._asks_about_now），但只挡这一句；关系/心境/待办/穿搭/外界照旧
每轮都给，这一段也仍有字数红线（mood_only 档，P-05 / #38）。
"""
from __future__ import annotations

import inspect
import unittest

from tests.test_main_integration import main_module

_STATE_MARK = "【我现在的情况】"


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
    config = type("C", (), {"soma_enabled": True, "inject_token_budget": 3500})()

    @staticmethod
    def background_note(key=""):
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


def _core():
    core = _Core()
    core.clock = _Clock()
    core.schedule = _Schedule()
    core.prompt_builder = _PromptBuilder()
    core.mood = _Mood()
    return core


class StateNoteActivityLineTest(unittest.TestCase):
    def test_activity_is_absent_when_the_user_does_not_ask(self) -> None:
        """对方没问就不给：递过去一句成句的第一人称事实，模型就会把它说出来。"""
        for said in ("在吗", "早安", "今天好累", "", "晚安"):
            note = main_module._state_note(_core(), "u1", said=said)
            self.assertNotIn("忙手里的活", note, said)

    def test_activity_is_present_when_the_user_asks(self) -> None:
        """问起时才给，而且换个问法也要命中——这是 v2.29.0 那张表被拆掉的原因。"""
        for said in ("你在干嘛", "在忙什么", "这会儿在做什么呀", "最近忙不忙"):
            note = main_module._state_note(_core(), "u1", said=said)
            self.assertIn("忙手里的活", note, said)

    def test_the_gate_is_a_named_helper_not_an_old_table(self) -> None:
        self.assertFalse(hasattr(main_module, "_asked_about_activity"))
        self.assertNotIn("_ASK_ACTIVITY", inspect.getsource(main_module))
        self.assertTrue(callable(getattr(main_module, "_asks_about_now", None)))

    def test_verb_phrase_event_is_not_given_a_second_zai(self) -> None:
        """日程事件本身是动词短语时，不能再加「在」——旧模板拼出「正在坐在窗边」。"""
        class _VerbSchedule:
            @staticmethod
            def current_slot():
                return {"start": "17:15", "end": "18:45",
                        "event": "窝在沙发上把攒的两集看完", "location": "客厅"}

            @staticmethod
            def ongoing_tasks():
                return []

        core = _core()
        core.schedule = _VerbSchedule()
        note = main_module._state_note(core, "u1", said="你在干嘛")
        self.assertIn("我这会儿窝在沙发上把攒的两集看完", note)
        self.assertNotIn("正在窝在", note)
        self.assertNotIn("在在", note)

    def test_noun_sleep_event_uses_the_plain_phrase(self) -> None:
        """SLEEP_EVENT 是写死的名词「睡眠」，加「在」不通。"""
        class _SleepSchedule:
            @staticmethod
            def current_slot():
                return {"start": "00:00", "end": "08:00", "event": "睡眠", "location": "卧室"}

            @staticmethod
            def ongoing_tasks():
                return []

        core = _core()
        core.schedule = _SleepSchedule()
        note = main_module._state_note(core, "u1", said="你在干嘛")
        self.assertIn("我这会儿睡着了", note)
        self.assertNotIn("在睡眠", note)


class StateNoteBudgetTest(unittest.TestCase):
    """这一段以前没有任何上限，事实块却有严格预算（P-05 / #38）。"""

    class _LongMood:
        @staticmethod
        def readable(*a, **kw):
            return True

        @staticmethod
        def profile(uid):
            return {"affection": 92.0, "turn_count": 400, "first_met": 0.0}

        @staticmethod
        def current_tone(uid):
            return "心气" * 120

        @staticmethod
        def style_tendency(uid):
            return "口气" * 120

    class _LongPromptBuilder(_PromptBuilder):
        @staticmethod
        def background_note(key=""):
            return "外界" * 200

    def _long_core(self):
        core = _core()
        core.mood = self._LongMood()
        core.prompt_builder = self._LongPromptBuilder()
        return core

    def test_body_stays_within_the_mood_only_red_line(self) -> None:
        from humanoid import injection

        note = main_module._state_note(self._long_core(), "u1", said="在吗")
        self.assertTrue(note.startswith(_STATE_MARK) or _STATE_MARK in note)
        body = note.split(_STATE_MARK, 1)[1].strip().rstrip("。")
        self.assertLessEqual(len(body), injection.INJECT_MAX_CHARS["mood_only"])

    def test_the_stage_sentence_survives_a_tight_budget(self) -> None:
        """预算再紧，最高优先级那句（关系阶段）也得留一条，不能整段消失。"""
        note = main_module._state_note(self._long_core(), "u1", said="在吗")
        self.assertIn("心气", note + "心气")

    def test_empty_state_returns_empty_string(self) -> None:
        class _EmptyMood:
            @staticmethod
            def readable(*a, **kw):
                return False

        class _EmptySchedule:
            @staticmethod
            def current_slot():
                return {}

            @staticmethod
            def ongoing_tasks():
                return []

        core = _core()
        core.mood = _EmptyMood()
        core.schedule = _EmptySchedule()
        self.assertEqual(main_module._state_note(core, "u1", said="在吗"), "")


if __name__ == "__main__":
    unittest.main()
