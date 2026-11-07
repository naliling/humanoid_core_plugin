"""v3.1.9 注入呈现层：把「仪表盘」换成「她此刻的样子」。

这一版不生成台词，只改**注入正文**。所以测试也钉在正文上，而不是钉在某个函数
有没有被调用：

1. 证据块不再是说明书——一行一条，标题 + 一句细节，没有出处、没有倒计时、
   没有「她可能想做」。
2. 身体块少判断、多细节——一句判断（周期 + 一条倾向）+ 一句感官细节 + 一句
   手上有没有事；抬头没有版本号，钟点到分钟。
3. 素材带感官细节（`sensory`），通用池扩到 99 条且每天轮换。
4. 她真的说了某条素材之后，这件事落成「已经做了」，随后 3 轮以事实形式出现，
   不再以「可能想做」出现。
5. 顺带修掉的线上 bug：`RoleScope.update_self(**kwargs)` 只收关键字参数，
   而 v3.1.7 起台账按位置参数写——异常被 fail-open 吞掉，`materials_used`
   在线上从未写进去过（假 scope 恰好是位置签名，测试一直是绿的）。
"""

from __future__ import annotations

import re
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from humanoid.services import life_material as lm

from .test_main_integration import main_module
from .test_v306_injection_golden import DAY, UID, _core, _schedule_bodies

TZ = ZoneInfo("Asia/Shanghai")


def _card(
    title: str,
    *,
    detail: str = "",
    kind: str = "life",
    source: str = "生活素材",
    captured_at: float = 0.0,
    expires_at: float = 0.0,
) -> dict:
    return {
        "id": f"opp-{kind}-{title}",
        "kind": kind,
        "purpose": kind,
        "title": title,
        "detail": detail,
        "source": source,
        "captured_at": captured_at,
        "expires_at": expires_at,
        "anchor_key": f"{kind}:{title}",
    }


class EvidenceBlockTest(unittest.TestCase):
    """证据块：一行一条，没有说明书。"""

    NOW = 1_800_000_000.0

    def _text(self, cards, materials=()) -> str:
        return main_module._evidence_note(cards, materials, self.NOW)["text"]

    def test_no_bookkeeping_words_left(self):
        text = self._text(
            [_card("去楼下超市把缺的东西补上", detail="水汽还没散",
                   captured_at=self.NOW - 600, expires_at=self.NOW + 7200)],
            [_card("把攒了两天的衣服洗了", detail="手上有股洗衣液味",
                   captured_at=self.NOW - 600, expires_at=self.NOW + 7200)],
        )
        for word in ("她可能想做", "采集于", "还剩", "大概", "分钟", "[生活素材]", "[日程]"):
            with self.subTest(word=word):
                self.assertNotIn(word, text)

    def test_lead_is_a_single_sentence(self):
        text = self._text([_card("把快递取了", detail="纸箱边角有点硌手")])
        lines = text.splitlines()
        self.assertEqual(lines[0], main_module._EVIDENCE_TITLE)
        self.assertEqual(lines[1], main_module._EVIDENCE_LEAD)
        self.assertEqual(main_module._EVIDENCE_LEAD, "这些是今天真有的、可能做的事，挑一件说，别编。")

    def test_line_is_title_plus_detail(self):
        text = self._text([_card("把快递取了", detail="纸箱边角有点硌手")])
        self.assertIn("- 把快递取了：纸箱边角有点硌手", text)

    def test_only_fresh_cards_get_the_freshness_mark(self):
        fresh = self._text([_card("把快递取了", captured_at=self.NOW - 600)])
        stale = self._text([_card("把快递取了", captured_at=self.NOW - 3 * 3600)])
        self.assertTrue(fresh.rstrip().endswith("（刚刚）"))
        self.assertNotIn("（刚刚）", stale)

    def test_list_is_capped_at_six(self):
        cards = [_card(f"第{i}件事", detail="细节") for i in range(9)]
        result = main_module._evidence_note(cards, [], self.NOW)
        rows = [line for line in result["text"].splitlines() if line.startswith("- ")]
        self.assertEqual(main_module._EVIDENCE_LIMIT, 6)
        self.assertEqual(len(rows), 6)
        self.assertEqual(result["used"], 6)

    def test_empty_block_does_not_tell_her_to_say_less(self):
        text = self._text([], [])
        self.assertEqual(text, "【暂时没有特别的事】\n想到什么说什么，别硬找由头。")
        self.assertNotIn("宁可只回一句", text)


class SensoryLineTest(unittest.TestCase):
    """感官句：只能影响语气，不能直接复述。"""

    @staticmethod
    def _all_options():
        options = []
        for pool in (prompt_pools()):
            options.extend(pool)
        return options

    def test_no_inner_voice_phrasing(self):
        for line in self._all_options():
            for word in ("我感到", "我觉得", "我心里"):
                with self.subTest(line=line, word=word):
                    self.assertNotIn(word, line)

    def test_options_are_short(self):
        for line in self._all_options():
            with self.subTest(line=line):
                self.assertGreaterEqual(len(line), 4)
                self.assertLessEqual(len(line), 24)

    def test_same_day_same_slot_is_stable(self):
        first = _core()
        second = _core()
        self.assertEqual(self._line(first), self._line(second))

    def test_a_different_day_reads_differently(self):
        lines = {
            self._line(_core(datetime(2026, 9, day, 13, 3, tzinfo=TZ)))
            for day in (26, 27, 28, 29)
        }
        self.assertGreater(len(lines), 1)

    def test_body_carries_exactly_one_sensory_line(self):
        body = _core().build_injection(UID, text="在吗")
        hits = [line for line in self._all_options() if line in body]
        self.assertEqual(len(hits), 1, f"感官句应恰好一条，实际：{hits}")

    def test_body_line_matches_the_helper(self):
        core = _core()
        body = core.build_injection(UID, text="在吗")
        expected = self._line(core)
        self.assertIn(expected, body)

    @staticmethod
    def _line(core) -> str:
        builder = core.prompt_builder
        now = core.clock.now()
        soma = core.snapshot(refresh=False).get("soma") or {}
        return builder._sensory_line(now, soma, builder._outfit_text(), builder._seed(UID))


def prompt_pools():
    from humanoid import prompt_builder as pb

    pools = [pb._SENSORY_BODY_LOW, pb._SENSORY_BODY_MID, pb._SENSORY_BODY_HIGH]
    pools.extend(pb._SENSORY_SLOT_LINES.values())
    pools.extend(pb._SENSORY_WEATHER_LINES.values())
    pools.append(pb._sensory_style_lines("外套"))
    pools.append(pb._sensory_style_lines("短袖"))
    return pools


class BodyTextTest(unittest.TestCase):
    """正文不再有版本号、秒、场景标签。"""

    def test_no_version_mark(self):
        body = _core().build_injection(UID, text="在吗")
        self.assertNotIn("v13", body)
        self.assertNotIn(prompt_version(), body)

    def test_no_seconds(self):
        body = _core().build_injection(UID, text="在吗")
        self.assertIsNone(re.search(r"\d{2}:\d{2}:\d{2}", body))

    def test_private_scene_is_a_situation_not_a_label(self):
        body = _core().build_injection(UID, text="在吗")
        self.assertIn("在跟TA私聊", body)
        self.assertNotIn("只有我和TA", body)

    def test_judgements_are_compressed_to_one_sentence(self):
        body = _core().build_injection(UID, text="在吗")
        for phrase in ("前面聊的还能接上", "我这会儿还挺专注", "我内心："):
            self.assertLessEqual(body.count(phrase), 1, phrase)
        self.assertIn("我现在很闲", body)

    def test_hands_are_empty_without_a_schedule(self):
        body = _core().build_injection(UID, text="在吗")
        self.assertIn("这会儿手上是空的", body)
        self.assertNotIn("手上还有件事没放下", body)

    def test_hands_are_busy_without_naming_the_schedule(self):
        body, note = _schedule_bodies()
        self.assertIn("手上还有件事没放下，不是急事", body)
        self.assertNotIn("整理个案笔记", body)
        self.assertNotIn("整理个案笔记", note)


def prompt_version() -> str:
    from humanoid import prompt_builder as pb

    return f"{pb.MARK_PREFIX} {pb.MARK_VERSION}〕"


class NowQuestionTest(unittest.TestCase):
    """「她此刻在做什么」只在对方问起时才给具体内容。"""

    def test_asks_about_now_matches(self):
        self.assertTrue(main_module._asks_about_now("你在干嘛"))
        self.assertTrue(main_module._asks_about_now("干啥呢"))
        self.assertFalse(main_module._asks_about_now("在吗"))

    def test_asked_gives_the_schedule(self):
        _body, note = _schedule_bodies(said="你在干嘛")
        self.assertIn("整理个案笔记", note)
        self.assertIn("书桌前", note)

    def test_unasked_keeps_the_schedule_out(self):
        _body, note = _schedule_bodies(said="在吗")
        self.assertNotIn("整理个案笔记", note)


class DoneMaterialTest(unittest.TestCase):
    """说过的事要落成事实，不能一直停在「可能想做」。"""

    def setUp(self):
        self.core = _core()
        self.now = self.core.now_epoch()
        self.card = _card("把快递取了", detail="纸箱边角有点硌手")
        self.card["captured_at"] = self.now
        self.card["expires_at"] = self.now + 3600.0

    def test_mention_needs_two_real_words(self):
        self.assertTrue(lm.material_mentioned("我下午把快递取了，箱子还挺沉", [self.card]))
        self.assertEqual(lm.material_mentioned("在吗", [self.card]), [])

    def test_said_material_becomes_a_fact(self):
        self.assertEqual(main_module._remember_evidence(self.core, UID, [self.card]), 1)
        self.assertEqual(
            main_module._match_evidence_said(self.core, UID, "我下午把快递取了，箱子还挺沉"),
            1,
        )
        cards, keys = main_module._done_material_cards(self.core)
        self.assertTrue(cards)
        title = cards[0]["title"]
        self.assertTrue(title.startswith("今天"), title)
        self.assertIn("取了", title)
        self.assertEqual(cards[0]["kind"], "life")
        self.assertIn(self.card["anchor_key"], keys)

    def test_done_card_reads_as_happened_not_as_intent(self):
        main_module._remember_evidence(self.core, UID, [self.card])
        main_module._match_evidence_said(self.core, UID, "我下午把快递取了，箱子还挺沉")
        cards, _keys = main_module._done_material_cards(self.core)
        text = main_module._evidence_note(cards, [], self.now)["text"]
        self.assertIn("取了", text)
        self.assertNotIn("她可能想做", text)

    def test_done_card_is_repeated_for_three_turns_then_stops(self):
        main_module._remember_evidence(self.core, UID, [self.card])
        main_module._match_evidence_said(self.core, UID, "我下午把快递取了，箱子还挺沉")
        seen = [len(main_module._done_material_cards(self.core)[0]) for _ in range(4)]
        self.assertEqual(seen[:3], [1, 1, 1])
        self.assertEqual(seen[3], 0)

    def test_note_materials_used_writes_through_the_real_scope(self):
        """回归：真实 `RoleScope.update_self` 只收关键字参数。"""

        class _KwScope:
            def __init__(self):
                self.state = {}

            def get_self(self, key, default=None):
                return self.state.get(key, default)

            def update_self(self, **kwargs):
                self.state.update(kwargs)

        class _KwCore:
            scope = _KwScope()

        core = _KwCore()
        self.assertEqual(main_module._note_materials_used(core, [self.card], self.now), 1)
        self.assertTrue(core.scope.state.get(lm.MATERIALS_USED_KEY))
        self.assertEqual(
            core.scope.state[lm.MATERIALS_USED_KEY][0]["anchor_key"],
            self.card["anchor_key"],
        )

    def test_positional_scope_still_works(self):
        class _PosScope:
            def __init__(self):
                self.state = {}

            def get_self(self, key, default=None):
                return self.state.get(key, default)

            def update_self(self, key, value):
                self.state[key] = value

        class _PosCore:
            scope = _PosScope()

        core = _PosCore()
        self.assertEqual(main_module._note_materials_used(core, [self.card], self.now), 1)
        self.assertTrue(core.scope.state.get(lm.MATERIALS_USED_KEY))


class GenericPoolTest(unittest.TestCase):
    """通用池：没有人设门槛的条目也要能被抽到，且每天不一样。"""

    def test_every_ungated_item_has_a_sensory_detail(self):
        ungated = [item for item in lm.MATERIALS if not item["needs"] and not item["interest"]]
        self.assertGreaterEqual(len(ungated), 60)
        for item in ungated:
            with self.subTest(event=item["event"]):
                self.assertTrue(str(item.get("sensory") or "").strip())

    def test_pool_covers_all_seven_slots(self):
        for slot in lm.SLOT_NAMES:
            with self.subTest(slot=slot):
                self.assertTrue(
                    any(slot in item["slots"] for item in lm.MATERIALS if not item["needs"]),
                    f"{slot} 没有任何无人设门槛的素材",
                )

    def test_ungated_items_are_reachable_without_a_persona(self):
        for minute in (60, 420, 600, 780, 960, 1140, 1290):
            with self.subTest(minute=minute):
                picked = lm.materials_for(
                    ["bot1", "2026-09-26"], "", now_minute=minute, count=10
                )
                self.assertTrue(picked)
                self.assertTrue(all(not item["needs"] for item in picked))

    def test_rotation_changes_with_the_date(self):
        for minute in (420, 780, 960, 1290):
            with self.subTest(minute=minute):
                today = lm.materials_for(["bot1", "2026-09-26"], "", now_minute=minute)
                tomorrow = lm.materials_for(["bot1", "2026-09-27"], "", now_minute=minute)
                self.assertNotEqual(
                    [item["event"] for item in today],
                    [item["event"] for item in tomorrow],
                )

    def test_card_detail_is_sensory_not_a_countdown(self):
        item = next(
            item for item in lm.MATERIALS if not item["needs"] and not item["interest"]
        )
        card = lm.material_card(item, captured_at=1_800_000_000.0, seed=["bot1", "2026-09-26"])
        self.assertTrue(card["detail"].strip())
        self.assertNotIn("分钟", card["detail"])
        self.assertNotIn("大概", card["detail"])


class NicknameCommandTest(unittest.TestCase):
    """`/拟人 查看昵称` 曾经把 5.5 万字符塞进一条合并转发，NapCat 直接抛 resId。"""

    def test_clip_text_hard_caps_the_length(self):
        out = main_module._clip_text("x" * 5000)
        self.assertLessEqual(len(out), main_module.COMMAND_OUTPUT_CHARS)
        self.assertTrue(out.startswith("x"))
        self.assertTrue(out.endswith("…"))

    def test_clip_text_keeps_a_giant_first_line(self):
        """回归：第一行就超预算时不能只剩一句「还有 N 行没显示」。"""

        out = main_module._clip_text("昵称" * 3000)
        self.assertTrue(out.startswith("昵称"))
        self.assertLessEqual(len(out), main_module.COMMAND_OUTPUT_CHARS)

    def test_clip_lines_reports_the_rest(self):
        out = main_module._clip_lines([str(i) for i in range(100)], 30)
        lines = out.splitlines()
        self.assertEqual(len(lines), 31)
        self.assertEqual(lines[-1], "……还有 70 行没显示")

    def test_nickname_command_is_capped_and_grouped(self):
        from pathlib import Path

        text = Path(main_module.__file__).read_text(encoding="utf-8")
        # v3.1.10：只保留「拟人」组这一个入口——管理员指令不该和查状态的指令混在顶层。
        self.assertIn('@humanoid_group.command("查看昵称")', text)
        self.assertNotIn('@filter.command("查看昵称")', text)
        self.assertNotIn("查看昵称", main_module.COMMAND_NAMES)
        self.assertIn("/拟人 查看昵称", main_module.HELP_TEXT)
        self.assertNotIn("/查看昵称", main_module.HELP_TEXT)
        self.assertIn("只显示前", text)
        self.assertIn("NICKNAME_ROWS", text)


if __name__ == "__main__":
    unittest.main()
