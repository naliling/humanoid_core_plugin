"""v3.1.6：主动消息「已发清单」的出处（provenance）。

社交层要记得「这条主动消息是拿哪张卡说的」，但又不能把正文抄进记录：卡会过期、会
重建，抄下来的正文就成了第二份会漂移的事实。所以记录只存指针（card_id），读的时候
回卡里补出处。要守住五件事：

1. **固定四键**：contact_id / card_id / source / captured_at，多一个都不存。
2. **最新在尾、超限丢最旧**：读「最近一条」不用倒序，裁剪只动头部。
3. **旧记录不猜**：没有 card_id 的记录原样返回——不知道出处就是不知道。
4. **不覆盖已知出处**：记录里的 source 是当时的事实，卡里的是现在的，以记录为准。
5. **脏数据不抛**：坏 entry 跳过、坏 records 当空、坏 card 忽略。
"""

from __future__ import annotations

import unittest

from humanoid.services import share_log


def entry(contact_id="c1", card_id="opp-1", source="问候时机", captured_at=100.0):
    return {
        "contact_id": contact_id,
        "card_id": card_id,
        "source": source,
        "captured_at": captured_at,
    }


class AppendTest(unittest.TestCase):
    """追加：归一化、上限、坏数据。"""

    def test_append_normalizes_to_exactly_four_keys(self):
        out = share_log.append([], entry())
        self.assertEqual(len(out), 1)
        self.assertEqual(tuple(out[0]), share_log.RECORD_KEYS)
        self.assertEqual(out[0], entry())

    def test_append_accepts_nested_provenance(self):
        out = share_log.append(
            [],
            {
                "contact_id": "c1",
                "provenance": {"card_id": "opp-9", "source": "天气", "captured_at": 7200},
            },
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["card_id"], "opp-9")
        self.assertEqual(out[0]["source"], "天气")
        self.assertEqual(out[0]["captured_at"], 7200.0)

    def test_append_does_not_mutate_inputs(self):
        records = [entry()]
        incoming = entry(contact_id="c2", card_id="opp-2")
        snapshot = [dict(item) for item in records]
        out = share_log.append(records, incoming)
        self.assertEqual(records, snapshot)
        self.assertEqual(incoming, entry(contact_id="c2", card_id="opp-2"))
        self.assertEqual(len(out), 2)
        self.assertIsNot(out, records)

    def test_append_keeps_newest_at_tail_and_trims_oldest(self):
        records = []
        for index in range(5):
            records = share_log.append(
                records, entry(contact_id="c%d" % index, card_id="opp-%d" % index), limit=3
            )
        self.assertEqual([item["contact_id"] for item in records], ["c2", "c3", "c4"])

    def test_append_limit_zero_drops_everything(self):
        self.assertEqual(share_log.append([entry()], entry(contact_id="c2"), limit=0), [])

    def test_append_skips_junk_entries(self):
        records = [entry()]
        for junk in (None, "字符串", 123, [], {}, {"contact_id": "  "}):
            with self.subTest(junk=repr(junk)):
                out = share_log.append(records, junk)
                self.assertEqual(len(out), 1)

    def test_append_tolerates_bad_records_argument(self):
        for junk in (None, "x", 5, {}):
            with self.subTest(junk=repr(junk)):
                out = share_log.append(junk, entry())
                self.assertEqual(len(out), 1)

    def test_append_keeps_old_records_untouched(self):
        """旧记录没有出处键：原样留着，不补空键、不猜出处。"""
        old = {"contact_id": "old"}
        out = share_log.append([old], entry())
        self.assertEqual(out[0], old)
        self.assertNotIn("card_id", out[0])

    def test_bad_captured_at_becomes_zero(self):
        out = share_log.append([], entry(captured_at="昨天"))
        self.assertEqual(out[0]["captured_at"], 0.0)
        out = share_log.append([], entry(captured_at=float("nan")))
        self.assertEqual(out[0]["captured_at"], 0.0)


class ProvenanceTest(unittest.TestCase):
    """回填：按 card_id 补 source / captured_at，不覆盖已知值。"""

    CARDS = [
        {"id": "opp-1", "source": "日程", "captured_at": 7200.0},
        {"id": "opp-2", "source": "天气", "captured_at": 3600.0},
    ]

    def test_fills_missing_fields_from_card(self):
        records = [entry(source="", captured_at=0.0)]
        out = share_log.with_provenance(records, self.CARDS)
        self.assertEqual(out[0]["source"], "日程")
        self.assertEqual(out[0]["captured_at"], 7200.0)

    def test_does_not_overwrite_existing_fields(self):
        records = [entry(source="问候时机", captured_at=100.0)]
        out = share_log.with_provenance(records, self.CARDS)
        self.assertEqual(out[0]["source"], "问候时机")
        self.assertEqual(out[0]["captured_at"], 100.0)

    def test_old_records_without_card_id_are_left_alone(self):
        old = {"contact_id": "old"}
        out = share_log.with_provenance([old], self.CARDS)
        self.assertEqual(out, [old])

    def test_unknown_card_id_is_left_alone(self):
        records = [entry(card_id="opp-404", source="", captured_at=0.0)]
        out = share_log.with_provenance(records, self.CARDS)
        self.assertEqual(out[0]["source"], "")
        self.assertEqual(out[0]["captured_at"], 0.0)

    def test_duplicate_card_ids_first_wins(self):
        cards = [
            {"id": "opp-1", "source": "日程", "captured_at": 7200.0},
            {"id": "opp-1", "source": "天气", "captured_at": 3600.0},
        ]
        out = share_log.with_provenance([entry(source="", captured_at=0.0)], cards)
        self.assertEqual(out[0]["source"], "日程")

    def test_dirty_cards_and_records_do_not_raise(self):
        junk_cards = [None, "x", {}, {"id": ""}, {"id": 123}]
        for cards in (junk_cards, None, "x", 5):
            with self.subTest(cards=repr(cards)):
                self.assertIsInstance(share_log.with_provenance([entry()], cards), list)
        self.assertEqual(share_log.with_provenance(None, self.CARDS), [])
        self.assertEqual(share_log.with_provenance("x", self.CARDS), [])

    def test_does_not_mutate_input_records(self):
        records = [entry(source="", captured_at=0.0)]
        snapshot = [dict(item) for item in records]
        share_log.with_provenance(records, self.CARDS)
        self.assertEqual(records, snapshot)


class FindByCardTest(unittest.TestCase):
    """按 card_id 取回发送记录：只给指针，正文回卡里取。"""

    def test_returns_latest_match(self):
        records = [
            entry(contact_id="c1", card_id="opp-1"),
            entry(contact_id="c2", card_id="opp-2"),
            entry(contact_id="c3", card_id="opp-1"),
        ]
        found = share_log.find_by_card(records, "opp-1")
        self.assertEqual(found["contact_id"], "c3")

    def test_missing_card_returns_empty_dict(self):
        self.assertEqual(share_log.find_by_card([entry()], "opp-404"), {})
        self.assertEqual(share_log.find_by_card([], "opp-1"), {})
        self.assertEqual(share_log.find_by_card(None, "opp-1"), {})

    def test_empty_card_id_returns_empty_dict(self):
        self.assertEqual(share_log.find_by_card([entry()], ""), {})
        self.assertEqual(share_log.find_by_card([entry()], None), {})

    def test_dirty_records_do_not_raise(self):
        junk = [None, "x", 5, {"contact_id": "no-card"}]
        self.assertEqual(share_log.find_by_card(junk, "opp-1"), {})
        found = share_log.find_by_card([entry()], "  opp-1  ")
        self.assertEqual(found["contact_id"], "c1")


if __name__ == "__main__":
    unittest.main()
