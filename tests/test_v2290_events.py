"""v2.29.0：日程能被真实事件改写——别人说的话进得了她的日子。

## 为什么

原来的 ``note_interruption`` 是个计数器：只回答「刚才有没有人来过」，
不回答「她经历了什么」。于是日程只能被身体读数（饿/困）和人设推动，
用户夸她一句、骂她一句，日程照走——她正做着的事不会因为一句话停下来。
那不是一个人的一天。

## 修法

- ``schedule.note_event(kind=...)``：把别人刚说的话的**性质**递进日程层。
- 极性不在日程层判：mood 层已有本地词典 + 模型融合的结论（账本），
  重复判一遍就会出现「两个情绪层各说各话」。
- ``core_instance._note_schedule_event()``：只做转交，不判定。

## 本文件盯的四条

1. 极性只有一个来源（mood 的 ``local_delta``）；
2. 中性的话不进日程（问句/陈述/客套都不该让她重排）；
3. 群消息也记——以前 ``is_group=True`` 直接挡掉；
4. 一句话不会让她重排一整天（保鲜期 + 冷却）。
"""

from __future__ import annotations

import unittest

from humanoid.config import HumanoidConfig
from humanoid.services.mood import local_delta
from humanoid.services.schedule import (
    EVENT_COOLDOWN_SECONDS,
    EVENT_TTL_SECONDS,
    ScheduleService,
    event_phrase,
)

from .fakes import RecordingLogger


class _Scope:
    """只够 ScheduleService 用的最小 scope。"""

    def __init__(self):
        self.self_state: dict = {}
        self.users: dict = {}

    def get_self(self, key, default=None):
        return self.self_state.get(key, default)

    def set_self(self, key, value):
        self.self_state[key] = value

    def update_self(self, **kw):
        self.self_state.update(kw)

    def mark_dirty(self):
        pass

    def get_user(self, uid, key, default=None):
        return self.users.setdefault(str(uid), {}).get(key, default)

    def peek_user(self, uid, key, default=None):
        entry = self.users.get(str(uid))
        return entry.get(key, default) if isinstance(entry, dict) else default

    def all_user_ids(self):
        return list(self.users.keys())


class _Clock:
    """测试用的可控时钟。"""

    def __init__(self, t: float = 1_000_000.0):
        self._t = float(t)

    def now(self):
        import datetime
        import zoneinfo
        return datetime.datetime.fromtimestamp(self._t, zoneinfo.ZoneInfo("Asia/Shanghai"))

    def today_str(self) -> str:
        return self.now().strftime("%Y-%m-%d")

    def weekday(self) -> str:
        return "周六"

    def advance(self, seconds: float) -> None:
        self._t += float(seconds)


def make_service(**conf) -> ScheduleService:
    """造一个只够测事件机制的 ScheduleService。"""
    cfg = HumanoidConfig.from_raw({"timezone_city": "北京", **conf})
    clock = _Clock()
    svc = ScheduleService(
        scope=_Scope(),
        config_provider=lambda: cfg,
        clock=clock,
        logger=RecordingLogger(),
        monotonic=lambda: clock._t,
        random_source=lambda: 0.5,
        persona_provider=None,
        body_provider=lambda: {},
        world_provider=lambda: {},
    )
    return svc


class EventPhraseTest(unittest.TestCase):
    def test_known_kinds_translate(self):
        for kind in ("很上心", "温和", "有点冷", "很敷衍", "一直没被理"):
            self.assertTrue(event_phrase(kind, ["seed"]), msg=f"{kind} 没翻出话来")

    def test_unknown_kind_returns_empty(self):
        self.assertEqual(event_phrase("不认识的词", ["seed"]), "")
        self.assertEqual(event_phrase("", ["seed"]), "")

    def test_stable_within_a_day(self):
        """同一天同一件事抽到同一句——否则模型会以为是两件事。"""
        self.assertEqual(
            event_phrase("很敷衍", ["role", "2026-10-24"]),
            event_phrase("很敷衍", ["role", "2026-10-24"]),
        )

    def test_varies_across_days(self):
        seen = {event_phrase("很敷衍", [f"2026-10-{i:02d}"]) for i in range(1, 20)}
        self.assertGreater(len(seen), 1)


class NoteEventTest(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        self.clock = self.svc._clock

    def test_records_event(self):
        self.assertTrue(self.svc.note_event(kind="很敷衍", seed=["r", "u"]))
        self.assertTrue(self.svc._fresh_event(clear=False))

    def test_unknown_kind_ignored(self):
        self.assertFalse(self.svc.note_event(kind="乱写的", seed=["r"]))

    def test_empty_kind_ignored(self):
        self.assertFalse(self.svc.note_event(kind="", seed=["r"]))

    def test_group_events_recorded(self):
        """群里发生的事她也该知道——以前 is_group=True 直接 return。"""
        self.assertTrue(self.svc.note_event(kind="温和", seed=["r", "u"], is_group=True))

    def test_cooldown_blocks_second(self):
        self.assertTrue(self.svc.note_event(kind="很敷衍", seed=["r"]))
        self.clock.advance(EVENT_COOLDOWN_SECONDS - 1)
        self.assertFalse(self.svc.note_event(kind="很敷衍", seed=["r"]))

    def test_after_cooldown_records_again(self):
        self.assertTrue(self.svc.note_event(kind="很敷衍", seed=["r"]))
        self.clock.advance(EVENT_COOLDOWN_SECONDS + 1)
        self.assertTrue(self.svc.note_event(kind="很敷衍", seed=["r"]))

    def test_expiry(self):
        self.svc.note_event(kind="很敷衍", seed=["r"])
        self.clock.advance(EVENT_TTL_SECONDS + 1)
        self.assertEqual(self.svc._fresh_event(clear=False), "")
        self.assertEqual(self.svc._fresh_event(clear=True), "")

    def test_consume_is_one_shot(self):
        self.svc.note_event(kind="很敷衍", seed=["r"])
        self.assertTrue(self.svc._consume_event())
        self.assertEqual(self.svc._consume_event(), "")
        self.assertEqual(self.svc._fresh_event(clear=False), "")

    def test_restore_after_gate_keeps_phrase(self):
        """被闸门挡回时原样还回——否则反复被挡就能把保鲜期一次次续满。"""
        self.svc.note_event(kind="很敷衍", seed=["r"])
        phrase = self.svc._consume_event()
        # 模拟 _generate_segment 里 OUTCOME_GATED 的归还路径
        self.svc._event_phrase = phrase
        self.svc._event_at = self.svc._monotonic() - 1.0
        self.assertEqual(self.svc._fresh_event(clear=False), phrase)

    def test_refresh_due_triggered_by_event(self):
        """事件够重时，该有一次决定下一段的机会。"""
        self.svc.note_event(kind="很敷衍", seed=["r"])
        self.assertTrue(self.svc.refresh_due())


class LocalDeltaKindTest(unittest.TestCase):
    """极性只有一个来源：mood 层的 local_delta。"""

    def test_praise_is_positive(self):
        self.assertGreater(local_delta("你今天真好看，太喜欢你了").affection, 0)

    def test_insult_is_negative(self):
        self.assertLess(local_delta("你就是个废物，看着就恶心").affection, 0)

    def test_severe_insult_is_very_negative(self):
        self.assertLess(local_delta("去死吧傻逼").affection, -1.5)

    def test_question_is_neutral(self):
        self.assertEqual(local_delta("你今天吃饭了吗").affection, 0.0)

    def test_self_state_is_neutral(self):
        """对方说自己难受，不该记成「她被人骂了」——那是冤枉人。"""
        self.assertEqual(local_delta("我今天好累好烦").affection, 0.0)

    def test_cold_reply_is_negative(self):
        """敷衍也是负向：能让她掉分的是冷她的人，不是只有骂她的人。"""
        self.assertLess(local_delta("随你吧，懒得理你").affection, 0)


if __name__ == "__main__":
    unittest.main()
