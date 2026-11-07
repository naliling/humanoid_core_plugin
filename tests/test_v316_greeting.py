"""v3.1.6：问候窗口 → 由头卡。

社交层原本自己算「现在是不是问候时段」，算出来的是**时机**；它没有 Core 的事实，只能
把时机当内容用（7 点一到就问「刚醒吗」）。这里把窗口翻成一张正式的卡：时机照旧，
但卡上明说「窗口只提供时机，不代表对方刚醒」，内容由她自己定。要守住四件事：

1. **三个窗口各一张**：早安 7-11、午间 12-14、晚安 21-24（左闭右开，24 即午夜）。
2. **同一天同一窗口只有一张**：anchor_key 固定，社交层据此记账，不会一天问三遍早安。
3. **窗口一过就不返回**：11 点还说「早安」比不说更假。
4. **脏契约不抛**：问候卡是锦上添花，绝不能把整份契约生成搞挂。
"""

from __future__ import annotations

import datetime as _dt
import json
import unittest

from humanoid import anchors, opportunity

DAY = "2026-11-07"


def contract_at(hour, minute=0, offset=480):
    return {
        "time": {
            "local": "%s %02d:%02d" % (DAY, hour, minute),
            "hour": hour,
            "utc_offset_minutes": offset,
        }
    }


def at(hour, minute=0):
    return _dt.datetime(2026, 11, 7, hour, minute)


class GreetingWindowTest(unittest.TestCase):
    """窗口 → 卡：形状、边界、去重、过期、脏数据。"""

    def test_each_window_yields_exactly_one_card(self):
        cases = ((8, "早安窗口"), (13, "午间窗口"), (22, "晚安窗口"))
        for hour, title in cases:
            with self.subTest(hour=hour):
                cards = anchors.greeting_opportunities(contract_at(hour), now=at(hour))
                self.assertEqual(len(cards), 1)
                item = cards[0]
                self.assertEqual(item["kind"], "greeting")
                self.assertEqual(item["purpose"], "greeting")
                self.assertEqual(item["title"], title)
                self.assertEqual(item["source"], anchors.SOURCE_GREETING)
                self.assertIn("窗口只提供时机", item["detail"])
                self.assertIn("不代表对方刚醒", item["detail"])
                self.assertEqual(opportunity.validate_card(item), [])
                self.assertGreater(item["expires_at"], item["captured_at"])
                self.assertTrue(item["anchor_key"].startswith("greeting:"))
                self.assertLessEqual(len(item["anchor_key"]), opportunity.ANCHOR_MAX)

    def test_outside_windows_yields_nothing(self):
        for hour in (0, 6, 11, 14, 20):
            with self.subTest(hour=hour):
                self.assertEqual(
                    anchors.greeting_opportunities(contract_at(hour), now=at(hour)), []
                )

    def test_boundaries_are_left_closed_right_open(self):
        self.assertEqual(len(anchors.greeting_opportunities(contract_at(7), now=at(7))), 1)
        self.assertEqual(anchors.greeting_opportunities(contract_at(11), now=at(11)), [])
        self.assertEqual(len(anchors.greeting_opportunities(contract_at(21), now=at(21))), 1)
        self.assertEqual(anchors.greeting_opportunities(contract_at(0), now=at(0)), [])

    def test_same_window_same_day_is_one_card(self):
        """同一天同一窗口，8:00 与 8:40 必须拿到同一张卡（id 与 anchor_key 都固定）。"""
        first = anchors.greeting_opportunities(contract_at(8, 0), now=at(8, 0))
        second = anchors.greeting_opportunities(contract_at(8, 40), now=at(8, 40))
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(first[0]["id"], second[0]["id"])
        self.assertEqual(first[0]["anchor_key"], second[0]["anchor_key"])

    def test_next_day_gets_a_new_card(self):
        today = anchors.greeting_opportunities(contract_at(8), now=at(8))
        tomorrow_contract = {"time": {"local": "2026-11-08 08:00", "hour": 8, "utc_offset_minutes": 480}}
        tomorrow = anchors.greeting_opportunities(
            tomorrow_contract, now=_dt.datetime(2026, 11, 8, 8, 0)
        )
        self.assertNotEqual(today[0]["id"], tomorrow[0]["id"])
        self.assertNotEqual(today[0]["anchor_key"], tomorrow[0]["anchor_key"])

    def test_expired_window_is_not_returned(self):
        """8 点造的卡 11 点整过期；11:30 再问就一条都不给，而不是给旧卡。"""
        item = anchors.greeting_opportunities(contract_at(8), now=at(8))[0]
        self.assertFalse(
            opportunity.is_expired(item, opportunity.to_epoch(at(10, 59)))
        )
        self.assertTrue(
            opportunity.is_expired(item, opportunity.to_epoch(at(11, 0)))
        )
        self.assertEqual(
            anchors.greeting_opportunities(contract_at(11, 30), now=at(11, 30)), []
        )

    def test_night_window_ends_at_midnight(self):
        """晚安窗口 21-24：23:30 的卡只剩 30 分钟有效期。"""
        item = anchors.greeting_opportunities(contract_at(23, 30), now=at(23, 30))[0]
        self.assertAlmostEqual(
            item["expires_at"] - opportunity.to_epoch(at(23, 30)), 1800.0, places=3
        )

    def test_epoch_now_uses_contract_offset(self):
        """给 epoch 秒时按契约里的 utc_offset_minutes 换成本地时刻再判窗口。"""
        # 本地 2026-11-07 08:00（+08:00）对应 UTC 00:00。
        epoch = _dt.datetime(2026, 11, 7, 0, 0, tzinfo=_dt.timezone.utc).timestamp()
        cards = anchors.greeting_opportunities(
            {"time": {"utc_offset_minutes": 480}}, now=epoch
        )
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["title"], "早安窗口")

    def test_dirty_contracts_never_raise(self):
        dirty = (
            None,
            {},
            [],
            "字符串",
            {"time": None},
            {"time": "不是字典"},
            {"time": {"local": None, "hour": "9"}},
            {"time": {"local": 123}},
            {"time": {"utc_offset_minutes": "abc"}},
            {"time": {"utc_offset_minutes": 999999}},
        )
        for junk in dirty:
            with self.subTest(junk=repr(junk)):
                cards = anchors.greeting_opportunities(junk, now=at(8))
                self.assertIsInstance(cards, list)
                self.assertLessEqual(len(cards), 1)

    def test_card_is_json_safe_and_has_the_nine_keys(self):
        """卡要进契约、进 prompt：恰好九键、能 JSON 序列化、不带多余字段。"""
        item = anchors.greeting_opportunities(contract_at(8), now=at(8))[0]
        self.assertEqual(set(item), set(opportunity.CARD_KEYS))
        self.assertEqual(json.loads(json.dumps(item, ensure_ascii=False)), item)

    def test_greeting_is_not_merged_into_contract_blocks(self):
        """问候卡由 main.py 单独注入；并进 contract_blocks 会重复两条。"""
        blocks = anchors.contract_blocks(
            _FakeCore(), {"day": {"done": ["在阳台晒被子"]}}, now=opportunity.to_epoch(at(8))
        )
        self.assertNotIn("greeting", {item["kind"] for item in blocks["opportunities"]})


class _FakeCore:
    """只够 contract_blocks 跑起来：问候卡本来就不该出现在那里。"""

    role_id = "3632823490"

    class _Clock:
        @staticmethod
        def now():
            return at(8)

    clock = _Clock()

    def persona_text(self):
        return ""


if __name__ == "__main__":
    unittest.main()
