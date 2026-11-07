"""v3.1.6 生活素材流动：按日期轮换、近 7 天去重、绑日程时段。

三条要守住的东西：
1. 同一天同一时段两次取到的素材逐字节相同（契约是「变了才重写」）；
2. 换一天换一批，且顺序可由 sha256 复算（不是 random 碰运气）；
3. materials_used 读不到 / 结构脏时不过滤（fail-open），不能因为一条烂记录
   让她今天一条素材都没有。
"""

from __future__ import annotations

import datetime as _dt
import unittest

from humanoid import anchors, opportunity
from humanoid.services import life_material as lm

PERSONA = "一个上班族，家里养了一只橘猫，喜欢看书和画画，周末会和朋友出去。"
NOW = 1_790_000_000.0
HOUR = 3600.0
DAY = 24 * HOUR


class _Scope:
    """最小 scope：只实现读，写侧由 main.py 接。"""

    def __init__(self, store=None):
        self.store = dict(store or {})

    def get_self(self, key, default=None):
        return self.store.get(key, default)

    def update_self(self, key, value):
        self.store[key] = value


class _Core:
    role_id = "bot1"

    def __init__(self, *, used=None, persona=PERSONA, broken=False):
        store = {}
        if used is not None:
            store[lm.MATERIALS_USED_KEY] = used
        self.scope = _Scope(store)
        self._persona = persona
        self._broken = broken

    def _persona_text(self):
        if self._broken:
            raise RuntimeError("persona 读不到")
        return self._persona


class _NoScopeCore(_Core):
    """scope 上根本没有 get_self：读不到 used 记录。"""

    def __init__(self):
        super().__init__()
        self.scope = object()


def _cards(core, contract=None, now=NOW, limit=8):
    return anchors.material_cards(core, contract or {}, now=now, limit=limit)


def _events(items):
    return [str(item["event"]) for item in items]


class DeterministicRotationTest(unittest.TestCase):
    def test_same_day_same_slot_is_byte_identical(self):
        seed = ["bot1", "2026-08-22"]
        first = lm.materials_for(seed, PERSONA, 13 * 60, (), 10)
        second = lm.materials_for(seed, PERSONA, 13 * 60, (), 10)
        self.assertEqual(first, second)

    def test_same_slot_different_minute_is_stable(self):
        seed = ["bot1", "2026-08-22"]
        first = lm.materials_for(seed, PERSONA, 13 * 60, (), 10)
        second = lm.materials_for(seed, PERSONA, 13 * 60 + 50, (), 10)
        self.assertEqual(_events(first), _events(second))

    def test_next_day_gets_a_different_batch(self):
        today = lm.materials_for(["bot1", "2026-08-22"], PERSONA, 13 * 60, (), 10)
        tomorrow = lm.materials_for(["bot1", "2026-08-23"], PERSONA, 13 * 60, (), 10)
        self.assertNotEqual(_events(today), _events(tomorrow))

    def test_order_is_recomputable_from_sha256(self):
        seed = ["bot1", "2026-08-22"]
        items = lm.materials_for(seed, PERSONA, 13 * 60, (), 10)
        seed_text = lm._seed_text(seed, lm.slot_name(13 * 60))
        expected = sorted(
            _events(items), key=lambda text: lm._rank(seed_text + "|out", text)
        )
        self.assertEqual(_events(items), expected)

    def test_cards_are_stable_within_the_hour(self):
        core = _Core()
        first = _cards(core, now=NOW)
        second = _cards(core, now=NOW + 30 * 60)
        self.assertEqual([c["id"] for c in first], [c["id"] for c in second])

    def test_cards_change_next_day(self):
        core = _Core()
        today = _cards(core, now=NOW)
        tomorrow = _cards(core, now=NOW + DAY)
        self.assertNotEqual([c["id"] for c in today], [c["id"] for c in tomorrow])


class UsedDedupTest(unittest.TestCase):
    def test_recent_used_anchor_is_skipped(self):
        baseline = _cards(_Core())
        target = baseline[0]
        used = [
            {
                "card_id": target["id"],
                "anchor_key": target["anchor_key"],
                "used_at": NOW - HOUR,
            }
        ]
        cards = _cards(_Core(used=used))
        self.assertNotIn(target["anchor_key"], [c["anchor_key"] for c in cards])
        self.assertEqual(len(cards), 8)

    def test_card_id_only_record_is_skipped(self):
        baseline = _cards(_Core())
        target = baseline[0]
        used = [{"card_id": target["id"], "used_at": NOW - HOUR}]
        cards = _cards(_Core(used=used))
        self.assertNotIn(target["id"], [c["id"] for c in cards])
        self.assertEqual(len(cards), 7)

    def test_eight_days_old_record_is_ignored(self):
        baseline = _cards(_Core())
        target = baseline[0]
        used = [
            {
                "card_id": target["id"],
                "anchor_key": target["anchor_key"],
                "used_at": NOW - 8 * DAY,
            }
        ]
        self.assertEqual(
            [c["id"] for c in _cards(_Core(used=used))],
            [c["id"] for c in baseline],
        )

    def test_unreadable_scope_fails_open(self):
        baseline = [c["id"] for c in _cards(_Core())]
        self.assertEqual([c["id"] for c in _cards(_NoScopeCore())], baseline)

    def test_dirty_records_fail_open(self):
        dirty = ["坏了", 42, None, {"used_at": "昨天"}, {"card_id": None, "anchor_key": 7}]
        self.assertEqual(
            [c["id"] for c in _cards(_Core(used=dirty))],
            [c["id"] for c in _cards(_Core())],
        )

    def test_used_keys_window_and_junk(self):
        now = 10 * DAY
        records = [
            {"anchor_key": "life:a", "used_at": now - 6 * DAY},
            {"anchor_key": "life:b", "used_at": now - 8 * DAY},
            {"anchor_key": "life:c", "used_at": 0},
            {"card_id": "opp-1", "used_at": now - HOUR},
            "bad",
            {"anchor_key": "", "used_at": now - HOUR},
        ]
        self.assertEqual(lm.used_keys(records, now), {"life:a", "opp-1"})

    def test_used_keys_tolerates_non_list(self):
        for bad in (None, "x", 42, {"a": 1}):
            self.assertEqual(lm.used_keys(bad, NOW), set())

    def test_remember_used_dedupes_and_caps(self):
        records = []
        for index in range(200):
            card = {"id": "opp-" + str(index), "anchor_key": "life:" + str(index)}
            records = lm.remember_used(records, card, NOW + index)
        self.assertEqual(len(records), 128)
        stamps = [row["used_at"] for row in records]
        self.assertEqual(stamps, sorted(stamps))
        self.assertNotIn("opp-0", [row["card_id"] for row in records])
        self.assertIn("opp-199", [row["card_id"] for row in records])

    def test_remember_used_overwrites_the_same_card(self):
        card = {"id": "opp-x", "anchor_key": "life:x"}
        records = lm.remember_used([], card, NOW)
        records = lm.remember_used(records, card, NOW + HOUR)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["used_at"], NOW + HOUR)


class SegmentBindingTest(unittest.TestCase):
    def test_segment_kind_classification(self):
        self.assertEqual(lm.segment_kind("在床上睡觉"), "sleep")
        self.assertEqual(lm.segment_kind("午觉"), "sleep")
        self.assertEqual(lm.segment_kind("", asleep=True), "sleep")
        self.assertEqual(lm.segment_kind("在公司上班"), "work")
        self.assertEqual(lm.segment_kind("午休"), "break")
        self.assertEqual(lm.segment_kind("在客厅看书"), "")

    def test_sleep_segment_keeps_only_night_rest(self):
        items = lm.materials_for(
            ["bot1", "2026-08-22"], PERSONA, 2 * 60, (), 10, segment="睡觉", asleep=True
        )
        self.assertTrue(items)
        for item in items:
            self.assertEqual(item["band"], "rest")
            self.assertTrue(set(item["slots"]) & {"凌晨", "夜里"})

    def test_work_segment_drops_home_chores(self):
        items = lm.materials_for(
            ["bot1", "2026-08-22"], PERSONA, 10 * 60, (), 10, segment="在公司上班"
        )
        bands = {item["band"] for item in items}
        self.assertTrue(items)
        self.assertTrue(bands <= {"duty", "interest"}, bands)

    def test_work_segment_falls_back_when_persona_has_no_job(self):
        # 人设里没有工作/兴趣条件时，duty 与 interest 两档整个被 _allowed 筛掉；
        # 上班段不能因此变成空批——兜底回不挑时段的池子，宁可递一条普通素材。
        items = lm.materials_for(
            ["bot1", "2026-08-22"], "", 10 * 60, (), 10, segment="在公司上班"
        )
        self.assertTrue(items)
        self.assertTrue({item["band"] for item in items} & {"life", "rest", "rare"})

    def test_sleep_segment_does_not_fall_back(self):
        # 睡觉段没有兜底：把中午能用的 rest 档全部当成「已经做过」，睡段就真的空了。
        # 如果兜底也覆盖 sleep，这里会退回不挑时段的池子、混进生活素材。
        recent = ("刷手机", "发呆", "泡茶", "泡壶茶", "吹风", "拖延")
        items = lm.materials_for(
            ["bot1", "2026-08-22"], PERSONA, 13 * 60, recent, 10, segment="睡觉", asleep=True
        )
        self.assertEqual(items, [])

    def test_break_segment_drops_duty(self):
        items = lm.materials_for(
            ["bot1", "2026-08-22"], PERSONA, 12 * 60 + 30, (), 10, segment="午休"
        )
        bands = {item["band"] for item in items}
        self.assertTrue(items)
        self.assertNotIn("duty", bands)

    def test_unknown_segment_keeps_the_usual_mix(self):
        items = lm.materials_for(
            ["bot1", "2026-08-22"], PERSONA, 15 * 60, (), 10, segment="在客厅发呆"
        )
        bands = {item["band"] for item in items}
        self.assertTrue(bands & {"life", "rest", "rare", "social"}, bands)

    def test_contract_activity_drives_the_filter(self):
        moment = _dt.datetime(2026, 8, 22, 10, 30)
        core = _Core()
        plain = _cards(core, now=moment)
        working = _cards(core, {"activity": {"schedule_event": "在公司上班"}}, now=moment)
        self.assertTrue(working)
        self.assertTrue({c["source"] for c in working} <= {"责任", "兴趣"})
        self.assertNotEqual([c["id"] for c in plain], [c["id"] for c in working])

    def test_deep_sleep_flag_drives_the_filter(self):
        moment = _dt.datetime(2026, 8, 22, 3, 0)
        cards = _cards(_Core(), {"time": {"is_deep_sleep": True}}, now=moment)
        self.assertTrue(cards)
        for card in cards:
            self.assertEqual(card["source"], "歇着")

    def test_broken_persona_still_yields_cards(self):
        self.assertTrue(_cards(_Core(broken=True)))


class CardStabilityTest(unittest.TestCase):
    def test_cards_are_valid_and_expire_in_eight_hours(self):
        cards = _cards(_Core())
        self.assertTrue(cards)
        for card in cards:
            self.assertEqual(opportunity.validate_card(card), [])
            self.assertEqual(card["captured_at"], opportunity.hour_bucket(NOW))
            self.assertEqual(
                card["expires_at"] - card["captured_at"], lm.MATERIAL_TTL_SECONDS
            )
            self.assertEqual(card["kind"], "life")

    def test_same_material_has_the_same_id_across_hours(self):
        item = dict(lm.MATERIALS[0])
        first = lm.material_card(item, captured_at=NOW, seed=["bot1", "2026-08-22"])
        second = lm.material_card(item, captured_at=NOW + 5 * HOUR, seed=["bot1", "2026-08-22"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["anchor_key"], second["anchor_key"])
        self.assertNotEqual(first["captured_at"], second["captured_at"])

    def test_one_batch_has_no_duplicate_ids(self):
        ids = [c["id"] for c in _cards(_Core())]
        self.assertEqual(len(ids), len(set(ids)))


class PoolTest(unittest.TestCase):
    def test_pool_is_big_enough_to_rotate(self):
        self.assertGreaterEqual(len(lm.MATERIALS), 75)
        self.assertLessEqual(len(lm.MATERIALS), 90)

    def test_every_band_has_room(self):
        counts = {}
        for item in lm.MATERIALS:
            counts[item["band"]] = counts.get(item["band"], 0) + 1
        for band, quota in lm.BAND_QUOTA:
            self.assertGreaterEqual(counts.get(band, 0), quota * 2, band)

    def test_topics_are_unique(self):
        topics = [item["topic"] for item in lm.MATERIALS]
        self.assertEqual(len(topics), len(set(topics)))
        self.assertTrue(all(topics))

    def test_entries_are_well_formed(self):
        bands = {band for band, _ in lm.BAND_QUOTA}
        needs = {name for name, _ in lm.CAPABILITY_WORDS}
        interests = {name for name, _ in lm.INTEREST_WORDS}
        for item in lm.MATERIALS:
            self.assertTrue(item["event"])
            self.assertNotIn("睡", item["event"])
            self.assertIn(item["band"], bands)
            self.assertTrue(set(item["slots"]) <= set(lm.SLOT_NAMES), item["event"])
            self.assertTrue(set(item["needs"]) <= needs, item["event"])
            self.assertTrue(item["interest"] == "" or item["interest"] in interests)
            lo, hi = item["minutes"]
            self.assertLess(lo, hi, item["event"])
            self.assertGreaterEqual(lo, 5)
            self.assertLessEqual(hi, 180)

    def test_night_slots_are_covered(self):
        night = [item for item in lm.MATERIALS if "凌晨" in item["slots"]]
        self.assertGreaterEqual(len(night), 2)


if __name__ == "__main__":
    unittest.main()
