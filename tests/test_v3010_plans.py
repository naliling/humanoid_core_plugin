"""v3.0.10：从对话里抽「她今天要办的事」。

日程生成原来只看得到人设、身体、天气和最近几段日程。可她今天要陪人去复查、要交稿、
要去看牙——这些只存在于对话里，于是模型给她排的永远是她一个人在屋里绕圈。

抽取有四道门，缺一不可：日期是今天、主语是她、不是疑问/否定、只存原话片段。
宁可漏抽，也不能把对方的安排记成她的：漏抽只是少一条素材，记错就是日程在编造。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.services import plans
from humanoid.services.schedule import ScheduleService, segment_prompt

from .fakes import FrozenClock, RecordingLogger, ScopeStore
from .test_schedule import cfg

TZ = ZoneInfo("Asia/Shanghai")
MOMENT = datetime(2026, 8, 22, 13, 0, tzinfo=TZ)
TODAY = date(2026, 8, 22)
WEEKDAY_CHARS = "一二三四五六日"


def _scope():
    conf = cfg()
    store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
    store.load(MOMENT.strftime("%Y-%m-%d"), conf.cycle_length)
    return store.scope


def _today_char(today=TODAY):
    return WEEKDAY_CHARS[today.weekday()]


class DayOffsetsTest(unittest.TestCase):
    def test_today_words_are_zero(self):
        for word in ("今天", "今晚", "今早", "今儿", "今日"):
            self.assertEqual(plans.day_offsets("你" + word + "要出门", TODAY), {0}, word)

    def test_tomorrow_is_one(self):
        self.assertEqual(plans.day_offsets("你明天要交稿", TODAY), {1})

    def test_this_week_weekday(self):
        clause = "你周" + _today_char() + "要去复查"
        self.assertEqual(plans.day_offsets(clause, TODAY), {0})

    def test_next_week_adds_seven(self):
        clause = "你下周" + _today_char() + "要去复查"
        self.assertEqual(plans.day_offsets(clause, TODAY), {7})

    def test_absolute_date(self):
        self.assertEqual(plans.day_offsets("你 8月22日 要去复查", TODAY), {0})
        self.assertEqual(plans.day_offsets("你 8月23日 要去复查", TODAY), {1})

    def test_past_absolute_date_is_not_today(self):
        self.assertNotIn(0, plans.day_offsets("你 8月1日 去过医院", TODAY))

    def test_bare_day_number(self):
        self.assertEqual(plans.day_offsets("你 22号 要去复查", TODAY), {0})
        self.assertEqual(plans.day_offsets("你 23号 要去复查", TODAY), {1})


class ExtractTest(unittest.TestCase):
    def test_her_plan_is_extracted(self):
        self.assertEqual(plans.extract_plans("你今天下午要去复查。", TODAY), ["你今天下午要去复查"])

    def test_third_person_is_extracted(self):
        self.assertEqual(plans.extract_plans("她今天得交稿", TODAY), ["她今天得交稿"])

    def test_shared_plan_is_extracted(self):
        self.assertEqual(plans.extract_plans("我们今天去看电影", TODAY), ["我们今天去看电影"])

    def test_the_other_persons_plan_is_not_hers(self):
        self.assertEqual(plans.extract_plans("我今天要去医院", TODAY), [])

    def test_no_subject_means_no_plan(self):
        self.assertEqual(plans.extract_plans("今天要去医院", TODAY), [])

    def test_tomorrow_is_not_today(self):
        self.assertEqual(plans.extract_plans("你明天记得吃药", TODAY), [])

    def test_question_is_not_a_plan(self):
        self.assertEqual(plans.extract_plans("你今天不是要去医院吗", TODAY), [])
        self.assertEqual(plans.extract_plans("你今天要去医院？", TODAY), [])

    def test_negation_is_not_a_plan(self):
        self.assertEqual(plans.extract_plans("你今天不用去了", TODAY), [])
        self.assertEqual(plans.extract_plans("你今晚不想出门", TODAY), [])

    def test_too_short_is_ignored(self):
        self.assertEqual(plans.extract_plans("今天去", TODAY), [])

    def test_splits_on_punctuation_and_newlines(self):
        text = "你今天要交稿！你今晚还得去医院。\n你明天休息"
        self.assertEqual(
            plans.extract_plans(text, TODAY),
            ["你今天要交稿", "你今晚还得去医院"],
        )

    def test_limit_and_dedupe(self):
        text = "你今天要交稿。你今天要交稿。你今晚要去医院。你今天要开会。你今天要买菜。"
        found = plans.extract_plans(text, TODAY)
        self.assertEqual(len(found), plans.MAX_PLANS)
        self.assertEqual(len(set(found)), len(found))

    def test_long_clause_is_truncated(self):
        clause = "你今天要去" + "很远的地方办一件很麻烦的事" * 5
        found = plans.extract_plans(clause, TODAY)
        self.assertEqual(len(found), 1)
        self.assertLessEqual(len(found[0]), plans.PLAN_MAX_CHARS)


class StoreTest(unittest.TestCase):
    def test_note_then_load(self):
        scope = _scope()
        plans.note_plans(scope, "你今天下午要去复查", TODAY)
        self.assertEqual(plans.load_plans(scope, TODAY), ["你今天下午要去复查"])

    def test_yesterdays_plans_do_not_leak_into_today(self):
        scope = _scope()
        plans.note_plans(scope, "你今天下午要去复查", TODAY)
        self.assertEqual(plans.load_plans(scope, date(2026, 8, 23)), [])

    def test_merge_keeps_the_latest(self):
        scope = _scope()
        plans.note_plans(scope, "你今天要交稿", TODAY)
        plans.note_plans(scope, "你今晚要去医院", TODAY)
        self.assertEqual(
            plans.load_plans(scope, TODAY), ["你今天要交稿", "你今晚要去医院"]
        )

    def test_garbage_on_disk_reads_as_empty(self):
        scope = _scope()
        scope.update_self(today_plans="坏了", today_plans_date=TODAY.strftime("%Y-%m-%d"))
        self.assertEqual(plans.load_plans(scope, TODAY), [])


class PromptBlockTest(unittest.TestCase):
    def _prompt(self, **kwargs):
        return segment_prompt(
            cfg(),
            now_text="12:30",
            weekday="六",
            persona=None,
            body={},
            prev=None,
            elapsed_minutes=0,
            history=[],
            history_note="",
            ongoing=[],
            **kwargs,
        )

    def test_plans_get_their_own_block(self):
        prompt = self._prompt(plans=["你今天下午要去复查"])
        self.assertIn("【她自己说过今天要办的事】", prompt)
        self.assertIn("  - 你今天下午要去复查", prompt)

    def test_no_plans_means_no_block(self):
        self.assertNotIn("【她自己说过今天要办的事】", self._prompt())

    def test_block_says_it_is_not_a_task_list(self):
        prompt = self._prompt(plans=["你今天下午要去复查"])
        self.assertIn("别把一整天都围着它们排", prompt)


class ServiceTest(unittest.IsolatedAsyncioTestCase):
    def _service(self, moment=MOMENT):
        conf = cfg()
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load(moment.strftime("%Y-%m-%d"), conf.cycle_length)
        return ScheduleService(
            store.scope,
            lambda: conf,
            FrozenClock(moment),
            logger=RecordingLogger(),
            monotonic=lambda: 10_000.0,
        )

    async def test_note_today_plans_stores_what_she_said(self):
        service = self._service()
        found = service.note_today_plans("你今晚要去医院陪床")
        self.assertEqual(found, ["你今晚要去医院陪床"])
        self.assertEqual(service._today_plans(), ["你今晚要去医院陪床"])

    async def test_chatter_is_ignored(self):
        service = self._service()
        self.assertEqual(service.note_today_plans("哈哈哈你好可爱"), [])
        self.assertEqual(service._today_plans(), [])

    async def test_empty_message_is_harmless(self):
        service = self._service()
        self.assertEqual(service.note_today_plans(""), [])
        self.assertEqual(service.note_today_plans("   "), [])
