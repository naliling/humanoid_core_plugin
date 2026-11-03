"""B5 / task-55（#66 #71 #156）的回归用例：睡着时听见的话、处境词段、事件名截断。

这三条都属「同一份事实在两处各判一次」的老毛病：
* 睡着那一段不重掷（对），于是事件与来访在保鲜期里悄悄过期（错）——现在收成
  「她睡着的时候发生过什么」，醒来那一段递进去；
* 兜底段自称「只给状态」（`status`），下游却按动作渲染，拼出「上午在有点饿了」；
* 事件名在**解析存盘**时按 24 字硬切，切在词中间，渲染侧再按标点收尾也补不回来。

用例只调公开/半公开入口，不复制实现里的字面量。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from humanoid.services.schedule import (
    ASLEEP_HEARD_MAX,
    ScheduleService,
    _at_phrase,
    _clip_event,
    day_phrases,
    period_of,
    segment_prompt,
)

from .fakes import FrozenClock, RecordingLogger, ScopeStore
from .test_schedule import FIXED_MOMENT, cfg


def _service(**overrides):
    conf = cfg(**overrides)
    tmp = Path(tempfile.mkdtemp()) / "state.json"
    store = ScopeStore(tmp)
    store.load(FIXED_MOMENT.strftime("%Y-%m-%d"), conf.cycle_length)
    return ScheduleService(
        store.scope, lambda: conf, FrozenClock(FIXED_MOMENT),
        logger=RecordingLogger(), monotonic=lambda: 10_000.0,
    )


class ClipEventTest(unittest.TestCase):
    """#156：截断必须在标点处收，不留半个词、不悬空标点。"""

    def test_short_event_is_untouched(self):
        self.assertEqual(_clip_event("去超市买菜"), "去超市买菜")

    def test_long_event_cuts_at_punctuation_not_mid_word(self):
        text = "在厨房里做红烧肉和清炒时蔬，还顺手把碗洗了"
        out = _clip_event(text)
        self.assertLessEqual(len(out), 24)
        self.assertTrue(text.startswith(out), f"截断后应是原文前缀，实际「{out}」")
        self.assertFalse(out.endswith("，"), "尾部不许留悬空标点")
        self.assertIn("时蔬", out, "有标点就应在标点处收，而不是砍掉半个词")

    def test_long_event_without_punctuation_falls_back_to_hard_cut(self):
        text = "一二三四五六七八九十一二三四五六七八九十一二三四五六七八"
        out = _clip_event(text)
        self.assertEqual(len(out), 24)
        self.assertEqual(out, text[:24])

    def test_dangling_punctuation_is_stripped(self):
        text = "啊" * 23 + "，后面还有很多字"
        out = _clip_event(text)
        self.assertEqual(out, "啊" * 23)


class StatusSlotPhraseTest(unittest.TestCase):
    """#71：兜底段的 event 是处境词，不是动作，不能补「在」。"""

    def test_status_slot_takes_no_preposition(self):
        slot = {"event": "肚子空着", "status": True}
        self.assertEqual(_at_phrase(9 * 60, "肚子空着", slot), f"{period_of(9 * 60)}肚子空着")

    def test_action_slot_still_takes_the_preposition(self):
        # 「整理房间」不以动词/介词开头，`_with_at` 该给它补一个「在」。
        slot = {"event": "整理房间", "status": False}
        self.assertEqual(_at_phrase(9 * 60, "整理房间", slot), f"{period_of(9 * 60)}在整理房间")

    def test_day_phrases_never_says_weizai_for_a_status_slot(self):
        slots = [{"start": "08:00", "end": "10:00", "event": "肚子空着",
                  "location": "厨房", "status": True}]
        out = day_phrases(slots, 11 * 60)
        self.assertIn("肚子空着", out["done"][0])
        self.assertNotIn("在肚子空着", out["done"][0])

    def test_day_phrases_never_says_haixao_for_a_status_slot(self):
        """处境词只接在时段词后面：是「下午有点饿了」，不是「下午还要有点饿了」。"""
        slots = [{"start": "13:00", "end": "14:00", "event": "有点饿了",
                  "location": "客厅", "status": True}]
        # #73：next 恒为空，改用「刚过完」的 done 分支验证同一件事。
        out = day_phrases(slots, 14 * 60 + 30)
        self.assertEqual(out["next"], [])
        self.assertEqual(out["done"][0], f"{period_of(13 * 60)}有点饿了")
        self.assertNotIn("还要", out["done"][0])
        self.assertNotIn("在有点饿", out["done"][0])


class HeardWhileAsleepTest(unittest.TestCase):
    """#66：睡着期间发生的事要留到醒来那一段，而不是在保鲜期里静默过期。"""

    def test_duplicates_are_kept_once(self):
        service = _service()
        service._heard_while_asleep_add("被人夸了")
        service._heard_while_asleep_add("被人夸了")
        self.assertEqual(service.heard_while_asleep(), ["被人夸了"])

    def test_only_the_newest_few_are_kept(self):
        service = _service()
        for i in range(ASLEEP_HEARD_MAX + 2):
            service._heard_while_asleep_add(f"第{i}件事")
        kept = service.heard_while_asleep()
        self.assertEqual(len(kept), ASLEEP_HEARD_MAX)
        self.assertEqual(kept[-1], f"第{ASLEEP_HEARD_MAX + 1}件事", "超量时丢最旧的")

    def test_capture_takes_the_pending_event_and_interruption(self):
        service = _service()
        service._event_phrase = "被人好好放在心上"
        service._event_at = 9_900.0
        service._fresh_interruption  # 存在即可
        service._capture_heard_while_asleep()
        heard = service.heard_while_asleep()
        self.assertIn("被人好好放在心上", heard)
        self.assertEqual(service._event_phrase, "", "事件已被摘走，不该还在待处理里")

    def test_prompt_carries_the_block_and_no_instruction(self):
        text = segment_prompt(
            cfg(), now_text="09:00", weekday="周六",
            heard_asleep=["被人夸了", "有人来找过她"],
        )
        self.assertIn("【她睡着的时候】", text)
        self.assertIn("被人夸了", text)
        self.assertIn("有人来找过她", text)
        self.assertIn("可能压根没听见", text)
        self.assertNotIn("所以", text.split("【她睡着的时候】")[1].split("【")[0],
                         "只递事实，不替她下结论")

    def test_no_block_when_nothing_was_heard(self):
        text = segment_prompt(cfg(), now_text="09:00", weekday="周六", heard_asleep=[])
        self.assertNotIn("【她睡着的时候】", text)


if __name__ == "__main__":
    unittest.main()
