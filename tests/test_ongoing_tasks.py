"""「还没做完的事」：让她的日程能跨天。

没有它，她的每一天都是**互不相关**的——今天改方案、明天改方案，两边没有「还没弄完」
这层，于是每段都像新任务。社交侧那边它也是最好的由头来源（比天气有意思得多）。
"""
import time
import unittest

from humanoid.config import HumanoidConfig
from humanoid.persona import Persona
from humanoid.services.schedule import ScheduleService, segment_prompt
from humanoid.slots import normalize_slots


class _Scope:
    def __init__(self):
        self.d = {}
    def get_self(self, k, dv=None):
        return self.d.get(k, dv)
    def set_self(self, k, v):
        self.d[k] = v
    def mark_dirty(self):
        pass


def _svc():
    s = object.__new__(ScheduleService)
    s._scope = _Scope()
    return s


class SlotKeepsIt(unittest.TestCase):
    def test_ongoing_survives_normalization(self):
        slots = normalize_slots([{
            "start": "09:00", "end": "11:00", "event": "整理个案笔记",
            "location": "书桌前", "ongoing": "整理那份个案笔记（还差 4 份）"}])
        got = [s for s in slots if s.get("ongoing")]
        self.assertTrue(got, "ongoing 字段在归一化时丢了")
        self.assertEqual(got[0]["ongoing"], "整理那份个案笔记（还差 4 份）")

    def test_absent_when_not_reported(self):
        slots = normalize_slots([
            {"start": "09:00", "end": "11:00", "event": "吃早饭", "location": "厨房"}])
        self.assertFalse(any("ongoing" in s for s in slots))


class PromptAsksAndFeeds(unittest.TestCase):
    def test_output_format_asks_for_it(self):
        t = segment_prompt(HumanoidConfig.from_raw({}), now_text="10:00", weekday="三",
                            persona=Persona("小满", "你是小满。", "x"))
        self.assertIn("ongoing", t, "输出格式里没有 ongoing，模型不知道能报这个")

    def test_rule_says_only_for_multi_day_work(self):
        t = segment_prompt(HumanoidConfig.from_raw({}), now_text="10:00", weekday="三",
                            persona=Persona("小满", "你是小满。", "x"))
        self.assertIn("跨好几天", t)
        self.assertIn("一次能做完的别写", t)

    def test_fed_back_when_there_is_one(self):
        t = segment_prompt(HumanoidConfig.from_raw({}), now_text="10:00", weekday="三",
                            persona=Persona("小满", "你是小满。", "x"),
                            ongoing=["整理那份个案笔记（还差 4 份）"])
        self.assertIn("你还没做完的事", t)
        self.assertIn("整理那份个案笔记", t)

    def test_says_so_when_there_is_none(self):
        t = segment_prompt(HumanoidConfig.from_raw({}), now_text="10:00", weekday="三",
                            persona=Persona("小满", "你是小满。", "x"), ongoing=None)
        self.assertIn("目前没有还没做完的事", t)


class StateAcrossDays(unittest.TestCase):
    def test_records_and_reads_back(self):
        s = _svc()
        s._note_ongoing({"ongoing": "整理那份个案笔记（还差 4 份）"})
        self.assertEqual(s.ongoing_tasks(), ["整理那份个案笔记（还差 4 份）"])

    def test_progress_update_replaces_rather_than_stacks(self):
        """模型每次报的措辞带进度，按字面比会堆成好几条同一件事。"""
        s = _svc()
        for t in ("整理那份个案笔记（还差 4 份）", "整理那份个案笔记（还差 2 份）"):
            s._note_ongoing({"ongoing": t})
        self.assertEqual(len(s.ongoing_tasks()), 1, s.ongoing_tasks())
        self.assertIn("还差 2 份", s.ongoing_tasks()[0], "该留最新那条")

    def test_two_different_things_coexist(self):
        s = _svc()
        s._note_ongoing({"ongoing": "整理那份个案笔记"})
        s._note_ongoing({"ongoing": "写完那封信"})
        self.assertEqual(len(s.ongoing_tasks()), 2)

    def test_cap(self):
        s = _svc()
        for i in range(6):
            s._note_ongoing({"ongoing": f"第{i}件不同的事"})
        self.assertLessEqual(len(s.ongoing_tasks()), ScheduleService.ONGOING_MAX)

    def test_expires_after_few_days(self):
        s = _svc()
        s._note_ongoing({"ongoing": "整理那份个案笔记"})
        s._scope.set_self(s.ONGOING_KEY, [
            {"what": "整理那份个案笔记",
             "at": time.time() - (ScheduleService.ONGOING_TTL_DAYS + 1) * 86400}])
        self.assertEqual(s.ongoing_tasks(), [], "过期的还没清")

    def test_a_slot_without_ongoing_does_not_clear_them(self):
        """她这次没提，不代表那件事不用做了——不能因为一次沉默就抹掉。"""
        s = _svc()
        s._note_ongoing({"ongoing": "整理那份个案笔记"})
        s._note_ongoing({"ongoing": ""})
        self.assertEqual(len(s.ongoing_tasks()), 1)

    def test_long_text_is_trimmed(self):
        s = _svc()
        s._note_ongoing({"ongoing": "很长的" * 40})
        self.assertLessEqual(len(s.ongoing_tasks()[0]), 40)


if __name__ == "__main__":
    unittest.main()
