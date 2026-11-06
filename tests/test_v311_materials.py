"""#311 的回归用例：day.done 素材质量、相邻合并、day.wants 兜底、清醒段时长下限。

线上症状：social 侧只能把 day.done 压成 4~8 字碎片发给多个人（「水壶都叫了」
「灯灭了」）。根因不在 social，而在 Core 交出去的素材：滚动分段日程碎成 17~18
分钟一段（实测 22 段/中位 17 分钟），段事件全是「把碗推到水池泡着」「挪去客厅
仰躺在沙发上」这类过渡小动作——它们不是「她今天做过的事」，只是两件事之间的黏合剂。

这里只钉四件可以客观判定的事：

* 纯位移/收纳/顺手动作不进 done；带可分享实体或情绪的事（吃了什么、去了哪、
  跟谁说了话）照留。处境段（status）不是动作，一律保留。
* 相邻同场景的两段先并成一句，再筛：别把「下饺子」「捞饺子」「吃饺子」拆成三条。
* 日程段没标 wants 时用 today_plans 兜底（最多 3 条）；两边都没有就是空列表，
  **键必须还在**——社交侧按 day.get("wants") 取。
* 清醒段最短 30 分钟（睡段仍是 3 分钟）：17 分钟一段的日程本身就是碎片之源。
"""

from __future__ import annotations

import unittest

from humanoid.services.schedule import (
    SEGMENT_AWAKE_MIN_MINUTES,
    SEGMENT_MIN_MINUTES,
    _clip_event,
    _event_phrase,
    _is_transition_clause,
    _is_transition_event,
    _join_events,
    _trim_dangling,
    _wants_from_plans,
    day_phrases,
    parse_segment,
)


def slot(start: str, end: str, event: str, location: str = "家里", **extra) -> dict:
    """日程段的最小形状：day_phrases 只读这几个键。"""
    data = {"start": start, "end": end, "event": event, "location": location}
    data.update(extra)
    return data


class TransitionFilterTest(unittest.TestCase):
    """纯过渡动作不进 done。"""

    def test_pure_transition_is_dropped(self):
        """「把碗推到水池泡着」「挪去客厅仰躺」是黏合剂，不是做过的事。"""
        phrases = day_phrases(
            [
                slot("09:00", "09:30", "把碗推到水池边泡着", "厨房"),
                slot("09:30", "10:00", "挪去客厅仰躺在沙发上，用蹄子夹着手机", "客厅"),
            ],
            10 * 60 + 30,
        )
        self.assertEqual(phrases["done"], [], "两条都是过渡动作，宁可空着也不硬塞")

    def test_action_with_something_to_share_is_kept(self):
        phrases = day_phrases(
            [slot("17:43", "17:59", "低头把碗里的饺子慢慢吃完，蹄子扶着碗沿", "厨房")],
            18 * 60 + 30,
        )
        self.assertEqual(len(phrases["done"]), 1)
        self.assertIn("饺子", phrases["done"][0], "吃了什么就是可分享的内容")

    def test_status_slot_is_never_filtered(self):
        """处境词（“肚子空着”）不是动作：它本来就是社交侧要的身体读数。"""
        phrases = day_phrases(
            [slot("08:00", "10:00", "肚子空着", "厨房", status=True)], 11 * 60
        )
        self.assertEqual(len(phrases["done"]), 1)
        self.assertIn("肚子空着", phrases["done"][0])
        self.assertNotIn("在肚子空着", phrases["done"][0])

    def test_nothing_left_falls_back_to_what_she_is_doing(self):
        """过滤后一条不剩：退回「正在做的那件事」，而不是把碎片塞回去。"""
        phrases = day_phrases(
            [
                slot("09:00", "09:30", "把碗推到水池边泡着", "厨房"),
                slot("09:30", "10:30", "在厨房煮面，靠着台边慢慢吃", "厨房"),
            ],
            10 * 60 + 5,
        )
        self.assertEqual(phrases["done"], ["上午在厨房煮面，靠着台边慢慢吃"])

    def test_done_keeps_newest_two_after_filtering(self):
        """past_limit 仍是 2，但只数**留下来**的那几条。"""
        phrases = day_phrases(
            [
                slot("09:00", "09:30", "去楼下取了快递，跟门卫聊了两句", "楼下"),
                slot("09:30", "10:00", "把快递盒拆开，东西一件件看了一遍", "客厅"),
                slot("10:00", "10:30", "煮了碗面，靠着台边慢慢吃完", "厨房"),
            ],
            11 * 60,
        )
        self.assertEqual(len(phrases["done"]), 2, "past_limit 还是 2")
        self.assertIn("煮了碗面", phrases["done"][0], "最新的在前")
        self.assertIn("快递盒", phrases["done"][1])


class AdjacentMergeTest(unittest.TestCase):
    """相邻同场景先合并再筛。"""

    def test_same_scene_neighbours_merge_into_one_sentence(self):
        phrases = day_phrases(
            [
                slot("17:30", "17:35", "用蹄子夹着袋子往锅里倒速冻饺子", "厨房"),
                slot("17:35", "17:43", "守着锅等饺子浮起来，用蹄子夹着漏勺把饺子捞进碗里", "厨房"),
            ],
            18 * 60,
        )
        self.assertEqual(len(phrases["done"]), 1, "同一场景的两段并成一句，不拆成并列碎片")
        line = phrases["done"][0]
        self.assertIn("速冻饺子", line)
        self.assertIn("捞进碗里", line)

    def test_different_scene_is_not_merged(self):
        phrases = day_phrases(
            [
                slot("12:03", "12:38", "煮了碗面，靠着台边慢慢吃完", "厨房"),
                slot("12:38", "13:00", "窝在沙发里刷手机，看到那条消息", "客厅"),
            ],
            13 * 60 + 30,
        )
        self.assertEqual(len(phrases["done"]), 2, "换了地方就是两件事")

    def test_a_long_gap_is_not_merged(self):
        phrases = day_phrases(
            [
                slot("09:00", "09:30", "煮了碗面，靠着台边慢慢吃完", "厨房"),
                slot("10:30", "11:00", "又煮了碗汤面，慢慢喝完", "厨房"),
            ],
            11 * 60 + 30,
        )
        self.assertEqual(len(phrases["done"]), 2, "隔了一小时的两件事不该并成一句")


class WantsFallbackTest(unittest.TestCase):
    """day.wants：段标记优先，today_plans 兜底，都没有就空列表（键留着）。"""

    def test_slot_wants_wins(self):
        phrases = day_phrases(
            [
                slot("09:00", "09:30", "去书店翻了翻书，站了很久", "街上", wants=True),
                slot("09:30", "10:00", "煮了碗面，靠着台边慢慢吃完", "厨房"),
            ],
            10 * 60,
            plans=["今天想去看电影"],
        )
        self.assertEqual(phrases["wants"], ["去书店翻了翻书，站了很久"], "段上标了就用段上的")

    def test_falls_back_to_today_plans(self):
        phrases = day_phrases(
            [slot("09:00", "09:30", "煮了碗面，靠着台边慢慢吃完", "厨房")],
            10 * 60,
            plans=["今天想去书店", "晚上给自己做顿好的", "下午去公园走走", "第四条不该进"],
        )
        self.assertEqual(
            phrases["wants"], ["想去书店", "晚上给自己做顿好的", "下午去公园走走"]
        )
        self.assertEqual(len(phrases["wants"]), 3, "最多 3 条")

    def test_empty_when_no_wants_and_no_plans(self):
        phrases = day_phrases(
            [slot("09:00", "09:30", "煮了碗面，靠着台边慢慢吃完", "厨房")], 10 * 60
        )
        self.assertIn("wants", phrases, "键必须留着：缺键和空列表是两件事")
        self.assertEqual(phrases["wants"], [])

    def test_junk_plan_is_not_turned_into_a_want(self):
        """线上那条 today_plans 是「因为副人格带我去过了…」——抱怨，不是她想做的事。"""
        phrases = day_phrases(
            [slot("09:00", "09:30", "煮了碗面，靠着台边慢慢吃完", "厨房")],
            10 * 60,
            plans=["因为副人格带我去过了，今天你再带我去就已经去了两次了"],
        )
        self.assertEqual(phrases["wants"], [])

    def test_plans_helper_strips_lead_words_and_punctuation(self):
        self.assertEqual(
            _wants_from_plans(["今天：想去河边走走。", "  ", "晚上想喝点酒"]),
            ["想去河边走走", "晚上想喝点酒"],
        )


class AwakeDurationFloorTest(unittest.TestCase):
    """清醒段最短 30 分钟，睡段仍是 3 分钟。"""

    def test_awake_segment_has_a_thirty_minute_floor(self):
        self.assertEqual(SEGMENT_AWAKE_MIN_MINUTES, 30)
        self.assertGreater(SEGMENT_AWAKE_MIN_MINUTES, SEGMENT_MIN_MINUTES)

    def test_a_five_minute_awake_segment_climbs_to_the_floor(self):
        parsed = parse_segment({"event": "倒杯水", "minutes": 5}, now_minute=9 * 60, step=15)
        self.assertEqual((parsed["start"], parsed["end"]), ("09:00", "09:30"))

    def test_sleep_segment_keeps_the_three_minute_floor(self):
        parsed = parse_segment({"event": "睡觉", "minutes": 1}, now_minute=9 * 60, step=15)
        self.assertEqual(parsed["end"], "09:03", "睡段不受清醒下限影响")


class MaterialPolishTest(unittest.TestCase):
    """线上回放（_fix_work/_tmp_core/probe_kept.py，读真实 state.json）暴露的残句问题。"""

    def test_a_hard_cut_does_not_leave_a_dangling_particle(self):
        # 线上真句：「凌晨在用蹄子端着热好的牛奶窝在」——硬截把「在」留在了末尾。
        self.assertEqual(_trim_dangling("用蹄子端着热好的牛奶窝在"), "用蹄子端着热好的牛奶")
        self.assertEqual(_trim_dangling("杯子洗了"), "杯子洗")

    def test_a_clipped_phrase_never_ends_with_a_hanging_word(self):
        text = "用蹄子端着热好的牛奶窝在沙发里慢慢啜着看窗外的雪慢慢化掉"
        out = _clip_event(text)
        self.assertLessEqual(len(out), 24)
        self.assertTrue(text.startswith(out))
        self.assertNotIn(out[-1], "在了着的把和跟往从对给就也还又而并")

    def test_a_leading_time_range_is_stripped(self):
        # 「凌晨在01:10–01:30 用蹄子端着热好的牛奶」——时刻不该进正文。
        self.assertEqual(
            _event_phrase("01:10–01:30 用蹄子端着热好的牛奶"), "用蹄子端着热好的牛奶"
        )
        self.assertEqual(_event_phrase("傍晚仰躺沙发"), "仰躺沙发")

    def test_negated_he_is_not_a_content_word(self):
        # 「没喝完的牛奶」说的是杯子里的状态，不是她喝过。
        self.assertTrue(_is_transition_event("端着没喝完的牛奶挪到客厅"))
        self.assertFalse(_is_transition_event("把牛奶喝完了，靠着台边歇着"))

    def test_a_chore_clause_only_counts_at_the_start(self):
        # 句首是家务物件 + 处置动作才算过渡；「守着锅…」句首是守着，那是真事。
        self.assertTrue(_is_transition_clause("杯子洗完没急着走"))
        self.assertFalse(_is_transition_clause("守着锅等饺子浮起来"))

    def test_repeated_clauses_are_deduped_when_merged(self):
        self.assertEqual(_join_events("把碗洗了", "把碗洗了，擦干手"), "把碗洗了，擦干手")

    def test_a_four_clause_merge_is_rejected(self):
        # 并起来四个小句就是流水账（「…开始洗漱，洗漱收尾，擦干脸，关灯回卧室」）。
        phrases = day_phrases(
            [
                slot("17:30", "17:35", "在厨房把速冻饺子下进锅里，守着锅等它浮起来", "厨房"),
                slot("17:35", "17:43", "用蹄子夹着漏勺把饺子捞进碗里，坐在桌边慢慢吃完", "厨房"),
            ],
            18 * 60,
        )
        self.assertEqual(len(phrases["done"]), 2, "小句太多就不合并，各自单独判")
        self.assertTrue(all("，" in line for line in phrases["done"]))


if __name__ == "__main__":
    unittest.main()
